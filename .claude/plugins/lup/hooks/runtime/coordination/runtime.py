# lup: ignore[constant-declaration]
# Every name below is the kernel's own layout of /proc/<pid>/stat, as proc(5)
# documents it: a reader that numbered a field differently would read another
# field, not make another choice.
"""The process a session is, read off ``/proc`` wherever a reader can see it.

A session is its runtime's process — the Claude Code or Codex CLI a person
started — and every other process of the session is its runtime's: the hooks
it spawns, the tool servers it feeds. A beat answers for less than that. A
tool server beats for as long as *it* runs, and a runtime started from the
session's own shell inherits the session's id, serves it from a tool server
of its own, and can outlive it — which is how a session the person had
stopped read as running for as long as another runtime lived.

So a row names the process it answers for, by its id and its start time — the
id alone is reused, the pair is not — and the namespace the id belongs to,
since an id means nothing outside the pid namespace that gave it. A reader in
that namespace asks the process directly: alive or gone, whatever the clock
says, which is also what keeps a suspended machine's sessions present when it
wakes. A reader anywhere else cannot ask, and is told so rather than guessing.

Shipped beside :mod:`.store` into every plugin, so the standard library alone:
the permission dispatcher and the prompt fold read presence through it.
Nothing here raises; what cannot be read is unknown.
"""

import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

STARTED_FIELD = 19
"""Where ``starttime`` sits among a ``/proc/<pid>/stat`` line's fields after the name.

Field 22 of proc(5), counting from the pid; the name and the fields before it
are cut off first, because a name may hold spaces and parentheses.
"""

PARENT_FIELD = 1
"""Where ``ppid`` sits among the same fields, field 4 of proc(5)."""

# lup: ignore[library-default] — proc(5)'s own state letters for a process that has exited
ENDED_STATES = ("Z", "X")
"""What a process that has exited reads as while it waits to be collected."""


class Runtime(TypedDict, total=False):
    """One runtime process, as the row answering for it records it."""

    pid: int
    started: str
    scope: str


def stat_fields(pid: int) -> list[str]:
    """The fields of ``/proc/<pid>/stat`` after the process's name, empty where it is gone."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return []
    # lup: ignore[string-split] — /proc stat's fields after the command's name
    return raw.rpartition(")")[2].split()


def process_scope() -> str:
    """Which pid namespace, on which boot, a pid this process reads belongs to.

    Two containers each number their own processes, so pid 7 is a different
    process in each; a boot starts the numbering again. Empty where the kernel
    says neither, which no recorded scope matches.
    """
    try:
        namespace = Path("/proc/self/ns/pid").stat()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""
    spelled = f"{boot}:{namespace.st_dev}:{namespace.st_ino}"
    return hashlib.sha256(spelled.encode()).hexdigest()


def runtime_of(pid: int) -> Runtime:
    """The process running under *pid* now, as a row records it."""
    fields = stat_fields(pid)
    return Runtime(
        pid=pid,
        started=fields[STARTED_FIELD] if len(fields) > STARTED_FIELD else "",
        scope=process_scope(),
    )


def same_runtime(recorded: Runtime, runtime: Runtime) -> bool:
    """Whether two records name one process: its id, its start, and where both were read."""
    return (
        recorded.get("pid") == runtime.get("pid")
        and recorded.get("started") == runtime.get("started")
        and recorded.get("scope") == runtime.get("scope")
    )


def runtime_alive(runtime: Runtime | None, scope: str) -> bool | None:
    """Whether that same process still runs, asked from a reader in *scope*.

    ``None`` where the reader cannot ask: nothing recorded, or recorded in a
    namespace other than the reader's — where the pid names another process
    or none, and the answer would be a guess. A process that has exited and
    not yet been collected has exited.
    """
    if not isinstance(runtime, dict):
        return None
    pid = runtime.get("pid")
    started = runtime.get("started")
    if not isinstance(pid, int) or pid <= 0 or not isinstance(started, str):
        return None
    if not started or not scope or runtime.get("scope") != scope:
        return None
    fields = stat_fields(pid)
    return (
        len(fields) > STARTED_FIELD
        and fields[STARTED_FIELD] == started
        and fields[0] not in ENDED_STATES
    )


def ancestry(pid: int) -> Iterator[int]:
    """*pid* and each process that started the one before, nearest first, short of init."""
    fields = stat_fields(pid)
    if pid <= 1 or len(fields) <= PARENT_FIELD:
        return
    yield pid
    yield from ancestry(int(fields[PARENT_FIELD]))


def beneath(runtime: Runtime, ancestor: Runtime, scope: str) -> bool:
    """Whether *runtime* runs now, started — at any remove — by *ancestor*, which runs too.

    Asked only where a reader in *scope* can ask both: a process that has
    stopped has been handed to whatever adopts orphans, so what started it
    is no longer on record, and the answer then is no rather than a guess.
    """
    if runtime_alive(runtime, scope) is not True:
        return False
    if runtime_alive(ancestor, scope) is not True:
        return False
    started = list(ancestry(runtime.get("pid", 0)))
    return ancestor.get("pid") in started[1:]


def stdin_runtime() -> Runtime:
    """The runtime a stdio server serves: the process feeding its standard input.

    Called before anything reads the input, while it is still the wire. A
    wrapper between the runtime and the server — ``uv run``, a shell — shares
    the wire as its own input, having inherited it; the runtime made it, and
    its own input is something else. So the first process up the chain whose
    input is not this one is the runtime, whether the wire is a socket pair,
    as Claude Code makes one, or a pipe, as Codex does. A server with no wire
    at all was started by hand, and its parent is all there is to answer for.
    """
    parent = os.getppid()
    try:
        wire = os.fstat(0)
    except OSError:
        return runtime_of(parent)
    if not (stat.S_ISSOCK(wire.st_mode) or stat.S_ISFIFO(wire.st_mode)):
        return runtime_of(parent)

    def sharing(pid: int) -> bool:
        """Whether *pid* reads this same wire as its own input."""
        try:
            theirs = Path(f"/proc/{pid}/fd/0").stat()
        except OSError:
            return False
        return (theirs.st_dev, theirs.st_ino) == (wire.st_dev, wire.st_ino)

    feeding = next((pid for pid in ancestry(parent) if not sharing(pid)), parent)
    return runtime_of(feeding)
