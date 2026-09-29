"""Answering whether a run is alive, and what it last did.

"Is it still running, or did it stop?" is the most common question about a
resolver run, because the resolver is built to be left alone: it parks,
persists, and is resumed later. Reading a journal's mtime and hand-parsing
its last event is the technique reinvented for it each session, and easy to
get wrong in both directions, since a run can legitimately print nothing
for tens of minutes while a planner works.

Everything here is derived from the run directory alone. Under a sandbox
`/proc` is PID-isolated, so `ps` and `pgrep` list nothing outside the
current shell and a healthy run is indistinguishable from a dead one; a
liveness answer that consults the process table is therefore no answer at
all on the host the resolver most often runs on.
"""

from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel
from tqdm import tqdm

from lup.channels.models import utc_now
from lup.resolver.join_desk import JoinDesk
from lup.resolver.record import journal_tail
from lup.resolver.models import (
    SETTLED_STATUSES,
    ConcernStatus,
    JoinProgress,
    ResolvePhase,
    ResolveState,
    RunTally,
)
from lup.resolver.recheck_desk import RecheckDesk
from lup.resolver.lifecycle import HostWait, HostWaitStore
from lup.resolver.state import ResolverStateRepository


BAR_SHADES = "░▏▎▍▌▋▊▉█"
"""The cells a bar is drawn from, emptiest first, as tqdm reads a gradient.

tqdm's own gradient leaves an empty cell blank, which suits a bar that owns
its line and not one joined into a ` · ` status line beside other figures:
an empty bar there reads as stray whitespace rather than as no progress.
Shading the empty cell keeps the bar's extent visible at zero, which is the
one moment a reader most wants to see it.
"""


class StatusCount(BaseModel, frozen=True):
    """How many concerns stand at one status."""

    status: ConcernStatus
    concerns: int


class LastRecorded(BaseModel, frozen=True):
    """The run's most recent journal entry, as a reader needs to see it."""

    event: str
    actor: str
    at: datetime

    def quiet_for(self, now: datetime | None = None) -> timedelta:
        """How long since the run recorded anything."""
        return (now if now is not None else utc_now()) - self.at


def elapsed_per_item(
    completions: list[datetime], gap: timedelta = timedelta(hours=1)
) -> timedelta | None:
    """The mean time one item took, over the stretches actually worked.

    A run is resumed, so consecutive samples can be separated by however
    long nobody was driving it — this one holds an interval of twenty-eight
    hours between two joins a minute of work apart. Averaged in, a single
    such gap makes every later estimate meaningless, so an interval longer
    than ``gap`` is read as the run having been away rather than as work.

    None until two samples on one stretch exist, because one timestamp is a
    when and not a duration. A bar with no rate yet prints without one.
    """
    intervals = [
        later - earlier
        for earlier, later in zip(completions, completions[1:], strict=False)
        if later - earlier <= gap
    ]
    if not intervals:
        return None
    return sum(intervals, timedelta()) / len(intervals)


