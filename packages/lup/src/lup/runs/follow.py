"""Two ways to watch a run, because two kinds of watcher want opposite things.

A person at a second terminal wants one screen that stays put and always
shows now: a bar that advances, a step line that changes, an activity line
that says what is being worked on. Redrawing in place is exactly right, and a
transcript of every redraw would be unreadable.

An agent wants the opposite. It cannot see a screen, and a bar rewritten in
place is invisible to it; what reaches it is a line at a time, so a watch
that emits one line per thing that happened — and stops when the run is over
— turns a long job into events it is woken for instead of something it has to
keep asking about. Silence is the failure mode there, so this face reports
what it means when nothing has happened for a long time, and reports a
failure as loudly as a landing.
"""

import time
from collections.abc import Iterator
from itertools import count
from pathlib import Path

from pydantic import BaseModel
from tqdm import tqdm

from lup.runs.directory import RunDirectory, RunningUnit
from lup.runs.models import UnitProgress, UnitResult
from lup.runs.progress import (
    OVERDUE_FACTOR,
    RateTracker,
    RunProgress,
    StepState,
    UnitRate,
    default_log,
    describe_summary,
    read_progress,
    render_count,
    render_overdue,
    render_span,
    unit_postfix,
)

UNIT_LINES = 12
"""How many running units get a line of their own before the rest are counted.

A sweep of thousands cannot have a line each, and the units worth looking at
are the ones running longest, which is the order a reading already arrives in.
The rest are counted on the activity line rather than dropped silently, so a
screen never implies it is showing everything.
"""


class UnitLine(BaseModel, arbitrary_types_allowed=True):
    """One running unit's line on the screen, and which row it holds."""

    bar: tqdm
    position: int


class UnitLines(BaseModel, arbitrary_types_allowed=True):
    """The per-unit lines a screen is showing, and the rows they occupy.

    Rows are recycled rather than grown: a unit that lands frees its row for
    whichever unit takes its place, so a long sweep redraws in place instead
    of walking off the bottom of the terminal.
    """

    below: int
    """The first row free under the run's own lines."""

    limit: int

    lines: dict[str, UnitLine] = {}

    def take(self) -> int:
        """The lowest free row, so a landed unit's row is the next one used."""
        held = {line.position for line in self.lines.values()}
        return next(row for row in count(self.below) if row not in held)

    def retire(self, slug: str) -> None:
        """Give one unit's row back, clearing what it was showing."""
        self.lines.pop(slug).bar.close()

    def open(self, slug: str) -> UnitLine:
        """A row for a unit that has none, drawn where the last one was given up."""
        row = self.take()
        line = UnitLine(
            bar=tqdm(position=row, dynamic_ncols=True, smoothing=0, leave=False),
            position=row,
        )
        self.lines[slug] = line
        return line

    def draw(self, unit: RunningUnit, rate: UnitRate | None) -> None:
        """Redraw one unit's line: a bar when it declared a budget, else a count."""
        record = unit.progress
        if record is None:
            return
        line = self.lines.get(unit.slug) or self.open(unit.slug)
        line.bar.bar_format = (
            "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}{postfix}"
            if record.total is not None
            else "{desc}: {n_fmt}{postfix}"
        )
        line.bar.total = record.total
        line.bar.n = record.done
        line.bar.set_description_str(unit.attempt.item)
        line.bar.set_postfix_str(unit_postfix(record, rate))
        line.bar.refresh()

    def refresh(self, units: list[RunningUnit], rates: dict[str, UnitRate]) -> int:
        """Draw a line per reporting unit, and say how many did not get one."""
        reporting = [unit for unit in units if unit.progress is not None]
        shown = reporting[: self.limit]
        keep = {unit.slug for unit in shown}
        for slug in [slug for slug in self.lines if slug not in keep]:
            self.retire(slug)
        for unit in shown:
            self.draw(unit, rates.get(unit.slug))
        return len(reporting) - len(shown)

    def close(self) -> None:
        """Give every row back, deepest first so the cursor ends where it began."""
        for slug in sorted(self.lines, key=lambda slug: -self.lines[slug].position):
            self.retire(slug)


def render_steps(progress: RunProgress) -> str:
    """Every step and where it stands, on one line.

    Empty for a run with a single step, whose bar already says everything a
    step line would repeat.
    """
    if len(progress.steps) <= 1:
        return ""
    return " · ".join(step.render() for step in progress.steps)


def render_landing(result: UnitResult) -> str:
    """One landed unit as a line whoever is following should be able to act on."""
    span = render_span(result.elapsed_seconds)
    head = f"{result.status.value} {result.slug} in {span}"
    if result.outcome:
        head = f"{head} ({result.outcome})"
    if result.error:
        return f"{head}: {result.error}"
    return head


