"""The seam from a policy verdict to a live session's refusal.

`lup.policy.enforcement` is where a :class:`Decision` becomes the answer a
session gives an attempted call. These pin the mapping no caller should
re-derive: deny refuses, ask reaches a human, defer answers nothing so the
ambient permission flow applies, and a tool family with no declared policy
asks rather than passes.
"""

from pathlib import Path

import pytest
from pydantic import AnyHttpUrl, ValidationError

from lup.providers.claude.harness import CLAUDE_DISPATCHER
from lup.providers.claude.hooks import CLAUDE_SEMANTICS
from lup.launch.declaration import InnerSandbox
from lup.devtools.harness.resolve import worker_policy_hooks
from lup.policy.hooks import LupHookInput, LupHookOutput
from lup.policy.enforcement import (
    NativeSemantics,
    SandboxPosture,
    SemanticToolPolicy,
    create_policy_hooks,
    policy_hook_output,
)
from lup.policy.kernel.decision import SANDBOX_TRAPPED_REASON, DecisionEffect
from lup.policy.kernel.diagnostic import step
from lup.policy.kernel.effects import declare
from lup.policy.shell_rules import ShellCommandRule
from lup.policy.vocabulary import runner_target_rules
from lup.types import JsonObject
from lup.policy.models import (
    Decision,
    EditBatch,
    EditChange,
    FetchUrl,
    SearchWeb,
    ShellCommand,
    ToolIdentity,
    UnknownTool,
)
from lup.policy.grants import LeaseGrants
from lup.policy.refused_tools import RefusedTool
from lup.policy.models import UrlScope
from lup.policy.rules import EditPolicy, FetchPolicy, ShellPolicy
from lup_template.harness.catalog import declared_hook_set
from tests.unit.native import claude_answer, codex_answer

SHELL_RULES = declared_hook_set().resolved_shell_rules()
"""This project's vocabulary as the runtime resolves it, not as it is declared."""

DOCS_ORIGIN = AnyHttpUrl("https://docs.example.com")
DENIED_URL = AnyHttpUrl("https://docs.example.com/private/token")


def docs_fetch_policy() -> FetchPolicy:
    return FetchPolicy(
        allowed=[UrlScope(origin=DOCS_ORIGIN, path_prefix="/api")],
        denied=[UrlScope(origin=DOCS_ORIGIN, path_prefix="/private")],
    )


def test_every_effect_maps_to_one_portable_decision() -> None:
    assert policy_hook_output(Decision(effect="allow")) == LupHookOutput(
        decision="allow"
    )
    assert policy_hook_output(
        Decision(effect="ask", reason="approval required")
    ) == LupHookOutput(decision="ask", reason="asks: approval required")
    assert policy_hook_output(
        Decision(effect="deny", reason="URL is denied")
    ) == LupHookOutput(decision="deny", reason="refused: URL is denied")
    # Defer carries no decision at all: the kernel declined to judge, so the
    # session's ambient permission flow decides instead of this hook granting
    # what nothing approved.
    deferred = policy_hook_output(Decision(effect="defer", reason="unjudged"))
    assert deferred.decision is None
    assert deferred.reason == "unjudged"


async def test_no_effect_reaches_codex_as_a_silent_allow() -> None:
    """The neutral vocabulary carries ask; Codex's approval reply has no channel
    for it, so the widening is only safe while every non-allow effect still
    refuses there — an approval nothing granted is declined."""
    answers: dict[DecisionEffect, str] = {
        "allow": "accept",
        "defer": "decline",
        "ask": "decline",
        "deny": "decline",
    }
    for effect, answer in answers.items():
        assert await codex_answer(Decision(effect=effect, reason="reason")) == answer


def test_router_sends_each_tool_to_the_policy_that_judges_it() -> None:
    router = SemanticToolPolicy(
        fetch=docs_fetch_policy(),
        shell=ShellPolicy(SHELL_RULES),
        edit=None,
    )

    assert router.decide(FetchUrl(url=DENIED_URL)).effect == "deny"
    assert (
        router.decide(FetchUrl(url=AnyHttpUrl("https://docs.example.com/api/x"))).effect
        == "allow"
    )
    assert router.decide(ShellCommand(command="git status")).effect == "allow"
    assert router.decide(ShellCommand(command="mycommand --flag")).effect == "ask"


