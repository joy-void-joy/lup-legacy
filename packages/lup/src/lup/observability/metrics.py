"""Tool call metrics tracking.

MCP tools defined with :func:`lup.tools.mcp.lup_tool` are tracked automatically —
every call's duration and error status is recorded into the module-level
collector, no decorator needed. Metrics are saved with each session for
feedback loop analysis.

Use the :func:`tracked` decorator for non-tool async functions (background
jobs, API helpers, sub-agent invocations) that should feed the same
metrics stream.

**Across processes.** A tool server a runtime starts for a session runs in a
process of its own, so what its tools record is written through, after every
call, to a snapshot under the session directory's ``metrics/``: one file per
process, because a session starts one process per server, and a natively
launched session shares its directory with every other session launched in
its checkout — a single file would hold whichever process wrote last. A
snapshot is replaced whole on every write, so it is only as large as the
tools its process has served, and the directory is held to a
:class:`MetricsRetention` each time a process starts. Two readers fold them:
:func:`read_metrics_summary` for the session result of a run this process
opened, and :func:`read_metrics_snapshots` for ``lup-devtools tools metrics``.

Examples:
    Record a non-tool helper alongside tool metrics::

        >>> @tracked("fetch_market_data")
        ... async def fetch_market_data(symbol: str) -> dict[str, float]:
        ...     return {"price": 101.5}

    Retrieve aggregated metrics at session end::

        >>> summary = get_metrics_summary()
        >>> summary["total_tool_calls"]
        15
        >>> summary["by_tool"]["search"]["avg_duration_ms"]
        42.5

    Reset metrics between sessions::

        >>> reset_metrics()
"""

import logging
import os
import time
from collections import defaultdict
from collections.abc import Callable, Coroutine, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import TypedDict

from pydantic import BaseModel, Field, ValidationError

from lup.channels.models import publish_atomic, utc_now

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — the snapshot directory's own name inside
# a session directory, which every writer and reader must spell alike
METRICS_DIRNAME = "metrics"


def metrics_directory(session_dir: Path) -> Path:
    """Where the tool-serving processes of one session write their snapshots."""
    return session_dir / METRICS_DIRNAME


class ToolMetricsDict(TypedDict):
    """Serialized metrics for a single tool."""

    call_count: int
    error_count: int
    error_rate: str
    total_duration_ms: float
    avg_duration_ms: float
    min_duration_ms: float
    max_duration_ms: float


class MetricsSummary(TypedDict):
    """Serialized summary of all tool metrics."""

    session_duration_seconds: float
    total_tool_calls: int
    total_errors: int
    overall_error_rate: str
    total_tool_time_ms: float
    tools_used: int
    by_tool: dict[str, ToolMetricsDict]


