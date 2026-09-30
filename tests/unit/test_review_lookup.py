"""Review selection reads fresh authority without projecting unrelated history."""

from pathlib import Path
from typing import Literal
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi import HTTPException

from lup.devtools.dashboard import reviews as dashboard
from lup.devtools.dashboard.reviews import ReviewAnswer, ReviewStore
from lup.devtools.review import app as questions
from lup.devtools.review import propose as proposals
from lup.devtools.review.app import ReviewSummary
from lup.devtools.review.notifications import (
    ReviewNotification,
    ReviewNotifications,
)
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion
from lup.policy.review import reviewed_preview
from lup.providers.harness import patch_review
from tests.unit.test_dashboard_reviews import parked


def test_review_keys_preserve_the_existing_root_and_question_identity(
    tmp_path: Path,
) -> None:
    entry = parked(tmp_path)
    expected = str(uuid5(NAMESPACE_URL, f"{tmp_path.as_uri()}#{entry.id}"))
    assert ReviewSummary.key_for(tmp_path, entry.id) == expected
    assert ReviewSummary.of(tmp_path, entry, "operator").key == expected
    assert ReviewSummary.key_for(tmp_path / "other", entry.id) != expected
    assert ReviewSummary.key_for(tmp_path, "another-question") != expected


@pytest.mark.parametrize("action", ["locate", "detail", "answer"])
def test_selection_never_projects_unrelated_questions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: Literal["locate", "detail", "answer"],
) -> None:
    other, selected = tmp_path / "other", tmp_path / "selected"
    unrelated = parked(other, "chosen")
    preceding = parked(selected, "preceding")
    entry = parked(selected, "chosen")
    store = ReviewStore(roots=(other, selected))
    key = ReviewSummary.key_for(selected, entry.id)
    standalone = Mock(side_effect=AssertionError("Unneeded standalone projection"))
    preview = Mock(wraps=reviewed_preview)
    monkeypatch.setattr(ReviewSummary, "of", standalone)
    monkeypatch.setattr(proposals, "reviewed_preview", preview)

    match action:
        case "locate":
            assert store.locate(key).question == entry.recorded()
            preview.assert_not_called()
        case "detail":
            assert store.detail(key).question == entry.shown()
            preview.assert_called_once_with(entry, patch_review)
        case "answer":
            result = store.answer(
                key, ReviewAnswer(approved=True, fingerprint=entry.fingerprint)
            )
            assert result.review.question.state == "approved"
            ((shown, reader),) = [call.args for call in preview.call_args_list]
            assert (shown.id, shown.state, reader) == (
                entry.id,
                "approved",
                patch_review,
            )
    standalone.assert_not_called()
    assert questions.relay(other).find(unrelated.id) == unrelated.recorded()
    assert questions.relay(selected).find(preceding.id) == preceding.recorded()


@pytest.mark.parametrize("unavailable", [False, True])
def test_lookup_preserves_missing_key_and_unavailable_queue_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable: bool
) -> None:
    broken, healthy = tmp_path / "broken", tmp_path / "healthy"
    if unavailable:
        questions.relay(broken).path.mkdir(parents=True)
    entry = parked(healthy)
    store = ReviewStore(roots=(broken, healthy))
    standalone = Mock(side_effect=AssertionError("Lookup must not project history"))
    monkeypatch.setattr(ReviewSummary, "of", standalone)

    assert (
        store.locate(ReviewSummary.key_for(healthy, entry.id)).question
        == entry.recorded()
    )
    with pytest.raises(HTTPException) as error:
        store.locate("unknown")
    assert error.value.status_code == (503 if unavailable else 404)
    standalone.assert_not_called()


def test_detail_and_answer_recheck_live_preimages_on_every_request(
    tmp_path: Path,
) -> None:
    target = tmp_path / "app.txt"
    target.write_text("before\n")
    operation = Operation(
        id="write",
        session="requester",
        requester="requester",
        tool="Write",
        payload={"file_path": str(target), "content": "after\n"},
        cwd=tmp_path,
        worktree=tmp_path,
    )
    entry = questions.relay(tmp_path).record(
        PersistentQuestion(
            id="write-review",
            operation=operation,
            fingerprint=operation.fingerprint(),
            reason="Review the replacement",
            eligible=["operator"],
            preconditions={target: "before\n"},
        )
    )
    store = ReviewStore(roots=(tmp_path,))
    key = ReviewSummary.key_for(tmp_path, entry.id)
    first = store.detail(key)
    assert first.summary.state == "pending"
    assert first.files[0].after == "after\n"
    assert first.summary.paths == ["app.txt"]

    target.write_text("changed\n")
    assert store.detail(key).summary.state == "stale"
    with pytest.raises(HTTPException) as error:
        store.answer(key, ReviewAnswer(approved=True, fingerprint=entry.fingerprint))
    assert error.value.status_code == 409
    stored = questions.relay(tmp_path).find(entry.id)
    assert stored is not None and stored.state == "stale" and stored.answer is None


def test_an_answer_carries_how_the_requester_heard_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = parked(tmp_path)
    expected = ReviewNotification(queued=True, woken=True, detail="Requester notified.")

    def deliver(
        roots: tuple[Path, ...], root: Path, question: PersistentQuestion
    ) -> ReviewNotification:
        assert roots == (tmp_path,)
        assert root == tmp_path
        assert question.state == "approved"
        return expected

    monkeypatch.setattr(dashboard, "notify_requester", deliver)
    decision = ReviewStore(roots=(tmp_path,)).answer(
        ReviewSummary.key_for(tmp_path, entry.id),
        ReviewAnswer(approved=True, fingerprint=entry.fingerprint),
    )

    assert decision.notification == expected
    assert ReviewNotifications(root=tmp_path).read(decision.review.question) == expected


def test_failed_delivery_keeps_the_recorded_answer_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = parked(tmp_path)
    store = ReviewStore(roots=(tmp_path,))
    failed = Mock(side_effect=OSError("Selected checkout became unavailable"))
    monkeypatch.setattr(ReviewStore, "checkout_roots", failed)

    decision = store.answer(
        ReviewSummary.key_for(tmp_path, entry.id),
        ReviewAnswer(approved=True, fingerprint=entry.fingerprint),
    )

    assert not decision.notification.queued and not decision.notification.woken
    assert "Selected checkout became unavailable" in decision.notification.detail
    outcome = ReviewNotifications(root=tmp_path).read(decision.review.question)
    assert outcome == decision.notification
    assert questions.relay(tmp_path).find(entry.id) == decision.review.question
