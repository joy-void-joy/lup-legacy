"""Answering a run's liveness from its directory, where /proc cannot be read."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from lup.channels.models import local_stamp, utc_now
from lup.channels.stream import Stream
from lup.resolver.join_desk import JoinDesk, JoinLanding
from lup.harness.models import ResolveSpec, SkillInvocation
from lup.resolver.models import (
    SETTLED_STATUSES,
    AcceptanceCriterion,
    Concern,
    ConcernProgress,
    ConcernStatus,
    IntegrationRecord,
    JoinProgress,
    ResolvePhase,
    ResolveState,
    RunTally,
    SourceSnapshot,
)
from lup.resolver.recheck_desk import RecheckDesk, RecheckRecord
from lup.resolver.state import ResolverStateRepository
from lup.resolver.status import (
    LastRecorded,
    PhaseProgress,
    RunStatus,
    StatusCount,
    elapsed_per_item,
    join_bar,
    join_tally_bar,
    phase_progress,
    recheck_bar,
    run_status,
    tally_bar,
    worker_bar,
)
from lup.devtools.harness.resolve import ConsoleResolverObserver
from lup.devtools.supervisor.doors import report_status, status_header


def test_a_bar_reports_the_rate_of_the_stretches_actually_worked() -> None:
    """A resume separates two samples by however long nobody was driving it.

    This run holds an interval of twenty-eight hours between two joins a
    minute of work apart. Averaged in, one such gap makes every later
    estimate meaningless — an ETA of days for eight joins of about a minute.
    """
    start = utc_now()
    worked = timedelta(minutes=1)
    away = timedelta(hours=28)
    samples = [
        start,
        start + worked,
        start + worked + away,
        start + worked + away + worked,
    ]

    assert elapsed_per_item(samples) == worked


def test_a_bar_has_no_rate_until_two_samples_share_a_stretch() -> None:
    """One timestamp is a when, not a duration, and neither is a lone gap."""
    start = utc_now()

    assert elapsed_per_item([]) is None
    assert elapsed_per_item([start]) is None
    assert elapsed_per_item([start, start + timedelta(hours=28)]) is None


def test_a_bar_without_a_rate_still_draws_its_count() -> None:
    """The first item of a phase has nothing to estimate from, and says so."""
    rendered = PhaseProgress(label="joins", done=0, total=13).render(width=4)

    assert rendered == "░░░░ 0/13"


def test_a_bar_carries_the_two_figures_a_reader_plans_around() -> None:
    """Both figures in the spelling a reader says out loud, on a finer bar.

    The durations stay ours — tqdm would write `131.00s/it` and `17:28` for
    these — while the cells become tqdm's, which is where the bar gains a
    partial: five of thirteen is a hair over an eighth of the way through
    four cells, which `█▌` says and a rounded `██` did not.
    """
    fifth = PhaseProgress(
        label="joins", done=5, total=13, per_item=timedelta(minutes=2, seconds=11)
    )

    assert fifth.render(width=4) == "█▌░░ 5/13 · 2m11s/it · ETA 17m28s"


def test_a_finished_bar_estimates_nothing_further() -> None:
    """Nothing remains, so an ETA would be a number about no work."""
    done = PhaseProgress(
        label="joins", done=13, total=13, per_item=timedelta(minutes=2)
    )

    assert done.remaining() is None
    assert "ETA" not in done.render()


def test_an_estimate_counts_down_through_the_item_in_flight() -> None:
    """Otherwise the figure holds still between two landings.

    A whole number of items however long the current one has already run
    reads as a countdown that does not count down, which is the shape a
    wedged run has.
    """
    landed = utc_now()
    minutes = timedelta(minutes=2)
    bar = PhaseProgress(
        label="joins", done=11, total=13, per_item=minutes, last_completed_at=landed
    )

    assert bar.remaining(at=landed) == minutes * 2
    assert bar.remaining(at=landed + timedelta(minutes=1)) == timedelta(minutes=3)


def test_an_overrunning_item_does_not_estimate_less_than_the_work_left() -> None:
    """The mean was optimistic; the work remaining is still whole items.

    Discounting the full overrun would walk the estimate to zero and then
    below it, saying a phase with two parents left to merge is done.
    """
    landed = utc_now()
    minutes = timedelta(minutes=2)
    bar = PhaseProgress(
        label="joins", done=11, total=13, per_item=minutes, last_completed_at=landed
    )

    assert bar.remaining(at=landed + timedelta(hours=3)) == minutes


def test_a_parked_run_is_not_working_through_the_item_in_flight() -> None:
    """Wall-clock while nobody drives the run is not progress through it.

    Discounted anyway, a weekend parked reads as a weekend of work done and
    the run reports itself nearly finished the moment it is resumed.
    """
    landed = utc_now()
    minutes = timedelta(minutes=2)
    bar = PhaseProgress(
        label="joins", done=11, total=13, per_item=minutes, last_completed_at=landed
    )

    assert bar.remaining(active=False, at=landed + timedelta(days=2)) == minutes * 2


PLANNED = [f"{index:040d}" for index in range(13)]
"""The thirteen parents these joins are planned against, by commit.

