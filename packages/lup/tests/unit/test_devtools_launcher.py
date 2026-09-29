"""Where a project's environment is, and how its programs get named.

Every one of these was a hardcoded `.venv/bin/...` before, and the failure
they guard against is not an error message: a project whose environment sits
anywhere else got a workflow that quietly did not exist for it. So what is
pinned here is that the answer follows the project rather than the layout
one project happens to use.
"""

import sys
import os
from pathlib import Path
from venv import EnvBuilder

import pytest

from lup.devtools.launcher import (
    CONSOLE_SCRIPT,
    DEFAULT_ENVIRONMENT,
    ENVIRONMENT_VARIABLE,
    console_script,
    launcher_invocation,
    project_python,
)
from lup.policy.assets.host import project_environment


def installed(root: Path, environment: str = DEFAULT_ENVIRONMENT) -> Path:
    """Put a console script where a sync into *root* would leave one."""
    binaries = root / environment / ("Scripts" if os.name == "nt" else "bin")
    binaries.mkdir(parents=True)
    script = binaries / CONSOLE_SCRIPT
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    return script


def test_an_environment_defaults_to_the_one_uv_creates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)

    assert project_environment(tmp_path) == tmp_path / DEFAULT_ENVIRONMENT


def test_a_relative_redirect_resolves_against_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`uv` reads it relative to the project, not to the working directory.

    Resolving it against the caller's cwd would answer about whichever
    directory a command happened to start in, which for a worktree workflow
    is routinely not the project being asked about.
    """
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, "envs/dev")

    assert project_environment(tmp_path) == tmp_path / "envs" / "dev"


def test_an_absolute_redirect_is_taken_as_it_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared environment is the case this exists for."""
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, str(tmp_path / "shared"))

    assert project_environment(tmp_path / "repo") == tmp_path / "shared"


def test_an_environment_inside_the_checkout_is_named_by_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `PATH` lookup would let a sibling worktree's environment answer.

    Every checkout has one, they are interchangeable to a search path, and
    the wrong one is wrong silently — which is the whole reason this spelling
    is a path rather than a name.
    """
    monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)
    script = installed(tmp_path)

    spelled = launcher_invocation(tmp_path)

    assert spelled == script.relative_to(tmp_path).as_posix()


def test_a_redirected_environment_still_resolves_inside_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory name is not the assumption — its location is."""
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, "envs/dev")
    installed(tmp_path, "envs/dev")

    assert launcher_invocation(tmp_path).startswith("envs/dev/")


def test_an_environment_outside_the_checkout_is_named_bare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """conda, pyenv, a system install: reached through `PATH` by design.

    An absolute path would be worse than useless — it is one machine's, and
    there is no per-checkout copy for a lookup to pick wrongly between.
    """
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv(ENVIRONMENT_VARIABLE, str(tmp_path / "shared"))
    installed(tmp_path, "shared")

    assert launcher_invocation(root) == CONSOLE_SCRIPT


def test_nothing_installed_is_named_bare_rather_than_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command that fails saying what is missing beats one saying a file is.

    An assembled path reads as the project being broken; the name reads as
    the tool being absent, which is what is true.
    """
    monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)

    assert console_script(tmp_path) is None
    assert launcher_invocation(tmp_path) == CONSOLE_SCRIPT


def test_caller_outside_a_venv_does_not_rename_project_script_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENVIRONMENT_VARIABLE, raising=False)
    environment = tmp_path / DEFAULT_ENVIRONMENT
    EnvBuilder(with_pip=False).create(environment)
    binaries = environment / ("Scripts" if os.name == "nt" else "bin")
    script = binaries / CONSOLE_SCRIPT
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    executable = "python.exe" if os.name == "nt" else "python"
    monkeypatch.setattr(
        sys, "executable", str(tmp_path / "unrelated-install" / executable)
    )

    assert console_script(tmp_path) == script
    assert launcher_invocation(tmp_path) == script.relative_to(tmp_path).as_posix()
    assert project_python(tmp_path) == binaries / executable
