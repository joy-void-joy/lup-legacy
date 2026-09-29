# lup: ignore[own-model-dispatch]
# Timing is a projection of the journal vocabulary; session and run events
# do not depend on this reporting layer or carry its aggregation policy.
"""Read-only timing evidence derived from a resolver's ordered journal."""

from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, computed_field

from lup.channels.stream import Stream
from lup.coordination.refs import ActorRef
from lup.resolver.record import (
    ENTRY_ADAPTER,
    JOURNAL_FILE,
    JournalEntry,
    RunFailedEvent,
)
from lup.sessions.events import TurnCompletedEvent, TurnIdentifiers, TurnStartedEvent


class TurnKey(BaseModel, frozen=True):
    """Native turn identity qualified by the actor whose stream recorded it."""

    actor: ActorRef
    identifiers: TurnIdentifiers


class ActorCost(BaseModel):
    """Completed turn durations and incomplete evidence for one actor kind."""

    kind: str
    completed: int = 0
    unfinished: int = 0
    interrupted: int = 0
    total_seconds: float = 0
    maximum_seconds: float = 0
    peak_concurrency: int = 0
    """Peak overlap among turns with both a recorded start and completion."""

    @computed_field
    @property
    def mean_seconds(self) -> float | None:
        return self.total_seconds / self.completed if self.completed else None


class IdleGap(BaseModel, frozen=True):
    """An interval covered by neither completed nor unresolved turn evidence."""

    started_at: datetime
    ended_at: datetime
    preceding: JournalEntry

    @computed_field
    @property
    def seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()


class FailureCount(BaseModel, frozen=True):
    """One exact recorded failure reason, with no heuristic classification."""

    reason: str
    count: int


class TurnInterval(BaseModel, frozen=True):
    """A completed turn or the bounds within which activity remains unresolved."""

    key: TurnKey
    started: JournalEntry
    ended: JournalEntry
    outcome: Literal["completed", "interrupted", "unfinished", "missing_start"]


class TimingBoundary(BaseModel, frozen=True):
    """One interval endpoint, or an endpoint of the journal's observed window."""

    entry: JournalEntry
    interval: TurnInterval | None = None
    delta: Literal[-1, 0, 1] = 0


class ActivityReport(BaseModel):
    """Disjoint wall-time categories measured across the interval boundaries."""

    active_seconds: float = 0
    idle_seconds: float = 0
    uncertain_seconds: float = 0
    peak_concurrency: int = 0
    actor_peaks: Counter[str] = Counter()
    idle_gaps: list[IdleGap] = []


def activity_report(
    intervals: list[TurnInterval],
    first: JournalEntry,
    last: JournalEntry,
    gap: timedelta,
) -> ActivityReport:
    """Measure completed activity, then uncertainty, then uncovered idle time."""
    boundaries = [
        TimingBoundary(entry=first),
        TimingBoundary(entry=last),
        *[
            boundary
            for interval in intervals
            for boundary in (
                TimingBoundary(entry=interval.started, interval=interval, delta=1),
                TimingBoundary(entry=interval.ended, interval=interval, delta=-1),
            )
        ],
    ]
    report = ActivityReport()
    active: Counter[str] = Counter()
    uncertain = 0
    previous = first
    idle_start: JournalEntry | None = first
    for boundary in sorted(
        boundaries, key=lambda point: (point.entry.at, point.entry.seq)
    ):
        seconds = (boundary.entry.at - previous.at).total_seconds()
        match bool(active.total()), bool(uncertain):
            case True, _:
                report.active_seconds += seconds
            case False, True:
                report.uncertain_seconds += seconds
            case False, False:
                report.idle_seconds += seconds
        if boundary.interval is not None:
            if boundary.interval.outcome == "completed":
                kind = boundary.interval.key.actor.kind
                active[kind] += boundary.delta
                report.peak_concurrency = max(report.peak_concurrency, active.total())
                report.actor_peaks[kind] = max(
                    report.actor_peaks.get(kind, 0), active[kind]
                )
            else:
                uncertain += boundary.delta
        if active.total() or uncertain:
            if idle_start is not None and boundary.entry.at - idle_start.at > gap:
                report.idle_gaps.append(
                    IdleGap(
                        started_at=idle_start.at,
                        ended_at=boundary.entry.at,
                        preceding=idle_start,
                    )
                )
            idle_start = None
        if not (active.total() or uncertain) and idle_start is None:
            idle_start = boundary.entry
        previous = boundary.entry
    if idle_start is not None and last.at - idle_start.at > gap:
        report.idle_gaps.append(
            IdleGap(started_at=idle_start.at, ended_at=last.at, preceding=idle_start)
        )
    return report


