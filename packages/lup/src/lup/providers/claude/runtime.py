"""Claude sessions opened through the Agent SDK, with per-turn MCP tool rebinding."""

import asyncio
import json
import logging
import shutil
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from functools import partial
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import uuid4

from mcp.server import Server, ServerRequestContext
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
from pydantic import BaseModel, TypeAdapter

from lup.tools.mcp import (
    LupMcpServerConfig,
    McpServerEntry,
    relay_recursive_agent_to_mcp,
    running_companions,
)
from lup.execution.threads import run_sync
from lup.providers.claude import (
    SUBMISSION_TOOL,
    Claude,
    ClaudeSession,
)
from lup.providers.claude.config_home import session_config_home
from lup.providers.claude.transcripts import ClaudeTranscripts, result_text
from lup.mcp import hosted_servers, opened_needs
from lup.coordination.repository import launched_member
from lup.launch.companions import CompanionLaunch, Joined, held_around
from lup.launch.compilation import (
    allowance_environment,
    inherited_environment,
    kept_record,
    semantic_hooks,
)
from lup.launch.declaration import Reopening, session_loopback
from lup.policy.hooks import merge_hooks
from lup.providers.claude.hooks import CLAUDE_SEMANTICS
from lup.providers.claude.launch import claude_server, claude_settings, compiled_claude
from lup.providers.claude.model_choice import claude_effort
from lup.sessions.recursion import (
    recursive_agent_allowance,
    recursive_agent_scope,
)
from lup.sessions.composition import AcceptedTurn, CompletedTurn, ComposedSession
from lup.sessions.capabilities import (
    ConversationRecord,
    EventStream,
    ForkSession,
    Interrupt,
    SessionEngine,
    TurnToolBinder,
)
from lup.sessions.errors import (
    DeltaStreamingDisabled,
    ProviderTurnError,
    QuotaExceededError,
    TurnError,
    TurnFailure,
    TurnInterruptedError,
)
from lup.sessions.events import (
    BlockCompletedEvent,
    BlockDeltaEvent,
    LiveTurnEvent,
    MessageCompletedEvent,
    SessionId,
    AnyTurnBlock,
    TurnIdentifiers,
    TurnId,
    TurnCompletedEvent,
    TurnEvent,
    TurnStartedEvent,
    TurnMessage,
    TurnToolBinding,
)
from lup.sessions.transcript import fold_blocks, fold_transcript
from lup.sessions.output import TurnSubmission, bound_submission
from lup.types import (
    EnvVars,
    JsonValue,
    Usage,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import claude_agent_sdk as claude
    from claude_agent_sdk import types as claude_types


type SubmissionBindingSource = Callable[[], TurnSubmission | None]


def attach_cli_stderr(error: Exception, stderr_lines: deque[str]) -> None:
    """Splice captured CLI stderr into the SDK's opaque process error.

    The SDK raises ``ProcessError`` carrying a fixed "Check stderr output for
    details" and never attaches the stderr it is pointing at, even when a
    stderr callback is set — so the exception names where the answer would be
    instead of carrying it, and every message built from it downstream
    inherits the same blank. Rewriting the message from the captured tail
    turns a blind ``exit code 1`` into the reason the process died, in place,
    so callers that only ever read ``str(error)`` need to know nothing about
    this. The ``exit code N`` token survives the rewrite, leaving exit-code
    matching reading what it read before.

    Anything that is not a ``ProcessError``, or a process that died saying
    nothing, is left exactly as it arrived.
    """
    from claude_agent_sdk import ProcessError

    if not isinstance(error, ProcessError):
        return

    tail = "\n".join(stderr_lines).strip()
    if not tail:
        return

    error.stderr = tail
    error.args = (
        f"Command failed with exit code {error.exit_code}\nError output: {tail}",
    )


ENVIRONMENTAL_SIGNATURES: tuple[str, ...] = (
    "oauth access token has been revoked",
    "failed to authenticate",
    "authentication_error",
    "invalid api key",
    "not logged in",
    "please run /login",
    "credit balance is too low",
    "session limit",
    "usage limit",
    "rate limit",
    "rate_limit_error",
    "overloaded_error",
    "api error: 401",
    "api error: 403",
    "api error: 429",
    "api error: 500",
    "api error: 502",
    "api error: 503",
    "api error: 529",
    "connection error",
    "connection reset",
    "temporary failure in name resolution",
)
"""Which CLI failures name the host rather than the work.

Claude Code reports an expired login, an exhausted allowance and a refused
upstream as ordinary process output rather than as typed exceptions, so
reading its words is the only place the distinction exists. Matched as
lowercase substrings against the whole message, since the CLI wraps the same
cause differently depending on where it surfaced.

Our judgement rather than the provider's vocabulary, so a caller replaces
it instead of forking this module. Over-matching costs a run that parks and
resumes; under-matching costs a concern recorded as having failed when its
credential died, which is the direction worth erring away from.
"""


def environmental_fault(
    message: str, signatures: tuple[str, ...] = ENVIRONMENTAL_SIGNATURES
) -> bool:
    """Whether this failure's own words name the host rather than the turn.

    Takes the message rather than the exception, because the message is what
    survives: several layers above the adapter re-wrap a raw exception into a
    fresh failure carrying `str(error)` and nothing else, so a caller reading
    a failure long after it was raised has the words and not the object.
    """
    return any(signature in message.casefold() for signature in signatures)


def quota_exhausted(
    spent: "claude_types.RateLimitInfo | None",
    blocks: list[AnyTurnBlock],
    identifiers: TurnIdentifiers,
) -> QuotaExceededError:
    """Build the typed exhaustion carrying the provider's own reset moment.

    A 429 with no accompanying rate-limit event says only that the allowance
    is gone, so the reset stays ``None`` and a waiter falls back to its own
    interval rather than inventing a time the provider never gave.
    """
    reset_at = (
        datetime.fromtimestamp(spent.resets_at, tz=UTC)
        if spent is not None and spent.resets_at is not None
        else None
    )
    until = f" until {reset_at.isoformat()}" if reset_at is not None else ""
    return QuotaExceededError(
        TurnFailure(
            message=f"Claude account allowance exhausted{until}",
            blocks=blocks,
            identifiers=identifiers,
            environmental=True,
        ),
        reset_at=reset_at,
        quota_type=spent.rate_limit_type if spent is not None else None,
    )


HUMAN_CLEARED_SIGNATURES: tuple[str, ...] = (
    "account allowance exhausted",
    "you've hit your session limit",
    "oauth access token has been revoked",
    "failed to authenticate",
    "authentication_error",
    "invalid api key",
    "not logged in",
    "please run /login",
    "credit balance is too low",
    "api error: 401",
    "api error: 403",
)
"""Which host faults stay broken until a person does something.

A dead credential, an empty balance and an exhausted account allowance need
an operator's account choice. A reported allowance reset does not require
waiting: re-login or account switching may clear it. Transient rate limits,
overload and unreachable upstreams retain the caller's bounded retry policy.

Our judgement rather than the provider's vocabulary, so a caller replaces it
instead of forking this module. Over-matching costs a run that stops when it
could have waited; under-matching costs one that sleeps through a credential
nobody is going to renew, so this errs toward naming what a person must fix.
"""


def needs_a_person(
    message: str, signatures: tuple[str, ...] = HUMAN_CLEARED_SIGNATURES
) -> bool:
    """Whether this host fault stays broken until somebody acts on it."""
    return any(signature in message.casefold() for signature in signatures)


ROTATION_AMBIGUOUS_SIGNATURES: tuple[str, ...] = (
    "oauth access token has been revoked",
    "failed to authenticate",
    "authentication_error",
    "not logged in",
    "please run /login",
    "api error: 401",
)
"""Which of those faults a sibling's token refresh produces identically.

Concurrent sessions share one credential file, because the alternative is
worse: a private copy each would mean the first refresh rotates the token and
strands every other copy for good. Sharing costs a race instead. A refresh
rotates, so the moment one session renews, every sibling still holding the
previous token is denied — in the words the provider also uses for a
credential nobody is going to renew.

The two are indistinguishable in the message and opposite in what they need,
so the run asks the only question that separates them: a session opened
afresh reads the rotated file and succeeds, where a dead credential fails
again. One probe is the whole difference, and it costs a delay to be wrong.

An empty balance and a rejected key are absent deliberately. Neither is
reachable by rotation, so probing one only postpones handing it back.
"""


def may_be_a_rotation(
    message: str, signatures: tuple[str, ...] = ROTATION_AMBIGUOUS_SIGNATURES
) -> bool:
    """Whether a fresh session could find this fault already cleared."""
    return any(signature in message.casefold() for signature in signatures)


def turn_error(interrupt: "ClaudeInterrupt") -> type[TurnError]:
    """Classify a failed turn the way Codex's terminal status does."""
    return TurnInterruptedError if interrupt.requested else ProviderTurnError


class ClaudeForkPoint(BaseModel, frozen=True):
    """Where a fork branches: the conversation, and the last message it carries.

    ``through`` is a message of the parent's transcript, the last one the fork
    keeps; ``None`` keeps everything the parent holds when the fork opens.
    """

    parent: str
    through: str | None = None


class ClaudeConversationState:
    """Adapter-private reconnect/resume state for one Lup session.

    A new conversation's id is dictated rather than waited for, so the
    session answers ``id`` from the moment it opens and a transcript is
    filed under the id it answers. The CLI's own report of the id is still
    adopted wherever it differs, because the transcript is filed under
    whatever the CLI says.
    """

    def __init__(
        self,
        opener: "ClaudeSessionOpener",
        config: Claude,
        servers: Mapping[str, McpServerEntry],
        resume: SessionId | None,
        fork: ClaudeForkPoint | None = None,
    ) -> None:
        self.opener = opener
        self.config = config
        self.servers = servers
        """The declaration's MCP servers, built once for this session."""
        self.resume = resume.value if resume is not None else None
        self.forking = fork
        self.session_id = self.resume or str(uuid4())
        self.recorded = self.resume is not None or fork is not None
        """Whether the provider holds a transcript of this conversation yet."""
        self.turn_points: dict[TurnId, str] = {}
        """The last transcript message of each turn this session took."""
        self.client: claude.ClaudeSDKClient | None = None
        self.submission: TurnSubmission | None = None
        self.schema_digest: str | None = None
        self.completion: asyncio.Task[CompletedTurn] | None = None
        self.stderr_lines: deque[str] = deque(maxlen=self.config.stderr_tail_lines)

    def current_submission(self) -> TurnSubmission | None:
        """Resolve the submission a live connection's tool should serve."""
        return self.submission

    async def settle_reader(self) -> None:
        """Unwind an unfinished turn's read before its transport goes away.

        Leaving a session interrupts the active turn without awaiting it, so
        the reader can still be suspended inside `receive_response()` when the
        transport closes that generator underneath it. Cancelling is what
        bounds the wait: awaiting the turn itself would hang teardown on any
        turn that never terminates.
        """
        completion = self.completion
        self.completion = None
        if completion is None or completion.done():
            return
        completion.cancel()
        await asyncio.wait([completion])

    async def disconnect(self) -> None:
        if self.client is None:
            return
        await self.settle_reader()
        try:
            await self.client.disconnect()
        finally:
            self.client = None

    def options(self) -> "claude.ClaudeAgentOptions":
        """The options this state's next connection opens with.

        A new conversation is started under the id this state dictates; a
        fork, until the CLI has made it, resumes its parent as a branch under
        that id; anything else resumes the conversation it holds.
        """
        match self.forking, self.resume:
            case ClaudeForkPoint(parent=parent, through=through), _:
                return build_claude_options(
                    self.config,
                    servers=self.servers,
                    binding=self.current_submission,
                    resume=parent,
                    session_id=self.session_id,
                    fork_session=True,
                    resume_session_at=through,
                )
            case None, None:
                return build_claude_options(
                    self.config,
                    servers=self.servers,
                    binding=self.current_submission,
                    resume=None,
                    session_id=self.session_id,
                )
            case None, resumed:
                return build_claude_options(
                    self.config,
                    servers=self.servers,
                    binding=self.current_submission,
                    resume=resumed,
                    session_id=None,
                )

    def fork_point(self, at: TurnId | None) -> ClaudeForkPoint:
        """Where a fork of this conversation branches, at ``at`` or at its end."""
        if not self.recorded:
            raise ValueError(
                "this conversation has no turn yet, so there is nothing to fork; "
                "ask it something first, or open a new session instead"
            )
        if at is None:
            return ClaudeForkPoint(parent=self.session_id)
        if at not in self.turn_points:
            raise ValueError(
                f"turn {at.value!r} is not one this session took, so it names no "
                "point in the transcript; fork at a turn whose result this session "
                "returned, or pass no turn to fork at the latest"
            )
        return ClaudeForkPoint(parent=self.session_id, through=self.turn_points[at])

    async def connect(self) -> "claude.ClaudeSDKClient":
        if self.client is not None:
            return self.client
        options = self.options()
        # Connecting is where a refused resume surfaces, and it is as much a
        # failed turn as one that breaks midway — so it leaves through the
        # portable error the rest of the runtime raises. Escaping as the SDK's
        # own exception would take it past every caller that handles a turn
        # failing, so a resume the provider has lost would end a whole run.
        try:
            self.client = await self.connected(options)
        except Exception as error:
            if self.resume is None or self.forking is not None:
                raise ProviderTurnError(
                    TurnFailure(
                        message=str(error),
                        environmental=environmental_fault(str(error)),
                    )
                ) from error
            # The provider no longer holds what this state was resuming. A
            # turn that cannot reach its history still beats one that cannot
            # happen, so the conversation is forgotten rather than the run —
            # under an id of its own, since the lost one may yet be on disk.
            logger.warning(
                "Claude refused to resume session %s (%s); continuing on a new one",
                self.resume,
                error,
            )
            self.resume = None
            self.recorded = False
            self.session_id = str(uuid4())
            try:
                self.client = await self.connected(self.options())
            except Exception as fresh_error:
                raise ProviderTurnError(
                    TurnFailure(
                        message=str(fresh_error),
                        environmental=environmental_fault(str(fresh_error)),
                    )
                ) from fresh_error
        return self.client

    async def connected(
        self, options: "claude.ClaudeAgentOptions"
    ) -> "claude.ClaudeSDKClient":
        """One connected client for these options, or the failure that stopped it."""
        import claude_agent_sdk as claude

        options.stderr = self.stderr_lines.append
        client = claude.ClaudeSDKClient(options=options)
        try:
            await client.connect()
        except Exception as error:
            attach_cli_stderr(error, self.stderr_lines)
            raise
        return client

    async def start_turn(self, text: str) -> AcceptedTurn:
        client = await self.connect()
        await client.query(text, session_id=self.session_id)
        identifiers = TurnIdentifiers(
            session=SessionId(value=self.session_id),
            turn=TurnId(value=uuid4().hex),
        )
        events: asyncio.Queue[LiveTurnEvent | None] = asyncio.Queue()
        events.put_nowait(TurnStartedEvent(identifiers=identifiers))
        interrupt = ClaudeInterrupt(self)

        async def complete() -> CompletedTurn:
            from claude_agent_sdk import types as claude_types

            nonlocal identifiers
            durable: list[TurnEvent] = []  # lup: ignore[empty-collection]
            result: claude_types.ResultMessage | None = None
            last_message: str | None = None
            exhausted: claude_types.RateLimitInfo | None = None
            started = perf_counter()

            def record(message: TurnMessage) -> None:
                """Emit one message and its blocks, so both views agree."""
                for block in message.blocks:
                    completed = BlockCompletedEvent(
                        identifiers=identifiers, block=block
                    )
                    durable.append(completed)
                    events.put_nowait(completed)
                whole = MessageCompletedEvent(identifiers=identifiers, message=message)
                durable.append(whole)
                events.put_nowait(whole)

            try:
                async for message in client.receive_response():
                    match message:
                        case claude_types.AssistantMessage(
                            content=content,
                            parent_tool_use_id=delegated_under,
                            model=message_model,
                            message_id=message_id,
                            uuid=written,
                        ):
                            last_message = written or last_message
                            record(
                                TurnMessage(
                                    role="assistant",
                                    blocks=[
                                        convert_claude_block(block) for block in content
                                    ],
                                    parent_tool_call_id=delegated_under,
                                    model=message_model,
                                    message_id=message_id,
                                )
                            )
                        case claude_types.UserMessage(
                            content=content,
                            parent_tool_use_id=delegated_under,
                            uuid=written,
                        ) if isinstance(content, list):
                            last_message = written or last_message
                            record(
                                TurnMessage(
                                    role="tool",
                                    blocks=[
                                        convert_claude_block(block) for block in content
                                    ],
                                    parent_tool_call_id=delegated_under,
                                )
                            )
                        case claude_types.UserMessage(content=str(text)):
                            from lup.sessions.events import TurnTextBlock

                            record(
                                TurnMessage(
                                    role="user", blocks=[TurnTextBlock(text=text)]
                                )
                            )
                        case claude_types.ResultMessage() as terminal:
                            result = terminal
                        # The provider says an allowance is spent, and says
                        # when it returns. Read as data rather than matched
                        # out of the error text, that reset time is what a
                        # waiter can sleep against exactly.
                        case claude_types.RateLimitEvent(
                            rate_limit_info=claude_types.RateLimitInfo(
                                status="rejected"
                            ) as spent
                        ):
                            exhausted = spent
                        # The CLI files the transcript under the id it
                        # reports here, which is the one this side dictated
                        # unless the CLI chose otherwise. From here on the
                        # conversation exists, so a reconnect resumes it and a
                        # fork is no longer pending.
                        case claude_types.SystemMessage(
                            subtype="init", data={"session_id": str(adopted)}
                        ):
                            self.session_id = adopted
                            self.resume = adopted
                            self.forking = None
                            self.recorded = True
                            identifiers = identifiers.model_copy(
                                update={"session": SessionId(value=adopted)}
                            )
                        case claude_types.StreamEvent(event=event):
                            match event:
                                case {
                                    "type": "content_block_delta",
                                    "delta": {"text": str(delta)},
                                } | {
                                    "type": "content_block_delta",
                                    "delta": {"thinking": str(delta)},
                                }:
                                    events.put_nowait(
                                        BlockDeltaEvent(
                                            identifiers=identifiers,
                                            delta=delta,
                                        )
                                    )
            except Exception as error:
                attach_cli_stderr(error, self.stderr_lines)
                raise turn_error(interrupt)(
                    TurnFailure(
                        message=str(error),
                        blocks=fold_blocks(durable),
                        duration=timedelta(seconds=perf_counter() - started),
                        identifiers=identifiers,
                        environmental=environmental_fault(str(error)),
                    )
                ) from error

            if exhausted is not None or (
                result is not None and result.api_error_status == 429
            ):
                raise quota_exhausted(exhausted, fold_blocks(durable), identifiers)
            if result is None:
                raise ProviderTurnError(
                    TurnFailure(
                        message="Claude completed without a terminal result",
                        blocks=fold_blocks(durable),
                        identifiers=identifiers,
                    )
                )
            if result.session_id is not None:
                self.session_id = result.session_id
                self.resume = result.session_id
            self.forking = None
            self.recorded = True
            if last_message is not None:
                self.turn_points[identifiers.turn] = last_message
            messages = fold_transcript(durable)
            blocks = fold_blocks(durable)
            usage = claude_usage(result.usage, total_cost_usd=result.total_cost_usd)
            duration = timedelta(milliseconds=result.duration_ms or 0)
            if result.is_error:
                raise turn_error(interrupt)(
                    TurnFailure(
                        message=str(result.result or "Claude turn failed"),
                        blocks=blocks,
                        usage=usage,
                        duration=duration,
                        identifiers=identifiers,
                    )
                )
            return CompletedTurn(
                messages=messages,
                blocks=blocks,
                usage=usage,
                duration=duration,
                identifiers=identifiers,
            )

        async def complete_with_events() -> CompletedTurn:
            try:
                return await complete()
            finally:
                events.put_nowait(TurnCompletedEvent(identifiers=identifiers))
                events.put_nowait(None)

        completion = asyncio.create_task(complete_with_events())
        self.completion = completion

        def observe_completion(task: asyncio.Task[CompletedTurn]) -> None:
            if not task.cancelled():
                task.exception()

        completion.add_done_callback(observe_completion)

        async def await_completion() -> CompletedTurn:
            return await completion

        return AcceptedTurn(
            identifiers=identifiers,
            complete=await_completion,
            events=ClaudeLiveEventStream(events, self.config.delta_streaming),
            interrupt=interrupt,
        )


class ClaudeLiveEventStream(EventStream):
    """One ordered queue, viewed either with in-flight deltas or without.

    The durable view filters the same sequence rather than reading a second
    one, so the two can never report different histories.
    """

    def __init__(
        self,
        events: asyncio.Queue[LiveTurnEvent | None],
        delta_streaming: bool = False,
    ) -> None:
        self.queue = events
        self.consumed = False
        self.delta_streaming = delta_streaming

    async def iterate(self) -> AsyncIterator[LiveTurnEvent]:
        if self.consumed:
            raise RuntimeError("live event stream can only be consumed once")
        self.consumed = True
        while (event := await self.queue.get()) is not None:
            yield event

    async def durable(self) -> AsyncIterator[TurnEvent]:
        async for event in self.iterate():
            if (durable := event.durable) is not None:
                yield durable

    def events(self) -> AsyncIterator[TurnEvent]:
        return self.durable()

    def live(self) -> AsyncIterator[LiveTurnEvent]:
        if not self.delta_streaming:
            raise DeltaStreamingDisabled(
                "this session was built without partial message streaming"
            )
        return self.iterate()


class ClaudeTurnToolBinder(TurnToolBinder):
    """Refresh handler state in place and reconnect only to change the schema.

    A connection advertises its submission schema once, so a turn that asks for
    a different one has to reconnect. A turn that asks for the same one does
    not, and reconnecting anyway would spend the conversation to install a tool
    identical to the one already there — which is what every worker turn does,
    against a provider that no longer persists a transcript to resume.
    """

    def __init__(self, state: ClaudeConversationState) -> None:
        self.state = state

    async def bind[T: BaseModel](self, binding: TurnToolBinding[T] | None) -> None:
        if binding is None and self.state.submission is None:
            return
        submission = bound_submission(binding) if binding is not None else None
        digest = submission.digest if submission is not None else None
        if digest != self.state.schema_digest:
            await self.state.disconnect()
        self.state.schema_digest = digest
        self.state.submission = submission


class ClaudeInterrupt(Interrupt):
    """Interrupt the currently connected Claude turn, and remember asking.

    The SDK ends an interrupted turn the way it ends a failed one, so what
    separates them is only that someone asked. Recording the request is what
    lets the turn raise as interrupted rather than as a provider failure the
    recovery wrapper would dutifully retry.
    """

    def __init__(self, state: ClaudeConversationState) -> None:
        self.state = state
        self.requested = False

    async def interrupt(self) -> None:
        self.requested = True
        if self.state.client is not None:
            await self.state.client.interrupt()


class ClaudeRecord(ConversationRecord):
    """This conversation as Claude Code keeps it: its id and its transcript."""

    def __init__(self, state: ClaudeConversationState) -> None:
        self.state = state

    def identity(self) -> SessionId:
        return SessionId(value=self.state.session_id)

    async def messages(self) -> list[TurnMessage]:
        """The transcript under this session's own configuration home.

        A conversation with no turn yet has no transcript, and reads as
        empty. One that has taken a turn and still has none — persistence
        switched off, or a home that is not the one the CLI wrote to — is
        refused rather than reported as a conversation that never happened.
        """
        config = self.state.config
        transcripts = ClaudeTranscripts(session_config_home(config.environment))
        session = self.identity()
        found = await run_sync(partial(transcripts.conversation, session, config.cwd))
        if found is not None:
            return found
        if self.state.recorded:
            raise LookupError(
                f"Claude Code keeps no transcript of session {session.value} "
                f"under {transcripts.config_home}"
            )
        return []


class ClaudeFork(ForkSession[ClaudeSession]):
    """Branch this conversation into a new one the CLI files under its own id.

    The CLI makes the branch itself, resuming the parent with its fork flags,
    so the new transcript lands under the session's own configuration home
    rather than under whichever home this process names.
    """

    def __init__(self, state: ClaudeConversationState) -> None:
        self.state = state

    def fork(
        self, at: TurnId | None = None
    ) -> AbstractAsyncContextManager[ClaudeSession]:
        return self.state.opener.open_session(fork=self.state.fork_point(at))


async def resumed_session(config: Claude, resume: Reopening | None) -> SessionId | None:
    """The conversation a declared reopening names, as a session opened here resumes one.

    ``Latest`` is the newest conversation on record for this workspace, the
    one ``claude --continue`` takes; a picker is a terminal's, and refused.
    """
    return None if resume is None else await resume.reopened(config.sessions)


class ClaudeSessionOpener:
    """Open independently configured reconnecting Claude sessions."""

    def __init__(self, config: Claude) -> None:
        self.config = Claude.model_validate(config)

    def compiled(self) -> Claude:
        """The declaration as a session opened in this process runs it.

        :func:`~lup.providers.claude.launch.compiled_claude` is the half a
        launch shares; what only an in-process session has is added here. The
        declared policy becomes hooks judging each call ahead of the declared
        ones, the identity joins the roster through the environment the
        session's tool servers read, and a plugin — delegated authority —
        needs Claude Code's stock tools to open at all.
        """
        declared = self.config
        if (declared.plugin is not None or declared.plugin_dirs) and (
            declared.tools.builtin != "stock"
        ):
            raise ValueError(
                "a plugin can introduce delegated authority, so a session "
                "opened here with one requires tools=ClaudeTools(builtin='stock')"
            )
        config = compiled_claude(declared)
        policy = declared.enforced_policy()
        if policy is not None:
            judged = semantic_hooks(policy, declared.sandbox, CLAUDE_SEMANTICS)
            hooks = (
                judged if config.hooks is None else merge_hooks(judged, config.hooks)
            )
            config = config.model_copy(update={"hooks": hooks})
        if declared.identity is None:
            return config
        member = launched_member(
            declared.cwd or Path.cwd(), declared.identity.name
        ).environment()
        return config.model_copy(
            update={"environment": {**config.environment, **member}}
        )

    @asynccontextmanager
    async def open_session(
        self, resume: Reopening | None = None, *, fork: ClaudeForkPoint | None = None
    ) -> AsyncGenerator[ClaudeSession]:
        """Open one session, its host companions held for as long as it is open."""
        declared = self.config
        launch = CompanionLaunch(
            root=declared.cwd or Path.cwd(),
            runtime="claude",
            environment={**inherited_environment(), **declared.environment},
            loopback=session_loopback(declared.plugin, declared.sandbox),
        )
        async with held_around(declared.companions, launch) as joined:
            for notice in joined.notices:
                logger.info("%s", notice.text)
            async with self.joined_session(joined, resume, fork=fork) as session:
                yield session

    @asynccontextmanager
    async def joined_session(
        self,
        joined: Joined,
        resume: Reopening | None = None,
        *,
        fork: ClaudeForkPoint | None = None,
    ) -> AsyncGenerator[ClaudeSession]:
        """Open one session reaching what its held companions hand it."""
        declared = self.compiled()
        compiled = declared.model_copy(
            update={
                "environment": {**declared.environment, **joined.environment},
                "sandbox": declared.sandbox.widened(joined.mounts),
            }
        )
        relayed = allowance_environment(
            compiled.max_recursive_agent, compiled.environment
        )
        allowance = recursive_agent_allowance(relayed)
        config = compiled.model_copy(
            update={"environment": {**compiled.environment, **relayed}}
        )
        reopened = await resumed_session(config, resume)
        # Built here rather than at declaration: a hosted server closes over
        # the session it serves — its identity, a container started for it,
        # a directory of its own that lives exactly as long as it does.
        scratch = TemporaryDirectory(prefix="lup-session-")
        servers = hosted_servers(
            config.tools.mcp,
            opened_needs(
                config.cwd or Path.cwd(), Path(scratch.name), config.environment
            ),
        )
        state = ClaudeConversationState(self, config, servers, reopened, fork)
        composed = ComposedSession(
            starter=state.start_turn,
            binder=ClaudeTurnToolBinder(state),
            gate_resolver=config.submission_gate_resolver,
            submission_tool=SUBMISSION_TOOL,
        )
        # lup: defer: A resolver run that parks can end on `an error occurred
        # during closing of asynchronous generator <ClaudeSessionOpener.
        # open_session>: RuntimeError: aclose(): asynchronous generator is
        # already running`, after the park output is complete and with exit
        # code 0 — so successful work reads as failed. Two candidates are
        # already refuted: `settle_reader` below was in the build that
        # reported it, and a concurrent double close of the actor's exit
        # stack cannot collide, because `AsyncExitStack.__aexit__` pops each
        # callback before awaiting it. What is left is that `asyncio.run`
        # cancels leftover tasks before finalizing async generators, so a
        # task still suspended inside this `finally` when the run returns
        # leaves the generator running for the finalizer's own `aclose` to
        # trip over — which means finding who closes a session without
        # awaiting it to completion. Needs a live parking run to confirm;
        # do not fix it from the shape alone.
        companions = [
            companion
            for server in servers.values()
            if isinstance(server, LupMcpServerConfig)
            for companion in server.companions
        ]

        @asynccontextmanager
        async def native() -> AsyncGenerator[SessionEngine]:
            kept = kept_record(
                "claude",
                ClaudeTranscripts(session_config_home(config.environment)),
                config.cwd or Path.cwd(),
                config.record,
                config.model_id(),
                config.profile,
            )
            with scratch, recursive_agent_scope(allowance), kept:
                async with running_companions(companions):
                    try:
                        yield composed
                    finally:
                        try:
                            await composed.abort_active()
                        finally:
                            await state.disconnect()

        resumed = SessionId(value=fork.parent) if fork is not None else reopened
        async with config.layers.around(native(), resumed) as engine:
            yield ClaudeSession(
                engine,
                ClaudeRecord(state),
                ClaudeFork(state),
                deltas=config.delta_streaming,
            )


def build_submission_server(
    current: SubmissionBindingSource,
) -> LupMcpServerConfig:
    """Build an exact-schema MCP server reading the binding the turn installed.

    The schema is fixed for a connection's lifetime because changing it
    reconnects, but a same-schema turn refreshes store and gate in place. So
    the handler resolves the binding when it runs rather than closing over the
    one that happened to be installed when the connection opened, which would
    write every later turn's output into the first turn's store.
    """

    async def list_tools(
        _context: ServerRequestContext[object],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        submission = current()
        if submission is None:
            return ListToolsResult(tools=[])
        return ListToolsResult(
            tools=[
                Tool(
                    name="submit_output",
                    description="Submit the final validated result for this turn.",
                    input_schema=submission.schema,
                )
            ]
        )

    async def call_tool(
        _context: ServerRequestContext[object], params: CallToolRequestParams
    ) -> CallToolResult:
        if params.name != "submit_output":
            raise ValueError(f"unknown output tool {params.name!r}")
        submission = current()
        if submission is None:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text="No matching turn output binding is active.",
                    )
                ],
                is_error=True,
            )
        response = await submission.submit(dict(params.arguments or {}))
        return CallToolResult(
            content=[TextContent(type="text", text=response.message)],
            is_error=not response.accepted,
        )

    server = Server(
        "lup-output", version="1", on_list_tools=list_tools, on_call_tool=call_tool
    )
    return LupMcpServerConfig(
        name="lup-output",
        server=server,
        tool_names=["submit_output"],
    )


