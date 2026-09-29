"""One provider-neutral, persisted resolver state machine."""

import asyncio
import hashlib
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from pydantic import BaseModel

from lup.harness.contracts import SkillInvocationRenderer
from lup.harness.models import ResolveSpec
from lup.harness.process import ProcessLauncher
from lup.resolver.admissions import (
    AdmissionMailbox,
    AdmissionReceipt,
    AdmissionStatus,
    ResolverAdmissionsPending,
)
from lup.resolver.contracts import (
    ResolverAssemblyDeferred,
    ResolverAwaitingAnswers,
    ResolverConcernRetired,
    ResolverDrained,
    ResolverEnvironmentFault,
    ResolverObserver,
    ResolverRegression,
    WorktreePreparer,
    settles_the_actor,
)
from lup.coordination.mailbox import ANSWER_POLL_SECONDS
from lup.coordination.refs import ActorRef
from lup.coordination.cohort import ActorCohort
from lup.resolver.dag import ConcernGraph
from lup.resolver.execution import ConcernExecutor
from lup.resolver.grants import GrantLedger
from lup.resolver.joins import Joiner
from lup.resolver.record import (
    Journal,
    LeaseDriftEvent,
    RunFailedEvent,
)
from lup.resolver.mailbox import QuestionMailbox
from lup.resolver.models import (
    INTEGRATION_CONCERN_ID,
    AdmissionRequest,
    AnswerBatch,
    CleanupRecord,
    ConcernApproval,
    Concern,
    ConcernAdmission,
    ConcernEligibility,
    ConcernInventory,
    ConcernOrigin,
    ConcernProgress,
    ConcernExecution,
    ConcernStatus,
    ConcernOutcome,
    IntegrationRecord,
    MaterialQuestion,
    QuestionBatch,
    RecheckRuling,
    RefreshReport,
    ResolveInventory,
    ResolveManifest,
    ResolvePhase,
    ResolveRequest,
    ResolverSource,
    IssueEvidence,
    ReviewNote,
    ResolverConfig,
    ResolveState,
    SupersessionRuling,
    WritableRootLease,
)
from lup.resolver.orchestrator import (
    LeaseViolationError,
    DependencyBaseBuilder,
    WorktreeOrchestrator,
    WritableRootLeases,
)
from lup.resolver.questions import QuestionBroker
from lup.resolver.rebase import BaseRefresher
from lup.resolver.run import ResolveRun, ResolverInvariantError
from lup.resolver.recovery import IntegrationRecoveryDesk
from lup.resolver.state import (
    PHASE_ORDER,
    ResolverStateRepository,
    StateTransitionError,
)
from lup.resolver.turns import (
    ReviewerFactoryRecipe,
    TurnRunner,
    WorkerFactoryRecipe,
)
from lup.resolver.verification import Verifier


logger = logging.getLogger(__name__)

# The two words an approval answer may carry live in `ConcernApproval`, which
# publishes the choices and is what a reader tests, so neither end can spell
# one the other does not offer.


class ApprovalDecisions(BaseModel, frozen=True):
    """Persisted direct choices and their dependency-safe eligible subset."""

    directly_approved: list[str]
    eligible: list[str]


class EvidenceCitation(BaseModel, frozen=True):
    """One concern's evidence, named in the fields the concern carries."""

    notes: list[ReviewNote]
    evidence: str
    issues: list[IssueEvidence]


def resolver_config_digest(config: ResolverConfig) -> str:
    """Bind resumed state to the exact injected resolver composition inputs."""
    encoded = config.model_dump_json().encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def moved_config_fields(
    persisted: ResolverConfig, current: ResolverConfig
) -> list[str]:
    """Which fields of the composition a run was persisted under now differ.

    Empty while the digest still disagrees means no recorded value moved and
    the composition's shape did: a field absent from the persisted document
    parses as its default, so growing the model changes the serialization
    every digest is taken over without changing anything a run was decided
    on. Naming that case is the difference between adopting a schema
    addition and adopting a changed verification gate.
    """
    return sorted(
        name
        for name in type(current).model_fields
        if getattr(persisted, name) != getattr(current, name)
    )


def failure_messages(error: BaseException) -> list[str]:
    """Flatten parallel failures so no sibling evidence is discarded."""
    if isinstance(error, BaseExceptionGroup):
        return [
            message
            for nested in error.exceptions
            for message in failure_messages(nested)
        ]
    return [str(error)]


INVENTORY_PLAN_ATTEMPTS = 3


def coverage_complaint(referenced: list[int], total: int) -> str | None:
    """Name the evidence a plan ignored and the evidence it invented.

    A note is not a unit of work: one can raise several concerns and several
    can raise one, so concerns reference evidence rather than partitioning
    it. What still cannot happen is evidence no concern claims, because it
    goes unresolved with nothing to show for it.
    """
    faults = [
        ("no concern references", [i for i in range(total) if i not in referenced]),
        ("outside the evidence", [i for i in referenced if i not in range(total)]),
    ]
    named = [f"{label}: {sorted(indexes)}" for label, indexes in faults if indexes]
    return "; ".join(named) if named else None


def planned_evidence(request: ResolveRequest, indexes: list[int]) -> EvidenceCitation:
    """Split one plan's positional references back into what it cites.

    The lists run end to end in declaration order — notes, then statements,
    then issues — so a planner references any kind the same way and the
    materialized concern still carries each in the form it came in.
    """
    statements = len(request.notes) + len(request.statements)
    return EvidenceCitation(
        notes=[
            ReviewNote.model_validate(
                request.notes[index].model_dump(exclude={"context"})
            )
            for index in indexes
            if index < len(request.notes)
        ],
        evidence="\n".join(
            request.statements[index - len(request.notes)]
            for index in indexes
            if len(request.notes) <= index < statements
        ),
        issues=[
            request.issues[index - statements]
            for index in indexes
            if statements <= index < statements + len(request.issues)
        ],
    )


def merge_parked(parked: list[ResolverAwaitingAnswers]) -> ResolverAwaitingAnswers:
    """Combine sibling parks so one rerun can answer every pending question."""
    pending = {question.id: question for park in parked for question in park.pending}
    problems = [problem for park in parked for problem in park.problems]
    return ResolverAwaitingAnswers(list(pending.values()), problems)


def merge_faults(faults: list[ResolverEnvironmentFault]) -> ResolverEnvironmentFault:
    """Combine sibling host faults into the one cause they all met.

    Every concern in a batch meets the same dead credential within seconds,
    so reporting each separately would say one thing several times. The
    first cause is the whole cause; the concerns are named so a reader knows
    which turns were spent and will be spent again.
    """
    return ResolverEnvironmentFault(
        faults[0].cause,
        sorted({concern for fault in faults for concern in fault.concerns}),
    )


def merge_drained(drained: list[ResolverDrained]) -> ResolverDrained:
    """Combine sibling drains into the one request they all answered.

    One operator asked once, so the reason is theirs and is stated once. The
    concerns are named because each is a turn that will be taken again.
    """
    return ResolverDrained(
        drained[0].reason,
        sorted({concern for stop in drained for concern in stop.concerns}),
    )


def approval_question(concern: Concern) -> MaterialQuestion:
    """Build the persisted human integration gate for one planned concern.

    A concern's declared allowances are named in the prompt rather than asked
    separately: approving the concern is approving the edit gates its own
    plan requires, decided once with the title and spec in view.
    """
    grants = "".join(f"\nGrants: {allowance}" for allowance in concern.allowances)
    return MaterialQuestion(
        id=f"integration-approval-{concern.id}",
        concern_id=concern.id,
        prompt=f"Include {concern.title!r} in this resolver run?{grants}",
        choices=ConcernApproval.choices(),
        recommendation=ConcernApproval.APPROVE,
        closed_choices=True,
    )


