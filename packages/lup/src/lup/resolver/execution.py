"""Taking one concern from its dependency base to a verified commit.

This is the run's inner loop, and every concern in a topological batch is in
it at once. It builds the tree the concern starts from, strips the concern's
own review markers so the spec is the whole of the feedback, and then spends
bounded rounds of worker-then-reviewer until the work is accepted or the
allowance is gone. A question the worker could not answer for itself parks
the concern rather than failing it, so the answer lands on a run that still
holds everything it had.

Acceptance is checked against what was declared, not against what the
reviewer says it checked: a report that accepts while leaving a persisted
criterion unaccounted for is turned back with the exact ids named, because
the alternative is a concern that passes on criteria nobody wrote down.

An id from outside the list is the opposite case and is only recorded. It
leaves every declared criterion as accounted for as it was, and the
reviewer reads its criteria beside the answered questions, so crediting a
question id is the slip that shape invites. Charging a revision round for
it cost a run its budget re-deriving an acceptance it already had.
"""

from collections.abc import Callable

from lup.coordination.refs import ActorRef
from lup.resolver.contracts import (
    ResolverAwaitingAnswers,
    ResolverConcernRetired,
    ResolverDrained,
    ResolverEnvironmentFault,
)
from lup.resolver.joins import Joiner
from lup.resolver.record import (
    CriteriaCarriedEvent,
    ForeignCriteriaEvent,
    Journal,
    ReviewResidualEvent,
    VerificationFailedEvent,
)
from lup.resolver.models import (
    AgentRound,
    Concern,
    ConcernExecution,
    ConcernOutcome,
    ConcernStatus,
    MaterialQuestion,
    ResidualRuling,
    ResolverConfig,
    ReviewReport,
    WorkAssignment,
    WritableRootLease,
)
from lup.resolver.orchestrator import (
    DependencyBaseBuilder,
    WorktreeOrchestrator,
    WritableRootLeases,
)
from lup.resolver.questions import QuestionBroker
from lup.resolver.run import ResolveRun
from lup.resolver.state import ResolverStateRepository
from lup.resolver.turns import TurnRunner
from lup.resolver.verification import Verifier, rejection_reason
from lup.sessions.errors import TurnError


