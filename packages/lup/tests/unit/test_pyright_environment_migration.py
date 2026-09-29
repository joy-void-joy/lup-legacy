"""Retiring scaffold selectors must preserve each adopter's environment choices."""

from pathlib import Path
import tomllib

import pytest
from tomlkit.exceptions import ParseError
from typer.testing import CliRunner

import lup.devtools.dev.app as app_mod
from lup.devtools.dev.declarations import DevDeclarations
from lup.devtools.dev.migrations import MigrationRecord, retire_pyright_environment
from lup.devtools.harness.composition import NativeTargets


DEFAULT_MANIFEST = """# Project-owned comments survive the migration.
[project]
name = "meeting_app"
version = "1.0.0"

[tool.pyright]
venvPath = "."
venv = ".venv"
pythonVersion = "3.14"
reportUnknownMemberType = "error"

[[tool.pyright.executionEnvironments]]
root = "src"
extraPaths = ["support"]
"""


def test_retiring_defaults_preserves_the_other_project_configuration(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(DEFAULT_MANIFEST, encoding="utf-8")
    expected = tomllib.loads(DEFAULT_MANIFEST)
    del expected["tool"]["pyright"]["venvPath"]
    del expected["tool"]["pyright"]["venv"]

    assert retire_pyright_environment(tmp_path)

    actual = manifest.read_text(encoding="utf-8")
    assert tomllib.loads(actual) == expected
    assert actual.startswith("# Project-owned comments survive the migration.\n")
    assert retire_pyright_environment(tmp_path) == []
    assert manifest.read_text(encoding="utf-8") == actual


def test_dry_run_reports_the_exact_change_without_writing(tmp_path: Path) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(DEFAULT_MANIFEST, encoding="utf-8")

    proposed = retire_pyright_environment(tmp_path, dry_run=True)

    assert proposed
    assert manifest.read_text(encoding="utf-8") == DEFAULT_MANIFEST
    assert retire_pyright_environment(tmp_path) == proposed


@pytest.mark.parametrize(
    "settings",
    [
        '[tool.pyright]\nvenvPath = "."\nvenv = "analysis"\n',
        '[tool.pyright]\nvenvPath = "/opt/envs"\nvenv = ".venv"\n',
        '[tool.pyright]\nvenvPath = "./"\nvenv = ".venv"\n',
        '[tool.pyright]\nvenvPath = "."\n',
        '[tool.pyright]\nvenv = ".venv"\n',
        '[tool.pyright]\npythonVersion = "3.14"\n',
        '[project]\nname = "owned"\n',
        '[tool.pyright]\nvenvPath = "."\nvenv = ".venv"\nextends = "base.json"\n',
    ],
)
def test_custom_partial_absent_and_inherited_settings_stay_byte_identical(
    tmp_path: Path, settings: str
) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(settings, encoding="utf-8")

    assert retire_pyright_environment(tmp_path) == []
    assert manifest.read_text(encoding="utf-8") == settings


def test_a_separate_pyright_config_is_never_rewritten(tmp_path: Path) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(DEFAULT_MANIFEST, encoding="utf-8")
    sidecar = tmp_path / "pyrightconfig.json"
    custom = '{"venvPath": "/home/owner/envs", "venv": "chosen"}\n'
    sidecar.write_text(custom, encoding="utf-8")

    assert retire_pyright_environment(tmp_path)
    assert sidecar.read_text(encoding="utf-8") == custom


def test_invalid_manifest_is_reported_without_writing(tmp_path: Path) -> None:
    manifest = tmp_path / "pyproject.toml"
    broken = '[tool.pyright]\nvenv = "unfinished\n'
    manifest.write_text(broken, encoding="utf-8")

    with pytest.raises(ParseError):
        retire_pyright_environment(tmp_path)

    assert manifest.read_text(encoding="utf-8") == broken


def test_declared_migration_command_supports_dry_run_and_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(DEFAULT_MANIFEST, encoding="utf-8")
    monkeypatch.setattr(app_mod, "project_root", lambda: tmp_path)

    def no_declarations() -> DevDeclarations:
        raise AssertionError(
            "An environment migration must not change domain declarations"
        )

    app = app_mod.create_dev_app(
        declared=no_declarations,
        native_targets=NativeTargets(builders={}),
        repository_writers=[],
        relocate_roots=[],
    )
    migration = next(
        item
        for item in MigrationRecord().pending()
        if "tool.pyright.venv" in item.subjects
    )
    command = migration.steps[0].command
    assert command[:4] == ["uv", "run", "lup-devtools", "dev"]
    runner = CliRunner()

    preview = runner.invoke(app, [*command[4:], "--dry-run"])

    assert preview.exit_code == 0, preview.output
    assert "Would change:" in preview.output
    assert manifest.read_text(encoding="utf-8") == DEFAULT_MANIFEST

    applied = runner.invoke(app, command[4:])

    assert applied.exit_code == 0, applied.output
    assert "Changed:" in applied.output
    assert "venv" not in tomllib.loads(manifest.read_text())["tool"]["pyright"]

    again = runner.invoke(app, command[4:])

    assert again.exit_code == 0, again.output
    assert "No unchanged scaffold" in again.output
