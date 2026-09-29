"""Stopped-run recovery preserves evidence and refuses unexplained authority."""

import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from lup.devtools.resolve import recovery as cli
from lup.channels.models import utc_now
from lup.coordination.mailbox import (
    AnswerDoor,
    AnswerOffer,
    MailboxConflictError,
    RecordedAnswer,
)
from lup.coordination.questions import QuestionAnswer
from lup.execution.shell import git
from lup.harness.process import LocalProcessLauncher
from lup.resolver.core import resolver_config_digest
from lup.resolver.join_desk import JoinDesk, JoinLanding, JoinPlan
from lup.resolver.record import IntegrationRecoveredEvent, Journal
from lup.resolver.mailbox import PendingQuestion, QuestionMailbox
from lup.resolver.models import (
    AnswerBatch,
    IntegrationRecord,
    MaterialQuestion,
    QuestionBatch,
    ResolvePhase,
    ResolveState,
    WritableRootLease,
)
from lup.resolver.recovery import IntegrationRecoveryDesk, IntegrationRecoveryMode
from lup.resolver.state import ResolverStateRepository, StateTransitionError
from lup.resolver.run import ResolverInvariantError
from tests.unit.test_resolver_core import (
    failure_leg_core,
    failure_leg_workspace,
    implementing_worker,
    planning_reviewer,
    resolve_spec,
    snapshot,
)


@dataclass
class RecoveryFixture:
    repository: ResolverStateRepository
    lease: WritableRootLease
    base: str

    def command(self, *arguments: str) -> str:
        return str(git(*arguments, _cwd=str(self.lease.root))).strip()


@pytest.fixture
def recovery(tmp_path: Path) -> RecoveryFixture:
    launcher = LocalProcessLauncher()
    source = failure_leg_workspace(tmp_path, launcher)
    base = snapshot(source, launcher)
    lease = WritableRootLease(
        concern_id="integration", root=tmp_path / "integration", branch="review"
    )
    git(
        "worktree",
        "add",
        "-b",
        lease.branch,
        str(lease.root),
        base.commit,
        _cwd=str(source),
    )
    repository = ResolverStateRepository(tmp_path / "state", "recover")
    repository.save(
        ResolveState(
            config_digest="fixture",
            run_id="recover",
            phase=ResolvePhase.FAILED,
            resume_from=ResolvePhase.VERIFICATION,
            source=base,
            spec=resolve_spec(),
            concerns=[],
            progress=[],
            answers=AnswerBatch(run_id="recover", answers=[]),
            leases=[lease],
            integration=IntegrationRecord(
                branch=lease.branch,
                worktree=lease.root,
                commit=base.commit,
                concerns=[],
            ),
        )
    )
    return RecoveryFixture(repository, lease, base.commit)