def test_router_asks_rather_than_allows_what_no_policy_covers() -> None:
    router = SemanticToolPolicy(fetch=docs_fetch_policy())

    undeclared = router.decide(ShellCommand(command="git status"))
    assert undeclared.effect == "ask"
    assert "no shell policy is declared" in undeclared.reason

    edit = router.decide(
        EditBatch(changes=[EditChange(path=Path("module.py"), after="value = 1")])
    )
    assert edit.effect == "ask"
    assert "no edit policy is declared" in edit.reason

    assert router.decide(SearchWeb(query="anything")).effect == "ask"
    assert (
        router.decide(
            UnknownTool(identity=ToolIdentity(original_name="mcp__notes__write"))
        ).effect
        == "ask"
    )


async def test_hook_judges_an_edit_by_the_documents_it_would_produce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook splices fragments before judging, as the dispatchers do.

    Repo-relative path on purpose: the scratchpad role skips the marker
    gate, and this test needs the gate reading the spliced document.
    """
    monkeypatch.chdir(tmp_path)
    Path("content.py").write_text(
        'TABLE = """\nA note spells itself as # lup: fix this here.\n"""\n',
        encoding="utf-8",
    )
    hooks = create_policy_hooks(
        SemanticToolPolicy(edit=EditPolicy(protected=[])),
        CLAUDE_SEMANTICS,
    )
    allowed = await hooks.pre_tool_use[0].hook(
        LupHookInput(
            event="PreToolUse",
            tool_name="Edit",
            tool_input={
                "file_path": "content.py",
                "old_string": "A note spells itself as # lup: fix this here.\n",
                "new_string": "",
            },
        )
    )
    assert allowed.decision == "allow"


async def test_hook_refuses_a_denied_call_and_leaves_other_events_alone() -> None:
    hooks = create_policy_hooks(
        SemanticToolPolicy(fetch=docs_fetch_policy()),
        CLAUDE_SEMANTICS,
    )
    matcher = hooks.pre_tool_use[0]
    assert matcher.tag == "semantic_policy"

    refused = await matcher.hook(
        LupHookInput(
            event="PreToolUse",
            tool_name="WebFetch",
            tool_input={"url": str(DENIED_URL)},
        )
    )
    assert refused == LupHookOutput(decision="deny", reason="refused: URL is denied")

    after = await matcher.hook(
        LupHookInput(
            event="PostToolUse",
            tool_name="WebFetch",
            tool_input={"url": str(DENIED_URL)},
        )
    )
    assert after == LupHookOutput()


async def test_a_placement_widens_only_the_operations_that_declare_one() -> None:
    """Three verdicts from one session, and the third is what makes the rest safe.

    A profile with a host channel carries an operation that declares the host,
    runs an ordinary allow where the session already is, and refuses an
    operation no rule classified rather than carrying it anywhere. A session
    that may place one operation on the host must not thereby place the ones
    it never judged.
    """
    posture = InnerSandbox(escapable=True).enforcement()
    assert posture == SandboxPosture(active=True, escapable=True)

    hooks = create_policy_hooks(
        SemanticToolPolicy(
            shell=ShellPolicy(
                [*SHELL_RULES, *placement_rules()],
                escapable=CLAUDE_SEMANTICS.escapes_from(posture),
                interactive=False,
                runner_targets=runner_target_rules(),
            )
        ),
        CLAUDE_SEMANTICS,
        sandbox=posture,
    )

    async def placed(command: str) -> LupHookOutput:
        return await hooks.pre_tool_use[0].hook(
            LupHookInput(
                event="PreToolUse",
                tool_name="Bash",
                tool_input={"command": command},
            )
        )

    remote = await placed("remote --run")
    toolchain = await placed("uv run lup-devtools harness resolve")

    assert (remote.decision, remote.sandbox) == ("allow", "outside")
    assert (toolchain.decision, toolchain.sandbox) == ("allow", "ambient")
    unjudged = await placed("frobnicate --wildly")
    assert unjudged.decision == "deny"
    assert unjudged.sandbox == "ambient"


