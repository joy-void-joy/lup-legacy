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

What the operator does to them is :mod:`lup.devtools.dashboard.supervision`'s.
"""

import os
from collections import deque
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Literal
from pathlib import Path, PurePath

from pydantic import BaseModel, TypeAdapter, ValidationError

from lup.coordination.bare import holds as bare_holds
from lup.coordination.bare import mail as bare_mail
from lup.coordination.bare import store
from lup.coordination.holds import Hold, held_calls
from lup.coordination.bare.runtime import Runtime, process_scope, runtime_alive
from lup.coordination.mail import (
    MAIL_BLOCK,
    MAIL_PAGE,
    ActorMail,
    MailCursor,
    PostedMessage,
    backward,
)
from lup.coordination.peers import USER_ADDRESS, user_peer
from lup.coordination.repository import PeerView, RepositoryPeers
from lup.coordination.roster import RosterMember
from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.dashboard.pulse import PulseSession
from lup.policy.relay import QuestionRecord
from lup.providers.transcripts import native_turn, subagent_transcript
from lup.sessions.events import TurnBlock
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

RECENT_CALLS = 12
"""How many of a conversation's latest calls its row carries."""

TRANSCRIPT_PAGE = 50
"""How many entries one page of a transcript holds where a reader names no count."""

SUMMARY_ARGUMENTS = (
    "description",
    "command",
    "file_path",
    "path",
    "pattern",
    "query",
    "url",
    "prompt",
)
"""The arguments a call is summed up by, the first one it carries."""

type CallState = Literal["ok", "error", "pending"]
"""Whether a call was answered, answered with an error, or not yet answered."""

type Feature = Literal[
    "reply-thread",
    "redirect",
    "interrupt",
    "bare-wake",
    "rename",
    "stop",
    "transcript",
    "notices",
    "describe",
    "claims",
    "inbox-read",
    "thread-post",
    "pause",
]
"""A piece of supervision the page can ask this server for, as the page names it."""


class SeenCall(BaseModel, frozen=True):
    """One call a conversation made, and whether it has been answered."""

    call: str
    tool: str
    summary: str = ""
    """What the call was about, from its own arguments: its description, its
    command or its path, whole."""

    at: datetime | None = None
    state: CallState = "pending"


def call_summary(
    arguments: JsonObject, names: tuple[str, ...] = SUMMARY_ARGUMENTS
) -> str:
    """What one call was about, in its own words: the first of *names* it carries as text."""
    return next(
        (
            value
            for name in names
            if name in arguments and isinstance(value := arguments[name], str) and value
        ),
        "",
    )


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

    recent: list[SeenCall] = []
    """Its latest calls, oldest first, each answered, failed, or waiting."""


class SessionProcess(BaseModel, frozen=True):
    """The runtime process one row answers for, and whether this dashboard could stop it."""

    pid: int
    started: str
    """Its start time as the kernel counts it, which with the id names one process."""

    here: bool
    """Whether it runs in this dashboard's pid namespace, where its id means this process."""

    stoppable: bool
    why: str = ""
    """Why this dashboard could not stop it, empty where it could."""


def session_process(
    member: RosterMember, rows: dict[str, RosterMember], scope: str
) -> SessionProcess | None:
    """The runtime process one row answers for, and whether a reader in *scope* could stop it.

    Only where the reader shares the process's pid namespace does its id name
    that process, and only while the process its row recorded — the same id
    and start time — still runs is a stop a stop of that session. A subagent
    runs inside its session's process, so it carries its session's, which
    stopping would stop the session with it. Nothing where no process was
    recorded.
    """
    if member.parent:
        session = rows[member.parent] if member.parent in rows else None
        found = session_process(session, rows, scope) if session is not None else None
        return (
            found.model_copy(
                update={
                    "stoppable": False,
                    "why": "a subagent runs inside its session's process, so "
                    "stopping it means stopping its session",
                }
            )
            if found is not None
            else None
        )
    recorded = member.process
    if not recorded.pid:
        return None
    here = bool(scope) and recorded.scope == scope
    alive = runtime_alive(
        Runtime(pid=recorded.pid, started=recorded.started, scope=recorded.scope),
        scope,
    )
    match (member.running, here, alive):
        case (False, _, _):
            why = "it has stopped"
        case (_, False, _):
            why = (
                "its runtime runs in another pid namespace than the dashboard's "
                "— a contained session — so only the launcher holding its "
                "container can stop it"
            )
        case (_, _, True):
            why = ""
        case _:
            why = "the process its row recorded is no longer running"
    return SessionProcess(
        pid=recorded.pid,
        started=recorded.started,
        here=here,
        stoppable=not why,
        why=why,
    )


