"""A declared pipeline, and the runtime that turns it into a monitorable run.

The point of declaring the work rather than writing a script that does it is
that three questions become answerable without asking whoever launched it:
what was scheduled, what has landed, and what would have to happen again if
one part changed. A script answers none of those once it has exited, which is
why rerunning "just the encoding step" usually means rerunning everything,
and why watching a long job usually means watching a terminal.

Reuse is decided by fingerprint rather than by a flag. A step's fingerprint
folds its own declaration — its parameters, and its body's source where that
can be read — together with the fingerprints of everything it depends on, so
a landed result recorded under a different one was computed from inputs that
no longer stand, and is not reused. Editing a step therefore reruns it and
everything downstream of it, and nothing else, without anybody having to
remember what rested on what.

What the runtime writes is :mod:`lup.runs.models`, so every run built here is
followable by ``run monitor`` with no cooperation from the step bodies.
"""

import hashlib
import inspect
import json
import logging
import os
import shlex
import threading
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import IO, Annotated, Self

import sh
import typer
from pydantic import BaseModel, Field, model_validator

from lup.channels.models import utc_now
from lup.execution.dag import DependencyGraph
from lup.execution.shell import LazyCommand
from lup.runs.follow import render_landing
from lup.runs.directory import WORKSPACE_ENV, RunDirectory, stderr_in, stdout_in
from lup.runs.limits import SessionRegistry, UnitLimitExceeded, UnitLimits, supervise
from lup.runs.models import (
    SINGLE_ITEM,
    RunManifest,
    RunSummary,
    SkippedStep,
    StepRecord,
    UnitAttempt,
    UnitProgress,
    UnitResult,
    UnitStatus,
)
from lup.runs.progress import describe_summary
from lup.runs.report import report_progress
from lup.types import JsonValue

logger = logging.getLogger(__name__)


class StepOutcome(BaseModel, frozen=True):
    """What one unit of work concluded.

    ``outcome`` is the word this unit is tallied under by anybody watching, so
    it belongs to the work's own vocabulary — ``unsat``, ``converged``,
    ``no-change`` — and is optional, because plenty of steps either worked or
    did not and have nothing further to say.
    """

    outcome: str = ""
    detail: JsonValue = None


class FanContext(BaseModel, frozen=True):
    """What a computed fan-out may read to decide how wide its step is.

    A width read off a dependency's results almost always means reading what
    those results *wrote*, so the run comes with them: a fan-out that had only
    the records would have to rebuild the artifact path by hand, against a
    layout it does not own.
    """

    run: RunDirectory
    step: str
    dependencies: dict[str, list[UnitResult]] = {}

    def artifacts_of(self, result: UnitResult) -> Path:
        """Where one landed unit put whatever it produced besides its result."""
        return self.run.workspace(result.step, result.item)


class StepContext(FanContext, frozen=True):
    """Everything one unit is given: where it is, and what it may read."""

    item: str = SINGLE_ITEM
    sessions: SessionRegistry = Field(default_factory=SessionRegistry, exclude=True)
    """Where a limited unit enrols the session it runs in, for the runner to stop.

    The runner's own, handed down rather than reached for, so a unit run
    outside a pipeline — a test, a one-off — gets a registry of its own and
    needs nothing global.
    """

    @property
    def workspace(self) -> Path:
        """A directory this unit owns, for whatever it produces besides a result."""
        path = self.run.workspace(self.step, self.item)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def report(
        self,
        done: int,
        total: int | None = None,
        phase: str = "",
        detail: dict[str, JsonValue] | None = None,
    ) -> UnitProgress | None:
        """Say how far into its own work this unit has got.

        The context already knows where this unit's workspace is, so a body
        names no path and cannot publish where nothing reads. Call it from
        the loop that does the work: reporting more often than a reader looks
        costs a dropped call rather than a write, which is what lets the
        cheapest call site be the right one.
        """
        return report_progress(
            self.workspace, done=done, total=total, phase=phase, detail=detail
        )


type StepBody = Callable[[StepContext], StepOutcome | None]
type ItemsBody = Callable[[FanContext], list[str]]


def source_text(body: StepBody | ItemsBody) -> str:
    """The body's own source, which is what makes editing a step rerun it.

    Empty when the source cannot be read — a callable built at runtime, one
    defined in a REPL. That weakens the fingerprint rather than breaking it,
    and it does so visibly: such a step rests on its declared parameters
    alone, so editing it will not invalidate what it already landed.
    """
    try:
        return inspect.getsource(body)
    except (OSError, TypeError) as error:
        logger.debug("step body source unavailable: %s", error)
        return ""


