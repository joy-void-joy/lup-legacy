"""One line of a runtime's transcript, read as the turn it carries, whichever runtime wrote it.

Each runtime answers for its own words: a record the other runtime wrote is
none of its business, so the reader that knows no runtime asks each in turn
and takes the answer that comes back.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from lup.providers.transcripts import native_turn, subagent_transcript
from lup.types import JsonObject, JsonValue


def claude(kind: str, content: JsonValue, **extra: JsonValue) -> JsonObject:
    message: JsonObject = {"role": kind, "content": content}
    record: JsonObject = {
        "type": kind,
        "uuid": "u1",
        "parentUuid": None,
        "timestamp": "2026-09-29T10:00:00Z",
        "message": message,
    }
    return {**record, **extra}


def codex(payload: JsonObject) -> JsonObject:
    return {
        "timestamp": "2026-09-29T10:00:01.500Z",
        "type": "response_item",
        "payload": payload,
    }


def test_a_claude_assistant_record_says_what_it_said_and_called() -> None:
    turn = native_turn(
        claude(
            "assistant",
            [
                {"type": "text", "text": "Reading the roster."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Read",
                    "input": {"file_path": "roster.py"},
                },
            ],
        )
    )

    assert turn is not None
    assert turn.at == datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    assert turn.message.role == "assistant"
    assert [block.text_payload for block in turn.message.blocks] == [
        "Reading the roster.",
        None,
    ]
    assert [block.tool_call_name for block in turn.message.blocks] == [None, "Read"]


def test_a_claude_tool_result_answers_the_call_it_names() -> None:
    turn = native_turn(
        claude(
            "user", [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]
        )
    )

    assert turn is not None
    assert turn.message.role == "tool"
    assert [block.answered_call_id for block in turn.message.blocks] == ["toolu_1"]


def test_bookkeeping_is_no_turn() -> None:
    assert native_turn(claude("user", "a note", isMeta=True)) is None
    assert native_turn({"type": "system", "subtype": "turn_duration"}) is None
    assert native_turn({"type": "session_meta", "payload": {"id": "t"}}) is None
    assert native_turn(codex({"type": "reasoning", "summary": []})) is None
    assert native_turn({"unrelated": True}) is None


def test_a_codex_message_and_its_calls_read_as_turns() -> None:
    said = native_turn(
        codex(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Tracing the failure."}],
            }
        )
    )
    called = native_turn(
        codex(
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call_1",
                "arguments": json.dumps({"cmd": "ls"}),
            }
        )
    )
    scripted = native_turn(
        codex(
            {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "call_2",
                "input": "const r = await tools.exec_command({})",
            }
        )
    )
    answered = native_turn(
        codex(
            {"type": "custom_tool_call_output", "call_id": "call_2", "output": "done"}
        )
    )

    assert said is not None and said.message.role == "assistant"
    assert [block.text_payload for block in said.message.blocks] == [
        "Tracing the failure."
    ]
    assert said.at == datetime(2026, 9, 29, 10, 0, 1, 500000, tzinfo=UTC)
    assert called is not None
    assert [block.tool_arguments for block in called.message.blocks] == [{"cmd": "ls"}]
    assert scripted is not None
    assert [block.tool_call_name for block in scripted.message.blocks] == ["exec"]
    assert answered is not None and answered.message.role == "tool"
    assert [block.answered_call_id for block in answered.message.blocks] == ["call_2"]


def test_codex_instructions_a_developer_injected_are_no_turn() -> None:
    assert (
        native_turn(
            codex(
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "<skills>"}],
                }
            )
        )
        is None
    )


def test_a_claude_subagent_transcript_sits_beside_its_session(tmp_path: Path) -> None:
    session = tmp_path / "projects" / "tree" / "abc.jsonl"
    sidechain = session.with_suffix("") / "subagents" / "agent-a1b2.jsonl"
    sidechain.parent.mkdir(parents=True)
    session.write_text("", encoding="utf-8")
    sidechain.write_text("", encoding="utf-8")

    assert subagent_transcript(session, "a1b2") == sidechain


def test_a_codex_subagent_rollout_is_found_by_its_thread(tmp_path: Path) -> None:
    day = tmp_path / "sessions" / "2026" / "09" / "29"
    day.mkdir(parents=True)
    session = day / "rollout-2026-09-29T10-00-00-0001.jsonl"
    rollout = day / "rollout-2026-09-29T10-05-00-0002.jsonl"
    session.write_text("", encoding="utf-8")
    rollout.write_text("", encoding="utf-8")

    assert subagent_transcript(session, "0002") == rollout


def test_a_subagent_nothing_recorded_has_no_transcript(tmp_path: Path) -> None:
    session = tmp_path / "abc.jsonl"
    session.write_text("", encoding="utf-8")

    assert subagent_transcript(session, "gone") is None
