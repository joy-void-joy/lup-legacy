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

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from lup.policy.assets.host import append_review_record
from lup.policy.relay import (
    LineComment,
    PersistentQuestion,
    QuestionRecord,
    QuestionRelay,
    RecordedRemark,
    RecordedReply,
    Remark,
    Reply,
)


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


class ReviewThread:
    """One relay's remarks and replies: where each is written, and how each is read back.

    The relay's log holds the requester's replies, and the host's file of
    answers to it the operator's remarks; the relay folds both, reading each
    only where it grew since it last read.
    """

    def __init__(self, relay: QuestionRelay) -> None:
        self.relay = relay

    @classmethod
    def of(cls, relay: QuestionRelay) -> "ReviewThread":
        return cls(relay)

    def remarks(self) -> dict[str, list[RecordedRemark]]:
        """Every remark the operator made on this relay's reviews, by review id, oldest first."""
        return self.relay.remarks()

    def replies(self) -> dict[str, list[Reply]]:
        """Every reply a requester wrote on this relay's reviews, by review id, oldest first."""
        return self.relay.replies()

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
        if unverifiable := question.unverifiable():
            raise ValueError(f"review {question.id!r}: {unverifiable}")
        if unbound := question.unbound():
            raise ValueError(f"review {question.id!r}: {unbound}")
        recorded = RecordedRemark(
            question=question.id,
            fingerprint=question.fingerprint,
            remark=Remark(principal=principal, note=note, comments=list(comments)),
        )
        answers = self.relay.answers
        for directory in (answers.parent.parent, answers.parent):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        append_review_record(answers, recorded.model_dump_json())
        return recorded

    def reply(self, question: QuestionRecord, author: str, text: str) -> Reply:
        """Record the requester's reply on its own review."""
        return self.relay.reply(
            RecordedReply(question=question.id, reply=Reply(author=author, text=text))
        ).reply

    def said(self, question: QuestionRecord) -> list[ThreadEntry]:
        """Everything said on one review, read from this relay and its answers."""
        return spoken_on(question, self.remarks(), self.replies())


def spoken_on(
    question: QuestionRecord,
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