# lup: ignore[constant-declaration] — a reserved question id a human answers by
# name on the command line, so the word is part of the interface rather than a
# setting behind it
ASSEMBLY_QUESTION_ID = "integration-assembly"
"""The one gate on assembling the review branch, asked once per run."""


def assembly_question(
    verified: list[ConcernOutcome],
    excluded: list[ConcernOutcome],
    base: str,
    behind: int = 0,
    branch: str = "",
) -> MaterialQuestion:
    """Build the gate on assembling the review branch itself.

    Every per-concern approval is cashed here: this is the step that merges
    the branches they authorized into one tree, and it is the least
    reversible thing a run does. Letting it follow the last worker
    automatically, in the same invocation, leaves killing the process in the
    seconds between as the only way to stop it.

    It is also the first moment three things are knowable — which concerns
    verified, which failed and are therefore excluded, and what the branch
    will be built on — so the prompt names all three instead of asking for a
    bare yes. A run parking here resumes at no cost, and the recorded answer
    is who decided to assemble that branch.

    How far the base has fallen behind is the fourth. A run parks for hours
    and its branch moves underneath; assembling onto a superseded base is
    exactly the moment a human wants to know, so the prompt says it. Reported
    rather than acted on, because refreshing here would move every lease
    under work already verified against where it stood.
    """
    merging = "\n".join(f"  merge {outcome.concern_id}" for outcome in verified)
    dropping = "\n".join(
        f"  exclude {outcome.concern_id}: {outcome.failure or 'not verified'}"
        for outcome in excluded
    )
    stale = (
        f"\n  this base is {behind} commit(s) behind {branch}; "
        "`resolve refresh --apply` moves it before you approve"
        if behind and branch
        else ""
    )
    return MaterialQuestion(
        id=ASSEMBLY_QUESTION_ID,
        concern_id="integration",
        prompt=(
            f"Assemble the review branch from {len(verified)} verified "
            f"concern(s) onto {base[:12]}?{stale}\n{merging}"
            + (f"\n{dropping}" if dropping else "")
        ),
        choices=ConcernApproval.choices(),
        recommendation=ConcernApproval.APPROVE,
        closed_choices=True,
    )


def approval_decisions(
    concerns: list[Concern], answers: AnswerBatch
) -> ApprovalDecisions:
    """Return direct and dependency-safe approvals from persisted answers."""
    answer_values = {answer.question_id: answer.value for answer in answers.answers}
    gated = [
        concern
        for concern in concerns
        if concern.eligible and concern.integration_approved
    ]
    missing = [
        concern.id
        for concern in gated
        if approval_question(concern).id not in answer_values
    ]
    if missing:
        raise ResolverInvariantError(
            "no persisted approval answer for " + ", ".join(sorted(missing))
        )
    direct = {
        concern.id
        for concern in gated
        if answer_values[approval_question(concern).id] == ConcernApproval.APPROVE
    }
    approved = ConcernGraph(concerns).transitively_approved(direct)
    return ApprovalDecisions(
        directly_approved=[concern.id for concern in concerns if concern.id in direct],
        eligible=[concern.id for concern in approved],
    )


