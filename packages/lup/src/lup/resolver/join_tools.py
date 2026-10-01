"""The verbs a merger joins a whole set of parents through.

A join run as a loop the orchestrator owns merges one parent, spends a
merger turn where the merge needs deciding, commits, verifies,
checkpoints, and goes round again. One session sees every parent, but it
meets each one cold — the merger learns that three branches all rewrite
the same module at the ninth of them, with two already resolved in ways it
would not have chosen knowing the third.

Here the merger is handed every parent at once and drives its own
sequence, the way somebody landing a stack of branches reads the whole
survey before touching the first. What such a loop owns does not go with
it: the checkpoint, the verification and the drain are verbs the merger
calls, so a join is resumable at every parent, a red gate names the parent
that turned it red, and a drain is observed between two of them.

The accounting moves the same way and gets stronger for it. An
orchestrator checking a report after the fact spends a correction
turn when it falls short; ``land_parent`` refuses instead, naming what is
unaccounted for while the merger is still on the parent it belongs to.

The handlers touch nothing but the run directory, the lease they are bound
to, and git — so one factory serves both transports, exactly as the
question tools do.
"""

from pathlib import Path

from pydantic import BaseModel, Field

from lup.execution.process import LocalProcessLauncher, ProcessLauncher
from lup.tools.mcp import LupMcpTool, ToolError, lup_tool
from lup.resolver.join_desk import JoinDesk, JoinLanding, JoinPlan, JoinTip
from lup.resolver.mailbox import QuestionMailbox
from lup.resolver.models import (
    CarriedParent,
    DeclaredEdit,
    DropCandidate,
    HunkDisposition,
    MergeReport,
    WritableRootLease,
)
from lup.resolver.orchestrator import WorktreeOrchestrator
from lup.resolver.verification import Verifier


def names_parent(declared: str, parent: str) -> bool:
    """Whether a disposition's commit names this candidate's parent.

    Prefix rather than equality, because the merger is shown each candidate
    abbreviated and keyed the full sha against it. Echoing back the twelve
    characters it was given then read as having said nothing, and the refusal
    quoted those same twelve characters at it, so there was no revision that
    could converge: one observed merger dispositioned all three candidates
    with correct rationales, twice, and the run failed on the second.
    """
    return bool(declared) and (
        parent.startswith(declared) or declared.startswith(parent)
    )


def merge_problems(
    merge: MergeReport, conflicted: list[Path], owed: list[DropCandidate]
) -> list[str]:
    """Every obligation this merge report left unmet.

    Two obligations rather than two prohibitions. Every candidate the
    detector raised must be dispositioned — containment, never equality,
    because a legitimate resolution rewrites hunks and requiring the exact
    candidate set back would reject the right answer. That holds for how a
    disposition is keyed as much as for which ones are owed: it is matched
    against the abbreviation the merger was shown rather than the sha it was
    not. And every edit outside the conflict set must be declared, because
    that is where a silent override lives: the merger holds an
    already-correct tree with unrestricted write access, and the canonical
    joint failure is fixed in a file that never conflicted.
    """
    undispositioned = sorted(
        f"{candidate.path.as_posix()} from {candidate.parent[:12]}"
        for candidate in owed
        if not any(
            disposition.path.as_posix() == candidate.path.as_posix()
            and names_parent(disposition.parent, candidate.parent)
            for disposition in merge.dispositions
        )
    )
    unreasoned = sorted(
        disposition.path.as_posix()
        for disposition in merge.dispositions
        if not disposition.rationale.strip()
    )
    declared = {edit.path.as_posix() for edit in merge.out_of_conflict_edits}
    conflicting = {path.as_posix() for path in conflicted}
    undeclared = sorted(
        disposition.path.as_posix()
        for disposition in merge.dispositions
        if disposition.fate in {"rewritten", "superseded", "dropped"}
        and disposition.path.as_posix() not in conflicting
        and disposition.path.as_posix() not in declared
    )
    return [
        *(
            [f"content lost with nothing said about it: {', '.join(undispositioned)}"]
            if undispositioned
            else []
        ),
        *(
            [f"dispositioned without a rationale: {', '.join(unreasoned)}"]
            if unreasoned
            else []
        ),
        *(
            [f"changed outside the conflict set undeclared: {', '.join(undeclared)}"]
            if undeclared
            else []
        ),
    ]


