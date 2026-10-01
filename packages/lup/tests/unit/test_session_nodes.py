"""Sessions and outputs are indexed in the ledger as pointers at notes/, never as bytes.

Written against the writers rather than the types: a session directory
opened and a factory closed record and then amend one node with the journal
pinned at close; a result written records an output about its session; a
writer handed no recorder records nothing and works; a ledger that refuses
is logged and the writer goes on; and the console refuses bytes on either
kind before anything reaches the blob store.
"""

import asyncio
import json
import logging
from pathlib import Path

import pytest
from pydantic import BaseModel
from rich.text import Text
from typer.testing import CliRunner

import lup.devtools.ledger.app as ledger_app
import lup.sessions.capabilities as capabilities
from lup.channels.models import utc_now
from lup.coordination.refs import ActorRef
import lup.launch.session as launch_session
from lup.formats import digest
from lup.ledger.journal import LedgerStore
from lup.ledger.models import Surroundings
from lup.ledger.tools import NoInput, RecordInput, create_ledger_tools
from lup.observability.sessions import (
    Output,
    OutputOf,
    Session,
    SessionRecorder,
    CloseRecordingWrapper,
    session_recorder,
    spelled_under,
)
from lup.observability.sweep import index_notes
from lup.observability.trace import TraceEvent, TraceLogger
from lup.providers.claude.transcripts import ClaudeTranscripts
from lup.sessions.layers import SessionLayers
from lup.sessions.events import StartedTurn, TurnRequest
from lup.tools.mcp import ToolError
from lup.workspace.history import save_session
from lup.workspace.notes import setup_notes
from lup.workspace.paths import parse_timestamp
from tests.unit.doubles import EngineAgent, agent_over

AUTHOR = ActorRef(kind="test", id="t")


class Result(BaseModel):
    summary: str


class IdleSession(capabilities.SessionEngine):
    """A session nothing starts a turn on: the factory around it is the subject."""

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> StartedTurn[T]:
        raise NotImplementedError(f"no turn is started here: {request}")


def idle_agent() -> EngineAgent:
    """An agent whose sessions nobody starts a turn on: the layers are the subject."""
    return agent_over(IdleSession())


def recorder_at(root: Path) -> SessionRecorder:
    return SessionRecorder(LedgerStore(root, AUTHOR))


async def test_opening_and_closing_a_session_records_then_amends_one_node(
    tmp_lup_project: Path,
) -> None:
    store = LedgerStore(tmp_lup_project, AUTHOR)
    recorder = recorder_at(tmp_lup_project)

    notes = setup_notes("s1", "t1", recorder=recorder, runtime="fake")

    assert notes.record is not None and notes.session.is_dir()
    [opened] = store.read(Session)
    assert opened.id == notes.record.id and opened.title == "s1"
    assert opened.directory == "notes/traces/1.2.3/sessions/s1"
    assert opened.journal == notes.trace_log.relative_to(tmp_lup_project).as_posix()
    assert opened.checkout == str(tmp_lup_project)
    assert opened.runtime == "fake" and opened.agent_version == "1.2.3"
    assert opened.ended is None and opened.outcome == ""
    assert store.standing(opened).label == "open"

    factory = idle_agent().layered(
        SessionLayers(wrappers=[CloseRecordingWrapper(recorder, notes.record)])
    )
    async with factory.open():
        notes.trace_log.write_text("# Trace\n", encoding="utf-8")

    [closed] = store.read(Session)
    assert closed.id == opened.id and closed.ended is not None
    assert closed.outcome == "completed" and closed.finished()
    assert closed.journal_digest == digest.file(notes.trace_log)
    # One record and one amendment: nothing was rewritten, the log grew.
    assert len(store.lines()) == 2