class ItemSource(BaseModel, ABC, frozen=True):
    """Where a step's fan-out comes from."""

    @abstractmethod
    def resolve(self, context: FanContext) -> list[str]:
        """The items this step spreads over, given what it rests on."""

    @abstractmethod
    def declared(self) -> list[str]:
        """The items knowable before anything has run; empty when computed."""

    @abstractmethod
    def signature(self) -> str:
        """What this source contributes to its step's fingerprint."""


class FixedItems(ItemSource, frozen=True):
    """A fan-out known when the pipeline is written."""

    items: list[str] = Field(min_length=1)

    def resolve(self, context: FanContext) -> list[str]:
        """The declared items, which need nothing to resolve."""
        return list(self.items)

    def declared(self) -> list[str]:
        """All of them, so the run's total is right before it starts."""
        return list(self.items)

    def signature(self) -> str:
        """The items themselves: changing the sweep is changing the step."""
        return ",".join(self.items)


class ComputedItems(ItemSource, frozen=True):
    """A fan-out read off what this step's dependencies landed."""

    compute: ItemsBody

    def resolve(self, context: FanContext) -> list[str]:
        """Ask the declared callable what to spread over."""
        return self.compute(context)

    def declared(self) -> list[str]:
        """None yet — the manifest is rewritten once this resolves."""
        return []

    def signature(self) -> str:
        """The callable's source, so editing the fan-out reruns the step."""
        return source_text(self.compute)


class Step(BaseModel, ABC, frozen=True):
    """One named piece of a pipeline, and what it rests on."""

    id: str = Field(min_length=1)
    dependencies: list[str] = []
    params: JsonValue = None
    over: ItemSource | None = None
    retries: int = 0
    limits: UnitLimits | None = None
    """How long each of this step's units may run and how much memory it may hold.

    Enforced from outside the unit, which only a unit in a process of its own
    allows; see :mod:`lup.runs.limits`. A unit stopped for a limit fails with
    the breach and is not retried, since another attempt would meet the same
    bound.
    """

    @abstractmethod
    def run(self, context: StepContext) -> StepOutcome:
        """Do this unit's work, or raise."""

    @abstractmethod
    def source(self) -> str:
        """What this step is, as text, for its fingerprint."""

    @property
    @abstractmethod
    def kind(self) -> str:
        """The word a reader sees for what sort of step this is."""

    def items(self, context: FanContext) -> list[str]:
        """The items this step spreads over; one unnamed unit when it does not."""
        if self.over is None:
            return [SINGLE_ITEM]
        return self.over.resolve(context)

    def declared_items(self) -> list[str]:
        """The items knowable before the run starts, for the first manifest."""
        if self.over is None:
            return [SINGLE_ITEM]
        return self.over.declared()

    def declaration(self) -> str:
        """The text this step's own fingerprint is taken over."""
        return json.dumps(
            {
                "id": self.id,
                "kind": self.kind,
                "params": self.params,
                "over": self.over.signature() if self.over is not None else "",
                "source": self.source(),
            },
            sort_keys=True,
        )


class CallableStep(Step, frozen=True):
    """A step whose work is a Python callable, run in this process."""

    body: StepBody

    @model_validator(mode="after")
    def refuse_limits(self) -> Self:
        """Refuse limits, which nothing could enforce on a body in this process.

        A callable runs on one of the runner's own threads. Stopping it would
        mean stopping the runner, and a thread cannot be killed at all; a limit
        accepted here would be a bound that silently never fires.
        """
        if self.limits is not None:
            raise ValueError(
                f"step {self.id!r} declares limits, which need the unit in a "
                "process of its own: run the work as a ShellStep, or drop the limits"
            )
        return self

    @property
    def kind(self) -> str:
        """What sort of step this is."""
        return "callable"

    def run(self, context: StepContext) -> StepOutcome:
        """Call the body; one that returns nothing simply succeeded."""
        return self.body(context) or StepOutcome()

    def source(self) -> str:
        """The body's source."""
        return source_text(self.body)


