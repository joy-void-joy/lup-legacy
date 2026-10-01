"""Saying something to a member, and saying something that stays true.

Two acts, and separating them is the whole of this module. A **message** is
addressed and consumed: it goes in one member's mailbox and leaves when that
member reads it. A **notice** is neither: it is a fact about the population,
read at the head of every turn by whoever is there to read it, and retracted
by taking it down.

Neither is a question. Both ride files rather than a
:class:`~lup.channels.slot.Slot`, which has no unsettled state for anything
to wait on, so no amount of saying things can park a run. That split —
questions are slots, everything here is not — is what lets a caller volunteer
information to a working actor without stalling whoever volunteered it.

**"Everyone" is resolved by the sender.** A sender that means everyone asks
the roster who is live and posts one file each, so the store holds nothing
but messages with one recipient. A token every reader matched against
itself would force a delivery position per member: a message nobody had
addressed to you could still be yours, so you would have to remember how
far you had read, and a redirect would reach members spawned *after* the
stop, which is not a thing a stop can sensibly mean.

What that costs — a standing fact reaching a member that arrives later —
is what a notice is for, and a notice does it better: it is still there at
that member's first turn, and at every turn after, because it has not
stopped being true.
"""

import os
from collections.abc import Iterator
from datetime import datetime
from io import BytesIO
from itertools import chain, islice
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import BaseModel, ValidationError

from lup.channels.models import Door, utc_now
from lup.coordination.bare import mail
from lup.coordination.bare import store
from lup.coordination.refs import ActorRef


class ActorMessage(BaseModel, frozen=True):
    """One thing a door told one member. This never settles anything.

    ``redirect`` separates telling a member something from stopping it. An
    ordinary message rides in front of the member's next tool call and it
    keeps going; a redirect refuses that call and hands back the text as the
    reason, so the member cannot carry on with what it was doing without
    first reading why it was stopped.
    """

    id: str
    to_actor: str
    text: str
    door: Door
    sent_at: datetime
    sender: str = ""
    """Whoever said it, by the address a reply reaches: a member's id, or `user`.

    Empty where a door with no address of its own said it — a run's own
    orchestration steering its workers.
    """

    in_reply_to: str = ""
    redirect: bool = False
    post: str = ""
    """Shared by every copy one send left, one per recipient; empty on a record
    carrying none, whose own id stands for it."""

    thread: str = ""
    """The post its thread began with: its own where it began one."""

    title: str = ""
    """The discussion it was posted into, empty where it is a message to one."""

    participants: list[str] = []
    """Everyone else in that discussion, as its reader addresses each."""

    def heading(self) -> str:
        """What its reader is told before the text, as the delivery hook tells it."""
        return mail.heading(
            mail.Message(
                sender=self.sender,
                door=str(self.door),
                redirect=self.redirect,
                post=self.post,
                thread=self.thread,
                title=self.title,
                participants=self.participants,
            )
        )


class Posting(BaseModel, frozen=True):
    """Where one send sits among the posts, which every copy it leaves shares.

    Empty everywhere is an ordinary message: a post of its own, beginning its
    own thread. A reply names the thread it answers into; a discussion's post
    also carries its title and who else is in it, which its reader is told.
    """

    post: str = ""
    """The id every copy shares; empty mints one."""

    thread: str = ""
    """The post the thread began with; empty begins one at this post."""

    title: str = ""
    participants: list[str] = []


class StandingNotice(BaseModel, frozen=True):
    """One fact about this population, true until somebody takes it down.

    It has no recipient and is never consumed, which is what lets a member
    that did not exist when it was posted read it at the head of its first
    turn. A member that has read it reads it again, because it has not
    stopped being true — and because nothing remembering otherwise is
    exactly the bookkeeping this shape exists to do without.
    """

    id: str
    text: str
    door: Door
    posted_at: datetime
    by: str = ""


class ActorDelivery(BaseModel, frozen=True):
    """What one member has waiting, and what consuming it would consume.

    The messages themselves are the handle: consuming is deleting the files
    they came from, so a caller that read and then committed cannot commit
    past something it never saw. An offset could, skipping a message posted
    between reading and handing over.
    """

    messages: list[ActorMessage]

    def redirects(self) -> list[ActorMessage]:
        return [message for message in self.messages if message.redirect]


class MailEventBase(BaseModel, frozen=True):
    """One thing that happened to a member's mail, answering about itself.

    A reader asks the event rather than testing which one it is holding, so a
    third thing that can happen to a message — expired, forwarded, refused by
    a closed door — is one class rather than an edit to every fold that would
    otherwise have to notice it and would not.

    What separates the kinds is whether the member took the message, which is
    the one fact a reader cannot infer and the one a sender most needs: a
    sender is told a message was sent on the strength of the mailbox accepting
    it, which is not the same as anybody having read it.
    """

    text: str
    door: str
    redirect: bool = False

    @property
    def delivered(self) -> bool:
        """Whether the member took this, or it only ever reached its mailbox."""
        raise NotImplementedError


