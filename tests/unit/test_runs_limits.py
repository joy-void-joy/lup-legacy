"""What a declared limit does to a unit that runs past it, and to nothing else.

A unit that hangs or grows without bound used to be invisible: its claim is
renewed by the runner's heartbeat, and its siblings keep landing, so neither
the lease nor the run's silence ever says anything is wrong. These pin the
answer — a limited unit runs in a session of its own, is measured over every
process in it, and is stopped past its limit with a breach saying which one —
together with the parts that must not move: a unit within its limits runs as
before, a failing command still fails as before, and its siblings land.
"""

import shlex
import sys
import time
from pathlib import Path

import pytest
import sh
from pydantic import ValidationError

from lup.runs.directory import RunDirectory
from lup.runs.limits import (
    LimitUnenforceable,
    SessionRegistry,
    UnitLimits,
    session_members,
    session_resident_bytes,
    stop_session,
    supervise,
)
from lup.runs.models import UnitResult, UnitStatus
from lup.runs.pipeline import (
    CallableStep,
    FixedItems,
    Pipeline,
    RunRequest,
    ShellStep,
    StepContext,
    StepOutcome,
)

QUICK = UnitLimits(timeout_seconds=20.0, sample_seconds=0.05, grace_seconds=0.5)
"""Limits that sample fast and stop fast, so each test spends seconds, not minutes."""

MEBIBYTE = 2**20


def allocate(mebibytes: int) -> str:
    """A shell command running a Python child that touches this much and waits."""
    program = f"import time; b = b'x' * ({mebibytes} * 2**20); time.sleep(30)"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


def limited(command: str, limits: UnitLimits, items: list[str]) -> Pipeline:
    """One fanned-out shell step under ``limits``, two units at a time."""
    return Pipeline(
        name="limited",
        workers=2,
        steps=[
            ShellStep(
                id="work", over=FixedItems(items=items), command=command, limits=limits
            )
        ],
    )


def landed(run: RunDirectory, item: str) -> UnitResult:
    """The landed result of one unit of the ``work`` step."""
    result = run.read_result("work", item)
    assert result is not None
    return result


def test_a_unit_past_its_timeout_is_stopped_while_its_sibling_lands(
    tmp_path: Path,
) -> None:
    command = 'if [ "$LUP_RUN_ITEM" = slow ]; then sleep 30; fi'
    limits = QUICK.model_copy(update={"timeout_seconds": 0.5})
    summary = limited(command, limits, ["slow", "quick"]).execute(
        RunRequest(directory=tmp_path)
    )
    run = RunDirectory(root=tmp_path)

    slow = landed(run, "slow")
    assert summary.failed == 1
    assert slow.status is UnitStatus.FAILED
    assert slow.breach is not None
    assert slow.breach.limit == "timeout"
    assert slow.error.startswith("exceeded time limit:")
    assert slow.elapsed_seconds < 10
    assert landed(run, "quick").status is UnitStatus.OK


def test_a_unit_past_its_memory_limit_is_stopped_with_the_measured_value(
    tmp_path: Path,
) -> None:
    limits = QUICK.model_copy(update={"memory_bytes": 100 * MEBIBYTE})
    limited(allocate(200), limits, ["big"]).execute(RunRequest(directory=tmp_path))

    big = landed(RunDirectory(root=tmp_path), "big")
    assert big.status is UnitStatus.FAILED
    assert big.breach is not None
    assert big.breach.limit == "memory"
    assert big.breach.measured > 100 * MEBIBYTE
    assert big.error.startswith("exceeded memory limit:")


def test_a_child_processes_memory_counts_against_its_unit(tmp_path: Path) -> None:
    """The shell is small; what it started is not, and that is the unit's too."""
    command = f"{{ {allocate(200)}; }} & wait"
    limits = QUICK.model_copy(update={"memory_bytes": 100 * MEBIBYTE})
    limited(command, limits, ["forked"]).execute(RunRequest(directory=tmp_path))

    forked = landed(RunDirectory(root=tmp_path), "forked")
    assert forked.breach is not None
    assert forked.breach.limit == "memory"


def test_stopping_a_unit_leaves_nothing_of_its_session_running(
    tmp_path: Path,
) -> None:
    """The shell, and everything it started, are all gone once the unit fails."""
    marker = tmp_path / "session.txt"
    command = f"echo $$ > {shlex.quote(str(marker))}; sleep 30 & sleep 30 & wait"
    limits = QUICK.model_copy(update={"timeout_seconds": 0.5})
    limited(command, limits, ["many"]).execute(RunRequest(directory=tmp_path / "run"))

    session = int(marker.read_text(encoding="utf-8"))
    assert session_members(session) == []


