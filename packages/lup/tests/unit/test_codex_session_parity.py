"""Native app-server 0.155.1 turn contracts without authentication or model calls."""

import asyncio
import json
from collections import deque
from pathlib import Path

import pytest
from pydantic import BaseModel

from lup.policy.hooks import LupHookInput, LupHookMatcher, LupHookOutput, LupHooksConfig

from lup.providers.codex.app_server import CodexAppServer, RpcMessage, RpcNotification
from lup.providers.codex.output import CodexJsonEnvelope, codex_output_contract
from lup.providers.codex import Codex, CodexMcpServerConfig
from lup.providers.codex.runtime import (
    CodexConversationState,
    CodexServing,
    CodexTurnChannel,
    decode_completed_item,
)
from lup.providers.codex.selection import codex_config
from lup.providers.selection import SessionRequest
from lup.sessions.errors import (
    StructuredOutputError,
    TurnInterruptedError,
    UnsupportedCapability,
)
from lup.sessions.events import SessionId, SubmissionDecision
from lup.sessions.events import AnyTurnBlock, TurnNativeActivityBlock, TurnThinkingBlock
from lup.sessions.events import TurnResult
from pydantic import TypeAdapter
from lup.sessions.layers import SessionLayers
from lup.sessions.middleware import CorrectionConfig
from lup.types import JsonObject, JsonValue


class Answer(BaseModel):
    answer: str


class Score(BaseModel):
    score: int


class FlexibleAnswer(BaseModel):
    scores: dict[str, int]
    note: str | None = None
    count: int = 7


@pytest.mark.parametrize("resume", [None, SessionId(value="thread-1")])
async def test_enveloped_output_corrects_json_and_gates_without_losing_defaults(
    tmp_path: Path, scripted_codex: "ScriptedCodex", resume: SessionId | None
) -> None:
    scripted_codex.answers.extend(
        [
            CodexJsonEnvelope(output_json="not JSON").model_dump_json(),
            CodexJsonEnvelope(
                output_json='{"scores":{"arbitrary/key":1}}'
            ).model_dump_json(),
            CodexJsonEnvelope(
                output_json='{"scores":{"arbitrary/key":2}}'
            ).model_dump_json(),
            '{"answer":"direct"}',
            "untyped",
        ]
    )

    async def gate(value: BaseModel) -> SubmissionDecision:
        if isinstance(value, FlexibleAnswer):
            assert value.model_fields_set == {"scores"}
            return SubmissionDecision(
                accepted=value.scores == {"arbitrary/key": 2}, message="Use score two"
            )
        return SubmissionDecision(accepted=True)

    config = Codex(cwd=tmp_path, submission_gate_resolver=lambda _output: gate)
    async with config.open(resume) as session:
        result = await session.ask("score", FlexibleAnswer)
        assert result.output == FlexibleAnswer(scores={"arbitrary/key": 2})
        assert result.usage.output_tokens == 6
        assert len(result.messages) == 3
        assert (await session.ask("answer", Answer)).output == Answer(answer="direct")
        assert (await session.ask("continue")).output is None
    turns = [
        params for method, params in scripted_codex.requests if method == "turn/start"
    ]
    assert turns[0]["outputSchema"] == CodexJsonEnvelope.model_json_schema()
    assert "Use score two" in str(turns[2]["input"])
    assert turns[0]["input"] == [
        {
            "type": "text",
            "text": codex_output_contract(FlexibleAnswer.model_json_schema()).prompt(
                "score"
            ),
        }
    ]
    assert "outputSchema" not in turns[-1]


