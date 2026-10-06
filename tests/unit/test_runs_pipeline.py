"""What a declared pipeline does that a script of the same work cannot.

Three claims are pinned here, and they are the reason the runtime exists at
all. A step is not recomputed when nothing it rests on has changed, so a
resumed run costs only what never landed. A step *is* recomputed when its own
declaration changed, and so is everything downstream of it, without anybody
maintaining the list of what that is. And a failure stops at the steps that
read the failed one rather than taking down the run's whole record — the
units that did land stay landed, and the summary says which steps never ran.

The fourth claim is the one the monitor rests on: whatever happens, including
an exception on the way out, the directory ends up holding a manifest, a
result per landed unit, and a summary. A follower that found none of those
would be watching a run it could never report on.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lup.channels.models import utc_now
from lup.devtools.run.app import create_run_app
from lup.runs.directory import (
    CLAIM_LEASE_SECONDS,
    FILENAME_BYTES,
    WORKSPACE_ENV,
    RunDirectory,
    filed_name,
    progress_in,
)
from lup.runs.models import UnitAttempt, UnitProgress, UnitResult, UnitStatus
from lup.runs.progress import describe_summary, read_progress
from lup.runs.pipeline import (
    CallableStep,
    ComputedItems,
    FanContext,
    FixedItems,
    Pipeline,
    PipelineError,
    RunRequest,
    ShellStep,
    StepBody,
    StepContext,
    StepOutcome,
)

CALLS: dict[str, int] = {}


@pytest.fixture(autouse=True)
def forget_calls() -> None:
    """Each test counts only its own invocations."""
    CALLS.clear()


def counted(context: StepContext) -> StepOutcome:
    """Record that this step ran, and say it worked."""
    CALLS[context.step] = CALLS.get(context.step, 0) + 1
    return StepOutcome(outcome="done")


def refuses(context: StepContext) -> StepOutcome:
    """A body that always raises, for pinning what a failure does downstream."""
    CALLS[context.step] = CALLS.get(context.step, 0) + 1
    raise ValueError(f"{context.step}/{context.item} cannot be done")


def flaky(context: StepContext) -> StepOutcome:
    """Fail once, then succeed, for pinning that retries are recorded."""
    CALLS[context.step] = CALLS.get(context.step, 0) + 1
    if CALLS[context.step] < 2:
        raise ValueError("not yet")
    return StepOutcome(outcome="eventually")


def names(context: FanContext) -> list[str]:
    """Fan out over one item per unit the first step landed."""
    return [f"from-{result.item}" for result in context.dependencies["first"]]


def chain(params: str = "") -> Pipeline:
    """Three steps in a line, the middle one carrying a declared parameter."""
    return Pipeline(
        name="chain",
        steps=[
            CallableStep(id="first", body=counted),
            CallableStep(
                id="middle", dependencies=["first"], body=counted, params=params
            ),
            CallableStep(id="last", dependencies=["middle"], body=counted),
        ],
    )


def test_a_run_lands_every_step_and_records_how_it_ended(tmp_path: Path) -> None:
    summary = chain().execute(RunRequest(directory=tmp_path))
    run = RunDirectory(root=tmp_path)
    assert CALLS == {"first": 1, "middle": 1, "last": 1}
    assert summary.ok
    assert summary.landed == 3
    assert run.read_manifest() is not None
    assert run.read_summary() is not None
    assert [result.step for result in run.read().results] == [
        "first",
        "last",
        "middle",
    ]


def test_nothing_is_recomputed_when_nothing_has_changed(tmp_path: Path) -> None:
    chain().execute(RunRequest(directory=tmp_path))
    CALLS.clear()
    summary = chain().execute(RunRequest(directory=tmp_path))
    assert CALLS == {}
    assert summary.landed == 3


def test_changing_a_step_reruns_it_and_everything_downstream(tmp_path: Path) -> None:
    """And nothing upstream, which is the half a --force flag cannot know."""
    chain().execute(RunRequest(directory=tmp_path))
    CALLS.clear()
    chain(params="widened").execute(RunRequest(directory=tmp_path))
    assert CALLS == {"middle": 1, "last": 1}


def test_forcing_a_current_step_reruns_it_and_everything_downstream(
    tmp_path: Path,
) -> None:
    chain().execute(RunRequest(directory=tmp_path))
    CALLS.clear()
    chain().execute(RunRequest(directory=tmp_path, force=["middle"]))
    assert CALLS == {"middle": 1, "last": 1}


def test_only_runs_exactly_what_it_names(tmp_path: Path) -> None:
    chain().execute(RunRequest(directory=tmp_path))
    CALLS.clear()
    chain().execute(RunRequest(directory=tmp_path, only=["middle"], force=["middle"]))
    assert CALLS == {"middle": 1}


def test_from_picks_up_at_a_step_and_carries_on(tmp_path: Path) -> None:
    chain().execute(RunRequest(directory=tmp_path))
    CALLS.clear()
    chain().execute(
        RunRequest(directory=tmp_path, start_from="middle", force=["middle"])
    )
    assert CALLS == {"middle": 1, "last": 1}


def test_a_lost_result_is_the_only_thing_recomputed(tmp_path: Path) -> None:
    """A run killed halfway resumes at the units that never landed."""
    chain().execute(RunRequest(directory=tmp_path))
    RunDirectory(root=tmp_path).unit_path("last").unlink()
    CALLS.clear()
    chain().execute(RunRequest(directory=tmp_path))
    assert CALLS == {"last": 1}


def test_a_failure_skips_what_reads_it_and_keeps_what_landed(tmp_path: Path) -> None:
    pipeline = Pipeline(
        name="broken",
        steps=[
            CallableStep(id="first", body=counted),
            CallableStep(id="middle", dependencies=["first"], body=refuses),
            CallableStep(id="last", dependencies=["middle"], body=counted),
        ],
    )
    summary = pipeline.execute(RunRequest(directory=tmp_path))
    run = RunDirectory(root=tmp_path)
    assert not summary.ok
    assert summary.failed == 1
    assert [step.id for step in summary.skipped] == ["last"]
    assert CALLS == {"first": 1, "middle": 1}
    failed = run.read_result("middle")
    assert failed is not None
    assert failed.status is UnitStatus.FAILED
    assert "cannot be done" in failed.error
    assert run.read_result("first") is not None


def test_a_fixed_fan_out_lands_one_unit_per_item(tmp_path: Path) -> None:
    pipeline = Pipeline(
        name="sweep",
        workers=2,
        steps=[
            CallableStep(
                id="solve", over=FixedItems(items=["a", "b", "c"]), body=counted
            )
        ],
    )
    summary = pipeline.execute(RunRequest(directory=tmp_path))
    run = RunDirectory(root=tmp_path)
    assert summary.landed == 3
    assert CALLS == {"solve": 3}
    assert sorted(result.item for result in run.read().results) == ["a", "b", "c"]


def test_a_computed_fan_out_widens_the_manifest_once_it_resolves(
    tmp_path: Path,
) -> None:
    """A follower watches the total grow rather than being told a wrong one."""
    pipeline = Pipeline(
        name="discovered",
        steps=[
            CallableStep(id="first", over=FixedItems(items=["x", "y"]), body=counted),
            CallableStep(
                id="second",
                dependencies=["first"],
                over=ComputedItems(compute=names),
                body=counted,
            ),
        ],
    )
    pipeline.execute(RunRequest(directory=tmp_path))
    manifest = RunDirectory(root=tmp_path).read_manifest()
    assert manifest is not None
    second = manifest.step("second")
    assert second is not None
    assert sorted(second.items) == ["from-x", "from-y"]
    assert manifest.total_units == 4


def test_a_shell_step_keeps_the_whole_of_its_output_beside_the_result(
    tmp_path: Path,
) -> None:
    pipeline = Pipeline(
        name="shell",
        steps=[ShellStep(id="say", command='echo "$LUP_RUN_STEP is running"')],
    )
    summary = pipeline.execute(RunRequest(directory=tmp_path))
    assert summary.ok
    stdout = tmp_path / "artifacts" / "say" / "once" / "stdout.txt"
    assert stdout.read_text(encoding="utf-8").strip() == "say is running"


def test_a_failing_shell_step_fails_its_unit(tmp_path: Path) -> None:
    pipeline = Pipeline(name="shell", steps=[ShellStep(id="nope", command="exit 3")])
    summary = pipeline.execute(RunRequest(directory=tmp_path))
    assert not summary.ok
    failed = RunDirectory(root=tmp_path).read_result("nope")
    assert failed is not None
    assert failed.status is UnitStatus.FAILED


def test_a_retried_step_records_the_attempts_it_survived(tmp_path: Path) -> None:
    """A step that passes on its second try is not the same as one that passed."""
    pipeline = Pipeline(
        name="flaky", steps=[CallableStep(id="try", body=flaky, retries=1)]
    )
    summary = pipeline.execute(RunRequest(directory=tmp_path))
    assert summary.ok
    result = RunDirectory(root=tmp_path).read_result("try")
    assert result is not None
    assert result.status is UnitStatus.OK
    assert "not yet" in result.error


def test_fresh_discards_what_landed(tmp_path: Path) -> None:
    chain().execute(RunRequest(directory=tmp_path))
    CALLS.clear()
    chain().execute(RunRequest(directory=tmp_path, fresh=True))
    assert CALLS == {"first": 1, "middle": 1, "last": 1}


def test_naming_a_step_that_does_not_exist_says_so(tmp_path: Path) -> None:
    with pytest.raises(PipelineError, match="no such step: ghost"):
        chain().execute(RunRequest(directory=tmp_path, only=["ghost"]))


def test_a_step_whose_input_never_landed_refuses_rather_than_guessing(
    tmp_path: Path,
) -> None:
    with pytest.raises(PipelineError, match="cannot run middle: first has landed"):
        chain().execute(RunRequest(directory=tmp_path, only=["middle"]))


def killed_claim(run: RunDirectory, step: str, renewed_ago: float) -> UnitAttempt:
    """A claim on disk with nothing landed beside it, last renewed some time ago.

    What a killed runner leaves behind: the file is written, the process is
    gone, and nothing will ever renew it or write the result that releases it.
    """
    stamp = utc_now() - timedelta(seconds=renewed_ago)
    attempt = UnitAttempt(step=step, pid=999999, started_at=stamp, renewed_at=stamp)
    run.claim(attempt)
    return attempt


def test_a_claim_nobody_renews_reads_as_stale(tmp_path: Path) -> None:
    """The reading a power cut produces, and the one age alone cannot give.

    A unit running for an hour and a unit whose runner died an hour ago have
    the same age. Only the renewal separates them, which is the whole reason a
    claim is a lease rather than a note.
    """
    run = RunDirectory(root=tmp_path)
    killed_claim(run, "abandoned", renewed_ago=CLAIM_LEASE_SECONDS * 2)
    killed_claim(run, "working", renewed_ago=0.0)

    standing = {unit.attempt.step: unit for unit in run.running()}

    assert standing["abandoned"].stale
    assert not standing["working"].stale


def test_a_long_unit_that_keeps_renewing_is_not_stale(tmp_path: Path) -> None:
    """Slow is not dead, and the lease is what stops one reading as the other."""
    run = RunDirectory(root=tmp_path)
    attempt = killed_claim(run, "slow", renewed_ago=CLAIM_LEASE_SECONDS * 2)
    run.renew(attempt.step)

    [unit] = run.running()

    assert not unit.stale
    assert unit.age_seconds > CLAIM_LEASE_SECONDS
    assert unit.since_renewed_seconds < CLAIM_LEASE_SECONDS


def test_resuming_reclaims_only_the_leases_that_lapsed(tmp_path: Path) -> None:
    """A resumed run frees what nobody holds and leaves a live sibling's alone.

    Dropping every claim is right exactly once — when this is the only runner
    and the last one is gone — and frees units another runner is working the
    moment two share a directory.
    """
    run = RunDirectory(root=tmp_path)
    killed_claim(run, "abandoned", renewed_ago=CLAIM_LEASE_SECONDS * 2)
    killed_claim(run, "held-by-a-sibling", renewed_ago=0.0)

    assert run.clear_claims() == ["abandoned/once"]
    assert [unit.attempt.step for unit in run.running()] == ["held-by-a-sibling"]


def test_a_renewal_does_not_resurrect_a_claim_that_landed(tmp_path: Path) -> None:
    """The unit finished while the heartbeat was mid-tick; it stays finished."""
    run = RunDirectory(root=tmp_path)
    attempt = killed_claim(run, "landed", renewed_ago=0.0)
    run.release(attempt.step)

    run.renew(attempt.step)

    assert run.running() == []


def test_a_run_that_finishes_leaves_no_claim_behind(tmp_path: Path) -> None:
    """The ordinary path, pinned because a lease only shows when it is not taken."""
    chain().execute(RunRequest(directory=tmp_path))

    assert RunDirectory(root=tmp_path).running() == []


def test_a_killed_run_reads_as_abandoned_rather_than_working(tmp_path: Path) -> None:
    """What the monitor says about the directory a power cut leaves.

    The failure this exists to stop is a reader taking "4 running" from four
    claims nobody holds, and settling in to wait on a process that is gone.
    """
    run = RunDirectory(root=tmp_path)
    killed_claim(run, "one", renewed_ago=CLAIM_LEASE_SECONDS * 3)
    killed_claim(run, "two", renewed_ago=CLAIM_LEASE_SECONDS * 3)

    reading = read_progress(run)

    assert len(reading.abandoned) == 2
    assert "no runner holds this" in reading.describe_activity()
    assert "abandoned=2" in reading.postfix()
    assert "running=" not in reading.postfix()


def crawls(context: StepContext) -> StepOutcome:
    """A body that says how far into its own work it is before it finishes."""
    CALLS[context.step] = CALLS.get(context.step, 0) + 1
    context.report(done=3, total=10, phase="fit", detail={"supports": 41})
    return StepOutcome(outcome="done")


def test_a_body_reports_through_the_context_it_was_already_given(
    tmp_path: Path,
) -> None:
    """The context knows the workspace, so a body names no path to get this wrong."""
    Pipeline(name="crawl", steps=[CallableStep(id="solve", body=crawls)]).execute(
        RunRequest(directory=tmp_path)
    )
    record = RunDirectory(root=tmp_path).read_progress_record("solve")
    assert record is not None
    assert (record.done, record.total, record.phase) == (3, 10, "fit")
    assert record.detail == {"supports": 41}


def test_a_units_last_reading_outlives_the_claim_that_carried_it(
    tmp_path: Path,
) -> None:
    """A unit that died at 2870 of 3000 leaves that reading beside its traceback."""
    run = RunDirectory(root=tmp_path)
    Pipeline(name="crawl", steps=[CallableStep(id="solve", body=crawls)]).execute(
        RunRequest(directory=tmp_path)
    )
    assert run.running() == []
    assert run.progress_path("solve").is_file()
    assert run.read_result("solve") is not None


def test_a_shell_unit_is_told_where_to_report(tmp_path: Path) -> None:
    """The doorway for a unit in any language is one environment variable."""
    step = ShellStep(id="solve", command="true")
    script = step.script(StepContext(run=RunDirectory(root=tmp_path), step="solve"))
    assert f"{WORKSPACE_ENV}=" in script


def test_run_report_writes_the_file_report_progress_writes(tmp_path: Path) -> None:
    """A unit in any language reaches the same record, parsed the same way."""
    workspace = tmp_path / "unit"
    result = CliRunner().invoke(
        create_run_app(),
        [
            "report",
            "--workspace",
            str(workspace),
            "--done",
            "41",
            "--total",
            "300",
            "--phase",
            "fit",
            "--detail",
            "supports=41",
            "--detail",
            "note=alpha",
            "--detail",
            'shape={"k":1}',
        ],
    )
    assert result.exit_code == 0
    written = UnitProgress.model_validate_json(
        progress_in(workspace).read_text(encoding="utf-8")
    )
    assert (written.done, written.total, written.phase) == (41, 300, "fit")
    assert written.detail == {"supports": 41, "note": "alpha", "shape": {"k": 1}}


def test_run_report_takes_the_workspace_the_runtime_gave_the_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(WORKSPACE_ENV, str(tmp_path / "unit"))
    assert (
        CliRunner().invoke(create_run_app(), ["report", "--done", "7"]).exit_code == 0
    )
    assert progress_in(tmp_path / "unit").is_file()


def test_run_report_refuses_a_detail_that_is_not_a_pair(tmp_path: Path) -> None:
    """A flag grammar that guessed would put a unit's own words somewhere odd."""
    failed = CliRunner().invoke(
        create_run_app(),
        ["report", "--workspace", str(tmp_path), "--done", "1", "--detail", "oops"],
    )
    assert failed.exit_code != 0


