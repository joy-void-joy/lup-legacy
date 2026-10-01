"""Retirement received inside a worker turn wins at the next owned boundary."""

from pathlib import Path

import pytest

from lup.execution.process import LocalProcessLauncher
from lup.resolver.models import (
    ConcernRetirement,
    ConcernStatus,
    ResolveInventory,
    WorkAssignment,
    WorkerReport,
)
from tests.unit.test_resolver_core import (
    admitting_core,
    concern,
    failure_leg_workspace,
    implementing_worker,
    planning_reviewer,
    seed_approvals,
    snapshot,
)


@pytest.mark.asyncio
async def test_retirement_during_a_worker_preserves_edits_and_allows_dependents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    core = admitting_core(
        tmp_path,
        workspace,
        launcher,
        "live-retire",
        implementing_worker({}, tmp_path / "state", "live-retire"),
        planning_reviewer({}, "a", "b"),
    )
    original = core.executor.runner.worker_turn

    async def worker_turn(
        assignment: WorkAssignment,
        feedback: str,
        round_number: int,
        holds_prior_work: bool = False,
    ) -> WorkerReport:
        report = await original(assignment, feedback, round_number, holds_prior_work)
        if report.concern_id == "a":
            core.repository.retire(
                ConcernRetirement(concern_id="a", reason="landed upstream")
            )
        return report

    monkeypatch.setattr(core.executor.runner, "worker_turn", worker_turn)
    concerns = [concern("a"), concern("b", ["a"])]
    seed_approvals(core, concerns)
    manifest = await core.run(
        ResolveInventory(source=snapshot(workspace, launcher), concerns=concerns)
    )
    saved = core.repository.load()
    assert (
        next(item for item in saved.progress if item.concern_id == "a").status
        == ConcernStatus.RETIRED
    )
    retired_lease = next(item for item in saved.leases if item.concern_id == "a")
    assert not retired_lease.active
    assert (retired_lease.root / "a.txt").read_text() == "durable\n"
    assert [outcome.concern_id for outcome in manifest.outcomes] == ["b"]
    assert manifest.outcomes[0].verified
    assert saved.failures == []
