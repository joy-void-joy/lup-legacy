"""One reading of a run, taken from its directory and nothing else.

A run launched detached, from another session, or before this shell existed
is fully observable without being touched: count the units that landed
against the units the manifest scheduled, tally what they concluded, and
re-read the last line the runner wrote. Nothing here consults the process
table, because under a sandbox ``/proc`` is PID-isolated and a healthy run is
indistinguishable there from a dead one — a liveness answer that asks the
process table is no answer at all on the host a long job most often runs on.

What a reading will not do is repeat the runner's own estimate of the time
left. A progress bar smooths its rate over the last few landings, and units
land in bursts — one per worker as a batch of budgets expires — so that
number said twenty-nine seconds about thirty-six two-hour cells. The estimate
here divides everything landed so far by the whole elapsed time, which is the
only estimate bursty landings support.

Inside a unit the reading is the other way round. A unit that reports its own
count says only what it has done, and the pace and the time left are taken
here from two readings and the clock between them — which is honest at this
grain, because one unit's work advances steadily where a run's landings come
in bursts. A unit that reports nothing falls back to the last line it printed,
so a shell step is readable without cooperating and a callable one that says
nothing shows nothing rather than a placeholder.
"""

import json
import time
from collections import Counter, defaultdict
from datetime import timedelta
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from lup.channels.models import utc_now
from lup.runs.directory import RunDirectory, RunningUnit, stdout_in
from lup.runs.models import (
    RunManifest,
    RunSummary,
    StepRecord,
    UnitProgress,
    UnitStatus,
)
from lup.types import JsonValue


class StepState(StrEnum):
    """Where one step of a run stands."""

    BLOCKED = "blocked"
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class StatusCount(BaseModel, frozen=True):
    """How many landed units are tallied under one word."""

    status: str
    count: int

    def render(self) -> str:
        """The tally as ``status=count``."""
        return f"{self.status}={self.count}"


def render_value(value: JsonValue) -> str:
    """One detail value as the unit sent it, whole.

    Text passes through, everything else takes its JSON spelling — so a float
    reads ``0.0123`` rather than in exponent notation, a flag reads ``true``,
    and a nested value reads as compact JSON rather than as a Python repr no
    other language wrote. Nothing is cut: a unit that reports a large value
    reports a large value, and a rendering that kept a prefix would look
    complete while being wrong.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def render_detail(detail: dict[str, JsonValue]) -> str:
    """A unit's own vocabulary, in the order it put it, filtered by nobody."""
    return " ".join(f"{key}={render_value(value)}" for key, value in detail.items())


def render_count(progress: UnitProgress) -> str:
    """How far a unit is: against its budget, or a bare count when it has none."""
    if progress.total is None:
        return str(progress.done)
    return f"{progress.done}/{progress.total}"


class ProgressSample(BaseModel, frozen=True):
    """One reading of a unit's own count, and the clock it was taken on."""

    done: int
    at: float
    """A monotonic stamp, because this is only ever subtracted from another."""


class UnitRate(BaseModel, frozen=True):
    """How fast one unit is going, derived from two readings that share a clock.

    Derived here rather than reported by the unit for three reasons that all
    point the same way: the two readings share one clock, this module already
    refuses to repeat a writer's own estimate, and a single unit's own work is
    not bursty the way a run's landings are — a training loop or a solver
    sweep advances at something like a steady pace, so the estimate this
    supports is honest in a way the run-level one deliberately is not.
    """

    per_second: float
    remaining_seconds: float | None = None

    def render(self) -> str:
        """The pace, and the time left when the unit declared a budget."""
        pace = (
            f"{self.per_second:.1f} steps/s"
            if self.per_second >= 1.0
            else f"{self.per_second * 60:.0f} steps/min"
        )
        if self.remaining_seconds is None:
            return pace
        return f"{pace} · eta {render_span(self.remaining_seconds)}"


def remaining_at(progress: UnitProgress, per_second: float) -> float | None:
    """How long this unit has left at the pace it is going, given a budget."""
    if progress.total is None or per_second <= 0:
        return None
    return max(0.0, (progress.total - progress.done) / per_second)


