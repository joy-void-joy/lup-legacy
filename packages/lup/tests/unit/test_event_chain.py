"""The observable transcript's hash chain: what keeps it whole, and what reads it."""

from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from lup.channels.stream import TAIL_WINDOW_BYTES, Stream
from lup.observability.audit import (
    BLOB_THRESHOLD_BYTES,
    ChainBreak,
    ObservableEvent,
    TraceActor,
    TraceContext,
    TraceJournal,
    chain_break,
    read_observable_events,
)
from lup.types import JsonValue


class Line(BaseModel, frozen=True):
    seq: int
    body: str


def journal_at(path: Path) -> TraceJournal:
    return TraceJournal(
        path,
        TraceContext.root("run-1", TraceActor(kind="harness", name="launcher")),
    )


def test_the_last_record_is_found_when_it_is_longer_than_the_window(
    tmp_path: Path,
) -> None:
    stream = Stream(tmp_path / "log.jsonl", TypeAdapter(Line))
    stream.append(Line(seq=0, body="short"))
    stream.append(Line(seq=1, body="x" * 500))

    assert stream.last(window=64) == Line(seq=1, body="x" * 500)


def test_a_log_with_no_complete_record_has_no_last(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_text('{"seq": 0, "body": "torn', encoding="utf-8")

    assert Stream(path, TypeAdapter(Line)).last(window=4) is None


def test_a_record_past_the_tail_window_does_not_restart_the_chain(
    tmp_path: Path,
) -> None:
    """One record bigger than the tail window, made of strings too short to spill.

    Each string stays under the blob threshold, so the record lands inline
    and whole; the writer appending after it must still find it as the head.
    """
    path = tmp_path / "observable.jsonl"
    journal = journal_at(path)
    journal.emit("run_start")
    piece = "y" * (BLOB_THRESHOLD_BYTES - 1024)
    parts: list[JsonValue] = [piece] * (TAIL_WINDOW_BYTES // len(piece) + 2)
    journal.emit("tool_result", {"parts": parts})
    journal.emit("run_end", {"succeeded": True})

    events = read_observable_events(path)
    assert [event.seq for event in events] == [0, 1, 2]
    assert chain_break(events) is None


def chained(path: Path, count: int) -> list[ObservableEvent]:
    journal = journal_at(path)
    for index in range(count):
        journal.emit("message", {"text": f"line {index}"})
    return read_observable_events(path)


def test_a_whole_chain_has_no_break(tmp_path: Path) -> None:
    assert chain_break(chained(tmp_path / "observable.jsonl", 3)) is None


def test_a_removed_record_breaks_the_sequence(tmp_path: Path) -> None:
    first, _, third = chained(tmp_path / "observable.jsonl", 3)

    assert chain_break([first, third]) == ChainBreak(
        position=1, seq=2, fault="sequence"
    )


def test_a_record_chained_onto_another_breaks_the_link(tmp_path: Path) -> None:
    first, _, third = chained(tmp_path / "observable.jsonl", 3)
    renumbered = third.model_copy(update={"seq": 1})

    assert chain_break([first, renumbered]) == ChainBreak(
        position=1, seq=1, fault="link"
    )


def test_a_second_chain_in_one_file_reads_as_a_restart(tmp_path: Path) -> None:
    earlier = chained(tmp_path / "earlier.jsonl", 2)
    later = chained(tmp_path / "later.jsonl", 1)

    assert chain_break([*earlier, *later]) == ChainBreak(
        position=2, seq=0, fault="restart"
    )


def test_a_record_of_another_shape_cannot_be_checked(tmp_path: Path) -> None:
    first, second = chained(tmp_path / "observable.jsonl", 2)
    older = second.model_copy(update={"schema_version": 1})

    assert chain_break([first, older]) == ChainBreak(position=1, seq=1, fault="schema")