class CostReport(BaseModel, frozen=True):
    """The observed journal window, distinguishing wall time from actor time."""

    started_at: datetime | None = None
    ended_at: datetime | None = None
    wall_seconds: float = 0
    active_seconds: float = 0
    idle_seconds: float = 0
    uncertain_seconds: float = 0
    peak_concurrency: int = 0
    """Peak overlap among completed turns; unresolved intervals supply no concurrency proof."""
    actors: list[ActorCost] = []
    failures: list[FailureCount] = []
    idle_gaps: list[IdleGap] = []
    unresolved_intervals: list[TurnInterval] = []
    gap_threshold_seconds: float = Field(default=600, ge=0)
    anomalies: list[str] = []


def cost_report(
    entries: Iterable[JournalEntry], gap: timedelta = timedelta(minutes=10)
) -> CostReport:
    """Pair recorded turns, then measure their overlapping intervals once.

    The window ends at the last recorded timestamp, never at the reader's
    clock. Only completed turns establish active time and concurrency. Other
    intervals remain uncertain up to a recorded failure or the window's end;
    a crash followed by a resume does not prove activity through the downtime.
    """
    if gap < timedelta():
        raise ValueError("the idle-gap threshold must be nonnegative")
    active: dict[
        TurnKey, JournalEntry
    ] = {}  # lup: ignore[empty-collection] — event fold
    actors: dict[str, ActorCost] = {}  # lup: ignore[empty-collection] — event fold
    failures: Counter[str] = Counter()
    intervals: list[TurnInterval] = []  # lup: ignore[empty-collection] — event fold
    anomalies: list[str] = []  # lup: ignore[empty-collection] — event fold
    first: JournalEntry | None = None
    previous: JournalEntry | None = None
    episode_start: JournalEntry | None = None
    for entry in entries:
        if previous is not None:
            if entry.at < previous.at:
                raise ValueError(
                    f"journal timestamp moved backward at sequence {entry.seq}"
                )
        else:
            first = entry
            episode_start = entry
        match entry.event:
            case TurnStartedEvent(identifiers=identifiers):
                key = TurnKey(actor=entry.actor, identifiers=identifiers)
                actor = actors.setdefault(
                    entry.actor.kind, ActorCost(kind=entry.actor.kind)
                )
                if key in active:
                    anomalies.append(f"duplicate turn start at sequence {entry.seq}")
                else:
                    active[key] = entry
            case TurnCompletedEvent(identifiers=identifiers):
                key = TurnKey(actor=entry.actor, identifiers=identifiers)
                started = active.pop(key, None)
                if started is None:
                    anomalies.append(
                        f"turn completion without a start at sequence {entry.seq}"
                    )
                    if episode_start is not None:
                        intervals.append(
                            TurnInterval(
                                key=key,
                                started=episode_start,
                                ended=entry,
                                outcome="missing_start",
                            )
                        )
                else:
                    actor = actors[entry.actor.kind]
                    seconds = (entry.at - started.at).total_seconds()
                    actor.completed += 1
                    actor.total_seconds += seconds
                    actor.maximum_seconds = max(actor.maximum_seconds, seconds)
                    intervals.append(
                        TurnInterval(
                            key=key, started=started, ended=entry, outcome="completed"
                        )
                    )
            case RunFailedEvent(reason=reason):
                failures[reason] += 1
                for key, started in active.items():
                    actors[key.actor.kind].interrupted += 1
                    intervals.append(
                        TurnInterval(
                            key=key, started=started, ended=entry, outcome="interrupted"
                        )
                    )
                active.clear()
                episode_start = entry
        previous = entry
    if first is None or previous is None:
        return CostReport(gap_threshold_seconds=gap.total_seconds())
    for key, started in active.items():
        actors[key.actor.kind].unfinished += 1
        intervals.append(
            TurnInterval(key=key, started=started, ended=previous, outcome="unfinished")
        )
    activity = activity_report(intervals, first, previous, gap)
    for actor in actors.values():
        actor.peak_concurrency = activity.actor_peaks.get(actor.kind, 0)
    wall = (previous.at - first.at).total_seconds()
    return CostReport(
        started_at=first.at,
        ended_at=previous.at,
        wall_seconds=wall,
        active_seconds=activity.active_seconds,
        idle_seconds=activity.idle_seconds,
        uncertain_seconds=activity.uncertain_seconds,
        peak_concurrency=activity.peak_concurrency,
        actors=[actors[kind] for kind in sorted(actors)],
        failures=[
            FailureCount(reason=reason, count=count)
            for reason, count in sorted(failures.items())
        ],
        idle_gaps=activity.idle_gaps,
        unresolved_intervals=[
            interval for interval in intervals if interval.outcome != "completed"
        ],
        gap_threshold_seconds=gap.total_seconds(),
        anomalies=anomalies,
    )


def read_cost(root: Path, gap: timedelta = timedelta(minutes=10)) -> CostReport:
    """Read the existing journal without opening a writer or taking the run lock."""
    path = root / JOURNAL_FILE
    if not path.is_file():
        raise FileNotFoundError(f"no resolver journal at {path}")
    return cost_report(Stream(path, ENTRY_ADAPTER).read_all(), gap)
