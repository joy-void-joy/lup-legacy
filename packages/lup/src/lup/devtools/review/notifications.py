"""Tell a review's requester how it was answered, and remember whether that landed.

The answer is authority and the notification is courtesy: an answer stands
whether or not its requester hears of it, so the outcome of telling them is
persisted apart from the relay, bound to the exact answer it reports.
"""

from collections.abc import Callable
from datetime import datetime
import logging
import shlex
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ValidationError

from lup.channels.models import Door, publish_atomic, utc_now
from lup.coordination.bare import store as roster
from lup.coordination.bare.store import subagent_id
from lup.coordination.repository import RepositoryPeers
from lup.coordination.roster import RosterMember
from lup.coordination.peers import USER_ADDRESS
from lup.coordination.watch import roused
from lup.devtools.review.thread import Remark
from lup.devtools.review.wait import ReviewWaiters
from lup.policy.relay import Answer, LineComment, PersistentQuestion


class ReviewNotification(BaseModel, frozen=True):
    """How the requester hears of an answer, apart from the answer itself.

    One channel: ``waited`` where its `review wait` held the review and
    carries the news itself, else ``queued`` in its mailbox and ``woken``
    where its runtime accepted the wake. ``copied`` where the session a
    subagent runs in got a copy of the operator's words. ``detail`` says
    which, as the operator reads it.
    """

    queued: bool
    woken: bool
    detail: str
    waited: bool = False
    copied: bool = False


class ReviewNotificationRecord(BaseModel, frozen=True):
    """An outcome bound to the exact answer, without storing notification text."""

    question: str
    fingerprint: str
    answered_at: datetime
    attempted_at: datetime
    notification: ReviewNotification


class ReviewNotificationAttempt(BaseModel, frozen=True):
    """The pending outcome and any failure to persist it before responding."""

    notification: ReviewNotification
    persistence_error: str = ""


class ReviewNotifications(BaseModel, frozen=True):
    """One checkout's durable notification diagnostics; these grant no authority."""

    root: Path

    def path(self, entry: PersistentQuestion) -> Path:
        identity = uuid5(NAMESPACE_URL, entry.id)
        return self.root / ".lup" / "review-notifications" / f"{identity}.json"

    def read(self, entry: PersistentQuestion) -> ReviewNotification | None:
        path = self.path(entry)
        if entry.answer is None or not path.is_file():
            return None
        try:
            record = ReviewNotificationRecord.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as error:
            return ReviewNotification(
                queued=False,
                woken=False,
                detail=f"Notification diagnostics are unreadable ({type(error).__name__}); delivery is unconfirmed.",
            )
        if (
            record.question != entry.id
            or record.fingerprint != entry.fingerprint
            or record.answered_at != entry.answer.at
        ):
            return None
        return record.notification

    def write(self, entry: PersistentQuestion, outcome: ReviewNotification) -> None:
        if entry.answer is None:
            raise ValueError("A notification requires a recorded answer")
        publish_atomic(
            self.path(entry),
            ReviewNotificationRecord(
                question=entry.id,
                fingerprint=entry.fingerprint,
                answered_at=entry.answer.at,
                attempted_at=utc_now(),
                notification=outcome,
            ),
        )

    def prepare(self, entry: PersistentQuestion) -> ReviewNotificationAttempt:
        """Record an honest pending outcome before delivery is scheduled."""
        pending = ReviewNotification(
            queued=False,
            woken=False,
            detail="Decision recorded; notification attempt has no confirmed outcome.",
        )
        try:
            self.write(entry, pending)
        except OSError as error:
            persistence_error = (
                f"Initial notification diagnostics could not be persisted: {error}"
            )
            return ReviewNotificationAttempt(
                notification=pending.model_copy(
                    update={"detail": f"{pending.detail} {persistence_error}"}
                ),
                persistence_error=persistence_error,
            )
        return ReviewNotificationAttempt(notification=pending)

    def complete(
        self,
        entry: PersistentQuestion,
        attempt: ReviewNotificationAttempt,
        deliver: Callable[[], ReviewNotification],
    ) -> ReviewNotification:
        """Keep failed delivery visible without undoing the captured answer."""
        try:
            outcome = deliver()
        except Exception as error:
            outcome = ReviewNotification(
                queued=False,
                woken=False,
                detail=f"Decision recorded; notification failed: {error}",
            )
        if attempt.persistence_error:
            outcome = outcome.model_copy(
                update={"detail": f"{outcome.detail} {attempt.persistence_error}"}
            )
        try:
            self.write(entry, outcome)
        except OSError as error:
            logging.getLogger(__name__).warning(
                "Final review notification diagnostics could not be persisted: %s",
                error,
            )
            return outcome.model_copy(
                update={
                    "detail": f"{outcome.detail} Final notification diagnostics could not be persisted: {error}"
                }
            )
        return outcome

    def notify(
        self,
        roots: tuple[Path, ...],
        entry: PersistentQuestion,
        deliver: Callable[[tuple[Path, ...], PersistentQuestion], ReviewNotification],
    ) -> ReviewNotification:
        """Prepare and complete delivery synchronously for non-HTTP callers."""
        return self.complete(entry, self.prepare(entry), lambda: deliver(roots, entry))


