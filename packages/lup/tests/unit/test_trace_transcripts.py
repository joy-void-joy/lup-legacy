"""``trace verify`` and ``trace events``: a launch transcript, its chain checked first."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lup.devtools.trace.app import create_trace_app
from lup.devtools.trace.traces import TranscriptEvents
from lup.observability.audit import TraceActor, TraceContext, TraceJournal
from lup.workspace.history import run_transcript
from lup.workspace.paths import harness_runs_path

RUNNER = CliRunner()
RUN = "20260906_023938_936131_claude_0384f789"


def launched(run: str = RUN, messages: int = 2) -> Path:
    """A launch's transcript as the launcher writes it, under the default runs root."""
    path = run_transcript(harness_runs_path() / "claude" / run)
    journal = TraceJournal(
        path, TraceContext.root(run, TraceActor(kind="harness", name="launcher"))
    )
    journal.emit("run_start", {"provider": "claude"})
    for index in range(messages):
        journal.emit("message", {"text": f"line {index}"})
    journal.emit("run_end", {"succeeded": True})
    return path


def edited(path: Path, line: int) -> None:
    """Rewrite one record's payload in place, leaving its recorded hash alone."""
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[line])
    record["payload"] = {"text": "rewritten"}
    lines[line] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.usefixtures("tmp_lup_project")
def test_every_launch_is_checked_and_an_intact_one_passes() -> None:
    launched()

    result = RUNNER.invoke(create_trace_app(), ["verify"])

    assert result.exit_code == 0, result.output
    assert RUN in result.output
    assert "1 of 1 transcript(s) verify" in result.output


@pytest.mark.usefixtures("tmp_lup_project")
def test_an_edited_record_fails_the_check_and_is_named() -> None:
    edited(launched(), 2)

    result = RUNNER.invoke(create_trace_app(), ["verify", RUN])

    assert result.exit_code == 1
    assert "record 2 is not what was hashed" in result.output


@pytest.mark.usefixtures("tmp_lup_project")
def test_events_say_the_chain_first_and_mark_where_it_breaks() -> None:
    edited(launched(), 2)

    result = RUNNER.invoke(create_trace_app(), ["events", RUN])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[1].startswith("4 record(s), chain ✗ record 2 is not what was hashed")
    marker = next(i for i, line in enumerate(lines) if "breaks here" in line)
    assert "line 0" in lines[marker - 1]
    assert "rewritten" in lines[marker + 1]


@pytest.mark.usefixtures("tmp_lup_project")
def test_events_narrow_by_kind_and_read_whole_as_json() -> None:
    launched()

    result = RUNNER.invoke(
        create_trace_app(), ["events", RUN, "--kind", "message", "--json"]
    )

    assert result.exit_code == 0, result.output
    read = TranscriptEvents.model_validate_json(result.output)
    assert read.check.broken is None
    assert read.check.records == 4
    assert [event.kind for event in read.events] == ["message", "message"]


@pytest.mark.usefixtures("tmp_lup_project")
def test_a_run_nobody_launched_is_refused() -> None:
    result = RUNNER.invoke(create_trace_app(), ["events", "no-such-run"])

    assert result.exit_code == 1
    assert "error: `no-such-run` — names no launch run under" in result.output