async def test_a_toolchain_needs_a_boundary_that_grants_paths_not_the_host() -> None:
    """What the retired ``outside`` on this toolchain was actually asking for.

    A session-opening toolchain needs the place it already runs to grant the
    runtime's configuration home. That is a statement about the profile, so it
    is declared and measured with the boundary — where a profile that cannot
    grant it fails at launch, with the gap actionable. Declared as a placement
    it was unmeasurable: the profile that grants the path and the profile that
    does not both read as ``outside``, and the second only found out at its
    first shell call, on a bare ``EROFS`` that reads like a broken repository.
    """
    posture = InnerSandbox().enforcement()
    assert posture == SandboxPosture(active=True, escapable=False)

    hooks = create_policy_hooks(
        SemanticToolPolicy(
            shell=ShellPolicy(
                SHELL_RULES,
                sandbox_active=posture.active,
                escapable=CLAUDE_SEMANTICS.escapes_from(posture),
                interactive=False,
                runner_targets=runner_target_rules(),
            )
        ),
        CLAUDE_SEMANTICS,
        sandbox=posture,
    )
    ran = await hooks.pre_tool_use[0].hook(
        LupHookInput(
            event="PreToolUse",
            tool_name="Bash",
            tool_input={"command": "uv run lup-devtools harness resolve"},
        )
    )

    assert (ran.decision, ran.sandbox) == ("allow", "ambient")


async def test_an_operation_needing_a_channel_this_session_lacks_is_blocked() -> None:
    """The composition is read from the session, not from the runtime alone.

    Read from the runtime alone, a session whose settings forbid unsandboxed
    commands was still handed the placement — rendered onto the wire, dropped
    without a word, and the operation left to die on whatever it touched
    first. Composed from the session, the pair says no, and the refusal names
    the missing channel rather than reading as a rule's judgement: no approval
    builds a channel, so no reviewer is shown the question.
    """
    posture = InnerSandbox().enforcement()
    assert CLAUDE_SEMANTICS.escapable
    assert not CLAUDE_SEMANTICS.escapes_from(posture)

    hooks = create_policy_hooks(
        SemanticToolPolicy(
            shell=ShellPolicy(
                placement_rules(),
                sandbox_active=posture.active,
                escapable=CLAUDE_SEMANTICS.escapes_from(posture),
                interactive=False,
                runner_targets=runner_target_rules(),
            )
        ),
        CLAUDE_SEMANTICS,
        sandbox=posture,
    )
    stopped = await hooks.pre_tool_use[0].hook(
        LupHookInput(
            event="PreToolUse", tool_name="Bash", tool_input={"command": "remote --run"}
        )
    )

    assert stopped.decision == "deny"
    assert stopped.reason == f"refused: {SANDBOX_TRAPPED_REASON}"
    assert stopped.sandbox == "ambient"


async def test_a_worker_is_judged_by_the_composition_a_run_actually_builds() -> None:
    """The judge itself, not a restatement of it — this is where it went wrong.

    Every verdict the kernel reached was already correct; what shipped a
    widening was the composition handing it host facts the session did not
    have. So this builds the worker's judge rather than a policy shaped like
    it: the toolchain runs where the session runs, a guarded verb parks a
    durable question reaching whoever supervises the run, and an escalation
    marker parks the same question rather than being the way to avoid one.

    A worker is non-interactive and not therefore alone, which is the whole of
    why those are questions. What stays a refusal is what no reviewer could
    answer: text the classifier could not read, where the person would be
    approving the readable half of something else entirely.
    """
    hooks = worker_policy_hooks(
        declared_hook_set(),
        LeaseGrants(),
        CLAUDE_SEMANTICS,
        InnerSandbox(escapable=True).enforcement(),
    )

    async def judged(command: str) -> LupHookOutput:
        return await hooks.pre_tool_use[0].hook(
            LupHookInput(
                event="PreToolUse",
                tool_name="Bash",
                tool_input={"command": command},
            )
        )

    toolchain = await judged("uv run lup-devtools harness resolve")
    assert (toolchain.decision, toolchain.sandbox) == ("allow", "ambient")
    for parked in (
        "find . -delete",
        "git push --delete origin feat",
        "# lup: escalate: I would rather not be asked\nsudo rm -rf /var/tmp/x",
    ):
        assert (await judged(parked)).decision == "ask", parked
    for refused in ('eval "$COMMAND"', "sh -c 'rm -rf /'"):
        assert (await judged(refused)).decision == "deny", refused


