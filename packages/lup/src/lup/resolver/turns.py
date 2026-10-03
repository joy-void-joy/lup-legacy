"""Putting one prompt to one actor, and taking its typed submission back.

A worker revising its own code, a reviewer reading what that worker wrote,
and a merger settling a join are three spellings of one thing: address an
actor, hand it a prompt built from what the run knows, and receive a
submission. The differences that look structural are not — which factory
opens the session, which declared skill is rendered into the prompt, which
model the submission validates against.

What a prompt is built from is the other half of this. An actor is told what
the run has settled: the allowances its concern was granted, the answers a
human gave it, and — for a merger — the joins it has already made and what
the tree it is joining into is carrying. That last is why the merge ledger
lives here rather than with the phase that drives the joins: it is the
merger's memory across its own turns, and only the thing that takes those
turns can keep it.
"""

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from lup.harness.contracts import SkillInvocationRenderer
from lup.harness.models import ResolveSpec
from lup.policy.hooks import LupHooksConfig
from lup.policy.identity import ConcernAllowance
from lup.coordination.mailbox import ParkRequest
from lup.coordination.questions import QuestionAnswer
from lup.coordination.refs import ActorRef
from lup.coordination.cohort import ActorCohort, ActorRecipe
from lup.resolver.grants import GrantLedger, concern_grants, lease_grants
from lup.resolver.join_desk import (
    JoinPlan,
    JoinProgressRecord,
    JoinTip,
)
from lup.resolver.join_tools import JoinReport
from lup.resolver.mailbox import QuestionMailbox
from lup.resolver.models import (
    CarriedParent,
    Concern,
    DropCandidate,
    MergeReport,
    ReviewerContext,
    ReviewReport,
    WorkAssignment,
    WorkerContext,
    WorkerReport,
    WritableRootLease,
)
from lup.resolver.run import ResolveRun, ResolverInvariantError
from lup.resolver.tools import WAIT_CONTRACT
from lup.sessions.events import TurnResult
from lup.sessions.layers import SessionLayers
from lup.sessions.middleware import CorrectionConfig
from lup.sessions.surface import Agent

type WorkerFactoryRecipe = Callable[[WorkerContext], Agent]
type ReviewerFactoryRecipe = Callable[[ReviewerContext], Agent]


def corrective[T](
    recipe: Callable[[T], Agent],
    layers: SessionLayers = SessionLayers(correction=CorrectionConfig(cycles=2)),
) -> Callable[[T], Agent]:
    """Give each opened session corrective structured-output reprompts.

    Every resolver turn ends in a typed submission; a model that answers in
    prose instead of calling the submission tool would otherwise fail the
    whole run on its first miss. Laid over whatever the recipe's agent
    already declares, so a worker keeps its own layers and gains these.
    """

    def factory(argument: T) -> Agent:
        return recipe(argument).layered(layers)

    return factory


ASK_PREAMBLE = (
    "When a decision is not yours to make, ask through the resolver's question "
    "tools — queue_questions, await_answers, ask_questions — rather than "
    "guessing or ending your turn to report it. " + WAIT_CONTRACT + " "
    # Naming the other half of the channel, because a worker told only about
    # questions uses questions for everything. Two workers in one run parked
    # on housekeeping — a stray temp file, a broken venv — each spending a
    # human round trip on something that carried no decision, while the tool
    # that says a thing without stopping was in their toolset all along and
    # in none of their instructions.
    "When what you have is not a decision — something you found that whoever "
    "merges your work should know, or something blocking you that a human "
    "would simply fix, like a gate that refused you or a file you cannot "
    "remove — use send_message instead. It reaches the humans watching, it "
    "never parks you, and it costs nobody a round trip. Reserve questions for "
    "what genuinely cannot proceed until somebody decides."
)

