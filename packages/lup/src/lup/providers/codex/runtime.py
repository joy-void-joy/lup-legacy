"""Codex sessions opened over app-server, with live turn capabilities."""

import asyncio
from functools import partial
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from lup.execution.threads import run_sync
from lup.providers.codex.app_server import (
    CodexAppServer,
    RpcMessage,
    RpcNotification,
    native_environment,
)
from lup.providers.codex.hooks import (
    APPROVAL_METHODS,
    CODEX_SEMANTICS,
    CodexApprovalResponder,
    codex_hook_approval_policy,
)
from lup.providers.codex.home import CodexWorktreeHomeStore, install_declared_policy
from lup.providers.codex.login import CODEX_HOME, native_home
from lup.coordination.repository import launched_member
from lup.launch.companions import CompanionLaunch, Joined, held_around
from lup.launch.compilation import (
    allowance_environment,
    inherited_environment,
    kept_record,
    semantic_hooks,
)
from lup.launch.declaration import Reopening
from lup.providers.codex.launch import (
    codex_account_environment,
    codex_plugin_root,
    codex_sandbox_mode,
    compiled_codex,
)
from lup.providers.codex.output import CodexOutputContract, codex_output_contract
from lup.providers.codex.transcripts import CodexTranscripts
from lup.providers.codex import (
    CODEX_PROGRAM,
    Codex,
    CodexMcpServerConfig,
    CodexSession,
)
from lup.policy.hooks import LupHookInput, LupHookOutput, LupHooksConfig, merge_hooks
from lup.policy.identity import POLICY_ROOT_ENV
from lup.tools.mcp import (
    LupMcpServerConfig,
    LupMcpTool,
    McpServerEntry,
    RawStdioServerConfig,
    ServerCompanion,
    ToolResponse,
    running_companions,
)
from lup.mcp import hosted_servers, opened_needs
from lup.sessions.composition import AcceptedTurn, CompletedTurn, ComposedSession
from lup.sessions.capabilities import (
    ConversationRecord,
    EventStream,
    SessionEngine,
    ForkSession,
    Interrupt,
    Steer,
    TurnToolBinder,
)
from lup.sessions.errors import ProviderTurnError, StructuredOutputError
from lup.sessions.errors import TurnFailure, TurnInterruptedError, ValidationAttempt
from lup.sessions.errors import UnsupportedCapability
from lup.sessions.middleware import DecoratingSession
from lup.sessions.errors import TurnAlreadyActiveError
from lup.sessions.middleware import SerializedTurn
from lup.sessions.events import (
    BlockCompletedEvent,
    BlockDeltaEvent,
    LiveTurnEvent,
    MessageCompletedEvent,
    SessionId,
    SessionSummary,
    AnyTurnBlock,
    TurnCompletedEvent,
    TurnEvent,
    TurnStartedEvent,
    TurnIdentifiers,
    TurnId,
    TurnInput,
    TurnRequest,
    StartedTurn,
    TurnMessage,
    TurnToolCallBlock,
    TurnToolResultBlock,
    TurnToolBinding,
)
from lup.sessions.output import TurnSubmission, bound_submission
from lup.sessions.recursion import (
    recursive_agent_allowance,
    recursive_agent_scope,
)
from lup.sessions.transcript import fold_transcript
from lup.types import JsonObject, JsonValue, Usage

# lup: ignore[constant-declaration] — the thread sources Codex names, the
# ones a person or a program started rather than one a thread spawned
LISTED_THREAD_SOURCES = ("cli", "vscode", "exec", "appServer")
"""Which of Codex's thread sources a listing of a workspace's sessions shows.

Subagent threads are their parent's to show, so they are left out; left
unnamed, the app-server would narrow to its interactive sources and drop
the ones a program opened."""


class CodexThreadRef(BaseModel, frozen=True):
    id: str
    path: Path | None = None


class CodexPersistedTool(BaseModel, frozen=True):
    """The function-tool metadata persisted in the native rollout header."""

    type: Literal["function"] = "function"
    name: str
    description: str
    input_schema: JsonObject = Field(alias="inputSchema")


class CodexRolloutSession(BaseModel, frozen=True):
    id: str
    dynamic_tools: list[CodexPersistedTool] = []


class CodexRolloutHeader(BaseModel, frozen=True):
    type: Literal["session_meta"]
    payload: CodexRolloutSession


class DynamicToolCall(BaseModel, frozen=True):
    thread_id: str = Field(alias="threadId")
    turn_id: str = Field(alias="turnId")
    call_id: str = Field(alias="callId")
    tool: str
    arguments: JsonValue


class CodexSchemaRebindingError(RuntimeError):
    """The current app-server cannot change thread-scoped dynamic tools safely."""


class CodexInheritedConfig(BaseModel, frozen=True):
    """Ambient tool sources disabled before the thread begins."""

    mcp_servers: dict[str, JsonValue] = {}
    plugins: dict[str, JsonValue] = {}
    model: str | None = None


class CodexConfigReadResponse(BaseModel, frozen=True):
    config: CodexInheritedConfig


class CodexNotificationScope(BaseModel, frozen=True):
    thread_id: str | None = Field(default=None, alias="threadId")


class CodexTurnRef(BaseModel, frozen=True):
    id: str
    status: str = "inProgress"
    duration_ms: int | None = Field(default=None, alias="durationMs")


class CodexThreadResponse(BaseModel, frozen=True):
    thread: CodexThreadRef


class CodexRecordedTurn(BaseModel, frozen=True, extra="ignore"):
    """One turn as ``thread/read`` returns it: its items, in order."""

    id: str
    items: list[JsonObject] = []


class CodexRecordedThread(BaseModel, frozen=True, extra="ignore"):
    id: str
    turns: list[CodexRecordedTurn] = []


class CodexThreadReadResponse(BaseModel, frozen=True):
    thread: CodexRecordedThread


class CodexListedThread(BaseModel, frozen=True, extra="ignore"):
    """One thread as ``thread/list`` returns it, for the fields a listing shows."""

    id: str
    name: str | None = None
    preview: str = ""
    cwd: str | None = None
    created_at: int | None = Field(default=None, alias="createdAt")
    updated_at: int = Field(alias="updatedAt")

    def summary(self) -> SessionSummary:
        return SessionSummary(
            id=SessionId(value=self.id),
            title=self.name,
            preview=self.preview,
            cwd=Path(self.cwd) if self.cwd is not None else None,
            created_at=(
                datetime.fromtimestamp(self.created_at, tz=UTC)
                if self.created_at is not None
                else None
            ),
            updated_at=datetime.fromtimestamp(self.updated_at, tz=UTC),
        )


class CodexThreadPage(BaseModel, frozen=True, extra="ignore"):
    data: list[CodexListedThread] = []
    next_cursor: str | None = Field(default=None, alias="nextCursor")


