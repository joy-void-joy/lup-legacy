"""The gate selects the project interpreter without overriding Pyright config."""

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from lup.devtools.dev import check


@pytest.fixture
def handed_to_pyright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], tuple[dict[str, object], list[str]]]:
    """Run the gate's type check with `uv` faked, and read what it configured.

    The configuration is a temporary file the gate unlinks on its way out, so
    the fake reads it at the moment Pyright would have.
    """
    configured: dict[str, object] = {}
    invocation: list[str] = []

    def capture(*arguments: str, **_options: str) -> None:
        invocation.extend(arguments)
        if arguments[1:3] == ("pyright", "--project"):
            configured.update(
                json.loads(Path(arguments[3]).read_text(encoding="utf-8"))
            )

    monkeypatch.setattr(check, "uv", capture)
    monkeypatch.setattr(check, "project_root", lambda: tmp_path)

    def run() -> tuple[dict[str, object], list[str]]:
        check.pyright_check([])
        return configured, invocation

    return run


def interpreter_in(environment: Path) -> Path:
    interpreter = (
        environment
        / Path(sys.executable).parent.name
        / ("python.exe" if os.name == "nt" else "python")
    )
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    return interpreter


def test_relative_environment_resolves_against_the_project(
    handed_to_pyright: Callable[[], tuple[dict[str, object], list[str]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", ".venv-contained")
    interpreter = interpreter_in(tmp_path / ".venv-contained")

    configured, invocation = handed_to_pyright()

    assert configured == {"include": ["."], "exclude": []}
    assert invocation[4:] == ["--pythonpath", str(interpreter)]


def test_absolute_environment_is_named_where_it_is(
    handed_to_pyright: Callable[[], tuple[dict[str, object], list[str]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elsewhere = tmp_path / "shared" / "environments" / "lup"
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(elsewhere))
    interpreter = interpreter_in(elsewhere)

    configured, invocation = handed_to_pyright()

    assert configured == {"include": ["."], "exclude": []}
    assert invocation[4:] == ["--pythonpath", str(interpreter)]


def test_unset_uses_the_existing_default_project_interpreter(
    handed_to_pyright: Callable[[], tuple[dict[str, object], list[str]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    interpreter = interpreter_in(tmp_path / ".venv")

    configured, invocation = handed_to_pyright()

    assert configured == {"include": ["."], "exclude": []}
    assert invocation[4:] == ["--pythonpath", str(interpreter)]


def test_missing_interpreter_preserves_the_base_configuration(
    handed_to_pyright: Callable[[], tuple[dict[str, object], list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "missing")

    configured, invocation = handed_to_pyright()

    assert configured == {"include": ["."], "exclude": []}
    assert "--pythonpath" not in invocation


def test_versioned_caller_selects_the_projects_generic_interpreter(
    handed_to_pyright: Callable[[], tuple[dict[str, object], list[str]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", ".venv-contained")
    interpreter = interpreter_in(tmp_path / ".venv-contained")
    monkeypatch.setattr(
        sys, "executable", str(Path(sys.executable).with_name("python3.99"))
    )
    assert not interpreter.with_name("python3.99").exists()

    _, invocation = handed_to_pyright()

    assert invocation[4:] == ["--pythonpath", str(interpreter)]