# lup: ignore[constant-declaration] — one instruction every worker prompt states
# identically, declared beside the turn that renders it
DECLARATION_PREAMBLE = (
    "Before you submit, put the file account you are about to report through "
    "check_declaration and act on what it says. It runs the same reading that "
    "judges the account, so one it settles is one that passes. Declaring is "
    "the only part of your turn you cannot verify by looking — you have no "
    "git of your own here — and an account the gate rejects costs a whole "
    "session to correct."
)


# lup: ignore[constant-declaration] — instruction prose a worker is handed, and
# what it instructs is this codebase's own criterion protocol
ANSWERED_ARE_NOT_CRITERIA = (
    "Those rulings are context for judging the work, not criteria in their own "
    "right. Report criteria_met using exactly the declared criterion ids — echo "
    "an id verbatim when its criterion holds, and omit it when it does not. A "
    "question id is never one of them."
)
"""Why an answered question must not be echoed as a criterion.

The rulings above it are the only other list of ids in the prompt, and a
reviewer that credits one as met sends a clean acceptance through a
correction turn it did not need. `Joiner.recheck_concern` carries the same
instruction for the same reason.
"""


def format_paths(paths: list[Path]) -> str:
    return "\n".join(f"- {path.as_posix()}" for path in paths) or "- (none)"


def format_tips(tips: list[JoinTip], sample: int = 12) -> str:
    """Every parent still to land, with the files that decide their order.

    The paths are the point. A merger reading only shas learns which work
    contests which by conflicting on it; reading the files, it can put two
    parents that rewrite one module next to each other before merging
    either. Capped per parent because the obligation is to notice the
    overlap, not to read every path.
    """
    return "Parents to join:\n" + (
        "\n\n".join(
            f"- {tip.commit[:12]} — {tip.concern_id}"
            + (f": {tip.summary}" if tip.summary else "")
            + (
                "\n  files: "
                + ", ".join(path.as_posix() for path in tip.files[:sample])
                + (
                    f" (+{len(tip.files) - sample} more)"
                    if len(tip.files) > sample
                    else ""
                )
                if tip.files
                else ""
            )
            for tip in tips
        )
        or "- (none)"
    )


def format_carried(carried: list[CarriedParent]) -> str:
    """Parents already inside another, named so nothing reads as dropped.

    They are not on the table and must not be merged separately — they are
    in the tree the moment their container lands — but a merger that sees a
    concern's work missing from its plan and cannot tell why will ask.
    """
    if not carried:
        return ""
    return "\n\nRiding inside another parent, so already accounted for:\n" + "\n".join(
        f"- {item.commit[:12]} is contained in {item.inside[:12]}" for item in carried
    )


def format_candidates(owed: list[DropCandidate]) -> str:
    """Show each parent's unaccounted content, capped so the list stays read.

    A merger handed four hundred lines reads none of them, and the obligation
    is per path rather than per line — the sample is enough to recognize what
    went missing and find the rest.

    Lost definitions lead, and are named in full rather than sampled. Lines go
    missing whenever a resolution rewrites them, so that list is long and
    mostly benign; a definition that existed and now does not is the shape a
    regression takes, and there are never so many that naming them all costs
    the reader anything.
    """
    return (
        "\n".join(
            f"- {candidate.path.as_posix()} (from {candidate.parent[:12]})"
            + (
                "\n  definitions no longer present: "
                + ", ".join(
                    f"{symbol.name} (parent line {symbol.line})"
                    for symbol in candidate.lost_symbols
                )
                if candidate.lost_symbols
                else ""
            )
            + (
                f"\n  {len(candidate.missing)} lines, first: "
                + " / ".join(candidate.missing[:3])
                if candidate.missing
                else ""
            )
            for candidate in owed
        )
        or "- (none)"
    )


class LedgerEntry(BaseModel, frozen=True):
    """One join, as the merger accounted for it."""

    parent: str
    summary: str
    merge: MergeReport


