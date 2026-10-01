"""Every CLI composed from the library says what to run while its manifest conflicts.

The notice used to be wired by each application's own composition root, so
every adopter carried a copy of it; the library's `compose` wires it now.
"""

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from lup.devtools.subapps import compose, subapp


def composed() -> typer.Typer:
    """A root composed from one sub-app, as an application composes its CLI."""
    leaf = typer.Typer()

    @leaf.command()
    def ping() -> None:
        typer.echo("pong")

    root = typer.Typer()
    compose(root, [subapp("leaf", "A leaf.", leaf)])
    return root


def test_a_composed_cli_names_the_launcher_while_the_manifest_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\n<<<<<<< ours\nname = "a"\n=======\nname = "b"\n>>>>>>> theirs\n'
    )
    monkeypatch.chdir(tmp_path)

    ran = CliRunner().invoke(composed(), ["leaf", "ping"])

    assert ran.exit_code == 0, ran.output
    assert "holds conflict markers" in ran.output
    assert "pong" in ran.output


def test_a_settled_manifest_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "a"\n')
    monkeypatch.chdir(tmp_path)

    ran = CliRunner().invoke(composed(), ["leaf", "ping"])

    assert ran.exit_code == 0, ran.output
    assert ran.output == "pong\n"
