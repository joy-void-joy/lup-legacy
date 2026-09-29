"""Direct regression coverage for adapter-owned runtime construction."""

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, get_args
from uuid import UUID

import pytest
from pydantic import BaseModel, Field, ValidationError

from lup.providers.claude import Claude, ClaudeTools, SESSION_THINKING_TOKENS
from lup.providers.claude.runtime import (
    ClaudeConversationState,
    ClaudeFork,
    ClaudeRecord,
    ClaudeSessionOpener,
    ClaudeTurnToolBinder,
    attach_cli_stderr,
    build_claude_options,
    build_submission_server,
    claude_usage,
    convert_claude_block,
    ROTATION_AMBIGUOUS_SIGNATURES,
    environmental_fault,
    may_be_a_rotation,
    needs_a_person,
)
from lup.providers.codex.app_server import CodexAppServer, RpcMessage, RpcNotification
from lup.providers.codex import Codex, CodexMcpServerConfig, CodexTools
from lup.providers.codex.runtime import (
    CodexConversationState,
    CodexServing,
    CodexSteer,
    CodexTurnChannel,
    CodexTurnToolBinder,
    DynamicToolCall,
    McpElicitationRequest,
    decode_completed_item,
    decode_usage,
    message_role,
    notification_turn_id,
)
from lup.policy.hooks import create_permission_hooks
from lup.providers.codex.builtins import CodexBuiltins
from lup.sessions.errors import ProviderTurnError, TurnInterruptedError
from lup.types import CustomModel, JsonObject, JsonValue, SubagentSpec
from lup.sessions.events import (
    AnyTurnBlock,
    BlockCompletedEvent,
    BlockDeltaEvent,
    LiveTurnEvent,
    MessageCompletedEvent,
    SessionId,
    SubmissionDecision,
    TurnCompletedEvent,
    TurnId,
    TurnIdentifiers,
    TurnInput,
    TurnMessage,
    TurnRequest,
    TurnStartedEvent,
    TurnTextBlock,
    TurnThinkingBlock,
    TurnNativeActivityBlock,
    TurnToolBinding,
    TurnToolCallBlock,
    TurnToolResultBlock,
)
from lup.sessions.output import InMemorySubmittedOutputStore, TurnSubmission
from lup.observability.cost import per_mtok_usage_cost
from lup.types import Usage
from tests.unit.test_adapter_transforms import arm_labels, decoder_arms

if TYPE_CHECKING:
    pass


def claude_state(config: Claude) -> ClaudeConversationState:
    """Conversation state for a new session, built the way the opener builds it."""
    return ClaudeConversationState(ClaudeSessionOpener(config), config, {}, None)


class FirstOutput(BaseModel):
    answer: str


class SecondOutput(BaseModel):
    score: int


def test_fresh_claude_session_uses_cli_valid_uuid() -> None:
    state = claude_state(Claude(model=CustomModel(id="claude")))

    assert str(UUID(state.session_id)) == state.session_id


def test_claude_session_defaults_and_hooks_reach_native_options(
    tmp_path: Path,
) -> None:
    hooks = create_permission_hooks([tmp_path / "rw"], [tmp_path / "ro"])
    options = build_claude_options(
        Claude(
            model=CustomModel(id="claude"),
            system_prompt="Project rules",
            tools=ClaudeTools(builtin="stock"),
            hooks=hooks,
        ),
        servers={},
        binding=lambda: None,
        resume=None,
        session_id="18f5debf-499a-42bb-8856-0b39dd59943d",
    )

    assert options.permission_mode == "bypassPermissions"
    assert options.max_thinking_tokens == SESSION_THINKING_TOKENS
    assert options.system_prompt == {
        "type": "preset",
        "preset": "claude_code",
        "append": "Project rules",
    }
    assert options.hooks is not None
    assert len(options.hooks["PreToolUse"]) == 2
    assert options.include_partial_messages


def test_the_manual_permission_mode_reaches_the_sdk_as_its_default() -> None:
    """The SDK's literal still spells the CLI's ``manual`` as ``default``."""
    options = build_claude_options(
        Claude(permission_mode="manual"),
        servers={},
        binding=lambda: None,
        resume=None,
        session_id="18f5debf-499a-42bb-8856-0b39dd59943d",
    )

    assert options.permission_mode == "default"


def test_a_named_plugin_directory_reaches_the_session(tmp_path: Path) -> None:
    """A session names the tree it is judged by, the way a launch does.

    An interactive launch passes `--plugin-dir`. A session opened through the
    SDK named nothing, so it resolved plugins through the settings at its
    working directory — and those register a marketplace under a name shared
    by every checkout declaring it. The plugin a worktree actually loaded was
    therefore whichever tree registered that name last, which is how a worker
    came to be refused an edit its own tree's policy kernel allows.
    """
    lease = tmp_path / "lease" / ".claude" / "plugins" / "lup"
    options = build_claude_options(
        Claude(
            model=CustomModel(id="claude"),
            cwd=tmp_path / "lease",
            plugin_dirs=[lease],
            tools=ClaudeTools(builtin="stock"),
        ),
        servers={},
        binding=lambda: None,
        resume=None,
        session_id="18f5debf-499a-42bb-8856-0b39dd59943d",
    )

    assert options.plugins == [{"type": "local", "path": str(lease)}]

    unnamed = build_claude_options(
        Claude(model=CustomModel(id="claude")),
        servers={},
        binding=lambda: None,
        resume=None,
        session_id="18f5debf-499a-42bb-8856-0b39dd59943d",
    )
    assert unnamed.plugins == []


def test_claude_isolation_knobs_reach_native_options() -> None:
    options = build_claude_options(
        Claude(
            model=CustomModel(id="claude"),
            max_buffer_size=500 * 1024 * 1024,
            setting_sources=[],
            extra_args={"strict-mcp-config": None, "no-session-persistence": None},
        ),
        servers={},
        binding=lambda: None,
        resume=None,
        session_id="18f5debf-499a-42bb-8856-0b39dd59943d",
    )

    assert options.max_buffer_size == 500 * 1024 * 1024
    assert options.setting_sources == []
    assert options.extra_args == {
        "strict-mcp-config": None,
        "no-session-persistence": None,
    }


def test_claude_isolation_knobs_default_to_no_inherited_tools() -> None:
    options = build_claude_options(
        Claude(model=CustomModel(id="claude")),
        servers={},
        binding=lambda: None,
        resume=None,
        session_id="18f5debf-499a-42bb-8856-0b39dd59943d",
    )

    assert options.max_buffer_size is None
    assert options.setting_sources == []
    assert options.tools == ["WebFetch", "WebSearch"]
    assert options.strict_mcp_config
    assert options.extra_args == {}


def test_a_dead_cli_explains_itself_instead_of_pointing_at_stderr() -> None:
    from claude_agent_sdk import ProcessError

    state = claude_state(Claude(model=CustomModel(id="claude")))
    for line in ("loading plugin lup@local", "error: marketplace 'local' not found"):
        state.stderr_lines.append(line)

    error = ProcessError("Check stderr output for details", exit_code=1)
    attach_cli_stderr(error, state.stderr_lines)

    # The reason travels in the message, so every caller that only reads
    # str(error) — the two TurnFailure paths included — inherits it.
    assert "marketplace 'local' not found" in str(error)
    assert "exit code 1" in str(error)
    assert error.stderr is not None
    assert "loading plugin lup@local" in error.stderr