class ScriptedCodex(CodexAppServer):
    """Record native requests and emit the documented per-turn notification shape."""

    def __init__(self, answers: list[str | None]) -> None:
        super().__init__(Path("codex"))
        self.answers = deque(answers)
        self.requests: list[tuple[str, JsonObject]] = []
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.turn_count = 0

    async def start(self) -> None:
        return None

    def emit(self, method: str, params: JsonObject) -> None:
        assert self.notification_handler is not None
        self.notification_handler(RpcNotification(method=method, params=params))

    async def request(self, method: str, params: JsonObject) -> JsonValue:
        self.requests.append((method, params))
        match method:
            case "config/read":
                return {"config": {"mcp_servers": {}, "plugins": {}, "model": None}}
            case "thread/start" | "thread/resume":
                return {"thread": {"id": "thread-1"}}
            case "turn/start":
                self.turn_count += 1
                turn_id = f"turn-{self.turn_count}"
                self.emit(
                    "turn/started", {"threadId": "thread-1", "turn": {"id": turn_id}}
                )
                self.started.put_nowait(turn_id)
                answer = self.answers.popleft()
                if answer is not None:
                    self.emit(
                        "item/completed",
                        {
                            "threadId": "thread-1",
                            "turnId": turn_id,
                            "item": {
                                "id": f"message-{turn_id}",
                                "type": "agentMessage",
                                "text": answer,
                                "phase": "final_answer",
                            },
                        },
                    )
                    self.emit(
                        "thread/tokenUsage/updated",
                        {
                            "threadId": "thread-1",
                            "turnId": turn_id,
                            "tokenUsage": {
                                "last": {
                                    "inputTokens": 4,
                                    "outputTokens": 2,
                                    "cachedInputTokens": 0,
                                }
                            },
                        },
                    )
                    self.emit(
                        "turn/completed",
                        {
                            "threadId": "thread-1",
                            "turn": {
                                "id": turn_id,
                                "status": "completed",
                                "durationMs": 10,
                            },
                        },
                    )
                return {"turn": {"id": turn_id}}
            case "turn/interrupt":
                self.emit(
                    "turn/completed",
                    {
                        "threadId": "thread-1",
                        "turn": {"id": params["turnId"], "status": "interrupted"},
                    },
                )
                return {}
            case "turn/steer":
                assert "expectedTurnId" in params
                assert "turnId" not in params
                return {"turnId": params["expectedTurnId"]}
            case _:
                raise AssertionError(f"unexpected request {method}")


@pytest.fixture
def scripted_codex(monkeypatch: pytest.MonkeyPatch) -> ScriptedCodex:
    server = ScriptedCodex([])
    monkeypatch.setattr(
        "lup.providers.codex.runtime.CodexAppServer", lambda *args, **kwargs: server
    )
    return server


async def test_envelope_stop_then_gate_correction_share_one_logical_continuation(
    tmp_path: Path, scripted_codex: ScriptedCodex
) -> None:
    answers = [
        CodexJsonEnvelope(
            output_json=json.dumps({"scores": {"key": value}})
        ).model_dump_json()
        for value in (0, 1, 2)
    ]
    scripted_codex.answers.extend([*answers, "fresh turn"])
    stop_active: list[bool] = []
    receipts: list[int] = []
    gated: list[int] = []

    async def stop(event: LupHookInput) -> LupHookOutput:
        stop_active.append(event.stop_hook_active)
        if len(stop_active) == 1:
            return LupHookOutput(
                decision="block",
                reason="Inspect the evidence before finishing",
                delivery_receipt=lambda: receipts.append(scripted_codex.turn_count),
            )
        return LupHookOutput()

    async def gate(value: BaseModel) -> SubmissionDecision:
        assert isinstance(value, FlexibleAnswer)
        gated.append(value.scores["key"])
        return SubmissionDecision(
            accepted=value.scores["key"] == 2, message="Use score two"
        )

    config = Codex(
        cwd=tmp_path,
        hooks=LupHooksConfig(stop=[LupHookMatcher(hook=stop)]),
        submission_gate_resolver=lambda _output: gate,
    )
    async with config.open() as session:
        turn = session.ask("score", FlexibleAnswer)
        events = [event async for event in turn.events()]
        result = await turn
        assert result.output == FlexibleAnswer(scores={"key": 2})
        assert result.usage.output_tokens == 6
        assert result.duration.total_seconds() == 0.03
        assert [
            message.native["text"] for message in result.messages if message.native
        ] == answers
        assert {event.identifiers.turn.value for event in events} == {
            "turn-1",
            "turn-2",
            "turn-3",
        }
        await session.ask("another logical turn")
    assert gated == [1, 2]
    assert receipts == [2]
    assert stop_active == [False, True, True, False]
    turns = [
        params for method, params in scripted_codex.requests if method == "turn/start"
    ]
    assert all(
        turn["outputSchema"] == CodexJsonEnvelope.model_json_schema()
        for turn in turns[:-1]
    )
    assert "Inspect the evidence" in str(turns[1]["input"])
    assert "Use score two" in str(turns[2]["input"])