Shared so a landing and the plan it belongs to name the same parent: the
count of joins done is the planned set's own members, so a test whose
landings fell outside it would be measuring nothing.
"""


def absorbed(joined: list[str], planned: list[str]) -> JoinProgress:
    """What the orchestrator has written back, as of its last turn."""
    return JoinProgress(joined=joined, commit="b" * 40, planned=planned)


def test_the_bar_moves_while_the_join_turn_is_still_running(tmp_path: Path) -> None:
    """The merger drives a whole join inside one turn, so nothing else does.

    The orchestrator's copy is written when the turn returns. Watched alone
    it sits still for the length of the phase, which is the same shape a
    wedged run has — and the guidance sends a reader to this surface
    precisely so they do not have to judge by silence.
    """
    desk = JoinDesk(tmp_path, "integration")
    for index in range(10):
        desk.record(JoinLanding(commit=f"{index:040d}", head="c" * 40), planned=PLANNED)

    progress = join_bar(absorbed(["0" * 40], planned=PLANNED), tmp_path)

    assert progress is not None
    assert progress.done == 10
    assert progress.total == 13


def test_the_bar_never_goes_backwards_when_a_turn_starts(tmp_path: Path) -> None:
    """A resumed run's checkpoint is empty until its merger lands something.

    Reading it alone would report a run mid-integration as having joined
    nothing, which is worse than the staleness it fixes.
    """
    earlier = [f"{index:040d}" for index in range(8)]

    progress = join_bar(absorbed(earlier, planned=PLANNED), tmp_path)

    assert progress is not None
    assert progress.done == 8


def test_a_parent_recorded_without_a_merge_does_not_set_the_rate(
    tmp_path: Path,
) -> None:
    """Sweeping what an earlier run landed times no work this one did.

    A resume records those in seconds, and a rate averaged over them
    promises an ETA the joins remaining will not come close to.
    """
    desk = JoinDesk(tmp_path, "integration")
    for index in range(4):
        desk.record(
            JoinLanding(commit=f"{index:040d}", head="c" * 40, merged=False),
            planned=PLANNED,
        )

    progress = join_bar(absorbed([], planned=PLANNED), tmp_path)

    assert progress is not None
    assert progress.done == 4
    assert progress.per_item is None


def test_a_landing_outside_the_plan_cannot_take_the_bar_past_its_end(
    tmp_path: Path,
) -> None:
    """Never ``joins 6/5``, with the bar drawn past its end.

    A parent already in the tree is swept and recorded without having been
    planned on its own, so landings outnumber the plan. Counted against a
    total kept as its own figure, the sixth landing of five planned parents
    would put the count past the end of the bar — and a bar past its end
    reads as something having gone wrong in a run that is entirely healthy.
    """
    plan = PLANNED[:5]
    desk = JoinDesk(tmp_path, "integration")
    for commit in [*plan, "f" * 40]:
        desk.record(JoinLanding(commit=commit, head="c" * 40), planned=plan)

    progress = join_bar(absorbed(["f" * 40], planned=plan), tmp_path)

    assert progress is not None
    assert (progress.done, progress.total) == (5, 5)
    assert progress.done <= progress.total


def test_the_join_rate_ignores_the_completions_carried_from_another_phase(
    tmp_path: Path,
) -> None:
    """An ETA is only worth reading where the phase measured it itself.

    Integration opened reporting ``24m19s/it · ETA 1h12m`` on
    resolve-4997351bbef0, a rate inherited from a worker-phase dependency
    join. Its own joins took about five minutes each and the phase finished
    in twenty-five, so the estimate was out by roughly three times — and an
    ETA that wrong invites abandoning a run that is nearly done.

    One landing of its own is not a duration, so this reports no rate rather
    than the wrong one, which is the bargain the re-check bar already makes.
    """
    stale = utc_now()
    carried = JoinProgress(
        joined=[],
        commit="b" * 40,
        planned=PLANNED,
        completions=[
            stale,
            stale + timedelta(minutes=24),
            stale + timedelta(minutes=48),
        ],
    )
    desk = JoinDesk(tmp_path, "integration")
    desk.record(JoinLanding(commit=PLANNED[0], head="c" * 40), planned=PLANNED)

    progress = join_bar(carried, tmp_path)

    assert progress is not None
    assert progress.done == 1
    assert progress.per_item is None


def verifying(concerns: list[str], examined: str | None) -> ResolveState:
    """A run in the phase that re-checks every concern it integrated."""
    return ResolveState(
        config_digest="config-sha",
        run_id="run-1",
        phase=ResolvePhase.VERIFICATION,
        source=SourceSnapshot(branch="dev", commit="source-sha"),
        spec=ResolveSpec(
            id="resolve",
            worker_identity="resolver-worker",
            worker_skill=SkillInvocation(plugin="lup", skill="worker"),
            review_skill=SkillInvocation(plugin="lup", skill="review"),
            merge_skill=SkillInvocation(plugin="lup", skill="merge"),
        ),
        concerns=[
            Concern(
                id=name,
                title=name,
                spec=f"Resolve {name}",
                criteria=[AcceptanceCriterion(id=f"{name}-done", description="done")],
            )
            for name in concerns
        ],
        progress=[
            ConcernProgress(concern_id=name, status=ConcernStatus.INTEGRATING)
            for name in concerns
        ],
        integration=IntegrationRecord(
            branch="review",
            worktree=Path("integration"),
            concerns=concerns,
            commit=examined,
        ),
    )


def test_the_bar_moves_while_every_concern_is_still_integrating(
    tmp_path: Path,
) -> None:
    """The re-check phase changes no status until the last concern lands.

    A reader watching the tally sees one figure for the length of the phase
    and then a jump, which is the shape a wedged run has — and the guidance
    sends them to this surface precisely so they need not judge by silence.
    The reviewers write a record each as they finish, so the phase knows both
    figures a bar is owed.
    """
    facing = [f"concern-{index}" for index in range(21)]
    desk = RecheckDesk(tmp_path)
    for name in facing[:4]:
        desk.record(RecheckRecord(concern_id=name, commit="b" * 40))

    progress = phase_progress(verifying(facing, "b" * 40), tmp_path)

    assert progress is not None
    assert (progress.label, progress.done, progress.total) == ("re-checks", 4, 21)


def test_a_recheck_of_another_tree_is_not_progress_through_this_one(
    tmp_path: Path,
) -> None:
    """Repairing the merged tree is what a stop-on-defects run asks for.

    Every record the tree before the repair earned names that commit, and
    counting them would report a phase as most of the way through the moment
    it started over.
    """
    facing = [f"concern-{index}" for index in range(21)]
    desk = RecheckDesk(tmp_path)
    for name in facing[:4]:
        desk.record(RecheckRecord(concern_id=name, commit="a" * 40))

    progress = phase_progress(verifying(facing, "b" * 40), tmp_path)

    assert progress is not None
    assert progress.done == 0


def test_a_verification_with_nothing_examined_earns_no_bar(tmp_path: Path) -> None:
    """An integration with no commit has nothing a record could be keyed to."""
    assert recheck_bar(verifying(["a"], None), tmp_path) is None


def worker_tally(statuses: list[ConcernStatus], stamps: list[datetime]) -> RunTally:
    """A worker phase that has settled some of what it faces."""
    return RunTally(
        phase=ResolvePhase.WORKERS,
        total=len(statuses),
        by_status={
            status: statuses.count(status) for status in dict.fromkeys(statuses)
        },
        joined=0,
        join_total=0,
        settled=len([status for status in statuses if status in SETTLED_STATUSES]),
        settled_at=stamps,
    )


def test_a_concern_that_failed_still_counts_as_settled() -> None:
    """However a concern ended, it is done being decided.

    Counted against only the ones that produced work, the fraction would stop
    short by every concern retired or found ineligible — and a bar that can
    never reach its own end teaches a reader to stop believing it.
    """
    bar = worker_bar(
        worker_tally(
            [
                ConcernStatus.VERIFIED,
                ConcernStatus.FAILED,
                ConcernStatus.RETIRED,
                ConcernStatus.RUNNING,
            ],
            [],
        )
    )

    assert bar is not None
    assert (bar.done, bar.total) == (3, 4)


def test_a_concern_assembly_moved_to_integrating_stays_settled() -> None:
    """A settled count may not fall, and the lifecycle moves work backwards.

    A run saying ``10/11 settled`` through the worker phase, whose assembly
    moves nine verified concerns to ``integrating``, would say ``2/11`` on the
    next line. Nothing has gone wrong — yet a reader watching the surface the
    guidance says to judge a run by sees progress collapse.

    The stamp is what carries the fact across the move: written the first time
    a concern reaches a settled status and never cleared, so a status that has
    left the settled set cannot take the count with it. ``verified`` to
    ``eligible`` on rework has the same shape, which is why this counts by the
    stamp rather than by adding one more status to the settled list.
    """
    landed = utc_now()
    state = verifying(["a", "b", "c"], "integration-sha")
    assembled = state.model_copy(
        update={
            "progress": [
                item.model_copy(update={"settled_at": landed if stamped else None})
                for item, stamped in zip(state.progress, [True, True, False])
            ]
        }
    )

    assert all(item.status == ConcernStatus.INTEGRATING for item in assembled.progress)
    assert ConcernStatus.INTEGRATING not in SETTLED_STATUSES
    assert assembled.tally().settled == 2


def test_a_caller_may_count_integrating_as_finished() -> None:
    """The settled line is the caller's to redraw, and one redraw reaches both.

    The table's own note offers ``integrating`` as the line a reader could
    reasonably draw elsewhere, and passing it here is the whole of that
    redraw. The bar a run prints and the supervisor's header both take their
    numerator from this fold, so neither is left counting by the default
    while the other counts by the argument.
    """
    state = verifying(["a", "b", "c"], "integration-sha")

    assert state.tally().settled == 0
    assert state.tally((*SETTLED_STATUSES, ConcernStatus.INTEGRATING)).settled == 3


def test_a_redrawn_line_cannot_take_back_a_concern_that_already_landed() -> None:
    """Narrowing reaches work still in flight, never work already stamped.

    The stamp is written once and never cleared, which is what keeps the
    count from falling when the lifecycle moves settled work backwards. A
    caller who drops a status from the line inherits that: the concerns that
    landed under it stay counted, and only the ones that have not landed yet
    follow the narrower line.
    """
    landed = utc_now()
    state = verifying(["a", "b"], "integration-sha")
    mixed = state.model_copy(
        update={
            "progress": [
                item.model_copy(
                    update={
                        "status": ConcernStatus.VERIFIED,
                        "settled_at": landed if stamped else None,
                    }
                )
                for item, stamped in zip(state.progress, [True, False])
            ]
        }
    )
    working = tuple(
        status for status in SETTLED_STATUSES if status is not ConcernStatus.VERIFIED
    )

    assert mixed.tally().settled == 2
    assert mixed.tally(working).settled == 1


def test_the_settled_bar_takes_its_rate_from_the_stamps() -> None:
    """The samples are the moments concerns landed, not the count of them."""
    start = utc_now()
    minutes = timedelta(minutes=3)
    bar = worker_bar(
        worker_tally(
            [ConcernStatus.VERIFIED, ConcernStatus.VERIFIED, ConcernStatus.RUNNING],
            [start, start + minutes],
        )
    )

    assert bar is not None
    assert bar.per_item == minutes
    assert bar.remaining() == minutes


def test_a_settled_concern_with_no_stamp_costs_the_rate_and_not_the_count() -> None:
    """An older run, or a path that moved a status without stamping it.

    The count is read from the status and the rate from the stamps, so the
    fraction stays exact where the estimate degrades — which is the safe
    direction for the two to disagree in.
    """
    bar = worker_bar(
        worker_tally([ConcernStatus.VERIFIED, ConcernStatus.VERIFIED], [utc_now()])
    )

    assert bar is not None
    assert bar.done == 2
    assert bar.per_item is None


def test_the_console_leads_with_the_bar_and_keeps_the_breakdown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two questions, one line: how much is left, and what it is doing.

    The bar answers the first and the per-status breakdown the second, and
    this line is the only place either is asked during a long worker phase.
    """
    start = utc_now()
    ConsoleResolverObserver().tally_changed(
        worker_tally(
            [ConcernStatus.VERIFIED, ConcernStatus.VERIFIED, ConcernStatus.RUNNING],
            [start, start + timedelta(minutes=3)],
        )
    )

    printed = capsys.readouterr().out.strip()

    assert printed == (
        "[resolve] progress: settled ██████████▋░░░░░ 2/3 · 3m00s/it · ETA 3m00s"
        " · verified 2 · running 1 of 3"
    )