def placement_rules() -> list[ShellCommandRule]:
    """One command that follows the session, one confined, one on the host."""
    return [
        ShellCommandRule(
            name="checker",
            effects=[declare("reads_path", scope="project")],
            sandbox="ambient",
        ),
        ShellCommandRule(
            name="confined",
            effects=[declare("reads_path", scope="project")],
            sandbox="inside",
        ),
        ShellCommandRule(
            name="remote",
            effects=[declare("reads_path", scope="project")],
            sandbox="outside",
        ),
    ]


def test_a_stated_placement_is_not_the_session_read_back() -> None:
    """The case where standing one placement in for the other is invisible.

    ``ambient`` reads the placement off the session, so an unconfined session
    runs the operation on the host without anybody choosing that; ``inside``
    confines it whatever the session is doing. The two agree only while the
    session is already confined — which is why this one is not, and why the
    assertion is on the arguments the call actually runs with rather than on
    the word in the verdict.
    """
    policy = ShellPolicy(placement_rules(), sandbox_active=False, escapable=True)

    following = policy.decide(ShellCommand(command="checker --run"))
    held = policy.decide(ShellCommand(command="confined --run"))

    assert (following.sandbox, held.sandbox) == ("ambient", "inside")

    call: JsonObject = {"command": "confined --run"}
    assert claude_answer(following, call).get("updatedInput") is None
    assert claude_answer(held, call).get("updatedInput") == {
        **call,
        "dangerouslyDisableSandbox": False,
    }


def test_an_inside_placement_survives_a_call_that_asked_to_leave() -> None:
    """Provider auto-mode and a self-set native flag are both overwritten.

    An operation Lup places inside stays inside. The agent's route to the
    host is a reviewed marker, and a flag it set on its own call is not that
    request — honouring it would make the placement advisory exactly where it
    is load-bearing.
    """
    policy = ShellPolicy(placement_rules(), sandbox_active=False, escapable=True)
    asked_out: JsonObject = {
        "command": "confined --run",
        "dangerouslyDisableSandbox": True,
    }

    rendered = claude_answer(
        policy.decide(ShellCommand(command="confined --run")), asked_out
    )

    assert rendered.get("updatedInput") == {
        **asked_out,
        "dangerouslyDisableSandbox": False,
    }
    assert rendered.get("permissionDecision") == "allow"


def test_asking_for_the_host_produces_a_question_rather_than_a_placement() -> None:
    """The crossing is reviewed however ordinary the operation reads inside.

    What the reviewer is being shown is not "may this run" but "may this run
    *there*", and the second question has an answer of its own.
    """
    policy = ShellPolicy(placement_rules(), sandbox_active=False, escapable=True)

    asked = policy.decide(
        ShellCommand(
            command="# lup: escalate[sandbox]: the host holds it\nchecker --run"
        )
    )

    assert (asked.effect, asked.sandbox) == ("ask", "outside")


def test_a_runtime_with_no_channel_is_handed_the_plain_effect() -> None:
    """An intent the runtime will not honour must not be spelled.

    Spelled anyway, the verdict reads as placed while the operation runs
    wherever the session already was — which is the one substitution the
    placement axis exists to prevent.
    """
    policy = ShellPolicy(placement_rules(), sandbox_active=False, escapable=True)
    placed = policy.decide(ShellCommand(command="confined --run"))

    assert placed.placed(escapable=False).sandbox == "ambient"