def test_run_report_says_so_when_nothing_told_it_where_to_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    failed = CliRunner().invoke(create_run_app(), ["report", "--done", "1"])
    assert failed.exit_code != 0


def sweep(items: list[str], body: StepBody = counted) -> Pipeline:
    """One fanned-out step over `items`, for pinning how units are filed."""
    return Pipeline(
        name="sweep",
        workers=2,
        steps=[CallableStep(id="solve", over=FixedItems(items=items), body=body)],
    )


def test_an_item_no_filename_can_hold_still_lands_and_reads_back(
    tmp_path: Path,
) -> None:
    """Items are data, so their text never decides whether a run survives."""
    long = "census." + "x" * 400
    items = [long, "a/b", "..", "plain"]
    summary = sweep(items).execute(RunRequest(directory=tmp_path))
    run = RunDirectory(root=tmp_path)
    assert summary.ok
    assert sorted(result.item for result in run.read().results) == sorted(items)
    filed = list((run.units_root / "solve").iterdir())
    assert all(path.is_file() for path in filed)
    assert all(len(path.name.encode()) <= FILENAME_BYTES for path in filed)
    assert read_progress(run).landed == len(items)
    assert (
        CliRunner().invoke(create_run_app(), ["monitor", str(tmp_path), "--once"])
    ).exit_code == 0


