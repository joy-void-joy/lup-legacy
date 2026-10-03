# lup: ignore[own-model-dispatch]
# What the display sink received and what the live stream framed are the
# claims here: the block and event types are the assertions, and the unmatched
# case fails the test on anything else rather than dispatching past it.
"""Whole-turn timeout, recovery, correction, persistence, and queue tests."""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from lup.sessions.composition import (
    AcceptedTurn,
    CompletedTurn,
    CompleteTurn,
    ComposedSession,
)
from lup.sessions.errors import (
    BudgetExceededError,
    ProviderTurnError,
    StructuredOutputError,
    TurnFailure,
    TurnTimeoutError,
    TurnContinuationError,
)
from lup.sessions.capabilities import EventStream, Steer
from lup.sessions.events import (
    BlockCompletedEvent,
    LiveTurnEvent,
    SessionId,
    TurnEvent,
    TurnIdentifiers,
    TurnId,
    TurnInput,
    TurnRequest,
    TurnTextBlock,
    TurnToolCallBlock,
    TurnToolResultBlock,
)
from lup.sessions.middleware import (
    BudgetConfig,
    CorrectionConfig,
    DecoratingSession,
    DisplayConfig,
    DisplayRecord,
    PersistenceConfig,
    RecoveryConfig,
    SerializedSession,
    TimeoutConfig,
    TraceRecord,
    TracingConfig,
    UsageConfig,
    UsageRecord,
)
from lup.types import Usage
from tests.unit.test_capability_runtime import RecordingBinder, RecordingInterrupt
from tests.unit.doubles import (
    IgnoredInterrupt,
    SilentStream,
    request_for,
)


class WrappedOutput(BaseModel, frozen=True):
    value: int


def accepted(
    sequence: int,
    complete: CompleteTurn,
    interrupt: RecordingInterrupt | None = None,
) -> AcceptedTurn:
    return AcceptedTurn(
        identifiers=TurnIdentifiers(
            session=SessionId(value="wrapped"),
            turn=TurnId(value=f"turn-{sequence}"),
        ),
        complete=complete,
        events=SilentStream(),
        interrupt=interrupt or IgnoredInterrupt(),
    )


@pytest.mark.asyncio
async def test_timeout_covers_terminal_completion_and_interrupts() -> None:
    binder = RecordingBinder()
    interrupt = RecordingInterrupt()

    async def start(_text: str) -> AcceptedTurn:
        async def complete() -> CompletedTurn:
            await asyncio.sleep(1)
            return CompletedTurn()

        return accepted(1, complete, interrupt)

    session = DecoratingSession(
        ComposedSession(start, binder),
        timeout=TimeoutConfig(seconds=0.001),
        budget=None,
        recovery=None,
        correction=None,
        persistence=None,
    )
    handle = await session.start(request_for("slow"))

    with pytest.raises(TurnTimeoutError):
        await handle.turn.result()
    assert interrupt.calls == 1


@pytest.mark.asyncio
async def test_timeout_interrupts_the_current_recovery_attempt() -> None:
    binder = RecordingBinder()
    first_interrupt = RecordingInterrupt()
    second_interrupt = RecordingInterrupt()
    sequence = 0

    async def start(_text: str) -> AcceptedTurn:
        nonlocal sequence
        sequence += 1
        turn_sequence = sequence

        async def complete() -> CompletedTurn:
            if turn_sequence == 1:
                raise ProviderTurnError(TurnFailure(message="retry"))
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        interrupt = first_interrupt if turn_sequence == 1 else second_interrupt
        return accepted(turn_sequence, complete, interrupt)

    session = DecoratingSession(
        ComposedSession(start, binder),
        timeout=TimeoutConfig(seconds=0.01),
        budget=None,
        recovery=RecoveryConfig(retries=1),
        correction=None,
        persistence=None,
    )
    handle = await session.start(request_for("retry then wait"))

    with pytest.raises(TurnTimeoutError):
        await handle.turn.result()
    assert first_interrupt.calls == 0
    assert second_interrupt.calls == 1


