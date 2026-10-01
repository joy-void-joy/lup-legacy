"""What the template lays over a provider's agent, observed through a canned one.

The submission gate is the reflection gate, typed; the main factory's
decoration persists each turn, displays it and traces it; and a Codex
composition refuses the options only Claude understands.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Self, overload

import pytest
from pydantic import BaseModel

from lup.observability.trace import TraceLogger
from lup.orchestration.reflection import ReviewGate
from lup.providers.claude import ClaudeSession
from lup.sessions.capabilities import (
    ConversationRecord,
    EventStream,
    ForkSession,
    Interrupt,
    SessionEngine,
    TurnEngine,
)
from lup.sessions.events import (
    SessionId,
    SessionSummary,
    StartedTurn,
    TurnBlock,
    TurnEvent,
    TurnId,
    TurnIdentifiers,
    TurnInput,
    TurnMessage,
    TurnRequest,
    TurnResult,
    TurnTextBlock,
)
from lup.sessions.layers import SessionLayers
from lup.types import Usage
from lup.workspace.notes import NotesConfig
from lup_template.agent.config import settings
from lup_template.agent.core import (
    decorate_factory,
    provider_factory,
    reflection_submission_gate,
)
from lup_template.agent.models import AgentOutput


@pytest.mark.asyncio
async def test_reflection_gate_is_the_typed_submission_gate() -> None:
    review = ReviewGate()
    gate = reflection_submission_gate(review)
    output = AgentOutput(summary="complete")

    assert not (await gate(output)).accepted
    review.mark_reflected()
    assert (await gate(output)).accepted


class StaticTurn[T: BaseModel | None](TurnEngine[T]):
    def __init__(self, result: TurnResult[T]) -> None:
        self.value = result

    async def result(self) -> TurnResult[T]:
        return self.value


class QuietStream(EventStream):
    """A canned turn reports no events: its stream simply ends."""

    async def ended(self) -> AsyncIterator[TurnEvent]:
        for event in ():
            yield event

    def events(self) -> AsyncIterator[TurnEvent]:
        return self.ended()

    def live(self) -> AsyncIterator[TurnEvent]:
        return self.ended()


class IgnoredInterrupt(Interrupt):
    """Accept an interrupt and do nothing: a canned turn is already done."""

    async def interrupt(self) -> None:
        return None


class StaticSession(SessionEngine):
    """Complete every turn with the same successful canned result."""

    def __init__(self, blocks: list[TurnBlock]) -> None:
        self.blocks = blocks

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> StartedTurn[T]:
        result = TurnResult[T].model_validate(
            {
                "output": None,
                "messages": [],
                "blocks": self.blocks,
                "usage": Usage(),
                "duration": timedelta(),
                "identifiers": TurnIdentifiers(
                    session=SessionId(value="decorated"),
                    turn=TurnId(value="turn-1"),
                ),
            }
        )
        return StartedTurn[T](
            turn=StaticTurn(result),
            events=QuietStream(),
            interrupt=IgnoredInterrupt(),
        )


class HeldRecord(ConversationRecord):
    """The record of a conversation nothing reads back."""

    def identity(self) -> SessionId:
        return SessionId(value="decorated")

    async def messages(self) -> list[TurnMessage]:
        return []


class Unforked(ForkSession[ClaudeSession]):
    def fork(
        self, at: TurnId | None = None
    ) -> AbstractAsyncContextManager[ClaudeSession]:
        raise AssertionError(f"these tests never fork (asked at {at})")


class StaticAgent:
    """An agent whose sessions complete every turn with the same canned blocks.

    Opened the way a real agent opens, through the layers laid over it, so
    what the template lays on is what these tests observe.
    """

    def __init__(
        self, blocks: list[TurnBlock], layers: SessionLayers = SessionLayers()
    ) -> None:
        self.blocks = blocks
        self.layers = layers

    def open(
        self, resume: SessionId | None = None
    ) -> AbstractAsyncContextManager[ClaudeSession]:
        return self.opening(resume)

    @asynccontextmanager
    async def opening(self, resume: SessionId | None) -> AsyncGenerator[ClaudeSession]:
        @asynccontextmanager
        async def native() -> AsyncGenerator[SessionEngine]:
            yield StaticSession(self.blocks)

        async with self.layers.around(native(), resume) as engine:
            yield ClaudeSession(engine, HeldRecord(), Unforked(), deltas=True)

    @overload
    async def ask(self, prompt: str | TurnInput) -> TurnResult[None]: ...

    @overload
    async def ask[T: BaseModel](
        self, prompt: str | TurnInput, output: type[T]
    ) -> TurnResult[T]: ...

    async def ask[T: BaseModel](
        self, prompt: str | TurnInput, output: type[T] | None = None
    ) -> TurnResult[T] | TurnResult[None]:
        async with self.open() as session:
            if output is None:
                return await session.ask(prompt)
            return await session.ask(prompt, output)

    async def sessions(self) -> list[SessionSummary]:
        return []

    def layered(self, layers: SessionLayers) -> Self:
        return type(self)(self.blocks, layers.over(self.layers))


@pytest.mark.asyncio
async def test_main_factory_decoration_wires_persistence_display_and_trace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = NotesConfig(
        session=tmp_path / "session",
        output=tmp_path / "output",
        trace_log=tmp_path / "logs" / "trace.md",
    )
    trace = TraceLogger(trace_path=notes.trace_log, title="test")
    inner = StaticAgent([TurnTextBlock(text="decorated turn")])

    decorated = decorate_factory(inner, notes=notes, trace_logger=trace)
    result = await decorated.ask("run one turn")

    assert result.blocks == [TurnTextBlock(text="decorated turn")]
    assert len(list((notes.trace_log.parent / "turns").glob("*.json"))) == 1
    assert notes.trace_log.exists()
    assert "decorated turn" in capsys.readouterr().out


def test_codex_rejects_explicit_claude_only_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "agent_sdk", "codex")
    monkeypatch.setattr(settings, "permission_mode", "plan")

    with pytest.raises(ValueError, match="AGENT_PERMISSION_MODE"):
        provider_factory(model="gpt", system_prompt="", cwd=tmp_path)
