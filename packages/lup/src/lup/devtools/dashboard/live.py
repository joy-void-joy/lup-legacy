"""What every session the dashboard serves is doing, what it said, and what it was told.

Read off what already exists, as it changes: each repository's roster for who
is here, what each says it is doing and what its calls hold; the transcript a
row names for what it last said and the call it is waiting on; the mail
record for what was said between them. Nothing here is written for the
dashboard's sake, and nothing is re-read that has not changed — a roster is
read again when a member's file moves (and every few seconds, since a
runtime can stop without writing), a transcript from the byte it was last
read to, the mail record from its cursor. The mail record is kept whole, so
it is read from its end: its latest page first, and an older page only when
the page asks for one.

A reply from the operator goes the way a session's own message to a peer
goes — into the recipient's mailbox, then a wake through its wake socket or
`codex queue` — signed `user`, which is the address the session answers.
"""

from collections import deque
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from lup.channels.models import Door
from lup.coordination.bare import mail as bare_mail
from lup.coordination.bare import store
from lup.coordination.mail import MAIL_PAGE, ActorMail, MailCursor, PostedMessage
from lup.coordination.peers import USER_ADDRESS
from lup.coordination.repository import PeerDepartedError, PeerView, RepositoryPeers
from lup.coordination.roster import Delivery
from lup.coordination.watch import roused
from lup.devtools.dashboard.companion import KnownRepository
from lup.providers.transcripts import native_turn, subagent_transcript
from lup.types import JsonObject

ROSTER_REFRESH_SECONDS = 5.0
"""How long a roster whose files have not moved is trusted before it is read again.

A runtime can stop, and a claimed path can change under its holder, without
any member file moving; this bounds how long the page says otherwise.
"""

TRANSCRIPT_TAIL_BYTES = 1 << 20
"""How much of a transcript's end is read the first time it is followed.

What a session is doing now is at the end of its transcript, and a
transcript runs to hundreds of megabytes; everything after the first read is
only what was appended since.
"""

JSON_OBJECT = TypeAdapter(JsonObject)


class SessionActivity(BaseModel, frozen=True):
    """What one conversation's transcript says it is doing now."""

    said: str = ""
    """The last thing it said in its own words, whole."""

    calling: str = ""
    """The tool it called and has had no answer from yet, empty between calls."""

    arguments: JsonObject = {}
    """What that call was made with."""

    at: datetime | None = None
    """When its transcript last recorded it saying or doing anything."""

    transcript: str = ""
    """The file this was read from, empty where the row names none."""


class LiveSession(BaseModel, frozen=True):
    """One session or subagent of one repository, as the page lists it.

    Addressed by its member id, which every verb accepts and no rename
    changes; ``key`` qualifies it by its repository, since ids are minted
    per repository.
    """

    key: str
    repository: str
    """The repository's key, as :class:`KnownRepository` names it."""

    id: str
    parent: str = ""
    """The session this is a subagent of, empty for a session."""

    kind: str
    name: str = ""
    doing: str = ""
    task: str = ""
    running: bool
    worktree: str = ""
    holding: list[str] = []
    contested: list[str] = []
    delivery: str = ""
    wake: str = ""
    """The runtime whose wake path it declared, empty where nothing can wake it."""

    arrived: datetime | None = None
    heard: datetime | None = None
    summary: str = ""
    error: str = ""
    waiting: int = 0
    """How many messages sit in its mailbox, not yet handed over."""

    activity: SessionActivity = SessionActivity()


class LiveMessage(BaseModel, frozen=True):
    """One message of one repository's mail record, and whether it still waits."""

    key: str
    repository: str
    id: str
    at: int
    """The byte its line starts at on the repository's mail record, which nothing later moves."""

    sender: str
    """Who signed it: a member's id, `user`, or empty for a door with no address."""

    recipient: str
    """The member id whose mailbox it was put in."""

    recipient_kind: str
    text: str
    door: str
    redirect: bool
    in_reply_to: str
    sent_at: datetime
    waiting: bool
    """Whether it still sits in the recipient's mailbox, not yet handed over."""

    def mailbox(self) -> str:
        """The mailbox it was put in, as the store names it."""
        return store.conversation_of(
            store.Actor(kind=self.recipient_kind, id=self.recipient)
        )


