"""PR lifecycle: status, merge, push, checks, and base sync.

Mechanical helpers for ``/lup:close`` and ``/lup:rebase``.

Examples::

    $ uv run lup-devtools git pr status --json
    $ uv run lup-devtools git pr merge 42
    $ uv run lup-devtools git pr sync-base --json
    $ uv run lup-devtools git pr push --force --json
    $ uv run lup-devtools git pr create --base dev --title "feat: search" --body "..."
    $ uv run lup-devtools git pr create --base dev --title "feat: search" --body-file body.md
    $ uv run lup-devtools git pr update 42 --body-file body.md
"""

import json
import logging
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlparse

import sh
import typer
from pydantic import BaseModel, Field

from lup.execution.git import Repository
import lup.devtools.dev.records as records
from lup.devtools.dev.branches import (
    delete_branch,
    detect_base_branch,
    get_integration_branch,
    parse_branches,
    parse_worktrees,
)
from lup.devtools.dev.remote_auth import check_forge_api

from lup.execution.shell import git
from lup.devtools.utils import (
    gh,
    decode_stderr,
    output_json,
    repository_arguments,
)

logger = logging.getLogger(__name__)


class MergeMethod(StrEnum):
    """How a PR's commits reach the base branch.

    The three GitHub offers. Which one a repository wants is its own
    decision rather than this command's: one whose history is merge commits
    reads a squash as a break in it, and one that squashes reads the
    reverse. A merge commit is the default because it is the only one of the
    three that loses nothing — the branch's own commits stay reachable, and
    a PR stacked on this one keeps its base as a real ancestor instead of
    facing a rewritten copy of the work it already contains.
    """

    merge = "merge"
    squash = "squash"
    rebase = "rebase"


class ChecksState(StrEnum):
    """Where a PR's checks stand, in the three answers they can give.

    A check that has not finished is neither passing nor failing, and two
    names force it under one of them: filtering to the completed checks and
    asking ``all()`` answers "passing" for a PR whose only check is still
    running, because nothing is left to disagree. That is how a run that
    concluded as a failure was presented as the one clean branch of three.
    The third name is what lets a reader wait rather than decide.
    """

    passing = "passing"
    failing = "failing"
    running = "running"

    def marker(self) -> str:
        """The character this state prints beside a check's name."""
        match self:
            case ChecksState.passing:
                return "✓"
            case ChecksState.failing:
                return "✗"
            case ChecksState.running:
                return "…"


def current_branch() -> str:
    """The branch this checkout stands on, empty on a detached head."""
    return Repository(Path.cwd()).branch()


class ReviewInfo(BaseModel):
    author: str
    state: str
    body: str


class CheckInfo(BaseModel):
    name: str
    status: str
    conclusion: str


def check_state(
    check: CheckInfo,
    # Which conclusions a finished check may report without being a failure
    # is a judgement about the forge's vocabulary, so a project reading it
    # differently passes its own rather than editing this.
    passing: tuple[str, ...] = ("SUCCESS", "NEUTRAL", "SKIPPED"),
) -> ChecksState:
    """Where one check stands, reading its status before its conclusion.

    A check reports a conclusion only once it has one, so an unfinished
    check's empty conclusion is not a verdict to compare — asking anyway
    reads "not a success" off a run that has not said anything yet.
    """
    if check.status.upper() != "COMPLETED":
        return ChecksState.running
    return (
        ChecksState.passing
        if check.conclusion.upper() in passing
        else ChecksState.failing
    )


def rollup_state(checks: list[CheckInfo]) -> ChecksState:
    """The one answer a PR's checks give together, worst first.

    A check that concluded as a failure settles the run whatever else is
    still going; short of one, anything unfinished holds the answer open.
    Passing is what is left — every check finished, none of them failing —
    which is also what an empty list says, having nothing to wait for.
    """
    states = [check_state(check) for check in checks]
    return next(
        (
            state
            for state in (ChecksState.failing, ChecksState.running)
            if state in states
        ),
        ChecksState.passing,
    )


