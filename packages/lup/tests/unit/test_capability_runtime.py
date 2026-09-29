"""Behavior tests for the neutral session, turn, and submission composition."""

import asyncio

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from lup.providers.codex.app_server import CodexAppServer, RpcNotification
from lup.providers.codex import Codex
from lup.providers.codex.runtime import CodexConversationState, CodexTurnChannel
from lup.providers.codex.hooks import (
    CODEX_SEMANTICS,
    COMMAND_APPROVAL,
    FILE_CHANGE_APPROVAL,
    CodexApprovalResponder,
)
from lup.policy.hooks import create_permission_hooks
from lup.policy.enforcement import SemanticToolPolicy, create_policy_hooks
from lup.policy.rules import ShellPolicy
from lup.policy.kernel.effects import declare
from lup.policy.shell_rules import ShellCommandRule
from lup.sessions.composition import (
    AcceptedTurn,
    CompletedTurn,
    ComposedSession,
    ComposedTurn,
    TurnLifecycle,
    submission_gate_resolver,
)
from lup.sessions.capabilities import Interrupt, TurnToolBinder
from lup.sessions.layers import CleanupWrapper, SessionLayers
from lup.sessions.errors import (
    ProviderTurnError,
    StructuredOutputError,
    TurnAbortedError,
    TurnAlreadyActiveError,
    TurnInterruptedError,
)
from lup.sessions.events import (
    SessionId,
    TurnIdentifiers,
    TurnId,
    TurnTextBlock,
    TurnToolBinding,
)
from lup.sessions.output import FileSubmittedOutputStore, submit_output
from lup.sessions.output import InMemorySubmittedOutputStore
from lup.types import CustomModel
from tests.unit.doubles import (
    EngineAgent,
    IgnoredInterrupt,
    SilentStream,
    request_for,
)


class OutputA(BaseModel, frozen=True):
    value: int


class OutputB(BaseModel, frozen=True):
    label: str


class RecordingBinder(TurnToolBinder):
    def __init__(self) -> None:
        self.current: TurnToolBinding[BaseModel] | None = None
        self.stores: list[int] = []

    async def bind[T: BaseModel](self, binding: TurnToolBinding[T] | None) -> None:
        if binding is None:
            self.current = None
            return
        gate = binding.gate

        async def erased_gate(
            value: BaseModel,  # lup: ignore[bare-basemodel] — test binder erasure
        ):
            from lup.sessions.events import SubmissionDecision

            if gate is None:
                return SubmissionDecision(accepted=True)
            typed = binding.output_type.model_validate(value.model_dump(mode="json"))
            return await gate(typed)

        self.current = TurnToolBinding[BaseModel](
            output_type=binding.output_type,
            store=binding.store,
            gate=erased_gate if gate is not None else None,
        )
        self.stores.append(id(binding.store))


class RecordingInterrupt(Interrupt):
    def __init__(self) -> None:
        self.calls = 0

    async def interrupt(self) -> None:
        self.calls += 1


def accepted_turn(sequence: int, interrupt: Interrupt | None = None) -> AcceptedTurn:
    async def complete() -> CompletedTurn:
        return CompletedTurn(duration=timedelta(milliseconds=sequence))

    return AcceptedTurn(
        identifiers=TurnIdentifiers(
            session=SessionId(value="session"),
            turn=TurnId(value=f"turn-{sequence}"),
        ),
        complete=complete,
        events=SilentStream(),
        interrupt=interrupt or IgnoredInterrupt(),
    )


@pytest.mark.asyncio
async def test_a_one_shot_ask_runs_one_typed_turn_and_closes_the_session() -> None:
    binder = RecordingBinder()
    closed: list[bool] = []

    async def start(_text: str) -> AcceptedTurn:
        assert binder.current is not None
        binder.current.store.write(OutputA(value=7))
        return accepted_turn(1)

    agent = EngineAgent(
        lambda _resume: ComposedSession(start, binder),
        SessionLayers(wrappers=[CleanupWrapper(lambda: closed.append(True))]),
    )

    result = await agent.ask("a", OutputA)
    again = await agent.ask("a", OutputA)

    assert result.output.value == 7
    assert again.output.value == result.output.value
    assert closed == [True, True]