class RateTracker(BaseModel):
    """The previous reading of each running unit, which is what a rate needs.

    Whoever is watching in a loop holds this, rather than the reading holding
    it: a rate is two readings and the time between them, and a reading taken
    once — ``--once``, a report — has one. That reader says nothing about
    pace, which is the honest thing for it to say.
    """

    seen: dict[str, ProgressSample] = {}

    def rate(self, unit: RunningUnit, now: float) -> UnitRate | None:
        """This unit's pace since the last reading, or None on the first one."""
        record = unit.progress
        if record is None:
            return None
        previous = self.seen.get(unit.slug)
        self.seen[unit.slug] = ProgressSample(done=record.done, at=now)
        if previous is None or record.done <= previous.done or now <= previous.at:
            return None
        per_second = (record.done - previous.done) / (now - previous.at)
        return UnitRate(
            per_second=per_second, remaining_seconds=remaining_at(record, per_second)
        )

    def rates(self, running: list[RunningUnit], now: float) -> dict[str, UnitRate]:
        """Every running unit's pace, forgetting the units that have landed.

        Forgetting matters because a sweep of thousands would otherwise keep a
        sample per unit it ever saw, for a rate nobody can ask about again.
        """
        paced = {unit.slug: self.rate(unit, now) for unit in running}
        self.seen = {
            slug: sample for slug, sample in self.seen.items() if slug in paced
        }
        return {slug: pace for slug, pace in paced.items() if pace is not None}


def unit_postfix(progress: UnitProgress, rate: UnitRate | None) -> str:
    """What a running unit says beyond its own count: phase, detail, pace."""
    return " · ".join(
        part
        for part in [
            progress.phase,
            render_detail(progress.detail),
            rate.render() if rate is not None else "",
        ]
        if part
    )


def render_unit(unit: RunningUnit, rate: UnitRate | None = None) -> str:
    """One running unit as a line, for a reader who sees lines and not a screen.

    A unit that reports carries its count; one that only prints carries its
    last line; one that does neither carries how long it has been going, which
    is all anybody can say about it.
    """
    if unit.progress is None:
        said = unit.last_line or f"running for {render_span(unit.age_seconds)}"
        return f"{unit.slug}: {said}"
    postfix = unit_postfix(unit.progress, rate)
    counted = render_count(unit.progress)
    return f"{unit.slug}: {counted} {postfix}".rstrip()


def observed(directory: RunDirectory, unit: RunningUnit) -> RunningUnit:
    """One running unit carrying whatever it has said about itself.

    Its own record when it publishes one, and otherwise the last line it
    printed — which for a shell unit is already an account of what it is
    doing. A callable step that neither reports nor prints carries nothing,
    which is honest: a placeholder there would read as a unit saying
    something.
    """
    step, item = unit.attempt.step, unit.attempt.item
    record = directory.read_progress_record(step, item)
    if record is not None:
        return unit.model_copy(update={"progress": record})
    return unit.model_copy(
        update={
            "last_line": latest_heartbeat(stdout_in(directory.workspace(step, item)))
        }
    )


class StepProgress(BaseModel, frozen=True):
    """Where one step stands, and how much of it has landed."""

    id: str
    state: StepState
    landed: int
    width: int
    failed: int

    def render(self) -> str:
        """The step as one reader-facing phrase."""
        return f"{self.id} {self.state.value} {self.landed}/{self.width}"