def test_the_console_outside_a_settling_phase_prints_the_breakdown_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No bar to lead with, so the breakdown stands alone."""
    integrating = worker_tally([ConcernStatus.INTEGRATING], []).model_copy(
        update={"phase": ResolvePhase.INTEGRATION}
    )

    ConsoleResolverObserver().tally_changed(integrating)

    assert capsys.readouterr().out.strip() == ("[resolve] progress: integrating 1 of 1")


def join_tally(joined: int, total: int, stamps: list[datetime]) -> RunTally:
    """An integration phase partway through the parents it plans to merge."""
    return RunTally(
        phase=ResolvePhase.INTEGRATION,
        total=1,
        by_status={ConcernStatus.INTEGRATING: 1},
        joined=joined,
        join_total=total,
        settled=0,
        join_completions=stamps,
    )


def test_the_integration_phase_draws_its_joins_as_a_bar() -> None:
    """The phase's unit of work is a parent, and it has both figures for one.

    Read from the tally alone because that is all the console observer is
    handed — :func:`join_bar` answers the same question for a reader that
    also holds the run directory.
    """
    start = utc_now()
    minutes = timedelta(minutes=2)
    bar = tally_bar(join_tally(5, 13, [start, start + minutes]))

    assert bar is not None
    assert (bar.label, bar.done, bar.total) == ("joins", 5, 13)
    assert bar.per_item == minutes


def test_a_phase_the_tally_cannot_account_for_draws_nothing() -> None:
    """The re-check counts records under a desk, which a tally does not hold.

    A bar measured on the concerns instead would be counting a unit this
    phase does not work in, which is worse than showing none.
    """
    verifying = join_tally(5, 13, []).model_copy(
        update={"phase": ResolvePhase.VERIFICATION}
    )

    assert tally_bar(verifying) is None


def test_joins_the_state_has_not_recorded_yet_earn_no_bar() -> None:
    """Nothing planned is nothing to draw a fraction against."""
    assert join_tally_bar(join_tally(0, 0, [])) is None


def test_the_console_shows_the_joins_once_it_draws_them_as_a_bar(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same fraction twice on one line is the line reading as two runs.

    The breakdown carries the joins inline wherever no bar covers them, so
    the one that draws a bar drops the fragment rather than repeating it.
    """
    start = utc_now()
    ConsoleResolverObserver().tally_changed(
        join_tally(5, 13, [start, start + timedelta(minutes=2)])
    )

    printed = capsys.readouterr().out.strip()

    assert printed == (
        "[resolve] progress: joins ██████▏░░░░░░░░░ 5/13 · 2m00s/it · ETA 16m00s"
        " · integrating 1 of 1"
    )
    assert "· joins 5/13" not in printed


