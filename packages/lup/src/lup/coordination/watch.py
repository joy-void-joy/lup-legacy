"""Following a repository's sessions as they come, go, speak and are spoken to.

Everything coordination writes is an append-only file under the shared git
directory, and nothing pushes: a session that wants to know what changed folds
the files again. That is fine for a session, which folds on its own next call,
and no use to a person at a second terminal or to a process meant to wake
peers nobody is currently asking about. This is the fold, run on a clock, that
says only what is different from the last time it looked.

**Noticing consumes nothing.** Mail is read through the same non-consuming
path a console peeks with, so a watcher reporting that a message arrived is
not a watcher that stopped the peer ever seeing it. What it saw is kept by
identity rather than by position, because a position is the peer's own —
committed when the peer reads — and a second reader holding one would be the
two-cursors-over-one-stream bug this module's neighbours close.

**Waking is on top of the record, never instead of it.** Where asked to, the
watcher nudges a member that has new mail by whatever path that member
declared, and reports what happened in the runtime's own terms: reached,
left to a session holding the tool, or nothing declared. A nudge that fails
costs the peer latency and never the message. A nudge that reached carried
the mail whole, so that mail is handed over then — :func:`roused` — and the
peer's own hook hands over only what no nudge carried.
"""

import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from lup.channels.models import utc_now
from lup.coordination.mail import ActorDelivery, ActorMessage
from lup.coordination.repository import PeerView, RepositoryPeers
from lup.coordination.roster import RosterMember
from lup.coordination.wake import Woken
from lup.providers.wake import wake


class WatchEvent(BaseModel, frozen=True):
    """One thing that changed between two looks at the repository.

    Each variant renders itself, so a console prints lines and a run writes
    them without either knowing the vocabulary — and a new kind of change is a
    new variant with its own line rather than a branch somewhere else.
    """

    at: datetime
    address: str

    def line(self) -> str:
        """This event as one line a person or a follower reads."""
        return f"{self.address}: changed"


class Arrived(WatchEvent, frozen=True):
    """A session joined the roster, or was already there when watching began."""

    doing: str = ""

    def line(self) -> str:
        return f"{self.address} arrived" + (f" — {self.doing}" if self.doing else "")


class Departed(WatchEvent, frozen=True):
    """A session left, and what it said it concluded."""

    summary: str = ""

    def line(self) -> str:
        return f"{self.address} left" + (f" — {self.summary}" if self.summary else "")


class Redescribed(WatchEvent, frozen=True):
    """A session changed what it says it is doing."""

    doing: str

    def line(self) -> str:
        return f"{self.address} now: {self.doing}"


class Mailed(WatchEvent, frozen=True):
    """A message reached a member's mailbox and has not been consumed."""

    text: str
    door: str
    redirect: bool = False

    def line(self) -> str:
        kind = "redirect" if self.redirect else "message"
        return f"{self.address} ← {kind} by {self.door}: {self.text}"


class Nudged(WatchEvent, frozen=True):
    """What happened when the watcher tried to make a member look."""

    outcome: Woken

    def line(self) -> str:
        if self.outcome.reached:
            return f"{self.address} woken"
        return f"{self.address} not woken — {self.outcome.reason}"


def nudge_text(fresh: list[ActorMessage]) -> str:
    """What a woken peer is handed, carrying the mail rather than pointing at it.

    Whole rather than a pointer, because the nudge arrives *as a turn*: a peer
    told only that something is waiting spends that turn fetching what the
    nudge could have carried, and a peer told nothing but the text cannot tell
    mail from an instruction its operator typed. So the frame says what this
    is and the body is every fresh message verbatim, in the order they were
    sent.

    Every one of them, not the newest. A watcher looks on a clock and two
    messages posted between looks are equally new to a peer that has read
    neither; handing over the last would leave the first readable only to
    somebody who thought to fold their mailbox, which is the habit the nudge
    exists because idle peers do not have.

    The record is named at the end rather than the beginning: it is what the
    reader needs *after* deciding the mail matters, and a line repeated on
    every nudge ahead of the content is a line that stops being read.

    Each message names who sent it where it was signed — a peer's id, or
    `user` for the person — since that is the address a reply goes to.
    """

    def said_by(message: ActorMessage) -> str:
        signed = f"{message.sender} by " if message.sender else ""
        return f"from {signed}{message.door} —\n{message.text}"

    return "\n\n".join(
        [
            (
                f"{len(fresh)} message(s) waiting for you are on this"
                " repository's coordination record, copied here in full:"
            ),
            *[said_by(message) for message in fresh],
            (
                "This is a nudge on top of the record, not instead of it —"
                " these are handed over with it, and `coordination_mailbox`"
                " holds anything since."
            ),
        ]
    )