def test_a_process_that_died_saying_nothing_is_left_as_it_arrived() -> None:
    from claude_agent_sdk import ProcessError

    error = ProcessError("Check stderr output for details", exit_code=1)
    before = str(error)

    attach_cli_stderr(error, deque())

    assert str(error) == before
    assert error.stderr is None


def test_only_the_sdk_s_process_error_is_rewritten() -> None:
    error = RuntimeError("unrelated")

    attach_cli_stderr(error, deque(["some stderr"]))

    assert str(error) == "unrelated"


def test_the_captured_tail_is_bounded_by_configuration() -> None:
    config = Claude(model=CustomModel(id="claude"), stderr_tail_lines=2)
    state = claude_state(config)

    for line in ("first", "second", "third"):
        state.stderr_lines.append(line)

    # stderr is unbounded and only its end says why the process stopped.
    assert list(state.stderr_lines) == ["second", "third"]


def test_claude_native_subagents_and_reported_cost_are_preserved() -> None:
    options = build_claude_options(
        Claude(
            model=CustomModel(id="claude"),
            subagents=[
                SubagentSpec(
                    name="researcher",
                    description="Research independently",
                    prompt="Gather evidence",
                    tools=["WebSearch"],
                    model="balanced",
                )
            ],
            tools=ClaudeTools(builtin=["Agent", "WebSearch"]),
        ),
        servers={},
        binding=lambda: None,
        resume=None,
        session_id="18f5debf-499a-42bb-8856-0b39dd59943d",
    )

    assert options.agents is not None
    assert options.agents["researcher"].tools == ["WebSearch"]
    assert claude_usage({}, total_cost_usd=0.75).cost_usd == 0.75


def test_codex_thread_config_contains_project_mcp_and_writable_roots(
    tmp_path: Path,
) -> None:
    config = Codex(
        model=CustomModel(id="gpt"),
        cwd=tmp_path,
        tools=CodexTools(builtin="none"),
        writable_roots=[tmp_path / "session"],
    )
    serving = CodexServing(
        servers={
            "notes": CodexMcpServerConfig(
                command="uv",
                args=["run", "serve-tools", "--server", "notes"],
                env={"LUP_SESSION_DIR": str(tmp_path / "session")},
            )
        }
    )
    state = CodexConversationState(
        config, CodexAppServer(Path("codex")), None, serving=serving
    )

    parameters = state.thread_parameters()

    assert parameters["config"] == {
        **CodexBuiltins().configuration(),
        "mcp_servers": {
            "notes": {
                "enabled": True,
                "required": True,
                "command": "uv",
                "args": ["run", "serve-tools", "--server", "notes"],
                "env": {"LUP_SESSION_DIR": str(tmp_path / "session")},
            }
        },
        "sandbox_workspace_write": {"writable_roots": [str(tmp_path / "session")]},
    }


def test_codex_thread_config_uses_app_server_approval_spelling(tmp_path: Path) -> None:
    config = Codex(
        cwd=tmp_path,
        approval_policy="on-request",
        tools=CodexTools(builtin=["Bash"]),
        hooks=create_permission_hooks([], []),
    )
    state = CodexConversationState(config, CodexAppServer(Path("codex")), None)
    assert state.thread_parameters()["approvalPolicy"] == "on-request"


async def test_thread_parameters_omit_model_for_the_native_default(
    tmp_path: Path,
) -> None:
    state = CodexConversationState(
        Codex(cwd=tmp_path), CodexAppServer(Path("codex")), None
    )

    assert "model" not in state.thread_parameters()


async def test_mcp_elicitation_accepts_composed_servers_declines_others(
    tmp_path: Path,
) -> None:
    config = Codex(model=CustomModel(id="gpt"), cwd=tmp_path)
    serving = CodexServing(servers={"notes": CodexMcpServerConfig(command="uv")})
    state = CodexConversationState(
        config, CodexAppServer(Path("codex")), None, serving=serving
    )

    def elicitation(server: str) -> RpcMessage:
        return RpcMessage(
            id=7,
            method="mcpServer/elicitation/request",
            params={
                "threadId": "t1",
                "turnId": "u1",
                "serverName": server,
                "_meta": {"codex_approval_kind": "mcp_tool_call"},
            },
        )

    state.thread_id = "t1"
    accepted = await state.handle_server_request(elicitation("notes"))
    declined = await state.handle_server_request(elicitation("stranger"))

    assert accepted == {"action": "accept"}
    assert declined == {"action": "decline"}


def test_every_sdk_content_block_kind_has_a_conversion_arm() -> None:
    """The SDK's own union is the roster, so a block kind it grows fails here.

    The wildcard keeps conversion total, which means a new kind would
    otherwise land in transcripts as its repr — recorded, but unreadable —
    and nothing else in the suite would say so.
    """
    from claude_agent_sdk.types import ContentBlock

    members = [member.__name__ for member in get_args(ContentBlock)]
    arms = arm_labels(convert_claude_block)

    assert members
    assert [name for name in members if name not in arms] == []
    assert arms[-1] == "_"


def test_every_conversion_arm_converts_a_direct_fixture() -> None:
    import claude_agent_sdk as claude
    from claude_agent_sdk import types as claude_types

    assert convert_claude_block(
        claude.ThinkingBlock(thinking="weighing options", signature="sig")
    ) == TurnThinkingBlock(thinking="weighing options")
    assert convert_claude_block(
        claude.ThinkingBlock(thinking="", signature="sig")
    ) == TurnThinkingBlock(thinking="", redacted=True)
    assert convert_claude_block(
        claude.ToolResultBlock(tool_use_id="call", content="2 passed")
    ) == TurnToolResultBlock(tool_call_id="call", content="2 passed")
    assert convert_claude_block(
        claude.ToolResultBlock(
            tool_use_id="call", content=[{"type": "text", "text": "chunk"}]
        )
    ) == TurnToolResultBlock(
        tool_call_id="call", content='[{"type": "text", "text": "chunk"}]'
    )
    assert convert_claude_block(
        claude_types.ServerToolUseBlock(
            id="srv", name="web_search", input={"query": "lup"}
        )
    ) == TurnToolCallBlock(id="srv", name="web_search", arguments={"query": "lup"})
    assert convert_claude_block(
        claude_types.ServerToolResultBlock(
            tool_use_id="srv", content={"type": "web_search_result"}
        )
    ) == TurnToolResultBlock(
        tool_call_id="srv", content="{'type': 'web_search_result'}"
    )


def test_claude_message_and_usage_translation_has_direct_fixtures() -> None:
    import claude_agent_sdk as claude

    assert convert_claude_block(claude.TextBlock(text="hello")) == TurnTextBlock(
        text="hello"
    )
    assert convert_claude_block(
        claude.ToolUseBlock(id="call", name="Read", input={"path": "README.md"})
    ) == TurnToolCallBlock(
        id="call",
        name="Read",
        arguments={"path": "README.md"},
    )
    usage = claude_usage({"input_tokens": 20, "output_tokens": 5}, total_cost_usd=0.12)
    assert usage == Usage(input_tokens=20, output_tokens=5, cost_usd=0.12)
    assert per_mtok_usage_cost(input_usd=2, output_usd=4)(usage) == 0.00006