def test_only_a_settling_phase_draws_a_settled_bar(tmp_path: Path) -> None:
    """Integration counts joins, so the concerns are not its unit of work.

    Its iterator is the join sequence, and a run whose sequence has recorded
    nothing yet has no bar to draw rather than a settled one to fall back on.
    """
    integrating = verifying(["a"], None).model_copy(
        update={"phase": ResolvePhase.INTEGRATION}
    )

    assert phase_progress(integrating, tmp_path) is None


def test_the_desk_stamps_a_record_so_a_caller_cannot_forget_to(
    tmp_path: Path,
) -> None:
    """Writing is the moment the fact becomes true, so writing states it.

    A rate is what turns a count into an estimate, and it needs when each item
    landed. Left to the caller, one that omits it costs the whole phase its
    ETA and nothing reports the omission — the count still moves, so the bar
    looks like it works.
    """
    desk = RecheckDesk(tmp_path)

    desk.record(RecheckRecord(concern_id="a", commit="b" * 40))

    recorded = desk.recorded("a", "b" * 40)
    assert recorded is not None
    assert datetime.fromisoformat(recorded.at) <= utc_now()


def test_re_recording_keeps_the_stamp_the_first_write_gave_it(
    tmp_path: Path,
) -> None:
    """A record read back and written again is not a re-check made now."""
    desk = RecheckDesk(tmp_path)
    desk.record(RecheckRecord(concern_id="a", commit="b" * 40))
    first = desk.recorded("a", "b" * 40)
    assert first is not None

    desk.record(first)

    again = desk.recorded("a", "b" * 40)
    assert again is not None
    assert again.at == first.at


