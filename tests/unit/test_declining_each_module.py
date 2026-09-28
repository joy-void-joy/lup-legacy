"""Every module this repository could decline, declined alone, leaves a harness that works.

The static gate in `dev check` reads what each module's content names and
holds it to what the module stands on. This is the other half, measured rather
than read: for each module a project may decline, the whole tree that project
would get is composed — both runtimes' plugins, every page, the always-loaded
document, the guidance an installer carries — and held to what generation
holds it to. The harness validates, every artifact compiles, every command a
generated file tells its reader to run is one that project's CLI serves, and
every skill it spells is one that project's plugin ships.

One module at a time rather than every combination, because that is the
promise being made: declining a module breaks nothing that did not say it
needed it. What does say so goes with it — declining the git loop declines the
resolver too — and that is what the operator is told when they try.

Composed in memory against the catalog's own entries, so nothing is written
and each case is independent of the tree this checkout generated.
"""

import pytest

import lup.harness.models as models
from lup.devtools.dev.commands import CommandSurface
from lup.devtools.dev.documented import MENTION, WrittenCommand
from lup.providers.claude.composition import ClaudeComposer
from lup.providers.codex.composition import CodexComposer
from lup.harness.dependencies import SPELLED_SKILL
from lup.harness.modules import Composition, anchored
from lup.workspace.paths import project_root
from lup_template.devtools.main import cli
from lup_template.harness.catalog import launched_tool_servers
from lup_template.harness.composition import installer_guidance, project_content
from lup_template.harness.content.catalog import MODULE_SPECS, composition, entries
from lup_template.harness.content.template_claude import DOCUMENT as TEMPLATE_CLAUDE
from lup_template.harness.content.template_codex import DOCUMENT as TEMPLATE_CODEX

DECLINABLE = [spec.id for spec in MODULE_SPECS if spec.id not in anchored(MODULE_SPECS)]
"""Every module a project may decline: all but the ones every project has."""

SWEPT = [".md", ".py"]
"""What the documented-command sweep reads, generated or not."""

PROMPTS = [".md", ".toml"]
"""Where a runtime reads prose: a skill, an agent, a page, the guidance.

The hook runtime's Python is library source copied byte for byte, whose
comments name skills the way any code comment does."""

ROSTER_SKILLS = {skill.name for skill in composition(declined=[]).content().skills}
"""Every skill some module here declares, for telling one from a placeholder.

A skill spelled in prose that no module declares is a pattern being taught —
``/lup:command-name`` in the command-authoring walk — rather than a pointer
that a declined module could have left dangling."""


def declining(module: str) -> list[str]:
    """*module* and every module whose ``requires`` reach it, as a project must decline them."""
    needs = {spec.id: spec.requires for spec in MODULE_SPECS}
    held = [module]
    for current in held:
        held.extend(
            spec.id
            for spec in MODULE_SPECS
            if current in needs[spec.id] and spec.id not in held
        )
    return held


def artifacts(composed: Composition) -> list[models.Artifact]:
    """Every artifact both runtimes' trees would hold for *composed*, unwritten."""
    root = project_root()
    content = project_content(root, composed=composed)
    return [
        *ClaudeComposer()
        .compose(root, content, installer_guidance(TEMPLATE_CLAUDE, composed))
        .recipe.desired.artifacts,
        *CodexComposer()
        .compose(root, content, installer_guidance(TEMPLATE_CODEX, composed))
        .recipe.desired.artifacts,
    ]


def test_every_module_is_offered_declined_or_pinned_for_a_reason() -> None:
    """The anchored set is small and named, so a module cannot become one silently."""
    assert anchored(MODULE_SPECS) == ["project", "core", "observability"]


@pytest.mark.parametrize("module", DECLINABLE)
def test_declining_a_module_leaves_every_other_one_generating(module: str) -> None:
    declined = declining(module)
    composed = composition(declined=declined)
    built = artifacts(composed)

    surface = CommandSurface.of(cli(composed))
    unserved = [
        f"{artifact.path.as_posix()}: uv run lup-devtools {tail.strip()}"
        for artifact in built
        if artifact.path.suffix in SWEPT
        for tail in MENTION.findall(artifact.content)
        if (
            words := WrittenCommand(
                file="", line=0, spelled=tail.strip()
            ).command_words()
        )
        and not surface.admits(words)
    ]
    shipped = {skill.name for skill in composed.content().skills}
    unshipped = [
        f"{artifact.path.as_posix()}: {plugin}:{skill}"
        for artifact in built
        if artifact.path.suffix in PROMPTS
        for plugin, skill in SPELLED_SKILL.findall(artifact.content)
        if plugin == "lup" and skill in ROSTER_SKILLS and skill not in shipped
    ]

    assert (unserved, unshipped) == ([], [])


@pytest.mark.parametrize("module", DECLINABLE)
def test_a_declined_module_leaves_nothing_of_its_own_behind(module: str) -> None:
    """No skill, no page, no command tree, no tool server — the whole subject goes."""
    declined = declining(module)
    composed = composition(declined=declined)
    everything = {entry.spec.id: entry for entry in entries()}
    gone = [everything[name].build() for name in declined]
    harness = project_content(project_root(), composed=composed).harness
    plugin = harness.plugins[0]

    assert {skill.name for skill in plugin.skills}.isdisjoint(
        skill.name for held in gone for skill in held.content.skills
    )
    assert {
        server.name for server in launched_tool_servers(composed.withheld_tool_groups())
    }.isdisjoint(group for held in gone for group in held.spec.tool_groups)
    assert set(composed.subapps()).isdisjoint(
        name for held in gone for name in held.spec.subapps
    )


def test_declining_the_sandbox_asks_no_machine_for_its_python_sandbox() -> None:
    """The container engine stays only while something else still runs containers."""
    without_sandbox = project_content(
        project_root(), composed=composition(declined=["sandbox"])
    ).harness.requirements
    without_either = project_content(
        project_root(), composed=composition(declined=["sandbox", "resolver"])
    ).harness.requirements

    assert "Python sandbox" not in {
        item.capability for item in without_sandbox.requirements
    }
    assert "container runtime" in {
        item.capability for item in without_sandbox.requirements
    }
    assert not {"container runtime", "Python sandbox"} & {
        item.capability for item in without_either.requirements
    }


def test_an_essential_module_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="'core' is essential"):
        composition(declined=["core"])
