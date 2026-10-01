"""Branch analysis: containment, PR status, base detection, freshness, PR bodies."""

import json
import logging
import sys
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from collections.abc import Set as AbstractSet
from itertools import groupby
from operator import attrgetter
from pathlib import Path, PurePosixPath
from typing import Literal, NoReturn, Required, TypedDict
from urllib.parse import urlparse

import sh
import typer
from pydantic import BaseModel, Field

from lup.execution.git import GitError, Repository
from lup.harness.environment import non_interactive_environment
from lup.workspace.checkout_state import CheckoutState
from lup.execution.process import ProcessLauncher
import lup.devtools.dev.records as records
import lup.devtools.dev.traces as traces
from lup.devtools.dev.remote_auth import (
    check_remote_auth,
    origin_auth_complaint,
    remote_auth_refusal,
)
from lup.sandbox.observed import is_mount_point
from lup.resolver.models import HeldLease
from lup.resolver.state import live_lease_branches
from lup.types import StringMap
from lup.workspace.paths import project_root
from lup.execution.shell import git
from lup.devtools.utils import (
    format_table,
    gh,
    config_lock_diagnosis,
    attributed_stderr,
    decode_stderr,
    output_json,
    repository_arguments,
    short_sha,
)

logger = logging.getLogger(__name__)


class PRStatus(BaseModel):
    """One PR row as `gh pr list --json` returns it (aliases are gh's names)."""

    number: int
    title: str = ""
    state: str = ""
    merged_at: str | None = Field(default=None, alias="mergedAt")
    head_ref: str = Field(default="", alias="headRefName")
    url: str = ""

    def reusable(self) -> bool:
        """Whether a retirement can close this rather than opening its own.

        Only one still open. A request that already reached a terminal state
        cannot be closed again — GitHub refuses the transition outright for a
        merged one — so a retirement that reused whichever was most recent
        would push the branch and then fail at the close, leaving the work
        half-moved. A fresh request over the same head is allowed once the
        previous is no longer open, and the old one keeps its own head ref
        regardless, so nothing it preserved is lost by opening another.
        """
        return self.state == "OPEN"


type BranchStatus = Literal[
    "LAND", "COMMIT", "DELETE", "STALE", "KEEP", "CURRENT", "UNRELATED", "NOT_FOUND"
]


class Disposition(BaseModel):
    """The one verb a branch resolves to, and the reason it got it."""

    status: BranchStatus
    reason: str


class WorktreeChanges(BaseModel):
    """What a worktree holds that removing it would discard."""

    modified: int = 0
    untracked: int = 0

    def dirty(self) -> bool:
        return bool(self.modified or self.untracked)

    def summary(self) -> str:
        return f"{self.modified} modified, {self.untracked} untracked"

    def compact(self) -> str:
        """The same count in a table cell, where a sentence would not fit."""
        if not self.dirty():
            return "-"
        return " ".join(
            [
                *([f"{self.modified}M"] if self.modified else []),
                *([f"{self.untracked}U"] if self.untracked else []),
            ]
        )


class BranchInfo(BaseModel):
    name: str
    commit: str
    tracking: str | None
    worktree: str | None
    is_current: bool
    contained_in: list[str]
    pr: PRStatus | None
    unique_commits: int
    source_diff_lines: int
    disposition: BranchStatus
    reason: str
    rewritten: int = 0
    """How many unique commits already name a subject in the integration branch.

    Never a disposition, and never subtracted from ``unique_commits``. A
    containment check is decided by patch-id, which a rewrite changes, so a
    commit that landed rebased or reworded reads as unlanded ever after and
    a pre-rewrite snapshot presents its whole history as work at risk. A
    shared subject is the cheapest signal that this happened and is not
    proof it did, so it is carried where a reader will see it and nowhere a
    verb is decided — the wrong direction to be wrong in is the one that
    marks real work landed.
    """
    behind: int = 0
    """How many commits the integration branch holds that this branch lacks.

    A signal, never a verdict, on the same footing as :attr:`rewritten`. It
    answers what a disposition cannot: whether the base under a branch is one
    a reader would still want to start from. Every merge changes this figure
    for every branch at once, which is exactly why no verb may read it — and
    why a reader has to, since a clean workspace whose base has gone stale is
    one nobody has opened, and saying nothing is how it stays reserved for a
    session that is not coming.
    """
    on_remote: bool | None = None
    """Whether a remote carries a branch of this name, None where none was read.

    A signal beside the verb, like :attr:`behind`. Work a remote does not
    carry is one disk away from gone, and nothing else in a row says so: a
    ``LAND`` branch pushed long ago and one that exists only here read
    alike, and so does an integration branch whose history reached no
    remote at all.
    """
    changes: WorktreeChanges | None = None
    """What this branch's worktree holds uncommitted, where it has one.

    Carried beside the disposition as well as feeding it, because the two ask
    different things of it. For most verbs this is the cost of acting rather
    than the action — a ``DELETE`` whose worktree is dirty still deletes, but
    refuses until forced, and a reader handed the disposition alone plans a
    step that will stop. On a reserved workspace it *is* the action: one
    nobody has started is a workspace to leave alone, and the same workspace
    holding uncommitted work is the one shape a sweep exists to surface,
    since nothing else in the repository records that work.
    """


class RemoteBranchInfo(BaseModel):
    """A branch on a remote that no local branch corresponds to.

    A sweep classifies what ``refs/heads`` holds, so a branch whose local
    copy was deleted once its work landed leaves nothing behind to resolve
    to a verb — it stops being mentioned rather than being reported done.
    The remote keeps it regardless, and the next sweep is blind to it in
    exactly the same way, which is the one shape a sweep exists to surface
    and the one it could not express.

    Kept apart from ``BranchInfo`` rather than folded into it: that model
    answers what to do with a checkout, and its worktree, lease, and
    dirty-state figures describe things a ref on a remote cannot have.
    Reporting one through the other would mean carrying a row whose every
    such column is a placeholder, which reads as measured and is not.
    """

    name: str
    remote: str
    commit: str
    contained_in_integration: bool
    pr: PRStatus | None
    unique_commits: int
    disposition: BranchStatus
    reason: str

    def qualified(self) -> str:
        """How git names this ref, which is also how a comparison must spell it."""
        return f"{self.remote}/{self.name}"

    def delete_command(self) -> str:
        """The push that removes it, for a reader who has to run it.

        Spelled against the branch rather than the tracking ref: a delete
        names what the remote calls the branch, and the ``remote/`` prefix
        that identifies it locally is not part of that name.
        """
        return f"git push {self.remote} --delete {self.name}"


class RunHold(BaseModel):
    """One resolver run and the branches it is still holding out of the sweep.

    A sweep reads this before it reads the branch list. A run that is still
    working answers for its own branches, so they are somebody's business
    already; a run that died answers for nothing, and the branches it holds
    are work with no owner and no verb — the one shape a sweep exists to
    surface and cannot express as a per-branch disposition.
    """

    run_id: str
    alive: bool
    branches: list[str]


class IntegrationStanding(BaseModel):
    """Where the local integration branch stands against origin's copy of it.

    Every disposition is judged against the local copy, so a sweep is only as
    right as that copy is current, and the two ways it can be wrong wrong
    every row differently. Commits only origin's copy holds — a pull request
    merged on the forge — make the work they carry read as unlanded here.
    Commits only the local copy holds are on no remote at all, and a sweep
    that lands more on top of them builds on history nobody else has.
    """

    branch: str
    remote: str = ""
    """Origin's copy, empty where origin carries no branch of that name."""

    ahead: int = 0
    """Commits the local copy holds that origin's lacks: on no remote yet."""

    behind: int = 0
    """Commits origin's copy holds that the local one lacks: not pulled yet."""

    def lines(self) -> list[str]:
        """What a reader of the table has to hear, which is nothing when level."""
        if not self.remote:
            return [
                f"{self.branch} exists only here: origin carries no {self.branch}, "
                "so none of its history is on a remote"
            ]
        return [
            *(
                [
                    f"{self.remote} holds {self.behind} commit(s) {self.branch} "
                    "lacks: every row is judged against the local copy, so bring "
                    "it level before acting on one"
                ]
                if self.behind
                else []
            ),
            *(
                [
                    f"{self.branch} holds {self.ahead} commit(s) {self.remote} "
                    "lacks: they are on no remote until pushed"
                ]
                if self.ahead
                else []
            ),
        ]


def integration_standing(integration: str) -> IntegrationStanding:
    """Count the local integration branch against origin's copy, both ways."""
    remote = f"origin/{integration}"
    if Repository(Path.cwd()).resolves(remote) is None:
        return IntegrationStanding(branch=integration)
    return IntegrationStanding(
        branch=integration,
        remote=remote,
        ahead=count_commits_behind(remote, integration),
        behind=count_commits_behind(integration, remote),
    )


class SurveyResult(BaseModel):
    integration_branch: str
    current_branch: str
    branches: list[BranchInfo]
    integration_remote: IntegrationStanding | None = None
    """How the integration branch stands against origin's copy, None unread.

    Read before any row: every disposition below is judged against the local
    copy, and this says whether that copy is one a sweep can trust.
    """
    runs: list[RunHold] = []
    remote_branches: list[RemoteBranchInfo] = []
    """Branches on a remote that no local branch corresponds to.

    Separate from ``branches`` so a consumer reading local dispositions is
    not handed rows it has no verb for, and present at all so the ones whose
    work already landed stop being invisible to the sweep that would clear
    them.
    """

    remotes_fetched: bool = True
    """Whether the remotes were read before the rows above were derived.

    An empty ``remote_branches`` has two opposite meanings — a repository
    with nothing stranded on a remote, and a repository whose fetch never
    answered — and the list alone spells them identically. ``/lup:land``
    reads that list to empty the one bucket a local sweep cannot see, so a
    failed fetch reported as an empty list skips the step silently and
    leaves exactly the branches it exists to clear. Reported rather than
    raised, because every local disposition here is still correct.
    """

    fetch_complaint: str = ""
    """Why the remotes were not read, empty when they were.

    Carried beside the flag rather than left on stderr, which is where the
    fetch's own message went and where a reader of the JSON never looks.
    """


def runs_holding(leased: dict[str, HeldLease]) -> list[RunHold]:
    """Group every held branch under the run answerable for it."""
    ordered = sorted(leased.values(), key=attrgetter("run_id"))
    return [
        RunHold(
            run_id=run_id,
            alive=held[0].alive,
            branches=sorted(lease.branch for lease in held),
        )
        for run_id, group in groupby(ordered, key=attrgetter("run_id"))
        if (held := list(group))
    ]


def leased_on_disk(
    lease_of: Mapping[str, HeldLease], branch_names: Iterable[str]
) -> dict[str, HeldLease]:
    """The held leases a branch survey can say anything about.

    A run records every branch it ever leased, and a completed run keeps
    reporting the ones its cleanup did not delete. Only a branch still in
    ``refs/heads`` can be landed, kept, or dropped, so every reader of the
    lease record meets it through this intersection — one that read the
    record alone would count a run's deleted branches as work still held.
    """
    return {name: lease_of[name] for name in branch_names if name in lease_of}


class BranchClassification(TypedDict, total=False):
    branch: Required[str]
    status: Required[BranchStatus]
    reason: Required[str]
    worktree: str | None
    pr: str | int
    pr_url: str


class ParsedBranch(TypedDict):
    """One local branch row from ``git for-each-ref``."""

    name: str
    commit: str
    tracking: str | None
    is_current: bool


def parse_branches() -> list[ParsedBranch]:
    """Structured local-branch rows via ``git for-each-ref``.

    NUL-separated ``--format`` fields are the machine interface — no marker
    columns or bracket surgery; ``%(HEAD)`` is ``*`` exactly for the branch
    checked out in the current worktree.
    """

    def parse(row: str) -> ParsedBranch:
        name, commit, upstream, head = row.split(  # lup: ignore[string-split] — NUL
            "\x00"
        )
        return {
            "name": name,
            "commit": commit,
            "tracking": upstream or None,
            "is_current": head == "*",
        }

    def published(branch: ParsedBranch) -> ParsedBranch:
        """The row with the remote lup recorded, where git's config names none.

        A branch `git pr push` published carries no tracking configuration,
        because the push states its destination instead of asking git to
        write one. Read from git alone the row says the branch answers to
        nothing, which is the report for a branch nobody ever sent anywhere.
        """
        if branch["tracking"]:
            return branch
        recorded = records.recorded_upstream(branch["name"])
        return {**branch, "tracking": recorded or None}

    return [
        published(parse(row))
        for row in git.lines(
            "for-each-ref",
            "refs/heads",
            "--format=%(refname:short)%00%(objectname:short)"
            "%00%(upstream:short)%00%(HEAD)",
        )
    ]


def parse_worktrees() -> dict[str, str]:  # lup: ignore[dict-str-payload]
    """Map branch name -> worktree path from ``git worktree list --porcelain``.

    Open, data-driven keys: whatever branches have worktrees.
    """
    return {
        listed.branch: str(listed.path)
        for listed in Repository(Path.cwd()).worktrees()
        if listed.branch
    }


