"""The relay keeps each question once: documents by digest, transitions as deltas, the settled retired.

What is pinned here is the shape the log is kept in and what that shape
promises: a parked question's documents stored once and read back whole, a
state change appended as a small transition rather than a copy, an older log
rewritten into this shape the first time it is opened, and settled reviews
retired with every document no live question names.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lup.policy.assets.host import (
    migrate_relay,
    native_review_records,
    observe_hook_call,
    recorded_fingerprint,
    relay_blobs,
    relay_current,
    relay_header,
    resolved_entry,
    review_hook_call,
    review_records,
)
from lup.devtools.dashboard.reviews import ReviewStore
from lup.policy.operations import Operation
from lup.policy.relay import (
    CapturedFileReview,
    PersistentQuestion,
    QuestionRelay,
    StoredDocument,
)


def written_question(root: Path, identifier: str = "q-1") -> PersistentQuestion:
    """A native review of one file's rewrite, bound to its fingerprint as a hook binds it."""
    target = root / "module.py"
    before = "print('before')\n" * 200
    after = "print('after')\n" * 200
    operation = Operation(
        id=identifier,
        session="session-1",
        requester="session-1",
        tool="Write",
        payload={"file_path": str(target), "content": after},
        cwd=root,
        worktree=root,
    )
    question = PersistentQuestion(
        id=identifier,
        operation=operation,
        fingerprint="",
        preconditions={target: before},
        file_reviews=[
            CapturedFileReview(
                path=target,
                effect="ask",
                reason="a rewrite",
                rule="edit:rewrite",
                rules=["edit:rewrite"],
                before_sha256=None,
                after_sha256=None,
                after=after,
            )
        ],
        resumption="native_retry",
        execution_payload=operation.payload,
        scheme=["file_reviews", "unpreviewed", "segments"],
        reason="a rewrite asks",
        chain_resolved=False,
        resolved={target: target},
    )
    return question.model_copy(update={"fingerprint": question.native_fingerprint()})


def new_file(root: Path, identifier: str, content: str) -> PersistentQuestion:
    """A native review of a new file's write, whose document only the call carries."""
    target = root / f"{identifier}.txt"
    operation = Operation(
        id=identifier,
        session="session-1",
        requester="session-1",
        tool="Write",
        payload={"file_path": str(target), "content": content},
        cwd=root,
        worktree=root,
    )
    question = PersistentQuestion(
        id=identifier,
        operation=operation,
        fingerprint="",
        preconditions={target: None},
        resumption="native_retry",
        execution_payload=operation.payload,
        scheme=[],
        reason="a new file asks",
        chain_resolved=False,
        resolved={target: target},
    )
    return question.model_copy(update={"fingerprint": question.native_fingerprint()})


@pytest.fixture
def relay(tmp_path: Path) -> QuestionRelay:
    return QuestionRelay(tmp_path / ".lup/questions.jsonl", tmp_path / "answers.jsonl")


def test_a_parked_question_keeps_its_documents_once_and_reads_them_back_whole(
    tmp_path: Path, relay: QuestionRelay
) -> None:
    question = relay.record(written_question(tmp_path))
    relay.record(written_question(tmp_path, "q-2"))

    header, parked, _ = review_records(relay.path)
    assert header == relay_header()
    stored = parked["parked"]
    assert stored["preconditions"] == {
        str(tmp_path / "module.py"): {
            "sha256": StoredDocument.model_validate(
                stored["preconditions"][str(tmp_path / "module.py")]
            ).sha256
        }
    }
    assert "print('before')" not in relay.path.read_text()
    assert len(list(relay_blobs(relay.path).iterdir())) == 2
    (recorded, _) = relay.questions()
    shown = relay.resolve(recorded)
    assert shown.preconditions == question.preconditions
    assert shown.file_reviews == question.file_reviews
    assert shown.bound()


