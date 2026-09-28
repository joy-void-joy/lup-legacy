"""The sandbox a launch opens under when its command line names none.

The default is the verified container, and a host with no container client
cannot start one. So the default, and only the default, falls back to the
runtime's own sandbox on the host and says so once, at warning level. An
explicit `--sandbox outer` asked for the container by name and is refused
rather than degraded; an explicit `inner` or `none` is taken as said, without
the host being asked anything.

Each case runs both launchers through the real gate, because the fallback is
a property of the launch rather than of either runtime: a runtime that
settled its default apart from the other is how one of them would end up
quietly contained, or quietly not.
"""

import inspect
from pathlib import Path
from unittest.mock import Mock

import pytest
import sh
import typer
from typer.testing import CliRunner

import lup.launch.container as contained
import lup.devtools.harness.launch as launch
import lup.launch.declaration as declaration
from lup.launch.declaration import LaunchSandbox
from lup.devtools.harness.app import create_harness_app
from lup.devtools.harness.composition import NativeTargets
from lup.harness.generate import NativeHarnessComposition
from lup.devtools.harness.launch import launch_claude, launch_codex
from lup.launch.session import runtime_preflight, session_argv
from lup.harness.codescan.common import RuleSelection
from lup.harness.image import ContainerClient
from lup.harness.messaging import WakeSockets


def composition() -> Mock:
    """A composition carrying what both launchers read before the argv."""
    plugin = Mock()
    plugin.name = "lup"
    built = Mock()
    built.recipe.source.plugins = [plugin]
    built.recipe.source.image.forge.sourced.return_value = ""
    # Declined, so no launch here binds a wake socket in the machine's directory.
    built.recipe.source.image.wake_sockets = WakeSockets(directory="")
    return built


def host(monkeypatch: pytest.MonkeyPatch, client: ContainerClient | None) -> None:
    """Answer the launcher's own probe as a host with this client, or with none."""
    monkeypatch.setattr(declaration, "detected_client", lambda: client)


def unprobed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the launcher asks the host for a container client."""

    def probed() -> ContainerClient | None:
        raise AssertionError("the launcher asked the host for a container client")

    monkeypatch.setattr(declaration, "detected_client", probed)


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Mock:
    """Stub everything a launch touches besides its gate, and record what reached it.

    The gate itself -- `ready_to_open`, where the default is settled -- runs
    for real; what it would generate, sweep and measure is stubbed, and so is
    everything after it that would reach this machine. `session_argv` is
    recorded rather than run, so a test that needs its container branch puts
    the real one back.
    """
    recorded = Mock()
    recorded.preflight.return_value = []
    recorded.claude_sandbox.return_value = []
    recorded.codex_sandbox.return_value = []
    recorded.session_argv.side_effect = lambda cli, *a, **k: [cli]
    monkeypatch.setattr(launch, "generate_with_report", lambda *a, **k: None)
    monkeypatch.setattr(launch, "generate_targets", lambda *a, **k: None)
    monkeypatch.setattr(launch, "project_root", lambda: tmp_path)
    monkeypatch.setattr(launch, "carry_claude_home", lambda *a, **k: None)
    monkeypatch.setattr(launch, "sweep_ledgers", lambda root: 0)
    monkeypatch.setattr(launch, "exclude_sandbox_placeholders", lambda root: [])
    monkeypatch.setattr(launch, "runtime_preflight", recorded.preflight)
    monkeypatch.setattr(launch, "settle_base_freshness", lambda *a, **k: None)
    monkeypatch.setattr(launch, "non_interactive_environment", lambda _env: {})
    monkeypatch.setattr(launch, "accessible_roots", lambda *a: [])
    monkeypatch.setattr(launch, "granted_devices", lambda *a: [])
    monkeypatch.setattr(launch, "start_harness_transcript", lambda *a, **k: Mock())
    monkeypatch.setattr(launch, "claude_sandbox_arguments", recorded.claude_sandbox)
    monkeypatch.setattr(launch, "apply_sandbox_environment", recorded.vouch)
    monkeypatch.setattr(launch, "ClaudeTranscripts", lambda _home: Mock())
    monkeypatch.setattr(launch, "ambient_config_home", lambda *a, **k: tmp_path)
    monkeypatch.setattr(launch, "codex_sandbox_arguments", recorded.codex_sandbox)
    monkeypatch.setattr(launch, "CodexWorktreeHomeStore", lambda **_: Mock())
    monkeypatch.setattr(
        launch,
        "select_codex_home",
        lambda *a: Mock(path=tmp_path / "codex-home", isolated=False),
    )
    monkeypatch.setattr(launch, "codex_login_preflight", lambda *a, **k: None)
    monkeypatch.setattr(launch, "prepare_codex_plugin", lambda *a, **k: None)
    monkeypatch.setattr(launch, "CodexTranscripts", lambda _home: Mock())
    monkeypatch.setattr(launch, "session_argv", recorded.session_argv)
    monkeypatch.setattr(sh, "Command", lambda _name: recorded.cli)
    return recorded


def opened(
    runtime: str, sandbox: LaunchSandbox | None = None, generate_only: bool = False
) -> None:
    """Launch one runtime; ``None`` is what the command line hands on for no flag."""
    match runtime:
        case "claude":
            profiles = Mock()
            profiles.launch_home.return_value = None
            launch_claude(
                composition(), [], profiles, None, None, generate_only, sandbox=sandbox
            )
        case "codex":
            launch_codex(
                composition(),
                [],
                None,
                None,
                None,
                generate_only,
                False,
                sandbox=sandbox,
            )
        case unknown:
            raise AssertionError(f"no launcher for {unknown}")


def opened_under(seen: Mock, runtime: str) -> LaunchSandbox:
    """The one posture every step after the gate was handed.

    Read off three places rather than one, because a fallback is only a
    fallback if they agree: the host roster, chosen by which side of the
    container the session runs on; the runtime's own sandbox words; and the
    argv, which decides whether a container is opened at all.
    """
    call = seen.session_argv.call_args
    argv = inspect.signature(session_argv).bind(*call.args, **call.kwargs)
    posture = argv.arguments["sandbox"]
    words = seen.claude_sandbox if runtime == "claude" else seen.codex_sandbox
    preflight = seen.preflight.call_args
    roster = inspect.signature(runtime_preflight).bind(
        *preflight.args, **preflight.kwargs
    )
    assert roster.arguments["contained"] is posture.contained()
    assert words.call_args.kwargs["sandbox"] is posture
    return posture


RUNTIMES = pytest.mark.parametrize("runtime", ["claude", "codex"])


@RUNTIMES
def test_a_default_launch_with_no_engine_opens_under_the_inner_sandbox(
    runtime: str,
    seen: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No flag and no container client: the host, its runtime's sandbox, one warning.

    Said once, and saying what to do about it both ways -- what to install
    to get the container back, and the flag that states this choice so a
    machine that means it is not warned on every launch.
    """
    host(monkeypatch, None)

    opened(runtime)

    said = capsys.readouterr().out
    assert opened_under(seen, runtime) is LaunchSandbox.INNER
    assert said.count("No working Docker or Podman client was found") == 1
    assert "runs under the inner sandbox on the host" in said
    assert "Install Docker or Podman" in said
    assert "`--sandbox inner`" in said
    seen.cli.assert_called_once()


