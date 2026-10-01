"""The observable journal: redaction, the hash chain, and delegated spans."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from lup.sessions.capabilities import EventStream
from lup.sessions.composition import AcceptedTurn, CompletedTurn, ComposedSession
from lup.sessions.errors import DeltaStreamingDisabled

from lup.sessions.events import (
    BlockCompletedEvent,
    BlockDeltaEvent,
    MessageCompletedEvent,
    SessionId,
    TurnCompletedEvent,
    TurnId,
    TurnIdentifiers,
    TurnMessage,
    TurnTextBlock,
    TurnToolCallBlock,
    LiveTurnEvent,
    TurnEvent,
    TurnStartedEvent,
)
from lup.observability.audit import (
    ArgvRedaction,
    ChainBreak,
    KeyRedaction,
    Redactions,
    TraceActor,
    TraceContext,
    TraceJournal,
    JournalSession,
    JournalEventStream,
    TurnRecorder,
    read_observable_events,
    chain_break,
)
from lup.types import JsonObject
from tests.unit.test_capability_runtime import RecordingBinder
from tests.unit.doubles import (
    IgnoredInterrupt,
    request_for,
)

IDENTIFIERS = TurnIdentifiers(
    session=SessionId(value="session-1"), turn=TurnId(value="turn-1")
)


def journal_at(path: Path) -> TraceJournal:
    return TraceJournal(
        path,
        TraceContext.root("run-1", TraceActor(kind="orchestrator", name="main")),
    )


def test_native_message_evidence_is_journaled_without_loss(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    native: JsonObject = {
        "type": "agentMessage",
        "id": "native-1",
        "text": "hello",
        "memoryCitation": {"entries": [{"path": "notes.md"}]},
    }
    message = TurnMessage(
        role="assistant", blocks=[TurnTextBlock(text="hello")], native=native
    )
    TurnRecorder(journal_at(path)).record(
        MessageCompletedEvent(identifiers=IDENTIFIERS, message=message)
    )
    events = read_observable_events(path)
    assert len(events) == 1 and events[0].kind == "native_activity"
    assert events[0].payload["message"] == message.model_dump(mode="json")
    assert TurnMessage.model_validate(events[0].payload["message"]).native == native


def test_a_key_that_names_a_secret_loses_its_value_at_any_depth() -> None:
    redaction = KeyRedaction()
    payload: JsonObject = {
        "headers": {"Authorization": "Bearer sk-abc", "Accept": "application/json"},
        "nested": [{"api_key": "sk-xyz"}, {"harmless": "kept"}],
    }

    assert redaction.apply(payload) == {
        "headers": {"Authorization": "[REDACTED]", "Accept": "application/json"},
        "nested": [{"api_key": "[REDACTED]"}, {"harmless": "kept"}],
    }


def test_a_secret_under_an_innocuous_key_is_not_caught_by_key_redaction() -> None:
    # Pinned because it is the known limit of matching on names, and the
    # reason another rule composes in rather than this one growing.
    payload: JsonObject = {"note": "the token is sk-abc123"}
    assert KeyRedaction().apply(payload) == payload


def test_argv_redaction_covers_both_spellings_of_a_flag() -> None:
    argv = ["run", "--api-key=sk-abc", "--password", "hunter2", "--verbose"]

    assert ArgvRedaction().arguments(argv) == [
        "run",
        "--api-key=[REDACTED]",
        "--password",
        "[REDACTED]",
        "--verbose",
    ]


def test_argv_redaction_leaves_a_list_that_is_not_a_command_line_alone() -> None:
    assert ArgvRedaction().apply(["alpha", "beta"]) == ["alpha", "beta"]


def test_redactions_apply_each_rule_to_what_it_understands() -> None:
    # A caller who knows it holds a command line composes the argv rule in.
    # Each rule passes over what is not its business, so order is safe.
    composed = Redactions(KeyRedaction(), ArgvRedaction())

    assert composed.apply(["run", "--api-key", "sk-abc"]) == [
        "run",
        "--api-key",
        "[REDACTED]",
    ]
    assert composed.apply({"api_key": "sk-abc"}) == {"api_key": "[REDACTED]"}


def test_the_chain_verifies_and_a_tampered_payload_breaks_it(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = journal_at(path)
    journal.emit("run_start")
    journal.emit("message", {"text": "hello"})

    events = read_observable_events(path)
    assert [event.seq for event in events] == [0, 1]
    assert chain_break(events) is None

    tampered = events[1].model_copy(update={"payload": {"text": "goodbye"}})
    assert chain_break([events[0], tampered]) == ChainBreak(
        position=1, seq=1, fault="digest"
    )


def test_a_secret_never_reaches_the_file(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal_at(path).emit("tool_call", {"api_key": "sk-should-not-persist"})

    assert "sk-should-not-persist" not in path.read_text(encoding="utf-8")


def test_a_child_span_shares_the_chain_and_names_its_parent(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = journal_at(path)
    journal.emit("run_start")
    child = journal.child(TraceActor(kind="tool", name="lup.search"))
    child.emit("tool_call")

    events = read_observable_events(path)
    assert chain_break(events) is None
    assert events[1].parent_span_id == events[0].span_id
    assert events[1].tool_name == "search"


def test_a_delegated_span_is_written_as_it_streams(tmp_path: Path) -> None:
    # The delegating call streams past first, so the role is known before the
    # delegated messages arrive and nothing has to be reconstructed at the end.
    path = tmp_path / "journal.jsonl"
    recorder = TurnRecorder(journal_at(path))

    recorder.record(
        BlockCompletedEvent(
            identifiers=IDENTIFIERS,
            block=TurnToolCallBlock(
                id="call-1", name="Agent", arguments={"subagent_type": "code-reviewer"}
            ),
        )
    )
    recorder.record(
        MessageCompletedEvent(
            identifiers=IDENTIFIERS,
            message=TurnMessage(
                role="assistant",
                blocks=[TurnTextBlock(text="reviewing")],
                parent_tool_call_id="call-1",
                model="claude",
            ),
        )
    )
    recorder.record(TurnCompletedEvent(identifiers=IDENTIFIERS))

    events = read_observable_events(path)
    assert chain_break(events) is None
    assert [event.kind for event in events] == [
        "tool_call",
        "subagent_start",
        "message",
        "subagent_end",
        "turn_end",
    ]
    delegated = events[1]
    assert delegated.actor.kind == "native_subagent"
    assert delegated.actor.name == "code-reviewer"
    assert delegated.actor.model == "claude"


def test_an_undelegated_message_opens_no_span(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    recorder = TurnRecorder(journal_at(path))

    recorder.record(
        MessageCompletedEvent(
            identifiers=IDENTIFIERS,
            message=TurnMessage(role="assistant", blocks=[TurnTextBlock(text="hi")]),
        )
    )

    assert read_observable_events(path) == []


class DurableSource(EventStream):
    """A provider that refuses deltas at accessor or iterator entry."""

    def __init__(self, deferred: bool) -> None:
        self.deferred = deferred
        self.reads = 0

    async def events(self) -> AsyncIterator[TurnEvent]:
        self.reads += 1
        yield TurnStartedEvent(identifiers=IDENTIFIERS)
        yield BlockCompletedEvent(
            identifiers=IDENTIFIERS, block=TurnTextBlock(text="durable answer")
        )
        yield TurnCompletedEvent(identifiers=IDENTIFIERS)

    def live(self) -> AsyncIterator[LiveTurnEvent]:
        if not self.deferred:
            raise DeltaStreamingDisabled("partials disabled")
        return self.refused()

    async def refused(self) -> AsyncIterator[LiveTurnEvent]:
        if self.deferred:
            raise DeltaStreamingDisabled("partials disabled")
        yield TurnStartedEvent(identifiers=IDENTIFIERS)


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True])
async def test_a_delta_free_session_keeps_its_durable_journal_and_result(
    tmp_path: Path, deferred: bool
) -> None:
    source = DurableSource(deferred)

    async def complete() -> CompletedTurn:
        return CompletedTurn(blocks=[TurnTextBlock(text="durable answer")])

    async def start(_text: str) -> AcceptedTurn:
        return AcceptedTurn(
            identifiers=IDENTIFIERS,
            complete=complete,
            events=source,
            interrupt=IgnoredInterrupt(),
        )

    path = tmp_path / "journal.jsonl"
    session = JournalSession(
        ComposedSession(start, RecordingBinder()), journal_at(path)
    )
    handle = await session.start(request_for("hello"))
    assert handle.events is not None
    with pytest.raises(DeltaStreamingDisabled):
        await anext(handle.events.live())

    durable = [event async for event in handle.events.events()]
    result = await handle.turn.result()

    assert [event.type for event in durable] == [
        "turn_started",
        "block_completed",
        "turn_completed",
    ]
    assert result.blocks == [TurnTextBlock(text="durable answer")]
    assert source.reads == 1
    recorded = read_observable_events(path)
    assert [event.kind for event in recorded] == [
        "turn_input",
        "turn_start",
        "message",
        "turn_end",
        "turn_result",
    ]
    assert chain_break(recorded) is None


class LiveSource(EventStream):
    def __init__(self, fails: bool) -> None:
        self.fails = fails

    def events(self) -> AsyncIterator[TurnEvent]:
        raise AssertionError("a partially consumed stream must not be replayed")

    async def live(self) -> AsyncIterator[LiveTurnEvent]:
        yield TurnStartedEvent(identifiers=IDENTIFIERS)
        if self.fails:
            raise DeltaStreamingDisabled("invalid late capability refusal")
        yield BlockDeltaEvent(identifiers=IDENTIFIERS, delta="partial")
        yield TurnCompletedEvent(identifiers=IDENTIFIERS)


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_journaling_preserves_live_events_and_never_replays_after_a_delta(
    tmp_path: Path, fails: bool
) -> None:
    source = JournalEventStream(
        LiveSource(fails), journal_at(tmp_path / "journal.jsonl"), asyncio.Queue()
    )

    events = [event async for event in source.live()]

    if fails:
        with pytest.raises(DeltaStreamingDisabled, match="late capability"):
            await source.wait()
        assert [event.type for event in events] == ["turn_started"]
    else:
        await source.wait()
        assert [event.type for event in events] == [
            "turn_started",
            "block_delta",
            "turn_completed",
        ]
