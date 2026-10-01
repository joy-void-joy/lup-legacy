# lup: ignore[any-type, dict-get, tuple-shape, own-model-dispatch]
# Test fixtures and assertions construct these shapes deliberately.
# The relay-event checks assert which kind the mailbox parsed back off
# actions.jsonl, and that a redelivered batch holds the same one — the variant
# is what is being observed from outside the union, not a walk over it that
# RelayEvent could answer.
"""Realtime relay wiring: mailbox protocol, served tools, and the wake loop.

The relay is how persistent mode works on backends whose tools run in a
subprocess (Codex/OpenAI): tools append events and a sleep request to
files, the parent applies them to the Scheduler and drives turn-per-wake
cycles. These tests round-trip that boundary without any LLM or
subprocess — the tool handlers and the parent loop share a directory,
exactly like the two processes share one in production.
"""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel


from lup.tools.mcp import LupMcpTool, ToolResponse
from lup.orchestration.realtime.relay import (
    MISSING_SLEEP_MESSAGE,
    ContextReadEvent,
    DebounceEvent,
    MetaEvent,
    RealtimeMailbox,
    RelayState,
    RemindEvent,
    ReplyEvent,
    ScheduleActionEvent,
    create_realtime_relay_tools,
    run_relay_session,
)
from lup.orchestration.realtime.models import SleepInput
from lup.orchestration.realtime.scheduler import Scheduler
from lup.orchestration.reflection import ReflectionGate
from lup.sessions.capabilities import SessionEngine, TurnEngine
from lup.sessions.events import (
    SessionId,
    StartedTurn,
    TurnId,
    TurnIdentifiers,
    TurnRequest,
    TurnResult,
)
from lup.observability.trace import TraceLogger
from lup.types import JsonObject, Usage
from tests.unit.doubles import IgnoredInterrupt, SilentStream, conversation_over


def tool_map(
    realtime_dir: Path, *, gate: ReflectionGate | None = None
) -> dict[str, LupMcpTool]:
    return {t.name: t for t in create_realtime_relay_tools(realtime_dir, gate=gate)}


def gated_tool_map(realtime_dir: Path) -> dict[str, LupMcpTool]:
    """Tools wired with a file-backed gate, as the template opts in."""
    gate = ReflectionGate(flag_path=RealtimeMailbox(realtime_dir).meta_flag_path)
    return tool_map(realtime_dir, gate=gate)


async def call(
    tools: dict[str, LupMcpTool], name: str, args: JsonObject
) -> ToolResponse:
    return await tools[name].handler(args)


