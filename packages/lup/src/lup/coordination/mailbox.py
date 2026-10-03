# lup: ignore[constant-declaration]
# The constants here name the question mailbox's own on-disk layout, which a
# writer and a reader in different processes must agree on to find each other's
# files at all — an identity of this format rather than a choice a caller can
# make.
"""The persisted question mailbox every answer door writes through.

A question is a :class:`~lup.channels.slot.Slot`: declared once by whoever
asks, offered to by any door, and settled exactly once. Messages ride an
:class:`~lup.coordination.mail.ActorMail` instead, held here and reached
through directly, so the run parks on a slot and never on a member's mailbox.

Reached rather than delegated to, because addressing one costs something this
layer does not hold: a message goes to a member, and turning what a door typed
into a member is the roster's. That resolution belongs to the population that
has one, so every sender goes through
:class:`~lup.coordination.cohort.ActorCohort` and a door holding this question
mailbox has no second route to a member's mailbox.

Doors write ``offered``, which is correctable — a mistyped free-text answer
can be replaced right up until it counts, and an offer may arrive before its
question exists, which is what lets a flag answer a question the run has not
asked yet. Exactly one writer promotes offers into ``settled``, taking the
earliest valid one, so "first answer wins" is a deterministic decision
rather than a race between whoever reached the filesystem first.

Generic over the question because what rides in the slot is the asker's:
a resolver gate carries the edit allowances a choice would need, and a
research session's carries none of that. The mailbox reads only what
:class:`~lup.coordination.questions.Question` guarantees, so each caller
round-trips its own type without the layer knowing any of them.
"""

import asyncio
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from lup.coordination.mail import ActorMail
from lup.coordination.questions import Question, QuestionAnswer
from lup.channels.models import (
    ChannelConflictError,
    ChannelCorruptionError,
    Door,
    DoorPolicy,
)
from lup.channels.slot import Slot, SlotSet
from lup.channels.wait import POLL_SECONDS, wait_until

QUESTION_DIR = "questions"
PARK_DIR = "park"
DRAIN_DIR = "drain"
RESUME_DIR = "resume"
ANSWER_POLL_SECONDS = POLL_SECONDS

# The mailbox's own names for the channel package's shapes, so a caller
# reads one vocabulary rather than two.
MailboxConflictError = ChannelConflictError
MailboxCorruptionError = ChannelCorruptionError
AnswerDoor = Door


class PendingQuestion[Q: Question](BaseModel, frozen=True):
    """One question a run is waiting on, written once by whoever asked."""

    run_id: str
    question: Q
    asked_by: str
    asked_at: datetime


class AnswerOffer(BaseModel, frozen=True):
    """One door's proposed answer, correctable until it is promoted."""

    run_id: str
    question_id: str
    value: str
    door: AnswerDoor
    offered_at: datetime


class RecordedAnswer(BaseModel, frozen=True):
    """The promoted answer to one question. Written once, never revised."""

    run_id: str
    answer: QuestionAnswer
    door: AnswerDoor
    answered_at: datetime


class ParkRequest(BaseModel, frozen=True):
    """A door asking every open wait in this run to give up now."""

    run_id: str
    reason: str


class MailboxWait(BaseModel, frozen=True):
    """How one wait ended. ``reason`` is empty only on a complete answer."""

    answered: list[RecordedAnswer]
    unanswered: list[str]
    reason: str = ""


class MailboxSlotRecord[Q: Question](BaseModel, frozen=True):
    """One question slot's payload at whichever of its three states it holds.

    A slot stores one model, and a question's declaration, its correctable
    offer, and its settled answer are three different shapes. Carrying all
    three optionally keeps the slot generic over a single type without
    collapsing what the three of them mean.
    """

    pending: PendingQuestion[Q] | None = None
    offer: AnswerOffer | None = None
    answer: RecordedAnswer | None = None