class ConcernExecutor:
    """One concern's revision loop, from dependency base to verified commit."""

    def __init__(
        self,
        config: ResolverConfig,
        run: ResolveRun,
        runner: TurnRunner,
        questions: QuestionBroker,
        joiner: Joiner,
        verifier: Verifier,
        worktrees: WorktreeOrchestrator,
        leases: WritableRootLeases,
        repository: ResolverStateRepository,
        journal: Journal,
        environmental_fault: Callable[[str], bool] = lambda _: False,
    ) -> None:
        self.config = config
        self.run = run
        self.runner = runner
        self.questions = questions
        self.joiner = joiner
        self.verifier = verifier
        self.worktrees = worktrees
        self.leases = leases
        self.repository = repository
        self.journal = journal
        self.environmental_fault = environmental_fault
        """Whether a failure's own words name the host rather than the work.

        Asked of the message rather than read off `TurnFailure.environmental`
        alone, because the flag is set where an exception is first caught and
        several layers above re-wrap a raw exception into a fresh failure —
        the message survives that, the flag does not. A run whose whole batch
        died on one session limit was recorded as six concerns failing for
        exactly this reason, with the classifier working and its answer
        discarded two frames up.

        Defaults to answering no, so a library with no adapter attributes a
        fault to the work — the conservative direction, since treating a real
        failure as the host's would retry it forever.
        """

    async def settled_actors(
        self, concern_id: str, summary: str = "", error: str = ""
    ) -> None:
        """Let go of the worker that carried one concern, once it has settled.

        The worker only. Its lease work is over and nothing consults it
        again, so holding its session past here costs a conversation for no
        reader. The reviewer is not done at the same moment: a join asks it
        again, over the merged tree, whether the criteria it accepted still
        hold — and retiring it here would make every such re-check open a fresh
        session, which is the reviewer re-deriving cold what it had just
        judged. That path already carries the concern record for the times a
        session genuinely cannot survive; making that the normal case spends
        a turn on every concern to save one session.

        Only where the concern is actually settled, either. A parked or
        drained concern keeps its worker too, because what resumes reattaches
        to that conversation rather than opening a new one — and an agent
        recorded finished while its work is merely suspended is the report
        that sends somebody looking for a failure that did not happen.
        """
        await self.runner.actors.finish(
            ActorRef(kind="worker", id=concern_id), summary=summary, error=error
        )

    async def execute_concern(
        self,
        concern: Concern,
        lease: WritableRootLease,
        commits: dict[str, str],  # lup: ignore[dict-str-payload] — concern-id index
        builder: DependencyBaseBuilder,
    ) -> ConcernExecution:
        """Execute one concern while persisting a terminal failure on exceptions."""
        try:
            execution = await self.execute_concern_inner(
                concern, lease, commits, builder
            )
            await self.settled_actors(
                concern.id,
                summary=execution.outcome.settled_summary(),
                error=execution.outcome.failure or "",
            )
            return execution
        except ResolverConcernRetired:
            await self.settled_actors(concern.id, summary="retired by the operator")
            raise
        except ResolverAwaitingAnswers:
            await self.run.transition_concern(
                concern.id,
                ConcernStatus.WAITING_FOR_ANSWERS,
                "parked on material questions",
            )
            raise
        except ResolverDrained:
            # Back to the boundary a resume re-enters from. The concern is
            # not failed and not waiting on anybody: its committed rounds
            # stand, and the only thing that did not happen is the turn this
            # would have started.
            await self.run.transition_concern(
                concern.id,
                ConcernStatus.ELIGIBLE,
                "drained at a round boundary",
            )
            raise
        except TurnError as error:
            # A host fault is not this concern's verdict. Transitioning here
            # would write `failed (401 OAuth access token has been revoked)`
            # into a record whose readers cannot tell it from work that did
            # not hold up — and the concern would then have to be re-admitted
            # by somebody who knew which reasons were environmental.
            if not (
                error.failure.environmental
                or self.environmental_fault(error.failure.message)
            ):
                await self.run.transition_concern(
                    concern.id, ConcernStatus.FAILED, str(error)
                )
                await self.settled_actors(concern.id, error=str(error))
                raise
            # An environmental fault settles nothing, so the agents stand:
            # what the host refused this run will retry, on the conversations
            # it already holds.
            raise ResolverEnvironmentFault(str(error), [concern.id]) from error
        except Exception as error:
            await self.settled_actors(concern.id, error=str(error))
            await self.run.transition_concern(
                concern.id, ConcernStatus.FAILED, str(error)
            )
            raise

    async def rule_on_residual(
        self,
        concern: Concern,
        review: ReviewReport,
        unaccounted: list[str],
        round_number: int,
    ) -> ReviewReport:
        """Put a reviewer's accept-with-a-gap to the human who set the bar.

        Turning an accept that leaves a declared criterion unaccounted for
        straight back into a rejection sends the disagreement to the
        worker. Sometimes that is right — a reviewer can accept
        without having checked. Sometimes it is the one thing the worker
        cannot act on: the reviewer had checked, found the criterion
        unreachable from inside the lease, and argued the remainder was a
        residual to carry. The worker then spends a round failing to close a
        gap the reviewer has already said no round will close, and the
        concern dies on its revision limit holding finished work.

        Whose call it is settles it. The criteria are the human's bar, so
        only they can say whether missing one still passes — which is what
        the join path already does when a criterion stops holding, for the
        same reason and in the same words.
        """
        question = MaterialQuestion(
            id=f"{concern.id}-residual-round-{round_number}",
            concern_id=concern.id,
            prompt=(
                f"The reviewer accepted {concern.id} while leaving "
                f"{', '.join(unaccounted)} unaccounted for. It says: "
                f"{review.reason}\n\nCarry that as a residual and take the "
                "acceptance, or send it back for another round?"
            ),
            choices=ResidualRuling.choices(),
            closed_choices=True,
            criteria=sorted(unaccounted),
        )
        self.questions.queue_questions([question], concern.id)
        answers = await self.questions.await_questions([question])
        carried = [
            answer
            for answer in answers.answers
            if answer.question_id == question.id
            and answer.value == ResidualRuling.CARRY
        ]
        if carried:
            self.journal.record(
                CriteriaCarriedEvent(
                    concern_id=concern.id,
                    round=round_number,
                    criteria=sorted(unaccounted),
                )
            )
            return review
        # The reviewer's own reason survives — the refusal is appended, never
        # substituted, and it names the exact ids so the next round can close
        # the gap instead of re-deriving it.
        refusal = (
            "criteria_met does not match the persisted acceptance criteria, and "
            "the human declined to carry the gap; unaccounted: "
            + ", ".join(unaccounted)
        )
        return review.model_copy(
            update={"accepted": False, "reason": review.reason + "\n\n" + refusal}
        )

    async def execute_concern_inner(
        self,
        concern: Concern,
        lease: WritableRootLease,
        commits: dict[str, str],  # lup: ignore[dict-str-payload] — concern-id index
        builder: DependencyBaseBuilder,
    ) -> ConcernExecution:
        """Build dependencies, run bounded revisions, and verify one concern."""
        self.run.require_active(concern.id)
        parent_commits = [commits[parent] for parent in concern.dependencies]
        if len(parent_commits) > 1:
            joined = await self.joiner.join_commits(
                lease,
                parent_commits,
                f"dependency base for {concern.id}",
                f"resolve: join dependencies for {concern.title}",
            )
            base = builder.build(concern, commits, joined_commit=joined)
            base = await self.run.record_dependency_base(base)
        else:
            base = builder.build(concern, commits)
            base = await self.run.record_dependency_base(base)
            if not lease.root.exists():
                self.worktrees.create(lease, base.commit)

        cleared = self.worktrees.clear_notes(lease, concern, base.commit)
        base = await self.run.record_note_clearance(base, cleared.commit)
        answers = self.questions.answers_for(concern.id)
        assignment = WorkAssignment(
            run_id=self.config.run_id,
            concern=concern,
            lease=lease,
            dependency_base=base,
            rendered_skill_invocation=self.runner.worker_invocation(),
            answers=answers,
        )
        # Re-entered rather than restarted. An interruption sending a
        # concern back to round one with its feedback discarded leaves its
        # branch carrying the rounds it already committed — so the
        # worker meets its own work with no record of why it was sent
        # back, and the review that produced that record is spent for
        # nothing. Every round is written whole before the next transition,
        # so this is a read rather than a reconstruction.
        rounds = self.repository.rounds_for(concern.id)
        feedback = (
            rounds[-1].review.reason + "\n" + "\n".join(rounds[-1].review.residual)
            if rounds
            else ""
        )
        maximum_round = self.config.max_revision_rounds + 1
        # Every attempt is a round on disk, because each one is a real worker
        # turn and its record is keyed by that number. What differs is which
        # allowance it spends: only a round the reviewer could have judged
        # counts against the revision budget.
        # Carried across the interruption with the rounds it was spent on. A
        # fresh budget would let a concern interrupted often enough revise
        # without limit, which is the bound this exists to hold.
        # Reconstructed by the rule the loop below charges by, so a resume
        # inherits the budget it would have had: a round advanced when it
        # left a commit the round before it did not hold.
        before = [base.commit, *(record.diff.commit for record in rounds)][
            : len(rounds)
        ]
        charged = sum(
            1
            for record, prior in zip(rounds, before, strict=True)
            if not record.diff.declaration
            and record.diff.commit is not None
            and record.diff.commit != prior
        )
        attempts = maximum_round + self.config.max_declaration_attempts
        for round_number in range(len(rounds) + 1, attempts + 1):
            # Before the turn rather than after it: everything the previous
            # round produced is committed by `validate_and_commit`, so this
            # is the point where stopping costs nothing at all.
            drain = self.questions.draining()
            if drain is not None:
                raise ResolverDrained(drain.reason, [concern.id])
            await self.run.transition_concern(concern.id, ConcernStatus.RUNNING)
            # The commit a turn is measured from is the lease's own head at the
            # moment the turn opens, read rather than carried. A carried value
            # is derived from the clearance while the check compares against
            # the worktree, so the two can disagree the instant anything but
            # this loop advances the branch — a concern resumed in a second
            # process re-entered at the clearance and failed for the commit the
            # first process had itself made.
            # A round that advances nothing is measured from the concern's
            # own base instead, because `round_base..round_base` is empty and
            # a reviewer handed it can only accept vacuously or reject for
            # having no content. It happens whenever a worker re-enters
            # finished work — the branch already holds the delivery, so there
            # is nothing left to commit and the whole branch is the answer.
            round_base = self.worktrees.head(lease)
            worker = await self.runner.worker_turn(
                assignment, feedback, round_number, round_base != base.commit
            )
            self.run.require_active(concern.id)
            outstanding = await self.questions.unanswered_for(concern.id)
            if outstanding:
                raise ResolverAwaitingAnswers(outstanding, [])
            await self.run.transition_concern(concern.id, ConcernStatus.VALIDATING)
            diff = self.worktrees.validate_and_commit(
                concern, worker, lease, round_base, self.leases, base.commit
            )
            if not diff.valid or diff.commit is None:
                review = ReviewReport(
                    concern_id=concern.id,
                    accepted=False,
                    generalized=False,
                    reason=diff.reason,
                )
            else:
                await self.run.transition_concern(concern.id, ConcernStatus.REVIEWING)
                # Verification ran exactly once in a run, over the fully
                # integrated tree, so a concern could reach VERIFIED with a
                # red suite and the breakage surfaced after every join with
                # nothing to attribute it to.
                # A failure a human has accepted for this concern is not a
                # verdict the worker can act on: it accepted one it had
                # already judged unfixable from inside the lease, and
                # rejecting again would spend a revision round to reach the
                # same place.
                accepted = [
                    acceptance.verification
                    for acceptance in self.run.require().acceptances
                    if acceptance.concern_id == concern.id
                ]
                broke = [
                    record
                    for record in self.verifier.verify(lease.root, base.commit)
                    if not record.passed and record.name not in accepted
                ]
                for record in broke:
                    self.journal.record(
                        VerificationFailedEvent(
                            concern_id=concern.id,
                            round=round_number,
                            name=record.name,
                            exit_code=record.exit_code,
                            output=record.output,
                        )
                    )
                review = (
                    ReviewReport(
                        concern_id=concern.id,
                        accepted=False,
                        generalized=False,
                        reason=rejection_reason(broke),
                    )
                    if broke
                    else await self.runner.review_turn(
                        concern,
                        worker,
                        base.commit if diff.commit == round_base else round_base,
                        diff.commit,
                        lease.root,
                        round_number,
                    )
                )
                declared = {criterion.id: True for criterion in concern.criteria}
                met = {identifier: True for identifier in review.criteria_met}
                unaccounted = [
                    identifier for identifier in declared if identifier not in met
                ]
                undeclared = [
                    label for label in review.criteria_met if label not in declared
                ]
                if review.accepted and undeclared:
                    # Recorded, never charged. An id from outside the list
                    # leaves every declared criterion exactly as accounted
                    # for as it was, so the verdict is unaffected and the
                    # correction turn upstream has already had its try.
                    self.journal.record(
                        ForeignCriteriaEvent(
                            concern_id=concern.id,
                            round=round_number,
                            labels=undeclared,
                        )
                    )
                if review.accepted and unaccounted:
                    review = await self.rule_on_residual(
                        concern, review, unaccounted, round_number
                    )
            rounds.append(
                AgentRound(
                    concern_id=concern.id,
                    round=round_number,
                    worker=worker,
                    diff=diff,
                    review=review,
                )
            )
            self.repository.write_round(rounds[-1])
            if review.accepted and diff.commit is not None:
                if review.residual:
                    # A residual on a rejection re-enters the worker's
                    # feedback below; on an acceptance nothing else carries it,
                    # and this run's residuals carried real findings.
                    self.journal.record(
                        ReviewResidualEvent(
                            concern_id=concern.id,
                            round=round_number,
                            residual=list(review.residual),
                        )
                    )
                verified = ConcernOutcome(
                    concern_id=concern.id,
                    branch=lease.branch,
                    commit=diff.commit,
                    head=diff.commit,
                    verified=True,
                    rounds=rounds,
                    notes_cleared=cleared.clearance.cleared,
                    notes_missing=cleared.clearance.missing,
                )
                await self.run.settle_concern(verified, ConcernStatus.VERIFIED)
                return ConcernExecution(base=base, outcome=verified)
            await self.run.transition_concern(
                concern.id, ConcernStatus.REVISING, review.reason
            )
            feedback = review.reason + "\n" + "\n".join(review.residual)
            # Only a round that moved the branch spends the revision budget.
            # A round that commits nothing gives the reviewer nothing new to
            # judge, so charging it would retire the concern for work it was
            # never shown — failing a concern whose branch is complete and
            # whose every criterion the reviewer has accepted.
            # The loop is still bounded by `attempts`, so this cannot spin.
            advanced = diff.commit is not None and diff.commit != round_base
            charged += 1 if advanced and not diff.declaration else 0
            if charged == maximum_round:
                break
        # A concern that never spent a revision round never had its work
        # judged at all: every attempt died on the declaration contract. That
        # is the harness failing to let the work be evaluated rather than the
        # work failing to hold up, and the two should not read alike.
        failure = (
            "revision limit exhausted"
            if charged
            else "declaration contract unmet: no round reached the criteria"
        )
        exhausted = ConcernOutcome(
            concern_id=concern.id,
            branch=lease.branch,
            commit=rounds[-1].diff.commit if rounds else None,
            head=self.worktrees.head(lease),
            verified=False,
            rounds=rounds,
            failure=failure,
            notes_cleared=cleared.clearance.cleared,
            notes_missing=cleared.clearance.missing,
        )
        await self.run.settle_concern(exhausted, ConcernStatus.FAILED, failure)
        return ConcernExecution(base=base, outcome=exhausted)
