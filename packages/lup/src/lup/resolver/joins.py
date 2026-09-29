"""Bringing branches together, and settling what the joining breaks.

Two phases need this and they need the same thing. A concern with several
dependencies has to start from a tree that holds all of them, and the run's
final integration has to build one tree from every verified concern — both
are the same problem, differing only in which commits go in.

What makes it one thing rather than a git call is everything that hangs off
a join. Parents are ordered so contested work meets while a merger is on it;
a conflict or a dropped hunk is put to that merger and held to what it
declares; the tree is verified after every parent rather than once at the
end, so a red result names the join that caused it; and every concern is
re-checked against the finished tree. A criterion that stops holding opens
a question rather than failing the run, because later work can legitimately
supersede an earlier one and only a human can say whether this did.

The same re-check can also run after each join, against the concerns that
join touched, which names the merge responsible instead of leaving the
finding to be attributed among every parent. That is what it buys, and it
costs a reviewer turn per overlapping pair, so it is asked for rather than
assumed.
"""

import asyncio
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from lup.channels.models import utc_now
from lup.resolver.contracts import ResolverAwaitingAnswers, ResolverDrained
from lup.resolver.dag import ConcernGraph
from lup.resolver.record import (
    JoinAuditEvent,
    JoinCompletedEvent,
    JoinPlannedEvent,
    JoinRenderedEvent,
    Journal,
    RecheckRepeatedEvent,
    RecheckReusedEvent,
    RecheckChangedEvent,
)
from lup.resolver.recheck_desk import (
    RecheckDesk,
    RecheckIdentity,
    RecheckRecord,
    RecheckVerdict,
)
from lup.resolver.join_desk import (
    JoinDesk,
    JoinPlan,
    JoinProgressRecord,
    JoinTip,
)
from lup.coordination.questions import QuestionAnswer
from lup.coordination.refs import ActorRef
from lup.resolver.join_tools import merge_problems
from lup.resolver.models import (
    CarriedParent,
    Concern,
    DropCandidate,
    IntegrationRecord,
    JoinProgress,
    MaterialQuestion,
    RecheckRuling,
    ResolveState,
    ReviewReport,
    SupersessionRuling,
    WritableRootLease,
)
from lup.resolver.orchestrator import WorktreeOrchestrator
from lup.resolver.questions import QuestionBroker
from lup.resolver.run import ResolveRun, ResolverInvariantError
from lup.resolver.turns import TurnRunner
from lup.resolver.verification import Verifier


def asked_rulings(
    asked: list[MaterialQuestion], answers: list[QuestionAnswer]
) -> list[RecheckRuling]:
    """Pair each re-check with its recorded answer, dropping the unanswered."""
    recorded = {answer.question_id: answer for answer in answers}
    return [
        RecheckRuling(
            concern_id=question.concern_id,
            criteria=list(question.criteria),
            ruling=recorded[question.id].value,
        )
        for question in asked
        if question.id in recorded
    ]


