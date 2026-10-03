"""Atomic schema-versioned resolver state persistence."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from lup.channels.models import publish_atomic
from lup.execution.locks import exclusive, try_exclusive
from lup.resolver.admissions import (
    AdmissionMailbox,
    AdmissionReceipt,
    ResolverAdmissionsPending,
)
from lup.resolver.models import (
    AgentRound,
    AdmissionRequest,
    AnswerBatch,
    BasesDocument,
    ConcernProgress,
    ConcernStatus,
    ConcernsDocument,
    HeldLease,
    LeasesDocument,
    QuestionBatch,
    ResolverConfig,
    CleanupRecord,
    ConcernRetirement,
    VerificationAcceptance,
    ReviewReport,
    ResolveState,
    ResolvePhase,
    WorkerReport,
)

logger = logging.getLogger(__name__)


def preserve_retirements(
    current: ResolveState, candidate: ResolveState
) -> ResolveState:
    """An operator's durable retirement outranks work computed before it."""
    retired = {item.concern_id for item in current.retirements}
    if not retired:
        return candidate
    progress = {item.concern_id: item for item in current.progress}
    roots = {lease.root for lease in current.leases if lease.concern_id in retired}
    return candidate.model_copy(
        update={
            "retirements": current.retirements,
            "progress": [
                progress[item.concern_id] if item.concern_id in retired else item
                for item in candidate.progress
            ],
            "leases": [
                lease.model_copy(update={"active": False})
                if lease.concern_id in retired
                else lease
                for lease in candidate.leases
            ],
            "outcomes": [
                item for item in candidate.outcomes if item.concern_id not in retired
            ]
            + [item for item in current.outcomes if item.concern_id in retired],
            "cleanup": [item for item in candidate.cleanup if item.path not in roots]
            + [item for item in current.cleanup if item.path in roots],
        }
    )


PHASE_ORDER: dict[ResolvePhase, int] = {
    phase: index for index, phase in enumerate(ResolvePhase)
}
# lup: ignore[library-default] — the successor of each phase in this library's own closed enum
PHASE_TRANSITIONS: dict[ResolvePhase, ResolvePhase] = {
    ResolvePhase.INVENTORY: ResolvePhase.QUESTIONS,
    ResolvePhase.QUESTIONS: ResolvePhase.ELIGIBILITY,
    ResolvePhase.ELIGIBILITY: ResolvePhase.DAG,
    ResolvePhase.DAG: ResolvePhase.LEASES,
    ResolvePhase.LEASES: ResolvePhase.WORKERS,
    ResolvePhase.WORKERS: ResolvePhase.DEPENDENCY_BASES,
    ResolvePhase.DEPENDENCY_BASES: ResolvePhase.REVIEW,
    ResolvePhase.REVIEW: ResolvePhase.INTEGRATION,
    ResolvePhase.INTEGRATION: ResolvePhase.VERIFICATION,
    ResolvePhase.VERIFICATION: ResolvePhase.CLEANUP,
    ResolvePhase.CLEANUP: ResolvePhase.COMPLETE,
}


def already_settled(status: ConcernStatus) -> bool:
    """Whether this concern is done, so retiring it would claim nothing.

    A concern whose work reached the review branch, or was deliberately
    retained, has an answer. Everything else — including failed, and
    including ineligible — is still open, and open is exactly what a human
    retires when the work turns out to have landed somewhere else.
    """
    return status in (
        ConcernStatus.INTEGRATED,
        ConcernStatus.CLEANED,
        ConcernStatus.RETAINED,
        ConcernStatus.RETIRED,
    )


