"""What a program writes against the provider classes, and what it gets back.

Two halves. The first is a function pyright checks and nothing runs: the
surface as a reader writes it, with every inference pinned by ``assert_type``
and every provider class held against the neutral protocols. It opens real
providers, which is why it is not run — the second half runs the same
classes over scripted engines, so the behaviour those types promise is
asserted too.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import assert_type

import pytest
from pydantic import BaseModel

from lup import Claude, Codex
from lup.providers.claude import ClaudeSession, ClaudeTurn
from lup.providers.codex import CodexSession, CodexTurn
from lup.sessions.capabilities import ConversationRecord, ForkSession, TurnToolBinder
from lup.sessions.composition import AcceptedTurn, CompletedTurn, ComposedSession
from lup.sessions.events import (
    AnyTurnBlock,
    LiveTurnEvent,
    SessionId,
    SessionSummary,
    TurnEvent,
    TurnId,
    TurnMessage,
    TurnResult,
    TurnTextBlock,
    TurnToolBinding,
)
from lup.sessions.layers import SessionLayers
from lup.sessions.middleware import CorrectionConfig
from lup.sessions.surface import Agent, Conversation, Turn
from tests.unit.test_lazy_turn import IDENTIFIERS, TEXT, Provider


class Plan(BaseModel):
    steps: list[str] = ["one"]


async def the_surface_as_written(repo: Path) -> None:
    """The branch's target surface, each inference pinned. Checked, never run."""
    agent = Claude(model="opus", system_prompt="...", cwd=repo)
    assert_type(await agent.ask("Summarize", Plan), TurnResult[Plan])
    assert_type(await agent.ask("hi"), TurnResult[None])

    async with agent.open() as session:
        assert_type(session, ClaudeSession)
        assert_type(await session.ask("Draft a plan"), TurnResult[None])
        turn = session.ask("Now as JSON", Plan)
        assert_type(turn, ClaudeTurn[Plan])
        async for block in turn:
            assert_type(block, AnyTurnBlock)
        assert_type(await turn, TurnResult[Plan])
        await session.ask("Long job").interrupt()
        live: AsyncIterator[LiveTurnEvent] = session.ask("x").live()
        durable: AsyncIterator[TurnEvent] = session.ask("y").events()
        assert live and durable
        assert_type(await session.history(), list[TurnMessage])
        assert_type(session.id, SessionId)
        reply = await session.ask("again", Plan)
        async with session.fork(at=reply.identifiers.turn) as branch:
            assert_type(branch, ClaudeSession)

    async with Codex(model="gpt-5.6-sol").open() as threaded:
        assert_type(threaded, CodexSession)
        steered = threaded.ask("go")
        assert_type(steered, CodexTurn[None])
        await steered.steer("also do z")
    past = await agent.sessions()
    assert_type(past, list[SessionSummary])
    async with agent.open(resume=past[0].id) as resumed:
        assert_type(resumed, ClaudeSession)

    # Each provider class is what provider-neutral code asks for.
    neutral: list[Agent] = [agent, Codex()]
    held: Conversation = session
    asked: Turn[Plan] = session.ask("typed", Plan)
    assert neutral and held and asked


class Recorded(ConversationRecord):
    """A provider record that already holds one conversation."""

    def __init__(self, identity: str, messages: list[TurnMessage]) -> None:
        self.held = SessionId(value=identity)
        self.held_messages = messages

    def identity(self) -> SessionId:
        return self.held

    async def messages(self) -> list[TurnMessage]:
        return self.held_messages


class Branching[S](ForkSession[S]):
    """Record where each fork was asked for, opening the session ``opened`` names."""

    def __init__(self, opened: Callable[[], S]) -> None:
        self.opened = opened
        self.asked: list[TurnId | None] = []

    def fork(self, at: TurnId | None = None) -> AbstractAsyncContextManager[S]:
        self.asked.append(at)
        return self.branch()

    @asynccontextmanager
    async def branch(self) -> AsyncGenerator[S]:
        yield self.opened()


class Submitting(TurnToolBinder):
    """Keep the store a typed turn binds, so the scripted provider can fill it."""

    def __init__(self) -> None:
        self.bound: TurnToolBinding[BaseModel] | None = None

    async def bind[T: BaseModel](self, binding: TurnToolBinding[T] | None) -> None:
        self.bound = (
            None
            if binding is None
            else TurnToolBinding[BaseModel](
                output_type=binding.output_type, store=binding.store
            )
        )


