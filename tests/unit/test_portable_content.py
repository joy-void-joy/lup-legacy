"""Behavior tests for the rule that keeps platforms out of portable prose.

The vocabulary is derived from the runtimes rather than written down, so these
tests check the derivation itself as much as the declarations it judges.
"""

import pytest

from lup.providers.claude.harness import ClaudeSpellings, claude_granted_tools
from lup.providers.codex.harness import CodexSpellings
from lup.providers.harness import compile_codex
from lup.harness.codescan.antipatterns import COMPOSITION_RULES
from lup.harness.codescan.common import RuleExample
from lup.harness.codescan.portable import (
    CompositionRule,
    native_vocabulary,
    prose_breaches,
)
from lup.harness.contracts import NativeSpellings
from lup.policy.kernel.decision import SANDBOX_ESCALATION_RECIPE
from lup.harness.models import PromptDocument, TextPart
from lup.harness.prompts import SPAWNED_SESSION_LOSES_SHELL
from lup_template.harness.catalog import launched_tool_servers, portable_harness

RUNTIMES: list[NativeSpellings] = [ClaudeSpellings(), CodexSpellings()]

MARK = "<supplied by the caller>"

REMAINING_PROSE_BREACHES: list[str] = []
"""Declarations whose prose still names a platform.

Empty, and the compilers now refuse a breach outright, so this is a second
reading of the same invariant rather than a worklist: it names which
declaration regressed, where the compile error names only the spelling."""


def instruction_text(runtime: NativeSpellings) -> str:
    """Everything this runtime says when it frames a caller's own words."""
    return " ".join(
        [
            runtime.ask_user(MARK),
            runtime.delegate("plugin:agent", MARK),
            runtime.request_approval(MARK, MARK),
            runtime.relocate_session(MARK),
            runtime.watch_output(MARK),
            runtime.resolver_entry(),
            runtime.runtime_docs(),
            runtime.escape_sandbox(MARK).in_prose(),
            runtime.read_document(MARK).in_prose(),
        ]
    )


def test_declared_identifiers_occur_in_what_the_runtime_spells() -> None:
    """A declared identifier that no instruction contains describes nothing."""
    for runtime in RUNTIMES:
        spelled = instruction_text(runtime)
        for identifier in runtime.native_identifiers:
            assert identifier in spelled, (
                f"{runtime.runtime_name} declares {identifier!r} but never spells it"
            )


def test_a_runtime_that_cannot_spell_an_idea_says_why_rather_than_nothing() -> None:
    """Declining is an answer here, and an answer without a reason is silence.

    Both spellings a runtime may decline are asked of both runtimes, so the
    seam being closed is checked as behavior and not only as an abstract
    method somebody remembered to implement.
    """
    for runtime in RUNTIMES:
        for spelling in [runtime.escape_sandbox(MARK), runtime.read_document(MARK)]:
            audited = spelling.audited()
            assert audited, f"{runtime.runtime_name} answers with nothing at all"
            assert audited != "unsupported — ", (
                f"{runtime.runtime_name} declines without saying why"
            )


def test_the_resolver_entry_asks_its_own_vocabulary_about_the_sandbox() -> None:
    """Neither entry may hardcode an escape, and neither may invent one.

    Both runtimes spell one, in words of their own that nothing here knows,
    so what the two entries share is the asking. The load-bearing assertion is
    the second: an entry may name the sandbox exactly when its own vocabulary
    spelled something, which is what an entry that grew a flag would break —
    and what an entry still naming the sandbox would break on a runtime that
    later declines.
    """
    for runtime in RUNTIMES:
        escape = runtime.escape_sandbox(SPAWNED_SESSION_LOSES_SHELL).in_prose()
        entry = runtime.resolver_entry()

        if escape:
            assert escape in entry, (
                f"{runtime.runtime_name} spells an escape its own entry drops"
            )
        assert ("sandbox" in entry.lower()) == bool(escape), (
            f"{runtime.runtime_name} names the sandbox outside escape_sandbox"
        )


def test_every_runtime_teaches_the_same_request_for_the_launcher_host() -> None:
    """The agent's route out is Lup's marker, so it is one sentence everywhere.

    A native flag is not that request: it says where a call runs and nobody
    answers it. Asking for the launcher's host is a reviewed request, so the
    words are Lup's and identical under every runtime — which is what keeps
    an agent from having to know which provider it is under before it can ask.

    What a runtime still spells for itself is the mechanism beneath an
    approved crossing, and that is what ``escape_sandbox`` answers.
    """
    for runtime in RUNTIMES:
        assert "escalate[sandbox]" in SANDBOX_ESCALATION_RECIPE
        spelling = runtime.escape_sandbox(MARK)
        assert spelling.audited(), (
            f"{runtime.runtime_name} answers the mechanism seam with nothing"
        )