def test_a_bar_says_its_rate_once_the_records_carry_when(tmp_path: Path) -> None:
    """Two stamps on one stretch are what an ETA is derived from."""
    desk = RecheckDesk(tmp_path)
    start = utc_now()
    for index, name in enumerate(["a", "b", "c"]):
        desk.record(
            RecheckRecord(
                concern_id=name,
                commit="b" * 40,
                at=(start + timedelta(minutes=index)).isoformat(),
            )
        )

    progress = recheck_bar(verifying(["a", "b", "c", "d"], "b" * 40), tmp_path)

    assert progress is not None
    assert progress.per_item == timedelta(minutes=1)
    assert progress.remaining() == timedelta(minutes=1)


def test_an_unheld_run_reads_as_not_running(tmp_path: Path) -> None:
    repository = ResolverStateRepository(tmp_path, "quiet")
    repository.root.mkdir(parents=True)
    (repository.root / ".run.lock").write_text("", encoding="utf-8")

    assert not repository.held()


def test_a_held_run_reads_as_running(tmp_path: Path) -> None:
    """The lock is the liveness answer, and it answers across processes.

    `ps` and `pgrep` cannot: under a sandbox `/proc` is PID-isolated, so
    they list nothing outside the current shell and a healthy long-running
    run is indistinguishable from one that died.
    """
    repository = ResolverStateRepository(tmp_path, "busy")

    with repository.exclusive():
        # A second reader of the same run directory, which is what a status
        # command is. Same process here; the flock is what a separate one
        # would meet too.
        assert ResolverStateRepository(tmp_path, "busy").held()

    assert not ResolverStateRepository(tmp_path, "busy").held()


