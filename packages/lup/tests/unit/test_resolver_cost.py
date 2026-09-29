"""Timing is read from journal evidence, without opening or mutating a run."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lup.channels.stream import Stream
from lup.coordination.refs import ActorRef
from lup.resolver.cost import cost_report, read_cost
from lup.resolver.record import (
    ENTRY_ADAPTER,
    JOURNAL_FILE,
    JournalEntry,
    RunEvent,
    RunFailedEvent,
    RecheckReusedEvent,
)
from lup.sessions.events import (
    SessionId,
    TurnCompletedEvent,
    TurnEvent,
    TurnId,
    TurnIdentifiers,
    TurnStartedEvent,
)


def recorded(
    seq: int, seconds: float, actor: str, event: TurnEvent | RunEvent
) -> JournalEntry:
    return JournalEntry(
        seq=seq,
        at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds),
        actor=ActorRef(kind=actor, id=actor),
        event=event,
    )


def identifiers(turn: str = "one") -> TurnIdentifiers:
    return TurnIdentifiers(session=SessionId(value="session"), turn=TurnId(value=turn))


def started(turn: str = "one") -> TurnStartedEvent:
    return TurnStartedEvent(identifiers=identifiers(turn))


def completed(turn: str = "one") -> TurnCompletedEvent:
    return TurnCompletedEvent(identifiers=identifiers(turn))


def test_overlap_and_silent_turns_are_active_without_double_counting() -> None:
    entries = [
        recorded(0, 0, "worker", started()),
        recorded(1, 10, "reviewer", started()),
        recorded(2, 20, "worker", started("two")),
        recorded(3, 700, "worker", completed()),
        recorded(4, 710, "reviewer", completed()),
        recorded(5, 720, "worker", completed("two")),
        recorded(6, 7320, "worker", started("three")),
        recorded(7, 7330, "worker", completed("three")),
    ]

    report = cost_report(iter(entries))

    assert report.wall_seconds == 7330
    assert report.active_seconds == 730
    assert report.idle_seconds == 6600
    assert report.peak_concurrency == 3
    worker = next(actor for actor in report.actors if actor.kind == "worker")
    assert worker.completed == 3
    assert worker.total_seconds == 1410
    assert worker.mean_seconds == 470
    assert worker.maximum_seconds == 700
    assert worker.peak_concurrency == 2
    assert worker.unfinished == worker.interrupted == 0
    assert len(report.idle_gaps) == 1
    assert report.idle_gaps[0].seconds == 6600
    assert report.idle_gaps[0].preceding == entries[5]
    assert report.model_dump(mode="json")["actors"][1]["mean_seconds"] == 470


def test_failure_closes_activity_and_retains_exact_reason_and_incomplete_counts() -> (
    None
):
    entries = [
        recorded(0, 0, "worker", started()),
        recorded(1, 30, "run", RunFailedEvent(reason="Not logged in")),
        recorded(2, 6330, "worker", started("two")),
        recorded(3, 6340, "run", RunFailedEvent(reason="Not logged in")),
        recorded(4, 6350, "reviewer", started()),
        recorded(5, 6360, "run", RunFailedEvent(reason="OAuth revoked")),
        recorded(6, 7000, "worker", started("three")),
        recorded(7, 7010, "worker", started("four")),
    ]

    report = cost_report(entries)

    assert report.active_seconds == 0
    assert report.uncertain_seconds == 60
    assert report.idle_seconds == 6950
    assert report.peak_concurrency == 0
    assert len(report.unresolved_intervals) == 5
    assert {failure.reason: failure.count for failure in report.failures} == {
        "Not logged in": 2,
        "OAuth revoked": 1,
    }
    worker = next(actor for actor in report.actors if actor.kind == "worker")
    assert worker.interrupted == 2 and worker.unfinished == 2
    assert worker.completed == 0 and worker.mean_seconds is None
    assert report.idle_gaps[0].preceding == entries[1]
    assert report.idle_gaps[0].seconds == 6300


def test_anomalies_do_not_fabricate_completed_turns() -> None:
    entries = [
        recorded(0, 0, "worker", completed()),
        recorded(1, 10, "worker", started()),
        recorded(2, 20, "worker", started()),
        recorded(3, 30, "worker", completed()),
    ]

    report = cost_report(entries, timedelta(seconds=5))

    assert len(report.anomalies) == 2
    assert report.actors[0].completed == 1
    assert report.actors[0].total_seconds == 20
    assert report.peak_concurrency == 1
    assert report.idle_seconds == 10


def test_unclosed_turn_does_not_make_later_resume_or_silence_active() -> None:
    entries = [
        recorded(0, 0, "worker", started("crashed")),
        recorded(1, 7200, "run", RecheckReusedEvent(concerns=[], commit="resume")),
        recorded(2, 7210, "worker", started("resumed")),
        recorded(3, 7220, "worker", completed("resumed")),
        recorded(4, 8000, "run", RecheckReusedEvent(concerns=[], commit="later")),
    ]

    report = cost_report(entries)

    assert report.wall_seconds == 8000
    assert report.active_seconds == 10
    assert report.uncertain_seconds == 7990
    assert report.idle_seconds == 0
    assert report.peak_concurrency == report.actors[0].peak_concurrency == 1
    assert report.actors[0].unfinished == 1
    assert len(report.unresolved_intervals) == 1
    interval = report.unresolved_intervals[0]
    assert interval.started == entries[0] and interval.ended == entries[-1]
    assert interval.outcome == "unfinished"
    assert report.idle_gaps == []


def test_missing_start_does_not_fabricate_idle_before_completion() -> None:
    entries = [
        recorded(0, 0, "run", RecheckReusedEvent(concerns=[], commit="resume")),
        recorded(1, 100, "worker", completed()),
        recorded(2, 1000, "worker", started("two")),
        recorded(3, 1010, "worker", completed("two")),
    ]

    report = cost_report(entries)

    assert report.active_seconds == 10
    assert report.uncertain_seconds == 100
    assert report.idle_seconds == 900
    assert report.unresolved_intervals[0].outcome == "missing_start"
    assert len(report.anomalies) == 1
    assert report.idle_gaps[0].preceding == entries[1]
    assert report.idle_gaps[0].seconds == 900


def test_empty_single_event_and_backward_timestamps_are_explicit() -> None:
    assert cost_report([]).started_at is None
    entry = recorded(0, 0, "run", RunFailedEvent(reason="startup failed"))
    assert cost_report([entry]).wall_seconds == 0
    with pytest.raises(ValueError, match="timestamp moved backward"):
        cost_report([entry, recorded(1, -1, "worker", started())])
    with pytest.raises(ValueError, match="nonnegative"):
        cost_report([], timedelta(seconds=-1))


def test_reading_only_needs_the_journal_and_preserves_every_byte(
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    path = root / JOURNAL_FILE
    stream = Stream(path, ENTRY_ADAPTER)
    stream.append(recorded(0, 0, "worker", started()))
    stream.append(recorded(1, 5, "worker", completed()))
    before = path.read_bytes()
    paths = set(root.rglob("*"))

    report = read_cost(root)

    assert report.active_seconds == 5
    assert path.read_bytes() == before
    assert set(root.rglob("*")) == paths
    with pytest.raises(FileNotFoundError, match="no resolver journal"):
        read_cost(tmp_path / "absent")
