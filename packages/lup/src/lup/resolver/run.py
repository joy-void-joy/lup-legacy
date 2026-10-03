"""The one live resolve state a run holds, and how it is written down.

Every phase of a resolve reads this state and most of them write it, which
is the pull toward putting them in one class: reaching the state means holding
whatever else that class has. Naming the state itself — with the lock that
guards it, the event that ends a poll early, and the observer that watches
it move — lets a phase take *this* instead of taking the composer.

Persisting is not a plain write. A resumed run re-enters completed stages
whose persisted evidence makes them no-ops, so the recorded phase is a
monotonic high-water mark: a re-entered stage may not move it backward.
Failure recording and explicit failed-state resumption are the only
non-forward moves, and both stay owned by the repository's ``save``.
"""

import asyncio

from lup.channels.models import utc_now
from lup.resolver.contracts import ResolverObserver
from lup.resolver.record import (
    ConcernProgressedEvent,
    Journal,
    PhaseChangedEvent,
)
from lup.resolver.models import (
    SETTLED_STATUSES,
    ConcernOutcome,
    ConcernStatus,
    DependencyBase,
    ResolvePhase,
    ResolveState,
)
from lup.resolver.state import (
    PHASE_ORDER,
    ResolverStateRepository,
    preserve_retirements,
)
from lup.resolver.contracts import ResolverConcernRetired


class ResolverInvariantError(RuntimeError):
    """A resolver invariant was violated by its own composition or inputs."""