class ToolMetrics(BaseModel):
    """Metrics for a single tool."""

    call_count: int = 0
    error_count: int = 0
    total_duration_ms: float = 0.0
    min_duration_ms: float = float("inf")
    max_duration_ms: float = 0.0

    @classmethod
    def recorded(cls, data: ToolMetricsDict) -> "ToolMetrics":
        """One tool's counts read back from their serialized form, to fold with others."""
        return cls(
            call_count=data["call_count"],
            error_count=data["error_count"],
            total_duration_ms=data["total_duration_ms"],
            min_duration_ms=(
                data["min_duration_ms"] if data["call_count"] else float("inf")
            ),
            max_duration_ms=data["max_duration_ms"],
        )

    @property
    def avg_duration_ms(self) -> float:
        """Average duration per call in milliseconds."""
        if self.call_count == 0:
            return 0.0
        return self.total_duration_ms / self.call_count

    @property
    def error_rate(self) -> float:
        """Percentage of calls that resulted in errors."""
        if self.call_count == 0:
            return 0.0
        return self.error_count / self.call_count

    def record_call(self, duration_ms: float, is_error: bool = False) -> None:
        """Record a tool call."""
        self.call_count += 1
        self.total_duration_ms += duration_ms
        self.min_duration_ms = min(self.min_duration_ms, duration_ms)
        self.max_duration_ms = max(self.max_duration_ms, duration_ms)
        if is_error:
            self.error_count += 1

    def combined(self, other: "ToolMetrics") -> "ToolMetrics":
        """The calls of both, as though one collector had recorded them all."""
        return ToolMetrics(
            call_count=self.call_count + other.call_count,
            error_count=self.error_count + other.error_count,
            total_duration_ms=self.total_duration_ms + other.total_duration_ms,
            min_duration_ms=min(self.min_duration_ms, other.min_duration_ms),
            max_duration_ms=max(self.max_duration_ms, other.max_duration_ms),
        )

    def to_dict(self) -> ToolMetricsDict:
        """Convert to dictionary for serialization."""
        return ToolMetricsDict(
            call_count=self.call_count,
            error_count=self.error_count,
            error_rate=f"{self.error_rate:.1%}",
            total_duration_ms=round(self.total_duration_ms, 2),
            avg_duration_ms=round(self.avg_duration_ms, 2),
            min_duration_ms=(
                round(self.min_duration_ms, 2)
                if self.min_duration_ms != float("inf")
                else 0
            ),
            max_duration_ms=round(self.max_duration_ms, 2),
        )


def summary_of(
    metrics: Mapping[str, ToolMetrics], duration_seconds: float
) -> MetricsSummary:
    """The serialized summary of a set of tools' metrics over one stretch of time."""
    total_calls = sum(m.call_count for m in metrics.values())
    total_errors = sum(m.error_count for m in metrics.values())
    return MetricsSummary(
        session_duration_seconds=round(duration_seconds, 2),
        total_tool_calls=total_calls,
        total_errors=total_errors,
        overall_error_rate=f"{total_errors / max(1, total_calls):.1%}",
        total_tool_time_ms=round(sum(m.total_duration_ms for m in metrics.values()), 2),
        tools_used=len(metrics),
        by_tool={name: m.to_dict() for name, m in metrics.items()},
    )


def merged_summary(summaries: Sequence[MetricsSummary]) -> MetricsSummary:
    """Several summaries as one: calls, errors and time summed, the extremes kept.

    The duration is the longest of them, since the summaries being folded
    are of processes that ran side by side for one session rather than one
    after another.
    """
    tools: defaultdict[str, ToolMetrics] = defaultdict(ToolMetrics)
    for summary in summaries:
        for name, data in summary["by_tool"].items():
            tools[name] = tools[name].combined(ToolMetrics.recorded(data))
    duration = max(
        (summary["session_duration_seconds"] for summary in summaries), default=0.0
    )
    return summary_of(tools, duration)


class MetricsRetention(BaseModel, frozen=True):
    """How much of what a session's tool servers flushed its directory keeps.

    Every tool-serving process writes a snapshot of its own, and a checkout's
    launched sessions start several each time one opens, so without a bound
    the directory would grow by every server any session there ever started.
    A snapshot goes once it is older than ``max_age`` or falls outside the
    newest ``max_snapshots``, by when it was last written.
    """

    max_age: timedelta = timedelta(days=30)
    max_snapshots: int = Field(default=1000, ge=1)


class MetricsSnapshot(BaseModel, frozen=True):
    """What one tool-serving process had recorded when it last wrote."""

    server: str
    """The server the process serves, by the name its tools are addressed under."""

    member: str = ""
    """The roster identity of the session it serves, where one was known.

    The directory alone cannot say: a checkout's natively launched sessions
    share one, so this is what tells their servers apart.
    """

    process: int
    started: datetime
    updated: datetime
    summary: MetricsSummary


