"""Codex's half of the caller hook: which conversation made a tool call.

Shipped verbatim into the plugin's ``hooks/runtime/``, where the caller hook's
generated entry runs it and the compiled permission dispatcher imports it. It
holds only what Codex spells for itself: the ``PreToolUse`` event, the
payload's keys, where the runtime keeps what a spawn was called, and the
output envelope. What a caller is and how it rides in a call are the store's.

A coordination call arriving at a tool server says nothing about who made it.
Measured on 0.158.0 with a probe server logging every request: a subagent's
calls reach a server process of its own, which the same Codex process starts
at the subagent's first tool call under the session's own environment and
leaves running after the subagent stops — so no server's environment names
its caller. The payload does: measured on 0.155.1 and again on 0.158.0, a
subagent's tool events carry its ``agent_id`` — its own thread's id — and
``agent_type`` beside the session's ``session_id``, and the parent's carry
neither.

How Codex applies the rewrite, measured on 0.158.0: an ``updatedInput``
beside ``permissionDecision: "allow"`` reached the server as the call's whole
arguments, from the session and from a subagent alike, even for a tool whose
schema sets ``additionalProperties: false``; the same rewrite with no decision
was dropped, and the call ran as the model wrote it with nothing in the exec
stream, its stderr or the rollout saying so. So the two travel together. The
``allow`` settles nothing else: the coordination servers are declared with
their tools approved already.

What the spawn called the subagent is in no payload, but it heads the
subagent's own rollout, which every event fired inside it names as
``transcript_path``. Measured on 0.158.0: the rollout's first line is a
``session_meta`` whose ``id`` is the subagent's ``agent_id`` and whose
``agent_path`` is ``/root/<task_name>`` — the name the spawn went out with,
after whatever its own ``PreToolUse`` hook rewrote. The name is that path's
last part. An opening naming another thread — the session's rollout, which
``SubagentStop`` hands as ``transcript_path`` — names nobody.

Every failure is silence: a call left unstamped acts as the session, which is
what every call did before there was anything to stamp.
"""

import json
import sys
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict

from coordination.store import Caller, called_by, text
from kernel.policy_protocol import WireValue


class Payload(TypedDict, total=False):
    """What a tool event hands a hook, as far as the caller is read from it."""

    hook_event_name: str
    tool_input: dict[str, WireValue]
    transcript_path: str
    agent_id: str
    agent_type: str
    cwd: str


class Thread(TypedDict, total=False):
    """What a rollout's opening says about its thread, as far as this reads."""

    id: str
    agent_path: str


class Opening(TypedDict, total=False):
    """A rollout's first line, a ``session_meta`` record naming its thread."""

    type: str
    payload: Thread


class Rewritten(TypedDict):
    hookEventName: str
    permissionDecision: Literal["allow"]
    updatedInput: dict[str, WireValue | Caller]


class Rewrite(TypedDict):
    """The answer: the same call with its caller written in."""

    hookSpecificOutput: Rewritten


def spawned_name(transcript: str, agent: str) -> str:
    """What the spawn called this subagent, blank where its own rollout does not say."""
    if not transcript or not agent:
        return ""
    try:
        with open(transcript, encoding="utf-8") as rollout:
            opening: Opening = json.loads(rollout.readline())
    except (OSError, ValueError):
        return ""
    if not isinstance(opening, dict) or opening.get("type") != "session_meta":
        return ""
    thread = opening.get("payload")
    if not isinstance(thread, dict) or text(thread.get("id")) != agent:
        return ""
    return PurePosixPath(text(thread.get("agent_path"))).name


def transcript_of(payload: Payload) -> Path | None:
    """The rollout of the conversation that made this call, where one is named.

    Every event fired inside a subagent names the subagent's own rollout,
    measured on 0.158.0, so the one named is the caller's.
    """
    transcript = text(payload.get("transcript_path"))
    return Path(transcript) if transcript else None


def said_in(part: WireValue) -> str:
    """The text one part of an assistant message holds, blank for any other part."""
    match part:
        case {"type": "output_text", "text": str(said)}:
            return said
        case _:
            return ""


def spoken(record: dict[str, WireValue]) -> str | None:
    """One rollout line, as far as the words an agent says before a call go.

    An assistant message's text is what the agent wrote, and a user message
    or a tool's output what it heard, before which nothing it said is about
    the call. Every other line -- its reasoning, the calls themselves, the
    events a rollout keeps beside the conversation -- says nothing, so the
    words read the same whether or not the call's own line is written yet.
    """
    match record:
        case {
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": list(parts)},
        }:
            return "".join(said_in(part) for part in parts)
        case {"type": "response_item", "payload": {"type": "message", "role": "user"}}:
            return None
        case {
            "type": "response_item",
            "payload": {"type": "function_call_output" | "custom_tool_call_output"},
        }:
            return None
        case _:
            return ""


def caller_of(payload: Payload) -> Caller:
    """The conversation one tool event came from, blank for the session's own."""
    agent = text(payload.get("agent_id"))
    return Caller(
        agent_id=agent,
        agent_type=text(payload.get("agent_type")),
        cwd=text(payload.get("cwd")),
        name=spawned_name(text(payload.get("transcript_path")), agent),
    )


def decided(payload: Payload) -> Rewrite | None:
    """The rewritten call, or nothing for an event this hook has no answer to."""
    if payload.get("hook_event_name") != "PreToolUse":
        return None
    return Rewrite(
        hookSpecificOutput=Rewritten(
            hookEventName="PreToolUse",
            permissionDecision="allow",
            updatedInput=called_by(payload.get("tool_input", {}), caller_of(payload)),
        )
    )


def main() -> None:
    """Answer the event on stdin, or say nothing and let the call through as it was."""
    try:
        payload: Payload = json.load(sys.stdin)
        answer = decided(payload)
    except Exception:
        return
    if answer is not None:
        print(json.dumps(answer))
