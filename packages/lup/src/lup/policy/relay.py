"""The durable record and single authority for every final ask.

Detached, supervised and interactive sessions require an explicit recorded
answer. Native execution and a pending native prompt supply no authority.
Generated hooks park an ask and refuse it while it waits; `review wait`, or an
exact retry, consumes the recorded answer once.

The question and its answer are kept apart. The question is written where the
session that asked keeps it, in its checkout's relay; the answer is written
only by the operator's side, into the host's own state
(:func:`~lup.policy.assets.host.review_answers`), which a contained session
reaches read-only. So nothing a session writes into its relay answers
anything: a record there claiming an answer is ignored, and a parked record
whose fields no longer hash to its fingerprint may not be answered at all.

Two invariants hold everywhere:

- **The requester never answers its own question.** Eligibility comes from
  authenticated session relationships recorded here, never from whether an
  answering command happens to be visible in somebody's tool list — which
  would make the reviewer whoever the agent could reach.
- **An approval is single-use and binds to a fingerprint.** A payload that
  changed after somebody answered is a fresh question, not a stale approval
  quietly reused.
"""

import fcntl
import json
from hashlib import sha256
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import cache
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from lup.policy.assets.host import (
    append_review_record,
    recorded_answers,
    review_answers,
    review_answers_home,
    review_fingerprint,
    review_records,
)
from lup.policy.identity import REVIEW_ANSWERS_ENV
from lup.policy.kernel.decision import DecisionEffect
from lup.policy.kernel.semantics import ReviewPurpose, ReviewerRequirement
from lup.policy.operations import Operation
from lup.types import JsonObject

type QuestionState = Literal[
    "pending",
    "approved",
    "rejected",
    "expired",
    "cancelled",
    "stale",
    "preparing",
    "dispatched",
    "completed",
    "failed",
    "in_doubt",
]
"""Where one question stands, including the state nobody wants to be in.

``stale`` is a question whose recorded files moved before anybody approved
it: no approval could release it any more, so it leaves the queue and its
requester asks again against what stands now.

``in_doubt`` is the honest name for a dispatch whose completion evidence never
arrived — a crash between sending an operation and recording its outcome. It
is not retried, because a retry is a second external effect and this
coordinator promises at-most-once dispatch rather than exactly-once effect.
A typed broker may reconcile one where the remote system offers an idempotency
key or an authoritative status query; nothing else may guess.
"""

type ReceiptKind = Literal["observed", "inferred", "recorded"]
"""How an answer reached this record, which decides how much it can be trusted.

``recorded`` is somebody answering through the relay. Historical ``observed``
and ``inferred`` entries describe native behavior, not a person's answer;
neither can release a native retry.
"""


class Principal(BaseModel, frozen=True):
    """One authenticated party in a session relationship.

    ``human`` is the fact that decides whether a human-only question can end
    here. It is authenticated by the launcher rather than claimed by the
    party, because a principal that could assert its own humanity is a
    supervisor chain with an exit at every link.
    """

    id: str
    kind: Literal["human", "agent"] = "agent"
    supervisor: str = ""

    def human(self) -> bool:
        return self.kind == "human"


class SupervisorChain(BaseModel, frozen=True):
    """Who may answer for whom, resolved from authenticated relationships.

    A missing supervisor, a cycle, or a principal declared as its own
    supervisor all resolve the same way: the question climbs to the human.
    Failing upward rather than closed, because the alternative is a question
    that reaches nobody in a session where somebody is right there — and
    failing *upward* rather than sideways, because the human is the one
    principal whose authority is not derived from another.
    """

    principals: list[Principal] = []

    def find(self, principal: str) -> Principal | None:
        return next((entry for entry in self.principals if entry.id == principal), None)

    def above(self, requester: str) -> list[Principal]:
        """Every principal above one requester, nearest first.

        A cycle stops the walk rather than hanging it: the visited set is what
        makes a self-supervising principal resolve to "nobody above me here",
        which the humans below then answer for.
        """
        seen = {requester}
        # lup: ignore[empty-collection] — a walk whose continuation
        # depends on what it has already visited, which is what stops a
        # cycle rather than hanging on one; no comprehension carries it
        chain: list[Principal] = []
        current = self.find(requester)
        while current is not None and current.supervisor:
            if current.supervisor in seen:
                break
            seen.add(current.supervisor)
            above = self.find(current.supervisor)
            if above is None:
                break
            chain.append(above)
            current = above
        return chain

    def eligible(self, requester: str, requirement: ReviewerRequirement) -> list[str]:
        """Who may answer one question, which never includes the requester.

        A human-only question skips agent supervisors entirely rather than
        letting them decline it, because a chain that can pass a question
        along can also pass it to somebody who answers it.
        """
        above = self.above(requester)
        if requirement == "human_only":
            return [entry.id for entry in above if entry.human()]
        return [entry.id for entry in above]