class CodexTurnResponse(BaseModel, frozen=True):
    turn: CodexTurnRef


class McpElicitationMetadata(BaseModel, frozen=True):
    codex_approval_kind: str | None = None


class CodexHookActivity(BaseModel, frozen=True, extra="ignore"):
    """Native notification/request identity and optional completed item."""

    thread_id: str | None = Field(default=None, alias="threadId")
    turn_id: str | None = Field(default=None, alias="turnId")
    item: JsonObject | None = None


class McpElicitationRequest(BaseModel, frozen=True):
    """One approval elicitation for an MCP server tool call.

    Codex treats session-scoped MCP servers as untrusted and elicits an
    approval (``mcpServer/elicitation/request``, with
    ``_meta.codex_approval_kind = "mcp_tool_call"``) before every call.
    """

    thread_id: str = Field(alias="threadId")
    server_name: str = Field(alias="serverName")
    metadata: McpElicitationMetadata = Field(
        default_factory=McpElicitationMetadata, alias="_meta"
    )


class CodexItemOutcome(BaseModel, frozen=True):
    """Outcome fields shared by native activities, with their absent defaults."""

    activity: str = Field(default="unknown", alias="type")
    status: str | None = None
    success: bool | None = None
    exit_code: int | None = Field(default=None, alias="exitCode")
    failure: JsonValue = None

    @property
    def failed(self) -> bool:
        return (
            self.success is False
            or self.exit_code not in (None, 0)
            or self.status in ("failed", "errored", "rejected")
            or self.failure is not None
        )


class CodexReasoningItem(BaseModel, frozen=True):
    summary: list[str] = []
    content: list[str] = []


class TokenUsageBreakdown(BaseModel, frozen=True):
    input_tokens: int = Field(alias="inputTokens")
    output_tokens: int = Field(alias="outputTokens")
    cached_input_tokens: int = Field(alias="cachedInputTokens")


class CodexTurnFailure(BaseModel, frozen=True):
    """The `error` object an app-server turn carries when it did not complete."""

    message: str | None = None


class CodexCompletedTurn(BaseModel, frozen=True):
    """One `turn/completed` payload, read for the fields this adapter needs."""

    status: str
    duration_ms: int | None = Field(default=None, alias="durationMs")
    error: CodexTurnFailure | None = None


def notification_turn_id(notification: RpcNotification) -> str | None:
    """Extract the native turn identity without mutating channel ownership."""
    match notification.params:
        case {"turnId": str(turn_id)}:
            return turn_id
        case {"turn": {"id": str(turn_id)}}:
            return turn_id
        case _:
            return None


