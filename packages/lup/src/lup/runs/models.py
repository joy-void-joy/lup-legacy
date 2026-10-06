"""What a run puts on disk, so anybody can read it while it is still running.

A job that outlives the tool call which started it is only as observable as
what it wrote down. These are the four things every run writes: the manifest
naming the units it scheduled, one result per unit that landed, one claim per
unit currently running, and a heartbeat line per landing. Nothing else has to
be true for a run to be followed — not that the launching shell survived, not
that anybody attached a terminal, not that the process is even still alive.

The declaration comes first and the reader reads it, rather than the reader
guessing a shape the writer happens to produce. That is the whole reason a
run is monitorable by construction: a writer that skipped the manifest would
fail to typecheck long before it produced an unfollowable run.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from lup.channels.models import utc_now
from lup.types import JsonValue

SINGLE_ITEM = "once"
"""The item a step with no fan-out lands under.

Canonical rather than a preference: the runtime writes this name and every
reader of a run resolves against it, so the two meet only by spelling it
alike. A run whose steps all lack a fan-out still has one file per unit,
which is what keeps the reader's arithmetic the same either way.
"""


class UnitStatus(StrEnum):
    """How one unit ended, in the run's own terms rather than the work's."""

    OK = "ok"
    FAILED = "failed"


class UnitResult(BaseModel, frozen=True):
    """One unit that landed, written atomically once and never edited.

    ``status`` is the run's vocabulary — the unit either produced a result or
    raised. ``outcome`` is the work's own word for what it found, carried
    beside the status rather than in place of it so a sweep can be tallied by
    what it concluded (``unsat=31 unknown=5``) without a failure hiding
    inside a domain label.
    """

    step: str
    item: str = SINGLE_ITEM
    status: UnitStatus
    outcome: str = ""
    fingerprint: str
    started_at: datetime
    finished_at: datetime
    detail: JsonValue = None
    error: str = ""

    @property
    def elapsed_seconds(self) -> float:
        """How long this unit took, from the two stamps that bound it."""
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def label(self) -> str:
        """The word this unit is tallied under: what it found, else how it ended."""
        return self.outcome or self.status.value

    @property
    def slug(self) -> str:
        """How this unit is named to a reader, step and item together."""
        return f"{self.step}/{self.item}"


class UnitAttempt(BaseModel, frozen=True):
    """A unit a runner has claimed and not yet landed.

    Written when the unit starts and removed when its result lands, so the
    claims still present are the units a runner believes it is working on.

    A claim is a **lease** rather than a note, because the two states it has to
    separate are otherwise identical on disk: a unit that is taking a long time
    and a unit whose runner was killed both leave a claim behind with no result
    beside it. Age since ``started_at`` cannot tell them apart — it grows for a
    healthy unit exactly as fast — so the runner re-stamps ``renewed_at`` while
    it holds the unit, and a claim nobody has renewed within the lease is one
    nobody is working.

    The process table is not the answer here and the reason is stated where the
    reading is taken: under a sandbox ``/proc`` is PID-isolated, so a healthy
    run is indistinguishable there from a dead one. ``pid`` is kept for a reader
    diagnosing a machine they are standing on, and nothing decides on it.
    """

    step: str
    item: str = SINGLE_ITEM
    started_at: datetime = Field(default_factory=utc_now)
    renewed_at: datetime = Field(default_factory=utc_now)
    """When the holder last said it was still working this unit.

    Separate from ``started_at`` because they answer different questions: how
    long this unit has been going, and whether anyone is still going. A reader
    wants both — a unit renewed thirty seconds ago and running for two hours is
    healthy and slow, which is not the same as stalled.
    """

    pid: int

    @property
    def slug(self) -> str:
        """How this unit is named to a reader, step and item together."""
        return f"{self.step}/{self.item}"


class UnitProgress(BaseModel, frozen=True):
    """What one unit says about its own inside while it is still running.

    A claim says a unit started and is still held. It cannot say whether the
    unit is forty percent through or spinning at zero, and from outside those
    read alike — so the unit publishes the one thing only it knows, how much
    of its own work it has done, and everything else is derived from that by
    whoever is watching.

    It carries no rate and no estimate. Two readings of ``done`` and the clock
    between them give both, and the reader is what holds two readings; a
    writer's own number would be taken over one burst, which is exactly the
    estimate :mod:`lup.runs.progress` exists to refuse.

    The record lives in the unit's own workspace rather than beside its claim
    because it outlives the claim: a unit that died at 2870 of 3000 in phase
    ``fit`` leaves that reading next to its traceback, where whoever comes
    back to the failure is already looking.
    """

    done: int = 0
    total: int | None = None
    """How much there is to do, or None when the unit cannot know its budget."""

    phase: str = ""
    """Which part of its own work the unit is in — a word, never a number."""

    detail: dict[str, JsonValue] = {}
    """The unit's own vocabulary, rendered as sent and never filtered."""

    reported_at: datetime = Field(default_factory=utc_now)

    @property
    def fraction(self) -> float | None:
        """How much of the unit is done, or None when it declared no budget."""
        if self.total is None or self.total <= 0:
            return None
        return min(1.0, self.done / self.total)


class StepRecord(BaseModel, frozen=True):
    """One scheduled step as the manifest carries it.

    The fingerprint is what makes a rerun decidable without asking: it folds
    the step's own declaration together with the fingerprints of everything it
    rests on, so a result recorded under a different one was computed from
    inputs that no longer stand.
    """

    id: str
    dependencies: list[str] = []
    fingerprint: str
    kind: str
    items: list[str] = []

    @property
    def width(self) -> int:
        """How many units this step contributes to the run's total.

        A step whose fan-out is computed from a dependency's results has no
        items until that dependency lands, and counts as the one unit it is
        certain to be. The manifest is rewritten as each fan-out resolves, so
        a reader watches the total grow rather than being told a wrong one.
        """
        return max(len(self.items), 1)


class RunManifest(BaseModel, frozen=True):
    """Everything a run scheduled, written before the first unit starts."""

    name: str
    started_at: datetime = Field(default_factory=utc_now)
    steps: list[StepRecord] = []

    @property
    def total_units(self) -> int:
        """How many units this run expects to land."""
        return sum(record.width for record in self.steps)

    def step(self, step_id: str) -> StepRecord | None:
        """The record for one step, or None when the manifest predates it."""
        return next(
            (record for record in self.steps if record.id == step_id),
            None,
        )


class SkippedStep(BaseModel, frozen=True):
    """A step the run reached and did not execute, and why."""

    id: str
    reason: str


class RunSummary(BaseModel, frozen=True):
    """How the run ended, written once by the runtime on its way out.

    A follower needs to tell "still working" from "over", and cannot get that
    from the unit count alone: a pipeline whose second stage failed never
    lands its fourth, so waiting for the total is waiting forever. The runtime
    writes this from a ``finally``, so it appears whether the run succeeded,
    failed, or was interrupted — and its absence beside an idle directory is
    exactly the signal that the runner was killed.
    """

    name: str
    finished_at: datetime = Field(default_factory=utc_now)
    landed: int
    failed: int
    skipped: list[SkippedStep] = []
    interrupted: bool = False
    unlanded: list[str] = []
    """Units that ran but whose result could not be written, by slug.

    Counted apart from ``failed``, which is read off the landed files: a unit
    whose result would not write has no file to be counted from, and without
    this it would vanish from the tally rather than read as a failure.
    """

    @property
    def ok(self) -> bool:
        """Whether the run ended with everything it attempted landing cleanly."""
        return (
            self.failed == 0
            and not self.skipped
            and not self.interrupted
            and not self.unlanded
        )