class ShellStep(Step, frozen=True):
    """A step whose work is a shell command.

    The unit's coordinates reach the command as shell variables rather than by
    substitution into its text, because a command is full of ``$`` and ``{}``
    that a templating pass would have to fight — and ``$LUP_RUN_ITEM`` reads
    as shell to whoever is writing shell.
    """

    command: str
    shell: str = "bash"

    @property
    def kind(self) -> str:
        """What sort of step this is."""
        return "shell"

    def source(self) -> str:
        """The command itself, which is the whole of what this step is."""
        return self.command

    def script(self, context: StepContext) -> str:
        """The command with this unit's coordinates bound above it."""
        bindings = "\n".join(
            f"{name}={shlex.quote(value)}"
            for name, value in [
                ("LUP_RUN_DIR", str(context.run.root)),
                ("LUP_RUN_STEP", context.step),
                ("LUP_RUN_ITEM", context.item),
                (WORKSPACE_ENV, str(context.workspace)),
            ]
        )
        return f"{bindings}\n{self.command}"

    def run(self, context: StepContext) -> StepOutcome:
        """Run the command, keeping the whole of its output beside the result.

        Output goes to files rather than into the result record, because a
        step that prints a hundred megabytes is an ordinary step: a record
        that swallowed it would be unreadable, and one that kept a prefix
        would look complete while being cut. The result points at all of it.
        """
        out_path = stdout_in(context.workspace)
        err_path = stderr_in(context.workspace)
        with (
            out_path.open("w", encoding="utf-8") as out,
            err_path.open("w", encoding="utf-8") as err,
        ):
            if self.limits is None:
                LazyCommand(self.shell)(
                    "-c", self.script(context), _out=out, _err=err, _tty_out=False
                )
            else:
                self.run_limited(context, self.limits, out, err)
        return StepOutcome(detail={"stdout": str(out_path), "stderr": str(err_path)})

    def run_limited(
        self, context: StepContext, limits: UnitLimits, out: IO[str], err: IO[str]
    ) -> None:
        """Run the command in a session of its own, under its declared limits.

        A session rather than the runner's own process group, so the unit's
        memory is summed over everything it started and stopping it reaches
        all of it; the runner's registry is what still stops it on an
        interrupt, which a terminal no longer delivers to it directly.
        """
        running = LazyCommand(self.shell)(
            "-c",
            self.script(context),
            _out=out,
            _err=err,
            _tty_out=False,
            _bg=True,
            _bg_exc=False,
            _new_session=True,
            _return_cmd=True,
        )
        if not isinstance(running, sh.RunningCommand):
            raise TypeError(
                f"step {self.id!r} needs a handle on its running command to "
                "enforce limits, and the shell returned none"
            )
        supervise(running, limits, context.sessions)
        # Read the way the unlimited path reads it, so a nonzero exit raises
        # the same error and fails the unit the same way.
        running.wait()


class RunRequest(BaseModel, frozen=True):
    """Which parts of a pipeline this invocation means to run, and where."""

    directory: Path
    only: list[str] = []
    start_from: str = ""
    force: list[str] = []
    workers: int = 0
    fresh: bool = False


class PlannedUnit(BaseModel, frozen=True):
    """One unit the runtime has decided to execute."""

    step: Step
    item: str
    fingerprint: str
    dependencies: dict[str, list[UnitResult]] = {}


class PipelineError(RuntimeError):
    """A pipeline was asked for something its declaration cannot answer."""


def digest(declaration: str, dependency_fingerprints: list[str]) -> str:
    """One step's fingerprint, chained through everything it rests on.

    Sixteen bytes is the whole digest rather than a prefix of a longer one:
    this is an identity to compare, not a hash to defend, and asking the
    function for the width wanted is what keeps it from being a truncation.
    """
    payload = json.dumps(
        {"declaration": declaration, "dependencies": dependency_fingerprints},
        sort_keys=True,
    )
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()


class StepPlan(BaseModel, frozen=True):
    """What the runtime settled about one step before anything ran.

    One record rather than three maps keyed by step id: the fingerprint, the
    eligibility and the forcing are decided together from the same request,
    and reading them apart is how they get out of step.
    """

    id: str
    fingerprint: str
    eligible: bool
    forced: bool


class Fault(BaseModel, frozen=True):
    """One write the runner could not make, in the words of what refused it."""

    what: str
    error: str

    @classmethod
    def of(cls, what: str, error: Exception) -> Self:
        """The fault one exception makes of one write."""
        return cls(what=what, error=f"{type(error).__name__}: {error}")


class Outage(BaseModel, frozen=True):
    """The writes the runner has been failing to make, and since when.

    Opened by the first write refused, and closed by the first heartbeat line
    the log takes in a tick whose renewals all went through — the line that
    reports it, because the log cannot carry the news while it refuses
    lines, and carries it the moment it takes one again.
    """

    since: datetime = Field(default_factory=utc_now)
    failed: dict[str, Fault] = {}
    """The latest refusal of each write that failed, by what the write was."""

    def including(self, faults: list[Fault]) -> Self:
        """This outage with more refused writes in it."""
        return self.model_copy(
            update={"failed": {**self.failed, **{f.what: f for f in faults}}}
        )

    def report(self, ongoing: bool) -> str:
        """What failed and since when, while it lasts or once it is over."""
        since = self.since.isoformat(timespec="seconds")
        failed = ", ".join(f"{f.what} ({f.error})" for f in self.failed.values())
        if ongoing:
            return f"failing since {since}: could not {failed}"
        until = utc_now().isoformat(timespec="seconds")
        return (
            f"writing again after failing from {since} to {until}: could not {failed}"
        )


