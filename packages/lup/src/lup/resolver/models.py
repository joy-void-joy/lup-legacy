"""Immutable, schema-versioned semantic resolver records."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from lup.coordination.questions import Question, QuestionAnswer
from lup.coordination.refs import ActorRef
from lup.harness.codescan.symbols import DefinedSymbol
from lup.harness.models import ResolveSpec
from lup.policy.hooks import LupHooksConfig
from lup.policy.grants import LeaseGrants
from lup.policy.identity import ConcernAllowance
from lup.policy.kernel.diagnostic import devtools, spelled

type ActorKind = Literal["worker", "reviewer", "merger", "planner", "run"]
"""The roles this run addresses, which the resolver validates at its own edge.

The neutral :class:`~lup.coordination.refs.ActorRef` leaves ``kind`` open, because a
closed set underneath every consumer would be every consumer's set at once.
The closed set that matters *here* is this one, checked where a run builds a
ref rather than carried in the type it shares with everyone else.
"""


def actor(kind: ActorKind, id: str, round: int = 1) -> ActorRef:
    """Build a ref for one of this run's own roles.

    The one construction site that names a kind, so the resolver's closed set
    is enforced by the type checker at every call without the shared record
    having to know what a resolver role is.
    """
    return ActorRef(kind=kind, id=id, round=round)


class ResolvePhase(StrEnum):
    """Persisted resolver phase names in their only valid forward order."""

    INVENTORY = "inventory"
    QUESTIONS = "questions"
    ELIGIBILITY = "eligibility"
    DAG = "dag"
    LEASES = "leases"
    WORKERS = "workers"
    DEPENDENCY_BASES = "dependency_bases"
    REVIEW = "review"
    INTEGRATION = "integration"
    VERIFICATION = "verification"
    CLEANUP = "cleanup"
    COMPLETE = "complete"
    ABORTED = "aborted"
    FAILED = "failed"

    def terminal(self) -> bool:
        """Whether a run in this phase holds nothing and reads nothing."""
        return self in {
            ResolvePhase.COMPLETE,
            ResolvePhase.ABORTED,
            ResolvePhase.FAILED,
        }

    def released_leases(self) -> bool:
        """Whether a run in this phase has let go of the branches it leased.

        Only a completed run has: it carried every lease through the join
        machinery, so no worker is coming back for those branches. That is
        not the same as the batch having landed — a run reaches this phase
        by finishing its own work, and its integration branch may still be
        sitting on disk unmerged. A branch that outlives its lease is the
        run's leftover, and is still the run's to answer for rather than
        work the sweep found abandoned. A failed or aborted run has its
        branches out on
        lease with nothing answerable for them, which is precisely when a
        survey must leave them alone — the two verbs it would otherwise
        offer both destroy work no one has salvaged yet.
        """
        return self is ResolvePhase.COMPLETE

    def settling(self) -> bool:
        """Whether concerns reach their terminal state during this phase.

        Which is what makes a settled count worth drawing a bar from. Outside
        these, the count moves backwards: integration takes a verified concern
        back out of the settled set and returns it, and a bar that retreats is
        worse than no bar at all.

        Asked by the iterator a status view draws and by the console a live
        run prints to, so it is a question the phase answers rather than a
        table each of them matches against.
        """
        return self in {
            ResolvePhase.WORKERS,
            ResolvePhase.REVIEW,
        }


class ConcernStatus(StrEnum):
    """Persisted lifecycle of one independently scheduled concern."""

    DISCOVERED = "discovered"
    WAITING_FOR_ANSWERS = "waiting_for_answers"
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    LEASED = "leased"
    RUNNING = "running"
    VALIDATING = "validating"
    REVIEWING = "reviewing"
    REVISING = "revising"
    VERIFIED = "verified"
    INTEGRATING = "integrating"
    INTEGRATED = "integrated"
    CLEANED = "cleaned"
    RETAINED = "retained"
    RETIRED = "retired"
    FAILED = "failed"


SETTLED_STATUSES: tuple[ConcernStatus, ...] = (
    ConcernStatus.VERIFIED,
    ConcernStatus.INELIGIBLE,
    ConcernStatus.RETIRED,
    ConcernStatus.FAILED,
    ConcernStatus.INTEGRATED,
    ConcernStatus.CLEANED,
    ConcernStatus.RETAINED,
)
"""Which statuses mean a concern is done being decided, however it ended.

Ours to draw rather than the lifecycle's own, so a caller redraws it: a
reader could reasonably count `integrating` as finished, and this does not.
Naming the settled half rather than the working one makes an unlisted status
read as still in flight, which is the safer way to be wrong — a new state
counted as finished would inflate the fraction silently.

Beside the lifecycle it partitions rather than beside its first reader,
because the aggregate a watcher reads and the supervisor's own header both
count against it, and the two must not drift into separate answers to "how
far along is this run".