@pytest.mark.asyncio
async def test_schema_transitions_get_fresh_turn_local_stores() -> None:
    binder = RecordingBinder()
    sequence = 0

    async def start(_text: str) -> AcceptedTurn:
        nonlocal sequence
        sequence += 1
        return accepted_turn(sequence)

    async def gate(value: OutputA):
        from lup.sessions.events import SubmissionDecision

        return SubmissionDecision(accepted=value.value > 0)

    session = ComposedSession(
        start,
        binder,
        gate_resolver=submission_gate_resolver(OutputA, gate),
    )

    prose = await session.start(request_for("none"))
    assert binder.current is None
    assert (await prose.turn.result()).output is None

    first = await session.start(request_for("a1", OutputA))
    assert binder.current is not None
    binder.current.store.write(OutputA(value=1))
    assert (await first.turn.result()).output == OutputA(value=1)

    second = await session.start(request_for("a2", OutputA))
    assert binder.current is not None
    binder.current.store.write(OutputA(value=2))
    assert (await second.turn.result()).output == OutputA(value=2)

    third = await session.start(request_for("b", OutputB))
    assert binder.current is not None
    binder.current.store.write(OutputB(label="done"))
    assert (await third.turn.result()).output == OutputB(label="done")
    final_prose = await session.start(request_for("none again"))
    assert binder.current is None
    assert (await final_prose.turn.result()).output is None
    assert len(binder.stores) == len(dict.fromkeys(binder.stores)) == 3


@pytest.mark.asyncio
async def test_missing_submission_never_produces_success() -> None:
    binder = RecordingBinder()

    async def start(_text: str) -> AcceptedTurn:
        return accepted_turn(1)

    session = ComposedSession(start, binder)
    handle = await session.start(request_for("typed", OutputA))
    assert binder.current is not None
    rejected = await submit_output(binder.current, {"value": "wrong"})
    assert not rejected.accepted

    with pytest.raises(StructuredOutputError) as raised:
        await handle.turn.result()
    assert len(raised.value.failure.validation_history) == 1
    assert "requested schema" in raised.value.failure.validation_history[0].message


@pytest.mark.asyncio
async def test_post_completion_failure_preserves_partial_evidence() -> None:
    from lup.types import Usage

    class BrokenStore(InMemorySubmittedOutputStore):
        def read[T: BaseModel](self, output_type: type[T]) -> T | None:
            raise OSError(f"submission store for {output_type.__name__} disappeared")

    async def complete() -> CompletedTurn:
        return CompletedTurn(
            blocks=[TurnTextBlock(text="partial")],
            usage=Usage(input_tokens=3, output_tokens=2),
            duration=timedelta(milliseconds=8),
        )

    turn = ComposedTurn(
        AcceptedTurn(
            identifiers=TurnIdentifiers(
                session=SessionId(value="session"),
                turn=TurnId(value="turn-partial"),
            ),
            complete=complete,
            events=SilentStream(),
            interrupt=IgnoredInterrupt(),
        ),
        request_for("typed", OutputA),
        BrokenStore(),
        lambda: None,
        TurnLifecycle(),
        "submit_output",
    )

    with pytest.raises(ProviderTurnError) as raised:
        await turn.result()

    assert raised.value.failure.blocks == [TurnTextBlock(text="partial")]
    assert raised.value.failure.usage.input_tokens == 3
    assert raised.value.failure.duration == timedelta(milliseconds=8)


@pytest.mark.asyncio
async def test_session_reserves_one_turn_until_result() -> None:
    binder = RecordingBinder()

    async def start(_text: str) -> AcceptedTurn:
        return accepted_turn(1)

    session = ComposedSession(start, binder)
    first = await session.start(request_for("first"))

    with pytest.raises(TurnAlreadyActiveError):
        await session.start(request_for("second"))

    await first.turn.result()
    second = await session.start(request_for("second"))
    await second.turn.result()


@pytest.mark.asyncio
async def test_abort_interrupts_and_marks_unfinished_result() -> None:
    binder = RecordingBinder()
    interrupt = RecordingInterrupt()

    async def start(_text: str) -> AcceptedTurn:
        return accepted_turn(1, interrupt)

    session = ComposedSession(start, binder)
    handle = await session.start(request_for("work"))
    await session.abort_active()

    assert interrupt.calls == 1
    with pytest.raises(TurnAbortedError):
        await handle.turn.result()


