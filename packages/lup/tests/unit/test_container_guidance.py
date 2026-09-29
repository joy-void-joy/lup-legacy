"""Guidance a container holds over the committed file, for a kind of session of its own.

The committed guidance is never rewritten: the document is rendered as
generation renders the project's, written outside the checkout, and mounted
read-only over the committed file's path inside the container.
"""

from pathlib import Path
from typing import Any

import pytest

import lup.launch.session as launch_session
import lup.providers.claude.launch as claude_launch
from lup.harness.image import Image
from lup.harness.models import PromptDocument, TextPart
from lup.launch.declaration import Member, OuterContainer
from lup.launch.guidance import held_guidance
from lup.launch.refusal import LaunchRefused
from lup.providers.claude import Claude
from tests.unit.harness_launch import CONTAINER, stub_host


def document(text: str) -> PromptDocument:
    return PromptDocument(source=__name__, parts=[TextPart(text=text)])


def test_guidance_is_written_outside_the_checkout_and_held_over_the_committed_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "work"
    committed = root / ".claude" / "CLAUDE.md"
    committed.parent.mkdir(parents=True)
    committed.write_text("the project's guidance\n", encoding="utf-8")

    held = held_guidance(
        root, Path(".claude/CLAUDE.md"), "this mode's guidance\n", tmp_path / "cache"
    )

    ((written, inside),) = held.items()
    assert inside == str(committed)
    assert not written.is_relative_to(root)
    assert written.read_text(encoding="utf-8") == "this mode's guidance\n"
    assert committed.read_text(encoding="utf-8") == "the project's guidance\n"


def test_guidance_with_nothing_committed_to_hold_it_over_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(LaunchRefused, match="AGENTS.md"):
        held_guidance(tmp_path, Path("AGENTS.md"), "guidance\n", tmp_path / "cache")


def test_the_container_mounts_what_it_holds_over_the_checkout_read_only() -> None:
    argv = Image().session_arguments(
        tag="lup-agent:x",
        checkout=Path("/work"),
        uid=1000,
        gid=1000,
        writable={Path("/work"): "/work"},
        read_only={},
        state_volume="lup-claude-x",
        config_home_env="CLAUDE_CONFIG_DIR",
        overlays={Path("/cache/g/CLAUDE.md"): "/work/.claude/CLAUDE.md"},
    )

    assert "/cache/g/CLAUDE.md:/work/.claude/CLAUDE.md:ro" in argv
    assert argv.index("/work:/work:rw") < argv.index(
        "/cache/g/CLAUDE.md:/work/.claude/CLAUDE.md:ro"
    )


def test_a_claude_launch_holds_its_rendered_guidance_over_the_committed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "work"
    committed = root / ".claude" / "CLAUDE.md"
    committed.parent.mkdir(parents=True)
    committed.write_text("committed\n", encoding="utf-8")
    stub_host(monkeypatch, root)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(claude_launch, "settle_claude_theme", lambda *_a, **_k: None)
    handed: dict[str, Any] = {}

    def contained(*_arguments: object, **keywords: Any) -> list[str]:
        handed.update(keywords)
        return list(CONTAINER)

    monkeypatch.setattr(launch_session, "contained_argv", contained)
    agent = Claude(
        cwd=root,
        identity=Member(wake_sockets=None),
        sandbox=OuterContainer(
            guidance=document("Explore freely; a later session tidies up.\n"),
            hold_generated=True,
        ),
    )

    agent.command()

    ((written, inside),) = handed["overlays"].items()
    assert inside == str(committed)
    assert "Explore freely" in written.read_text(encoding="utf-8")
    assert written.is_relative_to(tmp_path / "home" / ".cache" / "lup" / "guidance")
    assert committed not in handed["trees"]
