"""Behavior tests for `lup-devtools feedback` against a tmp project.

Pins: status reports session counts from disk, uncommitted-session
discovery survives paths with spaces and staged renames (porcelain -z),
and `collect` rejects the contradictory --since + --all-time combination.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lup.devtools.feedback import commits
from lup_template.devtools.main import app
from lup.workspace.paths import configure, project_root
from tests.unit.repos import initialized_repo

from tests.unit.conftest import LUP_PROJECT_VERSION

runner = CliRunner()


def make_session(root: Path, session_id: str, stamp: str) -> None:
    session_dir = (
        root / "notes" / "traces" / LUP_PROJECT_VERSION / "sessions" / session_id
    )
    session_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": "2026-01-01T12:00:00",
        "output": {"summary": f"summary for {session_id}"},
    }
    (session_dir / f"{stamp}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_status_reports_session_counts(tmp_lup_project: Path) -> None:
    for i in range(10):
        make_session(tmp_lup_project, f"sess-{i:02d}", f"20260101_1200{i:02d}")

    result = runner.invoke(app, ["feedback", "status", "-v", LUP_PROJECT_VERSION])

    assert result.exit_code == 0, result.output
    assert "Session directories: 10" in result.output
    assert "Unanalyzed: 10" in result.output


def test_collect_rejects_since_with_all_time(tmp_lup_project: Path) -> None:
    result = runner.invoke(
        app, ["feedback", "collect", "--since", "2026-01-01", "--all-time"]
    )

    assert result.exit_code == 1
    assert "mutually exclusive" in result.stderr


def session_path(repo: Path, session_id: str) -> Path:
    return repo / "notes" / "traces" / LUP_PROJECT_VERSION / "sessions" / session_id


def test_uncommitted_session_ids_handle_spaces_and_renames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    git = initialized_repo(repo, tmp_path / "no-hooks")

    committed = session_path(repo, "old-name")
    committed.mkdir(parents=True)
    (committed / "result.json").write_text("{}", encoding="utf-8")
    git("add", "notes")
    git("commit", "-m", "data(sessions): seed")

    spaced = session_path(repo, "sess with space")
    spaced.mkdir(parents=True)
    (spaced / "result.json").write_text("{}", encoding="utf-8")

    git("mv", str(committed), str(session_path(repo, "renamed-session")))

    monkeypatch.chdir(repo)
    session_ids = commits.get_uncommitted_session_ids()

    assert sorted(session_ids) == ["renamed-session", "sess with space"]


ORIGINAL_ROOT = project_root()


@pytest.fixture
def isolated_root(tmp_path: Path) -> Iterator[Path]:
    configure(root=tmp_path, version="1.2.3")
    yield tmp_path
    configure(root=ORIGINAL_ROOT)


class TestToolMetricsNull:
    def write_session(self, root: Path, body: str) -> None:
        sdir = root / "notes" / "traces" / "1.2.3" / "sessions" / "s-null"
        sdir.mkdir(parents=True)
        (sdir / "result.json").write_text(body)

    def test_tools_does_not_crash_on_null_metrics(
        self, isolated_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from lup.devtools.feedback import reports

        self.write_session(
            isolated_root,
            '{"session_id": "s-null", "timestamp": "2026-01-01T00:00:00", '
            '"tool_metrics": null}',
        )
        # A null tool_metrics reads as no metrics, never as a mapping.
        reports.tools(version="1.2.3", all_versions=False, as_json=True)
        assert "Traceback" not in capsys.readouterr().err

    def test_errors_does_not_crash_on_null_metrics(
        self, isolated_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from lup.devtools.feedback import reports

        self.write_session(
            isolated_root,
            '{"session_id": "s-null", "timestamp": "2026-01-01T00:00:00", '
            '"tool_metrics": null}',
        )
        reports.errors(limit=10, version="1.2.3", all_versions=False, as_json=True)
        assert "Traceback" not in capsys.readouterr().err


class TestSessionIdsFromStatus:
    def test_versioned_layout_and_root_anchoring(self) -> None:
        from lup.devtools.feedback.commits import session_ids_from_status

        root = Path("notes/traces")
        status = "\0".join(
            [
                " M notes/traces/0.1.0/sessions/sess with space/result.json",
                "?? notes/traces/0.2.0/logs/sess-2/trace.md",
                "?? notes/other/sessions/ignored/x.json",
                "?? unrelated.py",
                "",
            ]
        )

        ids = session_ids_from_status(status, root)

        assert ids == ["sess with space", "sess-2"]

    def test_rename_source_is_discarded(self) -> None:
        from lup.devtools.feedback.commits import session_ids_from_status

        root = Path("notes/traces")
        status = "\0".join(
            [
                "R  notes/traces/0.1.0/sessions/new-id/result.json",
                "notes/traces/0.1.0/sessions/old-id/result.json",
                "",
            ]
        )

        ids = session_ids_from_status(status, root)

        assert ids == ["new-id"]