class TestMailbox:
    def test_events_round_trip_in_order(self, tmp_path: Path) -> None:
        writer = RealtimeMailbox(tmp_path)
        reader = RealtimeMailbox(tmp_path)

        writer.append_event(ReplyEvent(message="hi"))
        writer.append_event(RemindEvent(label="follow up", delay_seconds=60))
        writer.append_event(ContextReadEvent())

        events = reader.read_new_events()
        assert [type(e) for e in events] == [ReplyEvent, RemindEvent, ContextReadEvent]
        assert reader.read_new_events() == []

        writer.append_event(MetaEvent(thought="pacing fine"))
        followup = reader.read_new_events()
        assert len(followup) == 1
        assert isinstance(followup[0], MetaEvent)

    def test_incomplete_line_not_consumed(self, tmp_path: Path) -> None:
        writer = RealtimeMailbox(tmp_path)
        reader = RealtimeMailbox(tmp_path)
        writer.append_event(ReplyEvent(message="complete"))

        with writer.actions_path.open("a", encoding="utf-8") as f:
            f.write('{"type": "reply", "message": "partial"')

        events = reader.read_new_events()
        assert [e.message for e in events if isinstance(e, ReplyEvent)] == ["complete"]

        with writer.actions_path.open("a", encoding="utf-8") as f:
            f.write(', "delay_seconds": 0}\n')

        completed = reader.read_new_events()
        assert [e.message for e in completed if isinstance(e, ReplyEvent)] == [
            "partial"
        ]

    def test_peek_without_commit_redelivers(self, tmp_path: Path) -> None:
        """A crash between applying an event and committing its offset must
        redeliver it — peek alone never consumes."""
        writer = RealtimeMailbox(tmp_path)
        reader = RealtimeMailbox(tmp_path)
        writer.append_event(ReplyEvent(message="first"))
        writer.append_event(ReplyEvent(message="second"))

        pairs = reader.peek_new_events()
        assert len(pairs) == 2
        repeated = reader.peek_new_events()
        assert [
            p.event.message for p in repeated if isinstance(p.event, ReplyEvent)
        ] == [
            "first",
            "second",
        ]

        assert isinstance(pairs[0].event, ReplyEvent)
        reader.read_offset = pairs[0].commit_offset

        redelivered = reader.peek_new_events()
        assert [
            p.event.message for p in redelivered if isinstance(p.event, ReplyEvent)
        ] == ["second"]

    def test_reset_for_new_run_clears_protocol_files(self, tmp_path: Path) -> None:
        """Re-running a session id must not replay the previous run: events,
        the sleep request, and the meta flag all clear."""
        from lup.orchestration.realtime.models import SleepInput

        old = RealtimeMailbox(tmp_path)
        old.append_event(ReplyEvent(message="stale"))
        old.write_sleep_request(SleepInput(seconds=60))
        old.meta_flag_path.write_text("", encoding="utf-8")

        fresh = RealtimeMailbox(tmp_path)
        fresh.reset_for_new_run()

        assert fresh.read_new_events() == []
        assert fresh.consume_sleep_request() is None
        assert not fresh.meta_flag_path.exists()

    def test_malformed_line_skipped(self, tmp_path: Path) -> None:
        mailbox = RealtimeMailbox(tmp_path)
        mailbox.root.mkdir(parents=True, exist_ok=True)
        with mailbox.actions_path.open("a", encoding="utf-8") as f:
            f.write('{"type": "unknown_event"}\n')
        mailbox.append_event(ReplyEvent(message="still parsed"))

        events = mailbox.read_new_events()
        assert len(events) == 1
        assert isinstance(events[0], ReplyEvent)

    def test_sleep_request_consumed_once(self, tmp_path: Path) -> None:
        from lup.orchestration.realtime.models import SleepInput

        mailbox = RealtimeMailbox(tmp_path)
        assert mailbox.consume_sleep_request() is None

        mailbox.write_sleep_request(SleepInput(seconds=300))
        request = mailbox.consume_sleep_request()
        assert request is not None
        assert request.seconds == 300
        assert mailbox.consume_sleep_request() is None

    def test_state_snapshot_carries_domain_fields(self, tmp_path: Path) -> None:
        mailbox = RealtimeMailbox(tmp_path)
        assert mailbox.read_state() is None

        mailbox.write_state(
            RelayState.model_validate({"unread_events": 2, "channel": "general"})
        )
        state = mailbox.read_state()
        assert state is not None
        assert state.unread_events == 2
        assert state.model_dump()["channel"] == "general"

    def test_append_event_backpressure(self, tmp_path: Path) -> None:
        """Once the actions file reaches the cap, writes raise instead of
        growing without bound — a looping agent gets an is_error response
        (RelayOverflowError is a ToolError) rather than a wedged parent."""
        from lup.orchestration.realtime.relay import RelayOverflowError

        mailbox = RealtimeMailbox(tmp_path, max_actions_bytes=16)
        mailbox.append_event(ReplyEvent(message="x" * 64))
        with pytest.raises(RelayOverflowError, match="not consuming"):
            mailbox.append_event(ReplyEvent(message="y"))

    def test_append_event_cap_disabled(self, tmp_path: Path) -> None:
        mailbox = RealtimeMailbox(tmp_path, max_actions_bytes=None)
        for n in range(3):
            mailbox.append_event(ReplyEvent(message=f"{n}" * 64))
        assert len(RealtimeMailbox(tmp_path).read_new_events()) == 3