class MessagePostedEvent(MailEventBase, frozen=True):
    """A door volunteered something to a member, or a member replied.

    An intervention belongs in the record beside what it interrupted. A
    reader scrolling one member's trace sees the moment someone redirected
    it, in order, against what it was doing — which is the difference between
    a trace and an audit filed somewhere else.
    """

    type: Literal["message_posted"] = "message_posted"
    in_reply_to: str | None = None

    @property
    def delivered(self) -> bool:
        """Posted is handed over: this record is written where it lands."""
        return True


class MessageOutstandingEvent(MailEventBase, frozen=True):
    """A message still in a member's mailbox as its session is being closed.

    Recorded because the sender was told the message was sent, and the mailbox
    alone cannot say whether anyone read it. On a park this is a message that
    will land at the head of the resumed turn; on a run that ended it is one
    that reached nobody, and a redirect nobody read is the failure of an
    operation somebody performed to stop something.
    """

    type: Literal["message_outstanding"] = "message_outstanding"

    @property
    def delivered(self) -> bool:
        """Queued and not handed over, which is the whole point of the record."""
        return False


type MailEvent = MessagePostedEvent | MessageOutstandingEvent
"""What the actor layer itself records, which any consumer's journal admits."""


def folded_message(message: mail.Message) -> ActorMessage:
    """One message file, as a typed caller reads it."""
    return ActorMessage(
        id=store.text(message.get("id")),
        to_actor=store.text(message.get("to")),
        text=store.text(message.get("text")),
        door=Door(store.text(message.get("door")) or Door.AGENT),
        sent_at=store.spoken_at(store.text(message.get("sent_at"))) or utc_now(),
        sender=store.text(message.get("sender")),
        in_reply_to=store.text(message.get("in_reply_to")),
        redirect=bool(message.get("redirect")),
        post=store.text(message.get("post")),
        thread=store.text(message.get("thread")),
        title=store.text(message.get("title")),
        participants=[
            each
            for each in (message.get("participants") or [])
            if isinstance(each, str)
        ],
    )


def folded_notice(notice: mail.Notice) -> StandingNotice:
    """One notice file, as a typed caller reads it."""
    return StandingNotice(
        id=store.text(notice.get("id")),
        text=store.text(notice.get("text")),
        door=Door(store.text(notice.get("door")) or Door.AGENT),
        posted_at=store.spoken_at(store.text(notice.get("posted_at"))) or utc_now(),
        by=store.text(notice.get("by")),
    )


MAIL_PAGE = 100
"""How many messages one page of the mail record holds where a reader names no count.

Enough for the latest exchange between a repository's sessions to read whole;
a reader wanting what came before pages back for it.
"""

MAIL_BLOCK = 1 << 16
"""How many bytes of the mail record one read takes, paging back from a byte.

A page costs the blocks its messages sit in, however long the record behind
them is: the record is kept whole, and nothing reads it all at once.
"""


class MailCursor(BaseModel, frozen=True):
    """Where a reader following the mail record stopped: the byte it resumes at, in which file."""

    offset: int = 0
    inode: int = 0
    """Which file the offset is into, so a record deleted and begun again is not read from there."""


class PostedMessage(BaseModel, frozen=True):
    """One message as the record keeps it: where its line starts, and whose mailbox it was put in."""

    at: int
    """The byte its line starts at, which nothing later moves: a page older than it is read before here."""

    recipient: ActorRef
    message: ActorMessage


class Discussion(BaseModel, frozen=True):
    """One thread as the record holds it: who is in it, what it is called, and where it stands."""

    thread: str
    """The post it began with."""

    title: str
    """Its first post's first line."""

    participants: list[str]
    """Everyone who wrote in it or was written to, by the address a reply
    reaches — a member's id, or `user` — in the order each first appears."""

    last: str
    """Its latest post, which a post naming nothing else answers."""


def first_line(said: str) -> str:
    """The first line of what somebody said that says anything."""
    return next((line.strip() for line in said.splitlines() if line.strip()), "")


class RecordedLine(BaseModel, frozen=True):
    """One line of the mail record as it parses: the member it went to, and the message.

    Both default to empty, so a line naming neither — the count an older
    sweep wrote at the head of a record whose head it cut — parses and
    records no message.
    """

    recipient: store.Actor = store.Actor()
    message: mail.Message = mail.Message()