class GhAuthor(BaseModel):
    """`author` object inside `gh pr view --json reviews`."""

    login: str = "unknown"


class GhReview(BaseModel):
    """One element of `gh pr view --json reviews`, as gh names the fields."""

    author: GhAuthor | None = None
    state: str = ""
    body: str = ""


class GhCheck(BaseModel):
    """One `statusCheckRollup` element: check runs carry `name`/`status`/
    `conclusion`; legacy status contexts carry `context`/`state`."""

    name: str = ""
    context: str = ""
    status: str = ""
    conclusion: str | None = ""
    state: str = ""

    def as_check(self) -> CheckInfo:
        """Normalize GitHub's two check payloads before computing the rollup."""
        status, conclusion = self.status, self.conclusion or ""
        if not status:
            match self.state.upper():
                case "SUCCESS":
                    status, conclusion = "COMPLETED", "SUCCESS"
                case "ERROR" | "FAILURE":
                    status, conclusion = "COMPLETED", "FAILURE"
                case _:
                    status, conclusion = "PENDING", ""
        return CheckInfo(
            name=self.name or self.context or "unknown",
            status=status,
            conclusion=conclusion,
        )


class GhPrDetail(BaseModel):
    """The `gh pr view --json` payload (aliases are gh's camelCase names)."""

    reviews: list[GhReview] = []
    checks: list[GhCheck] = Field(default=[], alias="statusCheckRollup")
    review_decision: str = Field(default="", alias="reviewDecision")
    mergeable: str = ""
    state: str = ""
    head_ref: str = Field(default="", alias="headRefName")
    base_ref: str = Field(default="", alias="baseRefName")


class GhPrRef(BaseModel):
    """One row of `gh pr list --json number,title,url`."""

    number: int
    title: str = ""
    url: str = ""


class PRInfo(BaseModel):
    number: int
    title: str
    url: str
    base_ref: str = ""
    review_decision: str
    mergeable: str
    checks_state: ChecksState
    reviews: list[ReviewInfo]
    checks: list[CheckInfo]

    def render(self) -> None:
        """Pretty-print PR status with formatted reviews and checks."""
        typer.echo(f"\n  PR #{self.number}: {self.title}")
        typer.echo(f"  {self.url}")
        typer.echo(f"  Review: {self.review_decision or 'pending'}")
        typer.echo(f"  Mergeable: {self.mergeable}")

        if self.reviews:
            typer.echo(f"\n  Reviews ({len(self.reviews)}):")
            author_width = max(len(r.author) for r in self.reviews)
            for r in self.reviews:
                typer.echo(f"    {r.author:<{author_width}} {r.state}")

        if self.checks:
            typer.echo(f"\n  Checks ({len(self.checks)}): {self.checks_state}")
            for c in self.checks:
                marker = check_state(c).marker()
                typer.echo(f"    {marker} {c.name}: {c.conclusion or c.status}")


class PRResult(BaseModel):
    """One PR command's outcome, rendering itself for a human reader.

    The only question the CLI asks of a result is how to print it, so the base
    declares that and each variant answers it — a new command's result is one
    class rather than an edit to a printer that would have to notice it. The
    default answer names each field in turn, which is the whole of what a flat
    result has to say; a variant whose shape deserves a layout overrides it.
    """

    def render(self) -> None:
        """Print this result as plain lines, one per field."""
        for key, value in self.model_dump().items():
            match value:
                case list():
                    typer.echo(f"{key}:")
                    for item in value:
                        typer.echo(f"  - {item}")
                case dict():
                    typer.echo(f"{key}:")
                    for k, v in value.items():
                        typer.echo(f"  {k}: {v}")
                case _:
                    typer.echo(f"{key}: {value}")


