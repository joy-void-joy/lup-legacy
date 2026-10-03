"""Claude Code's half of the caller hook: which conversation made a tool call.

Shipped verbatim into the plugin's ``hooks/runtime/``, where the caller hook's
generated entry reads each event, asks :func:`decided` and prints its answer,
and the compiled permission dispatcher imports it. It
holds only what Claude Code spells for itself: the ``PreToolUse`` event, the
payload's keys, where the runtime keeps what a spawn was called, and the
output envelope. What a caller is and how it rides in a call are the store's.

One tool server serves every conversation of a session — its own and each
native subagent it dispatches — so a coordination call arriving there says
nothing about who made it. The payload does. Measured on 2.1.283 with a probe
tool server logging each call it received, and a hook returning
``updatedInput`` and no ``permissionDecision``:

- the parent's and a subagent's calls reached the one server process, and
  the request's ``_meta`` carried only ``claudecode/toolUseId`` and a
  progress token, never the caller;
- every tool event fired inside the subagent carried its ``agent_id`` and
  ``agent_type``, and the parent's carried neither;
- the rewrite reached the server for both, even for a tool whose schema sets
  ``additionalProperties: false``, so no decision needs to ride with it and
  the call keeps whatever permission the project's settings gave it.

What the spawn called the subagent is in no payload. Claude Code writes it to
``<transcript without .jsonl>/subagents/agent-<agent_id>.meta.json`` under
``name``, beside the spawn's ``description``, ``agentType`` and
``toolUseId`` — measured on 2.1.283, where the file was absent at
``SubagentStart`` and present from the subagent's first tool call, and where a
spawn carrying no ``name`` wrote none. ``transcript_path`` names the parent's
transcript in every subagent event, which is what the path is read from.

The same record says which subagent spawned this one, where another subagent
did: a fork's carries ``parentAgentId``, that subagent's own id, beside
``isFork`` and a ``spawnDepth`` of two or more, and a subagent the session
itself spawned carries none — read off 2.1.285's records of an orchestrating
session's subagents and the forks they spawned.

Every failure is silence: a call left unstamped acts as the session, which is
what every call did before there was anything to stamp.
"""

from pathlib import Path
from typing import TypedDict

from coordination.store import Caller, called_by, loaded, text
from kernel.policy_protocol import WireValue


class Payload(TypedDict, total=False):
    """What a tool event hands a hook, as far as the caller is read from it."""

    hook_event_name: str
    tool_input: dict[str, WireValue]
    transcript_path: str
    agent_id: str
    agent_type: str
    cwd: str


class Spawned(TypedDict, total=False):
    """What the runtime recorded about one subagent's spawn, as far as this reads."""

    name: str
    parentAgentId: str


class Rewritten(TypedDict):
    hookEventName: str
    updatedInput: dict[str, WireValue | Caller]


class Rewrite(TypedDict):
    """The answer: the same call with its caller written in, deciding nothing."""

    hookSpecificOutput: Rewritten


def subagent_record(transcript: str, agent: str, suffix: str) -> Path:
    """Where Claude Code keeps one of a subagent's files, beside its session's transcript."""
    return Path(transcript).with_suffix("") / "subagents" / f"agent-{agent}{suffix}"


def spawn_of(transcript: str, agent: str) -> Spawned:
    """What the runtime recorded about this subagent's spawn, empty where nothing did."""
    if not transcript or not agent:
        return Spawned()
    recorded = loaded(subagent_record(transcript, agent, ".meta.json"), Spawned)
    return recorded if recorded is not None else Spawned()


def transcript_of(payload: Payload) -> Path | None:
    """The transcript of the conversation that made this call, where one is named.

    A subagent's own, which Claude Code writes beside its session's -- the
    one ``transcript_path`` names in every event, a subagent's included --
    as ``subagents/agent-<agent_id>.jsonl``, measured on 2.1.283.
    """
    transcript = text(payload.get("transcript_path"))
    agent = text(payload.get("agent_id"))
    if not transcript:
        return None
    return subagent_record(transcript, agent, ".jsonl") if agent else Path(transcript)


def said_in(block: WireValue) -> str:
    """The text one content block holds, blank for any other block."""
    match block:
        case {"type": "text", "text": str(said)}:
            return said
        case _:
            return ""


def spoken(record: dict[str, WireValue]) -> str | None:
    """One transcript record, as far as the words an agent says before a call go.

    An assistant record's text is what the agent wrote, and a user record --
    a person's message, a tool's result -- what it heard, before which
    nothing it said is about the call; any other record says nothing. Each
    of a message's blocks is a record of its own, and the call's is written
    before its ``PreToolUse`` hook runs, measured on 2.1.283.
    """
    match record:
        case {"type": "user"}:
            return None
        case {"type": "assistant", "message": {"content": list(blocks)}}:
            return "".join(said_in(block) for block in blocks)
        case _:
            return ""


def caller_of(payload: Payload) -> Caller:
    """The conversation one tool event came from, blank for the session's own."""
    agent = text(payload.get("agent_id"))
    spawn = spawn_of(text(payload.get("transcript_path")), agent)
    return Caller(
        agent_id=agent,
        agent_type=text(payload.get("agent_type")),
        cwd=text(payload.get("cwd")),
        name=text(spawn.get("name")),
        spawned_by=text(spawn.get("parentAgentId")),
    )


def decided(payload: Payload) -> Rewrite | None:
    """The rewritten call, or nothing for an event this hook has no answer to."""
    if payload.get("hook_event_name") != "PreToolUse":
        return None
    return Rewrite(
        hookSpecificOutput=Rewritten(
            hookEventName="PreToolUse",
            updatedInput=called_by(payload.get("tool_input", {}), caller_of(payload)),
        )
    )