class ParsedRemoteBranch(TypedDict):
    """One remote-tracking ref, split into the remote and the branch on it."""

    remote: str
    name: str
    commit: str


def fetch_remote_tracking() -> None:
    """Refresh remote-tracking refs, naming the refspec rather than assuming one.

    ``git clone --bare`` configures no ``remote.<name>.fetch``, so a plain
    ``git fetch --prune`` in such a clone writes ``FETCH_HEAD`` and nothing
    else: ``refs/remotes`` stays empty, and every branch that exists only on
    the remote is invisible to whatever reads it. The failure is silent in
    the worst direction — the survey that follows looks complete, because a
    branch it cannot see is indistinguishable from one that is not there.

    Naming the standard refspec is what a non-bare clone already does from
    its own configuration, so the repaired clone is the only one whose
    behaviour changes, and nothing is written to the repository's config to
    do it. Failures propagate to the caller, which already logs a fetch it
    could not complete and surveys the refs it has.
    """
    for remote in git.lines("remote"):
        git("fetch", "--prune", remote, f"+refs/heads/*:refs/remotes/{remote}/*")


def parse_remote_branches() -> list[ParsedRemoteBranch]:
    """Structured remote-tracking rows via ``git for-each-ref``, per remote.

    Iterating the remotes and stripping a known prefix length keeps the
    remote and the branch separate without splitting a ref name on ``/`` —
    a branch is allowed to contain one, so surgery on the joined form would
    report ``feat`` for ``origin/feat/x`` and be wrong exactly where branch
    names are most conventional.

    ``%(symref)`` is set for ``origin/HEAD`` alone, which names another ref
    rather than a branch of its own: counting it would report the default
    branch a second time, under a name no push can delete.
    """

    def parse(remote: str, row: str) -> ParsedRemoteBranch | None:
        name, commit, symref = row.split("\x00")  # lup: ignore[string-split] — NUL
        if symref:
            return None
        return {"remote": remote, "name": name, "commit": commit}

    return [
        parsed
        for remote in git.lines("remote")
        for row in git.lines(
            "for-each-ref",
            f"refs/remotes/{remote}",
            "--format=%(refname:lstrip=3)%00%(objectname:short)%00%(symref)",
        )
        if (parsed := parse(remote, row)) is not None
    ]


def build_containment(branch_names: list[str]) -> dict[str, list[str]]:
    """For each branch, find which other branches contain it."""
    repository = Repository(Path.cwd())
    return {
        branch: [
            target
            for target in branch_names
            if target != branch and repository.is_ancestor(branch, target)
        ]
        for branch in branch_names
    }


def fetch_pr_status(branch_names: list[str]) -> dict[str, PRStatus]:
    """Query GitHub for PR status of branches."""
    try:
        rows = json.loads(
            gh.out(
                "pr",
                "list",
                *repository_arguments(),
                "--state",
                "all",
                "--limit",
                "200",
                "--json",
                "number,title,headRefName,state,mergedAt,url",
            )
        )
    except sh.ErrorReturnCode as e:
        logger.warning("Failed to fetch PR status: %s", decode_stderr(e))
        return {}
    prs = [PRStatus.model_validate(row) for row in rows]
    return {pr.head_ref: pr for pr in prs if pr.head_ref in branch_names}


def count_unique_commits(branch: str, integration: str) -> int:
    """Count commits on branch not cherry-picked into integration (-1: unknown)."""
    try:
        return Repository(Path.cwd()).count(
            "--cherry-pick", "--left-only", f"{branch}...{integration}"
        )
    except GitError:
        return -1


def count_commits_behind(branch: str, integration: str) -> int:
    """Count commits the integration branch holds that ``branch`` lacks (-1: unknown).

    Reported beside a verb and never inside one. Every merge moves the
    integration branch under every other branch at once, so a disposition
    reading this figure would retire a workspace as a side effect of
    unrelated work landing — which is the trap the reserved-workspace guard
    exists to avoid, and reading the tip rather than the distance is how it
    avoids it.

    What it answers is the question left over once the verb is settled: a
    clean workspace reserved long enough for the base beneath it to go stale
    is one nobody has opened, and a sweep that never says so is how it goes
    on being reserved for a session that is not coming.
    """
    try:
        return Repository(Path.cwd()).count(f"{branch}..{integration}")
    except GitError:
        return -1


def count_source_diff_lines(branch: str, integration: str) -> int:
    """Count lines of source-file diff between branch and integration (-1: unknown).

    ``--numstat`` is the machine format: one ``added<TAB>deleted<TAB>path`` row
    per file (``-`` for binary files, which count no lines).
    """
    try:
        rows = git.lines(
            "diff",
            "--numstat",
            branch,
            integration,
            "--",
            "src/",
            ".claude/",
            "tests/",
            _ok_code=[0, 1],
        )
    except sh.ErrorReturnCode:
        return -1
    total = 0
    for row in rows:
        added, _, rest = row.partition("\t")  # lup: ignore[string-split] — numstat
        deleted = rest.partition("\t")[0]  # lup: ignore[string-split] — numstat
        total += int(added) if added.isdigit() else 0
        total += int(deleted) if deleted.isdigit() else 0
    return total


def get_integration_branch() -> str:
    """Return 'dev' if it exists locally, else 'main'."""
    from lup.devtools.dev.worktree import branch_exists

    if branch_exists("dev"):
        return "dev"
    return "main"


def shares_history(branch: str, integration: str) -> bool:
    """Whether the two have any commit in common.

    Asked because neither counter fails without one, so nothing downstream
    reports the difference. ``A...B`` degenerates to both sides entire when
    there is no merge base, and ``--left-only`` then returns a plausible
    small number; the diff is a direct two-tree comparison that never needed
    a merge base, and reports the whole distance between two unrelated trees
    as though it were a divergence. Both figures are then read as one, and a
    branch whose real relationship to the integration branch is *none*
    presents as ordinary unlanded work — which a sweep offers to rebase or
    merge, and both would replay an unrelated tree.
    """
    try:
        git("merge-base", branch, integration)
        return True
    except sh.ErrorReturnCode:
        return False


def still_at_reservation(branch: str) -> bool:
    """Whether this branch stands exactly where its workspace was reserved.

    The difference between a workspace nobody opened and a branch whose work
    landed. Both are ancestors of the integration branch, so containment
    reads them alike and calls the first spent.

    Asked of a record rather than of the graph, because the graph stops
    holding the answer. A tip on the integration branch's first-parent
    history would stand in for it — work that landed through a merge sits
    on the side parent, which never appears there — but that reads the way
    the landing was spelled rather than whether any landing happened. Work
    rebased and fast-forwarded moves the integration branch's own tip onto
    the branch, so a spent branch and an untouched workspace become the same
    graph, and no cleverer topological question separates them afterwards.

    So the fact is recorded while it is still true and compared afterwards.
    A branch standing where it was reserved has had nothing committed to it;
    the first commit moves the tip and it stops being a workspace, whatever
    the integration branch does later. Distance never enters: a sweep moves
    that branch under every workspace reserved against it, and a reservation
    compared against the recorded commit is unmoved by that, where one
    compared against the current tip would be retired by the sweep's first
    merge.

    A branch with no record is not a workspace. Reserving one writes the
    record, so its absence is a branch made another way — never somebody's
    held session, while a spent branch read as reserved is one nothing offers
    to clear again.
    """
    reserved_at = records.recorded_reservation(branch)
    try:
        return bool(reserved_at) and git.out("rev-parse", branch).strip() == reserved_at
    except sh.ErrorReturnCode:
        return False


def rewrite_suspects(branch: str, integration: str) -> list[str]:
    """This branch's commits whose subject already names one in *integration*.

    Containment is decided by patch-id, which a rewrite changes: a commit
    rebased, reworded, or squashed before it landed keeps its content and
    reads as unlanded ever after. A branch kept as a pre-rewrite snapshot
    then presents its whole history as work at risk.

    Advisory, and deliberately not a disposition. A subject is not proof —
    two commits may honestly share one — and a classifier that treated it as
    proof would mark real work landed and invite deleting it. What this
    supports is the reader's next question, which is whether to go and
    compare; what it must never do is answer it.

    Counted over the set :func:`count_unique_commits` counts, down to the
    same ``--cherry-pick --left-only`` filter, so this is a subset of that
    figure and the pair reads as "so many of those". Taken over a plain
    range instead it drew from a wider set and reported five suspects
    against four unique commits — a fraction past its own denominator, which
    is the shape a reader stops believing.
    """
    landed = {line for line in git.lines("log", "--format=%s", integration) if line}
    return [
        subject
        for subject in git.lines(
            "log",
            "--format=%s",
            "--cherry-pick",
            "--left-only",
            f"{branch}...{integration}",
        )
        if subject in landed
    ]


def get_branch_worktree(branch: str) -> str | None:
    """Return the worktree path for a branch, or None."""
    return parse_worktrees().get(branch)


def get_pr_info(branch: str) -> PRStatus | None:
    """Get PR info for a branch via gh CLI. None when there is none (or no gh).

    The repository is named rather than inferred. `gh` reads the origin
    remote to decide which repository it is talking about, and a remote
    written through an SSH alias names no host it recognizes — so every
    query failed, and this returned the same ``None`` it returns for a
    branch that genuinely has no pull request. A branch with an open PR
    then classified ``LAND``.
    """
    try:
        items = json.loads(
            gh.out(
                "pr",
                "list",
                *repository_arguments(),
                "--state=all",
                f"--head={branch}",
                "--json=number,title,state,mergedAt,url",
                "--limit=1",
                _ok_code=[0],
            )
        )
    except (sh.ErrorReturnCode, sh.CommandNotFound, json.JSONDecodeError) as error:
        logger.warning("no pull-request status for %s: %s", branch, error)
        return None
    return PRStatus.model_validate(items[0]) if items else None


PROTECTED_BRANCHES = {"main", "master", "dev", "develop"}
"""Branches no workflow here offers to delete, unless a project says otherwise.

The four names cover the two-tier and single-tier conventions in common use;
a project whose trunk is called something else passes its own set rather than
forking the functions that consult this one.
"""


def scaffold_carrier(branch: str) -> str:
    """Why the branch a project's copied half is compiled onto is never spent.

    `dev update` compiles upstream's copied half onto it and merges it from
    there, so once an update lands the branch is an ancestor of the integration
    branch — the exact shape of a branch whose work is done. It is not done:
    its tip is the merge base the next update is measured from, and deleting
    it leaves that update merging the copied half against nothing.
    """
    return (
        f"scaffold carrier: `dev update` compiles the copied half onto {branch} "
        "and its tip is the next update's merge base"
    )


def kept_whatever_it_holds(name: str, scaffold: str = "") -> str:
    """Why no verb may take ``name`` away, or nothing where one may.

    A protected branch, or ``scaffold``, the branch the project's copied half
    is compiled onto. Deleting and retiring both end in a deletion, so both
    ask this before anything else and no force lifts the answer: the sweep
    that reads these as spent is exactly the reader that must not act on it.
    """
    if name == scaffold:
        return scaffold_carrier(name)
    if name in PROTECTED_BRANCHES:
        return "a protected branch, which no sweep retires"
    return ""


def disposition_for(
    name: str,
    *,
    integration: str,
    current: str,
    contained_in: list[str],
    pr: PRStatus | None,
    unique_commits: int,
    protected: AbstractSet[str] = PROTECTED_BRANCHES,
    held: str = "",
    related: bool = True,
    reserved: bool = False,
    worktree: str | None = None,
    changes: WorktreeChanges | None = None,
) -> Disposition:
    """Resolve a branch to its single disposition.

    Every branch resolves to exactly one verb, so unlanded work has no silent
    bucket to sit in: a branch holding commits the integration branch lacks,
    with no open PR driving it, is ``LAND`` rather than ``KEEP``. Containment
    counts as landed only against the integration branch — sitting inside a
    sibling that has not landed either is no reason to drop work.

    ``held`` is why something else is already answerable for this branch, and
    outranks every disposition but the current and protected ones. A resolver
    lease is the case: it reads as abandoned work by every other signal here,
    and both verbs a sweep would offer for it destroy something.

    Two guards sit ahead of containment because containment answers them
    wrongly rather than not at all. A branch sharing no history has no
    divergence to measure, so every figure downstream describes a comparison
    that means nothing. A branch still standing where its workspace was
    reserved has spent nothing — an ancestor exactly as a merged branch is,
    and for the opposite reason — so a worktree held open on one is a
    workspace somebody reserved, not a leftover. Which of the two it is comes
    from the record written when the workspace was cut, never from where the
    tip stands now: work that lands by fast-forward leaves the tip on the
    integration branch's own tip, which is where an untouched workspace's
    tip is too, so a guard reading topology protects spent branches forever.

    What that workspace holds then decides which verb it gets. Reserving one
    says nobody has started, which a clean tree agrees with and an uncommitted
    change contradicts — and work left uncommitted sits in no commit, on no
    branch and on no remote, so leaving it for somebody's next session is how
    it goes stale with nothing to recover it from. Dirt elsewhere prices an
    action without changing it, a dirty ``DELETE`` being a delete that refuses
    until forced; on a reserved workspace it decides what the action is.

    A merged PR stays below the reserved guard, though it reads as the
    stronger evidence. :func:`get_pr_info` finds a PR by head branch name in
    every state, so a reused name matches one merged long before this
    workspace was cut: hoisted, the check offers to ``DELETE`` a workspace
    cut a moment ago, and a delete takes the worktree with the branch. A
    reserved branch has no commit of its own, so it cannot own a merged PR,
    and the guard shadows nothing real. A second net, where one is wanted,
    honours a PR only when it merged before the reservation was recorded.
    """
    if name == current:
        return Disposition(status="CURRENT", reason="current branch")
    if name in protected:
        return Disposition(status="KEEP", reason="protected branch")
    if held:
        return Disposition(status="KEEP", reason=held)
    if not related:
        return Disposition(
            status="UNRELATED", reason=f"shares no history with {integration}"
        )
    if reserved and worktree is not None:
        if changes is not None and changes.dirty():
            return Disposition(
                status="COMMIT",
                reason=(
                    f"reserved workspace cut from {integration}, "
                    f"holding {changes.summary()}"
                ),
            )
        return Disposition(
            status="KEEP", reason=f"reserved workspace cut from {integration}"
        )
    if integration in contained_in:
        return Disposition(status="DELETE", reason=f"merged into {integration}")
    if pr is not None and pr.state == "MERGED":
        return Disposition(status="DELETE", reason=f"PR #{pr.number} merged")
    if unique_commits == 0:
        return Disposition(
            status="STALE", reason=f"all commits cherry-picked into {integration}"
        )
    if pr is not None and pr.state == "OPEN":
        return Disposition(
            status="KEEP",
            reason=f"PR #{pr.number} open, {unique_commits} unique commits",
        )

    carried = [b for b in contained_in if b != integration]
    also = f"; also carried by {', '.join(carried)}" if carried else ""
    return Disposition(
        status="LAND", reason=f"{unique_commits} unique commits, no PR{also}"
    )


