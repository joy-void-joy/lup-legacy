"""Metrics collection behavior, including the cross-process file mode."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from lup.observability.metrics import (
    MetricsCollector,
    collector,
    configure_metrics,
    get_metrics_summary,
    metrics_path,
    read_metrics_summary,
    reset_metrics,
)


@pytest.fixture(autouse=True)
def clean_collector() -> Iterator[None]:
    reset_metrics()
    yield
    configure_metrics(None)
    reset_metrics()


class TestFileMode:
    def test_records_flush_through_to_disk(self, tmp_path: Path) -> None:
        configure_metrics(metrics_path(tmp_path))

        collector.record("search", 12.5)
        collector.record("search", 7.5, is_error=True)

        summary = read_metrics_summary(tmp_path)
        assert summary is not None
        assert summary["total_tool_calls"] == 2
        assert summary["total_errors"] == 1
        assert summary["by_tool"]["search"]["call_count"] == 2

    def test_corrupt_flush_file_reads_as_none(self, tmp_path: Path) -> None:
        metrics_path(tmp_path).write_text("{broken", encoding="utf-8")

        assert read_metrics_summary(tmp_path) is None

    def test_absent_file_reads_as_none(self, tmp_path: Path) -> None:
        assert read_metrics_summary(tmp_path) is None


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


class TestMetricsAtomicFlush:
    def test_flush_leaves_valid_json_and_no_temp(self, tmp_path: Path) -> None:
        target = metrics_path(tmp_path)
        collector = MetricsCollector()
        collector.flush_path = target

        collector.record("search", 12.5)
        collector.record("search", 7.5, is_error=True)

        # The target is always complete, parseable JSON.
        summary = json.loads(target.read_text(encoding="utf-8"))
        assert summary["total_tool_calls"] == 2
        # The write-then-rename temp file is gone after a successful flush.
        assert not target.with_suffix(".tmp").exists()
        assert read_metrics_summary(tmp_path) is not None

    def test_target_only_changes_on_atomic_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The target file is mutated only by the rename, never in place.

        If the commit (``Path.replace``) fails, the target a reader may be
        parsing must still hold the previous complete snapshot — proof the
        new bytes were staged on a temp file, not written into the target.
        A direct write-into-place would truncate the target first.
        """
        target = metrics_path(tmp_path)
        collector = MetricsCollector()
        collector.flush_path = target

        collector.record("good", 1.0)
        good = target.read_text(encoding="utf-8")
        assert json.loads(good)["total_tool_calls"] == 1

        def failing_replace(src: object, dst: object) -> None:
            raise OSError("rename interrupted")

        monkeypatch.setattr(Path, "replace", failing_replace)
        collector.record("doomed", 1.0)  # flush() swallows the OSError

        # The target still parses as the last complete snapshot, untouched.
        assert target.read_text(encoding="utf-8") == good