class TestRelayTools:
    async def test_sleep_requires_meta_then_records(self, tmp_path: Path) -> None:
        tools = gated_tool_map(tmp_path)

        denied = await call(tools, "sleep", {"seconds": 60})
        assert denied.get("is_error") is True
        assert not (tmp_path / "sleep_request.json").exists()

        reflected = await call(tools, "meta", {"thought": "good pacing"})
        assert reflected.get("is_error") is None
        assert (tmp_path / "meta_flag").exists()

        recorded = await call(tools, "sleep", {"seconds": 60})
        assert recorded.get("is_error") is None
        assert (tmp_path / "sleep_request.json").exists()

    async def test_no_gate_skips_meta_requirement(self, tmp_path: Path) -> None:
        """The library default imposes no reflection: sleep records without a
        prior meta, while meta is still relayed for tracing."""
        tools = tool_map(tmp_path)

        recorded = await call(tools, "sleep", {"seconds": 60})
        assert recorded.get("is_error") is None
        assert (tmp_path / "sleep_request.json").exists()
        assert not (tmp_path / "meta_flag").exists()

        await call(tools, "meta", {"thought": "recorded for tracing only"})
        assert not (tmp_path / "meta_flag").exists()
        events = RealtimeMailbox(tmp_path).read_new_events()
        assert any(isinstance(e, MetaEvent) for e in events)

    async def test_reply_resets_meta_gate(self, tmp_path: Path) -> None:
        tools = gated_tool_map(tmp_path)

        await call(tools, "meta", {"thought": "assessed"})
        await call(tools, "reply", {"messages": [{"message": "hello"}]})

        denied = await call(tools, "sleep", {"seconds": 60})
        assert denied.get("is_error") is True

        await call(tools, "meta", {"thought": "assessed again"})
        allowed = await call(tools, "sleep", {"seconds": 60})
        assert allowed.get("is_error") is None

    async def test_unread_events_block_sleep_until_context(
        self, tmp_path: Path
    ) -> None:
        tools = gated_tool_map(tmp_path)
        parent = RealtimeMailbox(tmp_path)
        parent.write_state(RelayState(unread_events=3))

        await call(tools, "meta", {"thought": "assessed"})
        denied = await call(tools, "sleep", {"seconds": 60})
        assert denied.get("is_error") is True

        context = await call(tools, "context", {})
        block = context.get("content", [])[0]
        assert block["type"] == "text"
        payload = json.loads(block["text"])
        assert payload["unread_events"] == 3

        allowed = await call(tools, "sleep", {"seconds": 60})
        assert allowed.get("is_error") is None

        events = parent.read_new_events()
        assert any(isinstance(e, ContextReadEvent) for e in events)

    async def test_force_bypasses_unread_guard(self, tmp_path: Path) -> None:
        tools = gated_tool_map(tmp_path)
        RealtimeMailbox(tmp_path).write_state(RelayState(unread_events=3))

        await call(tools, "meta", {"thought": "assessed"})
        forced = await call(tools, "sleep", {"seconds": 60, "force": True})
        assert forced.get("is_error") is None

    async def test_reply_relays_staggered_messages(self, tmp_path: Path) -> None:
        tools = tool_map(tmp_path)
        parent = RealtimeMailbox(tmp_path)

        result = await call(
            tools,
            "reply",
            {
                "messages": [
                    {"message": "now"},
                    {"message": "later", "delay_seconds": 10},
                ]
            },
        )
        block = result.get("content", [])[0]
        assert block["type"] == "text"
        payload = json.loads(block["text"])
        assert payload == {"sent": 1, "scheduled": 1}

        events = parent.read_new_events()
        replies = [e for e in events if isinstance(e, ReplyEvent)]
        assert [(r.message, r.delay_seconds) for r in replies] == [
            ("now", 0),
            ("later", 10),
        ]


class TestRelayEventApply:
    async def test_reply_delivers_and_cancels_scheduled_action(self) -> None:
        delivered: list[str] = []

        async def on_action(content: str) -> None:
            delivered.append(content)

        scheduler = Scheduler(on_action=on_action)
        await ScheduleActionEvent(content="nudge", delay_seconds=300).apply(
            scheduler=scheduler
        )
        assert scheduler.get_state().get("scheduled_action") is not None

        await ReplyEvent(message="hello").apply(scheduler=scheduler)
        assert delivered == ["hello"]
        assert scheduler.get_state().get("scheduled_action") is None
        assert "nudge" in scheduler.ideas

    async def test_scheduling_events_reach_scheduler(self) -> None:
        async def on_action(_content: str) -> None:
            pass

        scheduler = Scheduler(on_action=on_action)
        await RemindEvent(label="check back", delay_seconds=120).apply(
            scheduler=scheduler
        )
        await DebounceEvent(initial_seconds=30, quiet_seconds=5).apply(
            scheduler=scheduler
        )

        state = scheduler.get_state()
        assert state.get("debounce_active") is True
        assert [r["label"] for r in state.get("pending_reminders", [])] == [
            "check back"
        ]

    async def test_context_read_consumes_pending_wake(self) -> None:
        async def on_action(_content: str) -> None:
            pass

        scheduler = Scheduler(on_action=on_action)
        scheduler.wake("user_message")
        assert scheduler.wake_pending is True

        await ContextReadEvent().apply(scheduler=scheduler)
        assert scheduler.wake_pending is False

    async def test_meta_logged_to_trace(self, tmp_path: Path) -> None:
        async def on_action(_content: str) -> None:
            pass

        trace = TraceLogger(trace_path=tmp_path / "trace.md", title="t")
        await MetaEvent(thought="pacing was rushed").apply(
            scheduler=Scheduler(on_action=on_action),
            trace_logger=trace,
        )
        assert any("pacing was rushed" in entry.content for entry in trace.entries)


