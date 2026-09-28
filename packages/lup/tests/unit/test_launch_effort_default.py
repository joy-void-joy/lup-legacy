"""A native launch naming no effort passes the model's default, as code does.

A session declared in code that names no effort thinks at ``xhigh``, clamped
to what its model's catalog row takes. ``harness claude`` and ``harness codex``
start the same CLIs by hand, and a launch that passed nothing would think at
whatever the CLI's own settings happened to say instead.
"""

from pathlib import Path
from unittest.mock import Mock

import pytest
import sh

import lup.devtools.harness.launch as launch
from lup.launch.session import LaunchOpening
from lup.launch.declaration import LaunchSandbox
from lup.harness.messaging import WakeSockets


class Transcript:
    """The launch-facing half of a transcript, closing without a trace."""

    def __init__(self) -> None:
        self.journal = Mock()

    def close(self, *, succeeded: bool, interrupted: bool = False) -> None:
        del succeeded, interrupted


def composition() -> Mock:
    """A composition carrying the one plugin each launcher reads first."""
    plugin = Mock()
    plugin.name = "lup"
    plugin.marketplace = "test"
    built = Mock()
    built.recipe.source.plugins = [plugin]
    # Declined, so no launch here binds a wake socket in the machine's directory.
    built.recipe.source.image.wake_sockets = WakeSockets(directory="")
    return built


def launched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Stub every launch side effect, and collect the arguments each CLI gets."""
    seen: list[list[str]] = []

    def argv(
        name: str, arguments: list[str], *args: object, **kwargs: object
    ) -> list[str]:
        del args, kwargs
        seen.append(list(arguments))
        return [name]

    monkeypatch.setattr(
        launch,
        "ready_to_open",
        lambda *a, **k: LaunchOpening(sandbox=LaunchSandbox.INNER),
    )
    monkeypatch.setattr(launch, "project_root", lambda: tmp_path)
    monkeypatch.setattr(launch, "ambient_config_home", lambda *a, **k: tmp_path)
    monkeypatch.setattr(launch, "session_argv", argv)
    monkeypatch.setattr(
        launch,
        "claude_sandbox_arguments",
        lambda _plugin, sandbox=LaunchSandbox.INNER, accessible=[], settings=None, tree=None: [],
    )
    monkeypatch.setattr(
        launch,
        "codex_sandbox_arguments",
        lambda _plugin, _environment, _args, sandbox=LaunchSandbox.INNER, accessible=[], tree=None: [],
    )
    monkeypatch.setattr(launch, "non_interactive_environment", lambda _env: {})
    monkeypatch.setattr(
        launch, "apply_sandbox_environment", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(launch, "ClaudeTranscripts", lambda _home: Mock())
    monkeypatch.setattr(launch, "CodexTranscripts", lambda _home: Mock())
    monkeypatch.setattr(launch, "CodexWorktreeHomeStore", lambda **_: Mock())
    monkeypatch.setattr(
        launch,
        "select_codex_home",
        lambda *args: Mock(path=tmp_path / "home", isolated=False),
    )
    monkeypatch.setattr(launch, "codex_login_preflight", lambda *args: None)
    monkeypatch.setattr(launch, "accessible_roots", lambda: [])
    monkeypatch.setattr(
        launch, "start_harness_transcript", lambda *args, **kwargs: Transcript()
    )
    monkeypatch.setattr(sh, "Command", lambda _name: lambda *args, **kwargs: None)
    return seen


def claude_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str, effort: str | None
) -> list[str]:
    """What ``harness claude --model <model>`` hands the CLI as its effort."""
    seen = launched(tmp_path, monkeypatch)
    profiles = Mock()
    profiles.launch_home.return_value = None

    launch.launch_claude(composition(), [], profiles, None, model, False, effort=effort)

    arguments = seen[0]
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    expected: list[str],
) -> None:
    assert claude_effort(tmp_path, monkeypatch, model, None) == expected


def test_claude_passes_a_named_effort_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert claude_effort(tmp_path, monkeypatch, "opus", "max") == ["--effort", "max"]


def test_codex_with_no_effort_flag_passes_the_models_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = launched(tmp_path, monkeypatch)

    launch.launch_codex(composition(), [], None, None, "gpt-5.5", False, False)

    assert 'model_reasoning_effort="xhigh"' in seen[0]
