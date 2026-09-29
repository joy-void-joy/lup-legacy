"""One provider-neutral state machine every operation passes through.

The adapters normalize, render, and deliver placement; the *decisions* happen
here, once. That split is what makes "the same operation through Claude and
Codex" checkable — a state machine reproduced per provider is two state
machines, and the second one is always the one nobody re-read.

The ordering is the design, and two steps in it are load-bearing:

**A question parks without a lease.** An independent review requirement is put
to somebody before anything is captured or locked, because a lock held across
a person's attention is a lock held for minutes and the rest of the session
needs it. Preparation happens after the answer, not before.

**Settlement runs twice.** Preliminary settlement reads what is known before
anything is measured and is what reaches the question; dynamic settlement runs
the same order with the capture actually taken. What differs is the evidence,
which is why neither pass can apply a rule the other does not, and why a
capture that failed keeps the question rather than quietly discharging it.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from lup.policy.boundary import BoundaryPreflight
from lup.policy.checkpoints import Checkpoint, RecoveryCoordinator, WorktreeLease
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.escalation import EscalationRequest
from lup.policy.kernel.settlement import SettlementFacts, settle
from lup.policy.models import Decision
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion, QuestionRelay, SupervisorChain


def changed_clause(arrived: Operation, approved: Operation) -> str:
    """Which approved facts this operation no longer matches, named.

    The comparison the fingerprint collapses into one digest, re-run part by
    part so the refusal says what moved: the reviewer approved an exact
    operation, and "its targets no longer match" is answerable where "its
    arguments, directory, targets, or placement" is a list to go check. The
    part names are the fingerprint material's own keys, chosen to be read
    here. The generic list survives as the fallback for the one case the
    parts cannot explain — a stored digest from material this comparison no
    longer reproduces.
    """
    ours = arrived.fingerprint_parts()
    theirs = approved.fingerprint_parts()
    changed = [name for name in ours if ours[name] != theirs.get(name)]
    if not changed:
        return (
            "its arguments, directory, targets, or placement no longer"
            " match what the reviewer was shown"
        )
    spelled = ", ".join(changed)
    plural = len(changed) > 1 or spelled.endswith("s")
    verb = "no longer match" if plural else "no longer matches"
    return f"its {spelled} {verb} what the reviewer was shown"


type Stage = Literal["settled", "parked", "prepared", "refused", "finished"]
"""Where one pass through the coordinator stopped.