@pytest.mark.asyncio
async def test_recovery_accumulates_failed_attempt_usage() -> None:
    binder = RecordingBinder()
    sequence = 0

    async def start(_text: str) -> AcceptedTurn:
        nonlocal sequence
        sequence += 1

        async def complete() -> CompletedTurn:
            if sequence == 1:
                raise RuntimeError("transient")
            return CompletedTurn(
                usage=Usage(input_tokens=3),
                duration=timedelta(milliseconds=2),
            )

        return accepted(sequence, complete)

    session = DecoratingSession(
        ComposedSession(start, binder),
        timeout=None,
        budget=None,
        recovery=RecoveryConfig(retries=1),
        correction=None,
        persistence=None,
    )
    handle = await session.start(request_for("retry"))
    result = await handle.turn.result()

    assert sequence == 2
    assert result.usage.input_tokens == 3


@pytest.mark.asyncio
async def test_correction_rebinds_a_fresh_store_and_aggregates_usage() -> None:
    binder = RecordingBinder()
    sequence = 0

    async def start(_text: str) -> AcceptedTurn:
        nonlocal sequence
        sequence += 1
        turn_sequence = sequence

        async def complete() -> CompletedTurn:
            if turn_sequence == 2:
                assert binder.current is not None
                binder.current.store.write(WrappedOutput(value=7))
            return CompletedTurn(
                usage=Usage(input_tokens=turn_sequence),
                duration=timedelta(milliseconds=turn_sequence),
            )

        return accepted(turn_sequence, complete)

    session = DecoratingSession(
        ComposedSession(start, binder),
        timeout=None,
        budget=None,
        recovery=None,
        correction=CorrectionConfig(cycles=1),
        persistence=None,
    )
    handle = await session.start(request_for("typed", WrappedOutput))
    result = await handle.turn.result()

    assert result.output == WrappedOutput(value=7)
    assert result.usage.input_tokens == 3
    assert len(binder.stores) == len(dict.fromkeys(binder.stores)) == 2


@pytest.mark.asyncio
async def test_a_refused_submission_fails_instead_of_spending_corrections() -> None:
    """Re-prompting a refused submission reproduces the refusal.

    A worker denied its submission tool reports the same empty store every
    cycle, so every configured correction would be spent reaching the failure
    the first turn already had — and reported as a turn that never submitted,
    which reads as a model mistake rather than a permission one.
    """
    binder = RecordingBinder()
    sequence = 0

    async def start(_text: str) -> AcceptedTurn:
        nonlocal sequence
        sequence += 1

        async def complete() -> CompletedTurn:
            return CompletedTurn(
                blocks=[
                    TurnToolCallBlock(id="call-1", name="submit_output"),
                    TurnToolResultBlock(
                        tool_call_id="call-1",
                        content="unclassified tool 'submit_output'",
                        is_error=True,
                    ),
                ]
            )

        return accepted(sequence, complete)

    session = DecoratingSession(
        ComposedSession(start, binder),
        timeout=None,
        budget=None,
        recovery=None,
        correction=CorrectionConfig(cycles=3),
        persistence=None,
    )
    handle = await session.start(request_for("typed", WrappedOutput))

    with pytest.raises(StructuredOutputError) as raised:
        await handle.turn.result()

    assert sequence == 1
    assert "unclassified tool 'submit_output'" in raised.value.failure.message
    assert raised.value.failure.correctable is False


@pytest.mark.asyncio
async def test_recovery_and_correction_share_one_logical_retry_loop() -> None:
    binder = RecordingBinder()
    sequence = 0

    async def start(_text: str) -> AcceptedTurn:
        nonlocal sequence
        sequence += 1
        turn_sequence = sequence

        async def complete() -> CompletedTurn:
            evidence = TurnFailure(
                message=f"provider failure {turn_sequence}",
                usage=Usage(input_tokens=turn_sequence),
                duration=timedelta(milliseconds=turn_sequence),
            )
            if turn_sequence in {1, 3}:
                raise ProviderTurnError(evidence)
            if turn_sequence == 4:
                assert binder.current is not None
                binder.current.store.write(WrappedOutput(value=9))
            return CompletedTurn(
                usage=Usage(input_tokens=turn_sequence),
                duration=timedelta(milliseconds=turn_sequence),
            )

        return accepted(turn_sequence, complete)

    session = DecoratingSession(
        ComposedSession(start, binder),
        timeout=None,
        budget=None,
        recovery=RecoveryConfig(retries=2),
        correction=CorrectionConfig(cycles=1),
        persistence=None,
    )
    handle = await session.start(request_for("typed", WrappedOutput))
    result = await handle.turn.result()

    assert sequence == 4
    assert result.output == WrappedOutput(value=9)
    assert result.usage.input_tokens == 10
    assert len(binder.stores) == len(dict.fromkeys(binder.stores)) == 4


