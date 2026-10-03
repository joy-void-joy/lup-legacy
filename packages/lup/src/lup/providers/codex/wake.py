"""Codex's wake: a message handed to the queue of the daemon its home selects.

Codex's ``queue`` uses the daemon selected by its configuration home, so a
sender must share the target's proven execution scope and select that home.
Queue acceptance does not prove an idle turn started.
"""

from pathlib import Path

from lup.coordination.bare.scope import execution_scope
from lup.coordination.wake import WakePath, Woken
from lup.execution.shell import LazyCommand
from lup.providers.codex.login import CODEX_HOME

# lup: ignore[constant-declaration] — env overlays the target home while retaining native process discovery settings
QUEUE_COMMAND = LazyCommand("env")


def queued(
    path: WakePath, message: str, cwd: Path | None = None, timeout: float = 20.0
) -> Woken:
    """Hand a message to Codex's queue; reached means the queue accepted it.

    Acceptance does not establish that an idle session started a turn.
    """
    if not path.home or not Path(path.home).is_absolute() or not path.scope:
        return Woken(
            reached=False,
            reason="Codex wake has no verified target home and execution scope; durable mail remains pending.",
            error_type="UnboundNativeRoute",
        )
    if path.scope != execution_scope():
        # lup: Add an owned execution bridge before supporting Codex wake across container boundaries.
        return Woken(
            reached=False,
            reason="Direct Codex wake cannot cross this execution boundary; durable mail remains pending for the peer's owned mailbox relay or its next activity.",
            error_type="ForeignExecutionScope",
        )
    try:
        QUEUE_COMMAND(
            f"{CODEX_HOME}={path.home}",
            "codex",
            "queue",
            "--thread",
            path.handle,
            "--message",
            message,
            _cwd=str(cwd) if cwd else None,
            _timeout=timeout,
        )
    except Exception as failure:
        return Woken(
            reached=False,
            reason=f"codex queue did not reach {path.handle!r}: {failure}",
            error_type=type(failure).__name__,
        )
    return Woken(reached=True)