class PRStatusResult(PRResult):
    branch: str
    pr: PRInfo | None

    def render(self) -> None:
        if self.pr is None:
            typer.echo(f"branch: {self.branch}")
            typer.echo("pr: no open PR")
            return
        self.pr.render()


class MergeResult(PRResult):
    pr_number: int
    merged: bool
    integration_branch: str
    pulled: bool


class SyncBaseResult(PRResult):
    feature_branch: str
    base_branch: str
    base_source: Literal["explicit", "created", "recorded", "guessed"]
    """Where the base came from, which decides whether anything was merged.

    Reaches a caller reading the JSON as well as one reading the terminal,
    since it is the difference between a merge that did not happen and one
    that was not needed.
    """
    merged: bool
    conflicts: list[str]

    base_synced: bool
    """Whether the base was brought up to date from its remote before merging.

    Reported rather than warned about, because a merge onto a base that could
    not be refreshed succeeds exactly like one onto a base that could, and the
    two are worth different things to whoever asked. A caller reading the JSON
    -- which is what the JSON is for -- cannot tell them apart from `merged`,
    and a rebase workflow that resets onto a stale base rewrites what it was
    supposed to preserve.
    """

    sync_complaint: str = ""
    """Why the base was not refreshed, empty when it was.

    A contained session reaches this by the boundary working as designed.
    Under a worker's lease the base is a sibling worktree held read-only with
    its administrative entry, so a fetch that would write `FETCH_HEAD` under
    it is refused; under an operator's, a human-owned file mounted read-only
    over the base refuses the fast-forward that would replace it. Neither is
    a fault to repair, so it is said plainly and carried rather than escalated.
    """


class ExistingPR(BaseModel):
    number: int
    url: str


class PushResult(PRResult):
    branch: str
    pushed: bool
    force: bool
    existing_pr: ExistingPR | None

    push_complaint: str = ""
    """Why the branch did not reach the remote, empty when it did.

    ``pushed: false`` on its own is a verdict with no case behind it: git's
    own message went to stderr, which is not where a reader of the JSON
    looks, and this transport fails intermittently — a plain retry clears
    it — so the message is what says whether to retry or to go and fix
    something. It is a field rather than a log line for the same reason the
    flag is.
    """

    upstream: str = ""
    """The remote branch this one was recorded as publishing to, or none.

    Reported because the record is the thing `-u` claimed to write and did
    not: a push that says it set up tracking and recorded nothing looks
    exactly like one that recorded everything, and a reader had no field to
    tell them apart by.
    """


class CreateResult(PRResult):
    number: int
    url: str


# The result already answers how to print itself; what is left is the flag.
def output_result(result: PRResult, as_json: bool) -> None:
    if as_json:
        output_json(result)
        return
    result.render()


class DetectedBase(BaseModel):
    """The auto-detected base branch and how its name was determined.

    ``created`` sits with ``recorded`` rather than with ``guessed``, which is
    what decides whether :func:`sync_base` merges. Both name a base somebody
    or something wrote down at the moment of the cut; topology names one
    read off the shape of the graph afterwards, and only that reading is
    unsafe to merge onto without a caller confirming it.
    """

    name: str
    source: Literal["recorded", "created", "guessed"]


def find_base_branch() -> DetectedBase:
    """Auto-detect the base branch, preferring the recorded creation base.

    A refusal from detection reaches the caller rather than being answered
    with the integration branch. Detection refuses over two things — a
    checkout holding no other local branch, and one whose branches share no
    history — and in both the integration branch is not a guess but a name
    that is absent or unrelated, so substituting it hands back something
    worse than the refusal. A tie between bases is not one of the two: it is
    an answer, and is taken.
    """
    candidate = detect_base_branch()
    return DetectedBase(name=candidate.name, source=candidate.source)