def test_a_transition_is_appended_as_a_delta_never_a_copy(
    tmp_path: Path, relay: QuestionRelay
) -> None:
    relay.record(written_question(tmp_path))
    size = relay.path.stat().st_size

    relay.advance("q-1", "dispatched", "carried out")
    relay.advance("q-1", "completed", "applied")

    grown = relay.path.stat().st_size - size
    assert grown < 600
    (_, _, first, second) = review_records(relay.path)
    assert first["transition"]["state"] == "dispatched"
    assert second["transition"]["state"] == "completed"
    (settled,) = relay.questions()
    assert (settled.state, settled.outcome) == ("completed", "applied")
    assert settled.completed is not None
    assert settled.since() == settled.changed


def test_a_transition_claiming_an_answer_moves_nothing(
    tmp_path: Path, relay: QuestionRelay
) -> None:
    relay.record(written_question(tmp_path))
    with relay.path.open("a", encoding="utf-8") as appended:
        appended.write(
            json.dumps(
                {
                    "transition": {
                        "id": "q-1",
                        "at": "2026-01-01T00:00:00+00:00",
                        "state": "approved",
                    }
                }
            )
            + "\n"
        )
        appended.write(
            json.dumps(
                {
                    "transition": {
                        "id": "q-1",
                        "at": "2026-01-01T00:00:00+00:00",
                        "operation": {},
                    }
                }
            )
            + "\n"
        )

    (question,) = relay.questions()
    assert question.state == "pending"
    assert relay.resolve(question).bound()


def test_a_reader_left_open_reads_only_what_was_appended_since(
    tmp_path: Path, relay: QuestionRelay
) -> None:
    relay.record(written_question(tmp_path))
    assert [each.id for each in relay.questions()] == ["q-1"]
    read = relay.log.offset

    QuestionRelay(relay.path, relay.answers).record(written_question(tmp_path, "q-2"))

    assert [each.id for each in relay.questions()] == ["q-1", "q-2"]
    assert relay.log.offset > read


def test_an_answer_settles_the_folded_question_without_touching_the_log(
    tmp_path: Path, relay: QuestionRelay
) -> None:
    relay.record(written_question(tmp_path))
    before = relay.path.read_bytes()

    answered = relay.answer("q-1", "operator", True, "go ahead")

    assert answered.state == "approved"
    assert relay.path.read_bytes() == before
    found = relay.find("q-1")
    assert found is not None and found.state == "approved"
    assert found.since() == answered.answer.at if answered.answer else False


def test_a_missing_document_refuses_an_answer_rather_than_guessing(
    tmp_path: Path, relay: QuestionRelay
) -> None:
    relay.record(written_question(tmp_path))
    for kept in relay_blobs(relay.path).iterdir():
        kept.write_text("altered\n")

    with pytest.raises(ValueError, match="cannot be read back whole"):
        relay.answer("q-1", "operator", True)


@pytest.mark.parametrize("restarting", [False, True])
def test_a_review_the_page_cannot_read_back_names_the_code_that_can(
    tmp_path: Path, relay: QuestionRelay, restarting: bool
) -> None:
    """Newer code spells a record this code cannot read; the queue's own checkout can."""
    relay.record(written_question(tmp_path))
    for kept in relay_blobs(relay.path).iterdir():
        kept.write_text("altered\n")
    (question,) = relay.pending()

    summary = ReviewStore(roots=(tmp_path,), restarting=lambda: restarting).summary(
        tmp_path, question
    )

    assert not summary.answerable
    assert "cannot be read back whole" in summary.unanswerable
    assert (
        f"`uv run --directory {tmp_path} lup-devtools review approve q-1 --as operator`"
    ) in summary.unanswerable
    assert (
        "restarts onto its checkout's newer code shortly" in summary.unanswerable
    ) is restarting


