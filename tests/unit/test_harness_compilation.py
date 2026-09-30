"""Canonical declaration, native rendering, and reconciliation tests."""

import ast
import errno
import json
import os
import shlex
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, get_args

import pytest
import sh
from claude_agent_sdk.types import SandboxNetworkConfig, SandboxSettings
from pydantic import BaseModel, Field, ValidationError

from lup.tools.lsp.tools import CODEINTEL_TOOL_DECLARATIONS
from lup.policy.identity import AGENT_IDENTITY_ENV
from lup.policy.relay import QuestionRelay
from lup.types import JsonObject
from lup.providers.claude.harness import CLAUDE_DISPATCHER, ClaudeSpellings
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.codex.harness import (
    CODEX_DISPATCHER,
    CodexSpellings,
    codex_project_config,
)
from lup.providers.codex.harness_runtime import (
    PluginCacheConfig,
    digest_directory,
    plugin_cache_evidence,
)
from lup.providers.harness import (
    claude_prompt_renderer,
    codex_prompt_renderer,
    compile_claude,
    compile_codex,
    guidance_artifacts,
)
from lup.devtools.dev.check import (
    budget_reports,
    guidance_budget_report,
    scaffold_budget_report,
)
from lup.workspace.paths import is_template_scaffold
from lup.ledger.writeup import WRITEUP_COMMAND
from lup_template.writeups import WRITEUPS
from lup.harness.codescan.registry import RULE_REFERENCE
from lup.devtools.dev.commands import COMMAND_REFERENCE
from lup.devtools.harness.generated_paths import GENERATED_PATHS
from lup.devtools.harness.drift import (
    generate_targets,
    generate_with_report,
    roster_gaps,
)
from lup.devtools.harness.settings import served_tool_grants
from lup.formats.banner import (
    ARTIFACT_COMMENT_ROUTER,
    COMMENT_FREE,
    REGENERATE_COMMAND,
    GeneratedBanner,
)
from lup.harness.environment import tool_server_env
from lup.harness.generation import ArtifactValidationError
from lup.harness.requirements import LostCapability, Requirement, Run
from lup.harness.materialization import (
    AtomicMaterializer,
    MaterializationConflictError,
    discard_staged_write,
    refused_write,
)
from lup.harness.validation import validated_tree
from lup.harness.models import (
    BulletItem,
    BulletList,
    InlinePart,
    Passage,
    GUIDANCE_BUDGET,
    INVOCATION_SIGILS,
    GuidanceBudget,
    Agent,
    Argument,
    Artifact,
    ArgumentsRef,
    ArtifactTree,
    AskUser,
    Delegate,
    Harness,
    InvocationArgument,
    MarkdownTable,
    NativePath,
    Plugin,
    PluginPath,
    PromptDocument,
    PromptPart,
    RelocateSession,
    NestedRun,
    WatchOutput,
    WhereTaken,
    CommandInvocation,
    RequestApproval,
    ResolverEntry,
    RuntimeDocs,
    SemanticPart,
    Skill,
    SkillInvocation,
    SkillPattern,
    SpellingExample,
    TextPart,
    ToolRoster,
    document_byte_size,
)
from lup.harness.contracts import PromptRenderer
from lup.formats.markdown import CodeCell, PlainCell, ProseCode, ProseStrong
from lup.harness.ownership import (
    OwnershipManifest,
    OwnershipManifestError,
    build_manifest,
    content_digest,
    generated_artifacts,
    load_manifest,
    save_manifest,
)
from lup.harness.proposals import ReconciliationProposalWriter
from lup.harness.reconciliation import (
    CurrentArtifact,
    CurrentTree,
    DeterministicReconciler,
    FilesystemCurrentTreeReader,
    source_patch_base_digest,
)
from lup.policy.bundle import policy_kernel_modules
from lup.policy.dispatcher import (
    SHARED_MEMBER,
    DECISIONS_MEMBER,
    SPLICED_MEMBERS,
    SHARED_PACKAGE,
    DispatcherDeclaration,
    SourceHalf,
    compile_dispatcher,
    resolvable,
    source_half,
    stranded_breaches,
)
from lup.types import EnvVars
from lup.tools.toolsets import startup_names
from lup_template.agent.toolsets import EXAMPLE_GROUP, NOTES_GROUP, declared_tool_groups
from lup.mcp.serve import harness_session_context
from lup_template.devtools.agent.serve import collect_tools_by_server
from lup.devtools.dev.rules import rule_reference_artifact
from lup_template.harness.catalog import (
    HARNESS_SESSION,
    launched_serve,
    launched_tool_servers,
    portable_harness,
)
from lup_template.harness.content.docs.catalog import documents
from lup_template.harness.content.catalog import GUIDANCE as COMPOSED_GUIDANCE
from lup_template.harness.content.catalog import LAYOUT
from lup_template.harness.content.modules.catalog import (
    closing_modules,
    opening_modules,
)
from lup_template.harness.content.settings import project_settings
from lup.mcp import ServeLaunch, ToolServer
from lup.providers.claude import ClaudeTools
from lup.providers.claude.launch import claude_mcp_arguments, claude_server_environment
from lup.providers.codex import CodexTools
from lup.providers.codex.launch import codex_mcp_arguments
import lup.providers.codex.launch as codex_launch
from lup.launch.declaration import InnerSandbox, Mount
from lup.providers.claude import Claude
from lup.providers.claude.launch import claude_settings, companion_plugin_directories
from lup.providers.codex.launch import codex_envelope
from lup.policy.kernel.shell import sandbox_excluded
from lup_template.harness.content.template_claude import (
    DOCUMENT as TEMPLATE_CLAUDE,
)
from lup_template.harness.content.template_codex import (
    DOCUMENT as TEMPLATE_CODEX,
)
from lup_template.harness.composition import (
    claude_target,
    codex_target,
)
from lup.harness.generate import (
    GenerationRecipe,
    NativeHarnessComposition,
    current_reader,
    manifest_of,
    generate,
    inspect_generation,
)

GUIDANCE = COMPOSED_GUIDANCE
"""The guidance this repository actually ships, module selection included."""


def settings_read(agent: Claude, tree: Path) -> dict[str, Any]:
    """The settings document a launch of ``agent`` carries, read back as a CLI reads it."""
    return json.loads(json.dumps(claude_settings(agent, tree)))


class ClaudeHookDecision(BaseModel, frozen=True):
    """Validated decision emitted by the generated Claude dispatcher."""

    hook_event_name: Literal["PreToolUse"] = Field(alias="hookEventName")
    permission_decision: Literal["allow", "ask", "deny"] = Field(
        alias="permissionDecision"
    )
    permission_decision_reason: str = Field(alias="permissionDecisionReason")


class ClaudeHookOutput(BaseModel, frozen=True):
    """Generated Claude hook output envelope."""

    hook_specific_output: ClaudeHookDecision = Field(alias="hookSpecificOutput")
    system_message: str = Field(default="", alias="systemMessage")

    def effect(self) -> str:
        """``ask`` for a call parked for the operator, else the decision itself."""
        decided = self.hook_specific_output.permission_decision
        return "ask" if decided == "deny" and self.system_message else decided


class CodexPermissionDecision(BaseModel, frozen=True):
    """Validated permission decision emitted by the Codex dispatcher."""

    behavior: Literal["allow", "deny"]
    message: str = ""


class CodexPermissionHookOutput(BaseModel, frozen=True):
    """Codex PermissionRequest hook-specific output."""

    hook_event_name: Literal["PermissionRequest"] = Field(alias="hookEventName")
    decision: CodexPermissionDecision


class CodexPermissionOutput(BaseModel, frozen=True):
    """Generated Codex permission hook output envelope."""

    hook_specific_output: CodexPermissionHookOutput = Field(alias="hookSpecificOutput")


class ShippedDispatcher(BaseModel, frozen=True):
    """One hook dispatcher: its declaration, its half, its script, its runtime."""

    declaration: DispatcherDeclaration
    asset: Path
    script: Path
    runtime: Path


SHIPPED_DISPATCHERS: dict[str, ShippedDispatcher] = {
    "claude": ShippedDispatcher(
        declaration=CLAUDE_DISPATCHER,
        asset=Path("packages/lup/src/lup/providers/claude/assets/policy_dispatcher.py"),
        script=Path(".claude/plugins/lup/hooks/scripts/policy.py"),
        runtime=Path(".claude/plugins/lup/hooks/runtime"),
    ),
    "codex": ShippedDispatcher(
        declaration=CODEX_DISPATCHER,
        asset=Path("packages/lup/src/lup/providers/codex/assets/policy_dispatcher.py"),
        script=Path(".codex/plugins/lup/hooks/scripts/policy.py"),
        runtime=Path(".codex/plugins/lup/hooks/runtime"),
    ),
}
"""Every script a session's permissions run through, canonical and shipped."""

SHARED_DISPATCHER_HALF = Path("packages/lup/src/lup/policy/assets/host.py")
"""The host-side half both dispatchers above are compiled from."""


