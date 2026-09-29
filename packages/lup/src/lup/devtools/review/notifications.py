"""Tell a review's requester how it was answered, and remember whether that landed.

The answer is authority and the notification is courtesy: an answer stands
whether or not its requester hears of it, so the outcome of telling them is
persisted apart from the relay, bound to the exact answer it reports.
"""

from collections.abc import Callable
from datetime import datetime
import logging
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ValidationError

from lup.channels.models import Door, publish_atomic, utc_now
from lup.coordination.repository import RepositoryPeers
from lup.coordination.roster import RosterMember
from lup.coordination.wake import wake
from lup.policy.relay import PersistentQuestion


class ReviewNotification(BaseModel, frozen=True):
    """Queue acceptance is separate from both approval and recipient delivery."""

    queued: bool
    woken: bool
    detail: str


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


class ReviewRecipient(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """The requesting member and the repository that can reach it."""

    peers: RepositoryPeers
    member: RosterMember


def notify_requester(
    roots: tuple[Path, ...], entry: PersistentQuestion
) -> ReviewNotification:
    """Queue the answer for its requester and try its declared wake route."""
    candidates = [RepositoryPeers(root) for root in roots]
    rosters = {peers.root: peers for peers in candidates}
    identities = {entry.operation.requester, entry.operation.session}
    members = [
        ReviewRecipient(peers=peers, member=member)
        for peers in rosters.values()
        for member in peers.present()
        if member.running
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
    if len(members) != 1:
        return ReviewNotification(
            queued=False,
            woken=False,
            detail="Decision recorded; no unique live requester is registered.",
        )
    recipient = members[0]
    member = recipient.member
    instruction = (
        "Retry the exact tool call; its preimages and policy are rechecked."
        if entry.state == "approved" and entry.resumption == "native_retry"
        else "Read the recorded decision before continuing."
    )
    message = (
        f"Review {entry.id} in {entry.operation.worktree} was {entry.state}. {instruction}\n"
        f"Operator note: {entry.answer.note if entry.answer else ''}"
    )
    delivered = recipient.peers.send(member.address, message, door=Door.PAGE)
    if delivered is None:
        return ReviewNotification(
            queued=False, woken=False, detail="Decision recorded; requester left."
        )
    try:
        nudged = wake(
            member.wake, message, Path(member.worktree) if member.worktree else None
        )
    except Exception as error:
        return ReviewNotification(
            queued=True,
            woken=False,
            detail=f"Decision recorded and notification queued; wake failed: {error}",
        )
    return ReviewNotification(
        queued=True,
        woken=nudged.reached,
        detail=(
            "Decision recorded and notification accepted by the requester runtime."
            if nudged.reached
            else f"Decision recorded and notification queued. {nudged.reason}"
        ),
    )
