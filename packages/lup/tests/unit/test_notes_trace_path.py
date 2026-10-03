"""Behavior tests for the notes/trace path layout.

Pins the seam between writer and readers: setup_notes() must put the trace log at
notes/traces/<version>/logs/<session_id>/<timestamp>.md (the location
the trace/feedback devtools scan), with a parseable timestamp, and the
session/output dirs under the same version root.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from lup.workspace import paths
from lup.devtools.trace.traces import find_trace, session_id_from_path
from lup.workspace.notes import setup_notes
from lup.workspace.paths import (
    parse_timestamp,
    path_is_under,
    sessions_dir,
    trace_logs_dir,
    traces_path,
)


@pytest.fixture
def tmp_project(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.lup]\nagent_version = "1.2.3"\n', encoding="utf-8"
    )
    old_root = paths.project_root()
    paths.configure(root=tmp_path)
    yield tmp_path
    paths.configure(root=old_root)


def test_trace_log_lands_in_versioned_logs_dir(tmp_project: Path) -> None:
    notes = setup_notes("session-abc", "task-1")

    logs_root = tmp_project / "notes" / "traces" / "1.2.3" / "logs"
    assert notes.trace_log.parent == logs_root / "session-abc"
    parse_timestamp(notes.trace_log.name)


def test_session_and_output_dirs_share_the_version_root(tmp_project: Path) -> None:
    notes = setup_notes("session-abc", "task-1")

    version_root = tmp_project / "notes" / "traces" / "1.2.3"
    assert notes.session == version_root / "sessions" / "session-abc"
    assert notes.session.is_dir()
    assert notes.output.parent == version_root / "outputs" / "task-1"
    assert notes.output.is_dir()


@pytest.mark.usefixtures("tmp_project")
def test_trace_log_dir_is_created_but_not_writable_grant() -> None:
    notes = setup_notes("session-abc", "task-1")

    assert notes.trace_log.parent.is_dir()
    assert all(notes.trace_log.parent != rw for rw in notes.rw)


def test_legacy_raw_trace_wins_over_empty_collected_session(
    tmp_project: Path,
) -> None:
    session_id = "legacy-session"
    raw = traces_path() / session_id / "210412.md"
    raw.parent.mkdir(parents=True)
    raw.write_text("full reasoning", encoding="utf-8")
    collected = traces_path() / "1.2.3" / "sessions" / session_id
    collected.mkdir(parents=True)

    assert find_trace(session_id) == raw
    assert session_id_from_path(raw) == session_id


class TestNotesReadOnlyExcludesLogs:
    def test_logs_dir_not_in_ro_grant(self, tmp_path: Path) -> None:
        """The agent's RO grant must not cover the feedback-loop logs dir."""
        saved_config = paths.state.config
        try:
            paths.configure(notes_dir=tmp_path / "notes", version="test")
            notes = setup_notes(session_id="s1", task_id="t1")

            logs_file = trace_logs_dir() / "s1" / "20200101_000000.md"
            assert not path_is_under(logs_file, notes.ro)
            assert not path_is_under(logs_file, notes.all_dirs)

            # Earlier sessions and outputs are readable.
            assert path_is_under(sessions_dir() / "other" / "x.json", notes.ro)
            assert path_is_under(trace_logs_dir(), [trace_logs_dir()])
        finally:
            paths.state.config = saved_config