async def test_late_tool_feedback_preserves_envelope_until_accepted_continuation(
    tmp_path: Path, scripted_codex: ScriptedCodex, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted_codex.answers.extend(
        [
            CodexJsonEnvelope(output_json='{"scores":{"key":1}}').model_dump_json(),
            CodexJsonEnvelope(output_json='{"scores":{"key":2}}').model_dump_json(),
        ]
    )
    original_emit = scripted_codex.emit

    def emit(method: str, params: JsonObject) -> None:
        if method == "item/completed" and params["turnId"] == "turn-1":
            original_emit(
                method,
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {
                        "id": "command",
                        "type": "commandExecution",
                        "command": "git status",
                        "aggregatedOutput": "complete evidence",
                        "status": "completed",
                        "exitCode": 0,
                    },
                },
            )
        original_emit(method, params)

    monkeypatch.setattr(scripted_codex, "emit", emit)
    receipts: list[int] = []
    gated: list[int] = []

    async def after(event: LupHookInput) -> LupHookOutput:
        assert event.tool_result == "complete evidence"
        return LupHookOutput(
            system_message="Check this completed command before finishing",
            delivery_receipt=lambda: receipts.append(scripted_codex.turn_count),
        )

    async def gate(value: BaseModel) -> SubmissionDecision:
        assert isinstance(value, FlexibleAnswer)
        gated.append(value.scores["key"])
        return SubmissionDecision(accepted=True)

    config = Codex(
        cwd=tmp_path,
        hooks=LupHooksConfig(
            post_tool_use=[LupHookMatcher(matcher="^ShellCommand$", hook=after)]
        ),
        submission_gate_resolver=lambda _output: gate,
    )
    async with config.open() as session:
        result = await session.ask("score", FlexibleAnswer)
    assert result.output == FlexibleAnswer(scores={"key": 2})
    assert result.usage.output_tokens == 4
    assert len(result.messages) == 3
    assert gated == [2]
    assert receipts == [2]
    turns = [
        params for method, params in scripted_codex.requests if method == "turn/start"
    ]
    assert len(turns) == 2
    assert turns[1]["outputSchema"] == CodexJsonEnvelope.model_json_schema()
    assert "Check this completed command" in str(turns[1]["input"])


@pytest.mark.parametrize("resume", [None, SessionId(value="thread-1")])
async def test_output_schema_is_per_turn_across_untyped_and_changed_types(
    tmp_path: Path,
    scripted_codex: ScriptedCodex,
    resume: SessionId | None,
) -> None:
    server = scripted_codex
    server.answers.extend(['{"answer":"first"}', "untyped", '{"score":7}'])
    async with Codex(cwd=tmp_path).open(resume) as session:
        assert (await session.ask("answer", Answer)).output == Answer(answer="first")
        assert (await session.ask("continue")).output is None
        assert (await session.ask("score", Score)).output == Score(score=7)
    thread_requests = [
        (method, params)
        for method, params in server.requests
        if method.startswith("thread/")
    ]
    assert len(thread_requests) == 1
    assert thread_requests[0][0] == (
        "thread/start" if resume is None else "thread/resume"
    )
    assert "dynamicTools" not in thread_requests[0][1]
    turns = [params for method, params in server.requests if method == "turn/start"]
    assert [turn.get("outputSchema") for turn in turns] == [
        {**Answer.model_json_schema(), "additionalProperties": False},
        None,
        {**Score.model_json_schema(), "additionalProperties": False},
    ]


async def test_gate_feedback_corrects_output_and_keeps_all_events_and_usage(
    tmp_path: Path,
    scripted_codex: ScriptedCodex,
) -> None:
    server = scripted_codex
    server.answers.extend(['{"answer":"wrong"}', '{"answer":"accepted"}'])

    async def gate(value: BaseModel) -> SubmissionDecision:
        assert isinstance(value, Answer)
        return SubmissionDecision(
            accepted=value.answer == "accepted", message="Use the exact answer accepted"
        )

    config = Codex(cwd=tmp_path, submission_gate_resolver=lambda _output: gate)
    async with config.open() as session:
        turn = session.ask("answer", Answer)
        events = [event async for event in turn.events()]
        result = await turn
    assert result.output == Answer(answer="accepted")
    assert result.usage.input_tokens == 8
    assert result.usage.output_tokens == 4
    assert result.duration.total_seconds() == 0.02
    assert {event.identifiers.turn.value for event in events} == {"turn-1", "turn-2"}
    assert len(result.blocks) == 2
    assert len(result.messages) == 2
    replay = TurnResult[Answer].model_validate_json(result.model_dump_json())
    assert replay.messages[0].native is not None
    assert replay.messages[0].native["phase"] == "final_answer"
    turns = [params for method, params in server.requests if method == "turn/start"]
    assert "Use the exact answer accepted" in str(turns[1]["input"])


