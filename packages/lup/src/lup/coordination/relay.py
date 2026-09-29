"""Relay a session's pending mail through its own native queue.

The stdio coordination server owns this companion; an in-process registration
without companion lifecycle does not provide it. The server reads its own
hook-bound route, so mail shared across containers needs no remote execution.
What the native queue accepted is handed over then, as any wake's is
(:func:`~lup.coordination.watch.roused`): the session's delivery hook hands
over only what no wake carried.

Delivery is at-least-once: a crash after queue acceptance but before the
hand-over repeats a nudge, a direct sender or external watcher can race this
relay, and a hook can consume the mail between the relay's snapshot and its
queue call. Queue acceptance does not prove that an idle model took a turn
or read the mail, and a thread replaced after it accepted takes that mail
with it; the mail record keeps what was said either way.
"""

import asyncio
import fcntl
import hashlib
import logging
from pathlib import Path

from pydantic import Field

from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import Woken
from lup.coordination.watch import roused
from lup.tools.mcp import ServerCompanion

logger = logging.getLogger(__name__)


class MailboxRelay(ServerCompanion, frozen=True):
    """One stdio server's receiver, bounded by the server's own lifetime.

    Every instance for the same member shares one file lock, so the several
    servers one runtime can start for a session queue a message once between
    them.
    """

    root: Path
    member_id: str
    interval_seconds: float = Field(default=2.0, gt=0)
    queue_timeout_seconds: float = Field(default=20.0, gt=0)
    """Native request deadline, matching the account readiness probe's default."""

    @property
    def lock_path(self) -> Path:
        """The lock every relay for this member takes around one batch."""
        identity = hashlib.sha256(self.member_id.encode()).hexdigest()
        return RepositoryPeers(self.root).root / "wake-relay" / f"{identity}.lock"

    def tick(self) -> Woken | None:
        """Queue what waits for this member, handing it over once the queue accepts it.

        Nothing where nothing waits, or another relay holds the batch; a
        failed queue leaves the mail waiting for the next tick to retry.
        """
        peers = RepositoryPeers(self.root)
        row = peers.row(self.member_id)
        if row is None or not row.running or not row.wake.receiver_local:
            return None
        lock = self.lock_path
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a", encoding="utf-8") as held:
            try:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            # Binding and delivery can move before the lock is acquired.
            row = peers.row(self.member_id)
            if row is None or not row.running or not row.wake.receiver_local:
                return None
            waiting = peers.waiting(self.member_id).messages
            if not waiting:
                return None
            return roused(
                peers,
                row,
                waiting,
                self.root,
                queue_timeout_seconds=self.queue_timeout_seconds,
            )

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
                detail = f"{current}; check lock {self.lock_path} and store permissions"
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
