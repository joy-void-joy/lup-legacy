"""Live admission is durable evidence consumed once at scheduler boundaries."""

from pathlib import Path

import pytest

from lup.execution.process import LocalProcessLauncher
from lup.resolver.admissions import (
    AdmissionMailbox,
    AdmissionStatus,
    ResolverAdmissionsPending,
)
from lup.resolver.models import (
    AdmissionRequest,
    ResolveInventory,
    ResolvePhase,
    ResolveState,
    ConcernOutcome,
    WorkAssignment,
    WorkerReport,
)
from lup.resolver.state import StateTransitionError
from lup.resolver.contracts import ResolverAwaitingAnswers
from tests.unit.test_resolver_core import (
    admitted_concern,
    admitted_plan,
    admitting_core,
    concern,
    failure_leg_workspace,
    implementing_worker,
    planning_reviewer,
    seed_approvals,
    snapshot,
)
from tests.unit.test_resolver_retirement import seeded
from lup.resolver.models import ConcernStatus


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["worker", "assembly"])
@pytest.mark.parametrize(
    ("approved", "planned_id"), [(False, "b"), (True, "b"), (True, "a")]
)
async def test_evidence_queued_inside_a_worker_reaches_gates_without_restarting_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approved: bool,
    planned_id: str,
    boundary: str,
) -> None:
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)
    core = admitting_core(
        tmp_path,
        workspace,
        launcher,
        "live-admit",
        implementing_worker({}, tmp_path / "state", "live-admit"),
        planning_reviewer(
            admitted_plan(
                admitted_concern(planned_id, ["a"] if planned_id == "b" else [])
            ),
            "a",
            "b",
        ),
    )
    original = core.executor.runner.worker_turn
    executed: list[str] = []
    queued = False

    def enqueue() -> None:
        nonlocal queued
        assert core.repository.held()
        receipt = core.repository.queue_admission(
            AdmissionRequest(statements=["also address b"])
        )
        assert receipt.status == AdmissionStatus.QUEUED
        assert [item.id for item in core.repository.load().concerns] == ["a"]
        queued = True

    assembly = core.approve_assembly

    async def approve_assembly(
        state: ResolveState, outcomes: list[ConcernOutcome]
    ) -> None:
        await assembly(state, outcomes)
        if boundary == "assembly" and not queued:
            enqueue()

    async def worker_turn(
        assignment: WorkAssignment,
        feedback: str,
        round_number: int,
        holds_prior_work: bool = False,
    ) -> WorkerReport:
        report = await original(assignment, feedback, round_number, holds_prior_work)
        executed.append(report.concern_id)
        if report.concern_id == "a" and boundary == "worker":
            enqueue()
        return report

    monkeypatch.setattr(core.executor.runner, "worker_turn", worker_turn)
    monkeypatch.setattr(core, "approve_assembly", approve_assembly)
    seed_approvals(core, [concern("a"), *([concern("b")] if approved else [])])
    inventory = ResolveInventory(
        source=snapshot(workspace, launcher), concerns=[concern("a")]
    )
    if not approved:
        with pytest.raises(ResolverAwaitingAnswers) as parked:
            await core.run(inventory)
        assert executed == ["a"]
        assert any(question.concern_id == "b" for question in parked.value.pending)
        assert (
            AdmissionMailbox(core.repository.root).receipts()[0].status
            == AdmissionStatus.APPLIED
        )
        return
    manifest = await core.run(inventory)
    if planned_id == "a":
        assert executed == ["a"]
        rejected = AdmissionMailbox(core.repository.root).receipts()[0]
        assert rejected.status == AdmissionStatus.REJECTED
        assert rejected.error
        assert len(core.repository.load().concerns) == 1
        return
    assert executed == ["a", "b"]
    assert all(item.verified for item in manifest.outcomes)
    mailbox = AdmissionMailbox(core.repository.root)
    receipt = mailbox.receipts()[0]
    assert receipt.status == AdmissionStatus.APPLIED
    assert receipt.result is not None
    assert receipt.result.concerns[0].id == "b"
    assert receipt.id in core.repository.load().admitted_requests

    # A process can die after committing the widened state and before its receipt.
    mailbox.write(receipt.model_copy(update={"status": AdmissionStatus.PLANNED}))
    with core.repository.exclusive():
        await core.apply_admissions()
    assert len(core.repository.load().concerns) == 2
    assert mailbox.receipts()[0].status == AdmissionStatus.APPLIED


def test_integration_cannot_pass_an_accepted_admission(tmp_path: Path) -> None:
    repository = seeded(tmp_path, ConcernStatus.LEASED)
    state = repository.load().model_copy(update={"phase": ResolvePhase.REVIEW})
    repository.write_model("state.json", state)
    receipt = repository.queue_admission(AdmissionRequest(statements=["new evidence"]))
    with pytest.raises(ResolverAdmissionsPending):
        repository.save(state.model_copy(update={"phase": ResolvePhase.INTEGRATION}))
    assert repository.load().phase == ResolvePhase.REVIEW
    assert AdmissionMailbox(repository.root).pending() == [receipt]


@pytest.mark.parametrize(
    "phase", [ResolvePhase.INTEGRATION, ResolvePhase.COMPLETE, ResolvePhase.ABORTED]
)
def test_late_admission_is_refused_before_creating_a_receipt(
    tmp_path: Path, phase: ResolvePhase
) -> None:
    repository = seeded(tmp_path, ConcernStatus.LEASED)
    repository.write_model(
        "state.json", repository.load().model_copy(update={"phase": phase})
    )
    with pytest.raises(StateTransitionError, match="before integration"):
        repository.queue_admission(AdmissionRequest(statements=["too late"]))
    assert AdmissionMailbox(repository.root).receipts() == []
