"""Review selection reads fresh authority without projecting unrelated history."""

import asyncio
from pathlib import Path
from threading import Event
from typing import Literal
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi import BackgroundTasks, HTTPException
from starlette.types import Message, Scope

from lup.devtools.dev import questions
from lup.devtools.dev.questions import (
    ReviewAnswer,
    ReviewDecision,
    ReviewStore,
    ReviewSummary,
)
from lup.devtools.dev.review_notifications import (
    ReviewNotification,
    ReviewNotifications,
)
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion
from lup.policy.review import reviewed_preview
from lup.providers.harness import patch_review
from tests.unit.test_review_inbox import BASE_URL, TOKEN, parked


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
    monkeypatch.setattr(questions, "reviewed_files", standalone)
    monkeypatch.setattr(questions, "reviewed_preview", preview)

    match action:
        case "locate":
            assert store.locate(key).question == entry
            preview.assert_not_called()
        case "detail":
            assert store.detail(key).question == entry
            preview.assert_called_once_with(entry, patch_review)
        case "answer":
            result = store.answer(
                key, ReviewAnswer(approved=True, fingerprint=entry.fingerprint)
            )
            assert result.review.question.state == "approved"
            preview.assert_called_once_with(result.review.question, patch_review)
    standalone.assert_not_called()
    assert questions.relay(other).find(unrelated.id) == unrelated
    assert questions.relay(selected).find(preceding.id) == preceding


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

    assert store.locate(ReviewSummary.key_for(healthy, entry.id)).question == entry
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
    assert first.stale_reason == ""
    assert first.files[0].after == "after\n"
    assert first.summary.paths == ["app.txt"]

    target.write_text("changed\n")
    assert store.detail(key).stale_reason
    with pytest.raises(HTTPException) as error:
        store.answer(key, ReviewAnswer(approved=True, fingerprint=entry.fingerprint))
    assert error.value.status_code == 409
    assert questions.relay(tmp_path).find(entry.id) == entry


async def test_http_answers_return_while_an_owned_notification_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = parked(tmp_path, "first"), parked(tmp_path, "second")
    entered, released = Event(), Event()
    expected = ReviewNotification(queued=True, woken=True, detail="Requester notified.")

    def deliver(
        roots: tuple[Path, ...], entry: PersistentQuestion
    ) -> ReviewNotification:
        assert roots == (tmp_path,)
        if entry.id == first.id:
            entered.set()
            if not released.wait(timeout=5):
                raise TimeoutError("The HTTP response waited for requester delivery")
        return expected

    monkeypatch.setattr(questions, "notify_requester", deliver)
    app = questions.review_app(BASE_URL, TOKEN, (tmp_path,))

    async def request(
        entry: PersistentQuestion, result: asyncio.Future[ReviewDecision]
    ) -> None:
        key = ReviewSummary.key_for(tmp_path, entry.id)
        body = (
            ReviewAnswer(approved=True, fingerprint=entry.fingerprint)
            .model_dump_json()
            .encode()
        )
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"/api/reviews/{key}/answer",
            "raw_path": f"/api/reviews/{key}/answer".encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"127.0.0.1:8765"),
                (b"authorization", f"Bearer {TOKEN}".encode()),
                (b"origin", BASE_URL.encode()),
                (b"content-type", b"application/json"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8765),
        }
        requested, disconnected = asyncio.Event(), asyncio.Event()
        received = bytearray()

        async def receive() -> Message:
            if not requested.is_set():
                requested.set()
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            match message["type"]:
                case "http.response.start":
                    assert message["status"] == 200
                case "http.response.body":
                    received.extend(message.get("body", b""))
                    if not message.get("more_body", False):
                        result.set_result(ReviewDecision.model_validate_json(received))

        await app(scope, receive, send)

    loop = asyncio.get_running_loop()
    first_result: asyncio.Future[ReviewDecision] = loop.create_future()
    second_result: asyncio.Future[ReviewDecision] = loop.create_future()
    first_task = asyncio.create_task(request(first, first_result))
    second_task: asyncio.Task[None] | None = None
    try:
        response = await asyncio.wait_for(first_result, timeout=2)
        assert await asyncio.to_thread(entered.wait, 2)
        assert not first_task.done()
        assert response.review.question.state == "approved"
        assert questions.relay(tmp_path).find(first.id) == response.review.question
        pending = ReviewNotifications(root=tmp_path).read(response.review.question)
        assert pending == response.notification
        assert pending is not None and "no confirmed outcome" in pending.detail
        second_task = asyncio.create_task(request(second, second_result))
        following = await asyncio.wait_for(second_result, timeout=2)
        assert following.review.question.state == "approved"
        assert not released.is_set()
    finally:
        released.set()
        await asyncio.wait_for(
            asyncio.gather(
                first_task, *([second_task] if second_task is not None else [])
            ),
            timeout=3,
        )
    assert ReviewNotifications(root=tmp_path).read(response.review.question) == expected


@pytest.mark.parametrize("run_delivery", [False, True])
async def test_unstarted_or_failed_background_delivery_keeps_the_recorded_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_delivery: bool
) -> None:
    entry = parked(tmp_path)
    background = BackgroundTasks()
    store = ReviewStore(roots=(tmp_path,))
    failed = Mock(side_effect=OSError("Selected checkout became unavailable"))
    monkeypatch.setattr(ReviewStore, "checkout_roots", failed)
    decision = store.answer(
        ReviewSummary.key_for(tmp_path, entry.id),
        ReviewAnswer(approved=True, fingerprint=entry.fingerprint),
        background,
    )
    failed.assert_not_called()
    assert len(background.tasks) == 1
    pending = ReviewNotifications(root=tmp_path).read(decision.review.question)
    assert pending == decision.notification
    if run_delivery:
        await background()
        outcome = ReviewNotifications(root=tmp_path).read(decision.review.question)
        assert outcome is not None and not outcome.queued and not outcome.woken
        assert "Selected checkout became unavailable" in outcome.detail
    else:
        assert pending is not None and "no confirmed outcome" in pending.detail
    assert questions.relay(tmp_path).find(entry.id) == decision.review.question