def compiled_functions(script: str) -> dict[str, str]:  # lup: ignore[dict-str-payload]
    """Every top-level function of a compiled dispatcher, by name.

    Open keys by construction: whatever the compiled source happens to define
    is what a reader of it can compare, which is the point of reading it back.
    """
    tree = ast.parse(script)
    return {
        node.name: ast.get_source_segment(script, node) or ""
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


class PyrightExecutionEnvironment(BaseModel, frozen=True):
    """One type-checking scope and the search paths it resolves imports on."""

    root: Path
    extra_paths: list[Path] = Field(alias="extraPaths", default=[])


class PyrightConfiguration(BaseModel, frozen=True):
    """The workspace type-checking scope, as pyproject.toml declares it."""

    include: list[Path]
    exclude: list[Path] = []
    execution_environments: list[PyrightExecutionEnvironment] = Field(
        alias="executionEnvironments", default=[]
    )


def test_catalog_has_one_portable_skill_per_baseline_command() -> None:
    harness = portable_harness()
    plugin = harness.plugins[0]

    assert len(plugin.skills) == len({skill.id for skill in plugin.skills})
    assert len(plugin.agents) == 4
    assert {skill.name for skill in plugin.skills} >= {
        "resolve",
        "implementer",
        "resolve-reviewer",
        "merge",
        "analyze",
    }
    encoded = harness.model_dump_json()
    assert "/lup:" not in encoded
    assert "$lup:" not in encoded


def test_project_tests_follow_the_ordinary_edit_policy() -> None:
    hooks = portable_harness().plugins[0].hooks

    assert hooks is not None
    assert hooks.acceptance_guard is None


def test_claude_tree_renders_every_typed_support_document() -> None:
    paths = {
        artifact.path for artifact in claude_target(Path.cwd()).recipe.desired.artifacts
    }

    assert Path("docs/orchestration.md") in paths
    assert Path("docs/patterns.md") in paths
    assert Path(".claude/plugins/lup/TEMPLATE_CLAUDE.md") in paths
    assert Path(".claude/plugins/lup/scripts/file_suggest.sh") in paths
    assert Path(".claude/settings.json") in paths
    assert {document.path for document in documents(Path.cwd())} <= paths


def test_every_published_document_is_generated_and_banners_itself() -> None:
    """A document under docs/ that generation does not own could be hand-edited.

    The roster is the only source of documents, so an entry missing from the
    tree, a stray file beside them, or a page whose banner does not name its
    own module all mean a reader cannot trust the banner to be true.
    """
    artifacts = {
        artifact.path: artifact
        for artifact in claude_target(Path.cwd()).recipe.desired.artifacts
    }
    roster = documents(Path.cwd())
    published = {document.path for document in roster}
    unmanaged = sorted(
        path for path in Path("docs").glob("*.md") if path not in published
    )

    # The three pages a repository writer produces rather than the docs
    # roster: each renders from something walked at generation time — the rule
    # registry, the composed CLI, the compiled trees — so none of them has a
    # declaring content module to be rostered against. And the writeups:
    # documents generated from the ledger and committed like any other
    # document — as repository writers where every kind they render is
    # committed with the code, by `ledger writeup` alone where one is local —
    # so a declared one that was never generated is missing here.
    writeups = [Path(writeup.path) for writeup in WRITEUPS]
    assert unmanaged == sorted(
        [Path(COMMAND_REFERENCE), Path(RULE_REFERENCE), GENERATED_PATHS, *writeups]
    )
    for writeup in WRITEUPS:
        banner = GeneratedBanner(source=writeup.source, command=WRITEUP_COMMAND)
        content = Path(writeup.path).read_text(encoding="utf-8")
        assert banner.opens(Path(writeup.path), content)
    for document in roster:
        banner = GeneratedBanner(
            source=document.document.declared_source(), command=REGENERATE_COMMAND
        )
        assert banner.opens(document.path, artifacts[document.path].content)


def test_platform_parity_audits_live_runtime_capabilities() -> None:
    page = next(
        document.document
        for document in documents(Path.cwd())
        if document.path == Path("docs/platform-differentiation.md")
    )
    content = claude_prompt_renderer().render(page)
    for family in (
        "Launch readiness",
        "Authentication",
        "Host bridges",
        "Delegated-agent paths",
        "Diagnostics and tests",
    ):
        assert f"| {family} |" in content


def test_guidance_reaches_sections_by_name_not_by_anchor() -> None:
    """Guidance carried links to sections that live only in the adopter template.

    A same-document anchor resolves right up until the text it names moves to
    another document, and then fails silently. Naming the section, or the file
    that holds it, survives the move that broke the anchor.
    """
    rendered = claude_prompt_renderer().render(GUIDANCE)

    assert "](#" not in rendered


def test_guidance_stays_within_its_always_loaded_budget() -> None:
    """The budget bounds what a session loads, not what the declaration holds.

    Measured in UTF-8 bytes, because that is the unit the runtime's own
    ceiling counts in — a character count runs looser than the real cap
    wherever the document uses non-ASCII punctuation, and would pass a
    document the runtime would silently truncate.
    """
    declared = GUIDANCE.text_size()
    rendered = {
        "claude": claude_prompt_renderer().render(GUIDANCE),
        "codex": codex_prompt_renderer().render(GUIDANCE),
    }

    for runtime, document in rendered.items():
        used = document_byte_size(document)
        assert used >= declared
        assert used <= GUIDANCE_BUDGET.ceiling, (
            f"{runtime} guidance is {used} bytes, over budget by "
            f"{used - GUIDANCE_BUDGET.ceiling}"
        )


def test_the_scaffold_leaves_its_adopter_room_inside_the_runtime_ceiling() -> None:
    """A template may spend only part of the budget every domain inherits.

    The runtime ceiling is what a session will load. A repository still
    shipping as the scaffold is writing guidance every domain built on it
    starts from, and that domain then describes its own architecture and
    conventions inside whatever is left — so the scaffold is held to the
    smaller number, and the difference is what the adopter gets.
    """
    for composition in (claude_target(Path.cwd()), codex_target(Path.cwd())):
        for artifact in guidance_artifacts(composition.recipe.desired):
            used = document_byte_size(artifact.content)
            ceiling = GUIDANCE_BUDGET.scaffold_ceiling
            assert used <= ceiling, (
                f"{artifact.path.as_posix()} is {used} bytes, over the "
                f"{ceiling} scaffold ceiling by {used - ceiling}. It fits the "
                f"runtime's {GUIDANCE_BUDGET.ceiling}, but leaves an adopting "
                f"domain less than the {GUIDANCE_BUDGET.template_headroom} bytes "
                "reserved for its own guidance."
            )


def test_the_scaffold_ceiling_is_weighed_only_while_the_flag_stands() -> None:
    """An adopted repository is held to the runtime ceiling and nothing else.

    The reservation exists to stop a scaffold spending its adopter's budget.
    Once adopted there is no further adopter to reserve for, and a domain that
    inherited a lean document is entitled to spend what it saved.
    """
    fits = GUIDANCE_BUDGET.scaffold_ceiling

    assert [report.name for report in budget_reports(fits, scaffold=True)] == [
        "guidance budget",
        "scaffold budget",
    ]
    assert [report.name for report in budget_reports(fits, scaffold=False)] == [
        "guidance budget"
    ]


def test_a_document_between_the_two_ceilings_fails_only_the_scaffold() -> None:
    """The two rows answer different questions, so they disagree in that band.

    A document over the scaffold's share but inside the runtime's will reach a
    session whole — nothing is truncated, and the runtime row is right to pass.
    What it has spent is its adopter's room, which is the only row that can say
    so. If both rows ever agree on everything, one of them is redundant.
    """
    between = GUIDANCE_BUDGET.scaffold_ceiling + 1

    verdicts = {report.name: report.passed for report in budget_reports(between, True)}

    assert verdicts == {"guidance budget": True, "scaffold budget": False}


def test_this_repository_is_still_the_scaffold_that_reservation_assumes() -> None:
    """The ceiling above binds only while the flag stands, so pin that it does.

    Were the flag cleared here, both assertions about the lean document would
    keep passing while nothing enforced them any more.
    """
    assert is_template_scaffold(Path.cwd())


def test_the_scaffold_row_reports_the_reservation_it_withholds() -> None:
    """Over budget, the row names the overage rather than only failing."""
    ceiling = GUIDANCE_BUDGET.scaffold_ceiling

    fits = scaffold_budget_report(ceiling)
    over = scaffold_budget_report(ceiling + 1)

    assert fits.passed
    assert not over.passed
    assert "over by 1" in over.lines[0]
    assert str(GUIDANCE_BUDGET.template_headroom) in fits.lines[0]


def test_the_scaffold_row_reports_the_room_a_session_may_still_spend() -> None:
    """A green row tells a writer how much it may add, not only that it fit.

    The reservation is a constant, so a row printing only that says the same
    thing at 23 bytes free as at 11 KiB, and a session learns the ceiling
    exists from the gate refusing writing it has already done. Both rows
    report the room left, in one shape, so either one answers before it does.
    """
    ceiling = GUIDANCE_BUDGET.scaffold_ceiling

    fits = scaffold_budget_report(ceiling - 23)
    runtime = guidance_budget_report(GUIDANCE_BUDGET.ceiling - 23)

    assert fits.lines == [
        f"scaffold budget: ok — {ceiling - 23}/{ceiling} bytes, 23 free, "
        f"{GUIDANCE_BUDGET.template_headroom} reserved for the adopting domain"
    ]
    assert runtime.lines == [
        f"guidance budget: ok — {GUIDANCE_BUDGET.ceiling - 23}/"
        f"{GUIDANCE_BUDGET.ceiling} bytes, 23 free"
    ]


def test_the_scaffold_row_over_budget_names_the_overage_and_not_the_room() -> None:
    """Room left is what a passing row adds; a failing one has none to state."""
    ceiling = GUIDANCE_BUDGET.scaffold_ceiling

    over = scaffold_budget_report(ceiling + 1_078)

    assert over.lines == [
        f"scaffold budget: FAIL (over by 1078) — {ceiling + 1_078}/{ceiling} "
        f"bytes, {GUIDANCE_BUDGET.template_headroom} reserved for the adopting domain"
    ]


def test_a_project_may_reserve_a_different_share_than_this_one() -> None:
    """The headroom is this repository's judgement, not a fact about a runtime.

    A downstream project with a thinner scaffold, or none, states its own
    share rather than forking the module that declares the default.
    """
    used = GUIDANCE_BUDGET.ceiling - 1_000

    assert scaffold_budget_report(used, GuidanceBudget(template_headroom=512)).passed
    assert not scaffold_budget_report(
        used, GuidanceBudget(template_headroom=4_096)
    ).passed


def test_what_a_scaffold_may_spend_is_derived_from_the_two_it_is_given() -> None:
    """The number every caller wanted, which neither field carries alone.

    Written out by hand it was the same subtraction at every site that needed
    it, and a project moving either number moved it at seven of them.
    """
    budget = GuidanceBudget(ceiling=20_000, template_headroom=8_000)

    assert budget.scaffold_ceiling == 12_000


def test_a_reserve_larger_than_the_ceiling_is_refused() -> None:
    """Both readings of a clamp are wrong, so the project is asked instead.

    Keeping nothing back abandons the reserve; lowering the ceiling tells a
    runtime to load less than it will. Only the project can say which of its
    two numbers it meant.
    """
    with pytest.raises(ValidationError, match="does not fit inside"):
        GuidanceBudget(ceiling=1_000, template_headroom=2_000)


def test_codex_config_states_the_same_ceiling_the_check_enforces() -> None:
    """A generated config that disagreed with the check would truncate silently."""
    config = next(
        artifact
        for artifact in codex_target(Path.cwd()).recipe.desired.artifacts
        if artifact.path.as_posix() == ".codex/config.toml"
    )
    assert f"project_doc_max_bytes = {GUIDANCE_BUDGET.ceiling}" in config.content


def test_codex_tree_renders_the_agents_flavored_template() -> None:
    paths = {
        artifact.path for artifact in codex_target(Path.cwd()).recipe.desired.artifacts
    }

    assert Path(".codex/plugins/lup/TEMPLATE_AGENTS.md") in paths


def test_template_flavors_share_sections_and_differ_natively() -> None:
    claude_render = claude_prompt_renderer().render(TEMPLATE_CLAUDE)
    codex_render = codex_prompt_renderer().render(TEMPLATE_CODEX)
    parity = "Every supported runtime must provide equivalent user-visible behavior"
    assert parity in claude_render
    assert parity in codex_render

    def sections(render: str) -> list[str]:
        return [
            line for line in render.splitlines() if line.startswith("<!-- section: ")
        ]

    assert sections(claude_render)[0] == "<!-- section: CLAUDE.md -->"
    assert sections(codex_render)[0] == "<!-- section: AGENTS.md -->"
    assert sections(claude_render)[1:] == sections(codex_render)[1:]
    assert "/lup:init" in claude_render and "$lup:" not in claude_render
    assert "$lup:init" in codex_render and "/lup:" not in codex_render
    assert "AskUserQuestion" in claude_render
    assert "AskUserQuestion" not in codex_render
    # Session relocation is a Claude tool; Codex can only be told to start
    # a session in the worktree, never to call one it does not have.
    assert "EnterWorktree" in claude_render and "ExitWorktree" in claude_render
    assert "EnterWorktree" not in codex_render
    assert "ExitWorktree" not in codex_render


def test_repository_guidance_requires_semantic_parity_on_both_runtimes() -> None:
    assert "Every runtime, same semantics." in claude_prompt_renderer().render(GUIDANCE)
    assert "Every runtime, same semantics." in codex_prompt_renderer().render(GUIDANCE)


def test_template_flavors_render_the_declared_codeintel_tools() -> None:
    expected = ToolRoster(tools=list(CODEINTEL_TOOL_DECLARATIONS)).text_payload

    assert expected in claude_prompt_renderer().render(TEMPLATE_CLAUDE)
    assert expected in codex_prompt_renderer().render(TEMPLATE_CODEX)


def registered_hook_commands(config: str, events: list[str] | None = None) -> list[str]:
    """Every shell command one runtime's hook registration would run.

    Narrowed to *events* where given, so a property of the policy's own
    registrations can be asserted without the prompt event's fold answering
    for it.
    """
    registered = json.loads(config)["hooks"]
    return [
        hook["command"]
        for event, entries in registered.items()
        if events is None or event in events
        for entry in entries
        for hook in entry["hooks"]
    ]


def test_claude_recipe_overrides_legacy_hook_entry_with_hermetic_dispatcher() -> None:
    """The hook starts outside the workspace, so nothing it runs may need uv.

    The registered command stays short because the native UI can echo it.
    Recovery instructions belong in the generated guard it invokes.
    """
    recipe = claude_target(Path.cwd()).recipe
    artifacts = {artifact.path: artifact for artifact in recipe.desired.artifacts}

    hook_config = artifacts[Path(".claude/plugins/lup/hooks/hooks.json")].content
    registered = registered_hook_commands(hook_config)
    # More than one hook is registered — the policy dispatcher and the peer
    # delivery guard — and the property under test belongs to all of them:
    # each starts outside the workspace, so none of them may reach for `uv`.
    for command in registered:
        assert "uv" not in shlex.split(command)
        assert "hooks/scripts/" in command
        assert REGENERATE_COMMAND not in command
    assert any("hooks/scripts/policy.sh" in command for command in registered)
    assert artifacts[Path(".claude/plugins/lup/hooks/scripts/policy.sh")].executable
    assert Path(".claude/plugins/lup/hooks/runtime/kernel/shell.py") in artifacts
    assert Path(".claude/plugins/lup/hooks/runtime/policy_data.py") in artifacts
    assert Path(".claude/plugins/lup/hooks/runtime/evidence.json") in artifacts


def test_peer_delivery_is_registered_for_every_tool_and_refuses_nothing() -> None:
    """The second hook group, and the two properties that keep it safe.

    Its matcher is empty, which is every tool: mail is worth carrying before a
    `Read` as much as before an `Edit`, and the policy matcher deliberately
    names neither. And its command cannot refuse — no `exit 2` beside it — so
    a mailbox nobody can read never becomes a tool call nobody can make.
    """
    recipe = claude_target(Path.cwd()).recipe
    artifacts = {artifact.path: artifact for artifact in recipe.desired.artifacts}
    hooks = json.loads(artifacts[Path(".claude/plugins/lup/hooks/hooks.json")].content)

    delivering = [
        group for group in hooks["hooks"]["PreToolUse"] if group["matcher"] == ""
    ]

    assert len(delivering) == 1
    command = delivering[0]["hooks"][0]["command"]
    assert "coordination_delivery.sh" in command
    assert "exit 2" not in command
    guard = Path(".claude/plugins/lup/hooks/scripts/coordination_delivery.sh")
    assert artifacts[guard].executable
    runtime = Path(".claude/plugins/lup/hooks/runtime/coordination_delivery.py")
    assert runtime in artifacts


def test_codex_recipe_registers_semantic_permission_approval() -> None:
    recipe = codex_target(Path.cwd()).recipe
    artifacts = {artifact.path: artifact for artifact in recipe.desired.artifacts}

    hook_config = artifacts[Path(".codex/plugins/lup/hooks/hooks.json")].content
    assert '"PermissionRequest"' in hook_config
    # Tool events carry the policy and native mailbox delivery; the prompt
    # event carries the separate roster fold.
    commands = registered_hook_commands(hook_config, CODEX_DISPATCHER.hook_events)
    for command in commands:
        assert "uv" not in shlex.split(command)
        assert "hooks/scripts/" in command
        assert REGENERATE_COMMAND not in command
    assert any("hooks/scripts/policy.sh" in command for command in commands)
    assert artifacts[Path(".codex/plugins/lup/hooks/scripts/policy.sh")].executable


def test_the_watching_event_is_registered_for_what_leaves_writes_behind() -> None:
    """A narrow matcher, because this event records rather than judges.

    Narrow means *what leaves a write behind*, not *what names a file*. The
    editing tools and the shell tool both do, and both have a reading
    afterwards — an edited file to check, a command's result to review and
    attribute — while a fetch leaves nothing, so sharing the deciding events'
    registration would spawn the script to find no write at all.

    The shell tool is pinned here because leaving it out fails silently: the
    review of what a command wrote was wired into both dispatchers and
    reachable from neither, for exactly as long as this matcher named only the
    tools that carry a file path.
    """
    for target, plugin_root, edits in (
        (claude_target, ".claude", "Edit|Write|Bash"),
        (codex_target, ".codex", "apply_patch|Bash"),
    ):
        artifacts = {
            artifact.path: artifact
            for artifact in target(Path.cwd()).recipe.desired.artifacts
        }
        registered = json.loads(
            artifacts[Path(f"{plugin_root}/plugins/lup/hooks/hooks.json")].content
        )["hooks"]

        assert registered["PostToolUse"][0]["matcher"] == edits
        assert registered["PreToolUse"][0]["matcher"] != edits


def test_generated_resolver_entries_only_launch_the_shared_python_core() -> None:
    harness = portable_harness()
    claude = {
        artifact.path: artifact.content
        for artifact in compile_claude(harness).artifacts
    }
    codex = {
        artifact.path: artifact.content for artifact in compile_codex(harness).artifacts
    }
    command = claude[Path(".claude/plugins/lup/commands/resolve.md")]
    skill = codex[Path(".codex/plugins/lup/skills/resolve/SKILL.md")]

    assert "uv run lup-devtools resolve --adapter claude --detach" in command
    assert "Triage into concerns" not in command
    assert "Workflow(" not in command
    assert "uv run lup-devtools resolve --adapter codex --detach" in skill
    assert "scheduling" not in skill
    for entry in (command, skill):
        assert "exactly one watch" in entry
        assert "--run-id" in entry and "--answer" in entry
        # The entry named flags the CLI has never had, and the acceptance
        # question it pointed at instead does not exist either. An entry
        # that documents a flag into being is worse than one that omits it:
        # the reader spends a turn on `No such option`.
        assert "--accept" not in entry and "--reject" not in entry
        assert "integration-assembly" in entry
    # Each entry names its own runtime's waiter rather than the idea of one.
    # The neutral wording — "the runtime's event-driven waiter" — was true of
    # Claude and false of Codex, where reading the session is the mechanism
    # rather than the mistake, so a reader on either had to guess which tool
    # was meant. The guess is an ordinary command with a long timeout, which
    # reports once, at the end.
    assert "`Monitor`" in command
    assert "exec_command" in skill and "write_stdin" in skill


def test_invocation_renderers_own_complete_spelling_and_escaping() -> None:
    invocation = SkillInvocation(
        plugin="lup",
        skill="merge",
        arguments=[InvocationArgument(name="target", value="feature with spaces")],
    )

    assert ClaudeSpellings().render(invocation) == (
        "/lup:merge target='feature with spaces'"
    )
    assert CodexSpellings().render(invocation) == (
        "$lup:merge target='feature with spaces'"
    )


def test_prompt_renderer_preserves_ordinary_trailing_whitespace() -> None:
    prompt = PromptDocument(parts=[TextPart(text="keep these spaces  ")])

    assert codex_prompt_renderer().render(prompt) == ("keep these spaces  \n")


def test_argument_reference_has_one_semantic_part_and_native_renderings() -> None:
    prompt = PromptDocument(parts=[ArgumentsRef()])

    assert claude_prompt_renderer().render(prompt) == ("$ARGUMENTS\n")
    assert codex_prompt_renderer().render(prompt) == (
        "the arguments supplied with this skill invocation\n"
    )


class PartExpectation(BaseModel, frozen=True):
    """One prompt part and whether its two native renderings must differ."""

    part: PromptPart
    diverges: bool


PART_CONTRACT: dict[str, PartExpectation] = {
    "TextPart": PartExpectation(part=TextPart(text="plain prose"), diverges=False),
    "SpellingExample": PartExpectation(
        part=SpellingExample(text="`/lup:merge` beside `$lup:merge`"), diverges=False
    ),
    "MarkdownTable": PartExpectation(
        part=MarkdownTable(
            headers=["Rule id", "Diagnostic"],
            rows=[[CodeCell(text="dict-get"), PlainCell(text="a | b")]],
        ),
        diverges=False,
    ),
    "ToolRoster": PartExpectation(
        part=ToolRoster(tools=list(CODEINTEL_TOOL_DECLARATIONS)),
        diverges=False,
    ),
    "SkillInvocation": PartExpectation(
        part=SkillInvocation(plugin="lup", skill="merge"), diverges=True
    ),
    "NativePath": PartExpectation(
        part=NativePath(location="ownership_manifest"), diverges=True
    ),
    "PluginPath": PartExpectation(
        part=PluginPath(plugin="lup", location="skills", member="merge"), diverges=True
    ),
    "SkillPattern": PartExpectation(
        part=SkillPattern(plugin="lup", placeholder="<name>"), diverges=True
    ),
    "RuntimeDocs": PartExpectation(part=RuntimeDocs(), diverges=True),
    "AskUser": PartExpectation(
        part=AskUser(question="which branch to land first"), diverges=True
    ),
    "Delegate": PartExpectation(
        part=Delegate(subagent_type="lup:trace-explorer", prompt="read the trace"),
        diverges=True,
    ),
    "RequestApproval": PartExpectation(
        part=RequestApproval(action="pushing", reason="it is visible"), diverges=False
    ),
    "RelocateSession": PartExpectation(
        part=RelocateSession(path="the path step 1 prints"), diverges=True
    ),
    "NestedRun": PartExpectation(
        part=NestedRun(prompt="reply with the single word ok"), diverges=True
    ),
    "WatchOutput": PartExpectation(
        part=WatchOutput(command="lup-devtools resolve status --watch"),
        diverges=True,
    ),
    # The same words for both, because both reach the same shell: what a
    # runtime spells differently is how it *asks* for something, and this asks
    # for nothing — it names a command the reader runs.
    "CommandInvocation": PartExpectation(
        part=CommandInvocation(path=["dev", "check"], arguments="--no-test"),
        diverges=False,
    ),
    "ResolverEntry": PartExpectation(part=ResolverEntry(), diverges=True),
    "ArgumentsRef": PartExpectation(part=ArgumentsRef(), diverges=True),
    # A passage is prose and the values it names, so it diverges exactly
    # where one of its values does. This one names an argument reference,
    # which each runtime spells in its own sigil.
    "Passage": PartExpectation(
        part=Passage(
            module="lup.harness.content.skills.analyze",
            values={
                "arguments": ArgumentsRef(),
                "ask": AskUser(question="which conversation to read"),
            },
        ),
        diverges=True,
    ),
    "InlinePart": PartExpectation(
        part=InlinePart(node=ProseCode(text="agent/core.py")), diverges=False
    ),
    "BulletList": PartExpectation(
        part=BulletList(
            items=[BulletItem(lead=ProseStrong(text="Open notes"), text="a | b")]
        ),
        diverges=False,
    ),
    # Parts that exist only where one other module is taken render as what
    # they hold, so they diverge exactly where what they hold does.
    "WhereTaken": PartExpectation(
        part=WhereTaken(
            module="git-workflow",
            parts=[SkillInvocation(plugin="lup", skill="merge")],
        ),
        diverges=True,
    ),
}
"""Every prompt part, with the cross-runtime promise its renderings make."""


def test_every_prompt_part_states_what_it_promises_across_runtimes() -> None:
    """A part neither renderer handles renders as nothing, silently.

    The base catches the kind that never says how it is spelled; this catches
    the kind nobody decided about — whether its two renderings agree is a
    design choice no type can hold.
    """
    union, _ = get_args(PromptPart.__value__)

    assert sorted(PART_CONTRACT) == sorted(
        member.__name__ for member in get_args(union)
    )


@pytest.mark.parametrize("name", sorted(PART_CONTRACT))
def test_each_prompt_part_keeps_its_cross_runtime_promise(name: str) -> None:
    expectation = PART_CONTRACT[name]
    prompt = PromptDocument(parts=[expectation.part])

    claude = claude_prompt_renderer().render(prompt)
    codex = codex_prompt_renderer().render(prompt)

    assert claude.strip() and codex.strip()
    assert (claude != codex) is expectation.diverges


class PartQuestion(BaseModel, frozen=True):
    """One question the harness asks a part, and the kinds that answer it."""

    ask: Callable[[SemanticPart], bool]
    answered_by: list[str]


PART_QUESTIONS: dict[str, PartQuestion] = {
    "text_payload": PartQuestion(
        ask=lambda part: part.text_payload is not None,
        answered_by=[
            "TextPart",
            "SpellingExample",
            "MarkdownTable",
            "ToolRoster",
            "CommandInvocation",
            "Passage",
            "BulletList",
        ],
    ),
    # What a skill's own `tools` grant is checked against, so a step telling
    # its reader to run something is one the skill actually granted a shell
    # for. Both kinds that name a command answer it.
    "shell_command": PartQuestion(
        ask=lambda part: part.shell_command is not None,
        answered_by=["WatchOutput", "CommandInvocation"],
    ),
    "invocation": PartQuestion(
        ask=lambda part: part.invocation is not None, answered_by=["SkillInvocation"]
    ),
    "named_plugin": PartQuestion(
        ask=lambda part: part.named_plugin is not None,
        answered_by=["SkillInvocation", "PluginPath", "SkillPattern"],
    ),
    "named_agent": PartQuestion(
        ask=lambda part: part.named_agent is not None, answered_by=["Delegate"]
    ),
    "references_arguments": PartQuestion(
        ask=lambda part: part.references_arguments, answered_by=["ArgumentsRef"]
    ),
}
"""Every question a walk asks a part rather than deciding about it from outside."""


@pytest.mark.parametrize("question", sorted(PART_QUESTIONS))
def test_each_question_is_answered_by_exactly_the_parts_that_carry_it(
    question: str,
) -> None:
    """A kind that carries something and declines to say so is the silent bug.

    Every question here defaults to declining, so a walk that asks it can never
    miss a kind by omission — but a kind that carries prose, or names a plugin,
    and forgets to answer would go unseen. Pinning who answers turns that into
    a failure the moment a new kind joins ``PART_CONTRACT``.
    """
    asked = PART_QUESTIONS[question]

    answering = [
        name
        for name, expectation in PART_CONTRACT.items()
        if asked.ask(expectation.part)
    ]

    assert answering == asked.answered_by


@pytest.mark.parametrize("name", sorted(PART_CONTRACT))
def test_each_prompt_part_round_trips_through_its_discriminator(name: str) -> None:
    """Answering for itself leaves a part exactly as parseable as it was."""
    document = PromptDocument(parts=[PART_CONTRACT[name].part])

    restored = PromptDocument.model_validate_json(document.model_dump_json())

    assert restored == document
    assert type(restored.parts[0]) is type(document.parts[0])


def test_prompt_documents_parse_every_part_from_its_discriminator() -> None:
    """One document holding every kind still validates through ``type`` alone."""
    payload = {
        "parts": [
            expectation.part.model_dump() for expectation in PART_CONTRACT.values()
        ]
    }

    document = PromptDocument.model_validate(payload)

    assert [type(part).__name__ for part in document.parts] == list(PART_CONTRACT)


def test_an_undeclared_plugin_behind_an_invocation_names_the_invocation() -> None:
    """One part answers two questions, and the sharper gate must speak first.

    An invocation names a plugin, so both the plugin gate and the invocation
    gate see it — but only one of them can say which skill went missing.
    """
    source = portable_harness().model_dump()
    source["guidance"]["parts"].append(
        SkillInvocation(plugin="absent", skill="merge").model_dump()
    )

    with pytest.raises(ValueError, match="unknown declaration: absent:merge"):
        Harness.model_validate(source)


class UnansweredPart(SemanticPart, frozen=True):
    """A thirteenth kind that declines to say how it should be spelled."""

    type: Literal["unanswered"] = "unanswered"


class AnsweredPart(SemanticPart, frozen=True):
    """A thirteenth kind that answers the base and declines everything else."""

    type: Literal["answered"] = "answered"

    def spell(self, renderer: PromptRenderer) -> str:
        return f"{renderer.own.runtime_name} answered"


def test_a_part_that_answers_nothing_cannot_be_constructed() -> None:
    """The base is what forces a new kind to decide, not a walk that meets it."""

    def construct(kind: type[SemanticPart]) -> SemanticPart:
        return kind()

    assert construct(AnsweredPart)

    with pytest.raises(TypeError, match="abstract"):
        construct(UnansweredPart)


def test_a_new_kind_of_part_renders_without_editing_a_renderer_or_walk() -> None:
    """The walk hands over the reader's vocabulary and never names a kind."""
    document = PromptDocument.model_construct(parts=[AnsweredPart()])

    assert claude_prompt_renderer().render(document) == (
        f"{ClaudeSpellings().runtime_name} answered\n"
    )
    assert codex_prompt_renderer().render(document) == (
        f"{CodexSpellings().runtime_name} answered\n"
    )
    assert document.text_size() == 0


def test_every_tree_paths_read_the_same_under_either_runtime() -> None:
    """Prose teaching every tree must not depend on which runtime renders it."""
    prompt = PromptDocument(
        parts=[
            NativePath(location="guidance_file", scope="every_tree"),
            TextPart(text=" and "),
            PluginPath(plugin="lup", location="skills", scope="every_tree"),
        ]
    )

    rendered = claude_prompt_renderer().render(prompt)

    assert rendered == codex_prompt_renderer().render(prompt)
    assert rendered == (
        ".claude/CLAUDE.md under Claude Code, AGENTS.md under Codex and "
        ".claude/plugins/lup/commands/ under Claude Code, "
        ".codex/plugins/lup/skills/ under Codex\n"
    )


@pytest.mark.parametrize(
    ("arguments", "parts"),
    [
        ([Argument(name="target", description="Target")], [TextPart(text="body")]),
        ([], [ArgumentsRef()]),
    ],
)
def test_skill_argument_declarations_require_a_matching_reference(
    arguments: list[Argument], parts: list[PromptPart]
) -> None:
    with pytest.raises(ValueError, match="argument declarations and ArgumentsRef"):
        Skill(
            id="skill.invalid",
            name="invalid",
            description="Invalid argument declaration",
            arguments=arguments,
            prompt=PromptDocument(parts=parts),
        )


def test_every_typed_content_module_is_reachable_from_a_catalog() -> None:
    """Every retained content module belongs to a declared application catalog.

    Generation imports only adopted modules. Audit every application-owned
    builder, including declined declarations, without changing that selection
    or importing the optional subjects owned by the library.
    """
    for entry in [*opening_modules(LAYOUT), *closing_modules(LAYOUT)]:
        assert entry.build().spec == entry.spec
    content = Path("src/lup_template/harness/content")
    assert content.is_dir()
    loaded = {
        Path(source).resolve()
        for source in (
            getattr(module, "__file__", None) for module in list(sys.modules.values())
        )
        if source is not None
    }
    orphans = [
        path.as_posix()
        for path in sorted(content.rglob("*.py"))
        if path.name != "__init__.py" and path.resolve() not in loaded
    ]

    assert not orphans


def test_source_tree_contains_no_embedded_base64() -> None:
    sources = list(Path("src").rglob("*.py"))

    assert sources
    assert all("base64" not in path.read_text(encoding="utf-8") for path in sources)


def test_retired_native_catalog_paths_stay_deleted() -> None:
    harness = Path("src/lup_template/devtools/harness")

    assert not (harness / "native_catalog.py").exists()
    assert not (harness / "native_overrides.py").exists()
    assert not (harness / "importer.py").exists()


INVOCATION_IN_PROSE = "then run /lup:merge and wait"

PROSE_DECLARATIONS: dict[
    str, Callable[[str], Agent | Argument | Plugin | SemanticPart | Skill]
] = {
    "Agent.description": lambda prose: Agent(
        id="agent.probe",
        name="probe",
        description=prose,
        prompt=PromptDocument(parts=[TextPart(text="body")]),
    ),
    "Argument.description": lambda prose: Argument(name="target", description=prose),
    "AskUser.question": lambda prose: AskUser(question=prose),
    "Delegate.prompt": lambda prose: Delegate(
        subagent_type="lup:trace-explorer", prompt=prose
    ),
    "Plugin.description": lambda prose: Plugin(
        id="plugin.probe",
        name="probe",
        marketplace="probe",
        version="0.0.0",
        description=prose,
        skills=[],
        agents=[],
    ),
    "RelocateSession.path": lambda prose: RelocateSession(path=prose),
    "RequestApproval.action": lambda prose: RequestApproval(
        action=prose, reason="it is visible"
    ),
    "RequestApproval.reason": lambda prose: RequestApproval(
        action="pushing", reason=prose
    ),
    "Skill.argument_hint": lambda prose: Skill(
        id="skill.probe",
        name="probe",
        description="Probe",
        argument_hint=prose,
        prompt=PromptDocument(parts=[TextPart(text="body")]),
    ),
    "Skill.description": lambda prose: Skill(
        id="skill.probe",
        name="probe",
        description=prose,
        prompt=PromptDocument(parts=[TextPart(text="body")]),
    ),
    "TextPart.text": lambda prose: TextPart(text=prose),
}
"""Every field a declaration holds as free text, and how one is declared.

A native tree renders each of these as prose, so each is the whole of the way
an invocation could reach a reader who cannot use it."""


NAMES_RATHER_THAN_PROSE = [
    "Agent.id",
    "Delegate.name",
    "Harness.generator_version",
    "Passage.name",
    "Plugin.id",
    "Plugin.version",
    "Skill.id",
    "SpellingExample.text",
]
"""Declared strings deliberately exempt from the portable-prose constraint.

Most identify or version a declaration rather than teaching anything, so they
never reach a reader as words. ``SpellingExample.text`` is the one that does
and is exempt anyway: its whole subject is what each runtime spells, which is
the one thing portable prose cannot say. ``Passage.name`` says which file
beside a module the words are read from rather than carrying any; the prose
itself is held to the constraint where the scan reads it, through
``text_payload``. Every other free-text field a prompt or its discovery
metadata carries is portable, and a new field has to join one list or the
other rather than quietly accepting anything."""


def test_every_free_text_declaration_field_is_portable_prose() -> None:
    """A field typed plain ``str`` is the one way back to scanning afterwards."""
    union, _ = get_args(PromptPart.__value__)
    declarations = [*get_args(union), Argument, Skill, Agent, Plugin, Harness]

    unconstrained = [
        f"{declaration.__name__}.{name}"
        for declaration in declarations
        for name, field in declaration.model_fields.items()
        if not field.metadata and field.annotation in (str, str | None)
    ]

    assert sorted(unconstrained) == NAMES_RATHER_THAN_PROSE


@pytest.mark.parametrize("field", sorted(PROSE_DECLARATIONS))
def test_declaring_an_invocation_in_prose_is_refused(field: str) -> None:
    """The words are refused where an author writes them, not where they compile."""
    declare = PROSE_DECLARATIONS[field]

    assert declare("then run the merge skill and wait")

    with pytest.raises(ValueError, match="portable prose spells"):
        declare(INVOCATION_IN_PROSE)


def test_a_refused_declaration_names_its_field_and_the_offending_spelling() -> None:
    """Naming both is what lets a contributor go straight to the words."""
    with pytest.raises(ValueError) as refusal:
        Agent(
            id="agent.probe",
            name="probe",
            description=INVOCATION_IN_PROSE,
            prompt=PromptDocument(parts=[TextPart(text="body")]),
        )

    report = str(refusal.value)
    assert "Agent" in report
    assert "description" in report
    assert "'/lup:merge'" in report


def test_no_runtime_spells_an_invocation_portable_prose_would_admit() -> None:
    """The shape is syntax, so each runtime proves its own sigil is one of them.

    That is what lets the declaration layer refuse an invocation without
    knowing which plugins exist or which runtime will read the words.
    """
    for runtime in (ClaudeSpellings(), CodexSpellings()):
        for spelling in (
            runtime.render(SkillInvocation(plugin="lup", skill="merge")),
            runtime.invocation_pattern("lup", "*"),
            runtime.invocation_pattern("lup", "<name>"),
        ):
            assert spelling[0] in INVOCATION_SIGILS
            with pytest.raises(ValueError, match="portable prose spells"):
                TextPart(text=f"Run {spelling}")


def test_deserializing_a_harness_refuses_an_invocation_in_guidance() -> None:
    """Reading a harness back is a declaration too, and refuses the same words."""
    source = portable_harness().model_dump()
    source["guidance"]["parts"].append({"type": "text", "text": "Run $lup:merge"})

    with pytest.raises(ValueError, match="portable prose spells"):
        Harness.model_validate(source)


def test_artifact_paths_reject_backslash_traversal() -> None:
    with pytest.raises(ValueError, match="beneath its root"):
        Artifact(path=Path("..\\secret"), content="x", semantic_id="unsafe")


def test_both_native_trees_compile_deterministically() -> None:
    harness = portable_harness()
    claude = compile_claude(harness)
    codex = compile_codex(harness)

    assert claude == compile_claude(harness)
    assert codex == compile_codex(harness)
    declared_skills = len(harness.plugins[0].committed_skills())
    assert (
        len([item for item in claude.artifacts if "/commands/" in item.path.as_posix()])
        == declared_skills
    )
    assert (
        len([item for item in codex.artifacts if item.path.name == "SKILL.md"])
        == declared_skills
    )
    assert Path(".codex/plugins/lup/.codex-plugin/plugin.json") in {
        item.path for item in codex.artifacts
    }
    assert Path(".codex/rules/lup.rules") in {item.path for item in codex.artifacts}
    assert Path("AGENTS.md") in {item.path for item in codex.artifacts}

    def shipped_kernel(artifacts: list[Artifact]) -> list[str]:
        """Render every shipped kernel module as one comparable block."""
        return sorted(
            f"{item.path.name}\n{item.content}"
            for item in artifacts
            if "hooks/runtime/kernel/" in item.path.as_posix()
        )

    canonical = sorted(
        f"{module.name}\n{module.source}" for module in policy_kernel_modules()
    )
    assert shipped_kernel(claude.artifacts) == shipped_kernel(codex.artifacts)
    assert shipped_kernel(claude.artifacts) == canonical


def test_codex_compiles_prefix_safe_shell_allows_to_native_rules() -> None:
    artifacts = {
        artifact.path: artifact.content
        for artifact in compile_codex(portable_harness()).artifacts
    }
    rules = artifacts[Path(".codex/rules/lup.rules")]

    assert 'pattern = ["uv", "run", "lup-devtools", "harness"]' in rules
    # The verbs that only read the tree are not approved for an escape they
    # never needed: the approval mirrors the boundary's own exclusion rather
    # than the rule that names the whole toolchain.
    assert 'pattern = ["uv", "run", "lup-devtools"],' not in rules
    assert 'pattern = ["git", "status"]' in rules
    assert 'pattern = ["gh", "pr", "view"]' in rules
    assert 'pattern = ["uv"]' not in rules
    assert 'pattern = ["uv", "run", "pytest"]' not in rules
    assert 'pattern = ["env"]' not in rules
    assert 'pattern = ["sort"]' not in rules
    assert 'pattern = ["git", "push"]' not in rules


def test_every_commentable_generated_file_carries_the_one_banner_form() -> None:
    harness = portable_harness()
    root = Path.cwd()
    trees = [
        claude_target(root).recipe.desired,
        codex_target(root).recipe.desired,
        ArtifactTree(artifacts=[rule_reference_artifact()]),
    ]
    bannered = [
        (artifact, artifact.banner)
        for tree in trees
        for artifact in tree.artifacts
        if isinstance(artifact.banner, GeneratedBanner)
    ]

    assert {Path("docs/rules.md"), Path("docs/permissions.md")} <= {
        artifact.path for artifact, _ in bannered
    }
    for artifact, banner in bannered:
        assert banner.opens(artifact.path, artifact.content)
        assert f"Generated from {banner.source} by " in artifact.content
        assert f"`{banner.command}`" in artifact.content
    # Every artifact says where it came from; only whether it can *print* that
    # varies. A format with no comment declares the exemption saying so rather
    # than declaring nothing, which is what keeps the provenance readable to a
    # walk even where it is unreadable in the file.
    for tree in trees:
        for artifact in tree.artifacts:
            spelled = ARTIFACT_COMMENT_ROUTER.route_for(artifact.path)
            assert artifact.banner is not None
            assert artifact.banner.attribution()
            comment_free = artifact.banner == COMMENT_FREE.compiled_from(
                artifact.banner.attribution()
            )
            assert comment_free == (spelled is None)
    assert harness == portable_harness()


def test_a_generated_file_that_states_no_provenance_fails_the_check() -> None:
    silent = Artifact(
        path=Path("docs/invented.md"), content="# Invented\n", semantic_id="docs.new"
    )

    with pytest.raises(ArtifactValidationError, match="declares no generated-from"):
        validated_tree([silent])


def test_a_banner_the_content_does_not_open_with_is_rejected() -> None:
    banner = GeneratedBanner(source="lup_template.invented", command="uv run invent")

    with pytest.raises(ValueError, match="does not open with the banner"):
        Artifact(
            path=Path("docs/invented.md"),
            content="# Invented\n",
            semantic_id="docs.new",
            banner=banner,
        )


def test_repository_generated_harness_is_drift_clean() -> None:
    reports = [
        inspect_generation(claude_target(Path.cwd()).recipe),
        inspect_generation(codex_target(Path.cwd()).recipe),
    ]

    assert all(report.clean for report in reports)


def test_generic_generation_accepts_an_injected_third_recipe(tmp_path: Path) -> None:
    desired = ArtifactTree(
        artifacts=[
            Artifact(
                path=Path(".third/plugin.txt"),
                content="third-provider\n",
                semantic_id="third.plugin",
            )
        ]
    )
    recipe = GenerationRecipe(
        label="third-provider",
        root=tmp_path,
        source=portable_harness(),
        desired=desired,
        manifest_path=tmp_path / ".third" / ".lup-ownership.json",
        prior=None,
        reader=current_reader(None, desired, sensitive_local_only=[]),
        target_requirements=["third-cli>=1"],
    )

    report = inspect_generation(recipe)

    assert report.target == "third-provider"
    assert [write.artifact.path for write in report.proposal.writes] == [
        Path(".third/plugin.txt")
    ]
    generated = generate(recipe)
    assert generated.target == "third-provider"
    assert (tmp_path / ".third" / "plugin.txt").read_text(encoding="utf-8") == (
        "third-provider\n"
    )
    assert recipe.manifest_path.is_file()


def third_recipe(tmp_path: Path, prior: OwnershipManifest | None) -> GenerationRecipe:
    """One injected recipe over a settled tree, reading *prior* as its proof."""
    desired = ArtifactTree(
        artifacts=[
            Artifact(
                path=Path(".third/plugin.txt"),
                content="third-provider\n",
                semantic_id="third.plugin",
            )
        ]
    )
    return GenerationRecipe(
        label="third-provider",
        root=tmp_path,
        source=portable_harness(),
        desired=desired,
        manifest_path=tmp_path / ".third" / ".lup-ownership.json",
        prior=prior,
        reader=current_reader(prior, desired, sensitive_local_only=[]),
        target_requirements=["third-cli>=1"],
    )


def test_a_regenerated_tree_reports_its_proof_current(tmp_path: Path) -> None:
    generate(third_recipe(tmp_path, None))
    settled = manifest_of(third_recipe(tmp_path, None))

    report = inspect_generation(third_recipe(tmp_path, settled))

    assert report.manifest_current
    assert report.clean


def test_a_proof_from_another_tree_is_stale_though_the_artifacts_match(
    tmp_path: Path,
) -> None:
    """The merge case: the driver keeps one side's proof, the artifacts merge clean.

    Reading only the artifacts calls that settled, and the regeneration the
    conflict existed to force becomes the step nothing asks for.
    """
    generate(third_recipe(tmp_path, None))
    settled = manifest_of(third_recipe(tmp_path, None))
    kept = settled.model_copy(update={"source_digest": "0" * 64})

    report = inspect_generation(third_recipe(tmp_path, kept))

    assert not report.proposal.writes, "the artifacts themselves are current"
    assert not report.manifest_current
    assert not report.clean


def test_reconciliation_preserves_local_and_sensitive_collisions(
    tmp_path: Path,
) -> None:
    local_content = "local\n"
    secret_content = "credential\n"
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("local.txt"),
                content=local_content,
                category="local_only",
                sha256=content_digest(local_content),
            ),
            CurrentArtifact(
                path=Path("secret.txt"),
                content="",
                category="sensitive_local_only",
                sha256=content_digest(secret_content),
            ),
        ],
    )
    desired = ArtifactTree(
        artifacts=[
            Artifact(path=Path("local.txt"), content="generated", semantic_id="local"),
            Artifact(
                path=Path("secret.txt"), content="generated", semantic_id="secret"
            ),
        ]
    )

    proposal = DeterministicReconciler().propose(current, desired)

    assert len(proposal.conflicts) == 2
    assert proposal.conflicts[1].sensitive
    with pytest.raises(MaterializationConflictError):
        AtomicMaterializer().apply(proposal)