class PhaseProgress(BaseModel, frozen=True):
    """One phase's iterator, as far through it as the run has got.

    Per phase rather than one figure for the run, because the phases do not
    share a rate: a join is a merge and a verification, while the re-check
    that follows the last of them is a reviewer turn per concern. One
    estimate spanning both would be wrong for whichever phase it was not
    measured on, and wrong most of the time.
    """

    label: str
    done: int
    total: int
    per_item: timedelta | None = None
    last_completed_at: datetime | None = None
    """When the most recent item landed, where the phase samples one.

    Carried so an estimate can account for the item in flight. Without it
    the figure is a whole number of items however long the current one has
    already run, and a reader watching it between two landings sees a
    countdown that does not count down.
    """

    def remaining(
        self, active: bool = True, at: datetime | None = None
    ) -> timedelta | None:
        """How long the rest of this phase should take at the observed rate.

        The item in flight is discounted by however long it has already run,
        so the estimate falls between landings rather than holding still and
        then dropping by a whole item. Never below zero of it: an item
        overrunning the mean says the mean is optimistic, not that the work
        left is negative, so the discount stops at one item's worth.

        Only while the run is ``active``. A parked run is not working on the
        item, so the wall-clock since its last landing is not progress
        through it, and discounting that would count a weekend as work done.
        """
        if not self.per_item or self.done >= self.total:
            return None
        estimate = self.per_item * (self.total - self.done)
        if not active or self.last_completed_at is None:
            return estimate
        in_flight = max(
            (at if at is not None else utc_now()) - self.last_completed_at, timedelta()
        )
        return estimate - min(in_flight, self.per_item)

    def render(
        self, width: int = 16, shades: str = BAR_SHADES, active: bool = True
    ) -> str:
        """The bar, its count, and the two figures a reader plans around.

        The cells are tqdm's and the durations are ours, which is the split
        that survives asking what each is good at. Placing a bar's fill is
        fiddly — a gradient, and a partial cell where a count falls between
        two — and rounding it by hand to whole cells loses the only
        movement a reader sees between two settlements. Saying how long is
        not fiddly, and tqdm says `131.00s/it` where a reader says `2m11s`.

        Nothing is asked of tqdm that needs a rate, so it is handed none: its
        own figure is items over wall-clock elapsed, which for a run that
        parks and resumes is exactly the number :func:`elapsed_per_item`
        exists to avoid. `elapsed` is zero for the same reason — no segment
        drawn here reads it.
        """
        eta = self.remaining(active=active)
        cells = tqdm.format_meter(
            n=self.done,
            total=self.total,
            elapsed=0,
            ascii=shades,
            bar_format=f"{{bar:{width}}}",
        )
        return " · ".join(
            [
                f"{cells} {self.done}/{self.total}",
                *([f"{compact_interval(self.per_item)}/it"] if self.per_item else []),
                *([f"ETA {compact_interval(eta)}"] if eta is not None else []),
            ]
        )