class LineComment(BaseModel, frozen=True):
    """One comment the operator anchored to lines of one document a review shows.

    A pull request's line comment, kept as data rather than folded into the
    note: the file, the first and last line, and which side of the change
    those lines number -- ``before``, the file as the review recorded it, or
    ``after``, as the call would leave it. The requester reads each back as
    ``path:line[-end]: note``, which is where its editor takes it.
    """

    path: Path
    start: int = Field(ge=1)
    end: int = Field(ge=1)
    side: Literal["before", "after"] = "after"
    note: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError(
                f"a comment on {self.path} ends at line {self.end}, before it starts"
            )
        return self

    def spelled(self, root: Path | None = None) -> str:
        """``path:line[-end]: note``, the path relative to *root* where it lies beneath it.

        A comment on the recorded side says so, since its numbers are the
        file's before the change; a note running over several lines keeps
        them, indented beneath the anchor.
        """
        shown = (
            self.path.relative_to(root)
            if root is not None and self.path.is_relative_to(root)
            else self.path
        )
        span = str(self.start) if self.start == self.end else f"{self.start}-{self.end}"
        side = " (before the change)" if self.side == "before" else ""
        body = "\n    ".join(self.note.splitlines())
        return f"{shown}:{span}{side}: {body}"


type AccountSource = Literal["description", "preceding", "doing", "proposal"]
"""Where the requester's own account of a call was found.

``description`` is the note the runtime's tool call carries beside the
command; ``preceding`` what the agent said just before the call, read off its
transcript when the call parked; ``doing`` what its session said it is on,
on the roster; ``proposal`` the reason a `review propose` was given.
"""


class Account(BaseModel, frozen=True):
    """What the requester said a call is for, in its own words, and where the words were found.

    A claim by the agent and never a fact the policy checked, shown beside
    the policy's own reason as the agent's: what it is for is the agent's to
    say, why it asks is the policy's. Recorded when the call parks, so the
    page never reads a transcript, and kept whole.
    """

    source: AccountSource
    text: str


class Answer(BaseModel, frozen=True):
    """One decision about one question, and who made it.

    ``note`` is how yes-plus-instructions and no-plus-instructions travel
    without a second message. It reaches the agent with the resumption or the
    refusal, which is the moment it is worth reading. ``comments`` are the
    operator's line comments, which travel the same way.
    """

    approved: bool
    principal: str
    receipt: ReceiptKind = "recorded"
    unresolved_chain: bool = False
    """Whether no authenticated chain stood behind this answerer's eligibility.

    Recorded on the answer rather than inferred later, because the question it
    settles is about the moment of answering: an approval given while nothing
    could resolve who was entitled to give it is a weaker receipt than one
    given against a chain, and an audit that could not tell them apart would
    report both as approved.
    """
    note: str = ""
    comments: list[LineComment] = []
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RecordedAnswer(BaseModel, frozen=True):
    """One answer as the host keeps it: the review it settles, bound to its fingerprint.

    An answer names the fingerprint it was given against, so it settles only
    the review that still carries that fingerprint, and never one parked
    since under the same id.
    """

    question: str
    fingerprint: str
    answer: Answer


class CapturedFileReview(BaseModel, frozen=True):
    """The original routed file verdict, bound to its captured input and result."""

    path: Path
    effect: DecisionEffect
    reason: str
    rule: str
    rules: list[str]
    before_sha256: str | None
    after_sha256: str | None