def test_a_run_that_does_not_exist_says_so_rather_than_answering(
    tmp_path: Path,
) -> None:
    """Silence and "no such run" are told apart, because one is wrong.

    A session in a worktree with no `.lup` would read an empty listing as
    "zero pending, so my answer promoted", and report that with nothing in
    the output to support it.
    """
    status = run_status(ResolverStateRepository(tmp_path, "absent"), "absent")

    assert not status.exists
    assert "no such run" in status.verdict()


def test_a_published_run_exists_before_inventory_is_persisted(tmp_path: Path) -> None:
    repository = ResolverStateRepository(tmp_path, "starting")
    repository.root.mkdir(parents=True)
    status = run_status(repository, "starting")

    assert status.exists and status.phase is None
    assert status.verdict() == "stopped before initialization"
    assert "initializing" in status_header(status)


def test_a_watch_stays_attached_while_inventory_is_being_planned(
    tmp_path: Path,
) -> None:
    repository = ResolverStateRepository(tmp_path, "planning")
    with repository.exclusive():
        status = run_status(repository, "planning")

    assert status.verdict() == "initializing"
    assert not status.settled(running_yet=True)


def test_a_log_s_last_record_is_read_without_its_whole_length(tmp_path: Path) -> None:
    """A resolver journal reaches tens of megabytes inside a single run."""
    adapter: TypeAdapter[dict[str, int]] = TypeAdapter(dict[str, int])
    stream = Stream(tmp_path / "log.jsonl", adapter)
    for index in range(500):
        stream.append({"seq": index})

    assert stream.last() == {"seq": 499}
    assert stream.last(window=64) == {"seq": 499}