async def test_exhausted_validation_keeps_each_attempt_and_usage(
    tmp_path: Path,
    scripted_codex: ScriptedCodex,
) -> None:
    server = scripted_codex
    server.answers.extend(["not JSON", '{"wrong":"shape"}'])
    config = Codex(
        cwd=tmp_path, layers=SessionLayers(correction=CorrectionConfig(cycles=1))
    )
    async with config.open() as session:
        with pytest.raises(StructuredOutputError) as raised:
            await session.ask("answer", Answer)
    assert len(raised.value.failure.validation_history) == 2
    assert len(raised.value.failure.messages) == 2
    assert "valid JSON" in raised.value.failure.validation_history[0].message
    assert "requested schema" in raised.value.failure.validation_history[1].message
    assert raised.value.failure.usage.output_tokens == 4
    assert server.turn_count == 2


async def test_correcting_turn_steers_and_interrupts_the_current_native_turn(
    tmp_path: Path,
    scripted_codex: ScriptedCodex,
) -> None:
    server = scripted_codex
    server.answers.extend(['{"wrong":"shape"}', None])
    async with Codex(cwd=tmp_path).open() as session:
        turn = session.ask("answer", Answer)
        pending = asyncio.ensure_future(turn)
        assert await server.started.get() == "turn-1"
        assert await asyncio.wait_for(server.started.get(), timeout=1) == "turn-2"
        await turn.steer("Use the answer field")
        await turn.interrupt()
        with pytest.raises(TurnInterruptedError):
            await pending
    assert (
        "turn/steer",
        {
            "threadId": "thread-1",
            "expectedTurnId": "turn-2",
            "input": [{"type": "text", "text": "Use the answer field"}],
        },
    ) in server.requests
    assert server.turn_count == 2


async def test_session_close_cancels_a_pending_submission_gate(
    tmp_path: Path, scripted_codex: ScriptedCodex
) -> None:
    scripted_codex.answers.append('{"answer":"ready"}')
    entered = asyncio.Event()
    canceled = asyncio.Event()

    async def gate(_value: BaseModel) -> SubmissionDecision:
        entered.set()
        try:
            await asyncio.Event().wait()
            return SubmissionDecision(accepted=True)
        finally:
            canceled.set()

    config = Codex(cwd=tmp_path, submission_gate_resolver=lambda _output: gate)
    async with config.open() as session:
        pending = asyncio.ensure_future(session.ask("answer", Answer))
        await asyncio.wait_for(entered.wait(), timeout=1)
    assert canceled.is_set()
    with pytest.raises(asyncio.CancelledError):
        await pending


async def test_exiting_a_session_mid_turn_aborts_that_turn(
    tmp_path: Path, scripted_codex: ScriptedCodex
) -> None:
    """The native turn is interrupted before its transport closes, as Claude's is.

    The caller still awaiting it is released by the session's own close.
    """
    scripted_codex.answers.append(None)
    async with Codex(cwd=tmp_path).open() as session:
        pending = asyncio.ensure_future(session.ask("answer"))
        assert await asyncio.wait_for(scripted_codex.started.get(), timeout=1) == (
            "turn-1"
        )
    assert (
        "turn/interrupt",
        {"threadId": "thread-1", "turnId": "turn-1"},
    ) in scripted_codex.requests
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, timeout=1)


@pytest.mark.parametrize("limit", ["max_turns", "max_thinking_tokens"])
def test_native_unavailable_numeric_limits_are_never_silently_dropped(
    tmp_path: Path, limit: str
) -> None:
    request = SessionRequest.model_validate({"cwd": tmp_path, limit: 4})
    with pytest.raises(UnsupportedCapability, match=limit):
        codex_config(request)


@pytest.mark.parametrize(
    "params",
    [
        {"mode": "form", "requestedSchema": {"type": "object"}},
        {"mode": "url", "url": "https://example.com/login", "elicitationId": "e1"},
        {"mode": "openai/userVerification", "credentialData": "challenge"},
    ],
)
async def test_interactive_mcp_elicitations_are_never_accepted_as_tool_approval(
    tmp_path: Path, params: JsonObject
) -> None:
    state = CodexConversationState(
        Codex(cwd=tmp_path),
        CodexAppServer(Path("codex")),
        None,
        serving=CodexServing(servers={"tools": CodexMcpServerConfig(command="tools")}),
    )
    state.thread_id = "thread-1"
    with pytest.raises(UnsupportedCapability, match="interactive"):
        await state.handle_server_request(
            RpcMessage(
                id=1,
                method="mcpServer/elicitation/request",
                params={"threadId": "thread-1", "serverName": "tools", **params},
            )
        )