class PersistentQuestion(BaseModel, frozen=True):
    """One parked ask, durable, with everything needed to resume it exactly.

    The operation is carried whole rather than summarized, because the
    requesting agent must not be the thing that reconstructs it: an agent
    asked to reissue an approved call is an agent that can reissue a
    different one.
    """

    id: str
    operation: Operation
    fingerprint: str
    preconditions: dict[Path, str | None] = {}
    """File preimages bound to a native hook review, rechecked before dispatch."""
    file_reviews: list[CapturedFileReview] | None = None
    """Original per-file attribution; absent on records that did not capture it."""
    resumption: Literal["coordinator", "native_retry"] = "coordinator"
    """Whether the coordinator dispatches or a native hook checks an exact retry."""
    reason: str
    rule: str = ""
    purpose: ReviewPurpose | None = None
    requirement: ReviewerRequirement = "human_only"
    eligible: list[str] = []
    chain_resolved: bool = True
    """Whether ``eligible`` was resolved from an authenticated chain here.

    False where the boundary that parked this question could not resolve one —
    the compiled dispatcher is hermetic and reaches a verdict without the
    session's principals. It is not the same as resolving to nobody, and
    collapsing the two made every question the live path parked unanswerable.

    What it relaxes is the narrowing and never the invariant: the requester
    still cannot answer, and the answer records that no chain stood behind the
    eligibility, so an audit shows an approval given without one.
    """
    escalation: str = ""
    checkpoint_failure: str = ""
    state: QuestionState = "pending"
    answer: Answer | None = None
    created: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires: datetime | None = None
    completed: datetime | None = None
    outcome: str = ""
    execution_id: str = ""
    """Native invocation observed after dispatch; never an authority receipt."""
    execution_payload: JsonObject | None = None
    """Exact approved native rewrite; the operation retains the requested input."""
    policy_identity: str = ""
    """Which policy judged a native review, part of what its fingerprint covers."""
    resolved: dict[Path, Path] = {}
    """Where each preimage's path resolved when it was parked, which a retry must match."""
    member: str = ""
    """The launched session that asked, by its roster id, where its launch named one."""
    agent: str = ""
    """The native subagent that asked, by its runtime's own id for it.

    Blank where the session's own conversation asked. Beside ``member``
    because the two name one roster row together -- a subagent's row is
    keyed under its session's -- and what reaches that row reaches the
    conversation that is handling the call, while its session is the one
    deciding anything past it.
    """
    moved: list[Path] = []
    """The recorded files that no longer stood as recorded when this review went stale.

    Set where a review was retired as ``stale``: its requester re-reads these
    and asks again against what stands now, since no approval could release
    the call as it was recorded.
    """
    account: list[Account] = []
    """What the requester said the call is for, each with where it was found.

    Beside the fingerprint rather than inside it: a retry of the same call
    may find the agent saying something else first, and the words are the
    agent's claim about the call, never what an approval binds to.
    """

    def native_fingerprint(self) -> str:
        """The digest a native hook binds this record to, recomputed from what it shows.

        The same material the hook hashed when it parked the call
        (:func:`~lup.policy.assets.host.review_fingerprint`), read back off the
        record: the call, the documents it would change, the verdict and the
        policy that reached it.
        """
        operation = self.operation
        return review_fingerprint(
            operation.session,
            str(operation.cwd),
            operation.tool,
            operation.payload,
            {str(path): before for path, before in self.preconditions.items()},
            self.reason,
            self.rule,
            self.purpose or "",
            self.requirement,
            self.execution_payload
            if self.execution_payload is not None
            else operation.payload,
            self.policy_identity,
            {str(path): str(landed) for path, landed in self.resolved.items()},
            [row.model_dump(mode="json") for row in self.file_reviews]
            if self.file_reviews is not None
            else None,
        )

    def bound(self) -> bool:
        """Whether what this question shows is what its fingerprint covers.

        A native review's own fields must hash to the fingerprint an answer
        names; a record altered since it was parked shows one call and carries
        another's authority. A coordinator's question is bound where the
        coordinator dispatches it, against the operation it holds.
        """
        if self.resumption != "native_retry":
            return True
        return self.native_fingerprint() == self.fingerprint

    def settled_by(self, recorded: "RecordedAnswer | None") -> "PersistentQuestion":
        """This question with the operator's answer, where one was recorded for it.

        A waiting question becomes approved or declined; one already carried
        further keeps its state and shows who answered it. An answer given
        against another fingerprint is not this question's.
        """
        if recorded is None or recorded.fingerprint != self.fingerprint:
            return self
        if self.state != "pending":
            return self.model_copy(update={"answer": recorded.answer})
        return self.model_copy(
            update={
                "state": "approved" if recorded.answer.approved else "rejected",
                "answer": recorded.answer,
            }
        )

    @classmethod
    def review_fingerprint(
        cls, operation: Operation, file_reviews: list[CapturedFileReview] | None
    ) -> str:
        """Bind captured attribution to an in-process operation's approval."""
        if file_reviews is None:
            return operation.fingerprint()
        material = [
            operation.fingerprint(),
            [row.model_dump(mode="json") for row in file_reviews],
        ]
        return sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()

    def answerable_by(self, principal: str) -> bool:
        """Whether this principal may answer, which the requester never may.

        Every half is checked here rather than at each caller, because a
        caller that remembered only the eligibility list is a caller that lets
        a requester answer itself by appearing in its own chain.

        An unresolved chain narrows nothing and excludes nobody but the
        requester. That is the honest reading: eligibility unknown here is a
        gap in what this boundary could compute, not a finding that nobody
        qualifies — and read as the second it made the durable queue
        unanswerable on the path that produces most of it.
        """
        if self.state != "pending" or principal == self.operation.requester:
            return False
        return not self.chain_resolved or principal in self.eligible

    def overdue(self, now: datetime | None = None) -> bool:
        """Whether this question has passed its expiry without being answered."""
        if self.expires is None or self.state != "pending":
            return False
        return (now or datetime.now(UTC)) >= self.expires

    def summary(self) -> str:
        """One line for a queue, which says what is being asked and by whom."""
        return (
            f"{self.id}  {self.state:<10} {self.requirement:<18}"
            f" {self.operation.summary()}  — {self.reason}"
        )


