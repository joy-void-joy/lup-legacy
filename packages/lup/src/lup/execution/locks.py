"""Sections only one process at a time may be inside, kept by a lock file.

Several processes that share a file and must not interleave a read, a decision
and a write over it agree on a lock file beside it and take ``flock`` on it.
The kernel drops a ``flock`` when its holder's descriptor closes, a crash
included, so a lock nobody holds can never be left behind. Every holder here
opens the file for append, which creates it and never truncates what a lock
that doubles as a record holds, and lets go on the way out whatever the body
did.

Two questions, and one answer each: :func:`exclusive` waits for its turn,
which is what a short transaction wants; :func:`try_exclusive` never waits,
and says whether it got the section, which is what a run that must not have
two drivers, a batch somebody else is already sending, and a probe asking
whether anybody holds a lease each want.

``flock`` is held per open file description, so two threads of one process
each opening the file contend like two processes; one thread entering a
section it already holds through another descriptor waits on itself.
"""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def exclusive(path: Path) -> Iterator[None]:
    """Hold the lock on ``path`` for the body, waiting for whoever holds it now."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def try_exclusive(path: Path) -> Iterator[bool]:
    """Hold the lock on ``path`` for the body where nobody does, saying whether it is held.

    ``False`` means another descriptor holds it now and this one never will
    inside the body. A caller asking only whether anybody holds it reads the
    answer and leaves, letting it go at once.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