class RunState(BaseModel, arbitrary_types_allowed=True):
    """What one execution accumulates while it works.

    The growing picture of what has landed is the driving thread's alone. A
    worker is handed a unit, writes its own result file, and hands the result
    back, so that picture never crosses a thread boundary.

    The claims this invocation holds do cross one, and are the exception the
    lock exists for: a worker takes a claim and drops it, and the heartbeat
    thread renews whatever is held at the time.
    """

    run: RunDirectory
    results: dict[str, list[UnitResult]] = {}
    items: dict[str, list[str]] = {}
    skipped: list[SkippedStep] = []
    interrupted: bool = False
    crashed: str = ""
    crash_traceback: str = ""

    unlanded: list[str] = []
    """Units whose result could not be written, by slug, taken under the lock."""

    held: dict[str, UnitAttempt] = {}
    """The claims this invocation is holding, by slug.

    This process's own, rather than every claim on disk: renewing a claim a
    sibling runner holds would be this process vouching for work it is not
    doing, which is the one thing the lease exists to make impossible.
    """

    holding: threading.Lock = Field(default_factory=threading.Lock, exclude=True)

    sessions: SessionRegistry = Field(default_factory=SessionRegistry, exclude=True)
    """The limited units' sessions, which an interrupt or a crash must stop."""

    outage: Outage | None = None
    """What has been refused since a heartbeat last went through whole.

    Shared across threads as the claims are, and under the same lock: a
    worker whose landing line is refused adds to it, and the heartbeat
    reports it and closes it.
    """

    def take(self, attempt: UnitAttempt) -> None:
        """Claim one unit and hold its lease until the unit lands."""
        with self.holding:
            self.held[attempt.slug] = attempt
        self.run.claim(attempt)

    def drop(self, attempt: UnitAttempt) -> None:
        """Stop holding a unit's lease, whatever became of the unit."""
        with self.holding:
            self.held.pop(attempt.slug, None)

    def renew(self) -> list[Fault]:
        """Say every unit this invocation holds is still being worked.

        Called from the heartbeat, which already ticks well inside the lease
        and already means "this runner is alive" — per unit rather than for the
        run as a whole, because that is the grain a claim is read at.

        Each claim is renewed on its own, so one the disk refuses costs that
        claim's renewal and not those of the claims after it. What was refused
        comes back for the heartbeat to report.
        """
        with self.holding:
            attempts = list(self.held.values())

        def refused(attempt: UnitAttempt) -> Fault | None:
            try:
                self.run.renew(attempt.step, attempt.item)
            except Exception as error:
                return Fault.of(f"renew {attempt.slug}", error)
            return None

        return [
            fault
            for fault in (refused(attempt) for attempt in attempts)
            if fault is not None
        ]

    def falter(self, faults: list[Fault]) -> Outage:
        """Take refused writes into the outage, opening one if none stands."""
        with self.holding:
            self.outage = (self.outage or Outage()).including(faults)
            return self.outage

    def standing(self) -> Outage | None:
        """The outage not yet reported as over, if one is."""
        with self.holding:
            return self.outage

    def settle(self, reported: Outage) -> None:
        """Close the outage a line has just reported, unless it has grown since."""
        with self.holding:
            if self.outage == reported:
                self.outage = None

    def log(self, line: str) -> bool:
        """Add a line to the run's log, and say whether the log took it.

        A line describes the run and is no part of it, so a log that refuses
        one costs that line and nothing else — never a unit, never the run. A
        refused line goes to stderr whole, through logging, which does not
        raise when stderr is itself on the disk that refused it, and the
        refusal joins the outage the next line the log takes reports.
        """
        try:
            self.run.append_heartbeat(line)
        except Exception as error:
            outage = self.falter(
                [Fault.of(f"write to {self.run.log_path.name}", error)]
            )
            logger.warning(
                "%s refused a line, %s; the line: %s",
                self.run.log_path,
                outage.report(ongoing=True),
                line,
            )
            return False
        return True

    def record(self, result: UnitResult) -> None:
        """Take one landed unit into the picture the remaining steps read."""
        self.results.setdefault(result.step, []).append(result)

    def results_of(self, step_id: str) -> list[UnitResult]:
        """What one step has landed so far, reused results included."""
        return list(self.results.get(step_id, []))

    def blocker(self, step: Step) -> str:
        """The first dependency that failed or was skipped, if any did."""
        skipped_ids = {entry.id for entry in self.skipped}
        return next(
            (
                dependency
                for dependency in step.dependencies
                if dependency in skipped_ids
                or any(
                    result.status is UnitStatus.FAILED
                    for result in self.results_of(dependency)
                )
            ),
            "",
        )

    def known_items(self, step: Step) -> list[str]:
        """The items a step nobody is running is known to have.

        A computed fan-out declares none, so what it landed is discovered by
        reading the directory rather than by asking the declaration — the only
        way a resumed run can reuse a fan-out an earlier invocation sized.
        Each item is read from its result, not its filename, which is the item
        only when the item was already a safe path component.
        """
        declared = step.declared_items()
        if declared:
            return declared
        return sorted(result.item for result in self.run.read_step(step.id))

    def adopt(self, step: Step) -> None:
        """Take a step this invocation will not run at its landed word."""
        landed = [
            result
            for result in (
                self.run.read_result(step.id, item) for item in self.known_items(step)
            )
            if result is not None
        ]
        for result in landed:
            self.record(result)
        self.items[step.id] = [result.item for result in landed]

    def plan(self, step: Step, decided: StepPlan) -> list[PlannedUnit]:
        """Decide which of this step's units have to run, and reuse the rest."""
        if not decided.eligible:
            self.adopt(step)
            return []
        blocked_by = self.blocker(step)
        if blocked_by:
            self.skipped.append(
                SkippedStep(id=step.id, reason=f"depends on {blocked_by}")
            )
            return []
        dependencies = {
            dependency: self.results_of(dependency) for dependency in step.dependencies
        }
        absent = sorted(name for name, landed in dependencies.items() if not landed)
        if absent:
            raise PipelineError(
                f"cannot run {step.id}: {', '.join(absent)} has landed nothing — "
                f"run it, or widen the selection to include it"
            )
        items = step.items(
            FanContext(run=self.run, step=step.id, dependencies=dependencies)
        )
        self.items[step.id] = items
        reusable = {
            item: result
            for item, result in (
                (item, self.run.read_result(step.id, item)) for item in items
            )
            if result is not None
            and not decided.forced
            and result.fingerprint == decided.fingerprint
            and result.status is UnitStatus.OK
        }
        for result in reusable.values():
            self.record(result)
        return [
            PlannedUnit(
                step=step,
                item=item,
                fingerprint=decided.fingerprint,
                dependencies=dependencies,
            )
            for item in items
            if item not in reusable
        ]

    def perform(self, unit: PlannedUnit) -> UnitResult:
        """Run one unit, land its result, and say what happened.

        Called on a worker thread, so it touches nothing shared: the claim and
        the result go to their own files, and the result comes back for the
        driving thread to fold in.

        Nothing about one unit ends the run. A claim that cannot be written
        is that unit's failure, with its traceback, exactly as a step that
        raised is — the units beside it go on, and the run reports the one
        that could not start rather than stopping on it. A landing line the
        log refuses costs that line, which goes to stderr instead.
        """
        context = StepContext(
            run=self.run,
            step=unit.step.id,
            item=unit.item,
            dependencies=unit.dependencies,
            sessions=self.sessions,
        )
        claimed = UnitAttempt(step=unit.step.id, item=unit.item, pid=os.getpid())
        begun = utc_now()
        try:
            self.take(claimed)
            result = attempt(unit, context)
        except Exception:
            result = unit_failure(unit, begun, [traceback.format_exc()])
        try:
            result = self.land(result)
        finally:
            # Dropped whatever happened, because the lease says who is working
            # this unit and this invocation stops being the answer either way.
            # `write_result` releases the claim on disk; this releases the hold
            # that would otherwise have the heartbeat put it back.
            self.drop(claimed)
        self.log(render_landing(result))
        return result

    def land(self, result: UnitResult) -> UnitResult:
        """Write one unit's result, and say what actually landed.

        A result that will not write — a payload the disk refuses, a record
        that will not serialize — lands as the unit's failure carrying why,
        since a failure with no payload is what most often still writes. One
        that cannot land at all is named in the summary's ``unlanded``, so
        the tally the summary gives cannot quietly lose it.
        """
        try:
            self.run.write_result(result)
        except Exception:
            failure = result.model_copy(
                update={
                    "status": UnitStatus.FAILED,
                    "outcome": "",
                    "detail": None,
                    "error": "\n".join(
                        part for part in (result.error, traceback.format_exc()) if part
                    ),
                }
            )
        else:
            return result
        try:
            self.run.write_result(failure)
        except Exception:
            with self.holding:
                self.unlanded.append(failure.slug)
            self.log(f"could not land {failure.slug}: {traceback.format_exc()}")
        return failure

    def summarize(self, name: str) -> RunSummary:
        """How this run ended, counted off the directory rather than off memory.

        Read back from disk because a resumed run's landed units include ones
        this invocation never touched, and whoever comes back to the directory
        wants what is in it, not what this process happened to do.
        """
        landed = self.run.read().results
        with self.holding:
            unlanded = list(self.unlanded)
        return RunSummary(
            name=name,
            landed=len(landed),
            failed=sum(1 for result in landed if result.status is UnitStatus.FAILED),
            skipped=self.skipped,
            interrupted=self.interrupted,
            crashed=self.crashed,
            crash_traceback=self.crash_traceback,
            unlanded=unlanded,
        )


