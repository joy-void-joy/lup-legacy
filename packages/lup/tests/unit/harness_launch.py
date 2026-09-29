"""A host stubbed for `harness claude|codex`: what a launch would run, caught instead.

Every measurement a launch takes of the host — the boundary, the runtime's
probes, the container's argv, the home a session opens in — is stubbed the
same way whether the harness launches a declaration or a test asks that
declaration for its ``command()``, and this repository's workflow around a
session runs nowhere. The CLI is caught rather than run. So what a test
compares is the compilation and the order things happen in, not the machine.
"""

from functools import partial
from pathlib import Path
from unittest.mock import Mock

import pytest
import sh

import lup.devtools.harness.launch as launch
import lup.launch.session as launch_session
import lup.providers.claude.launch as claude_launch
import lup.providers.codex.launch as codex_launch
from lup.coordination.identity import LaunchedMember
from lup.harness.generate import MachineOverlay, NativeHarnessComposition
from lup.providers.claude.harness import CLAUDE_OVERLAY
from lup.providers.codex.harness import CODEX_OVERLAY
from lup.providers.harness import claude_machine_overlay, codex_machine_overlay
from lup.harness.messaging import WakeSockets
from lup.harness.models import Harness
from lup.observability.audit import TraceJournal
from lup.providers.codex.home import CodexHomeSelection
from lup.providers.codex.profile import CodexAccountSettings
from lup.sessions.recursion import MAX_RECURSIVE_AGENT_ENV
from lup.types import EnvVars

MEMBER = LaunchedMember(member_id="member-1", cli_name="work")
CONTAINER = ["engine", "run", "-it", "lup-image"]


class Caught:
    """What the stubbed CLI was started with, and everything that happened around it."""

    def __init__(self) -> None:
        self.argv: list[str] = []
        self.env: EnvVars = {}
        self.cwd: Path | None = None
        self.events: list[str] = []


class Transcript:
    """The launch-facing half of a transcript, writing down how it closed."""

    def __init__(self, caught: Caught) -> None:
        self.caught = caught
        self.journal = Mock(spec=TraceJournal, path=None)

    def close(self, *, succeeded: bool, interrupted: bool = False) -> None:
        del interrupted
        self.caught.events.append(f"close:{succeeded}")


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


def composition(root: Path, label: str) -> NativeHarnessComposition:
    """A composition of this repository's harness for ``root``, as the harness reads one."""
    from lup_template.harness.catalog import launched_serve, launched_tool_servers

    built = Mock(spec=NativeHarnessComposition)
    built.recipe = Mock()
    built.recipe.source = harness()
    built.recipe.root = root
    built.recipe.label = label
    built.servers = launched_tool_servers()
    built.serve = launched_serve(root)
    built.clipboard_transport = "commands" if label == "claude" else "x11"
    built.overlay = (
        MachineOverlay(
            directory=CLAUDE_OVERLAY,
            render=partial(claude_machine_overlay, built.recipe.source),
            loaded=True,
        )
        if label == "claude"
        else MachineOverlay(
            directory=CODEX_OVERLAY,
            render=partial(codex_machine_overlay, built.recipe.source),
        )
    )
    return built


def profiles(home: Path | None = None, active: str | None = None) -> Mock:
    """A profile directory resolving every name, and the selection, to ``home``."""
    directory = Mock()
    directory.launch_home.return_value = home
    directory.active_name.return_value = active
    return directory


def catching(caught: Caught) -> object:
    """``sh.Command`` for a test: every program started is written down, never run."""

    def command(program: str) -> object:
        def run(
            *arguments: str, _env: EnvVars, _fg: bool, _cwd: str | None = None
        ) -> None:
            assert _fg, "a launch hands the CLI the terminal"
            caught.argv = [program, *arguments]
            caught.env = dict(_env)
            caught.cwd = Path(_cwd) if _cwd is not None else None
            caught.events.append("cli")

        return run

    return command


def stub_host(monkeypatch: pytest.MonkeyPatch, root: Path) -> Caught:
    """Stub every measurement of the host and every workflow step; catch the CLI."""
    caught = Caught()
    monkeypatch.delenv(MAX_RECURSIVE_AGENT_ENV, raising=False)
    monkeypatch.setattr(launch_session, "settle_boundary", Mock())
    monkeypatch.setattr(launch_session, "say_opening", Mock())
    monkeypatch.setattr(launch_session, "verify_inside", Mock(return_value=[]))
    monkeypatch.setattr(
        launch_session, "contained_argv", lambda *_a, **_k: list(CONTAINER)
    )
    monkeypatch.setattr(
        launch_session, "exclude_sandbox_placeholders", lambda _root: []
    )
    for module in (launch_session, claude_launch, codex_launch):
        monkeypatch.setattr(module, "launched_member", lambda *_a, **_k: MEMBER)
    for module in (claude_launch, codex_launch):
        monkeypatch.setattr(
            module,
            "runtime_preflight",
            lambda *_a, **_k: caught.events.append("ready") or [],
        )
        monkeypatch.setattr(
            module, "apply_sandbox_environment", lambda *_a, **_k: False
        )
        monkeypatch.setattr(
            module, "start_harness_transcript", lambda *_a, **_k: Transcript(caught)
        )
    monkeypatch.setattr(claude_launch, "carry_claude_home", lambda *_a, **_k: None)
    seed = Mock()
    seed.compose.return_value.write.return_value = root / "seed"
    monkeypatch.setattr(claude_launch, "ClaudeHomeSeed", seed)
    home = CodexHomeSelection(path=root / "codex-home", isolated=False)
    monkeypatch.setattr(codex_launch, "select_codex_home", lambda *_a, **_k: home)
    monkeypatch.setattr(codex_launch, "codex_login_preflight", lambda *_a, **_k: None)
    monkeypatch.setattr(
        codex_launch,
        "prepare_codex_plugin",
        lambda *_a, **_k: caught.events.append("installed"),
    )
    monkeypatch.setattr(codex_launch, "carry_codex_home", lambda *_a, **_k: None)
    monkeypatch.setattr(
        CodexAccountSettings, "capture", classmethod(lambda cls, *_a, **_k: None)
    )
    monkeypatch.setattr(
        launch, "refuse_redirected_pointers", lambda: caught.events.append("pointers")
    )
    monkeypatch.setattr(
        launch, "settle_base_freshness", lambda *_a, **_k: caught.events.append("base")
    )
    monkeypatch.setattr(
        launch,
        "generate_with_report",
        lambda _composition, in_passing=False: caught.events.append(
            f"generated:{'passing' if in_passing else 'reported'}"
        ),
    )
    monkeypatch.setattr(
        launch, "generate_targets", lambda *_a, **_k: caught.events.append("siblings")
    )
    monkeypatch.setattr(launch, "accessible_roots", lambda *_a, **_k: [])
    monkeypatch.setattr(launch, "granted_devices", lambda *_a, **_k: [])
    monkeypatch.setattr(sh, "Command", catching(caught))
    return caught


def checkout(tmp_path: Path) -> Path:
    """A checkout inside a ``tree/``, so every launch widens to the same siblings."""
    root = tmp_path / "tree" / "work"
    root.mkdir(parents=True)
    return root
