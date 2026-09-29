"""What `lup-devtools` says when its environment registers no project application, or several."""

from pathlib import Path

import pytest
import typer

import lup.devtools.entrypoint as entrypoint


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
    assert "`<name>.egg-info`, which the editable install reads" in said
    assert "`uv sync --reinstall-package <the project's name>`" in said


def test_an_environment_with_no_project_says_what_installs_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(entrypoint, "entry_points", lambda **_selection: [])

    with pytest.raises(typer.Exit):
        entrypoint.project_application()

    assert "`uv sync` in the project installs it" in capsys.readouterr().err