class RecordLine(BaseModel, frozen=True):
    """One whole line of the record, newline included, and the byte it starts at."""

    start: int
    content: bytes

    def end(self) -> int:
        """The byte just past this line, where the next one starts."""
        return self.start + len(self.content)

    def posted(self) -> PostedMessage | None:
        """The message this line records, or nothing where it records none.

        Nothing for a line that will not parse or names no recipient, so
        every other line reads as it always did.
        """
        try:
            recorded = RecordedLine.model_validate_json(self.content)
        except ValidationError:
            return None
        kind = store.actor_kind(recorded.recipient)
        if not kind:
            return None
        return PostedMessage(
            at=self.start,
            recipient=ActorRef(
                kind=kind,
                id=store.actor_id(recorded.recipient),
                round=store.actor_round(recorded.recipient) or 1,
            ),
            message=folded_message(recorded.message),
        )


class MailPage(BaseModel, frozen=True):
    """One run of the mail record: its messages oldest first, where it starts, and where it ends."""

    messages: list[PostedMessage] = []
    start: int = 0
    """The byte the run starts at: what is older ends here, and 0 is a run nothing older precedes."""

    cursor: MailCursor = MailCursor()
    """Just past the run's last whole line, which is where following the record resumes."""


def backward(
    record: BinaryIO, end: int, floor: int, block: int
) -> Iterator[RecordLine]:
    """Every whole line between byte *floor* and byte *end*, latest first.

    Read a block at a time from *end* back, so a caller that stops early has
    read only the blocks it stopped in. *floor* is where a line starts; what
    follows the last newline before *end* is not a whole line — a sender part
    way through one, or a byte named in the middle of one — and is passed over.
    Lines are framed by :class:`io.BytesIO`, which ends one at each newline
    and nowhere else.
    """
    held = b""
    trailing = True
    for stop in range(end, floor, -block):
        begin = max(floor, stop - block)
        record.seek(begin)
        held = record.read(stop - begin) + held
        if trailing:
            last = held.rfind(b"\n")
            held = held[: last + 1]
            trailing = last == -1
        pieces = list(BytesIO(held))
        whole = pieces if begin == floor else pieces[1:]
        position = begin + len(held)
        for content in reversed(whole):
            position -= len(content)
            yield RecordLine(start=position, content=content)
        held = pieces[0] if pieces and begin != floor else b""


def paged(record: BinaryIO, end: int, floor: int, limit: int, block: int) -> MailPage:
    """The latest *limit* messages between byte *floor* and byte *end*, and where that run starts.

    The run starts at *floor* where every message past it is on the page,
    and at its oldest message's line where an older one was left for the
    next page — so a reader knows from the start alone whether anything
    lies between the floor and what it was handed.
    """
    if limit < 1:
        raise ValueError(f"a page holds at least one message, not {limit}")
    inode = os.fstat(record.fileno()).st_ino
    lines = backward(record, end, floor, block)
    latest = next(lines, None)
    if latest is None:
        return MailPage(start=floor, cursor=MailCursor(offset=floor, inode=inode))
    messages = (line.posted() for line in chain([latest], lines))
    found = list(islice((each for each in messages if each is not None), limit + 1))
    kept = found[:limit]
    return MailPage(
        messages=kept[::-1],
        start=kept[-1].at if len(found) > limit else floor,
        cursor=MailCursor(offset=latest.end(), inode=inode),
    )