def criteria_recital(concern: Concern) -> str:
    """The ids and descriptions an acceptance is checked against.

    `criteria_met` is compared to these ids exactly, so they are what a
    reviewer must be able to name — not a summary of them, and not the whole
    concern, which is what a later round deliberately leaves out.
    """
    return (
        "Acceptance criteria, by the ids `criteria_met` is checked against:\n"
        + "".join(
            f"- {criterion.id}: {criterion.description}\n"
            for criterion in concern.criteria
        )
        + "\n"
    )


class TurnRunner:
    """Every actor this run addresses, and the prompts it puts to them."""

    def __init__(
        self,
        spec: ResolveSpec,
        run: ResolveRun,
        actors: ActorCohort,
        mailbox: QuestionMailbox,
        worker_factory: WorkerFactoryRecipe,
        reviewer_factory: ReviewerFactoryRecipe,
        invocation_renderer: SkillInvocationRenderer,
        grants: GrantLedger,
    ) -> None:
        self.spec = spec
        self.run = run
        self.actors = actors
        self.mailbox = mailbox
        self.grants = grants
        self.worker_factory = corrective(worker_factory)
        self.reviewer_factory = corrective(reviewer_factory)
        self.invocation_renderer = invocation_renderer
        self.ledger: list[LedgerEntry] = []

    def worker_recipe(
        self, root: Path, allowances: list[ConcernAllowance]
    ) -> ActorRecipe:
        """How one writing actor's session opens, authorized for its gates.

        A recipe rather than an opened session, because opening is the
        cohort's: it announces the round as it opens one, and a caller that
        opens its own leaves the population record with nothing to say about
        the agent now working in it.

        The gates are published to this lease's document and the session is
        handed the reader for it, not the list: an answer that arrives after
        the session starts reaches it through the document, and the same
        document is what the lease's deployed dispatcher reads.

        Whatever a caller passes, what this lease has itself been granted is
        folded in here rather than left to each caller to remember. A turn
        republishes before it runs and the session holding the reader is the
        one opened for the first turn, so a set that omitted a gate that
        reader had already seen would take it away — and be reported as the
        withdrawal it is indistinguishable from.
        """

        def recipe(opened: ActorRef, hooks: LupHooksConfig) -> Agent:
            """Configure this worker's session around the mail that reaches it."""
            return self.worker_factory(
                WorkerContext(
                    root=root,
                    concern_id=opened.id,
                    actor=opened,
                    grants=self.grants.lease(
                        opened.id,
                        lease_grants(opened.id, allowances, self.recorded_answers()),
                        self.park_lease,
                    ),
                    hooks=hooks,
                )
            )

        return recipe

    def recorded_answers(self) -> list[QuestionAnswer]:
        """Every answer this run has settled, from the record that settles them.

        The mailbox rather than the persisted state, because the state is a
        fold of the mailbox taken a moment afterwards and the publisher that
        delivers a mid-lease grant works from the mailbox. A lease opened in
        the gap would otherwise publish a set missing a gate a human had
        already granted — and its reader would call that a withdrawal.
        """
        return [record.answer for record in self.mailbox.answers()]

    def park_lease(self, reason: str) -> None:
        """Stop the run because a human took back what a lease was granted."""
        self.mailbox.park(ParkRequest(run_id=self.run.require().run_id, reason=reason))

    def reviewer_recipe(self, worktree: Path) -> ActorRecipe:
        """How one reading actor's session opens over the tree it judges."""
        return lambda _opened, hooks: self.reviewer_factory(
            ReviewerContext(root=worktree, hooks=hooks)
        )

    async def reviewer_round[T: BaseModel](
        self, actor: ActorRef, worktree: Path, prompt: str, output: type[T]
    ) -> TurnResult[T]:
        """One turn of a reading actor, announced against its own address.

        Called again for a correction, which is the same round and so the
        same conversation: what it just judged is still in the session, and
        the population record keeps one start for the round either way.
        """
        return await self.actors.round(
            actor, prompt, output, self.reviewer_recipe(worktree), task=actor.id
        )

    def worker_invocation(self) -> str:
        """The rendered call for this run's declared worker skill."""
        return self.invocation_renderer.render(self.spec.worker_skill)

    async def worker_turn(
        self,
        assignment: WorkAssignment,
        feedback: str,
        round_number: int,
        holds_prior_work: bool = False,
    ) -> WorkerReport:
        prompt = (
            "Execute the portable worker skill below. You may edit only the assigned "
            "writable root. Do not create branches or commits; the orchestrator owns "
            "that authority. Resolve every acceptance criterion. Your concern's "
            "review-note markers are already gone from this worktree — the "
            "orchestrator removed them before you started, so the spec is the whole "
            "of the feedback. Do not re-introduce a marker, and do not leave an "
            "explanatory comment where one stood. Every `# lup:` marker still "
            "present belongs to another concern or is `defer:` parked work you "
            "were not asked to wake: leave it in place. If resolving your concern "
            "means deleting or moving code that carries one, do so and name it in "
            "your summary.\n\n"
            f"{ASK_PREAMBLE}\n\n"
            f"{DECLARATION_PREAMBLE}\n\n"
            f"{assignment.rendered_skill_invocation}\n\n"
            f"Assignment:\n{assignment.model_dump_json(indent=2)}"
        )
        match round_number > 1, holds_prior_work:
            case True, _:
                # The assignment rides along rather than being assumed
                # remembered. This session is usually the one that produced
                # the work under review, but a resumed run opens a fresh one,
                # and a worker handed only "did not pass" with no concern and
                # no criteria has nothing to revise against.
                prompt = (
                    f"Round {round_number}. Your submitted work was reviewed and "
                    "did not pass. Revise it in the same worktree and submit an "
                    "updated report — this is a revision, not a fresh start, and "
                    "the assignment below is the one you already worked.\n\n"
                    f"Review feedback:\n{feedback}\n\n"
                    f"Assignment:\n{assignment.model_dump_json(indent=2)}"
                )
            case _, True:
                # A lease whose branch already carries commits is a re-entry
                # that lost its round record, not a fresh concern. Told nothing,
                # a worker spends a whole turn re-deriving a verification an
                # earlier session already did, because a re-lease and a
                # rejection arrive as byte-identical assignments.
                prompt = (
                    "Round 1, re-entered. This lease's branch already carries "
                    "committed work for this concern from an earlier session, "
                    "and no review of it survived. Read what is there before you "
                    "change anything: if it already satisfies the criteria, "
                    "submit it as it stands and say so rather than redoing "
                    "it.\n\n" + prompt
                )
        result = await self.actors.round(
            ActorRef(kind="worker", id=assignment.concern.id, round=round_number),
            prompt,
            WorkerReport,
            self.worker_recipe(
                assignment.lease.root, self.granted_for(assignment.concern)
            ),
            task=assignment.concern.id,
        )
        if result.output.concern_id != assignment.concern.id:
            raise ResolverInvariantError("worker returned a foreign concern id")
        return result.output

    async def review_turn(
        self,
        concern: Concern,
        worker: WorkerReport,
        base: str,
        commit: str,
        worktree: Path,
        round_number: int,
    ) -> ReviewReport:
        invocation = self.invocation_renderer.render(self.spec.review_skill)
        # The whole range, not the head: a round's work regularly spans
        # several commits, and a reviewer handed one spends its round
        # discovering the others.
        span = f"Commits under review: {base[:12]}..{commit[:12]} — every one."
        rulings = self.rulings_for(concern.id)
        answered = (
            f"\n\nQuestions this concern has had answered:\n{rulings}"
            f"\n\n{ANSWERED_ARE_NOT_CRITERIA}"
            if rulings
            else ""
        )
        prompt = (
            "Independently review the committed concern against every persisted "
            "acceptance criterion.\n\n"
            f"{invocation}\n\nConcern:\n{concern.model_dump_json(indent=2)}\n\n"
            f"Worker report:\n{worker.model_dump_json(indent=2)}\n\n"
            f"{span}{answered}"
        )
        if round_number > 1:
            # This reviewer wrote the criticism the worker was revising, so it
            # knows what it asked for, and re-reading its whole concern cold on
            # every round is the cost a one-shot session would pay. The
            # criteria are the exception, carried every round: the acceptance
            # guard checks `criteria_met` against these exact ids, and a
            # reviewer whose session did not survive a resumed run has nowhere
            # to read them — one left to reconstruct the ids from the concern's
            # answered questions gets an acceptance it already argued for
            # refused by the guard. A round that cannot name what it is judged
            # against fails identically however often it is retried.
            prompt = (
                "The worker revised in response to your review. Review the "
                "updated work against the same acceptance criteria, and say "
                "explicitly whether each point you raised was addressed.\n\n"
                f"{criteria_recital(concern)}"
                f"Worker report:\n{worker.model_dump_json(indent=2)}\n\n"
                f"{span}{answered}"
            )
        reviewer = ActorRef(kind="reviewer", id=concern.id, round=round_number)
        result = await self.reviewer_round(reviewer, worktree, prompt, ReviewReport)
        if result.output.concern_id != concern.id:
            raise ResolverInvariantError("reviewer returned a foreign concern id")
        return await self.declared_labels_only(
            reviewer, worktree, concern, result.output
        )

    async def join_turn(
        self,
        lease: WritableRootLease,
        plan: JoinPlan,
        progress: JoinProgressRecord,
        purpose: str,
    ) -> JoinReport:
        """Put the whole set of parents to the merger, and let it sequence them.

        The plan goes over in full — every tip, the concern behind it, and
        the paths it wrote — because the alternative is a merger that
        discovers the shape of the work parent by parent: where three
        branches rewrite a single module, it meets the third at parent nine,
        with the first two already resolved in ways it would not choose
        knowing about it.

        What the orchestrator would otherwise interleave is in the tools instead,
        so nothing is traded for the foreknowledge: ``land_parent`` refuses a
        short account while the merger is still on that parent, verifies the
        tree it just made, writes the checkpoint, and says on the way out
        whether a drain is waiting.
        """
        landed = {commit for commit in progress.joined}
        remaining = [tip for tip in plan.tips if tip.commit not in landed]
        questions = {
            item.question.id: item.question
            for item in self.mailbox.questions()
            if item.question.concern_id == lease.concern_id
        }
        decisions = "\n".join(
            f"Question: {questions[item.answer.question_id].prompt}\n"
            f"Answer: {item.answer.value}"
            for item in self.mailbox.answers()
            if item.answer.question_id in questions
        )
        prompt = (
            "Join every parent below into the assigned worktree. You own the "
            "sequence: read the whole set first, decide the order that puts "
            "related work together, and say what you plan before you start. "
            "Parents that touch the same files are the ones worth thinking "
            "about — their paths are listed so you can see that before you "
            "merge rather than at the conflict.\n\n"
            "For each parent: call start_parent, resolve any conflict it "
            "reports and stage it with `git add`, then call land_parent with "
            "the account of what you did. Do not commit and do not change "
            "branches — land_parent owns commit authority, and it is what "
            "checks the account, verifies the tree and records the "
            "checkpoint.\n\n"
            "Two things you must account for in land_parent. Every candidate "
            "it names is content one parent contributed that the tree no "
            "longer holds — disposition each as kept, rewritten, superseded "
            "or dropped, with a reason. A rewrite is a legitimate answer; "
            "silence is not, and where a candidate names definitions no "
            "longer present, read those first: a function one parent defined "
            "and this tree does not is the shape a regression takes, and "
            "'rewritten' is only true if you can name where the behaviour "
            "landed. And any file you edit outside the conflict set must be "
            "declared with a reason — fixing a caller whose file merged "
            "clean is correct and expected, and is exactly what has to be "
            "visible.\n\n"
            "land_parent verifies the tree the moment you land a parent and "
            "brings a failure straight back to you naming what broke, so you "
            "are not the last line of defence. What costs the run is "
            "iterating the project's whole gate to green by hand. Rendered "
            "artifacts are settled by the generator before you see a "
            "conflict, so a difference in one is never yours to resolve.\n\n"
            "Stop and end your turn when land_parent reports "
            "drain_requested, or when every parent has landed.\n\n"
            + f"{self.invocation_renderer.render(self.spec.merge_skill)}\n\n"
            + f"Purpose: {purpose}\nWorktree: {lease.root}\n"
            + f"Already landed: {len(landed)} of {len(plan.tips)}\n\n"
            + format_tips(remaining)
            + format_carried(plan.carried)
            + f"\n\nRecorded decisions for this join:\n{decisions}"
        )
        result = await self.merger_round(lease, prompt, JoinReport)
        return result.output

    async def merge_turn(
        self,
        lease: WritableRootLease,
        parent: str,
        purpose: str,
        conflicted: list[Path],
        owed: list[DropCandidate],
        problems: list[str] | None = None,
    ) -> MergeReport:
        """Put one join to the merger, with what each side meant by it.

        A merger handed only a purpose string, a worktree and two
        shas can read what changed but not which behaviour was a
        deliberate decision. The argument against telling it more is that a
        merger who knows what each side intended can rationalize a bad merge
        as intended; what settled it the other way is that the observed
        failure was declining to classify something it had no basis for,
        which is missing context rather than misused context.
        """
        if problems is not None:
            return await self.merge_retry(lease, problems)
        prompt = (
            "Resolve the prepared semantic merge in the assigned worktree. Stage "
            "every resolution with `git add` — settling the index is your work "
            "and the merge cannot complete without it. Do not commit and do not "
            "change branches; the orchestrator owns commit authority, which "
            "covers committing only. If you resolve the content but cannot "
            "stage it, say so in `blocked` rather than reporting completion.\n\n"
            "Two things you must account for. Every candidate below is content "
            "one parent contributed that this tree no longer holds — disposition "
            "each as kept, rewritten, superseded or dropped, with a reason. A "
            "rewrite is a legitimate answer; silence is not. Where a candidate "
            "names definitions no longer present, read those first: a function "
            "or class one parent defined and this tree does not is the shape a "
            "regression takes, and 'rewritten' is only true if you can name "
            "where the behaviour landed. And any file you "
            "edit that is not in the conflict set must be declared, with a "
            "reason: fixing a caller whose file merged clean is correct and "
            "expected, and is exactly what has to be visible.\n\n"
            "The tree is verified the moment you hand it back — scoped to what "
            "this join changed — and a failure comes straight back to you "
            "naming what broke, so you are not the last line of defence here. "
            "Sanity-check what you actually edited if you want it; what costs "
            "the run is iterating the project's whole gate to green, which one "
            "observed join did four times over before reporting, against a "
            "tree that was about to be checked anyway. Rendered artifacts are "
            "settled by the generator before you are called, so a difference "
            "in one is never yours to resolve.\n\n"
            + f"{self.invocation_renderer.render(self.spec.merge_skill)}\n\n"
            + f"Purpose: {purpose}\nWorktree: {lease.root}\nJoining: {parent}\n\n"
            + self.merge_context(parent)
            + f"Conflicted paths:\n{format_paths(conflicted)}\n\n"
            + f"Unaccounted content:\n{format_candidates(owed)}"
        )
        result = await self.merger_round(lease, prompt, MergeReport)
        return result.output

    async def merge_retry(
        self, lease: WritableRootLease, problems: list[str]
    ) -> MergeReport:
        """Name what the last report left unmet, on the session that wrote it."""
        result = await self.merger_round(
            lease,
            (
                "Your merge report did not account for everything this "
                "join changed:\n- " + "\n- ".join(problems) + "\n\nFix "
                "the tree where it is wrong and resubmit a report that "
                "accounts for each item. Declaring a rewrite or a "
                "deliberate supersession with a reason is a complete "
                "answer; leaving one unmentioned is not."
            ),
            MergeReport,
        )
        return result.output

    def merger_recipe(self, lease: WritableRootLease) -> ActorRecipe:
        """How the merger conversation this lease's joins are settled in opens."""
        return self.worker_recipe(lease.root, self.merge_allowances())

    async def merger_round[T: BaseModel](
        self, lease: WritableRootLease, prompt: str, output: type[T]
    ) -> TurnResult[T]:
        """One turn of that conversation, announced against the merger's address.

        Every join this lease settles is the same merger on the same round:
        a join is not an attempt at a concern, it is the concern's work. So
        the address is stable and the population record carries one merger
        per lease rather than one per parent it took.
        """
        return await self.actors.round(
            ActorRef(kind="merger", id=lease.concern_id),
            prompt,
            output,
            self.merger_recipe(lease),
            task=lease.concern_id,
        )

    def record_join(self, parent: str, merge: MergeReport) -> None:
        """Keep one settled join, so later ones are decided consistently with it."""
        self.ledger.append(
            LedgerEntry(parent=parent, summary=merge.summary, merge=merge)
        )

    def merge_context(self, parent: str) -> str:
        """What the concern behind this parent was for, and what it decided.

        A merger that knows only the diff can tell what changed but not what
        was deliberate. What it gets is the concern's own specification, the
        criteria it had to meet, the answers a human gave it, and the merge
        notes its worker left for whoever would join it — the last being the
        one thing only that worker could know.
        """
        state = self.run.state
        if state is None:
            return ""
        owner = next(
            (
                outcome.concern_id
                for outcome in state.outcomes
                if outcome.commit == parent
            ),
            None,
        )
        if owner is None:
            return ""
        concern = next(
            (item for item in state.concerns if item.id == owner),
            None,
        )
        if concern is None:
            return ""
        answers = [
            f"- {record.answer.question_id}: {record.answer.value}"
            for record in self.mailbox.answers()
            for item in self.mailbox.questions()
            if item.question.id == record.answer.question_id
            and item.question.concern_id == owner
        ]
        notes = [
            note
            for outcome in state.outcomes
            if outcome.concern_id == owner
            for round_record in outcome.rounds
            for note in round_record.worker.merge_notes
        ]
        return (
            f"Concern behind this parent:\n{concern.model_dump_json(indent=2)}\n\n"
            + (
                "Answers this concern was given:\n" + "\n".join(answers) + "\n\n"
                if answers
                else ""
            )
            + (
                "Merge notes its worker left for whoever joins it — these are "
                "consequences only that worker could know:\n- "
                + "\n- ".join(notes)
                + "\n\n"
                if notes
                else ""
            )
            + self.ledger_recital()
            + self.accumulated_recital()
        )

    def ledger_recital(self) -> str:
        """What this merger has already settled, carried forward as a record.

        The accumulating session means it was there for all of them, but
        attention is not memory: by parent ten the earliest joins are far
        behind, and a decision it made at join two is exactly what it needs
        at join nine to stay consistent.
        """
        if not self.ledger:
            return ""
        return (
            "Joins you have already settled in this run:\n"
            + "\n".join(
                f"- {entry.parent[:12]}: {entry.summary}" for entry in self.ledger
            )
            + "\n\n"
        )

    def accumulated_recital(self) -> str:
        """What the tree being joined into is already carrying, and why.

        The incoming parent arrived with its concern, its answers and its
        merge notes; the side it is being joined into had only one-line
        summaries. That asymmetry is what produces the characteristic bad
        join — the incoming change is legible and the standing one is not, so
        the resolution favours what the merger can read, and an earlier
        concern is quietly reverted by someone who never saw what it was for.
        """
        state = self.run.state
        if state is None or not self.ledger:
            return ""
        joined = {entry.parent for entry in self.ledger}
        concerns = {
            outcome.concern_id: outcome.commit
            for outcome in state.outcomes
            if outcome.commit in joined
        }
        recited = [
            f"- {concern.id}: {concern.title}\n"
            + "".join(
                f"    criterion: {criterion.description}\n"
                for criterion in concern.criteria
            )
            for concern in state.concerns
            if concern.id in concerns
        ]
        if not recited:
            return ""
        return (
            "Already in the tree you are joining into. These are settled, and "
            "a resolution that stops one of them holding is a regression "
            "however reasonable the incoming change looks:\n"
            + "\n".join(recited)
            + "\n"
        )

    async def declared_labels_only(
        self,
        reviewer: ActorRef,
        worktree: Path,
        concern: Concern,
        report: ReviewReport,
    ) -> ReviewReport:
        """Correct a report crediting an id this concern never declared.

        Put back to the same session, which still holds what it just judged,
        so the correction costs one turn instead of the revision round the
        acceptance guard would otherwise spend. The reviewer reads its
        criteria beside the answered questions, and crediting a question id
        as met is the mistake that shape invites: a run reached its revision
        limit re-deriving an acceptance every reviewer had already given.

        Only the labels are re-asked, and only once. A verdict is the
        reviewer's to hold, so a report that still names an undeclared id
        after this keeps both the id and the verdict: the guard records it
        rather than charging for it. This is what keeps the record honest,
        not what makes the acceptance safe.
        """
        declared = {criterion.id: True for criterion in concern.criteria}
        unknown = [label for label in report.criteria_met if label not in declared]
        if not unknown:
            return report
        correction = (
            "Your report labelled criteria this concern never declared: "
            + ", ".join(unknown)
            + ". Those are answered questions or ids from elsewhere, not "
            "acceptance criteria. Resubmit the same verdict with criteria_met "
            "drawn only from the declared ids: " + ", ".join(declared)
        )
        result = await self.reviewer_round(reviewer, worktree, correction, ReviewReport)
        if result.output.concern_id != concern.id:
            raise ResolverInvariantError("reviewer returned a foreign concern id")
        return result.output

    def rulings_for(self, concern_id: str) -> str:
        """The concern's mid-run Q&A, rendered for an actor's prompt.

        Workers cite rulings in their reports; a reviewer that cannot read
        the ruling can only take the citation on faith or spend its round
        disputing settled questions. The record travels with the prompt.
        """
        state = self.run.state
        if state is None or state.questions is None:
            return ""
        prompts = {
            question.id: question.prompt
            for question in state.questions.questions
            if question.concern_id == concern_id
            and question.id not in state.retired_questions
        }
        return "\n".join(
            f"- {answer.question_id}: {prompts[answer.question_id]}\n"
            f"  answered: {answer.value}"
            for answer in (state.answers.answers if state.answers else [])
            if answer.question_id in prompts
        )

    def granted_for(self, concern: Concern) -> list[ConcernAllowance]:
        """Every gate this concern may pass: planned grants plus mid-run ones.

        A `request_allowance` question a human answered "grant" extends the
        concern's authority from that answer on — reaching the session that
        asked, which is still running, through the document that session's
        judges read. This is what the run believes; the document is what
        governs, and the two are the same until a human says otherwise.
        """
        return concern_grants(concern, self.recorded_answers())

    def merge_allowances(self) -> list[ConcernAllowance]:
        """Every gate the joined concerns were approved to pass.

        A join can newly require a suppression that neither parent needed: a
        rule one branch adds first meets a constant another branch adds only
        once the two are together. Nobody could have declared that at plan
        time, and a merge session carrying no allowance at all had no route
        to it except failing.
        """
        state = self.run.state
        if state is None:
            return []
        return list(
            dict.fromkeys(
                allowance
                for concern in state.concerns
                for allowance in self.granted_for(concern)
            )
        )