def test_materialization_rejects_stale_base(tmp_path: Path) -> None:
    path = tmp_path / "owned.txt"
    path.write_text("old\n", encoding="utf-8")
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("owned.txt"),
                content="old\n",
                category="generated",
                sha256=content_digest("old\n"),
            )
        ],
    )
    desired = ArtifactTree(
        artifacts=[Artifact(path=Path("owned.txt"), content="new", semantic_id="owned")]
    )
    proposal = DeterministicReconciler().propose(current, desired)
    path.write_text("changed locally\n", encoding="utf-8")

    with pytest.raises(MaterializationConflictError, match="stale base"):
        AtomicMaterializer().apply(proposal)


def test_a_refused_write_names_the_boundary_and_drops_its_staging(
    tmp_path: Path,
) -> None:
    """A runtime protects its own configuration by mounting it, not by mode.

    So a sandboxed session replacing one of those paths is refused with a
    busy device — an errno about hardware, which sends a reader looking at
    the disk instead of at the boundary that actually decided.
    """
    staged = tmp_path / ".settings.json.abc123.tmp"
    staged.write_text("staged\n", encoding="utf-8")
    error = OSError(errno.EBUSY, "Device or resource busy", str(staged))
    error.filename2 = str(tmp_path / "settings.json")

    discard_staged_write(error)
    refusal = str(refused_write(error))

    assert not staged.exists()
    assert "settings.json" in refusal and ".tmp" not in refusal
    assert "sandbox" in refusal