class CodexTurnChannel:
    """Route one turn's notifications into live events and completed replay."""

    notifications: Sequence[str] = (
        "turn/started",
        "item/agentMessage/delta",
        "item/completed",
        "thread/tokenUsage/updated",
        "turn/completed",
    )
    """Every notification method :meth:`decode` answers.

    Those arms narrow vendor method strings, which no union of ours enumerates,
    so the roster is declared here beside them and gates the match rather than
    describing it. A method that gains an arm without gaining an entry never
    reaches it, and the suite naming each shape fails instead of a renamed
    notification passing as one this turn had no interest in.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.turn_id: str | None = None
        self.events: asyncio.Queue[LiveTurnEvent | None] = asyncio.Queue()
        self.completed: asyncio.Future[CompletedTurn] = (
            asyncio.get_running_loop().create_future()
        )
        self.durable: list[TurnEvent] = []
        self.blocks: list[AnyTurnBlock] = []
        self.usage = Usage()
        self.started = perf_counter()
        self.hook_tasks: list[asyncio.Task[None]] = []
        self.hook_error: Exception | None = None
        self.deferred_context: list[LupHookOutput] = []

    def identifiers(self) -> TurnIdentifiers:
        if self.turn_id is None:
            raise RuntimeError("turn notification arrived without a turn identity")
        return TurnIdentifiers(
            session=SessionId(value=self.session_id),
            turn=TurnId(value=self.turn_id),
        )

    def emit(self, event: TurnEvent) -> None:
        """Record one durable event and publish it, so both views agree."""
        self.durable.append(event)
        self.events.put_nowait(event)

    def feed(self, notification: RpcNotification) -> None:
        try:
            self.decode(notification)
        except Exception as error:
            self.fail(error)

    def fail(self, error: Exception) -> None:
        """Fail this turn with all live evidence accumulated so far."""
        if isinstance(error, ProviderTurnError | TurnInterruptedError):
            failure = error
        else:
            identifiers = self.identifiers() if self.turn_id is not None else None
            failure = ProviderTurnError(
                TurnFailure(
                    message=str(error),
                    blocks=self.blocks,
                    messages=fold_transcript(self.durable),
                    usage=self.usage,
                    duration=timedelta(seconds=perf_counter() - self.started),
                    identifiers=identifiers,
                )
            )
        if not self.completed.done():
            self.completed.set_exception(failure)
        self.events.put_nowait(None)

    def decode(self, notification: RpcNotification) -> None:
        if notification.method not in self.notifications:
            return
        scope = CodexNotificationScope.model_validate(notification.params)
        if scope.thread_id not in (None, self.session_id):
            return
        candidate = notification_turn_id(notification)
        if candidate is not None:
            if self.turn_id is not None and candidate != self.turn_id:
                return
            self.turn_id = candidate
        match notification.method, notification.params:
            case "turn/started", {"turn": {"id": str()}}:
                self.events.put_nowait(TurnStartedEvent(identifiers=self.identifiers()))
            case "item/agentMessage/delta", {
                "turnId": str(),
                "delta": str(delta),
            }:
                self.events.put_nowait(
                    BlockDeltaEvent(
                        identifiers=self.identifiers(),
                        delta=delta,
                    )
                )
            case "item/completed", {
                "turnId": str(),
                "item": item,
            } if isinstance(item, dict):
                completed = decode_completed_item(item)
                role = message_role(item)
                # The prompt joins the transcript and not the turn's blocks:
                # a turn's blocks are what it produced, which is all a Claude
                # turn's blocks ever are, and a final answer read from the
                # last text block must not find the question instead.
                if role != "user":
                    for block in completed:
                        self.blocks.append(block)
                        self.emit(
                            BlockCompletedEvent(
                                identifiers=self.identifiers(), block=block
                            )
                        )
                if completed:
                    self.emit(
                        MessageCompletedEvent(
                            identifiers=self.identifiers(),
                            message=TurnMessage(
                                role=role, blocks=completed, native=item
                            ),
                        )
                    )
            case "thread/tokenUsage/updated", {
                "turnId": str(),
                "tokenUsage": {"last": usage},
            } if isinstance(usage, dict):
                self.usage = decode_usage(usage)
            case "turn/completed", {"turn": {"id": str(), "status": str()} as turn}:
                completed_turn = CodexCompletedTurn.model_validate(turn)
                status = completed_turn.status
                duration = timedelta(
                    milliseconds=completed_turn.duration_ms
                    if completed_turn.duration_ms is not None
                    else (perf_counter() - self.started) * 1000
                )
                if not self.completed.done():
                    match status:
                        case "completed":
                            self.completed.set_result(
                                CompletedTurn(
                                    messages=fold_transcript(self.durable),
                                    blocks=self.blocks,
                                    usage=self.usage,
                                    duration=duration,
                                )
                            )
                        case _:
                            message = (
                                completed_turn.error.message
                                if completed_turn.error
                                else None
                            )
                            failure = TurnFailure(
                                message=(
                                    message
                                    if message
                                    else f"Codex turn ended with status {status}"
                                ),
                                blocks=self.blocks,
                                messages=fold_transcript(self.durable),
                                usage=self.usage,
                                duration=duration,
                                identifiers=self.identifiers(),
                            )
                            error = (
                                TurnInterruptedError(failure)
                                if status.lower()
                                in {"interrupted", "cancelled", "canceled"}
                                else ProviderTurnError(failure)
                            )
                            self.completed.set_exception(error)
                self.events.put_nowait(
                    TurnCompletedEvent(identifiers=self.identifiers())
                )
                self.events.put_nowait(None)
            case _:
                return


class CodexLiveEventStream(EventStream):
    """One ordered channel, viewed either with in-flight deltas or without.

    The durable view filters the same sequence rather than reading a second
    one, so the two can never report different histories.
    """

    def __init__(self, channel: CodexTurnChannel) -> None:
        self.channel = channel
        self.consumed = False

    async def iterate(self) -> AsyncIterator[LiveTurnEvent]:
        if self.consumed:
            raise RuntimeError("live event stream can only be consumed once")
        self.consumed = True
        while (event := await self.channel.events.get()) is not None:
            yield event

    async def durable(self) -> AsyncIterator[TurnEvent]:
        async for event in self.iterate():
            if (durable := event.durable) is not None:
                yield durable

    def events(self) -> AsyncIterator[TurnEvent]:
        return self.durable()

    def live(self) -> AsyncIterator[LiveTurnEvent]:
        return self.iterate()


class CodexConversationState:
    """One app-server connection, thread, and current turn binding."""

    def __init__(
        self,
        config: Codex,
        server: CodexAppServer,
        resume: SessionId | None,
        policy_plugin: str | None = None,
        models: dict[str, JsonObject] | None = None,
        fork_from: SessionId | None = None,
        fork_at: TurnId | None = None,
        opener: "CodexSessionOpener | None" = None,
        serving: "CodexServing | None" = None,
    ) -> None:
        self.config = Codex.model_validate(config)
        self.serving = serving or CodexServing()
        """The declaration's MCP servers, as this session was served them."""
        self.opener = opener or CodexSessionOpener(self.config)
        self.fork_at = fork_at
        self.server = server
        self.resume = resume
        self.thread_id: str | None = None
        self.submission: TurnSubmission | None = None
        self.channel: CodexTurnChannel | None = None
        self.inherited_servers: list[str] = []
        self.context_lock = asyncio.Lock()
        self.stop_hook_active = False
        self.pending_hook_receipts: list[LupHookOutput] = []
        self.inherited_plugins: list[str] = []
        self.policy_plugin = policy_plugin
        self.models = models
        self.fork_from = fork_from
        self.server.server_request_handler = self.handle_server_request
        self.server.notification_handler = self.handle_notification
        self.server.disconnect_handler = self.handle_disconnect

    async def ensure_thread(self) -> str:
        if self.thread_id is not None:
            return self.thread_id
        if self.config.delegated_tools is not None and self.resume is not None:
            raise ValueError(
                "a delegated role cannot resume a thread with inherited tools"
            )
        inherited = CodexConfigReadResponse.model_validate(
            await self.server.request(
                "config/read",
                {"cwd": str(self.config.workspace()), "includeLayers": False},
            )
        )
        self.inherited_servers = list(inherited.config.mcp_servers)
        self.inherited_plugins = list(inherited.config.plugins)
        if (
            self.config.model_id() is None
            and inherited.config.model is not None
            and self.models is not None
            and inherited.config.model not in self.models
        ):
            raise ValueError(
                f"Codex cannot bound tools for inherited unknown model {inherited.config.model!r}; select a model from its installed catalog"
            )
        if self.resume is not None or self.fork_from is not None:
            prior = self.resume or self.fork_from
            assert prior is not None
            if self.serving.applications:
                await self.validate_persisted_tools(prior)
            params = self.thread_parameters()
            params.pop("dynamicTools", None)
            params["threadId"] = prior.value
            if self.resume is None and self.fork_at is not None:
                params["lastTurnId"] = self.fork_at.value
            result = await self.server.request(
                "thread/resume" if self.resume else "thread/fork", params
            )
        else:
            params = self.thread_parameters()
            result = await self.server.request("thread/start", params)
        response = CodexThreadResponse.model_validate(result)
        self.thread_id = response.thread.id
        return self.thread_id

    async def validate_persisted_tools(self, session: SessionId) -> None:
        """Resume cannot change dynamicTools in Codex 0.155.1; compare the native header."""
        response = CodexThreadResponse.model_validate(
            await self.server.request(
                "thread/read",
                {"threadId": session.value, "includeTurns": False},
            )
        )
        if response.thread.path is None:
            raise CodexSchemaRebindingError(
                "Codex cannot verify resumed tools without a persisted rollout; open a fresh session"
            )
        with response.thread.path.open() as source:
            header = CodexRolloutHeader.model_validate_json(source.readline())
        declared = self.thread_parameters()["dynamicTools"]
        if not isinstance(declared, list):
            raise ValueError("dynamic tool declaration must be a list")
        expected = [CodexPersistedTool.model_validate(tool) for tool in declared]
        if header.payload.id != session.value or sorted(
            header.payload.dynamic_tools, key=lambda tool: tool.name
        ) != sorted(expected, key=lambda tool: tool.name):
            raise CodexSchemaRebindingError(
                "Codex cannot change or remove persisted application/output tools on resume; open a fresh session with the intended tools"
            )

    def thread_parameters(self) -> JsonObject:
        """Preserve configured thread behavior for new and resumed threads."""
        params: JsonObject = {
            "cwd": str(self.config.workspace()),
            "developerInstructions": self.config.system_prompt,
        }
        # One half of the pair `model_selection` settles; its other half rides
        # `turn/start`, which is the only reason they are written apart.
        selected = self.config.model_selection()
        if "model" in selected:
            params["model"] = selected["model"]
        if self.config.model_provider is not None:
            params["modelProvider"] = self.config.model_provider
        configuration = dict(self.config.provider_config or {})
        configuration.update(self.config.builtins().configuration())
        if self.policy_plugin is not None:
            features = configuration["features"]
            assert isinstance(features, dict)
            features["hooks"] = True
            features["plugins"] = True
            configuration["plugins"] = {
                **{name: {"enabled": False} for name in self.inherited_plugins},
                self.policy_plugin: {"enabled": True},
            }
        configuration["mcp_servers"] = {
            **{name: {"enabled": False} for name in self.inherited_servers},
            **{
                name: {"enabled": True, **server.model_dump(mode="json")}
                for name, server in self.serving.servers.items()
            },
        }
        dynamic: list[JsonValue] = [
            {
                "name": name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for name, tool in self.serving.applications.items()
        ]
        # Only a session declaring application tools binds the thread-scoped
        # channel; typed output rides `outputSchema` on each turn instead.
        if dynamic:
            params["dynamicTools"] = dynamic
        writable = [
            *self.config.writable_roots,
            *[root.path for root in self.config.sandbox.roots() if root.writable],
        ]
        if writable:
            configuration["sandbox_workspace_write"] = {
                "writable_roots": [str(path) for path in writable]
            }
        if configuration:
            params["config"] = configuration
        mode = codex_sandbox_mode(self.config.sandbox, self.config.sandbox_mode)
        if mode is not None:
            params["sandbox"] = mode
        if self.config.approval_policy is not None:
            params["approvalPolicy"] = self.config.approval_policy
        native = self.config.builtins()
        if native.write and mode is None:
            params["sandbox"] = "workspace-write"
        if (
            not native.shell
            and not native.write
            and self.config.delegated_tools is None
        ):
            params["sandbox"] = "read-only"
            params["approvalPolicy"] = "never"
        return params

    async def start_turn(self, text: str) -> AcceptedTurn:
        thread_id = await self.ensure_thread()
        channel = CodexTurnChannel(thread_id)
        stop_hook_active = self.stop_hook_active
        self.channel = channel
        submission = self.submission
        output = codex_output_contract(submission.schema) if submission else None
        params: JsonObject = {
            "threadId": thread_id,
            "input": [
                {"type": "text", "text": output.prompt(text) if output else text}
            ],
        }
        # The other half. A named model always brings one, so the home's own
        # effort never rides beside a model the home did not choose.
        selected = self.config.model_selection()
        if "effort" in selected:
            params["effort"] = selected["effort"]
        if output is not None:
            params["outputSchema"] = output.native
        result = await self.server.request("turn/start", params)
        response = CodexTurnResponse.model_validate(result)
        for receipt in self.pending_hook_receipts:
            receipt.delivered()
        self.pending_hook_receipts.clear()
        channel.turn_id = response.turn.id
        identifiers = TurnIdentifiers(
            session=SessionId(value=thread_id),
            turn=TurnId(value=response.turn.id),
        )

        async def complete() -> CompletedTurn:
            completed = await self.complete_turn(channel, identifiers, stop_hook_active)
            if submission is not None and output is not None:
                await submit_completed_output(
                    submission, output, completed, identifiers
                )
            return completed

        return AcceptedTurn(
            identifiers=identifiers,
            complete=complete,
            events=CodexLiveEventStream(channel),
            interrupt=CodexInterrupt(self, response.turn.id),
            steer=CodexSteer(self, response.turn.id),
        )

    async def handle_server_request(self, message: RpcMessage) -> JsonValue:
        if message.method in APPROVAL_METHODS:
            return await self.resolve_approval(message)
        if message.method == "mcpServer/elicitation/request":
            return self.resolve_mcp_elicitation(message)
        if message.method == "item/tool/call":
            return await self.resolve_application_call(message)
        raise UnsupportedCapability(
            f"Codex app-server request {message.method!r} requires a client handler; use an interactive client for this capability"
        )

    async def complete_turn(
        self,
        channel: CodexTurnChannel,
        identifiers: TurnIdentifiers,
        stop_hook_active: bool,
    ) -> CompletedTurn:
        """Evaluate completion only after all completed-tool hooks have settled."""
        from lup.sessions.errors import TurnContinuationError

        completed = await channel.completed
        await asyncio.gather(*channel.hook_tasks)
        async with self.context_lock:
            outputs = list(channel.deferred_context)
            if self.config.hooks is not None and channel.hook_error is None:
                responder = CodexApprovalResponder(hooks=self.config.hooks)
                try:
                    outputs.extend(
                        await responder.evaluate(
                            LupHookInput(
                                event="Stop",
                                cwd=str(self.config.workspace()),
                                stop_hook_active=stop_hook_active,
                            )
                        )
                    )
                except Exception as error:
                    channel.hook_error = error
            feedback = CodexApprovalResponder(hooks=LupHooksConfig()).context(outputs)
            refused = any(
                output.decision in {"deny", "block", "ask"} for output in outputs
            )
            if channel.hook_error is None and not feedback and not refused:
                return completed
            failure = TurnFailure(
                message=str(channel.hook_error)
                if channel.hook_error
                else (feedback or "The Stop hook requires another continuation."),
                blocks=completed.blocks,
                messages=completed.messages,
                usage=completed.usage,
                duration=completed.duration,
                identifiers=identifiers,
            )
            if channel.hook_error is not None:
                raise ProviderTurnError(failure) from channel.hook_error
            self.stop_hook_active = True
            self.pending_hook_receipts = outputs
            raise TurnContinuationError(failure)

    async def deliver_activity(
        self,
        channel: CodexTurnChannel,
        notification: RpcNotification,
    ) -> None:
        """Run completed-tool callbacks and retain feedback the turn cannot accept."""
        try:
            if self.config.hooks is None:
                return
            async with self.context_lock:
                if channel is not self.channel:
                    return
                responder = CodexApprovalResponder(
                    hooks=self.config.hooks,
                    deliver_context=partial(self.deliver_context, channel),
                )
                item = CodexHookActivity.model_validate(notification.params).item
                if item is not None:
                    blocks = decode_completed_item(item)
                    results = {
                        block.tool_call_id: block.content
                        for block in blocks
                        # lup: ignore[own-model-dispatch] — adapter pairs tool call/result records
                        if isinstance(block, TurnToolResultBlock)
                    }
                    for block in blocks:
                        # lup: ignore[own-model-dispatch] — adapter pairs tool call/result records
                        if isinstance(block, TurnToolCallBlock):
                            outputs = await responder.evaluate(
                                LupHookInput(
                                    event="PostToolUse",
                                    tool_name=block.name,
                                    tool_input=block.arguments,
                                    tool_result=results.get(block.id, ""),
                                    cwd=str(self.config.workspace()),
                                )
                            )
                            if not await responder.deliver(outputs):
                                channel.deferred_context.extend(outputs)
                if not channel.completed.done():
                    await responder.deliver_pending()
        except Exception as error:
            channel.hook_error = error
            raise

    async def resolve_application_call(self, message: RpcMessage) -> JsonValue:
        """Route one native dynamic-tool call to the application tool it names."""
        call = DynamicToolCall.model_validate(message.params)
        if (
            self.channel is None
            or self.channel.turn_id != call.turn_id
            or self.thread_id != call.thread_id
        ):
            return {
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": "Tool call belongs to a stale or foreign turn.",
                    }
                ],
                "success": False,
            }
        if call.tool not in self.serving.applications:
            return {
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": f"No application tool {call.tool!r} is declared.",
                    }
                ],
                "success": False,
            }
        return await self.call_application_tool(call)

    async def call_application_tool(self, call: DynamicToolCall) -> JsonObject:
        """Invoke a declared closure in its hosting process with @lup_tool validation."""
        if not isinstance(call.arguments, dict):
            return {
                "contentItems": [
                    {"type": "inputText", "text": "Tool arguments must be an object."}
                ],
                "success": False,
            }
        try:
            result: ToolResponse = await self.serving.applications[call.tool].handler(
                call.arguments
            )
        except Exception as error:
            logging.getLogger(__name__).exception(
                "application tool %s failed", call.tool
            )
            return {
                "contentItems": [
                    {"type": "inputText", "text": f"{call.tool}: {error}"}
                ],
                "success": False,
            }

        def content_items() -> Iterator[JsonValue]:
            for item in result["content"] if "content" in result else []:
                match item:
                    case {"type": "text", "text": str(text)}:
                        yield {"type": "inputText", "text": text}
                    case {"type": "image", "data": str(data), "mimeType": str(mime)}:
                        yield {
                            "type": "inputImage",
                            "imageUrl": f"data:{mime};base64,{data}",
                        }
                    case _:
                        raise ValueError("unsupported application tool content")

        return {
            "contentItems": list(content_items()),
            "success": not ("is_error" in result and result["is_error"]),
        }

    async def resolve_approval(self, message: RpcMessage) -> JsonValue:
        """Answer one approval request from this session's declared hooks.

        A session with no hooks declines rather than accepting. Reaching here
        at all means the thread was started under a policy that asks, and the
        constructor refuses that combination — so this is the belt to that
        validator's braces, and the safe answer to a question nobody can
        answer is no.
        """
        if self.config.hooks is None:
            return {"decision": "decline"}
        channel = self.channel
        if channel is None or channel.completed.done():
            return {"decision": "decline"}
        if self.thread_id is None or channel.turn_id is None:
            return {"decision": "decline"}
        activity = CodexHookActivity.model_validate(message.params)
        if activity.turn_id != channel.turn_id:
            return {"decision": "decline"}
        if activity.thread_id != self.thread_id:
            return {"decision": "decline"}
        responder = CodexApprovalResponder(
            hooks=self.config.hooks,
            deliver_context=partial(self.deliver_context, channel),
            delivery_lock=self.context_lock,
        )
        method = message.method or ""
        return {"decision": await responder.decide(method, message.params)}

    def resolve_mcp_elicitation(self, message: RpcMessage) -> JsonValue:
        """Accept tool-call elicitations for servers this session composed.

        The composition that opened this session declared its MCP servers,
        so calls to those servers are pre-authorized; an elicitation naming
        any other server declines. Without this, every project tool call is
        reported to the model as "user rejected MCP tool call".
        """
        request = McpElicitationRequest.model_validate(message.params)
        if request.thread_id != self.thread_id:
            return {"action": "decline"}
        if request.metadata.codex_approval_kind != "mcp_tool_call":
            raise UnsupportedCapability(
                "Codex MCP forms, URL and verification elicitations require an interactive client; use an interactive session to answer them"
            )
        if request.server_name in self.serving.servers:
            return {"action": "accept"}
        return {"action": "decline"}

    async def deliver_context(self, channel: CodexTurnChannel, text: str) -> None:
        """Steer only the active turn whose context this hook is delivering."""
        if (
            channel is not self.channel
            or channel.turn_id is None
            or channel.completed.done()
        ):
            raise RuntimeError("no active Codex turn can receive hook context")
        await CodexSteer(self, channel.turn_id).steer(TurnInput(text=text))

    def handle_notification(self, notification: RpcNotification) -> None:
        activity = CodexHookActivity.model_validate(notification.params)
        if activity.thread_id not in {None, self.thread_id}:
            return
        if self.channel is not None:
            self.channel.feed(notification)
            if (
                notification.method == "item/completed"
                and notification_turn_id(notification) == self.channel.turn_id
            ):
                self.channel.hook_tasks.append(
                    self.server.spawn_handler(
                        self.deliver_activity(self.channel, notification)
                    )
                )

    def handle_disconnect(self, error: Exception) -> None:
        if self.channel is not None:
            self.channel.fail(error)

    def thread(self) -> str:
        """The thread this session holds, which it has from the moment it opens."""
        if self.thread_id is None:
            raise RuntimeError("this Codex session has not started its thread")
        return self.thread_id

    async def recorded(self) -> list[TurnMessage]:
        """Every item of the thread, as the app-server reads its rollout back."""
        response = CodexThreadReadResponse.model_validate(
            await self.server.request(
                "thread/read", {"threadId": self.thread(), "includeTurns": True}
            )
        )
        return [
            message
            for turn in response.thread.turns
            for item in turn.items
            if (message := recorded_message(item)) is not None
        ]


