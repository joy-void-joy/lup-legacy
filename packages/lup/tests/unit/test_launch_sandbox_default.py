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
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

import lup.devtools.harness.launch as launch
import lup.launch.container as contained
import lup.launch.declaration as declaration
import lup.launch.session as launch_session
import lup.providers.claude.launch as claude_launch
import lup.providers.codex.launch as codex_launch
from lup.devtools.harness.app import create_harness_app
from lup.devtools.harness.composition import NativeTargets
from lup.devtools.harness.launch import launch_claude, launch_codex
from lup.devtools.utils import Refusal
from lup.harness.codescan.common import RuleSelection
from lup.harness.generate import NativeHarnessComposition
from lup.harness.image import ContainerClient
from lup.launch.declaration import LaunchSandbox, LaunchStep
from lup.providers.claude import Claude
from lup.providers.codex import Codex
from tests.unit.harness_launch import Caught, checkout, composition, profiles, stub_host


def host(monkeypatch: pytest.MonkeyPatch, client: ContainerClient | None) -> None:
    """Answer the launcher's own probe as a host with this client, or with none."""
    monkeypatch.setattr(declaration, "detected_client", lambda: client)


def unprobed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the launcher asks the host for a container client."""

    def probed() -> ContainerClient | None:
        raise AssertionError("the launcher asked the host for a container client")

    monkeypatch.setattr(declaration, "detected_client", probed)


class Opened:
    """The sandbox each declaration a launch opened carried, and the host it ran on."""

    def __init__(self, root: Path, caught: Caught) -> None:
        self.root = root
        self.caught = caught
        self.sandboxes: list[LaunchSandbox] = []


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Opened:
    """Stub the host, and record the posture every declaration launched was given."""
    root = checkout(tmp_path)
    monkeypatch.setattr(claude_launch, "settle_claude_theme", lambda *_a, **_k: None)
    opened = Opened(root, stub_host(monkeypatch, root))
    claude_session = claude_launch.launch_claude_session
    codex_session = codex_launch.launch_codex_session

    def claude(agent: Claude, words: list[str], steps: Sequence[LaunchStep]) -> int:
        opened.sandboxes.append(agent.sandbox.posture())
        return claude_session(agent, words, steps)

    def codex(
        agent: Codex, words: list[str], steps: Sequence[LaunchStep], force: bool
    ) -> int:
        opened.sandboxes.append(agent.sandbox.posture())
        return codex_session(agent, words, steps, force)

    monkeypatch.setattr(claude_launch, "launch_claude_session", claude)
    monkeypatch.setattr(codex_launch, "launch_codex_session", codex)
    return opened


def opened(
    seen: Opened,
    runtime: str,
    sandbox: LaunchSandbox | None = None,
    generate_only: bool = False,
) -> None:
    """Launch one runtime; ``None`` is what the command line hands on for no flag."""
    request = launch.LaunchRequest(sandbox=sandbox)
    match runtime:
        case "claude":
            launch_claude(
                composition(seen.root, runtime), request, profiles(), generate_only
            )
        case "codex":
            launch_codex(
                composition(seen.root, runtime), request, None, generate_only, False
            )
        case unknown:
            raise AssertionError(f"no launcher for {unknown}")


def opened_under(seen: Opened) -> LaunchSandbox:
    """The one posture the launch declared, and whether the CLI opened in a container.

    Read off two places rather than one, because a fallback is only a
    fallback if they agree: the declaration the harness launched, and the
    argv, which decides whether a container is opened at all.
    """
    [posture] = seen.sandboxes
    assert (seen.caught.argv[0] == "engine") is posture.contained()
    return posture


RUNTIMES = pytest.mark.parametrize("runtime", ["claude", "codex"])


@RUNTIMES
def test_a_default_launch_with_no_engine_opens_under_the_inner_sandbox(
    runtime: str,
    seen: Opened,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No flag and no container client: the host, its runtime's sandbox, one warning.

    Said once, and saying what to do about it both ways -- what to install
    to get the container back, and the flag that states this choice so a
    machine that means it is not warned on every launch.
    """
    host(monkeypatch, None)

    opened(seen, runtime)

    said = capsys.readouterr().out
    assert opened_under(seen) is LaunchSandbox.INNER
    assert said.count("No working Docker or Podman client was found") == 1
    assert "runs under the inner sandbox on the host" in said
    assert "Install Docker or Podman" in said
    assert "`--sandbox inner`" in said
    assert seen.caught.events.count("cli") == 1


@RUNTIMES
def test_a_default_launch_with_an_engine_opens_in_the_container(
    runtime: str,
    seen: Opened,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default is still the container wherever a client answers, and silent."""
    host(
        monkeypatch, ContainerClient(binary="podman", client="podman", server="podman")
    )

    opened(seen, runtime)

    assert opened_under(seen) is LaunchSandbox.OUTER
    assert "Docker or Podman" not in capsys.readouterr().out


@RUNTIMES
def test_an_explicit_outer_with_no_engine_is_refused_rather_than_degraded(
    runtime: str,
    seen: Opened,
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
    monkeypatch.setattr(launch_session, "contained_argv", contained.contained_argv)

    with pytest.raises(Refusal) as refused:
        opened(seen, runtime, sandbox=LaunchSandbox.OUTER)

    assert "Install one to launch in a container" in refused.value.said["why"]
    assert "runs under the inner sandbox" not in capsys.readouterr().out
    assert "cli" not in seen.caught.events


@RUNTIMES
@pytest.mark.parametrize("asked", list(LaunchSandbox))
def test_an_explicit_sandbox_is_taken_as_said_without_asking_the_host(
    runtime: str,
    asked: LaunchSandbox,
    seen: Opened,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only a command line naming no sandbox has anything to settle."""
    unprobed(monkeypatch)

    opened(seen, runtime, sandbox=asked)

    assert opened_under(seen) is asked
    assert "Docker or Podman" not in capsys.readouterr().out


@RUNTIMES
def test_generating_only_settles_no_sandbox(
    runtime: str,
    seen: Opened,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A generate-only invocation opens nothing, so it is neither probed nor warned."""
    unprobed(monkeypatch)

    opened(seen, runtime, generate_only=True)

    assert "ready" not in seen.caught.events
    assert seen.sandboxes == []
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
    assert bound.arguments["request"].sandbox is asked