# lup: ignore[library-default] — the legal successors of each status in this library's own closed enum
DECLARED_TRANSITIONS: dict[ConcernStatus, list[ConcernStatus]] = {
    ConcernStatus.DISCOVERED: [
        ConcernStatus.WAITING_FOR_ANSWERS,
        ConcernStatus.ELIGIBLE,
        ConcernStatus.INELIGIBLE,
        ConcernStatus.FAILED,
    ],
    ConcernStatus.WAITING_FOR_ANSWERS: [
        ConcernStatus.ELIGIBLE,
        ConcernStatus.INELIGIBLE,
        ConcernStatus.RUNNING,
        ConcernStatus.FAILED,
    ],
    ConcernStatus.ELIGIBLE: [ConcernStatus.LEASED, ConcernStatus.FAILED],
    ConcernStatus.INELIGIBLE: [],
    ConcernStatus.LEASED: [
        ConcernStatus.RUNNING,
        ConcernStatus.WAITING_FOR_ANSWERS,
        ConcernStatus.ELIGIBLE,
        ConcernStatus.FAILED,
    ],
    ConcernStatus.RUNNING: [
        ConcernStatus.VALIDATING,
        ConcernStatus.WAITING_FOR_ANSWERS,
        ConcernStatus.ELIGIBLE,
        ConcernStatus.FAILED,
    ],
    ConcernStatus.VALIDATING: [
        ConcernStatus.REVIEWING,
        ConcernStatus.REVISING,
        ConcernStatus.ELIGIBLE,
        ConcernStatus.FAILED,
    ],
    ConcernStatus.REVIEWING: [
        ConcernStatus.REVISING,
        ConcernStatus.VERIFIED,
        ConcernStatus.ELIGIBLE,
        ConcernStatus.FAILED,
    ],
    ConcernStatus.REVISING: [
        ConcernStatus.RUNNING,
        ConcernStatus.ELIGIBLE,
        ConcernStatus.FAILED,
    ],
    ConcernStatus.VERIFIED: [
        ConcernStatus.ELIGIBLE,
        ConcernStatus.INTEGRATING,
        ConcernStatus.RETAINED,
        ConcernStatus.FAILED,
    ],
    ConcernStatus.INTEGRATING: [ConcernStatus.INTEGRATED, ConcernStatus.FAILED],
    ConcernStatus.INTEGRATED: [
        ConcernStatus.CLEANED,
        ConcernStatus.RETAINED,
        ConcernStatus.FAILED,
    ],
    ConcernStatus.CLEANED: [],
    ConcernStatus.RETAINED: [],
    ConcernStatus.FAILED: [
        ConcernStatus.ELIGIBLE,
        ConcernStatus.CLEANED,
        ConcernStatus.RETAINED,
    ],
}

CONCERN_TRANSITIONS: dict[ConcernStatus, list[ConcernStatus]] = {
    status: targets if already_settled(status) else [*targets, ConcernStatus.RETIRED]
    for status, targets in DECLARED_TRANSITIONS.items()
} | {ConcernStatus.RETIRED: []}
"""Every move a concern may make, with retirement derived rather than listed.

Retiring is a human's decision about a concern that is still open, so it is
reachable from every status that is not already settled. Deriving it is what
keeps a status added later from silently being un-retirable — the omission a
hand-written table cannot report.
"""
type PersistedResolverModel = (
    ResolveState
    | AgentRound
    | ConcernsDocument
    | QuestionBatch
    | AnswerBatch
    | LeasesDocument
    | BasesDocument
    | WorkerReport
    | ReviewReport
)


class StateTransitionError(RuntimeError):
    """A persisted run cannot make the requested state transition."""


class StateCorruptionError(RuntimeError):
    """A persisted resolver document cannot be decoded as its schema."""


def progress_index(progress: list[ConcernProgress]) -> dict[str, ConcernProgress]:
    """Index a complete progress projection while rejecting duplicate ids."""
    indexed = {item.concern_id: item for item in progress}
    if len(indexed) != len(progress):
        raise StateTransitionError("resolver concern progress ids must be unique")
    return indexed


