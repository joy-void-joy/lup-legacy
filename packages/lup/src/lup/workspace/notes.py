"""RO/RW notes directory structure for agent sessions.

Key patterns:
1. Explicit separation of RW (session can write) and RO (historical, read-only)
2. Session-specific directories prevent cross-session pollution
3. Logs directory is NOT accessible to agent (for feedback loop only) —
   the RO grant lists each version's ``sessions/`` and ``outputs/``
   explicitly so ``logs/`` is never readable
4. Permission hooks enforce the access control

Examples:
    Set up session directories and wire into permission hooks::

        >>> notes = setup_notes(session_id="12345", task_id="my-task")
        >>> notes.rw  # Agent can write here
        [PosixPath('.../sessions/12345'), PosixPath('.../outputs/my-task/...')]
        >>> notes.ro  # Agent can only read here — every version, never logs/
        [PosixPath('.../traces/0.1.0/sessions'), PosixPath('.../traces/0.1.0/outputs')]
"""

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from lup.observability.sessions import Session, SessionRecorder
from lup.workspace.user_directories import UserDirectories
from lup.workspace.paths import (
    TIMESTAMP_FMT,
    agent_version,
    outputs_dir,
    runtime_logs_path,
    sessions_dir,
    trace_logs_dir,
    traces_path,
)


class NotesConfig(BaseModel, arbitrary_types_allowed=True):
    """Notes folder configuration with explicit RW/RO separation."""

    session: Path = Field(description="This session's working directory")
    output: Path = Field(description="Where this session saves its outputs")
    trace_log: Path = Field(description="Trace log path (agent cannot access)")
    rw: list[Path] = Field(default=[], description="Read-write directories")
    ro: list[Path] = Field(default=[], description="Read-only directories")
    record: Session | None = Field(
        default=None,
        description="The ledger node pointing at this session, where one was recorded",
    )

    @property
    def all_dirs(self) -> list[Path]:
        """All directories the agent can access (RW + RO)."""
        return self.rw + self.ro


def collect_ro_dirs() -> list[Path]:
    """Read-only grants: ``sessions/`` and ``outputs/`` of every version.

    Iterates version directories under ``traces_path()`` and grants each
    version's ``sessions/`` and ``outputs/`` (the current version's are
    always included — they exist by the time this runs). Granting the
    version directories themselves would also expose ``logs/``, which is
    reserved for the feedback loop and must stay invisible to the agent.
    """
    candidates = [sessions_dir(), outputs_dir()]
    if traces_path().exists():
        candidates += [
            version_dir / sub
            for version_dir in sorted(traces_path().iterdir())
            if version_dir.is_dir()
            for sub in ("sessions", "outputs")
        ]
    return [c for c in dict.fromkeys(candidates) if c.is_dir()]


def setup_notes(
    session_id: str,
    task_id: str | None = None,
    type: str | None = None,
    *,
    recorder: SessionRecorder | None = None,
    runtime: str = "",
) -> NotesConfig:
    """Create session-specific notes folder structure.

    Uses version-aware paths from lup.workspace.paths. Separates:
    - RW directories: This session can write here
    - RO directories: Historical data, read-only for this session
    - Logs: Agent cannot access (for feedback loop analysis)

    This is the writer that opens a session's directory, so it is where the
    session is recorded as open: handed a ``recorder``, it records one
    :class:`~lup.observability.sessions.Session` pointing at the directory
    and the trace log, under ``runtime`` as the client that opened it, and
    hands the node back on the config for the closer to amend. Handed none
    it records nothing and works as before. The scaffold's
    ``build_session_factory`` wires the recorder from the project's declared
    kinds; a refusing ledger is logged there and the session goes on.

    Args:
        session_id: Unique session identifier.
        task_id: Optional task identifier (for organizing by task).
        type: Optional prefix inserted under sessions/, outputs/, and logs/
            to separate data by category (e.g. "background", "interactive").
        recorder: Where to record the session as a pointer, or nothing.
        runtime: The client opening the session, recorded on its node.

    Returns:
        NotesConfig with RW and RO directories separated.
    """
    timestamp = datetime.now().strftime(TIMESTAMP_FMT)

    sessions_base = sessions_dir() / type if type else sessions_dir()
    outputs_base = outputs_dir() / type if type else outputs_dir()
    logs_base = trace_logs_dir() / type if type else trace_logs_dir()

    session_path = sessions_base / session_id
    output_path = outputs_base / (task_id or session_id) / timestamp

    session_path.mkdir(parents=True, exist_ok=True)
    output_path.mkdir(parents=True, exist_ok=True)
    runtime_logs_path().mkdir(parents=True, exist_ok=True)

    trace_log = logs_base / session_id / f"{timestamp}.md"
    trace_log.parent.mkdir(parents=True, exist_ok=True)

    return NotesConfig(
        session=session_path,
        output=output_path,
        trace_log=trace_log,
        rw=[session_path, output_path],
        ro=collect_ro_dirs(),
        record=(
            recorder.opened(runtime, agent_version(), session_path, trace_log)
            if recorder is not None
            else None
        ),
    )


def session_gate_flag(session_id: str) -> Path:
    """Cross-process reflection-flag path outside every agent-writable root.

    The Codex sandbox grants the workspace, ``/tmp``, and ``$TMPDIR``; gate
    state the submission resolver trusts must live where only host-side
    processes (the tool server and the adapter) can write — a flag the
    sandboxed agent could create itself would make the gate forgeable.
    """
    return UserDirectories().cache() / "gates" / f"{session_id}.reflection"
