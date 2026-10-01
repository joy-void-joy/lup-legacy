"""A merger's unanswered decision remains a durable, resumable question."""

from pathlib import Path

import pytest

from lup.resolver.contracts import ResolverAwaitingAnswers
from lup.resolver.join_desk import JoinDesk, JoinPlan, JoinProgressRecord
from lup.resolver.join_tools import JoinReport
from lup.resolver.models import MaterialQuestion, ResolveInventory, WritableRootLease
from lup.execution.process import LocalProcessLauncher
from tests.unit.test_resolver_core import (
    admitting_core,
    concern,
    failure_leg_workspace,
    implementing_worker,
    seed_approvals,
    snapshot,
)
from lup.types import JsonObject
from lup.channels.models import utc_now
from lup.coordination.mailbox import AnswerDoor, AnswerOffer


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("dependent", [False, True])
async def test_merger_blockers_park_with_visible_questions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued: bool, dependent: bool
) -> None:
    launcher = LocalProcessLauncher()
    workspace = failure_leg_workspace(tmp_path, launcher)

    def review(root: Path, _output_name: str) -> JsonObject:
        return {
            "concern_id": root.name,
            "accepted": True,
            "generalized": True,
            "reason": "criteria met",
            "criteria_met": [f"{root.name}-done"],
        }

    core = admitting_core(
        tmp_path,
        workspace,
        launcher,
        "blocked-join",
        implementing_worker({}, tmp_path / "state", "blocked-join"),
        review,
    )
    blocked = "Three alternatives remain. Keep every original detail.\n" * 200
    calls = 0

    async def join_turn(
        lease: WritableRootLease,
        _plan: JoinPlan,
        _progress: JoinProgressRecord,
        _purpose: str,
    ) -> JoinReport:
        nonlocal calls
        calls += 1
        if queued:
            core.questions.queue_questions(
                [
                    MaterialQuestion(
                        id="choose", concern_id=lease.concern_id, prompt=blocked
                    )
                ],
                lease.concern_id,
            )
        return JoinReport(
            plan="wait for a decision", summary="waiting", blocked=blocked
        )

    monkeypatch.setattr(core.joiner.runner, "join_turn", join_turn)
    concerns = [concern("a"), concern("b")]
    if dependent:
        concerns.append(concern("c", ["a", "b"]))
    seed_approvals(core, concerns)
    with pytest.raises(ResolverAwaitingAnswers) as parked:
        await core.run(
            ResolveInventory(source=snapshot(workspace, launcher), concerns=concerns)
        )

    assert calls == 1
    assert len(parked.value.pending) == 1
    question = parked.value.pending[0]
    assert blocked in question.prompt
    assert question.choices == []
    assert question.id not in core.mailbox.answered_ids()
    assert any(item.question == question for item in core.mailbox.questions())
    assert (
        JoinDesk(core.repository.root, "c" if dependent else "integration").plan()
        is not None
    )
    core.mailbox.offer(
        AnswerOffer(
            run_id=core.config.run_id,
            question_id=question.id,
            value="Retry the join with the original intent.",
            door=AnswerDoor.FLAG,
            offered_at=utc_now(),
        )
    )
    with pytest.raises(ResolverAwaitingAnswers) as repeated:
        await core.resume()
    assert calls == 2
    assert repeated.value.pending[0].id != question.id
    assert blocked in repeated.value.pending[0].prompt