def validate_concern_admission(current: ResolveState, candidate: ResolveState) -> None:
    """Allow new concerns to join a live run, and none to change or vanish.

    A concern discovered mid-run that cannot join the run that discovered
    it leaves the work for the next inventory pass, which means committing
    a note, re-deriving from scratch, and discarding every material answer
    already collected. Append-only keeps what resume integrity needs: an
    existing entry is immutable, so a resumed run reads back exactly
    what it persisted. A successor names its predecessor rather than editing
    it, which is what lets a plan be corrected without rewriting history.
    """
    existing = {concern.id: concern for concern in current.concerns}
    admitted = {concern.id: concern for concern in candidate.concerns}
    missing = sorted(
        identifier for identifier in existing if identifier not in admitted
    )
    if missing:
        raise StateTransitionError(
            "resolver concerns are append-only; dropped: " + ", ".join(missing)
        )
    altered = sorted(
        identifier
        for identifier, concern in existing.items()
        if admitted[identifier] != concern
    )
    if altered:
        raise StateTransitionError(
            "an admitted concern is immutable; supersede it instead of editing "
            "it: " + ", ".join(altered)
        )
    unknown = sorted(
        concern.supersedes
        for concern in admitted.values()
        if concern.supersedes and concern.supersedes not in admitted
    )
    if unknown:
        raise StateTransitionError(
            "a successor names no concern in this run: " + ", ".join(unknown)
        )


def validate_progress_transition(
    current: ResolveState, candidate: ResolveState
) -> None:
    """Require one legal persisted lifecycle transition per changed concern.

    A concern admitted mid-run appears here for the first time, so the
    covering check runs against the candidate alone: requiring both sides to
    cover the same set is what made admission impossible. What may never
    happen is a concern leaving the projection: that would drop recorded work.
    """
    before = progress_index(current.progress)
    after = progress_index(candidate.progress)
    concern_ids = {concern.id for concern in candidate.concerns}
    if after.keys() != concern_ids or not before.keys() <= after.keys():
        raise StateTransitionError("resolver progress must cover every concern exactly")
    for identifier, prior in before.items():
        if identifier not in after:
            raise StateTransitionError(f"concern {identifier!r} lost its progress")
        next_item = after[identifier]
        if next_item.status == prior.status:
            continue
        if next_item.status not in CONCERN_TRANSITIONS[prior.status]:
            raise StateTransitionError(
                f"concern {identifier!r} cannot move from {prior.status} "
                f"to {next_item.status}"
            )
    validate_settled_outcomes(candidate)


def validate_settled_outcomes(candidate: ResolveState) -> None:
    """Refuse a state whose progress claims a success its outcomes cannot support.

    Progress and outcome are two records of one fact, and a write that
    carries only the first leaves the run reporting work no integration can
    consume — the surfaces that count progress read the higher number while
    the outcome that would have proved it never arrives. Checked here rather
    than trusted at the one call site that settles a concern, because the
    next site added would reintroduce the skew silently.
    """
    recorded = {outcome.concern_id for outcome in candidate.outcomes}
    unsupported = sorted(
        item.concern_id
        for item in candidate.progress
        if item.status == ConcernStatus.VERIFIED and item.concern_id not in recorded
    )
    if unsupported:
        raise StateTransitionError(
            "verified concerns have no recorded outcome: " + ", ".join(unsupported)
        )


