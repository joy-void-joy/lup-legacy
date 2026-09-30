"""What the operator and the requester say on a review besides answering it.

An answer settles a review, and the note and line comments on it reach the
requester with the settling. Two more things are said on a review while it
waits, and neither settles it:

- a **remark**: the operator's note and line comments sent without deciding.
  It is recorded on the host beside the answers, where only the operator
  writes, so it wakes the requester's `review wait` the way an answer does
  and leaves the review waiting;
- a **reply**: what the requester says back on its own review, recorded in
  its own relay, where only its sessions write.

Neither is authority, and neither is read as one: the hook and the relay read
an answer by its shape, which neither record has. A remark is bound to the
fingerprint it was written against, as an answer is, so it never shows on a
review parked since under the same id. Both are read in the order they were
said, beside the answer, as the review's thread.
"""

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from lup.policy.assets.host import append_review_record, review_records
from lup.policy.relay import LineComment, PersistentQuestion, QuestionRelay


class Remark(BaseModel, frozen=True):
    """The operator's note and line comments on a review, sent without deciding it."""

    principal: str
    note: str = ""
    comments: list[LineComment] = []
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RecordedRemark(BaseModel, frozen=True):
    """One remark as the host keeps it: the review it was written on, bound to its fingerprint."""

    question: str
    fingerprint: str
    remark: Remark


class Reply(BaseModel, frozen=True):
    """What the requester said back on its own review."""

    author: str
    text: str = Field(min_length=1)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RecordedReply(BaseModel, frozen=True):
    """One reply as the requester's relay keeps it, beside the reviews it parked."""

    question: str
    reply: Reply


class ThreadEntry(BaseModel, frozen=True):
    """One thing said on a review, as a reader of its thread sees it.

    ``kind`` says who could have said it: the operator remarking or
    answering, or the requester replying. ``approved`` is the answer's
    verdict, and nothing on the other two.
    """

    kind: Literal["remark", "reply", "answer"]
    author: str
    text: str = ""
    comments: list[LineComment] = []
    at: datetime
    approved: bool | None = None


class ReviewThread(BaseModel, frozen=True):
    """One relay's remarks and replies: where each is written, and how each is read back.

    ``questions`` is the relay the requester's sessions write, which holds
    their replies; ``answers`` is the host's file of answers to it, which
    holds the operator's remarks.
    """

    questions: Path
    answers: Path

    @classmethod
    def of(cls, relay: QuestionRelay) -> "ReviewThread":
        return cls(questions=relay.path, answers=relay.answers)

    def remarks(self) -> dict[str, list[RecordedRemark]]:
        """Every remark the operator made on this relay's reviews, by review id, oldest first."""

        def valid() -> Iterator[RecordedRemark]:
            for record in review_records(self.answers):
                if "remark" not in record:
                    continue
                try:
                    yield RecordedRemark.model_validate(record)
                except ValidationError:
                    continue

        ordered = sorted(valid(), key=lambda recorded: recorded.question)
        return {
            question: list(recorded)
            for question, recorded in groupby(ordered, key=lambda each: each.question)
        }

    def replies(self) -> dict[str, list[Reply]]:
        """Every reply a requester wrote on this relay's reviews, by review id, oldest first."""

        def valid() -> Iterator[RecordedReply]:
            for record in review_records(self.questions):
                if "reply" not in record:
                    continue
                try:
                    yield RecordedReply.model_validate(record)
                except ValidationError:
                    continue

        ordered = sorted(valid(), key=lambda recorded: recorded.question)
        return {
            question: [each.reply for each in recorded]
            for question, recorded in groupby(ordered, key=lambda each: each.question)
        }

    def remark(
        self,
        question: PersistentQuestion,
        principal: str,
        note: str,
        comments: Sequence[LineComment] = (),
    ) -> RecordedRemark:
        """Record the operator's remark on one review, which answers nothing.

        Refused where there is nothing to say, and where the record no longer
        shows what its fingerprint covers -- a remark on an altered record
        would be read against a call the operator never saw.
        """
        if not note.strip() and not comments:
            raise ValueError("a remark says something: a note or a line comment")
        if not question.bound():
            raise ValueError(
                f"review {question.id!r} changed after it was parked: what it "
                "shows is not what its fingerprint covers"
            )
        recorded = RecordedRemark(
            question=question.id,
            fingerprint=question.fingerprint,
            remark=Remark(principal=principal, note=note, comments=list(comments)),
        )
        for directory in (self.answers.parent.parent, self.answers.parent):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        append_review_record(self.answers, recorded.model_dump_json())
        return recorded

    def reply(self, question: PersistentQuestion, author: str, text: str) -> Reply:
        """Record the requester's reply on its own review."""
        recorded = RecordedReply(
            question=question.id, reply=Reply(author=author, text=text)
        )
        append_review_record(self.questions, recorded.model_dump_json())
        return recorded.reply

    def said(self, question: PersistentQuestion) -> list[ThreadEntry]:
        """Everything said on one review, read from this relay and its answers."""
        return spoken_on(question, self.remarks(), self.replies())


def spoken_on(
    question: PersistentQuestion,
    remarks: dict[str, list[RecordedRemark]],
    replies: dict[str, list[Reply]],
) -> list[ThreadEntry]:
    """Everything said on one review, the answer included, in the order it was said.

    Out of readings a caller already holds, so a queue read once serves every
    review in it. A remark written against another fingerprint was written
    on a review parked earlier under the same id, and is not this one's.
    """
    answer = question.answer
    entries = [
        *(
            ThreadEntry(
                kind="remark",
                author=recorded.remark.principal,
                text=recorded.remark.note,
                comments=recorded.remark.comments,
                at=recorded.remark.at,
            )
            for recorded in remarks.get(question.id, [])
            if recorded.fingerprint == question.fingerprint
        ),
        *(
            ThreadEntry(kind="reply", author=reply.author, text=reply.text, at=reply.at)
            for reply in replies.get(question.id, [])
        ),
        *(
            [
                ThreadEntry(
                    kind="answer",
                    author=answer.principal,
                    text=answer.note,
                    comments=answer.comments,
                    at=answer.at,
                    approved=answer.approved,
                )
            ]
            if answer is not None
            else []
        ),
    ]
    return sorted(entries, key=lambda entry: entry.at)
