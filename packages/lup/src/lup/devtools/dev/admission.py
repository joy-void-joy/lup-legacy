"""How many gates this clone runs at once, and how wide each of them spreads.

One gate already saturates the machine: it starts both test suites together
and gives each as many processes as there are cores to share. Several gates
therefore do not share the machine, they multiply the queue — measured on 32
cores, where the same suite took 115 seconds run alone and over 630 contended
by five sessions at load 90, and every one of those sessions came away
believing the gate is slow.

Serialising would be wrong, though, and that is the whole shape of this. The
gates that legitimately overlap are resolver leases, which check different
worktrees holding different code and each answer a question no other run
answers. Making them queue would trade a real parallelism for a false one.

So the machine is divided rather than rationed. A run takes a slot, sees how
many others are held, and spreads itself over the share of the cores that
leaves — four gates of four workers rather than four of sixteen. The total
stays near what the machine has, each run keeps its own answer, and none of
them waits on another.

A run is anything that opens the suites, which is `dev test` as much as the
gate. `dev test` is the same suite at the same width over the files a caller
names, and it is the half of the scoped loop a change runs while it is still
moving — so ten agents iterating are ten suites of sixteen workers beside
whatever gate is running, the contention this exists for arriving by the side
door. `dev check --changed` and `dev check --no-test` take no slot. Neither
opens a suite: what they run is ruff and a Pyright handed no `--threads`, a
single core however long it takes. A slot either held would divide nothing of
its own, and would narrow every run that started beside it for the whole of
that run, since the share is fixed when a run opens — and would queue
`--changed`, a check of seconds, behind runs of minutes. That Pyright stays on
one core is a decision `pyright_check` records, and the day it spreads, these
two runs open something the share has to divide.

Nothing here is correctness. A slot that cannot be taken, a lock file that
cannot be made, a holder that died without releasing: each ends in the gate
running anyway at its full width, because a session that cannot coordinate
should still be able to check its work, and the worst that costs is the
contention this exists to avoid — which is where every session was before.
"""

import fcntl
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel

from lup.harness.notice import Notice
from lup.workspace.edition import shared_git_directory
from lup.workspace.shared_directory import SLOT_DIRECTORY

SLOTS = 4
"""How many gates may run at once before one has to wait.

Four because that is what the resolver runs concurrently by default, so the
arrangement it already makes is the one this admits without queueing. A fifth
waits, which is the case this is least worried about: five gates at once was
never anybody's plan.
"""

# lup: ignore[constant-declaration] — not a judgement but where the library's
# own `parallel_arguments` stops spelling parallel: below two it runs the suite
# behind a single interpreter, which is slower than the contention avoided
MINIMUM_WORKERS = 2
"""The narrowest a divided suite is allowed to get.

Below two, `parallel_arguments` spells serial, and a serial suite is slower
than the contention being avoided. A machine with few enough cores to reach
this floor is one where the division has nothing left to give, and running
slightly wide there is the better error.
"""

PATIENCE = 1800.0
"""How long a run waits for a slot before going ahead without one.

Long enough that an honest queue is waited through — the slowest measured run
is a tenth of this — and short enough that a slot whose holder died in a way
the lock outlived costs one session one wait rather than costing the
repository its gate.
"""


class Admission(BaseModel, frozen=True):
    """A run's share of the machine, and what to tell the operator about it."""

    workers: int
    """How many processes each suite of this run may spread over."""

    said: list[Notice] = []
    """What the operator is told, empty where nothing worth saying happened.

    Returned rather than printed, because the gate says everything it has to
    say in one report, and a line written from inside would land in the middle
    of somebody else's output. The whole record either way: a caller whose
    output streams hears each notice as it arises through `admitted`'s
    *announce* as well.
    """


def slot_paths(root: Path, slots: int) -> list[Path]:
    """This clone's slot files, made if they are not there yet."""
    directory = shared_git_directory(root) / SLOT_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    return [directory / f"{index}.slot" for index in range(slots)]


def taken(path: Path) -> int | None:
    """An open descriptor holding *path*'s lock, or ``None`` if somebody else does.

    The descriptor is the holding, so a caller that wants the lock keeps it and
    a caller that only wanted to know closes it — which is how a run counts the
    slots around it without taking them.
    """
    try:
        handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(handle)
        return None
    return handle


def held_beside(paths: list[Path], mine: Path) -> int:
    """How many of the other slots somebody is holding right now.

    A snapshot, and deliberately not maintained: a run that starts later does
    not narrow one already running. What this bounds is how wide a run opens,
    which is the moment the decision is made and the only moment it can be.
    """
    others = 0
    for path in paths:
        if path == mine:
            continue
        handle = taken(path)
        if handle is None:
            others += 1
        else:
            os.close(handle)
    return others


@contextmanager
def admitted(
    root: Path,
    workers: int,
    slots: int = SLOTS,
    patience: float = PATIENCE,
    announce: Callable[[Notice], None] | None = None,
) -> Iterator[Admission]:
    """Hold one of this clone's gate slots, and answer with this run's share.

    *workers* is what a run alone would use, which is what it keeps when it is
    alone. Divided by the number of runs under way when this one starts, so
    the machine carries about what it carries for one gate however many are on
    it.

    *announce* hears each notice the moment it arises, beside the record the
    admission carries back. It is for a caller whose output streams: a run
    that waits for a slot and says nothing until it has one reads as hung, and
    whatever launched it — a terminal, an agent's shell with a timeout — kills
    it as hung, never having been told it was queued.
    """
    paths = slot_paths(root, slots)
    waited = 0.0
    said: list[Notice] = []

    def note(text: str) -> None:
        notice = Notice(text=text, urgency="boundary")
        said.append(notice)
        if announce is not None:
            announce(notice)

    while True:
        for path in paths:
            handle = taken(path)
            if handle is None:
                continue
            try:
                others = held_beside(paths, path)
                share = max(MINIMUM_WORKERS, workers // (others + 1))
                if share < workers:
                    note(
                        f"gate admission: {share} workers per suite rather "
                        f"than {workers} — {others} other run(s) hold a slot "
                        "on this clone, and the machine is divided rather "
                        "than shared"
                    )
                os.ftruncate(handle, 0)
                os.pwrite(handle, f"{root.name} (pid {os.getpid()})".encode(), 0)
                yield Admission(workers=share, said=said)
                return
            finally:
                os.close(handle)
        if waited >= patience:
            note(
                f"gate admission: waited {waited:.0f}s for a slot and ran "
                "anyway; a holder may have died without releasing one"
            )
            yield Admission(workers=workers, said=said)
            return
        if not waited:
            note(
                f"gate admission: all {slots} slots on this clone are held — "
                "waiting rather than adding to the contention"
            )
        time.sleep(1.0)
        waited += 1.0