@RUNTIMES
def test_a_default_launch_with_an_engine_opens_in_the_container(
    runtime: str,
    seen: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default is still the container wherever a client answers, and silent."""
    host(
        monkeypatch, ContainerClient(binary="podman", client="podman", server="podman")
    )

    opened(runtime)

    assert opened_under(seen, runtime) is LaunchSandbox.OUTER
    assert "Docker or Podman" not in capsys.readouterr().out


@RUNTIMES
def test_an_explicit_outer_with_no_engine_is_refused_rather_than_degraded(
    runtime: str,
    seen: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--sandbox outer` asked for the container by name, so nothing opens without one.

    The launcher does not probe on its own account -- an explicit posture is
    not a default to settle -- and the container's argv refuses in its own
    words, before any runtime is started.
    """
    unprobed(monkeypatch)
    monkeypatch.setattr(contained, "detected_client", lambda: None)
    monkeypatch.setattr(launch, "session_argv", session_argv)

    with pytest.raises(
        typer.BadParameter, match="Install one to launch in a container"
    ):
        opened(runtime, sandbox=LaunchSandbox.OUTER)

    assert "runs under the inner sandbox" not in capsys.readouterr().out
    seen.cli.assert_not_called()


@RUNTIMES
@pytest.mark.parametrize("asked", list(LaunchSandbox))
def test_an_explicit_sandbox_is_taken_as_said_without_asking_the_host(
    runtime: str,
    asked: LaunchSandbox,
    seen: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only a command line naming no sandbox has anything to settle."""
    unprobed(monkeypatch)

    opened(runtime, sandbox=asked)

    assert opened_under(seen, runtime) is asked
    assert "Docker or Podman" not in capsys.readouterr().out


@RUNTIMES
def test_generating_only_settles_no_sandbox(
    runtime: str,
    seen: Mock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A generate-only invocation opens nothing, so it is neither probed nor warned."""
    unprobed(monkeypatch)

    opened(runtime, generate_only=True)

    seen.preflight.assert_not_called()
    assert "Docker or Podman" not in capsys.readouterr().out


@RUNTIMES
@pytest.mark.parametrize(
    ("words", "asked"),
    [
        ([], None),
        (["--sandbox", "outer"], LaunchSandbox.OUTER),
        (["--sandbox", "inner"], LaunchSandbox.INNER),
        (["--sandbox", "none"], LaunchSandbox.NONE),
    ],
)
def test_the_command_line_tells_a_missing_flag_from_an_explicit_outer(
    runtime: str,
    words: list[str],
    asked: LaunchSandbox | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag's own default is no posture at all, so the launcher can tell.

    A default spelled `outer` here would reach the launcher looking exactly
    like an operator who asked for the container, and the fallback would
    have nothing left to fall back from.
    """
    launcher = launch_claude if runtime == "claude" else launch_codex
    recorded = Mock()
    monkeypatch.setattr(launch, launcher.__name__, recorded)

    def built(
        root: Path, rules: RuleSelection | None = None
    ) -> NativeHarnessComposition:
        return Mock()

    app = create_harness_app(NativeTargets(builders={runtime: built}), [])

    result = CliRunner().invoke(app, [runtime, *words])

    assert result.exit_code == 0, result.output
    call = recorded.call_args
    bound = inspect.signature(launcher).bind(*call.args, **call.kwargs)
    assert bound.arguments["sandbox"] is asked
