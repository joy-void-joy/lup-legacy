"""Publishing a run's material questions, and collecting what answers them.

A worker asks through its tools and waits there; a door — the supervisor
page, a CLI flag, an agent writing the mailbox directly — offers a value
whenever a human gets to it. Between the two sits this: the only writer of
recorded answers, and the only thing that folds the mailbox into the state
the run persists.

It holds the run rather than the composer that owns every other phase, so
what it can reach is exactly the state, the mailbox, and the journal.
"""

import asyncio
import logging

from lup.channels.models import utc_now
from lup.policy.identity import ConcernAllowance
from lup.resolver.contracts import ResolverAwaitingAnswers
from lup.resolver.grants import GrantLedger
from lup.resolver.record import (
    AnswerSettledEvent,
    Journal,
    QuestionAskedEvent,
)
from lup.coordination.mailbox import (
    ANSWER_POLL_SECONDS,
    ParkRequest,
    RecordedAnswer,
    wait_for_answers,
)
from lup.coordination.questions import QuestionAnswer
from lup.resolver.mailbox import PendingQuestion, QuestionMailbox
from lup.resolver.models import (
    AllowanceRuling,
    AnswerBatch,
    ConcernStatus,
    MaterialQuestion,
    QuestionBatch,
    ResolveState,
    ResolverConfig,
    allowance_question_id,
)
from lup.resolver.run import ResolveRun

logger = logging.getLogger(__name__)