class MailExtent(BaseModel, frozen=True):
    """Where the stream's messages of one repository start on its mail record."""

    repository: str
    """The repository's key, as :class:`KnownRepository` names it."""

    earlier: int
    """The byte its messages on the stream start at: what came before is read a
    page at a time from here back, and 0 is a repository whose every message is
    on the stream."""


class MessagePage(BaseModel, frozen=True):
    """One page of a repository's mail record, older than what the page already held."""

    messages: list[LiveMessage]
    """Oldest first, each with whether it still waits in the mailbox it was put in."""

    earlier: int
    """Where this page starts on the record: the next page is read before here, and 0 is none."""


def live_message(repository: str, root: Path, posted: PostedMessage) -> LiveMessage:
    """One message of a repository's mail record, with whether it still waits for its reader.

    *repository* is the repository's key, and *root* its coordination store.
    """
    return LiveMessage(
        key=f"{repository}/{posted.message.id}",
        repository=repository,
        id=posted.message.id,
        at=posted.at,
        sender=posted.message.sender,
        recipient=posted.recipient.id,
        recipient_kind=posted.recipient.kind,
        text=posted.message.text,
        door=str(posted.message.door),
        redirect=posted.message.redirect,
        in_reply_to=posted.message.in_reply_to,
        sent_at=posted.message.sent_at,
        waiting=bare_mail.message_path(
            root, posted.recipient.conversation(), posted.message.id
        ).is_file(),
    )


def earlier_messages(
    known: KnownRepository, before: int, limit: int = MAIL_PAGE
) -> MessagePage:
    """The page of one repository's mail record whose lines end by byte *before*.

    What the page asks for once it holds a page starting at *before*: read
    back from there a block at a time, never the record whole.
    """
    root = RepositoryPeers(known.checkout).root
    page = ActorMail(root).earlier(before, limit)
    return MessagePage(
        messages=[live_message(known.key(), root, posted) for posted in page.messages],
        earlier=page.start,
    )


class LiveRepository(BaseModel, frozen=True):
    """One repository the dashboard serves, by the key every address under it carries."""

    key: str
    name: str
    repository: str
    checkout: str

    @classmethod
    def of(cls, known: KnownRepository) -> "LiveRepository":
        return cls(
            key=known.key(),
            name=known.name(),
            repository=str(known.repository),
            checkout=str(known.checkout),
        )


class FileStamp(BaseModel, frozen=True):
    """One file of a directory as a stat leaves it; a write changes it."""

    name: str
    modified: int
    size: int


def stamps(directory: Path) -> list[FileStamp]:
    """Every file of one directory, stamped, in name order; empty where it is absent."""
    try:
        paths = sorted(directory.iterdir())
    except OSError:
        return []

    def stamped(path: Path) -> FileStamp | None:
        try:
            status = path.stat()
        except OSError:
            return None
        return FileStamp(
            name=path.name, modified=status.st_mtime_ns, size=status.st_size
        )

    return [stamp for path in paths if (stamp := stamped(path)) is not None]


class Outstanding(BaseModel, frozen=True):
    """One call a conversation made that nothing has answered yet."""

    tool: str
    arguments: JsonObject = {}