class ResolverStateRepository:
    """Persist a complete state and its stable operational projections."""

    def __init__(self, state_root: Path, run_id: str) -> None:
        self.root = state_root / run_id

    def exists(self) -> bool:
        return (self.root / "state.json").exists()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold one process-wide run lease, released automatically after a crash."""
        with try_exclusive(self.root / ".run.lock") as held:
            if not held:
                raise StateTransitionError(
                    f"resolver run {self.root.name!r} is already active"
                )
            yield

    def held(self) -> bool:
        """Whether another process is driving this run right now.

        Asked of the lock rather than of the process table, because under a
        sandbox `/proc` is PID-isolated and `ps` lists nothing outside the
        current shell — so a healthy long-running run is indistinguishable
        from one that died, and that ambiguity has produced a confident
        wrong conclusion in both directions. Taking the lock and dropping it
        answers from the run directory alone, which is the only thing a
        reader is guaranteed to be able to see.
        """
        lock_path = self.root / ".run.lock"
        if not lock_path.exists():
            return False
        with try_exclusive(lock_path) as free:
            return not free

    def load(self) -> ResolveState:
        path = self.root / "state.json"
        if not path.exists():
            raise FileNotFoundError(f"resolver state does not exist: {path}")
        try:
            return ResolveState.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as error:
            raise StateCorruptionError(
                f"resolver state at {path} cannot be decoded; restore the file "
                "or remove the run directory to start over"
            ) from error

    def accept(self, acceptance: VerificationAcceptance) -> ResolveState:
        """Record that a human accepts one concern over one failing check.

        Written through its own door for the same reason adoption is: a run
        that is parked has no process to route this through, and `save`
        guards transitions this is not one of. Re-accepting the same pair
        replaces the reason rather than accumulating, so a corrected reason
        reads as the decision rather than beside it.
        """
        current = self.load()
        kept = [
            recorded
            for recorded in current.acceptances
            if (recorded.concern_id, recorded.verification)
            != (acceptance.concern_id, acceptance.verification)
        ]
        accepted = current.model_copy(update={"acceptances": [*kept, acceptance]})
        self.write_model("state.json", accepted)
        return accepted

    def retire(self, retirement: ConcernRetirement) -> ResolveState:
        """Retire atomically with respect to every scheduler save."""
        with self.writing():
            return self.retire_locked(retirement)

    def retire_locked(self, retirement: ConcernRetirement) -> ResolveState:
        """Record that a human settled one concern somewhere other than here.

        Through its own door, like acceptance and adoption, and for the same
        reason: a parked run has no process to route the decision through.
        The concern leaves the eligible set without failing — its record
        says it was retired and by what, rather than that its work did not
        hold up — and its dependents proceed from the base, which is where
        the work that settled it now lives.

        Retiring the same concern twice replaces the reason rather than
        accumulating one, so a corrected reason reads as the decision.
        """
        current = self.load()
        known = {concern.id for concern in current.concerns}
        if retirement.concern_id not in known:
            raise StateTransitionError(
                f"concern {retirement.concern_id!r} is not in this run"
            )
        settled = [
            item.status
            for item in current.progress
            if item.concern_id == retirement.concern_id and already_settled(item.status)
        ]
        if settled:
            raise StateTransitionError(
                f"concern {retirement.concern_id!r} is already {settled[0]}; "
                "retiring claims a concern is settled elsewhere, and this one "
                "is settled here"
            )
        kept = [
            recorded
            for recorded in current.retirements
            if recorded.concern_id != retirement.concern_id
        ]
        # The lease stops being active so nothing re-opens on it, and its
        # worktree is left where it stands. A door cannot safely remove a
        # tree a live run may be holding, and the branch is evidence besides:
        # a retired concern often built its own answer to work that landed
        # upstream, and that answer is worth reading before it is discarded.
        retired = current.model_copy(
            update={
                "retirements": [*kept, retirement],
                "leases": [
                    lease.model_copy(update={"active": False})
                    if lease.concern_id == retirement.concern_id
                    else lease
                    for lease in current.leases
                ],
                "cleanup": [
                    *current.cleanup,
                    *[
                        CleanupRecord(
                            path=lease.root,
                            branch=lease.branch,
                            action="retained",
                            reason=f"lease retained after retirement: {retirement.reason}",
                        )
                        for lease in current.leases
                        if lease.concern_id == retirement.concern_id
                    ],
                ],
                "progress": [
                    item.model_copy(
                        update={
                            "status": ConcernStatus.RETIRED,
                            "reason": retirement.reason,
                        }
                    )
                    if item.concern_id == retirement.concern_id
                    else item
                    for item in current.progress
                ],
            }
        )
        self.write_model("state.json", retired)
        return retired

    def adopt(self, config: ResolverConfig, digest: str) -> ResolveState:
        """Re-stamp a persisted run onto a composition a human accepted.

        `save` holds the composition immutable, which is what stops a run
        drifting under itself between resumes. Adoption is the one change to
        it anybody sanctions, so it is written through its own door rather
        than by loosening that guard for every path that saves — including
        the ones a run takes while work is in flight.
        """
        adopted = self.load().model_copy(
            update={"config": config, "config_digest": digest}
        )
        self.write_model("state.json", adopted)
        return adopted

    @contextmanager
    def writing(self) -> Iterator[None]:
        """Serialize short state transactions while a run keeps its long lease."""
        with exclusive(self.root / ".state.lock"):
            yield

    def save(self, state: ResolveState) -> ResolveState:
        with self.writing():
            return self.save_locked(state)

    def save_locked(self, state: ResolveState) -> ResolveState:
        if state.run_id != self.root.name:
            raise StateTransitionError(
                f"state run id {state.run_id!r} does not match {self.root.name!r}"
            )
        if self.exists():
            current = self.load()
            state = preserve_retirements(current, state)
            if (
                current.phase != ResolvePhase.INTEGRATION
                and state.phase == ResolvePhase.INTEGRATION
                and any(
                    item.id not in state.admitted_requests
                    for item in AdmissionMailbox(self.root).pending()
                )
            ):
                raise ResolverAdmissionsPending()
            if (
                state.source != current.source
                or state.spec != current.spec
                or state.config_digest != current.config_digest
            ):
                raise StateTransitionError(
                    "resolver source, specification, and configuration are immutable"
                )
            if state.concerns[: len(current.concerns)] != current.concerns:
                raise StateTransitionError(
                    "a recorded resolver concern is immutable; a concern "
                    "discovered later joins the run beside them"
                )
            validate_concern_admission(current, state)
            validate_progress_transition(current, state)
            resumed = (
                current.phase == ResolvePhase.FAILED
                and current.resume_from is not None
                and state.phase == current.resume_from
            )
            terminal = state.phase in {ResolvePhase.FAILED, ResolvePhase.ABORTED}
            next_phase = (
                PHASE_TRANSITIONS[current.phase]
                if current.phase in PHASE_TRANSITIONS
                else None
            )
            if (
                state.phase != current.phase
                and not resumed
                and not terminal
                and state.phase != next_phase
            ):
                raise StateTransitionError(
                    f"resolver phase cannot move from {current.phase} to {state.phase}"
                )
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in ["agents", "reviews", "integration"]:
            (self.root / directory).mkdir(exist_ok=True)
        self.write_model("state.json", state)
        self.write_model("concerns.json", ConcernsDocument(concerns=state.concerns))
        self.write_model(
            "questions.json",
            state.questions or QuestionBatch(run_id=state.run_id, questions=[]),
        )
        self.write_model(
            "answers.json",
            state.answers or AnswerBatch(run_id=state.run_id, answers=[]),
        )
        self.write_model("leases.json", LeasesDocument(leases=state.leases))
        self.write_model("bases.json", BasesDocument(bases=state.bases))
        return state

    def queue_admission(self, request: AdmissionRequest) -> AdmissionReceipt:
        """Accept evidence atomically with the run's integration cutoff."""
        with self.writing():
            state = self.load()
            reached = (
                state.resume_from or state.phase
                if state.phase == ResolvePhase.FAILED
                else state.phase
            )
            if (
                state.phase in {ResolvePhase.ABORTED, ResolvePhase.COMPLETE}
                or PHASE_ORDER[reached] >= PHASE_ORDER[ResolvePhase.INTEGRATION]
            ):
                raise StateTransitionError(
                    f"run has reached {reached}; admission is available only before integration"
                )
            return AdmissionMailbox(self.root).submit(request)

    def write_agent_round(self, state: ResolveState) -> None:
        """Write each complete round independently for inspection and resumption."""
        for outcome in state.outcomes:
            for round_record in outcome.rounds:
                self.write_round(round_record)

    def write_round(self, round_record: AgentRound) -> None:
        """Persist one complete worker/reviewer round before the next transition."""
        identifier = round_record.concern_id
        self.write_model(
            f"agents/{identifier}-round-{round_record.round}.json",
            round_record.worker,
        )
        self.write_model(
            f"reviews/{identifier}-round-{round_record.round}.json",
            round_record.review,
        )
        # The whole round as well as its halves. The halves are what a human
        # reads; this is what a resume re-enters from, and splitting a round
        # across two documents loses the diff that joins them.
        self.write_model(
            f"rounds/{identifier}-round-{round_record.round}.json", round_record
        )

    def rounds_for(self, concern_id: str) -> list[AgentRound]:
        """Every round this concern completed, in the order it took them.

        A concern interrupted mid-flight and re-entered at round one with
        its feedback discarded meets a branch carrying the rounds it
        already committed — so the worker meets its own work with no record of
        why the reviewer sent it back, and the review that produced that
        record is spent for nothing.
        """
        directory = self.root / "rounds"
        if not directory.is_dir():
            return []
        rounds = [
            record
            for path in sorted(directory.glob(f"{concern_id}-round-*.json"))
            # The glob is a prefix match, so a concern whose id ends in
            # `-round-<n>` would collect its neighbour's files. The record
            # names its own concern, so that is what decides.
            if (
                record := AgentRound.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            ).concern_id
            == concern_id
        ]
        return sorted(rounds, key=lambda record: record.round)

    def write_model(self, relative: str, value: PersistedResolverModel) -> None:
        publish_atomic(self.root / relative, value)