def follow(
    directory: RunDirectory,
    log: Path | None = None,
    interval: float = 2.0,
    once: bool = False,
    unit_lines: int = UNIT_LINES,
) -> RunProgress:
    """Render the run in place until it ends; return the last reading.

    Three lines: the units landed against the units scheduled with the
    monitor's own elapsed and time-left, the steps and where each stands, and
    what the run is doing right now. Then one line per running unit that
    reports its own progress, which is the difference between a unit forty
    percent through and one spinning at zero. None of them reproduces the
    runner's own smoothed estimate.
    """
    resolved_log = log if log is not None else default_log(directory)
    reading = read_progress(directory, resolved_log)
    units_bar = tqdm(
        total=max(reading.total, reading.landed),
        initial=reading.landed,
        desc=f"{reading.name}: units",
        unit="unit",
        position=0,
        dynamic_ncols=True,
        smoothing=0,
        bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}{postfix}",
    )
    steps_line = tqdm(total=0, bar_format="{desc}", position=1, dynamic_ncols=True)
    activity_line = tqdm(total=0, bar_format="{desc}", position=2, dynamic_ncols=True)
    own = [units_bar, steps_line, activity_line]
    lines = UnitLines(below=len(own), limit=unit_lines)
    tracker = RateTracker()
    try:
        while True:
            units_bar.total = max(reading.total, reading.landed)
            units_bar.n = reading.landed
            units_bar.set_postfix_str(reading.postfix())
            units_bar.refresh()
            steps_line.set_description_str(render_steps(reading))
            hidden = lines.refresh(
                reading.running, tracker.rates(reading.running, time.monotonic())
            )
            activity_line.set_description_str(
                describe_screen(reading.describe_activity(), hidden)
            )
            if once or reading.finished:
                break
            time.sleep(interval)
            reading = read_progress(directory, resolved_log)
    finally:
        lines.close()
        activity_line.close()
        steps_line.close()
        units_bar.close()
    return reading


def describe_screen(activity: str, hidden: int) -> str:
    """What the run is doing, and what the screen had no room to show.

    Counted rather than dropped: a screen showing twelve of forty reporting
    units and saying nothing about it is a screen that reads as forty.
    """
    if not hidden:
        return activity
    return f"{activity} · {hidden} more reporting"


def reporting_units(reading: RunProgress) -> dict[str, UnitProgress]:
    """What each running unit that reports last said about itself, by unit."""
    return {
        unit.slug: unit.progress
        for unit in reading.running
        if unit.progress is not None
    }


def phase_events(reading: RunProgress, known: dict[str, UnitProgress]) -> Iterator[str]:
    """One line per unit that has entered a phase it was not in before.

    Per phase rather than per tick, because a watcher woken on every sample of
    every unit is a watcher that stops reading. A phase change is the unit
    saying it is doing something else now, which is the one inner change worth
    a line — the counts between them are a screen's business, or ``--once``'s.
    """
    for slug, record in reporting_units(reading).items():
        previous = known.get(slug)
        if not record.phase or (
            previous is not None and previous.phase == record.phase
        ):
            continue
        yield f"{slug} entered {record.phase} at {render_count(record)}"


def overdue_events(
    reading: RunProgress, flagged: list[str], factor: float = OVERDUE_FACTOR
) -> Iterator[str]:
    """One line per unit the first time it runs past ``factor`` times its median.

    Once per unit rather than per reading, so a unit stuck for an hour is one
    event an agent is woken for, not one every interval; ``flagged`` is the
    caller's memory of which units have already been named.
    """
    for unit in reading.overdue(factor):
        ratio = unit.overdue(factor)
        if ratio is None or unit.slug in flagged:
            continue
        flagged.append(unit.slug)
        yield f"{unit.slug} {render_overdue(unit, ratio)}"


def follow_events(
    directory: RunDirectory,
    log: Path | None = None,
    interval: float = 2.0,
    quiet_limit: float = 900.0,
    factor: float = OVERDUE_FACTOR,
) -> Iterator[str]:
    """Yield one line per thing that happens, ending when the run does.

    The first line is a baseline rather than a replay: attaching to a run that
    already landed four hundred units should say so once, not four hundred
    times. Everything after it is a change — a unit landing, a step moving, a
    unit entering a new phase of its own work, the run ending, or nothing
    happening for longer than ``quiet_limit``, which on a run whose heartbeat
    has stopped is the only evidence there will ever be.

    A unit's own count never yields a line, however often it is reported: an
    agent following a thousand-unit sweep would be flooded by samples it can
    read at any moment with ``--once``.
    """
    resolved_log = log if log is not None else default_log(directory)
    reading = read_progress(directory, resolved_log)
    landed = {result.slug: result for result in directory.read().results}
    states: dict[str, StepState] = {step.id: step.state for step in reading.steps}
    phases = reporting_units(reading)
    flagged: list[str] = []
    yield f"attached {reading.landed}/{reading.total} landed · {reading.postfix()}"
    yield from overdue_events(reading, flagged, factor)
    reported_stall = False
    while True:
        summary = reading.summary
        if summary is not None:
            yield describe_summary(summary)
            return
        if reading.stalled(quiet_limit) and not reported_stall:
            reported_stall = True
            quiet = render_span(reading.quiet_for or 0.0)
            yield f"stalled: nothing has changed for {quiet}; the runner may be gone"
        time.sleep(interval)
        reading = read_progress(directory, resolved_log)
        for result in directory.read().results:
            if result.slug not in landed:
                landed[result.slug] = result
                yield render_landing(result)
        for step in reading.steps:
            if states.get(step.id) is not step.state:
                states[step.id] = step.state
                yield f"step {step.render()}"
        yield from phase_events(reading, phases)
        phases = reporting_units(reading)
        yield from overdue_events(reading, flagged, factor)
        reported_stall = reported_stall and reading.stalled(quiet_limit)
