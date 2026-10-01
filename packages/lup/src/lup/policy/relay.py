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

The relay keeps each question once. Its log holds a record per question as it
was parked -- every document it binds, a file's preimage or what a verdict
judged it becoming, and every string of its call a kibibyte or longer, kept
once in a content store beside the log and named in the record by digest --
and a small transition per state it moves to since.
Reading folds the transitions into the questions they move; a reader that
stays open reads only what was appended since it last read. What the
fingerprint binds is still each document whole: a record is read back through
the documents its digests name, and one that cannot be read back cannot be
answered. A question that settled long enough ago leaves the log for the
archive its reader keeps (:meth:`QuestionRelay.retire`), and the documents no
question names any more leave the store with it.

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
import os
import threading
import time
from hashlib import sha256
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from functools import cache
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import (
    BaseModel,
    Field,
    SkipValidation,
    ValidationError,
    model_validator,
)

from lup.policy.assets.host import (
    append_relay_records,
    append_review_record,
    bound_parts,
    document_name,
    fold_relay,
    migrate_relay,
    named_blobs,
    opened_relay,
    park_relay_entry,
    relay_blobs,
    relay_current,
    relay_header,
    relay_lock,
    resolved_entry,
    review_answers,
    review_answers_home,
    review_fingerprint,
    rewrite_relay,
    stored_form,
    stream_records,
)
from lup.execution.locks import exclusive
from lup.policy.identity import REVIEW_ANSWERS_ENV
from lup.policy.kernel.decision import DecisionEffect
from lup.policy.kernel.semantics import ReviewPurpose, ReviewerRequirement
from lup.policy.operations import Operation
from lup.types import JsonObject, JsonValue

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


type AccountSource = Literal[
    "description", "justification", "preceding", "doing", "proposal"
]
"""Where the requester's own account of a call was found.

``description`` is the note Claude Code's tool call carries beside the
command, saying what it does; ``justification`` the reason a Codex shell call
gives for running outside its sandbox; ``preceding`` what the agent said just
before the call, read off its transcript when the call parked; ``doing`` what
its session said it is on, on the roster; ``proposal`` the reason a
`review propose` was given.
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


class Remark(BaseModel, frozen=True):
    """The operator's note and line comments on a review, sent without deciding it."""

    principal: str
    note: str = ""
    comments: list[LineComment] = []
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RecordedRemark(BaseModel, frozen=True):
    """One remark as the host keeps it beside the answers: the review it was written on, bound to its fingerprint."""

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


class StoredDocument(BaseModel, frozen=True):
    """A document the relay keeps once, in the store beside its log, named by the SHA-256 of its UTF-8 text."""

    sha256: str


class FileVerdict(BaseModel, frozen=True):
    """The original routed verdict on one file, bound by digest to the images it judged."""

    path: Path
    effect: DecisionEffect
    reason: str
    rule: str
    rules: list[str]
    before_sha256: str | None
    after_sha256: str | None


class CapturedFileReview(FileVerdict, frozen=True):
    """The original routed file verdict, bound to its captured input and result.

    ``after`` is the document the verdict judged, which is what a reviewer
    is shown the file becoming; ``None`` with a digest beside it is a record
    that kept only the digest.
    """

    after: str | None = None


class RecordedFileReview(FileVerdict, frozen=True):
    """A file verdict as the relay records it: the document it judged named by digest.

    ``None`` where the call removes the file, or where the record kept only
    the digest beside it.
    """

    after: StoredDocument | None = None


class CommandSegment(BaseModel, frozen=True):
    """One command of a parked line, and the verdict it reached on its own.

    The line's record as a reviewer reads it: every command that asks, each
    with its own reason and rule, beside the ones allowed on their own: the
    rows the verdict kept when it parked
    (:attr:`~lup.policy.kernel.decision.KernelDecision.segments`). ``command``
    is blank for a verdict about the line rather than one of its commands.
    """

    command: str
    effect: DecisionEffect
    reason: str
    rule: str


class UnpreviewedStep(BaseModel, frozen=True):
    """One step of a parked command whose effect no document states.

    ``cause`` is ``run`` where only running the step makes its result, and
    ``unread`` where a file it leaves does not read as text; ``paths`` are
    the files it leaves so, and none where it is a program that may write
    wherever it likes.
    """

    command: str
    paths: list[Path]
    cause: Literal["run", "unread"]