@pytest.mark.asyncio
async def test_serialized_session_queues_until_prior_result_finishes() -> None:
    binder = RecordingBinder()
    sequence = 0

    async def start(_text: str) -> AcceptedTurn:
        nonlocal sequence
        sequence += 1

        async def complete() -> CompletedTurn:
            return CompletedTurn()

        return accepted(sequence, complete)

    session = SerializedSession(ComposedSession(start, binder))
    first = await session.start(request_for("first"))
    second_task = asyncio.create_task(session.start(request_for("second")))
    await asyncio.sleep(0)
    assert not second_task.done()

    await first.turn.result()
    second = await second_task
    await second.turn.result()
    assert sequence == 2


@pytest.mark.asyncio
async def test_consumed_serialized_turn_cannot_release_a_new_owner() -> None:
    binder = RecordingBinder()
    sequence = 0

    async def start(_text: str) -> AcceptedTurn:
        nonlocal sequence
        sequence += 1

        async def complete() -> CompletedTurn:
            return CompletedTurn()

        return accepted(sequence, complete)

    session = SerializedSession(ComposedSession(start, binder))
    first = await session.start(request_for("first"))
    await first.turn.result()
    second = await session.start(request_for("second"))

    with pytest.raises(RuntimeError):
        await first.turn.result()
    assert session.lock.locked()

    await second.turn.result()


@pytest.mark.asyncio
async def test_budget_exhaustion_preserves_completed_evidence() -> None:
    binder = RecordingBinder()

    async def start(_text: str) -> AcceptedTurn:
        async def complete() -> CompletedTurn:
            return CompletedTurn(
                usage=Usage(input_tokens=20, output_tokens=10),
                duration=timedelta(milliseconds=7),
            )

        return accepted(1, complete)

    session = DecoratingSession(
        ComposedSession(start, binder),
        timeout=None,
        budget=BudgetConfig(
            maximum_usd=1.0,
            usage_cost=lambda usage: usage.input_tokens / 10,
        ),
        recovery=None,
        correction=None,
        persistence=None,
    )
    handle = await session.start(request_for("expensive"))

    with pytest.raises(BudgetExceededError) as raised:
        await handle.turn.result()
    assert raised.value.failure.usage.input_tokens == 20
    assert raised.value.failure.duration == timedelta(milliseconds=7)


@pytest.mark.asyncio
async def test_cancelled_serialized_acceptance_releases_queue_lock() -> None:
    binder = RecordingBinder()
    entered = asyncio.Event()

    async def start(_text: str) -> AcceptedTurn:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    session = SerializedSession(ComposedSession(start, binder))
    task = asyncio.create_task(session.start(request_for("cancel")))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not session.lock.locked()


@pytest.mark.asyncio
async def test_successful_result_is_persisted_atomically(tmp_path: Path) -> None:
    binder = RecordingBinder()

    async def start(_text: str) -> AcceptedTurn:
        async def complete() -> CompletedTurn:
            return CompletedTurn()

        return accepted(1, complete)

    session = DecoratingSession(
        ComposedSession(start, binder),
        timeout=None,
        budget=None,
        recovery=None,
        correction=None,
        persistence=PersistenceConfig(directory=tmp_path),
    )
    handle = await session.start(request_for("persist"))
    await handle.turn.result()

    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".json"
    assert not files[0].name.startswith(".")


