"""What `lup-devtools` says when its environment registers no project application, or several.

And what it answers without loading one: a session's status line runs at
every render, so its route reads the dashboard's pulse and what the runtime
hands it on stdin, and nothing else.
"""

import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer

import lup.devtools.entrypoint as entrypoint
from lup.devtools.dashboard.pulse import (
    DashboardPulse,
    PulseFile,
    PulseSession,
    StatusInput,
    status_line,
)
from lup.launch.companions import lent_directory


def registering(site: Path, name: str, target: str) -> None:
    """Install into `site` a distribution registering `target` as the project application."""
    info = site / f"{name}-0.1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1.0\n", encoding="utf-8"
    )
    (info / "entry_points.txt").write_text(
        f"[lup.devtools]\napplication = {target}\n", encoding="utf-8"
    )


def test_a_package_left_behind_by_a_rename_is_named_with_what_removes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registering(tmp_path, "before_rename", "before_rename.devtools.main:app")
    registering(tmp_path, "after_rename", "after_rename.devtools.main:app")
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(typer.Exit):
        entrypoint.project_application()

    said = capsys.readouterr().err
    assert f"before_rename.devtools.main:app, from before_rename in {tmp_path}" in said
    assert f"after_rename.devtools.main:app, from after_rename in {tmp_path}" in said
    assert "<name>.egg-info, which the editable install reads" in said
    assert "`uv sync --reinstall-package <project>`" in said


def test_an_environment_with_no_project_says_what_installs_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(entrypoint, "entry_points", lambda **_selection: [])

    with pytest.raises(typer.Exit):
        entrypoint.project_application()

    assert "→ install it, in the project: `uv sync`" in capsys.readouterr().err


def test_the_status_line_is_read_without_the_project_application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pulse = PulseFile.of(lent_directory(tmp_path))
    pulse.path.parent.mkdir(parents=True)
    lead = PulseSession(
        repository=str(tmp_path / "lup.git"),
        project="lup",
        id="lead",
        name="lead",
        worktree=str(tmp_path / "lup.git" / "tree" / "fix-x"),
        runtime=["conversation-1"],
        reviews=["41cb73e1a2b3"],
    )
    pulse.path.write_text(
        DashboardPulse(
            url="http://127.0.0.1:8766",
            pid=1,
            pending=2,
            beat=datetime.now(UTC),
            members=[lead],
        ).model_dump_json()
    )
    handed = json.dumps({"session_id": "conversation-1"})

    def refused() -> typer.Typer:
        raise AssertionError("the status line loaded the project application")

    monkeypatch.setattr(entrypoint, "project_application", refused)
    monkeypatch.setattr(
        sys, "argv", ["lup-devtools", "dashboard", "line", str(pulse.path)]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(handed))
    monkeypatch.setenv("COLUMNS", "200")

    entrypoint.main()

    printed = capsys.readouterr().out
    expected = status_line(pulse.path, StatusInput.read(io.StringIO(handed)))
    assert expected.plain() == (
        "lup · lead · tree/fix-x │ ?2 reviews (1 here: 41cb73e1) │ "
        "● http://127.0.0.1:8766"
    )
    assert printed == expected.painted() + "\n"