def classify_branch(
    branch: str,
    integration: str,
    current: str,
    *,
    has_remote: bool = True,
    protected: AbstractSet[str] = PROTECTED_BRANCHES,
) -> BranchClassification:
    """Classify a branch as DELETE/STALE/KEEP/CURRENT/NOT_FOUND with reason."""
    from lup.devtools.dev.worktree import branch_exists

    if not branch_exists(branch):
        return {
            "branch": branch,
            "status": "NOT_FOUND",
            "reason": "no such local branch",
        }

    if branch == current or branch in protected:
        guard = disposition_for(
            branch,
            integration=integration,
            current=current,
            contained_in=[],
            pr=None,
            unique_commits=0,
        )
        return {"branch": branch, "status": guard.status, "reason": guard.reason}

    merged_into_integration = Repository(Path.cwd()).is_ancestor(branch, integration)
    worktree = get_branch_worktree(branch)
    pr = get_pr_info(branch) if has_remote else None
    pr_number: str | int = pr.number if pr else ""
    pr_url = pr.url if pr else ""

    counted = count_unique_commits(branch, integration)
    verdict = disposition_for(
        branch,
        integration=integration,
        current=current,
        contained_in=[integration] if merged_into_integration else [],
        pr=pr,
        unique_commits=counted if counted >= 0 else 999,
    )

    return {
        "branch": branch,
        "status": verdict.status,
        "reason": verdict.reason,
        "worktree": worktree,
        "pr": pr_number,
        "pr_url": pr_url,
    }


class UnlandedBranch(BaseModel):
    """A branch holding commits the integration branch does not have.

    ``contained_by`` names the unlanded sibling that already carries every
    commit of this one, when there is such a sibling: the same work under
    two names is one decision, not two lines of backlog.
    """

    name: str
    unique_commits: int
    source_diff_lines: int
    worktree: str | None
    contained_by: str | None = None

    def standing(self) -> str:
        """What this branch holds, as one line reads it."""
        if self.contained_by is not None:
            return f"every commit already inside {self.contained_by}"
        return f"{self.unique_commits} commit(s), {self.source_diff_lines} ln unlanded"


def unlanded_siblings(
    protected: AbstractSet[str] = PROTECTED_BRANCHES,
) -> list[UnlandedBranch]:
    """Local-only scan for branches holding work the integration branch lacks.

    Deliberately offline — no fetch, no PR query — because this runs inside
    every ``dev check``. An open PR driving a branch is therefore invisible
    here, which is why the result is advisory: it reports what has not
    reached integration, and the full sweep decides what to do about it.

    The current branch is excluded: work in hand is not work parked out of
    sight, and reporting it every run would train the reader to skip the line.

    So is any branch a resolver run answers for. A run holds its branches
    out of the sweep deliberately, and its leftovers are one decision about
    the run rather than a list of parked work; a batch of them here is the
    same line repeated until the reader skips it, and reads as a backlog
    nobody is carrying when a run either is carrying it or has finished.
    Reading the run directory keeps this offline, which the rest of it is.

    A branch every commit of which another unlanded sibling already carries
    is reported inside that sibling rather than beside it. Two lines showing
    one line's figures read as twice the backlog, and a reader adding them
    up lands on a number nothing holds.
    """
    integration = get_integration_branch()
    worktrees = parse_worktrees()
    current = Repository(Path.cwd()).branch()
    leased = live_lease_branches(CheckoutState(root=project_root()).resolve())

    def measure(name: str) -> UnlandedBranch | None:
        if name == current or name in protected or name in leased:
            return None
        if Repository(Path.cwd()).is_ancestor(name, integration):
            return None
        unique = count_unique_commits(name, integration)
        if unique <= 0:
            return None
        return UnlandedBranch(
            name=name,
            unique_commits=unique,
            source_diff_lines=count_source_diff_lines(name, integration),
            worktree=worktrees.get(name),
        )

    measured = [
        found
        for name in git.lines("branch", "--format=%(refname:short)")
        if (found := measure(name)) is not None
    ]

    def container_of(branch: UnlandedBranch) -> str | None:
        """The sibling that carries all of this branch, if one does.

        The widest strict container wins, so a chain names its top rather
        than its next link. Two names on one commit contain each other, and
        the first by name stands for the group.
        """
        carriers = [
            other
            for other in measured
            if other.name != branch.name
            and Repository(Path.cwd()).is_ancestor(branch.name, other.name)
        ]
        strict = [
            other
            for other in carriers
            if not Repository(Path.cwd()).is_ancestor(other.name, branch.name)
        ]
        if strict:
            return max(strict, key=attrgetter("unique_commits")).name
        same_tip = sorted(other.name for other in carriers)
        if same_tip and same_tip[0] < branch.name:
            return same_tip[0]
        return None

    return [
        branch.model_copy(update={"contained_by": container_of(branch)})
        for branch in measured
    ]


class BaseCandidate(BaseModel):
    """A candidate base branch, measured against the branch under test.

    ``source`` states where the name came from, and the three are ordered by
    how much weight they carry. ``recorded`` is the base lup wrote at worktree
    creation: somebody's statement of intent. ``created`` is the cut git
    logged in the branch's own reflog: a byproduct, and evidence rather than
    authority — :func:`created_from` says what it cannot answer for.
    ``guessed`` is topology, which cannot recover a creation point at all and
    is the one a caller has to confirm before acting on it.
    """

    name: str
    distance: int
    merge_base: str
    is_ancestor: bool
    source: Literal["recorded", "created", "guessed"] = "guessed"


def recorded_base(branch: str) -> str | None:
    """The base recorded at worktree creation, when one was written.

    ``None`` rather than an empty name, because detection branches on whether
    a record exists at all and an empty string is a name nothing carries.
    """
    return records.recorded_base(branch) or None


# lup: ignore[constant-declaration] — git's own reflog wording, which `git
# branch`, `git switch -c` and `git worktree add -b` all write, and not a
# spelling anything here chooses
CREATION_ENTRY = "branch: Created from "


def created_from(branch: str) -> str:
    """The branch git logged this one as cut from, empty where it logged none.

    `branch: Created from <ref>` is the first reflog entry git writes for a
    new branch, and `git branch`, `git switch -c` and `git worktree add -b`
    all write it — measured on git 2.55, all three. So a branch made with
    plain git answers here, where lup's own record answers only for a branch
    made by lup's own command. That is the whole point of asking: a base
    should not depend on which command happened to cut the branch.

    **Evidence, not authority, and empty is unremarkable.** Four ways it says
    nothing, none of them a fault worth reporting:

    - A bare clone lup did not prepare does not log ref updates:
      `core.logAllRefUpdates` defaults to false without a working tree, so a
      branch cut by running git against the git directory itself carries no
      entry, while the same command run from one of its worktrees does. lup
      turns the setting on wherever it prepares a bare clone and it is unset
      (:func:`lup.devtools.dev.records.log_ref_updates`), so this is a clone
      made some other way, one whose owner set it off, or one only ever
      prepared from a contained session, which holds the shared config
      read-only.
    - Reflogs expire, at 90 days for a reachable entry, and the creation is
      the oldest entry a branch has.
    - They are per-clone and never fetched, so nobody else's clone can answer
      about a branch this one cut.
    - The entry names whatever ref the command was given, which may be `HEAD`
      or a bare commit rather than a branch.

    So this ranks below the written record, which is a deliberate statement,
    and a caller reads its absence as "ask topology" rather than as anything
    being wrong.

    A branch name is the only answer taken. `HEAD` names wherever the creating
    checkout stood and leaves a later reader nothing to measure, a
    remote-tracking ref answers as the local branch it tracks, and a bare
    commit is a base no `pr sync-base` could fetch.
    """
    from lup.devtools.dev.worktree import branch_exists

    logged = git.lines("reflog", "show", "--format=%gs", f"refs/heads/{branch}")
    cut = next(
        (
            entry.removeprefix(CREATION_ENTRY)
            for entry in reversed(logged)
            if entry.startswith(CREATION_ENTRY)
        ),
        "",
    )
    if branch_exists(cut):
        return cut
    # A remote's own prefix, stripped from the list of remotes rather than at
    # the first slash: `feat/x` is one branch name and `origin/dev` is two
    # things, and only the second is somebody else's copy of a local branch.
    tracked = (
        stripped
        for remote in git.lines("remote")
        if (stripped := cut.removeprefix(f"{remote}/")) != cut
    )
    return next((name for name in tracked if branch_exists(name)), "")


def decayed_base_complaint(branch: str, recorded: str, *, present: bool) -> str:
    """Why a recorded base stopped answering, said at the moment it stops.

    The record holds a branch name, and a name is only an answer while
    something still carries it. Deleting the base leaves the record intact
    and pointing at nothing, so detection falls through to the topological
    guess it exists to avoid — and reports ``guessed``, which is what a
    branch that never had a record reports. The two become the same story,
    told much later by whatever refuses to run on a guess.

    So the decay is named where it happens rather than inferred from a
    refusal several commands away. What the reader needs is that a record
    was found, whose referent is gone, and that naming a base directly is
    the way past it — none of which survives being reduced to ``guessed``.
    """
    cause = (
        "which no longer exists"
        if not present
        else "which shares no history with it, so nothing could be measured"
    )
    return (
        f"{branch} records {recorded} as its base, {cause}. "
        "Falling back to the cut git logged, and to topology where it logged "
        "none — so whatever reads this cannot tell a record that decayed from "
        "a branch that never carried one, and a command that refuses a "
        "guessed base will refuse this one.\n"
        f"Name the base with --base <branch> to settle it, or write "
        f'{{"base": "<branch>"}} into {records.record_location(branch)}, '
        "beneath the git directory every worktree of this repository shares."
    )