class ResolverCore:
    """Drive one run through its phases, composing who does each of them.

    What stays here is the sequence and the boundaries: plan an inventory,
    put its gates to a human, lease a writable root per approved concern,
    walk the dependency graph, assemble the review branch, and free what the
    run held. Everything a phase does inside those boundaries belongs to a
    collaborator that can be reached without reaching this class — the run's
    state, the question desk, the turn runner, the joiner, the verifier, and
    the per-concern executor.
    """

    def __init__(
        self,
        config: ResolverConfig,
        spec: ResolveSpec,
        worker_factory: WorkerFactoryRecipe,
        reviewer_factory: ReviewerFactoryRecipe,
        invocation_renderer: SkillInvocationRenderer,
        process_launcher: ProcessLauncher,
        observer: ResolverObserver | None = None,
        worktree_preparer: WorktreePreparer | None = None,
        answer_wait_seconds: float = 0.0,
        poll_interval_seconds: float = ANSWER_POLL_SECONDS,
        adopt_config: bool = False,
        environmental_fault: Callable[[str], bool] = lambda _: False,
    ) -> None:
        self.adopt_config = adopt_config
        self.config = config
        self.spec = spec
        self.answer_wait_seconds = answer_wait_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.observer = observer
        self.repository = ResolverStateRepository(config.state_root, config.run_id)
        self.leases = WritableRootLeases(config.worktree_root)
        self.worktrees = WorktreeOrchestrator(
            process_launcher, config.workspace, worktree_preparer
        )
        self.mailbox = QuestionMailbox(self.repository.root)
        self.journal = Journal(self.repository.root)
        # The cohort joins the person itself, so a worker telling the humans
        # something has an address to send it to. Addressing the run's own ref
        # instead reaches a name constructed to attribute journal entries and
        # answering to nothing else, leaving a worker no route out but a question.
        self.actors = ActorCohort(
            self.repository.root,
            journal=self.journal,
            mail=self.mailbox.mail,
            description=f"resolver run {config.run_id}",
            parallel=config.max_parallel_workers,
            settles=lambda error: settles_the_actor(error, environmental_fault),
        )
        self.run_state = ResolveRun(self.repository, self.journal, observer)
        self.rebaser = BaseRefresher(self.run_state, self.worktrees, self.journal)
        self.grants = GrantLedger(self.repository.root)
        self.questions = QuestionBroker(
            config,
            self.run_state,
            self.mailbox,
            self.journal,
            self.grants,
            answer_wait_seconds,
            poll_interval_seconds,
        )
        self.turns = TurnRunner(
            spec,
            self.run_state,
            self.actors,
            self.mailbox,
            worker_factory,
            reviewer_factory,
            invocation_renderer,
            self.grants,
        )
        self.verifier = Verifier(config.verification_commands, process_launcher)
        self.joiner = Joiner(
            self.run_state,
            self.turns,
            self.questions,
            self.verifier,
            self.worktrees,
            self.journal,
            standing_rechecks=config.recheck_standing_per_join,
            regeneration=config.regeneration_command,
            parallel_rechecks=config.max_parallel_workers,
        )
        self.executor = ConcernExecutor(
            config,
            self.run_state,
            self.turns,
            self.questions,
            self.joiner,
            self.verifier,
            self.worktrees,
            self.leases,
            self.repository,
            self.journal,
            environmental_fault,
        )

    @property
    def state(self) -> ResolveState | None:
        """The live state, held by the run every phase shares."""
        return self.run_state.state

    @state.setter
    def state(self, value: ResolveState | None) -> None:
        self.run_state.state = value

    @property
    def state_lock(self) -> asyncio.Lock:
        """The lock every writer of that state takes."""
        return self.run_state.lock

    @property
    def promoter_problems(self) -> list[str]:
        """What the answer promoter could not do, as the desk recorded it."""
        return self.questions.problems

    @property
    def wake(self) -> asyncio.Event:
        """Set whenever recorded answers change, to end a poll early."""
        return self.run_state.wake

    def require_state(self) -> ResolveState:
        return self.run_state.require()

    def persist(self, state: ResolveState) -> None:
        """Persist while keeping the phase a monotonic high-water mark."""
        self.run_state.persist(state)

    async def run(self, source: ResolverSource) -> ResolveManifest:
        """Run through final review and stop at the human acceptance boundary."""
        with self.repository.exclusive():
            inventory = await source.inventory(self.plan_inventory)
            return await self.run_exclusive(inventory)

    async def plan_inventory(
        self,
        request: ResolveRequest,
        origin: ConcernOrigin = ConcernOrigin.INVENTORY,
        taken: list[str] | None = None,
        attempts: int = INVENTORY_PLAN_ATTEMPTS,
    ) -> ResolveInventory:
        """Organize raw review evidence into concerns in a read-only turn.

        A rejected partition is fed back to the planner by name rather than
        ending the run: intake is the point where the least work is persisted
        and the most would be lost. ``taken`` names the ids a live run has
        already recorded, so evidence planned into a run that is already
        moving cannot collide with the concerns it joins.
        """
        reserved = (
            "\nThese ids already name a concern in this run and may not be "
            "reused; depend on one by id instead: " + ", ".join(taken)
            if taken
            else ""
        )
        prompt = (
            "Organize every piece of review evidence into generalized "
            "implementation concerns without editing. Cluster by underlying "
            "issue, not file. Reference evidence through evidence_indexes using "
            "its zero-based position in the evidence below — notes first, then "
            "statements, then issues. Neither a note nor an issue is a unit of "
            "work: split one that raises several problems across several "
            "concerns, and reference it from each. "
            "Every index must appear at least once; none may repeat "
            "within a single concern. Give each concern path-safe id, complete acceptance "
            "criteria, dependencies, material questions, and starting files. Every "
            "concern's criteria must scope analysis and action together — never "
            "plan one concern to audit and a second to act on what it found. That "
            "splits one piece of work across two leases that cannot see each "
            "other, and the auditing half finds real violations it is forbidden "
            "to fix. Declare "
            "an allowance only when the plan cannot be carried out without the gate "
            "it names, so approving the concern approves what it actually needs. A "
            "question offering an option that would need a gate names it on the "
            "question too, and the concern must request it — an option the concern "
            "cannot be granted is one to omit, not one to disclaim in its text. A "
            "concern's own notes are stripped from its lease before the worker "
            "starts, so never write a criterion demanding an in-place marker "
            "conversion: the worker finishes by writing `# lup: solved: <the "
            "note's original words>` fresh at the site, and the "
            "do-not-reintroduce rule governs open feedback only. Do "
            "not decide eligibility or integration approval; the resolver asks the "
            f"user.{reserved}"
            f"\n\nReview evidence:\n{request.model_dump_json(indent=2)}"
        )
        planner = ActorRef(kind="planner", id=self.config.run_id)
        # A retry is a second turn on the planner's own session, so the
        # correction is the whole input: it already holds the evidence and the
        # partition it just proposed, and restating both would invite it to
        # re-derive from scratch what it should be revising.
        attempt = prompt
        complaint: str | None = None
        for _ in range(attempts):
            result = await self.turns.reviewer_round(
                planner,
                self.config.workspace,
                attempt,
                ConcernInventory,
            )
            referenced = [
                index
                for planned in result.output.concerns
                for index in planned.evidence_indexes
            ]
            complaint = coverage_complaint(referenced, request.evidence_count())
            if complaint is None:
                break
            attempt = (
                f"That partition left review evidence unaccounted for — "
                f"{complaint}. Every index from 0 to "
                f"{request.evidence_count() - 1} must be referenced by at least "
                "one concern. Revise and resubmit."
            )
        else:
            raise ResolverInvariantError(
                "inventory planner left review evidence unaccounted for across "
                f"{attempts} attempts — {complaint}"
            )
        concerns = [
            Concern(
                **planned_evidence(request, planned.evidence_indexes).model_dump(),
                origin=origin,
                eligible=True,
                integration_approved=True,
                **planned.model_dump(exclude={"evidence_indexes"}),
            )
            for planned in result.output.concerns
        ]
        return ResolveInventory(source=request.source, concerns=concerns)

    async def run_exclusive(self, inventory: ResolveInventory) -> ResolveManifest:
        """Execute a new run while the repository owns its process lease."""
        if self.repository.exists():
            raise ResolverInvariantError(
                f"resolver run {self.config.run_id!r} already exists; resume it explicitly"
            )
        state = ResolveState(
            run_id=self.config.run_id,
            config_digest=resolver_config_digest(self.config),
            config=self.config,
            phase=ResolvePhase.INVENTORY,
            source=inventory.source,
            spec=self.spec,
            concerns=inventory.concerns,
            progress=[
                ConcernProgress(concern_id=concern.id) for concern in inventory.concerns
            ],
        )
        self.persist(state)
        try:
            return await self.advance(state)
        except (
            ResolverAwaitingAnswers,
            ResolverEnvironmentFault,
            ResolverAssemblyDeferred,
            ResolverDrained,
        ):
            # None of these is this run failing. Persisting a failure here
            # would move the phase to `failed` and make the resume path
            # re-derive a `resume_from`, when nothing about the run's own
            # state is wrong — only the host it was running on, a human who
            # has not yet said to assemble the branch, or one who asked it
            # to stop.
            raise
        except Exception as error:
            self.persist_failure(error)
            raise

    async def resume(self) -> ResolveManifest:
        """Resume a persisted run without redoing completed concern outcomes."""
        with self.repository.exclusive():
            return await self.resume_exclusive()

    def composition_delta(self, state: ResolveState) -> str:
        """Which composition fields moved, for a refusal that must be judged.

        A run persisted before the composition was recorded can only say
        that something moved. One that recorded it names the fields, and
        names their absence too: no field differing while the digest does is
        the signature of a field added to the model rather than a decision
        this run was made under, which is the one move that is safe to adopt
        without re-deriving anything.
        """
        if state.config is None:
            return " (persisted before the composition itself was recorded)"
        fields = moved_config_fields(state.config, self.config)
        if not fields:
            return (
                " (every recorded field matches, so the model gained or lost one"
                " rather than this run's inputs changing)"
            )
        return f" (moved: {', '.join(fields)})"

    def adopted(self) -> ResolveState:
        """Re-stamp a run onto the current composition, on the human's word.

        Adoption is recorded rather than silent: the digest is what a later
        resume checks, so a run that adopted one composition and reports the
        old one would refuse itself again for a move nobody made.
        """
        adopted = self.repository.adopt(
            self.config, resolver_config_digest(self.config)
        )
        self.state = adopted
        return adopted

    async def resume_exclusive(self) -> ResolveManifest:
        """Resume while holding the run's inter-process lease."""
        if not self.repository.exists():
            raise ResolverInvariantError(
                f"resolver run {self.config.run_id!r} does not exist"
            )
        state = self.repository.load()
        self.state = state
        moved = [
            name
            for name, persisted, current in (
                ("run id", state.run_id, self.config.run_id),
                ("specification", state.spec, self.spec),
                (
                    "configuration",
                    state.config_digest,
                    resolver_config_digest(self.config),
                ),
            )
            if persisted != current
        ]
        match moved, self.adopt_config:
            case ["configuration"], True:
                state = self.adopted()
            case [_, *_], _:
                # Naming neither what moved nor the way out left one recovery
                # to guess at, and the run holding the most answers is the one
                # that hits this: parking exposes a defect, and fixing the
                # defect is what moves the configuration under the parked run.
                raise ResolverInvariantError(
                    f"resolver run {state.run_id!r} was persisted under a "
                    "different "
                    + " and ".join(moved)
                    + self.composition_delta(state)
                    + "; resume it from the same tree and gate, adopt the move "
                    "with --adopt-config once it reads as compatible, or abort "
                    "it with --abort <reason> to start a run that matches"
                )
        if state.phase == ResolvePhase.ABORTED:
            raise ResolverInvariantError(
                f"resolver run {state.run_id!r} was aborted: {state.abort_reason}"
            )
        if state.phase == ResolvePhase.COMPLETE:
            self.state = state
            return self.manifest(state)
        if state.phase == ResolvePhase.FAILED:
            if state.resume_from is None:
                raise ResolverInvariantError(
                    "failed resolver state has no resume phase"
                )
            state = state.model_copy(
                update={"phase": state.resume_from, "resume_from": None}
            )
            self.persist(state)
        self.restore_leases(state)
        state = self.require_state()
        try:
            return await self.advance(state)
        except (
            ResolverAwaitingAnswers,
            ResolverEnvironmentFault,
            ResolverAssemblyDeferred,
            ResolverDrained,
        ):
            # None of these is this run failing. Persisting a failure here
            # would move the phase to `failed` and make the resume path
            # re-derive a `resume_from`, when nothing about the run's own
            # state is wrong — only the host it was running on, a human who
            # has not yet said to assemble the branch, or one who asked it
            # to stop.
            raise
        except Exception as error:
            self.persist_failure(error)
            raise

    async def advance(self, state: ResolveState) -> ResolveManifest:
        """Advance an initial or restored state through the acceptance boundary.

        A stale park marker would abort the first wait of a resumed run, so
        it is the one mailbox record a resume clears.
        """
        async with self.promoting():
            return await self.advance_exclusive(state)

    @asynccontextmanager
    async def promoting(self) -> AsyncGenerator[None]:
        """Keep every door's offers promoted for the lifetime of one operation.

        Waiting reads ``answers/``, and only a promoter writes there, so any
        wait that runs without this is a wait no door can satisfy.

        Every actor's session is released here too. A park is an exit like
        any other: the session ids are persisted on the way out, so resuming
        reattaches to the conversations rather than starting new ones.

        The promoter is awaited for its exceptions rather than by re-raising
        them, because this runs while the body's own exception is propagating:
        raising here would replace the reason the operation ended with a
        symptom of it ending. What the promoter could not do is already on
        ``promoter_problems``, which every park report carries.
        """
        self.mailbox.clear_park()
        # A satisfied drain goes the same way, and for the same reason: left
        # standing, the run it was asked of stops again at the first
        # boundary of the resume that answered it.
        self.mailbox.clear_drain()
        self.promoter_problems.clear()
        stop = asyncio.Event()
        promoter = asyncio.create_task(self.questions.promote_until(stop))
        try:
            yield
        finally:
            stop.set()
            outcome = await asyncio.gather(promoter, return_exceptions=True)
            self.record_promoter_exit(outcome[0])
            await self.actors.close()

    def record_promoter_exit(self, outcome: None | BaseException) -> None:
        """Record a promoter that ended by raising rather than by being stopped.

        ``promote_until`` handles its own errors, so anything arriving here
        escaped that net — a cancellation, or a failure of the handling
        itself. Either way the doors stopped being served at that moment, so
        it is recorded as a problem the next park report carries rather than
        discarded.
        """
        if outcome is None:
            return
        logger.exception("resolver promoter ended early", exc_info=outcome)
        self.promoter_problems.append(
            f"the answer promoter stopped early ({outcome!r}), so offers made "
            "after that point were never taken"
        )

    async def advance_exclusive(self, state: ResolveState) -> ResolveManifest:
        """Revisit admission gates whenever a wave receives durable evidence."""
        while True:
            await self.apply_admissions()
            state = self.require_state()
            try:
                return await self.advance_round(state)
            except ResolverAdmissionsPending:
                self.leases.adopt(self.require_state().leases)
                continue

    async def advance_round(self, state: ResolveState) -> ResolveManifest:
        """Advance while a promoter is folding every door's answers in.

        Each stage asks what the current concern set still needs rather than
        whether the stage has run before, so a concern admitted after the run
        started reaches the same gates as one from intake instead of skipping
        the stages its siblings already passed.
        """
        asked = {
            question.id
            for question in (state.questions.questions if state.questions else [])
        }
        unasked = [
            question
            for question in self.pending_questions(state.concerns)
            if question.id not in asked
        ]
        if state.questions is None or unasked:
            self.questions.queue_questions(unasked, "planning")
            state = state.model_copy(
                update={
                    "phase": ResolvePhase.QUESTIONS,
                    "questions": QuestionBatch(
                        run_id=state.run_id,
                        questions=[
                            *(state.questions.questions if state.questions else []),
                            *unasked,
                        ],
                    ),
                }
            )
            state = self.run_state.progress_state(
                state,
                [question.concern_id for question in unasked],
                ConcernStatus.WAITING_FOR_ANSWERS,
            )
            self.persist(state)
        questions = state.questions
        if questions is None:
            raise ResolverInvariantError("question phase has no persisted batch")
        await self.questions.await_questions(questions.questions)
        state = self.require_state()

        answers = state.answers
        if answers is None:
            raise ResolverInvariantError("eligibility requires persisted answers")
        decisions = approval_decisions(state.concerns, answers)
        decision_ids = decisions.directly_approved
        approved_ids = decisions.eligible
        graph = ConcernGraph(state.concerns)
        approved = [concern for concern in state.concerns if concern.id in approved_ids]
        settled = {item.concern_id for item in state.eligibility}
        undecided = [concern for concern in state.concerns if concern.id not in settled]
        if undecided:
            eligibility = [
                ConcernEligibility(
                    concern_id=concern.id,
                    eligible=concern.id in approved_ids,
                    integration_approved=concern.id in decision_ids,
                    reason=(
                        "ready"
                        if concern.id in approved_ids
                        else (
                            "an ancestor is deferred or not approved"
                            if concern.id in decision_ids
                            else "deferred or not approved"
                        )
                    ),
                )
                for concern in undecided
            ]
            state = state.model_copy(
                update={
                    "phase": ResolvePhase.ELIGIBILITY,
                    "eligibility": [*state.eligibility, *eligibility],
                }
            )
            state = self.run_state.progress_state(
                state,
                [item.concern_id for item in eligibility if item.eligible],
                ConcernStatus.ELIGIBLE,
            )
            # Each exclusion keeps the reason its own ruling gave it. One
            # string covering all of them read as a three-way ambiguity, and
            # telling "nobody approved this" from "its ancestor was not
            # approved" meant reading the raw state back — the first is a
            # decision to revisit, the second only a consequence of one.
            excluded = [item for item in eligibility if not item.eligible]
            for reason in dict.fromkeys(item.reason for item in excluded):
                state = self.run_state.progress_state(
                    state,
                    [item.concern_id for item in excluded if item.reason == reason],
                    ConcernStatus.INELIGIBLE,
                    reason,
                )
            self.persist(state)

        if state.phase in {
            ResolvePhase.INVENTORY,
            ResolvePhase.QUESTIONS,
            ResolvePhase.ELIGIBILITY,
        }:
            state = state.model_copy(update={"phase": ResolvePhase.DAG})
            self.persist(state)

        lease_by_concern = {
            lease.concern_id: lease
            for lease in state.leases
            if lease.concern_id != "integration"
        }
        unleased = [
            concern
            for concern in approved
            if concern.id not in lease_by_concern
            and concern.id
            not in {item.concern_id for item in self.require_state().retirements}
        ]
        # A resume is when the branch has moved: the run parked, the fix that
        # unblocks it landed, and the base is what carries that fix to every
        # lease cut afterwards. Conditioning this on needing a new lease left
        # a run whose concerns were all leased reading its original commit for
        # the rest of its life, and asking the human, once per lease, about a
        # blocker the branch had already fixed.
        state = self.rebaser.refreshed(state)
        if unleased:
            fresh = [
                self.leases.acquire(concern.id, self.concern_branch(concern.id))
                for concern in unleased
            ]
            lease_by_concern.update({lease.concern_id: lease for lease in fresh})
            state = state.model_copy(
                update={
                    "phase": ResolvePhase.LEASES,
                    "leases": [*state.leases, *fresh],
                }
            )
            state = self.run_state.progress_state(
                state,
                [concern.id for concern in unleased],
                ConcernStatus.LEASED,
            )
            self.persist(state)
        if state.phase in {ResolvePhase.DAG, ResolvePhase.LEASES}:
            state = state.model_copy(update={"phase": ResolvePhase.WORKERS})
            self.persist(state)

        commits = {
            outcome.concern_id: outcome.commit
            for outcome in state.outcomes
            if outcome.verified and outcome.commit is not None
        }
        outcomes = list(state.outcomes)
        # A retired concern is settled somewhere else, so it neither runs nor
        # blocks: its dependents build from the base, which is where the work
        # that settled it now lives. Treating it as completed is what keeps it
        # out of the eligible set without recording it as having failed.
        retired = {item.concern_id for item in state.retirements}
        completed_ids = {outcome.concern_id for outcome in outcomes} | retired
        builder = DependencyBaseBuilder(state.root_base())
        match state.integration:
            case None:
                for batch in graph.topological_batches():
                    if AdmissionMailbox(self.repository.root).pending():
                        raise ResolverAdmissionsPending()
                    state = self.require_state()
                    retired = {item.concern_id for item in state.retirements}
                    completed_ids.update(retired)
                    commits.update(
                        {identifier: state.root_base().commit for identifier in retired}
                    )
                    selected = [
                        item
                        for item in batch
                        if item.id in approved_ids and item.id not in completed_ids
                    ]
                    runnable = [
                        concern
                        for concern in selected
                        if all(
                            parent in commits or parent in retired
                            for parent in concern.dependencies
                        )
                    ]
                    unmet = "a dependency did not produce a verified commit"
                    for blocked in selected:
                        if blocked in runnable:
                            continue
                        await self.run_state.settle_concern(
                            ConcernOutcome(
                                concern_id=blocked.id,
                                branch=lease_by_concern[blocked.id].branch,
                                failure=unmet,
                            ),
                            ConcernStatus.FAILED,
                            unmet,
                        )
                        completed_ids.add(blocked.id)

                    # Capped rather than gathered wholesale. A concern still
                    # waiting on the cap has started nothing and recorded
                    # nothing, so an interruption leaves it exactly as the
                    # lease phase left it and the next batch selects it again
                    # — which is what makes a cut wave resumable rather than
                    # lost.
                    #
                    # The population's own wave rather than one assembled
                    # here. How many agents run at once, which of them are
                    # running, and what a close reaches are three facts about
                    # the population, and a phase that fanned out for itself
                    # answered the last two differently from the roster every
                    # door reads.
                    runnable_by_id = {concern.id: concern for concern in runnable}

                    async def execute_for(opened: ActorRef) -> ConcernExecution | None:
                        """This address's concern, carried through its whole work."""
                        try:
                            return await self.executor.execute_concern(
                                runnable_by_id[opened.id],
                                lease_by_concern[opened.id],
                                commits,
                                builder,
                            )
                        except ResolverConcernRetired:
                            return None

                    results = await self.actors.work_all(
                        execute_for,
                        [
                            ActorRef(kind="worker", id=concern.id)
                            for concern in runnable
                        ],
                    )
                    failures = [
                        result
                        for result in results
                        if isinstance(result, BaseException)
                    ]
                    executions = [
                        result
                        for result in results
                        if isinstance(result, ConcernExecution)
                    ]
                    for execution in executions:
                        if (
                            execution.outcome.verified
                            and execution.outcome.commit is not None
                        ):
                            commits[execution.outcome.concern_id] = (
                                execution.outcome.commit
                            )
                        completed_ids.add(execution.outcome.concern_id)
                    # Every outcome above was persisted beside its own
                    # terminal transition, so the batch reads the record back
                    # rather than keeping a second copy that an interruption
                    # could contradict.
                    state = self.require_state()
                    outcomes = list(state.outcomes)
                    bases = list(state.bases)
                    state = state.model_copy(
                        update={"phase": ResolvePhase.WORKERS, "bases": bases}
                    )
                    self.persist(state)
                    self.repository.write_agent_round(state)
                    if failures:
                        errors = [
                            failure
                            for failure in failures
                            if isinstance(failure, Exception)
                        ]
                        if len(errors) != len(failures):
                            raise ResolverInvariantError(
                                "parallel concern execution was cancelled"
                            )
                        # A host fault reaches every concern in the batch at
                        # once, so it decides the batch even when only some
                        # of them had reached the provider. Nothing here is
                        # worth asking a human about until the host works
                        # again, which is why this outranks a park rather
                        # than joining it.
                        faults = [
                            error
                            for error in errors
                            if isinstance(error, ResolverEnvironmentFault)
                        ]
                        if faults:
                            raise merge_faults(faults)
                        parked = [
                            error
                            for error in errors
                            if isinstance(error, ResolverAwaitingAnswers)
                        ]
                        if parked and len(parked) == len(errors):
                            raise merge_parked(parked)
                        # A drain reaches every concern in the batch the same
                        # way a host fault does, and says as little about any
                        # of them: an operator asked, and each one stopped
                        # where stopping was free.
                        drained = [
                            error
                            for error in errors
                            if isinstance(error, ResolverDrained)
                        ]
                        if drained and len(drained) == len(errors):
                            raise merge_drained(drained)
                        raise ExceptionGroup("parallel concern failures", errors)
                    if (request := self.questions.draining()) is not None:
                        # The other junction the issue names. Concerns already
                        # settled keep their outcomes; what does not happen is
                        # the next batch being started.
                        raise ResolverDrained(request.reason, [])
                    if AdmissionMailbox(self.repository.root).pending():
                        raise ResolverAdmissionsPending()

                state = self.require_state()
                state = state.model_copy(
                    update={"phase": ResolvePhase.DEPENDENCY_BASES}
                )
                self.persist(state)
                state = state.model_copy(update={"phase": ResolvePhase.REVIEW})
                self.persist(state)
                await self.approve_assembly(state, outcomes)
                state = await self.integrate(state, outcomes)
            case IntegrationRecord(completed=False):
                # A resumed run re-enters integration until the record says
                # it finished. `completed` is written once verification
                # passes, which is the last mechanical fact the run produces
                # — the judgement on top of it belongs to whoever opens the
                # journal afterwards.
                state = await self.integrate(state, outcomes)
        self.land_nested(state)
        return self.manifest(self.release(state))

    def refresh(self, apply: bool = False) -> RefreshReport:
        """Report, and optionally take, what refreshing this run would do.

        The leases already made are the ones the automatic refresh cannot
        reach: they hold work, so bringing the branch into them is a
        decision rather than a default. Reporting first is what makes it a
        decision somebody can take — several concerns in the run this comes
        from were editing the very files the upstream fix touched.
        """
        with self.repository.exclusive():
            state = self.repository.load()
            self.state = state
            return self.rebaser.report(state, apply)

    def restore_leases(self, state: ResolveState) -> None:
        """Validate persisted authority and reset incomplete attempts for retry."""
        self.leases.adopt(state.leases)
        outcomes = {outcome.concern_id: outcome for outcome in state.outcomes}
        bases = {base.concern_id: base for base in state.bases}
        concerns = {concern.id: concern for concern in state.concerns}
        commits = {
            outcome.concern_id: outcome.commit
            for outcome in state.outcomes
            if outcome.verified and outcome.commit is not None
        }
        for lease in self.leases.leases.values():
            if lease.concern_id == "integration":
                # The merger can publish its landing before the run process
                # projects it. Recovery and resume read that same authority.
                expected = IntegrationRecoveryDesk(self.repository).recorded_commit(
                    state, lease
                )
                self.restore_worktree(lease, expected, True)
                continue
            outcome = (
                outcomes[lease.concern_id] if lease.concern_id in outcomes else None
            )
            base = bases[lease.concern_id] if lease.concern_id in bases else None
            if base is not None:
                expected = base.commit
            else:
                concern = concerns[lease.concern_id]
                parent_commits = [
                    commits[parent]
                    for parent in concern.dependencies
                    if parent in commits
                ]
                expected = (
                    parent_commits[0] if parent_commits else state.root_base().commit
                )
            if outcome is not None:
                self.restore_worktree(
                    lease,
                    outcome.head or outcome.commit or expected,
                    True,
                    abandoned=not outcome.verified,
                )
                continue
            if not lease.root.exists() and not self.worktrees.branch_exists(lease):
                self.restore_concern_progress(lease.concern_id)
                continue
            self.restore_worktree(lease, expected, False)
            self.restore_concern_progress(lease.concern_id)

    def restore_worktree(
        self,
        lease: WritableRootLease,
        expected: str,
        terminal: bool,
        *,
        abandoned: bool = False,
    ) -> None:
        """Restore one persisted branch and validate or reset its exact commit.

        An abandoned tree is one no actor will open again in this run: the
        concern failed, so nothing reads it and nothing merges from it. Its
        drift is recorded rather than raised, because a resume that refuses
        the whole run over it strands every healthy concern beside it — four
        verified and five newly eligible, in the run that reported this.
        """
        if not lease.root.exists():
            if self.worktrees.branch_exists(lease):
                self.worktrees.restore(lease)
            else:
                self.worktrees.create(lease, expected)
        self.worktrees.branch(lease)
        if terminal:
            found = self.worktrees.head(lease)
            if found == expected:
                return
            if not abandoned:
                raise ResolverInvariantError(
                    f"persisted commit changed for {lease.concern_id}: expected "
                    f"{expected}, found {found}"
                    + (
                        "; stop the run and use resolve recover-integration "
                        "restore-recorded or adopt-head --run-id "
                        f"{self.config.run_id} after inspecting the worktree"
                        if lease.concern_id == "integration"
                        else ""
                    )
                )
            self.journal.record(
                LeaseDriftEvent(
                    concern_id=lease.concern_id, expected=expected, found=found
                )
            )
            return
        # A park is a pause, not an abandonment. What sits in the working tree
        # is the turn a question interrupted — the join being resolved, or the
        # edits a worker made before it found the decision it could not take —
        # and the actor's session is reattached still holding it. Discarding it
        # priced every question at a wasted turn, and the rerun re-derived that
        # question under an id no recorded answer matched, so the same decision
        # was put to the human as many as four times in one run.

    def restore_concern_progress(self, concern_id: str) -> None:
        """Return one interrupted concern to its leased retry boundary."""
        state = self.require_state()
        current = next(
            item.status for item in state.progress if item.concern_id == concern_id
        )
        if current == ConcernStatus.LEASED:
            return
        if current != ConcernStatus.ELIGIBLE:
            state = self.run_state.progress_state(
                state, [concern_id], ConcernStatus.ELIGIBLE, "retry after interruption"
            )
            self.persist(state)
        state = self.run_state.progress_state(
            state, [concern_id], ConcernStatus.LEASED, "retry lease restored"
        )
        self.persist(state)

    def record_regressions(
        self, state: ResolveState, regressed: list[RecheckRuling]
    ) -> ResolveState:
        """Write the lost criteria onto the outcomes that lost them.

        The ruling is a durable fact about the merged tree, not a detail of
        the invocation that heard it. Recorded on the outcome, the next
        session reads which concern broke and which criteria it broke,
        instead of finding a completed run and an answer file nothing acted
        on.
        """
        lost = {ruling.concern_id: ruling for ruling in regressed}
        return state.model_copy(
            update={
                "outcomes": [
                    outcome.model_copy(
                        update={"regressed": lost[outcome.concern_id].criteria}
                    )
                    if outcome.concern_id in lost
                    else outcome
                    for outcome in state.outcomes
                ]
            }
        )

    def persist_failure(self, error: Exception) -> None:
        """Persist both the failure and the exact phase from which to resume."""
        state = self.require_state()
        resume_from = (
            state.resume_from if state.phase == ResolvePhase.FAILED else state.phase
        )
        for message in failure_messages(error):
            self.journal.record(RunFailedEvent(reason=message))
        self.persist(
            state.model_copy(
                update={
                    "phase": ResolvePhase.FAILED,
                    "resume_from": resume_from,
                    "failures": [*state.failures, *failure_messages(error)],
                }
            )
        )

    async def admit_concern(self, concern: Concern) -> None:
        """Take a concern discovered mid-run into the run that discovered it.

        The worked example this exists for: an audit concern whose criteria
        forbade it from moving code, with a second concern depending on it to
        act — two concerns that should have been one, discovered only once
        both were leased. Without admission the choice was to drop the
        finding or restart the run and lose every answer already given.
        """
        async with self.state_lock:
            state = self.require_state()
            if any(item.id == concern.id for item in state.concerns):
                raise ResolverInvariantError(
                    f"concern {concern.id!r} is already in this run"
                )
            self.persist(
                state.model_copy(
                    update={
                        "concerns": [*state.concerns, concern],
                        "progress": [
                            *state.progress,
                            ConcernProgress(
                                concern_id=concern.id,
                                status=ConcernStatus.DISCOVERED,
                                reason=(
                                    f"admitted mid-run, superseding {concern.supersedes}"
                                    if concern.supersedes
                                    else "admitted mid-run"
                                ),
                            ),
                        ],
                    }
                )
            )

    async def approve_assembly(
        self, state: ResolveState, outcomes: list[ConcernOutcome]
    ) -> None:
        """Put the assembly of the review branch to a human before doing it.

        Asked once per run and only when there is something to merge. The
        answer parks the run the way every other gate does, so the pause
        costs nothing and the wait is where a human reads what is about to
        be assembled.
        """
        verified = [outcome for outcome in outcomes if outcome.verified]
        if not verified:
            return
        excluded = [outcome for outcome in outcomes if not outcome.verified]
        source = state.root_base()
        question = assembly_question(
            verified,
            excluded,
            source.commit,
            self.worktrees.behind(source.commit, source.branch),
            source.branch,
        )
        self.questions.queue_questions([question], "integration")
        answers = await self.questions.await_questions([question])
        if any(answer.value == ConcernApproval.DEFER for answer in answers.answers):
            raise ResolverAssemblyDeferred(
                [outcome.concern_id for outcome in verified],
                [outcome.concern_id for outcome in excluded],
            )

    async def integrate(
        self, state: ResolveState, outcomes: list[ConcernOutcome]
    ) -> ResolveState:
        if state.integration is None:
            retired = {item.concern_id for item in self.require_state().retirements}
            verified = [
                outcome
                for outcome in outcomes
                if outcome.verified and outcome.concern_id not in retired
            ]
            integration_lease = next(
                (lease for lease in state.leases if lease.concern_id == "integration"),
                None,
            )
            if integration_lease is None:
                integration_lease = self.leases.acquire(
                    "integration", self.integration_lease_branch()
                )
                leases = [*state.leases, integration_lease]
            else:
                leases = state.leases
            # Cleared as the phase opens, because what it holds belongs to the
            # joins the worker phase drove: its commit names another lease's
            # tree, which a resume here would restore the integration lease
            # to, and its completions would have the dependency joins timing
            # the integration ones. This sequence has not started, which is
            # the state the restore already knows how to re-enter.
            state = state.model_copy(
                update={
                    "phase": ResolvePhase.INTEGRATION,
                    "leases": leases,
                    "join_progress": None,
                }
            )
            state = self.run_state.progress_state(
                state,
                [outcome.concern_id for outcome in verified],
                ConcernStatus.INTEGRATING,
            )
            self.persist(state)
            if not integration_lease.root.exists():
                self.worktrees.create(integration_lease, state.root_base().commit)
            commits = [
                outcome.commit for outcome in verified if outcome.commit is not None
            ]
            if commits:
                parents = [state.root_base().commit, *commits]
                integration_commit = await self.joiner.join_commits(
                    integration_lease,
                    parents,
                    "final review-master integration",
                    "resolve: integrate approved concerns",
                )
            else:
                integration_commit = self.worktrees.head(integration_lease)
            integrated_ids = {outcome.concern_id for outcome in verified}
            integration = IntegrationRecord(
                branch=integration_lease.branch,
                worktree=integration_lease.root,
                concerns=sorted(integrated_ids),
                commit=integration_commit,
                completed=False,
            )
            # Re-read rather than updating the copy this scope has held since
            # before the joins: `join_commits` persisted progress under it,
            # and building from the stale local would drop what it recorded.
            # The record supersedes that progress, so it is cleared with the
            # same write that establishes it.
            state = self.require_state().model_copy(
                update={
                    "integration": integration,
                    "join_progress": None,
                }
            )
            self.persist(state)
        else:
            integration = state.integration
            try:
                integration_lease = next(
                    lease for lease in state.leases if lease.concern_id == "integration"
                )
            except StopIteration as error:
                raise ResolverInvariantError(
                    "persisted integration has no writable-root lease"
                ) from error

        if not integration.completed:
            verification = self.verifier.verify(
                integration_lease.root, state.root_base().commit
            )
            state = state.model_copy(
                update={
                    "phase": ResolvePhase.VERIFICATION,
                    "verification": verification,
                }
            )
            self.persist(state)
            failed = [record.name for record in verification if not record.passed]
            if failed:
                raise ResolverInvariantError(
                    "integration verification failed: " + ", ".join(failed)
                )
            # The re-check's two answers mean opposite things about this
            # branch, so the run waits for them here rather than completing
            # around them. "Superseded" settles a lost criterion; a
            # regression says the assembled tree is wrong, and finishing
            # anyway is what shipped one.
            rechecks = await self.joiner.recheck_criteria(state, integration)
            regressed = [
                ruling
                for ruling in await self.joiner.settle_rechecks(rechecks)
                if ruling.ruling == SupersessionRuling.REGRESSION
            ]
            if regressed:
                self.persist(self.record_regressions(state, regressed))
                raise ResolverRegression(regressed)
            integrated_ids = {identifier: True for identifier in integration.concerns}
            integration = integration.model_copy(update={"completed": True})
            integrated_outcomes = [
                outcome.model_copy(
                    update={"integrated": outcome.concern_id in integrated_ids}
                )
                for outcome in state.outcomes
            ]
            state = state.model_copy(
                update={
                    "outcomes": integrated_outcomes,
                    "integration": integration,
                }
            )
            state = self.run_state.progress_state(
                state,
                integration.concerns,
                ConcernStatus.INTEGRATED,
            )
            self.persist(state)
        else:
            verification = state.verification

        if any(not record.passed for record in verification):
            raise ResolverInvariantError("persisted integration verification failed")

        return state

    async def admit(self, request: AdmissionRequest) -> ConcernAdmission:
        """Plan evidence found mid-run into the run that found it.

        Restarting was the only way to widen a concern set, and it re-derived
        the inventory from scratch — discarding every material answer already
        collected at exactly the moment a run holds the most of them. Only the
        new evidence is planned here; the run keeps its id, its answers, and
        its completed work, so nothing already decided is decided again.

        Admission stops at integration, because past it the review branch is
        assembled and a concern joining would have to reopen it. Everything
        earlier is fair game: the admitted concern enters at ``discovered``
        and walks the same question, approval, eligibility, and lease path as
        one from intake.

        Nothing is written until the widened set holds: a writable root the
        run already handed out refuses the admission, and so does a graph
        that would not be a unique-id, present-dependency, acyclic one.
        """
        with self.repository.exclusive():
            self.state = self.repository.load()
            return await self.admit_owned(request)

    async def apply_admissions(self) -> None:
        """Consume each accepted request exactly once, under the run's ownership."""
        mailbox = AdmissionMailbox(self.repository.root)
        for receipt in mailbox.pending():
            if receipt.id in self.require_state().admitted_requests:
                mailbox.write(
                    receipt.model_copy(update={"status": AdmissionStatus.APPLIED})
                )
                continue
            try:
                await self.admit_owned(receipt.request, receipt)
            except (
                ValueError,
                ResolverInvariantError,
                StateTransitionError,
                LeaseViolationError,
            ) as error:
                mailbox.write(
                    receipt.model_copy(
                        update={"status": AdmissionStatus.REJECTED, "error": str(error)}
                    )
                )
                self.actors.tell_user(f"Admission {receipt.id} rejected: {error}")

    async def admit_owned(
        self, request: AdmissionRequest, receipt: AdmissionReceipt | None = None
    ) -> ConcernAdmission:
        """Plan and insert evidence while the caller owns the resolver run."""
        state = self.require_state()
        if state.phase in {ResolvePhase.ABORTED, ResolvePhase.COMPLETE}:
            raise ResolverInvariantError(f"run is already {state.phase}")
        reached = (
            state.resume_from or state.phase
            if state.phase is ResolvePhase.FAILED
            else state.phase
        )
        if PHASE_ORDER[reached] >= PHASE_ORDER[ResolvePhase.INTEGRATION]:
            raise ResolverInvariantError(
                f"run has reached {reached}; a concern may only join a run "
                "before its review branch is assembled"
            )
        if receipt is not None and receipt.result is not None:
            planned_concerns = receipt.result.concerns
        else:
            planned = await self.plan_inventory(
                ResolveRequest(
                    source=state.source,
                    notes=request.notes,
                    statements=request.statements,
                    issues=request.issues,
                ),
                origin=ConcernOrigin.ADMITTED,
                taken=[concern.id for concern in state.concerns],
            )
            planned_concerns = planned.concerns
        state = self.require_state()
        self.leases.adopt(state.leases)
        for concern in planned_concerns:
            self.leases.plan(concern.id, self.concern_branch(concern.id))
        concerns = [*state.concerns, *planned_concerns]
        ConcernGraph(concerns)
        # An admitted concern's questions join the run's batch the way
        # intake's do. Returning them without recording them left the
        # concern admitted and unanswerable: no door could see a question
        # that was never written, and the gate it names can never pass.
        admitted = self.pending_questions(planned_concerns)
        result = ConcernAdmission(
            run_id=state.run_id,
            phase=state.phase,
            concerns=planned_concerns,
            questions=admitted,
        )
        mailbox = AdmissionMailbox(self.repository.root)
        if receipt is not None:
            receipt = receipt.model_copy(
                update={"status": AdmissionStatus.PLANNED, "result": result}
            )
            mailbox.write(receipt)
        self.questions.queue_questions(admitted, "admission")
        # An answer offered in the same invocation is offered before the
        # question exists, so only promoting here can match the two. The
        # run's own rerun recipe hands out `--answer` flags, which made
        # combining them with `--admit` the obvious thing to try and
        # silently discarded every one of them.
        problems = self.questions.promote_offers()
        widened = state.model_copy(
            update={
                "concerns": concerns,
                "questions": QuestionBatch(
                    run_id=state.run_id,
                    questions=[
                        *(state.questions.questions if state.questions else []),
                        *admitted,
                    ],
                ),
                "answers": AnswerBatch(
                    run_id=state.run_id,
                    answers=[record.answer for record in self.mailbox.answers()],
                ),
                "progress": [
                    *state.progress,
                    *[
                        ConcernProgress(concern_id=concern.id)
                        for concern in planned_concerns
                    ],
                ],
                "admitted_requests": [
                    *state.admitted_requests,
                    *([receipt.id] if receipt is not None else []),
                ],
            }
        )
        self.persist(widened)
        result = ConcernAdmission(
            run_id=state.run_id,
            phase=self.require_state().phase,
            concerns=planned_concerns,
            questions=admitted,
            outstanding=[
                question
                for question in admitted
                if question.id not in self.mailbox.answered_ids()
            ],
            rejected=problems,
        )
        if receipt is not None:
            mailbox.write(
                receipt.model_copy(
                    update={"status": AdmissionStatus.APPLIED, "result": result}
                )
            )
        return result

    def concern_branch(self, concern_id: str) -> str:
        return f"resolve/{self.config.run_id}/{concern_id}"

    def pending_questions(self, concerns: list[Concern]) -> list[MaterialQuestion]:
        """Every gate these concerns must pass before any work begins."""
        return [question for concern in concerns for question in concern.questions] + [
            approval_question(concern)
            for concern in concerns
            if concern.eligible and concern.integration_approved
        ]

    def abort(self, reason: str) -> ResolveManifest:
        """End a run from any phase, freeing its leases but keeping its evidence.

        Cleanup was reachable only at acceptance, so a run abandoned while its
        concerns held leases stranded one worktree and one branch each with no
        way back. Aborting frees those the same way acceptance does and retains
        the integration lease, because the review branch may hold real work.
        Concern statuses are left as they stood: what each concern reached is
        the evidence an abort exists to preserve.
        """
        with self.repository.exclusive():
            state = self.repository.load()
            self.state = state
            if state.phase in {ResolvePhase.ABORTED, ResolvePhase.COMPLETE}:
                raise ResolverInvariantError(f"run is already {state.phase}")
            cleanup = [
                CleanupRecord(
                    path=lease.root,
                    branch=lease.branch,
                    action="retained",
                    reason=f"review branch retained after abort: {reason}",
                )
                if lease.concern_id == INTEGRATION_CONCERN_ID
                else self.abort_lease(lease)
                for lease in state.leases
            ]
            aborted = state.model_copy(
                update={
                    "phase": ResolvePhase.ABORTED,
                    "abort_reason": reason,
                    "resume_from": None,
                    "cleanup": [*state.cleanup, *cleanup],
                    "leases": [
                        lease.model_copy(update={"active": False})
                        for lease in state.leases
                    ],
                }
            )
            self.persist(aborted)
            return self.manifest(aborted)

    def abort_lease(self, lease: WritableRootLease) -> CleanupRecord:
        """Free one concern lease, reporting a dirty tree instead of forcing it."""
        removal = self.worktrees.remove(lease)
        if not removal.freed:
            return CleanupRecord(
                path=lease.root,
                branch=lease.branch,
                action="retained",
                reason=f"worktree not freed; remove manually: {removal.detail}",
            )
        return CleanupRecord(
            path=lease.root,
            branch=lease.branch,
            action="removed",
            reason="; ".join(
                part
                for part in ["concern worktree freed by abort", removal.detail]
                if part
            ),
        )

    def nested(self) -> bool:
        """Whether this run integrates onto a branch already checked out."""
        return (
            self.worktrees.current_branch(self.config.workspace)
            == self.config.integration_branch
        )

    def integration_lease_branch(self) -> str:
        """The branch the integration worktree owns.

        A nested run cannot take the review branch directly, because git will
        not add a second worktree for a branch that is already checked out.
        It builds on a ref of its own and the standing worktree
        fast-forwards to it at the end, which moves ref, index and working
        tree in one step.
        """
        if self.nested():
            return f"{self.config.integration_branch}-wip-{self.config.run_id}"
        return self.config.integration_branch

    def land_nested(self, state: ResolveState) -> None:
        """Advance the launching worktree's review branch to what this run built."""
        if not self.nested() or state.integration is None:
            return
        built = state.integration.commit
        if built is None or self.worktrees.fast_forward(self.config.workspace, built):
            return
        self.journal.record(
            RunFailedEvent(
                reason=(
                    f"{self.config.integration_branch} could not fast-forward to "
                    f"{built[:12]}; it moved while this run was building on it. "
                    "Merge it by hand."
                )
            )
        )

    def concern_status(
        self, state: ResolveState, concern_id: str
    ) -> ConcernStatus | None:
        return next(
            (item.status for item in state.progress if item.concern_id == concern_id),
            None,
        )

    def release(self, state: ResolveState) -> ResolveState:
        """Free the concern leases and hand the review branch over.

        There was a human gate here, and it decided nothing. Accept and
        reject both retained the integration lease, both removed every
        concern lease, and differed only in a sentence recorded against the
        cleanup — so the run stopped, spent a question, and used the answer
        to choose wording. Per-concern control is the live stop-and-retarget
        channel while the run moves, and the acceptance of the result is
        whatever the human does with the branch afterwards.
        """
        cleanup: list[CleanupRecord] = []  # lup: ignore[empty-collection]
        progress = state
        for lease in state.leases:
            if lease.concern_id == "integration":
                cleanup.append(
                    CleanupRecord(
                        path=lease.root,
                        branch=lease.branch,
                        action="retained",
                        reason="review branch retained for the human to land",
                    )
                )
                continue
            removal = self.worktrees.remove(lease)
            # Cleaning a worktree is housekeeping, and says nothing about what
            # was decided — `CleanupRecord` below already carries whether the
            # tree went. So only the one status that is provisional on cleanup
            # moves: a concern that failed keeps saying so, a retired one keeps
            # the human's word, and a status added later stays put rather than
            # crashing on a transition its table never declared.
            if self.concern_status(progress, lease.concern_id) == (
                ConcernStatus.INTEGRATED
            ):
                progress = self.run_state.progress_state(
                    progress,
                    [lease.concern_id],
                    ConcernStatus.CLEANED if removal.freed else ConcernStatus.RETAINED,
                )
            cleanup.append(
                CleanupRecord(
                    path=lease.root,
                    branch=lease.branch,
                    action="removed" if removal.freed else "retained",
                    reason="; ".join(
                        part
                        for part in [
                            "concern worktree cleaned after review"
                            if removal.freed
                            else "automatic cleanup failed; remove manually",
                            removal.detail,
                        ]
                        if part
                    ),
                )
            )
        completed = progress.model_copy(
            update={
                "phase": ResolvePhase.CLEANUP,
                "cleanup": cleanup,
                "leases": [
                    lease.model_copy(update={"active": False}) for lease in state.leases
                ],
            }
        )
        self.persist(completed)
        completed = completed.model_copy(update={"phase": ResolvePhase.COMPLETE})
        self.persist(completed)
        return completed

    def manifest(self, state: ResolveState) -> ResolveManifest:
        return ResolveManifest(
            run_id=state.run_id,
            source=state.source,
            review_branch=(
                state.integration.branch
                if state.integration is not None
                else self.config.integration_branch
            ),
            outcomes=state.outcomes,
            verification=state.verification,
            cleanup=state.cleanup,
        )