def test_input_tokens_are_cache_inclusive_on_both_adapters() -> None:
    claude_side = claude_usage(
        {
            "input_tokens": 10,
            "output_tokens": 3,
            "cache_read_input_tokens": 90,
            "cache_creation_input_tokens": 25,
        },
        total_cost_usd=None,
    )
    assert claude_side.input_tokens == 125
    assert claude_side.cache_read_input_tokens == 90
    assert claude_side.cache_creation_input_tokens == 25

    codex_side = decode_usage(
        {"inputTokens": 125, "cachedInputTokens": 90, "outputTokens": 3}
    )
    assert codex_side.input_tokens == claude_side.input_tokens
    assert codex_side.cache_read_input_tokens == claude_side.cache_read_input_tokens

    priced = per_mtok_usage_cost(input_usd=10.0, output_usd=0.0, cached_input_usd=1.0)
    expected = (35 * 10.0 + 90 * 1.0) / 1_000_000
    assert priced(claude_side) == pytest.approx(expected)
    assert priced(codex_side) == pytest.approx(expected)


@pytest.mark.asyncio
async def test_claude_partial_events_are_live_and_completed_replay_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import claude_agent_sdk as claude
    from claude_agent_sdk import types as claude_types

    class FixtureClient:
        def __init__(self, options: claude.ClaudeAgentOptions) -> None:
            self.options = options

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def interrupt(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            assert prompt == "hello"
            assert session_id

        async def receive_response(
            self,
        ) -> AsyncIterator[
            claude_types.StreamEvent
            | claude_types.AssistantMessage
            | claude_types.ResultMessage
        ]:
            yield claude_types.StreamEvent(
                uuid="event",
                session_id="session",
                event={
                    "type": "content_block_delta",
                    "delta": {"text": "hel"},
                },
            )
            yield claude_types.AssistantMessage(
                content=[claude.TextBlock(text="hello")],
                model="claude",
            )
            yield claude_types.ResultMessage(
                subtype="success",
                duration_ms=4,
                duration_api_ms=3,
                is_error=False,
                num_turns=1,
                session_id="18f5debf-499a-42bb-8856-0b39dd59943d",
                usage={"input_tokens": 2, "output_tokens": 1},
            )

    monkeypatch.setattr(claude, "ClaudeSDKClient", FixtureClient)
    state = claude_state(Claude(model=CustomModel(id="claude")))

    accepted = await state.start_turn("hello")
    assert accepted.events is not None
    observed = [event async for event in accepted.events.live()]
    completed = await accepted.complete()

    assert [event.type for event in observed] == [
        "turn_started",
        "block_delta",
        "block_completed",
        "message_completed",
        "turn_completed",
    ]
    assert completed.blocks == [TurnTextBlock(text="hello")]
    assert completed.messages == [
        TurnMessage(
            role="assistant",
            blocks=[TurnTextBlock(text="hello")],
            model="claude",
        )
    ]


async def test_claude_adopts_the_session_id_the_cli_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completion identifiers and resume follow the init-reported session id.

    The CLI persists its transcript under an id of its own, not under the
    channel id this side minted — a record built from the minted id names a
    conversation that never existed, which is exactly how a parked run loses
    its workers' context on resume.
    """
    import claude_agent_sdk as claude
    from claude_agent_sdk import types as claude_types

    class InitReportingClient:
        def __init__(self, options: claude.ClaudeAgentOptions) -> None:
            self.options = options

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def interrupt(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            assert session_id

        async def receive_response(
            self,
        ) -> AsyncIterator[claude_types.SystemMessage | claude_types.ResultMessage]:
            yield claude_types.SystemMessage(
                subtype="init",
                data={"session_id": "persisted-by-the-cli"},
            )
            yield claude_types.ResultMessage(
                subtype="success",
                duration_ms=4,
                duration_api_ms=3,
                is_error=False,
                num_turns=1,
                session_id="persisted-by-the-cli",
                usage={"input_tokens": 1, "output_tokens": 1},
            )

    monkeypatch.setattr(claude, "ClaudeSDKClient", InitReportingClient)
    state = claude_state(Claude(model=CustomModel(id="claude")))
    minted = state.session_id

    accepted = await state.start_turn("hello")
    completed = await accepted.complete()

    assert minted != "persisted-by-the-cli"
    assert completed.identifiers is not None
    assert completed.identifiers.session.value == "persisted-by-the-cli"
    assert state.session_id == "persisted-by-the-cli"
    assert state.resume == "persisted-by-the-cli"


@pytest.mark.asyncio
async def test_an_interrupted_claude_turn_is_not_a_retryable_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex reads interruption off its terminal status; Claude has none.

    Both ways of ending a turn look identical here, so the request is what
    tells them apart. Raising the provider's failure instead sent a turn the
    caller deliberately stopped back through the recovery wrapper to be run
    again.
    """
    import claude_agent_sdk as claude
    from claude_agent_sdk import types as claude_types

    class InterruptibleClient:
        def __init__(self, options: claude.ClaudeAgentOptions) -> None:
            self.options = options

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def interrupt(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            return None

        async def receive_response(self) -> AsyncIterator[claude_types.ResultMessage]:
            yield claude_types.ResultMessage(
                subtype="error_during_execution",
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=1,
                session_id="18f5debf-499a-42bb-8856-0b39dd59943d",
                result="Turn ended early",
            )

    monkeypatch.setattr(claude, "ClaudeSDKClient", InterruptibleClient)
    state = claude_state(Claude(model=CustomModel(id="claude")))

    unasked = await state.start_turn("hello")
    with pytest.raises(ProviderTurnError):
        await unasked.complete()

    asked = await state.start_turn("hello")
    assert asked.interrupt is not None
    await asked.interrupt.interrupt()
    with pytest.raises(TurnInterruptedError):
        await asked.complete()


@pytest.mark.asyncio
async def test_the_durable_view_is_the_live_one_without_its_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subset and superset of one ordering, never two disagreeing records."""
    import claude_agent_sdk as claude
    from claude_agent_sdk import types as claude_types

    class FixtureClient:
        def __init__(self, options: claude.ClaudeAgentOptions) -> None:
            self.options = options

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def interrupt(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            return None

        async def receive_response(
            self,
        ) -> AsyncIterator[
            claude_types.StreamEvent
            | claude_types.AssistantMessage
            | claude_types.UserMessage
            | claude_types.ResultMessage
        ]:
            yield claude_types.StreamEvent(
                uuid="event",
                session_id="session",
                event={
                    "type": "content_block_delta",
                    "delta": {"text": "partial"},
                },
            )
            yield claude_types.AssistantMessage(
                content=[
                    claude_types.ToolUseBlock(id="t1", name="Read", input={"p": "x"})
                ],
                model="claude",
            )
            yield claude_types.UserMessage(
                content=[
                    claude_types.ToolResultBlock(
                        tool_use_id="t1", content="file body", is_error=False
                    )
                ]
            )
            yield claude_types.ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.0,
                usage={},
                result="done",
            )

    monkeypatch.setattr(claude, "ClaudeSDKClient", FixtureClient)
    state = claude_state(Claude(model=CustomModel(id="claude")))

    accepted = await state.start_turn("hello")
    assert accepted.events is not None
    durable = [event async for event in accepted.events.events()]
    completed = await accepted.complete()

    # A tool call and its result both reach the durable record. The old
    # stream carried the call and never the result, which is what made it
    # unusable as a trace.
    assert [event.type for event in durable] == [
        "turn_started",
        "block_completed",
        "message_completed",
        "block_completed",
        "message_completed",
        "turn_completed",
    ]
    assert [message.role for message in completed.messages] == ["assistant", "tool"]


@pytest.mark.asyncio
async def test_a_claude_fork_branches_the_parent_under_an_id_of_its_own(
    tmp_path: Path,
) -> None:
    """The CLI makes the branch, resuming the parent with its fork flags.

    So the new transcript is filed under the session's own home, which the
    SDK's file-copying fork cannot promise: it reads only this process's
    environment for where transcripts live.
    """
    state = claude_state(Claude(model=CustomModel(id="claude"), cwd=tmp_path))
    state.recorded = True

    async with ClaudeFork(state).fork() as forked:
        assert isinstance(forked.record, ClaudeRecord)
        options = forked.record.state.options()

    assert forked.id.value != state.session_id
    assert options.resume == state.session_id
    assert options.fork_session is True
    assert options.session_id == forked.id.value
    assert options.resume_session_at is None


@pytest.mark.asyncio
async def test_a_claude_fork_at_a_turn_keeps_that_turn_s_last_message() -> None:
    state = claude_state(Claude(model=CustomModel(id="claude")))
    state.recorded = True
    state.turn_points[TurnId(value="the-turn")] = "the-last-message"

    async with ClaudeFork(state).fork(TurnId(value="the-turn")) as forked:
        assert isinstance(forked.record, ClaudeRecord)
        options = forked.record.state.options()

    assert options.resume_session_at == "the-last-message"


def test_a_claude_fork_refuses_a_turn_the_session_never_took() -> None:
    state = claude_state(Claude(model=CustomModel(id="claude")))
    state.recorded = True

    with pytest.raises(ValueError, match="not one this session took"):
        ClaudeFork(state).fork(TurnId(value="elsewhere"))


def test_a_conversation_with_no_turn_has_nothing_to_fork() -> None:
    state = claude_state(Claude(model=CustomModel(id="claude")))

    with pytest.raises(ValueError, match="nothing to fork"):
        ClaudeFork(state).fork()


def test_a_new_claude_conversation_is_started_under_the_id_it_answers() -> None:
    """Dictated rather than waited for, so ``id`` holds from the moment it opens."""
    state = claude_state(Claude(model=CustomModel(id="claude")))

    options = state.options()

    assert options.session_id == state.session_id
    assert options.resume is None
    assert options.fork_session is False


def test_a_resumed_claude_conversation_names_nothing_new() -> None:
    config = Claude(model=CustomModel(id="claude"))
    state = ClaudeConversationState(
        ClaudeSessionOpener(config), config, {}, SessionId(value="resumed-id")
    )

    options = state.options()

    assert state.session_id == "resumed-id"
    assert options.resume == "resumed-id"
    assert options.session_id is None


@pytest.mark.asyncio
async def test_claude_binder_refreshes_same_schema_turns_without_reconnecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import claude_agent_sdk as claude

    disconnects = 0

    class RecordingClient:
        def __init__(self, options: claude.ClaudeAgentOptions) -> None:
            self.options = options

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            nonlocal disconnects
            disconnects += 1

    monkeypatch.setattr(claude, "ClaudeSDKClient", RecordingClient)
    state = claude_state(Claude(model=CustomModel(id="claude")))
    binder = ClaudeTurnToolBinder(state)

    def submission_tool_is_bound() -> bool:
        options = build_claude_options(
            state.config,
            servers=state.servers,
            binding=state.current_submission,
            resume=None,
            session_id=state.session_id,
        )
        servers = options.mcp_servers
        assert isinstance(servers, dict)
        return (
            "lup-output" in servers
            and "mcp__lup-output__submit_output" in options.allowed_tools
        )

    def current_submission() -> TurnSubmission | None:
        return state.submission

    await binder.bind(None)
    assert current_submission() is None
    assert disconnects == 0
    assert not submission_tool_is_bound()

    first_gated: list[FirstOutput] = []

    async def first_gate(value: FirstOutput) -> SubmissionDecision:
        first_gated.append(value)
        return SubmissionDecision(accepted=True)

    first_store = InMemorySubmittedOutputStore()
    await binder.bind(
        TurnToolBinding(output_type=FirstOutput, store=first_store, gate=first_gate)
    )
    await state.connect()
    first_submission = current_submission()
    assert first_submission is not None
    assert first_submission.schema == FirstOutput.model_json_schema()
    assert submission_tool_is_bound()
    first_response = await first_submission.submit({"answer": "first"})
    assert first_response.accepted
    assert first_gated == [FirstOutput(answer="first")]
    assert first_store.value == FirstOutput(answer="first")

    gated: list[FirstOutput] = []

    async def gate(value: FirstOutput) -> SubmissionDecision:
        gated.append(value)
        return SubmissionDecision(accepted=True)

    refreshed_store = InMemorySubmittedOutputStore()
    connected = state.client
    await binder.bind(
        TurnToolBinding(output_type=FirstOutput, store=refreshed_store, gate=gate)
    )
    # The schema is unchanged, so the conversation the provider is holding
    # survives the turn boundary instead of being spent reinstalling it.
    assert disconnects == 0
    assert state.client is connected
    refreshed = current_submission()
    assert refreshed is not None
    assert refreshed is not first_submission
    response = await refreshed.submit({"answer": "checked"})
    assert response.accepted
    # The store and gate the later turn installed are the ones reached, not
    # the pair that happened to be bound when the connection opened.
    assert gated == [FirstOutput(answer="checked")]
    assert refreshed_store.value == FirstOutput(answer="checked")
    assert first_gated == [FirstOutput(answer="first")]

    await state.connect()
    await binder.bind(
        TurnToolBinding(output_type=SecondOutput, store=InMemorySubmittedOutputStore())
    )
    # A different schema can only be advertised by reconnecting.
    assert disconnects == 1
    second_submission = current_submission()
    assert second_submission is not None
    assert second_submission.schema == SecondOutput.model_json_schema()

    await state.connect()
    await binder.bind(None)
    assert disconnects == 2
    assert current_submission() is None
    assert not submission_tool_is_bound()


@pytest.mark.asyncio
async def test_claude_submission_server_serves_the_binding_installed_now() -> None:
    """A connection outlives the turn that opened it, so its tool must too."""
    from mcp import Client

    state = claude_state(Claude(model=CustomModel(id="claude")))
    binder = ClaudeTurnToolBinder(state)

    opening_store = InMemorySubmittedOutputStore()
    await binder.bind(
        TurnToolBinding(output_type=FirstOutput, store=opening_store, gate=None)
    )
    # Built once, as a connection builds it, and never rebuilt afterwards.
    server = build_submission_server(state.current_submission).server

    later_store = InMemorySubmittedOutputStore()
    await binder.bind(
        TurnToolBinding(output_type=FirstOutput, store=later_store, gate=None)
    )

    # Called the way a runtime calls it, over the protocol against the live
    # instance, rather than by reaching into the server for its handler.
    async with Client(server) as client:
        submitted = await client.call_tool("submit_output", {"answer": "later"})

    assert submitted.is_error is False
    assert later_store.read(FirstOutput) == FirstOutput(answer="later")
    assert opening_store.read(FirstOutput) is None


@pytest.mark.asyncio
async def test_codex_binder_refreshes_each_schema_without_replacing_the_thread(
    tmp_path: Path,
) -> None:
    state = CodexConversationState(
        Codex(model=CustomModel(id="gpt"), cwd=tmp_path),
        CodexAppServer(Path("codex")),
        None,
    )
    binder = CodexTurnToolBinder(state)

    first_store = InMemorySubmittedOutputStore()
    await binder.bind(TurnToolBinding(output_type=FirstOutput, store=first_store))
    assert state.submission is not None
    state.thread_id = "thread-1"

    refreshed_store = InMemorySubmittedOutputStore()
    await binder.bind(TurnToolBinding(output_type=FirstOutput, store=refreshed_store))
    installed = state.submission
    assert installed is not None
    assert (await installed.submit({"answer": "refreshed"})).accepted
    assert refreshed_store.read(FirstOutput) == FirstOutput(answer="refreshed")
    assert first_store.read(FirstOutput) is None

    await binder.bind(
        TurnToolBinding(output_type=SecondOutput, store=InMemorySubmittedOutputStore())
    )
    assert state.submission is not None
    assert state.submission.schema == SecondOutput.model_json_schema()
    await binder.bind(None)
    assert state.submission is None
    assert state.thread_id == "thread-1"


@pytest.mark.asyncio
async def test_codex_steer_targets_the_active_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = CodexConversationState(
        Codex(model=CustomModel(id="gpt"), cwd=tmp_path),
        CodexAppServer(Path("codex")),
        None,
    )
    state.thread_id = "thread-1"
    requests: list[JsonObject] = []

    async def request(method: str, params: JsonObject) -> JsonValue:
        requests.append({"method": method, "params": params})
        return {}

    monkeypatch.setattr(state.server, "request", request)

    await CodexSteer(state, "turn-9").steer(TurnInput(text="focus on the tests"))

    assert requests == [
        {
            "method": "turn/steer",
            "params": {
                "threadId": "thread-1",
                "expectedTurnId": "turn-9",
                "input": [{"type": "text", "text": "focus on the tests"}],
            },
        }
    ]


@pytest.mark.asyncio
async def test_closing_a_session_settles_the_reader_before_the_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session exit interrupts an unfinished turn and does not await its read.

    The reader is still suspended inside `receive_response()` when the
    transport is torn down, so closing that generator raises `aclose():
    asynchronous generator is already running`. Persistence has completed by
    then, which makes a finished run read as a failed one.
    """
    import claude_agent_sdk as claude

    class SuspendingClient:
        def __init__(self, options: claude.ClaudeAgentOptions) -> None:
            self.options = options
            self.response: AsyncGenerator[object] | None = None
            self.reading = asyncio.Event()

        async def connect(self) -> None:
            return None

        async def interrupt(self) -> None:
            return None

        async def query(self, prompt: str, session_id: str = "default") -> None:
            return None

        def receive_response(self) -> AsyncGenerator[object]:
            async def stream() -> AsyncGenerator[object]:
                self.reading.set()
                await asyncio.Event().wait()
                yield object()

            self.response = stream()
            return self.response

        async def disconnect(self) -> None:
            # A transport closes the generator it handed out, and doing so
            # while the reader still owns it is what raises.
            if self.response is not None:
                await self.response.aclose()

    clients: list[SuspendingClient] = []

    def track(options: claude.ClaudeAgentOptions) -> SuspendingClient:
        clients.append(SuspendingClient(options))
        return clients[-1]

    monkeypatch.setattr(claude, "ClaudeSDKClient", track)
    opener = ClaudeSessionOpener(Claude(model=CustomModel(id="claude")))

    async with opener.open_session() as session:
        await session.engine.start(TurnRequest(input=TurnInput(text="hello")))
        # An unfinished turn is torn down with its reader suspended inside the
        # response generator, so wait until it is actually there.
        await clients[-1].reading.wait()


# -- app-server decoding: every notification, item, and parameter shape --
#
# `CodexTurnChannel.decode` and its helpers narrow app-server payloads with no
# union of ours to enumerate, so each roster below is read back out of the arms
# themselves. A notification method, an item type, or a payload key that
# changes without a case naming it fails here rather than reaching the ignored
# default and being reported as a turn that simply said nothing.


def turn_identifiers() -> TurnIdentifiers:
    """The identity every notification in this section is routed under."""
    return TurnIdentifiers(
        session=SessionId(value="thread-1"), turn=TurnId(value="turn-1")
    )


def routed_channel() -> CodexTurnChannel:
    """One channel already bound to the turn its notifications name."""
    channel = CodexTurnChannel("thread-1")
    channel.turn_id = "turn-1"
    return channel


def published(channel: CodexTurnChannel) -> list[LiveTurnEvent]:
    """Everything one channel published, up to its end-of-turn sentinel."""

    def events() -> Iterator[LiveTurnEvent]:
        while not channel.events.empty():
            match channel.events.get_nowait():
                case None:
                    return
                case event:
                    yield event

    return list(events())


class TurnIdentityCase(BaseModel, frozen=True):
    """One shape ``notification_turn_id`` reads a native turn identity from."""

    name: str
    arm: str
    params: JsonObject
    turn_id: str | None = None


class NotificationCase(BaseModel, frozen=True):
    """One app-server notification and everything its channel does with it."""

    name: str
    method: str
    params: JsonObject
    events: list[LiveTurnEvent] = Field(default_factory=list)
    usage: Usage = Usage()


class CompletedItemCase(BaseModel, frozen=True):
    """One completed app-server item and the blocks it decodes into."""

    name: str
    arm: str
    payload: JsonObject
    blocks: list[AnyTurnBlock] = Field(default_factory=list)


TURN_IDENTITY_CASES = [
    TurnIdentityCase(
        name="a-flat-turn-id",
        arm="(turnId)",
        params={"turnId": "turn-1", "delta": "hel"},
        turn_id="turn-1",
    ),
    TurnIdentityCase(
        name="a-nested-turn-reference",
        arm="(turn)",
        params={"turn": {"id": "turn-1", "status": "completed"}},
        turn_id="turn-1",
    ),
    TurnIdentityCase(
        name="a-notification-claiming-no-turn",
        arm="_",
        params={"threadId": "thread-1"},
    ),
]


NOTIFICATION_CASES = [
    NotificationCase(
        name="turn/started",
        method="turn/started",
        params={"turn": {"id": "turn-1"}},
        events=[TurnStartedEvent(identifiers=turn_identifiers())],
    ),
    NotificationCase(
        name="item/agentMessage/delta",
        method="item/agentMessage/delta",
        params={"turnId": "turn-1", "delta": "hel"},
        events=[BlockDeltaEvent(identifiers=turn_identifiers(), delta="hel")],
    ),
    NotificationCase(
        name="item/completed",
        method="item/completed",
        params={"turnId": "turn-1", "item": {"type": "agentMessage", "text": "hello"}},
        events=[
            BlockCompletedEvent(
                identifiers=turn_identifiers(), block=TurnTextBlock(text="hello")
            ),
            MessageCompletedEvent(
                identifiers=turn_identifiers(),
                message=TurnMessage(
                    role="assistant",
                    blocks=[TurnTextBlock(text="hello")],
                    native={"type": "agentMessage", "text": "hello"},
                ),
            ),
        ],
    ),
    NotificationCase(
        # The whole reason `message_role` exists, asserted where it is read
        # rather than only on the helper: a call and its result are the model's
        # act and the environment's reply, so they reach a transcript as a tool
        # message carrying both blocks. Replace `message_role(item)` at the call
        # site with the literal "assistant" and this is what refuses. It is also
        # the only case that drives the block loop round more than once.
        name="item/completed-as-a-tool-message",
        method="item/completed",
        params={
            "turnId": "turn-1",
            "item": {
                "type": "commandExecution",
                "id": "c1",
                "command": "uv run pytest",
                "aggregatedOutput": "2 passed",
                "status": "completed",
            },
        },
        events=[
            BlockCompletedEvent(
                identifiers=turn_identifiers(),
                block=TurnToolCallBlock(
                    id="c1", name="ShellCommand", arguments={"command": "uv run pytest"}
                ),
            ),
            BlockCompletedEvent(
                identifiers=turn_identifiers(),
                block=TurnToolResultBlock(tool_call_id="c1", content="2 passed"),
            ),
            MessageCompletedEvent(
                identifiers=turn_identifiers(),
                message=TurnMessage(
                    role="tool",
                    native={
                        "type": "commandExecution",
                        "id": "c1",
                        "command": "uv run pytest",
                        "aggregatedOutput": "2 passed",
                        "status": "completed",
                    },
                    blocks=[
                        TurnToolCallBlock(
                            id="c1",
                            name="ShellCommand",
                            arguments={"command": "uv run pytest"},
                        ),
                        TurnToolResultBlock(tool_call_id="c1", content="2 passed"),
                    ],
                ),
            ),
        ],
    ),
    NotificationCase(
        name="item/completed-with-an-undecodable-item",
        method="item/completed",
        params={"turnId": "turn-1", "item": {"type": "unheardOf", "id": "m1"}},
        events=[
            BlockCompletedEvent(
                identifiers=turn_identifiers(),
                block=TurnNativeActivityBlock(
                    provider="codex",
                    activity="unheardOf",
                    payload={"type": "unheardOf", "id": "m1"},
                ),
            ),
            MessageCompletedEvent(
                identifiers=turn_identifiers(),
                message=TurnMessage(
                    role="assistant",
                    blocks=[
                        TurnNativeActivityBlock(
                            provider="codex",
                            activity="unheardOf",
                            payload={"type": "unheardOf", "id": "m1"},
                        )
                    ],
                    native={"type": "unheardOf", "id": "m1"},
                ),
            ),
        ],
    ),
    NotificationCase(
        # Usage is folded into the channel rather than published, so this arm
        # is the one whose whole effect is invisible on the event stream.
        name="thread/tokenUsage/updated",
        method="thread/tokenUsage/updated",
        params={
            "turnId": "turn-1",
            "tokenUsage": {
                "last": {"inputTokens": 120, "outputTokens": 8, "cachedInputTokens": 90}
            },
        },
        usage=Usage(input_tokens=120, output_tokens=8, cache_read_input_tokens=90),
    ),
    NotificationCase(
        name="turn/completed",
        method="turn/completed",
        params={"turn": {"id": "turn-1", "status": "completed", "durationMs": 40}},
        events=[TurnCompletedEvent(identifiers=turn_identifiers())],
    ),
]


COMPLETED_ITEM_CASES = [
    CompletedItemCase(
        name="agentMessage",
        arm="(type=agentMessage, text)",
        payload={"type": "agentMessage", "text": "hello"},
        blocks=[TurnTextBlock(text="hello")],
    ),
    CompletedItemCase(
        name="userMessage",
        arm="(type=userMessage, content)",
        payload={
            "type": "userMessage",
            "id": "prompt",
            "content": [
                {"type": "text", "text": "Reply with pong", "text_elements": []},
                {"type": "image", "url": "file:///shot.png"},
            ],
        },
        blocks=[TurnTextBlock(text="Reply with pong")],
    ),
    CompletedItemCase(
        name="reasoning",
        arm="(type=reasoning)",
        payload={"type": "reasoning", "content": ["step one", "step two"]},
        blocks=[TurnThinkingBlock(thinking="step one\nstep two")],
    ),
    CompletedItemCase(
        name="commandExecution",
        arm="(type=commandExecution, id, command, aggregatedOutput, status)",
        payload={
            "type": "commandExecution",
            "id": "c1",
            "command": "uv run pytest",
            "aggregatedOutput": "2 passed",
            "status": "completed",
        },
        blocks=[
            TurnToolCallBlock(
                id="c1", name="ShellCommand", arguments={"command": "uv run pytest"}
            ),
            TurnToolResultBlock(tool_call_id="c1", content="2 passed"),
        ],
    ),
    CompletedItemCase(
        # A non-terminal status is the error flag, and output the vendor left
        # unset reaches the transcript as empty rather than as the word None.
        name="commandExecution-that-failed",
        arm="(type=commandExecution, id, command, aggregatedOutput, status)",
        payload={
            "type": "commandExecution",
            "id": "c2",
            "command": "uv run pytest",
            "aggregatedOutput": None,
            "status": "failed",
        },
        blocks=[
            TurnToolCallBlock(
                id="c2", name="ShellCommand", arguments={"command": "uv run pytest"}
            ),
            TurnToolResultBlock(tool_call_id="c2", content="", is_error=True),
        ],
    ),
    CompletedItemCase(
        name="fileChange",
        arm="(type=fileChange, id, changes, status)",
        payload={
            "type": "fileChange",
            "id": "f1",
            "changes": [{"path": "a.py", "kind": "update"}],
            "status": "completed",
        },
        blocks=[
            TurnToolCallBlock(
                id="f1",
                name="EditBatch",
                arguments={"changes": [{"path": "a.py", "kind": "update"}]},
            ),
            TurnToolResultBlock(tool_call_id="f1", content="completed"),
        ],
    ),
    CompletedItemCase(
        # The status is the result content here rather than a stream of output,
        # so a refused patch carries the word it was refused with.
        name="fileChange-that-failed",
        arm="(type=fileChange, id, changes, status)",
        payload={
            "type": "fileChange",
            "id": "f2",
            "changes": [{"path": "a.py", "kind": "update"}],
            "status": "rejected",
        },
        blocks=[
            TurnToolCallBlock(
                id="f2",
                name="EditBatch",
                arguments={"changes": [{"path": "a.py", "kind": "update"}]},
            ),
            TurnToolResultBlock(tool_call_id="f2", content="rejected", is_error=True),
        ],
    ),
    CompletedItemCase(
        name="dynamicToolCall",
        arm="(type=dynamicToolCall, id, tool, arguments, status)",
        payload={
            "type": "dynamicToolCall",
            "id": "d1",
            "tool": "submit_output",
            "arguments": {"answer": "x"},
            "status": "completed",
        },
        blocks=[
            TurnToolCallBlock(id="d1", name="submit_output", arguments={"answer": "x"}),
            TurnToolResultBlock(
                tool_call_id="d1",
                content=(
                    '{"arguments": {"answer": "x"}, "id": "d1", '
                    '"status": "completed", "tool": "submit_output", '
                    '"type": "dynamicToolCall"}'
                ),
            ),
        ],
    ),
    CompletedItemCase(
        # Arguments the vendor did not send as an object are kept whole under
        # one name rather than dropped for not being a mapping.
        name="dynamicToolCall-with-opaque-arguments",
        arm="(type=dynamicToolCall, id, tool, arguments, status)",
        payload={
            "type": "dynamicToolCall",
            "id": "d2",
            "tool": "submit_output",
            "arguments": "raw",
            "status": "failed",
        },
        blocks=[
            TurnToolCallBlock(
                id="d2", name="submit_output", arguments={"value": "raw"}
            ),
            TurnToolResultBlock(
                tool_call_id="d2",
                content=(
                    '{"arguments": "raw", "id": "d2", "status": "failed", '
                    '"tool": "submit_output", "type": "dynamicToolCall"}'
                ),
                is_error=True,
            ),
        ],
    ),
    CompletedItemCase(
        name="mcpToolCall",
        arm="(type=mcpToolCall, id, server, tool, arguments, status)",
        payload={
            "type": "mcpToolCall",
            "id": "m1",
            "server": "notes",
            "tool": "review",
            "arguments": {"confidence": 0.7},
            "status": "completed",
        },
        blocks=[
            TurnToolCallBlock(
                id="m1", name="mcp__notes__review", arguments={"confidence": 0.7}
            ),
            TurnToolResultBlock(
                tool_call_id="m1",
                content=(
                    '{"arguments": {"confidence": 0.7}, "id": "m1", '
                    '"server": "notes", "status": "completed", '
                    '"tool": "review", "type": "mcpToolCall"}'
                ),
            ),
        ],
    ),
    CompletedItemCase(
        # Arguments the vendor did not send as an object are kept whole under
        # one name rather than dropped for not being a mapping.
        name="mcpToolCall-with-opaque-arguments",
        arm="(type=mcpToolCall, id, server, tool, arguments, status)",
        payload={
            "type": "mcpToolCall",
            "id": "m2",
            "server": "notes",
            "tool": "review",
            "arguments": "raw",
            "status": "failed",
        },
        blocks=[
            TurnToolCallBlock(
                id="m2", name="mcp__notes__review", arguments={"value": "raw"}
            ),
            TurnToolResultBlock(
                tool_call_id="m2",
                content=(
                    '{"arguments": "raw", "id": "m2", "server": "notes", '
                    '"status": "failed", "tool": "review", '
                    '"type": "mcpToolCall"}'
                ),
                is_error=True,
            ),
        ],
    ),
    CompletedItemCase(
        name="an-item-type-with-no-arm",
        arm="_",
        payload={"type": "unheardOf", "id": "m1", "status": "completed"},
        blocks=[
            TurnNativeActivityBlock(
                provider="codex",
                activity="unheardOf",
                payload={"type": "unheardOf", "id": "m1", "status": "completed"},
            )
        ],
    ),
    CompletedItemCase(
        name="native-function-output",
        arm="(type=functionCallOutput, id)",
        payload={
            "type": "functionCallOutput",
            "id": "f1",
            "name": "inspect",
            "output": "ok",
        },
        blocks=[
            TurnToolResultBlock(
                tool_call_id="f1",
                content='{"id": "f1", "name": "inspect", "output": "ok", "type": "functionCallOutput"}',
            )
        ],
    ),
    CompletedItemCase(
        name="native-image-view",
        arm="(type, id)",
        payload={"type": "imageView", "id": "i1", "path": "/tmp/image.png"},
        blocks=[
            TurnToolCallBlock(
                id="i1",
                name="imageView",
                arguments={"type": "imageView", "id": "i1", "path": "/tmp/image.png"},
            ),
            TurnToolResultBlock(
                tool_call_id="i1",
                content='{"id": "i1", "path": "/tmp/image.png", "type": "imageView"}',
            ),
        ],
    ),
    CompletedItemCase(
        name="an-item-missing-the-field-its-arm-reads",
        arm="_",
        payload={"type": "agentMessage"},
        blocks=[
            TurnNativeActivityBlock(
                provider="codex",
                activity="agentMessage",
                payload={"type": "agentMessage"},
            )
        ],
    ),
]


def test_every_turn_identity_arm_is_named_by_a_case() -> None:
    assert sorted({case.arm for case in TURN_IDENTITY_CASES}) == sorted(
        arm_labels(notification_turn_id)
    )


@pytest.mark.parametrize(
    "case", TURN_IDENTITY_CASES, ids=[case.name for case in TURN_IDENTITY_CASES]
)
def test_each_turn_identity_shape_reads_the_turn_it_names(
    case: TurnIdentityCase,
) -> None:
    notification = RpcNotification(method="turn/completed", params=case.params)

    assert notification_turn_id(notification) == case.turn_id


def test_the_declared_notification_roster_is_exactly_the_arms_it_names() -> None:
    arms = decoder_arms(CodexTurnChannel.decode)

    assert [method for arm in arms for method in arm.discriminators] == list(
        CodexTurnChannel.notifications
    )
    assert arms[-1].label == "_"


def test_every_declared_notification_is_named_by_a_case() -> None:
    assert sorted({case.method for case in NOTIFICATION_CASES}) == sorted(
        CodexTurnChannel.notifications
    )


@pytest.mark.parametrize(
    "case", NOTIFICATION_CASES, ids=[case.name for case in NOTIFICATION_CASES]
)
async def test_each_notification_arm_publishes_what_it_names(
    case: NotificationCase,
) -> None:
    channel = routed_channel()

    channel.feed(RpcNotification(method=case.method, params=case.params))

    assert published(channel) == case.events
    assert channel.usage == case.usage


async def test_a_completed_turn_replays_its_transcript_blocks_and_usage() -> None:
    channel = routed_channel()

    channel.feed(
        RpcNotification(
            method="thread/tokenUsage/updated",
            params={
                "turnId": "turn-1",
                "tokenUsage": {
                    "last": {
                        "inputTokens": 120,
                        "outputTokens": 8,
                        "cachedInputTokens": 90,
                    }
                },
            },
        )
    )
    channel.feed(
        RpcNotification(
            method="item/completed",
            params={
                "turnId": "turn-1",
                "item": {"type": "agentMessage", "text": "hello"},
            },
        )
    )
    channel.feed(
        RpcNotification(
            method="turn/completed",
            params={"turn": {"id": "turn-1", "status": "completed", "durationMs": 40}},
        )
    )

    completed = await channel.completed

    assert completed.blocks == [TurnTextBlock(text="hello")]
    assert completed.messages == [
        TurnMessage(
            role="assistant",
            blocks=[TurnTextBlock(text="hello")],
            native={"type": "agentMessage", "text": "hello"},
        )
    ]
    assert completed.usage == Usage(
        input_tokens=120, output_tokens=8, cache_read_input_tokens=90
    )
    assert completed.duration == timedelta(milliseconds=40)


@pytest.mark.parametrize(
    "status", ["interrupted", "cancelled", "canceled", "Cancelled"]
)
async def test_a_stopped_turn_is_a_typed_interruption(status: str) -> None:
    channel = routed_channel()

    channel.feed(
        RpcNotification(
            method="turn/completed",
            params={"turn": {"id": "turn-1", "status": status}},
        )
    )

    with pytest.raises(TurnInterruptedError) as raised:
        await channel.completed
    assert raised.value.failure.message == f"Codex turn ended with status {status}"


async def test_a_failed_turn_carries_the_native_message_and_its_evidence() -> None:
    channel = routed_channel()
    channel.blocks.append(TurnTextBlock(text="partial"))

    channel.feed(
        RpcNotification(
            method="turn/completed",
            params={
                "turn": {
                    "id": "turn-1",
                    "status": "failed",
                    "error": {"message": "model overloaded"},
                }
            },
        )
    )

    with pytest.raises(ProviderTurnError) as raised:
        await channel.completed
    assert raised.value.failure.message == "model overloaded"
    assert raised.value.failure.blocks == [TurnTextBlock(text="partial")]


async def test_a_failed_turn_without_a_native_message_names_its_status() -> None:
    channel = routed_channel()

    channel.feed(
        RpcNotification(
            method="turn/completed",
            params={"turn": {"id": "turn-1", "status": "failed"}},
        )
    )

    with pytest.raises(ProviderTurnError) as raised:
        await channel.completed
    assert raised.value.failure.message == "Codex turn ended with status failed"


async def test_an_undeclared_notification_is_ignored_rather_than_raising() -> None:
    channel = routed_channel()

    channel.feed(RpcNotification(method="turn/thinking", params={"turnId": "turn-1"}))

    assert published(channel) == []
    assert not channel.completed.done()


async def test_a_declared_notification_with_an_unmatched_payload_is_ignored() -> None:
    channel = routed_channel()

    channel.feed(
        RpcNotification(
            method="item/agentMessage/delta",
            params={"turnId": "turn-1", "text": "hel"},
        )
    )

    assert published(channel) == []
    assert not channel.completed.done()


async def test_a_notification_naming_another_turn_never_reaches_the_arms() -> None:
    channel = routed_channel()

    channel.feed(
        RpcNotification(
            method="turn/completed",
            params={"turn": {"id": "turn-2", "status": "completed"}},
        )
    )

    assert channel.turn_id == "turn-1"
    assert published(channel) == []
    assert not channel.completed.done()


def test_every_completed_item_arm_is_named_by_a_case() -> None:
    assert sorted({case.arm for case in COMPLETED_ITEM_CASES}) == sorted(
        arm_labels(decode_completed_item)
    )


@pytest.mark.parametrize(
    "case", COMPLETED_ITEM_CASES, ids=[case.name for case in COMPLETED_ITEM_CASES]
)
def test_each_completed_item_decodes_to_the_blocks_it_names(
    case: CompletedItemCase,
) -> None:
    assert decode_completed_item(case.payload) == case.blocks


def test_every_message_role_arm_is_named_by_a_case() -> None:
    """Both arms spelled out, so a third cannot be added in silence.

    Order is asserted rather than sorted away, unlike the gates over disjoint
    class patterns: here the wildcard's position is the behaviour. Promote it
    above the first arm and every item becomes an assistant message.
    """
    assert arm_labels(message_role) == [
        "(type=commandExecution) | (type=fileChange) | (type=mcpToolCall) | (type=functionCallOutput) | (type=webSearch) | (type=imageView) | (type=imageGeneration) | (type=collabAgentToolCall) | (type=sleep)",
        "(type=userMessage)",
        "(type)",
        "_",
    ]


@pytest.mark.parametrize("item_type", ["commandExecution", "fileChange", "mcpToolCall"])
def test_an_environment_reply_item_is_a_tool_message(item_type: str) -> None:
    assert message_role({"type": item_type}) == "tool"


@pytest.mark.parametrize("item_type", ["agentMessage", "reasoning"])
def test_a_model_act_item_is_an_assistant_message(item_type: str) -> None:
    assert message_role({"type": item_type}) == "assistant"


def test_a_dynamic_tool_call_item_is_recorded_as_the_model_speaking() -> None:
    """The split names three item types as the environment's reply.

    A dynamic tool call decodes to a call block and its result the same way a
    command execution does, and is still folded under the assistant. That is
    what this pins, not what it endorses.
    """
    assert message_role({"type": "dynamicToolCall"}) == "assistant"


def test_an_item_naming_no_type_is_an_assistant_message() -> None:
    assert message_role({"id": "m1"}) == "assistant"


def test_the_usage_breakdown_maps_every_native_count() -> None:
    assert decode_usage(
        {"inputTokens": 120, "outputTokens": 8, "cachedInputTokens": 90}
    ) == Usage(input_tokens=120, output_tokens=8, cache_read_input_tokens=90)


def test_a_usage_breakdown_missing_a_count_is_refused() -> None:
    with pytest.raises(ValidationError):
        decode_usage({"inputTokens": 120, "outputTokens": 8})


def test_a_dynamic_tool_call_reads_the_native_call_identity() -> None:
    call = DynamicToolCall.model_validate(
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "callId": "call-1",
            "tool": "lup_app_app__record",
            "arguments": {"value": "x"},
        }
    )

    assert call.thread_id == "thread-1"
    assert call.turn_id == "turn-1"
    assert call.call_id == "call-1"
    assert call.tool == "lup_app_app__record"
    assert call.arguments == {"value": "x"}


def test_a_dynamic_tool_call_missing_the_call_it_answers_is_refused() -> None:
    with pytest.raises(ValidationError):
        DynamicToolCall.model_validate(
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tool": "lup_app_app__record",
                "arguments": {"value": "x"},
            }
        )