class ActorMail:
    """Every member's mailbox, and the notices standing over all of them.

    Holds nothing and remembers nothing: a door posting, a console peeking and
    a member reading all reach the same directories, so none of them has to be
    running for the others to work.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def send(
        self,
        to: ActorRef,
        text: str,
        door: Door = Door.AGENT,
        sender: str = "",
        in_reply_to: str = "",
        redirect: bool = False,
        posting: Posting = Posting(),
    ) -> ActorMessage:
        """Put one message in one member's mailbox, and say what was put there."""
        message = mail.new_message(
            sender=sender,
            to=to.label(),
            body=text,
            door=str(door),
            in_reply_to=in_reply_to,
            redirect=redirect,
            post=posting.post,
            thread=posting.thread,
            title=posting.title,
            participants=tuple(posting.participants),
        )
        mail.post(
            self.root, store.Actor(kind=to.kind, id=to.id, round=to.round), message
        )
        return folded_message(message)

    def waiting(self, actor: ActorRef) -> ActorDelivery:
        """Everything in this member's mailbox, consuming none of it.

        Reading is separated from consuming so that asking what a member has
        waiting — which is how a sender learns whether anything was read —
        cannot itself be what makes it disappear.
        """
        return ActorDelivery(
            messages=[
                folded_message(message)
                for message in mail.waiting(self.root, actor.conversation())
            ]
        )

    def delivered(self, actor: ActorRef, delivery: ActorDelivery) -> None:
        """Record that this member has been handed exactly these messages.

        By deleting them, so what was handed over is what leaves the mailbox and
        nothing between the read and the commit is consumed unseen.
        """
        mail.consume(
            self.root,
            actor.conversation(),
            [mail.Message(id=message.id) for message in delivery.messages],
        )

    def posted(
        self, cursor: MailCursor, limit: int = MAIL_PAGE, block: int = MAIL_BLOCK
    ) -> MailPage:
        """The latest *limit* messages the record gained since *cursor*, oldest first.

        Read from the record's end back to the cursor, so what it costs is
        what was posted since, never the record. Whole lines only: one a
        sender is still writing waits for the next read. A cursor into no
        file of this name — a reader that has read nothing, or a record
        deleted and begun again — reads back as far as the record's start,
        which is its latest page. Where more than *limit* were posted since,
        the page starts past the cursor, and what lies between is read with
        :meth:`earlier`.
        """
        try:
            record = (self.root / store.MAIL_RECORD).open("rb")
        except OSError:
            return MailPage()
        with record:
            found = os.fstat(record.fileno())
            following = cursor.inode == found.st_ino and cursor.offset <= found.st_size
            floor = cursor.offset if following else 0
            return paged(record, found.st_size, floor, limit, block)

    def earlier(
        self, before: int, limit: int = MAIL_PAGE, block: int = MAIL_BLOCK
    ) -> MailPage:
        """The latest *limit* messages whose lines end by byte *before*, oldest first.

        What a reader holding a page asks for next, naming where that page
        starts; the page it is handed starts at 0 once nothing older is left.
        """
        try:
            record = (self.root / store.MAIL_RECORD).open("rb")
        except OSError:
            return MailPage()
        with record:
            end = min(before, os.fstat(record.fileno()).st_size)
            return paged(record, end, 0, limit, block)

    def latest_first(self, block: int = MAIL_BLOCK) -> Iterator[PostedMessage]:
        """Every message on the record, latest first, read back a block at a time.

        A caller that stops early has read only the blocks it stopped in, so
        a reader looking for something recent never reads the record whole.
        """
        try:
            record = (self.root / store.MAIL_RECORD).open("rb")
        except OSError:
            return
        with record:
            end = os.fstat(record.fileno()).st_size
            for line in backward(record, end, 0, block):
                if (posted := line.posted()) is not None:
                    yield posted

    def found(self, post: str) -> PostedMessage | None:
        """The latest copy on the record of one post — by its post id, or a message's own id."""
        if not post:
            return None
        return next(
            (
                posted
                for posted in self.latest_first()
                if post in (posted.message.post, posted.message.id)
            ),
            None,
        )

    def discussion(self, thread: str) -> "Discussion | None":
        """Everyone in one thread, what it is called, and its latest post; nothing where no post began it.

        Read back from the record's end to the post the thread began with,
        which is the oldest of it, so a thread costs the record since it
        began however long the record behind it: every copy of that first
        post is read, one per mailbox it was put in, and nothing older.
        """

        def beginning(posted: PostedMessage) -> bool:
            return thread in (posted.message.post, posted.message.id)

        def back_to_its_start() -> Iterator[PostedMessage]:
            reached = False
            for posted in self.latest_first():
                if reached and not beginning(posted):
                    return
                reached = reached or beginning(posted)
                yield posted

        held = [
            posted
            for posted in back_to_its_start()
            if thread in (posted.message.thread, posted.message.post, posted.message.id)
        ]
        if not held or not beginning(held[-1]):
            return None
        oldest_first = held[::-1]
        return Discussion(
            thread=thread,
            title=first_line(held[-1].message.text) or thread,
            participants=list(
                dict.fromkeys(
                    each
                    for posted in oldest_first
                    for each in (posted.message.sender, posted.recipient.id)
                    if each
                )
            ),
            last=held[0].message.post or held[0].message.id,
        )

    def standing(self) -> list[StandingNotice]:
        """Every fact standing over this population, oldest first."""
        return [folded_notice(notice) for notice in mail.notices(self.root)]

    def notify(
        self, text: str, door: Door = Door.AGENT, by: str = ""
    ) -> StandingNotice:
        """Post one standing fact, and say what was posted.

        Posting only. Telling whoever is already working is the caller's, and
        it is a separate act: this is what makes the fact true for the members
        that arrive next, and a message is what makes it heard by the ones
        mid-turn now.
        """
        notice = mail.new_notice(body=text, door=str(door), by=by)
        mail.post_notice(self.root, notice)
        return folded_notice(notice)

    def retract(self, notice_id: str) -> bool:
        """Take one standing fact down, saying whether it was there to take down."""
        return mail.retract(self.root, notice_id)
