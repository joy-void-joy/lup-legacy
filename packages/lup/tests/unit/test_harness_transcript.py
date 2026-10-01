"""A native CLI launch has to leave a transcript behind.

This is the guard whose absence let the feature die quietly. The wiring that
built a journal and started a watcher around an interactive launch was deleted
with the file holding it, and nothing failed: no test asserted that a launch
records anything, so the only signal was a directory that stopped filling up.
A trace nobody wrote reads exactly like a session nobody ran, which is why the
assertion has to be that the transcript exists rather than that it is correct.
"""

import json
import logging
from pathlib import Path

import pytest

from lup.providers.claude.transcripts import ClaudeTranscripts
import lup.launch.session as launch_session
from lup.observability.audit import read_observable_events


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root the launcher would write its harness transcript under."""
    monkeypatch.setattr(
        launch_session, "harness_runs_path", lambda: tmp_path / "notes" / "harness"
    )
    return tmp_path


def started(project: Path) -> launch_session.HarnessTranscript:
    """One transcript, as a launcher starts it."""
    return launch_session.start_harness_transcript(
        "claude",
        ClaudeTranscripts(project / "config"),
        project,
        model="claude-fable-5",
        profile=None,
        arguments=["--model", "claude-fable-5"],
    )


def test_starting_a_launch_opens_a_transcript(project: Path) -> None:
    transcript = started(project)
    transcript.close(succeeded=True)

    written = list((project / "notes" / "harness" / "claude").rglob("observable.jsonl"))
    assert len(written) == 1


def test_the_transcript_records_the_run_starting_and_ending(project: Path) -> None:
    transcript = started(project)
    transcript.close(succeeded=True)

    journal = next((project / "notes" / "harness").rglob("observable.jsonl"))
    kinds = [event.kind for event in read_observable_events(journal)]
    assert kinds[0] == "run_start"
    assert kinds[-1] == "run_end"


def test_a_failed_launch_is_recorded_as_one(project: Path) -> None:
    transcript = started(project)
    transcript.close(succeeded=False)

    journal = next((project / "notes" / "harness").rglob("observable.jsonl"))
    ending = read_observable_events(journal)[-1]
    assert ending.payload == {"succeeded": False}


def test_the_launch_starts_a_watcher_that_stops_on_close(project: Path) -> None:
    transcript = started(project)
    assert transcript.watcher is not None
    assert transcript.watcher.thread is not None
    assert transcript.watcher.thread.is_alive()

    transcript.close(succeeded=True)

    assert not transcript.watcher.thread.is_alive()


def test_transcription_can_be_disabled_without_losing_run_boundaries(
    project: Path,
) -> None:
    transcript = launch_session.start_harness_transcript(
        "claude",
        ClaudeTranscripts(project / "config"),
        project,
        model="claude-fable-5",
        profile=None,
        arguments=[],
        transcribe=False,
    )
    assert transcript.watcher is None
    assert transcript.diagnostics is None

    transcript.close(succeeded=True)

    journal = next((project / "notes" / "harness").rglob("observable.jsonl"))
    assert [event.kind for event in read_observable_events(journal)] == [
        "run_start",
        "run_end",
    ]


def test_the_watcher_is_scoped_to_this_project(project: Path) -> None:
    """Unscoped, it would mirror every concurrent project's sessions in here."""
    transcript = started(project)
    transcript.close(succeeded=True)

    assert transcript.watcher is not None
    assert transcript.watcher.scope == project


def test_a_credential_passed_on_the_command_line_is_not_recorded(
    project: Path,
) -> None:
    transcript = launch_session.start_harness_transcript(
        "claude",
        ClaudeTranscripts(project / "config"),
        project,
        model=None,
        profile=None,
        arguments=["--api-key", "hunter2", "--model=claude-fable-5"],
    )
    transcript.close(succeeded=True)

    journal = next((project / "notes" / "harness").rglob("observable.jsonl"))
    assert "hunter2" not in journal.read_text(encoding="utf-8")


def test_watcher_diagnostics_land_in_a_file_rather_than_the_terminal(
    project: Path,
) -> None:
    """The launcher hands its terminal to a CLI drawing over the whole screen.

    A recovered polling error reaching the last-resort handler prints a
    traceback into that UI and reads as a crash.
    """
    transcript = started(project)
    launch_session.watcher_logger().error("a recovered polling failure")
    transcript.close(succeeded=True)

    written = next((project / "notes" / "harness").rglob("watcher.log"))
    assert "a recovered polling failure" in written.read_text(encoding="utf-8")


def test_closing_releases_the_diagnostics_handler(project: Path) -> None:
    """Left attached, every launch in one process would stack another handler."""
    transcript = started(project)
    transcript.close(succeeded=True)

    assert transcript.diagnostics not in launch_session.watcher_logger().handlers


def test_the_diagnostics_logger_does_not_propagate(project: Path) -> None:
    transcript = started(project)
    try:
        assert launch_session.watcher_logger().propagate is False
    finally:
        transcript.close(succeeded=True)


def test_a_second_launch_reuses_no_stale_handler(project: Path) -> None:
    first = started(project)
    first.close(succeeded=True)
    before = len(launch_session.watcher_logger().handlers)

    second = started(project)
    second.close(succeeded=True)

    assert len(launch_session.watcher_logger().handlers) == before


def test_the_watcher_reports_its_failures_on_the_captured_logger() -> None:
    """The handler is attached by module name, so the two must agree."""
    from lup.observability.native import NativeTranscriptWatcher

    assert launch_session.watcher_logger().name == NativeTranscriptWatcher.__module__
    assert (
        logging.getLogger("lup.observability.native") is launch_session.watcher_logger()
    )


def test_a_transcript_that_verifies_closes_quietly(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started(project).close(succeeded=True)

    assert "verify" not in capsys.readouterr().out


def test_a_transcript_edited_while_open_is_said_at_close(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every launch, Claude's or Codex's, closes through here, so both are checked."""
    transcript = started(project)
    path = transcript.journal.path
    first, *rest = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(first)
    record["payload"]["model"] = "another-model"
    path.write_text("\n".join([json.dumps(record), *rest]) + "\n", encoding="utf-8")

    transcript.close(succeeded=True)

    said = capsys.readouterr().out
    assert "transcript does not verify: record 0 is not what was hashed" in said
    assert f"trace events {path.parent.name}" in said