def status(
    branch: str | None,
    as_json: bool,
) -> None:
    """Fetch PR review status, checks, and comments for a branch."""
    branch_name = branch or current_branch()

    try:
        rows = json.loads(
            gh.out(
                "pr",
                "list",
                *repository_arguments(),
                "--head",
                branch_name,
                "--state",
                "open",
                "--json",
                "number,title,url",
            )
        )
    except sh.ErrorReturnCode as e:
        typer.echo(f"Failed to query PRs via gh: {decode_stderr(e)}", err=True)
        raise typer.Exit(1)
    prs = [GhPrRef.model_validate(row) for row in rows]

    if not prs:
        result = PRStatusResult(branch=branch_name, pr=None)
        output_result(result, as_json)
        if not as_json:
            typer.echo(f"No open PR found for branch {branch_name}")
        return

    pr_data = prs[0]
    pr_number = pr_data.number

    try:
        detail = GhPrDetail.model_validate_json(
            gh.out(
                "pr",
                "view",
                str(pr_number),
                *repository_arguments(),
                "--json",
                "reviews,statusCheckRollup,mergeable,mergeStateStatus,reviewDecision,baseRefName",
            )
        )
    except sh.ErrorReturnCode as e:
        typer.echo(
            f"Failed to fetch PR #{pr_number} via gh: {decode_stderr(e)}", err=True
        )
        raise typer.Exit(1)

    reviews = [
        ReviewInfo(
            author=r.author.login if r.author else "unknown",
            state=r.state,
            body=r.body,
        )
        for r in detail.reviews
    ]

    checks = [check.as_check() for check in detail.checks]

    pr_info = PRInfo(
        number=pr_number,
        title=pr_data.title,
        url=pr_data.url,
        base_ref=detail.base_ref,
        review_decision=detail.review_decision,
        mergeable=detail.mergeable,
        checks_state=rollup_state(checks),
        reviews=reviews,
        checks=checks,
    )

    result = PRStatusResult(branch=branch_name, pr=pr_info)
    output_result(result, as_json)


def pr_merged(pr_number: int) -> bool:
    """Whether GitHub says the PR is merged, asked rather than inferred.

    ``gh pr merge`` merges and then deletes the branch, and reports a
    failure of the second as a failure of the whole. Reading the state back
    separates a merge that did not happen from a cleanup that did not, which
    are opposite situations: the first is retried, the second is finished
    work with a leftover, and treating either as the other is how a landed
    PR comes to look like one still waiting.
    """
    try:
        detail = GhPrDetail.model_validate_json(
            gh.out(
                "pr", "view", str(pr_number), *repository_arguments(), "--json", "state"
            )
        )
    except sh.ErrorReturnCode:
        logger.exception("could not read PR #%s state back", pr_number)
        return False
    return detail.state == "MERGED"


def pr_head_ref(pr_number: int) -> str:
    """Which branch the PR merges from, asked before its cleanup needs it.

    Read from the PR rather than taken from the checkout, because the branch
    a PR merges from is the PR's own fact and whoever runs this may be
    anywhere — the integration worktree, another feature's, or a clone that
    never fetched the head at all.
    """
    try:
        detail = GhPrDetail.model_validate_json(
            gh.out(
                "pr",
                "view",
                str(pr_number),
                *repository_arguments(),
                "--json",
                "headRefName",
            )
        )
    except sh.ErrorReturnCode:
        logger.exception("could not read PR #%s head branch", pr_number)
        return ""
    return detail.head_ref


def pr_base_ref(pr_number: int) -> str:
    """Which branch the PR merges into, asked before a merge trusts it.

    A stacked PR's base is its stack parent, so merging it lands the work in
    another feature branch while the caller pulls the integration branch and
    reads nothing arriving. The base is the PR's own fact, read from the
    forge for the reason :func:`pr_head_ref` reads the head there.
    """
    try:
        detail = GhPrDetail.model_validate_json(
            gh.out(
                "pr",
                "view",
                str(pr_number),
                *repository_arguments(),
                "--json",
                "baseRefName",
            )
        )
    except sh.ErrorReturnCode:
        logger.exception("could not read PR #%s base branch", pr_number)
        return ""
    return detail.base_ref