class CodexHookSession(SessionEngine):
    """Reset Stop state once per logical turn, preserving it across native retries."""

    def __init__(self, state: CodexConversationState, inner: SessionEngine) -> None:
        self.state = state
        self.inner = inner
        self.lock = asyncio.Lock()

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> StartedTurn[T]:
        if self.lock.locked():
            raise TurnAlreadyActiveError("a logical Codex turn is already active")
        await self.lock.acquire()
        self.state.stop_hook_active = False
        self.state.pending_hook_receipts.clear()
        accepted = False
        try:
            handle = await self.inner.start(request)
            accepted = True
        finally:
            if not accepted:
                self.lock.release()
        return StartedTurn[T](
            turn=SerializedTurn(handle.turn, self.lock),
            events=handle.events,
            interrupt=handle.interrupt,
            steer=handle.steer,
        )


class CodexTurnToolBinder(TurnToolBinder):
    """Bind each turn's native final-output schema and validation independently."""

    def __init__(self, state: CodexConversationState) -> None:
        self.state = state

    async def bind[T: BaseModel](self, binding: TurnToolBinding[T] | None) -> None:
        submission = bound_submission(binding) if binding is not None else None
        self.state.submission = submission


class CodexInterrupt(Interrupt):
    def __init__(self, state: CodexConversationState, turn_id: str) -> None:
        self.state = state
        self.turn_id = turn_id

    async def interrupt(self) -> None:
        thread_id = await self.state.ensure_thread()
        await self.state.server.request(
            "turn/interrupt", {"threadId": thread_id, "turnId": self.turn_id}
        )