class HeldBy(BaseModel, frozen=True):
    """One hold over a session or subagent, as its row says why it waits."""

    reason: str
    """Why: `paused` for the operator's pause, or the budget's own reason."""

    owner: str
    """Who placed it, and so who lifts it: `operator` or `budget`."""

    scope: str
    """Whom it covers: `self`, `agent`, `tree` or `repository`."""

    on: str = ""
    """The member it was placed on, by id -- this one, its session, an
    ancestor -- empty for a whole repository."""

    said: str
    """What the row, and a call refused at the hold's limit, read."""

    since: datetime | None = None
    until: datetime | None = None
    """When it lifts by itself, empty where only its owner lifts it."""

    freeze: bool = False
    """Whether the operator froze it too: its commands stopped and its turn interrupted."""


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

    runtime: str = ""
    """The runtime it runs in — a subagent in its session's — empty where nothing says."""

    spawned_by: str = ""
    """The session whose shell started its runtime, by member id, empty for any other."""

    process: SessionProcess | None = None
    """Its runtime process, where its row recorded one; a subagent's is its session's."""

    arrived: datetime | None = None
    heard: datetime | None = None
    summary: str = ""
    error: str = ""
    waiting: int = 0
    """How many messages sit in its mailbox, not yet handed over."""

    holds: list[HeldBy] = []
    """What keeps its next tool call waiting, the operator's pause first;
    empty for one nothing holds."""

    held_since: datetime | None = None
    """When its hook began holding the oldest call it holds now, empty where
    it holds none: a held agent that is not mid-call is idle, paused."""

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
    """The post it answers, empty where it answers none."""

    post: str = ""
    """Shared by every copy one send left; a message whose record carries none
    stands for its own post by its id."""

    thread: str = ""
    """The post its thread began with, its own where it began one; empty where
    the record carries none."""

    sent_at: datetime
    waiting: bool
    """Whether it still sits in the recipient's mailbox, not yet handed over."""

    prompt: bool = False
    """A bare prompt its recipient's runtime was woken with -- a resume's
    "continue" -- which the page shows as a prompt rather than a message."""

    def mailbox(self) -> str:
        """The mailbox it was put in, as the store names it."""
        return store.conversation_of(
            store.Actor(kind=self.recipient_kind, id=self.recipient)
        )


class LiveNotice(BaseModel, frozen=True):
    """One fact standing over a repository's sessions, read at the head of every prompt."""

    id: str
    text: str
    by: str = ""
    """Who stated it: a member's id, `user` for the person, or empty."""

    door: str
    posted_at: datetime