def test_a_call_s_long_strings_are_kept_once_and_leave_with_the_last_question_naming_them(
    tmp_path: Path, relay: QuestionRelay
) -> None:
    content = "a line of the new file\n" * 100
    first = relay.record(new_file(tmp_path, "q-1", content))
    relay.record(new_file(tmp_path, "q-2", content))

    records = [record["parked"] for record in review_records(relay.path)[1:]]
    (kept,) = relay_blobs(relay.path).iterdir()
    assert [record["stored"] for record in records] == [
        [["operation", "payload", "content"]]
    ] * 2
    assert [record["execution_payload"] for record in records] == [None, None]
    assert "a line of the new file" not in relay.path.read_text()
    recorded, _ = relay.questions()
    assert recorded == first.recorded()
    assert recorded.operation.payload["content"] == {"sha256": kept.name}
    whole = relay.resolve(recorded)
    assert whole.operation == first.operation
    assert whole.execution_payload is None and whole.bound()
    assert first.shown().operation == first.operation
    relay.advance("q-1", "completed", "done")
    assert relay.retire(["q-1"]) == []
    relay.advance("q-2", "completed", "done")
    assert relay.retire(["q-2"]) == [kept.name]


def test_a_short_call_stays_whole_in_its_record_and_a_rewrite_beside_it(
    tmp_path: Path, relay: QuestionRelay
) -> None:
    question = new_file(tmp_path, "q-1", "short\n")
    rewrite = {**question.operation.payload, "content": "rewritten\n"}
    rewritten = question.model_copy(update={"execution_payload": rewrite})
    relay.record(
        rewritten.model_copy(update={"fingerprint": rewritten.native_fingerprint()})
    )

    (_, parked) = review_records(relay.path)
    assert "stored" not in parked["parked"]
    assert parked["parked"]["operation"]["payload"]["content"] == "short\n"
    assert parked["parked"]["execution_payload"] == rewrite
    (recorded,) = relay.questions()
    assert relay.resolve(recorded).bound()


@pytest.mark.parametrize(
    ("ran", "state"), [("value = 1\n", "completed"), ("value = 2\n", "in_doubt")]
)
def test_a_retried_write_matches_its_record_and_what_ran_settles_it(
    tmp_path: Path, ran: str, state: str
) -> None:
    target = tmp_path / "module.py"
    content = "value = 1\n" * 300
    relay = QuestionRelay(tmp_path / ".lup/questions.jsonl")
    call = (
        tmp_path,
        "requester",
        "Write",
        json.dumps({"file_path": str(target), "content": content}),
        json.dumps({str(target): None}),
        "reason",
        "rule",
        "",
        "human_only",
    )

    first = review_hook_call(*call, answers=str(relay.answers))
    (_, parked) = review_records(relay.path)
    relay.answer(first["id"], "operator", True)
    retried = review_hook_call(*call, answers=str(relay.answers), execution_id="run")
    observed = observe_hook_call(
        tmp_path,
        "requester",
        "Write",
        {"file_path": str(target), "content": ran * 300},
        "run",
    )

    assert parked["parked"]["stored"] == [["operation", "payload", "content"]]
    assert parked["parked"]["execution_payload"] is None
    assert "value = 1" not in relay.path.read_text()
    assert retried == {"state": "approved", "id": first["id"], "reason": ""}
    assert (observed == []) == (state == "completed")
    settled = relay.find(first["id"])
    assert settled is not None and settled.state == state


def full_copy_log(tmp_path: Path) -> tuple[Path, PersistentQuestion]:
    """A relay kept the older way: a full copy of the question at every transition."""
    question = written_question(tmp_path)
    log = tmp_path / ".lup/questions.jsonl"
    log.parent.mkdir(parents=True)
    copies = [
        question,
        question.model_copy(update={"state": "dispatched", "execution_id": "call"}),
        question.model_copy(
            update={
                "state": "completed",
                "completed": datetime(2026, 9, 1, tzinfo=UTC),
                "outcome": "done",
            }
        ),
    ]
    other = written_question(tmp_path, "q-2")
    log.write_text(
        "".join(
            f"{copy.model_dump_json()}\n"
            for copy in [
                *copies,
                other,
                other.model_copy(update={"state": "approved"}),
            ]
        )
        + json.dumps(
            {
                "question": "q-2",
                "reply": {
                    "author": "s",
                    "text": "hi",
                    "at": "2026-09-02T00:00:00+00:00",
                },
            }
        )
        + "\n"
    )
    return log, question


