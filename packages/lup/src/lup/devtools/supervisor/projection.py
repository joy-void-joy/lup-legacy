"""Browser-facing projections of a resolver run.

Every model here is a pure function of what a run leaves on disk: its
persisted :class:`ResolveState` and its question mailbox. Nothing here needs
a resolver attached to this process, which is what lets one page render a
moving run, a parked run, and a finished one through the same code.

The mailbox is authoritative for every question a run asks, pending or
settled. ``state.json``'s question and answer copies are a fold of it, and a
projection reads the mailbox rather than the fold.
"""

import time
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from lup.coordination.bare.store import MAILBOX_DIR
from lup.coordination.mailbox import QUESTION_DIR
from lup.coordination.questions import QuestionAnswer
from lup.coordination.refs import ActorRef
from lup.resolver.mailbox import QuestionMailbox
from lup.resolver.models import (
    ConcernStatus,
    MaterialQuestion,
    ResolvePhase,
    ResolveState,
    VerificationRecord,
)

LIVENESS_WINDOW_SECONDS = 90.0
"""How long after its last write a run still reads as moving.

A judgement, not a fact about the resolver: it trades how quickly a killed
run stops claiming to be live against how long a slow phase may go quiet
without being called parked. Callers that know their own runs' rhythm pass
their own."""
# lup: ignore[constant-declaration] — the projection's own file name, which the
# writer and every reader must spell alike to meet at all
STATE_FILE = "state.json"


class RunStatus(StrEnum):
    """What the run is waiting on, from the operator's view."""

    RUNNING = "running"
    AWAITING_ANSWERS = "awaiting_answers"
    PARKED = "parked"
    COMPLETE = "complete"
    ABORTED = "aborted"
    FAILED = "failed"


class ConcernView(BaseModel, frozen=True):
    """One concern joined across progress, eligibility, and its outcome."""

    id: str
    title: str
    status: ConcernStatus
    reason: str
    eligible: bool
    integration_approved: bool
    eligibility_reason: str
    dependencies: list[str]
    branch: str | None
    commit: str | None
    rounds: int
    failure: str | None


class PendingQuestionView(BaseModel, frozen=True):
    """One question this run has asked, with whatever answer state it holds.

    ``offer`` is a value some door proposed that no promoter has taken yet.
    Showing it is what makes an offer correctable in the page: a human sees
    the pending value and can replace it before it counts.
    """

    question: MaterialQuestion
    asked_by: str
    answered: str | None = None
    offer: str | None = None


class ReviewView(BaseModel, frozen=True):
    """The branch this run built and what mechanically holds about it.

    No verdict. Whether twelve merged concerns are jointly right is a
    judgement, and the actor that can act on the answer is the agent that
    lands the branch — so it is produced from the journal by whoever opens
    the run, not persisted here by a reviewer nothing consumed.
    """

    review_branch: str
    verification: list[VerificationRecord]


class SupervisorState(BaseModel, frozen=True):
    """Everything one page render needs, in one response.

    ``phases`` and ``statuses`` are served from the library enums rather
    than restated in the page, so adding a phase upstream cannot leave the
    frontend silently out of date.
    """

    run_id: str
    live: bool
    status: RunStatus
    phase: ResolvePhase
    phases: list[ResolvePhase]
    statuses: list[ConcernStatus]
    concerns: list[ConcernView]
    pending: list[PendingQuestionView]
    review: ReviewView | None
    failures: list[str]
    rerun_recipe: str
    progress_line: str
    last_activity: float = 0.0


class RunSummary(BaseModel, frozen=True):
    """One row of the persisted-run index."""

    run_id: str
    phase: ResolvePhase | None
    concerns: int
    pending_questions: int
    live: bool = False
    unreadable: bool = False
    detail: str = ""
    last_activity: float = 0.0
    """When this run last wrote, as an epoch seconds stamp.

    A run id is a commit digest, so sorting the rail by name is sorting by
    hash — arbitrary with respect to time, which is the only order an
    operator scanning their runs actually wants."""


class RunIndex(BaseModel, frozen=True):
    """Every run found under the resolver state root."""

    runs: list[RunSummary]


class ActorIndex(BaseModel, frozen=True):
    """Every actor a run's record names, which is every trace it can open."""

    actors: list[ActorRef]


class AnswerSubmission(BaseModel):
    """Answers submitted from the browser, for any subset of the questions."""

    answers: list[QuestionAnswer] = Field(
        description="One value per question being answered now"
    )