def attempt(unit: PlannedUnit, context: StepContext) -> UnitResult:
    """Run one unit, retrying as its step allows, and record how it ended.

    Every attempt's traceback is kept, the successful run's included: a step
    that passes on its third try is a different thing from one that passed,
    and a result that mentioned only the last attempt would hide it.

    A unit stopped for a declared limit fails at once and is not retried:
    another attempt would meet the same bound, and spend it again.
    """
    errors: list[str] = []
    for remaining in reversed(range(max(1, unit.step.retries + 1))):
        begun = utc_now()
        try:
            outcome = unit.step.run(context)
        except UnitLimitExceeded as exceeded:
            failure = unit_failure(unit, begun, [*errors, exceeded.breach.render()])
            return failure.model_copy(update={"breach": exceeded.breach})
        except Exception:
            errors.append(traceback.format_exc())
            if remaining:
                continue
            return unit_failure(unit, begun, errors)
        return UnitResult(
            step=unit.step.id,
            item=unit.item,
            status=UnitStatus.OK,
            outcome=outcome.outcome,
            fingerprint=unit.fingerprint,
            started_at=begun,
            finished_at=utc_now(),
            detail=outcome.detail,
            error="\n".join(errors),
        )
    raise PipelineError(f"{unit.step.id} ran zero times, which cannot happen")