class UserRow(BaseModel, frozen=True):
    """The person's own row in one repository, as every agent there reads it."""

    key: str
    """The repository's key and `user`, as a session's key is its repository's and its id."""

    repository: str
    description: str = ""
    """What they say they are on, empty until they have said."""

    holding: list[str] = []
    """What they hold, each spelled as a claim is: `under <path>` for a lock."""

    contested: list[str] = []
    """What of it another session holds too."""

    notices: list[LiveNotice] = []
    """Every notice standing in the repository, oldest first; theirs say `user`."""

    unread: int = 0
    """How many messages to them still wait in their mailbox."""


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
        post=posted.message.post or posted.message.id,
        thread=posted.message.thread,
        sent_at=posted.message.sent_at,
        prompt=posted.message.prompt,
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

    Mutable and owned by one reader: it keeps the byte it stopped at, the
    calls still waiting for an answer and the latest *recent* calls with what
    became of each, and starts again from the recent end where the file was
    replaced or cut short.
    """

    def __init__(
        self, path: Path, tail: int = TRANSCRIPT_TAIL_BYTES, recent: int = RECENT_CALLS
    ) -> None:
        self.path = path
        self.tail = tail
        self.recent = recent
        self.offset: int | None = None
        self.inode = -1
        self.outstanding: dict[str, Outstanding] = {}
        self.seen: dict[str, SeenCall] = {}
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
            self.seen.clear()
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
                        if answered in self.seen:
                            self.seen[answered] = self.seen[answered].model_copy(
                                update={"state": "error" if block.refusal else "ok"}
                            )
                    if (call := block.invoked_call_id) is not None:
                        self.outstanding[call] = Outstanding(
                            tool=block.tool_call_name or "",
                            arguments=block.tool_arguments or {},
                        )
                        self.seen[call] = SeenCall(
                            call=call,
                            tool=block.tool_call_name or "",
                            summary=call_summary(block.tool_arguments or {}),
                            at=turn.at,
                        )
                self.seen = dict(list(self.seen.items())[-self.recent :])
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
            recent=list(self.seen.values()),
        )


type EntryKind = Literal["text", "call", "result"]
"""What one entry of a transcript is: words, a call made, or what a call returned."""


class TranscriptEntry(BaseModel, frozen=True):
    """One thing a transcript recorded: words said, a call made, or what a call returned.

    Placed by the byte its line starts at and its place among that line's
    blocks, which nothing later moves, so a reader holding entries from one
    read and from a frame after it keeps each once.
    """

    at: int
    block: int = 0
    time: datetime | None = None
    role: str
    """Who it is from: `user`, `assistant`, `tool` or `system`."""

    kind: EntryKind
    text: str = ""
    """The words, whole: what was said, or what a call returned."""

    tool: str = ""
    arguments: JsonObject = {}
    call: str = ""
    """The call it makes or answers."""

    error: bool = False
    """Whether the call it answers came back an error."""


def turn_entries(line: bytes, at: int) -> list[TranscriptEntry]:
    """What one transcript line recorded, as entries; none where it records no turn.

    Each block is asked what it carries — prose, a call it makes, a call it
    answers — so a kind of block a runtime adds later is read by the same
    questions, and one that answers none of them is passed over.
    """
    try:
        record = JSON_OBJECT.validate_json(line)
    except ValidationError:
        return []
    turn = native_turn(record)
    if turn is None:
        return []
    role = turn.message.role

    def entry(index: int, block: TurnBlock) -> TranscriptEntry | None:
        if (call := block.invoked_call_id) is not None:
            return TranscriptEntry(
                at=at,
                block=index,
                time=turn.at,
                role=role,
                kind="call",
                text=call_summary(block.tool_arguments or {}),
                tool=block.tool_call_name or "",
                arguments=block.tool_arguments or {},
                call=call,
            )
        if (answered := block.answered_call_id) is not None:
            return TranscriptEntry(
                at=at,
                block=index,
                time=turn.at,
                role=role,
                kind="result",
                text=block.answer_payload or "",
                call=answered,
                error=block.refusal is not None,
            )
        if text := block.text_payload:
            return TranscriptEntry(
                at=at, block=index, time=turn.at, role=role, kind="text", text=text
            )
        return None

    return [
        found
        for index, block in enumerate(turn.message.blocks)
        if (found := entry(index, block)) is not None
    ]


class TranscriptPage(BaseModel, frozen=True):
    """One run of a transcript's entries, oldest first, and where it sits in the file."""

    session: str
    """The session or subagent it is the transcript of, by the key the stream gives it."""

    entries: list[TranscriptEntry]
    earlier: int
    """Where this run starts: the next page is read before here, and 0 is none."""

    end: int
    """The byte just past this run's last whole line, which a follower reads on from."""


