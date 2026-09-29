"""Where Codex writes its session transcripts, and what its words mean."""

import json
from datetime import datetime
from pathlib import Path
from pydantic import BaseModel, TypeAdapter, ValidationError

from lup.providers.codex.login import CODEX_LOGIN
from lup.observability.audit import ObservableEventKind
from lup.observability.native import (
    NativeRecordOrigin,
    NativeSemanticBlock,
    NativeTranscripts,
    NativeTurn,
    NativeTurns,
    blocks_by_type,
    first_string,
)
from lup.sessions.events import (
    AnyTurnBlock,
    TurnMessage,
    TurnTextBlock,
    TurnToolCallBlock,
    TurnToolResultBlock,
)
from lup.types import JsonObject, JsonValue

# lup: ignore[constant-declaration] — Codex's own wire spellings, which it
# chooses and this only reads. Deliberately not merged with Claude Code's:
# the two share words that do not share meanings.
CODEX_BLOCK_SPELLINGS: dict[str, ObservableEventKind] = {
    "reasoning": "reasoning",
    "reasoning_content": "reasoning",
    "function_call": "tool_call",
    "function_call_output": "tool_result",
    "usage": "usage",
}

# lup: ignore[constant-declaration] — the directory Codex itself writes to
CODEX_SESSIONS_DIR = "sessions"
"""Codex files every session under one directory in its home."""

JSON_OBJECT = TypeAdapter(JsonObject)


class RolloutRecord(BaseModel, frozen=True, extra="ignore"):
    """One rollout line, as far as reading a turn out of it needs."""

    type: str = ""
    timestamp: datetime | None = None
    payload: JsonValue = None


class SpokenPart(BaseModel, frozen=True, extra="ignore"):
    """One part of a message's content; only text parts carry words."""

    type: str = ""
    text: str = ""


def spoken_parts(content: list[JsonValue]) -> list[AnyTurnBlock]:
    """The text a message's parts carry, one block per part that carries any."""
    parts = [
        SpokenPart.model_validate(part) for part in content if isinstance(part, dict)
    ]
    return [
        TurnTextBlock(text=part.text)
        for part in parts
        if part.type in ("output_text", "input_text") and part.text
    ]


def called_with(arguments: str) -> JsonObject:
    """A function call's arguments, which Codex writes as a JSON document in a string."""
    try:
        return JSON_OBJECT.validate_json(arguments)
    except ValidationError:
        return {"arguments": arguments}


def output_text(output: JsonValue) -> str:
    """A call's output as text: as written where it is text, else as JSON."""
    return output if isinstance(output, str) else json.dumps(output)


def rollout_message(payload: JsonValue) -> TurnMessage | None:
    """One ``response_item`` payload as the message it carries, if it carries one.

    What a developer message injects — instructions, skills, the harness's own
    framing — is nobody speaking, and reads as no message.
    """
    match payload:
        case {
            "type": "message",
            "role": "assistant" | "user" as role,
            "content": list(content),
        }:
            spoken = spoken_parts(content)
            return TurnMessage(role=role, blocks=spoken) if spoken else None
        case {"type": "function_call", "name": str(name), "call_id": str(call)}:
            arguments = payload["arguments"] if "arguments" in payload else ""
            return TurnMessage(
                role="assistant",
                blocks=[
                    TurnToolCallBlock(
                        id=call,
                        name=name,
                        arguments=called_with(arguments)
                        if isinstance(arguments, str)
                        else {"arguments": arguments},
                    )
                ],
            )
        case {"type": "custom_tool_call", "name": str(name), "call_id": str(call)}:
            given = payload["input"] if "input" in payload else ""
            return TurnMessage(
                role="assistant",
                blocks=[
                    TurnToolCallBlock(id=call, name=name, arguments={"input": given})
                ],
            )
        case {
            "type": "function_call_output" | "custom_tool_call_output",
            "call_id": str(call),
        }:
            output = payload["output"] if "output" in payload else ""
            return TurnMessage(
                role="tool",
                blocks=[
                    TurnToolResultBlock(tool_call_id=call, content=output_text(output))
                ],
            )
        case _:
            return None


class CodexTranscripts(NativeTranscripts):
    """Read Codex's persisted session records."""

    def __init__(self, codex_home: Path | None = None) -> None:
        self.codex_home = codex_home or CODEX_LOGIN.ambient_home

    def roots(self) -> list[Path]:
        return [self.codex_home / CODEX_SESSIONS_DIR]

    def belongs_to(self, record: JsonObject) -> NativeRecordOrigin:
        # Both arrive inside the opening `session_meta` payload rather than at
        # the top level, which the descent reaches without naming the envelope.
        # Snake case here, and deliberately not the sibling `id`: that repeats
        # the value on the opening record and means something else on later ones.
        directory = first_string(record, "cwd")
        return NativeRecordOrigin(
            directory=Path(directory) if directory is not None else None,
            session=first_string(record, "session_id"),
        )

    def semantic_blocks(self, record: JsonObject) -> list[NativeSemanticBlock]:
        return blocks_by_type(record, CODEX_BLOCK_SPELLINGS)


class CodexTurns(NativeTurns):
    """Read one Codex rollout line as a turn, and find a subagent's rollout."""

    def turn(self, record: JsonObject) -> NativeTurn | None:
        try:
            line = RolloutRecord.model_validate(record)
        except ValidationError:
            return None
        if line.type != "response_item":
            return None
        message = rollout_message(line.payload)
        return NativeTurn(message=message, at=line.timestamp) if message else None

    def subagent(self, transcript: Path, agent: str) -> Path | None:
        # Measured on 0.158.0: a subagent is a thread of its own with a
        # rollout of its own, named for the thread's id, filed by the day it
        # started under the same sessions directory — the session's day, or
        # a later one of the same year.
        if not transcript.name.startswith("rollout-") or len(transcript.parents) < 3:
            return None
        wanted = f"rollout-*-{agent}.jsonl"
        found = sorted(
            [
                *transcript.parent.glob(wanted),
                *transcript.parents[2].glob(f"*/*/{wanted}"),
            ]
        )
        return found[0] if found else None