@pytest.mark.parametrize("split_index", [False, True])
def test_restore_preserves_conflict_index_staged_blobs_and_untracked_files(
    recovery: RecoveryFixture,
    split_index: bool,
) -> None:
    recovery.command("checkout", "-b", "other")
    (recovery.lease.root / "README.md").write_text("other\n")
    recovery.command("commit", "-am", "other")
    recovery.command("checkout", "review")
    (recovery.lease.root / "README.md").write_text("review\n")
    recovery.command("commit", "-am", "review")
    recorded = recovery.command("rev-parse", "HEAD")
    state = recovery.repository.load()
    assert state.integration is not None
    recovery.repository.save(
        state.model_copy(
            update={
                "integration": state.integration.model_copy(update={"commit": recorded})
            }
        )
    )
    git("merge", "other", _cwd=str(recovery.lease.root), _ok_code=1)
    staged = b"staged\x00binary\xff"
    (recovery.lease.root / "staged.bin").write_bytes(staged)
    recovery.command("add", "staged.bin")
    (recovery.lease.root / "staged.bin").write_bytes(b"unstaged contents")
    (recovery.lease.root / "untracked.txt").write_text("keep me")
    if split_index:
        recovery.command("update-index", "--split-index")
    index = Path(recovery.command("rev-parse", "--git-path", "index"))
    before_index = index.read_bytes()
    conflict = (recovery.lease.root / "README.md").read_bytes()

    result = IntegrationRecoveryDesk(recovery.repository).recover(
        IntegrationRecoveryMode.RESTORE
    )

    assert recovery.command("rev-parse", "HEAD") == recorded
    assert not Path(recovery.command("rev-parse", "--git-path", "MERGE_HEAD")).exists()
    assert (result.evidence / "git" / "index").read_bytes() == before_index
    assert (result.evidence / "git" / "MERGE_HEAD").is_file()
    with tarfile.open(result.evidence / "worktree.tar") as archive:
        for name, content in [
            ("README.md", conflict),
            ("staged.bin", b"unstaged contents"),
            ("untracked.txt", b"keep me"),
        ]:
            member = archive.extractfile(name)
            assert member is not None
            assert member.read() == content
    # The staged version remains recoverable even after unreachable blobs are pruned.
    recovery.command("gc", "--prune=now")
    with (result.evidence / "index.pack").open("rb") as packed:
        git("unpack-objects", _in=packed, _cwd=str(recovery.lease.root))
    shutil.copy2(result.evidence / "git" / "index", index)
    for shared in (result.evidence / "git").glob("sharedindex.*"):
        shutil.copy2(shared, index.parent / shared.name)
    restored = result.evidence / "staged-restored.bin"
    with restored.open("wb") as output:
        git("show", ":staged.bin", _out=output, _cwd=str(recovery.lease.root))
    assert restored.read_bytes() == staged


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_adopted_repair_resumes_with_fresh_verification(
    recovery: RecoveryFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
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
    question = MaterialQuestion(
        id="a-superseded-integrated" if legacy else "recheck-original-tree",
        concern_id="a",
        prompt="The original integration lost a criterion.",
        criteria=["criterion"],
        recheck_commit=None if legacy else recovery.base,
    )
    design = MaterialQuestion(id="design", concern_id="a", prompt="Keep the interface?")
    mailbox = QuestionMailbox(recovery.repository.root)
    for item in (question, design):
        mailbox.queue(
            PendingQuestion(
                run_id="recover", question=item, asked_by="reviewer", asked_at=utc_now()
            )
        )
    answer = QuestionAnswer(question_id="design", value="yes")
    mailbox.record(
        RecordedAnswer(
            run_id="recover",
            answer=answer,
            door=AnswerDoor.CONSOLE,
            answered_at=utc_now(),
        )
    )
    core.repository.save(
        core.repository.load().model_copy(
            update={
                "questions": QuestionBatch(
                    run_id="recover", questions=[question, design]
                ),
                "answers": AnswerBatch(run_id="recover", answers=[answer]),
            }
        )
    )
    (recovery.lease.root / "repair").write_text("fixed")
    recovery.command("add", "repair")
    recovery.command("commit", "-m", "repair")
    repaired = recovery.command("rev-parse", "HEAD")
    with pytest.raises(ResolverInvariantError, match="persisted commit changed"):
        await core.resume()
    IntegrationRecoveryDesk(recovery.repository).recover(IntegrationRecoveryMode.ADOPT)
    seen: list[str] = []

    async def recheck(
        state: ResolveState, integration: IntegrationRecord
    ) -> list[MaterialQuestion]:
        assert state.verification and all(
            record.passed for record in state.verification
        )
        assert integration.commit is not None
        seen.append(integration.commit)
        return []

    monkeypatch.setattr(core.joiner, "recheck_criteria", recheck)
    await core.resume()
    assert seen == [repaired]
    state = recovery.repository.load()
    assert state.phase == ResolvePhase.COMPLETE
    assert state.integration is not None and state.integration.completed
    assert state.retired_questions == [question.id]
    assert state.questions is not None and question in state.questions.questions
    assert state.answers is not None and answer in state.answers.answers
    assert [item.question.id for item in mailbox.questions()] == ["design"]
    assert len(mailbox.questions(include_retired=True)) == 2
    with pytest.raises(MailboxConflictError, match="retired"):
        mailbox.offer(
            AnswerOffer(
                run_id="recover",
                question_id=question.id,
                value="regression",
                door=AnswerDoor.CONSOLE,
                offered_at=utc_now(),
            )
        )


@pytest.mark.parametrize(
    "operation",
    ["CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"],
)
def test_recovery_refuses_other_git_operations(
    recovery: RecoveryFixture, operation: str
) -> None:
    admin = Path(recovery.command("rev-parse", "--absolute-git-dir"))
    (admin / operation).write_text(recovery.base)
    with pytest.raises(StateTransitionError, match="active Git operation"):
        IntegrationRecoveryDesk(recovery.repository).recover(
            IntegrationRecoveryMode.RESTORE
        )


def test_restore_retains_unrecorded_commit_and_uses_durable_join_landing(
    recovery: RecoveryFixture,
) -> None:
    (recovery.lease.root / "landed").write_text("recorded")
    recovery.command("add", "landed")
    recovery.command("commit", "-m", "landing")
    landed = recovery.command("rev-parse", "HEAD")
    recovery.repository.save(
        recovery.repository.load().model_copy(
            update={"integration": None, "resume_from": ResolvePhase.INTEGRATION}
        )
    )
    desk = JoinDesk(recovery.repository.root, "integration")
    desk.write_plan(
        JoinPlan(
            concern_id="integration",
            worktree=recovery.lease.root,
            base=recovery.base,
            title="join",
            purpose="review",
        )
    )
    desk.record(JoinLanding(commit=landed, head=landed), [landed])
    (recovery.lease.root / "extra").write_text("unrecorded")
    recovery.command("add", "extra")
    recovery.command("commit", "-m", "unrecorded")
    before = recovery.command("rev-parse", "HEAD")

    result = IntegrationRecoveryDesk(recovery.repository).recover(
        IntegrationRecoveryMode.RESTORE
    )

    assert result.after == landed
    assert recovery.command("rev-parse", "HEAD") == landed
    assert recovery.command("rev-parse", result.backup_ref) == before
    assert desk.progress().commit == landed


def test_adopt_descendant_preserves_answers_and_journals_the_move(
    recovery: RecoveryFixture,
) -> None:
    state = recovery.repository.load()
    (recovery.lease.root / "repair").write_text("fixed")
    recovery.command("add", "repair")
    recovery.command("commit", "-m", "repair")
    repaired = recovery.command("rev-parse", "HEAD")

    result = IntegrationRecoveryDesk(recovery.repository).recover(
        IntegrationRecoveryMode.ADOPT
    )

    adopted = recovery.repository.load()
    assert adopted.integration is not None and adopted.integration.commit == repaired
    assert not adopted.integration.completed
    assert adopted.answers == state.answers
    assert adopted.source == state.source
    assert adopted.verification == []
    assert (
        ResolveState.model_validate_json((result.evidence / "state.json").read_text())
        == state
    )
    event = Journal(recovery.repository.root).read()[-1].event
    assert isinstance(event, IntegrationRecoveredEvent)
    assert event.before == repaired and event.recorded == recovery.base


@pytest.mark.parametrize(
    "obstacle", ["active", "dirty", "unrelated", "unchanged", "complete"]
)
def test_recovery_refuses_invalid_authority(
    recovery: RecoveryFixture, obstacle: str
) -> None:
    if obstacle == "dirty":
        (recovery.lease.root / "README.md").write_text("pending")
    if obstacle == "unrelated":
        recovery.command("checkout", "--orphan", "unrelated")
        recovery.command("commit", "-am", "unrelated")
        recovery.command("branch", "-M", "review")
    if obstacle == "complete":
        recovery.repository.write_model(
            "state.json",
            recovery.repository.load().model_copy(
                update={"phase": ResolvePhase.COMPLETE}
            ),
        )
    before = recovery.repository.load()
    desk = IntegrationRecoveryDesk(recovery.repository)
    if obstacle == "active":
        with (
            recovery.repository.exclusive(),
            pytest.raises(StateTransitionError, match="already active"),
        ):
            desk.recover(IntegrationRecoveryMode.ADOPT)
    else:
        with pytest.raises(StateTransitionError):
            desk.recover(IntegrationRecoveryMode.ADOPT)
    assert recovery.repository.load() == before
    assert not (recovery.repository.root / "integration" / "recovery").exists()


def test_recovery_refusal_exits_nonzero(
    recovery: RecoveryFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "resolve_state_root", lambda: recovery.repository.root.parent
    )
    app = typer.Typer()
    app.command()(cli.recover_integration)
    result = CliRunner().invoke(app, ["adopt-head", "--run-id", "recover"])
    assert result.exit_code != 0
    assert "strict descendant" in result.output