class ParkSubmission(BaseModel):
    """A door asking every open wait in this run to give up now."""

    reason: str = Field(
        default="parked from the supervisor page",
        description="Why the run is being parked, recorded for the rerun",
    )


class MessageSubmission(BaseModel):
    """Something a door is telling an actor. This decides nothing."""

    text: str = Field(description="What to say")
    to_actor: str = Field(
        default="",
        description=(
            "Actor label to address. Required to say something; left empty on "
            "a redirect it stops every actor that is working"
        ),
    )
    in_reply_to: str = Field(
        default="", description="Question or message id this answers, if any"
    )
    redirect: bool = Field(
        default=False,
        description=(
            "Refuse the actor's next tool call and hand it this text as the "
            "reason, instead of letting it read alongside what it was doing"
        ),
    )


class NoticeSubmission(BaseModel):
    """A standing fact about this run, true until somebody takes it down.

    Apart from a message rather than a flag on one, because the two are read
    on different schedules: a message is handed over once and consumed, and
    this is restated at the head of every turn for as long as it stands —
    including the first turn of an actor that did not exist when it was
    posted. There is no recipient for the same reason.
    """

    text: str = Field(description="What is true for this whole run")


def answer_recipe(adapter: str, run_id: str, questions: list[MaterialQuestion]) -> str:
    """Build the flag-carrying rerun that answers exactly these questions.

    One definition, because this is the CLI's own interface restated as
    text: the page prints it and the parked run prints it, and a change to
    `resolve`'s flags that reached only one of them would have the
    other teaching a command that no longer exists.
    """
    return " ".join(
        [
            "uv run lup-devtools resolve",
            f"--adapter {adapter}",
            f"--run-id {run_id}",
            *(f"--answer {question.id}=<value>" for question in questions),
        ]
    )


def answer_problems(
    questions: list[MaterialQuestion], answers: list[QuestionAnswer]
) -> list[str]:
    """Every reason these answers could not be offered as given.

    Partial answers are legal — a question is answered by whoever knows that
    decision, whenever they know it — so an unanswered question is not a
    problem here. What remains a problem is an answer to something nothing
    asked, the same question answered twice, or a value outside a gate whose
    choices really are the whole domain — a design question's choices are
    suggestions, so an answer in the reader's own words is not a problem.
    """
    expected = {question.id: question for question in questions}
    submitted = [answer.question_id for answer in answers]
    duplicates = sorted(
        {identifier for identifier in submitted if submitted.count(identifier) > 1}
    )
    unknown = sorted({name for name in submitted if name not in expected})
    invalid = [
        f"{answer.question_id!r} accepts only: "
        + ", ".join(expected[answer.question_id].choices)
        for answer in answers
        if answer.question_id in expected
        and expected[answer.question_id].closed_choices
        and answer.value not in expected[answer.question_id].choices
    ]
    return [
        *(f"answered {name!r} more than once" for name in duplicates),
        *(f"{name!r} names no question this run asked" for name in unknown),
        *invalid,
    ]


def review_branch_name(state: ResolveState) -> str:
    """Name the review branch from the record, or the naming convention."""
    if state.integration is not None:
        return state.integration.branch
    return f"resolve/{state.run_id}/review"


def concern_views(state: ResolveState) -> list[ConcernView]:
    """Join every concern with its progress, eligibility, and outcome.

    Every join here tolerates a missing right-hand side, progress included.
    A run persists its six state files in sequence, so a reader can arrive
    between a concern being written and its progress row landing — and a
    page whose whole job is to report a run that went wrong is the last
    place that should require the run to have gone right.
    """
    progress = {item.concern_id: item for item in state.progress}
    eligibility = {item.concern_id: item for item in state.eligibility}
    outcomes = {item.concern_id: item for item in state.outcomes}
    views: list[ConcernView] = []  # lup: ignore[empty-collection]
    for concern in state.concerns:
        status = progress[concern.id] if concern.id in progress else None
        verdict = eligibility[concern.id] if concern.id in eligibility else None
        outcome = outcomes[concern.id] if concern.id in outcomes else None
        views.append(
            ConcernView(
                id=concern.id,
                title=concern.title,
                status=status.status if status else ConcernStatus.DISCOVERED,
                reason=status.reason if status else "no progress recorded yet",
                eligible=verdict.eligible if verdict else concern.eligible,
                integration_approved=(
                    verdict.integration_approved
                    if verdict
                    else concern.integration_approved
                ),
                eligibility_reason=verdict.reason if verdict else "",
                dependencies=concern.dependencies,
                branch=outcome.branch if outcome else None,
                commit=outcome.commit if outcome else None,
                rounds=len(outcome.rounds) if outcome else 0,
                failure=outcome.failure if outcome else None,
            )
        )
    return views