class PreparedJoin(BaseModel, frozen=True):
    """What preparing one parent found, kept until its landing records it."""

    conflicted: bool = False
    rendered: bool = False


class StartParentInput(BaseModel):
    """Which parent to prepare a merge for."""

    commit: str = Field(
        description=(
            "The parent to merge next, as it appears in your plan. Any prefix "
            "that names exactly one of them is accepted."
        )
    )


class LandParentInput(BaseModel):
    """One prepared merge, accounted for and ready to commit."""

    commit: str = Field(description="The parent you prepared with start_parent.")
    summary: str = Field(description="What this join did, in one or two sentences.")
    dispositions: list[HunkDisposition] = Field(
        default=[],
        description=(
            "What became of each piece of content this parent contributed "
            "that the merged tree no longer holds. Every candidate named by "
            "a refusal must appear here, with a reason."
        ),
    )
    out_of_conflict_edits: list[DeclaredEdit] = Field(
        default=[],
        description=(
            "Every file you edited that was not in the conflict set, with the "
            "reason. Fixing a caller whose file merged clean is correct and "
            "expected; leaving it undeclared is the rejection."
        ),
    )


class JoinStatusInput(BaseModel):
    """Nothing: where the join stands is not a question with parameters."""


class JoinReport(BaseModel, frozen=True, extra="forbid"):
    """The merger's account of one turn spent joining a set of parents.

    Narrative rather than authoritative. What actually landed is whatever
    ``land_parent`` recorded, because a report can be wrong about its own
    turn and the checkpoint cannot — it is written by the verb that made
    the commit. This is where the merger says what it decided and why, and
    what it wants whoever reads the run to know.
    """

    plan: str = Field(
        description=(
            "The order you chose and why — which parents you expected to "
            "contest each other, and what you decided to do about it."
        )
    )
    summary: str = Field(description="What this turn joined, and what it settled.")
    stopped_for_drain: bool = Field(
        default=False,
        description="Whether you stopped because land_parent asked you to.",
    )
    blocked: str = Field(
        default="",
        description=(
            "Why you stopped with parents still on the table, when nothing "
            "asked you to. An incompletion with a cause is answerable; one "
            "without a cause reads as an unexplained failure. An unfinished join "
            "with a blocker and no pending question opens a recovery question "
            "containing this complete text."
        ),
    )


class StartParentOutput(BaseModel, frozen=True):
    """What a prepared merge left to decide."""

    commit: str
    state: str
    conflicted: list[Path] = []
    rendered_settled: bool = False
    guidance: str = ""


class LandParentOutput(BaseModel, frozen=True):
    """Whether the parent landed, and what is owed if it did not."""

    commit: str
    landed: bool
    head: str = ""
    joined: int = 0
    planned: int = 0
    problems: list[str] = []
    unaccounted: list[DropCandidate] = []
    broke: list[str] = []
    drain_requested: bool = False
    guidance: str = ""


class JoinStatusOutput(BaseModel, frozen=True):
    """Where the join stands, for a session that has to re-establish it."""

    head: str
    joined: list[str] = []
    remaining: list[JoinTip] = []
    carried: list[CarriedParent] = []
    drain_requested: bool = False


def resolve_tip(plan: JoinPlan, commit: str) -> JoinTip:
    """The one planned tip this abbreviation names, or a refusal saying so.

    Prefix rather than equality for the same reason a disposition is keyed
    that way: the merger reads the plan abbreviated, and echoing back what it
    was shown must not read as naming nothing.
    """
    matched = [
        tip
        for tip in plan.tips
        if tip.commit.startswith(commit) or commit.startswith(tip.commit)
    ]
    if not matched:
        named = ", ".join(tip.commit[:12] for tip in plan.tips)
        raise ToolError(
            f"{commit} is not one of this join's parents. On the table: {named}"
        )
    if len(matched) > 1:
        raise ToolError(
            f"{commit} names {len(matched)} of this join's parents; say more of it"
        )
    return matched[0]


