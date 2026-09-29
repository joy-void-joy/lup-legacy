"""One ordered record of everything every actor in a run did.

A run's actors each hold their own session, and until now nothing outside
those sessions could see what happened inside one. The journal is that
record: one append-only file per run, one writer, every entry naming the
actor it belongs to.

Ordering needs no coordination. A run holds its state lock for its entire
life, so there is exactly one writer and the sequence number is simply the
count of what came before.

The per-actor view and the merged view are the same sequence, filtered or
not. That is deliberate — a merged view assembled from separate per-actor
logs would have to invent an ordering between them, and knowing what
actually happened first is the one thing a reader wants from a merged view.
"""

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter

from lup.coordination.mail import MessageOutstandingEvent, MessagePostedEvent
from lup.coordination.questions import QuestionAnswer
from lup.coordination.refs import ActorRef
from lup.coordination.sessions import ActorRecord
from lup.channels.models import utc_now
from lup.observability.journal import Journal as SharedJournal
from lup.observability.journal import JournalRecord
from lup.observability.journal import JournalTail as SharedTail
from lup.observability.journal import last_record
from lup.resolver.models import (
    CarriedParent,
    ConcernProgress,
    MaterialQuestion,
    ResolvePhase,
)
from lup.sessions.events import TurnEvent

# lup: ignore[constant-declaration] — the journal's own file name, which the
# writing run and every reader of it must spell alike to meet at all
JOURNAL_FILE = "journal.jsonl"


class PhaseChangedEvent(BaseModel, frozen=True):
    """The run moved to a new phase."""

    type: Literal["phase_changed"] = "phase_changed"
    phase: ResolvePhase


class ConcernProgressedEvent(BaseModel, frozen=True):
    """One concern reached a new status."""

    type: Literal["concern_progressed"] = "concern_progressed"
    progress: ConcernProgress


class QuestionAskedEvent(BaseModel, frozen=True):
    """An actor put a material question to the humans."""

    type: Literal["question_asked"] = "question_asked"
    question: MaterialQuestion
    asked_by: str


class AnswerSettledEvent(BaseModel, frozen=True):
    """A question took its answer, from whichever door supplied it."""

    type: Literal["answer_settled"] = "answer_settled"
    answer: QuestionAnswer
    door: str


class JoinCompletedEvent(BaseModel, frozen=True):
    """One parent was joined, and whether git had to be adjudicated for it."""

    type: Literal["join_completed"] = "join_completed"
    parent: str
    commit: str
    conflicted: bool
    broke: list[str] = []


class JoinPlannedEvent(BaseModel, frozen=True):
    """Which parents have to be merged, and which already ride inside one.

    Concerns are cut from their dependencies' commits, so the branches stack:
    a parent contained in another is already in the tree once that one lands,
    and merging it costs a verification and can cost a merger turn to
    conclude that nothing happened. Named here rather than left to the skip
    inside the loop, so what the run declined to merge is on the record.
    """

    type: Literal["join_planned"] = "join_planned"
    tips: list[str]
    carried: list["CarriedParent"] = []


class JoinRenderedEvent(BaseModel, frozen=True):
    """A join disagreed only in artifacts, and the generator settled it.

    Worth recording because it is the difference between a join that cost a
    merger turn and one that cost a second: every lease touching a catalog
    re-renders both plugin trees, so a conflict there says nothing about the
    work and everything about when each branch last generated.
    """

    type: Literal["join_rendered"] = "join_rendered"
    parent: str


class RecheckReusedEvent(BaseModel, frozen=True):
    """A resume took these concerns' re-checks from the pass that ran them."""

    type: Literal["recheck_reused"] = "recheck_reused"
    concerns: list[str]
    commit: str


class JoinAuditEvent(BaseModel, frozen=True):
    """The finished tree was re-checked against every parent that built it."""

    type: Literal["join_audit"] = "join_audit"
    parents: list[str]
    outstanding: int
    commit: str