async def test_standing_reads_open_then_fresh_stale_and_missing(
    tmp_lup_project: Path,
) -> None:
    store = LedgerStore(tmp_lup_project, AUTHOR)
    recorder = recorder_at(tmp_lup_project)
    notes = setup_notes("s1", recorder=recorder, runtime="fake")
    assert notes.record is not None
    assert store.standing(notes.record).label == "open"

    async with (
        idle_agent()
        .layered(
            SessionLayers(wrappers=[CloseRecordingWrapper(recorder, notes.record)])
        )
        .open()
    ):
        notes.trace_log.write_text("one\n", encoding="utf-8")

    [closed] = store.read(Session)
    assert store.standing(closed).label == "fresh"
    with notes.trace_log.open("a", encoding="utf-8") as journal:
        journal.write("two\n")
    grown = store.standing(closed)
    assert grown.label == "stale" and not grown.sound and closed.journal in grown.reason
    notes.trace_log.unlink()
    assert store.standing(closed).label == "missing"
    assert closed.standing(Surroundings()).label == "unchecked"


async def test_a_session_closed_by_amending_ended_reads_unpinned_and_says_why(
    tmp_lup_project: Path,
) -> None:
    """The console can end a session by amending `ended`; that pins nothing,
    and the reading says so rather than reporting an unnamed path missing."""
    store = LedgerStore(tmp_lup_project, AUTHOR)
    notes = setup_notes("s1", recorder=recorder_at(tmp_lup_project), runtime="fake")
    assert notes.record is not None

    amended = store.amend(
        notes.record.model_copy(update={"ended": utc_now(), "outcome": "completed"})
    )
    reading = store.standing(amended)

    assert reading.label == "unpinned" and reading.sound
    assert "closed()" in reading.reason and "is not in the tree" not in reading.reason


async def test_a_session_that_raises_closes_as_failed_or_interrupted(
    tmp_lup_project: Path,
) -> None:
    store = LedgerStore(tmp_lup_project, AUTHOR)
    recorder = recorder_at(tmp_lup_project)
    notes = setup_notes("s1", recorder=recorder, runtime="fake")
    assert notes.record is not None
    factory = idle_agent().layered(
        SessionLayers(wrappers=[CloseRecordingWrapper(recorder, notes.record)])
    )

    with pytest.raises(RuntimeError):
        async with factory.open():
            raise RuntimeError("the provider fell over")
    assert store.read(Session)[0].outcome == "failed"

    with pytest.raises(asyncio.CancelledError):
        async with factory.open():
            raise asyncio.CancelledError()
    assert store.read(Session)[0].outcome == "interrupted"


def test_an_output_records_a_node_pointing_at_its_session(
    tmp_lup_project: Path,
) -> None:
    store = LedgerStore(tmp_lup_project, AUTHOR)
    recorder = recorder_at(tmp_lup_project)
    notes = setup_notes("s1", recorder=recorder, runtime="fake")
    assert notes.record is not None

    written = save_session(
        Result(summary="done"), session_id="s1", recorder=recorder, session=notes.record
    )

    [output] = store.read(Output)
    assert output.path == written.relative_to(tmp_lup_project).as_posix()
    assert output.title == output.path and output.checkout == str(tmp_lup_project)
    assert output.session == notes.record.id and output.digest == digest.file(written)
    assert store.standing(output).label == "fresh"
    [edge] = store.edges()
    assert (edge.kind, edge.source, edge.target) == (
        "observability:output_of",
        output.id,
        notes.record.id,
    )
    written.write_text("{}", encoding="utf-8")
    assert store.standing(output).label == "stale"
    written.unlink()
    assert store.standing(output).label == "missing"


def test_a_writer_with_no_recorder_records_nothing_and_still_works(
    tmp_lup_project: Path,
) -> None:
    notes = setup_notes("s2", "t2")
    written = save_session(Result(summary="quiet"), session_id="s2")

    assert notes.record is None and notes.session.is_dir() and written.is_file()
    assert not (tmp_lup_project / "lup").exists()