def cleanup_merged_branch(name: str) -> None:
    """Delete a merged PR's branch through the path that knows about worktrees.

    ``gh pr merge --delete-branch`` cannot. It runs a plain ``git branch -d``,
    which refuses while any worktree holds the branch, so in a tree of
    worktrees every merge reported a cleanup failure and left the branch and
    its checkout behind. :func:`delete_branch` removes the worktree first,
    archives the branch's traces — usually their only copy, and gone for good
    once the checkout is — and takes origin's copy once the commits are in the
    integration branch.

    A failure here is reported rather than raised, keeping the distinction
    :func:`pr_merged` draws: the merge already happened, so a branch left
    standing is finished work with a loose end, not work to retry.
    """
    if not any(branch["name"] == name for branch in parse_branches()):
        return
    try:
        delete_branch(name, dry_run=False, force=False)
    except (typer.Exit, SystemExit):
        typer.echo(f"The merge stands; {name} is still here to remove.", err=True)


def merge(
    pr_number: int,
    dry_run: bool,
    as_json: bool = False,
    method: MergeMethod = MergeMethod.merge,
    gh_args: tuple[str, ...] = (),
    retarget: bool = False,
) -> None:
    """Merge a PR and pull changes into the integration branch.

    Args:
        pr_number: PR to merge.
        dry_run: Report what would happen, changing nothing.
        as_json: Emit the result as JSON.
        method: How the commits reach the base branch.
        gh_args: Further flags handed to ``gh pr merge`` untouched, for
            anything this signature does not name.
        retarget: Point a stacked PR's base at the integration branch first.
    """
    integration = get_integration_branch()

    if dry_run:
        typer.echo(f"Would merge PR #{pr_number} ({method})")
        typer.echo(f"Would pull changes into {integration}")
        return

    # A stacked PR's base is its stack parent, and merging it there lands
    # the work in another feature branch while this command pulls the
    # integration branch and reports nothing arriving. Retargeting must
    # happen while the head still holds commits the integration branch
    # lacks — once it is contained, GitHub refuses the edit with "no new
    # commits" and the PR can only ever be closed, never marked merged.
    base_ref = pr_base_ref(pr_number)
    if base_ref and base_ref != integration:
        if not retarget:
            typer.echo(
                f"PR #{pr_number} merges into '{base_ref}', not '{integration}': "
                f"merging now would land the work in that branch instead. "
                f"Rerun with --retarget to point it at {integration} first.",
                err=True,
            )
            raise typer.Exit(1)
        gh("pr", "edit", str(pr_number), *repository_arguments(), "--base", integration)
        typer.echo(f"Retargeted PR #{pr_number}: {base_ref} -> {integration}")

    head_ref = pr_head_ref(pr_number)

    try:
        gh(
            "pr",
            "merge",
            str(pr_number),
            *repository_arguments(),
            f"--{method}",
            *gh_args,
        )
        typer.echo(f"Merged PR #{pr_number}")
    except sh.ErrorReturnCode as e:
        if not pr_merged(pr_number):
            typer.echo(f"Merge failed: {decode_stderr(e)}", err=True)
            raise typer.Exit(1)
        typer.echo(
            f"Merged PR #{pr_number}, but its cleanup did not finish: "
            f"{decode_stderr(e)}",
            err=True,
        )

    integration_path = parse_worktrees().get(integration)
    pulled = False
    if integration_path and Path(integration_path).is_dir():
        try:
            git("-C", str(integration_path), "pull", "--ff-only")
            typer.echo(f"Pulled changes into {integration}")
            pulled = True
        except sh.ErrorReturnCode as e:
            typer.echo(
                f"Warning: pull failed in {integration}: {decode_stderr(e)}", err=True
            )

    # After the pull, so the branch is an ancestor of the integration branch
    # by the time the deletion plan asks: that is what lets it go without
    # --force and what marks origin's copy spent.
    if head_ref:
        cleanup_merged_branch(head_ref)

    result = MergeResult(
        pr_number=pr_number,
        merged=True,
        integration_branch=integration,
        pulled=pulled,
    )
    output_result(result, as_json)