class QuestionRecord(BaseModel, frozen=True):
    """One parked ask, and everything it records beside the documents it binds.

    The operation is carried whole rather than summarized, because the
    requesting agent must not be the thing that reconstructs it: an agent
    asked to reissue an approved call is an agent that can reissue a
    different one. The documents -- each file's preimage, and what each
    file verdict judged it becoming -- are the two forms' own:
    :class:`RecordedQuestion` names them by digest, as the relay folds its
    log, and :class:`PersistentQuestion` holds them whole, as a reviewer and
    a fingerprint read them.
    """

    id: str
    operation: Operation
    fingerprint: str
    unpreviewed: list[UnpreviewedStep] | None = None
    """The steps of a command no document shows, beside the files ``file_reviews`` does."""
    segments: list[CommandSegment] | None = None
    """Each command of the line with its own verdict; absent on records that kept none."""
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
    """Exact approved native rewrite; the operation retains the requested input.

    ``None`` where the retry runs the requested input itself, which is how a
    record keeps a rewrite that repeats it.
    """
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
    scheme: list[str] | None = None
    """The parts the fingerprint binds beyond the call itself, by name, in the order it hashed them.

    Kept so a reader on other code tells a record it cannot check from one
    that changed; absent on a record that keeps none, which binds the parts
    it carries.
    """
    changed: datetime | None = None
    """When it came to the state it stands in, where a transition moved it; its parking otherwise."""

    def since(self) -> datetime:
        """When it came to the state it stands in: the operator's answer, its last transition, else its parking."""
        if self.state in ("approved", "rejected") and self.answer is not None:
            return self.answer.at
        return self.changed if self.changed is not None else self.created

    def unverifiable(self) -> str:
        """Why this reader cannot check the record against its fingerprint, or nothing where it can.

        A record whose scheme names a part this code does not know was
        parked by a newer hook: it has not changed, and this reader cannot
        tell either way, so it says which rather than refusing it as altered.
        """
        if (
            self.resumption != "native_retry"
            or self.scheme is None
            or bound_parts({"scheme": self.scheme}) is not None
        ):
            return ""
        return (
            "this dashboard runs older code than the hook that parked this "
            "review, so it cannot check the review against its fingerprint"
        )

    def settled_by(self, recorded: "RecordedAnswer | None") -> Self:
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


class RecordedQuestion(QuestionRecord, frozen=True):
    """One question as the relay folds it from its log: every document it binds named by digest.

    What a queue is read as, since it needs none of the documents; the
    relay reads one back whole (:meth:`QuestionRelay.resolve`) for whoever
    shows it, checks it against its fingerprint, or carries it out.
    """

    preconditions: dict[Path, StoredDocument | None] = {}
    """Each file the call binds, by the document it held when it was parked; ``None`` where none stood."""
    file_reviews: list[RecordedFileReview] | None = None
    """Original per-file attribution; absent on records that did not capture it."""
    stored: list[list[str | int]] = []
    """Where a string of the call stood that the store keeps instead, each a path of keys and indices.

    Each leads into ``operation.payload`` or ``execution_payload``, where the
    record keeps the string's name (:class:`StoredDocument`, as a JSON
    object) in its place; nothing where the call keeps every string itself.
    """


