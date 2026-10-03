"""Who occupies an environment, read off the metadata installers already write.

The failure this exists to name: an absolute ``UV_PROJECT_ENVIRONMENT`` is
one directory for every project on the machine, and `uv sync` makes the
environment match the project it was run for — uninstalling whoever else was
there. That succeeds, so nothing reports it, and the other project finds out
later as an import error in a checkout nobody touched.

Nothing here stamps anything. ``direct_url.json`` is written by every
installer and records where a distribution was installed from, so an
environment answers for itself and answers correctly for one this project has
never run in.
"""

import json
from pathlib import Path

import pytest
import typer

from lup.devtools.dev.environment import (
    foreign_installs,
    installed_from,
    sync_environment,
)


def install(environment: Path, name: str, source: Path, editable: bool = True) -> None:
    """Write the metadata an editable install of *source* would leave behind."""
    metadata = (
        environment / "lib" / "python3.14" / "site-packages" / f"{name}.dist-info"
    )
    metadata.mkdir(parents=True, exist_ok=True)
    (metadata / "direct_url.json").write_text(
        json.dumps({"url": source.as_uri(), "dir_info": {"editable": editable}}),
        encoding="utf-8",
    )


def test_an_environment_names_the_checkouts_installed_into_it(tmp_path: Path) -> None:
    environment = tmp_path / "venv"
    install(environment, "alpha-1.0", tmp_path / "alpha")
    install(environment, "beta-1.0", tmp_path / "beta")

    assert installed_from(environment) == [tmp_path / "alpha", tmp_path / "beta"]


def test_a_wheel_from_an_index_claims_nothing(tmp_path: Path) -> None:
    """Editable alone, because that is what names a checkout.

    A distribution installed from an index records the index, which says
    nothing about who owns the environment.
    """
    environment = tmp_path / "venv"
    install(environment, "vendored-2.0", tmp_path / "wheelhouse", editable=False)

    assert installed_from(environment) == []


def test_an_unreadable_record_is_skipped_rather_than_raised_on(tmp_path: Path) -> None:
    """A corrupt file must not come between a project and its own environment."""
    environment = tmp_path / "venv"
    install(environment, "alpha-1.0", tmp_path / "alpha")
    broken = environment / "lib/python3.14/site-packages/broken-1.0.dist-info"
    broken.mkdir(parents=True)
    (broken / "direct_url.json").write_text("{not json", encoding="utf-8")

    assert installed_from(environment) == [tmp_path / "alpha"]


def test_a_workspace_member_is_not_somebody_else(tmp_path: Path) -> None:
    """Belonging is containment, not equality.

    This repository installs itself and ``packages/lup`` from inside one
    checkout. Reading the second as a foreign project would report every
    correctly-synced environment as borrowed, which is the reading that would
    have made the guard useless.
    """
    root = tmp_path / "repo"
    environment = tmp_path / "venv"
    install(environment, "app-1.0", root)
    install(environment, "lib-1.0", root / "packages" / "lup")

    assert foreign_installs(root, environment) == []


def test_another_project_in_the_same_environment_is_named(tmp_path: Path) -> None:
    """The nori case: two checkouts, one absolute variable, one directory."""
    root = tmp_path / "repo"
    other = tmp_path / "other"
    environment = tmp_path / "venv"
    install(environment, "app-1.0", root)
    install(environment, "other-1.0", other)

    assert foreign_installs(root, environment) == [other]


def test_a_sync_refuses_to_write_over_another_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused before the install, which is the only moment it can be refused.

    Afterwards the other project's packages are gone and the command that
    removed them reports success, because removing them is what `uv sync` is
    for.

    The variable is set to the seeded environment because that *is* the
    arrangement under test: an absolute value, outside the project, shared
    with whoever else points at it. Left to the ambient one, the lookup lands
    on a directory beside a checkout this test never creates, finds no
    occupant, and the refusal never fires -- which reaches the run as `uv`
    forking into a missing cwd rather than as the assertion failing.
    """
    root = tmp_path / "repo"
    environment = tmp_path / "venv"
    install(environment, "other-1.0", tmp_path / "other")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(environment))

    with pytest.raises(typer.Exit):
        sync_environment(root=root, take_over=False)


def test_taking_over_is_sayable_rather_than_walled_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Somebody moving between two projects that share one is obeying the
    configuration, and must be able to say so without editing their shell.

    Pointed at the seeded environment for the reason the refusal above is,
    and with more riding on it: without an occupant found there is nothing to
    take over, so the assertion below passes on a run that never reached the
    branch it names.
    """
    root = tmp_path / "repo"
    environment = tmp_path / "venv"
    install(environment, "other-1.0", tmp_path / "other")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(environment))
    synced: list[Path] = []
    monkeypatch.setattr("lup.devtools.dev.environment.sync_dependencies", synced.append)

    sync_environment(root=root, take_over=True)

    assert synced == [root]
    assert (
        f"Taking {environment} over from 1 other project(s)." in capsys.readouterr().out
    )
