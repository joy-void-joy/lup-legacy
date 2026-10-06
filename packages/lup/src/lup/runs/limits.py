"""How long a unit may run and how much memory it may hold, enforced from outside.

A unit that hangs or grows without bound is invisible to the run until it
takes the machine with it: the runner renews every claim it holds from its own
heartbeat, so a stuck unit reads as healthy, and the units around it go on
landing, so the run is never quiet enough to look stalled. Bounding it has to
happen from outside the unit, because the work that hangs — a native solver in
a tight loop, a child process it forked — is exactly the work that never comes
back to check a clock of its own.

So a limited shell unit runs in a **session** of its own. Everything it starts
inherits that session, so its resident memory is summed over every process in
it, a solver's helpers included, and stopping it reaches all of them: SIGTERM
to each member, a grace period for whatever writes out what it has, then
SIGKILL to whatever is left. Memory is read as resident pages from ``/proc``,
never as an address-space limit, because a process that reserves a large
virtual range it never touches — a tensor library does — is not using it, and
an ``RLIMIT_AS`` would kill it for memory it does not hold.

Breaking a limit fails that unit alone, with a :class:`LimitBreach` saying
which limit and by how much. The runner keeps a registry of the sessions it
started, so an interrupt or a crash of the runner itself takes them down too
rather than leaving them running unwatched.
"""

import os
import signal
import threading
import time
from pathlib import Path

import sh
from pydantic import BaseModel, Field

from lup.runs.models import LimitBreach

PROC = Path("/proc")
"""Where the kernel publishes each process, read for sessions and memory."""

PAGE_BYTES = os.sysconf("SC_PAGE_SIZE")
"""The size of one resident page, which ``statm`` counts in."""

STOP_POLL_SECONDS = 0.05
"""How often a stopped session is checked for members that have not exited."""


class UnitLimits(BaseModel, frozen=True):
    """What one unit may spend before the runner stops it.

    Either bound may be absent; a step with neither runs exactly as an
    unlimited one does. They are how a unit may run, not what it computes, so
    like ``retries`` they stay out of the step's fingerprint: raising a limit
    and resuming reruns the units it stopped, which failed and so always rerun,
    and reuses everything that landed.
    """

    timeout_seconds: float | None = Field(default=None, gt=0)
    """Wall time one attempt may take, from its launch."""

    memory_bytes: int | None = Field(default=None, gt=0)
    """Resident memory summed over every process in the unit's session."""

    sample_seconds: float = Field(default=1.0, gt=0)
    """How often the runner measures the unit against its limits."""

    grace_seconds: float = Field(default=5.0, ge=0)
    """How long a stopped unit has between SIGTERM and SIGKILL."""


class UnitLimitExceeded(RuntimeError):
    """A unit the runner stopped because it ran past one of its declared limits."""

    def __init__(self, breach: LimitBreach) -> None:
        super().__init__(breach.render())
        self.breach = breach


class LimitUnenforceable(RuntimeError):
    """A limit was declared on a machine that cannot measure it."""