class PersistentQuestion(QuestionRecord, frozen=True):
    """One parked ask with every document it binds whole: everything needed to resume it exactly."""

    preconditions: dict[Path, str | None] = {}
    """File preimages bound to a native hook review, rechecked before dispatch."""
    file_reviews: list[CapturedFileReview] | None = None
    """Original per-file attribution; absent on records that did not capture it."""

    def recorded(self) -> RecordedQuestion:
        """This question as the relay records it: every document it holds, and each long string of its call, named by digest.

        Worked out as the relay works out the record it parks
        (:func:`~lup.policy.assets.host.stored_form`), so the two never differ.
        """
        return RecordedQuestion.model_validate(
            stored_form(self.model_dump(mode="json"), document_name)
        )

    def shown(self) -> RecordedQuestion:
        """This question as a reviewer's page carries it: its call whole, its documents named by digest.

        The page reads each file's documents beside the question, and the
        tool input off the question itself.
        """
        recorded = self.recorded()
        return recorded.model_copy(
            update={
                "operation": self.operation,
                "execution_payload": self.execution_payload
                if recorded.execution_payload is not None
                else None,
                "stored": [],
            }
        )

    def bound_parts(self) -> dict[str, JsonValue] | None:
        """What else this record's fingerprint binds, by name, or ``None`` where its scheme is not this code's."""
        carried: dict[str, JsonValue] = {
            "file_reviews": [row.model_dump(mode="json") for row in self.file_reviews]
            if self.file_reviews is not None
            else None,
            "unpreviewed": [step.model_dump(mode="json") for step in self.unpreviewed]
            if self.unpreviewed is not None
            else None,
            "segments": [row.model_dump(mode="json") for row in self.segments]
            if self.segments is not None
            else None,
        }
        return bound_parts(
            {
                **carried,
                **({"scheme": self.scheme} if self.scheme is not None else {}),
            }
        )

    def native_fingerprint(self) -> str:
        """The digest a native hook binds this record to, recomputed from what it shows.

        The same material the hook hashed when it parked the call
        (:func:`~lup.policy.assets.host.review_fingerprint`), read back off the
        record: the call, the documents it would change, the verdict and the
        policy that reached it. Blank where the record's scheme is not this
        code's (:meth:`unverifiable`).
        """
        operation = self.operation
        bound = self.bound_parts()
        if bound is None:
            return ""
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
            bound,
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

    @classmethod
    def review_fingerprint(
        cls,
        operation: Operation,
        file_reviews: list[CapturedFileReview] | None,
        unpreviewed: list[UnpreviewedStep] | None,
        segments: list[CommandSegment] | None,
    ) -> str:
        """Bind captured attribution to an in-process operation's approval."""
        if file_reviews is None and unpreviewed is None and segments is None:
            return operation.fingerprint()
        material = [
            operation.fingerprint(),
            [row.model_dump(mode="json") for row in file_reviews or []],
            [step.model_dump(mode="json") for step in unpreviewed or []],
            *(
                [[row.model_dump(mode="json") for row in segments]]
                if segments is not None
                else []
            ),
        ]
        return sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


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


class Appended(BaseModel, frozen=True):
    """What one read of an appended file found: the records since the last read, and whether it read from the start.

    From the start where the file is new to this reader, was moved over, or
    was cut short: what was folded from before is then the old file's, and
    let go. The records are what the JSON reader made, each already an
    object, so they are carried as they are rather than checked field by
    field again: a cold read of a relay holds thousands.
    """

    records: SkipValidation[list[JsonObject]]
    began: bool


def named_question(record: JsonObject) -> str:
    """The question a record of a relay's log is about -- parked, moved, or replied on -- or "" where none."""
    match record:
        case {"parked": {"id": str() as question}}:
            return question
        case {"transition": {"id": str() as question}}:
            return question
        case {"question": str() as question, "reply": dict()}:
            return question
    return ""


class AppendedRecords:
    """One JSON-lines file written only at its end, read from where the last read stopped.

    A reader that stays open -- the dashboard, a waiter -- pays for what was
    appended since it last looked, never the whole file again. A file moved
    over the path, or cut short, is read again from its start, and the read
    says so, so what was folded from the old one is let go. A line with no
    terminator yet is left for the next read; one that is not a JSON object
    is inert evidence, as it is to every reader of these files.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.inode = -1
        self.offset = 0

    def read(self) -> Appended:
        """The records appended since the last read, and whether this read began at the file's start."""
        try:
            stream = self.path.open("rb")
        except FileNotFoundError:
            self.inode, self.offset = -1, 0
            return Appended(records=[], began=True)
        with stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            status = os.fstat(stream.fileno())
            if status.st_ino != self.inode or status.st_size < self.offset:
                self.inode, self.offset = status.st_ino, 0
            began = self.offset == 0
            stream.seek(self.offset)
            appended = stream.read()
        complete = appended.rfind(b"\n") + 1
        self.offset += complete

        def records() -> Iterator[JsonObject]:
            for line in appended[:complete].splitlines():
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(record, dict):
                    yield record

        return Appended(records=list(records()), began=began)


def swept_blobs(
    blobs: Path, named: Collection[str], grace: float = 3600.0
) -> list[str]:
    """Remove every document the store keeps that no record names, and say which.

    A document is written beside its name and moved into place, so one found
    still beside its name an hour on was left by a writer that stopped
    midway, and goes too.
    """
    if not blobs.is_dir():
        return []

    def unnamed(kept: Path) -> bool:
        if not kept.name.startswith("."):
            return kept.name not in named
        try:
            return time.time() - kept.stat().st_mtime > grace
        except FileNotFoundError:
            return False

    stranded = [kept for kept in blobs.iterdir() if unnamed(kept)]
    for kept in stranded:
        kept.unlink(missing_ok=True)
    return [kept.name for kept in stranded]


