"""The wall a session opens behind, rendered by each runtime in its own words.

Both declarations take the launcher's three walls. Claude holds a permission
mode and a sandbox, so the two render into fields that decide nothing about
each other; Codex has one field for both, so a wall and a declared mode are
reconciled to the narrower. The rosters are read out of the types rather
than restated, so a wall or a mode added to either fails here until it is
answered.
"""

from pathlib import Path
from typing import get_args

import pytest

from lup.launch.declaration import (
    InnerSandbox,
    NoSandbox,
    OuterContainer,
    SessionSandbox,
)
from lup.providers.claude import Claude, ClaudePermissionMode
from lup.providers.claude.launch import claude_settings
from lup.providers.codex import CODEX_PROGRAM, Codex, CodexSandbox
from lup.providers.codex.launch import CODEX_SANDBOX_WIDTH, codex_sandbox_mode

PERMISSION_MODES = get_args(ClaudePermissionMode.__value__)
CODEX_MODES = get_args(CodexSandbox.__value__)
WALLS: list[SessionSandbox] = [OuterContainer(), InnerSandbox(), NoSandbox()]


def codex_mode(config: Codex) -> CodexSandbox | None:
    """The sandbox mode a Codex thread is started with, wall and mode reconciled."""
    return codex_sandbox_mode(config.sandbox, config.sandbox_mode)


def test_claude_opens_an_inner_session_in_its_own_sandbox(tmp_path: Path) -> None:
    config = Claude(cwd=tmp_path, sandbox=InnerSandbox())

    assert claude_settings(config)["sandbox"] == {
        "enabled": True,
        "allowUnsandboxedCommands": False,
        "filesystem": {"allowWrite": []},
    }
    assert config.sandbox.enforcement().active
    assert config.cli_path is None


def test_claude_stands_its_sandbox_down_inside_the_container(tmp_path: Path) -> None:
    """The container is the wall, and the session is told so in both fields.

    Told rather than left unsaid: a spawned session reads none of the
    settings files a launched one does, so an unstated sandbox is decided by
    whatever the runtime falls back to — and the policy judging the session
    would be reading a posture nobody set.
    """
    program = tmp_path / "enter.sh"
    config = Claude(cwd=tmp_path, sandbox=OuterContainer(), cli_path=program)

    assert config.cli_path == program
    assert claude_settings(config)["sandbox"] == {"enabled": False}
    assert not config.sandbox.enforcement().active


@pytest.mark.parametrize("mode", PERMISSION_MODES)
def test_claude_decides_permission_and_containment_apart(
    mode: ClaudePermissionMode,
) -> None:
    """Every permission mode keeps the sandbox the declaration asked for.

    The property Codex cannot state this simply, and the reason the two
    adapters are tested differently rather than through one parametrization.
    """
    walled = Claude(sandbox=InnerSandbox())
    config = Claude(permission_mode=mode, sandbox=InnerSandbox())

    assert config.permission_mode == mode
    assert claude_settings(config)["sandbox"] == claude_settings(walled)["sandbox"]


@pytest.mark.parametrize("wall", WALLS)
def test_every_runtime_spells_every_wall(wall: SessionSandbox) -> None:
    """A wall added to the axis fails here until both runtimes answer it."""
    assert "sandbox" in claude_settings(Claude(sandbox=wall))
    assert codex_sandbox_mode(wall, None) != "read-only"


def test_codex_stands_its_sandbox_down_inside_the_container(tmp_path: Path) -> None:
    """Codex confines with kernel facilities a container will not nest.

    So the container takes the field outright rather than being narrowed
    into a second boundary that cannot start where it was asked for.
    """
    program = tmp_path / "enter.sh"
    config = Codex(cwd=tmp_path, sandbox=OuterContainer(), executable=program)

    assert codex_mode(config) == "danger-full-access"
    assert config.executable == program
    assert Codex(cwd=tmp_path).executable == CODEX_PROGRAM


@pytest.mark.parametrize("mode", CODEX_MODES)
def test_neither_codex_field_widens_what_the_other_narrowed(
    mode: CodexSandbox, tmp_path: Path
) -> None:
    """The inner wall and every declared mode settle on the narrower of the two.

    Stated over every mode rather than over the interesting one, because the
    property is that no mode escapes the wall — a full-access declaration
    reaches `workspace-write` and no further, and a read-only one is not
    widened to it.
    """
    config = Codex(cwd=tmp_path, sandbox_mode=mode, sandbox=InnerSandbox())

    reconciled = codex_mode(config)
    assert reconciled is not None
    ordering = CODEX_SANDBOX_WIDTH.index
    assert ordering(reconciled) == min(ordering("workspace-write"), ordering(mode))