def planning_engine(provider: Provider) -> ComposedSession:
    """A composed engine whose turns submit a plan whenever one is asked for."""
    binder = Submitting()

    async def start(text: str) -> AcceptedTurn:
        accepted = await provider.start(text)
        bound = binder.bound

        async def complete() -> CompletedTurn:
            finished = await accepted.complete()
            if bound is not None:
                bound.store.write(Plan())
            return finished

        return accepted.model_copy(update={"complete": complete})

    return ComposedSession(start, binder)


def claude_session(provider: Provider) -> ClaudeSession:
    """A Claude session over a scripted engine, whose forks open itself again."""
    record = Recorded("claude-session", [TurnMessage(role="user", blocks=[TEXT])])

    def itself() -> ClaudeSession:
        return session

    session = ClaudeSession(
        planning_engine(provider), record, Branching(itself), deltas=True
    )
    return session


async def test_a_claude_turn_starts_when_first_awaited_and_types_its_output() -> None:
    provider = Provider()
    session = claude_session(provider)

    turn = session.ask("Now as JSON", Plan)
    await asyncio.sleep(0)
    assert provider.started == []

    reply = await turn

    assert provider.started == ["Now as JSON"]
    assert reply.output == Plan()
    assert reply.identifiers == IDENTIFIERS


async def test_iterating_a_turn_yields_its_blocks_and_awaiting_after_agrees() -> None:
    provider = Provider()
    turn = claude_session(provider).ask("hi")

    blocks = [block async for block in turn]
    result = await turn

    assert blocks == [TEXT] == result.blocks
    assert provider.started == ["hi"]


async def test_a_turn_can_be_stopped_before_anything_else_asked() -> None:
    provider = Provider(waits=True)
    session = claude_session(provider)

    await session.ask("Long job").interrupt()

    assert provider.interrupted == 1
    assert (await session.ask("next")).blocks == [TEXT]


async def test_the_session_answers_its_record_and_hands_forks_their_point() -> None:
    session = claude_session(Provider())

    reply = await session.ask("draft")
    async with session.fork(at=reply.identifiers.turn) as branch:
        assert branch is session

    assert session.id == SessionId(value="claude-session")
    assert await session.history() == [TurnMessage(role="user", blocks=[TEXT])]
    assert isinstance(session.forks, Branching)
    assert session.forks.asked == [reply.identifiers.turn]


def test_only_a_codex_turn_steers() -> None:
    """The capability is on the type or not at all, never there and None."""
    assert hasattr(CodexTurn, "steer")
    assert not hasattr(ClaudeTurn, "steer")


async def test_a_codex_turn_steers_the_turn_it_is() -> None:
    provider = Provider(steers=True, waits=True)

    def itself() -> CodexSession:
        return session

    session = CodexSession(
        planning_engine(provider), Recorded("codex-thread", []), Branching(itself)
    )

    turn = session.ask("go")
    await turn.steer("also do z")
    provider.release.set()
    result = await turn

    assert provider.steered == ["also do z"]
    assert result.blocks == [TurnTextBlock(text="the answer")]


def test_layering_an_agent_keeps_its_own_and_adds_the_callers() -> None:
    """How the resolver gives any worker its correction cycles, naming no provider."""
    agent = Claude(layers=SessionLayers(serialized=True))

    layered = agent.layered(SessionLayers(correction=CorrectionConfig(cycles=2)))

    assert layered.layers.serialized is True
    assert layered.layers.correction == CorrectionConfig(cycles=2)
    assert agent.layers.correction is None


def test_codex_corrects_a_turn_once_beneath_its_hooks() -> None:
    """A correction laid on Codex moves beneath its hooks and leaves the outer layers."""
    agent = Codex().layered(
        SessionLayers(correction=CorrectionConfig(cycles=1), serialized=True)
    )

    assert agent.turn_corrections().correction == CorrectionConfig(cycles=1)
    assert agent.session_layers().correction is None
    assert agent.session_layers().serialized is True
    assert Codex().turn_corrections().correction == CorrectionConfig()
    assert Codex().turn_corrections().continuation is not None


@pytest.mark.parametrize("agent", [Claude(), Codex()], ids=["claude", "codex"])
def test_an_agent_is_declared_without_its_sdk(agent: Agent) -> None:
    """Declaring loads no provider SDK; opening a session is what does."""
    assert agent.layered(SessionLayers()) is not agent
