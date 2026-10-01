# lup: ignore[constant-declaration]
# The scaffold section ids are this repository's own vocabulary; the prose
# naming a hook event lives in the passages beside this module.
# Every constant here is one block of that scaffold's prose: a project wanting
# different words composes different blocks, which is an override the
# mechanical half of the constant rule cannot see.
"""Portable downstream-template sections shared by every guidance flavor."""

import lup.harness.models as models

import lup.harness.content.conventions as conventions
from lup.tools.lsp.tools import rendered_tool_declarations


def bumped(lead: str, tail: str) -> models.WhereTaken:
    """A pointer at `/lup:bump`, present only where the version module is taken."""
    return models.WhereTaken(
        module="version",
        parts=[
            models.TextPart(text=lead),
            models.SkillInvocation(plugin="lup", skill="bump"),
            models.TextPart(text=tail),
        ],
    )


CODEINTEL_TOOL_ROSTER: list[models.PromptPart] = [
    models.ToolRoster(tools=rendered_tool_declarations())
]

SETUP_THROUGH_NAMING: list[models.PromptPart] = [
    models.Passage(module=__name__),
]

INNER_AGENT_BULLET: list[models.PromptPart] = [
    models.Passage(module=__name__, name="agent-vocabulary"),
]

PRINCIPLES_THROUGH_PATTERN_MENU: list[models.PromptPart] = [
    models.Passage(
        module=__name__,
        name="important-context",
        values={
            # A downstream project is installed the plugin this one ships, so
            # each line naming an optional module's skill or command goes with
            # the module.
            "version_bump": bumped(
                " — bump on behavior changes with "
                "`uv run lup-devtools version bump` (or `",
                "`)",
            ),
            "setup_command": models.WhereTaken(
                module="setup",
                parts=[
                    models.TextPart(
                        text="uv run lup-devtools setup                # keys, "
                        "integrations, env vars (`dashboard` for the web UI)\n"
                    )
                ],
            ),
            "debug_skill": models.SkillInvocation(plugin="lup", skill="debug"),
            "feedback_scripts": models.WhereTaken(
                module="feedback-loop",
                parts=[
                    models.TextPart(
                        text="# Collect feedback from sessions\n"
                        "uv run lup-devtools feedback collect --all-time\n\n"
                        "# Status: version, data, analysis state, aggregate "
                        "stats\nuv run lup-devtools feedback status\n\n"
                    )
                ],
            ),
            "init_skill": models.SkillInvocation(plugin="lup", skill="init"),
            "bump_step": bumped(
                "\n- Bump on behavior changes (prompts, tools, subagents) with "
                "`uv run lup-devtools version bump <level>` or `",
                "`",
            ),
        },
    ),
]

PATTERN_MENU_TAIL_THROUGH_WORKTREE_STEP: list[models.PromptPart] = [
    models.Passage(
        module=__name__,
        name="plan-at-agent-speed",
        values={"init_skill": models.SkillInvocation(plugin="lup", skill="init")},
    ),
]

WORKFLOW_THROUGH_COMMIT_FORMAT: list[models.PromptPart] = [
    models.Passage(
        module=__name__,
        name="worktrees",
        values={
            "relocate": models.RelocateSession(path="the absolute path step 1 prints"),
            "rebase_skill": models.SkillInvocation(plugin="lup", skill="rebase"),
            "close_skill": models.SkillInvocation(plugin="lup", skill="close"),
        },
    ),
    *conventions.MERGE_CONFLICT_RESOLUTION.parts,
    *conventions.COMMIT_GUIDELINES.parts,
    models.Passage(module=__name__, name="commit-types"),
    *conventions.COMMIT_TYPES.parts,
    models.Passage(module=__name__, name="commit-examples"),
]

DIRECTORY_STRUCTURE_THROUGH_TOOLS: list[models.PromptPart] = [
    models.Passage(
        module=__name__,
        name="directory-structure",
        values={
            "resolve_clears": models.WhereTaken(
                module="resolver",
                parts=[
                    models.TextPart(text=" Use `"),
                    models.SkillInvocation(plugin="lup", skill="resolve"),
                    models.TextPart(text="` to clear resolved notes."),
                ],
            )
        },
    ),
]

TOOLING_INTRO: list[models.PromptPart] = [
    models.Passage(module=__name__, name="tooling"),
]

POLICY_JOIN = r"""The policy classifies every shell command against the vocabulary `lup.policy.vocabulary`
declares and `src/<project>/harness/content/shell_vocabulary.py` adjusts, every URL scope, and every edit
in a batch. Segments join deny > ask > defer > allow, so a judged deny wins the
batch and malformed input fails conservatively. Ask is reserved for judged
risk: an unjudged command denies with a hint naming the
`# lup: escalate[decision]: <why>` marker, and that marker as a command's leading line
promotes the decision into an approval question carrying your stated reason.
Under a launcher-verified sandbox (`LUP_SANDBOX_ACTIVE`), unjudged work defers
to that boundary rather than denying."""
"""What a reader needs before a denial, which is the shape and the way out.

The rest of the lattice — how substitutions, loops, redirections and wrappers
classify, and what an edit decision weighs — is in docs/permissions.md, because
a denial names what tripped and the recovery at the moment it matters. Carrying
the whole table on every turn buys nothing a reader could not open, and this
document is the one with a byte budget.
"""

CLAUDE_POLICY_SCOPE = (
    POLICY_JOIN
    + r""" A `dangerouslyDisableSandbox` escape re-enters the deny lattice, and
the sandbox block in `.claude/settings.json` derives from the same `HookSet`
declaration. Where a command runs is a second axis a rule declares beside its
effect and cascades to the levels beneath it, so every `git` verb already runs
outside the sandbox unasked. [docs/permissions.md](docs/permissions.md) carries
the full lattice."""
)

CODEX_POLICY_SCOPE = (
    POLICY_JOIN
    + r""" Native `apply_patch` commands are decoded into complete before/after
batches for the canonical edit policy, and malformed or unsupported patches
fail closed. Codex's own sandbox and approval policy remain the outer
filesystem and network boundary. Generation also compiles every prefix-safe
shell allow into `.codex/rules/lup.rules`, which Codex uses to run matching
commands outside the sandbox without prompting, while flag- and
content-sensitive forms stay under the hook.
[docs/permissions.md](docs/permissions.md) carries the full lattice."""
)


def permission_hooks(policy_scope: str) -> list[models.PromptPart]:
    """Render the permission-hooks section around one flavor-owned scope claim.

    The canonical-policy framing is identical on both platforms; each native
    adapter supplies complete edit documents through its own decoding boundary,
    so each template passes its own scope paragraph.
    """
    return [
        models.Passage(
            module=__name__,
            name="permission-hooks",
            values={
                "textpart": models.TextPart(text=policy_scope),
                "hooks_skill": models.SkillInvocation(plugin="lup", skill="hooks"),
            },
        ),
    ]


SELF_IMPROVEMENT_THROUGH_END: list[models.PromptPart] = [
    models.Passage(module=__name__, name="self-improvement-loop"),
    *conventions.FAILURE_ANALYSIS.parts,
    models.Passage(module=__name__, name="diagnosing-failures"),
    # The walk through running the loop is the feedback-loop module's; what a
    # session records, and how a failure is diagnosed, are every project's.
    models.WhereTaken(
        module="feedback-loop",
        parts=[models.Passage(module=__name__, name="running-the-feedback-loop")],
    ),
    models.Passage(module=__name__, name="what-to-track"),
]