def test_an_empty_log_has_no_last_record(tmp_path: Path) -> None:
    adapter: TypeAdapter[dict[str, int]] = TypeAdapter(dict[str, int])

    assert Stream(tmp_path / "missing.jsonl", adapter).last() is None


def status_at(
    phase: ResolvePhase, held: bool, unanswered: int = 0, verified: int = 0
) -> RunStatus:
    """One projection, built directly rather than through a run on disk."""
    return RunStatus(
        run_id="watched",
        exists=True,
        held=held,
        phase=phase,
        counts=[StatusCount(status=ConcernStatus.VERIFIED, concerns=verified)],
        unanswered=unanswered,
    )


def test_a_watch_reports_a_change_in_any_of_the_four_facts() -> None:
    """Phase, per-status counts, questions waiting, and the run stopping."""
    running = status_at(ResolvePhase.WORKERS, held=True, verified=3)

    assert (
        running.watched()
        != status_at(ResolvePhase.REVIEW, held=True, verified=3).watched()
    )
    assert (
        running.watched()
        != status_at(ResolvePhase.WORKERS, held=True, verified=4).watched()
    )
    assert (
        running.watched()
        != status_at(
            ResolvePhase.WORKERS, held=True, verified=3, unanswered=1
        ).watched()
    )
    assert (
        running.watched()
        != status_at(ResolvePhase.WORKERS, held=False, verified=3).watched()
    )


def test_a_watch_does_not_report_the_journal_advancing() -> None:
    """A run records tens of thousands of events; each is not a change."""
    quiet = status_at(ResolvePhase.WORKERS, held=True, verified=3)
    noisy = quiet.model_copy(
        update={
            "last": LastRecorded(
                event="message_posted", actor="worker:alpha", at=utc_now()
            )
        }
    )

    assert quiet.watched() == noisy.watched()


def test_a_watch_ends_when_the_run_parks() -> None:
    """A park is waiting on an answer, which is the reader's turn."""
    assert status_at(ResolvePhase.WORKERS, held=False).settled(running_yet=True)


def test_a_watch_ends_when_the_run_finishes() -> None:
    assert status_at(ResolvePhase.COMPLETE, held=True).settled(running_yet=True)


def test_a_watch_survives_the_seconds_before_a_detached_run_takes_its_lock() -> None:
    """Spawning returns immediately; the interpreter takes seconds to start."""
    assert not status_at(ResolvePhase.WORKERS, held=False).settled(running_yet=False)


def test_a_watch_on_a_run_parked_before_it_started_still_ends() -> None:
    """The other direction of the same window, and the one that hung.

    Waiting only for a lock that has been seen means a watch attached to an
    already-parked run never ends: it is unheld, its phase is not terminal,
    and no reading will change either. The caller closes the window on
    elapsed time as well, which is what this asks for.
    """
    parked = status_at(ResolvePhase.WORKERS, held=False)

    assert not parked.settled(running_yet=False)
    assert parked.settled(running_yet=True)


def test_a_watch_survives_the_terminal_phase_a_resume_was_started_from() -> None:
    """The phase on disk is the last process's until this one persists its own.

    A resume is most often started from a terminal phase, because failing is
    what stopped the run. Read inside the startup window that says finished,
    so a watch armed on a just-relaunched run would announce the failure it
    is resuming from and end without polling once, while the run is already
    integrating.
    """
    failed = status_at(ResolvePhase.FAILED, held=False)

    assert not failed.settled(running_yet=False)
    assert failed.settled(running_yet=True)


def test_a_watch_on_a_run_that_does_not_exist_ends_at_once() -> None:
    absent = RunStatus(run_id="absent", exists=False, held=False)

    assert absent.settled(running_yet=False)


