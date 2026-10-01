"""A harness launch naming no effort passes the model's default, as code does.

A session declared in code that names no effort thinks at ``xhigh``, clamped
to what its model's catalog row takes. ``harness claude`` and ``harness codex``
launch the same declaration, so a launch naming none thinks at the same
default rather than at whatever the CLI's own settings happened to say. A
named effort the model's row lacks is refused on the command line, before
anything is generated.
"""

from pathlib import Path

import pytest

import lup.devtools.harness.launch as launch
import lup.providers.claude.launch as claude_launch
from lup.devtools.utils import Refusal
from lup.launch.declaration import LaunchSandbox
from tests.unit.harness_launch import Caught, checkout, composition, profiles, stub_host


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return checkout(tmp_path)


@pytest.fixture
def caught(root: Path, monkeypatch: pytest.MonkeyPatch) -> Caught:
    monkeypatch.setattr(claude_launch, "settle_claude_theme", lambda *_a, **_k: None)
    return stub_host(monkeypatch, root)


def claude_effort(
    root: Path, caught: Caught, model: str, effort: str | None
) -> list[str]:
    """What ``harness claude --model <model>`` hands the CLI as its effort."""
    launch.launch_claude(
        composition(root, "claude"),
        launch.LaunchRequest(model=model, effort=effort, sandbox=LaunchSandbox.INNER),
        profiles(),
        False,
    )
    arguments = caught.argv
    return (
        arguments[arguments.index("--effort") :][:2] if "--effort" in arguments else []
    )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("opus", ["--effort", "xhigh"]),
        ("claude-opus-4-6", ["--effort", "high"]),
        ("haiku", []),
    ],
)
def test_claude_with_no_effort_flag_passes_the_models_default(
    root: Path, caught: Caught, model: str, expected: list[str]
) -> None:
    assert claude_effort(root, caught, model, None) == expected


def test_claude_passes_a_named_effort_unchanged(root: Path, caught: Caught) -> None:
    assert claude_effort(root, caught, "opus", "max") == ["--effort", "max"]


def test_a_named_effort_the_model_lacks_is_the_command_lines_mistake(
    root: Path, caught: Caught
) -> None:
    with pytest.raises(Refusal) as lacked:
        claude_effort(root, caught, "haiku", "max")
    with pytest.raises(Refusal) as unknown:
        claude_effort(root, caught, "opus", "enormous")
    assert "haiku" in lacked.value.said["why"]
    assert "not an effort" in unknown.value.said["why"]
    assert caught.events == []


def test_a_model_no_catalog_lists_is_passed_through_for_the_cli_to_judge(
    root: Path, caught: Caught
) -> None:
    claude_effort(root, caught, "claude-opus-9-preview", None)

    assert caught.argv[caught.argv.index("--model") + 1] == "claude-opus-9-preview"


def test_codex_with_no_effort_flag_passes_the_models_default(
    root: Path, caught: Caught
) -> None:
    launch.launch_codex(
        composition(root, "codex"),
        launch.LaunchRequest(model="gpt-5.5", sandbox=LaunchSandbox.INNER),
        None,
        False,
        False,
    )

    assert 'model_reasoning_effort="xhigh"' in caught.argv
