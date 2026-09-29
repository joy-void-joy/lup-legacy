"""A browser decision retains the relay's authority and the operation shown."""

import asyncio
import json
import secrets
import subprocess
import sys
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent
from typing import Final

import pytest
import sh
import uvicorn
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Scope
from typer.testing import CliRunner

from lup.channels.models import Door
from lup.coordination.refs import ActorRef
from lup.coordination.repository import RepositoryPeers
from lup.coordination.roster import RosterMember
from lup.coordination.wake import WakePath, Woken
from lup.devtools.dev import questions
from lup.devtools.dev.questions import (
    ReviewDecision,
    ReviewDetail,
    ReviewFile,
    ReviewInbox,
    ReviewLine,
    ReviewSuppression,
)
from lup.devtools.dev.review_notifications import (
    ReviewNotification,
    ReviewNotifications,
)
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion, QuestionRelay
from lup.policy.review import ReviewedFile
from lup.web import serve as web_serve
from lup.web.serve import page_app

BASE_URL: Final = "http://127.0.0.1:8765"
TOKEN: Final = "test-operator-secret"
AUTHORIZATION: Final = {"Authorization": f"Bearer {TOKEN}"}
ANSWER_HEADERS: Final = {**AUTHORIZATION, "Origin": BASE_URL}