class ReviewResidualEvent(BaseModel, frozen=True):
    """Observations an accepting review recorded beside its verdict.

    A residual on a rejected review re-enters the worker's feedback; on an
    accepted one nothing else carries it, and real findings would sit unread
    in the round records. The journal is where they surface — for the CLI,
    the supervisor, and whoever files the follow-up work.
    """

    type: Literal["review_residual"] = "review_residual"
    concern_id: str
    round: int
    residual: list[str]


class CriteriaCarriedEvent(BaseModel, frozen=True):
    """A human took an acceptance over criteria the reviewer left unmet.

    The bar is theirs, so waiving part of it is theirs too, and the waiver
    is worth more than the verdict it produced: whoever lands this concern
    inherits work that does not meet everything it was admitted against,
    and only this says which parts and on whose word.
    """

    type: Literal["criteria_carried"] = "criteria_carried"
    concern_id: str
    round: int
    criteria: list[str]


class ForeignCriteriaEvent(BaseModel, frozen=True):
    """An accepted review credited ids the concern never declared.

    Recorded rather than acted on. Every declared criterion was accounted
    for, so nothing passed unchecked and the verdict stands; the stray label
    is the reviewer's bookkeeping, and turning an acceptance back over it
    spent a revision round re-deriving the same verdict until the budget
    ran out. Journalled so a reviewer that keeps miscrediting is still
    visible to whoever reads the run.
    """

    type: Literal["foreign_criteria"] = "foreign_criteria"
    concern_id: str
    round: int
    labels: list[str]


class VerificationFailedEvent(BaseModel, frozen=True):
    """What one gate saw at the moment it decided a concern's round.

    A run's record held no verification event of any kind: the check ran,
    produced a verdict that decided a concern's fate, and left nothing
    behind. Journalling it puts what the gate saw beside the turns it
    decided about, which is the one place a later session can read it — the
    lease worktree the check ran in is usually still held by the run.
    """

    type: Literal["verification_failed"] = "verification_failed"
    concern_id: str
    round: int
    name: str
    exit_code: int
    output: str


class RecheckRepeatedEvent(BaseModel, frozen=True):
    """A re-check reproduced a standing finding already put to the humans.

    The same lost-criteria set for the same concern asks once; a later join
    that reproduces it is recorded here instead of re-raising an identical
    question per join.
    """

    type: Literal["recheck_repeated"] = "recheck_repeated"
    concern_id: str
    occasion: str
    criteria: list[str]


class RecheckChangedEvent(BaseModel, frozen=True):
    """A recorded finding and a later verdict disagree about lost criteria."""

    type: Literal["recheck_changed"] = "recheck_changed"
    concern_id: str
    occasion: str
    commit: str
    previous_question: MaterialQuestion
    criteria: list[str]


class BaseRefreshedEvent(BaseModel, frozen=True):
    """A lease made from here starts from the branch as it stands now.

    A run pinned to the commit it was created at cannot see a fix made to
    unblock it, and its workers reason about code that has already been
    replaced — reaching careful conclusions that contradict decisions the
    repository has already taken. Recorded whether it moved or not: a
    refresh that could not be made cleanly is the reason the leases beside
    it are still where they were.
    """

    type: Literal["base_refreshed"] = "base_refreshed"
    branch: str
    was: str
    commit: str
    conflicts: list[str] = []
    reason: str = ""


class LeaseRefreshedEvent(BaseModel, frozen=True):
    """What bringing the refreshed base into one lease did, or what stopped it.

    The base event above says where the run now starts; it says nothing
    about which branches actually took it. An answer reaching stdout alone
    is never written down, so a detached run — whose output goes to a
    file nobody is watching — keeps no record that three of its leases were
    refused. A lease still on the old base is the reason its worker reads
    replaced code, which is the failure the refresh exists to prevent.
    """

    type: Literal["lease_refreshed"] = "lease_refreshed"
    concern_id: str
    commit: str
    applied: bool
    conflicts: list[str] = []
    uncommitted: list[str] = []
    reason: str = ""