class QuestionMailbox[Q: Question]:
    """File-backed question, answer, and message exchange for one run.

    The question type is taken at construction rather than read from the
    annotation, because the slots validate against a real class: a mailbox
    parameterized only in the type checker would read every record back as
    whatever base the layer happens to name.
    """

    def __init__(self, root: Path, question_type: type[Q]) -> None:
        self.root = root
        self.question_type = question_type
        self.slots: SlotSet[MailboxSlotRecord[Q]] = SlotSet(
            root / QUESTION_DIR, MailboxSlotRecord[question_type]
        )
        self.mail = ActorMail(root)
        self.park_slot: Slot[ParkRequest] = Slot(root / PARK_DIR, ParkRequest)
        # Two verbs, because they are two requests. Park ends every open
        # *wait*, which reaches a run sitting on an answer and no other; a
        # worker inside a model turn is not waiting on anything, so a busy
        # run is unaffected by it and killing would be its only stop.
        # Draining ends the *work*, at the next boundary where stopping
        # costs nothing. One verb meaning both would surprise whoever
        # wanted the first.
        self.drain_slot: Slot[ParkRequest] = Slot(root / DRAIN_DIR, ParkRequest)
        # Pause and resume are asymmetric on purpose. Pausing is a directive
        # any door may issue; resuming is a decision, and excluding AGENT
        # makes it one the orchestrator physically cannot take for itself.
        self.resume_slot: Slot[ParkRequest] = Slot(
            root / RESUME_DIR, ParkRequest, DoorPolicy(excluded=[Door.AGENT])
        )

    def record_type(self) -> type[MailboxSlotRecord[Q]]:
        """The concrete slot record this mailbox reads and writes.

        Named rather than rebuilt at each call site, so a caller constructing
        one to publish reaches the same parameterization the slots validate
        against.
        """
        return MailboxSlotRecord[self.question_type]

    def queue(self, pending: PendingQuestion[Q]) -> None:
        """Record a question once; re-asking it re-renders what has moved.

        Identical is a no-op, so the first asking keeps its timestamp. A
        restatement takes, because the facts a gate quotes go stale while
        the run is parked on it. A moved answer domain is refused, which is
        the case this guard exists for: an actor redefining one id.
        """
        slot = self.slots.slot(pending.question.id)
        existing = slot.declared()
        if existing is not None and existing.pending is not None:
            if not pending.question.restates(existing.pending.question):
                raise MailboxConflictError(
                    f"question {pending.question.id!r} is already asked differently"
                )
            if pending.question != existing.pending.question:
                slot.redeclare(self.record_type()(pending=pending))
            return
        slot.declare(self.record_type()(pending=pending))

    def settled_answer(self, question_id: str) -> RecordedAnswer | None:
        """The promoted answer to one question, where a promoter took one."""
        record = self.slots.slot(question_id).settled()
        return None if record is None else record.answer

    def offer(self, offer: AnswerOffer) -> None:
        """Propose an answer, replacing any earlier proposal for that question.

        A promoted answer is never revised, so an offer reaching a settled
        question cannot take. Re-offering the value that settled is how a
        rerun recipe resumes, and passes as the no-op it already is. A
        *different* value is a correction, and recording one silently left
        the run advancing under exactly the value its author meant to
        replace — a concern the human had rejected was leased for work. It
        is refused at the one point every door writes through, so no door
        can be the one that still does this quietly.
        """
        settled = self.settled_answer(offer.question_id)
        if settled is None:
            self.slots.slot(offer.question_id).offer(
                self.record_type()(offer=offer), offer.door
            )
            return
        if settled.answer.value != offer.value:
            raise MailboxConflictError(
                f"question {offer.question_id!r} is already settled as "
                f"{settled.answer.value!r}, a promoted answer is not revisable, "
                f"so the offered {offer.value!r} would not take"
            )

    def record(self, answer: RecordedAnswer) -> bool:
        """Promote one answer, or report that another door already won."""
        return self.slots.slot(answer.answer.question_id).settle(
            self.record_type()(answer=answer), answer.door
        )

    def questions(self) -> list[PendingQuestion[Q]]:
        return [
            record.pending
            for record in self.slots.declared()
            if record.pending is not None
        ]

    def offers(self) -> list[AnswerOffer]:
        return [
            record.offer for record in self.slots.offered() if record.offer is not None
        ]

    def answers(self) -> list[RecordedAnswer]:
        return [
            record.answer
            for record in self.slots.settled()
            if record.answer is not None
        ]

    def answered_ids(self) -> list[str]:
        return [record.answer.question_id for record in self.answers()]

    def park(self, request: ParkRequest) -> None:
        self.park_slot.clear()
        self.park_slot.settle(request)

    def parked(self) -> ParkRequest | None:
        return self.park_slot.settled()

    def clear_park(self) -> None:
        """Drop a stale park marker so a resumed run can wait again."""
        self.park_slot.clear()

    def drain(self, request: ParkRequest) -> None:
        """Ask this run to stop taking new work at its next safe boundary."""
        self.drain_slot.clear()
        self.drain_slot.settle(request)

    def draining(self) -> ParkRequest | None:
        return self.drain_slot.settled()

    def clear_drain(self) -> None:
        """Drop a satisfied drain marker so a resumed run works again."""
        self.drain_slot.clear()

    def request_resume(self, request: ParkRequest, door: AnswerDoor) -> bool:
        """Release a paused run. Refused for ``AGENT`` by the slot's own policy."""
        return self.resume_slot.settle(request, door)

    def resumed(self) -> ParkRequest | None:
        return self.resume_slot.settled()


async def wait_for_answers[Q: Question](
    mailbox: QuestionMailbox[Q],
    question_ids: list[str],
    *,
    wait_seconds: float,
    poll_interval_seconds: float = ANSWER_POLL_SECONDS,
    wake: asyncio.Event | None = None,
) -> MailboxWait:
    """Wait for every named answer, a park request, or the deadline.

    The tick bounds how late an out-of-process answer can be noticed; the
    optional event lets an in-process promoter deliver one with no delay at
    all. Both surfaces observe the same promotion, so there is one mechanism
    rather than a fast path and a slow path that can disagree.
    """
    wanted = list(dict.fromkeys(question_ids))

    def recorded() -> dict[str, RecordedAnswer]:
        return {record.answer.question_id: record for record in mailbox.answers()}

    def resolved() -> MailboxWait | None:
        found = recorded()
        answered = [found[name] for name in wanted if name in found]
        outstanding = [name for name in wanted if name not in found]
        if not outstanding:
            return MailboxWait(answered=answered, unanswered=[])
        request = mailbox.parked()
        if request is not None:
            return MailboxWait(
                answered=answered, unanswered=outstanding, reason=request.reason
            )
        return None

    settled = await wait_until(
        resolved,
        wait_seconds=wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
        wake=wake,
    )
    if settled is not None:
        return settled
    found = recorded()
    return MailboxWait(
        answered=[found[name] for name in wanted if name in found],
        unanswered=[name for name in wanted if name not in found],
        reason="no answer arrived before the wait elapsed",
    )
