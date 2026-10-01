"""Stopped recovery imports historical decisions without inventing authority."""

from pathlib import Path

import pytest

from lup.channels.models import Door, utc_now
from lup.coordination.mailbox import RecordedAnswer
from lup.coordination.questions import QuestionAnswer
from lup.execution.process import LocalProcessLauncher
from lup.resolver.contracts import ResolverAwaitingAnswers
from lup.resolver.core import resolver_config_digest
from lup.resolver.mailbox import PendingQuestion, QuestionMailbox
from lup.resolver.models import (
    AnswerBatch,
    IntegrationRecord,
    MaterialQuestion,
    QuestionBatch,
    ResolvePhase,
    ResolveState,
)
from lup.resolver.recovery import (
    IntegrationRecoveryDesk,
    IntegrationRecoveryMode,
    RecoveryQuestionImport,
)
from lup.resolver.state import StateTransitionError
from tests.unit.test_resolver_core import (
    failure_leg_core,
    implementing_worker,
    planning_reviewer,
)
from tests.unit.test_resolver_recovery import RecoveryFixture
from tests.unit.test_resolver_recovery import recovery as recovery_fixture

recovery = recovery_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("answered", [False, True])
async def test_adoption_imports_state_only_decisions_and_retires_only_old_recheck(
    recovery: RecoveryFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
    answered: bool,
) -> None:
    core = failure_leg_core(
        tmp_path,
        tmp_path / "source",
        LocalProcessLauncher(),
        "recover",
        implementing_worker({}, tmp_path / "state", "recover"),
        planning_reviewer({}),
    )
    core.repository.adopt(core.config, resolver_config_digest(core.config))
    finding = MaterialQuestion(
        id="a-superseded-integrated" if legacy else "old-recheck",
        concern_id="a",
        prompt="The old integration lost a criterion.",
        criteria=["criterion"],
        recheck_commit=None if legacy else recovery.base,
    )
    design = MaterialQuestion(id="design", concern_id="a", prompt="Keep the interface?")
    answer = QuestionAnswer(question_id="design", value="yes")
    original = core.repository.load().model_copy(
        update={
            "questions": QuestionBatch(run_id="recover", questions=[finding, design]),
            "answers": AnswerBatch(
                run_id="recover", answers=[answer] if answered else []
            ),
        }
    )
    core.repository.save(original)
    (recovery.lease.root / "repair").write_text("fixed")
    recovery.command("add", "repair")
    recovery.command("commit", "-m", "repair")
    imported_after = utc_now()
    result = IntegrationRecoveryDesk(core.repository).recover(
        IntegrationRecoveryMode.ADOPT
    )
    mailbox = QuestionMailbox(core.repository.root)
    assert [item.question for item in mailbox.questions()] == [design]
    assert {item.question.id for item in mailbox.questions(include_retired=True)} == {
        finding.id,
        design.id,
    }
    assert mailbox.settled_answer(finding.id) is None
    imported = mailbox.settled_answer(design.id)
    if answered:
        assert imported is not None and imported.answer == answer
        assert imported.door == Door.RECOVERY
        assert imported.answered_at >= imported_after
    else:
        assert imported is None
    assert all(
        item.asked_by == "recovery:state-import" and item.asked_at >= imported_after
        for item in mailbox.questions(include_retired=True)
    )
    assert (
        ResolveState.model_validate_json((result.evidence / "state.json").read_text())
        == original
    )
    seen: list[str] = []

    async def recheck(
        state: ResolveState, integration: IntegrationRecord
    ) -> list[MaterialQuestion]:
        assert state.verification and all(item.passed for item in state.verification)
        assert integration.commit is not None
        seen.append(integration.commit)
        return []

    monkeypatch.setattr(core.joiner, "recheck_criteria", recheck)
    if answered:
        await core.resume()
        assert core.repository.load().phase == ResolvePhase.COMPLETE
        assert seen == [result.after]
    else:
        with pytest.raises(ResolverAwaitingAnswers) as waiting:
            await core.resume()
        assert [item.id for item in waiting.value.pending] == [design.id]
        assert seen == []
    state = core.repository.load()
    assert state.retired_questions == [finding.id]
    assert state.questions is not None and finding in state.questions.questions
    assert state.answers is not None
    assert state.answers.answers == ([answer] if answered else [])


