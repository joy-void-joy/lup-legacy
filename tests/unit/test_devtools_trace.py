"""Behavior tests for `lup-devtools trace` against a tmp project.

Pins the trace listing seams: version filtering must not leak logs from
other versions, listing must be ordered by recency (parsed timestamps)
rather than by name, and `show` must exit non-zero for unknown sessions.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lup.workspace.history import iter_trace_log_files
from lup.observability.trace import TraceLogger
from lup.types import (
    LupContentBlock,
    LupTextBlock,
    LupToolResultBlock,
    LupToolUseBlock,
)
from lup_template.devtools.main import app
from lup.devtools.trace.traces import (
    scan_for_capability_gaps,
    scan_for_errors,
)

from tests.unit.conftest import LUP_PROJECT_VERSION

runner = CliRunner()

OTHER_VERSION = "9.9.9"


def make_session(root: Path, version: str, session_id: str, stamp: str) -> None:
    session_dir = root / "notes" / "traces" / version / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": "2026-01-01T12:00:00",
        "output": {"summary": f"summary for {session_id}"},
    }
    (session_dir / f"{stamp}.json").write_text(json.dumps(payload), encoding="utf-8")


def make_log(root: Path, version: str, session_id: str, stamp: str, text: str) -> None:
    log_dir = root / "notes" / "traces" / version / "logs" / session_id
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{stamp}.md").write_text(text, encoding="utf-8")


@pytest.fixture
def populated_project(tmp_lup_project: Path) -> Path:
    # Ten sessions keep resolve_version() at the exact-version scope
    for i in range(10):
        make_session(
            tmp_lup_project,
            LUP_PROJECT_VERSION,
            f"sess-{i:02d}",
            f"20260101_1200{i:02d}",
        )
    make_log(
        tmp_lup_project,
        LUP_PROJECT_VERSION,
        "sess-00",
        "20260101_120000",
        "hello from sess-00",
    )
    make_log(
        tmp_lup_project, OTHER_VERSION, "other-sess", "20260201_120000", "other version"
    )
    return tmp_lup_project


def list_session_ids(args: list[str]) -> list[str]:
    result = runner.invoke(app, ["trace", "list", *args, "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    return [t["session_id"] for t in data["traces"]]


def test_list_shows_sessions_for_version(populated_project: Path) -> None:
    ids = list_session_ids(["-v", LUP_PROJECT_VERSION])
    assert "sess-00" in ids


def test_list_version_filter_excludes_other_versions_logs(
    populated_project: Path,
) -> None:
    ids = list_session_ids(["-v", LUP_PROJECT_VERSION])
    assert "other-sess" not in ids


def test_list_all_versions_includes_everything(populated_project: Path) -> None:
    ids = list_session_ids(["--all-versions"])
    assert "other-sess" in ids
    assert "sess-00" in ids


def test_list_orders_by_recency_not_name(populated_project: Path) -> None:
    # Lexically first but chronologically newest — name ordering would bury it
    make_session(
        populated_project, LUP_PROJECT_VERSION, "aaa-newest", "20260301_120000"
    )

    ids = list_session_ids(["-v", LUP_PROJECT_VERSION])
    assert ids[0] == "aaa-newest"


def test_iter_trace_log_files_version_filter(populated_project: Path) -> None:
    all_ids = {p.parent.name for p in iter_trace_log_files()}
    assert all_ids == {"sess-00", "other-sess"}

    filtered = {
        p.parent.name for p in iter_trace_log_files(version=LUP_PROJECT_VERSION)
    }
    assert filtered == {"sess-00"}


def test_show_prints_trace_content(populated_project: Path) -> None:
    event_file = next(iter_trace_log_files(session_id="sess-00")).parent / "events.json"
    event_file.write_text('[{"type": "progress"}]', encoding="utf-8")
    result = runner.invoke(app, ["trace", "show", "sess-00"])
    assert result.exit_code == 0, result.output
    assert "hello from sess-00" in result.output
    assert '"type": "progress"' in result.output


def test_show_prints_browser_events_without_reasoning_trace(
    populated_project: Path,
) -> None:
    log_dir = (
        populated_project
        / "notes"
        / "traces"
        / LUP_PROJECT_VERSION
        / "logs"
        / "browser-only"
    )
    log_dir.mkdir(parents=True)
    (log_dir / "events.json").write_text(
        json.dumps([{"type": "agent", "status": "running"}]), encoding="utf-8"
    )

    result = runner.invoke(app, ["trace", "show", "browser-only", "--full"])

    assert result.exit_code == 0, result.output
    assert "events.json" in result.output
    assert '"status": "running"' in result.output
    assert "browser-only" in list_session_ids(["-v", LUP_PROJECT_VERSION])


def test_show_missing_session_exits_nonzero(populated_project: Path) -> None:
    result = runner.invoke(app, ["trace", "show", "does-not-exist"])
    assert result.exit_code == 1


# ── analysis reads the structured sidecar, not the markdown ───────────────


def write_trace_with_sidecar(
    root: Path, session_id: str, blocks: list[LupContentBlock]
) -> None:
    """Write a real .md trace plus its .events.jsonl sidecar for a session."""
    log_dir = root / "notes" / "traces" / LUP_PROJECT_VERSION / "logs" / session_id
    log_dir.mkdir(parents=True, exist_ok=True)
    trace = TraceLogger(trace_path=log_dir / "20260101_120000.md", title=session_id)
    for block in blocks:
        trace.log_block(block)
    trace.save()


def test_scan_for_errors_reads_sidecar(tmp_lup_project: Path) -> None:
    write_trace_with_sidecar(
        tmp_lup_project,
        "sess-fail",
        [
            LupToolUseBlock(id="a", name="fetch", input={}),
            LupToolResultBlock(tool_use_id="a", content='{"is_error": true}'),
        ],
    )

    results = scan_for_errors([LUP_PROJECT_VERSION])

    assert [r["session_id"] for r in results] == ["sess-fail"]
    assert results[0]["error_count"] == 1
    assert "fetch" in results[0]["errors"][0]


def test_sidecar_is_preferred_over_markdown_keywords(tmp_lup_project: Path) -> None:
    """A healthy result whose prose contains "error" must not be flagged: the
    structured sidecar says is_error=false, so the markdown keyword is moot."""
    write_trace_with_sidecar(
        tmp_lup_project,
        "sess-ok",
        [
            LupToolUseBlock(id="a", name="search", input={}),
            LupToolResultBlock(
                tool_use_id="a",
                content='{"is_error": false, "note": "no error occurred"}',
            ),
        ],
    )

    assert scan_for_errors([LUP_PROJECT_VERSION]) == []


def test_scan_for_capability_gaps_reads_sidecar(tmp_lup_project: Path) -> None:
    write_trace_with_sidecar(
        tmp_lup_project,
        "sess-wish",
        [LupTextBlock(text="A tool that queries PyPI would be useful here.")],
    )

    results = scan_for_capability_gaps([LUP_PROJECT_VERSION])

    assert len(results) == 1
    assert "PyPI" in results[0]["text"]
    assert results[0]["session_ids"] == ["sess-wish"]


class TestToolCallView:
    def test_renders_calls_from_the_events_sidecar(self, tmp_path: Path) -> None:
        from lup.devtools.trace.traces import render_tool_calls

        trace_path = tmp_path / "t.md"
        trace = TraceLogger(trace_path=trace_path, title="t")
        trace.log_block(LupToolUseBlock(id="a", name="search", input={"q": "x"}))
        trace.log_block(LupToolResultBlock(tool_use_id="a", content="done"))
        trace.log_block(LupTextBlock(text="some prose, not a tool call"))

        out = render_tool_calls(trace_path)

        assert "✓ search" in out
        assert "prose" not in out

    def test_no_tool_calls_recorded(self, tmp_path: Path) -> None:
        from lup.devtools.trace.traces import render_tool_calls

        trace_path = tmp_path / "t.md"
        trace = TraceLogger(trace_path=trace_path, title="t")
        trace.log_block(LupTextBlock(text="Just some narrative text."))
        trace.save()

        assert render_tool_calls(trace_path) == "(no tool calls recorded)"