def test_a_dynamic_tool_call_spelled_in_snake_case_is_refused() -> None:
    """The wire spelling is the vendor's, so only the vendor's is accepted."""
    with pytest.raises(ValidationError):
        DynamicToolCall.model_validate(
            {
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "call_id": "call-1",
                "tool": "lup_app_app__record",
                "arguments": {"value": "x"},
            }
        )


def test_an_elicitation_reads_the_server_it_names() -> None:
    request = McpElicitationRequest.model_validate(
        {
            "threadId": "thread-1",
            "serverName": "notes",
            "_meta": {"codex_approval_kind": "mcp_tool_call"},
        }
    )

    assert request.thread_id == "thread-1"
    assert request.server_name == "notes"


def test_an_elicitation_without_the_server_it_names_is_refused() -> None:
    with pytest.raises(ValidationError):
        McpElicitationRequest.model_validate({"threadId": "thread-1"})


def test_the_faults_that_named_the_host_in_a_real_run_are_classified() -> None:
    """The three messages that produced 19 false concern failures in one run."""
    observed = [
        "Failed to authenticate. API Error: 401 OAuth access token has been revoked.",
        "You've hit your session limit · resets 5:40pm (Europe/Paris)",
        "Not logged in · Please run /login",
    ]

    assert all(environmental_fault(message) for message in observed)