def detect_base_branch(branch: str | None = None) -> BaseCandidate:
    """Detect the base branch for the given (or current) branch.

    A base recorded at worktree creation (:mod:`lup.devtools.dev.records`) wins
    outright — topology cannot recover the creation point once the parent
    has merged on. The cut git logged comes next, which is what lets a branch
    made with plain ``git worktree add -b`` answer at all, and which
    :func:`created_from` leaves empty often enough that its absence is
    ordinary. Without either, every local branch is measured by its merge-base
    distance and the nearest wins, with ancestry breaking a tie between equals
    and disqualifying nobody: an integration branch that has taken a commit
    since the cut is not an ancestor, and that is the ordinary state of one
    rather than a reason to rule it out.

    A record whose branch has since been deleted is the one case where
    winning outright and having nothing to say are the same code path, so it
    says so on the way past. The guess it falls back to is reported as
    ``guessed`` either way, which is the whole problem: a base that decayed
    and a base nobody recorded are indistinguishable downstream, and the
    reader meets the difference as a refusal several commands later.
    """
    effective = branch or Repository(Path.cwd()).branch()

    local_branches = [
        b for b in git.lines("branch", "--format=%(refname:short)") if b != effective
    ]

    if not local_branches:
        typer.echo("No other local branches to compare against.", err=True)
        raise typer.Exit(1)

    def measure(candidate: str) -> BaseCandidate | None:
        try:
            merge_base = git.out("merge-base", effective, candidate, _ok_code=[0])
            distance = Repository(Path.cwd()).count(f"{merge_base}..{effective}")
        except (sh.ErrorReturnCode, GitError):
            return None
        return BaseCandidate(
            name=candidate,
            distance=distance,
            merge_base=merge_base,
            is_ancestor=Repository(Path.cwd()).is_ancestor(candidate, effective),
        )

    recorded = recorded_base(effective)
    if recorded is not None:
        present = recorded in local_branches
        pinned = measure(recorded) if present else None
        if pinned is not None:
            return pinned.model_copy(update={"source": "recorded"})
        typer.echo(
            decayed_base_complaint(effective, recorded, present=present), err=True
        )

    # What git logged when the branch was cut: a fact about this branch rather
    # than a reading of the shape around it, so it is taken ahead of topology
    # and behind the record, which is somebody saying what they meant. Its
    # absence is the ordinary case for half the ways a branch is made, and is
    # passed over without a word.
    cut = created_from(effective)
    logged = measure(cut) if cut in local_branches else None
    if logged is not None:
        return logged.model_copy(update={"source": "created"})

    candidates = [m for c in local_branches if (m := measure(c)) is not None]

    if not candidates:
        typer.echo("Could not determine base branch.", err=True)
        raise typer.Exit(1)

    # Every measured candidate is ranked, and ancestry only breaks a tie among
    # equals. Ancestry disqualifies nobody, because a branch fails it for the
    # most ordinary reason there is — the integration branch moved on. A
    # feature branch whose `dev` has taken one commit since the cut has no
    # ancestor in `dev` at all, so filtering on ancestry drops `dev` and hands
    # the answer to whichever stale sibling happens to sit in the branch's
    # history, however far away: a branch whose real base is no distance off
    # can be handed a sibling hundreds of commits away, and the gate then
    # reports capabilities gone that nothing touched.
    #
    # Distance is the merge-base distance, which needs no ancestry to be
    # meaningful and is what makes a moved-on integration branch comparable
    # again. Ancestry survives as a tiebreaker because a candidate sitting
    # inside this branch's history is the likelier parent of two equals, which
    # is all the evidence it ever was.
    ranked = sorted(candidates, key=lambda c: (c.distance, not c.is_ancestor))
    best = ranked[0]

    # Equal on both keys, not on distance alone: a candidate the tiebreaker
    # already settled is an answer, and reporting it as ambiguous would put
    # the ranking's own decision back to the caller.
    tied = [
        c
        for c in ranked[1:]
        if (c.distance, c.is_ancestor) == (best.distance, best.is_ancestor)
    ]
    if not tied:
        return best

    # A tie is two answers, not none, and exiting over it stopped every caller
    # rather than the one that could not proceed on a guess — which is how a
    # clone with two siblings at one distance had its quality gate refuse to
    # run at all, on a branch with nothing wrong with it.
    #
    # The integration branch settles it where it is among the candidates,
    # since that is where work lands and what a branch nobody recorded was
    # most likely cut from; otherwise the ranking's own first answer stands,
    # which is stable because the candidate order is git's, by name. Either
    # way the tie is named and the answer is reported as `guessed`, so a
    # caller that must not act on one — `pr sync-base` declines to merge —
    # still declines.
    integration = get_integration_branch()
    settled = next((c for c in [best, *tied] if c.name == integration), best)
    typer.echo(
        "Ambiguous base branch, equally close to "
        + ", ".join(f"{c.name} ({c.distance} commits ahead)" for c in [best, *tied])
        + f".\nTaking {settled.name}. Name the base with --base <branch> where "
        "that is wrong, or record it once so nothing has to be guessed again.",
        err=True,
    )
    return settled


class RemoteMeasure(BaseModel, ABC, frozen=True):
    """One remote branch, and how many of its commits this checkout is missing.

    The commits arrive by a different route depending on which remote was
    measured, so the route is a property of the reading rather than a setting
    a reader passes alongside it. Each member answers with its own, which is
    what keeps a remedy from being printed beside a count it cannot close.
    """

    tracked: str
    """The remote branch measured against."""

    behind: int = 0
    """Commits that branch holds which this checkout does not."""

    @abstractmethod
    def update_command(self) -> str:
        """What a reader runs to take the commits this reading found missing."""

    @abstractmethod
    def subject(self) -> str:
        """What a report calls the thing that is behind."""

    def stale(self) -> bool:
        """Whether the remote is known to hold commits this checkout does not."""
        return self.behind > 0

    def report(self) -> str:
        """One line naming the count and the way to close it."""
        if not self.behind:
            return f"{self.subject()} is current with {self.tracked}"
        return (
            f"{self.subject()} is {self.behind} commit(s) behind {self.tracked}: "
            f"update with `{self.update_command()}`"
        )

    def notice(self) -> str:
        """The same reading where somebody has to hear it, empty when current.

        A command asked how this checkout stands answers either way, which is
        what :meth:`report` is for. A launch passing through has not been
        asked, and `branch is current` is a line that appears before every
        session and can never mean anything but that the next line is the
        first one worth reading.
        """
        return "" if not self.behind else self.report()


class UpstreamMeasure(RemoteMeasure, frozen=True):
    """The branch's own remote, whose commits arrive by fast-forward."""

    def update_command(self) -> str:
        return "git pull --ff-only"

    def subject(self) -> str:
        return "branch"


class BaseMeasure(RemoteMeasure, frozen=True):
    """The remote of the base a worktree was cut from, taken by merge.

    A feature branch holds commits its base does not, so there is nothing
    here to fast-forward. Naming a pull would name the one command that
    exits non-zero on the only checkout this reading is ever printed for.
    """

    def update_command(self) -> str:
        return f"git merge {self.tracked}"

    def subject(self) -> str:
        return "base"


class BaseFreshness(BaseModel, frozen=True):
    """What the remote says about a checkout: its own branch, and its base.

    Two readings rather than one, because which of them a checkout happens to
    have says nothing about which one a reader wants. Asking only the first
    ref that resolves answers "am I behind my own push" wherever a branch has
    been pushed and "has my base moved" wherever it has not — so the same
    gate would call a base three commits gone current, and offer a base
    forty-one commits gone a pull that cannot run there.
    """

    upstream: UpstreamMeasure | None = None
    """The branch's own remote, when it tracks one."""

    base: BaseMeasure | None = None
    """The remote of the base recorded at worktree creation, when one was."""

    unreachable: str = ""
    """Why the remote could not be asked; empty when it answered."""

    def measures(self) -> list[RemoteMeasure]:
        """Every reading taken, in the order a report names them."""
        return [reading for reading in (self.upstream, self.base) if reading]

    def stale(self) -> bool:
        """Whether either remote is known to hold commits this checkout does not."""
        return any(reading.stale() for reading in self.measures())

    def unanswered(self) -> bool:
        """Whether a remote was asked and did not answer.

        The reading a caller has to tell apart from a clean one, because
        every other question here answers the same for both: no measure was
        taken, so nothing is behind, so nothing is stale. What separates
        them is not the count but whether there is one.

        Having nothing to ask is not this. A checkout that answers to no
        remote branch has no base that could have moved out from under it,
        where one whose fetch was refused has exactly the base it had before
        asking and no idea whether it still stands.
        """
        return bool(self.unreachable)

    def report(self) -> str:
        """Every reading on its own line, or the one reason there are none.

        An unknown answer says so rather than reading as a clean bill: a
        checkout that could not reach its remote knows exactly as much about
        its base as it did before asking.
        """
        if self.unreachable:
            return f"base freshness unknown: {self.unreachable}"
        return "\n".join(reading.report() for reading in self.measures()) or (
            "base freshness unknown: this checkout answers to no remote branch"
        )

    def notice(self) -> str:
        """Only the readings somebody has to hear, which is often none at all.

        An unreadable remote is one of them and stays: not knowing is the
        finding there, and it is the one this whole reading exists to put in
        front of somebody before a session opens on it.
        """
        if self.unreachable:
            return self.report()
        return "\n".join(
            said for reading in self.measures() if (said := reading.notice())
        )


def probing(launcher: ProcessLauncher, root: Path) -> Repository:
    """The checkout as the freshness probe asks it: through the launcher, never prompting.

    Through the launcher seam rather than this module's own bound git,
    because one of its steps reaches the network: the non-interactive
    environment every agent spawn point uses is laid over the console's, so
    a credential nobody can supply fails fast instead of waiting on a
    terminal prompt. Most of what it asks has a blank answer — no upstream,
    no recorded base, no branch — so a failure reads as that blank.
    """
    return Repository(root, launcher, non_interactive_environment({}))


def upstream_of(launcher: ProcessLauncher, root: Path, branch: str) -> str:
    """The remote branch a local branch tracks, empty when it tracks none.

    An empty ``branch`` asks about whichever is checked out, which is how a
    detached HEAD answers nothing at all rather than answering for the commit
    it happens to sit on.
    """
    status = probing(launcher, root).run(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{upstream}}"
    )
    return status.stdout.strip() if status.code == 0 else ""


class TrackedRemotes(BaseModel, frozen=True):
    """Which remote branches a checkout answers to; either may be absent.

    A feature worktree tracks nothing until it is pushed, so the base
    recorded when it was created is asked what *it* tracks — which is how a
    worktree cut from an integration branch is measured against that branch
    on the remote. Both are asked rather than whichever resolves first,
    because they answer different questions and a checkout having one is no
    reason to stop asking the other.
    """

    upstream: str = ""
    """The branch's own remote, when it tracks one."""

    base: str = ""
    """The remote of the base recorded at worktree creation, when one was."""

    def named(self) -> bool:
        """Whether there is any remote here to measure against."""
        return bool(self.upstream or self.base)

    def counted(self, launcher: ProcessLauncher, root: Path) -> BaseFreshness:
        """How far this checkout sits behind each, off refs already fetched.

        Separate from fetching so that a caller which has just moved HEAD can
        re-read the counts without paying for the network a second time.
        """

        repository = probing(launcher, root)

        def behind(tracked: str) -> int | None:
            try:
                return repository.count(f"HEAD..{tracked}")
            except GitError:
                return None

        own = behind(self.upstream) if self.upstream else 0
        cut = behind(self.base) if self.base else 0
        if own is None or cut is None:
            return BaseFreshness(
                unreachable=f"git did not count {self.upstream if own is None else self.base}"
            )
        return BaseFreshness(
            upstream=UpstreamMeasure(tracked=self.upstream, behind=own)
            if self.upstream
            else None,
            base=BaseMeasure(tracked=self.base, behind=cut) if self.base else None,
        )


def remote_of(launcher: ProcessLauncher, root: Path, branch: str) -> str:
    """Which remote branch this one answers to, however it came to be known.

    Git's own tracking configuration is asked first and settles it wherever a
    branch has one, since a person who set it meant it. A branch published by
    `git pr push` has none: the push names its destination as a refspec and
    records it here instead, so that git's shared configuration holds nothing
    lup put there. Without either, the branch has never been published.
    """
    return upstream_of(launcher, root, branch) or records.recorded_upstream(
        branch, root
    )


def tracked_remotes(launcher: ProcessLauncher, root: Path) -> TrackedRemotes:
    """Ask git and lup's own records which remotes this checkout answers to."""
    try:
        branch = probing(launcher, root).branch()
    except GitError:
        branch = ""
    recorded = records.recorded_base(branch, root) if branch else ""
    return TrackedRemotes(
        upstream=remote_of(launcher, root, branch) if branch else "",
        base=remote_of(launcher, root, recorded) if recorded else "",
    )


def probe_base_freshness(launcher: ProcessLauncher, root: Path) -> BaseFreshness:
    """Fetch, then count what each remote holds and this checkout does not.

    A tree whose base has moved is self-consistent and says nothing about it,
    so only the remote can answer the question — one fetch, then a count per
    ref found. A remote that cannot be reached leaves the answer unknown
    rather than guessing it either way.

    A refusal is asked about again before it is reported, because the fetch
    runs where nothing may prompt and git's own account of that is the least
    useful true thing there is to say: it names the key it was refused for,
    never the one to load. The credential probe knows which identity ssh
    would offer this destination, so where it identifies one that answer
    replaces git's — the same event, re-asked by something that can name the
    way out of it. Where it identifies nothing, git keeps the floor: the
    probe is a second command and can disagree with the first about whether
    the host was reachable at all.
    """
    remotes = tracked_remotes(launcher, root)
    if not remotes.named():
        return BaseFreshness()
    repository = probing(launcher, root)
    fetched = repository.run("fetch", "--quiet", stream=True)
    if fetched.code != 0:
        # `remote get-url` expands `url.<base>.insteadOf` and contacts
        # nothing, so what is probed is the URL the fetch above actually
        # reached rather than the one written in the config beside it.
        origin = repository.remote_url("origin") or ""
        return BaseFreshness(
            unreachable=remote_auth_refusal(origin).diagnoses()
            or fetched.stderr.strip()
            or f"`git fetch` exited {fetched.code}",
        )
    return remotes.counted(launcher, root)