def test_a_refusing_store_is_logged_and_the_writer_goes_on(
    tmp_lup_project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The ledger's root cannot be made: a file sits where its directory goes.
    (tmp_lup_project / "lup").write_text("not a directory", encoding="utf-8")
    store = LedgerStore(tmp_lup_project, AUTHOR)
    recorder = recorder_at(tmp_lup_project)

    with caplog.at_level(logging.ERROR):
        notes = setup_notes("s3", recorder=recorder, runtime="fake")
        written = save_session(
            Result(summary="still written"), session_id="s3", recorder=recorder
        )
        standing = Session.model_validate(
            {
                "id": "abc",
                "author": AUTHOR.model_dump(),
                "at": utc_now(),
                "runtime": "fake",
                "started": utc_now(),
                "directory": "notes/traces/1.2.3/sessions/s3",
                "checkout": str(tmp_lup_project),
            }
        )
        assert standing.title == "s3"
        assert recorder.closed(standing, "completed") is None

    assert notes.record is None and notes.session.is_dir() and written.is_file()
    assert store.lines() == []
    assert caplog.text.count("the ledger refused") == 3


def test_a_recorder_exists_only_where_both_kinds_are_declared(
    tmp_lup_project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        assert session_recorder(tmp_lup_project, AUTHOR, [Session]) is None
    assert "observability:output" in caplog.text
    found = session_recorder(tmp_lup_project, AUTHOR, [Session, Output])
    assert found is not None and found.checkout == tmp_lup_project


@pytest.fixture
def launched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root a harness launch would record its transcript under."""
    monkeypatch.setattr(
        launch_session, "harness_runs_path", lambda: tmp_path / "notes" / "harness"
    )
    monkeypatch.setattr(launch_session, "agent_version", lambda: "1.2.3")
    return tmp_path


def transcript_at(
    root: Path, recorder: SessionRecorder
) -> launch_session.HarnessTranscript:
    return launch_session.start_harness_transcript(
        "claude",
        ClaudeTranscripts(root / "config"),
        root,
        model=None,
        profile=None,
        arguments=[],
        transcribe=False,
        recorder=recorder,
    )


def test_a_harness_launch_records_its_transcript_directory(launched: Path) -> None:
    store = LedgerStore(launched, AUTHOR)
    transcript = transcript_at(launched, SessionRecorder(store))

    [opened] = store.read(Session)
    assert opened.runtime == "claude" and opened.agent_version == "1.2.3"
    assert opened.directory.startswith("notes/harness/claude/")
    assert opened.journal == f"{opened.directory}/observable.jsonl"
    assert opened.title == Path(opened.directory).name
    assert store.standing(opened).label == "open"

    transcript.close(succeeded=True)

    [closed] = store.read(Session)
    assert closed.outcome == "completed"
    assert closed.journal_digest == digest.file(launched / closed.journal)
    assert store.standing(closed).label == "fresh"


def test_a_launch_that_failed_or_was_interrupted_says_so(launched: Path) -> None:
    store = LedgerStore(launched, AUTHOR)
    transcript_at(launched, SessionRecorder(store)).close(succeeded=False)
    transcript_at(launched, SessionRecorder(store)).close(
        succeeded=False, interrupted=True
    )

    assert [each.outcome for each in store.read(Session)] == ["failed", "interrupted"]


class Stamped(BaseModel):
    """A result document naming the backend that wrote it, as a run's does."""

    summary: str
    agent_sdk: str


def wrote_session(
    session_id: str, backend: str = "", saved: bool = True, failed: bool = False
) -> Path:
    """One session directory under notes/traces/, written with no recorder wired.

    The tree an existing checkout already has: the directory, a result
    document, and the trace log its writer saved — or did not, where the run
    stopped before it got that far.
    """
    notes = setup_notes(session_id)
    save_session(
        Stamped(summary="done", agent_sdk=backend)
        if backend
        else Result(summary="done"),
        session_id=session_id,
    )
    trace = TraceLogger(trace_path=notes.trace_log, title=session_id)
    if failed:
        trace.emit_event(
            TraceEvent(kind="error", timestamp=utc_now().isoformat(), brief="fell over")
        )
    if saved:
        trace.save()
    return notes.session


def launched_run(root: Path, succeeded: bool = True, ended: bool = True) -> Path:
    """One launch directory under notes/harness/, written with no recorder wired."""
    transcript = launch_session.start_harness_transcript(
        "claude",
        ClaudeTranscripts(root / "config"),
        root,
        model=None,
        profile=None,
        arguments=[],
        transcribe=False,
    )
    if ended:
        transcript.close(succeeded=succeeded)
    return transcript.journal.path.parent


def test_index_notes_records_one_closed_session_per_session_directory(
    tmp_lup_project: Path,
) -> None:
    store = LedgerStore(tmp_lup_project, AUTHOR)
    wrote_session("clean", backend="codex")
    wrote_session("broken", failed=True)
    wrote_session("unfinished", saved=False)

    swept = index_notes(recorder_at(tmp_lup_project))

    assert {(each.title, each.runtime, each.outcome) for each in swept.sessions()} == {
        ("clean", "codex", "completed"),
        ("broken", "unknown", "failed"),
        ("unfinished", "unknown", "interrupted"),
    }
    assert [each.agent_version for each in swept.sessions()] == ["1.2.3"] * 3
    assert all(each.finished() for each in swept.sessions())
    assert {each.id for each in store.read(Session)} == {
        each.id for each in swept.sessions()
    }


def test_a_swept_session_pins_the_journal_as_the_tree_holds_it(
    tmp_lup_project: Path,
) -> None:
    store = LedgerStore(tmp_lup_project, AUTHOR)
    directory = wrote_session("clean")

    [closed] = index_notes(recorder_at(tmp_lup_project)).sessions()

    assert closed.directory == spelled_under(directory, tmp_lup_project)
    assert closed.journal.startswith("notes/traces/1.2.3/logs/clean/")
    assert closed.journal_digest == digest.file(closed.journal_path())
    assert store.standing(closed).label == "fresh"
    closed.journal_path().write_text("one more line\n", encoding="utf-8")
    assert store.standing(closed).label == "stale"


def test_a_swept_session_with_no_trace_pins_nothing_and_says_so(
    tmp_lup_project: Path,
) -> None:
    store = LedgerStore(tmp_lup_project, AUTHOR)
    wrote_session("unfinished", saved=False)

    [closed] = index_notes(recorder_at(tmp_lup_project)).sessions()

    assert closed.journal == "" and closed.journal_digest == ""
    assert store.standing(closed).label == "unpinned"


def test_index_notes_records_one_output_per_result_about_its_session(
    tmp_lup_project: Path,
) -> None:
    store = LedgerStore(tmp_lup_project, AUTHOR)
    wrote_session("clean")

    swept = index_notes(recorder_at(tmp_lup_project))

    [session] = swept.sessions()
    [output] = swept.outputs()
    assert output.path.startswith("notes/traces/1.2.3/sessions/clean/")
    assert output.session == session.id and output.digest == digest.file(
        output.held_at()
    )
    assert store.standing(output).label == "fresh"
    [edge] = store.edges()
    assert (edge.kind, edge.source, edge.target) == (
        "observability:output_of",
        output.id,
        session.id,
    )


def test_index_notes_reads_a_launchs_outcome_off_its_observable_journal(
    tmp_lup_project: Path,
) -> None:
    store = LedgerStore(tmp_lup_project, AUTHOR)
    done = launched_run(tmp_lup_project)
    fell = launched_run(tmp_lup_project, succeeded=False)
    stopped = launched_run(tmp_lup_project, ended=False)

    swept = index_notes(recorder_at(tmp_lup_project))

    assert {(each.title, each.outcome) for each in swept.sessions()} == {
        (done.name, "completed"),
        (fell.name, "failed"),
        (stopped.name, "interrupted"),
    }
    [closed] = [each for each in swept.sessions() if each.title == done.name]
    assert closed.runtime == "claude"
    # Nothing in the tree says which agent version opened a launch, so the
    # record says nothing rather than taking the version running the sweep.
    assert closed.agent_version == ""
    assert closed.journal == f"notes/harness/claude/{done.name}/observable.jsonl"
    assert closed.journal_digest == digest.file(closed.journal_path())
    assert closed.started == parse_timestamp(done.name).astimezone()
    assert store.standing(closed).label == "fresh"
    assert swept.outputs() == []


def test_index_notes_run_again_records_nothing_twice(tmp_lup_project: Path) -> None:
    store = LedgerStore(tmp_lup_project, AUTHOR)
    wrote_session("clean")
    launched_run(tmp_lup_project)
    first = index_notes(recorder_at(tmp_lup_project))
    written = len(store.lines())

    again = index_notes(recorder_at(tmp_lup_project))

    assert len(first.sessions()) == 2 and len(first.outputs()) == 1
    assert again.sessions() == [] and again.outputs() == []
    assert sorted(again.known()) == sorted(each.directory for each in first.runs)
    assert len(store.lines()) == written


def test_a_session_already_recorded_as_it_ran_is_passed_over(
    tmp_lup_project: Path,
) -> None:
    recorder = recorder_at(tmp_lup_project)
    notes = setup_notes("s1", recorder=recorder, runtime="fake")

    swept = index_notes(recorder)

    assert notes.record is not None
    assert swept.sessions() == [] and swept.known() == [notes.session]


def test_the_console_indexes_the_tree_it_is_run_over(
    tmp_lup_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ledger_app, "project_root", lambda: tmp_lup_project)
    app = ledger_app.create_ledger_app([Session, Output], [OutputOf])
    wrote_session("clean")

    swept = CliRunner().invoke(app, ["index-notes"])
    again = CliRunner().invoke(app, ["index-notes"])

    assert swept.exit_code == 0, swept.output
    assert "1 session(s) and 1 output(s) recorded; 0 already indexed" in swept.output
    assert "0 session(s) and 0 output(s) recorded; 1 already indexed" in again.output
    assert len(LedgerStore(tmp_lup_project, AUTHOR).read(Session)) == 1


def test_the_console_says_where_a_project_does_not_index_its_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ledger_app, "project_root", lambda: tmp_path)
    app = ledger_app.create_ledger_app([Session])

    refused = CliRunner().invoke(app, ["index-notes"])

    assert refused.exit_code == 1
    assert "observability:output" in refused.output


def session_fields(root: Path) -> str:
    return json.dumps(
        {
            "runtime": "claude",
            "started": "2026-09-10T12:00:00+00:00",
            "directory": "notes/traces/1.2.3/sessions/s1",
            "checkout": str(root),
        }
    )


def output_fields(root: Path) -> str:
    return json.dumps(
        {
            "path": "notes/traces/1.2.3/sessions/s1/20260910_120000.json",
            "checkout": str(root),
            "produced": "2026-09-10T12:00:00+00:00",
        }
    )


def unwrapped(panel: str) -> str:
    """The console's error panel read as one line: its words, without the box's edges.

    The console wraps a refusal mid-sentence inside a drawn box, so the
    sentence is read back as words rather than matched against the drawing.
    Where the terminal colours the box, its edges arrive wrapped in escape
    codes, so the panel is read through Rich's own parser first.
    """
    plain = Text.from_ansi(panel).plain
    return " ".join(word for word in plain.split() if word != "│")


def test_the_console_refuses_bytes_on_either_kind_before_they_are_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ledger_app, "project_root", lambda: tmp_path)
    trace = tmp_path / "trace.md"
    trace.write_text("hundreds of megabytes, in spirit", encoding="utf-8")
    app = ledger_app.create_ledger_app([Session, Output])
    runner = CliRunner()

    session = runner.invoke(
        app,
        ["record", "observability:session", "", "--json", session_fields(tmp_path)]
        + ["--attach", str(trace)],
    )
    output = runner.invoke(
        app,
        ["record", "observability:output", "", "--json", output_fields(tmp_path)]
        + ["--attach", str(trace)],
    )
    types = runner.invoke(app, ["types"])

    assert session.exit_code != 0 and "pointers only" in unwrapped(session.output)
    assert output.exit_code != 0 and "pointers only" in unwrapped(output.output)
    assert "observability:session" in types.output
    assert "observability:output" in types.output
    # Nothing reached the log or the blob store: the refusal came first.
    assert LedgerStore(tmp_path, AUTHOR).lines() == []
    assert not (tmp_path / "lup" / "ledger" / "blobs").exists()


async def test_the_session_tools_refuse_bytes_the_same_way(tmp_path: Path) -> None:
    (tmp_path / "trace.md").write_text("bytes", encoding="utf-8")
    served = {
        tool.name: tool
        for tool in create_ledger_tools(tmp_path, AUTHOR, [Session, Output], [])
    }

    listed = await served["ledger_types"](NoInput())
    assert [kind.kind for kind in listed.nodes] == [
        "observability:session",
        "observability:output",
    ]
    with pytest.raises(ToolError, match="pointers only"):
        await served["ledger_record"](
            RecordInput(
                kind="observability:session",
                title="",
                fields=json.loads(session_fields(tmp_path)),
                attach=["trace.md"],
            )
        )
    assert not (tmp_path / "lup" / "ledger" / "blobs").exists()