class MetricsSink(BaseModel, frozen=True):
    """Where one tool-serving process writes through what it records."""

    path: Path
    server: str
    member: str = ""
    process: int
    started: datetime

    def snapshot(self, summary: MetricsSummary) -> MetricsSnapshot:
        """This process's summary as of now, stamped with whose it is."""
        return MetricsSnapshot(
            server=self.server,
            member=self.member,
            process=self.process,
            started=self.started,
            updated=utc_now(),
            summary=summary,
        )


class MetricsCollector:
    """Collects metrics for all tools.

    When ``sink`` is set (tool-serving subprocesses), the summary is written
    through to that process's snapshot after every recorded call, so the
    parent process can read it even if the subprocess is killed.
    """

    def __init__(self) -> None:
        self.metrics: dict[str, ToolMetrics] = defaultdict(ToolMetrics)
        self.session_start: float = time.time()
        self.sink: MetricsSink | None = None

    def record(
        self, tool_name: str, duration_ms: float, is_error: bool = False
    ) -> None:
        """Record a tool call."""
        self.metrics[tool_name].record_call(duration_ms, is_error)
        if self.sink is not None:
            self.flush()

    def flush(self) -> None:
        """Replace this process's snapshot with the current summary, atomically.

        A kill mid-write must not corrupt the snapshot a reader parses, so it
        lands on a temp file in the same directory and is renamed onto the
        target (``Path.replace`` is atomic on POSIX).
        """
        if self.sink is None:
            return
        try:
            publish_atomic(self.sink.path, self.sink.snapshot(self.get_summary()))
        except OSError:
            logger.exception("Failed to flush metrics to %s", self.sink.path)

    def get_summary(self) -> MetricsSummary:
        """Get a summary of all metrics."""
        return summary_of(self.metrics, time.time() - self.session_start)

    def log_summary(self, level: int = logging.INFO) -> None:
        """Log a summary of all metrics."""
        summary = self.get_summary()
        logger.log(
            level,
            "Tool Metrics: %d calls, %d errors, %.1fs session",
            summary["total_tool_calls"],
            summary["total_errors"],
            summary["session_duration_seconds"],
        )

    def reset(self) -> None:
        """Reset all metrics."""
        self.metrics.clear()
        self.session_start = time.time()


# Global metrics collector
collector = MetricsCollector()


def tracked[**P, T](
    tool_name: str | None = None,
) -> Callable[
    [Callable[P, Coroutine[object, object, T]]],
    Callable[P, Coroutine[object, object, T]],
]:
    """Decorator recording call metrics for non-tool async functions.

    **What:** Records each call's duration and error status (raised
    exceptions, or a returned dict carrying ``is_error``) into the same
    collector that :func:`lup.tools.mcp.lup_tool` feeds automatically for MCP
    tools.

    **When:** Apply to async helpers that are not MCP tools — background
    jobs, API wrappers, sub-agent invocations — so their health shows up
    in :func:`get_metrics_summary` next to the tool metrics. Tools defined
    via ``lup_tool`` are already tracked; do not double-decorate them.

    **Why:** Session analysis reads a single metrics stream; work that
    happens outside tool handlers would otherwise be invisible to the
    feedback loop.

    Args:
        tool_name: Name to record metrics under. If None, uses function name.

    Example:
        @tracked("refresh_cache")
        async def refresh_cache(bucket: str) -> dict[str, int]:
            ...
    """

    def decorator(
        func: Callable[P, Coroutine[object, object, T]],
    ) -> Callable[P, Coroutine[object, object, T]]:
        name = tool_name or func.__name__

        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            start = time.perf_counter()
            is_error = False

            try:
                result = await func(*args, **kwargs)
                match result:
                    case {"is_error": flag} if flag:
                        is_error = True
                return result
            except BaseException:  # lup: ignore[except-baseexception] — mark + reraise
                is_error = True
                raise
            finally:
                duration_ms = (time.perf_counter() - start) * 1000
                collector.record(name, duration_ms, is_error)

        return wrapper

    return decorator


