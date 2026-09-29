"""The run journal: one ordered record, per-actor and merged views."""

from pathlib import Path

from lup.coordination.refs import ActorRef
from lup.resolver.record import Journal
from lup.sessions.events import (
    BlockCompletedEvent,
    MessageCompletedEvent,
    SessionId,
    TurnIdentifiers,
    TurnId,
    TurnMessage,
    TurnTextBlock,
    TurnThinkingBlock,
    TurnToolCallBlock,
    TurnToolResultBlock,
)

WORKER = ActorRef(kind="worker", id="docs-set", round=1)
REVIEWER = ActorRef(kind="reviewer", id="docs-set", round=1)
MERGER = ActorRef(kind="merger", id="integration")


def identifiers() -> TurnIdentifiers:
    return TurnIdentifiers(session=SessionId(value="s"), turn=TurnId(value="t"))


def block_event(text: str) -> BlockCompletedEvent:
    return BlockCompletedEvent(
        identifiers=identifiers(), block=TurnTextBlock(text=text)
    )


def test_the_merged_view_is_the_unfiltered_sequence(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    journal.append(WORKER, block_event("worker one"))
    journal.append(MERGER, block_event("merger one"))
    journal.append(WORKER, block_event("worker two"))

    assert [entry.seq for entry in journal.read()] == [0, 1, 2]
    assert [entry.actor.kind for entry in journal.read()] == [
        "worker",
        "merger",
        "worker",
    ]


def test_a_per_actor_view_is_the_same_sequence_filtered(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    journal.append(WORKER, block_event("worker one"))
    journal.append(MERGER, block_event("merger one"))
    journal.append(WORKER, block_event("worker two"))

    worker = journal.for_actor(WORKER)
    assert [entry.seq for entry in worker] == [0, 2]
    assert all(entry.actor == WORKER for entry in worker)


def test_a_later_round_is_a_different_actor(tmp_path: Path) -> None:
    """Round two holds a different session, so its trace must not merge in."""
    journal = Journal(tmp_path)
    second = ActorRef(kind="worker", id="docs-set", round=2)
    journal.append(WORKER, block_event("first attempt"))
    journal.append(second, block_event("second attempt"))

    assert len(journal.for_actor(WORKER)) == 1
    assert len(journal.for_actor(second)) == 1
    assert journal.actors() == [WORKER, second]


def test_reconnecting_replays_from_the_next_sequence_not_from_zero(
    tmp_path: Path,
) -> None:
    journal = Journal(tmp_path)
    for index in range(5):
        journal.append(WORKER, block_event(f"event {index}"))

    assert [entry.seq for entry in journal.read(after_seq=2)] == [3, 4]


def test_a_reopened_journal_continues_the_sequence(tmp_path: Path) -> None:
    """A parked run resumes without renumbering what a page already saw."""
    first = Journal(tmp_path)
    first.append(WORKER, block_event("before the park"))
    first.append(WORKER, block_event("also before"))

    resumed = Journal(tmp_path)
    entry = resumed.append(WORKER, block_event("after the park"))

    assert entry.seq == 2
    assert [item.seq for item in resumed.read()] == [0, 1, 2]


def test_reasoning_speech_calls_and_results_all_reach_the_record(
    tmp_path: Path,
) -> None:
    """A trace that omitted any of these could not show what an actor did."""
    journal = Journal(tmp_path)
    blocks = [
        TurnThinkingBlock(thinking="weighing two options"),
        TurnTextBlock(text="I will read the file"),
        TurnToolCallBlock(id="t1", name="Read", arguments={"path": "x.py"}),
        TurnToolResultBlock(tool_call_id="t1", content="file body"),
    ]
    for block in blocks:
        journal.append(
            WORKER, BlockCompletedEvent(identifiers=identifiers(), block=block)
        )

    recorded = [
        entry.event.block.type
        for entry in journal.for_actor(WORKER)
        if isinstance(entry.event, BlockCompletedEvent)
    ]
    assert recorded == ["thinking", "text", "tool_call", "tool_result"]


def test_one_entry_is_readable_whole_for_an_expanding_reader(tmp_path: Path) -> None:
    journal = Journal(tmp_path)
    journal.append(WORKER, block_event("short"))
    journal.append(
        REVIEWER,
        MessageCompletedEvent(
            identifiers=identifiers(),
            message=TurnMessage(
                role="assistant", blocks=[TurnTextBlock(text="x" * 5000)]
            ),
        ),
    )

    entry = journal.entry(1)
    assert entry is not None
    assert entry.actor == REVIEWER
    assert isinstance(entry.event, MessageCompletedEvent)
    block = entry.event.message.blocks[0]
    assert isinstance(block, TurnTextBlock)
    assert len(block.text) == 5000


def test_the_record_before_a_sequence_is_paged_oldest_first(tmp_path: Path) -> None:
    """A bounded tail leaves older record reachable one page at a time."""
    journal = Journal(tmp_path)
    for index in range(10):
        journal.append(WORKER, block_event(f"entry {index}"))

    assert [entry.seq for entry in journal.before(6, 3)] == [3, 4, 5]
    assert [entry.seq for entry in journal.before(2, 100)] == [0, 1]
    assert journal.before(0, 3) == []
    assert journal.before(6, 0) == []


def test_a_journal_never_refuses_to_record(tmp_path: Path) -> None:
    """The stream's cap is optional precisely so this one declares none."""
    journal = Journal(tmp_path)
    for index in range(200):
        journal.append(WORKER, block_event("x" * 200 + str(index)))

    assert len(journal.read()) == 200
