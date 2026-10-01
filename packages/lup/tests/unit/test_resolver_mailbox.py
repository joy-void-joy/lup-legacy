"""The lock-free question mailbox every answer door writes through."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lup.coordination.mailbox import (
    AnswerDoor,
    AnswerOffer,
    MailboxConflictError,
    MailboxCorruptionError,
    ParkRequest,
    RecordedAnswer,
    wait_for_answers,
)
from lup.coordination.questions import QuestionAnswer
from lup.resolver.mailbox import PendingQuestion, QuestionMailbox
from lup.resolver.models import MaterialQuestion

EPOCH = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def question(identifier: str, choices: list[str] | None = None) -> MaterialQuestion:
    return MaterialQuestion(
        id=identifier,
        concern_id="alpha",
        prompt=f"Decide {identifier}?",
        choices=choices or [],
    )


def pending(identifier: str, choices: list[str] | None = None) -> PendingQuestion:
    return PendingQuestion(
        run_id="run-1",
        question=question(identifier, choices),
        asked_by="alpha",
        asked_at=EPOCH,
    )


def offer(identifier: str, value: str, seconds: int = 0) -> AnswerOffer:
    return AnswerOffer(
        run_id="run-1",
        question_id=identifier,
        value=value,
        door=AnswerDoor.PAGE,
        offered_at=EPOCH + timedelta(seconds=seconds),
    )


def recorded(identifier: str, value: str) -> RecordedAnswer:
    return RecordedAnswer(
        run_id="run-1",
        answer=QuestionAnswer(question_id=identifier, value=value),
        door=AnswerDoor.PAGE,
        answered_at=EPOCH,
    )


def test_queueing_the_same_question_twice_is_a_no_op(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    mailbox.queue(pending("q1"))
    mailbox.queue(pending("q1"))

    assert [item.question.id for item in mailbox.questions()] == ["q1"]


def test_reasking_a_question_differently_is_refused(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    mailbox.queue(pending("q1"))

    with pytest.raises(MailboxConflictError):
        mailbox.queue(pending("q1", ["yes", "no"]))


def test_a_gate_requoting_facts_that_moved_re_renders_rather_than_conflicts(
    tmp_path: Path,
) -> None:
    """The assembly gate names its base and how far behind that base is.

    Both move while the run is parked on the question, so the gate
    re-derives itself on resume — and refused by the guard meant for an actor
    redefining one id, the run could not reach its own last step.
    """
    mailbox = QuestionMailbox(tmp_path)
    mailbox.queue(pending("integration-assembly", ["approve", "defer"]))
    moved = pending("integration-assembly", ["approve", "defer"])
    restated = moved.model_copy(
        update={
            "question": moved.question.model_copy(
                update={"prompt": "Merge 5 concerns onto abc1234, 3 behind dev?"}
            )
        }
    )

    mailbox.queue(restated)

    assert [item.question.prompt for item in mailbox.questions()] == [
        "Merge 5 concerns onto abc1234, 3 behind dev?"
    ]


def test_an_offer_is_correctable_until_it_is_promoted(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    mailbox.offer(offer("q1", "typo"))
    mailbox.offer(offer("q1", "corrected", seconds=1))

    assert [item.value for item in mailbox.offers()] == ["corrected"]


def test_an_offer_may_arrive_before_its_question(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    mailbox.offer(offer("q1", "yes"))

    assert mailbox.questions() == []
    assert [item.question_id for item in mailbox.offers()] == ["q1"]


def test_only_the_first_promotion_of_a_question_is_recorded(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)

    assert mailbox.record(recorded("q1", "first")) is True
    assert mailbox.record(recorded("q1", "second")) is False
    assert [item.answer.value for item in mailbox.answers()] == ["first"]


def test_a_half_written_record_is_never_read(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    mailbox.queue(pending("q1"))
    (tmp_path / "questions" / ".q2.json.tmp").write_text(
        "{ truncated", encoding="utf-8"
    )

    assert [item.question.id for item in mailbox.questions()] == ["q1"]


def test_a_corrupt_record_is_named_rather_than_skipped(tmp_path: Path) -> None:
    """A slot decides something, so an unreadable one must not read as absent."""
    mailbox = QuestionMailbox(tmp_path)
    slot = tmp_path / "questions" / "q1"
    slot.mkdir(parents=True)
    (slot / "declared.json").write_text("[]", encoding="utf-8")

    with pytest.raises(MailboxCorruptionError, match="declared.json"):
        mailbox.questions()


def test_the_park_marker_is_the_only_thing_cleared(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    mailbox.queue(pending("q1"))
    mailbox.park(ParkRequest(run_id="run-1", reason="operator parked"))

    assert mailbox.parked() is not None
    mailbox.clear_park()

    assert mailbox.parked() is None
    assert [item.question.id for item in mailbox.questions()] == ["q1"]


def test_parking_and_draining_are_two_requests(tmp_path: Path) -> None:
    """Park ends waits, drain ends work; one verb meaning both would surprise.

    A worker inside a model turn waits on nothing, so a run parked while
    busy kept working — and killing it was the only way to stop one.
    """
    mailbox = QuestionMailbox(tmp_path)

    mailbox.park(ParkRequest(run_id="run-1", reason="operator parked"))

    assert mailbox.parked() is not None
    assert mailbox.draining() is None

    mailbox.drain(ParkRequest(run_id="run-1", reason="operator drained"))

    assert mailbox.draining() is not None
    assert mailbox.clear_park() is None
    assert mailbox.draining() is not None


def test_a_satisfied_drain_is_cleared_so_a_resume_works(tmp_path: Path) -> None:
    """Left standing, the run stops again at the first boundary of the resume."""
    mailbox = QuestionMailbox(tmp_path)
    mailbox.drain(ParkRequest(run_id="run-1", reason="operator drained"))

    mailbox.clear_drain()

    assert mailbox.draining() is None


async def test_an_already_answered_question_never_waits(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    mailbox.record(recorded("q1", "yes"))

    result = await wait_for_answers(mailbox, ["q1"], wait_seconds=0)

    assert result.unanswered == []
    assert [item.answer.value for item in result.answered] == ["yes"]


async def test_the_wait_ends_the_moment_an_answer_is_promoted(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    wake = asyncio.Event()

    async def promote() -> None:
        mailbox.record(recorded("q1", "yes"))
        wake.set()

    waiting = asyncio.create_task(
        wait_for_answers(mailbox, ["q1"], wait_seconds=5, wake=wake)
    )
    await asyncio.sleep(0)
    await promote()
    result = await asyncio.wait_for(waiting, timeout=1)

    assert result.reason == ""
    assert [item.answer.value for item in result.answered] == ["yes"]


async def test_a_park_request_ends_an_open_wait(tmp_path: Path) -> None:
    mailbox = QuestionMailbox(tmp_path)
    mailbox.park(ParkRequest(run_id="run-1", reason="operator parked"))

    result = await wait_for_answers(mailbox, ["q1"], wait_seconds=5)

    assert result.unanswered == ["q1"]
    assert result.reason == "operator parked"


async def test_an_elapsed_wait_reports_what_is_still_outstanding(
    tmp_path: Path,
) -> None:
    mailbox = QuestionMailbox(tmp_path)
    mailbox.record(recorded("q1", "yes"))

    result = await wait_for_answers(
        mailbox, ["q1", "q2"], wait_seconds=0.05, poll_interval_seconds=0.01
    )

    assert [item.answer.question_id for item in result.answered] == ["q1"]
    assert result.unanswered == ["q2"]
    assert "elapsed" in result.reason
