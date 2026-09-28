"""Relay a session's pending mail through its own native queue.

The stdio coordination server owns this companion; an in-process registration
without companion lifecycle does not provide it. The server reads its own
hook-bound route, so mail shared across containers needs no remote execution.
Native acceptance is recorded separately from delivery and never consumes mail.

Delivery is at-least-once: a crash after queue acceptance but before receipt
publication can repeat a nudge. A direct sender or external watcher does not
share these receipts and can race this relay, and a hook can consume the mail
between the relay's snapshot and queue call. Neither duplicate notifications
nor queue acceptance prove that an idle model took a turn or read the mail.
"""

import asyncio
import fcntl
import hashlib
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from lup.channels.models import publish_atomic
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath, Woken, wake
from lup.coordination.watch import nudge_text
from lup.tools.mcp import ServerCompanion

logger = logging.getLogger(__name__)


class WakeReceipts(BaseModel, frozen=True):
    """Messages accepted by one native route, still distinct from mail delivery."""

    route: WakePath
    message_ids: list[str] = []


class MailboxRelay(ServerCompanion, frozen=True):
    """One stdio server's receiver, bounded by the server's own lifetime.

    Every instance for the same member shares a file lock and receipt. Receipt
    identities include the native session, home and execution scope through the
    full route, so pending mail can reach a session whose binding changed.
    """

    root: Path
    member_id: str
    interval_seconds: float = Field(default=2.0, gt=0)
    queue_timeout_seconds: float = Field(default=20.0, gt=0)
    """Native request deadline, matching the account readiness probe's default."""

    @property
    def receipt_path(self) -> Path:
        """The member's acceptance state; mail bodies never appear in this file."""
        identity = hashlib.sha256(self.member_id.encode()).hexdigest()
        return RepositoryPeers(self.root).root / "wake-relay" / f"{identity}.json"

    def tick(self) -> Woken | None:
        """Try one batch without acknowledging it; a failed queue remains retryable."""
        peers = RepositoryPeers(self.root)
        row = peers.row(self.member_id)
        if row is None or not row.running or not row.wake.receiver_local:
            return None
        receipt = self.receipt_path
        receipt.parent.mkdir(parents=True, exist_ok=True)
        with receipt.with_suffix(".lock").open("a", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            # Binding and delivery can move before the lock is acquired.
            row = peers.row(self.member_id)
            if row is None or not row.running or not row.wake.receiver_local:
                return None
            previous = (
                WakeReceipts.model_validate_json(receipt.read_text(encoding="utf-8"))
                if receipt.exists()
                else WakeReceipts(route=row.wake)
            )
            waiting = peers.waiting(self.member_id).messages
            accepted = [
                message.id
                for message in waiting
                if previous.route == row.wake and message.id in previous.message_ids
            ]
            fresh = [message for message in waiting if message.id not in accepted]
            if not fresh:
                if previous.message_ids != accepted:
                    publish_atomic(
                        receipt, WakeReceipts(route=row.wake, message_ids=accepted)
                    )
                return None
            outcome = wake(
                row.wake,
                nudge_text(fresh),
                self.root,
                queue_timeout_seconds=self.queue_timeout_seconds,
            )
            if outcome.reached:
                publish_atomic(
                    receipt,
                    WakeReceipts(
                        route=row.wake, message_ids=[message.id for message in waiting]
                    ),
                )
            return outcome

    async def run(self) -> None:
        """Serve until cancelled, joining any bounded native call before stopping."""
        problem = ""
        while True:
            attempt = asyncio.create_task(asyncio.to_thread(self.tick))
            try:
                outcome = await asyncio.shield(attempt)
            except asyncio.CancelledError:
                # Cancelling a thread's awaiter cannot stop its child process.
                # The native timeout bounds it, and joining retains ownership.
                await asyncio.gather(attempt, return_exceptions=True)
                raise
            except Exception as error:
                current = type(error).__name__
                detail = f"{current}; check receipt {self.receipt_path} and store permissions"
            else:
                current = (
                    outcome.error_type or "NativeQueueRejected"
                    if outcome is not None and not outcome.reached
                    else ""
                )
                detail = f"{current}; check the native session binding and queue availability"
            if current and current != problem:
                logger.warning(
                    "Mailbox relay for %s could not queue pending mail: %s. "
                    "Mail remains pending and the relay will retry.",
                    self.member_id,
                    detail,
                )
            problem = current
            await asyncio.sleep(self.interval_seconds)