def installed_claude(environment: EnvVars) -> Path | None:
    """The `claude` on the session's PATH, which is what an interactive launch runs.

    The SDK prefers the CLI it bundles, which lags the installed one — and the
    model catalog this library types against is read from the installed one,
    so a model it lists could be one the bundled CLI has never heard of. ``None``
    where nothing is installed, which is the one case the bundled CLI is for.
    """
    search = environment["PATH"] if "PATH" in environment else None
    found = shutil.which("claude", path=search)
    return Path(found) if found is not None else None


def claude_tool_allowed(
    name: str,
    roster: Sequence[str] | None,
    servers: Mapping[str, McpServerEntry],
) -> bool:
    """Whether a session holding ``roster`` and ``servers`` may call ``name``.

    An MCP tool is authorized by the server serving it and never by a
    built-in grant, so an ambient server a session did not declare stays
    refused even under Claude Code's preset.
    """
    if not name.startswith("mcp__"):
        return roster is None or name in roster
    return any(
        name in [f"mcp__{key}__{tool}" for tool in server.tool_names]
        if isinstance(server, LupMcpServerConfig)
        else name.startswith(f"mcp__{key}__")
        for key, server in servers.items()
    )


def build_claude_options(
    config: Claude,
    *,
    servers: Mapping[str, McpServerEntry],
    binding: SubmissionBindingSource,
    resume: str | None,
    session_id: str | None,
    fork_session: bool = False,
    resume_session_at: str | None = None,
) -> "claude.ClaudeAgentOptions":
    """Build SDK options lazily and never enable native structured output.

    ``servers`` are the declaration's MCP servers as this session built them,
    which is the one part of the options that is not read off ``config``.
    """
    import claude_agent_sdk as claude
    from claude_agent_sdk import types as claude_types
    from lup.providers.claude.hooks import lup_hooks_to_claude
    from lup.providers.claude.subagents import model_alias, subagent_tools

    config = Claude.model_validate(config)
    native = config.tools.roster()
    servers = dict(servers)
    allowed = list(config.allowed_tools)
    if binding() is not None:
        servers["lup-output"] = build_submission_server(binding)
        allowed.append(SUBMISSION_TOOL)

    async def enforce_grants(
        payload: claude_types.HookInput,
        _tool_use_id: str | None,
        _context: claude_types.HookContext,
    ) -> claude_types.HookJSONOutput:
        if payload["hook_event_name"] != "PreToolUse":
            return {}
        name = payload["tool_name"]
        if claude_tool_allowed(name, native, servers):
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"Tool {name!r} is outside this session's declared tools.",
            }
        }

    hooks = lup_hooks_to_claude(config.hooks) if config.hooks is not None else {}
    hooks.setdefault("PreToolUse", []).insert(
        0, claude_types.HookMatcher(hooks=[enforce_grants])
    )

    loaded = {server.name for server in config.tools.mcp if server.always_load}

    # The stock tools are Claude Code's coding agent, and that agent is its
    # tools and the system prompt that teaches them; a narrower grant keeps
    # only the caller's instructions, since the preset's describe tools the
    # session does not have.
    system_prompt: str | claude_types.SystemPromptPreset | None = (
        {
            "type": "preset",
            "preset": "claude_code",
            "append": config.system_prompt,
        }
        if native is None
        else config.system_prompt or None
    )
    chosen = config.resolved_effort()
    effort = claude_effort(chosen) if chosen is not None else None
    return claude.ClaudeAgentOptions(
        model=config.model_id(),
        system_prompt=system_prompt,
        tools=(
            {"type": "preset", "preset": "claude_code"}
            if native is None
            else list[str](native)
        ),
        allowed_tools=list(dict.fromkeys(allowed)),
        disallowed_tools=list(config.disallowed_tools),
        mcp_servers={
            name: claude_server(
                relay_recursive_agent_to_mcp(server, config.environment),
                name in loaded,
            )
            for name, server in servers.items()
        },
        strict_mcp_config=True,
        agents={
            spec.name: claude_types.AgentDefinition(
                description=spec.description,
                prompt=spec.prompt,
                tools=subagent_tools(spec),
                model=model_alias(spec.model),
                maxTurns=spec.max_turns,
            )
            for spec in config.subagents
        }
        or None,
        permission_mode=(
            "default" if config.permission_mode == "manual" else config.permission_mode
        ),
        max_turns=config.max_turns,
        max_thinking_tokens=config.max_thinking_tokens,
        effort=effort.level if effort is not None else None,
        # The one document a launched CLI reads as `--settings`: the declared
        # wall and an effort's switches, so neither output inherits them.
        settings=json.dumps(claude_settings(config)),
        cwd=config.cwd,
        add_dirs=[str(path) for path in config.add_dirs],
        plugins=[
            claude_types.SdkPluginConfig(type="local", path=str(path))
            for path in config.plugin_dirs
        ],
        env=config.environment,
        hooks=hooks,
        include_partial_messages=config.delta_streaming,
        resume=resume,
        session_id=session_id,
        fork_session=fork_session,
        resume_session_at=resume_session_at,
        output_format=None,
        max_buffer_size=config.max_buffer_size,
        setting_sources=[],
        cli_path=config.cli_path or installed_claude(config.environment),
        extra_args=dict(config.extra_args),
    )


