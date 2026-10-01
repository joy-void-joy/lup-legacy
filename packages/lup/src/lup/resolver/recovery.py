"""Explicit reconciliation of a stopped run's integration worktree."""

import shutil
import tarfile
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from lup.channels.models import Door, publish_atomic, utc_now
from lup.coordination.mailbox import RecordedAnswer
from lup.execution.shell import git
from lup.execution.process import LaunchRequest, LocalProcessLauncher
from lup.resolver.join_desk import JoinDesk
from lup.resolver.record import IntegrationRecoveredEvent, Journal
from lup.resolver.mailbox import PendingQuestion, QuestionMailbox
from lup.resolver.models import (
    AnswerBatch,
    QuestionBatch,
    ResolvePhase,
    ResolveState,
    WritableRootLease,
)
from lup.resolver.orchestrator import WorktreeOrchestrator
from lup.resolver.state import ResolverStateRepository, StateTransitionError


class IntegrationRecoveryMode(StrEnum):
    RESTORE = "restore-recorded"
    ADOPT = "adopt-head"


class IntegrationRecovery(BaseModel, frozen=True):
    """The exact move requested, with its retained evidence directory."""

    mode: IntegrationRecoveryMode
    recorded: str
    before: str
    after: str
    evidence: Path
    worktree: Path
    backup_ref: str
    retired_questions: list[str] = []


class RecoveryReference(BaseModel, frozen=True):
    """A saved Git metadata target retained through garbage collection."""

    source: str
    reference: str
    object_id: str


class RecoverySnapshot(BaseModel, frozen=True):
    references: list[RecoveryReference]


class RecoveryQuestionImport(BaseModel, frozen=True):
    """Current import provenance; original authors and timestamps are unknown."""

    imported_questions: list[str]
    imported_answers: list[str]
    conflicting_questions: list[str]
    conflicting_answers: list[str]


class IndexSnapshotObject(BaseModel, frozen=True):
    """The mode and object emitted by Git's formatted index reader."""

    mode: Literal["100644", "100755", "120000", "040000", "160000"]
    object_id: str

    def superproject(self) -> bool:
        """Gitlinks name objects owned by another repository."""
        return self.mode != "160000"


