"""What every channel shares: who may write, and what a read yields.

A channel stores the consumer's own record verbatim. Attribution that
belongs to the decision — which door, under what id, answering what — is the
consumer's to model, because only the consumer knows which of those its
records mean. What the channel owns is the part no consumer can enforce for
itself: which doors it accepts at all.
"""

import os
import secrets
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel


class ChannelConflictError(RuntimeError):
    """A write contradicted a record the channel already holds."""


class ChannelCorruptionError(RuntimeError):
    """A channel file could not be read as the record it should hold."""


class ChannelOverflowError(RuntimeError):
    """A capped stream reached its ceiling because nobody is consuming it."""


class Door(StrEnum):
    """Which surface a write came through.

    Attribution is not decoration. Some decisions are a human's to take, and
    naming the doors a channel refuses is the only way to say so
    structurally — an orchestrating agent then cannot write the record that
    would release its own run.
    """

    FLAG = "flag"
    PAGE = "page"
    CONSOLE = "console"
    AGENT = "agent"
    RECOVERY = "recovery"


class DoorPolicy(BaseModel, frozen=True):
    """Which doors a channel refuses, named as refusals rather than a roster.

    Stating it negatively is what makes the guarantee legible: a resume slot
    excludes ``AGENT``, so the orchestrator physically cannot write the
    record that would release its own run. A roster of allowed doors says the
    same thing and reads as an accident of enumeration.
    """

    excluded: list[Door] = []

    def accepts(self, door: Door) -> bool:
        return door not in self.excluded


class Offset[T](BaseModel, frozen=True):
    """One stream record and the offset that consumes exactly it.

    Committing per record is what keeps a crash between two of them from
    dropping the one that had not been applied yet.
    """

    item: T
    commit_offset: int


def write_atomic(
    path: Path,
    content: bytes,
    *,
    mode: int | None = None,
    durable: bool = False,
    expected: bytes | None = None,
) -> None:
    """Write one file so no reader can ever observe it half-written.

    The rename is the whole guarantee, because a reader holds no lock. Every
    write in this library that a concurrent reader may catch goes through
    here — a channel record, a state file, a rendered artifact, a metrics
    flush — so the temporary name, the parent creation, and the rename are
    decided once. The temporary is dot-prefixed so a lister that catches one
    mid-write does not offer it as an ordinary file, and named for this write
    alone: two writers sharing one name truncate each other's, and the first
    rename then publishes a file whose front is NUL bytes.

    ``mode`` is set on the temporary before a byte is written, so the file is
    never readable at its final path without it — a secret store, an
    executable, a configuration naming a tool server's token. Without one it
    is created as ``write_bytes`` creates one, with the mode the umask gives.

    ``durable`` syncs the bytes before the rename and the directory after it,
    for a file whose loss to a crash would be read as something it is not: a
    login, a seeded configuration. An ordinary state file is rewritten by its
    owner on the next run and stays cheap.

    ``expected`` makes the replacement a compare-and-swap: where the file no
    longer holds exactly those bytes when the rename is due — another writer
    replaced it since the caller read it — nothing is published and
    :class:`ChannelConflictError` says so, leaving the caller to read again.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as handle:
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            handle.write(content)
            if durable:
                handle.flush()
                os.fsync(handle.fileno())
        if expected is not None and (
            not path.is_file() or path.read_bytes() != expected
        ):
            raise ChannelConflictError(
                f"{path} changed after it was read, so this replacement was not "
                "written; read it again and redo the change"
            )
        temporary.replace(path)
        if durable:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def publish_atomic(
    path: Path,
    record: BaseModel,  # lup: ignore[bare-basemodel] — any model to disk
) -> None:
    """Write one record as indented JSON, atomically.

    Every channel publishes this way, and so does anything else in a run
    directory that a door may read while the run is writing it.
    """
    write_atomic(path, (record.model_dump_json(indent=2) + "\n").encode("utf-8"))


def utc_now() -> datetime:
    """Now, as every stamp this library records it: aware, in UTC.

    One clock for everything that is written down and later compared, so a
    stamp from one process and a stamp from another — or from a session, a
    trace and a ledger — order by the moment they name rather than by the
    zone each writer happened to run in.
    """
    return datetime.now(UTC)


def aware(moment: datetime) -> datetime:
    """A moment that compares with any other: a naive one read as this machine's local time.

    What a stamp written before every writer recorded UTC, or a bound a
    person typed without a zone, means — the time on the clock in front of
    them — so it is pinned to that zone rather than refused or read as UTC.
    """
    return moment if moment.tzinfo is not None else moment.astimezone()


LOCAL_STAMP_FORMAT = "%a %H:%M %Z"
"""How a reported time reads to whoever is deciding whether to come back.

The weekday and the zone both carry because a run spans days and is read
from wherever its operator is: a bare clock time reads as today, in the
reader's own zone, and both of those are exactly what a stale report is
not. A caller wanting another shape passes one rather than forking this.
"""


def local_stamp(fmt: str = LOCAL_STAMP_FORMAT) -> str:
    """Now, in the reader's own zone, for a report they read hours later.

    A run records UTC because a journal is compared against itself. A person
    deciding how stale a report is compares it against their own clock. A
    relative age from the run's point of view answers a different question —
    how long a worker has been quiet, never how long ago they were told.
    """
    return datetime.now().astimezone().strftime(fmt)
