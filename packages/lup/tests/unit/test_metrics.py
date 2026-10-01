"""Metrics collection behavior, including the snapshots tool-serving processes write."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lup.observability.metrics import (
    MetricsCollector,
    MetricsRetention,
    collector,
    configure_metrics,
    get_metrics_summary,
    merged_summary,
    metrics_directory,
    open_metrics_sink,
    prune_metrics,
    read_metrics_snapshots,
    read_metrics_summary,
    reset_metrics,
)


@pytest.fixture(autouse=True)
def clean_collector() -> Iterator[None]:
    reset_metrics()
    yield
    configure_metrics(None)
    reset_metrics()


def serving(session_dir: Path, server: str, member: str = "") -> MetricsCollector:
    """A collector standing in for one tool-serving process of a session."""
    process = MetricsCollector()
    process.sink = open_metrics_sink(session_dir, server, member)
    return process


def written_at(path: Path, moment: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (moment.timestamp(), moment.timestamp()))


class TestSnapshots:
    def test_each_process_keeps_a_snapshot_of_its_own(self, tmp_path: Path) -> None:
        coordination = serving(tmp_path, "coordination", "member-a")
        ledger = serving(tmp_path, "ledger", "member-a")

        coordination.record("coordination_send", 12.5)
        ledger.record("ledger_record", 7.5, is_error=True)
        coordination.record("coordination_send", 3.0)

        snapshots = read_metrics_snapshots(tmp_path)
        assert sorted(snapshot.server for snapshot in snapshots) == [
            "coordination",
            "ledger",
        ]
        assert {snapshot.member for snapshot in snapshots} == {"member-a"}
        merged = merged_summary([snapshot.summary for snapshot in snapshots])
        assert merged["total_tool_calls"] == 3
        assert merged["total_errors"] == 1
        send = merged["by_tool"]["coordination_send"]
        assert (send["min_duration_ms"], send["max_duration_ms"]) == (3.0, 12.5)

    def test_an_unreadable_snapshot_is_passed_over(self, tmp_path: Path) -> None:
        serving(tmp_path, "search").record("search", 1.0)
        broken = metrics_directory(tmp_path) / "broken.json"
        broken.write_text("{broken", encoding="utf-8")

        assert [s.server for s in read_metrics_snapshots(tmp_path)] == ["search"]

    def test_a_session_nothing_served_has_no_snapshots(self, tmp_path: Path) -> None:
        assert read_metrics_snapshots(tmp_path) == []

    def test_since_keeps_the_processes_still_writing(self, tmp_path: Path) -> None:
        serving(tmp_path, "earlier").record("earlier_tool", 1.0)
        [earlier] = read_metrics_snapshots(tmp_path)
        serving(tmp_path, "later").record("later_tool", 1.0)

        since = earlier.updated + timedelta(microseconds=1)
        assert [s.server for s in read_metrics_snapshots(tmp_path, since)] == ["later"]


class TestRetention:
    def test_a_snapshot_older_than_the_window_goes(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        stale, fresh = tmp_path / "stale.json", tmp_path / "fresh.json"
        written_at(stale, now - timedelta(days=31))
        written_at(fresh, now - timedelta(days=1))

        removed = prune_metrics(
            tmp_path, MetricsRetention(max_age=timedelta(days=30)), now
        )

        assert removed == [stale]
        assert fresh.exists()

    def test_only_the_newest_are_kept_past_the_count(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        for age in range(5):
            written_at(tmp_path / f"{age}.json", now - timedelta(minutes=age))

        prune_metrics(tmp_path, MetricsRetention(max_snapshots=2), now)

        assert sorted(path.name for path in tmp_path.iterdir()) == ["0.json", "1.json"]

    def test_a_process_starting_prunes_its_session(self, tmp_path: Path) -> None:
        stale = metrics_directory(tmp_path) / "gone.json"
        written_at(stale, datetime.now(UTC) - timedelta(days=365))

        open_metrics_sink(tmp_path, "search")

        assert not stale.exists()


class TestSessionSummary:
    def test_this_process_and_its_servers_read_as_one(self, tmp_path: Path) -> None:
        collector.record("in_process", 2.0)
        serving(tmp_path, "served").record("served_tool", 4.0, is_error=True)

        summary = read_metrics_summary(tmp_path)

        assert set(summary["by_tool"]) == {"in_process", "served_tool"}
        assert summary["total_errors"] == 1

    def test_servers_of_an_earlier_run_are_not_counted(self, tmp_path: Path) -> None:
        serving(tmp_path, "earlier").record("earlier_tool", 1.0)
        reset_metrics()

        assert read_metrics_summary(tmp_path)["by_tool"] == {}


class TestInProcessSummary:
    def test_error_rate_reflects_recorded_calls(self) -> None:
        collector.record("fetch", 10.0)
        collector.record("fetch", 10.0, is_error=True)

        summary = get_metrics_summary()
        assert summary["by_tool"]["fetch"]["error_rate"] == "50.0%"

    def test_reset_clears_state(self) -> None:
        collector.record("fetch", 10.0)
        reset_metrics()

        assert get_metrics_summary()["total_tool_calls"] == 0