def prune_metrics(
    directory: Path, retention: MetricsRetention, now: datetime
) -> list[Path]:
    """Remove the snapshots ``retention`` no longer keeps, and say which.

    Judged by when each was last written, which every flush renews, so a
    process still serving keeps its snapshot however long ago it started.
    The processes of one session start together and prune the same directory
    at once, so a snapshot that vanishes under this one was taken by a
    sibling and is passed over.
    """

    def written(path: Path) -> datetime | None:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except FileNotFoundError:
            return None

    held = sorted(
        (
            (moment, path)
            for path in directory.glob("*.json")
            if (moment := written(path)) is not None
        ),
        reverse=True,
    )
    cutoff = now - retention.max_age
    expired = [
        path
        for rank, (moment, path) in enumerate(held)
        if rank >= retention.max_snapshots or moment < cutoff
    ]
    for path in expired:
        path.unlink(missing_ok=True)
    return expired


def open_metrics_sink(
    session_dir: Path,
    server: str,
    member: str = "",
    retention: MetricsRetention = MetricsRetention(),
) -> MetricsSink:
    """Name this process's snapshot under a session, pruning the directory first.

    A process starting is what keeps the directory bounded: it is the one
    moment a writer is there anyway, and the processes of one session start
    together, so the prune runs as often as the directory can grow. The name
    carries the server, the moment and the process, so no two processes ever
    write one file.
    """
    directory = metrics_directory(session_dir)
    started = utc_now()
    prune_metrics(directory, retention, started)
    process = os.getpid()
    return MetricsSink(
        path=directory / f"{server}.{started:%Y%m%d_%H%M%S_%f}.{process}.json",
        server=server,
        member=member,
        process=process,
        started=started,
    )


def configure_metrics(sink: MetricsSink | None) -> None:
    """Write this process's metrics through to a snapshot, or stop writing them.

    Call in a tool-serving subprocess with :func:`open_metrics_sink`, so the
    process that opened the session — and ``lup-devtools tools metrics`` —
    can read what its tools did.
    """
    collector.sink = sink


def read_metrics_snapshots(
    session_dir: Path, since: datetime | None = None
) -> list[MetricsSnapshot]:
    """Every snapshot a session's tool-serving processes wrote, oldest first.

    ``since`` keeps the processes whose last call came at or after it. Each
    is counted whole, from its own start, because a snapshot is cumulative
    and says nothing of when within its life a call was made. A snapshot
    that will not read is logged and passed over: one bad file must not cost
    a reader the rest.
    """

    def read(path: Path) -> MetricsSnapshot | None:
        try:
            return MetricsSnapshot.model_validate_json(path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValidationError):
            logger.exception("Flushed metrics at %s are unreadable", path)
            return None

    found = [
        snapshot
        for path in metrics_directory(session_dir).glob("*.json")
        if (snapshot := read(path)) is not None
        and (since is None or snapshot.updated >= since)
    ]
    return sorted(found, key=lambda snapshot: snapshot.started)


def read_metrics_summary(session_dir: Path) -> MetricsSummary:
    """Every tool call one session recorded since this process's collector was reset.

    This process's own calls, folded with what the tool-serving processes it
    started for the session wrote under the session's directory since then.
    A backend that cannot host tools in process records them there and
    nowhere else, so this is what keeps a session's metrics the same
    whichever backend ran it.
    """
    since = datetime.fromtimestamp(collector.session_start, UTC)
    flushed = [
        snapshot.summary for snapshot in read_metrics_snapshots(session_dir, since)
    ]
    return merged_summary([collector.get_summary(), *flushed])


def log_metrics_summary() -> None:
    """Log a summary of all tool metrics."""
    collector.log_summary()


def get_metrics_summary() -> MetricsSummary:
    """Get a summary of all tool metrics."""
    return collector.get_summary()


def reset_metrics() -> None:
    """Reset all tool metrics."""
    collector.reset()