def compact_interval(span: timedelta) -> str:
    """A duration as a reader says it out loud, to two units at most.

    Kept rather than surrendered to tqdm's `MM:SS`, which is ambiguous at a
    glance about which unit it stopped at and reads a pace as `131.00s/it`
    where a person says two minutes eleven.
    """
    seconds = int(span.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


class RunStatus(BaseModel, frozen=True):
    """Where a run stands, and whether anything is driving it."""

    run_id: str
    exists: bool
    held: bool
    """Whether a process holds the run lock — the liveness answer."""

    phase: ResolvePhase | None = None
    counts: list[StatusCount] = []
    settled_concerns: int | None = None
    """How many concerns are done being decided, as the run's tally counts it.

    None where the projection was assembled without one, which is what
    :meth:`settled_count` falls back for. Carried rather than summed out of
    ``counts`` by each reporter, because the tally counts a concern settled
    once it has been stamped and a per-status breakdown has no stamp in it.

    Named for the concerns rather than sharing :meth:`settled`'s word, which
    asks whether the run itself has come to rest.
    """
    unanswered: int = 0
    host_wait: HostWait | None = None
    last: LastRecorded | None = None
    progress: PhaseProgress | None = None
    """The current phase's iterator, where the phase has one worth drawing."""

    def settled_count(
        self, settled: tuple[ConcernStatus, ...] = SETTLED_STATUSES
    ) -> int:
        """How many concerns are done being decided, however the caller has it.

        Where the line falls is the caller's, which is why it arrives as an
        argument: the default deliberately excludes ``integrating``, and a
        reader who counts assembly as finished is not wrong, only counting
        something else.

        The tally's figure where the projection carries one, because only it
        knows which concerns have been stamped and so only it stays put when
        assembly moves verified work to ``integrating``. Where it does not,
        the statuses are all there is, and summing them is the same figure
        the tally would give for every concern that has not moved back out
        of a settled status — right for a finished run, and low by the
        concerns in flight for a live one.

        Degrading to the weaker answer rather than to nothing, because a
        projection built by hand reports on a run either way, and a zero
        beside a total of four reads as a run that has achieved nothing.
        """
        if self.settled_concerns is not None:
            return self.settled_concerns
        return sum(count.concerns for count in self.counts if count.status in settled)

    def verdict(self) -> str:
        """Whether anything is driving this run, in the words to say it in.

        The held lock is the fact; quiet time is context on top of it, never
        the verdict on its own. Judging by silence produced a confident
        wrong "it crashed" about a run that was mid-turn, because a long
        model turn looks exactly like a stall from outside.

        Short enough to sit at the end of a line that already carries the
        phase, so the phase is not named twice.
        """
        if not self.exists:
            return "no such run under this project's .lup/resolve"
        if self.held and self.host_wait is not None:
            return f"running, host {self.host_wait.mode} until {self.host_wait.retry_at.isoformat()}: {self.host_wait.cause}"
        if self.phase is None:
            return "initializing" if self.held else "stopped before initialization"
        if not self.held:
            return "stopped"
        if self.last is None:
            return "running"
        return f"running, last {int(self.last.quiet_for().total_seconds())}s ago"

    def watched(self) -> str:
        """The part of this projection a watch reports a change in.

        Deliberately not the last journal entry. A run records tens of
        thousands of events, so watching that field would emit on each one
        and drown the four facts a reader is actually waiting for — the
        phase moving, a concern changing status, a question arriving, and
        the run stopping.
        """
        return "|".join(
            [
                str(self.held),
                str(self.phase),
                str(self.unanswered),
                str(self.host_wait),
                # The phase's own progress is one of the four facts a reader
                # waits on, and the statuses above move for neither phase that
                # has it: every concern is already `integrating` and stays
                # there through the last join and every re-check after it.
                str(self.progress.done if self.progress else 0),
                *(f"{count.status}={count.concerns}" for count in self.counts),
            ]
        )

    def settled(self, running_yet: bool) -> bool:
        """Whether nothing more will happen until somebody acts.

        A watch that outlives what it watches is the loop to avoid, so
        it ends where a reader has to do something: a terminal phase, or a
        run whose lock nobody holds — a park, which is waiting on an answer.

        ``running_yet`` is what keeps a watch from ending on the run it was
        started for. A detached run is spawned and returns immediately, so
        for the seconds its interpreter takes to start there is no lock to
        hold and an unheld run is indistinguishable from a parked one. The
        caller answers whether that window has closed — by seeing the lock
        held, or by waiting long enough that an unheld run is not one that
        is still starting. Both, because either alone is wrong in one
        direction: a flag alone spins forever on a run parked before the
        watch began, and a timer alone ends a slow start.

        It gates the phase for the same reason it gates the lock. Until the
        resuming process persists one, the phase on disk is whatever the
        last one left — and what a resume is most often started from is a
        terminal phase, because failing is what stopped the run. Read inside
        the startup window that reads as finished, so a watch armed on a
        just-relaunched run announced the failure it was resuming from and
        ended, having never polled once.
        """
        if not self.exists:
            return True
        if not running_yet:
            return False
        if self.phase is not None and self.phase.terminal():
            return True
        return not self.held


class RunSummary(BaseModel, frozen=True):
    """One run on disk, as far as choosing between them needs to know."""

    run_id: str
    phase: ResolvePhase
    held: bool
    last: datetime | None = None

    def line(self) -> str:
        """This run as one row of the list a chooser is shown."""
        when = f"{self.last:%Y-%m-%d %H:%M}Z" if self.last is not None else "never"
        alive = "running" if self.held else "stopped"
        return f"{self.run_id}  {self.phase}  {alive}  last {when}"


def unfinished_runs(state_root: Path) -> list[RunSummary]:
    """Every run here that is neither finished nor abandoned, newest first.

    A run keyed to the commit it started from gets a different id at every
    later commit, so "the run for this project" is not a question an id can
    answer — asking the directory is the only way to find one still owed
    something. Failed counts as unfinished: a resume re-enters from the phase
    the failure stopped at, which is the whole reason failing is not the end
    of a run.
    """
    if not state_root.is_dir():
        return []
    summaries = [
        RunSummary(
            run_id=directory.name,
            phase=state.phase,
            held=repository.held(),
            last=None if entry is None else entry.at,
        )
        for directory in sorted(state_root.iterdir())
        if directory.is_dir()
        for repository in [ResolverStateRepository(state_root, directory.name)]
        if repository.exists()
        for state in [repository.load()]
        if state.phase not in {ResolvePhase.COMPLETE, ResolvePhase.ABORTED}
        for entry in [journal_tail(repository.root)]
    ]
    # Sorted on the stamp rather than the datetime so a run that has recorded
    # nothing sorts last without a sentinel date standing in for "never".
    return sorted(
        summaries,
        key=lambda summary: summary.last.isoformat() if summary.last else "",
        reverse=True,
    )


def phase_progress(state: ResolveState, run_dir: Path) -> PhaseProgress | None:
    """The iterator the current phase is working through, where it has one.

    A phase earns a bar by knowing both how many items it faces and when each
    one landed. Each of these drives one item at a time and records the moment
    it finished: the joins and the re-checks against their own desks, the
    workers against the stamp a settling concern now carries.

    The worker phases are the long ones and the reason the bar exists, but
    they are also the loosest — several concerns are in flight at once, so a
    rate here is the mean interval between settlements rather than the cost of
    one concern, and it speeds up as leases free rather than holding steady.
    That is the figure a reader planning around "how long until this run is
    done" actually wants, which is why it is the one drawn.
    """
    if state.phase == ResolvePhase.VERIFICATION:
        return recheck_bar(state, run_dir)
    if state.phase.settling():
        return worker_bar(state.tally())
    return join_bar(state.join_progress, run_dir)


def worker_bar(tally: RunTally) -> PhaseProgress | None:
    """How far the run has got through settling its concerns.

    Counted from the tally rather than recomputed here, so this bar and the
    supervisor's own header answer "how far along" with one number. Every
    concern that reached a terminal state counts, however it ended: measured
    against only the ones that produced work the fraction would stop short by
    each concern retired or found ineligible, and a figure that can never
    complete teaches a reader to stop believing it.

    The samples are shorter than the count whenever a concern settled without
    a stamp — an older run, or a path that moved a status without going
    through the transition that stamps one. That costs the ETA its precision
    and the fraction nothing, which is the safe direction to degrade in.
    """
    if not tally.total:
        return None
    return PhaseProgress(
        label="settled",
        done=tally.settled,
        total=tally.total,
        per_item=elapsed_per_item(tally.settled_at),
        last_completed_at=max(tally.settled_at, default=None),
    )


def tally_bar(tally: RunTally) -> PhaseProgress | None:
    """The iterator one persisted aggregate can account for on its own.

    The same choice :func:`phase_progress` makes, for a reader that holds a
    tally and no run directory. Every phase it cannot answer from the tally
    alone returns nothing rather than a bar measured on the wrong thing: the
    re-check counts records under a desk, which is not in here.
    """
    if tally.phase.settling():
        return worker_bar(tally)
    if tally.phase == ResolvePhase.INTEGRATION:
        return join_tally_bar(tally)
    return None


def join_tally_bar(tally: RunTally) -> PhaseProgress | None:
    """The join sequence as far as resolver state alone records it.

    The counterpart to :func:`join_bar` for a reader without the run
    directory — the console observer, which is handed a tally per change and
    nothing else. Both figures come off the planned set for the reason that
    one gives, the tally having already reduced it to two counts.

    The rate is sound here only because the sequence clears its progress
    when the phase opens: ``completions`` accumulate across every join a run
    performs, so before that clear this would have been timing the
    worker-phase dependency joins against the integration ones. What it
    cannot do is fall back to the checkpoint the way :func:`join_bar` does,
    so a resumed run reports no rate until it has timed two of its own
    parents.
    """
    if not tally.join_total:
        return None
    return PhaseProgress(
        label="joins",
        done=tally.joined,
        total=tally.join_total,
        per_item=elapsed_per_item(tally.join_completions),
        last_completed_at=max(tally.join_completions, default=None),
    )


def join_bar(progress: JoinProgress | None, run_dir: Path) -> PhaseProgress | None:
    """How far the join sequence has got.

    Read from both records the sequence writes, because neither is complete
    on its own. The merger drives a whole join inside one turn, so the
    orchestrator's copy does not move until the turn returns and a reader
    watching it sees nothing for the length of the phase; the checkpoint
    ``land_parent`` writes is current between two parents but starts empty
    for a run whose earlier joins a previous sequence recorded. Their union
    is what has landed, so the count can never go backwards.

    Both figures come off the planned set, so the bar cannot pass its own
    end: the total is how many parents were planned, and the count is how
    many of *those* have landed. A landing outside the plan is real work —
    a parent already in the tree from an earlier run is recorded as it is
    swept — but it is not progress through this plan, and counting it
    against a total kept separately is how a bar comes to read six of five.

    The rate is the checkpoint's own landings and nothing else. The
    orchestrator's completions accumulate across every join a run performs,
    so a worker-phase dependency join was estimating the integration joins
    that followed it — 24m19s an item against the five minutes they actually
    took. A phase that has not yet timed two of its own parents reports no
    rate, which is the same bargain :func:`recheck_bar` makes and the reason
    verification was the one phase estimating accurately.
    """
    desks = JoinDesk.active(run_dir)
    checkpoints = [(desk, desk.progress()) for desk in desks]
    planned = {
        ("integration", commit) for commit in (progress.planned if progress else [])
    }
    landed = {
        ("integration", commit) for commit in (progress.joined if progress else [])
    }
    for desk, checkpoint in checkpoints:
        plan = desk.plan()
        planned.update((desk.concern_id, commit) for commit in checkpoint.planned)
        if plan is not None:
            planned.update((desk.concern_id, tip.commit) for tip in plan.tips)
        landed.update((desk.concern_id, commit) for commit in checkpoint.joined)
    if not planned:
        return None
    completions = sorted(
        datetime.fromisoformat(landing.at)
        for _desk, checkpoint in checkpoints
        for landing in checkpoint.landings
        if landing.merged and landing.at
    )
    return PhaseProgress(
        label="joins",
        done=len(planned & landed),
        total=len(planned),
        per_item=elapsed_per_item(completions),
        last_completed_at=max(completions, default=None),
    )


def recheck_bar(state: ResolveState, run_dir: Path) -> PhaseProgress | None:
    """How far the final re-check has got through the concerns it examines.

    The statuses cannot answer this. Every concern the re-check faces is
    already ``integrating`` and stays there until the phase ends, so a reader
    watching the tally sees one figure for the length of the phase and then a
    jump — which is the shape a wedged run has, and exactly the judgement the
    liveness verdict exists to spare them.

    Counted against the commit examined, because that is what makes a record
    reusable: a tree reassembled from different parents is a different
    question, and the records of the tree before it are not progress through
    this one.
    """
    examined = state.integration.commit if state.integration else None
    facing = {
        item.concern_id
        for item in state.progress
        if item.status == ConcernStatus.INTEGRATING
    }
    if not examined or not facing:
        return None
    records = [
        record
        for record in RecheckDesk(run_dir).examined(examined)
        if record.concern_id in facing
    ]
    return PhaseProgress(
        label="re-checks",
        done=len(records),
        total=len(facing),
        per_item=elapsed_per_item(
            sorted(datetime.fromisoformat(record.at) for record in records if record.at)
        ),
    )


def run_status(repository: ResolverStateRepository, run_id: str) -> RunStatus:
    """Everything the run directory can say about where this run stands."""
    held = repository.held()
    host_wait = HostWaitStore(repository.root).read() if held else None
    if not repository.root.is_dir():
        return RunStatus(run_id=run_id, exists=False, held=held)
    if not repository.exists():
        return RunStatus(run_id=run_id, exists=True, held=held, host_wait=host_wait)
    state = repository.load()
    tally = Counter(item.status for item in state.progress)
    entry = journal_tail(repository.root)
    answered = {
        answer.question_id
        for answer in (state.answers.answers if state.answers else [])
    }
    return RunStatus(
        run_id=run_id,
        exists=True,
        held=held,
        phase=state.phase,
        host_wait=host_wait,
        counts=[
            StatusCount(status=status, concerns=count)
            for status, count in sorted(
                tally.items(), key=lambda pair: (-pair[1], pair[0])
            )
        ],
        settled_concerns=state.tally().settled,
        unanswered=sum(
            1
            for question in (state.questions.questions if state.questions else [])
            if question.id not in answered
            and question.id not in state.retired_questions
        ),
        progress=phase_progress(state, repository.root),
        last=None
        if entry is None
        else LastRecorded(
            event=entry.event.type,
            actor=f"{entry.actor.kind}:{entry.actor.id}",
            at=entry.at,
        ),
    )
