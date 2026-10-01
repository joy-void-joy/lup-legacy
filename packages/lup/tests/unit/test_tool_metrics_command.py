"""``tools metrics``: what the tool servers of this checkout's sessions recorded, read back."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from lup.devtools.tools import ToolMetricsReport, app
from lup.mcp.serve import harness_session_context
from lup.observability.metrics import MetricsCollector, open_metrics_sink
from lup.workspace.paths import sessions_dir

RUNNER = CliRunner()


def served(session_dir: Path, server: str, member: str = "") -> MetricsCollector:
    """A collector standing in for one tool-serving process of a session."""
    process = MetricsCollector()
    process.sink = open_metrics_sink(session_dir, server, member)
    return process


def report(*arguments: str) -> ToolMetricsReport:
    result = RUNNER.invoke(app, ["metrics", "--json", *arguments])
    assert result.exit_code == 0, result.output
    return ToolMetricsReport.model_validate_json(result.output)


@pytest.mark.usefixtures("tmp_lup_project")
def test_launched_sessions_fold_into_one_row_per_tool() -> None:
    shared = harness_session_context("harness").session_dir
    first = served(shared, "coordination", "member-a")
    first.record("coordination_send", 10.0)
    first.record("coordination_send", 30.0, is_error=True)
    second = served(shared, "coordination", "member-b")
    second.record("coordination_send", 20.0)
    served(shared, "ledger", "member-b").record("ledger_record", 5.0)

    read = report()

    assert read.processes == 3
    assert [(row.server, row.tool, row.calls, row.errors) for row in read.tools] == [
        ("coordination", "coordination_send", 3, 1),
        ("ledger", "ledger_record", 1, 0),
    ]
    assert read.tools[0].avg_ms == 20.0


@pytest.mark.usefixtures("tmp_lup_project")
def test_member_narrows_to_one_launched_session() -> None:
    shared = harness_session_context("harness").session_dir
    served(shared, "coordination", "member-a").record("coordination_send", 1.0)
    served(shared, "ledger", "member-b").record("ledger_record", 1.0)

    assert [row.tool for row in report("--member", "member-b").tools] == [
        "ledger_record"
    ]


@pytest.mark.usefixtures("tmp_lup_project")
def test_session_reads_one_session_opened_in_process() -> None:
    session_dir = sessions_dir() / "run-1"
    served(session_dir, "notes").record("review", 2.0)
    launched = harness_session_context("harness").session_dir
    served(launched, "coordination").record("coordination_send", 1.0)

    assert [row.tool for row in report("--session", "run-1").tools] == ["review"]


@pytest.mark.usefixtures("tmp_lup_project")
def test_the_table_names_each_tool_and_its_server() -> None:
    shared = harness_session_context("harness").session_dir
    served(shared, "coordination").record("coordination_send", 12.0)

    result = RUNNER.invoke(app, ["metrics"])

    assert result.exit_code == 0, result.output
    assert "1 server process(es) in 1 session directory" in result.output
    assert "coordination coordination_send" in " ".join(result.output.split())


@pytest.mark.usefixtures("tmp_lup_project")
def test_a_moment_that_is_not_one_is_refused() -> None:
    result = RUNNER.invoke(app, ["metrics", "--since", "yesterday"])

    assert result.exit_code == 1
    assert "not an ISO 8601 moment" in result.output