class ResolveRun:
    """The state one resolve run holds, with everything that guards it."""

    def __init__(
        self,
        repository: ResolverStateRepository,
        journal: Journal,
        observer: ResolverObserver | None = None,
    ) -> None:
        self.repository = repository
        self.journal = journal
        self.observer = observer
        self.state: ResolveState | None = None
        self.lock = asyncio.Lock()
        """Taken by every writer of the state below."""
        self.wake = asyncio.Event()
        """Set whenever recorded answers change, so a poll can end early."""

    def require(self) -> ResolveState:
        """The state, or a failure naming the invariant that was broken."""
        if self.state is None:
            raise ResolverInvariantError("resolver state is not initialized")
        if self.repository.exists():
            self.state = preserve_retirements(self.repository.load(), self.state)
        return self.state

    def require_active(self, concern_id: str) -> None:
        """Stop retired work at an owned boundary, retaining its tree as evidence."""
        if any(item.concern_id == concern_id for item in self.require().retirements):
            raise ResolverConcernRetired(f"concern {concern_id!r} was retired")

    def persist(self, state: ResolveState) -> None:
        """Persist while keeping the phase a monotonic high-water mark."""
        current = self.state
        if (
            current is not None
            and ResolvePhase.FAILED not in {current.phase, state.phase}
            and PHASE_ORDER[state.phase] < PHASE_ORDER[current.phase]
        ):
            state = state.model_copy(update={"phase": current.phase})
        # Only after the save, so a rejected candidate cannot stay behind as
        # what this believes. A failure handler reads this back to record the
        # phase to resume from, so a poisoned copy makes persisting the
        # failure fail the same way the failure did — and the run reports the
        # second raise, from inside its own handler, instead of the first.
        state = self.repository.save(state)
        self.state = state
        self.emit_transitions(current, state)

    def emit_transitions(
        self, previous: ResolveState | None, state: ResolveState
    ) -> None:
        """Report only durably saved phase and concern changes.

        The journal takes the same transitions the observer does, so a page
        following the record sees state moves interleaved with the turns
        that caused them rather than having to correlate two sources.
        """
        if previous is None or previous.phase != state.phase:
            self.journal.record(PhaseChangedEvent(phase=state.phase))
            if self.observer is not None:
                self.observer.phase_changed(state.phase)
        before = (
            {item.concern_id: item for item in previous.progress}
            if previous is not None
            else {}
        )
        for item in state.progress:
            prior = before[item.concern_id] if item.concern_id in before else None
            if prior == item:
                continue
            self.journal.record(ConcernProgressedEvent(progress=item))
            if self.observer is not None:
                self.observer.concern_changed(item)
        if self.observer is not None:
            tally = state.tally()
            if previous is None or previous.tally() != tally:
                self.observer.tally_changed(tally)

    def progress_state(
        self,
        state: ResolveState,
        concern_ids: list[str],
        status: ConcernStatus,
        reason: str = "",
    ) -> ResolveState:
        """Return one state with the selected concern transitions applied.

        A concern moving into a settled status is stamped with the moment it
        landed, unless it already carries one from an earlier settlement.
        Stamped where the transition is applied rather than where the state
        is written, because the in-memory copy is what the observer reads to
        draw its bar: a stamp added at the write boundary would reach the
        file and never the surface watching it move.
        """
        selected = dict.fromkeys(concern_ids)
        landed = utc_now() if status in SETTLED_STATUSES else None
        return state.model_copy(
            update={
                "progress": [
                    item.model_copy(
                        update={
                            "status": status,
                            "reason": reason,
                            "settled_at": item.settled_at or landed,
                        }
                    )
                    if item.concern_id in selected
                    else item
                    for item in state.progress
                ]
            }
        )

    async def transition_concern(
        self, concern_id: str, status: ConcernStatus, reason: str = ""
    ) -> None:
        """Persist one concern transition without losing parallel sibling updates."""
        async with self.lock:
            self.require_active(concern_id)
            state = self.require()
            self.persist(self.progress_state(state, [concern_id], status, reason))

    async def settle_concern(
        self, outcome: ConcernOutcome, status: ConcernStatus, reason: str = ""
    ) -> None:
        """Persist one concern's terminal transition and its outcome together.

        Progress and outcome are two records of the same fact, and written
        apart they let an interruption land between: the run would then claim
        a success it cannot integrate, because every surface that counts
        progress reads the higher number while the batch that would gather
        the outcome never returns. Recorded under one lock, the two cannot
        disagree.

        Replace-or-append by concern id, because a resumed run re-executes a
        concern whose outcome was already written and must overwrite that
        record rather than shadow it with a second one.
        """
        async with self.lock:
            self.require_active(outcome.concern_id)
            state = self.require()
            kept = [
                item for item in state.outcomes if item.concern_id != outcome.concern_id
            ]
            progressed = self.progress_state(
                state, [outcome.concern_id], status, reason
            )
            self.persist(progressed.model_copy(update={"outcomes": [*kept, outcome]}))

    async def record_note_clearance(
        self, base: DependencyBase, commit: str
    ) -> DependencyBase:
        """Move this concern's recorded base onto the commit that cleared its notes.

        The orchestrator strips a concern's notes as a commit of its own, so
        the tree its worker starts from is that commit and not the one the
        base was built at. Leaving the record behind bricked every resume of a
        concern that failed: with no verified commit to restore, the expected
        commit fell back to the base while HEAD sat at the clearance, and the
        invariant the resolver itself had violated raised with no CLI
        operation able to repair it.

        Recorded rather than tolerated. An invariant that accepts whatever
        HEAD says is not one, and the fact it needs was always available at
        the moment the commit was made.
        """
        if commit == base.commit:
            return base
        moved = base.model_copy(update={"commit": commit})
        async with self.lock:
            state = self.require()
            self.persist(
                state.model_copy(
                    update={
                        "bases": [
                            moved if item.concern_id == base.concern_id else item
                            for item in state.bases
                        ]
                    }
                )
            )
        return moved

    def replace_dependency_base(self, base: DependencyBase) -> None:
        """Move one recorded base's commit, keeping its dependency shape.

        Not a path :meth:`record_dependency_base` can offer, because a
        concern re-deriving its own base must adopt what the run holds
        rather than overwrite it. This is the other direction: the run
        itself moved the lease, so the record follows the lease.
        """
        state = self.require()
        self.persist(
            state.model_copy(
                update={
                    "bases": [
                        base if item.concern_id == base.concern_id else item
                        for item in state.bases
                    ]
                }
            )
        )

    async def record_dependency_base(self, base: DependencyBase) -> DependencyBase:
        """Persist one dependency base, or adopt the one already recorded.

        A base is immutable in its dependency shape, not in its commit:
        `record_note_clearance` advances the commit by design. So a concern
        retried after an interruption re-derives the pre-clearance commit and
        must adopt what this run recorded, rather than read its own clearance
        as the base moving underneath it — which failed every concern that had
        a note to clear, leaving only admitted ones able to resume.
        """
        async with self.lock:
            state = self.require()
            existing = next(
                (
                    candidate
                    for candidate in state.bases
                    if candidate.concern_id == base.concern_id
                ),
                None,
            )
            if existing is not None:
                if existing.model_copy(update={"commit": base.commit}) != base:
                    raise ResolverInvariantError(
                        f"dependency base changed for {base.concern_id}"
                    )
                return existing
            self.persist(state.model_copy(update={"bases": [*state.bases, base]}))
            return base