def test_materialization_rejects_symlink_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    current = CurrentTree(root=root, artifacts=[])
    desired = ArtifactTree(
        artifacts=[
            Artifact(
                path=Path("link/escaped.txt"),
                content="unsafe",
                semantic_id="escape",
            )
        ]
    )
    proposal = DeterministicReconciler().propose(current, desired)

    with pytest.raises(MaterializationConflictError, match="symlink"):
        AtomicMaterializer().apply(proposal)
    assert not (outside / "escaped.txt").exists()


def test_exact_generated_content_can_acquire_first_ownership(tmp_path: Path) -> None:
    content = "generated\n"
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("new.txt"),
                content=content,
                category="unknown_conflict",
                sha256=content_digest(content),
            )
        ],
    )
    desired = ArtifactTree(
        artifacts=[Artifact(path=Path("new.txt"), content=content, semantic_id="new")]
    )

    proposal = DeterministicReconciler().propose(current, desired)

    assert proposal.conflicts == []
    assert proposal.writes == []


def test_interrupted_exact_write_can_reacquire_prior_ownership(tmp_path: Path) -> None:
    content = "new generated content\n"
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("owned.txt"),
                content=content,
                category="backpropagation_candidate",
                sha256=content_digest(content),
            )
        ],
    )
    desired = ArtifactTree(
        artifacts=[
            Artifact(path=Path("owned.txt"), content=content, semantic_id="owned")
        ]
    )

    proposal = DeterministicReconciler().propose(current, desired)

    assert proposal.conflicts == []
    assert proposal.writes == []


def test_an_owned_path_the_generator_disagrees_with_is_regenerated(
    tmp_path: Path,
) -> None:
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("owned.txt"),
                content="what a merge left behind\n",
                category="backpropagation_candidate",
                sha256=content_digest("what a merge left behind\n"),
            )
        ],
    )
    desired = ArtifactTree(
        artifacts=[
            Artifact(
                path=Path("owned.txt"),
                content="what the generator wants\n",
                semantic_id="owned",
            )
        ]
    )

    proposal = DeterministicReconciler().propose(current, desired)

    # A resolver join merges both plugin trees and their ownership proof, so
    # the recorded digest ends up describing neither parent. Read as an edit
    # nobody made, that refused generation outright and left no recovery a
    # worker could reach.
    assert proposal.conflicts == []
    assert [write.artifact.path.as_posix() for write in proposal.writes] == [
        "owned.txt"
    ]


def test_native_override_does_not_silently_reown_backpropagation(
    tmp_path: Path,
) -> None:
    content = "locally changed native override\n"
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("owned.txt"),
                content=content,
                category="backpropagation_candidate",
                sha256=content_digest(content),
            )
        ],
    )
    desired = ArtifactTree(
        artifacts=[
            Artifact(path=Path("owned.txt"), content=content, semantic_id="owned")
        ]
    )

    proposal = DeterministicReconciler(adopt_exact_backpropagation=False).propose(
        current, desired
    )

    assert [conflict.category for conflict in proposal.conflicts] == [
        "backpropagation_candidate"
    ]


def test_codex_cache_digest_requires_an_exact_separate_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    home = tmp_path / "home"
    # codex caches an installed plugin under its manifest version segment.
    installed = home / "plugins" / "cache" / "lup-template-repository" / "lup" / "9.9.9"
    source.mkdir()
    installed.mkdir(parents=True)
    for root in (source, installed):
        manifest_dir = root / ".codex-plugin"
        manifest_dir.mkdir()
        (manifest_dir / "plugin.json").write_text(
            '{"name": "lup", "version": "9.9.9"}\n', encoding="utf-8"
        )
        (root / "plugin.txt").write_text("same\n", encoding="utf-8")
    config = PluginCacheConfig(
        codex_home=home, marketplace="lup-template-repository", version="9.9.9"
    )

    assert plugin_cache_evidence(source, config).ready
    (installed / "plugin.txt").write_text("stale\n", encoding="utf-8")
    assert not plugin_cache_evidence(source, config).ready