def test_a_reading_is_dated_in_the_reader_s_own_zone() -> None:
    """A run outlasts the attention of whoever started it.

    The run's own ages say how long a worker has been quiet, which is a
    different question from how long ago the reader was last told anything —
    and a terminal shows how long a turn took, never when it ended.
    """
    now = datetime.now().astimezone()
    stamp = local_stamp()

    # Against the clock rather than against LOCAL_STAMP_FORMAT: comparing the
    # function to its own constant passes for every format string, including
    # the bare `%H:%M` that reads as today and names no zone.
    assert stamp.startswith(now.strftime("%a"))
    assert now.strftime("%H:%M") in stamp
    assert stamp.endswith(now.strftime("%Z"))


def test_the_header_carries_progress_losses_and_who_is_held_up() -> None:
    """Three facts and no fourth: how far, what was lost, who is waiting.

    A per-status breakdown is progress rather than attention — nobody acts
    on "9 retired" — and a header carrying one asks a reader to find what
    needs them among nine figures that do not.
    """
    counts = [
        StatusCount(status=ConcernStatus.VERIFIED, concerns=21),
        StatusCount(status=ConcernStatus.RETIRED, concerns=9),
        StatusCount(status=ConcernStatus.INELIGIBLE, concerns=7),
        StatusCount(status=ConcernStatus.FAILED, concerns=1),
        StatusCount(status=ConcernStatus.REVISING, concerns=1),
    ]
    working = RunStatus(
        run_id="r", exists=True, held=True, phase=ResolvePhase.WORKERS, counts=counts
    )

    assert status_header(working).endswith(
        "workers · 38/39 settled · 1 failed · running"
    )
    assert status_header(working.model_copy(update={"unanswered": 2})).endswith(
        "38/39 settled · 1 failed · 2 questions waiting · running"
    )
    assert status_header(working.model_copy(update={"held": False})).endswith("stopped")
    assert "retired" not in status_header(working)


def test_the_header_is_the_report_it_heads(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One composition, so the compact form cannot drift from the full one.

    Two renderers over the same projection is what let a flag go on saying
    what it carried after the line it described had moved on.
    """
    working = RunStatus(
        run_id="r",
        exists=True,
        held=True,
        phase=ResolvePhase.INTEGRATION,
        counts=[StatusCount(status=ConcernStatus.VERIFIED, concerns=4)],
        progress=PhaseProgress(label="joins", done=5, total=13),
    )

    report_status(working)

    printed = capsys.readouterr().out.splitlines()
    assert printed[0] == status_header(working)
    assert not any(line.startswith(status_header(working)) for line in printed[1:])


def test_the_header_says_how_much_longer_the_phase_it_is_in_should_take() -> None:
    """The figure a reader plans around, on the line they are handed."""
    joining = RunStatus(
        run_id="r",
        exists=True,
        held=True,
        phase=ResolvePhase.INTEGRATION,
        counts=[StatusCount(status=ConcernStatus.INTEGRATING, concerns=13)],
        progress=PhaseProgress(
            label="joins", done=5, total=13, per_item=timedelta(minutes=2)
        ),
    )

    assert "joins " in status_header(joining)
    assert "5/13" in status_header(joining)
    assert "ETA 16m00s" in status_header(joining)


def test_a_run_that_lost_nothing_says_nothing_about_losses() -> None:
    """The failure field is absent rather than zero, so its presence means it."""
    clean = RunStatus(
        run_id="r",
        exists=True,
        held=True,
        phase=ResolvePhase.WORKERS,
        counts=[StatusCount(status=ConcernStatus.VERIFIED, concerns=4)],
    )

    assert status_header(clean).endswith("workers · 4/4 settled · running")


def test_the_fraction_can_reach_its_own_total() -> None:
    """Counting only what produced work leaves a bar that never completes.

    Measured against the plan it stops short by every concern retired or
    found ineligible, and a progress figure that cannot finish teaches a
    reader to stop believing it.
    """
    ended = RunStatus(
        run_id="r",
        exists=True,
        held=False,
        phase=ResolvePhase.COMPLETE,
        counts=[
            StatusCount(status=ConcernStatus.VERIFIED, concerns=2),
            StatusCount(status=ConcernStatus.RETIRED, concerns=1),
            StatusCount(status=ConcernStatus.INELIGIBLE, concerns=1),
        ],
    )

    assert "4/4 settled" in status_header(ended)


def test_a_caller_wanting_another_shape_passes_one() -> None:
    """The format is this project's judgement, so a default and not a law."""
    assert local_stamp("%Y") == str(datetime.now().astimezone().year)