class RunProgress(BaseModel, frozen=True):
    """One reading: scheduled, landed, how they ended, and what is going on."""

    directory: Path
    name: str
    total: int
    landed: int
    failed: int
    statuses: list[StatusCount] = []
    running: list[RunningUnit] = []
    steps: list[StepProgress] = []
    unreadable: list[Path] = []
    heartbeat: str = ""
    elapsed_seconds: float | None = None
    eta_seconds: float | None = None
    quiet_for: float | None = None
    summary: RunSummary | None = None

    @property
    def complete(self) -> bool:
        """Whether every scheduled unit has landed."""
        return self.total > 0 and self.landed >= self.total

    @property
    def finished(self) -> bool:
        """Whether the runtime recorded an ending, however the run ended.

        A pipeline whose second stage failed never lands its fourth, so
        waiting for the unit count to reach the total waits forever. The
        summary is what says the run is over.
        """
        return self.summary is not None

    @property
    def oldest_running(self) -> RunningUnit | None:
        """The unit claimed longest, which is the one worth looking at."""
        return self.running[0] if self.running else None

    @property
    def abandoned(self) -> list[RunningUnit]:
        """The claimed units whose lease has lapsed, so nobody is working them.

        Counted apart from the running ones because they mean the opposite: a
        directory holding four claims and no live lease is a run whose process
        is gone, which reads as "4 running" to anybody counting claims alone.
        """
        return [unit for unit in self.running if unit.stale]

    def render_clock(self) -> str:
        """Elapsed against the time left, in the shape a progress bar reads in.

        ``[0:01:23<0:12:30]`` is where a reader's eye already goes for those
        two numbers, so they are spelled that way rather than as two tallies.
        The numbers stay the monitor's own: a bar's ``elapsed`` counts from
        when the watcher attached, which is wrong for a run started hours
        before it, and its ``remaining`` is the smoothed estimate this module
        exists to refuse. ``?`` where nothing has landed yet, because a run
        that cannot be estimated should say so rather than show a zero.
        """
        if self.elapsed_seconds is None and self.eta_seconds is None:
            return ""
        elapsed = (
            render_span(self.elapsed_seconds)
            if self.elapsed_seconds is not None
            else "?"
        )
        left = render_span(self.eta_seconds) if self.eta_seconds is not None else "?"
        return f"[{elapsed}<{left}]"

    def postfix(self) -> str:
        """The clock, then the status tally, then what the monitor knows itself."""
        clock = self.render_clock()
        parts = [
            *([clock] if clock else []),
            *(
                entry.render()
                for entry in sorted(self.statuses, key=lambda e: e.status)
            ),
        ]
        held = len(self.running) - len(self.abandoned)
        if held:
            parts.append(f"running={held}")
        if self.abandoned:
            parts.append(f"abandoned={len(self.abandoned)}")
        if self.unreadable:
            parts.append(f"unreadable={len(self.unreadable)}")
        return " ".join(parts)

    def describe_activity(self) -> str:
        """What the run is doing now, in the monitor's own honest terms.

        Never the runner's own estimate. When units are running, their count
        and the oldest one's age say the useful thing that estimate was
        crowding out; when none are, the runner's last line is the best
        available account of what it is doing.
        """
        if self.summary is not None:
            return describe_summary(self.summary)
        if self.abandoned and len(self.abandoned) == len(self.running):
            # Every claim lapsed and no summary was written: the runner was
            # killed rather than stopped. Said outright, because the alternative
            # reading — several units still working — is what a reader would
            # otherwise take from the same directory, and it sends them waiting
            # on a process that is gone.
            oldest = self.abandoned[0]
            return (
                f"no runner holds this: {len(self.abandoned)} claim(s) abandoned, "
                f"oldest {oldest.slug}, none renewed for "
                f"{render_span(oldest.since_renewed_seconds)}. Resume it to "
                "re-run them"
            )
        if self.oldest_running is not None:
            span = render_span(self.oldest_running.age_seconds)
            return (
                f"{len(self.running)} running; oldest "
                f"{self.oldest_running.slug} for {span}"
            )
        if self.complete:
            return "every scheduled unit has landed"
        return self.heartbeat or "(no unit running yet)"

    def stalled(self, quiet_limit: float) -> bool:
        """Whether nothing has happened here for longer than a caller accepts.

        A run whose process was killed leaves no summary and no claim it will
        ever release, so silence is the only evidence that nobody is driving
        it. How long counts as silence belongs to the caller: a pipeline of
        shell steps is quiet for seconds, a solver sweep for hours.
        """
        return (
            not self.finished
            and self.quiet_for is not None
            and self.quiet_for > quiet_limit
        )


def units(count: int) -> str:
    """A unit count that still reads as English when there is one of them."""
    return f"{count} unit" if count == 1 else f"{count} units"


def describe_summary(summary: RunSummary) -> str:
    """How a finished run reads to whoever comes back to it."""
    if summary.interrupted:
        return f"run interrupted after {units(summary.landed)}"
    if summary.failed or summary.skipped or summary.unlanded:
        skipped = ", ".join(step.id for step in summary.skipped)
        unlanded = ", ".join(summary.unlanded)
        tail = "".join(
            [
                f"; {units(len(summary.unlanded))} could not be landed: {unlanded}"
                if unlanded
                else "",
                f"; skipped {skipped}" if skipped else "",
            ]
        )
        return f"run failed: {units(summary.failed)} failed{tail}"
    return f"run complete: {units(summary.landed)} landed"


def render_span(seconds: float) -> str:
    """A duration as hours, minutes and seconds, whole seconds only."""
    return str(timedelta(seconds=int(seconds)))


def remaining_estimate(total: int, landed: int, elapsed: float | None) -> float | None:
    """The global-average estimate of the time left, the one bursty landings need."""
    if elapsed is None or landed <= 0 or total <= 0:
        return None
    if landed >= total:
        return 0.0
    return elapsed * (total - landed) / landed


def latest_heartbeat(log: Path | None, tail_bytes: int = 16 * 1024) -> str:
    """The last progress line the runner wrote, as it would stand on a terminal.

    A bar rewrites its line with carriage returns rather than newlines;
    ``splitlines`` breaks on both, so the newest heartbeat is the last
    non-empty segment of the log's tail. The tail is bounded because the
    newest line is at the end whatever the log's total size — the rest of the
    file stays on disk, and this is the only reader that does not want it.
    """
    if log is None or not log.is_file():
        return ""
    with log.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - tail_bytes))
        tail = handle.read().decode("utf-8", errors="replace")
    latest = [segment.strip() for segment in tail.splitlines() if segment.strip()]
    return latest[-1] if latest else ""