@pytest.mark.asyncio
async def test_abort_is_idempotent() -> None:
    binder = RecordingBinder()
    interrupt = RecordingInterrupt()

    async def start(_text: str) -> AcceptedTurn:
        return accepted_turn(1, interrupt)

    session = ComposedSession(start, binder)
    handle = await session.start(request_for("work"))
    await session.abort_active()
    await session.abort_active()

    assert interrupt.calls == 1
    with pytest.raises(TurnAbortedError):
        await handle.turn.result()


@pytest.mark.asyncio
async def test_stale_aborted_turn_cannot_release_newer_turn() -> None:
    binder = RecordingBinder()
    sequence = 0

    async def start(_text: str) -> AcceptedTurn:
        nonlocal sequence
        sequence += 1
        return accepted_turn(sequence)

    session = ComposedSession(start, binder)
    stale = await session.start(request_for("stale"))
    await session.abort_active()
    current = await session.start(request_for("current"))

    with pytest.raises(TurnAbortedError):
        await stale.turn.result()
    with pytest.raises(TurnAlreadyActiveError):
        await session.start(request_for("must wait"))

    await current.turn.result()


@pytest.mark.asyncio
async def test_abort_during_provider_exception_is_reported_as_aborted() -> None:
    binder = RecordingBinder()
    completion_started = asyncio.Event()
    release = asyncio.Event()

    async def start(_text: str) -> AcceptedTurn:
        async def complete() -> CompletedTurn:
            completion_started.set()
            await release.wait()
            raise RuntimeError("transport closed")

        accepted = accepted_turn(1)
        return accepted.model_copy(update={"complete": complete})

    session = ComposedSession(start, binder)
    handle = await session.start(request_for("work"))
    result = asyncio.create_task(handle.turn.result())
    await completion_started.wait()
    await session.abort_active()
    release.set()

    with pytest.raises(TurnAbortedError):
        await result


@pytest.mark.asyncio
async def test_cancelled_acceptance_releases_session_reservation() -> None:
    binder = RecordingBinder()
    entered = asyncio.Event()

    async def start(_text: str) -> AcceptedTurn:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session = ComposedSession(start, binder)
    task = asyncio.create_task(session.start(request_for("cancel")))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not session.active


@pytest.mark.asyncio
async def test_malformed_app_server_output_fails_pending_requests() -> None:
    server = CodexAppServer(Path("codex"))
    reader = asyncio.create_task(server.read_messages())
    request = asyncio.create_task(server.request("thread/start", {}))
    await asyncio.sleep(0)
    server.output.put_nowait("not json")

    with pytest.raises(ValueError):
        await request
    with pytest.raises(ValueError):
        await reader
    assert server.pending == {}


@pytest.mark.asyncio
async def test_app_server_close_surfaces_reader_failure() -> None:
    server = CodexAppServer(Path("codex"))
    server.reader = asyncio.create_task(server.read_messages())
    server.output.put_nowait("not json")
    await asyncio.sleep(0)

    with pytest.raises(ValueError):
        await server.close()


@pytest.mark.asyncio
async def test_app_server_eof_fails_current_turn_with_partial_evidence(
    tmp_path: Path,
) -> None:
    server = CodexAppServer(Path("codex"))
    state = CodexConversationState(
        Codex(model=CustomModel(id="gpt"), cwd=tmp_path), server, None
    )
    channel = CodexTurnChannel("session")
    channel.turn_id = "turn"
    from lup.sessions.events import TurnTextBlock

    channel.blocks.append(TurnTextBlock(text="partial"))
    state.channel = channel
    reader = asyncio.create_task(server.read_messages())
    server.output.put_nowait(None)

    with pytest.raises(RuntimeError):
        await reader
    with pytest.raises(ProviderTurnError) as raised:
        await channel.completed
    assert raised.value.failure.blocks == [TurnTextBlock(text="partial")]