class AgentTurn:
    """One scripted agent turn: tool calls the fake agent makes."""

    def __init__(
        self,
        tools: dict[str, LupMcpTool],
        calls: list[tuple[str, dict[str, Any]]],
        *,
        pause_seconds: float = 0.0,
    ) -> None:
        self.tools = tools
        self.calls = calls
        self.pause_seconds = pause_seconds

    async def play(self) -> None:
        for name, args in self.calls:
            await self.tools[name].handler(args)
        if self.pause_seconds:
            await asyncio.sleep(self.pause_seconds)


class FakeTurn(TurnEngine[None]):
    """Resolve one scripted relay turn."""

    def __init__(self, conversation: "FakeConversation", index: int) -> None:
        self.conversation = conversation
        self.index = index

    async def result(self) -> TurnResult[None]:
        await self.conversation.play_turn(self.index)
        return TurnResult[None](
            output=None,
            messages=[],
            blocks=[],
            usage=Usage(),
            duration=timedelta(),
            identifiers=TurnIdentifiers(
                session=SessionId(value="relay-test"),
                turn=TurnId(value=str(self.index)),
            ),
        )


class FakeConversation(SessionEngine):
    """Session stand-in that plays scripted turns."""

    def __init__(self, turns: list[AgentTurn]) -> None:
        self.turns = turns
        self.prompts: list[str] = []

    async def play_turn(self, index: int) -> None:
        await self.turns[index].play()

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> StartedTurn[T]:
        self.prompts.append(request.input.text)
        handle = StartedTurn[None](
            turn=FakeTurn(self, len(self.prompts) - 1),
            events=SilentStream(),
            interrupt=IgnoredInterrupt(),
        )
        return cast("StartedTurn[T]", handle)  # lup: ignore[cast] — generic test double


META = ("meta", {"thought": "assessed"})
SLEEP = ("sleep", {"seconds": 0})