Which is also why :meth:`ResolveState.tally` is the one place that takes it
as an argument, rather than each reader taking its own: both of those
readers get their figure from that fold, so a line redrawn there is a line
redrawn for both, and there is no second copy to disagree with. The stamp
:meth:`ResolveRun.progress_state` writes keeps this table because it records
that a concern landed rather than interpreting how far along the run is —
a ratchet a reader's preference has no business writing into.
"""


class ConcernOrigin(StrEnum):
    """How one concern entered the run that owns it."""

    INVENTORY = "inventory"
    ADMITTED = "admitted"


class SourceSnapshot(BaseModel, frozen=True):
    branch: str
    commit: str


class BaseRefresh(BaseModel, frozen=True):
    """What bringing a run's base up to its branch would do, or did.

    Reported rather than performed silently, because a lease cut from a
    commit is a lease whose worker reasons about that commit's code: a run
    sealed against its own repository does not merely go stale, it argues
    confidently for reverting decisions it cannot see. Conflicts are named
    per path so the answer to "what would this cost" is available before
    anything moves.
    """

    branch: str = ""
    """Which branch the base was brought up to, empty when it was another
    base rather than a branch — a lease combining what it inherited."""
    was: str
    commit: str
    conflicts: list[Path] = []
    reason: str = ""

    def moved(self) -> bool:
        return self.commit != self.was


class LeaseRefresh(BaseModel, frozen=True):
    """What bringing one lease up to a refreshed base would do, or did."""

    concern_id: str
    conflicts: list[Path] = []
    uncommitted: list[Path] = []
    """Paths held outside any commit, which is a different stop from a conflict:
    the merge is clean and the tree is not ready to take it."""
    applied: bool = False
    reason: str = ""


class RefreshReport(BaseModel, frozen=True):
    """A refresh as it stands: what the base would become, lease by lease.

    Answering before acting is the whole point, because the concerns most
    likely to conflict with an upstream fix are the ones editing the files
    it touched — and those are branches with work in flight.
    """

    base: BaseRefresh
    leases: list[LeaseRefresh] = []
    applied: bool = False


class ReviewNote(BaseModel, frozen=True):
    file: Path
    line: int = Field(ge=1)
    text: str


class NoteClearance(BaseModel, frozen=True):
    """What one lease's pre-worker note clearance removed and could not find."""

    concern_id: str
    cleared: list[ReviewNote] = []
    missing: list[ReviewNote] = []


class NoteClearanceCommit(BaseModel, frozen=True):
    """A clearance and the commit the worker should treat as its base."""

    clearance: NoteClearance
    commit: str


class InventoryNote(ReviewNote, frozen=True):
    """One review note together with the source context used for planning."""

    context: str


class IssueEvidence(BaseModel, frozen=True):
    """One tracker issue offered to a run as evidence.

    Forge-neutral by construction: a number, where to read it, and what it
    says. Fetching belongs to whatever tooling knows the forge, which keeps
    this library free of one — a project on a different tracker supplies its
    own fetcher rather than waiting for the library to learn its API.

    An issue is evidence, not a unit of work. It is clustered into concerns
    exactly as a note is, because one issue routinely raises several pieces
    of work and several issues routinely describe one.
    """

    number: int = Field(ge=1)
    url: str
    title: str
    body: str = ""

    def reference(self) -> str:
        return f"#{self.number}"


class AcceptanceCriterion(BaseModel, frozen=True):
    id: str
    description: str


class MaterialQuestion(Question, frozen=True):
    """A resolver question: the mailbox's shape, plus what the run consults.

    The id, the prompt, the answer domain and the two validators over them
    are what any door reads, and live on :class:`~lup.coordination.questions.Question`.
    Added here is only what the resolver itself consults — which concern the
    question belongs to, which edit gates an option would need, and which
    lost criteria a re-check is about.
    """

    concern_id: str
    allowances: list[ConcernAllowance] = Field(
        default=[],
        description=(
            "Every edit gate some choice here would need. An option the "
            "concern has no grant for is an option whose worker is denied, "
            "so naming the gate here is what makes the concern carry it."
        ),
    )
    criteria: list[str] = Field(
        default=[],
        description=(
            "The lost criterion ids a re-check question is about, carried as "
            "data so an identical standing finding is recognized across "
            "occasions instead of re-asked per join."
        ),
    )
    recheck_commit: str | None = None
    """The exact tree this criterion finding examined, absent for other questions."""


class RecheckRuling(BaseModel, frozen=True):
    """One answered re-check, read where the decision it governs is taken.

    The question is closed over two words that mean opposite things about
    the review branch, so the answer is only worth asking for if something
    consults it. This is what integration consults.
    """

    concern_id: str
    criteria: list[str]
    ruling: str


class ClosedAnswer(StrEnum):
    """One gate's whole answer domain, published and read from one place.

    A question with a fixed set of answers has one fact to state, and with
    no one place to state it that fact gets stated four ways: two constant
    pairs, a third pair, and a list of literals whose reader compares the
    same literals by hand. Nothing ties the choices a question publishes to
    the token its reader tests — which is how the allowance gate offers its
    answers as suggestions while its only reader tests for a literal, so a
    human's prose grant promotes cleanly and then means refusal, with
    nothing anomalous to report anywhere.

    Deriving the offer from the domain closes that by construction: a reader
    can only test a member, and every member is offered. A gate whose answers
    are open — a name, a path, prose — is not one of these and should not be
    made into one.
    """

    @classmethod
    def choices(cls) -> list[str]:
        """Every answer this gate offers, in declaration order."""
        return [member.value for member in cls]


class ResidualRuling(ClosedAnswer):
    """Whether an acceptance survives a criterion the reviewer left unmet."""

    CARRY = "carry"
    SEND_BACK = "send back"


class AllowanceRuling(ClosedAnswer):
    """Whether a concern's request for authority it does not have is granted."""

    GRANT = "grant"
    REFUSE = "refuse"


class ConcernApproval(ClosedAnswer):
    """Whether a planned concern joins this run or waits for another."""

    APPROVE = "approve"
    DEFER = "defer"


class SupersessionRuling(ClosedAnswer):
    """Whether an earlier parent's unmet criterion was overtaken or broken."""

    SUPERSEDED = "superseded"
    REGRESSION = "regression"


def allowance_question_id(concern_id: str, allowance: ConcernAllowance) -> str:
    """The composed id a `request_allowance` question is recorded under.

    The worker's tool asks with the bare `allow-<gate>` id and the binding
    prefixes its concern, so this spelling is the contract between the ask
    and the run-side reader that turns a "grant" answer into authority.
    """
    return f"{concern_id}-allow-{allowance}"