def test_codex_cache_digest_ignores_python_bytecode(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    (root / "__pycache__").mkdir(parents=True)
    (root / "policy.py").write_text("pass\n", encoding="utf-8")
    before = digest_directory(root, Path.read_bytes)

    (root / "__pycache__" / "policy.cpython-314.pyc").write_bytes(b"cache")

    assert digest_directory(root, Path.read_bytes) == before


def test_binary_managed_file_becomes_typed_unknown_conflict(tmp_path: Path) -> None:
    path = Path("managed.md")
    (tmp_path / path).write_bytes(b"\xff\xfe")

    tree = FilesystemCurrentTreeReader(None, managed_paths=[path]).read(tmp_path)

    assert tree.artifacts[0].category == "unknown_conflict"
    assert tree.artifacts[0].content == ""


def test_executable_mode_drift_is_not_treated_as_generated(tmp_path: Path) -> None:
    path = Path("hook.py")
    target = tmp_path / path
    target.write_text("pass\n", encoding="utf-8")
    target.chmod(0o644)
    source = portable_harness()
    manifest = build_manifest(
        source,
        ArtifactTree(
            artifacts=[
                Artifact(
                    path=path,
                    content="pass\n",
                    semantic_id="hook",
                    executable=True,
                )
            ]
        ),
        generator_version="test",
        target_requirements=[],
    )

    tree = FilesystemCurrentTreeReader(manifest, managed_paths=[path]).read(tmp_path)

    assert tree.artifacts[0].category == "generated"
    proposal = DeterministicReconciler().propose(
        tree,
        ArtifactTree(
            artifacts=[
                Artifact(
                    path=path,
                    content="pass\n",
                    semantic_id="hook",
                    executable=True,
                )
            ]
        ),
    )
    assert proposal.writes[0].artifact.executable


def codex_hook_result(
    body: JsonObject, sandboxed: bool, plugin_data: Path | None = None
) -> sh.RunningCommand:
    environment = {**os.environ, "LUP_SANDBOX_ACTIVE": "1" if sandboxed else "0"}
    if plugin_data is not None:
        environment["PLUGIN_DATA"] = str(plugin_data)
    return sh.Command(
        str(Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve())
    )(_in=json.dumps(body), _env=environment, _ok_code=[0, 2], _return_cmd=True)


def test_generated_codex_hook_records_metadata_only_evidence(tmp_path: Path) -> None:
    body: JsonObject = {}
    body["hook_event_name"] = "PreToolUse"
    body["session_id"] = "session-one"
    body["turn_id"] = "turn-one"
    body["tool_use_id"] = "tool-one"
    body["tool_name"] = "Bash"
    body["tool_input"] = {"command": "git status", "secret": "do-not-record"}
    result = codex_hook_result(body, sandboxed=True, plugin_data=tmp_path)
    evidence = tmp_path / "hook-events.jsonl"
    assert result.exit_code == 0
    assert evidence.is_file()
    assert "do-not-record" not in evidence.read_text(encoding="utf-8")
    records = [json.loads(line) for line in evidence.read_text().splitlines()]
    timestamps = [record.pop("timestamp") for record in records]
    assert all(timestamp.endswith("+00:00") for timestamp in timestamps)
    common = {"schema_version": 1, "event_name": "PreToolUse"}
    common.update(session_id="session-one", turn_id="turn-one")
    common.update(tool_use_id="tool-one", tool_name="Bash")
    assert records == [
        {**common, "phase": "started"},
        {**common, "phase": "completed", "outcome": "allow"},
    ]


def test_generated_codex_hook_records_dispatch_failure(tmp_path: Path) -> None:
    body: JsonObject = {
        "hook_event_name": "PreToolUse",
        "session_id": "session-two",
        "tool_name": "Bash",
    }
    result = codex_hook_result(body, sandboxed=True, plugin_data=tmp_path)
    evidence = tmp_path / "hook-events.jsonl"
    assert result.exit_code == 2
    records = [json.loads(line) for line in evidence.read_text().splitlines()]
    timestamps = [record.pop("timestamp") for record in records]
    assert all(timestamp.endswith("+00:00") for timestamp in timestamps)
    common = {"schema_version": 1, "event_name": "PreToolUse"}
    common.update(session_id="session-two", tool_name="Bash")
    assert records[0] == {**common, "phase": "started"}
    failed = records[1]
    assert failed.pop("detail") == "KeyError: 'tool_input'"
    assert failed == {**common, "phase": "failed", "outcome": "error"}


def test_generated_claude_hook_records_metadata_only_evidence(tmp_path: Path) -> None:
    body: JsonObject = {"hook_event_name": "PreToolUse"}
    body.update(session_id="session-one", turn_id="turn-one")
    body.update(tool_use_id="tool-one", tool_name="Bash")
    body["tool_input"] = {"command": "python -c 1", "secret": "do-not-record"}
    environment = {**os.environ, "CLAUDE_PLUGIN_DATA": str(tmp_path)}
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()
    result = sh.Command(str(script))(
        _in=json.dumps(body), _env=environment, _return_cmd=True
    )
    assert isinstance(result, sh.RunningCommand)
    output = ClaudeHookOutput.model_validate_json(result.stdout)
    evidence = tmp_path / "hook-events.jsonl"
    assert output.hook_specific_output.permission_decision == "deny"
    assert "do-not-record" not in evidence.read_text(encoding="utf-8")
    records = [json.loads(line) for line in evidence.read_text().splitlines()]
    timestamps = [record.pop("timestamp") for record in records]
    assert all(timestamp.endswith("+00:00") for timestamp in timestamps)
    detail = records[1].pop("detail")
    assert "bare interpreter" in detail
    common = {"schema_version": 1, "event_name": "PreToolUse"}
    common.update(session_id="session-one", turn_id="turn-one")
    common.update(tool_use_id="tool-one", tool_name="Bash")
    assert records == [
        {**common, "phase": "started"},
        {**common, "phase": "completed", "outcome": "deny"},
    ]


def test_generated_hooks_record_a_fetch_by_origin_and_nothing_further(
    tmp_path: Path,
) -> None:
    """Both journals name the origin a fetch reached for, and stop there.

    A record that carries no part of the input reads as "a URL was outside
    the declared scopes" with no way to tell which URL, so the question the
    journal exists to answer -- what did this session try to reach -- is
    settled by inference. The origin is the half the scope table is written
    against; the path and the query are where a token or a document id ride,
    and they stay out, as does everything else in the call.

    The origin is outside every scope, which this project hands to the
    runtime's own permission system: Claude's hook says nothing, Codex's
    exits clean on both judging events -- a deferral is never rendered as an
    allow -- and no question is parked for a reviewer.
    """
    url = "https://docs.example.test:8443/private/page?token=do-not-record"
    body: JsonObject = {"hook_event_name": "PreToolUse", "cwd": str(tmp_path)}
    body.update(session_id="session-four", tool_use_id="tool-four")
    claude_data, codex_data = tmp_path / "claude", tmp_path / "codex"
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()
    # lup: ignore[os-environ] — test shell
    environment = {**os.environ, "CLAUDE_PLUGIN_DATA": str(claude_data)}
    claude = sh.Command(str(script))(
        _in=json.dumps({**body, "tool_name": "WebFetch", "tool_input": {"url": url}}),
        _env=environment,
        _return_cmd=True,
    )
    assert isinstance(claude, sh.RunningCommand)
    assert "hookSpecificOutput" not in json.loads(claude.stdout)
    fetch = {**body, "tool_name": "web_fetch", "tool_input": {"url": url}}
    codex = codex_hook_result(fetch, sandboxed=True, plugin_data=codex_data)
    assert codex.exit_code == 0
    assert not codex.stdout.strip()
    requested = codex_hook_result(
        {**fetch, "hook_event_name": "PermissionRequest"}, sandboxed=True
    )
    assert requested.exit_code == 0
    assert not requested.stdout.strip()
    assert not QuestionRelay(tmp_path / ".lup/questions.jsonl").pending()

    for data_root in (claude_data, codex_data):
        written = (data_root / "hook-events.jsonl").read_text(encoding="utf-8")
        assert "do-not-record" not in written
        assert "/private/page" not in written
        records = [json.loads(line) for line in written.splitlines()]
        assert [record["phase"] for record in records] == ["started", "completed"]
        assert records[-1]["outcome"] == "defer"
        for record in records:
            assert record["fetch_origin"] == "https://docs.example.test:8443"


def test_a_journal_names_no_origin_for_a_tool_that_fetches_nothing(
    tmp_path: Path,
) -> None:
    """The omission stands everywhere the fetch surface does not.

    A shell command names no origin and gets no key for one: what widened is
    the fetch decision alone, rather than the journal's appetite for input.
    """
    body: JsonObject = {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
    body["tool_input"] = {"command": "git status", "url": ["not-a-url"]}
    result = codex_hook_result(body, sandboxed=True, plugin_data=tmp_path)
    records = [
        json.loads(line)
        for line in (tmp_path / "hook-events.jsonl").read_text().splitlines()
    ]

    assert result.exit_code == 0
    assert all("fetch_origin" not in record for record in records)


def test_generated_codex_hook_fails_closed_for_inline_code() -> None:
    script = Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()
    result = sh.Command(str(script))(
        _in='{"tool_name":"Bash","tool_input":{"command":"python -c 1"}}',
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    assert result.exit_code == 2
    assert b"bare interpreter" in result.stderr


def test_generated_codex_pretool_accepts_a_safe_requested_escape() -> None:
    body: JsonObject = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {
            "command": "uv run lup-devtools resolve intake",
            "sandbox_permissions": "require_escalated",
        },
    }
    assert codex_hook_result(body, sandboxed=True).exit_code == 0


def test_generated_codex_pretool_refuses_an_escape_no_declaration_covers() -> None:
    """The agent placing its own call is not a request Lup answers.

    Asking for the launcher's host is a marker a reviewer sees. A native escape
    on a command the boundary confines and no rule places elsewhere is the
    agent choosing where its own operation runs, so the hook says which
    placement policy reached and what to remove.
    """
    body: JsonObject = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {
            "command": "uv run pytest tests/unit",
            "sandbox_permissions": "require_escalated",
        },
    }
    result = codex_hook_result(body, sandboxed=True)

    assert result.exit_code == 2
    assert b"remove sandbox_permissions" in result.stderr


def test_generated_codex_pretool_accepts_a_safe_automatic_escape() -> None:
    body: JsonObject = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "uv run lup-devtools resolve intake"},
    }
    assert codex_hook_result(body, sandboxed=True).exit_code == 0


def test_generated_codex_pretool_defers_an_escape_only_native_codex_can_see() -> None:
    body: JsonObject = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git diff --check"},
    }
    assert codex_hook_result(body, sandboxed=True).exit_code == 0


def test_generated_codex_pretool_refuses_an_ambient_escape() -> None:
    body: JsonObject = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {
            "command": "cat README.md",
            "sandbox_permissions": "require_escalated",
        },
    }
    assert codex_hook_result(body, sandboxed=True).exit_code == 2