class CodexSteer(Steer):
    def __init__(self, state: CodexConversationState, turn_id: str) -> None:
        self.state = state
        self.turn_id = turn_id

    async def steer(self, input: TurnInput) -> None:
        thread_id = await self.state.ensure_thread()
        await self.state.server.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": self.turn_id,
                "input": [{"type": "text", "text": input.text}],
            },
        )


class CodexRecord(ConversationRecord):
    """This thread as Codex keeps it: its id and its items."""

    def __init__(self, state: CodexConversationState) -> None:
        self.state = state

    def identity(self) -> SessionId:
        return SessionId(value=self.state.thread())

    async def messages(self) -> list[TurnMessage]:
        return await self.state.recorded()


class CodexFork(ForkSession[CodexSession]):
    """Branch this thread into a new one through ``thread/fork``."""

    def __init__(self, state: CodexConversationState) -> None:
        self.state = state

    def fork(
        self, at: TurnId | None = None
    ) -> AbstractAsyncContextManager[CodexSession]:
        return self.state.opener.open_session(
            fork_from=SessionId(value=self.state.thread()), fork_at=at
        )


async def resumed_thread(config: Codex, resume: Reopening | None) -> SessionId | None:
    """The thread a declared reopening names, as a session opened here resumes one.

    ``Latest`` is the newest thread on record for this workspace, the one
    ``codex resume --last`` takes; a picker is a terminal's, and refused.
    """
    if resume is None:
        return None
    return await resume.reopened(lambda: codex_sessions(config))