@pytest.mark.asyncio
async def test_trace_usage_and_display_observe_one_complete_logical_turn() -> None:
    binder = RecordingBinder()
    traces: list[TraceRecord] = []
    usages: list[UsageRecord] = []
    displays: list[DisplayRecord] = []

    async def start(_text: str) -> AcceptedTurn:
        async def complete() -> CompletedTurn:
            from lup.sessions.events import TurnMessage, TurnTextBlock

            block = TurnTextBlock(text="done")
            return CompletedTurn(
                messages=[TurnMessage(role="assistant", blocks=[block])],
                blocks=[block],
                usage=Usage(input_tokens=4),
                duration=timedelta(milliseconds=3),
            )

        return accepted(1, complete)

    async def trace(record: TraceRecord) -> None:
        traces.append(record)

    async def usage(record: UsageRecord) -> None:
        usages.append(record)

    async def display(record: DisplayRecord) -> None:
        displays.append(record)

    session = DecoratingSession(
        ComposedSession(start, binder),
        timeout=None,
        budget=None,
        recovery=None,
        correction=None,
        persistence=None,
        tracing=TracingConfig(sink=trace),
        usage=UsageConfig(sink=usage),
        display=DisplayConfig(sink=display),
    )
    handle = await session.start(request_for("observe"))
    result = await handle.turn.result()

    assert result.usage.input_tokens == 4
    assert [record.succeeded for record in traces] == [True]
    assert [record.usage.input_tokens for record in usages] == [4]
    displayed = displays[0].blocks[0]
    assert isinstance(displayed, TurnTextBlock)
    assert displayed.text == "done"


@pytest.mark.asyncio
async def test_persistence_failure_surfaces_as_typed_turn_error(
    tmp_path: Path,
) -> None:
    binder = RecordingBinder()

    async def start(_text: str) -> AcceptedTurn:
        async def complete() -> CompletedTurn:
            return CompletedTurn()

        return accepted(1, complete)

    blocker = tmp_path / "blocker"
    blocker.write_text("occupied\n", encoding="utf-8")
    session = DecoratingSession(
        ComposedSession(start, binder),
        timeout=None,
        budget=None,
        recovery=None,
        correction=None,
        persistence=PersistenceConfig(directory=blocker / "turns"),
    )
    handle = await session.start(request_for("persist"))

    with pytest.raises(ProviderTurnError):
        await handle.turn.result()


@pytest.mark.asyncio
async def test_retry_start_failure_is_wrapped_as_provider_error() -> None:
    binder = RecordingBinder()
    sequence = 0

    async def start(_text: str) -> AcceptedTurn:
        nonlocal sequence
        sequence += 1
        if sequence == 2:
            raise RuntimeError("session lost")

        async def complete() -> CompletedTurn:
            raise ProviderTurnError(TurnFailure(message="retry"))

        return accepted(sequence, complete)

    session = DecoratingSession(
        ComposedSession(start, binder),
        timeout=None,
        budget=None,
        recovery=RecoveryConfig(retries=1),
        correction=None,
        persistence=None,
    )
    handle = await session.start(request_for("fragile"))

    with pytest.raises(ProviderTurnError) as caught:
        await handle.turn.result()
    assert "session lost" in str(caught.value)


class ScriptedEventStream(EventStream):
    """Yield one labeled block per attempt and signal when fully drained."""

    def __init__(
        self,
        label: str,
        identifiers: TurnIdentifiers,
        drained: asyncio.Event,
    ) -> None:
        self.label = label
        self.identifiers = identifiers
        self.drained = drained

    async def iterate(self) -> AsyncIterator[TurnEvent]:
        yield BlockCompletedEvent(
            identifiers=self.identifiers,
            block=TurnTextBlock(text=self.label),
        )
        self.drained.set()

    def events(self) -> AsyncIterator[TurnEvent]:
        return self.iterate()

    def live(self) -> AsyncIterator[LiveTurnEvent]:
        return self.iterate()


class RecordingSteer(Steer):
    def __init__(self, name: str, record: list[str]) -> None:
        self.name = name
        self.record = record

    async def steer(self, input: TurnInput) -> None:
        self.record.append(f"{self.name}:{input.text}")