@pytest.mark.parametrize(
    "command",
    [
        "UV_CACHE_DIR=/tmp/lup-uv-cache uv run lup-devtools resolve --adapter codex",
        "ENV_VAR=constant git status",
    ],
)
def test_generated_codex_permission_request_allows_safe_assignment(
    command: str,
) -> None:
    script = Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()
    body = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    result = sh.Command(str(script))(
        _in=json.dumps(body),
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    output = CodexPermissionOutput.model_validate_json(result.stdout)
    assert result.exit_code == 0
    assert output.hook_specific_output.decision.behavior == "allow"


@pytest.mark.parametrize(
    "command,reason",
    [
        (
            "# lup: escalate[decision]: required diagnostic\npython -c 1",
            "bare interpreter",
        ),
        ("PATH=/tmp git status", "security-sensitive variable PATH"),
        # An invalid identifier is not an assignment, so the shell would run a
        # program literally named `1BAD=constant` — which the vocabulary lists
        # nowhere. Its native permission event must deny until explicitly reviewed.
        ("1BAD=constant git status", "1BAD=constant"),
    ],
)
def test_generated_codex_permission_request_preserves_assignment_guards(
    tmp_path: Path, command: str, reason: str
) -> None:
    script = Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()
    body = {
        "hook_event_name": "PermissionRequest",
        "session_id": "assignment-requester",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    result = sh.Command(str(script))(
        _in=json.dumps(body),
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    assert result.exit_code == 0
    decision = CodexPermissionOutput.model_validate_json(
        result.stdout
    ).hook_specific_output.decision
    assert decision.behavior == "deny"
    assert reason in decision.message
    (question,) = QuestionRelay(tmp_path / ".lup/questions.jsonl").pending()
    assert question.id in decision.message
    assert question.operation.payload == {"command": command}


def test_generated_codex_pretool_never_treats_pending_requests_as_approval(
    tmp_path: Path,
) -> None:
    command = "gh pr merge 180 --admin"
    common: JsonObject = {
        "session_id": "session-one",
        "turn_id": "turn-one",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
    }
    permission: JsonObject = {
        **common,
        "hook_event_name": "PermissionRequest",
        "tool_input": {
            "command": command,
            "sandbox_permissions": "require_escalated",
        },
    }
    requested = codex_hook_result(permission, sandboxed=True, plugin_data=tmp_path)
    assert requested.exit_code == 0
    decision = CodexPermissionOutput.model_validate_json(
        requested.stdout
    ).hook_specific_output.decision
    assert decision.behavior == "deny"
    store = QuestionRelay(tmp_path / ".lup/questions.jsonl")
    (question,) = store.pending()
    assert question.id in decision.message
    assert question.operation.payload == permission["tool_input"]
    requested_again = codex_hook_result(permission, True, tmp_path)
    assert requested_again.exit_code == 0
    repeated = CodexPermissionOutput.model_validate_json(
        requested_again.stdout
    ).hook_specific_output.decision
    assert repeated.behavior == "deny"
    assert question.id in repeated.message
    assert store.pending() == [question]

    pretool: JsonObject = {
        **common,
        "hook_event_name": "PreToolUse",
        "tool_use_id": "tool-one",
        "tool_input": {"command": command},
    }
    mismatched: JsonObject = {
        **pretool,
        "tool_input": {"command": command.replace("180", "181")},
    }
    for proposed in (mismatched, pretool, pretool, pretool):
        refused = codex_hook_result(proposed, True, tmp_path)
        assert refused.exit_code == 0
        output = json.loads(refused.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "Lup review " in output["systemMessage"]


def test_generated_codex_permission_request_denies_unapproved_code() -> None:
    script = Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()
    body = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "python -c 1"},
    }
    result = sh.Command(str(script))(
        _in=json.dumps(body),
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    assert result.exit_code == 0
    decision = CodexPermissionOutput.model_validate_json(
        result.stdout
    ).hook_specific_output.decision
    assert decision.behavior == "deny"
    assert "bare interpreter" in decision.message


def test_generated_codex_hook_refuses_the_declared_calls() -> None:
    """The refusal table is consulted on both runtimes, not only on Claude.

    Everything the refusal is made of is portable — the field on ``HookSet``,
    the kernel module both trees carry, the rows this renderer emits — so a
    tree that shipped all of it and never asked would read as a refusal in
    force while the call went through. These two names are Claude's own
    spellings and match nothing Codex offers; what is pinned is that the
    mechanism reaches this dispatcher, for whatever an adopter refuses here.
    """
    hook = sh.Command(str(Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()))

    def run(name: str, payload: JsonObject) -> sh.RunningCommand:
        body = json.dumps({"tool_name": name, "tool_input": payload})
        result = hook(_in=body, _ok_code=[0, 2], _return_cmd=True)
        assert isinstance(result, sh.RunningCommand)
        return result

    refused = run("Artifact", {"content": "a page"})
    assert refused.exit_code == 2
    assert b"lup-devtools dev report" in refused.stderr

    narrowed = run("Skill", {"skill": "artifact-design"})
    assert narrowed.exit_code == 2

    other = run("Skill", {"skill": "lup:commit"})
    assert other.exit_code == 0
    assert other.stdout == b""


def test_generated_codex_hook_allows_managed_skill_scripts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    script = Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()
    helper = codex_home / "skills/.system/openai-docs/scripts/fetch-codex-manual.mjs"
    body = {"tool_name": "Bash", "tool_input": {"command": f"node {helper}"}}
    allowed = sh.Command(str(script))(
        _in=json.dumps(body),
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(allowed, sh.RunningCommand)
    assert allowed.exit_code == 0

    body["tool_input"]["command"] = "python3 /tmp/untrusted-script.py"
    denied = sh.Command(str(script))(
        _in=json.dumps(body),
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(denied, sh.RunningCommand)
    assert denied.exit_code == 2


def test_generated_claude_hook_allows_managed_skill_scripts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "claude-profile"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()

    def decision(command: str) -> str:
        body = {"tool_name": "Bash", "tool_input": {"command": command}}
        result = sh.Command(str(script))(_in=json.dumps(body), _return_cmd=True)
        assert isinstance(result, sh.RunningCommand)
        output = ClaudeHookOutput.model_validate_json(result.stdout)
        return output.hook_specific_output.permission_decision

    helper = config_dir / "plugins/cache/official/tool/scripts/validate.mjs"
    assert decision(f"node {helper}") == "allow"
    assert decision(f"node {config_dir}/skills/tool/scripts/report.mjs") == "allow"
    assert decision(f"python3 {config_dir}/skills/tool/scripts/report.py") == "allow"
    assert decision("python3 /tmp/untrusted-script.py") == "deny"
    assert decision("node -e 'process.exit()'") == "deny"


def test_generated_hooks_find_uv_dependency_routes_past_global_flags(
    tmp_path: Path,
) -> None:
    """Both shipped hooks walk to uv's verb past its global options.

    A global before the verb, or between its words, is the spelling that
    would slip a route past a reader matching by position. The Claude hook
    asks and the Codex hook, which has no ask to render, parks the question;
    the listing verbs read on both.
    """
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()

    def claude(command: str) -> str:
        body = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(tmp_path),
        }
        result = sh.Command(str(script))(_in=json.dumps(body), _return_cmd=True)
        assert isinstance(result, sh.RunningCommand)
        return ClaudeHookOutput.model_validate_json(result.stdout).effect()

    def codex(command: str) -> str:
        body: JsonObject = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(tmp_path),
        }
        result = codex_hook_result(body, sandboxed=True)
        if not result.stdout.strip():
            return "allow" if result.exit_code == 0 else "deny"
        return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]

    for command in (
        "uv -q pip install y",
        "uv pip --quiet install x",
        "uv --cache-dir /tmp/c tool install ruff",
        "uv --offline publish",
        "uvx ruff",
    ):
        assert claude(command) == "ask", command
        assert codex(command) == "deny", command
    for command in ("uv -q pip list", "uv --cache-dir /tmp/c tool list"):
        assert claude(command) == "allow", command
        assert codex(command) == "allow", command


def test_generated_claude_hook_refuses_the_declared_calls(tmp_path: Path) -> None:
    """The refusal this repository declares, as the shipped hook enforces it.

    Routing is half the mechanism and the declared rows are the other half,
    so this goes through the compiled script rather than the kernel beneath
    it: a row the hook is never handed refuses nothing, and no unit below
    this level would notice.
    """
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()

    def decision(name: str, payload: JsonObject) -> ClaudeHookDecision:
        body = {
            "tool_name": name,
            "tool_input": payload,
            "cwd": str(tmp_path),
            "session_id": "artifact-requester",
        }
        result = sh.Command(str(script))(_in=json.dumps(body), _return_cmd=True)
        assert isinstance(result, sh.RunningCommand)
        return ClaudeHookOutput.model_validate_json(result.stdout).hook_specific_output

    refused = decision("Artifact", {"content": "a page"})
    assert refused.permission_decision == "deny"
    assert "lup-devtools dev report" in refused.permission_decision_reason

    narrowed = decision("Skill", {"skill": "artifact-design"})
    assert narrowed.permission_decision == "deny"

    proposal: JsonObject = {
        "content": "# lup: escalate[decision]: the user asked for a page\npage"
    }
    # The marker exists to turn a refusal into the question its caller asked
    # for, and where no dashboard is held the question is put where the
    # caller's reader already is.
    escalated = decision("Artifact", proposal)
    assert escalated.permission_decision == "ask"
    assert "the user asked for a page" in escalated.permission_decision_reason
    assert QuestionRelay(tmp_path / ".lup/questions.jsonl").pending() == []


def test_generated_claude_hook_leaves_every_other_skill_to_the_runtime() -> None:
    """Routing `Skill` must not put every skill invocation to a human."""
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()
    result = sh.Command(str(script))(
        _in='{"tool_name":"Skill","tool_input":{"skill":"lup:commit"}}',
        _return_cmd=True,
    )

    assert isinstance(result, sh.RunningCommand)
    assert json.loads(result.stdout) == {}


def test_generated_claude_hook_executes_the_canonical_kernel() -> None:
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()
    result = sh.Command(str(script))(
        _in='{"tool_name":"Bash","tool_input":{"command":"python -c 1"}}',
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    output = ClaudeHookOutput.model_validate_json(result.stdout)
    assert output.hook_specific_output.permission_decision == "deny"
    assert "bare interpreter" in output.hook_specific_output.permission_decision_reason


@pytest.mark.parametrize("target", sorted(SHIPPED_DISPATCHERS))
def test_generated_dispatcher_resolves_its_runtime_from_anywhere(
    target: str, tmp_path: Path
) -> None:
    """A plugin host spawns the hook as a bare script and arranges nothing.

    Isolated mode drops the working directory, the script's own directory, and
    every ``PYTHON*`` variable from the search path, and the environment is
    empty, so reaching ``kernel.*`` and ``policy_data`` at all proves the
    script finds its runtime from its own location.
    """
    result = sh.Command(sys.executable)(
        "-I",
        str(SHIPPED_DISPATCHERS[target].script.resolve()),
        _in='{"tool_name":"Bash","tool_input":{"command":"python -c 1"}}',
        _cwd=str(tmp_path),
        _env={},
        _ok_code=[0, 2],
        _return_cmd=True,
    )

    assert isinstance(result, sh.RunningCommand)
    match target:
        case "claude":
            decision = ClaudeHookOutput.model_validate_json(
                result.stdout
            ).hook_specific_output
            assert decision.permission_decision == "deny"
            reason = decision.permission_decision_reason
        case _:
            assert result.exit_code == 2
            reason = result.stderr.decode()
    assert "bare interpreter" in reason


def test_static_checking_reaches_every_shipped_dispatcher() -> None:
    """A dispatcher is the one artifact whose breakage is silent.

    A plugin host runs these scripts, not the workspace, so an unresolved
    import or a mistyped argument surfaces as a permission decision that never
    happens — in a session that only sees the tool go through. Every scope
    that could exempt one is asserted here rather than trusted.
    """
    declared = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    config = PyrightConfiguration.model_validate(declared["tool"]["pyright"])
    halves = [SHARED_DISPATCHER_HALF]

    for dispatcher in SHIPPED_DISPATCHERS.values():
        halves.append(dispatcher.asset)
        for source in (dispatcher.asset, dispatcher.script):
            assert dispatcher.runtime in [
                path
                for environment in config.execution_environments
                if source.is_relative_to(environment.root)
                for path in environment.extra_paths
            ]
        assert SHARED_DISPATCHER_HALF.parent in [
            path
            for environment in config.execution_environments
            if dispatcher.asset.is_relative_to(environment.root)
            for path in environment.extra_paths
        ]
    for source in [*halves, *[item.script for item in SHIPPED_DISPATCHERS.values()]]:
        assert any(source.is_relative_to(root) for root in config.include)
        assert not any(source.is_relative_to(root) for root in config.exclude)


def test_a_spliced_half_declares_nothing_splicing_would_leave_behind() -> None:
    """What ships must not depend on a name the script never receives.

    Splicing emits functions, so a constant beside them is read where it is
    written, passes every check in the workspace, and is absent only from the
    generated script — where the function reading it raises `NameError` on the
    first edit, inside a hook whose failure a session sees as a decision that
    never happened.
    """
    assert not [
        name
        for member in SPLICED_MEMBERS
        for name in source_half(SHARED_PACKAGE, member).stranded()
    ]


def test_a_stranded_name_is_refused_at_generation_rather_than_shipped() -> None:
    """The compiler is the only reader placed to catch it.

    A name declared beside the functions resolves everywhere it is read and
    nowhere it is used, so no type checker downstream is looking at both. This
    turns that into a construction error, which is what the rest of the
    compiler's proofs already are.
    """
    written = source_half(SHARED_PACKAGE, SHARED_MEMBER)
    beside = SourceHalf(
        module=written.module,
        text=f"DEFAULT_ENVIRONMENT = '.venv'\n{written.text}",
        tree=ast.parse(f"DEFAULT_ENVIRONMENT = '.venv'\n{written.text}"),
    )

    assert beside.stranded() == ["DEFAULT_ENVIRONMENT"]
    assert stranded_breaches(beside, source_half(SHARED_PACKAGE, DECISIONS_MEMBER)) == [
        f"{written.module} declares DEFAULT_ENVIRONMENT, which splicing leaves behind"
    ]


def test_both_dispatchers_are_compiled_from_one_shared_host_half() -> None:
    """The half neither runtime spells differently is written exactly once.

    Every function the two scripts genuinely share must be a function the
    shared half offers — anything else is the same code living in two places,
    which is how the halves drifted apart before they were compiled.
    """
    shared = [
        node.name
        for member in SPLICED_MEMBERS
        for node in source_half(SHARED_PACKAGE, member).functions()
    ]
    claude = compiled_functions(compile_dispatcher(CLAUDE_DISPATCHER))
    codex = compiled_functions(compile_dispatcher(CODEX_DISPATCHER))

    assert "sandbox_active" in shared and "existing_write_targets" in shared
    assert "granted_allowances" in shared and "declared_identity" in shared
    # What a lease currently holds has one reader as well as one document:
    # two readings of the same file is the same drift as two files.
    assert "document_allowances" in shared
    # Every kernel call site is shared, which is what stops one runtime from
    # passing a fact the other has quietly stopped passing.
    assert "bash_decision" in shared and "edit_decision" in shared
    identical = [
        name
        for name in claude
        if name in codex
        and ast.dump(ast.parse(claude[name])) == ast.dump(ast.parse(codex[name]))
    ]
    assert sorted(identical) == sorted(shared)
    assert all(claude[name] == codex[name] for name in shared)


@pytest.mark.parametrize("target", sorted(SHIPPED_DISPATCHERS))
def test_compiled_dispatcher_reaches_only_what_a_bare_script_resolves(
    target: str,
) -> None:
    """The compiled script keeps the hermeticity floor its runtime promises.

    ``host`` is deliberately absent: the shared half is compiled in rather
    than shipped beside the script, so there is no second runtime module to
    keep in step with the one this dispatcher was type-checked against.
    """
    dispatcher = SHIPPED_DISPATCHERS[target]
    script = compile_dispatcher(dispatcher.declaration)
    modules = [
        item.module
        for item in SourceHalf(
            module=target, text=script, tree=ast.parse(script)
        ).imports()
    ]

    assert modules
    assert all(resolvable(module, dispatcher.declaration) for module in modules)
    assert SHARED_MEMBER not in modules
    assert not any(module == "lup" or module.startswith("lup.") for module in modules)
    assert script == dispatcher.script.read_text(encoding="utf-8")


@pytest.mark.parametrize("target", sorted(SHIPPED_DISPATCHERS))
def test_compiled_dispatcher_is_already_formatted(target: str) -> None:
    """What the compiler emits has to be formatted, not merely correct.

    The generated tree is checked by the same formatter as everything else, so
    a compiler that splices source into a shape the formatter would rewrite
    fails a gate nothing near the compiler runs. Asserting it here is what
    couples the two: emitting an unformatted script is a failing test rather
    than a red sweep somebody meets later, on a file they are told not to
    edit.
    """
    script = compile_dispatcher(SHIPPED_DISPATCHERS[target].declaration)
    formatted = str(
        sh.Command("ruff")("format", "-", "--stdin-filename", "policy.py", _in=script)
    )

    assert script == formatted


def test_compilation_refuses_a_dispatcher_that_breaks_its_declaration() -> None:
    """A dispatcher that cannot be proven is a session without a boundary.

    Every axis is read back out of the syntax rather than trusted, so a tool
    the plugin registers the hook for but the router never reaches stops
    generation instead of reaching a session as a decision that never happens.
    """
    unrouted = CLAUDE_DISPATCHER.model_copy(
        update={"routed_tools": [*CLAUDE_DISPATCHER.routed_tools, "NotebookEdit"]}
    )
    misread = CODEX_DISPATCHER.model_copy(update={"managed_root_env": "CODEX_ROOT"})
    unregistered = CODEX_DISPATCHER.model_copy(update={"hook_events": ["PreToolUse"]})

    with pytest.raises(ValueError, match="routes"):
        compile_dispatcher(unrouted)
    with pytest.raises(ValueError, match="never reads CODEX_ROOT"):
        compile_dispatcher(misread)
    with pytest.raises(ValueError, match="not registered for"):
        compile_dispatcher(unregistered)


AUTONOMY_PROBE = "".join(f"VALUE_{index} = {index}\n" for index in range(8))


@pytest.fixture
def autonomy_checkout(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    return tmp_path


def hook_environment(identity: str | None) -> EnvVars:
    """Build the hook's environment, never inheriting a declared identity.

    An operator with the identity exported would otherwise decide the
    outcome of every autonomy assertion below.
    """
    inherited = {
        key: value
        for key, value in os.environ.items()  # lup: ignore[os-environ] — test shell
        if key != "LUP_AGENT_IDENTITY"
    }
    return (
        inherited if identity is None else {**inherited, AGENT_IDENTITY_ENV: identity}
    )


def hook_decision(
    payload: JsonObject,
    root: Path,
    agent_type: str | None = None,
    identity: str | None = None,
) -> ClaudeHookDecision:
    """Run the installed Claude hook over one payload and identity pair."""
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()
    body = {
        **payload,
        "session_id": "autonomy-requester",
        "cwd": str(root),
        **({} if agent_type is None else {"agent_type": agent_type}),
    }
    result = sh.Command(str(script))(
        _in=json.dumps(body),
        _env=hook_environment(identity),
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    output = ClaudeHookOutput.model_validate_json(result.stdout)
    return output.hook_specific_output


def autonomy_decision(
    root: Path, agent_type: str | None = None, identity: str | None = None
) -> ClaudeHookDecision:
    return hook_decision(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(root / "packages/lup/src/lup/generated_probe.py"),
                "content": AUTONOMY_PROBE,
            },
        },
        root,
        agent_type=agent_type,
        identity=identity,
    )


def test_generated_claude_hook_maps_agent_type_to_editor_autonomy(
    autonomy_checkout: Path,
) -> None:
    tmp_path = autonomy_checkout
    for agent_type in (None, "implementer"):
        decision = autonomy_decision(tmp_path, agent_type=agent_type)
        assert decision.permission_decision == "ask"
        assert "written whole" in decision.permission_decision_reason
        assert QuestionRelay(tmp_path / ".lup/questions.jsonl").pending() == []
    for agent_type in ("resolver-worker", "lup:resolver-worker"):
        assert (
            autonomy_decision(tmp_path, agent_type=agent_type).permission_decision
            == "allow"
        )


def test_generated_claude_hook_maps_declared_identity_to_editor_autonomy(
    autonomy_checkout: Path,
) -> None:
    """The resolver's worker is a top-level session, so `agent_type` is empty.

    Autonomy has to reach it through the identity its launcher declared, or
    the mechanism is unreachable on the one path that needs it.
    """
    tmp_path = autonomy_checkout
    for identity in ("", "implementer"):
        decision = autonomy_decision(tmp_path, identity=identity)
        assert decision.permission_decision == "ask"
        assert "written whole" in decision.permission_decision_reason
        assert QuestionRelay(tmp_path / ".lup/questions.jsonl").pending() == []
    for identity in ("resolver-worker", "lup:resolver-worker"):
        assert (
            autonomy_decision(tmp_path, identity=identity).permission_decision
            == "allow"
        )


def test_generated_claude_hook_requires_review_for_human_owned_readme_edits(
    autonomy_checkout: Path,
) -> None:
    """Autonomy is a release of named rules, never a blanket bypass.

    Both channels are checked: an identity that grants autonomy through the
    environment must not buy anything the payload channel would not.
    """
    tmp_path = autonomy_checkout
    readme = tmp_path / "README.md"
    readme.write_text("# Operator-authored design\n")
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(readme),
            "content": "# Rewritten by an agent\n",
        },
    }
    for granted in (
        hook_decision(payload, tmp_path),
        hook_decision(payload, tmp_path, agent_type="resolver-worker"),
        hook_decision(payload, tmp_path, identity="resolver-worker"),
    ):
        assert granted.permission_decision == "ask"
        assert "human-authored" in granted.permission_decision_reason
        assert QuestionRelay(tmp_path / ".lup/questions.jsonl").pending() == []
        assert readme.read_text(encoding="utf-8") == "# Operator-authored design\n"


def test_generated_codex_hook_fails_closed_for_unknown_tools() -> None:
    script = Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()
    result = sh.Command(str(script))(
        _in='{"tool_name":"custom_tool","tool_input":{}}',
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    # The call is put to review, and a blocked review carries its reason on
    # stdout so the operator sees it; Codex drops a warning sent with exit 2.
    assert result.exit_code == 0
    spoken = json.loads(result.stdout)["hookSpecificOutput"]
    assert spoken["permissionDecision"] == "deny"
    assert "unknown tool" in spoken["permissionDecisionReason"]


def test_reconciliation_source_digest_rejects_a_stale_preimage(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("old\n", encoding="utf-8")
    patch = """diff --git a/source.py b/source.py
--- a/source.py
+++ b/source.py
@@ -1 +1 @@
-old
+new
"""

    before = source_patch_base_digest(tmp_path, patch)
    source.write_text("changed after proposal\n", encoding="utf-8")

    assert source_patch_base_digest(tmp_path, patch) != before


def test_reconciliation_proposal_persists_reviewable_patch_and_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("old\n", encoding="utf-8")
    patch = """diff --git a/source.py b/source.py
--- a/source.py
+++ b/source.py
@@ -1 +1 @@
-old
+new
"""

    record = ReconciliationProposalWriter().write(tmp_path, patch)
    directory = tmp_path / ".lup" / "reconcile" / record.proposal_id

    assert (directory / "source.patch").read_text(encoding="utf-8") == patch
    assert (directory / "metadata.json").read_text(encoding="utf-8").endswith("\n")


def test_reconciliation_source_digest_tracks_new_file_absence(tmp_path: Path) -> None:
    patch = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+new
"""

    before = source_patch_base_digest(tmp_path, patch)
    (tmp_path / "new.py").write_text("occupied\n", encoding="utf-8")

    assert source_patch_base_digest(tmp_path, patch) != before


def test_source_patch_parser_does_not_treat_hunk_content_as_file_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("-- /dev/null\n", encoding="utf-8")
    patch = """diff --git a/source.py b/source.py
--- a/source.py
+++ b/source.py
@@ -1 +1 @@
--- /dev/null
+value
"""

    assert source_patch_base_digest(tmp_path, patch)


def test_reconciliation_source_digest_rejects_escaping_paths(tmp_path: Path) -> None:
    patch = """diff --git a/../secret b/../secret
--- a/../secret
+++ b/../secret
"""

    with pytest.raises(ValueError, match="escapes"):
        source_patch_base_digest(tmp_path, patch)


def test_unchanged_ownership_manifest_is_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / ".native" / ".lup-ownership.json"
    manifest = build_manifest(
        portable_harness(),
        ArtifactTree(artifacts=[]),
        generator_version="test",
        target_requirements=[],
    )
    save_manifest(path, manifest)
    inode = path.stat().st_ino

    save_manifest(path, manifest)

    assert path.stat().st_ino == inode


def test_annotated_ownership_manifest_raises_a_typed_recovery_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".lup-ownership.json"
    assert load_manifest(path) is None
    manifest = build_manifest(
        portable_harness(),
        ArtifactTree(artifacts=[]),
        generator_version="test",
        target_requirements=[],
    )
    save_manifest(path, manifest)
    assert load_manifest(path) == manifest

    with path.open("a", encoding="utf-8") as handle:
        handle.write("# a trailing annotation\n")

    with pytest.raises(OwnershipManifestError, match="repair or remove"):
        load_manifest(path)


def test_the_generator_owns_the_proof_it_writes_and_never_lists(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".claude"
    home.mkdir()
    manifest = build_manifest(
        portable_harness(),
        ArtifactTree(artifacts=[]),
        generator_version="test",
        target_requirements=[],
    )
    save_manifest(home / ".lup-ownership.json", manifest)

    # A manifest lists what it proves and never itself, so every consumer
    # asking who owns the proof was told "the repository" about the one file
    # materialization always writes.
    assert not [item for item in manifest.files if "ownership" in str(item.path)]
    owned = generated_artifacts(tmp_path, homes=[".claude"])
    assert owned.owning(".claude/.lup-ownership.json") is not None
    assert owned.owning("packages/lup/src/lup/harness/ownership.py") is None


def test_proven_obsolete_deletion_is_proposed_and_executed(tmp_path: Path) -> None:
    obsolete = tmp_path / "obsolete.txt"
    obsolete.write_text("stale output\n", encoding="utf-8")
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("obsolete.txt"),
                content="stale output\n",
                category="generated",
                sha256=content_digest("stale output\n"),
            )
        ],
    )

    proposal = DeterministicReconciler().propose(current, ArtifactTree(artifacts=[]))
    result = AtomicMaterializer().apply(proposal)

    assert [delete.path for delete in proposal.deletes] == [Path("obsolete.txt")]
    assert result.removed == [Path("obsolete.txt")]
    assert not obsolete.exists()


def test_deletion_prunes_the_directories_it_empties(tmp_path: Path) -> None:
    gone = tmp_path / "skills" / "gone" / "SKILL.md"
    gone.parent.mkdir(parents=True)
    gone.write_text("stale skill\n", encoding="utf-8")
    kept = tmp_path / "skills" / "kept" / "SKILL.md"
    kept.parent.mkdir(parents=True)
    kept.write_text("live skill\n", encoding="utf-8")
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("skills/gone/SKILL.md"),
                content="stale skill\n",
                category="generated",
                sha256=content_digest("stale skill\n"),
            )
        ],
    )

    AtomicMaterializer().apply(
        DeterministicReconciler().propose(current, ArtifactTree(artifacts=[]))
    )

    assert not gone.parent.exists()
    assert kept.exists()
    assert tmp_path.exists()


def test_deletion_with_changed_ownership_proof_is_refused(tmp_path: Path) -> None:
    obsolete = tmp_path / "obsolete.txt"
    obsolete.write_text("stale output\n", encoding="utf-8")
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("obsolete.txt"),
                content="stale output\n",
                category="generated",
                sha256=content_digest("stale output\n"),
            )
        ],
    )
    proposal = DeterministicReconciler().propose(current, ArtifactTree(artifacts=[]))
    obsolete.write_text("user edited after the proposal\n", encoding="utf-8")

    with pytest.raises(MaterializationConflictError, match="ownership proof changed"):
        AtomicMaterializer().apply(proposal)
    assert obsolete.exists()


def test_materialization_rejects_stale_executable_mode(tmp_path: Path) -> None:
    path = tmp_path / "hook.py"
    path.write_text("pass\n", encoding="utf-8")
    path.chmod(0o644)
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("hook.py"),
                content="pass\n",
                category="generated",
                sha256=content_digest("pass\n"),
                executable=False,
            )
        ],
    )
    desired = ArtifactTree(
        artifacts=[
            Artifact(path=Path("hook.py"), content="updated\n", semantic_id="hook")
        ]
    )
    proposal = DeterministicReconciler().propose(current, desired)
    path.chmod(0o755)  # mode drift between proposal and apply

    with pytest.raises(MaterializationConflictError, match="stale executable mode"):
        AtomicMaterializer().apply(proposal)
    assert path.read_text(encoding="utf-8") == "pass\n"


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        (
            "diff --git a/x.py b/x.py\ndiff --git a/y.py b/y.py\n--- a/y.py\n",
            "missing its old-file header",
        ),
        ("diff --git a/x.py\n--- a/x.py\n", "malformed git source-patch header"),
        ("diff --git x/a.py y/a.py\n--- x/a.py\n", "repository-relative"),
        ("diff --git a/x.py b/x.py\n--- a/other.py\n", "does not match its diff"),
        ("diff --git a/x.py b/x.py\n", "missing its old-file header"),
        ("just prose\n", "no git file entries"),
    ],
    ids=[
        "header-without-old-file",
        "truncated-header",
        "foreign-prefixes",
        "mismatched-old-file",
        "trailing-header",
        "no-entries",
    ],
)
def test_malformed_source_patches_are_rejected(
    tmp_path: Path, patch: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        source_patch_base_digest(tmp_path, patch)


def test_source_digest_rejects_symlinked_preimage_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "file.py").write_text("secret\n", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)
    patch = (
        "diff --git a/link/file.py b/link/file.py\n"
        "--- a/link/file.py\n"
        "+++ b/link/file.py\n"
    )

    with pytest.raises(ValueError, match="escapes the repository"):
        source_patch_base_digest(root, patch)


def test_declared_sensitive_paths_are_classified_without_content(
    tmp_path: Path,
) -> None:
    path = Path("secrets.env")
    (tmp_path / path).write_text("API_KEY=hunter2\n", encoding="utf-8")

    tree = FilesystemCurrentTreeReader(None, sensitive_local_only=[path]).read(tmp_path)

    assert [(item.category, item.content) for item in tree.artifacts] == [
        ("sensitive_local_only", "")
    ]
    assert tree.artifacts[0].sha256  # identity still proven for reconciliation


def test_missing_root_reads_as_an_empty_tree(tmp_path: Path) -> None:
    tree = FilesystemCurrentTreeReader(None).read(tmp_path / "ghost")

    assert tree.artifacts == []


def test_symlink_escaping_the_root_is_sensitive_and_unread(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("credential\n", encoding="utf-8")
    (root / "escape.txt").symlink_to(secret)

    tree = FilesystemCurrentTreeReader(None).read(root)

    assert [(item.category, item.content, item.sha256) for item in tree.artifacts] == [
        ("sensitive_local_only", "", "")
    ]


def test_binary_local_only_file_cannot_claim_local_preservation(
    tmp_path: Path,
) -> None:
    path = Path("local.bin")
    (tmp_path / path).write_bytes(b"\xff\xfe")

    tree = FilesystemCurrentTreeReader(None, local_only=[path]).read(tmp_path)

    assert tree.artifacts[0].category == "unknown_conflict"
    assert tree.artifacts[0].content == ""


def test_binary_owned_file_requires_explicit_reconciliation(tmp_path: Path) -> None:
    path = Path("owned.md")
    manifest = build_manifest(
        portable_harness(),
        ArtifactTree(
            artifacts=[Artifact(path=path, content="text\n", semantic_id="owned")]
        ),
        generator_version="test",
        target_requirements=[],
    )
    (tmp_path / path).write_bytes(b"\xff\xfe")

    tree = FilesystemCurrentTreeReader(manifest, managed_paths=[path]).read(tmp_path)

    assert tree.artifacts[0].category == "unknown_conflict"
    assert tree.artifacts[0].content == ""


def test_exact_content_adoption_still_corrects_executable_drift(
    tmp_path: Path,
) -> None:
    content = "#!/bin/sh\n"
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("hook.sh"),
                content=content,
                category="unknown_conflict",
                sha256=content_digest(content),
                executable=False,
            )
        ],
    )
    desired = ArtifactTree(
        artifacts=[
            Artifact(
                path=Path("hook.sh"),
                content=content,
                semantic_id="hook",
                executable=True,
            )
        ]
    )

    proposal = DeterministicReconciler().propose(current, desired)

    assert proposal.conflicts == []
    assert [
        (write.previous_sha256, write.previous_executable) for write in proposal.writes
    ] == [(content_digest(content), False)]
    assert proposal.writes[0].artifact.executable


def test_proposal_rewrite_is_idempotent_but_tampering_refuses(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("old\n", encoding="utf-8")
    patch = (
        "diff --git a/source.py b/source.py\n"
        "--- a/source.py\n"
        "+++ b/source.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new"  # no trailing newline: the writer must normalize it
    )

    writer = ReconciliationProposalWriter()
    record = writer.write(tmp_path, patch)
    assert writer.write(tmp_path, patch) == record  # identical re-write is a no-op

    patch_path = tmp_path / ".lup" / "reconcile" / record.proposal_id / "source.patch"
    assert patch_path.read_text(encoding="utf-8").endswith("\n")
    patch_path.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="collision"):
        writer.write(tmp_path, patch)


def test_project_settings_derive_sandbox_from_hook_declaration() -> None:
    plugin = portable_harness().plugins[0]
    hooks = plugin.hooks
    assert hooks is not None
    settings = project_settings(plugin)
    sandbox = settings["sandbox"]
    assert isinstance(sandbox, dict)
    filesystem = sandbox["filesystem"]
    network = sandbox["network"]
    assert isinstance(filesystem, dict) and isinstance(network, dict)
    # A human-owned path asks through the policy; the runtime sandbox, which
    # can only refuse, is told nothing about it.
    assert "denyWrite" not in filesystem
    domains = network["allowedDomains"]
    assert isinstance(domains, list)
    assert "code.claude.com" in domains
    assert "github.com" in domains
    # The redirecting documentation host, and the domain around it left out:
    # egress reaches exactly the origins the fetch scopes name.
    assert "docs.anthropic.com" in domains
    assert "anthropic.com" not in domains
    assert "sandbox" not in project_settings(None)
    assert sandbox["excludedCommands"] == hooks.excluded_commands()


def test_a_declared_tool_server_is_granted_rather_than_asked_about() -> None:
    """A server every session carries is this project's own code.

    The grant names each server by the key a launch declares it under.
    """
    servers = launched_tool_servers()
    permissions = project_settings(portable_harness().plugins[0], servers)[
        "permissions"
    ]
    assert isinstance(permissions, dict)
    allowed = permissions["allow"]
    assert isinstance(allowed, list)
    for server in servers:
        assert f"mcp__{server.name}" in allowed
    assert "WebSearch" in allowed


def test_rendered_sandbox_keys_are_the_runtime_documented_ones() -> None:
    """The block is checked against the runtime's shape rather than assumed.

    A misspelled sandbox key changes nothing and reports nothing, so every
    key is drawn from the SDK's published shape. One is settings-file-only,
    because the SDK routes filesystem limits through permission rules instead
    — named here so the split reads as a fact about the two surfaces rather
    than as a mismatch nobody checked. Credential reads have no key of their
    own: the `Read` deny rules carry them, and the runtime merges those into
    the sandbox.
    """
    sandbox = project_settings(portable_harness().plugins[0])["sandbox"]
    assert isinstance(sandbox, dict)
    network = sandbox["network"]
    assert isinstance(network, dict)
    session_keys = SandboxSettings.__annotations__
    network_keys = SandboxNetworkConfig.__annotations__

    assert "excludedCommands" in session_keys
    assert sorted(key for key in sandbox if key not in session_keys) == [
        "filesystem",
    ]
    assert [key for key in network if key not in network_keys] == []


def test_declared_exclusions_cover_the_commands_the_boundary_cannot_carry() -> None:
    """Each pattern is checked against the command as the toolchain spells it.

    A pattern matches on a literal prefix, so one that reads convincingly
    beside the declaration and misses the invocation as typed excludes
    nothing — and it fails as whatever the boundary blocks, naming neither
    the pattern nor the sandbox.
    """
    hooks = portable_harness().plugins[0].hooks
    assert hooks is not None
    excluded = hooks.excluded_commands()
    for command in (
        "git push origin HEAD",
        "git fetch --all",
        "gh pr view 47",
        "ssh -T git@github.com",
    ):
        assert sandbox_excluded(command, excluded), command
    # The verbs that drive git rather than read it, for the reason `gh` is
    # excluded: a child of a confined command is confined too, so leaving
    # these inside moves the same failure one call deeper — measured in #351,
    # where `dev worktree create` could not take the lock its config write
    # needs while the identical `git config --local` succeeded one call away.
    for driving in (
        "uv run lup-devtools git worktree create feat-x",
        "uv run lup-devtools git pr push",
        "uv run lup-devtools resolve intake",
    ):
        assert sandbox_excluded(driving, excluded), driving
    assert not sandbox_excluded("uv run pytest -q", excluded)
    assert not sandbox_excluded(
        "uv run lup-devtools py info lup.policy.hooks", excluded
    )


def test_claude_sandbox_widens_the_writable_set_to_sibling_worktrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Claude roots writes at the launch cwd, so a second checkout is outside.

    The declared paths ride along with the resolved tree: this key is
    documented as merging across scopes and as overriding per session, and a
    list carrying both is the same list under either reading.
    """
    plugin = portable_harness().plugins[0]
    assert plugin.hooks is not None and plugin.hooks.sandbox is not None
    widened = settings_read(
        Claude(policy=plugin.hooks, sandbox=InnerSandbox()), tmp_path
    )

    assert widened["sandbox"]["filesystem"]["allowWrite"] == [
        *plugin.hooks.sandbox.writable_paths,
        str(tmp_path),
    ]


def test_a_launch_mount_widens_the_inner_sandbox_where_it_asked_to_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--mount` says this session may reach a folder, in whichever posture.

    Contained, that promise lands in the container's mount table through the
    fleet lease; on the host it has to land here, in the runtime's own
    widening, or where the session runs would decide what it can do. The
    read-only mount stays out of `allowWrite` because writing is exactly what
    it withheld -- reads are not what this key governs.
    """
    plugin = portable_harness().plugins[0]
    writable = tmp_path / "notes"
    read_only = tmp_path / "reference"

    widened = settings_read(
        Claude(
            policy=plugin.hooks,
            sandbox=InnerSandbox(
                mounts=[Mount(path=writable, writable=True), Mount(path=read_only)]
            ),
        ),
        tmp_path,
    )

    allowed = widened["sandbox"]["filesystem"]["allowWrite"]
    assert str(writable) in allowed
    assert str(read_only) not in allowed


def test_a_plugin_kept_beside_the_generated_one_is_named_at_launch(
    tmp_path: Path,
) -> None:
    """A marketplace name is one global namespace; a directory is this checkout's.

    Left to the namespace, the plugin a session loads is whichever tree
    registered that name last — so a project's own plugin is named on the
    same terms as the compiled one, and travels with the checkout.
    """
    plugins = tmp_path / ".claude" / "plugins"
    for name in ("lup", "aib"):
        declaration = plugins / name / ".claude-plugin" / "plugin.json"
        declaration.parent.mkdir(parents=True)
        declaration.write_text(json.dumps({"name": name}))
    (plugins / "cache").mkdir()

    assert companion_plugin_directories(tmp_path, "lup") == [plugins / "aib"]


def test_a_checkout_keeping_no_companion_names_nothing_further(tmp_path: Path) -> None:
    """A directory without a plugin declaration is not one, and neither is no directory."""
    assert companion_plugin_directories(tmp_path, "lup") == []


def test_leaving_a_worktree_is_granted_and_entering_one_is_not() -> None:
    """Entering arms a wall; leaving is how a session that got in gets out.

    The grant and the refusal have to agree, and once did not: entering was
    granted here while the guidance told every session not to do it, which
    left the whole gate resting on prose. Neither tool is a shell command, so
    no vocabulary sweep reaches them and `hooks classify` cannot answer for
    them -- the grant lives in the settings artifact and the refusal in the
    tool table, which is why both pins do too.
    """
    plugin = portable_harness().plugins[0]
    permissions = project_settings(plugin)["permissions"]
    assert isinstance(permissions, dict)
    allowed = permissions["allow"]
    assert isinstance(allowed, list)
    assert "ExitWorktree" in allowed
    assert "EnterWorktree" not in allowed
    assert plugin.hooks is not None
    refused = {item.tool for item in plugin.hooks.refused_tools}
    assert "EnterWorktree" in refused


def envelope_that(works: bool) -> Requirement:
    """A stand-in for the envelope probe, which either passes or does not.

    The real one runs `codex sandbox` and watches two witnesses, so it answers
    about the machine rather than about the code. That is the right shape for
    the probe and the wrong one for a unit test of what the launcher composes:
    on a host with no Codex CLI it made this assert a capability nobody
    claimed, and the failure named `LUP_SANDBOX_ACTIVE` rather than the
    missing runtime. The real probe is exercised where a host is in scope.
    """
    return Requirement(
        capability="codex envelope",
        purpose="testing what the launcher does with a probe's answer",
        where="host",
        exercise=Run(command=["true"] if works else ["false"]),
        absence=LostCapability(capability="the codex envelope"),
    )


def test_codex_sandbox_arguments_establish_the_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_launch, "codex_envelope_requirement", lambda: envelope_that(True)
    )
    environment: EnvVars = {}
    arguments = codex_envelope(
        portable_harness().plugins[0].hooks, environment, ["--model", "gpt-5.2"]
    )
    assert arguments[:2] == ["--sandbox", "workspace-write"]
    assert environment["LUP_SANDBOX_ACTIVE"] == "1"


def test_a_failed_probe_leaves_the_deny_lattice_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The safety-relevant direction, and the one nothing asserted.

    A flag set on an envelope nobody tested tells every dispatcher downstream
    to relax into a boundary that may not be there. So when the probe fails the
    envelope arguments still compose — the sandbox is still asked for — and the
    flag stays unset, which is what keeps the lattice carrying the escalation
    recipe.
    """
    monkeypatch.setattr(
        codex_launch, "codex_envelope_requirement", lambda: envelope_that(False)
    )
    environment: EnvVars = {}
    arguments = codex_envelope(portable_harness().plugins[0].hooks, environment, [])

    assert arguments[:2] == ["--sandbox", "workspace-write"]
    assert "LUP_SANDBOX_ACTIVE" not in environment


def test_codex_sandbox_widens_the_root_to_sibling_worktrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Codex roots writes at the launch cwd; the prescribed worktree is outside."""
    environment: EnvVars = {}
    arguments = codex_envelope(
        portable_harness().plugins[0].hooks, environment, [], tree=tmp_path
    )
    roots = arguments[arguments.index("-c") + 1]

    assert roots.startswith("sandbox_workspace_write.writable_roots=")
    assert str(tmp_path) in roots


def test_codex_sandbox_omits_the_root_outside_a_tree_layout() -> None:
    """A plain clone has no tree/ to widen to, so the envelope stands alone."""
    environment: EnvVars = {}

    assert codex_envelope(portable_harness().plugins[0].hooks, environment, []) == [
        "--sandbox",
        "workspace-write",
    ]


def test_codex_sandbox_arguments_defer_to_a_caller_envelope() -> None:
    environment: EnvVars = {}
    caller_forms = [
        ["--sandbox", "danger-full-access"],
        ["--sandbox=read-only"],
        ["-s", "read-only"],
        ["--yolo"],
        ["--approve-for-me"],
        ["--not-so-yolo"],
        ["--dangerously-bypass-approvals-and-sandbox"],
    ]
    for extra_args in caller_forms:
        assert (
            codex_envelope(portable_harness().plugins[0].hooks, environment, extra_args)
            == []
        )
    assert "LUP_SANDBOX_ACTIVE" not in environment


def test_declared_tool_servers_are_the_registry_the_backends_assemble() -> None:
    """A group added to the toolsets registry reaches a launched session too."""
    servers = launched_tool_servers()
    assert [server.name for server in servers] == startup_names(declared_tool_groups())


def launched_claude_servers(
    servers: list[ToolServer], serve: ServeLaunch
) -> dict[str, Any]:
    """The ``--mcp-config`` a launched Claude Code starts these servers from."""
    words = claude_mcp_arguments(ClaudeTools(mcp=servers, serve=serve))
    return json.loads(words[words.index("--mcp-config") + 1])["mcpServers"]


def launched_codex_servers(
    servers: list[ToolServer], serve: ServeLaunch
) -> dict[str, dict[str, Any]]:
    """The ``mcp_servers`` tables a launched Codex starts these servers from."""
    words = codex_mcp_arguments(CodexTools(mcp=servers, serve=serve))
    tables: dict[str, dict[str, Any]] = {}
    for word in words[1::2]:
        key, _, value = word.partition("=")  # lup: ignore[string-split]
        _, name, field = key.split(".", 2)  # lup: ignore[string-split]
        tables.setdefault(name, {})[field] = json.loads(value)
    return tables


def test_every_launched_server_starts_in_this_checkout_for_its_own_engine(
    tmp_path: Path,
) -> None:
    """Neither runtime may leave the root to whatever directory a launch had."""
    serve = launched_serve(tmp_path)
    claude = launched_claude_servers(launched_tool_servers(), serve)
    codex = launched_codex_servers(launched_tool_servers(), serve)

    for runtime, servers in (("claude", claude), ("codex", codex)):
        for server in servers.values():
            arguments = server["args"]
            assert server["command"] == "uv"
            assert arguments[arguments.index("--directory") + 1] == str(tmp_path)
            assert arguments[arguments.index("--runtime") + 1] == runtime


def test_neither_tree_carries_the_servers_a_launch_declares() -> None:
    """A strict roster drops a plugin's servers, so the tree carries none."""
    claude = compile_claude(portable_harness())
    codex = tomllib.loads(codex_project_config())

    assert not any(artifact.path.name == ".mcp.json" for artifact in claude.artifacts)
    assert "mcp_servers" not in codex
    assert codex["features"]["hooks"] is True


def test_only_the_runtime_that_forwards_nothing_is_told_what_to_forward(
    tmp_path: Path,
) -> None:
    """A name is asked for exactly where a server would otherwise never see it.

    One runtime hands a server it spawns the whole environment, so its
    declaration has nothing to say about any of it. The other hands one a
    fixed base and forwards only what it was asked for, so the roster
    identity and recursion allowance the launcher exported reach a server
    only where it names them — without them the coordination server joins
    the roster under no id and a nested agent spends no allowance.
    """
    serve = launched_serve(tmp_path)
    claude = launched_claude_servers(launched_tool_servers(), serve)
    codex = launched_codex_servers(launched_tool_servers(), serve)

    assert {name: server.get("env_vars") for name, server in codex.items()} == {
        name: tool_server_env() for name in startup_names(declared_tool_groups())
    }
    assert not any("env_vars" in server for server in claude.values())


def test_an_always_loaded_server_is_exempt_only_where_tools_are_deferred(
    tmp_path: Path,
) -> None:
    """A server called dozens of times a session should not cost a search each time.

    Claude Code withholds MCP tool definitions until a search asks for them
    and exempts a server declaring ``alwaysLoad``. Codex documents no such
    control, so its tables gain no key it would not read; and a server that
    declares nothing keeps each runtime's own loading.
    """
    serve = launched_serve(tmp_path)
    loaded = [
        server.model_copy(update={"always_load": True})
        for server in launched_tool_servers()
    ]

    assert launched_claude_servers(loaded, serve)["notes"]["alwaysLoad"] is True
    assert (
        "alwaysLoad"
        not in launched_claude_servers(launched_tool_servers(), serve)["notes"]
    )
    codex = launched_codex_servers(loaded, serve)
    unloaded = launched_codex_servers(launched_tool_servers(), serve)
    assert {name: sorted(table) for name, table in codex.items()} == {
        name: sorted(table) for name, table in unloaded.items()
    }


def test_both_runtimes_grant_the_declared_servers_the_same_way(tmp_path: Path) -> None:
    """Serving a tool and being allowed to call it are different claims.

    A session with no operator to ask holds every server it was given and
    can call none of them, refusing each with its approval policy rather
    than with anything naming a server — which reads as an agent that chose
    not to use its instruments. The grant is derived from the declaration on
    both runtimes so neither can be the one that forgot.
    """
    servers = launched_tool_servers()
    codex = launched_codex_servers(servers, launched_serve(tmp_path))
    approved = {
        name
        for name, table in codex.items()
        if table["default_tools_approval_mode"] == "approve"
    }
    declared = {server.name for server in servers}
    permissions = project_settings(portable_harness().plugins[0], servers)[
        "permissions"
    ]
    assert isinstance(permissions, dict)

    allowed = permissions["allow"]
    assert isinstance(allowed, list)

    assert approved == declared
    assert all(f"mcp__{name}" in allowed for name in declared)


def harness_granting_its_own_servers() -> Harness:
    """The example harness, with every skill and agent granting every server.

    The example declares servers and grants none of them, so a tree built
    from it says nothing about what happens when a declaration asks for one —
    which is exactly the case that was broken. Adopters do ask, so the grant
    is put where an adopter puts it rather than left to whichever example
    happens to carry one.
    """
    source = portable_harness()
    plugin = source.plugins[0]
    wanted = [f"mcp__{server.name}" for server in launched_tool_servers()]
    return source.model_copy(
        update={
            "plugins": [
                plugin.model_copy(
                    update={
                        "skills": [
                            skill.model_copy(update={"tools": [*skill.tools, *wanted]})
                            for skill in plugin.skills
                        ],
                        "agents": [
                            agent.model_copy(update={"tools": [*agent.tools, *wanted]})
                            for agent in plugin.agents
                        ],
                    }
                )
            ]
        }
    )


def test_a_grant_in_a_command_names_the_tool_its_permission_admits() -> None:
    """The two halves of one grant, rendered into two artifacts.

    A skill's `allowed-tools` and the settings permission that admits it are
    written by different renderers and have to name the same tool: each
    names the server by the key a launch declares it under, so a pass granted
    its instruments in its own declaration opens with them.
    """
    source = harness_granting_its_own_servers()
    admitted = set(served_tool_grants(launched_tool_servers()))
    assert admitted, "the project declares no tool servers to grant"

    rendered = {
        artifact.path.as_posix(): artifact.content
        for artifact in compile_claude(source).artifacts
    }
    declarations = {
        path: content
        for path, content in rendered.items()
        if "/commands/" in path or "/agents/" in path
    }
    assert declarations, "no commands or agents were rendered"

    for path, content in declarations.items():
        # The grant line is what this renderer wrote: one `key: value` line of
        # the frontmatter, its value joined with ", ". Read back the same way.
        header = content.partition("\n---")[0]  # lup: ignore[string-split]
        for line in header.splitlines():
            key, _, value = line.partition(": ")  # lup: ignore[string-split]
            if key not in ("allowed-tools", "tools"):
                continue
            for grant in value.split(", "):  # lup: ignore[string-split]
                if grant.startswith("mcp__"):
                    assert grant in admitted, (
                        f"{path} grants {grant}, which no settings permission admits"
                    )


def test_a_declared_startup_deadline_reaches_the_runtime_that_waits_on_one(
    tmp_path: Path,
) -> None:
    """A group resolving its package before it imports anything starts slowly.

    The runtime's own default is set for a server already installed, and
    missing it drops that one server while the session keeps the rest — so
    the symptom is a group simply absent from a session that otherwise
    works, which reads as flakiness rather than as a configured limit. Codex
    takes it per server; Claude Code reads one ``MCP_TIMEOUT``, in
    milliseconds, for every server it starts.
    """
    serve = launched_serve(tmp_path)
    codex = launched_codex_servers(launched_tool_servers(), serve)

    for name in ("notes", "codeintel", "sandbox"):
        assert codex[name]["startup_timeout_sec"] == 60.0
    assert claude_server_environment(
        ClaudeTools(mcp=launched_tool_servers(), serve=serve)
    ) == {"MCP_TIMEOUT": "60000"}


def test_a_launch_naming_no_deadline_keeps_the_runtimes_own(tmp_path: Path) -> None:
    """Declaring nothing leaves the default, rather than this file's opinion."""
    serve = launched_serve(tmp_path).model_copy(
        update={"startup_timeout_seconds": None}
    )

    assert (
        "startup_timeout_sec"
        not in launched_codex_servers(launched_tool_servers(), serve)["notes"]
    )
    assert (
        claude_server_environment(ClaudeTools(mcp=launched_tool_servers(), serve=serve))
        == {}
    )


def test_a_named_session_is_what_makes_a_native_server_serve_real_tools() -> None:
    """No adapter relays a context to a natively launched server; it opens one."""
    context = harness_session_context(HARNESS_SESSION)
    groups = collect_tools_by_server(context)

    assert context.session_id == HARNESS_SESSION
    assert NOTES_GROUP in groups
    # Built, and servable only by name: a live agent is handed the rest.
    assert EXAMPLE_GROUP in groups


def test_every_target_renders_every_declaration_the_source_names() -> None:
    """Parity, at the compiler where a target would drop one.

    The two trees shape a skill differently — `commands/<name>.md` against
    `skills/<name>/SKILL.md` — so nothing about the files says the rosters
    agree. What both carry back is the declaration's own semantic id, and
    measuring each tree against the source's list is what makes a renderer
    that quietly skips a kind fail here rather than months later, when
    somebody on the other runtime asks where a skill went.
    """
    harness = portable_harness()

    for tree in (compile_claude(harness), compile_codex(harness)):
        rendered = {artifact.semantic_id for artifact in tree.artifacts}
        assert [
            declared for declared in harness.rendered_ids if declared not in rendered
        ] == []


def test_a_target_that_renders_one_declaration_short_is_named_with_it(
    tmp_path: Path,
) -> None:
    """The gate has to be able to fail, and to say which target and which name."""
    harness = portable_harness()
    full = compile_claude(harness)
    dropped = harness.plugins[0].skills[0].id
    composition = NativeHarnessComposition(
        login=CLAUDE_LOGIN,
        default_config_home=tmp_path,
        recipe=GenerationRecipe(
            label="claude",
            root=tmp_path,
            source=harness,
            desired=ArtifactTree(
                artifacts=[
                    artifact
                    for artifact in full.artifacts
                    if artifact.semantic_id != dropped
                ]
            ),
            manifest_path=tmp_path / "manifest.json",
            prior=None,
            reader=FilesystemCurrentTreeReader(None),
            target_requirements=[],
        ),
        readiness=list,
        invocation_renderer=ClaudeSpellings(),
    )

    gaps = roster_gaps([composition])

    assert [gap.declaration for gap in gaps] == [dropped]
    assert gaps[0].describe() == f"claude renders nothing for {dropped}"


def test_a_tree_generated_on_the_way_to_something_else_says_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A launch regenerates before it opens, and is not what anybody typed.

    `0 writes, 0 deletes, 0 conflicts` over `0 changed, 0 removed` reports
    that the command did what it always does, at the top of the block where a
    reader is looking for the line that is different today.
    """
    generate(third_recipe(tmp_path, None))
    settled = manifest_of(third_recipe(tmp_path, None))
    composed = NativeHarnessComposition(
        login=CLAUDE_LOGIN,
        default_config_home=tmp_path,
        recipe=third_recipe(tmp_path, settled),
        readiness=lambda: [],
        invocation_renderer=ClaudeSpellings(),
    )

    generate_with_report(composed, in_passing=True)
    assert capsys.readouterr().out == ""

    generate_with_report(composed)
    asked_for = capsys.readouterr().out
    assert "0 writes, 0 deletes, 0 conflicts" in asked_for
    assert "0 changed, 0 removed" in asked_for


def test_a_current_repository_artifact_is_neither_rewritten_nor_announced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """In passing, the check is the whole of what a current artifact costs."""
    calls: list[str] = []

    def current(root: Path | None = None, *, check: bool = False) -> Path:
        calls.append(f"current:{'check' if check else 'write'}")
        return tmp_path / "current.md"

    def behind(root: Path | None = None, *, check: bool = False) -> Path:
        calls.append(f"behind:{'check' if check else 'write'}")
        if check:
            raise RuntimeError("behind its source")
        return tmp_path / "behind.md"

    generate_targets([], [current, behind], in_passing=True)
    assert calls == ["current:check", "behind:check", "behind:write"]
    assert capsys.readouterr().out == (
        f"repository artifact ready: {tmp_path / 'behind.md'}\n"
    )

    calls.clear()
    generate_targets([], [current, behind])
    assert calls == ["current:write", "behind:write"]
    assert capsys.readouterr().out.count("repository artifact ready") == 2


def test_the_claude_watch_says_when_to_stop_it() -> None:
    """A watch that outlives the report resumes the finished reader.

    So the Claude spelling names the call that ends a watch and the moment
    to make it, beside the advice against polling it was written for. The
    Codex spelling describes a session that is read rather than pushed, and
    says nothing of the kind.
    """
    prompt = PromptDocument(
        parts=[WatchOutput(command="uv run lup-devtools dev check")]
    )

    claude = claude_prompt_renderer().render(prompt)
    codex = codex_prompt_renderer().render(prompt)

    assert "`Monitor`" in claude
    assert "`TaskStop`" in claude
    assert "before reporting" in claude
    assert "TaskStop" not in codex