class FileSignature(BaseModel, frozen=True):
    """What one append-only file is on disk now; an appended record changes it."""

    size: int = -1
    modified: int = -1
    inode: int = -1

    @classmethod
    def of(cls, path: Path) -> "FileSignature":
        try:
            status = path.stat()
        except FileNotFoundError:
            return cls()
        return cls(
            size=status.st_size, modified=status.st_mtime_ns, inode=status.st_ino
        )


class RelaySignature(BaseModel, frozen=True):
    """What one relay's queue is on disk now: its questions, and the host's answers.

    Both, because a question is appended to the one and its answer to the
    other, and a reader that kept a queue until only the first changed would
    go on showing an answered review as waiting.
    """

    questions: FileSignature = FileSignature()
    answers: FileSignature = FileSignature()


@cache
def answers_file(relay: Path, home: Path) -> Path:
    """The host's file of answers to one relay, found once per relay and home.

    Finding it asks git which repository the relay's checkout belongs to,
    which a checkout never changes; a dashboard reading every queue each
    second asks once rather than every time.
    """
    return review_answers(relay, home)


class QuestionRelay:
    """The durable store every final ask is written to before anybody sees it.

    Append-only on disk, because the failure this has to survive is a crash
    between recording a question and answering it — and a store that rewrites
    a file in place has a window where the question is neither the old one nor
    the new one. Reading folds the log forward, so the last record for an id
    is its state and every earlier record is still there to be read.

    ``answers`` is the host's file of answers to this relay's questions,
    derived from where the relay is unless a caller names it: the one place
    an answer is read from, and the one place :meth:`answer` writes.
    """

    path: Path
    answers: Path

    def __init__(self, path: Path, answers: Path | None = None) -> None:
        self.path = path
        self.answers = (
            answers
            if answers is not None
            else answers_file(path, review_answers_home(REVIEW_ANSWERS_ENV))
        )

    def signature(self) -> RelaySignature:
        """What this relay's queue is on disk now, to read it again only once it changes."""
        return RelaySignature(
            questions=FileSignature.of(self.path),
            answers=FileSignature.of(self.answers),
        )

    def record(self, question: PersistentQuestion) -> PersistentQuestion:
        """Append one question's current state, and return it unchanged."""
        append_review_record(self.path, question.model_dump_json())
        return question

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize a state transition across relay instances and processes.

        A separate lock leaves the log's own read and append locks available.
        Each call opens its own descriptor, so threads also contend for it.
        """
        path = self.path.resolve()
        lock = path.with_name(f"{path.name}.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def questions(self) -> list[PersistentQuestion]:
        """Every question, folded forward to its latest state, with the host's answer.

        Malformed or unterminated lines remain inert evidence. Appenders
        preserve those bytes and frame later records separately, so a torn
        write neither loses the queue nor becomes authority on a later read.
        A record in this relay claiming an answer is skipped: the answer is
        the host's, and :meth:`recorded` is where it is read.
        """

        def valid() -> Iterator[PersistentQuestion]:
            for record in review_records(self.path):
                try:
                    entry = PersistentQuestion.model_validate(record)
                except ValueError:
                    continue
                if entry.state not in ("approved", "rejected"):
                    yield entry

        folded = {entry.id: entry for entry in valid()}
        recorded = self.recorded()
        return [
            entry.settled_by(recorded[entry.id] if entry.id in recorded else None)
            for entry in folded.values()
        ]

    def recorded(self) -> dict[str, RecordedAnswer]:
        """The operator's answer to each of this relay's reviews, by review id."""

        def valid() -> Iterator[RecordedAnswer]:
            for entry in recorded_answers(self.answers).values():
                try:
                    yield RecordedAnswer.model_validate(entry)
                except ValueError:
                    continue

        return {recorded.question: recorded for recorded in valid()}

    def find(self, question: str) -> PersistentQuestion | None:
        return next((entry for entry in self.questions() if entry.id == question), None)

    def pending(self, principal: str = "") -> list[PersistentQuestion]:
        """Questions still waiting, optionally narrowed to one reviewer's own.

        Narrowed by *eligibility* rather than by ownership: a supervisor
        seeing a question it may not answer is a supervisor about to try.
        """
        waiting = [
            entry
            for entry in self.questions()
            if entry.state == "pending" and not entry.overdue()
        ]
        if not principal:
            return waiting
        return [entry for entry in waiting if entry.answerable_by(principal)]

    def answer(
        self,
        question: str,
        principal: str,
        approved: bool,
        note: str = "",
        receipt: ReceiptKind = "recorded",
        comments: Sequence[LineComment] = (),
    ) -> PersistentQuestion:
        """Record one decision on the host, refusing every answer that is not this one's.

        Five refusals, and each is a way an approval could otherwise be
        reused or forged: a question that does not exist, one already
        answered, one whose expiry passed, one this principal may not answer
        — which includes the requester, always — and one whose record no
        longer shows what its fingerprint covers. ``comments`` ride on the
        answer beside the note, and settle nothing of their own.
        """
        with self.transaction():
            entry = self.find(question)
            if entry is None:
                raise ValueError(f"no question {question!r} is recorded")
            if entry.state != "pending":
                raise ValueError(
                    f"question {question!r} is {entry.state} and cannot be answered again"
                )
            if entry.overdue():
                return self.record(entry.model_copy(update={"state": "expired"}))
            if not entry.answerable_by(principal):
                raise ValueError(
                    f"{principal!r} may not answer {question!r}"
                    f" — eligible: {', '.join(entry.eligible) or 'nobody'}"
                )
            if not entry.bound():
                raise ValueError(
                    f"review {question!r} changed after it was parked: what it "
                    "shows is not what its fingerprint covers, so nothing may "
                    "answer it"
                )
            given = RecordedAnswer(
                question=entry.id,
                fingerprint=entry.fingerprint,
                answer=Answer(
                    approved=approved,
                    principal=principal,
                    note=note,
                    comments=list(comments),
                    receipt=receipt,
                    unresolved_chain=not entry.chain_resolved,
                ),
            )
            for directory in (self.answers.parent.parent, self.answers.parent):
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            append_review_record(self.answers, given.model_dump_json())
            return entry.settled_by(given)

    def cancel(self, question: str, reason: str = "") -> PersistentQuestion:
        """Withdraw a question nobody needs answered any more."""
        with self.transaction():
            entry = self.find(question)
            if entry is None:
                raise ValueError(f"no question {question!r} is recorded")
            return self.record(
                entry.model_copy(update={"state": "cancelled", "outcome": reason})
            )

    def expire(self, questions: list[str], reason: str) -> list[PersistentQuestion]:
        """Expire each of these questions still waiting, saying why.

        For questions nobody can use any more — their requester is gone, so
        no retry will ever read the answer — which is not the same as ones
        their requester withdrew. Settled in one transaction over one read, so
        an answer recorded a moment before is never overwritten by the sweep,
        and a sweep over a long record reads it once.
        """
        expired = datetime.now(UTC)
        with self.transaction():
            waiting = {
                entry.id: entry
                for entry in self.questions()
                if entry.state == "pending"
            }
            return [
                self.record(
                    waiting[question].model_copy(
                        update={
                            "state": "expired",
                            "outcome": reason,
                            "expires": expired,
                        }
                    )
                )
                for question in questions
                if question in waiting
            ]

    def retire_stale(
        self, question: str, moved: Sequence[Path], by: str
    ) -> PersistentQuestion:
        """Retire one waiting question whose recorded files moved since it was parked.

        No approval could release it any more: an approval binds to the files
        as the question recorded them, and a retry of the call records them as
        they stand now, which is a fresh question. So it leaves the queue as
        ``stale`` rather than waiting on an answer that could carry out
        nothing, naming what moved and *by* whom it was noticed -- the
        requester's waiter, or the dashboard. One already settled is left as
        it is: an answer recorded a moment before stands.
        """
        with self.transaction():
            entry = self.find(question)
            if entry is None:
                raise ValueError(f"no question {question!r} is recorded")
            if entry.state != "pending":
                return entry
            named = ", ".join(str(path) for path in moved)
            return self.record(
                entry.model_copy(
                    update={
                        "state": "stale",
                        "moved": list(moved),
                        "outcome": f"{named} changed since it was recorded; noticed by {by}",
                        "completed": datetime.now(UTC),
                    }
                )
            )

    def advance(
        self, question: str, state: QuestionState, outcome: str = ""
    ) -> PersistentQuestion:
        """Move one question along its lifecycle after it was answered.

        The dispatch states live here rather than beside the executor because
        the record is what makes at-most-once dispatch checkable: a
        coordinator that crashed after sending finds ``dispatched`` written
        down, and a coordinator that finds it does not send again.
        """
        with self.transaction():
            entry = self.find(question)
            if entry is None:
                raise ValueError(f"no question {question!r} is recorded")
            completed = (
                datetime.now(UTC)
                if state in ("completed", "failed", "in_doubt")
                else entry.completed
            )
            return self.record(
                entry.model_copy(
                    update={"state": state, "outcome": outcome, "completed": completed}
                )
            )

    def dispatchable(self, question: str) -> PersistentQuestion:
        """The approved question this operation may act on, or a refusal saying why.

        The single-use check is here rather than at the executor because every
        route to the executor passes through this: a check at the caller is a
        check each caller can forget, and the one that forgets it is the one
        that dispatches an approval twice.
        """
        entry = self.find(question)
        if entry is None:
            raise ValueError(f"no question {question!r} is recorded")
        if entry.state != "approved":
            raise ValueError(
                f"question {question!r} is {entry.state}, so nothing may be dispatched"
            )
        answer = entry.answer
        if (
            answer is None
            or not answer.approved
            or answer.receipt != "recorded"
            or answer.principal == entry.operation.requester
        ):
            raise ValueError(
                f"question {question!r} has no recorded independent approval"
            )
        return entry