class Mailed(BaseModel, frozen=True):
    """What came of mailing one member: queued, woken, and how to say it."""

    queued: bool
    woken: bool
    said: str


class ReviewRecipient(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One member a review's news goes to, the repository that reaches it, and its name."""

    peers: RepositoryPeers
    member: RosterMember
    name: str

    def mailed(self, message: str) -> Mailed:
        """Queue *message* in this member's mailbox, signed as the operator's, and wake it.

        The wake carries every message waiting for the member whole, and a
        wake its runtime accepted hands them over
        (:func:`~lup.coordination.watch.roused`), so its own hook does not hand
        the same words over a second time. A subagent has no wake of its own:
        its hook hands them over before its next tool call.
        """
        member = self.member
        sent = self.peers.send(
            member.address, message, door=Door.PAGE, sender=USER_ADDRESS
        )
        if sent is None:
            return Mailed(queued=False, woken=False, said=f"{self.name} left")
        if member.parent:
            return Mailed(
                queued=True,
                woken=False,
                said=f"in {self.name}'s mailbox, handed over before its next tool call",
            )
        try:
            nudged = roused(
                self.peers,
                member,
                self.peers.waiting(member.actor.id).messages,
                Path(member.worktree) if member.worktree else None,
            )
        except Exception as error:
            return Mailed(
                queued=True,
                woken=False,
                said=f"in {self.name}'s mailbox, but waking it failed: {error}",
            )
        if nudged.reached:
            return Mailed(
                queued=True, woken=True, said=f"sent to {self.name}, which was woken"
            )
        return Mailed(
            queued=True,
            woken=False,
            said=f"in {self.name}'s mailbox, not woken: {nudged.reason}",
        )


class ReviewRecipients(BaseModel, frozen=True):
    """Who a review's news reaches: the conversation that asked, and the session it runs in.

    ``requester`` is the live row that handles the call -- a subagent's own
    where one asked and is still running, else its session's. ``parent`` is
    that session where a subagent asked: it hears the operator's words too,
    as a copy, since anything past the call -- the workflow, the policy, what
    to change next -- is its to decide.
    """

    requester: ReviewRecipient | None = None
    parent: ReviewRecipient | None = None

    @classmethod
    def of(
        cls, roots: tuple[Path, ...], entry: PersistentQuestion
    ) -> "ReviewRecipients":
        rosters = {
            peers.root: peers for peers in (RepositoryPeers(root) for root in roots)
        }
        identities = {entry.operation.requester, entry.operation.session}
        subagent = subagent_id(entry.member, entry.agent) if entry.agent else ""
        running = [
            (peers, member)
            for peers in rosters.values()
            for member in peers.present()
            if member.running
        ]

        def named(peers: RepositoryPeers, member: RosterMember) -> ReviewRecipient:
            called = roster.called(peers.root)
            return ReviewRecipient(
                peers=peers,
                member=member,
                name=called[member.actor.id]
                if member.actor.id in called
                else member.actor.id,
            )

        sessions = [
            (peers, member)
            for peers, member in running
            if not member.parent
            and (
                entry.resumption != "native_retry"
                or not entry.operation.session
                or member.wake.session == entry.operation.session
            )
            and (
                member.actor.id in identities
                or bool(member.wake.session and member.wake.session in identities)
            )
        ]
        session = named(*sessions[0]) if len(sessions) == 1 else None
        subagents = [
            named(peers, member)
            for peers, member in running
            if subagent and member.actor.id == subagent
        ]
        if subagents:
            return cls(requester=subagents[0], parent=session)
        return cls(requester=session)


def waiting_command(root: Path, entry: PersistentQuestion) -> str:
    """The `review wait` that carries out or reports one review, runnable from anywhere."""
    return shlex.join(
        [
            "uv",
            "run",
            "--directory",
            str(root),
            "lup-devtools",
            "review",
            "wait",
            entry.id,
        ]
    )


def spoken(note: str, comments: list[LineComment], root: Path) -> str:
    """The operator's note and line comments, as lines the requester reads."""
    listed = [f"\n  {comment.spelled(root)}" for comment in comments]
    return "".join(
        [
            *([f"\nOperator note: {note}"] if note else []),
            *(["\nLine comments:", *listed] if listed else []),
        ]
    )


def answered_message(root: Path, entry: PersistentQuestion) -> str:
    """What the session that asked is told of how its review settled, and what to do about it.

    An approved native call is carried out by `review wait`, which the
    session was told to start when the call was parked; the message names the
    command again for a session that never started it, rather than asking
    for a retry that a running waiter might already have made unnecessary.
    """
    answer = entry.answer
    wait = waiting_command(root, entry)
    match entry.state, entry.resumption:
        case "approved", "native_retry":
            state = "approved"
            instruction = (
                f"`{wait}` carries it out and reports what it did; start it "
                "if it is not already running."
            )
        case "rejected", _:
            state = "declined"
            instruction = (
                "Don't retry the call as it stands: change course, or ask the user."
            )
        case "stale", _:
            state = "retired as stale"
            named = ", ".join(str(path) for path in entry.moved)
            instruction = (
                f"{named} changed since it was recorded, so no approval could "
                "release it. Re-read it and ask again."
            )
        case state, _:
            instruction = "Read the recorded decision before continuing."
    words = (
        spoken(answer.note, answer.comments, entry.operation.worktree)
        if answer is not None
        else ""
    )
    return (
        f"Review {entry.id} in {entry.operation.worktree} was {state}. "
        f"{instruction}{words}"
    )


def remarked_message(root: Path, entry: PersistentQuestion, remark: Remark) -> str:
    """What the session that asked is told of a remark: the words, and that nothing was decided."""
    return (
        f"The operator commented on review {entry.id} in "
        f"{entry.operation.worktree} without deciding it; it is still pending."
        + spoken(remark.note, remark.comments, entry.operation.worktree)
        + f"\nReply on the review with `uv run lup-devtools review reply {entry.id} "
        f"<text>`, or cancel it and ask again; `{waiting_command(root, entry)}` "
        "waits on it again."
    )


def copied_message(entry: PersistentQuestion, requester: str, message: str) -> str:
    """The copy a subagent's session gets of what the operator said to the subagent."""
    return (
        f"[copy] The operator's words on review {entry.id}, which your subagent "
        f"{requester} asked. It handles the call itself; this copy is for "
        "anything past the call — the workflow, the policy, what to change "
        f"next.\n{message}"
    )


def notify_requester(
    roots: tuple[Path, ...],
    root: Path,
    entry: PersistentQuestion,
    remark: Remark | None = None,
) -> ReviewNotification:
    """Tell the conversation that asked what came of its review, by one channel.

    Where its `review wait` holds the review, that waiter is the channel: it
    wakes and reports the answer, the remark or the staleness itself, so
    nothing is mailed beside it and the session is not put the same words
    twice. Otherwise the words go to the requester's mailbox, signed as the
    operator's, and its wake route is tried. Where a subagent asked and the
    operator said something -- a note, line comments, a remark -- the session
    it runs in gets a copy either way; a bare approval pings nobody but the
    waiter.
    """
    answer = entry.answer
    match remark, answer:
        case Remark(at=said), _:
            words = True
            message = remarked_message(root, entry, remark)
        case None, Answer(at=said):
            words = bool(answer.note) or bool(answer.comments)
            message = answered_message(root, entry)
        case _:
            said = None
            words = False
            message = answered_message(root, entry)
    recipients = ReviewRecipients.of(roots, entry)

    def reached() -> ReviewNotification:
        """How the requester hears of it: its waiter, its mailbox, or nobody running."""
        if ReviewWaiters(root=root).carries(entry.id, said):
            return ReviewNotification(
                queued=False,
                woken=False,
                waited=True,
                detail="Its `review wait` holds it and tells the session now.",
            )
        if recipients.requester is None:
            return ReviewNotification(
                queued=False,
                woken=False,
                detail=(
                    "No `review wait` holds it and no running session matches the "
                    "one that asked; the next `review wait` it starts reports it."
                ),
            )
        mailed = recipients.requester.mailed(message)
        return ReviewNotification(
            queued=mailed.queued,
            woken=mailed.woken,
            detail=f"No `review wait` holds it: {mailed.said}.",
        )

    told = reached()
    parent = recipients.parent
    if not words or parent is None or recipients.requester is None:
        return told
    copied = parent.mailed(copied_message(entry, recipients.requester.name, message))
    return told.model_copy(
        update={
            "copied": copied.queued,
            "detail": f"{told.detail} A copy for {parent.name}: {copied.said}.",
        }
    )