def test_an_item_that_already_is_a_filename_keeps_its_own_text() -> None:
    """Every directory filed before stays readable exactly where it is."""
    assert filed_name("census.sort.q3.w1.0-10000000") == "census.sort.q3.w1.0-10000000"
    assert filed_name("once") == "once"


def test_two_long_items_sharing_a_prefix_are_filed_apart() -> None:
    shared = "x" * 400
    assert filed_name(shared + "a") != filed_name(shared + "b")
    assert "/" not in filed_name("a/" * 200)


def test_a_claim_that_cannot_be_written_fails_only_its_own_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unit's filesystem error is that unit's failure, never the run's."""
    claim = RunDirectory.claim

    def refuse_b(run: RunDirectory, attempt: UnitAttempt) -> None:
        if attempt.item == "b":
            raise OSError(36, "File name too long")
        claim(run, attempt)

    monkeypatch.setattr(RunDirectory, "claim", refuse_b)
    summary = sweep(["a", "b", "c"]).execute(RunRequest(directory=tmp_path))
    failed = RunDirectory(root=tmp_path).read_result("solve", "b")
    assert summary.landed == 3
    assert summary.failed == 1
    assert CALLS == {"solve": 2}
    assert failed is not None
    assert failed.status is UnitStatus.FAILED
    assert "File name too long" in failed.error


