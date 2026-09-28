"""``command()`` compiles what the harness launcher runs, for the same inputs.

The launcher behind ``lup-devtools harness claude|codex`` and a declaration's
``command()`` reach the CLI through the same library session, so for a
declaration saying what a command line said, the process each would start is
the same process. Two differences are the declaration's by design and are
compared as such: it names its MCP servers itself (``--mcp-config`` with
``--strict-mcp-config``, where the launcher leaves them to the plugin), and
its one ``--settings`` document carries the whole declared sandbox where the
launcher's carries only the widening beside the project's settings file —
so the launcher's document must be contained in it.

Everything that measures the host is stubbed identically on both paths —
the boundary, the probes, the container's argv — so what is compared is the
compilation, not the machine.
"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import sh

import lup.devtools.harness.launch as launch
import lup.launch.session as launch_session
import lup.providers.claude.launch as claude_launch
import lup.providers.codex.launch as codex_launch
from lup.coordination.identity import LaunchedMember
from lup.harness.messaging import WakeSockets
from lup.harness.models import Harness, Resumption
from lup.launch.declaration import (
    InnerSandbox,
    LaunchSandbox,
    Latest,
    Member,
    NoSandbox,
    OuterContainer,
)
from lup.providers.claude import Claude
from lup.providers.claude.models import ClaudeEffort
from lup.providers.codex import Codex
from lup.providers.codex.home import CodexHomeSelection
from lup.providers.codex.profile import CodexProfileSettings
from lup.sessions.recursion import MAX_RECURSIVE_AGENT_ENV
from lup.types import EnvVars, JsonValue

MEMBER = LaunchedMember(member_id="member-1", cli_name="work")
CONTAINER = ["engine", "run", "-it", "lup-image"]


class Transcript:
    """The launch-facing half of a transcript, closing without a trace."""

    def __init__(self) -> None:
        self.journal = Mock(path=None)

    def close(self, *, succeeded: bool, interrupted: bool = False) -> None:
        del succeeded, interrupted


class Launched:
    """What the stubbed CLI was started with."""

    def __init__(self) -> None:
        self.argv: list[str] = []
        self.env: EnvVars = {}


def harness() -> Harness:
    """This repository's harness, its wake socket declined so nothing binds one."""
    from lup_template.harness.catalog import portable_harness

    declared = portable_harness()
    return declared.model_copy(
        update={
            "image": declared.image.model_copy(
                update={"wake_sockets": WakeSockets(directory="")}
            )
        }
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A checkout inside a ``tree/``, so both paths widen to the same siblings."""
    checkout = tmp_path / "tree" / "work"
    checkout.mkdir(parents=True)
    return checkout


@pytest.fixture
def launched(root: Path, monkeypatch: pytest.MonkeyPatch) -> Launched:
    """Stub every host measurement identically on both paths, and catch the CLI."""
    caught = Launched()
    monkeypatch.delenv(MAX_RECURSIVE_AGENT_ENV, raising=False)
    monkeypatch.setattr(launch_session, "settle_boundary", Mock())
    monkeypatch.setattr(launch_session, "say_opening", Mock())
    monkeypatch.setattr(launch_session, "verify_inside", Mock(return_value=[]))
    monkeypatch.setattr(
        launch_session, "contained_argv", lambda *args, **kwargs: list(CONTAINER)
    )
    for module in (launch, launch_session, claude_launch, codex_launch):
        monkeypatch.setattr(module, "launched_member", lambda *_a, **_k: MEMBER)
    monkeypatch.setattr(launch, "project_root", lambda: root)
    monkeypatch.setattr(launch, "find_tree_dir", lambda: root.parent)
    monkeypatch.setattr(launch, "accessible_roots", lambda *_a, **_k: [])
    monkeypatch.setattr(launch, "granted_devices", lambda *_a, **_k: [])
    monkeypatch.setattr(launch, "settle_claude_theme", lambda *_a, **_k: None)
    monkeypatch.setattr(launch, "carry_claude_home", lambda *_a, **_k: None)
    monkeypatch.setattr(launch, "carry_codex_home", lambda *_a, **_k: None)
    seed = Mock()
    seed.compose.return_value.write.return_value = root / "seed"
    monkeypatch.setattr(launch, "ClaudeHomeSeed", seed)
    monkeypatch.setattr(
        launch, "start_harness_transcript", lambda *_a, **_k: Transcript()
    )
    for module in (claude_launch, codex_launch):
        monkeypatch.setattr(module, "runtime_preflight", lambda *_a, **_k: [])
    home = CodexHomeSelection(path=root / "codex-home", isolated=False)
    for module in (launch, codex_launch):
        monkeypatch.setattr(module, "select_codex_home", lambda *_a, **_k: home)
        monkeypatch.setattr(module, "codex_login_preflight", lambda *_a, **_k: None)
        monkeypatch.setattr(module, "prepare_codex_plugin", lambda *_a, **_k: None)
    monkeypatch.setattr(launch, "CodexWorktreeHomeStore", lambda **_k: Mock())
    monkeypatch.setattr(
        CodexProfileSettings, "capture", classmethod(lambda cls, *_a, **_k: None)
    )

    def command(program: str) -> object:
        def run(
            *arguments: str, _env: EnvVars, _fg: bool, _cwd: str | None = None
        ) -> None:
            assert _fg, "the launcher hands the CLI the terminal"
            del _cwd
            caught.argv = [program, *arguments]
            caught.env = dict(_env)

        return run

    monkeypatch.setattr(sh, "Command", command)
    for module in (launch, claude_launch, codex_launch):
        monkeypatch.setattr(
            module, "apply_sandbox_environment", lambda *_a, **_k: False
        )
    return caught


def composition(label: str, clipboard: str) -> Mock:
    """A composition carrying this repository's harness, as the launcher reads it."""
    built = Mock()
    built.recipe.source = harness()
    built.recipe.label = label
    built.clipboard_transport = clipboard
    return built


def opened(monkeypatch: pytest.MonkeyPatch, sandbox: LaunchSandbox) -> None:
    """Clear the launcher's gate as the declaration's check clears it."""
    monkeypatch.setattr(
        launch,
        "ready_to_open",
        lambda *_a, **_k: launch_session.LaunchOpening(sandbox=sandbox),
    )


def settings(argv: list[str]) -> dict[str, JsonValue]:
    """The one ``--settings`` document an argv carries."""
    return json.loads(argv[argv.index("--settings") + 1])


def without_declared_servers(argv: list[str]) -> list[str]:
    """The argv with the declaration's own MCP flags and settings value taken out."""
    servers = argv.index("--mcp-config")
    kept = [*argv[:servers], *argv[servers + 2 :]]
    kept.remove("--strict-mcp-config")
    document = kept.index("--settings")
    return [*kept[: document + 1], *kept[document + 2 :]]


def without_settings(argv: list[str]) -> list[str]:
    document = argv.index("--settings")
    return [*argv[: document + 1], *argv[document + 2 :]]


def contained_in(small: JsonValue, large: JsonValue) -> bool:
    """Whether every key the launcher's document sets has the same value in the declaration's."""
    if isinstance(small, dict) and isinstance(large, dict):
        return all(
            key in large and contained_in(value, large[key])
            for key, value in small.items()
        )
    return small == large


POSTURES = [
    (LaunchSandbox.INNER, InnerSandbox(escapable=True)),
    (LaunchSandbox.OUTER, OuterContainer()),
    (LaunchSandbox.NONE, NoSandbox()),
]

CODEX_POSTURES = [
    (LaunchSandbox.INNER, InnerSandbox()),
    (LaunchSandbox.OUTER, OuterContainer()),
    (LaunchSandbox.NONE, NoSandbox()),
]

REOPENINGS: list[tuple[Resumption, Latest | None]] = [
    (Resumption(), None),
    (Resumption(latest=True), Latest()),
]


@pytest.mark.parametrize(("posture", "sandbox"), POSTURES)
@pytest.mark.parametrize(("resumption", "reopening"), REOPENINGS)
@pytest.mark.parametrize("effort", [None, "ultra"])
def test_claude_command_is_what_the_launcher_runs(
    launched: Launched,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    posture: LaunchSandbox,
    sandbox: InnerSandbox | OuterContainer | NoSandbox,
    resumption: Resumption,
    reopening: Latest | None,
    effort: ClaudeEffort | None,
) -> None:
    opened(monkeypatch, posture)
    profiles = Mock()
    profiles.launch_home.return_value = None
    launch.launch_claude(
        composition("claude", "commands"),
        ["--verbose"],
        profiles,
        None,
        "opus",
        False,
        resume=resumption,
        sandbox=posture,
        effort=effort,
    )
    agent = Claude(
        model="opus",
        effort=effort,
        cwd=root,
        plugin=harness(),
        sandbox=sandbox,
        resume=reopening,
        identity=Member(wake_sockets=None),
    )
    command = agent.command("--verbose")

    assert without_declared_servers(command.argv) == without_settings(launched.argv)
    assert contained_in(settings(launched.argv), settings(command.argv))
    assert command.env == launched.env
    assert command.cwd == root


@pytest.mark.parametrize(("posture", "sandbox"), CODEX_POSTURES)
@pytest.mark.parametrize(("resumption", "reopening"), REOPENINGS)
def test_codex_command_is_what_the_launcher_runs(
    launched: Launched,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    posture: LaunchSandbox,
    sandbox: InnerSandbox | OuterContainer | NoSandbox,
    resumption: Resumption,
    reopening: Latest | None,
) -> None:
    opened(monkeypatch, posture)
    launch.launch_codex(
        composition("codex", "x11"),
        ["--search"],
        None,
        None,
        "gpt-5.5",
        False,
        False,
        resume=resumption,
        sandbox=posture,
    )
    agent = Codex(
        model="gpt-5.5",
        cwd=root,
        plugin=harness(),
        sandbox=sandbox,
        resume=reopening,
        identity=Member(wake_sockets=None),
    )
    command = agent.command("--search")

    assert command.argv == launched.argv
    assert command.env == launched.env
    assert command.cwd == root


class Step:
    """A lifecycle step that writes down when it ran."""

    def __init__(self, seen: list[str]) -> None:
        self.seen = seen

    def before(self) -> None:
        self.seen.append("before")

    def after(self, succeeded: bool) -> None:
        self.seen.append(f"after:{succeeded}")


class Closing(Transcript):
    """A transcript that says how the session it recorded ended."""

    def __init__(self, closed: list[bool]) -> None:
        super().__init__()
        self.closed = closed

    def close(self, *, succeeded: bool, interrupted: bool = False) -> None:
        del interrupted
        self.closed.append(succeeded)


@pytest.mark.parametrize("status", [0, 3])
def test_a_claude_launch_runs_the_cli_between_its_steps_and_cleans_up(
    launched: Launched,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    seen: list[str] = []
    closed: list[bool] = []
    released: list[Path] = []
    monkeypatch.setattr(
        claude_launch, "start_harness_transcript", lambda *_a, **_k: Closing(closed)
    )
    monkeypatch.setattr(
        claude_launch, "release_ledger", lambda at, _nonce: released.append(at)
    )
    monkeypatch.setattr(claude_launch, "settle_claude_theme", lambda *_a, **_k: None)
    if status:

        class Failed(sh.ErrorReturnCode):
            exit_code = status

        failing = Failed

        def command(program: str) -> object:
            def run(
                *arguments: str, _env: EnvVars, _fg: bool, _cwd: str | None = None
            ) -> None:
                launched.argv = [program, *arguments]
                del _env, _fg, _cwd
                raise failing(program, b"", b"")

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
    assert launched.argv[0] == "claude"
    assert launched.argv[-1] == "--verbose"
    assert seen == ["before", f"after:{status == 0}"]
    assert closed == [status == 0]
    assert released == [root]


def test_a_codex_launch_runs_the_cli_between_its_steps_and_cleans_up(
    launched: Launched, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    closed: list[bool] = []
    monkeypatch.setattr(
        codex_launch, "start_harness_transcript", lambda *_a, **_k: Closing(closed)
    )
    agent = Codex(
        model="gpt-5.5",
        cwd=root,
        sandbox=InnerSandbox(),
        identity=Member(wake_sockets=None),
    )

    assert agent.launch("--search", steps=[Step(seen)]) == 0
    assert launched.argv[0] == "codex"
    assert launched.argv[-1] == "--search"
    assert seen == ["before", "after:True"]
    assert closed == [True]