class Joiner:
    """Join commits into one tree, and resolve whatever the joining breaks."""

    def __init__(
        self,
        run: ResolveRun,
        runner: TurnRunner,
        questions: QuestionBroker,
        verifier: Verifier,
        worktrees: WorktreeOrchestrator,
        journal: Journal,
        standing_rechecks: bool = False,
        regeneration: list[str] | None = None,
        parallel_rechecks: int = 4,
    ) -> None:
        self.run = run
        self.runner = runner
        self.questions = questions
        self.verifier = verifier
        self.worktrees = worktrees
        self.journal = journal
        self.standing_rechecks = standing_rechecks
        self.regeneration = regeneration or []
        self.parallel_rechecks = parallel_rechecks
        """How many concerns re-check the finished tree at once.

        The final pass is one reviewer turn per integrated concern and they
        are wholly independent — each reads the same tree and answers only
        about its own criteria. Run in sequence, one measured run's 21
        concerns at 16.3 minutes a turn is most of a working day for a phase
        with no ordering in it at all.
        """

    async def join_commits(
        self,
        lease: WritableRootLease,
        commits: list[str],
        purpose: str,
        title: str,
    ) -> str:
        """Hand the merger every parent at once, and let it drive the sequence.

        Every join is pairwise because git cannot merge N branches at once
        when it matters — octopus refuses outright on conflict rather than
        leaving an index to resolve, and 9 of 12 parents measured against one
        run's part-built tree conflicted. What that argues for is a sequence,
        not an orchestrator owning it: a merger handed one parent per turn
        discovers the shape of the work as it goes, and meets the fact that
        three branches all rewrite one module at the ninth of them, with two
        already resolved in ways it would not have chosen knowing the third.

        So the plan goes over in full — every tip, the concern behind it and
        the paths it wrote — and the merger sequences its own work through
        ``start_parent`` and ``land_parent``. Those verbs keep what an owning
        loop would: the accounting gate refuses a short account while the merger
        is still on the parent it belongs to, verification runs per parent so
        a red gate names one, the checkpoint is written as each lands, and a
        drain is reported on the way out of every landing.

        Only the parents no other parent contains are on the table. The rest
        are in the tree the moment their container lands, so naming them
        separately buys a verification and can buy a turn spent concluding
        that nothing happened.
        """
        if len(commits) < 2:
            raise ValueError("a semantic join requires at least two commits")
        if not lease.root.exists():
            self.worktrees.create(lease, commits[0])
        base = self.worktrees.head(lease)
        ordered = self.join_order(lease, base, commits[1:])
        carried = self.carried_parents(lease, ordered)
        riding = {item.commit: item.inside for item in carried}
        tips = [parent for parent in ordered if parent not in riding]
        self.journal.record(JoinPlannedEvent(tips=tips, carried=carried))
        desk = JoinDesk(self.run.repository.root, lease.concern_id)
        desk.write_plan(
            JoinPlan(
                concern_id=lease.concern_id,
                worktree=lease.root,
                base=base,
                title=title,
                purpose=purpose,
                tips=[self.tip_of(lease, base, parent) for parent in tips],
                carried=carried,
                regeneration=list(self.regeneration),
                verification=list(self.verifier.commands),
            )
        )
        blocked = await self.drive_join(lease, desk, purpose)
        progress = desk.progress()
        current = progress.commit or self.worktrees.head(lease)
        if lease.concern_id == "integration":
            self.record_join_progress(progress.joined, current, tips)
        outstanding = [tip for tip in tips if tip not in progress.joined]
        if outstanding:
            pending = await self.questions.unanswered_for(lease.concern_id)
            if pending:
                raise ResolverAwaitingAnswers(pending, [])
            drain = self.questions.draining()
            if drain is None:
                raise ResolverInvariantError(
                    f"semantic join failed for {lease.concern_id}: "
                    f"{len(outstanding)} parent(s) unjoined with nothing asking "
                    "it to stop" + (f" — {blocked}" if blocked else "")
                )
            raise ResolverDrained(drain.reason, [])
        # A parent that rode inside another was never merged on its own, but
        # its content is in the tree and is exactly as capable of having been
        # dropped there, so the final audit answers for it too.
        await self.audit_join(
            lease, base, [*progress.joined, *riding], current, purpose
        )
        desk.clear()
        return current

    def tip_of(self, lease: WritableRootLease, base: str, parent: str) -> JoinTip:
        """One parent as the merger meets it: whose work, and which files.

        The files are what make the set worth handing over whole — two tips
        rewriting one module are visible here before either is merged, where
        the loop only ever revealed it by conflicting on the second.
        """
        state = self.run.state
        owner = next(
            (
                outcome.concern_id
                for outcome in (state.outcomes if state is not None else [])
                if outcome.commit == parent
            ),
            "",
        )
        concern = next(
            (
                item
                for item in (state.concerns if state is not None else [])
                if item.id == owner
            ),
            None,
        )
        return JoinTip(
            commit=parent,
            concern_id=owner,
            summary=concern.title if concern is not None else "",
            files=self.authored_by(lease, base, parent),
        )

    async def drive_join(
        self, lease: WritableRootLease, desk: JoinDesk, purpose: str
    ) -> str:
        """Put the whole plan to the merger, and let it come back when done.

        Re-entered rather than assumed one-shot: a merger that stops with
        parents outstanding and no drain asked for is given the remaining
        plan again, because its tools have already recorded everything it
        did land and the tree it left is the one the next turn continues
        from. The bound is the plan itself — a turn that lands nothing new
        is not asked a third time.
        """
        plan = desk.plan()
        if plan is None:
            raise ResolverInvariantError("a join was driven with no plan on the table")
        if self.questions.draining() is not None:
            # Before the turn rather than only inside the landing verb: a
            # drain that arrived while the previous phase finished would
            # otherwise buy a whole merger session that stops at its first
            # landing, and the boundary before the first parent is as good
            # a place to resume from as the one after it.
            return ""
        blocked = ""
        for _attempt in plan.tips:
            before = desk.progress()
            report = await self.runner.join_turn(lease, plan, before, purpose)
            blocked = report.blocked
            progress = desk.progress()
            self.record_landings(before, progress)
            pending = await self.questions.unanswered_for(lease.concern_id)
            if (
                blocked
                and not pending
                and len(progress.joined) < len(plan.tips)
                and self.questions.draining() is None
            ):
                question = MaterialQuestion(
                    id=f"join-blocked-{uuid4().hex}",
                    concern_id=lease.concern_id,
                    prompt=(
                        f"How should the join for {lease.concern_id} proceed? "
                        f"The merger reported this blocker for {purpose}:\n\n{blocked}"
                    ),
                )
                self.questions.queue_questions([question], lease.concern_id)
                pending = await self.questions.unanswered_for(lease.concern_id)
            if pending:
                raise ResolverAwaitingAnswers(pending, [])
            await self.recheck_landed(lease, plan, before, progress)
            # Every parent landed, nothing landed this turn, or the run was
            # asked to stop. The last is checked here as well as inside the
            # landing verb, because a merger that ends its turn holding a
            # drain must not be handed the remaining plan again.
            if len(progress.joined) in {len(plan.tips), len(before.joined)}:
                return blocked
            if self.questions.draining() is not None:
                return blocked
        return blocked

    async def recheck_landed(
        self,
        lease: WritableRootLease,
        plan: JoinPlan,
        before: JoinProgressRecord,
        after: JoinProgressRecord,
    ) -> None:
        """Ask whether the parents landed this turn broke an earlier one.

        Off unless asked for, and the argument for it is unchanged: what it
        buys is attribution, and it costs a reviewer turn per overlapping
        pair. What moved is when it can run — the merger lands several
        parents in one turn, so each is examined against the concerns that
        were standing before it landed rather than at the moment it did.
        """
        if not self.standing_rechecks:
            return
        standing = list(before.joined)
        for landing in after.landings[len(before.landings) :]:
            await self.recheck_standing(lease, plan.base, standing, landing.commit)
            standing = [*standing, landing.commit]

    def record_landings(
        self, before: JoinProgressRecord, after: JoinProgressRecord
    ) -> None:
        """Journal each parent the merger landed during its turn.

        Written here rather than by the tool that landed them: the journal
        numbers its entries, so a second writer appending from the merger's
        own process would have to agree with this one about the sequence.
        The checkpoint is what has to survive an interruption, and that is a
        file the tool owns; the journal is the run's account of it.
        """
        for landing in after.landings[len(before.landings) :]:
            if landing.rendered and not landing.conflicted:
                self.journal.record(JoinRenderedEvent(parent=landing.commit))
            self.journal.record(
                JoinCompletedEvent(
                    parent=landing.commit,
                    commit=landing.head,
                    conflicted=landing.conflicted,
                    broke=landing.broke,
                )
            )

    def join_order(
        self, lease: WritableRootLease, base: str, parents: list[str]
    ) -> list[str]:
        """Order the joins so related work meets while the session is on it.

        Completion order is the order concerns happened to finish, which
        scatters related work across the sequence and puts a merger's own
        precedent behind five unrelated joins. Parents that overlap no file
        with anything already placed go first — those are the ones git
        settles alone — and the rest follow in dependency order, so each
        contested join lands next to the work it contests.
        """
        touched = {
            parent: {path.as_posix() for path in self.authored_by(lease, base, parent)}
            for parent in parents
        }
        ranked = sorted(
            parents,
            key=lambda parent: (
                sum(
                    1
                    for other in parents
                    if other != parent and touched[parent] & touched[other]
                ),
                self.dependency_depth(parent),
                parent,
            ),
        )
        return ranked

    def carried_parents(
        self, lease: WritableRootLease, parents: list[str]
    ) -> list[CarriedParent]:
        """Every parent another parent already contains, and which one does.

        Concerns are cut from their dependencies' commits, so the branches
        stack rather than fanning out from the base. A parent inside another
        is in the tree the moment that one lands, and merging it separately
        buys nothing: a verification, and a merger turn where the session
        cannot tell "already joined" from "something upstream is wrong". In
        one measured run, 8 of 21 parents were inside a sibling, and two of
        the three joins spent before it was stopped were on such a parent —
        one of them contained in five different siblings.
        """

        def container_of(parent: str) -> str | None:
            """The first other parent holding this one, of however many do."""
            return next(
                (
                    other
                    for other in parents
                    if other != parent
                    and self.worktrees.contained_in(lease, parent, other)
                ),
                None,
            )

        return [
            CarriedParent(commit=parent, inside=container)
            for parent in parents
            if (container := container_of(parent)) is not None
        ]

    def authored_by(
        self, lease: WritableRootLease, base: str, commit: str
    ) -> list[Path]:
        """Every path one parent wrote, measured from where it forked.

        Measured from the base instead, a parent is credited with every path
        the base moved ahead on since the leases were cut — 124 paths where
        the real answer was 19, in one measured run. That inflation is the
        same for every parent, so every pair appears to overlap and the
        filters built on this stop discriminating: the ordering below ranks
        every parent equally, and the standing re-check examines every pair
        it was written to prune. The fork point is what the drop-candidate
        detector already asks from, for the same reason.
        """
        fork = self.worktrees.merge_base(lease, base, commit)
        return self.worktrees.authored_between(lease, fork, commit)

    def record_join_progress(
        self, joined: list[str], commit: str, planned: list[str]
    ) -> None:
        """Say where the join sequence got to, as each parent lands.

        Written after the parent is committed, so what it names is a tree
        that exists. A resume restores to this commit instead of the run's
        source, which is what stops it discarding joins it already built.
        """
        state = self.run.state
        if state is None:
            return
        before = state.join_progress.completions if state.join_progress else []
        self.run.persist(
            state.model_copy(
                update={
                    "join_progress": JoinProgress(
                        joined=list(joined),
                        commit=commit,
                        planned=planned,
                        completions=[*before, utc_now()],
                    )
                }
            )
        )

    async def adjudicate(
        self,
        lease: WritableRootLease,
        parent: str,
        purpose: str,
        owed: list[DropCandidate],
    ) -> None:
        """Put one join to the merger and hold its report to what it declared."""
        conflicted = self.worktrees.conflicted_paths(lease)
        merge = await self.runner.merge_turn(lease, parent, purpose, conflicted, owed)
        # An incompletion is the merger's considered answer and spending
        # another turn on it only re-derives it. An accounting gap is
        # different: the tree may be right and the declaration short, which
        # is precisely what a second turn can settle.
        if not merge.completed or merge.unresolved_paths:
            raise ResolverInvariantError(
                f"semantic join failed for {lease.concern_id}: "
                + (merge.blocked or merge.summary)
            )
        problems = merge_problems(merge, conflicted, owed)
        if problems:
            merge = await self.runner.merge_turn(
                lease, parent, purpose, conflicted, owed, problems
            )
            problems = merge_problems(merge, conflicted, owed)
        if problems:
            raise ResolverInvariantError(
                f"semantic join for {lease.concern_id} was not accounted for: "
                + "; ".join(problems)
            )
        self.runner.record_join(parent, merge)

    async def audit_join(
        self,
        lease: WritableRootLease,
        base: str,
        joined: list[str],
        result: str,
        purpose: str,
    ) -> None:
        """Re-check every parent against the finished tree, not just the last.

        A hunk lost at join three and never noticed is invisible to the
        per-join check, which only ever looked at one parent against one
        result. Running the same detector over every parent once the tree is
        final is what catches it.
        """
        owed = [
            candidate
            for parent in joined
            for candidate in self.worktrees.drop_candidates(
                lease, self.worktrees.merge_base(lease, base, parent), parent, result
            )
        ]
        if not owed:
            return
        self.journal.record(
            JoinAuditEvent(parents=joined, outstanding=len(owed), commit=result)
        )
        await self.adjudicate(lease, joined[-1], f"{purpose} — final audit", owed)
        self.worktrees.commit_join(lease, "resolve: settle the final join audit")

    def dependency_depth(self, commit: str) -> int:
        """How deep in the concern graph the concern that produced this sits."""
        state = self.run.state
        if state is None:
            return 0
        owner = next(
            (
                outcome.concern_id
                for outcome in state.outcomes
                if outcome.commit == commit
            ),
            None,
        )
        if owner is None:
            return 0
        return len(ConcernGraph(state.concerns).ancestors(owner))

    async def recheck_standing(
        self,
        lease: WritableRootLease,
        base: str,
        standing: list[str],
        parent: str,
    ) -> None:
        """Ask whether this join stopped an already-joined concern holding.

        A regression is precisely an earlier parent's criterion that no longer
        holds. Checking that only after the last join reports it with every
        parent a candidate and the merger long past the context; asking it
        here names the join that caused it while the tree is still small
        enough to read. Off unless asked for: the naming is the whole of what
        it adds, and it costs a reviewer turn for every overlapping pair.

        Only concerns this join could have touched are re-examined. A join
        that changes no file an earlier concern changed cannot have broken its
        criteria in a way the per-join verification above would not already
        have caught, and re-reading every concern after every join would cost
        a reviewer turn per pair.
        """
        state = self.run.state
        if state is None or not standing:
            return
        changed = {path.as_posix() for path in self.authored_by(lease, base, parent)}
        owners = {
            outcome.commit: outcome.concern_id
            for outcome in state.outcomes
            if outcome.commit is not None
        }
        for earlier in standing:
            if earlier not in owners:
                continue
            overlap = changed & {
                path.as_posix() for path in self.authored_by(lease, base, earlier)
            }
            if not overlap:
                continue
            concern = next(
                (item for item in state.concerns if item.id == owners[earlier]), None
            )
            if concern is None:
                continue
            await self.recheck_concern(
                concern,
                lease.root,
                situation=(
                    "A later concern has just been joined into the tree your "
                    "concern is already in, and the two changed the same "
                    "files. Re-check your concern's acceptance criteria "
                    "against this tree. A criterion you passed before may no "
                    "longer hold; say so plainly if it does not.\n\n"
                    "Files both touched:\n"
                    + "\n".join(f"- {path}" for path in sorted(overlap))
                    + f"\n\nJoined commit: {parent[:12]}\nWorktree: {lease.root}"
                ),
                occasion=f"join-{parent[:12]}",
                lost_because=f"once {parent[:12]} was joined into the same tree",
                commit=self.worktrees.head(lease),
            )

    async def recheck_criteria(
        self, state: ResolveState, integration: IntegrationRecord
    ) -> list[MaterialQuestion]:
        """Re-run each concern's reviewer against the tree its siblings built.

        ``review_turn`` runs against a concern's own worktree before
        integration, so nothing else re-checks a criterion that stops holding
        once a sibling lands. This is the only instrument aimed at "concern
        three's criterion two no longer holds now that concern seven merged"
        — the final audit is about content that went missing, which is a
        different failure.

        A failed criterion opens a question rather than failing the run,
        because later work can legitimately supersede an earlier criterion
        and only a human can say whether this did. The questions are returned
        rather than merely queued, so integration waits on the answers it is
        about to act on instead of completing around them.
        """
        integrated = {identifier: True for identifier in integration.concerns}
        examining = [concern for concern in state.concerns if concern.id in integrated]
        if not examining:
            return []
        # An integration with no commit has nothing to have examined, so a
        # record could name no tree and reuse would be against whatever the
        # reader happened to check out.
        examined = integration.commit or ""
        desk = RecheckDesk(self.questions.mailbox.root)
        settled = {
            concern.id: record
            for concern in examining
            if examined
            for record in [desk.recorded(concern.id, examined)]
            if record is not None
        }
        asking = [concern for concern in examining if concern.id not in settled]
        if settled:
            self.journal.record(
                RecheckReusedEvent(concerns=sorted(settled), commit=examined)
            )
        carried = self.questions.raised(
            [record.question_id for record in settled.values() if record.question_id]
        )
        if not asking:
            return carried
        asking_by_id = {concern.id: concern for concern in asking}
        async with self.reading_pool(integration, len(asking)) as trees:

            async def examine(opened: ActorRef) -> MaterialQuestion | None:
                """This address's concern, re-checked against the merged tree."""
                concern = asking_by_id[opened.id]
                tree = await trees.get()
                try:
                    question = await self.recheck_one(concern, tree, integration)
                finally:
                    trees.put_nowait(tree)
                # After the turn, so a re-check interrupted part way through
                # is re-run rather than recorded as having decided nothing.
                if examined:
                    desk.record(
                        RecheckRecord(
                            concern_id=concern.id,
                            commit=examined,
                            question_id="" if question is None else question.id,
                        )
                    )
                return question

            # The population's own wave rather than one assembled here, for
            # the reasons the worker wave takes it: how many agents run at
            # once is the population's cap and not this phase's, the roster
            # carries the whole re-check rather than only the turns inside
            # it, and a close reaches what is still reading.
            asked = await self.runner.actors.work_all(
                examine,
                [ActorRef(kind="reviewer", id=concern.id) for concern in asking],
            )

        def reported() -> Iterator[MaterialQuestion]:
            """What each re-check found, re-raising the ones that failed.

            The wave answers positionally and keeps every failure rather than
            propagating one, so the raise is made here instead: a re-check
            that never reported is not a re-check that found nothing, and
            integration is about to wait on exactly this list.
            """
            for outcome in asked:
                if isinstance(outcome, BaseException):
                    raise outcome
                if outcome is not None:
                    yield outcome

        return [*carried, *reported()]

    @asynccontextmanager
    async def reading_pool(
        self, integration: IntegrationRecord, wanted: int
    ) -> AsyncGenerator["asyncio.Queue[Path]"]:
        """A few checkouts of the finished tree, handed round by the readers.

        One checkout per concern would be dozens of copies of the repository
        for a phase that only reads; one shared between them has concurrent
        gates regenerating into each other. A pool of the same width the
        workers run at is the middle, and it is the width the host's
        allowance was already sized for.

        The integration worktree itself is never lent out. It is what the
        audit and the review branch are read from, and a reader running the
        project's gate in it would leave it dirty.
        """
        trees: asyncio.Queue[Path] = asyncio.Queue()
        commit = integration.commit
        width = min(self.parallel_rechecks, wanted)
        roots = [
            integration.worktree.parent / f"{integration.worktree.name}-reading-{index}"
            for index in range(width)
        ]
        made: list[Path] = []  # lup: ignore[empty-collection] — cleanup ledger
        try:
            for root in roots:
                if commit is None:
                    # Nothing to check out from, so the readers share the tree
                    # they would otherwise have copied. Only reachable before
                    # the join has committed anything.
                    trees.put_nowait(integration.worktree)
                    continue
                self.worktrees.discard_checkout(root)
                self.worktrees.read_only_checkout(root, commit)
                made.append(root)
                trees.put_nowait(root)
            yield trees
        finally:
            for root in made:
                self.worktrees.discard_checkout(root)

    async def recheck_one(
        self, concern: Concern, tree: Path, integration: IntegrationRecord
    ) -> MaterialQuestion | None:
        """Ask one concern's reviewer whether it still holds in the merged tree."""
        return await self.recheck_concern(
            concern,
            tree,
            situation=(
                "Every concern in this run is now integrated into one tree. "
                "Re-check your concern's acceptance criteria against that "
                "tree rather than the worktree you reviewed. A criterion you "
                "passed before may no longer hold now that a sibling has "
                "landed; say so plainly if it does not.\n\n"
                f"Integrated concerns: {', '.join(integration.concerns)}\n"
                f"Worktree: {tree}"
            ),
            occasion="integrated",
            lost_because="once every sibling is integrated",
            commit=integration.commit,
        )

    async def settle_rechecks(
        self, asked: list[MaterialQuestion]
    ) -> list[RecheckRuling]:
        """Wait for these re-checks, and report what each was ruled.

        Queuing publishes a question; it does not wait for one. A run that
        only queues reaches the end of integration with its re-checks
        unanswered, marks the branch complete, and finishes — a `regression`
        ruling arriving afterwards is recorded into a run that has already
        shipped it. Waiting parks the run instead, which costs nothing to
        resume and makes the answer arrive before the decision it governs.

        Only the final pass waits. A per-join standing check already has a
        consumer — it is what stops the same finding being re-asked join
        after join — and every concern it examines is examined again here,
        against the finished tree, where the answer can still change what
        happens.
        """
        if not asked:
            return asked_rulings(asked, [])
        answers = await self.questions.await_questions(asked)
        return asked_rulings(asked, answers.answers)

    async def recheck_concern(
        self,
        concern: Concern,
        worktree: Path,
        *,
        situation: str,
        occasion: str,
        lost_because: str,
        commit: str | None = None,
    ) -> MaterialQuestion | None:
        """Ask one concern's reviewer whether its criteria still hold here.

        A failed criterion opens a question rather than failing the run,
        because later work can legitimately supersede an earlier criterion
        and only a human can say whether this did. The question is keyed by
        occasion so the same concern examined after two different joins asks
        twice rather than colliding on one id — the second failure is its own
        fact, and it names a different join.
        """
        identity = RecheckIdentity(
            concern=concern, occasion=occasion, commit=commit or ""
        )
        desk = RecheckDesk(self.questions.mailbox.root)
        cached = desk.verdict(identity) if commit else None
        if cached is not None:
            self.journal.record(
                RecheckReusedEvent(concerns=[concern.id], commit=commit or "")
            )
            if cached.question is not None:
                self.questions.queue_questions([cached.question], concern.id)
            return cached.question
        reviewer = ActorRef(kind="reviewer", id=concern.id)
        declared = {criterion.id: True for criterion in concern.criteria}
        # The reviewer session may be fresh — a resumed run, a parked actor —
        # so the concern record rides with the prompt rather than being
        # assumed remembered. Criteria reconstructed by archaeology produced
        # labels no declared id matched, and every mismatch read as a loss.
        prompt = (
            f"{situation}\n\n"
            "Your concern's persisted record, including the acceptance "
            f"criteria to re-check:\n{concern.model_dump_json(indent=2)}\n\n"
            "Report criteria_met using exactly the declared criterion ids "
            "above — echo an id verbatim when its criterion still holds, and "
            "omit it when it does not."
        )
        result = await self.runner.reviewer_round(
            reviewer, worktree, prompt, ReviewReport
        )
        unknown = [
            label for label in result.output.criteria_met if label not in declared
        ]
        if unknown:
            correction = (
                "Your report labelled criteria this concern never declared: "
                + ", ".join(unknown)
                + ". Resubmit with criteria_met drawn only from the declared "
                "ids: " + ", ".join(declared)
            )
            result = await self.runner.reviewer_round(
                reviewer,
                worktree,
                correction,
                ReviewReport,
            )
        met = {identifier: True for identifier in result.output.criteria_met}
        lost = [
            criterion.id for criterion in concern.criteria if criterion.id not in met
        ]
        for previous in self.questions.mailbox.questions():
            if (
                previous.question.concern_id == concern.id
                and previous.question.criteria
                and sorted(previous.question.criteria) != sorted(lost)
            ):
                self.journal.record(
                    RecheckChangedEvent(
                        concern_id=concern.id,
                        occasion=occasion,
                        commit=commit or "",
                        previous_question=previous.question,
                        criteria=sorted(lost),
                    )
                )
        if not lost:
            if commit:
                desk.preserve(
                    RecheckVerdict(
                        identity=identity, report=result.output, question=None
                    )
                )
            return None
        if self.standing_ruling_exists(
            concern.id, lost, commit=commit if occasion == "integrated" else None
        ):
            self.journal.record(
                RecheckRepeatedEvent(
                    concern_id=concern.id, occasion=occasion, criteria=sorted(lost)
                )
            )
            if commit:
                desk.preserve(
                    RecheckVerdict(
                        identity=identity, report=result.output, question=None
                    )
                )
            return None
        question = MaterialQuestion(
            id=identity.question_id(lost),
            concern_id=concern.id,
            prompt=(
                f"{concern.id} no longer meets {', '.join(lost)} "
                f"{lost_because}. The reviewer says: "
                f"{result.output.reason}. Was this criterion "
                "superseded by later work, or is this a regression?"
            ),
            choices=SupersessionRuling.choices(),
            closed_choices=True,
            criteria=sorted(lost),
            recheck_commit=commit,
        )
        if commit:
            desk.preserve(
                RecheckVerdict(
                    identity=identity, report=result.output, question=question
                )
            )
        self.questions.queue_questions([question], concern.id)
        return question

    def standing_ruling_exists(
        self, concern_id: str, lost: list[str], *, commit: str | None = None
    ) -> bool:
        """Whether this lost-criteria set was already put to the humans.

        The same standing finding reproduced by join after join asked five
        identical questions in one run. One open question, or one answered
        "superseded", settles the set; only a "regression" ruling — whose
        remediation makes a later identical loss a genuinely new fact —
        lets the question be asked again.
        """
        state = self.run.state
        if state is None or state.questions is None:
            return False
        lost_map = {identifier: True for identifier in lost}
        answered = {
            answer.question_id: answer.value
            for answer in (state.answers.answers if state.answers else [])
        }
        for question in state.questions.questions:
            if question.id in state.retired_questions:
                continue
            if question.concern_id != concern_id or not question.criteria:
                continue
            if commit is not None and (
                question.recheck_commit not in (None, commit)
                or question.id == f"{concern_id}-superseded-integrated"
            ):
                continue
            if {identifier: True for identifier in question.criteria} != lost_map:
                continue
            ruling = answered[question.id] if question.id in answered else None
            if ruling != SupersessionRuling.REGRESSION:
                return True
        return False