def test_a_line_that_has_to_leave_outranks_one_that_merely_follows() -> None:
    """One command line is one process, so its segments cannot be placed apart.

    A segment that has to stay inside keeps the whole line inside, which is
    the conservative direction; only a line where something needs the host
    and nothing needs the boundary leaves.
    """
    policy = ShellPolicy(placement_rules(), sandbox_active=False, escapable=True)

    def placed(command: str) -> str:
        return policy.decide(ShellCommand(command=command)).sandbox

    assert placed("checker --run && remote --run") == "outside"
    assert placed("remote --run && confined --run") == "inside"
    assert placed("checker --run && checker --run") == "ambient"


def test_hook_is_scoped_to_the_tools_the_policy_has_rules_for() -> None:
    """A tool with no rule surface must never reach this hook.

    Composed beside a directory ACL, an unscoped hook answers ``ask`` for
    every read the ACL allows, and ``ask`` outranks ``allow`` — which is
    what denied resolver workers their own leases. The matcher is the same
    list the generated plugin registers, so both paths judge one set.
    """
    hooks = create_policy_hooks(
        SemanticToolPolicy(fetch=docs_fetch_policy()),
        CLAUDE_SEMANTICS,
    )
    matched = hooks.pre_tool_use[0].matcher or ""

    assert set(matched.split("|")) == set(CLAUDE_DISPATCHER.routed_tools)
    for unruled in ("Read", "Skill", "mcp__lup-output__submit_output"):
        assert unruled not in matched.split("|")


def test_a_refusal_is_what_widens_the_routed_set() -> None:
    """A refused tool is routed, and refusing none routes nothing extra.

    Both halves matter. Unrouted, a declared refusal judges nothing and the
    reflex it exists to stop goes through; routed by a runtime that merely
    anticipates the name, every adopter refusing nothing pays the
    unclassified ``ask`` for a tool they have no opinion about.
    """
    refusal = RefusedTool(
        tool="Artifact",
        reason="publishing leaves the repository",
        recovery=[step("write the report under tmp/ instead")],
    )

    assert "Artifact" not in CLAUDE_SEMANTICS.routed_tools
    assert "Artifact" in CLAUDE_SEMANTICS.also_refusing([refusal]).routed_tools
    assert CLAUDE_SEMANTICS.also_refusing([]).routed_tools == (
        CLAUDE_SEMANTICS.routed_tools
    )


def test_a_refused_tool_is_routed_exactly_once() -> None:
    """A refusal narrowed by specifier still names one tool to register."""
    refusals = [
        RefusedTool(
            tool="Skill",
            specifier="artifact-design",
            reason="leaves it",
            recovery=[step("stay in the repository")],
        ),
        RefusedTool(
            tool="Skill",
            specifier="page-design",
            reason="leaves it too",
            recovery=[step("stay in the repository")],
        ),
    ]

    routed = CLAUDE_SEMANTICS.also_refusing(refusals).routed_tools

    assert routed.count("Skill") == 1


def test_refusing_a_tool_the_runtime_decodes_is_refused_outright() -> None:
    """An unreachable refusal is a declaration that lies about being in force.

    ``Bash`` is judged by the shell lattice before the table is ever consulted,
    so a row naming it would read as a ban while every command went through.
    """
    with pytest.raises(ValueError, match="answer first"):
        CLAUDE_SEMANTICS.also_refusing(
            [
                RefusedTool(
                    tool="Bash",
                    reason="shells leave the repository",
                    recovery=[step("stay in the repository")],
                )
            ]
        )


def test_a_decoder_cannot_be_enforced_over_no_tools_at_all() -> None:
    """The scope is carried with the decoder, and an empty one is refused.

    Handing the routed set in separately let a caller supply a decoder and
    no scope, which reads as configuration and silently inverts the
    enforcement: matched against nothing the hook judges every call, and the
    conservative ``ask`` an unclassified tool earns outranks the ``allow``
    beside it. There is no such pair to construct.
    """
    assert CLAUDE_SEMANTICS.routed_tools == CLAUDE_DISPATCHER.routed_tools

    with pytest.raises(ValidationError):
        NativeSemantics(decode=CLAUDE_SEMANTICS.decode, routed_tools=[])