class LeaseDriftEvent(BaseModel, frozen=True):
    """An abandoned concern's tree does not hold the commit last recorded.

    Recorded at restore rather than raised. The concern failed, so nothing
    in this run reads that tree again — but the work is still on its branch,
    and a reader salvaging it wants both commits named.
    """

    type: Literal["lease_drift"] = "lease_drift"
    concern_id: str
    expected: str
    found: str


class IntegrationRecoveredEvent(BaseModel, frozen=True):
    """An explicit integration move with its preserved recovery evidence."""

    type: Literal["integration_recovered"] = "integration_recovered"
    mode: Literal["restore-recorded", "adopt-head"]
    recorded: str
    before: str
    after: str
    evidence: str
    retired_questions: list[str] = []


class RunFailedEvent(BaseModel, frozen=True):
    """The run reached a terminal failure."""

    type: Literal["run_failed"] = "run_failed"
    reason: str


class ActorBindingRetiredEvent(BaseModel, frozen=True):
    """An operator authorized replacing this binding with a fresh conversation."""

    type: Literal["actor_binding_retired"] = "actor_binding_retired"
    previous: ActorRecord
    reason: str


type RunEvent = (
    PhaseChangedEvent
    | ConcernProgressedEvent
    | QuestionAskedEvent
    | AnswerSettledEvent
    | MessagePostedEvent
    | MessageOutstandingEvent
    | JoinPlannedEvent
    | JoinRenderedEvent
    | JoinCompletedEvent
    | JoinAuditEvent
    | RecheckReusedEvent
    | ReviewResidualEvent
    | CriteriaCarriedEvent
    | ForeignCriteriaEvent
    | VerificationFailedEvent
    | RecheckRepeatedEvent
    | RecheckChangedEvent
    | BaseRefreshedEvent
    | LeaseRefreshedEvent
    | LeaseDriftEvent
    | IntegrationRecoveredEvent
    | RunFailedEvent
    | ActorBindingRetiredEvent
)
"""What the run did, as opposed to what one actor's session did.

These share the sequence with turn events rather than living in a file of
their own, because the one thing a reader wants from a merged view is to
know what actually happened first — and an ordering invented between two
logs cannot answer that.
"""


class JournalEntry(JournalRecord[ActorRef], frozen=True):
    """One event, stamped and attributed."""

    at: datetime
    event: TurnEvent | RunEvent


ENTRY_ADAPTER: TypeAdapter[JournalEntry] = TypeAdapter(JournalEntry)

type JournalTail = SharedTail[JournalEntry]
"""What one follower read, and where it should resume."""


class Journal(SharedJournal[ActorRef, JournalEntry]):
    """The run's single ordered record, appended by one writer.

    The ordering, the paging and the actor filter are the shared journal's;
    what is here is the resolver's own vocabulary for them — an event
    attributed to an actor, and one attributed to the run itself.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root / JOURNAL_FILE, ENTRY_ADAPTER)
        self.run = ActorRef(kind="run", id=root.name)

    def append(self, actor: ActorRef, event: TurnEvent | RunEvent) -> JournalEntry:
        """Record one event."""
        return self.write(
            lambda seq, _previous: JournalEntry(
                seq=seq, at=utc_now(), actor=actor, event=event
            )
        )

    def record(self, event: RunEvent) -> JournalEntry:
        """Record something the run did rather than something an actor did."""
        return self.append(self.run, event)


def journal_tail(root: Path) -> JournalEntry | None:
    """A run's most recent entry, without constructing a journal to ask.

    A status view asks this each time it runs, against a file that reaches
    tens of megabytes in one run. Opening a journal to read one record is
    what a writer needs, so a reader goes straight to the record.
    """
    return last_record(root / JOURNAL_FILE, ENTRY_ADAPTER)