class CodexSessionOpener:
    """Validate hook coverage before opening one app-server per Lup session.

    Direct configuration has the same hook coverage contract as portable
    selection. Native approval callbacks also require an explicit asking
    policy; inheriting a policy could silently prevent every callback.
    Lifecycle observers leave the selected approval policy untouched.
    """

    def __init__(self, config: Codex) -> None:
        # Re-validated first, because model_copy skips every validator and a
        # copy is how an unsupported grant reaches this boundary unchecked.
        self.config = Codex.model_validate(config)

    @asynccontextmanager
    async def open_session(
        self,
        resume: Reopening | None = None,
        *,
        fork_from: SessionId | None = None,
        fork_at: TurnId | None = None,
    ) -> AsyncGenerator[CodexSession]:
        """Open one session, its host companions held for as long as it is open."""
        if (
            self.config.sandbox.posture().contained()
            and self.config.executable == CODEX_PROGRAM
        ):
            raise ValueError(
                "a session opened here inside the container is started as the "
                "program that enters it; name it in executable, or launch() it"
            )
        declared = self.config
        launch = CompanionLaunch(
            root=declared.workspace(),
            runtime="codex",
            environment={**inherited_environment(), **declared.environment},
        )
        async with held_around(declared.companions, launch) as joined:
            for notice in joined.notices:
                logging.getLogger(__name__).info("%s", notice.text)
            async with self.joined_session(
                joined, resume, fork_from=fork_from, fork_at=fork_at
            ) as session:
                yield session

    @asynccontextmanager
    async def joined_session(
        self,
        joined: Joined,
        resume: Reopening | None = None,
        *,
        fork_from: SessionId | None = None,
        fork_at: TurnId | None = None,
    ) -> AsyncGenerator[CodexSession]:
        """Open one session reaching what its held companions hand it."""
        declared = self.compiled()
        compiled = declared.model_copy(
            update={
                "environment": {**declared.environment, **joined.environment},
                "sandbox": declared.sandbox.widened(joined.mounts),
            }
        )
        approval = codex_hook_approval_policy(compiled.hooks)
        if approval == "on-request" and compiled.approval_policy in {None, "never"}:
            raise UnsupportedCapability(
                "Native approval-scoped PreToolUse hooks require an explicit asking "
                "approval_policy; use 'on-request' so the app-server can call them."
            )
        # Installed here rather than where the home is named: installing runs
        # a package manager, and a home is named wherever a request is merely
        # described. A session that opened without the policy it was meant to
        # run under is indistinguishable from one running under it, so a
        # failure is raised rather than warned past.
        relayed = allowance_environment(
            compiled.max_recursive_agent, compiled.environment
        )
        allowance = recursive_agent_allowance(relayed)
        environment = {**compiled.environment, **relayed}
        reopened = await resumed_thread(compiled, resume)
        config = compiled.model_copy(
            update={
                "cwd": compiled.workspace(),
                "environment": {
                    **environment,
                    POLICY_ROOT_ENV: str(
                        compiled.policy_root
                        or codex_plugin_root(compiled, compiled.workspace())
                    ),
                },
            }
        )
        # Built here rather than at declaration: a hosted server closes over
        # the session it serves — its identity, a container started for it,
        # a directory of its own that lives exactly as long as it does.
        scratch = TemporaryDirectory(prefix="lup-session-")
        served = codex_serving(
            hosted_servers(
                config.tools.mcp,
                opened_needs(
                    config.workspace(), Path(scratch.name), config.environment
                ),
            )
        )
        serving = served.model_copy(
            update={
                "servers": {
                    name: server.model_copy(
                        update={"env": allowance.environment(server.env)}
                    )
                    for name, server in served.servers.items()
                }
            }
        )
        policy_plugin = None
        if not config.sandbox.posture().contained():
            effective = native_environment(config.environment)
            home = native_home(effective)
            if not effective.get(CODEX_HOME):
                home.mkdir(mode=0o700, parents=True, exist_ok=True)
            policy_plugin = await run_sync(
                partial(
                    install_declared_policy,
                    home,
                    config.policy_root or codex_plugin_root(config, config.workspace()),
                    seed=CodexWorktreeHomeStore().derived(home),
                    workspace=config.workspace(),
                    executable=config.executable,
                    environment=effective,
                )
            )
            config = config.model_copy(
                update={"environment": {**effective, CODEX_HOME: str(home)}}
            )
        native = config.builtins()
        server = CodexAppServer(
            config.executable,
            environment=config.environment,
            arguments=native.arguments(),
        )
        state = CodexConversationState(
            config,
            server,
            reopened,
            policy_plugin.selector if policy_plugin else None,
            fork_from=fork_from,
            fork_at=fork_at,
            opener=self,
            serving=serving,
        )
        session = ComposedSession(
            state.start_turn,
            CodexTurnToolBinder(state),
            gate_resolver=config.submission_gate_resolver,
        )
        corrected = DecoratingSession(
            session,
            timeout=None,
            budget=None,
            recovery=None,
            correction=config.correction,
            continuation=config.continuation,
            persistence=None,
        )

        @asynccontextmanager
        async def process() -> AsyncGenerator[SessionEngine]:
            """The app-server's whole life: started, its thread opened, closed."""
            catalog = await asyncio.to_thread(
                native.model_catalog,
                config.executable,
                config.environment,
                config.model_id(),
            )
            state.models = (
                {
                    item["slug"]: item
                    for item in catalog["models"]
                    if isinstance(item, dict)
                    and "slug" in item
                    and isinstance(item["slug"], str)
                }
                if isinstance(catalog["models"], list)
                else {}
            )
            # Beside the session's own home, so a contained launch can read the
            # catalog it is pointed at; the home is the launch's to create when
            # policy installation has not already made it.
            catalog_home = (
                Path(config.environment[CODEX_HOME])
                if CODEX_HOME in config.environment
                else None
            )
            if catalog_home is not None:
                catalog_home.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = TemporaryDirectory(prefix="lup-native-", dir=catalog_home)
            try:
                catalog_path = Path(directory.name) / "models.json"
                catalog_path.write_text(json.dumps(catalog))
                server.arguments.extend(
                    ["--config", f"model_catalog_json={json.dumps(str(catalog_path))}"]
                )
                kept = kept_record(
                    "codex",
                    CodexTranscripts(native_home(config.environment)),
                    config.workspace(),
                    config.record,
                    config.model_id(),
                    config.profile,
                )
                with recursive_agent_scope(allowance), kept:
                    await server.start()
                    async with running_companions(serving.companions):
                        try:
                            await state.ensure_thread()
                            yield CodexHookSession(state, corrected)
                        finally:
                            try:
                                await session.abort_active()
                            finally:
                                await corrected.close()
            finally:
                try:
                    await server.close()
                finally:
                    directory.cleanup()
                    scratch.cleanup()

        async with config.layers.around(process(), reopened or fork_from) as engine:
            yield CodexSession(engine, CodexRecord(state), CodexFork(state))

    def compiled(self) -> Codex:
        """The declaration as its sessions open with it.

        A compatible endpoint becomes the provider definition and credential
        the thread is configured with, here rather than at declaration, so an
        agent can be copied and changed before it is built.

        :func:`~lup.providers.codex.launch.compiled_codex` is the half a
        launch shares; what only an in-process session has is added here.
        The declared policy becomes hooks the app-server asks through,
        ahead of the declared ones, and the identity joins the roster
        through the environment the session's tool servers read.
        """
        declared = self.config
        compiled = compiled_codex(declared)
        config = compiled.model_copy(
            update={
                "environment": {
                    **compiled.environment,
                    **codex_account_environment(declared),
                }
            }
        )
        policy = declared.enforced_policy()
        if policy is not None:
            judged = semantic_hooks(policy, declared.sandbox, CODEX_SEMANTICS)
            hooks = (
                judged if config.hooks is None else merge_hooks(judged, config.hooks)
            )
            config = config.model_copy(
                update={
                    "hooks": hooks,
                    "approval_policy": config.approval_policy
                    or codex_hook_approval_policy(hooks),
                }
            )
        if declared.identity is None:
            return config
        member = launched_member(
            declared.workspace(), declared.identity.name
        ).environment()
        return config.model_copy(
            update={"environment": {**config.environment, **member}}
        )


