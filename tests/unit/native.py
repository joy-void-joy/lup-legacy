"""Reading what the generated dispatchers answered, and parking as they park.

Codex has two refusal channels and the dispatcher picks between them by what
the refusal is for. A call the review queue parked answers on stdout, as a
structured denial carrying the agent's reason and, beside it, the line the
operator is shown, because Codex drops `systemMessage` when a hook exits 2.
Every other refusal takes exit 2, and a permitted call says nothing at all —
so exit status alone cannot tell an allow from a parked review, and each
suite that asked it separately got a different answer.

Claude's refusals all take the one structured channel; a parked one is the
refusal that carries the operator's line as `systemMessage`, which is what
:func:`claude_effect` reads as the question it is.

A session opened in process answers through its adapter's hooks instead, and
:func:`claude_answer` and :func:`codex_answer` read one verdict through the
same composition a policy hook runs: placed for what the runtime can place,
then rendered on its native channel.
"""

import json

import sh
from pydantic import TypeAdapter

from lup.policy.enforcement import policy_hook_output
from lup.policy.hooks import LupHookInput, LupHookMatcher, LupHookOutput, LupHooksConfig
from lup.policy.models import Decision
from lup.policy.relay import PersistentQuestion
from lup.providers.claude.hooks import claude_placed_input, lup_hook_output_to_claude
from lup.providers.codex.hooks import COMMAND_APPROVAL, CodexApprovalResponder
from lup.types import JsonObject


def codex_effect(result: sh.RunningCommand) -> str:
    """``allow`` or ``deny``, over whichever channel this refusal took."""
    if result.exit_code == 2:
        return "deny"
    if not result.stdout:
        return "allow"
    return str(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"])


def codex_denial(result: sh.RunningCommand) -> str:
    """The refused call's agent-facing reason, from either channel."""
    if result.exit_code == 2:
        return result.stderr.decode()
    assert result.exit_code == 0
    rendered = json.loads(result.stdout)
    output = rendered["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert output["hookEventName"] == "PreToolUse"
    assert rendered["systemMessage"]
    return str(output["permissionDecisionReason"])


def claude_effect(rendered: dict) -> str:
    """``ask`` for a call the review queue parked, else the permission decision."""
    output = rendered["hookSpecificOutput"]
    if output["permissionDecision"] == "deny" and "systemMessage" in rendered:
        return "ask"
    return str(output["permissionDecision"])


def claude_answer(decision: Decision, call: JsonObject | None = None) -> JsonObject:
    """The PreToolUse answer an in-process Claude session sends for one shell verdict.

    Empty where the verdict defers, since the session's own permission mode
    decides then and the hook says nothing.
    """
    output = policy_hook_output(decision, escapable=True)
    rendered = TypeAdapter(JsonObject).validate_python(
        lup_hook_output_to_claude(
            output, placed_input=claude_placed_input("Bash", call or {}, output.sandbox)
        )
    )
    match rendered.get("hookSpecificOutput", {}):
        case dict() as answer:
            return answer
        case other:
            raise AssertionError(f"not a PreToolUse answer: {other!r}")


async def codex_answer(decision: Decision) -> str:
    """What an in-process Codex session answers a command approval with, for one verdict."""

    async def judged(_: LupHookInput) -> LupHookOutput:
        return policy_hook_output(decision)

    responder = CodexApprovalResponder(
        hooks=LupHooksConfig(
            pre_tool_use=[LupHookMatcher(matcher=COMMAND_APPROVAL, hook=judged)]
        )
    )
    return await responder.decide(COMMAND_APPROVAL, {"command": "ls"})


def bound(question: PersistentQuestion) -> PersistentQuestion:
    """The same question under the fingerprint a native hook binds its record to."""
    return question.model_copy(update={"fingerprint": question.native_fingerprint()})