async def test_mcp_approval_from_another_thread_is_declined(tmp_path: Path) -> None:
    state = CodexConversationState(
        Codex(cwd=tmp_path),
        CodexAppServer(Path("codex")),
        None,
        serving=CodexServing(servers={"tools": CodexMcpServerConfig(command="tools")}),
    )
    state.thread_id = "thread-1"
    assert await state.handle_server_request(
        RpcMessage(
            id=1,
            method="mcpServer/elicitation/request",
            params={
                "threadId": "other",
                "serverName": "tools",
                "_meta": {"codex_approval_kind": "mcp_tool_call"},
            },
        )
    ) == {"action": "decline"}


def test_reasoning_summary_survives_without_a_content_field() -> None:
    assert decode_completed_item(
        {"id": "r1", "type": "reasoning", "summary": ["Reasoning summary"]}
    ) == [TurnThinkingBlock(thinking="Reasoning summary")]


def test_unsuccessful_dynamic_tool_result_is_recorded_as_an_error() -> None:
    blocks = decode_completed_item(
        {
            "type": "dynamicToolCall",
            "id": "d1",
            "tool": "inspect",
            "arguments": {},
            "status": "completed",
            "success": False,
        }
    )
    assert blocks[-1].refusal is not None


def test_completed_command_with_nonzero_exit_status_is_an_error() -> None:
    blocks = decode_completed_item(
        {
            "type": "commandExecution",
            "id": "c1",
            "command": "false",
            "status": "completed",
            "aggregatedOutput": "",
            "exitCode": 1,
        }
    )
    assert blocks[-1].refusal is not None


def test_native_activity_survives_result_serialization_and_telemetry() -> None:
    payload: JsonObject = {
        "type": "contextCompaction",
        "id": "c1",
        "futureField": {"complete": [1, 2, 3]},
    }
    blocks = decode_completed_item(payload)
    assert blocks == [
        TurnNativeActivityBlock(
            provider="codex", activity="contextCompaction", payload=payload
        )
    ]
    adapter = TypeAdapter(list[AnyTurnBlock])
    assert adapter.validate_json(adapter.dump_json(blocks)) == blocks
    assert json.loads(blocks[0].telemetry_block.display_body) == payload
    assert blocks[0].text_payload is None
    assert blocks[0].tool_call_name is None


def test_version_generated_schema_requires_expected_turn_identity() -> None:
    fixtures = Path(__file__).parent / "fixtures" / "codex"
    steer = json.loads((fixtures / "TurnSteerParams.json").read_text())
    start = json.loads((fixtures / "TurnStartParams.json").read_text())
    assert set(steer["required"]) == {"expectedTurnId", "threadId", "input"}
    assert "turnId" not in steer["properties"]
    assert "outputSchema" in start["properties"]
    assert "dynamicTools" not in start["properties"]


async def test_unrelated_notifications_do_not_claim_a_turn_channel() -> None:
    channel = CodexTurnChannel("thread-1")
    channel.feed(
        RpcNotification(method="unrelated/event", params={"turnId": "other-turn"})
    )
    channel.feed(
        RpcNotification(
            method="turn/started",
            params={"threadId": "other-thread", "turn": {"id": "other-turn"}},
        )
    )
    assert channel.turn_id is None
    assert channel.events.empty()


def test_every_native_completed_item_kind_produces_replay_evidence() -> None:
    schema = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "codex"
            / "ItemCompletedNotification.json"
        ).read_text()
    )
    kinds = {
        item["properties"]["type"]["enum"][0]
        for item in schema["definitions"]["ThreadItem"]["oneOf"]
    }
    assert kinds == {
        "userMessage",
        "hookPrompt",
        "agentMessage",
        "functionCallOutput",
        "plan",
        "reasoning",
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "dynamicToolCall",
        "collabAgentToolCall",
        "subAgentActivity",
        "webSearch",
        "imageView",
        "sleep",
        "imageGeneration",
        "enteredReviewMode",
        "exitedReviewMode",
        "contextCompaction",
    }
    for kind in kinds | {"futureNativeActivity"}:
        payload: JsonObject = {
            "id": "item-1",
            "type": kind,
            "text": "complete text",
            "summary": ["summary"],
            "content": [],
            "arguments": {},
            "server": "tools",
            "tool": "inspect",
            "status": "completed",
            "command": "pwd",
            "aggregatedOutput": "/tmp",
            "changes": [],
        }
        blocks = decode_completed_item(payload)
        assert blocks, kind
        assert any(block.telemetry_block.display_body for block in blocks), kind