class QuestionBroker:
    """The run's question desk: publish, promote, wait, and read back."""

    def __init__(
        self,
        config: ResolverConfig,
        run: ResolveRun,
        mailbox: QuestionMailbox,
        journal: Journal,
        grants: GrantLedger,
        answer_wait_seconds: float = 0.0,
        poll_interval_seconds: float = ANSWER_POLL_SECONDS,
    ) -> None:
        self.config = config
        self.run = run
        self.mailbox = mailbox
        self.journal = journal
        self.grants = grants
        self.answer_wait_seconds = answer_wait_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.problems: list[str] = []

    def draining(self) -> ParkRequest | None:
        """Whether an operator has asked this run to stop taking new work."""
        return self.mailbox.draining()

    async def unanswered_for(self, concern_id: str) -> list[MaterialQuestion]:
        """Questions this concern asked that no door has answered yet.

        A worker asks through its tools and waits there, so reaching this
        point with anything outstanding means the tool returned ``parked``
        and the worker submitted rather than guessing. Reading the mailbox
        is what turns that into the run's existing park.
        """
        await self.apply_mailbox()
        answered = self.mailbox.answered_ids()
        return [
            item.question
            for item in self.mailbox.questions()
            if item.question.concern_id == concern_id
            and item.question.id not in answered
        ]

    def raised(self, question_ids: list[str]) -> list[MaterialQuestion]:
        """The questions behind these ids, as the mailbox holds them.

        A re-check reused from a previous pass knows only that it raised a
        question, because the mailbox owns what it says. Reading it back is
        what lets the reuse still be waited on: a question whose answer is
        already recorded resolves at once, and one still open holds the run
        exactly as it would have on the pass that asked it.
        """
        wanted = {identifier: True for identifier in question_ids}
        return [
            item.question
            for item in self.mailbox.questions()
            if item.question.id in wanted
        ]

    def queue_questions(self, questions: list[MaterialQuestion], asked_by: str) -> None:
        """Publish questions so any door can answer them."""
        for question in questions:
            self.mailbox.queue(
                PendingQuestion(
                    run_id=self.config.run_id,
                    question=question,
                    asked_by=asked_by,
                    asked_at=utc_now(),
                )
            )
            self.journal.record(
                QuestionAskedEvent(question=question, asked_by=asked_by)
            )

    def promote_offers(self) -> list[str]:
        """Promote what the doors offered, and report what could not count.

        This is the only writer of recorded answers. A design question's
        choices are the planner's suggestions, so an answer in the human's own
        words is recorded as given; only the reserved integration gates close
        their domain, and an offer outside one is a correctable problem rather
        than a fatal one — a door is a form, not a trusted caller.
        """
        questions = {
            item.question.id: item.question for item in self.mailbox.questions()
        }
        answered = self.mailbox.answered_ids()
        fresh = [
            offer
            for offer in sorted(self.mailbox.offers(), key=lambda item: item.offered_at)
            if offer.question_id in questions and offer.question_id not in answered
        ]
        valid = [
            offer
            for offer in fresh
            if not questions[offer.question_id].closed_choices
            or offer.value in questions[offer.question_id].choices
        ]
        for offer in valid:
            answer = QuestionAnswer(question_id=offer.question_id, value=offer.value)
            self.mailbox.record(
                RecordedAnswer(
                    run_id=self.config.run_id,
                    answer=answer,
                    door=offer.door,
                    answered_at=utc_now(),
                )
            )
            self.journal.record(AnswerSettledEvent(answer=answer, door=offer.door))
        self.publish_grants(
            [
                questions[offer.question_id]
                for offer in valid
                if offer.value == AllowanceRuling.GRANT
            ]
        )
        return [
            f"{offer.question_id} was answered {offer.value!r}, but that gate "
            "accepts only: " + ", ".join(questions[offer.question_id].choices)
            for offer in fresh
            if offer not in valid
        ]

    def unparked(self, state: ResolveState) -> list[str]:
        """Concerns recorded as waiting whose questions the mailbox has answered.

        The status is written where a concern raises to park and overwritten
        only where that concern executes again, so between advances it
        records the last transition rather than what the mailbox holds. A
        parked run is precisely the span in which nothing executes, so the
        one value a human reads to decide whether the run is unblocked is
        the one value nothing refreshes — a stored status says "waiting"
        over a mailbox that has settled every answer it named.

        Derived from the questions rather than tracked alongside them: an
        answer settling is already recorded, and a second record of the same
        fact is a second thing to keep true.

        Only a concern already judged eligible is moved. One still waiting on
        the answer that decides its own approval may yet be found ineligible,
        and ``eligible`` does not lead there — so unparking it on the strength
        of the answer arriving would replace a stale status with an illegal
        transition out of it.
        """
        answered = self.mailbox.answered_ids()
        approved = {item.concern_id for item in state.eligibility if item.eligible}
        waiting = {
            item.concern_id
            for item in state.progress
            if item.status == ConcernStatus.WAITING_FOR_ANSWERS
            and item.concern_id in approved
        }
        outstanding = {
            item.question.concern_id
            for item in self.mailbox.questions()
            if item.question.id not in answered
        }
        return sorted(waiting - outstanding)

    def publish_grants(self, granted: list[MaterialQuestion]) -> None:
        """Extend a lease's authority the moment a human grants it.

        In the same breath as the promotion that settled the answer, because
        the worker waiting on it is woken by that promotion: a document
        written any later would have the worker retrying its edit against a
        gate that had not opened yet, on the strength of an answer it had
        already been handed.

        Each question names the lease that asked it, so this reaches whoever
        asked rather than only the leases a concern stands behind. The one
        that needed it most stands behind none: an integration merger holds
        the reserved lease, and a rule that first meets its exception once
        two branches are joined is the case `request_allowance` exists for.

        The gate is recognized by composing the id its lease would have
        asked under rather than by taking the id apart, and the document is
        widened rather than rewritten — so a lease that legitimately holds
        more keeps it, and a document a human has edited is not racing a
        publisher that would put back what they took out.
        """
        for question in granted:
            for allowance in ConcernAllowance:
                if question.id == allowance_question_id(question.concern_id, allowance):
                    self.grants.extend(question.concern_id, [allowance])

    async def apply_mailbox(self) -> list[str]:
        """Promote the doors' offers and fold the mailbox into persisted state."""
        problems = self.promote_offers()
        async with self.run.lock:
            state = self.run.require()
            questions = QuestionBatch(
                run_id=state.run_id,
                questions=[
                    item.question
                    for item in self.mailbox.questions(include_retired=True)
                ],
            )
            answers = AnswerBatch(
                run_id=state.run_id,
                answers=[record.answer for record in self.mailbox.answers()],
            )
            settled = self.unparked(state)
            if state.questions != questions or state.answers != answers or settled:
                folded = state.model_copy(
                    update={"questions": questions, "answers": answers}
                )
                self.run.persist(
                    self.run.progress_state(folded, settled, ConcernStatus.ELIGIBLE)
                    if settled
                    else folded
                )
        self.run.wake.set()
        return problems

    async def promote_until(self, stop: asyncio.Event) -> None:
        """Keep promoting for the lifetime of one advance.

        This is the only writer of ``answers/``, so an unhandled failure here
        would leave every later wait unsatisfiable by any door — parking the
        run on questions a human had already answered, with nothing saying
        why. One round failing is therefore recorded and retried rather than
        ending the promoter: a malformed offer file, a full disk, or a state
        read that lost a race are all conditions the next round may not meet.
        """
        while not stop.is_set():
            await self.promote_round()
            try:
                async with asyncio.timeout(self.poll_interval_seconds):
                    await stop.wait()
            except TimeoutError:
                continue
        await self.promote_round()

    async def promote_round(self) -> None:
        """Fold the mailbox in once, recording rather than raising a failure."""
        try:
            await self.apply_mailbox()
        except Exception:
            logger.exception("resolver answer promotion failed")
            self.problems.append(
                "an answer-promotion round failed; offers are being retried "
                "and the run log carries the reason"
            )

    async def await_questions(self, questions: list[MaterialQuestion]) -> AnswerBatch:
        """Wait for every named question, or park the run on what is missing."""
        retired = self.mailbox.retired_ids()
        questions = [question for question in questions if question.id not in retired]
        if not questions:
            return AnswerBatch(run_id=self.config.run_id, answers=[])
        await self.apply_mailbox()
        result = await wait_for_answers(
            self.mailbox,
            [question.id for question in questions],
            wait_seconds=self.answer_wait_seconds,
            poll_interval_seconds=self.poll_interval_seconds,
            wake=self.run.wake,
        )
        problems = await self.apply_mailbox()
        if result.unanswered:
            raise ResolverAwaitingAnswers(
                [
                    question
                    for question in questions
                    if question.id in result.unanswered
                ],
                [*problems, *self.problems],
            )
        return AnswerBatch(
            run_id=self.config.run_id,
            answers=[record.answer for record in result.answered],
        )

    def answers_for(self, concern_id: str) -> list[QuestionAnswer]:
        """Every recorded answer to a question this concern asked."""
        state = self.run.require()
        question_ids = {
            question.id
            for question in (state.questions.questions if state.questions else [])
            if question.concern_id == concern_id
            and question.id not in state.retired_questions
        }
        return [
            answer
            for answer in (state.answers.answers if state.answers else [])
            if answer.question_id in question_ids
        ]