Named separately from the question lifecycle because they answer different
things: a question's state is what a reviewer sees, and this is what the
caller does next. An operation can be ``settled`` with no question at all.
"""


class CoordinatorResult(BaseModel, frozen=True):
    """What one pass produced, and what the caller does with it.

    The decision is always present, because even a parked operation has a
    settled verdict — parking is what the verdict said to do. The question and
    the checkpoint are present only where this pass created one, so a caller
    reading them is reading what happened rather than what usually happens.
    """

    stage: Stage
    decision: Decision
    question: PersistentQuestion | None = None
    checkpoint: Checkpoint | None = None
    note: str = ""
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def executable(self) -> bool:
        """Whether the caller may run the operation now."""
        return self.stage in ("settled", "prepared") and self.decision.effect == "allow"


# lup: defer: only tests construct this, `RecoveryCoordinator` and
# `WorktreeLease` in `lup.policy.checkpoints`, or `AuditLog` in
# `lup.policy.audit`, and the host executor they would hand work to has no
# transport (the catalog declares `hostexec` capability-blocked); neither
# dispatcher runs through them. Wire them in or delete them -- the user's call
class OperationCoordinator:
    """Owns the lifecycle: settle, park, revalidate, capture, settle again.

    Provider adapters do not reproduce any of this. They normalize a call into
    an :class:`~lup.policy.operations.Operation`, render whatever this returns,
    enforce true defer, and deliver the placement mechanism their provider
    supports.
    """

    relay: QuestionRelay
    recovery: RecoveryCoordinator
    preflight: BoundaryPreflight
    chain: SupervisorChain

    def __init__(
        self,
        relay: QuestionRelay,
        recovery: RecoveryCoordinator,
        preflight: BoundaryPreflight,
        chain: SupervisorChain,
    ) -> None:
        self.relay = relay
        self.recovery = recovery
        self.preflight = preflight
        self.chain = chain

    def facts(
        self,
        decision: KernelDecision,
        escalation: EscalationRequest | None,
        checkpoint: Checkpoint | None,
        reviewable: bool,
        hint: str,
    ) -> SettlementFacts:
        """The session's own answers, read from the boundary rather than guessed.

        Every one of these is measured somewhere else and passed here, which
        is what stops the settlement order from consulting the filesystem: a
        row that read the world would settle differently on two machines with
        the same declaration.
        """
        boundary = self.preflight.boundary
        return SettlementFacts(
            decision,
            escalation=escalation,
            contained=boundary.contained,
            inside_placement=self.preflight.delivered("inside_placement"),
            host_executor=self.preflight.delivered("host_executor"),
            human_execution=reviewable,
            reviewable=reviewable,
            checkpoint=checkpoint.evidence() if checkpoint else "absent",
            unjudged_ambient=boundary.unjudged_ambient,
            hint=hint,
        )

    def preliminary(
        self,
        operation: Operation,
        classified: KernelDecision,
        escalation: EscalationRequest | None = None,
        reviewable: bool = True,
        hint: str = "",
    ) -> CoordinatorResult:
        """Settle what is known before anything is captured or locked.

        A refusal ends here — there is nothing to prepare for an operation
        that will not run. A question parks here, holding no lease, because
        the point of parking early is that the rest of the session keeps
        working while somebody reads it.
        """
        decision = settle(self.facts(classified, escalation, None, reviewable, hint))
        settled = Decision.of(decision)
        if decision.effect == "deny":
            return CoordinatorResult(stage="refused", decision=settled)
        if decision.effect != "ask":
            return CoordinatorResult(stage="settled", decision=settled)
        return CoordinatorResult(
            stage="parked",
            decision=settled,
            question=self.park(operation, decision, escalation),
        )

    def park(
        self,
        operation: Operation,
        decision: KernelDecision,
        escalation: EscalationRequest | None,
    ) -> PersistentQuestion:
        """Write the question down before anybody sees it.

        Eligibility is resolved from the authenticated chain at parking time
        and stored, so who may answer is a fact about the moment the question
        was asked. Resolved at answering time instead, a chain that changed
        mid-run would silently change who could approve something already
        pending.
        """
        return self.relay.record(
            PersistentQuestion(
                id=f"{operation.id}:{operation.fingerprint()[:12]}",
                operation=operation,
                fingerprint=operation.fingerprint(),
                reason=decision.reason,
                rule=decision.rule,
                purpose=decision.purpose,
                requirement=decision.reviewer,
                eligible=self.chain.eligible(operation.requester, decision.reviewer),
                escalation=escalation.reason if escalation else "",
            )
        )

    def prepare(
        self,
        operation: Operation,
        classified: KernelDecision,
        precious: list[Path],
        escalation: EscalationRequest | None = None,
        reviewable: bool = True,
        hint: str = "",
    ) -> CoordinatorResult:
        """Take the lease, capture, and settle again on what was measured.

        The lease is held across capture and execution and released by the
        caller's context, never across a question: a checkpoint failure that
        creates one is settled here, and the question it produces is parked by
        the next preliminary pass with nothing held.
        """
        lease = WorktreeLease(operation.worktree, operation.session)
        if not lease.acquire():
            return CoordinatorResult(
                stage="refused",
                decision=Decision.of(
                    classified.revised(
                        effect="deny",
                        reason=(
                            "another operation holds this worktree's mutation"
                            f" lease ({lease.held() or 'holder unrecorded'})"
                        ),
                        cause="capability",
                        capability="checkpoint_store",
                    )
                ),
                note="the lease serializes captures, so this one waits rather"
                " than capturing over another",
            )
        try:
            checkpoint = self.recovery.capture(
                operation.id, operation.mutations, precious
            )
            decision = settle(
                self.facts(classified, escalation, checkpoint, reviewable, hint)
            )
        finally:
            lease.release()
        if decision.effect == "ask":
            return CoordinatorResult(
                stage="parked",
                decision=Decision.of(decision),
                checkpoint=checkpoint,
                question=self.park(operation, decision, escalation),
                note=checkpoint.failure,
            )
        return CoordinatorResult(
            stage="prepared" if decision.effect == "allow" else "refused",
            decision=Decision.of(decision),
            checkpoint=checkpoint,
        )

    def resume(self, question: str, operation: Operation) -> CoordinatorResult:
        """Revalidate an answered question against the operation now in hand.

        The requesting agent does not reconstruct the call — it is carried in
        the record — but the operation *arriving* here is revalidated anyway,
        because the two can differ: a resumption assembled from a stale
        payload, a worktree that moved, a target that resolved differently.
        Any difference is a fresh question rather than a stale approval.
        """
        entry = self.relay.find(question)
        if entry is None:
            raise ValueError(f"no question {question!r} is recorded")
        if entry.state == "rejected":
            note = entry.answer.note if entry.answer else ""
            return CoordinatorResult(
                stage="refused",
                decision=Decision.of(
                    KernelDecision(
                        "deny", entry.reason, cause="deliberate", rule=entry.rule
                    )
                ),
                question=entry,
                note=note,
            )
        if entry.state != "approved":
            return CoordinatorResult(
                stage="refused",
                decision=Decision.of(
                    KernelDecision(
                        "deny",
                        f"question {question!r} is {entry.state},"
                        " so nothing is authorized",
                        cause="deliberate",
                        rule=entry.rule,
                    )
                ),
                question=entry,
            )
        if operation.fingerprint() != entry.fingerprint:
            return CoordinatorResult(
                stage="refused",
                decision=Decision.of(
                    KernelDecision(
                        "deny",
                        "the operation changed after it was approved:"
                        f" {changed_clause(operation, entry.operation)}",
                        cause="deliberate",
                        rule=entry.rule,
                    )
                ),
                question=entry,
            )
        return CoordinatorResult(
            stage="prepared",
            decision=Decision.of(
                KernelDecision(
                    "allow",
                    entry.reason,
                    operation.placement,
                    rule=entry.rule,
                    evaluator="relay",
                )
            ),
            question=entry,
            note=entry.answer.note if entry.answer else "",
        )
