"""When a resilient turn's events may be consumed, relative to its result.

The answer has to be "whenever", and the failure it guards against is a hang
rather than a wrong value. A resilient turn's event stream spans its cycles:
each correction switches the stream to a fresh native stream, and the last one
closes it. Those cycles run in the turn itself, not inside whichever caller
awaits the result: run there, a caller who drains the events first — the shape
anyone writes who wants to print a turn as it happens — would wait on a close
that only the unasked result causes, and wait forever.

That deadlock would be reachable from the public surface with nothing in the
type or the docstring warning of it: session layers with a `recovery` or a
`correction` are enough. Each case here runs under a timeout, because a suite
that hangs reports nothing where one that fails names the regression.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta

import pytest
from pydantic import BaseModel

from lup.sessions.capabilities import (
    EventStream,
    Interrupt,
    SessionEngine,
    TurnEngine,
)
from lup.sessions.errors import StructuredOutputError, TurnFailure
from lup.sessions.events import (
    MessageCompletedEvent,
    SessionId,
    StartedTurn,
    TurnId,
    TurnIdentifiers,
    TurnMessage,
    TurnRequest,
    TurnResult,
    TurnInput,
    TurnTextBlock,
)
from lup.sessions.layers import SessionLayers
from lup.sessions.middleware import CorrectionConfig
from lup.types import Usage

IDENTIFIERS = TurnIdentifiers(
    session=SessionId(value="session"), turn=TurnId(value="turn")
)


class Answer(BaseModel):
    """The typed output a scripted turn either submits or does not."""

    value: int


def said(text: str) -> TurnMessage:
    """One assistant message carrying a single block of text."""
    return TurnMessage(role="assistant", blocks=[TurnTextBlock(text=text)])


class ScriptedStream(EventStream):
    """One native turn's messages, delivered once and then ended."""

    def __init__(self, messages: list[TurnMessage]) -> None:
        self.messages = messages

    async def iterate(self) -> AsyncIterator[MessageCompletedEvent]:
        for message in self.messages:
            yield MessageCompletedEvent(identifiers=IDENTIFIERS, message=message)

    def events(self) -> AsyncIterator[MessageCompletedEvent]:
        return self.iterate()

    def live(self) -> AsyncIterator[MessageCompletedEvent]:
        return self.iterate()


class IgnoredInterrupt(Interrupt):
    """Accept an interrupt and do nothing: these turns end on their own."""

    async def interrupt(self) -> None:
        return None


class ScriptedTurn(TurnEngine[Answer | None]):
    """A native turn that submits its scripted output, or admits it did not."""

    def __init__(self, messages: list[TurnMessage], output: Answer | None) -> None:
        self.messages = messages
        self.output = output

    async def result(self) -> TurnResult[Answer | None]:
        if self.output is None:
            raise StructuredOutputError(
                TurnFailure(
                    message="turn completed without a valid submit_output call",
                    identifiers=IDENTIFIERS,
                    correctable=True,
                )
            )
        return TurnResult[Answer | None](
            output=self.output,
            messages=self.messages,
            blocks=[block for message in self.messages for block in message.blocks],
            usage=Usage(),
            duration=timedelta(0),
            identifiers=IDENTIFIERS,
        )


class ScriptedSession(SessionEngine):
    """A session whose successive turns are scripted in advance."""

    def __init__(self, script: list[tuple[list[TurnMessage], Answer | None]]) -> None:
        self.script = script
        self.attempts: list[TurnRequest[Answer | None]] = []

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> StartedTurn[T]:
        self.attempts.append(request)  # pyright: ignore[reportArgumentType]
        step = min(len(self.attempts) - 1, len(self.script) - 1)
        messages, output = self.script[step]
        handle = StartedTurn[Answer | None](
            turn=ScriptedTurn(messages, output),
            events=ScriptedStream(messages),
            interrupt=IgnoredInterrupt(),
        )
        return handle  # pyright: ignore[reportReturnType]


