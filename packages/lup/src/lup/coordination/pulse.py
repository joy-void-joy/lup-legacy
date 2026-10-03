"""How often a live session beats, how long a silence reads as absence, and who beats.

The roster records arrivals and departures, and a departure is a record a
session writes on its way out. A session that is killed, or whose container
stops, writes nothing, and its row reads as present until somebody notices —
which nobody does, because the row is what they would notice it by. So
presence is not left to the record alone. A session is its runtime's process,
and the row names it: a reader that can see that process asks it, a reader
that cannot tests the pulse a live server holds for it, and only where
neither speaks is a row whose last beat is older than the window read as gone
whatever its records say.

The stamps, the lock and the window they are read against belong to
:mod:`lup.coordination.bare.store`: every process reading this store reads
them, and only one of those processes can import anything of lup's. What is
left here is what a typed caller turns and holds — how often the tool server
ticks, and the hold it keeps on the pulse while it answers.
"""

import fcntl
from datetime import datetime
from io import BufferedWriter
from pathlib import Path

from pydantic import BaseModel

from lup.coordination.bare.store import STALE_AFTER_SECONDS, stale


class Pulse(BaseModel, frozen=True):
    """How often a live session beats, and how long a silence reads as absence.

    The window is a few beats wide rather than one, so a stalled scheduler or
    a slow disk does not read as a departure; it is short because the roster
    is read to decide whether a path is safe to write, and a dead session
    holding that decision open for an hour is the failure this prevents.

    Both figures are defaults a caller may turn — a test wants a window it can
    cross, and a population beating in-process wants its own tick. The
    window's default is the store's own, because a reader that cannot import
    this model still has to reach the same verdict about the same silence.
    """

    interval_seconds: float = 30.0
    stale_after_seconds: float = STALE_AFTER_SECONDS

    def stale(self, heard: datetime, now: datetime) -> bool:
        """Whether a member last heard at *heard* reads as gone at *now*.

        The store's own test, over this caller's window: a hook and a tool
        server reading one session's silence reach the same verdict, and a
        caller that moved the window changed how long a silence is tolerated
        rather than what tolerating it means.
        """
        return stale(heard, now, self.stale_after_seconds)


class PulseHold:
    """This process's hold on one session's pulse, kept while it answers for that session.

    The lock at :func:`~lup.coordination.bare.store.pulse_path`, held
    exclusively, so a reader in another container — who cannot see this
    process, and cannot ask it — can test that somebody still answers; the
    kernel lets it go when this process ends, however it ends, and nothing
    about it lapses while a machine sleeps. One holder at a time, which keeps
    the several tool servers one runtime can start for a session — Codex
    starts one per conversation — from all answering for it at once: the
    rest wait, and one takes over only where the holder stopped.

    Not a model: it holds an open file, which is state of this process rather
    than a value anybody else could be handed.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: BufferedWriter | None = None
        self.ceded = False
        """Whether this process met the session answered for by another runtime, alive.

        A runtime started from the session's own shell inherits its id and is
        somebody else for good, however long it outlives the session; a
        runtime that had stopped before this process met it is one this
        process succeeds, as a resumed conversation does.
        """

    @property
    def held(self) -> bool:
        """Whether this process holds the pulse now."""
        return self.handle is not None

    def take(self) -> bool:
        """Hold the pulse where nobody else does, saying whether this process now holds it.

        Never waits: a pulse somebody else holds is answered for already, and
        a server that waited on it would be a server that stopped ticking.
        """
        if self.handle is not None:
            return True
        try:
            handle = self.path.open("ab")
        except OSError:
            return False
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        """Let the pulse go, where this process holds it."""
        if self.handle is None:
            return
        self.handle.close()
        self.handle = None