class ReadLine(BaseModel, frozen=True):
    """One whole line of a transcript as a page read it: where it sits, and its entries."""

    start: int
    end: int
    entries: list[TranscriptEntry]


def transcript_page(
    session: str,
    path: Path,
    before: int | None = None,
    limit: int = TRANSCRIPT_PAGE,
    block: int = MAIL_BLOCK,
) -> TranscriptPage:
    """The latest *limit* entries of a transcript whose lines end by byte *before*.

    Read back from *before* — the file's end where it is not named — a block
    at a time, as the mail record is paged, so a transcript of hundreds of
    megabytes costs the lines its page sits in. Whole lines only: one the
    runtime is still writing waits for the next read. A line's entries are
    kept whole, so a page can hold a few more than *limit*.
    """
    if limit < 1:
        raise ValueError(f"a page holds at least one entry, not {limit}")
    try:
        transcript = path.open("rb")
    except OSError:
        return TranscriptPage(session=session, entries=[], earlier=0, end=0)
    more = False

    def within(end: int) -> Iterator[ReadLine]:
        nonlocal more
        held = 0
        for line in backward(transcript, end, 0, block):
            if held >= limit:
                more = True
                return
            found = turn_entries(line.content, line.start)
            held += len(found)
            yield ReadLine(start=line.start, end=line.end(), entries=found)

    with transcript:
        size = os.fstat(transcript.fileno()).st_size
        end = size if before is None else min(before, size)
        read = list(within(end))
    return TranscriptPage(
        session=session,
        entries=[entry for line in reversed(read) for entry in line.entries],
        earlier=read[-1].start if more and read else 0,
        end=read[0].end if read else end,
    )


class TranscriptTail:
    """One transcript read on from a byte, for a tab following it; mutable, owned by the feed."""

    def __init__(self, path: Path, offset: int) -> None:
        self.path = path
        self.offset = offset

    def fresh(self) -> list[TranscriptEntry]:
        """The entries of every whole line appended since the last read.

        Read from the start again where the file is now shorter than where
        this stopped, which is a transcript begun again rather than one cut.
        """
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            self.offset = 0

        def appended() -> Iterator[TranscriptEntry]:
            with self.path.open("rb") as transcript:
                transcript.seek(self.offset)
                for line in transcript:
                    if not line.endswith(b"\n"):
                        return
                    at = self.offset
                    self.offset += len(line)
                    yield from turn_entries(line, at)

        return list(appended()) if size > self.offset else []


class Answering(BaseModel, frozen=True):
    """One running session, and every id it or a subagent of its answers to."""

    session: PulseSession
    answers: list[str] = []
    """Its roster id, its runtime's ids for it, and each running subagent's own."""


class RepositoryNeeds(BaseModel, frozen=True):
    """What one repository's running sessions need of the operator, as every status line says it."""

    sessions: list[Answering] = []
    """Its running sessions, before the reviews each parked are counted in."""

    quiet: int = 0
    """Its agents with a call outstanding and nothing new in their transcript
    for the window asked, none of whose subagents runs."""

    contested: list[str] = []
    """The paths two of its sessions hold at once, a session and its
    subagents counted as one."""

    unread: int = 0
    """Messages its agents sent the operator that still wait in its mailbox."""

    held: int = 0
    """Its running agents the operator's pause or a budget holds at their next call."""

    def asker(self, question: QuestionRecord) -> str:
        """The running session here that parked *question*, by its roster id; empty where none did.

        The session its launch named first, then the runtime's ids it asked
        under, as :class:`~lup.devtools.review.app.RequesterPresence` reads them.
        """
        operation = question.operation
        asked = [question.member, operation.session, operation.requester]
        return next(
            (
                row.session.id
                for each in asked
                if each
                for row in self.sessions
                if each in row.answers
            ),
            "",
        )