def test_an_older_log_is_rewritten_once_into_this_shape(tmp_path: Path) -> None:
    log, question = full_copy_log(tmp_path)
    assert not relay_current(log)

    relay = QuestionRelay(log, tmp_path / "answers.jsonl")
    first, second = relay.questions()

    assert relay_current(log)
    records = review_records(log)
    assert records[0] == relay_header()
    assert [record["parked"]["id"] for record in records if "parked" in record] == [
        "q-1",
        "q-2",
    ]
    assert (first.state, first.outcome) == ("completed", "done")
    assert first.since() == datetime(2026, 9, 1, tzinfo=UTC)
    assert second.state == "pending"
    assert relay.replies() == {"q-2": [relay.replies()["q-2"][0]]}
    assert relay.resolve(first).preconditions == question.preconditions
    assert relay.resolve(second).bound()
    rewritten = log.read_bytes()
    migrate_relay(log)
    assert log.read_bytes() == rewritten


def test_the_hook_reads_an_older_log_in_the_new_shape(tmp_path: Path) -> None:
    """The dispatcher's own reader rewrites it too, and matches the parked call's fingerprint."""
    log, question = full_copy_log(tmp_path)

    entries = native_review_records(log)

    assert set(entries) == {"q-1", "q-2"}
    assert recorded_fingerprint(resolved_entry(entries["q-2"], relay_blobs(log))) == (
        question.model_copy(update={"id": "q-2"}).fingerprint
    )


def test_retiring_moves_questions_out_and_sweeps_what_nothing_names(
    tmp_path: Path, relay: QuestionRelay
) -> None:
    relay.record(written_question(tmp_path))
    kept = written_question(tmp_path, "q-2").model_copy(
        update={
            "preconditions": {tmp_path / "other.py": "another document\n"},
        }
    )
    relay.record(kept.model_copy(update={"fingerprint": kept.native_fingerprint()}))
    relay.advance("q-1", "completed", "done")
    archived: list[str] = []

    removed = relay.retire(["q-1"], lambda: archived.append("kept first"))

    assert archived == ["kept first"]
    assert [each.id for each in relay.questions()] == ["q-2"]
    assert all("q-1" not in json.dumps(record) for record in review_records(relay.path))
    named = {path.name for path in relay_blobs(relay.path).iterdir()}
    assert len(removed) == 1 and removed[0] not in named
    (left,) = relay.questions()
    assert relay.resolve(left).preconditions == {
        tmp_path / "other.py": "another document\n"
    }


def test_a_writer_waiting_on_a_rewritten_log_appends_to_the_new_one(
    tmp_path: Path, relay: QuestionRelay
) -> None:
    relay.record(written_question(tmp_path))
    relay.advance("q-1", "completed", "done")
    stale = relay.path.open("rb")
    relay.retire(["q-1"])
    relay.record(written_question(tmp_path, "q-2"))
    stale.close()

    assert [
        each.id for each in QuestionRelay(relay.path, relay.answers).questions()
    ] == ["q-2"]


def test_a_review_settled_long_ago_says_when(
    tmp_path: Path, relay: QuestionRelay
) -> None:
    relay.record(written_question(tmp_path))
    (cancelled,) = relay.transitioned(
        {"q-1": {"state": "cancelled", "outcome": "withdrawn"}},
        datetime.now(UTC) - timedelta(days=30),
    )
    assert datetime.now(UTC) - cancelled.since() > timedelta(days=29)
