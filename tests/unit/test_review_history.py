"""Settled reviews retire to an archive, and the page reads History a page at a time.

The relay holds what a week of sessions asked; what settled before that moves
to the archive with its summary and thread, its documents swept. The page is
handed every waiting review and the most recently settled as rows, reads a
review's documents only when it is opened, and pages through the rest of
History; an answer reads nothing but the review it answers.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lup.devtools.dashboard.reviews import (
    ReviewAnswer,
    ReviewHistory,
    ReviewSnapshot,
    ReviewStore,
)
from lup.devtools.review.app import (
    ReviewArchive,
    ReviewDetail,
    ReviewSummary,
    create_review_app,
    relay,
    retire_settled,
    review_retention_days,
)
from lup.policy.assets.host import relay_blobs, resolved_entry, review_records
from lup.policy.relay import PersistentQuestion, QuestionRelay, RelayReading
from lup.types import JsonObject
from tests.unit.native import bound
from tests.unit.test_dashboard_reviews import AUTHORIZATION, client, parked

RUNNER = CliRunner()


def written(root: Path, question_id: str, text: str) -> PersistentQuestion:
    """A native review of one file's rewrite, its preimage *text* standing on disk, parked in *root*'s relay."""
    target = root / f"{question_id}.txt"
    question = parked(root, question_id)
    target.write_text(text)
    rewritten = question.operation.model_copy(
        update={
            "tool": "Write",
            "payload": {"file_path": str(target), "content": text + "rewritten\n"},
        }
    )
    return relay(root).record(
        bound(
            question.model_copy(
                update={
                    "operation": rewritten,
                    "execution_payload": None,
                    "preconditions": {target: text},
                }
            )
        )
    )


def settled_ago(root: Path, question_id: str, days: int) -> None:
    """Carry one review to rest *days* ago, as a waiter would have."""
    relay(root).transitioned(
        {question_id: {"state": "completed", "outcome": "carried out"}},
        datetime.now(UTC) - timedelta(days=days),
    )


def test_a_review_settled_past_the_window_moves_to_the_archive_without_its_documents(
    tmp_path: Path,
) -> None:
    old = written(tmp_path, "old", "the old document\n")
    written(tmp_path, "waiting", "a waiting document\n")
    settled_ago(tmp_path, old.id, 10)
    blobs = relay_blobs(relay(tmp_path).path)
    kept_before = {path.name for path in blobs.iterdir()}

    assert retire_settled(tmp_path) == [old.id]

    store = relay(tmp_path)
    assert [question.id for question in store.questions()] == ["waiting"]
    assert all("old" not in str(record) for record in review_records(store.path)[1:])
    (archived,) = ReviewArchive(store).reviews().values()
    assert archived.question.id == old.id
    assert archived.question.state == "completed"
    assert archived.title == "Replace old.txt"
    kept_after = {path.name for path in blobs.iterdir()}
    assert len(kept_after) == len(kept_before) - 1
    with pytest.raises(ValueError, match="not kept"):
        store.resolve(archived.question)
    assert retire_settled(tmp_path) == []


def test_the_terminal_reaches_what_the_archive_kept(tmp_path: Path) -> None:
    old = written(tmp_path, "old", "the old document\n")
    settled_ago(tmp_path, old.id, 10)
    retire_settled(tmp_path)

    listed = RUNNER.invoke(create_review_app(tmp_path), ["list", "--all"])
    shown = RUNNER.invoke(create_review_app(tmp_path), ["show", old.id])

    assert listed.exit_code == 0, listed.output
    assert old.id in listed.output
    assert shown.exit_code == 0, shown.output
    assert "its documents gone: Replace old.txt" in shown.output


@pytest.mark.parametrize(("days", "retired"), [(30, False), (0, True)])
def test_the_retention_window_is_the_project_s_to_set(
    tmp_path: Path, days: int, retired: bool
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.lup]\nreview-retention-days = {days}\n"
    )
    old = written(tmp_path, "old", "the old document\n")
    settled_ago(tmp_path, old.id, 10)

    assert review_retention_days(tmp_path) == days
    assert retire_settled(tmp_path) == ([old.id] if retired else [])


def test_the_window_defaults_to_a_week(tmp_path: Path) -> None:
    old = written(tmp_path, "old", "the old document\n")
    settled_ago(tmp_path, old.id, 6)

    assert review_retention_days(tmp_path) == 7
    assert retire_settled(tmp_path) == []


def test_the_snapshot_carries_rows_and_counts_all_of_history(tmp_path: Path) -> None:
    store = ReviewStore(roots=(tmp_path,), recent=5)
    waiting = written(tmp_path, "waiting", "waiting\n")
    for index in range(store.recent + 5):
        settled = written(tmp_path, f"done-{index}", f"document {index}\n")
        settled_ago(tmp_path, settled.id, 0)
    archived = written(tmp_path, "archived", "archived\n")
    settled_ago(tmp_path, archived.id, 30)
    retire_settled(tmp_path)

    snapshot = store.snapshot()

    assert snapshot.history == store.recent + 6
    assert len(snapshot.reviews) == store.recent + 1
    assert waiting.id in {row.id for row in snapshot.reviews}
    assert "document" not in snapshot.model_dump_json()


def test_a_fold_reads_no_document_and_a_snapshot_only_its_rows_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ReviewStore(roots=(tmp_path,), recent=5)
    written(tmp_path, "waiting", "waiting\n")
    for index in range(store.recent * 4):
        settled = written(tmp_path, f"done-{index}", f"document {index}\n")
        settled_ago(tmp_path, settled.id, 0)
    read: list[str] = []

    def counted(entry: JsonObject, blobs: Path) -> JsonObject:
        read.append(str(entry["id"]))
        return resolved_entry(entry, blobs)

    monkeypatch.setattr("lup.policy.relay.resolved_entry", counted)
    folded = relay(tmp_path).questions()
    unread = list(read)
    snapshot = store.snapshot()

    assert len(folded) == store.recent * 4 + 1
    assert unread == []
    assert read
    assert set(read) <= {row.id for row in snapshot.reviews}


async def test_history_is_read_a_page_at_a_time_archived_reviews_included(
    tmp_path: Path,
) -> None:
    for index in range(3):
        settled = written(tmp_path, f"done-{index}", f"document {index}\n")
        settled_ago(tmp_path, settled.id, 3 - index)
    archived = written(tmp_path, "archived", "archived\n")
    settled_ago(tmp_path, archived.id, 30)
    retire_settled(tmp_path)

    async with client(tmp_path) as http:
        first = await http.get(
            "/api/reviews/history",
            params={"offset": 0, "limit": 2},
            headers=AUTHORIZATION,
        )
        rest = await http.get(
            "/api/reviews/history",
            params={"offset": 2, "limit": 2},
            headers=AUTHORIZATION,
        )
        linked = await http.get(
            "/api/reviews/history", params={"review": "archived"}, headers=AUTHORIZATION
        )
        key = ReviewSummary.key_for(tmp_path, "archived")
        opened = await http.get(f"/api/reviews/{key}", headers=AUTHORIZATION)

    pages = [ReviewHistory.model_validate(page.json()) for page in (first, rest)]
    assert [[row.id for row in page.reviews] for page in pages] == [
        ["done-2", "done-1"],
        ["done-0", "archived"],
    ]
    assert all(page.total == 4 for page in pages)
    assert pages[1].reviews[1].archived
    assert [row.id for row in ReviewHistory.model_validate(linked.json()).reviews] == [
        "archived"
    ]
    detail = ReviewDetail.model_validate(opened.json())
    assert detail.files == []
    assert "Archived" in detail.preview_unavailable
    assert detail.summary.title == "Replace archived.txt"


async def test_an_answer_reads_nothing_but_the_review_it_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = written(tmp_path, "waiting", "waiting\n")
    for index in range(20):
        written(tmp_path, f"other-{index}", f"other {index}\n")
    store = ReviewStore(roots=(tmp_path,))
    ReviewSnapshot.model_validate(store.snapshot().model_dump())

    def whole(relay: QuestionRelay) -> RelayReading:
        raise AssertionError("an answer folded the whole queue")

    monkeypatch.setattr(QuestionRelay, "read", whole)
    decided = store.answer(
        ReviewSummary.key_for(tmp_path, entry.id),
        ReviewAnswer(approved=True, fingerprint=entry.fingerprint),
    )

    assert decided.review.question.state == "approved"
    assert decided.review.files[0].after == "waiting\nrewritten\n"