def sync_base(
    base: str | None,
    as_json: bool,
) -> None:
    """Sync the base branch and merge it into the current feature branch.

    A base only topology could name is reported and not merged. Guessing wrong
    picks a branch the feature never diverged from, and every later step reads
    that answer as settled — the history rebuild resets onto it, so a wrong
    guess rewrites whatever sits between the two branches. An authoritative
    base is one the caller passed or worktree creation recorded.

    A base that could not be refreshed is merged anyway and said so in the
    result, rather than refused. The merge is still the one the caller asked
    for and is still correct against the base as it stands here; what changes
    is only whether the base was current, which is a fact about the answer
    rather than a reason to withhold it. It reaches the caller as a field
    because a warning on stderr does not reach one reading the JSON.
    """
    feature = current_branch()
    base_source: Literal["explicit", "created", "recorded", "guessed"] = "explicit"
    if base:
        base_branch = base
    else:
        detected = find_base_branch()
        base_branch = detected.name
        base_source = detected.source

    if not as_json:
        typer.echo(f"Feature branch: {feature}", err=True)
        typer.echo(f"Base branch: {base_branch}", err=True)

    if base_source == "guessed":
        if not as_json:
            typer.echo(
                f"Topology alone picked {base_branch}, so nothing is merged."
                f" Confirm the base, then rerun with --base <branch>.",
                err=True,
            )
        output_result(
            SyncBaseResult(
                feature_branch=feature,
                base_branch=base_branch,
                base_source=base_source,
                merged=False,
                conflicts=[],
                base_synced=False,
                sync_complaint="the base was never identified, so none was fetched",
            ),
            as_json,
        )
        raise typer.Exit(1)

    base_path = parse_worktrees().get(base_branch)

    synced = False
    complaint = f"no worktree for {base_branch}, so it was merged as it stands"
    if base_path and Path(base_path).is_dir():
        if not as_json:
            typer.echo(f"Syncing {base_branch}...", err=True)
        try:
            git("-C", str(base_path), "pull", "--ff-only")
            git("-C", str(base_path), "push")
            synced, complaint = True, ""
        except sh.ErrorReturnCode as e:
            complaint = decode_stderr(e)
            typer.echo(f"Warning: sync of {base_branch} failed: {complaint}", err=True)

    if not as_json:
        typer.echo(f"Merging {base_branch} into {feature}...", err=True)

    try:
        git("merge", base_branch)
        result = SyncBaseResult(
            feature_branch=feature,
            base_branch=base_branch,
            base_source=base_source,
            merged=True,
            conflicts=[],
            base_synced=synced,
            sync_complaint=complaint,
        )
    except sh.ErrorReturnCode:
        conflicts = [str(path) for path in Repository(Path.cwd()).conflicted()]
        result = SyncBaseResult(
            feature_branch=feature,
            base_branch=base_branch,
            base_source=base_source,
            merged=False,
            conflicts=conflicts,
            base_synced=synced,
            sync_complaint=complaint,
        )
        if not as_json:
            typer.echo(f"Merge conflicts in {len(conflicts)} file(s):", err=True)
            for f in conflicts:
                typer.echo(f"  {f}", err=True)

    output_result(result, as_json)
    if not result.merged:
        raise typer.Exit(1)