def test_a_result_that_will_not_write_lands_as_its_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_result = RunDirectory.write_result

    def refuse_success(run: RunDirectory, result: UnitResult) -> None:
        if result.item == "b" and result.status is UnitStatus.OK:
            raise OSError(28, "No space left on device")
        write_result(run, result)

    monkeypatch.setattr(RunDirectory, "write_result", refuse_success)
    summary = sweep(["a", "b"]).execute(RunRequest(directory=tmp_path))
    failed = RunDirectory(root=tmp_path).read_result("solve", "b")
    assert summary.failed == 1
    assert failed is not None
    assert "No space left on device" in failed.error


def test_a_unit_that_cannot_land_at_all_is_named_in_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_result = RunDirectory.write_result

    def refuse_b(run: RunDirectory, result: UnitResult) -> None:
        if result.item == "b":
            raise OSError(5, "Input/output error")
        write_result(run, result)

    monkeypatch.setattr(RunDirectory, "write_result", refuse_b)
    summary = sweep(["a", "b", "c"]).execute(RunRequest(directory=tmp_path))
    assert summary.unlanded == ["solve/b"]
    assert not summary.ok
    assert "could not be landed: solve/b" in describe_summary(summary)
    assert RunDirectory(root=tmp_path).read_summary() == summary