def corrective_factory(
    script: list[tuple[list[TurnMessage], Answer | None]],
) -> tuple[AbstractAsyncContextManager[SessionEngine], ScriptedSession]:
    """A session that corrects, laid over one scripted session."""
    inner = ScriptedSession(script)

    @asynccontextmanager
    async def native() -> AsyncIterator[SessionEngine]:
        yield inner

    layers = SessionLayers(correction=CorrectionConfig(cycles=2))
    return layers.around(native(), None), inner


def asked_for_answer() -> TurnRequest[Answer]:
    """The one request every case starts: a prompt wanting an ``Answer``."""
    return TurnRequest[Answer](input=TurnInput(text="go"), output_type=Answer)


async def spoken(events: EventStream | None) -> list[str]:
    """Everything a turn said, drained to the close of its logical stream."""
    if events is None:
        return []
    said_so_far: list[str] = []
    async for event in events.events():
        if isinstance(event, MessageCompletedEvent):
            said_so_far.extend(
                block.text
                for block in event.message.blocks
                if isinstance(block, TurnTextBlock)
            )
    return said_so_far


@pytest.mark.asyncio
async def test_draining_the_events_first_still_reaches_the_result() -> None:
    """The deadlock itself: a caller who watches before it asks.

    Reaching the assertions at all is the property. Were the turn not to
    drive itself, the drain below would never return, because the close it
    waits for would happen only inside the result nobody has asked for yet.
    """
    factory, _ = corrective_factory(
        [([said("preamble")], None), ([said("done")], Answer(value=3))]
    )

    async with factory as engine:
        turn = await engine.start(asked_for_answer())
        heard = await asyncio.wait_for(spoken(turn.events), timeout=5)
        result = await asyncio.wait_for(turn.turn.result(), timeout=5)

    assert heard == ["preamble", "done"]
    assert result.output == Answer(value=3)


@pytest.mark.asyncio
async def test_asking_for_the_result_first_still_answers() -> None:
    """The order lup's own callers use, which must keep working unchanged."""
    factory, inner = corrective_factory(
        [([said("preamble")], None), ([said("done")], Answer(value=5))]
    )

    async with factory as engine:
        turn = await engine.start(asked_for_answer())
        result = await asyncio.wait_for(turn.turn.result(), timeout=5)

    assert result.output == Answer(value=5)
    assert len(inner.attempts) == 2


@pytest.mark.asyncio
async def test_watching_and_asking_at_once_agree_on_one_turn() -> None:
    """Two consumers of one turn, neither of them driving it alone."""
    factory, inner = corrective_factory(
        [([said("preamble")], None), ([said("done")], Answer(value=7))]
    )

    async with factory as engine:
        turn = await engine.start(asked_for_answer())
        heard, result = await asyncio.wait_for(
            asyncio.gather(spoken(turn.events), turn.turn.result()), timeout=5
        )

    assert heard == ["preamble", "done"]
    assert result.output == Answer(value=7)
    assert len(inner.attempts) == 2


@pytest.mark.asyncio
async def test_a_turn_nobody_asks_about_still_ends() -> None:
    """A turn advances on its own, and its stream closes because it did.

    A caller may watch a turn and never ask for its value — printing it is
    reason enough. The events still have to end, and a failure nobody
    retrieves must not surface later as a warning naming the collector.
    """
    factory, inner = corrective_factory([([said("still thinking")], None)])

    async with factory as engine:
        turn = await engine.start(asked_for_answer())
        heard = await asyncio.wait_for(spoken(turn.events), timeout=5)

    attempts = CorrectionConfig().cycles + 1
    assert heard == ["still thinking"] * attempts
    assert len(inner.attempts) == attempts


@pytest.mark.asyncio
async def test_the_result_is_the_same_one_however_often_it_is_asked() -> None:
    """One logical turn settles once, so a second ask is not a second turn."""
    factory, inner = corrective_factory([([said("done")], Answer(value=9))])

    async with factory as engine:
        turn = await engine.start(asked_for_answer())
        first = await asyncio.wait_for(turn.turn.result(), timeout=5)
        second = await asyncio.wait_for(turn.turn.result(), timeout=5)

    assert first == second
    assert len(inner.attempts) == 1