class TranscriptFollower:
    """One transcript, followed from where it was last read, folded into what is happening.

    Mutable and owned by one reader: it keeps the byte it stopped at and the
    calls still waiting for an answer, and starts again from the recent end
    where the file was replaced or cut short.
    """

    def __init__(self, path: Path, tail: int = TRANSCRIPT_TAIL_BYTES) -> None:
        self.path = path
        self.tail = tail
        self.offset: int | None = None
        self.inode = -1
        self.outstanding: dict[str, Outstanding] = {}
        self.activity = SessionActivity(transcript=str(path))

    def advance(self) -> SessionActivity:
        """Read whatever whole lines were appended since, and say what it is doing now."""
        try:
            status = self.path.stat()
        except OSError:
            return self.activity
        if (
            self.offset is None
            or status.st_ino != self.inode
            or status.st_size < self.offset
        ):
            self.inode = status.st_ino
            self.outstanding.clear()
            self.activity = SessionActivity(transcript=str(self.path))
            start = max(0, status.st_size - self.tail)
            self.offset = self.aligned(start) if start else 0
        if status.st_size == self.offset:
            return self.activity
        with self.path.open("rb") as transcript:
            transcript.seek(self.offset)
            for line in transcript:
                if not line.endswith(b"\n"):
                    break
                self.offset += len(line)
                self.fold(line)
        return self.activity

    def aligned(self, offset: int) -> int:
        """The first byte of the first whole line at or after *offset*."""
        with self.path.open("rb") as transcript:
            transcript.seek(offset - 1)
            passed = transcript.readline()
        return offset - 1 + len(passed)

    def fold(self, line: bytes) -> None:
        """What one line changes about what the conversation is doing."""
        try:
            record = JSON_OBJECT.validate_json(line)
        except ValidationError:
            return
        turn = native_turn(record)
        if turn is None:
            return
        said = self.activity.said
        match turn.message.role:
            case "user":
                # A new prompt: whatever was outstanding belongs to a turn that ended.
                self.outstanding.clear()
            case "assistant" | "tool":
                for block in turn.message.blocks:
                    if (answered := block.answered_call_id) is not None:
                        self.outstanding.pop(answered, None)
                    if (call := block.invoked_call_id) is not None:
                        self.outstanding[call] = Outstanding(
                            tool=block.tool_call_name or "",
                            arguments=block.tool_arguments or {},
                        )
                spoken = [
                    text
                    for block in turn.message.blocks
                    if (text := block.text_payload)
                ]
                said = "\n\n".join(spoken) if spoken else said
            case _:
                return
        waiting = list(self.outstanding.values())
        self.activity = SessionActivity(
            said=said,
            calling=waiting[-1].tool if waiting else "",
            arguments=waiting[-1].arguments if waiting else {},
            at=turn.at or self.activity.at,
            transcript=str(self.path),
        )


class RepositoryWatch:
    """One repository's sessions and mail, read again only where something moved.

    Of the mail record it reads the latest *page* messages first, then what
    is posted after them, and keeps where the latest *page* of what it read
    start — which is where the stream's messages for this repository begin.

    Mutable and owned by the one producer that ticks it, from one thread at a
    time.
    """

    def __init__(
        self,
        known: KnownRepository,
        refresh: float = ROSTER_REFRESH_SECONDS,
        page: int = MAIL_PAGE,
    ) -> None:
        self.known = known
        self.key = known.key()
        self.peers = RepositoryPeers(known.checkout)
        self.mail = ActorMail(self.peers.root)
        self.refresh = refresh
        self.page = page
        self.cursor = MailCursor()
        self.start = 0
        self.read = 0
        self.latest: deque[int] = deque(maxlen=page)
        self.signature: list[FileStamp] | None = None
        self.read_at = 0.0
        self.rows: list[PeerView] = []
        self.followers: dict[Path, TranscriptFollower] = {}
        self.pending: dict[str, LiveMessage] = {}

    def roster(self, now: float) -> list[PeerView]:
        """Every session here and those that stopped lately, read again only where it moved."""
        signature = [
            *stamps(self.peers.root / store.MEMBERS_DIR),
            *stamps(self.peers.root / store.DEPARTED_DIR),
        ]
        if (
            self.signature is None
            or signature != self.signature
            or now - self.read_at >= self.refresh
        ):
            self.rows = self.peers.recent()
            self.signature = signature
            self.read_at = now
        return self.rows

    def transcript(self, view: PeerView, rows: dict[str, PeerView]) -> Path | None:
        """The transcript one row's conversation is written to, where one is known."""
        member = view.member
        if member.transcript:
            return Path(member.transcript)
        if not member.parent or member.parent not in rows:
            return None
        session = rows[member.parent].member.transcript
        if not session:
            return None
        return subagent_transcript(
            Path(session), store.agent_of(member.actor.id, member.parent)
        )

    def following(self, paths: list[Path]) -> dict[Path, TranscriptFollower]:
        """A follower for each transcript named, keeping the ones already reading."""
        return {
            path: self.followers[path]
            if path in self.followers
            else TranscriptFollower(path)
            for path in paths
        }

    def sessions(self, now: float) -> list[LiveSession]:
        """Every session and subagent here, with what each is doing now."""
        views = self.roster(now)
        rows = {view.member.actor.id: view for view in views}
        followed = {
            member_id: path
            for member_id, view in rows.items()
            if view.member.running and (path := self.transcript(view, rows)) is not None
        }
        self.followers = self.following(list(followed.values()))
        activity = {
            member_id: self.followers[path].advance()
            for member_id, path in followed.items()
        }
        return [
            LiveSession(
                key=f"{self.key}/{view.member.actor.id}",
                repository=self.key,
                id=view.member.actor.id,
                parent=view.member.parent,
                kind=view.member.actor.kind,
                name=view.cli_name,
                doing=view.doing,
                task=view.member.task,
                running=view.member.running,
                worktree=view.member.worktree,
                holding=view.holding,
                contested=view.contested,
                delivery=str(view.member.delivery),
                wake=view.member.wake.runtime,
                arrived=view.member.arrived,
                heard=view.member.heard,
                summary=view.member.summary,
                error=view.member.error,
                waiting=len(
                    bare_mail.waiting(self.peers.root, view.member.actor.conversation())
                ),
                activity=activity[view.member.actor.id]
                if view.member.actor.id in activity
                else SessionActivity(),
            )
            for view in views
        ]

    def waits(self, mailbox: str, message_id: str) -> bool:
        """Whether one message still sits in the mailbox it was put in."""
        return bare_mail.message_path(self.peers.root, mailbox, message_id).is_file()

    def extent(self) -> MailExtent:
        """Where on the mail record the stream's messages of this repository start.

        Where the latest page of what this watch read starts, once it has
        read more than a page; before that, where its first read started —
        0 where that read reached the record's start.
        """
        return MailExtent(
            repository=self.key,
            earlier=self.latest[0] if self.read > self.page else self.start,
        )

    def fresh_messages(self) -> list[LiveMessage]:
        """Every message the record gained since the last look, and every one taken since.

        The first look reads the latest page, and so does a look finding more
        posted since the last than a page holds, or the record begun again:
        what it reads then starts a run of its own, older than which the
        stream holds nothing.
        """
        page = self.mail.posted(self.cursor, self.page)
        if (page.start, page.cursor.inode) != (self.cursor.offset, self.cursor.inode):
            self.start, self.read = page.start, 0
            self.latest.clear()
        self.cursor = page.cursor
        arrived = [
            live_message(self.key, self.peers.root, posted) for posted in page.messages
        ]
        self.read += len(arrived)
        self.latest.extend(message.at for message in arrived)
        taken = [
            message
            for message in self.pending.values()
            if not self.waits(message.mailbox(), message.id)
        ]
        gone = {message.id for message in taken}
        self.pending = {
            message.id: message
            for message in [*self.pending.values(), *arrived]
            if message.waiting and message.id not in gone
        }
        return [
            *[message.model_copy(update={"waiting": False}) for message in taken],
            *arrived,
        ]


