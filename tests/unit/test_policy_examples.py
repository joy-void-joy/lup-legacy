"""The shipped policy examples refuse, driven as a session would drive them.

`examples/semantic_policy` and `examples/semantic_policy_shell` claim that a
denied call never reaches the provider. Each example's own session
configuration is built here and its registered hooks are invoked with the
payloads the SDK sends — every step of a live session except the model call.
An example nothing executes decays into a wrong demonstration, so the claim
is checked on each run instead of the day a reader tries it.
"""

from typing import Literal

from claude_agent_sdk import types as claude_types

from examples import semantic_policy, semantic_policy_shell
from lup.providers.claude import Claude
from lup.providers.claude.runtime import build_claude_options
from lup.policy.kernel.decision import ESCALATE_HINT
from lup.policy.kernel.diagnostic import way
from lup.types import JsonObject

ALLOWED_URL = "https://docs.example.com/api/runtime"


async def attempted_call(
    config: Claude, tool_name: str, tool_input: JsonObject
) -> claude_types.HookJSONOutput:
    """Answer one attempted tool call through the session's own hooks.

    Neither example declares an MCP server, so the session builds none.
    """
    assert config.tools.mcp == []
    options = build_claude_options(
        config, servers={}, binding=lambda: None, resume=None, session_id=None
    )
    assert options.hooks is not None, "the example session registers no hooks"
    payload = claude_types.PreToolUseHookInput(
        hook_event_name="PreToolUse",
        session_id="session",
        transcript_path="/transcript",
        cwd="/cwd",
        tool_name=tool_name,
        tool_input=dict(tool_input),
        tool_use_id="use-1",
    )
    decisions = [
        await hook(payload, "use-1", claude_types.HookContext(signal=None))
        for matcher in options.hooks["PreToolUse"]
        for hook in matcher.hooks
    ]
    assert len(decisions) == 2
    return decisions[0] or decisions[1]


def permission(
    decision: Literal["allow", "ask", "deny"], reason: str
) -> claude_types.SyncHookJSONOutput:
    """The whole native answer, so a verdict on another channel fails here."""
    return claude_types.SyncHookJSONOutput(
        hookSpecificOutput=claude_types.PreToolUseHookSpecificOutput(
            hookEventName="PreToolUse",
            permissionDecision=decision,
            permissionDecisionReason=reason,
        )
    )


async def test_fetch_example_refuses_the_url_its_policy_denies() -> None:
    decision = await attempted_call(
        semantic_policy.session_config(),
        "WebFetch",
        {"url": semantic_policy.DENIED_URL},
    )

    assert decision == permission("deny", "refused: URL is denied")


async def test_fetch_example_allows_the_scope_it_declares() -> None:
    decision = await attempted_call(
        semantic_policy.session_config(), "WebFetch", {"url": ALLOWED_URL}
    )

    assert decision == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


async def test_fetch_example_denies_a_family_it_never_granted() -> None:
    decision = await attempted_call(
        semantic_policy.session_config(), "Bash", {"command": "git status"}
    )

    assert decision == permission(
        "deny",
        "Tool 'Bash' is outside this session's declared tools.",
    )


async def test_shell_example_refuses_the_command_its_policy_denies() -> None:
    decision = await attempted_call(
        semantic_policy_shell.session_config(),
        "Bash",
        {"command": semantic_policy_shell.DENIED_COMMAND},
    )

    assert decision == permission(
        "deny", "\n".join(["refused: URL is denied", *map(way, ESCALATE_HINT)])
    )


async def test_shell_example_allows_a_read_only_command() -> None:
    decision = await attempted_call(
        semantic_policy_shell.session_config(), "Bash", {"command": "ls -la"}
    )

    # An allow grants and says nothing further: the portable vocabulary
    # carries a reason only where one changes what the agent does next, and a
    # command whose rule states no placement is not rewritten either.
    assert decision == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


async def test_shell_example_runs_a_git_read_where_the_session_runs() -> None:
    """`git` states its placement once, and what it states is ambient.

    What git needs is a route to the remote and the repository's own locks,
    which the boundary declares and a launch measures — not the launcher's
    host, which is what a placement would be requesting and what a
    reviewer would then answer for every ordinary read. So the verdict states
    no placement, and nothing is rewritten onto the call.
    """
    command = "git status"

    decision = await attempted_call(
        semantic_policy_shell.session_config(), "Bash", {"command": command}
    )

    assert decision == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


async def test_shell_example_asks_before_a_destructive_command() -> None:
    decision = await attempted_call(
        semantic_policy_shell.session_config(), "Bash", {"command": "rm -rf build"}
    )

    assert decision == permission(
        "ask", "asks: `rm` — deleting files requires approval"
    )