def test_a_failure_that_names_the_work_is_not_read_as_the_host() -> None:
    """False is the conservative default: a real failure retried forever is worse."""
    assert not environmental_fault("the model refused the tool")
    assert not environmental_fault("Command failed with exit code 1")


def test_account_refusals_are_handed_back_while_transient_faults_can_retry() -> None:
    """Account changes can clear an allowance before a reported reset time."""
    assert needs_a_person("You've hit your session limit · resets 4pm (Paris)")
    assert needs_a_person("Claude account allowance exhausted until tomorrow")
    assert not needs_a_person("API Error: 429 rate_limit_error")
    assert not needs_a_person("Connection error")

    assert needs_a_person("API Error: 401 OAuth access token has been revoked.")
    assert needs_a_person("Not logged in · Please run /login")
    assert needs_a_person("Your credit balance is too low")


def test_a_rotated_credential_is_probed_where_a_spent_balance_is_handed_back() -> None:
    """One run's 12 auth faults fired in three instants of 4, 2 and 6 at once.

    Eleven sessions sharing one credential file, one of them refreshing: the
    siblings still holding the previous token are denied in the provider's
    words for a credential nobody will renew. That run recorded a failed
    phase and was working again 19 seconds later on the same file, which is
    not something a person can do and not something a dead credential allows.
    """
    assert may_be_a_rotation("API Error: 401 OAuth access token has been revoked.")
    assert may_be_a_rotation("Not logged in · Please run /login")

    # Neither is reachable by a refresh, so probing only delays the hand-back.
    assert not may_be_a_rotation("Your credit balance is too low")
    assert not may_be_a_rotation("Invalid API key")

    # Every one of them is a fault a person is asked about, probe or no probe.
    assert all(needs_a_person(message) for message in ROTATION_AMBIGUOUS_SIGNATURES)
