"""Every CLI composed from the library says what to run while its manifest conflicts.

The library's `compose` wires the notice, so no application's composition
root carries a copy of it.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from lup.devtools.subapps import compose, subapp
from lup.workspace.paths import find_nearest_pyproject


@pytest.fixture(autouse=True)
def manifest_found_afresh() -> Iterator[None]:
    """The nearest manifest is cached per process, and each test stands somewhere else."""
    find_nearest_pyproject.cache_clear()
    yield
    find_nearest_pyproject.cache_clear()


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
