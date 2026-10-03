"""Translate backend-neutral Lup hooks to Claude SDK hook handlers."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from lup.providers.claude.native import (
    ClaudeEventDecoder,
    ClaudeHookPayload,
    claude_sandbox_input,
    parse_claude_before_tool,
)
from lup.policy.hooks import (
    LupHookEvent,
    LupHookInput,
    LupHookMatcher,
    LupHookOutput,
    LupHooksConfig,
)
from lup.providers.claude.harness import CLAUDE_DISPATCHER
from lup.policy.enforcement import NativeSemantics, unjudged_output
from lup.policy.kernel.decision import SandboxPlacement
from lup.policy.models import SemanticTool
from lup.types import JsonObject, ToolName
from lup.workspace.paths import extract_glob_dir

if TYPE_CHECKING:
    import claude_agent_sdk as claude
    from claude_agent_sdk import types as claude_types

logger = logging.getLogger(__name__)


def claude_hook_tool_path(tool_name: ToolName, tool_input: JsonObject) -> str:
    """Normalize a path-bearing Claude tool request for portable hooks."""
    match tool_name, tool_input:
        case ("Write" | "Edit" | "Read", {"file_path": path}):
            return str(path)
        case ("Grep", {"path": path}):
            return str(path)
        case ("Glob", {"path": path}) if path:
            return str(path)
        case ("Glob", {"pattern": pattern}):
            return extract_glob_dir(str(pattern))
        case _:
            return ""


def claude_hook_semantic_tool(event: LupHookInput) -> SemanticTool:
    """Decode one in-process hook event into the tool a semantic policy judges.

    The generated dispatchers decode the same names and payload fields from a
    subprocess hook; this is that decode for a session whose hooks run
    in-process, so both enforcement paths judge one vocabulary.
    """
    payload = ClaudeHookPayload(
        tool_name=event.tool_name,
        tool_input=event.tool_input,
        cwd=Path(event.cwd) if event.cwd else None,
    )
    return ClaudeEventDecoder().decode(parse_claude_before_tool(payload)).tool


CLAUDE_SEMANTICS = NativeSemantics(
    decode=claude_hook_semantic_tool,
    routed_tools=CLAUDE_DISPATCHER.routed_tools,
    escapable=True,
)
"""What an in-process Claude session hands a semantic policy.