def sync_upstream(
    launcher: ProcessLauncher, root: Path, measure: UpstreamMeasure, *, publish: bool
) -> Iterator[str]:
    """Take what the branch's own remote holds, and say what it still lacks.

    Taking is what a reader would have done by hand and cannot go wrong
    quietly: ``--ff-only`` cannot invent a merge. What makes it safe to do
    unattended is the clean tree, so a checkout with work in it is left
    exactly as it was — the mistake this prevents is smaller than the one it
    would risk.

    Handing back is ``publish``, and happens only where a caller asked for
    it. The two directions read as symmetrical and are not: a pull changes
    this checkout, where a push publishes commits under somebody's name and
    runs whatever the local and remote hooks run — minutes of it, on the
    hook side, with nothing to say it started. A caller that means to push
    says so; one that only wants the checkout current is told the count
    instead and can push it itself. A diverged branch stops after the failed
    pull either way, rather than pushing on top of the divergence it just
    failed to close.

    The pull and the push are shown as they run, because they have a network
    or a hook at the other end: working through a slow transfer and having
    stopped look identical from a terminal told nothing until the exit. What
    is said of a failure is that exit rather than the stderr already on
    screen, so it is not printed twice.
    """
    repository = probing(launcher, root)
    if repository.run("status", "--porcelain").stdout.strip():
        yield f"not synced with {measure.tracked}: the working tree has changes"
        return
    if measure.behind:
        pulled = repository.run("pull", "--ff-only", stream=True)
        if pulled.code:
            yield (
                f"not synced with {measure.tracked}: "
                f"`git pull --ff-only` exited {pulled.code}"
            )
            return
        yield f"pulled {measure.behind} commit(s) from {measure.tracked}"
    try:
        ahead = repository.count(f"{measure.tracked}..HEAD")
    except GitError:
        return
    if not ahead:
        return
    if not publish:
        yield f"{ahead} commit(s) {measure.tracked} does not have; `git push` sends them"
        return
    pushed = repository.run("push", stream=True)
    yield (
        f"not pushed to {measure.tracked}: `git push` exited {pushed.code}"
        if pushed.code
        else f"pushed {ahead} commit(s) to {measure.tracked}"
    )


def settle_base_freshness(
    launcher: ProcessLauncher, root: Path, *, publish: bool = False
) -> None:
    """Make the checkout current where that is free, and report what is left.

    Being behind is not grounds for refusing a session. A clean checkout is
    brought level with its own remote, which costs nothing and heads off the
    divergence that comes of committing onto a branch the remote has moved
    past; a base that has moved needs a merge, so it is named along with the
    merge that would take it and the session opens either way.

    Free is what decides it, which is why ``publish`` is off unless asked
    for. Taking commits costs a fetch nobody has to think about; handing
    them back costs whatever the hooks on either side cost and puts this
    checkout's work somewhere it can be read, neither of which is a price to
    charge somebody who typed a command about opening a session.

    Being unable to read the base at all is the one part that does put a
    question to whoever is there. A session opened on an unread base is the
    mistake this whole reading exists to prevent, and a line of output does
    not prevent it: the one that reported this arrived under four lines
    saying ready, in the shape of the status lines around it, a moment
    before the terminal was handed to something that draws over all of them.
    """
    # Said before the fetch rather than after it, because this is the first
    # thing here that waits on somebody else's machine, and a line naming the
    # wait is what separates a slow one from a stopped one.
    typer.echo("reading what the remote holds")
    freshness = probe_base_freshness(launcher, root)
    synced = (
        list(sync_upstream(launcher, root, freshness.upstream, publish=publish))
        if freshness.upstream
        else []
    )
    for line in synced:
        typer.echo(line)
    if synced:
        # A pull moves HEAD, which is what the base is measured from, so the
        # counts are re-read — off the refs the probe has already fetched.
        freshness = tracked_remotes(launcher, root).counted(launcher, root)
    said = freshness.notice()
    if said:
        typer.echo(said)
    if freshness.unanswered():
        admit_an_unread_base()


def admit_an_unread_base() -> None:
    """Put an unread base to whoever is at the terminal; open anyway when nobody is.

    Asked rather than refused, because a base that cannot be read is not the
    same as one that has moved, and offline is a way of working rather than
    a fault: the answer belongs to the person who knows which of the two
    they are in. Asked rather than printed, because a line that is only
    printed is one nothing has to answer.

    Only where somebody can answer. A refusal here falls on exactly the
    scripted sessions nobody is watching, and a prompt on an absent terminal
    is that refusal wearing a question mark — `typer.confirm` reads
    end-of-file as an abort. So a session with nobody in front of it says
    what it is doing and opens.
    """
    if not sys.stdin.isatty():
        typer.echo("nobody is here to answer, so the session opens on it unread")
        return
    if not typer.confirm("Could not check the remote. Continue opening the session?"):
        raise typer.Abort()


def require_fresh_base(freshness: BaseFreshness) -> None:
    """Refuse to start work that pins this base for everything it hands out.

    A run captures its base once and cuts every lease from it, so following a
    base that has already moved means re-basing each lease, re-deriving each
    diff against the new base, and re-running intake — which can add or drop
    concerns while work is in flight. Refusing before any of that exists
    costs a fetch; discovering it afterwards costs the run.

    A base nothing could read is refused on the same terms rather than
    passed. The session launchers ask about one, because a person is sitting
    in front of them and offline is a way of working; here there is nobody to
    ask and nothing to gain by guessing — a run that pins an unread base has
    committed every lease it will cut to a guess, and it finds out whether
    the guess held at the point where the answer costs the run.
    """
    typer.echo(freshness.report())
    if freshness.stale():
        raise typer.BadParameter(freshness.report())
    if freshness.unanswered():
        raise typer.BadParameter(
            "a run pins one base for every lease it cuts, so it does not start "
            "on a base nothing could read"
        )


# -- CLI functions --


def branch_status(branch: str | None, as_json: bool) -> None:
    """Analyze branch containment, PR status, and worktree info."""
    has_remote = check_remote_auth()

    integration = get_integration_branch()
    current = Repository(Path.cwd()).branch()

    branch_list = (
        [branch] if branch else git.lines("branch", "--format=%(refname:short)")
    )

    results = [
        classify_branch(b, integration, current, has_remote=has_remote)
        for b in branch_list
    ]

    if as_json:
        output_json(results)
        return

    typer.echo(f"\nIntegration branch: {integration}")
    typer.echo(f"Current branch: {current}\n")
    status_markers: dict[BranchStatus, str] = {
        "LAND": "^",
        "DELETE": "x",
        "STALE": "~",
        "KEEP": " ",
        "CURRENT": "*",
        "NOT_FOUND": "?",
    }

    def row(r: BranchClassification) -> list[str]:
        marker = status_markers[r["status"]]
        wt = " [worktree]" if r.get("worktree") else ""
        return [f"[{marker}] {r['branch']}", r["status"], f"{r['reason']}{wt}"]

    typer.echo(format_table(("Branch", "Status", "Reason"), [row(r) for r in results]))

    typer.echo()
    deletable = [r for r in results if r["status"] in ("DELETE", "STALE")]
    if deletable:
        typer.echo(f"{len(deletable)} branch(es) can be cleaned up")

    landable = [r for r in results if r["status"] == "LAND"]
    if landable:
        typer.echo(f"{len(landable)} branch(es) hold unlanded work")


def base_branch(branch: str | None, as_json: bool) -> None:
    """Detect the base branch for the current (or specified) branch."""
    base = detect_base_branch(branch)
    effective = branch or Repository(Path.cwd()).branch()

    if as_json:
        output_json(
            {
                "branch": effective,
                "base": base.name,
                "merge_base": base.merge_base,
                "commits_ahead": base.distance,
            }
        )
    else:
        typer.echo(f"Branch: {effective}")
        typer.echo(f"Base: {base.name}")
        typer.echo(f"Merge base: {short_sha(base.merge_base)}")
        typer.echo(f"Commits ahead: {base.distance}")


COMMIT_PREFIX_LABELS = {
    "feat": "Features",
    "fix": "Fixes",
    "refactor": "Refactoring",
    "docs": "Documentation",
    "test": "Tests",
    "chore": "Chores",
    "meta": "Meta",
    "data": "Data",
}
"""How a PR body heads the group of commits sharing one type.

A heading over the subjects rather than a sentence opener joined to one. A
commit subject is written in whatever voice the project writes them in, and
a past-tense opener in front of a declarative one — "Fixed say that a
sign-in ends on an error" — is ungrammatical for every commit written that
way, which in a project writing them that way is all of them. A noun phrase
stands over any of them.

The types are one convention among several, and the English is a second
choice on top: a project spelling either differently passes its own table
rather than reading someone else's vocabulary back in its summaries.
"""


def names_a_test(
    path: str,
    directories: tuple[str, ...] = ("test", "tests", "spec", "specs"),
    affixes: tuple[str, ...] = ("test_", "_test", ".test", "spec_", "_spec", ".spec"),
) -> bool:
    """Whether a changed path names a test rather than what a test covers.

    Read off the path, because what a project calls its tests is a naming
    convention and a diff records no more than the names. Both halves are
    defaults, so a project spelling either differently passes its own.
    """
    posix = PurePosixPath(path)
    stem = posix.stem.lower()
    return any(part.lower() in directories for part in posix.parent.parts) or any(
        stem.startswith(affix) or stem.endswith(affix) for affix in affixes
    )


def tests_touched(base: str) -> list[str]:
    """The test files this branch's diff touches, sorted, possibly none.

    What a branch's own tests are is derivable from its diff, so the test
    plan is derived. The alternative a fixed checklist offers is a sentence
    nobody wrote about a change nobody read, which a reviewer meets as a
    claim that a plan exists — and where a branch touches no test at all,
    saying nothing is the honest form of that.
    """
    changed = git.lines("diff", "--name-only", f"{base}...HEAD", _ok_code=[0])
    return sorted(path for path in changed if path and names_a_test(path))


def pr_body(
    base_override: str | None,
    # Open keys: a prefix is whatever a commit subject happens to start with,
    # read off the log rather than chosen from a set this code knows.
    labels: StringMap = COMMIT_PREFIX_LABELS,
) -> None:
    """Generate a PR body from the current branch's commits against its base."""
    base = base_override or detect_base_branch().name

    log_lines = git.lines(
        "log",
        "--oneline",
        "--no-decorate",
        "--no-merges",
        f"{base}..HEAD",
        _ok_code=[0],
    )
    if not log_lines:
        typer.echo("No commits found since base branch", err=True)
        raise typer.Exit(1)

    groups: dict[str, list[str]] = defaultdict(list)
    for line in log_lines:
        message = line.partition(" ")[2]  # lup: ignore[string-split] — log line
        if not message:
            continue
        head = message.partition("(")[0]  # lup: ignore[string-split] — commit type
        prefix = head.partition(":")[0].lower()  # lup: ignore[string-split] — type
        groups[prefix].append(message)

    def summarize(prefix: str, messages: list[str]) -> list[str]:
        """One heading and one bullet per commit, with none of them folded."""
        heading = labels.get(prefix, prefix.capitalize())
        subjects = [
            # lup: ignore[string-split] — commit subject after its type
            (message.partition(":")[2] or message).strip()
            for message in messages
        ]
        return [f"**{heading}**", *(f"- {subject}" for subject in subjects), ""]

    summary_lines = [
        line for prefix, msgs in groups.items() for line in summarize(prefix, msgs)
    ]
    body_parts = ["## Summary", "", *summary_lines, "## Commits", *log_lines]
    if tests := tests_touched(base):
        body_parts.extend(["", "## Test plan", *(f"- [ ] `{path}`" for path in tests)])

    typer.echo("\n".join(body_parts))


