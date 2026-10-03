"""``command()`` is what `harness claude|codex` runs, for the declaration its flags make.

The harness builds a :class:`~lup.providers.claude.Claude` or
:class:`~lup.providers.codex.Codex` from this repository's composition and
its command line, and launches it; the same declaration's ``command()``
prints the process that launch starts. So for every posture, reopening and
effort, the argv, environment and directory the harness hands the CLI are
exactly the ones ``command()`` answers — one orchestration, in the library.

Everything that measures the host is stubbed identically on both paths —
the boundary, the probes, the container's argv — so what is compared is the
compilation, not the machine.
"""

from pathlib import Path

import pytest
import sh

import lup.devtools.harness.launch as launch
import lup.providers.claude.launch as claude_launch
from lup.harness.models import Resumption
from lup.launch.declaration import InnerSandbox, LaunchSandbox, Member
from lup.providers.claude import Claude
from lup.providers.codex import Codex
from lup.types import EnvVars
from tests.unit.harness_launch import (
    Caught,
    checkout,
    composition,
    profiles,
    stub_host,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return checkout(tmp_path)


@pytest.fixture
def caught(root: Path, monkeypatch: pytest.MonkeyPatch) -> Caught:
    monkeypatch.setattr(claude_launch, "settle_claude_theme", lambda *_a, **_k: None)
    return stub_host(monkeypatch, root)


REOPENINGS = [
    Resumption(),
    Resumption(latest=True),
    Resumption(pick=True),
    Resumption(session="abc"),
]


@pytest.mark.parametrize("posture", list(LaunchSandbox))
@pytest.mark.parametrize("resumption", REOPENINGS)
@pytest.mark.parametrize("effort", [None, "ultra"])
def test_claude_command_is_what_the_harness_runs(
    caught: Caught,
    root: Path,
    posture: LaunchSandbox,
    resumption: Resumption,
    effort: str | None,
) -> None:
    request = launch.LaunchArguments(
        words=["--verbose"],
        model="opus",
        effort=effort,
        resume=resumption,
        sandbox=posture,
    )
    launch.launch_claude(composition(root, "claude"), request, profiles(), False)
    agent = launch.claude_declaration(composition(root, "claude"), request, profiles())
    command = agent.command(*request.words)

    assert command.argv == caught.argv
    assert command.env == caught.env
    assert command.cwd == caught.cwd == root


@pytest.mark.parametrize("posture", list(LaunchSandbox))
@pytest.mark.parametrize("resumption", REOPENINGS)
def test_codex_command_is_what_the_harness_runs(
    caught: Caught, root: Path, posture: LaunchSandbox, resumption: Resumption
) -> None:
    request = launch.LaunchArguments(
        words=["--search"], model="gpt-5.5", resume=resumption, sandbox=posture
    )
    launch.launch_codex(composition(root, "codex"), request, None, False, False)
    agent = launch.codex_declaration(composition(root, "codex"), request, None)
    command = agent.command(*request.words)

    assert command.argv == caught.argv
    assert command.env == caught.env
    assert command.cwd == caught.cwd == root


def test_the_harness_declares_the_servers_a_session_carries(
    caught: Caught, root: Path
) -> None:
    """Strict MCP config drops a plugin's servers, so the launch names every one."""
    launch.launch_claude(
        composition(root, "claude"),
        launch.LaunchArguments(sandbox=LaunchSandbox.INNER),
        profiles(),
        False,
    )
    servers = caught.argv[caught.argv.index("--mcp-config") + 1]

    assert "--strict-mcp-config" in caught.argv
    for name in ("coordination", "ledger", "notes"):
        assert f'"{name}"' in servers


class Step:
    """A lifecycle step that writes down when it ran."""

    def __init__(self, seen: list[str]) -> None:
        self.seen = seen

    def before(self) -> None:
        self.seen.append("before")

    def after(self, succeeded: bool) -> None:
        self.seen.append(f"after:{succeeded}")


@pytest.mark.parametrize("status", [0, 3])
def test_a_claude_launch_runs_the_cli_between_its_steps_and_cleans_up(
    caught: Caught,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    seen: list[str] = []
    released: list[Path] = []
    monkeypatch.setattr(
        claude_launch, "release_ledger", lambda at, _nonce: released.append(at)
    )
    if status:

        class Failed(sh.ErrorReturnCode):
            exit_code = status

        def command(program: str) -> object:
            def run(
                *arguments: str, _env: EnvVars, _fg: bool, _cwd: str | None = None
            ) -> None:
                caught.argv = [program, *arguments]
                del _env, _fg, _cwd
                raise Failed(program, b"", b"")

            return run

        monkeypatch.setattr(sh, "Command", command)
    agent = Claude(
        model="opus",
        cwd=root,
        plugin=root / "plugin",
        sandbox=InnerSandbox(escapable=True),
        identity=Member(wake_sockets=None),
    )

    assert agent.launch("--verbose", steps=[Step(seen)]) == status
    assert caught.argv[0] == "claude"
    assert caught.argv[-1] == "--verbose"
    assert seen == ["before", f"after:{status == 0}"]
    assert [event for event in caught.events if event.startswith("close")] == [
        f"close:{status == 0}"
    ]
    assert released == [root]


def test_a_codex_launch_runs_the_cli_between_its_steps_and_cleans_up(
    caught: Caught, root: Path
) -> None:
    seen: list[str] = []
    agent = Codex(
        model="gpt-5.5",
        cwd=root,
        sandbox=InnerSandbox(),
        identity=Member(wake_sockets=None),
    )

    assert agent.launch("--search", steps=[Step(seen)]) == 0
    assert caught.argv[0] == "codex"
    assert caught.argv[-1] == "--search"
    assert seen == ["before", "after:True"]
    assert "close:True" in caught.events