def asks_for_an_allowance(concern_id: str, question_id: str) -> bool:
    """Whether this question is one of the concern's allowance gates.

    Both the declaration and the reader ask this, so they cannot disagree
    about which questions have a two-word domain. They did: the gate
    published its choices as suggestions while its only reader tested for
    the literal token, so a human's prose grant promoted cleanly and then
    meant refusal, with nothing anomalous to report anywhere.
    """
    return any(
        question_id == allowance_question_id(concern_id, allowance)
        for allowance in ConcernAllowance
    )


class QuestionBatch(BaseModel, frozen=True):
    run_id: str
    questions: list[MaterialQuestion]

    @model_validator(mode="after")
    def unique_questions(self) -> "QuestionBatch":
        identifiers = [question.id for question in self.questions]
        if len(identifiers) != len(dict.fromkeys(identifiers)):
            raise ValueError("question ids must be unique")
        return self


class AnswerBatch(BaseModel, frozen=True):
    run_id: str
    answers: list[QuestionAnswer]

    @model_validator(mode="after")
    def unique_answers(self) -> "AnswerBatch":
        identifiers = [answer.question_id for answer in self.answers]
        if len(identifiers) != len(dict.fromkeys(identifiers)):
            raise ValueError("answer question ids must be unique")
        return self


# lup: ignore[constant-declaration] — the reserved id a run's own vocabulary
# defines, which planning refuses and integration claims
INTEGRATION_CONCERN_ID = "integration"


class ConcernShape(BaseModel, frozen=True):
    """Planning fields shared by a planned concern and its materialization."""

    id: str = Field(min_length=1)
    title: str
    spec: str
    files: list[Path] = []
    criteria: list[AcceptanceCriterion] = Field(min_length=1)
    dependencies: list[str] = []
    questions: list[MaterialQuestion] = []
    allowances: list[ConcernAllowance] = []
    supersedes: str = Field(
        default="",
        description=(
            "The concern this one replaces, when a plan is corrected mid-run. "
            "The predecessor stays in the record — a run is evidence of what "
            "was tried, and editing it in place would erase the correction "
            "along with what prompted it."
        ),
    )

    @model_validator(mode="after")
    def references_are_local_and_unique(self) -> "ConcernShape":
        if Path(self.id).name != self.id or self.id in {"integration", "review"}:
            raise ValueError(f"concern id {self.id!r} is reserved or not path-safe")
        if any(question.concern_id != self.id for question in self.questions):
            raise ValueError(f"concern {self.id!r} contains a foreign question")
        for values, label in [
            (self.dependencies, "dependencies"),
            ([criterion.id for criterion in self.criteria], "criteria"),
            ([question.id for question in self.questions], "questions"),
        ]:
            if len(values) != len(dict.fromkeys(values)):
                raise ValueError(f"concern {self.id!r} has duplicate {label}")
        if self.id in self.dependencies:
            raise ValueError(f"concern {self.id!r} cannot depend on itself")
        return self

    @model_validator(mode="after")
    def offered_choices_carry_their_gates(self) -> "ConcernShape":
        """A question cannot offer what this concern was not granted.

        The answer to a material question becomes a worker's assignment, so
        an option needing a gate the concern lacks dispatches a worker that
        is denied on arrival. Prose inside the choice text disclosing that
        is not a gate — the human still picks it, and the run still spends a
        lease finding out. Requiring the concern to carry the grant makes
        the unapprovable option unplannable instead.
        """
        for question in self.questions:
            ungranted = [
                allowance
                for allowance in dict.fromkeys(question.allowances)
                if allowance not in self.allowances
            ]
            if ungranted:
                raise ValueError(
                    f"question {question.id!r} offers a choice needing "
                    f"{', '.join(sorted(ungranted))}, which concern "
                    f"{self.id!r} does not request"
                )
        return self


class Concern(ConcernShape, frozen=True):
    """One generalized concern and its complete dependency/acceptance inputs."""

    notes: list[ReviewNote] = []
    evidence: str = Field(
        default="",
        description=(
            "What a human said that no note in the tree carries. Recorded "
            "beside `notes` rather than in place of them, so a reviewer can "
            "tell a concern traceable to code from one traceable only to a "
            "statement someone made."
        ),
    )
    issues: list[IssueEvidence] = Field(
        default=[],
        description=(
            "The tracker issues this concern answers. Carried so a landing "
            "can say so where the issue is read, and so a reviewer can tell "
            "work the tracker asked for from work the tree did."
        ),
    )
    origin: ConcernOrigin = ConcernOrigin.INVENTORY
    eligible: bool = True
    integration_approved: bool = False

    @model_validator(mode="after")
    def admission_is_grounded(self) -> "Concern":
        """An admitted concern names the evidence that raised it mid-run."""
        grounded = self.notes or self.evidence or self.issues
        if self.origin is ConcernOrigin.ADMITTED and not grounded:
            raise ValueError(f"admitted concern {self.id!r} cites no evidence")
        return self


class PlannedConcern(ConcernShape, frozen=True):
    """One planned concern referencing its evidence by zero-based position.

    The planner never echoes evidence content — positional references make
    copy fidelity a mechanical property instead of a model obligation.
    References are shared rather than exclusive: a note raising several
    issues is referenced by each concern that answers one of them.
    """

    evidence_indexes: list[int] = Field(min_length=1)

    @model_validator(mode="after")
    def references_are_distinct(self) -> "PlannedConcern":
        if len(self.evidence_indexes) != len(dict.fromkeys(self.evidence_indexes)):
            raise ValueError("a concern may reference each piece of evidence once")
        return self


class ConcernEligibility(BaseModel, frozen=True):
    concern_id: str
    eligible: bool
    integration_approved: bool
    reason: str = ""


