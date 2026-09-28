# lup: ignore[native-spelling]
# This map's subject matter is the native spellings themselves.
"""Every intended Claude/Codex difference and the parity audit."""

import lup.harness.models as models

from lup.harness.content.application import ApplicationLayout
from lup.formats.markdown import contained


def document(
    skills: list[models.Skill],
    agents: list[models.Agent],
    claude_decodes: list[str],
    codex_decodes: list[str],
    layout: ApplicationLayout,
) -> models.PromptDocument:
    """The parity audit, counted against the roster it is auditing.

    The counts are read from the declarations rather than written down, so a
    skill added on either side of the split cannot leave this table claiming
    a number that stopped being true. Each decoded set arrives the same way,
    from the root that composes the runtimes, and is named as what it is: the
    refusal table widens the rendered matcher past it, by whatever the
    composing project declared.
    """
    claude_decoded = contained("|".join(claude_decodes))
    codex_decoded = contained("|".join(codex_decodes))
    # The resolver's entry is described only where a project declares one:
    # a project that declined the module serves no `resolve` command, and a
    # row telling its reader to run one would document a command it lacks.
    has_resolver = any(skill.name == "resolve" for skill in skills)
    resolver_entry: list[models.PromptPart] = (
        [
            models.SpellingExample(
                text=(
                    "| Resolver entry | `/lup:resolve` instructs `uv run "
                    "lup-devtools resolve --adapter claude` | "
                    "`$lup:resolve` instructs the same command with "
                    "`--adapter codex` |"
                )
            ),
            models.TextPart(
                text=r""" Both entries only launch the shared persisted Python resolver, and differ solely in the adapter they name — `ResolverEntry` is deliberately undifferentiated because workflow scripts execute in an isolated VM with no shell, leaving the Claude entry nothing to wrap. The entry contract is the CLI's: optional `--run-id <id>` (resume) and repeatable `--answer <question-id>=<value>`, through which the reserved `integration-assembly` gate is approved like any other question. Both rendered entries document it (pinned by `test_generated_resolver_entries_only_launch_the_shared_python_core`, which also asserts no `Workflow(` wrapper appears). |
"""
            ),
        ]
        if has_resolver
        else []
    )
    resolver_parity: list[models.PromptPart] = (
        [
            models.TextPart(
                text=r"""| Resolver entry | skill instructs the CLI directly | skill instructs the CLI directly | Parity — neither tree generates a launcher artifact; the shared `resolve --adapter <runtime>` CLI is the entry on both sides. |
"""
            )
        ]
        if has_resolver
        else []
    )
    return models.PromptDocument(
        source=__name__,
        parts=[
            models.Passage(
                module=__name__,
                values={
                    "spellingexample": models.SpellingExample(
                        text="| Skill invocation spelling | `/lup:<skill>` (`ClaudeSpellings.render`) | `$lup:<skill>` (`CodexSpellings.render`) | Native sigils. Canonical content stores `SkillInvocation` parts; only the vocabularies spell them. `SkillPattern` carries the placeholder or wildcard form a prompt uses when it teaches the shape of an invocation instead of issuing one. |"
                    ),
                    "harness_catalog_py": models.code(
                        layout.path("harness", "catalog.py")
                    ),
                    "claude_decoded": models.code(claude_decoded),
                    "codex_decoded": models.code(codex_decoded),
                },
            ),
            *resolver_entry,
            models.Passage(
                module=__name__,
                name="artifact-parity",
                values={
                    "len_skills": models.counted(
                        len([skill for skill in skills if skill.machine_hint is None])
                    ),
                    "len_machine": models.counted(
                        len([skill for skill in skills if skill.machine_hint])
                    ),
                    "len_agents": models.counted(len(agents)),
                },
            ),
            *resolver_parity,
            models.Passage(module=__name__, name="runtime-parity"),
        ],
    )