def survey(as_json: bool, scaffold: str = "") -> None:
    """Collect branch, worktree, PR, and containment data.

    ``scaffold`` names the branch the project's copied half is compiled onto,
    empty where it adopted none; its row is kept whatever containment says.
    """
    complaint = origin_auth_complaint()
    if complaint:
        typer.echo(complaint, err=True)
    has_remote = not complaint
    if has_remote:
        if not as_json:
            typer.echo("Fetching and pruning remote...", err=True)
        try:
            fetch_remote_tracking()
        except sh.ErrorReturnCode as e:
            # The refs already here are still read, so the survey says what
            # the last successful fetch left rather than nothing at all --
            # which is the reading `remotes_fetched` marks as unrefreshed.
            complaint = decode_stderr(e)
            logger.warning("Failed to fetch: %s", complaint)

    integration = get_integration_branch()
    cur = Repository(Path.cwd()).branch()

    raw_branches = parse_branches()
    worktrees = parse_worktrees()
    branch_names = [b["name"] for b in raw_branches]
    containment = build_containment(branch_names)
    # A branch the remote still carries after its local copy is gone is the
    # one shape a local sweep cannot express: nothing in refs/heads names
    # it, so no row is emitted and no verb is owed, and the next sweep is
    # blind in exactly the same way. Matched by name, which is what a local
    # branch and its counterpart on the remote share.
    local_names = {b["name"] for b in raw_branches}
    remote_rows = parse_remote_branches() if has_remote else []
    remote_only = [row for row in remote_rows if row["name"] not in local_names]
    published = {row["name"] for row in remote_rows}

    if has_remote and not as_json:
        typer.echo("Querying PR status...", err=True)
    pr_named = branch_names + [row["name"] for row in remote_only]
    pr_map: dict[str, PRStatus] = fetch_pr_status(pr_named) if has_remote else {}
    leased = leased_on_disk(
        live_lease_branches(CheckoutState(root=project_root()).resolve()), branch_names
    )

    def answerable(name: str) -> str:
        """Why something outside this sweep already answers for the branch."""
        if name == scaffold:
            return scaffold_carrier(name)
        return leased[name].reason() if name in leased else ""

    def info(b: ParsedBranch) -> BranchInfo:
        name = b["name"]
        checkout = worktrees.get(name)
        contained_in = containment[name]
        pr_merged = name in pr_map and pr_map[name].state == "MERGED"

        related = name == integration or shares_history(name, integration)
        if integration in contained_in or pr_merged or not related:
            # An unrelated branch reports both figures from comparisons that
            # needed no merge base, so they measure distance between trees
            # rather than divergence. Left at zero rather than shown wrong.
            unique = 0
            diff_lines = 0
        else:
            unique = count_unique_commits(name, integration)
            diff_lines = count_source_diff_lines(name, integration)

        uncommitted = worktree_changes(checkout) if checkout else None
        verdict = disposition_for(
            name,
            integration=integration,
            current=cur,
            contained_in=contained_in,
            pr=pr_map.get(name),
            unique_commits=unique,
            held=answerable(name),
            related=related,
            reserved=still_at_reservation(name),
            worktree=checkout,
            changes=uncommitted,
        )

        return BranchInfo(
            name=name,
            commit=b["commit"],
            tracking=b["tracking"],
            worktree=checkout,
            is_current=b["is_current"],
            contained_in=contained_in,
            pr=pr_map.get(name),
            unique_commits=unique,
            source_diff_lines=diff_lines,
            disposition=verdict.status,
            reason=verdict.reason,
            rewritten=len(rewrite_suspects(name, integration)) if unique else 0,
            behind=count_commits_behind(name, integration),
            on_remote=name in published if has_remote else None,
            changes=uncommitted,
        )

    def remote_info(row: ParsedRemoteBranch) -> RemoteBranchInfo:
        """Resolve one remote-only branch through the same classifier.

        Reusing ``disposition_for`` is the point rather than a convenience:
        a second verb table for remote branches would answer the same
        question a little differently the first time either changed. What a
        ref on a remote cannot have — a worktree, a lease, being the current
        branch — is left at the defaults that say so.
        """
        name = row["name"]
        ref = f"{row['remote']}/{name}"
        related = shares_history(ref, integration)
        contained = related and Repository(Path.cwd()).is_ancestor(ref, integration)
        unique = (
            0 if contained or not related else count_unique_commits(ref, integration)
        )
        verdict = disposition_for(
            name,
            integration=integration,
            current=cur,
            contained_in=[integration] if contained else [],
            pr=pr_map.get(name),
            unique_commits=unique,
            held=answerable(name),
            related=related,
        )
        return RemoteBranchInfo(
            name=name,
            remote=row["remote"],
            commit=row["commit"],
            contained_in_integration=contained,
            pr=pr_map.get(name),
            unique_commits=unique,
            disposition=verdict.status,
            reason=verdict.reason,
        )

    branches_list = [info(b) for b in raw_branches]
    remote_list = [remote_info(row) for row in remote_only]

    result = SurveyResult(
        integration_branch=integration,
        current_branch=cur,
        branches=branches_list,
        integration_remote=integration_standing(integration) if has_remote else None,
        runs=runs_holding(leased),
        remote_branches=remote_list,
        remotes_fetched=not complaint,
        fetch_complaint=complaint,
    )

    if as_json:
        output_json(result)
    else:
        typer.echo(f"\nIntegration: {integration} | Current: {cur}\n")

        def display_row(bi: BranchInfo) -> list[str]:
            pr_str = f"#{bi.pr.number} {bi.pr.state}" if bi.pr else "-"
            marker = "* " if bi.is_current else "  "
            return [
                f"{marker}{bi.name}",
                bi.disposition,
                str(bi.unique_commits),
                str(bi.behind) if bi.behind else "-",
                f"{bi.rewritten}?" if bi.rewritten else "-",
                str(bi.source_diff_lines),
                bi.changes.compact() if bi.changes else "-",
                pr_str,
                bi.reason,
            ]

        headers = (
            "Branch",
            "Disposition",
            "Unique",
            "Behind",
            "Rewr",
            "Diff",
            "Dirt",
            "PR",
            "Reason",
        )
        typer.echo(format_table(headers, [display_row(bi) for bi in branches_list]))

        if result.integration_remote is not None:
            for line in result.integration_remote.lines():
                typer.echo(f"\n{line}")
        # Only work the integration branch lacks: a landed branch's commits
        # are wherever the integration branch is, which the lines above say.
        unpublished = [
            bi.name
            for bi in branches_list
            if bi.on_remote is False and bi.unique_commits
        ]
        if unpublished:
            typer.echo(
                f"\n{len(unpublished)} branch(es) holding unlanded work on no "
                f"remote, so nothing but this clone holds it: {', '.join(unpublished)}"
            )

        if not result.remotes_fetched:
            typer.echo(
                "\nThe remotes were not read for this survey, so what it says"
                " about them is whatever the last read left, and an empty list"
                f" means nothing here: {result.fetch_complaint}"
            )

        if result.remote_branches:
            typer.echo("\nOn the remote, with no local branch:\n")

            def remote_row(rb: RemoteBranchInfo) -> list[str]:
                pr_str = f"#{rb.pr.number} {rb.pr.state}" if rb.pr else "-"
                return [
                    rb.qualified(),
                    rb.disposition,
                    str(rb.unique_commits),
                    pr_str,
                    rb.reason,
                ]

            typer.echo(
                format_table(
                    ("Remote branch", "Disposition", "Unique", "PR", "Reason"),
                    [remote_row(rb) for rb in result.remote_branches],
                )
            )

        for hold in result.runs:
            if not hold.alive:
                typer.echo(
                    f"\nrun {hold.run_id} is not running and holds "
                    f"{len(hold.branches)} branch(es): nothing will retire them.\n"
                    f"  uv run lup-devtools resolve status "
                    f"--run-id {hold.run_id}"
                )


type ActionVerdict = Literal["ok", "forced", "blocked", "refused"]
"""What a preflight probe made of one step.

``blocked`` is a judgement the caller can overrule, and ``--force`` is how.
``refused`` is one they cannot: the step has no safe form, so no flag turns
it into one and the diagnostic offers a different route instead.
"""


class PlannedAction(BaseModel):
    """One step of a deletion, carrying the verdict a preflight probe gave it.

    ``detail`` says what forcing would discard, or why the step cannot run —
    the annotation is a probe result, never a restatement of the flag.
    """

    description: str
    verdict: ActionVerdict = "ok"
    detail: str = ""

    def render(self) -> str:
        match self.verdict:
            case "ok":
                return f"{self.description} (ok{f': {self.detail}' if self.detail else ''})"
            case "forced":
                return f"{self.description} (force: {self.detail})"
            case "blocked":
                return f"{self.description} (blocked: {self.detail})"
            case "refused":
                return f"{self.description} (refused: {self.detail})"


class DeletionPlan(BaseModel):
    """Every step a deletion would take, evaluated without mutating anything."""

    branch: str
    worktree: str | None = None
    stranded: bool = False
    has_local: bool = True
    """Whether ``refs/heads`` carries the branch, which is what ``-D`` needs.

    A branch whose local copy went when its work landed leaves origin's
    behind, and the name then resolves to nothing git can delete locally.
    Every containment question about it has to be asked of the ref that does
    resolve, which :meth:`ref` names.
    """
    has_remote: bool = False
    delete_remote: bool = False
    """Whether origin's copy goes too, which is a decision, not an observation.

    ``has_remote`` is a fact about the world and this is an intention about
    it. Reading both from one field is what made a remote copy delete itself
    for existing — and a copy that exists is the thing that made deleting
    the local branch survivable in the first place.
    """
    actions: list[PlannedAction] = []

    def blocked(self) -> list[PlannedAction]:
        """Every step that stops the run, whether or not forcing would lift it."""
        return [
            action
            for action in self.actions
            if action.verdict in ("blocked", "refused")
        ]

    def forceable(self) -> bool:
        """Whether ``--force`` would change any of those answers."""
        return any(action.verdict == "blocked" for action in self.actions)

    def remote_ref(self) -> str:
        """How git names origin's copy, which is how a comparison spells it."""
        return f"origin/{self.branch}"

    def ref(self) -> str:
        """The ref carrying these commits, which containment has to be read from."""
        return self.branch if self.has_local else self.remote_ref()


def worktree_changes(path: str) -> WorktreeChanges:
    """Count what a worktree holds, so a report can say what force discards."""
    modified = 0
    untracked = 0
    for line in git.lines("-C", path, "status", "--porcelain"):
        if line.startswith("??"):
            untracked += 1
        else:
            modified += 1
    return WorktreeChanges(modified=modified, untracked=untracked)


def locked_worktrees() -> dict[str, str]:  # lup: ignore[dict-str-payload]
    """Map worktree path -> why it was locked, from ``git worktree list``.

    A lock is the one claim on a checkout that survives this command's own
    judgement. Dirt says a checkout holds work; a lock says somebody is
    *using* it, which a clean tree cannot rule out — a session that has just
    committed looks exactly like an abandoned one, and removing it takes the
    directory out from under a process still writing there.

    The reason is whatever the locker passed, empty when they passed none, and
    whole: the hold a session's `git worktree create` writes is JSON.
    """
    return {
        str(listed.path): listed.lock_reason
        for listed in Repository(Path.cwd()).worktrees()
        if listed.locked
    }


def upstream_ref(name: str) -> str | None:
    """The remote-tracking ref this branch follows, if anything says it has one.

    Git's own tracking configuration answers first, and lup's record answers
    where there is none — a branch published by `git pr push` has no tracking
    configuration at all, because the push names its destination rather than
    asking git to write one into the shared config.

    The recorded name is checked against the refs actually here before it is
    handed back, since the caller compares against it and a comparison
    against a ref nothing carries answers no — which reads as a branch ahead
    of a remote it never fell behind, and turns an ordinary delete into one
    reported as discarding work.
    """
    try:
        tracked = git.out(
            "rev-parse", "--symbolic-full-name", f"{name}@{{upstream}}"
        ).strip()
    except sh.ErrorReturnCode:
        tracked = ""
    recorded = records.recorded_upstream(name)
    return tracked or (
        recorded if recorded and Repository(Path.cwd()).resolves(recorded) else None
    )


def holding_copy(ref: str, integration: str, by_content: bool = False) -> str:
    """The copy of the integration branch already holding *ref*, local first.

    Origin's copy answers where the local one does not, because a pull
    request merged on the forge lands there, and the local integration branch
    holds it only once somebody pulls. Judged against the local copy alone,
    every branch merged that way read as unmerged in the meantime, and the
    refusal offered `--force` where nothing was at stake. Either copy holding
    the commits means deleting the branch discards nothing.

    ``by_content`` also counts a branch every commit of which is in by
    patch-id, which is the survey's reading of containment; without it only
    ancestry answers. Empty where neither copy holds it.
    """
    return next(
        (
            copy
            for copy in (integration, f"origin/{integration}")
            if Repository(Path.cwd()).resolves(copy) is not None
            and (
                Repository(Path.cwd()).is_ancestor(ref, copy)
                or (by_content and count_unique_commits(ref, copy) == 0)
            )
        ),
        "",
    )


def outgrew_upstream(name: str) -> bool:
    """Whether the branch holds commits the upstream it tracks does not.

    The question ``git branch -d`` actually asks of a tracking branch, and
    the reason it refuses one every commit of which is already in HEAD. A
    branch has a remote from the first push that carried something, so this
    is the ordinary shape of one that landed by a merge into the integration
    branch rather than by a push of its own: the work is in, and the remote
    copy is simply behind.
    """
    upstream = upstream_ref(name)
    return upstream is not None and not Repository(Path.cwd()).is_ancestor(
        name, upstream
    )


def plan_worktree_step(path: str, stranded: bool, force: bool) -> PlannedAction:
    """Judge the worktree removal — the one irreversible step.

    A lock that is a session's hold, and not a live one, is no refusal: the
    session that took it has left or is the one asking, and the removal
    drops it first.
    """
    from lup.devtools.dev.worktree import hold_on, live_worktree_owners

    if stranded:
        return PlannedAction(
            description=f"Prune stranded worktree: {path}",
            detail="checkout already gone",
        )

    description = f"Remove worktree: {path}"
    if owners := live_worktree_owners(Path(path)):
        return PlannedAction(
            description=description,
            verdict="refused",
            detail=f"live sessions {', '.join(owners)} use this checkout; wait for their departure",
        )
    lock = locked_worktrees().get(path)
    if lock is not None and hold_on(Path(path)) is None:
        return PlannedAction(
            description=description,
            verdict="refused",
            detail=(
                f"the checkout is locked{f': {lock}' if lock else ''} — the "
                "lock is how whoever is working there says so, and no force "
                "this command offers lifts it; `git worktree unlock "
                f"{path}` once they are not"
            ),
        )

    changes = worktree_changes(path)
    if not changes.dirty():
        return PlannedAction(description=description)

    if force:
        return PlannedAction(
            description=description,
            verdict="forced",
            detail=f"discards {changes.summary()}",
        )
    return PlannedAction(
        description=description,
        verdict="blocked",
        detail=f"holds {changes.summary()}; --force discards them",
    )


