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

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter, ValidationError

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

    def heading(self) -> str:
        """What its reader is told before the text: what it is, who sent it, through what."""
        kind = "redirected" if self.redirect else "message"
        signed = f" from {self.sender}" if self.sender else ""
        return f"[{kind}{signed} by {self.door}]"


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


POSTED = TypeAdapter(mail.Posted)


class MailCursor(BaseModel, frozen=True):
    """Where a reader of the mail record stopped: the byte it resumes at, and the line."""

    offset: int = 0
    seq: int = 0


class PostedMessage(BaseModel, frozen=True):
    """One message as the record keeps it: its line, and whose mailbox it was put in."""

    seq: int
    recipient: ActorRef
    message: ActorMessage


class RecordLine(BaseModel, frozen=True):
    """One whole line of the record, and the byte just past it."""

    end: int
    content: bytes


class MailPage(BaseModel, frozen=True):
    """What the record gained since a cursor, and the cursor to resume from."""

    messages: list[PostedMessage] = []
    cursor: MailCursor = MailCursor()


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
    ) -> ActorMessage:
        """Put one message in one member's mailbox, and say what was put there."""
        message = mail.new_message(
            sender=sender,
            to=to.label(),
            body=text,
            door=str(door),
            in_reply_to=in_reply_to,
            redirect=redirect,
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

    def posted(self, cursor: MailCursor) -> MailPage:
        """Every message the record gained since *cursor*, each at the line it sits on.

        Whole lines only: one a sender is still writing waits for the next
        read. A record shorter than the cursor was replaced, and is read again
        from its start. A line that will not parse keeps its number and
        yields nothing, so every other line keeps the number it always had.
        """
        path = self.root / store.MAIL_RECORD
        try:
            size = path.stat().st_size
        except OSError:
            return MailPage()
        start = cursor if size >= cursor.offset else MailCursor()

        def lines() -> Iterator[RecordLine]:
            with path.open("rb") as record:
                record.seek(start.offset)
                offset = start.offset
                for line in record:
                    if not line.endswith(b"\n"):
                        return
                    offset += len(line)
                    yield RecordLine(end=offset, content=line)

        read = list(lines())

        def messages() -> Iterator[PostedMessage]:
            for seq, line in enumerate(read, start=start.seq):
                try:
                    posted = POSTED.validate_json(line.content)
                except ValidationError:
                    continue
                recipient = posted.get("recipient") or store.Actor()
                if not store.actor_kind(recipient):
                    continue
                yield PostedMessage(
                    seq=seq,
                    recipient=ActorRef(
                        kind=store.actor_kind(recipient),
                        id=store.actor_id(recipient),
                        round=store.actor_round(recipient) or 1,
                    ),
                    message=folded_message(posted.get("message") or mail.Message()),
                )

        return MailPage(
            messages=list(messages()),
            cursor=MailCursor(
                offset=read[-1].end if read else start.offset,
                seq=start.seq + len(read),
            ),
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