def held_leases(state_root: Path) -> Iterator[HeldLease]:
    """Each branch a resolver run answers for — held, or left behind by it.

    A lease looks exactly like abandoned work to a branch survey: commits the
    integration branch lacks, and no pull request driving them. So a sweep
    offers to land it individually or drop it, and either answer destroys
    something — landing bypasses the join machinery the run is partway
    through, dropping deletes a parked run's work outright. The run directory
    is the only thing that can tell a lease from an abandoned branch, so it is
    what gets asked.

    A run whose state cannot be read holds nothing here, and says so in the
    log. The alternative is a survey that any unreadable resolver directory
    takes down with it.

    Failing is not finishing. A run that died mid-flight still has its
    branches out on lease, and the hazard above is if anything sharper then:
    the join machinery it was partway through never ran, and nobody has come
    back for the work.

    Completion releases a lease but does not dispose of the branch, and the
    two are not the same fact: a run reaches ``COMPLETE`` by finishing its
    own work, not by getting its batch onto the integration branch. So a
    branch that survives a completed run is reported here too. Cleanup
    deletes the branches it managed to, and those never match a survey's
    branch list; the ones left are exactly the leftovers worth a decision,
    and reporting them as one run is what keeps a sweep from meeting them
    as unrelated abandoned work carrying the whole batch's commit count.
    """
    if not state_root.is_dir():
        return
    for run in sorted(state_root.iterdir()):
        repository = ResolverStateRepository(state_root, run.name)
        if not repository.exists():
            continue
        try:
            state = repository.load()
        except (StateCorruptionError, OSError, ValidationError):
            logger.exception("resolver run %s could not be read", run.name)
            continue
        progress = {record.concern_id: record.status for record in state.progress}
        leftover = state.phase.released_leases()
        for lease in state.leases:
            if lease.active or leftover:
                yield HeldLease(
                    branch=lease.branch,
                    run_id=state.run_id,
                    # A run that died reports that, rather than the per-concern
                    # status it froze at: the phase is what tells a reader the
                    # branch is waiting on salvage and not on a working run.
                    standing=state.phase
                    if state.phase.terminal()
                    else progress[lease.concern_id]
                    if lease.concern_id in progress
                    else state.phase,
                    alive=not state.phase.terminal(),
                )


def live_lease_branches(state_root: Path) -> dict[str, HeldLease]:
    """Every held lease, by the branch a survey will meet it as."""
    return {held.branch: held for held in held_leases(state_root)}