def plan_branch_step(name: str, force: bool) -> PlannedAction:
    """Judge the branch deletion against the branch work lands on.

    Not against HEAD, which is whichever worktree the caller happens to be
    standing in — routinely a feature branch holding nothing but its own
    work. Read from there, every landed branch is unmerged, and the cleanup
    a merge just asked for is refused as though something were at stake.

    A branch that tracks an upstream is judged against that upstream too, so
    one whose every commit is already in the integration branch is still
    refused while its remote copy sits behind. Reporting that as forced
    rather than blocked is the honest reading: nothing is discarded.

    Containment is the reading :func:`disposition_for` uses, not ancestry.
    They disagree over exactly the branches a sweep is for: work that landed
    through a rebase or a squash is in the integration branch by content and
    behind it by no commit, while `merge-base --is-ancestor` says no because
    the tip is not reachable. Asking the stricter question at the destructive
    gate meant every branch a sweep had just landed demanded `--force`, which
    is the answer for discarding work and was being given for cleaning up
    after it.

    Where the branch really does hold something, the refusal says what it
    found rather than only that it found it. A rebase leaves the subjects
    intact, so counting the unique commits already naming one in the
    integration branch is what tells a reader whether to go and compare —
    and it is a signal, never a verdict, which is why it is spoken here and
    not read above.
    """
    description = f"Delete local branch: {name}"
    integration = get_integration_branch()
    landed = holding_copy(name, integration, by_content=True)
    if landed:
        if outgrew_upstream(name):
            return PlannedAction(
                description=description,
                verdict="forced",
                detail=f"ahead of origin/{name}, which {landed} already contains",
            )
        stale = (
            f"{landed} holds it, and {integration} is "
            f"{count_commits_behind(integration, landed)} behind it — "
            "`git pull --ff-only` in the integration checkout"
            if landed != integration
            else ""
        )
        return PlannedAction(description=description, detail=stale)
    unique = count_unique_commits(name, integration)
    suspects = rewrite_suspects(name, integration)
    trail = (
        f"; {len(suspects)} of {unique} unique commit(s) share a subject with "
        f"{integration}, which is the trace a rebase leaves — "
        f"`git cherry -v {integration} {name}` says which"
        if suspects
        else ""
    )
    if force:
        return PlannedAction(
            description=description,
            verdict="forced",
            detail=f"branch is unmerged{trail}",
        )
    return PlannedAction(
        description=description,
        verdict="blocked",
        detail=f"branch is unmerged{trail}; --force deletes it anyway",
    )


def dependent_pulls(name: str) -> list[int]:
    """Open PRs based on *name*, which deleting origin's copy would close.

    A stacked PR names its parent as its base, and GitHub closes a pull
    request whose base branch is deleted rather than moving it. Landing the
    parent and cleaning up after it is therefore enough to close the child,
    which is the ordinary end of the parent's life and no part of the
    child's — and the branch survives, so the closure reads as work lost
    until somebody recreates the base to reopen it.

    An unanswerable query is no dependents: this refines a deletion the
    caller already asked for, and a network failure is not a reason to
    refuse one.
    """
    try:
        rows = json.loads(
            gh.out(
                "pr",
                "list",
                *repository_arguments(),
                "--base",
                name,
                "--state",
                "open",
                "--json",
                "number",
            )
        )
    except sh.ErrorReturnCode as e:
        logger.warning(
            "Could not check for PRs based on %s: %s", name, decode_stderr(e)
        )
        return []
    return [PRStatus.model_validate(row).number for row in rows]


def plan_remote_step(name: str, force: bool, contained: bool) -> PlannedAction:
    """Judge deleting origin's copy by what it holds and what points at it.

    Containment is handed in because the ref that answers it differs: where a
    local branch exists the caller has already judged that one, and where it
    does not, origin's copy is both the subject and the only copy — the case
    where deleting it is the whole of the deletion and the only thing that
    can say whether anything is discarded.
    """
    description = f"Delete remote branch: origin/{name}"
    dependents = dependent_pulls(name)
    if not contained:
        integration = get_integration_branch()
        listed = ", ".join(f"#{number}" for number in dependents)
        closes = f", closing {listed}" if dependents else ""
        if force:
            return PlannedAction(
                description=description,
                verdict="forced",
                detail=f"discards commits {integration} does not hold{closes}",
            )
        return PlannedAction(
            description=description,
            verdict="blocked",
            detail=(
                f"origin/{name} holds commits {integration} does not{closes}; "
                "--force deletes it anyway"
            ),
        )
    if not dependents:
        return PlannedAction(description=description)
    listed = ", ".join(f"#{number}" for number in dependents)
    if force:
        return PlannedAction(
            description=description,
            verdict="forced",
            detail=f"closes {listed}",
        )
    return PlannedAction(
        description=description,
        verdict="blocked",
        detail=(
            f"{listed} targets this branch and would be closed; "
            "retarget it first, or --force to close it anyway"
        ),
    )


def plan_remote_only_deletion(
    name: str, force: bool, has_remote: bool, remote: bool | None
) -> DeletionPlan:
    """Plan for a name ``refs/heads`` does not carry, which origin still may.

    A survey classifies these — the local copy went when the work landed and
    origin's did not — so the verb has to be able to act on the disposition
    its own survey hands out. Deleting origin's copy is the whole of what the
    name can mean here, and the containment that says whether it discards
    anything is read off that copy: judging it through a local branch that
    was never there reported an unmerged branch where there was no branch at
    all, and reached ``git branch -D`` before the push that was the only
    thing left to run.
    """
    if not has_remote:
        return DeletionPlan(
            branch=name,
            has_local=False,
            actions=[
                PlannedAction(
                    description=f"Delete branch: {name}",
                    verdict="refused",
                    detail=f"no branch by that name, locally or as origin/{name}",
                )
            ],
        )
    if remote is False:
        return DeletionPlan(
            branch=name,
            has_local=False,
            has_remote=True,
            actions=[
                PlannedAction(
                    description=f"Delete branch: {name}",
                    verdict="refused",
                    detail=(
                        f"origin/{name} is the only copy and --no-remote keeps it, "
                        "so there is nothing left to delete"
                    ),
                )
            ],
        )
    contained = bool(holding_copy(f"origin/{name}", get_integration_branch()))
    return DeletionPlan(
        branch=name,
        has_local=False,
        has_remote=True,
        delete_remote=True,
        actions=[plan_remote_step(name, force=force, contained=contained)],
    )


def plan_deletion(
    name: str, force: bool, remote: bool | None = None, scaffold: str = ""
) -> DeletionPlan:
    """Evaluate every precondition a deletion depends on, changing nothing.

    A name resolves locally, only on origin, or nowhere, and the three are
    different deletions rather than one with steps that happen to fail:
    :func:`plan_remote_only_deletion` carries the second and the third.

    A name the survey keeps whatever it holds (:func:`kept_whatever_it_holds`)
    is refused before any of that.

    A dry run and the real path both read this, so what the dry run promises
    is what the real path went on to check.

    Whether origin's copy goes with it defaults to whether the branch is
    merged, because that is what the answer turns on. Cleaning up after a
    merged branch should take the remote too — the commits are in the
    integration branch and the copy is spent. An unmerged branch is the
    opposite case: origin holds the only copy that outlives this command,
    which is exactly what a push before deleting was for, so it stays
    unless a caller says otherwise in so many words.
    """
    from lup.devtools.dev.worktree import branch_exists

    if kept := kept_whatever_it_holds(name, scaffold):
        return DeletionPlan(
            branch=name,
            actions=[
                PlannedAction(
                    description=f"Delete branch: {name}",
                    verdict="refused",
                    detail=kept,
                )
            ],
        )

    has_remote = (
        Repository(Path.cwd()).resolves(f"refs/remotes/origin/{name}") is not None
    )
    if not branch_exists(name):
        return plan_remote_only_deletion(
            name, force=force, has_remote=has_remote, remote=remote
        )

    worktree = parse_worktrees().get(name)
    stranded = worktree is not None and not Path(worktree).exists()
    actions: list[PlannedAction] = []

    if worktree is not None:
        actions.append(plan_worktree_step(worktree, stranded=stranded, force=force))

    actions.append(plan_branch_step(name, force=force))

    merged = bool(holding_copy(name, get_integration_branch()))
    delete_remote = has_remote and (merged if remote is None else remote)
    if delete_remote:
        actions.append(plan_remote_step(name, force=force, contained=merged))

    return DeletionPlan(
        branch=name,
        worktree=worktree,
        stranded=stranded,
        has_remote=has_remote,
        delete_remote=delete_remote,
        actions=actions,
    )


def abort_deletion(plan: DeletionPlan, completed: list[str], failure: str) -> NoReturn:
    """Report a mid-deletion failure, repairing a stranded registration first.

    ``git worktree remove`` clears the checkout before it unregisters, so a
    failure here can leave a worktree git still believes in. Pruning is the
    repair, and the caller cannot be expected to know that.

    Prune, removal, and the branch deletion itself all take the config lock,
    so all three fail alike where the sandbox holds it, and there the repair
    is not a repair. That is reported by attempting the prune and letting its
    own failure carry the diagnosis, rather than by reading the lock first:
    the admin directories of a contained session are unwritable for the whole
    session, so a diagnosis conditioned on them alone explains every failure
    equally, including the ones it has nothing to do with — and it stood
    where the repair would otherwise have run.
    """
    typer.echo(f"Failed to delete {plan.branch}: {failure}", err=True)

    if plan.worktree is not None and not Path(plan.worktree).exists():
        try:
            git("worktree", "prune")
            typer.echo(
                f"Pruned the stranded registration for {plan.worktree}", err=True
            )
        except sh.ErrorReturnCode as error:
            typer.echo(
                f"The checkout at {plan.worktree} is gone but still registered. "
                f"Recover with `git worktree prune`: {decode_stderr(error)}",
                err=True,
            )
            diagnosis = config_lock_diagnosis()
            if diagnosis:
                typer.echo(diagnosis, err=True)

    typer.echo(f"Completed first: {', '.join(completed) or 'nothing'}", err=True)
    raise typer.Exit(1)


def worktree_left_as_mount_point(path: str) -> bool:
    """Whether git gave up the registration and only the directory's removal failed.

    `git worktree remove` clears the checkout and unregisters it before the
    final rmdir, and a directory that is a mount point in this session's
    namespace -- every checkout a launch bind-mounted is one -- refuses that
    last step alone, after everything before it succeeded. On a land sweep
    every first removal fails there with the entry gone and the directory
    empty, and a report read off the failure would say nothing had
    completed. The mount is not a failure of the deletion; it is the host's
    directory to drop once the container is gone, so the deletion carries on
    to the branch and says what it left.

    Asked of the mount table and of git rather than read off the error's
    words, because both facts are what the decision rests on: a mount point
    still registered is a failure to report, and an unregistered directory
    that is no mount point was removed outright.

    The mount half goes through :func:`~lup.sandbox.observed.is_mount_point`
    rather than :meth:`pathlib.Path.is_mount`, which answers by device number
    and so cannot see the same-filesystem bind every launch makes. Under that
    probe this would answer false for every checkout a launch bind-mounted,
    and the branch it guards would never be taken.
    """
    return is_mount_point(Path(path)) and path not in parse_worktrees().values()


def run_deletion(plan: DeletionPlan, force: bool) -> None:
    """Carry out a plan whose preflight passed, reporting what actually ran."""
    from lup.devtools.dev.worktree import (
        drop_the_hold,
        refuse_live_worktree_removal,
        said_checkout_state_removed,
    )

    completed: list[str] = []

    match plan:
        case DeletionPlan(stranded=True, worktree=str() as worktree):
            try:
                # A hold would keep the entry through the prune, and the
                # checkout it held is already gone.
                drop_the_hold(Path(worktree))
                git("worktree", "prune")
                typer.echo(f"Pruned stranded worktree: {plan.worktree}")
                completed.append("pruned worktree")
            except sh.ErrorReturnCode as error:
                abort_deletion(
                    plan, completed, f"prune failed: {attributed_stderr(error)}"
                )
        case DeletionPlan(worktree=str() as worktree):
            refuse_live_worktree_removal(Path(worktree))
            try:
                drop_the_hold(Path(worktree))
                git("worktree", "remove", *(["--force"] if force else []), worktree)
                typer.echo(f"Removed worktree: {worktree}")
                completed.append("removed worktree")
                said_checkout_state_removed(Path(worktree))
            except sh.ErrorReturnCode as error:
                if not worktree_left_as_mount_point(worktree):
                    # `git worktree remove` unregisters before its final rmdir,
                    # so the entry can be gone even where the failure is real.
                    # Read what happened rather than what was attempted: a
                    # report of "nothing" sends the next run looking for an
                    # entry git no longer holds.
                    if worktree not in parse_worktrees().values():
                        completed.append("unregistered worktree")
                    abort_deletion(
                        plan,
                        completed,
                        f"worktree removal failed: {attributed_stderr(error)}",
                    )
                typer.echo(
                    f"Unregistered worktree: {worktree} — its directory is a "
                    "mount point of this session, so it stays, empty, for the "
                    "host to remove once the container is gone"
                )
                completed.append("unregistered worktree")

    # A name origin alone carries has nothing here to delete, and asking git
    # to delete it anyway is what stopped the run before the push that was
    # the whole deletion.
    if plan.has_local:
        try:
            # The preflight is what judges containment, and it judges against
            # the integration branch. `-d` would ask git the same question
            # again from HEAD and from the upstream, refuse on either, and
            # refuse here — past the worktree removal above, which is the one
            # place a refusal was promised to change nothing.
            git("branch", "-D", plan.branch)
            typer.echo(f"Deleted branch: {plan.branch}")
            completed.append("deleted branch")
        except sh.ErrorReturnCode as error:
            abort_deletion(
                plan, completed, f"branch deletion failed: {decode_stderr(error)}"
            )

    if not plan.delete_remote:
        if plan.has_remote:
            typer.echo(
                f"Kept remote branch: origin/{plan.branch} — it holds these "
                "commits now (--remote deletes it too)"
            )
        return

    try:
        git("push", "origin", "--delete", plan.branch)
        typer.echo(f"Deleted remote branch: origin/{plan.branch}")
    except sh.ErrorReturnCode as error:
        failure = f"remote deletion failed: {decode_stderr(error)}"
        # Where origin held the only copy this push was the deletion, so a
        # warning beside a zero exit would report a branch deleted that stands.
        if not plan.has_local:
            abort_deletion(plan, completed, failure)
        typer.echo(f"Warning: {failure}", err=True)