class TestRelaySession:
    async def test_wake_cycles_with_corrective_turn(self, tmp_path: Path) -> None:
        tools = tool_map(tmp_path)
        delivered: list[str] = []

        async def on_action(content: str) -> None:
            delivered.append(content)

        scheduler = Scheduler(on_action=on_action)
        conversation = FakeConversation(
            [
                AgentTurn(
                    tools,
                    [("reply", {"messages": [{"message": "hello"}]}), META, SLEEP],
                ),
                AgentTurn(tools, []),  # ends turn without sleeping
                AgentTurn(tools, [META, SLEEP]),
            ]
        )

        turns = await run_relay_session(
            conversation_over(conversation),
            scheduler=scheduler,
            mailbox=RealtimeMailbox(tmp_path),
            initial_prompt="[session start]",
            should_continue=lambda: len(conversation.prompts) < 3,
            poll_interval_seconds=0.01,
        )

        assert turns == 3
        assert delivered == ["hello"]
        assert conversation.prompts[0] == "[session start]"
        assert conversation.prompts[1].startswith("[wake] reason: timer")
        assert conversation.prompts[2] == MISSING_SLEEP_MESSAGE

    async def test_turn_completion_checkpoints_the_cumulative_count(
        self, tmp_path: Path
    ) -> None:
        # A durable caller checkpoints after each turn, so the count it is
        # handed has to arrive once per turn and already include that turn.
        tools = tool_map(tmp_path)
        checkpoints: list[int] = []

        async def on_action(_content: str) -> None:
            return None

        async def on_turn_complete(turns: int) -> None:
            checkpoints.append(turns)

        conversation = FakeConversation(
            [AgentTurn(tools, [META, SLEEP]), AgentTurn(tools, [META, SLEEP])]
        )

        turns = await run_relay_session(
            conversation_over(conversation),
            scheduler=Scheduler(on_action=on_action),
            mailbox=RealtimeMailbox(tmp_path),
            initial_prompt="[session start]",
            on_turn_complete=on_turn_complete,
            should_continue=lambda: len(conversation.prompts) < 2,
            poll_interval_seconds=0.01,
        )

        assert checkpoints == [1, 2]
        assert turns == 2

    async def test_events_applied_during_turn_not_after(self, tmp_path: Path) -> None:
        tools = tool_map(tmp_path)
        delivered_during_turn: list[bool] = []

        async def on_action(_content: str) -> None:
            delivered_during_turn.append(in_turn[0])

        in_turn = [False]

        class MidTurnConversation(FakeConversation):
            async def play_turn(self, index: int) -> None:
                in_turn[0] = True
                try:
                    await super().play_turn(index)
                finally:
                    in_turn[0] = False

        conversation = MidTurnConversation(
            [
                AgentTurn(
                    tools,
                    [
                        ("reply", {"messages": [{"message": "mid-turn"}]}),
                        META,
                        SLEEP,
                    ],
                    pause_seconds=0.1,
                ),
            ]
        )

        await run_relay_session(
            conversation_over(conversation),
            scheduler=Scheduler(on_action=on_action),
            mailbox=RealtimeMailbox(tmp_path),
            initial_prompt="[session start]",
            should_continue=lambda: len(conversation.prompts) < 1,
            poll_interval_seconds=0.01,
        )

        assert delivered_during_turn == [True]

    async def test_session_ends_after_repeated_missing_sleep(
        self, tmp_path: Path
    ) -> None:
        tools = tool_map(tmp_path)

        async def on_action(_content: str) -> None:
            pass

        conversation = FakeConversation([AgentTurn(tools, []) for _ in range(5)])

        turns = await run_relay_session(
            conversation_over(conversation),
            scheduler=Scheduler(on_action=on_action),
            mailbox=RealtimeMailbox(tmp_path),
            initial_prompt="[session start]",
            max_missing_sleep_retries=2,
            poll_interval_seconds=0.01,
        )

        assert turns == 3  # initial turn + two corrective retries
        assert conversation.prompts[1:] == [MISSING_SLEEP_MESSAGE] * 2

    async def test_sleep_request_applies_debounce_and_follow_ups(
        self, tmp_path: Path
    ) -> None:
        tools = tool_map(tmp_path)
        delivered: list[str] = []

        async def on_action(content: str) -> None:
            delivered.append(content)

        scheduler = Scheduler(on_action=on_action)
        conversation = FakeConversation(
            [
                AgentTurn(
                    tools,
                    [
                        META,
                        (
                            "sleep",
                            {
                                "seconds": 0,
                                "follow_ups": [
                                    {"message": "thread pull", "delay_seconds": 600}
                                ],
                            },
                        ),
                    ],
                ),
            ]
        )

        await run_relay_session(
            conversation_over(conversation),
            scheduler=scheduler,
            mailbox=RealtimeMailbox(tmp_path),
            initial_prompt="[session start]",
            should_continue=lambda: len(conversation.prompts) < 1,
            poll_interval_seconds=0.01,
        )

        scheduler.cancel_delayed_actions()
        assert "thread pull" in scheduler.ideas
        assert delivered == []

    async def test_gate_reset_between_turns_requires_fresh_meta(
        self, tmp_path: Path
    ) -> None:
        """A supplied gate keeps reflection per-turn: the parent resets it
        after each sleep, so a turn that tries to sleep without a fresh meta
        is refused and redirected as a missing sleep."""
        gate = ReflectionGate(flag_path=RealtimeMailbox(tmp_path).meta_flag_path)
        tools = tool_map(tmp_path, gate=gate)

        async def on_action(_content: str) -> None:
            pass

        conversation = FakeConversation(
            [
                AgentTurn(tools, [META, SLEEP]),
                AgentTurn(tools, [SLEEP]),  # no fresh meta — sleep is refused
                AgentTurn(tools, [META, SLEEP]),
            ]
        )

        turns = await run_relay_session(
            conversation_over(conversation),
            scheduler=Scheduler(on_action=on_action),
            mailbox=RealtimeMailbox(tmp_path),
            initial_prompt="[session start]",
            gate=gate,
            should_continue=lambda: len(conversation.prompts) < 3,
            poll_interval_seconds=0.01,
        )

        assert turns == 3
        assert conversation.prompts[2] == MISSING_SLEEP_MESSAGE