class CodexServing(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """A declaration's MCP servers, as one Codex session is served them.

    A server this library hosts answers in this process, each of its tools a
    dynamic tool of the thread named ``lup_app_``, its server and its own name,
    with whatever runs beside it running here too. An external one is a
    subprocess the app-server starts from its declared transport.
    """

    servers: dict[str, CodexMcpServerConfig] = {}
    applications: dict[str, LupMcpTool] = {}
    companions: list[ServerCompanion] = []


def codex_mcp_server(name: str, server: McpServerEntry) -> CodexMcpServerConfig:
    """Narrow one external server into the subprocess Codex launches.

    Only a stdio transport has a Codex spelling: the app-server starts its
    servers as subprocesses and reaches no networked one this adapter could
    name, so any other transport is refused rather than approximated.
    """
    match server:
        case {"command": str(command)}:
            stdio: RawStdioServerConfig = server
            return CodexMcpServerConfig(
                command=command,
                args=list(stdio["args"]) if "args" in stdio else [],
                env=dict(stdio["env"]) if "env" in stdio else {},
            )
        case _:
            raise ValueError(
                f"Codex starts tool server {name!r} as a subprocess, and it is "
                "declared with a networked transport; declare a stdio command"
            )


def codex_serving(entries: dict[str, McpServerEntry]) -> CodexServing:
    """Sort built servers into what this process answers and what Codex starts."""
    hosted = {
        name: entry
        for name, entry in entries.items()
        if isinstance(entry, LupMcpServerConfig)
    }
    applications = [
        (f"lup_app_{name}__{tool.name}", tool)
        for name, entry in hosted.items()
        for tool in entry.tools
    ]
    named = [name for name, _tool in applications]
    if len(named) != len(dict.fromkeys(named)):
        raise ValueError(
            "hosted server/tool names collide in Codex; rename the ambiguous server or tool"
        )
    for name in named:
        if (
            not name.isascii()
            or not all(char.isalnum() or char in "_-" for char in name)
            or len(name) > 64
        ):
            raise ValueError(
                f"Codex cannot name dynamic tool {name!r}: it takes at most 64 "
                "ASCII letters, digits, '_' and '-'; rename the server or tool"
            )
    return CodexServing(
        servers={
            name: codex_mcp_server(name, entry)
            for name, entry in entries.items()
            if name not in hosted
        },
        applications=dict(applications),
        companions=[
            companion for entry in hosted.values() for companion in entry.companions
        ],
    )


async def codex_sessions(config: Codex) -> list[SessionSummary]:
    """The threads Codex keeps for an agent's workspace, newest first.

    Asked of an app-server started under the environment the agent's sessions
    run with — its profile's home, where it names one — so the home it reads
    is the one they write to. It starts no thread and installs nothing:
    listing is a read.
    """
    account = codex_account_environment(config)
    environment = native_environment({**config.environment, **account})
    if not config.sandbox.posture().contained():
        environment = {**environment, CODEX_HOME: str(native_home(environment))}
    server = CodexAppServer(config.executable, environment=environment)
    await server.start()

    async def pages() -> AsyncIterator[CodexThreadPage]:
        cursor: str | None = None
        while True:
            params: JsonObject = {
                "cwd": str(config.workspace()),
                "sourceKinds": list(LISTED_THREAD_SOURCES),
            }
            if cursor is not None:
                params["cursor"] = cursor
            page = CodexThreadPage.model_validate(
                await server.request("thread/list", params)
            )
            yield page
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    try:
        listed = [thread async for page in pages() for thread in page.data]
    finally:
        await server.close()
    summaries = [thread.summary() for thread in listed]
    return sorted(summaries, key=lambda summary: summary.updated_at, reverse=True)


async def submit_completed_output(
    submission: TurnSubmission,
    output: CodexOutputContract,
    completed: CompletedTurn,
    identifiers: TurnIdentifiers,
) -> None:
    """Validate a native final answer and retain actionable correction evidence."""
    text = next(
        (
            text
            for block in reversed(completed.blocks)
            if (text := block.text_payload) is not None
        ),
        "",
    )
    try:
        value = output.decode(text)
    except ValidationError as error:
        message = f"Final output is not valid JSON: {error}"
    else:
        response = await submission.submit(value)
        if response.accepted:
            return
        message = response.message
    raise StructuredOutputError(
        TurnFailure(
            message=message,
            blocks=completed.blocks,
            messages=completed.messages,
            usage=completed.usage,
            duration=completed.duration,
            identifiers=identifiers,
            validation_history=[ValidationAttempt(message=message)],
        )
    )


def recorded_message(item: JsonObject) -> TurnMessage | None:
    """One item of a thread's record as the message a live turn made of it."""
    blocks = decode_completed_item(item)
    if not blocks:
        return None
    return TurnMessage(role=message_role(item), blocks=blocks, native=item)


def decode_usage(payload: JsonObject) -> Usage:
    """Decode one app-server token-usage breakdown."""
    native = TokenUsageBreakdown.model_validate(payload)
    return Usage(
        input_tokens=native.input_tokens,
        output_tokens=native.output_tokens,
        cache_read_input_tokens=native.cached_input_tokens,
    )


def message_role(payload: JsonObject) -> Literal["user", "assistant", "tool", "system"]:
    """Which transcript role one completed item belongs to.

    A tool call and its result are the model's own act and the environment's
    reply, and collapsing both into one assistant message is what made a
    trace unable to show a call beside the result it produced.
    """
    match payload:
        case (
            {"type": "commandExecution"}
            | {"type": "fileChange"}
            | {"type": "mcpToolCall"}
            | {"type": "functionCallOutput"}
            | {"type": "webSearch"}
            | {"type": "imageView"}
            | {"type": "imageGeneration"}
            | {"type": "collabAgentToolCall"}
            | {"type": "sleep"}
        ):
            return "tool"
        case {"type": "userMessage"}:
            return "user"
        case {
            "type": "hookPrompt"
            | "contextCompaction"
            | "enteredReviewMode"
            | "exitedReviewMode"
            | "subAgentActivity"
        }:
            return "system"
        case _:
            return "assistant"


def prompt_text(part: JsonValue) -> str | None:
    """The words one part of a prompt carries, where it is words at all."""
    match part:
        case {"type": "text", "text": str(words)}:
            return words
        case _:
            return None


def decode_completed_item(payload: JsonObject) -> list[AnyTurnBlock]:
    """Decode one typed completed app-server item into canonical blocks."""
    from lup.sessions.events import (
        TurnTextBlock,
        TurnThinkingBlock,
        TurnToolCallBlock,
        TurnToolResultBlock,
        TurnNativeActivityBlock,
    )

    native = CodexItemOutcome.model_validate(payload)
    match payload:
        case {"type": "agentMessage", "text": str(text)}:
            return [TurnTextBlock(text=text)]
        case {"type": "userMessage", "content": list(content)}:
            said: list[AnyTurnBlock] = [
                TurnTextBlock(text=words)
                for part in content
                if (words := prompt_text(part)) is not None
            ]
            return said or [
                TurnNativeActivityBlock(
                    provider="codex", activity=native.activity, payload=payload
                )
            ]
        case {"type": "reasoning"}:
            reasoning = CodexReasoningItem.model_validate(payload)
            return [
                TurnThinkingBlock(
                    thinking="\n".join([*reasoning.summary, *reasoning.content])
                )
            ]
        case {
            "type": "commandExecution",
            "id": str(identifier),
            "command": str(command),
            "aggregatedOutput": output,
            "status": status,
        }:
            blocks: list[AnyTurnBlock] = [
                TurnToolCallBlock(
                    id=identifier,
                    name="ShellCommand",
                    arguments={"command": command},
                ),
                TurnToolResultBlock(
                    tool_call_id=identifier,
                    content=output if isinstance(output, str) else "",
                    is_error=status != "completed" or native.failed,
                ),
            ]
            return blocks
        case {
            "type": "fileChange",
            "id": str(identifier),
            "changes": list(changes),
            "status": status,
        }:
            blocks = [
                TurnToolCallBlock(
                    id=identifier,
                    name="EditBatch",
                    arguments={"changes": changes},
                ),
                TurnToolResultBlock(
                    tool_call_id=identifier,
                    content=str(status),
                    is_error=status != "completed",
                ),
            ]
            return blocks
        case {
            "type": "mcpToolCall",
            "id": str(identifier),
            "server": str(server),
            "tool": str(tool),
            "arguments": arguments,
            "status": status,
        }:
            encoded_arguments: JsonObject = (
                {str(key): value for key, value in arguments.items()}
                if isinstance(arguments, dict)
                else {"value": arguments}
            )
            blocks = [
                TurnToolCallBlock(
                    id=identifier,
                    name=f"mcp__{server}__{tool}",
                    arguments=encoded_arguments,
                ),
                TurnToolResultBlock(
                    tool_call_id=identifier,
                    content=json.dumps(payload, sort_keys=True),
                    is_error=status != "completed",
                ),
            ]
            return blocks
        case {
            "type": "dynamicToolCall",
            "id": str(identifier),
            "tool": str(tool),
            "arguments": arguments,
            "status": status,
        }:
            encoded_arguments: JsonObject = (
                {str(key): value for key, value in arguments.items()}
                if isinstance(arguments, dict)
                else {"value": arguments}
            )
            blocks = [
                TurnToolCallBlock(
                    id=identifier,
                    name=tool,
                    arguments=encoded_arguments,
                ),
                TurnToolResultBlock(
                    tool_call_id=identifier,
                    content=json.dumps(payload, sort_keys=True),
                    is_error=status != "completed" or native.failed,
                ),
            ]
            return blocks
        case {"type": "functionCallOutput", "id": str(identifier)}:
            return [
                TurnToolResultBlock(
                    tool_call_id=identifier, content=json.dumps(payload, sort_keys=True)
                )
            ]
        case {
            "type": (
                "webSearch"
                | "imageView"
                | "collabAgentToolCall"
                | "imageGeneration"
                | "sleep"
            ) as activity,
            "id": str(identifier),
        }:
            return [
                TurnToolCallBlock(id=identifier, name=activity, arguments=payload),
                TurnToolResultBlock(
                    tool_call_id=identifier,
                    content=json.dumps(payload, sort_keys=True),
                    is_error=native.failed,
                ),
            ]
        case _:
            return [
                TurnNativeActivityBlock(
                    provider="codex",
                    activity=native.activity,
                    payload=payload,
                )
            ]
