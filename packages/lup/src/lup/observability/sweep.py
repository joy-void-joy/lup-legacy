"""Indexing a notes/ tree that was already there, when somebody asks.

Recording starts when a recorder is wired: :func:`~lup.workspace.notes.setup_notes`
records a session as it opens the directory, the harness launcher records a
launch as it opens one, and :func:`~lup.workspace.history.save_session` records
each result. A tree written before any of that was wired holds runs the ledger
points at nothing of, so a later session asking what ran here is told about the
ones since and none of the ones before.

This is the sweep that answers for those. It walks the two trees a run writes
under — a session directory per agent version under ``notes/traces/``, a launch
directory per runtime under ``notes/harness/`` — and records one closed session
per directory, with the journal pinned as the tree holds it now and the outcome
read off that journal's last record, plus one output per result document under
a session, about that session.

**Through the writers a live run uses.** A backfilled session is the same two
records a live one leaves — the directory opened, then the close amended over
it — so nothing reads it specially, and a refusal is swallowed and logged here
exactly as it is there.

**What it records is what the tree says.** The moments come from the run's own
layout — the stamp a launch directory's name carries, the stamp a trace log's
does — and from the modification time the filesystem kept where the layout
stamps nothing. Nothing is filled in from this checkout: a launch directory
says nothing about the agent version that opened it, so that field stays empty
rather than taking the version running the sweep.

**Running it twice records nothing twice.** A directory the log already points
at is passed over, and so is a result document already recorded; identity is
the checkout and the path under it, which is what a record of a notes tree is.
"""

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from lup.observability.audit import ObservableEvent, read_last_observable_event
from lup.observability.sessions import (
    Outcome,
    Output,
    Session,
    SessionRecorder,
    spelled_under,
)
from lup.observability.trace import TraceEvent, read_trace_events
from lup.workspace.history import (
    iter_run_dirs,
    iter_session_dirs,
    iter_trace_log_files,
    run_transcript,
    session_backend,
    version_dirs,
)
from lup.workspace.paths import parse_timestamp


class Pointed(BaseModel, frozen=True):
    """One path a record in the log already points at, as that record spells it.

    The checkout and the path under it, because ``notes/`` is per worktree
    while the log is per clone: the same relative path under two checkouts is
    two directories, and a sweep in one must not read the other's record as
    covering its own.
    """

    checkout: str
    path: str


class Found(BaseModel, frozen=True):
    """One run's directory as the tree spells it, before anything is recorded."""

    runtime: str
    agent_version: str = ""
    directory: Path
    journal: Path | None = None
    """The journal the run wrote, or nothing where the tree holds none."""

    started: datetime
    ended: datetime
    outcome: Outcome


class Indexed(BaseModel, frozen=True):
    """What one sweep did with one run directory."""

    directory: Path
    session: Session | None = None
    outputs: list[Output] = []
    known: bool = False
    """Whether the log already pointed here, which is what a second run finds."""


class Sweep(BaseModel, frozen=True):
    """Every run directory one sweep walked, in the order it walked them."""

    runs: list[Indexed] = []

    def sessions(self) -> list[Session]:
        """The sessions this run recorded."""
        return [each.session for each in self.runs if each.session is not None]

    def outputs(self) -> list[Output]:
        """The outputs this run recorded, whichever session each is about."""
        return [output for each in self.runs for output in each.outputs]

    def known(self) -> list[Path]:
        """The directories the log already pointed at."""
        return [each.directory for each in self.runs if each.known]

    def refused(self) -> list[Path]:
        """The directories the ledger declined; the log says of each why."""
        return [
            each.directory
            for each in self.runs
            if not each.known and each.session is None
        ]


def modified(path: Path) -> datetime:
    """When this path was last written, as an aware moment.

    Which for a journal nothing is appending to any more is when its run
    stopped writing, and so when the run ended.
    """
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone()


def stamped(path: Path) -> datetime:
    """When one path says it is from: the stamp in its name, or when it was written.

    A launch directory and a trace log are both named for the moment they
    were opened, which is the fact a record of the run wants. A path the
    layout stamps with nothing — a session directory, named for its session —
    answers with what the filesystem kept instead.
    """
    try:
        return parse_timestamp(path.name).astimezone()
    except ValueError:
        return modified(path)


def launch_outcome(journal: Path | None) -> Outcome:
    """How a launch ended, read off the last record of its observable journal.

    A launch writes ``run_end`` as the last thing it does, carrying whether it
    succeeded, so that one record is the whole answer. A journal whose last
    record is anything else stopped before its launch wrote its end, which is
    what an interruption leaves behind — and so is a launch directory holding
    no journal at all.
    """
    match read_last_observable_event(journal) if journal is not None else None:
        case ObservableEvent(kind="run_end", payload={"succeeded": True}):
            return "completed"
        case ObservableEvent(kind="run_end"):
            return "failed"
        case _:
            return "interrupted"