class ConcernProgress(BaseModel, frozen=True):
    """Atomic persisted status for one concern in the resolver DAG."""

    concern_id: str
    status: ConcernStatus = ConcernStatus.DISCOVERED
    reason: str = ""
    settled_at: datetime | None = None
    """When this concern first reached a settled status, if it has yet.

    The sample a worker-phase rate is taken from, the way
    ``JoinProgress.completions`` serves the join sequence: without one the
    phase knows how many concerns it faces and not when any of them landed,
    which is the gap that kept it from drawing a bar at all.

    Written once and never moved, so a concern that settles, re-opens into
    integration, and settles again keeps the moment it first landed. A
    missing stamp costs the rate one sample and never the count, which is
    read from the status — so an older run, or a path that forgets, degrades
    to the bar it already had rather than to a wrong one.
    """


class WritableRootLease(BaseModel, frozen=True):
    concern_id: str
    root: Path
    branch: str
    active: bool = True


class HeldLease(BaseModel, frozen=True):
    """One branch a run is answerable for — still leased, or left behind.

    What a branch survey needs in order to leave it alone: which run, and
    where that run had got to, so the reason it reports is checkable against
    the run directory rather than being a bare assertion that something is
    using this.
    """

    branch: str
    run_id: str
    standing: str
    alive: bool = True

    def reason(self) -> str:
        """Why a sweep leaves this branch alone, and what moves it if dead.

        A lease held by a run that will never move again is the silent
        bucket this disposition exists to prevent: it reports the same
        `KEEP` on every sweep, forever, with nothing in the workflow saying
        what to do about it. A live run needs no instruction — it is
        working — so only a dead one carries the two commands that end the
        holding, in the order worth trying them. A completed run is neither:
        nothing will resume it and nothing is coming back for the branch, so
        what it needs is the reading that says whether the work is already
        somewhere else, not an offer to restart a run that finished.
        """
        if self.alive:
            return f"lease of run {self.run_id} ({self.standing})"
        if self.standing == ResolvePhase.COMPLETE:
            status = devtools("resolve", "status", "--run-id", self.run_id)
            return (
                f"leftover of completed run {self.run_id}; check what of it "
                f"reached the integration branch with `{spelled(status)}` before "
                "clearing it"
            )
        resume = devtools("resolve", "--adapter", "<adapter>", "--run-id", self.run_id)
        abort = devtools("resolve", "--run-id", self.run_id, "--abort", "<reason>")
        return (
            f"lease of run {self.run_id} ({self.standing}); resume it with "
            f"`{spelled(resume)}`, or release every lease with `{spelled(abort)}`"
        )


class DependencyBase(BaseModel, frozen=True):
    concern_id: str
    parent_concerns: list[str]
    parent_commits: list[str]
    commit: str
    semantic_join: bool = False


class WorkAssignment(BaseModel, frozen=True):
    run_id: str
    concern: Concern
    lease: WritableRootLease
    dependency_base: DependencyBase
    rendered_skill_invocation: str
    answers: list[QuestionAnswer] = []


