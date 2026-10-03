"""Making a peer look, by the transport of the runtime its wake path names.

The one place a wake path's runtime is matched to the adapter that carries
it, as :mod:`lup.providers.identity` is for a session's identity, so neutral
code asks here and no runtime's transport is spelled outside its adapter.
"""

from pathlib import Path

from lup.coordination.wake import WakePath, WakePriority, Woken
from lup.providers.claude.wake import injected
from lup.providers.codex.wake import queued


def wake(
    path: WakePath,
    message: str,
    cwd: Path | None = None,
    *,
    queue_timeout_seconds: float = 20.0,
    priority: WakePriority = "next",
) -> Woken:
    """Make one member look at what is waiting.

    Never raises on a failed wake. The mail is already written by the time
    anything calls this, so a runtime that is missing, a session that has since
    exited, or a handle that no longer resolves all leave the record intact and
    the peer merely un-nudged — which is the state a member with no wake path
    is in permanently and which the system is built to tolerate.

    *priority* `now` interrupts a Claude turn that is generating; Codex's
    queue takes a message for its next turn whatever is asked.
    """
    match path.runtime:
        case "codex" if path.handle:
            return queued(path, message, cwd, queue_timeout_seconds)
        case "claude" if path.handle:
            return injected(Path(path.handle), message, path.session, priority=priority)
        case _:
            return Woken(
                reached=False,
                reason=(
                    "this member declared no wake path, so the mail waits until"
                    " it next looks"
                ),
            )