def trace_outcome(journal: Path | None) -> Outcome:
    """How a session ended, read off the last record of its trace journal.

    A trace's records are the typed events in the ``.events.jsonl`` sidecar
    beside it, which :meth:`~lup.observability.trace.TraceLogger.save` creates
    whether or not the run produced one — so a trace with a sidecar is a trace
    that was written out, and its last record says how the run was going when
    it stopped: an ``error`` ended on a failure, anything else ended on work,
    and a run that logged nothing at all still reached its save. A trace
    nothing ever saved has no sidecar and so no last record, which is the run
    that stopped before writing itself out.
    """
    sidecar = journal.with_suffix(".events.jsonl") if journal is not None else None
    if sidecar is None or not sidecar.is_file():
        return "interrupted"
    match read_trace_events(sidecar)[-1:]:
        case [TraceEvent(kind="error")]:
            return "failed"
        case _:
            return "completed"


def trace_sessions() -> Iterator[Found]:
    """Every session directory under ``notes/traces/``, oldest version first.

    The version directory above it names the agent version that wrote it, the
    result documents inside name the backend that opened it, and the trace log
    under that version's ``logs/`` is the journal the live writer pinned — the
    newest where one session id was opened more than once, which is the one
    that writer pinned last. A session whose results carry no backend stamp
    names no client, and the record stands in for it rather than leaving a
    field the type requires empty.
    """
    for version in version_dirs():
        for directory in iter_session_dirs(version=version.name):
            held = sorted(iter_trace_log_files(directory.name, version.name))
            journal = held[-1] if held else None
            yield Found(
                runtime=session_backend(directory) or "unknown",
                agent_version=version.name,
                directory=directory,
                journal=journal,
                started=stamped(journal or directory),
                ended=modified(journal or directory),
                outcome=trace_outcome(journal),
            )


def harness_launches() -> Iterator[Found]:
    """Every launch directory under ``notes/harness/``, one per interactive CLI run.

    The provider directory above it names the runtime, its own name carries
    the moment it opened, and the observable journal beside it is the one the
    launcher pinned. Nothing in the tree says which agent version opened it,
    so the record says nothing either.
    """
    for directory in iter_run_dirs():
        written = run_transcript(directory)
        journal = written if written.is_file() else None
        yield Found(
            runtime=directory.parent.name,
            directory=directory,
            journal=journal,
            started=stamped(directory),
            ended=modified(journal or directory),
            outcome=launch_outcome(journal),
        )


def record_run(recorder: SessionRecorder, found: Found) -> Session | None:
    """Record one directory as a session that has already closed.

    Opened and then closed over it, the two records a live run leaves, with
    the moments the tree spells rather than this sweep's: what a record of a
    run that is over says about when it ran has to be about the run.
    """
    opened = recorder.opened(
        found.runtime,
        found.agent_version,
        found.directory,
        found.journal,
        started=found.started,
    )
    if opened is None:
        return None
    return recorder.closed(opened, found.outcome, at=found.ended)


def index_notes(recorder: SessionRecorder) -> Sweep:
    """Record the runs already under ``notes/`` that the log points at nothing of.

    One fold of the log held for the whole sweep: what it already points at is
    read once and grown with each record, so a tree of ten thousand
    directories asks the log once rather than once per directory.
    """
    checkout = recorder.checkout

    def pointed(path: Path) -> Pointed:
        return Pointed(checkout=str(checkout), path=spelled_under(path, checkout))

    with recorder.store.batch():
        indexed = {
            Pointed(checkout=node.checkout, path=node.directory)
            for node in recorder.store.read(Session)
        }
        produced = {
            Pointed(checkout=node.checkout, path=node.path)
            for node in recorder.store.read(Output)
        }

        def results(directory: Path, session: Session | None) -> Iterator[Output]:
            for path in sorted(directory.glob("[0-9]*.json")):
                if pointed(path) in produced:
                    continue
                output = recorder.produced(path, session, at=stamped(path))
                if output is not None:
                    yield output

        def swept() -> Iterator[Indexed]:
            for found in (*trace_sessions(), *harness_launches()):
                if pointed(found.directory) in indexed:
                    yield Indexed(directory=found.directory, known=True)
                    continue
                session = record_run(recorder, found)
                yield Indexed(
                    directory=found.directory,
                    session=session,
                    outputs=list(results(found.directory, session)),
                )

        return Sweep(runs=list(swept()))