class WorkerContext(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """What one worker session needs to know about its own assignment.

    The concern id is supplied rather than derived from the lease directory
    name: that derivation holds only incidentally for concern worktrees and
    is wrong for the integration lease. The question tools bind whatever id
    they are given as the identity a worker cannot post outside of.
    """

    root: Path
    concern_id: str
    actor: ActorRef
    """Whose session this is, which is not derivable from the concern: one
    recipe opens both a concern's worker and the merger that joins into it,
    and mail addressed to either must reach that one and not the other."""
    grants: LeaseGrants = Field(default_factory=LeaseGrants)
    """Where this lease's edit gates are read from, asked afresh at every
    judgment rather than resolved once here: a gate a human grants while this
    session runs has no other way to reach it, and one they take back has no
    other way to stop applying. The document it names is the same one the
    lease's own deployed dispatcher reads."""
    hooks: LupHooksConfig = Field(default_factory=LupHooksConfig)
    """The mid-turn delivery this session must open with, handed over rather
    than fetched. Delivery works only if the hook is in the options the
    session is built from, so a recipe that had to remember to go and get it
    could be written once without it — producing a worker that looks addressed
    and reads nothing anyone sends it."""


class ReviewerContext(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """What one reading session needs: the tree it judges, and its own mail.

    A reviewer once took only a path, which is why it was the one actor kind
    nobody could say anything to. Judging is where a fact arriving late is
    most worth having — a criterion already checked elsewhere, a base that
    moved under the tree — so it carries the same delivery every other actor
    does.
    """

    root: Path
    hooks: LupHooksConfig = Field(default_factory=LupHooksConfig)


class WorkerReport(BaseModel, frozen=True, extra="forbid"):
    """One worker's account of its turn.

    Extra fields are forbidden rather than ignored: a model still emitting
    the retired ``questions`` field would otherwise have it silently dropped
    and the question simply lost. Forbidding makes that a loud correction
    the reprompt wrapper can fix.
    """

    concern_id: str
    changed: bool
    summary: str
    files_changed: list[Path] = []
    swept_beyond_scope: list[Path] = []
    merge_notes: list[str] = Field(
        default=[],
        description=(
            "What anyone joining this work needs to know that the diff does "
            "not say — a changed signature whose callers live elsewhere, an "
            "invariant now enforced in one place. This is not a message to a "
            "sibling worker, which could not act on it anyway since the "
            "changed code is not in its worktree; it reaches whoever merges."
        ),
    )


class DiffValidation(BaseModel, frozen=True):
    concern_id: str
    valid: bool
    commit: str | None = None
    reason: str = ""
    declaration: bool = False
    """Whether the only thing wrong was the file-declaration contract.

    Bookkeeping, and mechanically checkable — which is why a caller spends a
    different allowance on it than on the reviewer's judgement of the work.
    """


class ReviewReport(BaseModel, frozen=True):
    concern_id: str
    accepted: bool
    generalized: bool
    reason: str
    residual: list[str] = []
    criteria_met: list[str] = []


class DropCandidate(BaseModel, frozen=True):
    """Content one parent contributed that the joined tree does not hold.

    A candidate is an obligation, never a verdict. A legitimate resolution
    rewrites what it merges, so a line going missing is exactly as likely to
    be correct as to be a loss — which is why the merger has to say which,
    and why nothing here decides on its own.
    """

    parent: str
    path: Path
    missing: list[str]
    lost_symbols: list[DefinedSymbol] = []
    """Definitions the parent introduced that the joined tree no longer holds.

    A separate finding from the missing lines, and a sharper one. Lines go
    missing whenever a resolution rewrites them, so the list is long and
    mostly benign; a function that was defined and now is not is the shape a
    silent regression actually takes.
    """


type HunkFate = Literal["kept", "rewritten", "superseded", "dropped"]


class HunkDisposition(BaseModel, frozen=True):
    """What became of one candidate hunk, and why.

    Containment rather than equality is the gate: a legitimate resolution
    rewrites hunks, so requiring the result to hold exactly the candidates
    would reject the correct answer. What must not happen is a candidate
    disappearing with nothing said about it.
    """

    path: Path
    parent: str
    fate: HunkFate
    rationale: str


class DeclaredEdit(BaseModel, frozen=True):
    """One edit made outside the conflict set, and the reason for it.

    A hard subset rule — changed files within conflicted files — is wrong,
    because the canonical joint failure is fixed in a file that never
    conflicted: one branch changes a signature, another adds a caller, and
    the caller's file merges clean and still needs updating. So edits
    outside the conflict set are permitted and undeclared ones are the
    rejection.
    """

    path: Path
    rationale: str


class MergeReport(BaseModel, frozen=True, extra="forbid"):
    """What one join did, declared in a form the orchestrator can check.

    Strict for the same reason ``WorkerReport`` is: a retired field must
    fail loudly rather than vanish, and the whole point of this report is
    that a semantic choice cannot go unrecorded.
    """

    completed: bool
    summary: str
    unresolved_paths: list[Path] = []
    dispositions: list[HunkDisposition] = []
    out_of_conflict_edits: list[DeclaredEdit] = []
    blocked: str = Field(
        default="",
        description=(
            "Why a resolution that is complete in the working tree could not "
            "be staged. An incompletion with a cause attached is answerable; "
            "one without a cause was read as an unexplained failure."
        ),
    )


class AgentRound(BaseModel, frozen=True):
    concern_id: str
    round: int = Field(ge=1)
    worker: WorkerReport
    diff: DiffValidation
    review: ReviewReport


class ConcernOutcome(BaseModel, frozen=True):
    concern_id: str
    branch: str
    commit: str | None = None
    head: str | None = None
    """Where the lease's branch actually ended, accepted or not.

    Distinct from ``commit``, which is the commit an accepted round
    produced. A concern that exhausts its rounds has no accepted commit and
    still has a branch, because it can only exhaust them by committing work
    across several — so reading ``commit=None`` as "no commit exists" is
    what made a restore expect the base and refuse the tree.
    """
    verified: bool = False
    integrated: bool = False
    regressed: list[str] = Field(
        default=[],
        description=(
            "Criteria a human ruled the merged tree broke. Verification is "
            "about this concern's own lease; this is about the tree its "
            "siblings built, and only the second can disqualify a branch "
            "that already passed the first."
        ),
    )
    rounds: list[AgentRound] = []
    failure: str | None = None
    notes_cleared: list[ReviewNote] = []
    notes_missing: list[ReviewNote] = []

    def settled_summary(self) -> str:
        """How this concern ended, in the one line a roster entry carries.

        The rounds are named because they are what a reader sizing the next
        pass wants and cannot get from the verdict: two concerns both
        verified are not the same result when one took four rounds.
        """
        if not self.verified:
            return ""
        spent = f" in {len(self.rounds)} round{'' if len(self.rounds) == 1 else 's'}"
        return f"verified{spent}"


class ConcernExecution(BaseModel, frozen=True):
    base: DependencyBase
    outcome: ConcernOutcome


class IntegrationRecord(BaseModel, frozen=True):
    branch: str
    worktree: Path
    concerns: list[str]
    commit: str | None = None
    completed: bool = False


class CarriedParent(BaseModel, frozen=True):
    """One parent whose commits another parent already contains."""

    commit: str
    inside: str


class JoinProgress(BaseModel, frozen=True):
    """How far integration has got, recorded as each parent lands.

    :class:`IntegrationRecord` is written once every join is done, so until
    then there was nowhere to say "six of twelve" — and writing a partial one
    would make ``integrate`` skip the join block and verify a half-merged
    tree. A resume therefore fell back to the run's source commit and hard
    reset the worktree, discarding every join already built and re-deriving
    the same questions under fresh ids, which a human then answered twice.

    Recording it separately keeps the two facts apart: this says where the
    join sequence got to, the record says the sequence finished.
    """

    joined: list[str]
    commit: str
    completions: list[datetime] = []
    """When each join this run actually performed landed.

    Not parallel to ``joined``: a parent already contained in the tree is
    appended there without a join happening, so pairing them would time
    merges that never ran. These are the samples a rate is taken from, kept
    beside the progress rather than scanned out of the journal, which
    reaches tens of megabytes and is read by a status view that runs often.
    """
    planned: list[str] = []
    """Every parent this join set out to merge, by commit.

    Recorded by the joiner rather than counted from the outcomes, because
    only the joiner knows which parents another parent already contains and
    so are never merged on their own. Derived downstream, the total counted
    every concern holding a commit — over-reading by each one that failed or
    retired still holding work, and again by each that rides inside a
    sibling, so a bar drawn from it could not reach its own end.

    The identities rather than their number, so every figure a reader is
    shown comes off one set: the total is how many there are, and the count
    is how many of them ``joined`` names. Two records each keeping their own
    tally of the same sequence is what let a status line say six of five
    while the log said six of nine.
    """

    def landed(self) -> int:
        """How many planned parents are in the tree.

        The planned set's own members rather than every landing, because a
        parent already contained in the tree is swept and recorded without
        having been planned on its own. That is real work and not progress
        through this plan, and counted into the numerator it takes the
        fraction past its denominator.
        """
        return len({*self.joined} & {*self.planned})


class VerificationRecord(BaseModel, frozen=True):
    name: str
    arguments: list[str]
    passed: bool
    exit_code: int

    output: str = ""
    """What the check said, kept because a verdict is read long after it ran.

    A rejection recording only the gate's own name makes learning which
    row of an eleven-row check failed mean reproducing the whole check
    inside the lease worktree — which a later session often cannot do,
    because the run is still holding it. Three concerns in one run were
    rejected on the same string for the same pre-existing finding, and each
    worker re-derived it from scratch; one then exhausted its revision
    budget with its acceptance criteria never evaluated.
    """


class ConcernRetirement(BaseModel, frozen=True):
    """One human's decision that a concern is settled somewhere else.

    A run parked while its branch moved forward will routinely find that
    the branch already did some of its work, and base refresh makes that
    the expected consequence of following a branch rather than a rare
    accident. Every route available without this is wrong: hand-resolving
    an add/add conflict between two independent implementations of one
    thing, letting a worker open on a concern whose notes no longer exist,
    or aborting the whole run to retire one concern.

    The reason is required because retiring is a claim about somewhere
    else — the commit, branch or issue that settled it — and a record
    saying only that a concern stopped is one nobody can check.
    """

    concern_id: str
    reason: str = Field(min_length=1)


class VerificationAcceptance(BaseModel, frozen=True):
    """One human's decision to accept a concern over a failing verification.

    A verification verdict is an exit code, and some failures are true and
    unfixable from inside the lease that meets them: a finding the worker
    did not introduce, reproduces at its base, and cannot converge on. The
    worker then resubmits into a rejection that spends a revision round each
    time, until the concern fails with its criteria never evaluated.

    Accepting is therefore a decision, not a repair, and it is recorded like
    one. The reason is required because the record is what review reads
    instead of a green check that was never green.
    """

    concern_id: str
    verification: str
    """The verification this accepts, by the name it fails under."""
    reason: str


class WorktreeRemoval(BaseModel, frozen=True):
    """Whether a lease's worktree is gone, and what stands in the way if not."""

    freed: bool
    detail: str = ""


class CleanupRecord(BaseModel, frozen=True):
    path: Path
    branch: str
    action: Literal["removed", "retained"]
    reason: str


type InventoryPlanner = Callable[["ResolveRequest"], Awaitable["ResolveInventory"]]
"""How a source carrying raw evidence has it organized into concerns."""


class ResolverSource(BaseModel, ABC, frozen=True):
    """What a resolver run starts from, able to yield the inventory it runs.

    A run begins either from concerns already organized or from the review
    evidence that has yet to be organized into them. Asking the source for its
    inventory keeps the entry point free of both spellings, so a further kind
    of starting point is one class rather than an edit to the entry point.
    """

    @abstractmethod
    async def inventory(self, planner: InventoryPlanner) -> "ResolveInventory":
        """The concerns this run executes, planned first if they are not yet."""


class ResolveInventory(ResolverSource, frozen=True):
    source: SourceSnapshot
    concerns: list[Concern]

    async def inventory(self, planner: InventoryPlanner) -> "ResolveInventory":
        return self

    @model_validator(mode="after")
    def unique_concerns(self) -> "ResolveInventory":
        identifiers = [concern.id for concern in self.concerns]
        if len(identifiers) != len(dict.fromkeys(identifiers)):
            raise ValueError("concern ids must be unique")
        return self


class ResolveRequest(ResolverSource, frozen=True):
    """Unorganized review evidence supplied to the shared inventory phase.

    Evidence is positional across the lists in declaration order: notes
    occupy indexes ``0`` to ``len(notes) - 1``, statements continue from
    there, and issues after those — so one planning turn references any kind
    the same way. A kind is appended rather than inserted, because the
    indexes a planner already wrote are persisted in run state and a resumed
    run must still read them as it meant them.
    """

    source: SourceSnapshot
    notes: list[InventoryNote] = []
    statements: list[str] = Field(
        default=[],
        description=(
            "Evidence a human gave in their own words, for work nothing in "
            "the tree carries a note for."
        ),
    )
    issues: list[IssueEvidence] = Field(
        default=[],
        description=(
            "Evidence the project's tracker already holds, so an issue does "
            "not have to be transcribed into a note before a run can act on "
            "it."
        ),
    )

    @model_validator(mode="after")
    def evidence_is_present(self) -> "ResolveRequest":
        if not self.evidence_count():
            raise ValueError("a resolve request needs at least one piece of evidence")
        return self

    def evidence_count(self) -> int:
        return len(self.notes) + len(self.statements) + len(self.issues)

    async def inventory(self, planner: InventoryPlanner) -> ResolveInventory:
        return await planner(self)


class ConcernInventory(BaseModel, frozen=True):
    """Structured concern plan produced by the read-only inventory turn.

    Concerns reference the request's notes by position; the resolver
    materializes them into :class:`Concern` objects with the authoritative
    note content, so nothing the planner writes can drift from the evidence.
    """

    concerns: list[PlannedConcern] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_concerns(self) -> "ConcernInventory":
        identifiers = [concern.id for concern in self.concerns]
        if len(identifiers) != len(dict.fromkeys(identifiers)):
            raise ValueError("planned concern ids must be unique")
        return self


class AdmissionRequest(BaseModel, frozen=True):
    """Evidence discovered while a run was already moving.

    Only this evidence is planned; the run's existing concerns, recorded
    answers, and completed work are carried forward untouched, because the
    moment a run is most informative about what else needs doing is the
    moment it can least afford to be re-derived.
    """

    notes: list[InventoryNote] = []
    statements: list[str] = []
    issues: list[IssueEvidence] = []

    @model_validator(mode="after")
    def evidence_is_present(self) -> "AdmissionRequest":
        if not self.notes and not self.statements and not self.issues:
            raise ValueError("an admission needs at least one piece of evidence")
        return self


class ConcernAdmission(BaseModel, frozen=True):
    """What one mid-run admission added to a live run."""

    run_id: str
    phase: ResolvePhase
    concerns: list[Concern]
    questions: list[MaterialQuestion]
    outstanding: list[MaterialQuestion] = []
    rejected: list[str] = []


class ResolverConfig(BaseModel, frozen=True):
    state_root: Path
    workspace: Path
    worktree_root: Path
    run_id: str
    integration_branch: str
    max_revision_rounds: int = Field(default=2, ge=0)
    max_declaration_attempts: int = Field(default=2, ge=0)
    """Rounds a worker may spend reconciling its file declaration for free.

    Separate from the revision budget because the two are not the same
    scarce thing: a declaration mismatch is mechanical and cheap to check,
    while a revision round is a reviewer's judgement of the work. Charging
    both to one allowance let a concern oscillate between under-declaring
    and over-declaring until it failed with its criteria never evaluated.
    """
    max_parallel_workers: int = Field(default=4, ge=1)
    """How many concerns may hold a session at once.

    Uncapped, a batch opens one session per runnable concern — a measured
    run reached eleven within the same second. Three things follow from
    that, and none of them is throughput. The host's allowance is spent at
    the width of the batch rather than the depth of the work, which was 18
    of one run's 35 refusals. Every session shares one credential file, so a
    refresh rotates the token under the others and denies them all at once,
    which was the other 12. And an interruption costs whatever is in flight,
    so a wide batch loses more of it.

    Four rather than one because the concerns are genuinely independent and
    waiting them out serially is real time; four rather than eleven because
    none of the three costs above is paid per concern, they are paid per
    concurrent session. Our judgement about one host, so a caller sets it.
    """
    recheck_standing_per_join: bool = False
    """Whether each join re-checks the concerns already in the tree.

    The per-join pass costs a reviewer turn for every overlapping pair, so
    it grows quadratically: 21 parents is up to 210 turns, and a measured
    run spent about fourteen minutes on each. What it buys is attribution —
    a criterion that stopped holding is reported against the one join that
    broke it rather than against every parent at once.

    Off by default because the final pass is the one that decides. Every
    concern is examined there against the finished tree, where an answer can
    still change what happens, so what this adds is a name for the cause
    rather than a finding that would otherwise be missed.
    """
    regeneration_command: list[str] = []
    """How this project re-renders whatever it generates, if it generates.

    A join conflicting in a rendered artifact is settled by running this
    rather than by a merger choosing between two stale renderings. Named by
    the application because no library can know it: one project renders with
    its own CLI, another with a build tool, and most render nothing at all.
    Empty leaves every conflict to the merger, as before.
    """
    verification_commands: list["VerificationCommand"] = []

    @model_validator(mode="after")
    def run_identity_is_path_safe(self) -> "ResolverConfig":
        if not self.run_id or Path(self.run_id).name != self.run_id:
            raise ValueError("resolver run id must be a non-empty path-safe name")
        if not self.verification_commands:
            raise ValueError("resolver integration requires verification commands")
        return self


class VerificationCommand(BaseModel, frozen=True):
    name: str
    arguments: list[str]
    base_option: str = ""
    """The flag this command is told the verified tree's own base through.

    A base belongs to the tree being checked, not to the command, and
    writing one into the arguments made it part of the run's composition:
    the digest that gates a resume moved whenever the base did, so a run
    could not resume itself once its base changed — including onto the
    commit that fixed the defect it was parked for. Naming the flag here
    and supplying the value per tree keeps the composition free of a commit
    and lets each lease be judged against the base it actually started from.

    Empty for a command that takes no such flag, which is then run exactly
    as declared.
    """

    def against(self, base: str) -> list[str]:
        """This command as run over a tree that started from ``base``."""
        if not self.base_option or not base:
            return self.arguments
        return [*self.arguments, self.base_option, base]


class ConcernsDocument(BaseModel, frozen=True):
    concerns: list[Concern]


class LeasesDocument(BaseModel, frozen=True):
    leases: list[WritableRootLease]


class BasesDocument(BaseModel, frozen=True):
    bases: list[DependencyBase]


class ResolveState(BaseModel, frozen=True):
    """Complete resumable run state written atomically after every transition."""

    schema_version: int = 2
    config_digest: str
    config: ResolverConfig | None = None
    """The composition this run was persisted under, for naming what moved.

    Optional because a run persisted before this was recorded has only the
    digest, which says that something differs and never which field: the
    resume that hits a moved configuration is exactly when a reader needs
    the difference, and a hash cannot be subtracted back into one.
    """
    run_id: str
    phase: ResolvePhase
    source: SourceSnapshot
    base: SourceSnapshot | None = None
    """Where a root concern starts now, when the branch has moved since.

    Apart from ``source`` because the two answer different questions. The
    source is where this run began and never changes, which is what a
    reader tracing a decision needs; the base is what a lease made today is
    cut from, and a run parked while its own blocking defect was fixed must
    be able to start its remaining leases from the fix.
    """
    spec: ResolveSpec
    concerns: list[Concern]
    progress: list[ConcernProgress]
    questions: QuestionBatch | None = None
    answers: AnswerBatch | None = None
    retired_questions: list[str] = []
    """Questions explicitly made inactive by adopting another integration tree.

    Their declarations and answers remain in the mailbox and state; this list
    changes atomically with the adopted commit so interrupted recovery cannot
    release a question about the tree still recorded as authoritative.
    """
    eligibility: list[ConcernEligibility] = []
    leases: list[WritableRootLease] = []
    bases: list[DependencyBase] = []
    outcomes: list[ConcernOutcome] = []
    integration: IntegrationRecord | None = None
    join_progress: JoinProgress | None = None
    verification: list[VerificationRecord] = []
    acceptances: list[VerificationAcceptance] = []
    retirements: list[ConcernRetirement] = []
    admitted_requests: list[str] = []
    cleanup: list[CleanupRecord] = []
    failures: list[str] = []
    resume_from: ResolvePhase | None = None
    abort_reason: str = Field(
        default="",
        description=(
            "Why a human ended this run. An abort is a decision rather than a "
            "failure, so it is recorded apart from `failures` and never offers "
            "a resume phase."
        ),
    )

    def root_base(self) -> SourceSnapshot:
        """What a concern with no dependency in this run is cut from."""
        return self.base if self.base is not None else self.source

    def tally(
        self, settled_statuses: tuple[ConcernStatus, ...] = SETTLED_STATUSES
    ) -> "RunTally":
        """Fold this persisted state into the aggregate a watcher wants.

        Where the settled line is drawn, because this is the one fold both
        readers of it pass through: the bar a run prints and the supervisor's
        header each take their numerator from here, so a caller who counts
        ``integrating`` as finished redraws the line once and both agree.

        Only the fallback half moves. A concern already stamped stays counted
        whatever is passed, which is the monotonicity ``settled_at`` exists
        for — so the line widens freely and narrows only over work that has
        not landed yet.
        """
        statuses = [item.status for item in self.progress]
        return RunTally(
            phase=self.phase,
            total=len(statuses),
            by_status={
                status: statuses.count(status) for status in dict.fromkeys(statuses)
            },
            settled=len(
                [
                    item
                    for item in self.progress
                    if item.settled_at is not None or item.status in settled_statuses
                ]
            ),
            settled_at=sorted(
                item.settled_at for item in self.progress if item.settled_at is not None
            ),
            joined=self.join_progress.landed() if self.join_progress else 0,
            join_completions=(
                self.join_progress.completions if self.join_progress else []
            ),
            join_total=len(self.join_progress.planned) if self.join_progress else 0,
        )

    @model_validator(mode="after")
    def complete_progress_projection(self) -> "ResolveState":
        concern_ids = [concern.id for concern in self.concerns]
        progress_ids = [item.concern_id for item in self.progress]
        if sorted(concern_ids) != sorted(progress_ids):
            raise ValueError("resolver progress must cover every concern exactly")
        return self


class RunTally(BaseModel, frozen=True):
    """Aggregate progress a watcher reads at a glance.

    Reconstructing "how far along is this run" took a full read of the
    record and the worktrees; every piece is already in state, so the
    aggregation lives beside it and every surface prints the same one.
    """

    phase: ResolvePhase
    total: int
    by_status: dict[ConcernStatus, int]
    joined: int
    join_total: int
    settled: int = 0
    """How many concerns are done being decided, however each one ended.

    Counted from the stamp a settling concern carries, falling back to its
    current status, so the figure only ever rises. Membership alone cannot
    do that: the lifecycle legitimately moves work back out of a settled
    status — ``verified`` to ``integrating`` as assembly opens, and
    ``verified`` or ``failed`` to ``eligible`` on rework — and a reader
    watching the count fall reads a healthy run as a broken one. The stamp
    is written once and never cleared, which is what makes this monotonic
    by construction rather than by a rule about which statuses to list.

    Derived here rather than left to each reader to sum, so the bar a run
    prints and the supervisor's own header cannot answer "how far along"
    differently.
    """
    settled_at: list[datetime] = []
    """When each settled concern landed, in order, as far as it is recorded.

    The rate samples, carried beside the counts for the reason
    ``JoinProgress.completions`` gives: the journal these could be scanned
    out of reaches tens of megabytes and is read by a status view that runs
    often. Shorter than ``settled`` whenever a concern settled without a
    stamp, which costs an ETA its precision and no count its accuracy.
    """
    join_completions: list[datetime] = []
    """When merges in the active join sequence landed, in order.

    What ``settled_at`` is to the concerns, for the phase that does not
    settle any: carried so a reader holding only this can time the joins.
    Confined to the active sequence by the phase clearing its progress as
    integration opens — these accumulate across every join a run performs,
    so without that they would be the worker phase's samples estimating the
    integration ones.
    """

    def concerns_line(self, include_joins: bool = True) -> str:
        """The tally as one compact human line.

        The joins are dropped where a caller draws them as their own bar, so
        the same fraction is not printed twice on one line.
        """
        counted = " · ".join(
            f"{status} {count}" for status, count in self.by_status.items() if count
        )
        line = f"{counted or 'no concerns'} of {self.total}"
        if include_joins and self.join_total:
            line += f" · joins {self.joined}/{self.join_total}"
        return line


class ResolveManifest(BaseModel, frozen=True):
    schema_version: int = 1
    run_id: str
    source: SourceSnapshot
    review_branch: str
    outcomes: list[ConcernOutcome]
    verification: list[VerificationRecord]
    cleanup: list[CleanupRecord] = []
