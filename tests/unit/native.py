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
"""

import json

import sh

from lup.policy.relay import PersistentQuestion


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


def bound(question: PersistentQuestion) -> PersistentQuestion:
    """The same question under the fingerprint a native hook binds its record to."""
    return question.model_copy(update={"fingerprint": question.native_fingerprint()})