def test_codex_config_accepts_approvals_its_hooks_can_answer(tmp_path: Path) -> None:
    """Declared hooks are what makes an asking policy answerable."""
    config = Codex(
        model=CustomModel(id="gpt"),
        cwd=tmp_path,
        approval_policy="on-request",
        hooks=create_permission_hooks([tmp_path], []),
    )

    assert config.approval_policy == "on-request"


async def test_a_command_approval_is_judged_on_the_command_it_carries() -> None:
    """The exec approval carries command and cwd, so a shell rule reads them."""
    policy = SemanticToolPolicy(
        shell=ShellPolicy(
            [
                ShellCommandRule(
                    name="ls", effects=[declare("reads_path", scope="project")]
                ),
                ShellCommandRule(
                    name="curl", effects=[], refuses="egress", reason="egress"
                ),
            ]
        )
    )
    responder = CodexApprovalResponder(
        hooks=create_policy_hooks(policy, CODEX_SEMANTICS)
    )

    allowed = await responder.decide(COMMAND_APPROVAL, {"command": "ls -la"})
    refused = await responder.decide(COMMAND_APPROVAL, {"command": "curl http://x"})

    assert allowed == "accept"
    assert refused == "decline"


async def test_a_file_change_approval_carries_no_content_so_it_is_refused() -> None:
    """Codex sends an item id and a reason — never the patch itself.

    An edit rule reads before-and-after text, so approving here would be
    approving something nothing inspected.
    """
    responder = CodexApprovalResponder(
        hooks=create_policy_hooks(SemanticToolPolicy(), CODEX_SEMANTICS)
    )

    decision = await responder.decide(
        FILE_CHANGE_APPROVAL, {"itemId": "item-1", "reason": "edit a file"}
    )

    assert decision == "decline"


@pytest.mark.asyncio
async def test_stale_codex_notification_cannot_complete_current_turn() -> None:
    channel = CodexTurnChannel("session")
    channel.turn_id = "current"
    channel.feed(
        RpcNotification(
            method="turn/completed",
            params={"turn": {"id": "stale", "status": "completed"}},
        )
    )
    assert not channel.completed.done()

    channel.feed(
        RpcNotification(
            method="turn/completed",
            params={"turn": {"id": "current", "status": "completed"}},
        )
    )
    await channel.completed


@pytest.mark.asyncio
async def test_codex_interrupted_status_is_a_typed_interruption() -> None:
    channel = CodexTurnChannel("session")
    channel.feed(
        RpcNotification(
            method="turn/completed",
            params={"turn": {"id": "turn", "status": "interrupted"}},
        )
    )

    with pytest.raises(TurnInterruptedError):
        await channel.completed


@pytest.mark.asyncio
async def test_file_store_and_gate_share_validation(tmp_path: Path) -> None:
    store = FileSubmittedOutputStore(tmp_path / "turn" / "output.json")

    async def gate(value: OutputA):
        from lup.sessions.events import SubmissionDecision

        return SubmissionDecision(
            accepted=value.value > 0,
            message="value must be positive",
        )

    binding = TurnToolBinding[OutputA](
        output_type=OutputA,
        store=store,
        gate=gate,
    )
    rejected = await submit_output(binding, {"value": 0})
    assert not rejected.accepted
    assert store.read(OutputA) is None

    accepted = await submit_output(binding, {"value": 4})
    assert accepted.accepted
    assert store.read(OutputA) == OutputA(value=4)


@pytest.mark.asyncio
async def test_request_gate_is_bound_before_native_acceptance() -> None:
    binder = RecordingBinder()

    async def gate(value: OutputA):
        from lup.sessions.events import SubmissionDecision

        return SubmissionDecision(accepted=value.value > 0, message="reflect first")

    async def start(_text: str) -> AcceptedTurn:
        assert binder.current is not None
        rejected = await submit_output(binder.current, {"value": 0})
        assert not rejected.accepted
        accepted = await submit_output(binder.current, {"value": 2})
        assert accepted.accepted
        return accepted_turn(1)

    session = ComposedSession(
        start,
        binder,
        gate_resolver=submission_gate_resolver(OutputA, gate),
    )
    handle = await session.start(request_for("gated", OutputA))

    assert (await handle.turn.result()).output == OutputA(value=2)