def test_vocabulary_follows_the_locations_a_runtime_can_spell() -> None:
    """Derivation, not a table: every location reachable is forbidden in prose."""
    vocabulary = native_vocabulary(ClaudeSpellings(), ["lup"])

    assert ".claude/CLAUDE.md" in vocabulary
    assert ".claude/plugins/lup/commands/" in vocabulary
    assert ".claude/.lup-ownership.json" in vocabulary
    assert "Claude Code" in vocabulary
    assert "AskUserQuestion" in vocabulary


def test_compiling_refuses_prose_that_names_a_platform() -> None:
    """The gate has to bite at the seam, not only in this file's inventory."""
    harness = portable_harness()
    leaked = harness.model_copy(
        update={
            "guidance": PromptDocument(
                parts=[TextPart(text="Edit .claude/settings.json by hand")]
            )
        }
    )

    with pytest.raises(ValueError, match="must name no platform"):
        compile_codex(leaked)


def test_portable_prose_names_no_platform_beyond_the_known_inventory() -> None:
    breaches = prose_breaches(portable_harness(), RUNTIMES)
    remaining = sorted(dict.fromkeys(breach.declaration_id for breach in breaches))

    assert remaining == sorted(REMAINING_PROSE_BREACHES), (
        "portable prose inventory changed; converted declarations must leave "
        f"the list. Current: {remaining}"
    )


def test_a_grant_claude_cannot_honor_never_reaches_the_rendered_tree() -> None:
    """The portable vocabulary is a superset, and the tree gets what exists.

    Claude Code ships no `Glob` and no `Grep`. Granting them is inert, which
    is worse than harmless: it reads as search the agent has, and the agent
    spends a turn per attempt learning otherwise. Declarations keep naming
    the portable set, and the filter is where the runtime is known.
    """
    assert claude_granted_tools(["Read", "Grep", "Glob", "Bash"]) == ["Read", "Bash"]
    assert claude_granted_tools(["Bash(git:*)", "Read"]) == ["Bash(git:*)", "Read"]

    declared = [
        (declaration.id, declaration.tools)
        for plugin in portable_harness().plugins
        for declaration in [*plugin.skills, *plugin.agents]
    ]
    assert declared, "the harness declares no skills or agents to check"
    for identifier, tools in declared:
        assert claude_granted_tools(tools) == [
            tool for tool in tools if tool not in ("Glob", "Grep")
        ], identifier


def test_a_grant_naming_a_server_keeps_the_key_it_is_declared_under() -> None:
    """A launch declares every server under its own key, so a grant spells it so.

    A grant rewritten to any other name would match no tool at all — and
    matching nothing is invisible: the skill opens, its instruments are
    absent, and the declaration that listed them reads as though they were
    there.
    """
    served = [f"mcp__{server.name}" for server in launched_tool_servers()]
    assert served, "the project declares no tool servers to grant"

    for declaration in [
        *portable_harness().plugins[0].skills,
        *portable_harness().plugins[0].agents,
    ]:
        granted = claude_granted_tools([*declaration.tools, *served])
        assert granted[len(granted) - len(served) :] == served, declaration.id


# The composition rule carries its snippets like every other rule, so the
# generation gate's table is covered by construction as well.
COMPOSITION_EXAMPLE_CASES = [
    pytest.param(rule, example, id=f"{rule.id}-{index}-{example.verdict}")
    for rule in COMPOSITION_RULES
    for index, example in enumerate(rule.examples)
]


@pytest.mark.parametrize(("rule", "example"), COMPOSITION_EXAMPLE_CASES)
def test_each_composition_rule_answers_its_own_examples(
    rule: CompositionRule, example: RuleExample
) -> None:
    """Generation says about each snippet what its rule declared it would.

    The snippet stands in for the guidance of a harness whose every other
    declaration is portable, so what the judge reports is the snippet's own.
    """
    judged = portable_harness().model_copy(
        update={"guidance": PromptDocument(parts=[TextPart(text=example.code)])}
    )
    breaches = rule.judge(judged, RUNTIMES)

    match example.verdict:
        case "flagged":
            assert breaches, f"generation admits {rule.id} at: {example.code}"
        case "cleared":
            assert breaches == [], f"generation refuses {rule.id} at: {example.code}"
        case "refuted":
            raise AssertionError(
                f"{rule.id} claims a refuted example, but generation has no "
                "second surface to take a verdict back on"
            )