def test_a_unit_within_its_limits_runs_as_an_unlimited_one(tmp_path: Path) -> None:
    limits = QUICK.model_copy(update={"memory_bytes": 512 * MEBIBYTE})
    summary = limited('echo "$LUP_RUN_ITEM done"', limits, ["fine"]).execute(
        RunRequest(directory=tmp_path)
    )

    fine = landed(RunDirectory(root=tmp_path), "fine")
    assert summary.ok
    assert fine.breach is None
    stdout = tmp_path / "artifacts" / "work" / "fine" / "stdout.txt"
    assert stdout.read_text(encoding="utf-8").strip() == "fine done"


def test_a_failing_command_under_limits_fails_as_it_always_did(tmp_path: Path) -> None:
    limited("exit 3", QUICK, ["nope"]).execute(RunRequest(directory=tmp_path))

    nope = landed(RunDirectory(root=tmp_path), "nope")
    assert nope.status is UnitStatus.FAILED
    assert nope.breach is None
    assert "ErrorReturnCode_3" in nope.error


def test_a_stopped_unit_is_not_retried(tmp_path: Path) -> None:
    """Another attempt would meet the same bound and spend it again."""
    pipeline = Pipeline(
        name="limited",
        steps=[
            ShellStep(
                id="work",
                command="sleep 30",
                retries=2,
                limits=QUICK.model_copy(update={"timeout_seconds": 0.3}),
            )
        ],
    )
    pipeline.execute(RunRequest(directory=tmp_path))

    result = RunDirectory(root=tmp_path).read_result("work")
    assert result is not None
    assert result.breach is not None
    assert result.error == result.breach.render()


def test_a_callable_step_refuses_limits() -> None:
    def body(context: StepContext) -> StepOutcome:
        return StepOutcome()

    with pytest.raises(ValidationError, match="process of its own"):
        CallableStep(id="inline", body=body, limits=QUICK)


def test_the_registry_stops_every_session_an_interrupt_would_otherwise_miss() -> None:
    """A limited unit is outside the runner's group, so the terminal cannot reach it."""
    running = sh.Command("bash")(
        "-c", "sleep 30 & wait", _bg=True, _bg_exc=False, _new_session=True
    )
    registry = SessionRegistry()
    registry.enrol(running.pid, 0.5)

    registry.close()

    assert session_members(running.pid) == []


def test_a_session_enrolled_once_the_run_is_ending_is_stopped_at_once() -> None:
    """A worker that started its unit just after the interrupt does not hold the run."""
    registry = SessionRegistry()
    registry.close()
    running = sh.Command("bash")(
        "-c", "sleep 30 & wait", _bg=True, _bg_exc=False, _new_session=True
    )

    registry.enrol(running.pid, 0.5)

    assert session_members(running.pid) == []


def test_a_session_that_has_ended_holds_no_memory() -> None:
    assert session_resident_bytes(999999999) == 0


def test_a_session_stops_through_its_group_where_there_is_no_proc(
    tmp_path: Path,
) -> None:
    """A timeout is enforceable anywhere; only memory needs ``/proc`` to read."""
    running = sh.Command("bash")(
        "-c", "sleep 30 & wait", _bg=True, _bg_exc=False, _new_session=True
    )

    stop_session(running.pid, 0.5, proc=tmp_path / "absent")

    assert session_members(running.pid) == []


def test_a_memory_limit_with_no_proc_to_read_is_refused(tmp_path: Path) -> None:
    """A bound that silently does nothing reads exactly like one that held."""
    running = sh.Command("true")(_bg=True, _bg_exc=False, _new_session=True)
    limits = QUICK.model_copy(update={"memory_bytes": MEBIBYTE})

    with pytest.raises(LimitUnenforceable, match="memory limit needs"):
        supervise(running, limits, SessionRegistry(), proc=tmp_path / "absent")


def test_an_interrupt_stops_a_limited_unit_rather_than_waiting_on_it(
    tmp_path: Path,
) -> None:
    """Ctrl-C ends a run of limited units as promptly as one of unlimited units.

    The interrupt is raised from a sibling unit, which reaches the runner's
    loop exactly as one from the terminal does; the limited unit beside it
    would otherwise hold the pool open for its whole twenty-second timeout.
    """

    def interrupts(context: StepContext) -> StepOutcome:
        time.sleep(0.5)
        raise KeyboardInterrupt

    pipeline = Pipeline(
        name="interrupted",
        workers=2,
        steps=[
            CallableStep(id="operator", body=interrupts),
            ShellStep(id="work", command="sleep 30", limits=QUICK),
        ],
    )
    began = time.monotonic()

    summary = pipeline.execute(RunRequest(directory=tmp_path))

    assert summary.interrupted
    assert time.monotonic() - began < 10