def default_log(directory: RunDirectory) -> Path | None:
    """The runner's log when it sits inside the run directory."""
    return directory.log_path if directory.log_path.is_file() else None


def silent_for(directory: RunDirectory, log: Path | None) -> float | None:
    """How long since anything in this directory changed.

    Taken from the newest mtime among the manifest, the landed units and the
    log, because those are the three things a working run touches. None when
    the directory holds nothing yet.
    """
    written = [directory.manifest_path, *directory.units_root.glob("*/*.json")]
    candidates = [
        path for path in [*written, log] if path is not None and path.is_file()
    ]
    if not candidates:
        return None
    newest = max(path.stat().st_mtime for path in candidates)
    return max(0.0, time.time() - newest)


def settled_state(statuses: list[UnitStatus], width: int, claimed: int) -> StepState:
    """Which state a step stands in judging only by its own units."""
    if UnitStatus.FAILED in statuses:
        return StepState.FAILED
    if len(statuses) >= width:
        return StepState.DONE
    if claimed or statuses:
        return StepState.RUNNING
    return StepState.PENDING


def step_progress(
    manifest: RunManifest,
    landed: dict[str, list[UnitStatus]],
    running: list[RunningUnit],
) -> list[StepProgress]:
    """Where each declared step stands, given what landed and what is claimed.

    Two passes, because blocked is not a fact about a step's own units: a step
    is blocked when something it rests on has not finished, which can only be
    read once every step's own state is known.
    """
    claimed = Counter(unit.attempt.step for unit in running)
    own = {
        record.id: StepProgress(
            id=record.id,
            state=settled_state(
                landed.get(record.id, []), record.width, claimed[record.id]
            ),
            landed=len(landed.get(record.id, [])),
            width=record.width,
            failed=sum(
                1 for status in landed.get(record.id, []) if status is UnitStatus.FAILED
            ),
        )
        for record in manifest.steps
    }
    return [blocked_or(record, own) for record in manifest.steps]


def blocked_or(record: StepRecord, own: dict[str, StepProgress]) -> StepProgress:
    """A pending step whose dependencies have not all finished reads as blocked."""
    progress = own[record.id]
    waiting = any(
        dependency not in own or own[dependency].state is not StepState.DONE
        for dependency in record.dependencies
    )
    if progress.state is not StepState.PENDING or not waiting:
        return progress
    return StepProgress(
        id=progress.id,
        state=StepState.BLOCKED,
        landed=progress.landed,
        width=progress.width,
        failed=progress.failed,
    )


def elapsed_span(
    manifest: RunManifest | None, summary: RunSummary | None
) -> float | None:
    """How long this run has been going, or how long it went once it is over.

    A finished run's elapsed stops at its ending rather than following the
    clock. Otherwise a directory read a day later reports a day, which is the
    reading of a run still working — and the one number a person uses to
    decide whether to wait would be the one that never settles.
    """
    if manifest is None:
        return None
    ended = summary.finished_at if summary is not None else utc_now()
    return max(0.0, (ended - manifest.started_at).total_seconds())


def read_progress(directory: RunDirectory, log: Path | None = None) -> RunProgress:
    """One reading of the run at ``directory``."""
    manifest = directory.read_manifest()
    reading = directory.read()
    running = [observed(directory, unit) for unit in directory.running()]
    resolved_log = log if log is not None else default_log(directory)
    tally = Counter(result.label for result in reading.results)
    landed_by_step: defaultdict[str, list[UnitStatus]] = defaultdict(list)
    for result in reading.results:
        landed_by_step[result.step].append(result.status)
    total = manifest.total_units if manifest is not None else len(reading.results)
    summary = directory.read_summary()
    elapsed = elapsed_span(manifest, summary)
    return RunProgress(
        directory=directory.root,
        name=manifest.name if manifest is not None else directory.root.name,
        total=total,
        landed=len(reading.results),
        failed=sum(
            1 for result in reading.results if result.status is UnitStatus.FAILED
        ),
        statuses=[
            StatusCount(status=status, count=count) for status, count in tally.items()
        ],
        running=running,
        steps=(
            step_progress(manifest, landed_by_step, running)
            if manifest is not None
            else []
        ),
        unreadable=reading.unreadable,
        heartbeat=latest_heartbeat(resolved_log),
        elapsed_seconds=elapsed,
        eta_seconds=(
            0.0
            if summary is not None
            else remaining_estimate(total, len(reading.results), elapsed)
        ),
        quiet_for=silent_for(directory, resolved_log),
        summary=summary,
    )