class IntegrationRecoveryDesk:
    """Recover only under the run lease and a short state transaction."""

    def __init__(self, repository: ResolverStateRepository) -> None:
        self.repository = repository

    def recover(self, mode: IntegrationRecoveryMode) -> IntegrationRecovery:
        with self.repository.exclusive(), self.repository.writing():
            return self.recover_locked(mode)

    def recover_locked(self, mode: IntegrationRecoveryMode) -> IntegrationRecovery:
        state = self.repository.load()
        phase = state.resume_from if state.phase == ResolvePhase.FAILED else state.phase
        if phase not in {ResolvePhase.INTEGRATION, ResolvePhase.VERIFICATION}:
            raise StateTransitionError(
                f"integration recovery requires an interrupted integration or "
                f"verification phase; this run is {state.phase}"
            )
        lease = next(
            (item for item in state.leases if item.concern_id == "integration"), None
        )
        if lease is None or not lease.root.is_dir():
            raise StateTransitionError("the recorded integration worktree is missing")
        worktrees = WorktreeOrchestrator(LocalProcessLauncher(), lease.root)
        worktrees.branch(lease)
        admin = self.admin_path(lease, worktrees)
        interrupted = [
            name
            for name in (
                "CHERRY_PICK_HEAD",
                "REVERT_HEAD",
                "rebase-merge",
                "rebase-apply",
                "sequencer",
            )
            if (admin / name).exists()
        ]
        if interrupted:
            raise StateTransitionError(
                "finish or abort the active Git operation before integration recovery: "
                + ", ".join(interrupted)
            )
        recorded = self.recorded_commit(state, lease)
        if not worktrees.resolved(recorded):
            raise StateTransitionError(
                f"recorded integration commit is missing: {recorded}"
            )
        before = worktrees.head(lease)
        match mode:
            case IntegrationRecoveryMode.ADOPT:
                if state.integration is None or state.integration.commit is None:
                    raise StateTransitionError(
                        "adoption requires a recorded integration result; finish or "
                        "restore the interrupted join sequence first"
                    )
                merge = worktrees.launcher.launch(
                    LaunchRequest(
                        arguments=["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                        cwd=lease.root,
                    )
                )
                if merge.code not in {0, 1}:
                    raise StateTransitionError(
                        "cannot inspect the integration merge state"
                    )
                if merge.code == 0 or worktrees.uncommitted(lease):
                    raise StateTransitionError(
                        "adoption requires a clean, committed worktree"
                    )
                if before == recorded or not worktrees.reachable(before, recorded):
                    raise StateTransitionError(
                        "adoption requires HEAD to be a strict descendant of the recorded commit"
                    )
                after = before
            case IntegrationRecoveryMode.RESTORE:
                after = recorded
        identifier = uuid4().hex
        evidence = self.repository.root / "integration" / "recovery" / identifier
        if evidence.resolve().is_relative_to(lease.root.resolve()):
            raise StateTransitionError(
                "recovery evidence must live outside the integration worktree"
            )
        publish_atomic(evidence / "state.json", state)
        state = self.import_questions(state, evidence)
        recovery = IntegrationRecovery(
            mode=mode,
            recorded=recorded,
            before=before,
            after=after,
            evidence=evidence,
            worktree=lease.root,
            backup_ref=f"refs/lup/recovery/{identifier}/head",
            retired_questions=[
                item.question.id
                for item in QuestionMailbox(self.repository.root).questions()
                if mode == IntegrationRecoveryMode.ADOPT
                and (
                    (
                        item.question.recheck_commit is not None
                        and worktrees.reachable(recorded, item.question.recheck_commit)
                    )
                    or (
                        item.question.recheck_commit is None
                        and item.question.criteria
                        and item.question.id
                        == f"{item.question.concern_id}-superseded-integrated"
                    )
                )
            ],
        )
        publish_atomic(evidence / "request.json", recovery)
        worktrees.require(
            LaunchRequest(
                arguments=["git", "update-ref", recovery.backup_ref, before, ""],
                cwd=lease.root,
            ),
            "cannot preserve the pre-recovery integration commit",
        )
        match mode:
            case IntegrationRecoveryMode.RESTORE:
                self.preserve_worktree(lease, evidence, worktrees, recovery.backup_ref)
                worktrees.require(
                    LaunchRequest(
                        arguments=["git", "reset", "--hard", after], cwd=lease.root
                    ),
                    f"cannot restore integration; recovery evidence is in {evidence}",
                )
                self.repository.save_locked(state)
            case IntegrationRecoveryMode.ADOPT:
                integration = state.integration
                if integration is None:
                    raise StateTransitionError("integration record disappeared")
                adopted = state.model_copy(
                    update={
                        "integration": integration.model_copy(
                            update={"commit": after, "completed": False}
                        ),
                        "verification": [],
                        "retired_questions": [
                            *state.retired_questions,
                            *recovery.retired_questions,
                        ],
                        "outcomes": [
                            outcome.model_copy(update={"regressed": []})
                            for outcome in state.outcomes
                        ],
                    }
                )
                self.repository.save_locked(adopted)
        Journal(self.repository.root).record(
            IntegrationRecoveredEvent(
                mode=mode.value,
                recorded=recorded,
                before=before,
                after=after,
                evidence=str(evidence),
                retired_questions=recovery.retired_questions,
            )
        )
        publish_atomic(evidence / "completed.json", recovery)
        return recovery

    def import_questions(self, state: ResolveState, evidence: Path) -> ResolveState:
        """Restore state-only decisions into the authoritative mailbox under run leases.

        The archived state retains every original value. Existing mailbox records
        win overlaps, and an answer cannot cross a changed question domain. Import
        timestamps and the recovery door describe this operation, not the unknown
        original decision-maker or time.
        """
        mailbox = QuestionMailbox(self.repository.root)
        original = {
            question.id: question
            for question in (state.questions.questions if state.questions else [])
        }
        existing = {
            item.question.id: item.question
            for item in mailbox.questions(include_retired=True)
        }
        conflicting_questions = [
            identifier
            for identifier, question in original.items()
            if identifier in existing and not question.restates(existing[identifier])
        ]
        answers = state.answers.answers if state.answers else []
        for answer in answers:
            if mailbox.settled_answer(answer.question_id) is not None:
                continue
            if answer.question_id in conflicting_questions:
                continue
            question = original.get(answer.question_id)
            if question is None or (
                question.closed_choices and answer.value not in question.choices
            ):
                raise StateTransitionError(
                    f"cannot import recorded answer {answer.question_id!r} without its "
                    "matching original question and valid answer domain; "
                    f"original state is retained in {evidence / 'state.json'}"
                )
        imported_questions = [
            identifier for identifier in original if identifier not in existing
        ]
        imported_at = utc_now()
        for identifier in imported_questions:
            mailbox.queue(
                PendingQuestion(
                    run_id=state.run_id,
                    question=original[identifier],
                    asked_by="recovery:state-import",
                    asked_at=imported_at,
                )
            )
        existing_answers = {
            record.answer.question_id: record.answer for record in mailbox.answers()
        }
        conflicting_answers = [
            answer.question_id
            for answer in answers
            if answer.question_id in conflicting_questions
            or (
                answer.question_id in existing_answers
                and existing_answers[answer.question_id] != answer
            )
        ]
        imported_answers = [
            answer.question_id
            for answer in answers
            if answer.question_id not in conflicting_questions
            and answer.question_id not in existing_answers
            and mailbox.record(
                RecordedAnswer(
                    run_id=state.run_id,
                    answer=answer,
                    door=Door.RECOVERY,
                    answered_at=imported_at,
                )
            )
        ]
        publish_atomic(
            evidence / "mailbox-import.json",
            RecoveryQuestionImport(
                imported_questions=imported_questions,
                imported_answers=imported_answers,
                conflicting_questions=conflicting_questions,
                conflicting_answers=conflicting_answers,
            ),
        )
        questions = [item.question for item in mailbox.questions(include_retired=True)]
        recorded = [item.answer for item in mailbox.answers()]
        return state.model_copy(
            update={
                "questions": QuestionBatch(run_id=state.run_id, questions=questions)
                if questions or state.questions is not None
                else None,
                "answers": AnswerBatch(run_id=state.run_id, answers=recorded)
                if recorded or state.answers is not None
                else None,
            }
        )

    def recorded_commit(self, state: ResolveState, lease: WritableRootLease) -> str:
        """Use the merger's durable landing when the process died before projection."""
        if state.integration is not None and state.integration.commit is not None:
            return state.integration.commit
        desk = JoinDesk(self.repository.root, lease.concern_id)
        plan = desk.plan()
        if plan is not None and plan.worktree.resolve() != lease.root.resolve():
            raise StateTransitionError(
                "the integration join plan names another worktree"
            )
        landed = desk.progress().commit
        if landed:
            return landed
        if state.join_progress is not None:
            return state.join_progress.commit
        return state.root_base().commit

    def preserve_worktree(
        self,
        lease: WritableRootLease,
        evidence: Path,
        worktrees: WorktreeOrchestrator,
        backup_ref: str,
    ) -> None:
        """Keep files, index objects and merge metadata before a destructive reset."""
        with tarfile.open(evidence / "worktree.tar", "w") as archive:
            for path in sorted(lease.root.iterdir()):
                if path.name != ".git":
                    archive.add(path, arcname=path.name)
        admin = self.admin_path(lease, worktrees)
        metadata = evidence / "git"
        metadata.mkdir()
        for name in (
            "index",
            "HEAD",
            "ORIG_HEAD",
            "MERGE_HEAD",
            "MERGE_MSG",
            "MERGE_MODE",
            "AUTO_MERGE",
        ):
            if (admin / name).is_file():
                shutil.copy2(admin / name, metadata / name)
        shared = worktrees.require(
            LaunchRequest(
                arguments=["git", "rev-parse", "--shared-index-path"], cwd=lease.root
            ),
            "cannot inspect the split integration index",
        ).stdout.strip()
        if shared:
            shared_path = lease.root / shared
            shutil.copy2(shared_path, metadata / shared_path.name)
        references = [
            RecoveryReference(
                source="HEAD",
                reference=backup_ref,
                object_id=worktrees.head(lease),
            )
        ]
        for name in ("ORIG_HEAD", "MERGE_HEAD", "AUTO_MERGE"):
            saved = metadata / name
            if not saved.is_file():
                continue
            # Pseudo refs contain one object id per line, including each head of
            # an octopus merge. Preserve all targets without repacking their history.
            for index, object_id in enumerate(
                saved.read_text(encoding="ascii").splitlines()
            ):
                reference = f"{PurePosixPath(backup_ref).parent}/{name}/{index}"
                worktrees.require(
                    LaunchRequest(
                        arguments=[
                            "git",
                            "-c",
                            "core.fsync=reference",
                            "update-ref",
                            reference,
                            object_id,
                            "",
                        ],
                        cwd=lease.root,
                    ),
                    f"cannot retain saved {name} object; recovery evidence is in {evidence}",
                )
                references.append(
                    RecoveryReference(
                        source=name, reference=reference, object_id=object_id
                    )
                )
        publish_atomic(
            evidence / "references.json", RecoverySnapshot(references=references)
        )
        # A copied index can reference staged blobs no commit retains. Pack those
        # objects as well, so garbage collection cannot make the snapshot useless.
        objects = worktrees.require(
            LaunchRequest(
                arguments=[
                    "git",
                    "ls-files",
                    "--sparse",
                    '--format={"mode":"%(objectmode)","object_id":"%(objectname)"}',
                ],
                cwd=lease.root,
            ),
            "cannot enumerate staged integration objects",
        ).stdout
        retained = [
            entry.object_id
            for line in objects.splitlines()
            for entry in [IndexSnapshotObject.model_validate_json(line)]
            if entry.superproject()
        ]
        with (evidence / "index.pack").open("wb") as output:
            git(
                "pack-objects",
                "--stdout",
                "--revs",
                _in="".join(f"{object_id}\n" for object_id in retained),
                _out=output,
                _cwd=str(lease.root),
            )

    def admin_path(
        self, lease: WritableRootLease, worktrees: WorktreeOrchestrator
    ) -> Path:
        return Path(
            worktrees.require(
                LaunchRequest(
                    arguments=["git", "rev-parse", "--absolute-git-dir"], cwd=lease.root
                ),
                "cannot locate the integration index",
            ).stdout.strip()
        )