def test_a_runner_crash_leaves_its_cause_in_the_directory(tmp_path: Path) -> None:
    """The traceback is read where the run is read, not on a forgotten stderr."""
    with pytest.raises(PipelineError, match="cannot run middle"):
        chain().execute(RunRequest(directory=tmp_path, only=["middle"]))
    run = RunDirectory(root=tmp_path)
    summary = run.read_summary()
    assert summary is not None
    assert "cannot run middle" in summary.crashed
    assert "PipelineError" in summary.crash_traceback
    assert not summary.ok
    assert describe_summary(summary).startswith("run crashed after 0 units")
    assert "crashed: PipelineError(" in run.log_path.read_text(encoding="utf-8")


def test_a_resumed_run_takes_down_the_last_ending_before_claiming(
    tmp_path: Path,
) -> None:
    """Until this attempt writes its own summary, the directory is still running."""
    sweep(["a"], body=refuses).execute(RunRequest(directory=tmp_path))
    run = RunDirectory(root=tmp_path)
    assert run.read_summary() is not None
    seen: list[bool] = []

    def notes_the_summary(context: StepContext) -> StepOutcome:
        seen.append(context.run.read_summary() is None)
        return StepOutcome()

    summary = sweep(["a"], body=notes_the_summary).execute(
        RunRequest(directory=tmp_path)
    )
    assert seen == [True]
    assert summary.ok
    assert "resuming after: run failed: 1 unit failed" in run.log_path.read_text(
        encoding="utf-8"
    )
