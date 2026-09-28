"""Application checkpoints around both native harness launchers."""

from pathlib import Path
from unittest.mock import Mock

import pytest
import sh

import lup.devtools.harness.launch as launch
from lup.launch.session import LaunchOpening
from lup.launch.declaration import LaunchSandbox
from lup.launch.preflight import LaunchSentinels
from lup.harness.messaging import WakeSockets


class Transcript:
    """The launch-facing half of a transcript, with observable closure."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.journal = Mock()

    def close(self, *, succeeded: bool, interrupted: bool = False) -> None:
        del interrupted
        self.events.append(f"close:{succeeded}")


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


def checkpoint(events: list[str]) -> launch.LaunchCheckpoint:
    """Record the provider a project checkpoint receives."""

    def record(*, provider: str) -> None:
        events.append(f"checkpoint:{provider}")

    return record


@pytest.mark.parametrize("sandbox", list(LaunchSandbox))
def test_claude_checkpoints_before_preflight_and_after_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox: LaunchSandbox
) -> None:
    events: list[str] = []
    profiles = Mock()
    profiles.launch_home.return_value = None
    preflight = Mock(
        side_effect=lambda *a, **k: (
            events.append("ready") or LaunchOpening(sandbox=k["sandbox"])
        )
    )
    monkeypatch.setattr(
        launch,
        "ready_to_open",
        preflight,
    )
    monkeypatch.setattr(launch, "project_root", lambda: tmp_path)
    monkeypatch.setattr(launch, "carry_claude_home", lambda *a, **k: None)
    monkeypatch.setattr(launch, "ambient_config_home", lambda *a, **k: tmp_path)
    monkeypatch.setattr(launch, "session_argv", lambda name, *a, **k: [name])
    monkeypatch.setattr(
        launch,
        "claude_sandbox_arguments",
        lambda _plugin, sandbox=LaunchSandbox.INNER, accessible=[], settings=None, tree=None: [],
    )
    monkeypatch.setattr(launch, "non_interactive_environment", lambda _env: {})
    monkeypatch.setattr(
        launch, "apply_sandbox_environment", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(launch, "ClaudeTranscripts", lambda _home: Mock())
    monkeypatch.setattr(launch, "accessible_roots", lambda: [])
    monkeypatch.setattr(
        launch,
        "start_harness_transcript",
        lambda *args, **kwargs: Transcript(events),
    )
    monkeypatch.setattr(
        sh,
        "Command",
        lambda _name: lambda *args, **kwargs: events.append("cli"),
    )

    launch.launch_claude(
        composition(),
        [],
        profiles,
        None,
        None,
        False,
        checkpoint=checkpoint(events),
        sandbox=sandbox,
    )

    assert preflight.call_args.kwargs["sandbox"] is sandbox
    assert events == [
        "checkpoint:claude",
        "ready",
        "cli",
        "close:True",
        "checkpoint:claude",
    ]


@pytest.mark.parametrize("sandbox", list(LaunchSandbox))
def test_codex_checkpoints_before_preflight_and_after_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox: LaunchSandbox
) -> None:
    events: list[str] = []
    home = Mock(path=tmp_path / "home", isolated=False)
    store = Mock()
    preflight = Mock(
        side_effect=lambda *a, **k: (
            events.append("ready") or LaunchOpening(sandbox=k["sandbox"])
        )
    )
    monkeypatch.setattr(
        launch,
        "ready_to_open",
        preflight,
    )
    monkeypatch.setattr(launch, "project_root", lambda: tmp_path)
    monkeypatch.setattr(launch, "carry_claude_home", lambda *a, **k: None)
    monkeypatch.setattr(launch, "ambient_config_home", lambda *a, **k: tmp_path)
    monkeypatch.setattr(launch, "session_argv", lambda name, *a, **k: [name])
    monkeypatch.setattr(launch, "non_interactive_environment", lambda _environment: {})
    monkeypatch.setattr(
        launch,
        "codex_sandbox_arguments",
        lambda _plugin, _environment, _args, sandbox=LaunchSandbox.INNER, accessible=[], tree=None: [],
    )
    monkeypatch.setattr(launch, "CodexWorktreeHomeStore", lambda **_: store)
    monkeypatch.setattr(launch, "select_codex_home", lambda *args: home)
    monkeypatch.setattr(launch, "codex_login_preflight", lambda *args: None)
    monkeypatch.setattr(launch, "CodexTranscripts", lambda _home: Mock())
    monkeypatch.setattr(launch, "accessible_roots", lambda: [])
    monkeypatch.setattr(
        launch,
        "start_harness_transcript",
        lambda *args, **kwargs: Transcript(events),
    )
    monkeypatch.setattr(
        sh,
        "Command",
        lambda _name: lambda *args, **kwargs: events.append("cli"),
    )

    launch.launch_codex(
        composition(),
        [],
        None,
        None,
        None,
        False,
        False,
        checkpoint=checkpoint(events),
        sandbox=sandbox,
    )

    assert preflight.call_args.kwargs["sandbox"] is sandbox
    assert events == [
        "checkpoint:codex",
        "ready",
        "cli",
        "close:True",
        "checkpoint:codex",
    ]


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_generate_only_never_checkpoints(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    monkeypatch.setattr(launch, "ready_to_open", lambda *args, **kwargs: None)
    if provider == "claude":
        launch.launch_claude(
            composition(),
            [],
            Mock(),
            None,
            None,
            True,
            checkpoint=checkpoint(events),
        )
    else:
        launch.launch_codex(
            composition(),
            [],
            None,
            None,
            None,
            True,
            False,
            checkpoint=checkpoint(events),
        )

    assert events == []


def test_opening_one_runtime_generates_every_declared_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What makes launching a runtime mean what `harness generate all` means.

    A shared source moves both trees, so a launcher that generated only its
    own left the other stale until somebody ran the selector by hand -- and
    what surfaced it was `dev check` failing on drift the session had not
    introduced.
    """
    generated: list[object] = []
    monkeypatch.setattr(
        launch,
        "generate_with_report",
        lambda composition, in_passing=False: generated.append(composition),
    )
    monkeypatch.setattr(
        launch,
        "generate_targets",
        lambda compositions, writers, in_passing=False: generated.extend(
            [*compositions, *writers]
        ),
    )
    opened, sibling, writer = composition(), composition(), Mock()

    assert (
        launch.ready_to_open(opened, True, LaunchSentinels(), [sibling], [writer])
        is None
    )
    assert generated == [opened, sibling, writer]