def stat_fields(pid: int, proc: Path = PROC) -> list[str] | None:
    """The fields of ``/proc/<pid>/stat`` after the command name, or None once gone.

    The command name sits in parentheses and may itself hold spaces or
    parentheses, so the fields are taken after its *last* closing parenthesis;
    what follows is a whitespace-separated record the kernel documents, and the
    standard library has no reader for it.
    """
    try:
        text = (proc / str(pid) / "stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    # lup: ignore[string-split] — /proc/<pid>/stat has no stdlib parser
    _, closing, rest = text.rpartition(")")
    return rest.split() if closing else None


def session_members(session: int, proc: Path = PROC) -> list[int]:
    """Every process currently in one session, read off the process table.

    The first field after the command name is the state, and fields three and
    four are the process group and the session; a member that moved to a group
    of its own is still in the session, which is why the session is what is
    counted and signalled. A zombie is left out: it has exited and holds no
    memory, and it cannot be signalled, only collected by its parent.
    """

    def belongs(pid: int) -> bool:
        fields = stat_fields(pid, proc)
        return (
            fields is not None
            and len(fields) > 3
            and fields[0] != "Z"
            and int(fields[3]) == session
        )

    if not proc.is_dir():
        return []
    return [
        int(entry.name)
        for entry in proc.iterdir()
        if entry.name.isdigit() and belongs(int(entry.name))
    ]


def resident_bytes(pid: int, proc: Path = PROC) -> int:
    """One process's resident memory, or zero once it has exited."""
    try:
        pages = (proc / str(pid) / "statm").read_text(encoding="utf-8").split()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return 0
    return int(pages[1]) * PAGE_BYTES if len(pages) > 1 else 0


def session_resident_bytes(session: int, proc: Path = PROC) -> int:
    """Resident memory summed over every process in one session."""
    return sum(resident_bytes(pid, proc) for pid in session_members(session, proc))


def signal_session(session: int, number: int, proc: Path = PROC) -> None:
    """Send one signal to a session's own group and to every member it holds.

    The leader of a new session also leads a process group of the same number,
    which everything it starts joins unless it asks otherwise; signalling the
    group needs nothing from ``/proc``, so a timeout is enforceable anywhere.
    The members ``/proc`` lists add whatever moved to a group of its own.
    """
    try:
        os.killpg(session, number)
    except ProcessLookupError:
        pass
    for pid in session_members(session, proc):
        try:
            os.kill(pid, number)
        except ProcessLookupError:
            continue


def session_alive(session: int, proc: Path = PROC) -> bool:
    """Whether anything of a session is left to stop.

    Read off ``/proc`` where it exists, which sees a member in a group of its
    own; elsewhere, whether the session's own group still has anybody in it.
    """
    if proc.is_dir():
        return bool(session_members(session, proc))
    try:
        os.killpg(session, 0)
    except ProcessLookupError:
        return False
    return True


def stop_session(
    session: int,
    grace_seconds: float,
    proc: Path = PROC,
    poll_seconds: float = STOP_POLL_SECONDS,
) -> None:
    """SIGTERM a whole session, then SIGKILL whatever outlives the grace period.

    The grace period is not politeness. A unit that catches the first signal
    can write out what it had finished, and that is the whole yield of a unit
    about to be reported as stopped; a straight SIGKILL loses it.
    """
    signal_session(session, signal.SIGTERM, proc)
    started = time.monotonic()
    while session_alive(session, proc):
        if time.monotonic() - started >= grace_seconds:
            break
        time.sleep(poll_seconds)
    signal_session(session, signal.SIGKILL, proc)


class SessionRegistry(BaseModel, arbitrary_types_allowed=True):
    """The limited units' sessions this runner started and has not seen end.

    A limited unit runs outside the runner's own process group, so an
    interrupt at the terminal no longer reaches it the way it reaches an
    unlimited one. Whatever ends the runner — an interrupt, a crash — stops
    every session listed here before the runner waits on its workers, or the
    wait would last as long as the slowest unit it was meant to abandon.
    """

    live: dict[int, float] = {}
    """Each live session's leader, with the grace period its step declared."""

    closed: bool = False
    """Whether the run is ending, so a session enrolled now is stopped at once.

    A worker that took its unit just before the interrupt can start its
    session just after the others were stopped; without this, that one unit
    would hold the run open for as long as it runs.
    """

    guard: threading.Lock = Field(default_factory=threading.Lock, exclude=True)

    def enrol(self, session: int, grace_seconds: float) -> None:
        """Record a session this runner started, stopping it if the run is ending."""
        with self.guard:
            self.live[session] = grace_seconds
            closed = self.closed
        if closed:
            stop_session(session, grace_seconds)

    def retire(self, session: int) -> None:
        """Forget a session whose unit has ended, however it ended."""
        with self.guard:
            self.live.pop(session, None)

    def close(self) -> None:
        """Stop every session still listed, and any enrolled from now on.

        Each with its own step's grace. The flag and the listing are read under
        one lock, so a session is either in the listing stopped here or sees
        the flag as it enrols, and none falls between them.
        """
        with self.guard:
            self.closed = True
            sessions = dict(self.live)
        for session, grace in sessions.items():
            stop_session(session, grace)


def breach_of(
    limits: UnitLimits, elapsed: float, resident: int | None
) -> LimitBreach | None:
    """Which declared limit one reading has crossed, if any.

    Memory is asked first: where both are crossed in one interval, the memory
    reading is the one that says what the unit was doing wrong, and a time-out
    reported instead would hide it.
    """
    if (
        limits.memory_bytes is not None
        and resident is not None
        and resident > limits.memory_bytes
    ):
        return LimitBreach(
            limit="memory",
            measured=resident,
            allowed=limits.memory_bytes,
            after_seconds=elapsed,
        )
    if limits.timeout_seconds is not None and elapsed > limits.timeout_seconds:
        return LimitBreach(
            limit="timeout",
            measured=elapsed,
            allowed=limits.timeout_seconds,
            after_seconds=elapsed,
        )
    return None


def supervise(
    running: sh.RunningCommand,
    limits: UnitLimits,
    sessions: SessionRegistry,
    proc: Path = PROC,
) -> None:
    """Measure one running session against its limits until it ends or breaks one.

    Returns when the command has ended on its own, leaving its exit status for
    the caller to read; raises :class:`UnitLimitExceeded` after stopping the
    session when a reading crosses a limit. A memory limit on a machine with no
    ``/proc`` is refused rather than ignored: a bound that silently does
    nothing reads exactly like one that held.
    """
    if limits.memory_bytes is not None and not (proc / "self" / "statm").exists():
        raise LimitUnenforceable(
            f"a memory limit needs {proc} to measure resident memory, and this "
            "machine has none; declare a timeout alone, or run where /proc exists"
        )
    session = running.pid
    sessions.enrol(session, limits.grace_seconds)
    started = time.monotonic()
    try:
        while running.is_alive():
            elapsed = time.monotonic() - started
            resident = (
                session_resident_bytes(session, proc)
                if limits.memory_bytes is not None
                else None
            )
            breach = breach_of(limits, elapsed, resident)
            if breach is not None:
                stop_session(session, limits.grace_seconds, proc)
                raise UnitLimitExceeded(breach)
            time.sleep(limits.sample_seconds)
    finally:
        sessions.retire(session)