def unit_failure(unit: PlannedUnit, begun: datetime, errors: list[str]) -> UnitResult:
    """One unit's failed result, carrying every traceback that led to it."""
    return UnitResult(
        step=unit.step.id,
        item=unit.item,
        status=UnitStatus.FAILED,
        fingerprint=unit.fingerprint,
        started_at=begun,
        finished_at=utc_now(),
        error="\n".join(errors),
    )


def heartbeat(state: RunState, stop: threading.Event, interval: float) -> None:
    """Renew what this run holds, and write a line, for as long as it is working.

    Without the line, a run whose units take hours writes nothing between
    landings, and a follower cannot tell a solver thinking from a runner that
    was killed. A line every interval makes silence mean one thing only.

    The renewal is the same statement made per unit, and it belongs on this
    thread because the interval is already chosen to sit well inside the claim
    lease.

    A tick that cannot write ends nothing. A disk filling under a live runner
    silences it without killing it, and a heartbeat that stopped at the first
    refusal would leave every claim it held to lapse under units still
    working — read as abandoned, and run twice by whoever resumed on that
    reading. Every tick tries again, so the claims are renewed on the first
    tick the disk takes writes, and only a runner that is gone stops renewing
    for good.
    """
    while not stop.wait(interval):
        try:
            tick(state)
        except Exception:
            # Every write in a tick is already its own; this catches what a
            # tick reads, so a directory that will not list costs one line.
            logger.exception("heartbeat tick failed; the next one runs as usual")


def tick(state: RunState) -> None:
    """One heartbeat: renew every claim this run holds, then write the line.

    Each write is tried on its own, so one the disk refuses costs only itself.
    What was refused goes to stderr on every tick it goes on being refused,
    and every line the log takes while it stands says what failed and since
    when. The first such line in a tick whose renewals all went through says
    it is over, and closes it.
    """
    faults = state.renew()
    if faults:
        logger.warning(
            "heartbeat %s; the runner goes on and tries again next tick",
            state.falter(faults).report(ongoing=True),
        )
    standing = state.standing()
    line = (
        f"working: {len(state.run.read().results)} landed, "
        f"{len(state.run.running())} running"
    )
    if standing is not None:
        line = f"{line}; {standing.report(ongoing=bool(faults))}"
    if state.log(line) and standing is not None and not faults:
        state.settle(standing)