def test_a_launch_names_the_waits_it_spends_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One line before each quiet stretch, and none when only generation was asked."""
    monkeypatch.setattr(
        launch, "generate_with_report", lambda composition, in_passing=False: None
    )
    monkeypatch.setattr(
        launch,
        "generate_targets",
        lambda compositions, writers, in_passing=False: None,
    )
    monkeypatch.setattr(launch, "project_root", lambda: tmp_path)
    monkeypatch.setattr(launch, "carry_claude_home", lambda *a, **k: None)
    monkeypatch.setattr(launch, "sweep_ledgers", lambda root: 0)
    monkeypatch.setattr(launch, "exclude_sandbox_placeholders", lambda root: [])
    monkeypatch.setattr(launch, "runtime_preflight", lambda *a, **k: [])
    monkeypatch.setattr(launch, "settle_base_freshness", lambda *a, **k: None)

    assert launch.ready_to_open(composition(), True, LaunchSentinels()) is None
    assert capsys.readouterr().out == ""

    # Named, so the host is not asked which posture the default settles to.
    assert (
        launch.ready_to_open(
            composition(), False, LaunchSentinels(), sandbox=LaunchSandbox.OUTER
        )
        is not None
    )
    assert capsys.readouterr().out.splitlines() == [
        "regenerating what this session opens against",
        "checking the host",
    ]