@pytest.mark.parametrize("changed_domain", [False, True])
@pytest.mark.parametrize("settled", [False, True])
def test_existing_mailbox_keeps_its_domain_and_recorded_answer(
    recovery: RecoveryFixture, changed_domain: bool, settled: bool
) -> None:
    original = MaterialQuestion(id="design", concern_id="a", prompt="Original prompt")
    native = original.model_copy(
        update={
            "prompt": "Mailbox prompt",
            "choices": ["native"] if changed_domain else [],
        }
    )
    historical = QuestionAnswer(question_id="design", value="historical decision")
    native_answer = QuestionAnswer(question_id="design", value="native decision")
    mailbox = QuestionMailbox(recovery.repository.root)
    pending = PendingQuestion(
        run_id="recover",
        question=native,
        asked_by="actual-reviewer",
        asked_at=utc_now(),
    )
    mailbox.queue(pending)
    recorded = RecordedAnswer(
        run_id="recover", answer=native_answer, door=Door.PAGE, answered_at=utc_now()
    )
    if settled:
        mailbox.record(recorded)
    state = recovery.repository.load().model_copy(
        update={
            "questions": QuestionBatch(run_id="recover", questions=[original]),
            "answers": AnswerBatch(run_id="recover", answers=[historical]),
        }
    )
    recovery.repository.save(state)
    result = IntegrationRecoveryDesk(recovery.repository).recover(
        IntegrationRecoveryMode.RESTORE
    )
    assert [item.model_dump() for item in mailbox.questions()] == [pending.model_dump()]
    answer = mailbox.settled_answer("design")
    if settled:
        assert answer == recorded
    elif changed_domain:
        assert answer is None
    else:
        assert answer is not None and answer.answer == historical
        assert answer.door == Door.RECOVERY
    imported = RecoveryQuestionImport.model_validate_json(
        (result.evidence / "mailbox-import.json").read_text()
    )
    assert imported.conflicting_questions == (["design"] if changed_domain else [])
    assert imported.conflicting_answers == (
        ["design"] if changed_domain or settled else []
    )
    assert (
        ResolveState.model_validate_json((result.evidence / "state.json").read_text())
        == state
    )
    canonical = recovery.repository.load()
    assert canonical.questions is not None and canonical.questions.questions == [native]
    assert canonical.answers is not None
    assert canonical.answers.answers == ([answer.answer] if answer is not None else [])
    IntegrationRecoveryDesk(recovery.repository).recover(
        IntegrationRecoveryMode.RESTORE
    )
    assert [item.model_dump() for item in mailbox.questions()] == [pending.model_dump()]
    assert mailbox.settled_answer("design") == answer


@pytest.mark.parametrize("missing_question", [False, True])
def test_unbound_or_invalid_historical_answer_refuses_without_rewriting_state(
    recovery: RecoveryFixture, missing_question: bool
) -> None:
    question = MaterialQuestion(
        id="decision",
        concern_id="a",
        prompt="Proceed?",
        choices=["yes"],
        closed_choices=True,
    )
    state = recovery.repository.load().model_copy(
        update={
            "questions": QuestionBatch(
                run_id="recover", questions=[] if missing_question else [question]
            ),
            "answers": AnswerBatch(
                run_id="recover",
                answers=[QuestionAnswer(question_id="decision", value="invalid")],
            ),
        }
    )
    recovery.repository.save(state)
    with pytest.raises(StateTransitionError, match="matching original question"):
        IntegrationRecoveryDesk(recovery.repository).recover(
            IntegrationRecoveryMode.RESTORE
        )
    assert recovery.repository.load() == state
    assert QuestionMailbox(recovery.repository.root).questions() == []
    archives = list(
        (recovery.repository.root / "integration" / "recovery").glob("*/state.json")
    )
    assert len(archives) == 1
    assert ResolveState.model_validate_json(archives[0].read_text()) == state
