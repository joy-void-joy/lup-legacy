"""The machine's own skills: rendered beside each tree from what the machine keeps.

`/lup:profile`'s hint names the profiles this machine keeps, which no committed
file may, so the skill is in neither tree. Generation and every launch render
it into a gitignored overlay beside each tree, rewritten whole, which the
drift check never reads and a Claude launch loads after the committed plugin.
"""

import json
from pathlib import Path

import pytest
import yaml

import lup.harness.models as models
from lup.devtools.harness.drift import settled, write_machine_overlay
from lup.harness.generate import generate, inspect_generation
from lup.providers.claude.harness import CLAUDE_OVERLAY
from lup.providers.claude.launch import companion_plugin_directories
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.codex.harness import CODEX_OVERLAY
from lup.providers.harness import (
    claude_machine_overlay,
    codex_machine_overlay,
    compile_claude,
    compile_codex,
)
from lup.providers.profile_tree import profile_directory
from lup.providers.user_config import UserConfigFile
from lup_template.harness.catalog import portable_harness
from lup.harness.generate import NativeHarnessComposition, ProjectContent
from lup.providers.claude.composition import ClaudeComposer
from lup.providers.codex.composition import CodexComposer


def claude_target(root: Path) -> NativeHarnessComposition:
    """This repository's harness composed for Claude Code into ``root``."""
    return ClaudeComposer().compose(root, ProjectContent(harness=portable_harness()))


def codex_target(root: Path) -> NativeHarnessComposition:
    """This repository's harness composed for Codex into ``root``."""
    return CodexComposer().compose(root, ProjectContent(harness=portable_harness()))


def frontmatter(content: str) -> dict[str, str]:
    """The YAML a Markdown artifact opens with, read as its runtime reads it."""
    # lup: ignore[string-split]
    document = content.partition("---\n")[2].partition("\n---")[0]
    return yaml.safe_load(document)


def test_a_hint_names_the_profiles_the_machine_keeps() -> None:
    hint = models.ProfileHint(alone=["list"], naming=["use", "switch"])

    assert hint.spelled(["personal", "work"]) == (
        "[list | use <personal|work> | switch <personal|work>]"
    )
    assert hint.spelled([]) == "[list | use <name> | switch <name>]"


def test_a_skill_takes_its_hint_from_the_machine_or_its_declaration_not_both() -> None:
    from lup.harness.content.skills.profile import SKILL

    with pytest.raises(ValueError, match="argument_hint and a machine_hint"):
        models.Skill.model_validate({**SKILL.model_dump(), "argument_hint": "[list]"})


def test_neither_committed_tree_carries_the_machines_own_skill() -> None:
    claude = compile_claude(portable_harness())
    codex = compile_codex(portable_harness())

    assert not any(artifact.path.name == "profile.md" for artifact in claude.artifacts)
    assert not any(
        artifact.path.parent.name == "profile" for artifact in codex.artifacts
    )


def test_claude_s_overlay_is_a_plugin_of_the_committed_name_holding_the_command() -> (
    None
):
    """Named as the committed plugin is, so the command stays ``/lup:profile``."""
    tree = claude_machine_overlay(portable_harness(), ["personal", "work"])
    files = {artifact.path: artifact.content for artifact in tree.artifacts}
    manifest = json.loads(files[CLAUDE_OVERLAY / ".claude-plugin" / "plugin.json"])
    command = frontmatter(files[CLAUDE_OVERLAY / "commands" / "profile.md"])

    assert manifest["name"] == portable_harness().plugins[0].name
    assert command["argument-hint"] == (
        "[list | use <personal|work> | switch <personal|work>]"
    )
    assert not any("hooks" in path.parts for path in files)


def test_codex_s_overlay_is_a_project_skill_under_the_plugin_s_namespace() -> None:
    tree = codex_machine_overlay(portable_harness(), ["work"])
    [skill] = tree.artifacts
    declared = frontmatter(skill.content)

    assert skill.path == CODEX_OVERLAY / "lup-profile" / "SKILL.md"
    assert declared["name"] == "lup:profile"
    assert declared["description"].endswith("[list | use <work> | switch <work>]")


@pytest.fixture
def person(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> UserConfigFile:
    """A person's lup config home, this test's own."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return UserConfigFile()


def project(tmp_path: Path) -> Path:
    """A project a generation can compile into, named as a project is."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "overlay-probe"\n', encoding="utf-8"
    )
    return root


def test_generation_renders_the_overlay_from_the_registry_and_rewrites_it_whole(
    tmp_path: Path, person: UserConfigFile
) -> None:
    root = project(tmp_path)
    accounts = profile_directory(CLAUDE_LOGIN, person, checkout=root)
    accounts.add("work", scope="global")
    accounts.add("personal")
    composition = claude_target(root)
    command = root / CLAUDE_OVERLAY / "commands" / "profile.md"

    write_machine_overlay(composition)
    first = frontmatter(command.read_text(encoding="utf-8"))["argument-hint"]
    (root / CLAUDE_OVERLAY / "commands" / "stale.md").write_text("x", encoding="utf-8")
    (root / ".lup" / "profiles" / "personal").rename(root / "gone")
    write_machine_overlay(composition)

    assert first == "[list | use <personal|work> | switch <personal|work>]"
    assert frontmatter(command.read_text(encoding="utf-8"))["argument-hint"] == (
        "[list | use <work> | switch <work>]"
    )
    assert not (root / CLAUDE_OVERLAY / "commands" / "stale.md").exists()


@pytest.mark.parametrize("target", [claude_target, codex_target])
def test_the_drift_check_never_reads_the_overlay(
    tmp_path: Path, person: UserConfigFile, target: object
) -> None:
    """A tree with its overlay beside it is as settled as one without."""
    root = project(tmp_path)
    build = claude_target if target is claude_target else codex_target
    composition = build(root)
    generate(composition.recipe)
    write_machine_overlay(composition)

    assert composition.overlay is not None
    assert any((root / composition.overlay.directory).rglob("*.md"))
    assert settled(inspect_generation(build(root).recipe))


def test_a_claude_launch_loads_the_overlay_after_the_committed_plugin(
    tmp_path: Path,
) -> None:
    """Named explicitly, since it is rendered on the way in; never found twice."""
    from lup.devtools.harness.launch import machine_overlay

    root = project(tmp_path)
    (root / CLAUDE_OVERLAY / ".claude-plugin").mkdir(parents=True)
    (root / CLAUDE_OVERLAY / ".claude-plugin" / "plugin.json").write_text(
        "{}", encoding="utf-8"
    )

    assert machine_overlay(claude_target(root)) == [root / CLAUDE_OVERLAY]
    assert machine_overlay(codex_target(root)) == []
    assert companion_plugin_directories(root, "lup") == []