@pytest.mark.asyncio
@pytest.mark.parametrize("continuation", [False, True])
async def test_retry_joins_live_events_and_retargets_steer(continuation: bool) -> None:
    binder = RecordingBinder()
    steered: list[str] = []
    first_drained = asyncio.Event()
    second_drained = asyncio.Event()
    sequence = 0

    async def start(_text: str) -> AcceptedTurn:
        nonlocal sequence
        sequence += 1
        turn_sequence = sequence
        identifiers = TurnIdentifiers(
            session=SessionId(value="wrapped"),
            turn=TurnId(value=f"turn-{turn_sequence}"),
        )

        async def complete() -> CompletedTurn:
            if turn_sequence == 1:
                await first_drained.wait()
                if continuation:
                    raise TurnContinuationError(TurnFailure(message="finish the tests"))
                raise ProviderTurnError(TurnFailure(message="retry"))
            return CompletedTurn()

        drained = first_drained if turn_sequence == 1 else second_drained
        return AcceptedTurn(
            identifiers=identifiers,
            complete=complete,
            events=ScriptedEventStream(
                f"attempt-{turn_sequence}", identifiers, drained
            ),
            interrupt=IgnoredInterrupt(),
            steer=RecordingSteer(f"steer-{turn_sequence}", steered),
        )

    session = DecoratingSession(
        ComposedSession(start, binder),
        timeout=None,
        budget=None,
        recovery=RecoveryConfig(retries=1),
        correction=None,
        persistence=None,
        continuation=CorrectionConfig(cycles=1) if continuation else None,
    )
    handle = await session.start(request_for("hello"))
    events = handle.events
    steer = handle.steer
    assert events is not None
    assert steer is not None

    def block_text(event: TurnEvent) -> str:
        match event:
            case BlockCompletedEvent(block=TurnTextBlock(text=text)):
                return text
        raise AssertionError(f"unexpected live event {event.type}")

    async def consume() -> list[str]:
        return [block_text(event) async for event in events.events()]

    consumer = asyncio.create_task(consume())
    result = await handle.turn.result()
    observed = await consumer
    await steer.steer(TurnInput(text="focus"))

    assert observed == ["attempt-1", "attempt-2"]
    assert steered == ["steer-2:focus"]
    assert result.output is None
    assert second_drained.is_set()


@pytest.mark.parametrize("typed", [False, True])
@pytest.mark.parametrize("always_block", [False, True])
async def test_completion_hook_continuation_is_bounded_and_preserves_feedback(
    typed: bool, always_block: bool
) -> None:
    binder = RecordingBinder()
    prompts: list[str] = []
    submissions = 0

    async def start(text: str) -> AcceptedTurn:
        prompts.append(text)
        sequence = len(prompts)

        async def complete() -> CompletedTurn:
            nonlocal submissions
            if sequence == 1 or always_block:
                raise TurnContinuationError(
                    TurnFailure(
                        message="Run the missing verification",
                        usage=Usage(input_tokens=3),
                    )
                )
            if binder.current is not None:
                binder.current.store.write(WrappedOutput(value=7))
                submissions += 1
            return CompletedTurn(usage=Usage(input_tokens=3))

        return accepted(sequence, complete)

    session = DecoratingSession(
        ComposedSession(start, binder),
        timeout=None,
        budget=None,
        recovery=None,
        correction=None,
        persistence=None,
        continuation=CorrectionConfig(cycles=1, instruction="Finish the hook request."),
    )
    request = TurnRequest[WrappedOutput | None](
        input=TurnInput(text="work"), output_type=WrappedOutput if typed else None
    )
    handle = await session.start(request)
    if always_block:
        with pytest.raises(TurnContinuationError) as raised:
            await handle.turn.result()
        assert raised.value.failure.usage.input_tokens == 6
        assert submissions == 0
    else:
        result = await handle.turn.result()
        assert result.usage.input_tokens == 6
        assert result.output == (WrappedOutput(value=7) if typed else None)
        assert submissions == int(typed)
    assert len(prompts) == 2
    assert "Run the missing verification" in prompts[1]
    await session.close()