class QuestionFold(BaseModel):
    """What one relay's log has folded to so far.

    Each question as its records leave it, what was made of each until a
    record moves it again -- ``None`` where it does not read as a question --
    and the replies on each, in the order they were written.
    """

    entries: dict[str, JsonObject] = {}
    validated: dict[str, RecordedQuestion | None] = {}
    replied: dict[str, list[RecordedReply]] = {}


class AnswerFold(BaseModel):
    """What the host's file of answers to one relay has folded to so far.

    The first answer recorded for each review -- ``None`` where that record
    does not read as one, which no later record then replaces -- and every
    remark on each, oldest first.
    """

    answered: dict[str, RecordedAnswer | None] = {}
    remarked: dict[str, list[RecordedRemark]] = {}


class QuestionRelay:
    """The durable store every final ask is written to before anybody sees it.

    Appended rather than rewritten in place, because the failure this has to
    survive is a crash between recording a question and answering it — and a
    store that rewrites a file in place has a window where the question is
    neither the old one nor the new one. A question is recorded once, as it
    was parked, and each state it moves to after is a transition naming it;
    reading folds them together, and only what was appended since the last
    read is read again. The two rewrites the log ever sees -- an older log
    brought into this shape, settled questions moved out
    (:meth:`retire`) -- move a whole new file over the old.

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
        self.reading = threading.RLock()
        self.holder = 0
        self.log = AppendedRecords(path)
        self.answer_log = AppendedRecords(self.answers)
        self.fold = QuestionFold()
        self.heard = AnswerFold()

    @property
    def blobs(self) -> Path:
        """Where this relay keeps each document its records name, once."""
        return relay_blobs(self.path)

    @property
    def store(self) -> Path:
        """The directory beside the log holding what the relay keeps apart from it."""
        return self.blobs.parent

    def signature(self) -> RelaySignature:
        """What this relay's queue is on disk now, to read it again only once it changes."""
        return RelaySignature(
            questions=FileSignature.of(self.path),
            answers=FileSignature.of(self.answers),
        )

    def refreshed(self) -> None:
        """Fold what was appended to the log and to the host's answers since the last read.

        An older log is rewritten into this shape the first time it is read,
        which takes the relay's lock: inside a transaction, which holds it
        already, a log moved over by one in the older shape is refused
        rather than waited on.
        """
        appended = self.log.read()
        records, began = appended.records, appended.began
        if began and records and records[0] != relay_header():
            if self.holder:
                raise ValueError(
                    f"{self.path} was replaced by a log in an older shape while a "
                    "transaction held it"
                )
            migrate_relay(self.path)
            appended = self.log.read()
            records, began = appended.records, appended.began
        if began:
            self.fold = QuestionFold()
        fold_relay(self.fold.entries, records)
        for moved in [
            question for record in records if (question := named_question(record))
        ]:
            self.fold.validated.pop(moved, None)
        for record in records:
            match record:
                case {"question": str() as question, "reply": dict()}:
                    try:
                        reply = RecordedReply.model_validate(record)
                    except ValidationError:
                        continue
                    self.fold.replied.setdefault(question, []).append(reply)
        heard = self.answer_log.read()
        if heard.began:
            self.heard = AnswerFold()
        for record in heard.records:
            match record:
                case {
                    "question": str() as question,
                    "fingerprint": str(),
                    "answer": {
                        "approved": bool(),
                        "principal": str(),
                        "receipt": str(),
                    },
                } if question not in self.heard.answered:
                    try:
                        answer = RecordedAnswer.model_validate(record)
                    except ValidationError:
                        answer = None
                    self.heard.answered[question] = answer
                case {"question": str() as question, "remark": dict()}:
                    try:
                        remark = RecordedRemark.model_validate(record)
                    except ValidationError:
                        continue
                    self.heard.remarked.setdefault(question, []).append(remark)

    def folded(self, question: str) -> RecordedQuestion | None:
        """One question as the log folds it, before the host's answer is laid over it.

        Made once until a record moves it again.
        """
        if question not in self.fold.entries:
            return None
        if question not in self.fold.validated:
            try:
                made = RecordedQuestion.model_validate(self.fold.entries[question])
            except ValidationError:
                made = None
            self.fold.validated[question] = made
        return self.fold.validated[question]

    def settled(self, question: str) -> RecordedQuestion | None:
        """One question as it stands: folded, with the host's answer where one was recorded."""
        entry = self.folded(question)
        if entry is None:
            return None
        return entry.settled_by(
            self.heard.answered[question] if question in self.heard.answered else None
        )

    def record(self, question: PersistentQuestion) -> PersistentQuestion:
        """Park one question: its documents kept in the store, its record naming them."""
        park_relay_entry(self.path, question.model_dump(mode="json"))
        return question

    def reply(self, recorded: RecordedReply) -> RecordedReply:
        """Record what the requester said back on one of its reviews, beside the reviews it parked."""
        append_relay_records(self.path, lambda: [recorded.model_dump(mode="json")])
        return recorded

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialize a state transition across relay instances and processes.

        A separate lock leaves the log's own read and append locks available.
        Each call opens its own descriptor, so threads also contend for it.
        An older log is rewritten before it is taken, since rewriting takes it.
        """
        if not relay_current(self.path):
            migrate_relay(self.path)
        with exclusive(relay_lock(self.path)):
            self.holder = threading.get_ident()
            try:
                yield
            finally:
                self.holder = 0

    def questions(self) -> list[RecordedQuestion]:
        """Every question, folded forward to its latest state, with the host's answer.

        Malformed or unterminated lines remain inert evidence. Appenders
        preserve those bytes and frame later records separately, so a torn
        write neither loses the queue nor becomes authority on a later read.
        A record in this relay claiming an answer is skipped: the answer is
        the host's, and :meth:`recorded` is where it is read.
        """
        with self.reading:
            self.refreshed()
            return [
                entry
                for question in list(self.fold.entries)
                if (entry := self.settled(question)) is not None
            ]

    def recorded(self) -> dict[str, RecordedAnswer]:
        """The operator's answer to each of this relay's reviews, by review id."""
        with self.reading:
            self.refreshed()
            return {
                question: answer
                for question, answer in self.heard.answered.items()
                if answer is not None
            }

    def remarks(self) -> dict[str, list[RecordedRemark]]:
        """Every remark the operator made on this relay's reviews, by review id, oldest first."""
        with self.reading:
            self.refreshed()
            return {
                question: list(said) for question, said in self.heard.remarked.items()
            }

    def replies(self) -> dict[str, list[Reply]]:
        """Every reply a requester wrote on this relay's reviews, by review id, oldest first."""
        with self.reading:
            self.refreshed()
            return {
                question: [each.reply for each in said]
                for question, said in self.fold.replied.items()
            }

    def find(self, question: str) -> RecordedQuestion | None:
        """One question as it stands, reading only what was appended since the last read."""
        with self.reading:
            self.refreshed()
            return self.settled(question)

    def resolve(self, entry: RecordedQuestion) -> PersistentQuestion:
        """One question with every document it names read back whole from the store.

        Raises ValueError where one is missing or no longer hashes to its
        name -- a question retired to the archive keeps none -- since what
        cannot be read back cannot be shown, or checked, for what it binds.
        """
        return PersistentQuestion.model_validate(
            resolved_entry(entry.model_dump(mode="json"), self.blobs)
        )

    def question(self, question: str) -> PersistentQuestion | None:
        """One question as it stands with its documents whole, or ``None`` where none is recorded."""
        entry = self.find(question)
        return self.resolve(entry) if entry is not None else None

    def pending(self, principal: str = "") -> list[RecordedQuestion]:
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

    def transitioned(
        self, steps: dict[str, JsonObject], at: datetime | None = None
    ) -> list[RecordedQuestion]:
        """Record each question's move to another state in one append, and return each as it now stands."""
        when = (at or datetime.now(UTC)).isoformat()
        append_relay_records(
            self.path,
            lambda: [
                {"transition": {"id": question, "at": when, **fields}}
                for question, fields in steps.items()
            ],
        )
        return [
            entry for question in steps if (entry := self.find(question)) is not None
        ]

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
        longer shows what its fingerprint covers, the documents it names read
        back included. ``comments`` ride on the answer beside the note, and
        settle nothing of their own.
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
                (expired,) = self.transitioned({question: {"state": "expired"}})
                return self.resolve(expired)
            if not entry.answerable_by(principal):
                raise ValueError(
                    f"{principal!r} may not answer {question!r}"
                    f" — eligible: {', '.join(entry.eligible) or 'nobody'}"
                )
            if unverifiable := entry.unverifiable():
                raise ValueError(f"review {question!r}: {unverifiable}")
            try:
                shown = self.resolve(entry)
            except ValueError as unread:
                raise ValueError(
                    f"review {question!r} cannot be read back whole, so nothing "
                    f"may answer it: {unread}"
                ) from unread
            if not shown.bound():
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
            return shown.settled_by(given)

    def cancel(self, question: str, reason: str = "") -> RecordedQuestion:
        """Withdraw a question nobody needs answered any more."""
        with self.transaction():
            if self.find(question) is None:
                raise ValueError(f"no question {question!r} is recorded")
            (cancelled,) = self.transitioned(
                {question: {"state": "cancelled", "outcome": reason}}
            )
            return cancelled

    def expire(self, questions: list[str], reason: str) -> list[RecordedQuestion]:
        """Expire each of these questions still waiting, saying why.

        For questions nobody can use any more — their requester is gone, so
        no retry will ever read the answer — which is not the same as ones
        their requester withdrew. Settled in one transaction over one read, so
        an answer recorded a moment before is never overwritten by the sweep,
        and one append records every one of them.
        """
        expired = datetime.now(UTC)
        with self.transaction():
            waiting = [
                question
                for question in dict.fromkeys(questions)
                if (entry := self.find(question)) is not None
                and entry.state == "pending"
            ]
            if not waiting:
                return []
            return self.transitioned(
                {
                    question: {
                        "state": "expired",
                        "outcome": reason,
                        "expires": expired.isoformat(),
                    }
                    for question in waiting
                },
                expired,
            )

    def retire_stale(
        self, question: str, moved: Sequence[Path], by: str
    ) -> RecordedQuestion:
        """Retire one waiting question whose recorded files moved since it was parked.

        No approval could release it any more: an approval binds to the files
        as the question recorded them, and a retry of the call records them as
        they stand now, which is a fresh question. So it leaves the queue as
        ``stale`` rather than waiting on an answer that could carry out
        nothing, naming what moved and *by* whom it was noticed -- the
        requester's waiter, or the dashboard. One approved and not yet carried
        out is retired too, since its approval binds the files as recorded;
        one declined, carried out or retired already is left as it is.
        """
        with self.transaction():
            entry = self.find(question)
            if entry is None:
                raise ValueError(f"no question {question!r} is recorded")
            if entry.state not in ("pending", "approved"):
                return entry
            named = ", ".join(str(path) for path in moved)
            now = datetime.now(UTC)
            (retired,) = self.transitioned(
                {
                    question: {
                        "state": "stale",
                        "moved": [str(path) for path in moved],
                        "outcome": f"{named} changed since it was recorded; noticed by {by}",
                        "completed": now.isoformat(),
                    }
                },
                now,
            )
            return retired

    def advance(
        self, question: str, state: QuestionState, outcome: str = ""
    ) -> RecordedQuestion:
        """Move one question along its lifecycle after it was answered.

        The dispatch states live here rather than beside the executor because
        the record is what makes at-most-once dispatch checkable: a
        coordinator that crashed after sending finds ``dispatched`` written
        down, and a coordinator that finds it does not send again.
        """
        with self.transaction():
            if self.find(question) is None:
                raise ValueError(f"no question {question!r} is recorded")
            now = datetime.now(UTC)
            finished: JsonObject = (
                {"completed": now.isoformat()}
                if state in ("completed", "failed", "in_doubt")
                else {}
            )
            (advanced,) = self.transitioned(
                {question: {"state": state, "outcome": outcome, **finished}}, now
            )
            return advanced

    def dispatchable(self, question: str) -> RecordedQuestion:
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

    def retire(
        self, questions: Collection[str], kept: Callable[[], None] = lambda: None
    ) -> list[str]:
        """Move these questions out of the log, and every document none of the rest names out of the store.

        Whoever retires a question keeps what it wants of it first -- the
        archive of settled reviews keeps each one's summary -- and *kept*
        runs under the relay's lock before anything is removed, so no
        transition lands between the two. The log is rewritten whole, the
        new file moved over the old, and the store swept under the log's own
        lock, which every writer of a document holds while it writes one.
        Returns the documents removed.
        """
        named = dict.fromkeys(questions)
        if not named:
            return []
        with self.transaction():
            kept()
            with opened_relay(self.path) as stream:
                records = stream_records(stream)
                if not records or records[0] != relay_header():
                    return []

                def documents(record: JsonObject) -> list[str]:
                    match record:
                        case {"parked": dict() as parked}:
                            return named_blobs(parked)
                    return []

                remaining = [
                    record
                    for record in records[1:]
                    if named_question(record) not in named
                ]
                rewrite_relay(self.path, [relay_header(), *remaining])
                return swept_blobs(
                    self.blobs,
                    dict.fromkeys(
                        digest for record in remaining for digest in documents(record)
                    ),
                )