def push(
    force: bool,
    as_json: bool,
    protected: list[str],
) -> None:
    """Push the current branch and report any existing PR.

    A forced push is a leased one. `--force-with-lease` replaces only what
    this checkout last saw of the branch, and `--force-if-includes` refuses
    where that sight came from a fetch rather than from this checkout's own
    history -- the case a lease alone waves through, overwriting a push it
    fetched and never built on. What this cannot see is whether the branch
    is one other people build on, and the shell policy asks before forcing
    one of ``protected``; so this refuses them, and names the command whose
    question reaches the user.

    Both spellings state the destination as a full refspec, because a
    checkout reaches this with no upstream: creation publishes nothing, so
    the first push of either kind is what gives the branch a remote. A bare
    `push --force` would have nothing to resolve the destination from, and
    where it did resolve one it took whatever `push.default` offered.

    Stated rather than asked for with `-u`, which records the relationship by
    writing `branch.<name>.remote` and `branch.<name>.merge` into the shared
    config. Where that file cannot be written the flag fails *mendaciously*:
    the push happens, git prints `set up to track`, and the command exits 0
    having recorded nothing — so every reader of the tracking configuration
    sees a branch that was never published. The refspec needs no such write,
    and what the flag was for is recorded beside the branch's other facts.
    """
    branch_name = current_branch()
    destination = f"refs/heads/{branch_name}:refs/heads/{branch_name}"
    if force and branch_name in protected:
        typer.echo(
            f"Refusing to force {branch_name}: other people build on it. "
            f"`git push --force-with-lease origin {branch_name}` puts the "
            "question to the user.",
            err=True,
        )
        raise typer.Exit(1)

    complaint = ""
    pushed = False
    try:
        forced = ["--force-with-lease", "--force-if-includes"] if force else []
        git("push", *forced, "origin", destination)
        pushed = True
        records.remember(
            branch_name, records.BranchRecord(upstream=f"origin/{branch_name}")
        )
    except sh.ErrorReturnCode as e:
        complaint = decode_stderr(e) or f"git push exited with status {e.exit_code}"
        typer.echo(f"Push failed: {complaint}", err=True)

    existing_pr = None
    try:
        pr_raw = gh.out(
            "pr",
            "list",
            *repository_arguments(),
            "--head",
            branch_name,
            "--state",
            "open",
            "--json",
            "number,url",
        )
        rows = json.loads(pr_raw) if pr_raw else []
        prs = [GhPrRef.model_validate(row) for row in rows]
        if prs:
            existing_pr = ExistingPR(number=prs[0].number, url=prs[0].url)
    except sh.ErrorReturnCode:
        pass

    result = PushResult(
        branch=branch_name,
        pushed=pushed,
        force=force,
        existing_pr=existing_pr,
        push_complaint=complaint,
        upstream=records.recorded_upstream(branch_name),
    )
    output_result(result, as_json)
    if not pushed:
        raise typer.Exit(1)


class HeadOnRemote(StrEnum):
    """Whether the remote carries the branch a request would be opened over."""

    present = "present"
    absent = "absent"
    unknown = "unknown"


def head_on_remote(branch: str, remote: str = "origin") -> HeadOnRemote:
    """Ask the remote whether it holds this branch, rather than assume it does.

    The remote is asked rather than the remote-tracking ref read, because a
    push that failed leaves that ref exactly as a fetch nobody ran does, and
    the whole question here is which of those happened.
    """
    try:
        listed = git.out("ls-remote", "--heads", remote, branch).strip()
    except sh.ErrorReturnCode:
        logger.exception("could not ask %s whether it holds %s", remote, branch)
        return HeadOnRemote.unknown
    return HeadOnRemote.present if listed else HeadOnRemote.absent


