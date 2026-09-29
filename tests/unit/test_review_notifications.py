"""A recorded answer retains its independently verifiable notification outcome."""

from datetime import timedelta
from pathlib import Path

import pytest

from lup.devtools.dev.review_notifications import (
    ReviewNotification,
    ReviewNotificationRecord,
    ReviewNotifications,
)
from lup.policy.operations import Operation
from lup.policy.relay import Answer, PersistentQuestion
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath
from lup.devtools.dev import questions


@pytest.fixture
def answered(tmp_path: Path) -> PersistentQuestion:
    operation = Operation(
        id="operation",
        session="native-thread",
        requester="recipient",
        tool="Bash",
        payload={"command": "touch untouched"},
        cwd=tmp_path,
        worktree=tmp_path,
    )
    return PersistentQuestion(
        id="../untrusted-question-id",
        operation=operation,
        fingerprint=operation.fingerprint(),
        reason="Review this operation",
        rule="test",
        eligible=["operator"],
        answer=Answer(principal="operator", approved=True, note="decision"),
    )


def test_result_survives_another_reader_and_is_bound_to_the_exact_answer(
    tmp_path: Path, answered: PersistentQuestion
) -> None:
    store = ReviewNotifications(root=tmp_path)
    expected = ReviewNotification(
        queued=True, woken=False, detail="Mail is queued; receiver will retry."
    )
    result = store.notify((tmp_path,), answered, lambda roots, entry: expected)
    assert result == expected
    assert ReviewNotifications(root=tmp_path).read(answered) == result
    assert store.path(answered).is_relative_to(tmp_path / ".lup/review-notifications")
    assert store.read(answered.model_copy(update={"fingerprint": "another"})) is None
    assert answered.answer is not None
    assert (
        store.read(
            answered.model_copy(
                update={
                    "answer": answered.answer.model_copy(
                        update={"at": answered.answer.at + timedelta(seconds=1)}
                    )
                }
            )
        )
        is None
    )
    assert not (tmp_path / "untouched").exists()


def test_failure_is_durable_and_does_not_change_the_answer(
    tmp_path: Path, answered: PersistentQuestion
) -> None:
    def fail(roots: tuple[Path, ...], entry: PersistentQuestion) -> ReviewNotification:
        assert roots == (tmp_path,) and entry == answered
        raise OSError("Native receiver is unavailable")

    store = ReviewNotifications(root=tmp_path)
    result = store.notify((tmp_path,), answered, fail)
    assert not result.queued and not result.woken
    assert "Native receiver is unavailable" in result.detail
    assert store.read(answered) == result
    record = ReviewNotificationRecord.model_validate_json(
        store.path(answered).read_bytes()
    )
    assert record.question == answered.id
    assert record.attempted_at >= record.answered_at
    assert answered.answer is not None and answered.answer.approved


def test_interrupted_attempt_remains_explicitly_unconfirmed(
    tmp_path: Path, answered: PersistentQuestion
) -> None:
    def interrupted(
        roots: tuple[Path, ...], entry: PersistentQuestion
    ) -> ReviewNotification:
        raise KeyboardInterrupt

    store = ReviewNotifications(root=tmp_path)
    with pytest.raises(KeyboardInterrupt):
        store.notify((tmp_path,), answered, interrupted)
    result = store.read(answered)
    assert result is not None and not result.queued and not result.woken
    assert "no confirmed outcome" in result.detail


@pytest.mark.parametrize("failed_write", [1, 2])
def test_persistence_failure_keeps_the_answer_and_attempts_delivery(
    tmp_path: Path,
    answered: PersistentQuestion,
    monkeypatch: pytest.MonkeyPatch,
    failed_write: int,
) -> None:
    writes = 0
    attempts = 0
    original = ReviewNotifications.write

    def write(
        store: ReviewNotifications,
        entry: PersistentQuestion,
        outcome: ReviewNotification,
    ) -> None:
        nonlocal writes
        writes += 1
        if writes == failed_write:
            raise OSError("Diagnostic store unavailable")
        original(store, entry, outcome)

    def deliver(
        roots: tuple[Path, ...], entry: PersistentQuestion
    ) -> ReviewNotification:
        nonlocal attempts
        attempts += 1
        return ReviewNotification(queued=True, woken=False, detail="Mail queued.")

    monkeypatch.setattr(ReviewNotifications, "write", write)
    store = ReviewNotifications(root=tmp_path)
    outcome = store.notify((tmp_path,), answered, deliver)
    assert attempts == 1 and outcome.queued
    assert "Diagnostic store unavailable" in outcome.detail
    assert answered.answer is not None and answered.answer.approved
    persisted = store.read(answered)
    assert persisted is not None
    if failed_write == 1:
        assert persisted == outcome
    else:
        assert "no confirmed outcome" in persisted.detail


def test_corrupt_diagnostics_remain_visible_without_hiding_the_answer(
    tmp_path: Path,
    answered: PersistentQuestion,
) -> None:
    store = ReviewNotifications(root=tmp_path)
    store.path(answered).parent.mkdir(parents=True)
    store.path(answered).write_text("not json")
    outcome = store.read(answered)
    assert outcome is not None and not outcome.queued and not outcome.woken
    assert "unreadable" in outcome.detail
    assert answered.answer is not None and answered.answer.approved


@pytest.mark.parametrize("bound_thread", ["another-native-thread", ""])
def test_native_retry_never_notifies_a_rebound_or_unbound_member(
    tmp_path: Path,
    answered: PersistentQuestion,
    bound_thread: str,
) -> None:
    entry = answered.model_copy(update={"resumption": "native_retry"})
    peers = RepositoryPeers(tmp_path)
    peers.join(
        "recipient",
        tmp_path,
        wake=WakePath(runtime="codex", session=bound_thread, handle=bound_thread),
    )
    outcome = questions.notify_requester((tmp_path,), entry)
    assert not outcome.queued and not outcome.woken
    assert peers.waiting("recipient").messages == []