def held_rows(root: Path) -> dict[str, list[HeldBy]]:
    """Every member something holds, with each hold as its row says it, the operator's first.

    A row reads frozen only where the freeze reached it -- its own session's
    commands stopped -- and paused where the freeze could not.
    """
    here = bare_holds.roster(root)
    return {
        member: [
            HeldBy(
                reason=hold.reason.value,
                owner=hold.owner.value,
                scope=hold.scope.value,
                on=hold.member,
                said=hold.said,
                since=hold.placed,
                until=hold.until,
                freeze=hold.froze(
                    member,
                    store.parent_of(here[member]) if member in here else "",
                ),
            )
            for record in records
            for hold in [Hold.read(record)]
            if hold is not None
        ]
        for member, records in bare_holds.covered(root).items()
    }


def held_since(root: Path) -> dict[str, datetime]:
    """When each member's hook began holding the oldest call it holds now.

    Read newest first, so the oldest of each member's calls is the one kept.
    """
    return {
        call.member: call.since
        for call in reversed(held_calls(root))
        if call.since is not None
    }


def transcript_path(view: PeerView, rows: dict[str, PeerView]) -> Path | None:
    """The transcript one row's conversation is written to, where one is known.

    A subagent's is kept beside its session's, where the session's runtime
    keeps it; *rows* is every row by member id, the session among them.
    """
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


def member_transcript(known: KnownRepository, member_id: str) -> Path:
    """The transcript of one session or subagent of *known*, by its member id.

    Refused for an id no row here answers to, and for a row whose transcript
    nothing has recorded.
    """
    rows = {
        view.member.actor.id: view for view in RepositoryPeers(known.checkout).recent()
    }
    if member_id not in rows:
        raise LookupError(f"no session of {known.name()} has the id {member_id!r}")
    found = transcript_path(rows[member_id], rows)
    if found is None:
        raise LookupError(
            f"{rows[member_id].address} has no transcript on record: its runtime "
            "has not named one to the roster yet"
        )
    return found