def roused(
    peers: RepositoryPeers,
    member: RosterMember,
    fresh: list[ActorMessage],
    cwd: Path | None = None,
    queue_timeout_seconds: float = 20.0,
) -> Woken:
    """Make one member look at *fresh*, and hand over what the wake carried.

    The wake carries the mail whole — :func:`nudge_text` — so a wake the
    member's runtime accepted has put it in front of the member, and it is
    taken out of the mailbox then, the way the member's own hook takes what
    it hands over: that hook, at the member's next tool call, hands over only
    what no wake carried. A wake that did not reach leaves every message
    waiting for it.

    Accepted is the most a runtime says: a frame its wake socket took, or a
    queue that took the message. Neither proves the turn it starts has read
    it, which is the same promise the hook's own hand-over makes.
    """
    outcome = wake(
        member.wake,
        nudge_text(fresh),
        cwd,
        queue_timeout_seconds=queue_timeout_seconds,
    )
    if outcome.reached:
        peers.delivered(member.actor.id, ActorDelivery(messages=fresh))
    return outcome


def mail_key(message: ActorMessage) -> str:
    """What makes one message the same message on the next look.

    The stream carries no per-message id, and a position belongs to the peer
    that consumes. Recipient, time and text together are what a reader would
    call the same message, and are enough to keep a watcher from reporting
    one twice or missing one posted in the same second as another.
    """
    return f"{message.to_actor}|{message.sent_at.isoformat()}|{message.text}"


class Watcher:
    """The diff between two looks at one repository's coordination store.

    Holds what it has already reported and nothing the store does not: a
    watcher restarted reports the live roster once as arrivals and the waiting
    mail once, which is a baseline rather than a replay — the same convention
    a run follower uses when attaching to work already under way.
    """

    def __init__(
        self,
        peers: RepositoryPeers,
        root: Path | None = None,
        member: str = "",
        nudge: bool = False,
    ) -> None:
        self.peers = peers
        self.root = root
        self.member = member
        self.nudge = nudge
        self.started = utc_now()
        """When this watcher began, which bounds the departures it is shown.

        A listing shows the live rows and the rows that stopped since a
        moment the reader names; a watcher names its own start, so a
        departure during the watch is a row it sees stop, and one before it
        is the history the baseline leaves out.
        """
        self.known: dict[str, PeerView] = {}
        self.seen: dict[str, ActorMessage] = {}
        """Every message already reported, under the identity it is known by.

        The message itself and not only its key, so what was reported can be
        read back — a watcher asked what it said about a peer has the record
        rather than a fingerprint of one.
        """

    def watched(self, view: PeerView) -> bool:
        """Whether this member's mail is this watcher's business."""
        return not self.member or self.member in (view.address, view.member.actor.id)

    def tick(self) -> list[WatchEvent]:
        """Look once, and say what is different from the last look."""
        now = datetime.now().astimezone()
        current = {
            view.member.actor.id: view
            for view in self.peers.listing(since=self.started)
        }

        def roster_changes() -> Iterator[WatchEvent]:
            for member_id, view in current.items():
                match (self.known.get(member_id), view):
                    case (None, PeerView(member=RosterMember(running=True))):
                        yield Arrived(at=now, address=view.address, doing=view.doing)
                    case (
                        PeerView(member=RosterMember(running=True)),
                        PeerView(member=RosterMember(running=False)),
                    ):
                        yield Departed(
                            at=now, address=view.address, summary=view.member.summary
                        )
                    case (PeerView(doing=was), PeerView(doing=doing)) if was != doing:
                        yield Redescribed(at=now, address=view.address, doing=doing)

        def mail_changes() -> Iterator[WatchEvent]:
            for member_id, view in current.items():
                if not self.watched(view) or not view.member.running:
                    continue
                fresh = [
                    message
                    for message in self.peers.waiting(member_id).messages
                    if mail_key(message) not in self.seen
                ]
                for message in fresh:
                    self.seen[mail_key(message)] = message
                    yield Mailed(
                        at=now,
                        address=view.address,
                        text=message.text,
                        door=str(message.door),
                        redirect=message.redirect,
                    )
                if fresh and self.nudge:
                    # One nudge per look, not one per message: a wake starts a
                    # turn, so two would interrupt the turn the first began.
                    outcome = roused(self.peers, view.member, fresh, self.root)
                    yield Nudged(at=now, address=view.address, outcome=outcome)

        events = [*roster_changes(), *mail_changes()]
        self.known = current
        return events

    def follow(
        self, interval: float = 2.0, until: Callable[[], bool] = lambda: False
    ) -> Iterator[WatchEvent]:
        """Look on a clock, yielding each change as it is found, until told to stop.

        ``until`` is checked after every look rather than before the first, so
        a watcher asked to stop once the roster is empty still reports the
        baseline it found — an empty roster included, by reporting nothing.
        """
        while True:
            yield from self.tick()
            if until():
                return
            time.sleep(interval)