def creation_diagnosis(branch: str, base: str, complaint: str) -> str:
    """What "no commits between" is saying, which is one of two things.

    GitHub refuses a request over a branch the remote never received with
    the same sentence it refuses one holding nothing its base lacks, and the
    two are opposite: the first is a push to retry, and the sentence points
    away from it by describing a branch with nothing to merge. Only the
    remote separates them, so it is asked and the answer said out loud.

    Empty for every other refusal, which says what it means already.
    """
    if "No commits between" not in complaint:
        return ""
    match head_on_remote(branch):
        case HeadOnRemote.absent:
            return (
                f"The remote has no {branch}. Nothing is wrong with the request:"
                f" the push that would have put the branch there did not land."
                f" Push it again and retry."
            )
        case HeadOnRemote.present:
            return (
                f"The remote's {branch} holds nothing {base} lacks, so there is"
                f" no change for a request to describe."
            )
        case HeadOnRemote.unknown:
            return (
                f"Whether the remote holds {branch} could not be established,"
                f" and that is what separates a branch that never arrived from"
                f" one identical to {base}."
            )


def parse_pr_url(stdout: str) -> str:
    """Extract the PR URL from ``gh pr create`` stdout (last URL-like line)."""
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if urlparse(candidate).scheme in ("http", "https"):
            return candidate
    return ""


def resolve_body(body: str | None, body_file: Path | None) -> str:
    """The body text, from whichever of the two ways of giving it was used.

    A PR body is prose long enough to want headings, code spans and lists,
    and whoever composes one has usually just written it to a file. Reading
    that file here keeps the text out of an argument list, where quoting is
    the caller's problem: a body spliced through a shell needs every
    apostrophe in its prose escaped by hand, and one missed truncates the
    document into a parse error that names an offset rather than the body.
    """
    match (body, body_file):
        case (None, None):
            raise typer.BadParameter("pass --body or --body-file")
        case (str(), Path()):
            raise typer.BadParameter("pass --body or --body-file, not both")
        case (None, Path() as path):
            try:
                return path.read_text(encoding="utf-8")
            except OSError as e:
                raise typer.BadParameter(f"cannot read {path}: {e}")
        case (str() as text, None):
            return text


def create(
    base: str,
    title: str,
    body: str,
    as_json: bool,
) -> None:
    """Create a new PR.

    ``gh pr create`` has no ``--json`` flag — on success it prints the new
    PR's URL to stdout. The PR number is the final path segment of that URL.

    The head branch is named rather than left to inference. Naming the
    repository is what stops the remote-URL inference an alias defeats, and
    once the repository is given the checkout no longer says which branch the
    request comes from — so the two go together.

    Gated on the forge client's own credential rather than on the remote's,
    because this is the step where the two part company: the push that got
    here needed a transport, and this needs an API nothing has asked about.
    """
    if not check_forge_api():
        raise typer.Exit(1)
    head = current_branch()
    try:
        raw = gh.out(
            "pr",
            "create",
            *repository_arguments(),
            "--base",
            base,
            "--head",
            head,
            "--title",
            title,
            "--body",
            body,
        )
    except sh.ErrorReturnCode as e:
        complaint = decode_stderr(e)
        typer.echo(f"Failed to create PR: {complaint}", err=True)
        if diagnosis := creation_diagnosis(head, base, complaint):
            typer.echo(diagnosis, err=True)
        raise typer.Exit(1)

    url = parse_pr_url(raw)
    if not url:
        typer.echo(f"PR created but URL not found in output:\n{raw}", err=True)
        raise typer.Exit(1)

    number_segment = PurePosixPath(urlparse(url).path).name
    if not number_segment.isdigit():
        typer.echo(f"PR created at {url} but could not parse number", err=True)
        raise typer.Exit(1)

    result = CreateResult(number=int(number_segment), url=url)
    output_result(result, as_json)


def update(
    pr_number: int,
    body: str,
) -> None:
    """Update a PR body, on the same API credential creating one needs."""
    if not check_forge_api():
        raise typer.Exit(1)
    try:
        gh("pr", "edit", str(pr_number), *repository_arguments(), "--body", body)
        typer.echo(f"Updated PR #{pr_number}")
    except sh.ErrorReturnCode as e:
        typer.echo(f"Failed to update PR: {decode_stderr(e)}", err=True)
        raise typer.Exit(1)