The routed set is the dispatcher's own, so the tools the plugin registers
the hook for and the tools this path enforces over cannot drift apart.
Claude Code answers yes to both sandbox questions from one field of one
schema: a verdict places a call by writing ``dangerouslyDisableSandbox``
through the rewrite channel :func:`claude_placed_input` spells, and an agent
takes its own call out by setting that same field on the call it makes.
"""


def claude_placed_input(
    tool_name: ToolName, tool_input: JsonObject, sandbox: SandboxPlacement
) -> JsonObject | None:
    """One in-process call's rewrite, for the one tool that takes the argument.

    Every other call is placed by the session it runs in, so a placement on
    one has nowhere to go and is dropped rather than written into arguments
    the tool would not read.
    """
    if tool_name != "Bash":
        return None
    return claude_sandbox_input(tool_input, sandbox)


def build_claude_hook_handler(
    matcher: LupHookMatcher,
    *,
    event: LupHookEvent,
) -> Callable[
    [claude.HookInput, str | None, claude_types.HookContext],
    Awaitable[claude_types.SyncHookJSONOutput],
]:
    """Close one native handler over a typed portable hook callback.

    A PreToolUse callback that raises is answered with a refusal rather than
    passed on as the error: Claude Code runs the tool past a callback that
    failed (measured on 2.1.259 and 2.1.285), and a gate that failed has not
    let anything through.
    """

    async def claude_hook(
        input_data: claude.HookInput,
        _tool_use_id: str | None,
        _context: claude_types.HookContext,
    ) -> claude_types.SyncHookJSONOutput:
        tool_name = input_data["tool_name"] if "tool_name" in input_data else ""
        tool_input = input_data["tool_input"] if "tool_input" in input_data else {}
        response = input_data["tool_response"] if "tool_response" in input_data else ""
        tool_result = (
            response if isinstance(response, str) else json.dumps(response, default=str)
        )
        stop_hook_active = (
            input_data["stop_hook_active"]
            if "stop_hook_active" in input_data
            else False
        )
        cwd = input_data["cwd"] if "cwd" in input_data else ""
        given = LupHookInput(
            event=event,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_path=claude_hook_tool_path(tool_name, tool_input),
            tool_result=tool_result,
            cwd=cwd,
            stop_hook_active=stop_hook_active,
        )
        try:
            output = await matcher.hook(given)
        except Exception as error:
            if event != "PreToolUse":
                raise
            logger.exception("refusing %s: its PreToolUse hook raised", tool_name)
            output = unjudged_output(error)
        rendered = lup_hook_output_to_claude(
            output,
            event=event,
            placed_input=claude_placed_input(tool_name, tool_input, output.sandbox),
        )
        if output.system_message is not None:
            rendered["systemMessage"] = output.system_message
        if event == "PostToolUse" and output.additional_context:
            rendered["hookSpecificOutput"] = {
                "hookEventName": "PostToolUse",
                "additionalContext": output.additional_context,
            }
        output.delivered()
        return rendered

    return claude_hook


def lup_hooks_to_claude(
    hooks: LupHooksConfig,
) -> dict[claude_types.HookEvent, list[claude_types.HookMatcher]]:
    """Convert portable hook registrations without importing the SDK eagerly."""
    from claude_agent_sdk import types as claude_types

    def native_matcher(
        matcher: LupHookMatcher, event: LupHookEvent
    ) -> claude_types.HookMatcher:
        handler = build_claude_hook_handler(matcher, event=event)
        return claude_types.HookMatcher(
            matcher=matcher.matcher, hooks=[handler], timeout=matcher.timeout
        )

    return {
        event: [native_matcher(matcher, event) for matcher in matchers]
        for event, matchers in hooks.by_event().items()
    }


def lup_hook_output_to_claude(
    output: LupHookOutput,
    *,
    event: LupHookEvent = "PreToolUse",
    placed_input: JsonObject | None = None,
) -> claude_types.SyncHookJSONOutput:
    """Render a portable hook result into the matching Claude hook shape.

    PreToolUse answers on the permission channel and only there, so a gate's
    block reaches the agent as a refusal carrying its corrective message.
    Every other event answers on the top-level decision channel, where a
    verdict that cannot be represented fails closed as a block.

    A rewrite rides the undecided path: it carries corrected arguments while
    leaving the verdict to the ambient permission flow, so fixing a call
    never doubles as granting it.

    ``placed_input`` is the separate case: a verdict that also says where the
    call runs, which Claude Code takes as an argument, so the decision and the
    rewrite go out together.

    A verdict carrying something for the agent rather than for whoever the
    permission channel reaches goes out on the context channel beside it, and
    on both the effects a placement survives — an approval question puts its
    reason to a human, which is no more the agent than a grant's record is.
    The verdict's own offer decides what that is.
    """
    from claude_agent_sdk import types as claude_types

    match event, output.decision:
        case "PreToolUse", None if output.updated_input is not None:
            return claude_types.SyncHookJSONOutput(
                hookSpecificOutput=claude_types.PreToolUseHookSpecificOutput(
                    hookEventName="PreToolUse",
                    updatedInput=output.updated_input,
                )
            )
        case "PreToolUse", None if output.additional_context:
            return claude_types.SyncHookJSONOutput(
                hookSpecificOutput=claude_types.PreToolUseHookSpecificOutput(
                    hookEventName="PreToolUse",
                    additionalContext=output.additional_context,
                )
            )
        case "PreToolUse", "allow" if (
            placed_input is not None and output.additional_context
        ):
            return claude_types.SyncHookJSONOutput(
                hookSpecificOutput=claude_types.PreToolUseHookSpecificOutput(
                    hookEventName="PreToolUse",
                    permissionDecision="allow",
                    updatedInput=placed_input,
                    additionalContext=output.additional_context,
                )
            )
        case "PreToolUse", "allow" if placed_input is not None:
            return claude_types.SyncHookJSONOutput(
                hookSpecificOutput=claude_types.PreToolUseHookSpecificOutput(
                    hookEventName="PreToolUse",
                    permissionDecision="allow",
                    updatedInput=placed_input,
                )
            )
        case "PreToolUse", "allow" if output.additional_context:
            return claude_types.SyncHookJSONOutput(
                hookSpecificOutput=claude_types.PreToolUseHookSpecificOutput(
                    hookEventName="PreToolUse",
                    permissionDecision="allow",
                    additionalContext=output.additional_context,
                )
            )
        case "PreToolUse", "allow":
            return claude_types.SyncHookJSONOutput(
                hookSpecificOutput=claude_types.PreToolUseHookSpecificOutput(
                    hookEventName="PreToolUse",
                    permissionDecision="allow",
                )
            )
        case "PreToolUse", "ask" if (
            placed_input is not None and output.additional_context
        ):
            return claude_types.SyncHookJSONOutput(
                hookSpecificOutput=claude_types.PreToolUseHookSpecificOutput(
                    hookEventName="PreToolUse",
                    permissionDecision="ask",
                    permissionDecisionReason=output.reason,
                    updatedInput=placed_input,
                    additionalContext=output.additional_context,
                )
            )
        case "PreToolUse", "ask" if placed_input is not None:
            return claude_types.SyncHookJSONOutput(
                hookSpecificOutput=claude_types.PreToolUseHookSpecificOutput(
                    hookEventName="PreToolUse",
                    permissionDecision="ask",
                    permissionDecisionReason=output.reason,
                    updatedInput=placed_input,
                )
            )
        case "PreToolUse", "ask" if output.additional_context:
            return claude_types.SyncHookJSONOutput(
                hookSpecificOutput=claude_types.PreToolUseHookSpecificOutput(
                    hookEventName="PreToolUse",
                    permissionDecision="ask",
                    permissionDecisionReason=output.reason,
                    additionalContext=output.additional_context,
                )
            )
        case "PreToolUse", "ask":
            return claude_types.SyncHookJSONOutput(
                hookSpecificOutput=claude_types.PreToolUseHookSpecificOutput(
                    hookEventName="PreToolUse",
                    permissionDecision="ask",
                    permissionDecisionReason=output.reason,
                )
            )
        case "PreToolUse", "deny" | "block":
            return claude_types.SyncHookJSONOutput(
                hookSpecificOutput=claude_types.PreToolUseHookSpecificOutput(
                    hookEventName="PreToolUse",
                    permissionDecision="deny",
                    permissionDecisionReason=output.reason,
                )
            )
        case _, "ask" | "deny" | "block":
            return claude_types.SyncHookJSONOutput(
                decision="block", reason=output.reason
            )
        case _:
            if output.system_message is not None:
                return claude_types.SyncHookJSONOutput(
                    systemMessage=output.system_message
                )
            return claude_types.SyncHookJSONOutput()