def delete_branch(
    name: str,
    dry_run: bool,
    force: bool,
    remote: bool | None = None,
    preserved: str = "",
    scaffold: str = "",
) -> None:
    """Delete a branch and its worktree, and origin's copy if it is spent.

    ``preserved`` names the ref a caller has already parked the commits at,
    for the one path — :func:`run_retirement` — that preserves them before
    deleting. Empty means nobody has, which is the case the warning below is
    written for. ``scaffold`` names the carrier :func:`plan_deletion` refuses.

    ``name`` need not be a local branch: a name origin alone carries is what
    ``git survey`` reports under its own heading and hands a disposition, and
    this is the verb that disposition names.
    """
    cur = Repository(Path.cwd()).branch()
    if name == cur:
        typer.echo(f"Error: cannot delete the current branch ({name})", err=True)
        raise typer.Exit(1)

    plan = plan_deletion(name, force, remote, scaffold)

    if dry_run:
        typer.echo(f"Would perform {len(plan.actions)} action(s):")
        for action in plan.actions:
            typer.echo(f"  {action.render()}")
        typer.echo(f"  {traces.archive(name, dry_run=True).summary()}")
        return

    blocked = plan.blocked()
    if blocked:
        typer.echo(f"Refusing to delete {name} — nothing was changed:", err=True)
        for action in blocked:
            typer.echo(f"  {action.render()}", err=True)
        if plan.forceable():
            typer.echo("Use --force to override.", err=True)
        raise typer.Exit(1)

    integration = get_integration_branch()
    landed = holding_copy(plan.ref(), integration)
    contained = bool(landed)
    if landed not in ("", integration):
        typer.echo(
            f"{name} is in {landed}, which {integration} has not pulled: "
            "`git pull --ff-only` in the integration checkout brings it level"
        )
    if plan.delete_remote and not contained and not preserved:
        typer.echo(
            f"Warning: {name} holds commits {integration} does not, and origin/{name} "
            "is going with it — after this the work is in no branch. To keep "
            f"it, `git retire {name} --reason ...` closes a pull request over "
            "it first, which preserves the commits past the deletion.",
            err=True,
        )

    # Before the worktree goes, not after: its trace store is usually the only
    # copy, and every later reader would see absence rather than loss.
    traces.keep_before_deleting(name)
    # The same reason, one fact along. Whether this branch landed is read off
    # the ref the next line removes, and a deferral parked on it asks that
    # question later, when absence is all there is to read and absence is not
    # an answer. So the verdict already reached above is written down.
    records.record_landing(name, landed)
    run_deletion(plan, force)


class RetirementPlan(BaseModel):
    """Every step retiring a branch takes, evaluated without mutating anything.

    Retiring is deleting a branch whose work is *not* wanted, without losing
    the work. The two are usually the same act and must not be: a branch the
    integration branch never absorbed, deleted with no copy on the remote,
    leaves its commits reachable from nothing and a collector free to take
    them. `git delete` says so at the moment it happens, which is too late to
    be a choice.

    A pull request is the durable copy. GitHub writes the head of every one
    it has ever seen to ``refs/pull/<number>/head`` and keeps it there
    whether the request merged, and whether the branch behind it still
    exists — so a request opened and closed over work nobody wants outlives
    both the branch and the remote copy, and carries the reason beside the
    commits rather than in a session nobody will read again.
    """

    branch: str
    integration: str
    unique_commits: int
    subjects: list[str] = []
    """``<short sha> <subject>`` per commit the integration branch lacks."""
    pull_request: int | None = None
    """An existing request for this branch, reused rather than duplicated."""
    actions: list[PlannedAction] = []

    def blocked(self) -> list[PlannedAction]:
        """Every step that stops the run, whether or not forcing would lift it."""
        return [
            action
            for action in self.actions
            if action.verdict in ("blocked", "refused")
        ]

    def body(self, reason: str, number: int) -> str:
        """The record the pull request exists to keep.

        Written after the number is known, because the recovery line is the
        one thing a reader arriving here needs and a command they have to
        assemble themselves is one they will get wrong.
        """
        listed = "\n".join(f"- `{subject}`" for subject in self.subjects)
        return f"""**This pull request exists to be closed, not merged.**

Retiring `{self.branch}`: {reason}

Its commits stay reachable after the branch is deleted. GitHub keeps a closed
request's head as `refs/pull/{number}/head` whether or not the branch behind it
still exists, where a deleted branch with no remote copy becomes unreachable and
is collected.

Recover the work with:

```bash
git fetch origin refs/pull/{number}/head:{self.branch}
```

## What it held

{self.unique_commits} commit(s) `{self.integration}` does not carry:

{listed}
"""


def unique_subjects(branch: str, integration: str) -> list[str]:
    """Each commit the integration branch lacks, as sha and subject.

    By ancestry, not by the ``--cherry-pick`` filter the survey counts with,
    because the two are wrong in opposite directions and only one of them is
    survivable here. This decides whether there is anything to preserve, so
    a false *yes* costs a pull request nobody needed and a false *no* sends
    the caller to `git delete` over work that had no other copy.
    """
    return [
        line
        for line in git.lines(
            "log", "--format=%h %s", "--no-merges", f"{integration}..{branch}"
        )
        if line
    ]


def plan_retirement(name: str, integration: str, scaffold: str = "") -> RetirementPlan:
    """Evaluate every step a retirement depends on, changing nothing.

    A retirement ends in a deletion, so a name no deletion may take
    (:func:`kept_whatever_it_holds`) is refused before anything is pushed.
    """
    from lup.devtools.dev.worktree import branch_exists

    if kept := kept_whatever_it_holds(name, scaffold):
        refused = PlannedAction(
            description=f"Retire {name}", verdict="refused", detail=kept
        )
        return RetirementPlan(
            branch=name, integration=integration, unique_commits=0, actions=[refused]
        )

    actions: list[PlannedAction] = []
    if not branch_exists(name):
        actions.append(
            PlannedAction(
                description=f"Retire {name}",
                verdict="blocked",
                detail="no such local branch",
            )
        )
        return RetirementPlan(
            branch=name, integration=integration, unique_commits=0, actions=actions
        )

    subjects = unique_subjects(name, integration)
    existing = get_pr_info(name)
    reused = existing.number if existing is not None and existing.reusable() else None

    actions.append(PlannedAction(description=f"Push {name} to origin"))
    if reused is None:
        actions.append(
            PlannedAction(description=f"Open a pull request onto {integration}")
        )
    else:
        actions.append(
            PlannedAction(
                description=f"Reuse pull request #{reused}",
                detail="already open for this branch",
            )
        )
    actions.append(PlannedAction(description="Close it without merging"))
    actions.append(PlannedAction(description=f"Delete {name}, locally and on origin"))

    return RetirementPlan(
        branch=name,
        integration=integration,
        unique_commits=len(subjects),
        subjects=subjects,
        pull_request=reused,
        actions=actions,
    )


def run_retirement(plan: RetirementPlan, reason: str) -> int:
    """Carry out a plan whose preflight passed, returning the request's number.

    Ordered so nothing is destroyed before the copy that replaces it exists:
    the push, then the request, then the close, and only then the deletion.
    A failure at any step leaves the branch where it was.
    """
    try:
        git("push", "--force-with-lease", "origin", f"{plan.branch}:{plan.branch}")
        typer.echo(f"Pushed {plan.branch} to origin")
    except sh.ErrorReturnCode as error:
        typer.echo(f"Could not push {plan.branch}: {decode_stderr(error)}", err=True)
        raise typer.Exit(1)

    number = plan.pull_request
    if number is None:
        try:
            raw = gh.out(
                "pr",
                "create",
                *repository_arguments(),
                "--base",
                plan.integration,
                "--head",
                plan.branch,
                "--title",
                f"retire: {plan.branch}",
                "--body",
                f"Retiring `{plan.branch}`: {reason}",
            )
        except sh.ErrorReturnCode as error:
            typer.echo(f"Could not open a request: {decode_stderr(error)}", err=True)
            raise typer.Exit(1)
        number = int(PurePosixPath(urlparse(raw.strip().splitlines()[-1]).path).name)
        typer.echo(f"Opened #{number}")

    try:
        gh(
            "pr",
            "edit",
            str(number),
            *repository_arguments(),
            "--body",
            plan.body(reason, number),
        )
    except sh.ErrorReturnCode as error:
        # The request carries the commits either way; only the note is missing.
        logger.warning("could not write the retirement note: %s", decode_stderr(error))

    try:
        gh(
            "pr",
            "close",
            str(number),
            *repository_arguments(),
            "--comment",
            f"Retired, not merged. Recover with "
            f"`git fetch origin refs/pull/{number}/head:{plan.branch}`.",
        )
        typer.echo(f"Closed #{number}")
    except sh.ErrorReturnCode as error:
        typer.echo(f"Could not close #{number}: {decode_stderr(error)}", err=True)
        raise typer.Exit(1)

    return number


def retire_branch(
    name: str,
    reason: str,
    dry_run: bool,
    integration: str | None = None,
    scaffold: str = "",
) -> None:
    """Retire a branch through a pull request, so its commits outlive it.

    ``scaffold`` names the carrier :func:`plan_retirement` refuses, as
    :func:`delete_branch` is told it.
    """
    target = integration if integration is not None else get_integration_branch()
    if name == Repository(Path.cwd()).branch():
        typer.echo(f"Error: cannot retire the current branch ({name})", err=True)
        raise typer.Exit(1)

    plan = plan_retirement(name, target, scaffold)

    if dry_run:
        typer.echo(f"Would perform {len(plan.actions)} action(s):")
        for action in plan.actions:
            typer.echo(f"  {action.render()}")
        if not plan.blocked():
            typer.echo(
                f"  Preserving {plan.unique_commits} commit(s) as refs/pull/<n>/head"
            )
        return

    blocked = plan.blocked()
    if blocked:
        typer.echo(f"Refusing to retire {name} — nothing was changed:", err=True)
        for action in blocked:
            typer.echo(f"  {action.render()}", err=True)
        raise typer.Exit(1)

    if not plan.unique_commits:
        typer.echo(
            f"{name} holds nothing {target} lacks — `git delete` is enough, and "
            "no request is needed to preserve it.",
            err=True,
        )
        raise typer.Exit(1)

    number = run_retirement(plan, reason)
    delete_branch(
        name,
        dry_run=False,
        force=True,
        remote=True,
        preserved=f"refs/pull/{number}/head",
    )
    typer.echo(
        f"Retired {name}: recover with `git fetch origin refs/pull/{number}/head`"
    )


def create_resolve_branch(concern_id: str) -> None:
    """Create and switch to the `resolve/<id>` branch for a /lup:resolve editor.

    The execute workflow's editor calls this as its first step, through the
    allowlisted `uv run lup-devtools` path, instead of a raw `git checkout -b` —
    so the bash hook needs no editor special-case. The name is fixed to the
    `resolve/<slug>` convention the workflow's merge and cleanup rely on.
    """
    slug = concern_id.strip().strip("/")  # lup: ignore[string-strip] — typed-id hygiene
    ok = bool(slug) and slug[0].isalnum()
    ok = ok and all(c.isalnum() or c in "._-" for c in slug)
    if not ok:
        typer.echo(f"Invalid concern id: {concern_id!r}", err=True)
        raise typer.Exit(1)
    branch = f"resolve/{slug}"
    try:
        git("checkout", "-b", branch)
    except sh.ErrorReturnCode as e:
        typer.echo(f"Could not create {branch}: {decode_stderr(e)}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Created and switched to {branch}")