def user_row(repository: str, peers: RepositoryPeers) -> UserRow:
    """The person's own row in one repository: what they say they are on, hold, and are told."""
    person = peers.person()
    return UserRow(
        key=f"{repository}/{USER_ADDRESS}",
        repository=repository,
        description=person.member.description,
        holding=person.holding,
        contested=person.contested,
        notices=[
            LiveNotice(
                id=notice.id,
                text=notice.text,
                by=notice.by,
                door=str(notice.door),
                posted_at=notice.posted_at,
            )
            for notice in ActorMail(peers.root).standing()
        ],
        unread=len(bare_mail.waiting(peers.root, user_peer().conversation())),
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
        self.person: UserRow | None = None
        self.person_signature: list[FileStamp] = []
        self.person_read_at = 0.0

    def roster(self, now: float) -> list[PeerView]:
        """Every session here and those that stopped lately, read again only where it moved."""
        signature = [
            *stamps(self.peers.root / store.MEMBERS_DIR),
            *stamps(self.peers.root / store.DEPARTED_DIR),
            *stamps(self.peers.root / store.HOLDS_DIR),
            *stamps(self.peers.root / store.HOLDS_DIR / store.WAITING_DIR),
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

    def user(self, now: float) -> UserRow:
        """The person's row here, read again where the roster was, or a notice or their mail moved."""
        self.roster(now)
        signature = [
            *stamps(self.peers.root / store.NOTICES_DIR),
            *stamps(
                bare_mail.mailbox_path(self.peers.root, user_peer().conversation())
            ),
        ]
        if (
            self.person is not None
            and signature == self.person_signature
            and self.person_read_at == self.read_at
        ):
            return self.person
        read = user_row(self.key, self.peers)
        self.person = read
        self.person_signature = signature
        self.person_read_at = self.read_at
        return read

    def following(self, paths: list[Path]) -> dict[Path, TranscriptFollower]:
        """A follower for each transcript named, keeping the ones already reading."""
        return {
            path: self.followers[path]
            if path in self.followers
            else TranscriptFollower(path)
            for path in paths
        }

    def activities(self, views: list[PeerView]) -> dict[str, SessionActivity]:
        """What each running row's transcript says it is doing now, following those alone."""
        rows = {view.member.actor.id: view for view in views}
        followed = {
            member_id: path
            for member_id, view in rows.items()
            if view.member.running and (path := transcript_path(view, rows)) is not None
        }
        self.followers = self.following(list(followed.values()))
        return {
            member_id: self.followers[path].advance()
            for member_id, path in followed.items()
        }

    def sessions(self, now: float) -> list[LiveSession]:
        """Every session and subagent here, with what each is doing now."""
        views = self.roster(now)
        activity = self.activities(views)
        rows = {view.member.actor.id: view.member for view in views}
        scope = process_scope()
        held = held_rows(self.peers.root)
        calls = held_since(self.peers.root)
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
                runtime=(
                    rows[view.member.parent]
                    if view.member.parent in rows
                    else view.member
                ).wake.runtime,
                spawned_by=view.member.spawned_by,
                process=session_process(view.member, rows, scope),
                arrived=view.member.arrived,
                heard=view.member.heard,
                summary=view.member.summary,
                error=view.member.error,
                waiting=len(
                    bare_mail.waiting(self.peers.root, view.member.actor.conversation())
                ),
                holds=held[view.member.actor.id]
                if view.member.running and view.member.actor.id in held
                else [],
                held_since=calls[view.member.actor.id]
                if view.member.running and view.member.actor.id in calls
                else None,
                activity=activity[view.member.actor.id]
                if view.member.actor.id in activity
                else SessionActivity(),
            )
            for view in views
        ]

    def needs(self, now: float, moment: datetime, silent: timedelta) -> RepositoryNeeds:
        """What the running sessions here need of the operator at *moment*.

        An agent is quiet where a call of its has had no answer and nothing
        new has reached its transcript for *silent*, and no subagent of its
        runs: a session waiting on a subagent is waiting on that subagent,
        which answers for itself. A path is held twice where two sessions
        hold it, a subagent holding with its session's hand.
        """
        views = [view for view in self.roster(now) if view.member.running]
        activity = self.activities(views)
        parents = {view.member.parent for view in views if view.member.parent}

        def session_of(view: PeerView) -> str:
            return view.member.parent or view.member.actor.id

        def runtime(view: PeerView) -> list[str]:
            member = view.member
            transcript = PurePath(member.transcript).stem if member.transcript else ""
            return list(
                dict.fromkeys(
                    each for each in (member.wake.session, transcript) if each
                )
            )

        def answers(session: PeerView) -> list[str]:
            family = [
                view for view in views if session_of(view) == session.member.actor.id
            ]
            return [
                *[view.member.actor.id for view in family],
                *[each for view in family for each in runtime(view)],
                *[
                    store.agent_of(view.member.actor.id, view.member.parent)
                    for view in family
                    if view.member.parent
                ],
            ]

        held = {path for view in views for path in view.contested}
        paused = held_rows(self.peers.root)
        return RepositoryNeeds(
            sessions=[
                Answering(
                    session=PulseSession(
                        repository=str(self.known.repository),
                        project=self.known.name(),
                        id=view.member.actor.id,
                        name=view.cli_name,
                        worktree=view.member.worktree,
                        runtime=runtime(view),
                    ),
                    answers=answers(view),
                )
                for view in views
                if not view.member.parent
            ],
            quiet=sum(
                1
                for member_id, doing in activity.items()
                if doing.calling
                and member_id not in parents
                and doing.at is not None
                and moment - doing.at >= silent
            ),
            contested=sorted(
                path
                for path in held
                if len({session_of(view) for view in views if path in view.contested})
                > 1
            ),
            unread=len(bare_mail.waiting(self.peers.root, user_peer().conversation())),
            held=sum(1 for view in views if view.member.actor.id in paused),
        )

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