def create_join_tools(
    run_dir: Path,
    lease_root: Path,
    concern_id: str,
    *,
    launcher: ProcessLauncher | None = None,
) -> list[LupMcpTool]:
    """Build the join verbs bound to one lease's plan.

    The lease is bound here rather than taken as an argument, so a merger
    structurally cannot land a parent into somebody else's tree.
    """
    desk = JoinDesk(run_dir, concern_id)
    mailbox = QuestionMailbox(run_dir)
    process = launcher if launcher is not None else LocalProcessLauncher()
    worktrees = WorktreeOrchestrator(process, lease_root)
    prepared: dict[str, PreparedJoin] = {}
    """What preparing each parent found, as start_parent found it.

    Kept for the landing record, which is written after the resolution has
    been staged and so can observe neither the conflict nor the regeneration
    that settled it. A session resumed mid-join re-prepares whatever it had
    not landed, so an entry is present whenever a landing follows one.
    """

    def lease_of(plan: JoinPlan) -> WritableRootLease:
        return WritableRootLease(
            concern_id=plan.concern_id, root=plan.worktree, branch=""
        )

    def require_plan() -> JoinPlan:
        plan = desk.plan()
        if plan is None or plan.concern_id != concern_id:
            raise ToolError(
                "no join plan is on the table for this lease; the orchestrator "
                "writes one before it calls you"
            )
        return plan

    @lup_tool(
        "Prepare the merge of one parent from your plan, and report what it "
        "left to decide. Rendered artifacts are settled by the generator "
        "before you see the result, so a difference in one is never yours to "
        "resolve. Stage every resolution with `git add`; do not commit — "
        "land_parent does that, and it is what checks your accounting.",
        name="start_parent",
    )
    async def start_parent(params: StartParentInput) -> StartParentOutput:
        plan = require_plan()
        tip = resolve_tip(plan, params.commit)
        lease = lease_of(plan)
        if worktrees.already_joined(lease, tip.commit):
            return StartParentOutput(
                commit=tip.commit,
                state="already-in-tree",
                guidance=(
                    "This parent is already contained in the tree, so there is "
                    "nothing to merge and it owes no dispositions. Record it "
                    "with land_parent and move on."
                ),
            )
        head = worktrees.head(lease)
        conflicted = worktrees.prepare_join(lease, [head, tip.commit])
        rendered = False
        if conflicted and plan.regeneration:
            rendered = worktrees.settle_generated(lease, plan.regeneration)
            conflicted = bool(worktrees.conflicted_paths(lease))
        prepared[tip.commit] = PreparedJoin(conflicted=conflicted, rendered=rendered)
        return StartParentOutput(
            commit=tip.commit,
            state="conflicted" if conflicted else "clean",
            conflicted=worktrees.conflicted_paths(lease) if conflicted else [],
            rendered_settled=rendered,
            guidance=(
                "Resolve each conflicted path, stage it, then call land_parent."
                if conflicted
                else "Nothing conflicted. Call land_parent to commit and verify it."
            ),
        )

    @lup_tool(
        "Commit the prepared merge, check what it accounted for, verify the "
        "tree, and checkpoint the join. Refuses and names what is owed if "
        "content this parent contributed went missing with nothing said about "
        "it, or a file outside the conflict set was edited undeclared — fix "
        "what it names and call it again. Read drain_requested on the way "
        "out: when it is true, stop after this parent and end your turn.",
        name="land_parent",
    )
    async def land_parent(params: LandParentInput) -> LandParentOutput:
        plan = require_plan()
        tip = resolve_tip(plan, params.commit)
        lease = lease_of(plan)
        progress = desk.progress()
        before = worktrees.head(lease)
        if tip.commit not in progress.joined and worktrees.already_joined(
            lease, tip.commit
        ):
            # In the tree without this run having put it there — a resume
            # re-entering a join a previous one got part way through. Its
            # account was settled when it landed, and asking for one again
            # would have the merger re-adjudicate somebody else's decisions
            # against a tree that has since moved past them.
            desk.record(
                JoinLanding(commit=tip.commit, head=before, merged=False),
                [planned.commit for planned in plan.tips],
            )
            return LandParentOutput(
                commit=tip.commit,
                landed=True,
                head=before,
                joined=len(progress.joined) + 1,
                planned=len(plan.tips),
                drain_requested=mailbox.draining() is not None,
                guidance=(
                    "Already in the tree from an earlier run, and accounted "
                    "for when it landed. Recorded; start the next parent."
                ),
            )
        try:
            head = worktrees.commit_join(lease, plan.title)
        except RuntimeError as unresolved:
            # A marker left in the content, or a path still unmerged in the
            # index. Handed back rather than raised: the merger is holding
            # the tree that has it and is the one who can settle it, where
            # ending the run makes a human do the same work later.
            return LandParentOutput(
                commit=tip.commit,
                landed=False,
                joined=len(progress.joined),
                planned=len(plan.tips),
                problems=[str(unresolved)],
                guidance=(
                    "The tree still carries an unresolved merge. Settle every "
                    "path it names, stage it with `git add`, and call "
                    "land_parent again."
                ),
            )
        fork = worktrees.merge_base(lease, before, tip.commit)
        owed = worktrees.drop_candidates(lease, fork, tip.commit, head)
        report = MergeReport(
            completed=True,
            summary=params.summary,
            dispositions=params.dispositions,
            out_of_conflict_edits=params.out_of_conflict_edits,
        )
        problems = merge_problems(report, worktrees.conflicted_paths(lease), owed)
        if problems:
            return LandParentOutput(
                commit=tip.commit,
                landed=False,
                head=head,
                joined=len(progress.joined),
                planned=len(plan.tips),
                problems=problems,
                unaccounted=owed,
                guidance=(
                    "The tree is committed but the account is short. Where the "
                    "tree is wrong, fix it and stage the fix; where it is "
                    "right, say so in a disposition or a declared edit. Then "
                    "call land_parent again with the whole account — it "
                    "re-reads the tree, so a correction commits on top."
                ),
            )
        broke = [
            record.name
            for record in Verifier(list(plan.verification), process).verify(
                lease.root, plan.base
            )
            if not record.passed
        ]
        found = prepared.get(tip.commit, PreparedJoin())
        desk.record(
            JoinLanding(
                commit=tip.commit,
                head=head,
                conflicted=found.conflicted,
                rendered=found.rendered,
                broke=broke,
            ),
            [planned.commit for planned in plan.tips],
        )
        drain = mailbox.draining() is not None
        return LandParentOutput(
            commit=tip.commit,
            landed=True,
            head=head,
            joined=len(progress.joined) + 1,
            planned=len(plan.tips),
            broke=broke,
            drain_requested=drain,
            guidance=(
                f"This parent broke: {', '.join(broke)}. Fix it in this tree "
                "and call land_parent again — the failure belongs to this "
                "join, which is why it is reported here and not at the end."
                if broke
                else "Stop here and end your turn; the run has been asked to drain."
                if drain
                else "Landed. Start the next parent in your plan."
            ),
        )

    @lup_tool(
        "Where this join stands: what is in the tree, what is left, and "
        "whether a drain is waiting. Call it first when you have just been "
        "resumed and do not know what your predecessor landed.",
        name="join_status",
    )
    async def join_status(_params: JoinStatusInput) -> JoinStatusOutput:
        plan = require_plan()
        progress = desk.progress()
        landed = {commit for commit in progress.joined}
        return JoinStatusOutput(
            head=worktrees.head(lease_of(plan)),
            joined=list(progress.joined),
            remaining=[tip for tip in plan.tips if tip.commit not in landed],
            carried=list(plan.carried),
            drain_requested=mailbox.draining() is not None,
        )

    return [start_parent, land_parent, join_status]