class ReplyRequest(BaseModel, frozen=True, extra="forbid"):
    """What the operator wrote to one session."""

    text: str = Field(min_length=1)


class ReplyOutcome(BaseModel, frozen=True):
    """What became of the operator's message: queued in the mailbox, and whether a wake landed."""

    session: str
    queued: bool
    woken: bool
    detail: str


def reply(known: KnownRepository, member_id: str, text: str) -> ReplyOutcome:
    """Send the operator's message to one session or subagent, by its member id, and wake it.

    The path a session's own `coordination_send` to a peer takes — the
    recipient's mailbox, where its hook hands it over at its next tool call —
    then its wake path, carrying everything waiting for it, so an idle
    session takes a turn; what a wake its runtime accepted carried is handed
    over with it, so the hook does not hand it over again. Refused for an id
    nothing here answers to, and for a session that has stopped.
    """
    peers = RepositoryPeers(known.checkout)
    row = peers.row(member_id)
    if row is None:
        raise LookupError(f"no session of {known.name()} has the id {member_id!r}")
    if not row.running:
        raise PeerDepartedError(row, peers.called(member_id))
    peers.send(member_id, text, door=Door.PAGE, sender=USER_ADDRESS)
    waiting = peers.waiting(member_id).messages
    woken = roused(peers, row, waiting, Path(row.worktree) if row.worktree else None)
    session = f"{known.key()}/{member_id}"
    if woken.reached:
        return ReplyOutcome(
            session=session,
            queued=True,
            woken=True,
            detail="Handed over with the wake its runtime accepted.",
        )
    if row.delivery == Delivery.HOOK:
        return ReplyOutcome(
            session=session,
            queued=True,
            woken=False,
            detail="Queued in its mailbox; it is handed over before its next tool call.",
        )
    return ReplyOutcome(
        session=session,
        queued=True,
        woken=False,
        detail=f"Queued in its mailbox. {woken.reason}",
    )