async def apply_with_per_event_commit(
    mailbox: RealtimeMailbox,
    applied: list[str],
    *,
    fail_on: str | None = None,
) -> None:
    """Mirror the parent loop: apply each event, commit only after.

    ``fail_on`` makes the handler raise when it sees a reply with that
    message, standing in for a handler that errors mid-batch.
    """
    for pair in mailbox.peek_new_events():
        if isinstance(pair.event, ReplyEvent):
            if pair.event.message == fail_on:
                raise RuntimeError("handler boom")
            applied.append(pair.event.message)
        mailbox.read_offset = pair.commit_offset


class TestRelayNoLoss:
    async def test_raising_handler_leaves_unapplied_events(
        self, tmp_path: Path
    ) -> None:
        """A mid-batch failure must not advance past un-applied events."""
        writer = RealtimeMailbox(tmp_path)
        reader = RealtimeMailbox(tmp_path)
        writer.append_event(ReplyEvent(message="one"))
        writer.append_event(ReplyEvent(message="two"))
        writer.append_event(ReplyEvent(message="three"))

        applied: list[str] = []
        with pytest.raises(RuntimeError):
            await apply_with_per_event_commit(reader, applied, fail_on="two")

        # "one" applied; the failure at "two" left the offset there.
        assert applied == ["one"]

        # A retry redelivers "two" and "three" — nothing was dropped.
        await apply_with_per_event_commit(reader, applied)
        assert applied == ["one", "two", "three"]

    async def test_no_redelivery_once_fully_applied(self, tmp_path: Path) -> None:
        writer = RealtimeMailbox(tmp_path)
        reader = RealtimeMailbox(tmp_path)
        writer.append_event(ReplyEvent(message="only"))

        applied: list[str] = []
        await apply_with_per_event_commit(reader, applied)
        await apply_with_per_event_commit(reader, applied)
        assert applied == ["only"]

    def test_peek_does_not_advance_offset(self, tmp_path: Path) -> None:
        writer = RealtimeMailbox(tmp_path)
        reader = RealtimeMailbox(tmp_path)
        writer.append_event(ReplyEvent(message="x"))

        before = reader.read_offset
        pairs = reader.peek_new_events()
        assert [p.event.message for p in pairs if isinstance(p.event, ReplyEvent)] == [
            "x"
        ]
        assert reader.read_offset == before  # peek is non-destructive
        # Committing the last pair's offset consumes the event.
        reader.read_offset = pairs[-1].commit_offset
        assert reader.peek_new_events() == []


class TestRelayStaleReplay:
    def test_reset_clears_previous_run(self, tmp_path: Path) -> None:
        """A fresh run must not consume the previous run's leftovers."""
        mailbox = RealtimeMailbox(tmp_path)
        mailbox.append_event(ReplyEvent(message="ghost"))
        mailbox.append_event(RemindEvent(label="old", delay_seconds=5))
        mailbox.write_sleep_request(SleepInput(seconds=42))
        mailbox.meta_flag_path.parent.mkdir(parents=True, exist_ok=True)
        mailbox.meta_flag_path.touch()

        fresh = RealtimeMailbox(tmp_path)
        # A partial read of the earlier run leaves the offset non-zero too.
        fresh.read_offset = 10
        fresh.reset_for_new_run()

        assert fresh.read_offset == 0
        assert fresh.read_new_events() == []
        assert fresh.consume_sleep_request() is None
        assert not mailbox.meta_flag_path.exists()
        # The file is truncated, not deleted, so the within-run append path
        # keeps working.
        assert mailbox.actions_path.read_text(encoding="utf-8") == ""

    def test_within_run_protocol_survives_reset(self, tmp_path: Path) -> None:
        mailbox = RealtimeMailbox(tmp_path)
        mailbox.reset_for_new_run()
        mailbox.append_event(ReplyEvent(message="after reset"))

        events = mailbox.read_new_events()
        assert [e.message for e in events if isinstance(e, ReplyEvent)] == [
            "after reset"
        ]