def question_views(mailbox: QuestionMailbox) -> list[PendingQuestionView]:
    """Project every question this run has asked, from its mailbox."""
    asked = mailbox.questions()
    answered = {
        record.answer.question_id: record.answer.value for record in mailbox.answers()
    }
    offered = {offer.question_id: offer.value for offer in mailbox.offers()}
    return [
        PendingQuestionView(
            question=item.question,
            asked_by=item.asked_by,
            answered=(
                answered[item.question.id] if item.question.id in answered else None
            ),
            offer=offered[item.question.id] if item.question.id in offered else None,
        )
        for item in asked
    ]


def unanswered_questions(views: list[PendingQuestionView]) -> list[MaterialQuestion]:
    """The questions still waiting for a promoted answer."""
    return [view.question for view in views if view.answered is None]


def last_activity(run_root: Path) -> float:
    """When this run last wrote anything a supervisor reads.

    Each question is a directory of its own, so the questions root only
    moves when one is added. Its children carry declaring, offering, and
    settling, which is most of what a live run does. The mailboxes are read the
    same way and for the same reason: one directory per member, whose own
    modification time moves as messages land in it and are taken out.
    """
    questions = run_root / QUESTION_DIR
    mailboxes = run_root / MAILBOX_DIR
    watched = [
        run_root / STATE_FILE,
        questions,
        mailboxes,
        *(questions.iterdir() if questions.is_dir() else []),
        *(mailboxes.iterdir() if mailboxes.is_dir() else []),
    ]
    stamps = [path.stat().st_mtime for path in watched if path.exists()]
    return max(stamps) if stamps else 0.0


def run_is_live(
    state: ResolveState,
    activity: float,
    now: float,
    window_seconds: float = LIVENESS_WINDOW_SECONDS,
) -> bool:
    """Whether this run is still moving.

    Liveness is never probed from ``.run.lock``. Asking for even a shared
    lock can make a run that is concurrently starting fail to take its
    exclusive one, so the page would break the very runs it reports on. A
    run is live when it has not finished and something wrote recently.
    """
    if state.phase.terminal():
        return False
    return now - activity <= window_seconds


def persisted_status(
    state: ResolveState, views: list[PendingQuestionView], live: bool
) -> RunStatus:
    """Derive the operator-facing status from durable state and the mailbox.

    Pending questions are read from the mailbox rather than the phase:
    workers ask mid-turn during ``WORKERS``, so the phase strip legitimately
    reads ``workers`` while a human is being waited on.
    """
    match state.phase:
        case ResolvePhase.FAILED:
            return RunStatus.FAILED
        case ResolvePhase.COMPLETE:
            return RunStatus.COMPLETE
        case ResolvePhase.ABORTED:
            return RunStatus.ABORTED
        case _:
            if not unanswered_questions(views):
                return RunStatus.RUNNING
            return RunStatus.AWAITING_ANSWERS if live else RunStatus.PARKED


def supervisor_state(
    state: ResolveState,
    mailbox: QuestionMailbox,
    adapter: str,
    now: float | None = None,
) -> SupervisorState:
    """Project one run on disk into a complete page render."""
    views = question_views(mailbox)
    activity = last_activity(mailbox.root)
    live = run_is_live(state, activity, time.time() if now is None else now)
    review = (
        ReviewView(
            review_branch=review_branch_name(state),
            verification=state.verification,
        )
        if state.integration is not None and state.integration.completed
        else None
    )
    return SupervisorState(
        run_id=state.run_id,
        live=live,
        status=persisted_status(state, views, live),
        phase=state.phase,
        phases=list(ResolvePhase),
        statuses=list(ConcernStatus),
        concerns=concern_views(state),
        pending=views,
        review=review,
        failures=state.failures,
        rerun_recipe=answer_recipe(adapter, state.run_id, unanswered_questions(views)),
        progress_line=state.tally().concerns_line(),
        last_activity=activity,
    )