def convert_claude_block(block: "claude.ContentBlock") -> AnyTurnBlock:
    """Convert one SDK block directly into the portable runtime vocabulary."""
    import claude_agent_sdk as claude
    from claude_agent_sdk import types as claude_types

    match block:
        case claude.TextBlock(text=text):
            from lup.sessions.events import TurnTextBlock

            return TurnTextBlock(text=text)
        case claude.ThinkingBlock(thinking=thinking, signature=signature):
            from lup.sessions.events import TurnThinkingBlock

            return TurnThinkingBlock(
                thinking=thinking or "", redacted=not thinking and bool(signature)
            )
        case claude.ToolUseBlock(id=identifier, name=name, input=input_data):
            from lup.sessions.events import TurnToolCallBlock

            return TurnToolCallBlock(
                id=identifier, name=name, arguments=input_data or {}
            )
        case claude.ToolResultBlock(
            tool_use_id=identifier, content=content, is_error=failed
        ):
            from lup.sessions.events import TurnToolResultBlock

            return TurnToolResultBlock(
                tool_call_id=identifier,
                content=result_text(TypeAdapter(JsonValue).validate_python(content)),
                is_error=bool(failed),
            )
        case claude_types.ServerToolUseBlock(
            id=identifier, name=name, input=input_data
        ):
            from lup.sessions.events import TurnToolCallBlock

            return TurnToolCallBlock(
                id=identifier, name=name, arguments=input_data or {}
            )
        case claude_types.ServerToolResultBlock(
            tool_use_id=identifier, content=content
        ):
            from lup.sessions.events import TurnToolResultBlock

            return TurnToolResultBlock(
                tool_call_id=identifier,
                content=content if isinstance(content, str) else str(content),
            )
        case _:
            from lup.sessions.events import TurnTextBlock

            return TurnTextBlock(text=str(block))


def claude_usage(
    raw: Mapping[str, JsonValue] | None,
    *,
    total_cost_usd: float | None = None,
) -> Usage:
    """Normalize only the portable count fields from Claude's open payload."""
    if raw is None:
        return Usage(cost_usd=total_cost_usd)

    def count(name: str) -> int:
        value = raw.get(name)
        return value if isinstance(value, int) else 0

    return Usage(
        cost_usd=total_cost_usd,
        input_tokens=count("input_tokens")
        + count("cache_read_input_tokens")
        + count("cache_creation_input_tokens"),
        output_tokens=count("output_tokens"),
        cache_read_input_tokens=count("cache_read_input_tokens"),
        cache_creation_input_tokens=count("cache_creation_input_tokens"),
    )