@pytest.fixture(autouse=True)
def isolated_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the real Host guard without making API tests build JavaScript."""

    def build(title: str, url: str, surface: str) -> FastAPI:
        assert surface == "reviews"
        return page_app(title, url, "<!doctype html><main>Review inbox</main>")

    monkeypatch.setattr(web_serve, "bundle_app", build)


def client(*roots: Path, discover: bool = False) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(
            app=questions.review_app(BASE_URL, TOKEN, roots, discover=discover)
        ),
        base_url=BASE_URL,
    )


def parked(root: Path, question_id: str = "q-1") -> PersistentQuestion:
    """A real durable question, answerable by the operator who serves the page."""
    operation = Operation(
        id=f"operation-{question_id}",
        session="absent-session",
        requester="absent-worker",
        tool="Bash",
        payload={"command": f"touch {root / 'must-not-execute'}"},
        cwd=root,
        worktree=root,
    )
    return questions.relay(root).record(
        PersistentQuestion(
            id=question_id,
            operation=operation,
            fingerprint=operation.fingerprint(),
            reason="The operator reviews this command before it may run.",
            rule="shell:test",
            eligible=["operator"],
            resumption="native_retry",
        )
    )


async def only_key(http: AsyncClient) -> str:
    response = await http.get("/api/reviews", headers=AUTHORIZATION)
    assert response.status_code == 200
    snapshot = ReviewInbox.model_validate(response.json())
    assert len(snapshot.reviews) == 1
    return snapshot.reviews[0].key


@pytest.mark.parametrize(
    "route", ["/api/reviews", "/api/reviews/unknown", "/api/events"]
)
@pytest.mark.parametrize("authorization", ["", "Bearer wrong", f"Basic {TOKEN}"])
async def test_api_reads_require_the_operator_token(
    tmp_path: Path, route: str, authorization: str
) -> None:
    parked(tmp_path)
    async with client(tmp_path) as http:
        response = await http.get(route, headers={"Authorization": authorization})

    assert response.status_code == 401
    assert "absent-worker" not in response.text


async def test_public_page_does_not_disclose_the_token(tmp_path: Path) -> None:
    async with client(tmp_path) as http:
        page = await http.get("/")
        wrong_host = await http.get("/", headers={"Host": "attacker.example"})
        wrong_api_host = await http.get(
            "/api/reviews", headers={**AUTHORIZATION, "Host": "attacker.example"}
        )

    assert page.status_code == 200
    assert TOKEN not in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert wrong_host.status_code == 421
    assert wrong_api_host.status_code == 421


async def test_authenticated_reads_are_not_cached(tmp_path: Path) -> None:
    parked(tmp_path)
    async with client(tmp_path) as http:
        response = await http.get("/api/reviews", headers=AUTHORIZATION)

    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize(
    "origin", [None, "null", "http://attacker.example", BASE_URL + "/"]
)
async def test_answer_requires_the_exact_origin(
    tmp_path: Path, origin: str | None
) -> None:
    entry = parked(tmp_path)
    headers = {**AUTHORIZATION, **({"Origin": origin} if origin is not None else {})}
    async with client(tmp_path) as http:
        key = await only_key(http)
        response = await http.post(
            f"/api/reviews/{key}/answer",
            headers=headers,
            json={"approved": True, "note": "", "fingerprint": entry.fingerprint},
        )

    assert response.status_code == 403
    assert questions.relay(tmp_path).find(entry.id) == entry


async def test_answer_requires_authentication_even_with_the_correct_origin(
    tmp_path: Path,
) -> None:
    entry = parked(tmp_path)
    async with client(tmp_path) as http:
        key = await only_key(http)
        http.cookies.clear()
        response = await http.post(
            f"/api/reviews/{key}/answer",
            headers={"Origin": BASE_URL},
            json={"approved": True, "note": "", "fingerprint": entry.fingerprint},
        )

    assert response.status_code == 401
    assert questions.relay(tmp_path).find(entry.id) == entry


@pytest.mark.parametrize(
    "content_type", ["text/plain", "application/x-www-form-urlencoded"]
)
async def test_answer_refuses_browser_simple_request_media_types(
    tmp_path: Path, content_type: str
) -> None:
    entry = parked(tmp_path)
    async with client(tmp_path) as http:
        key = await only_key(http)
        response = await http.post(
            f"/api/reviews/{key}/answer",
            headers={**ANSWER_HEADERS, "Content-Type": content_type},
            content=json.dumps(
                {"approved": True, "note": "", "fingerprint": entry.fingerprint}
            ),
        )

    assert response.status_code == 415
    assert questions.relay(tmp_path).find(entry.id) == entry


async def test_browser_cannot_choose_the_answering_principal(tmp_path: Path) -> None:
    entry = parked(tmp_path)
    async with client(tmp_path) as http:
        key = await only_key(http)
        response = await http.post(
            f"/api/reviews/{key}/answer",
            headers=ANSWER_HEADERS,
            json={
                "approved": True,
                "note": "",
                "fingerprint": entry.fingerprint,
                "principal": "absent-worker",
            },
        )

    assert response.status_code == 422
    assert questions.relay(tmp_path).find(entry.id) == entry


async def test_malformed_answer_is_a_validation_error(tmp_path: Path) -> None:
    entry = parked(tmp_path)
    async with client(tmp_path) as http:
        key = await only_key(http)
        response = await http.post(
            f"/api/reviews/{key}/answer",
            headers={**ANSWER_HEADERS, "Content-Type": "application/json"},
            content="{",
        )

    assert response.status_code == 422
    assert questions.relay(tmp_path).find(entry.id) == entry


async def test_same_question_id_in_two_roots_stays_distinct(tmp_path: Path) -> None:
    roots = (tmp_path / "first", tmp_path / "second")
    entries = [parked(root) for root in roots]
    outside = tmp_path / "outside"
    parked(outside, "unregistered-question")
    async with client(*roots) as http:
        response = await http.get(
            "/api/reviews", params={"root": str(outside)}, headers=AUTHORIZATION
        )
        snapshot = ReviewInbox.model_validate(response.json())
        details = [
            ReviewDetail.model_validate(
                (
                    await http.get(f"/api/reviews/{item.key}", headers=AUTHORIZATION)
                ).json()
            )
            for item in snapshot.reviews
        ]
        unknown = await http.get(
            "/api/reviews/unregistered-question", headers=AUTHORIZATION
        )

    assert len(snapshot.roots) == len(snapshot.reviews) == 2
    assert len({item.key for item in snapshot.reviews}) == 2
    assert len({item.root_id for item in snapshot.reviews}) == 2
    assert all("/" not in item.key for item in snapshot.reviews)
    assert {detail.question.operation.cwd for detail in details} == set(roots)
    assert {detail.question.fingerprint for detail in details} == {
        entry.fingerprint for entry in entries
    }
    assert unknown.status_code == 404


async def test_detail_keeps_the_full_command_and_its_metadata(tmp_path: Path) -> None:
    entry = parked(tmp_path)
    async with client(tmp_path) as http:
        key = await only_key(http)
        response = await http.get(f"/api/reviews/{key}", headers=AUTHORIZATION)

    detail = ReviewDetail.model_validate(response.json())
    assert detail.question == entry
    assert detail.files == []
    assert detail.summary.answerable
    assert detail.stale_reason == ""
    assert detail.command == entry.operation.payload["command"]
    assert detail.preview_unavailable


async def test_file_detail_uses_the_captured_preimage_and_preserves_both_documents(
    tmp_path: Path,
) -> None:
    entry = parked(tmp_path)
    path = tmp_path / "document.txt"
    before = "".join(f"original line {index}\n" for index in range(500))
    after = "".join(f"replacement line {index}\n" for index in range(600))
    operation = entry.operation.model_copy(
        update={"tool": "Write", "payload": {"file_path": str(path), "content": after}}
    )
    entry = entry.model_copy(
        update={
            "operation": operation,
            "fingerprint": operation.fingerprint(),
            "preconditions": {path: before},
        }
    )
    questions.relay(tmp_path).record(entry)
    path.write_text("somebody else's intervening edit\n", encoding="utf-8")
    async with client(tmp_path) as http:
        key = await only_key(http)
        response = await http.get(f"/api/reviews/{key}", headers=AUTHORIZATION)
        refused = await http.post(
            f"/api/reviews/{key}/answer",
            headers=ANSWER_HEADERS,
            json={"approved": True, "note": "", "fingerprint": entry.fingerprint},
        )
        rejected = await http.post(
            f"/api/reviews/{key}/answer",
            headers=ANSWER_HEADERS,
            json={
                "approved": False,
                "note": "Refresh this proposal.",
                "fingerprint": entry.fingerprint,
            },
        )

    detail = ReviewDetail.model_validate(response.json())
    assert len(detail.files) == 1
    assert detail.files[0].before == before
    assert detail.files[0].after == after
    assert detail.command is None
    assert detail.preview_unavailable == ""
    assert "-original line 499" in detail.files[0].unified
    assert "+replacement line 599" in detail.files[0].unified
    assert detail.files[0].additions == 600
    assert detail.files[0].deletions == 500
    assert detail.files[0].hunks[0].lines[-1] == ReviewLine(
        kind="add", text="replacement line 599\n", new_line=600
    )
    assert detail.stale_reason
    assert refused.status_code == 409
    assert rejected.status_code == 200


@pytest.mark.parametrize(
    ("before", "after", "operation", "header", "additions", "deletions"),
    [
        (None, "first\nsecond\n", "create", "@@ -0,0 +1,2 @@", 2, 0),
        ("first\nsecond\n", None, "delete", "@@ -1,2 +0,0 @@", 0, 2),
        ("first\n", "", "modify", "@@ -1 +0,0 @@", 0, 1),
        ("", "first\n", "modify", "@@ -0,0 +1 @@", 1, 0),
    ],
)
def test_structured_diff_preserves_creation_deletion_and_empty_file_changes(
    before: str | None,
    after: str | None,
    operation: str,
    header: str,
    additions: int,
    deletions: int,
) -> None:
    change = ReviewedFile(path=Path("document.txt"), before=before, after=after)
    shown = ReviewFile.of(change)

    assert shown.before == before
    assert shown.after == after
    assert shown.operation == operation
    assert shown.unified == change.unified()
    assert shown.additions == additions
    assert shown.deletions == deletions
    assert len(shown.hunks) == 1
    assert shown.hunks[0].header == header
    assert [
        line.new_line for line in shown.hunks[0].lines if line.kind == "add"
    ] == list(range(1, additions + 1))
    assert [
        line.old_line for line in shown.hunks[0].lines if line.kind == "remove"
    ] == list(range(1, deletions + 1))


@pytest.mark.parametrize(
    ("before", "after", "operation", "unchanged"),
    [
        (None, "", "create", False),
        ("", None, "delete", False),
        ("", "", "modify", True),
    ],
)
def test_empty_file_changes_keep_the_operation_without_invented_diff_lines(
    before: str | None, after: str | None, operation: str, unchanged: bool
) -> None:
    shown = ReviewFile.of(
        ReviewedFile(path=Path("empty.txt"), before=before, after=after)
    )

    assert shown.operation == operation
    assert shown.unchanged == unchanged
    assert shown.hunks == []
    assert shown.additions == shown.deletions == 0


def test_separated_hunks_keep_all_changes_and_document_line_numbers() -> None:
    before = "".join(f"line {number}\n" for number in range(1, 31))
    after = "".join(
        [
            "line 1\n",
            "replacement two\n",
            "extra\n",
            *(f"line {number}\n" for number in range(3, 25)),
            *(f"line {number}\n" for number in range(26, 31)),
        ]
    )
    shown = ReviewFile.of(
        ReviewedFile(path=Path("long.txt"), before=before, after=after)
    )

    assert [hunk.header for hunk in shown.hunks] == [
        "@@ -1,5 +1,6 @@",
        "@@ -22,7 +23,6 @@",
    ]
    assert shown.additions == shown.deletions == 2
    assert [
        line for hunk in shown.hunks for line in hunk.lines if line.kind != "context"
    ] == [
        ReviewLine(kind="remove", text="line 2\n", old_line=2),
        ReviewLine(kind="add", text="replacement two\n", new_line=2),
        ReviewLine(kind="add", text="extra\n", new_line=3),
        ReviewLine(kind="remove", text="line 25\n", old_line=25),
    ]
    assert ReviewLine(kind="context", text="line 3\n", old_line=3, new_line=4) in (
        shown.hunks[0].lines
    )
    assert ReviewLine(kind="context", text="line 26\n", old_line=26, new_line=26) in (
        shown.hunks[1].lines
    )


def test_suppressions_are_located_with_rules_reasons_and_result_line_highlights() -> (
    None
):
    before = "# lup: ignore[empty-collection] — existing exception\nvalue = 1\n"
    after = before + (
        "# lup: ignore[dict-get, string-replace] — optional key\n"
        "# documented by the external response contract\n"
        "lookup = items.get('key')\n"
    )
    shown = ReviewFile.of(
        ReviewedFile(path=Path("source.py"), before=before, after=after)
    )

    assert shown.suppressions == [
        ReviewSuppression(
            line=1,
            rule_ids=["empty-collection"],
            reason="— existing exception",
            introduced=False,
            review_effect="allow",
            review_reason="This directive introduces no rule exception.",
            review_rule_ids=[],
        ),
        ReviewSuppression(
            line=3,
            rule_ids=["dict-get", "string-replace"],
            reason="— optional key documented by the external response contract",
            introduced=True,
            review_reason="No per-file decision was captured; this file remains visible as unclassified context.",
            review_rule_ids=["dict-get", "string-replace"],
        ),
    ]
    assert [
        line.new_line for hunk in shown.hunks for line in hunk.lines if line.suppression
    ] == [1, 3]


def test_suppression_summary_includes_unchanged_markers_outside_diff_context() -> None:
    before = "# lup: ignore[empty-collection] — existing exception\n" + "".join(
        f"value_{number} = {number}\n" for number in range(30)
    )
    shown = ReviewFile.of(
        ReviewedFile(path=Path("source.py"), before=before, after=before + "last = 1\n")
    )

    assert len(shown.suppressions) == 1
    assert shown.suppressions[0].line == 1
    assert not shown.suppressions[0].introduced
    assert not any(line.suppression for hunk in shown.hunks for line in hunk.lines)


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("source.py", "message = '# lup: ignore[dict-get] — quoted example'\n"),
        ("notes.md", "Use `# lup: ignore[dict-get]` for a deliberate exception.\n"),
        ("notes.md", "```python\n# lup: ignore[dict-get] — fenced example\n```\n"),
        ("record.json", '{"message": "# lup: ignore[dict-get]"}\n'),
    ],
)
def test_quoted_suppression_examples_are_not_presented_as_exceptions(
    path: str, source: str
) -> None:
    shown = ReviewFile.of(ReviewedFile(path=Path(path), after=source))

    assert shown.suppressions == []
    assert not any(line.suppression for hunk in shown.hunks for line in hunk.lines)


def test_bare_and_empty_suppressions_remain_distinct_and_visible() -> None:
    shown = ReviewFile.of(
        ReviewedFile(path=Path("source.py"), after="# lup: ignore\n# lup: ignore[]\n")
    )

    assert [marker.rule_ids for marker in shown.suppressions] == [None, []]
    assert all(marker.introduced for marker in shown.suppressions)


def test_line_ending_changes_and_unterminated_lines_remain_visible() -> None:
    shown = ReviewFile.of(
        ReviewedFile(
            path=Path("endings.txt"), before="same\r\nlast", after="same\nlast\n"
        )
    )

    assert shown.additions == shown.deletions == 2
    assert shown.hunks[0].lines == [
        ReviewLine(kind="remove", text="same\r\n", old_line=1),
        ReviewLine(kind="remove", text="last", old_line=2),
        ReviewLine(kind="meta", text="\\ No newline at end of file"),
        ReviewLine(kind="add", text="same\n", new_line=1),
        ReviewLine(kind="add", text="last\n", new_line=2),
    ]


def test_captured_null_bytes_and_unterminated_context_are_preserved() -> None:
    shown = ReviewFile.of(
        ReviewedFile(
            path=Path("captured.dat"),
            before="\x00before\r\nlast",
            after="\x00after\r\nlast",
        )
    )

    assert shown.additions == shown.deletions == 1
    assert shown.hunks[0].lines == [
        ReviewLine(kind="remove", text="\x00before\r\n", old_line=1),
        ReviewLine(kind="add", text="\x00after\r\n", new_line=1),
        ReviewLine(kind="context", text="last", old_line=2, new_line=2),
        ReviewLine(kind="meta", text="\\ No newline at end of file"),
    ]


async def test_an_absent_preimage_becoming_a_file_prevents_approval(
    tmp_path: Path,
) -> None:
    entry = parked(tmp_path)
    path = tmp_path / "created-later.txt"
    entry = entry.model_copy(update={"preconditions": {path: None}})
    questions.relay(tmp_path).record(entry)
    path.write_text("must survive", encoding="utf-8")
    async with client(tmp_path) as http:
        key = await only_key(http)
        response = await http.post(
            f"/api/reviews/{key}/answer",
            headers=ANSWER_HEADERS,
            json={"approved": True, "note": "", "fingerprint": entry.fingerprint},
        )

    assert response.status_code == 409
    assert questions.relay(tmp_path).find(entry.id) == entry


async def test_a_changed_fingerprint_never_records_a_decision(tmp_path: Path) -> None:
    entry = parked(tmp_path)
    async with client(tmp_path) as http:
        key = await only_key(http)
        response = await http.post(
            f"/api/reviews/{key}/answer",
            headers=ANSWER_HEADERS,
            json={"approved": True, "note": "", "fingerprint": "another-operation"},
        )

    assert response.status_code == 409
    assert questions.relay(tmp_path).find(entry.id) == entry


async def test_expired_questions_are_visible_but_cannot_be_answered(
    tmp_path: Path,
) -> None:
    entry = parked(tmp_path)
    entry = entry.model_copy(
        update={"expires": datetime.now(UTC) - timedelta(seconds=1)}
    )
    questions.relay(tmp_path).record(entry)
    async with client(tmp_path) as http:
        key = await only_key(http)
        shown = await http.get(f"/api/reviews/{key}", headers=AUTHORIZATION)
        answered = await http.post(
            f"/api/reviews/{key}/answer",
            headers=ANSWER_HEADERS,
            json={"approved": True, "note": "", "fingerprint": entry.fingerprint},
        )

    assert not ReviewDetail.model_validate(shown.json()).summary.answerable
    assert answered.status_code == 409
    stored = questions.relay(tmp_path).find(entry.id)
    assert stored is not None
    assert stored.answer is None


async def test_decision_is_durable_without_executing_the_command(
    tmp_path: Path,
) -> None:
    entry = parked(tmp_path)
    async with client(tmp_path) as http:
        key = await only_key(http)
        response = await http.post(
            f"/api/reviews/{key}/answer",
            headers=ANSWER_HEADERS,
            json={
                "approved": True,
                "note": "Retry the exact call.",
                "fingerprint": entry.fingerprint,
            },
        )
        duplicate = await http.post(
            f"/api/reviews/{key}/answer",
            headers=ANSWER_HEADERS,
            json={"approved": False, "note": "", "fingerprint": entry.fingerprint},
        )
        shown = await http.get("/api/reviews", headers=AUTHORIZATION)

    assert response.status_code == 200
    decision = ReviewDecision.model_validate(response.json())
    assert decision.review.question.state == "approved"
    assert decision.review.question.answer is not None
    assert decision.review.question.answer.principal == "operator"
    assert decision.review.question.answer.note == "Retry the exact call."
    assert questions.relay(tmp_path).find(entry.id) == decision.review.question
    assert not decision.notification.woken
    assert not (tmp_path / "must-not-execute").exists()
    assert duplicate.status_code == 409
    assert not ReviewInbox.model_validate(shown.json()).reviews[0].answerable


async def test_notification_failure_cannot_undo_the_recorded_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = parked(tmp_path)

    def fail(roots: tuple[Path, ...], question: PersistentQuestion) -> None:
        assert roots == (tmp_path,)
        assert question.state == "rejected"
        raise OSError("notification transport unavailable")

    monkeypatch.setattr(questions, "notify_requester", fail)
    async with client(tmp_path) as http:
        key = await only_key(http)
        response = await http.post(
            f"/api/reviews/{key}/answer",
            headers=ANSWER_HEADERS,
            json={
                "approved": False,
                "note": "Try another approach.",
                "fingerprint": entry.fingerprint,
            },
        )

    assert response.status_code == 200
    decision = ReviewDecision.model_validate(response.json())
    assert decision.review.question.state == "rejected"
    assert questions.relay(tmp_path).find(entry.id) == decision.review.question
    assert not decision.notification.queued
    assert not decision.notification.woken
    assert "no confirmed outcome" in decision.notification.detail
    persisted = ReviewNotifications(root=tmp_path).read(decision.review.question)
    assert persisted is not None
    assert "notification transport unavailable" in persisted.detail


async def test_event_stream_sends_a_snapshot_then_queue_changes(tmp_path: Path) -> None:
    parked(tmp_path)
    app = questions.review_app(BASE_URL, TOKEN, (tmp_path,))
    snapshots: list[ReviewInbox] = []
    disconnected = asyncio.Event()
    started = asyncio.Event()
    statuses: list[int] = []
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/events",
        "raw_path": b"/api/events",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1:8765"),
            (b"authorization", f"Bearer {TOKEN}".encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8765),
    }

    async def receive() -> Message:
        if not started.is_set():
            started.set()
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        match message["type"]:
            case "http.response.start":
                statuses.append(message["status"])
            case "http.response.body" if message.get("body"):
                for line in message["body"].splitlines():
                    if not line:
                        continue
                    snapshots.append(ReviewInbox.model_validate_json(line))
                if len(snapshots) == 1:
                    parked(tmp_path, "q-2")
                else:
                    disconnected.set()

    await asyncio.wait_for(app(scope, receive, send), timeout=5)

    assert statuses == [200]
    assert {item.id for item in snapshots[0].reviews} == {"q-1"}
    assert {item.id for item in snapshots[1].reviews} == {"q-1", "q-2"}


def test_root_discovery_keeps_only_named_repositories_and_their_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = tmp_path / "current"
    sibling = tmp_path / "sibling"
    additional = tmp_path / "additional"
    additional_sibling = tmp_path / "additional-sibling"
    bare = tmp_path / "bare"
    outside = tmp_path / "outside"
    for root in [current, sibling, additional, additional_sibling, outside]:
        (root / ".git").mkdir(parents=True)
    bare.mkdir()
    discoveries = {
        current: [bare, current, sibling],
        additional: [additional, additional_sibling],
    }
    requested: list[Path] = []

    def discover(root: Path) -> list[Path]:
        requested.append(root)
        return discoveries[root]

    monkeypatch.setattr(questions, "sibling_worktrees", discover)

    roots = questions.review_roots(current, [additional, current])

    assert roots == (current, sibling, additional, additional_sibling)
    assert set(requested) == {current, additional}
    assert outside not in roots


@pytest.mark.parametrize("open_page", [False, True])
@pytest.mark.parametrize(
    "selected_names", [(), ("additional",), ("additional", "other", "additional")]
)
@pytest.mark.parametrize(
    ("requested_port", "expected_url"), [(8765, BASE_URL), (80, "http://127.0.0.1")]
)
async def test_cli_serves_selected_roots_and_keeps_the_token_out_of_public_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    open_page: bool,
    selected_names: tuple[str, ...],
    requested_port: int,
    expected_url: str,
) -> None:
    current = tmp_path / "current"
    roots = [
        tmp_path / name
        for name in (
            "current",
            "current-sibling",
            "additional",
            "additional-sibling",
            "other",
            "other-sibling",
        )
    ]
    for root in roots:
        (root / ".git").mkdir(parents=True)
    entries = {root.name: parked(root, f"{root.name}-question") for root in roots}
    watched = selected_names or ("current",)
    entry = entries[watched[0]]
    monkeypatch.setattr(
        questions,
        "sibling_worktrees",
        lambda root: [root, root.with_name(f"{root.name}-sibling")],
    )
    served: list[FastAPI] = []
    opened: list[str] = []
    sizes: list[int] = []

    def token(size: int) -> str:
        sizes.append(size)
        return TOKEN

    def serve(app: FastAPI, host: str, port: int, access_log: bool) -> None:
        assert host == "127.0.0.1"
        assert port == requested_port
        assert not access_log
        served.append(app)

    monkeypatch.setattr(secrets, "token_urlsafe", token)
    monkeypatch.setattr(webbrowser, "open", opened.append)
    monkeypatch.setattr(uvicorn, "run", serve)
    arguments = ["serve", "--port", str(requested_port)]
    arguments.extend(
        argument
        for name in selected_names
        for argument in ["--root", str(tmp_path / name)]
    )
    if not open_page:
        arguments.append("--no-open")

    result = CliRunner().invoke(questions.create_questions_app(current), arguments)

    assert result.exit_code == 0, result.output
    assert sizes == [32]
    browser_url = f"{expected_url}/#token={TOKEN}"
    assert browser_url in result.stdout
    assert opened == ([browser_url] if open_page else [])
    assert len(served) == 1
    async with AsyncClient(
        transport=ASGITransport(app=served[0]), base_url=expected_url
    ) as http:
        page = await http.get("/")
        unauthenticated = await http.get("/api/reviews")
        snapshot = await http.get("/api/reviews", headers=AUTHORIZATION)
        inbox = ReviewInbox.model_validate(snapshot.json())
        selected = next(item for item in inbox.reviews if item.id == entry.id)
        decision = await http.post(
            f"/api/reviews/{selected.key}/answer",
            headers={**AUTHORIZATION, "Origin": expected_url},
            json={"approved": True, "note": "", "fingerprint": entry.fingerprint},
        )

    assert page.status_code == 200
    assert TOKEN not in page.text
    assert unauthenticated.status_code == 401
    assert decision.status_code == 200
    expected_names = {name for root in watched for name in (root, f"{root}-sibling")}
    assert {item.id for item in inbox.reviews} == {
        f"{name}-question" for name in expected_names
    }
    assert {Path(item.path).name for item in inbox.roots} == expected_names
    assert len(inbox.roots) == len(expected_names)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "attacker.example"])
def test_cli_refuses_non_loopback_before_serving_or_discovering_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    discovered: list[Path] = []
    served: list[FastAPI] = []

    def discover(root: Path) -> list[Path]:
        discovered.append(root)
        return [root]

    def serve(app: FastAPI, host: str, port: int, access_log: bool) -> None:
        del host, port, access_log
        served.append(app)

    monkeypatch.setattr(questions, "sibling_worktrees", discover)
    monkeypatch.setattr(uvicorn, "run", serve)

    result = CliRunner().invoke(
        questions.create_questions_app(tmp_path), ["serve", "--host", host, "--no-open"]
    )

    assert result.exit_code != 0
    assert "loopback" in str(result.exception)
    assert discovered == []
    assert served == []


def git_repository(root: Path) -> None:
    """Real Git metadata makes worktree discovery and common-store identity observable."""
    sh.git("init", "--initial-branch=main", str(root))
    sh.git(
        "-C",
        str(root),
        "-c",
        "user.name=Inbox test",
        "-c",
        "user.email=inbox-test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "Test baseline",
    )


async def test_open_inbox_discovers_a_worktree_created_after_startup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    sibling = tmp_path / "arrived-later"
    git_repository(root)
    parked(root, "original-question")
    async with client(root, discover=True) as http:
        before = await http.get("/api/reviews", headers=AUTHORIZATION)
        sh.git("-C", str(root), "worktree", "add", "-b", "later", str(sibling))
        entry = parked(sibling, "arrived-later-question")
        after = await http.get("/api/reviews", headers=AUTHORIZATION)
        snapshot = ReviewInbox.model_validate(after.json())
        found = next(row for row in snapshot.reviews if row.id == entry.id)
        detail = await http.get(f"/api/reviews/{found.key}", headers=AUTHORIZATION)

    assert {row.id for row in ReviewInbox.model_validate(before.json()).reviews} == {
        "original-question"
    }
    assert {row.id for row in snapshot.reviews} == {
        "original-question",
        "arrived-later-question",
    }
    assert {Path(row.path) for row in snapshot.roots} == {root, sibling}
    assert detail.status_code == 200
    assert ReviewDetail.model_validate(detail.json()).question == entry


async def test_inbox_tracks_siblings_after_its_launch_worktree_is_removed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    launch = tmp_path / "launch"
    remaining = tmp_path / "remaining"
    arrived = tmp_path / "arrived-later"
    git_repository(root)
    for worktree in (launch, remaining):
        sh.git("-C", str(root), "worktree", "add", "-b", worktree.name, str(worktree))
    parked(remaining, "remaining-question")

    async with client(launch, discover=True) as http:
        before = await http.get("/api/reviews", headers=AUTHORIZATION)
        sh.git("-C", str(root), "worktree", "remove", str(launch))
        sh.git("-C", str(root), "worktree", "add", "-b", "arrived", str(arrived))
        entry = parked(arrived, "arrived-question")
        after = await http.get("/api/reviews", headers=AUTHORIZATION)
        snapshot = ReviewInbox.model_validate(after.json())
        found = next(row for row in snapshot.reviews if row.id == entry.id)
        detail = await http.get(f"/api/reviews/{found.key}", headers=AUTHORIZATION)

    assert before.status_code == after.status_code == 200
    assert {row.id for row in ReviewInbox.model_validate(before.json()).reviews} == {
        "remaining-question"
    }
    assert snapshot.errors == []
    assert {Path(row.path) for row in snapshot.roots} == {root, remaining, arrived}
    assert {row.id for row in snapshot.reviews} == {
        "remaining-question",
        "arrived-question",
    }
    assert detail.status_code == 200
    assert ReviewDetail.model_validate(detail.json()).question == entry


@pytest.mark.parametrize(
    ("ambiguous", "wake_failure"), [(False, False), (True, False), (False, True)]
)
def test_requester_notification_crosses_repositories_and_deduplicates_worktree_rosters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ambiguous: bool, wake_failure: bool
) -> None:
    upstream = tmp_path / "upstream"
    consumer = tmp_path / "consumer"
    sibling = tmp_path / "consumer-sibling"
    git_repository(upstream)
    git_repository(consumer)
    sh.git("-C", str(consumer), "worktree", "add", "-b", "sibling", str(sibling))
    entry = parked(upstream)
    entry = questions.relay(upstream).answer(
        entry.id, "operator", True, "Please retry the reviewed operation."
    )
    member = RosterMember(
        actor=ActorRef(kind="session", id="launcher-identity"),
        task="translation setup",
        running=True,
        worktree=str(sibling),
        wake=WakePath(
            runtime="codex", handle="native-thread", session=entry.operation.session
        ),
    )
    consumer_store = RepositoryPeers(consumer).root
    upstream_store = RepositoryPeers(upstream).root
    visited: list[Path] = []
    sent: list[tuple[Path, str, str, Door]] = []
    nudges: list[tuple[WakePath, str, Path | None]] = []

    def present(
        peers: RepositoryPeers, now: datetime | None = None
    ) -> list[RosterMember]:
        del now
        visited.append(peers.root)
        return [member] if peers.root == consumer_store or ambiguous else []

    def send(
        peers: RepositoryPeers,
        to: str,
        text: str,
        redirect: bool = False,
        door: Door = Door.AGENT,
        in_reply_to: str = "",
    ) -> ActorRef:
        assert not redirect
        assert not in_reply_to
        sent.append((peers.root, to, text, door))
        return member.actor

    def wake(path: WakePath, message: str, cwd: Path | None = None) -> Woken:
        nudges.append((path, message, cwd))
        if wake_failure:
            raise OSError("requester wake transport failed")
        return Woken(reached=True)

    monkeypatch.setattr(RepositoryPeers, "present", present)
    monkeypatch.setattr(RepositoryPeers, "send", send)
    monkeypatch.setattr(questions, "wake", wake)

    notification = questions.notify_requester((upstream, consumer, sibling), entry)

    assert visited.count(consumer_store) == 1
    assert visited.count(upstream_store) == 1
    if ambiguous:
        assert not notification.queued
        assert not notification.woken
        assert sent == []
        assert nudges == []
        return
    assert notification.queued
    assert notification.woken is not wake_failure
    if wake_failure:
        assert "requester wake transport failed" in notification.detail
    assert len(sent) == len(nudges) == 1
    recorded_store, address, message, door = sent[0]
    assert recorded_store == consumer_store
    assert address == member.address
    assert door == Door.PAGE
    assert entry.id in message
    assert str(upstream) in message
    assert "Please retry the reviewed operation." in message
    assert nudges[0] == (member.wake, message, sibling)


@pytest.mark.parametrize("missing_root", [True, False])
async def test_unavailable_first_queue_does_not_hide_a_healthy_repository(
    tmp_path: Path, missing_root: bool
) -> None:
    unavailable = tmp_path / "unavailable"
    healthy = tmp_path / "healthy"
    git_repository(healthy)
    entry = parked(healthy, "healthy-question")
    if not missing_root:
        git_repository(unavailable)
        questions.relay(unavailable).path.mkdir(parents=True)
    async with client(unavailable, healthy, discover=True) as http:
        response = await http.get("/api/reviews", headers=AUTHORIZATION)
        assert response.status_code == 200
        snapshot = ReviewInbox.model_validate(response.json())
        key = next(item.key for item in snapshot.reviews if item.id == entry.id)
        found = await http.get(f"/api/reviews/{key}", headers=AUTHORIZATION)
        missing = await http.get("/api/reviews/unknown", headers=AUTHORIZATION)
        answered = await http.post(
            f"/api/reviews/{key}/answer",
            headers=ANSWER_HEADERS,
            json={"approved": True, "note": "", "fingerprint": entry.fingerprint},
        )

    assert len(snapshot.errors) == 1
    assert Path(snapshot.errors[0].root) == unavailable
    assert snapshot.errors[0].message
    assert found.status_code == 200
    assert ReviewDetail.model_validate(found.json()).question == entry
    assert missing.status_code == 503
    assert answered.status_code == 200
    assert (
        ReviewDecision.model_validate(answered.json()).review.question.state
        == "approved"
    )


async def test_saved_answer_is_returned_when_notification_makes_the_queue_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = parked(tmp_path)

    def unavailable(root: Path) -> QuestionRelay:
        assert root == tmp_path
        raise OSError("requester removed its checkout after receiving the decision")

    def notify(
        roots: tuple[Path, ...], settled: PersistentQuestion
    ) -> ReviewNotification:
        assert roots == (tmp_path,)
        assert settled.state == "approved"
        assert settled.answer is not None
        monkeypatch.setattr(questions, "relay", unavailable)
        return ReviewNotification(queued=True, woken=True, detail="Requester notified.")

    monkeypatch.setattr(questions, "notify_requester", notify)
    async with client(tmp_path) as http:
        key = await only_key(http)
        response = await http.post(
            f"/api/reviews/{key}/answer",
            headers=ANSWER_HEADERS,
            json={
                "approved": True,
                "note": "Continue.",
                "fingerprint": entry.fingerprint,
            },
        )

    assert response.status_code == 200
    decision = ReviewDecision.model_validate(response.json())
    assert decision.review.question.state == "approved"
    assert not decision.notification.queued
    assert not decision.notification.woken
    persisted = ReviewNotifications(root=tmp_path).read(decision.review.question)
    assert persisted is not None and persisted.queued and persisted.woken
    assert (
        QuestionRelay(tmp_path / ".lup/questions.jsonl").find(entry.id)
        == decision.review.question
    )


@pytest.mark.parametrize(
    "module", ["lup.devtools.dev.questions", "lup.devtools.dev.app"]
)
def test_terminal_review_commands_import_without_optional_web_dependencies(
    tmp_path: Path, module: str
) -> None:
    script = tmp_path / "without_web.py"
    script.write_text(
        dedent(
            """\
            import importlib
            import importlib.abc
            import sys

            class NoWeb(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname in {"fastapi", "uvicorn"}:
                        raise ModuleNotFoundError(
                            f"Optional web dependency unavailable: {fullname}",
                            name=fullname,
                        )
                    return None

            sys.meta_path.insert(0, NoWeb())
            importlib.import_module(sys.argv[1])
            assert "fastapi" not in sys.modules
            assert "uvicorn" not in sys.modules
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(script), module],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