class Pipeline(BaseModel, frozen=True):
    """A named set of steps, and everything a run of them needs to be watched."""

    name: str = Field(min_length=1)
    steps: list[Step] = Field(min_length=1)
    workers: int = 1
    heartbeat_seconds: float = 30.0

    def graph(self) -> DependencyGraph[Step]:
        """The steps ordered, validated for missing nodes and for cycles."""
        return DependencyGraph(self.steps, subject="step")

    def decide(self, request: RunRequest) -> dict[str, StepPlan]:
        """What this invocation will do with every step, decided before any runs.

        ``--only`` names an exact set. ``--from`` names where to pick up, which
        is that step together with everything downstream of it. ``--force``
        reruns a step whose fingerprint has not changed — and everything
        downstream of it, which rests on a result just recomputed.

        Fingerprints are taken in dependency order and chained, so that editing
        one step invalidates everything downstream of it without anybody
        maintaining a list of what that is: a dependent's fingerprint is taken
        over its parents' fingerprints too.
        """
        graph = self.graph()
        known = [step.id for step in self.steps]
        named = [*request.only, *request.force, request.start_from]
        unknown = sorted({name for name in named if name and name not in known})
        if unknown:
            raise PipelineError(f"no such step: {', '.join(unknown)}")
        eligible = self.eligible(request, graph)
        forced = {*request.force} | {
            step.id for name in request.force for step in graph.descendants(name)
        }
        # lup: ignore[empty-collection] — a chained fold, each entry taken over
        # the entries its step depends on, which no comprehension expresses
        decided: dict[str, StepPlan] = {}
        for batch in graph.topological_batches():
            for step in batch:
                decided[step.id] = StepPlan(
                    id=step.id,
                    fingerprint=digest(
                        step.declaration(),
                        [
                            decided[dependency].fingerprint
                            for dependency in step.dependencies
                        ],
                    ),
                    eligible=step.id in eligible,
                    forced=step.id in forced,
                )
        return decided

    def eligible(self, request: RunRequest, graph: DependencyGraph[Step]) -> list[str]:
        """The steps this invocation is allowed to touch at all."""
        if request.only:
            return list(request.only)
        if request.start_from:
            return [
                request.start_from,
                *(step.id for step in graph.descendants(request.start_from)),
            ]
        return [step.id for step in self.steps]

    def declare(self, run: RunDirectory, decided: dict[str, StepPlan]) -> RunManifest:
        """Write what this run scheduled, keeping a resumed run's start time."""
        existing = run.read_manifest()
        manifest = RunManifest(
            name=self.name,
            started_at=existing.started_at if existing is not None else utc_now(),
            steps=[
                StepRecord(
                    id=step.id,
                    dependencies=step.dependencies,
                    fingerprint=decided[step.id].fingerprint,
                    kind=step.kind,
                    items=step.declared_items(),
                )
                for step in self.steps
            ],
        )
        run.write_manifest(manifest)
        return manifest

    def republish(self, manifest: RunManifest, state: RunState) -> RunManifest:
        """Rewrite the manifest with the fan-outs that have since resolved.

        A computed fan-out has no width until the step it reads from lands, so
        the total a follower sees grows as the run discovers it. Rewriting is
        how the follower learns; an unrewritten manifest would leave a
        thousand-cell sweep reading as one unit forever.
        """
        updated = RunManifest(
            name=manifest.name,
            started_at=manifest.started_at,
            steps=[
                StepRecord(
                    id=record.id,
                    dependencies=record.dependencies,
                    fingerprint=record.fingerprint,
                    kind=record.kind,
                    items=state.items.get(record.id, record.items),
                )
                for record in manifest.steps
            ],
        )
        state.run.write_manifest(updated)
        return updated

    def execute(self, request: RunRequest) -> RunSummary:
        """Run the selected steps and return how it ended.

        The summary is written whatever happens, an interrupt or a crash
        included: a follower has no other way to tell a run still working from
        one whose process is gone, and leaving that ambiguous is what sends
        people looking for a process table a sandbox will not show them. A
        crash writes its cause there and to the log before it propagates,
        because the launching shell's stderr is the one place nobody reads.

        An earlier attempt's summary is taken down before anything is claimed,
        and what it said goes to the log: until this attempt writes its own,
        the directory holds a run that is still going.

        Every line is written through :meth:`RunState.log`, so a log the disk
        refuses costs lines and never the run; an outage still standing when
        the run ends is reported on its last line.
        """
        run = RunDirectory(root=request.directory)
        run.root.mkdir(parents=True, exist_ok=True)
        decided = self.decide(request)
        if request.fresh:
            self.wipe(run)
        state = RunState(run=run)
        previous = run.retire_summary()
        if previous is not None:
            state.log(f"resuming after: {describe_summary(previous)}")
        for slug in run.clear_claims():
            state.log(f"reclaimed {slug}: its lease had lapsed")
        stop = threading.Event()
        beat = threading.Thread(
            target=heartbeat, args=(state, stop, self.heartbeat_seconds), daemon=True
        )
        beat.start()
        try:
            manifest = self.declare(run, decided)
            state.log(f"started {self.name}: {len(self.steps)} steps")
            self.drive(state, manifest, decided, request)
        except KeyboardInterrupt:
            state.interrupted = True
            state.log("interrupted")
        except Exception as error:
            state.crashed = repr(error)
            state.crash_traceback = traceback.format_exc()
            state.log(
                f"crashed: {state.crashed}; the traceback is in {run.summary_path.name}"
            )
            raise
        finally:
            stop.set()
            beat.join(timeout=self.heartbeat_seconds)
            summary = state.summarize(self.name)
            run.write_summary(summary)
            ending = f"finished: {summary.landed} landed"
            standing = state.standing()
            if standing is not None:
                ending = f"{ending}; {standing.report(ongoing=False)}"
            state.log(ending)
        return summary

    def wipe(self, run: RunDirectory) -> None:
        """Drop every landed result, so a fresh run recomputes all of it."""
        for path in sorted(run.units_root.glob("*/*.json")):
            path.unlink(missing_ok=True)
        run.summary_path.unlink(missing_ok=True)

    def drive(
        self,
        state: RunState,
        manifest: RunManifest,
        decided: dict[str, StepPlan],
        request: RunRequest,
    ) -> None:
        """Work the graph one batch at a time, every ready unit at once."""
        workers = max(1, request.workers or self.workers)
        for batch in self.graph().topological_batches():
            units = [
                unit for step in batch for unit in state.plan(step, decided[step.id])
            ]
            manifest = self.republish(manifest, state)
            if not units:
                continue
            with ThreadPoolExecutor(max_workers=workers) as pool:
                try:
                    for result in pool.map(state.perform, units):
                        state.record(result)
                # lup: ignore[except-baseexception] — re-raised; an interrupt cannot reach the sessions it stops
                except BaseException:
                    # Before the pool's exit waits on its workers: a limited
                    # unit runs in a session of its own, which an interrupt at
                    # the terminal does not reach, so the wait would otherwise
                    # last as long as the slowest unit being abandoned.
                    state.sessions.close()
                    raise

    def main(self) -> None:
        """Serve this pipeline as a command line, so every one gets these flags."""
        app = typer.Typer(no_args_is_help=True)
        default_directory = Path("tmp/runs") / self.name

        @app.command("run")
        def run_command(
            directory: Annotated[
                Path, typer.Option("--directory", "-d", help="Where the run lives")
            ] = default_directory,
            only: Annotated[
                list[str], typer.Option("--only", help="Run exactly these steps")
            ] = [],
            start_from: Annotated[
                str, typer.Option("--from", help="Run this step and everything after")
            ] = "",
            force: Annotated[
                list[str],
                typer.Option("--force", help="Rerun this step even if it is current"),
            ] = [],
            workers: Annotated[
                int, typer.Option("--workers", help="Units to run at once")
            ] = 0,
            fresh: Annotated[
                bool, typer.Option("--fresh", help="Discard every landed result first")
            ] = False,
        ) -> None:
            """Run the pipeline, reusing every step whose inputs have not changed."""
            summary = self.execute(
                RunRequest(
                    directory=directory,
                    only=only,
                    start_from=start_from,
                    force=force,
                    workers=workers,
                    fresh=fresh,
                )
            )
            typer.echo(
                f"{summary.landed} landed, {summary.failed} failed; "
                f"follow with: uv run lup-devtools run monitor {directory}"
            )
            if not summary.ok:
                raise typer.Exit(1)

        @app.command("plan")
        def plan_command() -> None:
            """Print the steps, what each rests on, and its current fingerprint."""
            decided = self.decide(RunRequest(directory=default_directory))
            for batch in self.graph().topological_batches():
                for step in batch:
                    rests = ", ".join(step.dependencies) or "-"
                    typer.echo(
                        f"{step.id}\t{step.kind}\t"
                        f"{decided[step.id].fingerprint}\trests on {rests}"
                    )

        app()
