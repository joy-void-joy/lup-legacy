"""Which branches a running resolver is still holding.

A lease is indistinguishable from abandoned work by every signal a branch
survey reads — commits the integration branch lacks, no pull request driving
them — so a sweep offers to land it individually or drop it, and both answers
destroy something. The run directory is the only thing that can tell them
apart, and these pin that it is asked and believed.
"""

from pathlib import Path

import pytest

from lup.devtools.dev.branches import (
    PRStatus,
    WorktreeChanges,
    disposition_for,
    still_at_reservation,
)
from lup.devtools.dev.worktree import RecordedBase
from lup.devtools.report.build import lease_items
from lup.harness.models import ResolveSpec, SkillInvocation
from lup.execution.process import LaunchRequest, LocalProcessLauncher
from lup.resolver.models import (
    AcceptanceCriterion,
    Concern,
    ConcernProgress,
    ConcernStatus,
    ResolvePhase,
    ResolveState,
    SourceSnapshot,
    WritableRootLease,
)
from lup.resolver.state import ResolverStateRepository, live_lease_branches


def run_state(
    run_id: str, phase: ResolvePhase, leases: list[WritableRootLease]
) -> ResolveState:
    concerns = [
        Concern(
            id=lease.concern_id,
            title=lease.concern_id,
            spec=f"Resolve {lease.concern_id}",
            criteria=[AcceptanceCriterion(id="c1", description="done")],
        )
        for lease in leases
    ]
    return ResolveState(
        config_digest="digest",
        run_id=run_id,
        phase=phase,
        source=SourceSnapshot(branch="dev", commit="a" * 40),
        spec=ResolveSpec(
            id="resolve",
            worker_identity="resolver-worker",
            worker_skill=SkillInvocation(plugin="lup", skill="worker"),
            review_skill=SkillInvocation(plugin="lup", skill="review"),
            merge_skill=SkillInvocation(plugin="lup", skill="merge"),
        ),
        concerns=concerns,
        progress=[
            ConcernProgress(
                concern_id=concern.id, status=ConcernStatus.WAITING_FOR_ANSWERS
            )
            for concern in concerns
        ],
        leases=leases,
    )


def lease(concern_id: str, root: Path, active: bool = True) -> WritableRootLease:
    return WritableRootLease(
        concern_id=concern_id,
        root=root / concern_id,
        branch=f"resolve/run-1/{concern_id}",
        active=active,
    )


def test_a_parked_run_still_holds_its_leases(tmp_path: Path) -> None:
    state_root = tmp_path / ".lup" / "resolve"
    ResolverStateRepository(state_root, "run-1").save(
        run_state("run-1", ResolvePhase.WORKERS, [lease("alpha", tmp_path / "leases")])
    )

    held = live_lease_branches(state_root)

    assert list(held) == ["resolve/run-1/alpha"]
    assert held["resolve/run-1/alpha"].run_id == "run-1"
    assert (
        held["resolve/run-1/alpha"].reason()
        == "lease of run run-1 (waiting_for_answers)"
    )


def test_a_finished_run_still_reports_what_it_left_behind(tmp_path: Path) -> None:
    """Completion releases the lease without disposing of the branch.

    Cleanup deactivates every lease whether or not it managed to delete the
    branch it named, so a survivor reads as loose work carrying the whole
    batch's commits. A sweep meeting it that way offers to land a batch that
    may already have gone in under another branch's pull request.
    """
    state_root = tmp_path / ".lup" / "resolve"
    ResolverStateRepository(state_root, "run-1").save(
        run_state(
            "run-1",
            ResolvePhase.COMPLETE,
            [lease("alpha", tmp_path / "leases", active=False)],
        )
    )

    held = live_lease_branches(state_root)

    assert list(held) == ["resolve/run-1/alpha"]
    assert held["resolve/run-1/alpha"].alive is False
    assert "resolve status --run-id run-1" in held["resolve/run-1/alpha"].reason()


def test_a_released_lease_is_not_held(tmp_path: Path) -> None:
    state_root = tmp_path / ".lup" / "resolve"
    ResolverStateRepository(state_root, "run-1").save(
        run_state(
            "run-1",
            ResolvePhase.WORKERS,
            [lease("alpha", tmp_path / "leases", active=False)],
        )
    )

    assert live_lease_branches(state_root) == {}


def test_no_resolver_directory_holds_nothing(tmp_path: Path) -> None:
    assert live_lease_branches(tmp_path / "nowhere") == {}


def test_a_run_whose_state_cannot_be_read_does_not_break_the_survey(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / ".lup" / "resolve"
    (state_root / "run-1").mkdir(parents=True)
    (state_root / "run-1" / "state.json").write_text("{ not json", encoding="utf-8")

    assert live_lease_branches(state_root) == {}


def test_a_held_branch_is_kept_rather_than_offered_for_landing() -> None:
    # The whole point: by every other signal this reads as abandoned work.
    verdict = disposition_for(
        "resolve/run-1/alpha",
        integration="dev",
        current="dev",
        contained_in=[],
        pr=None,
        unique_commits=3,
        held="lease of run run-1 (waiting_for_answers)",
    )

    assert verdict.status == "KEEP"
    assert verdict.reason == "lease of run run-1 (waiting_for_answers)"


def test_an_unheld_branch_with_the_same_shape_is_still_landable() -> None:
    verdict = disposition_for(
        "some-feature",
        integration="dev",
        current="dev",
        contained_in=[],
        pr=None,
        unique_commits=3,
    )

    assert verdict.status == "LAND"


def test_only_an_open_request_can_be_closed_a_second_time() -> None:
    """A retirement closes the request it reuses, and two states refuse that.

    Reusing whichever was most recent pushed the branch and then failed at
    the close, leaving the work half-moved: a branch that merged and then
    gained commits has a MERGED request against its head, and GitHub refuses
    that transition outright.
    """
    assert PRStatus(number=1, state="OPEN").reusable()
    assert not PRStatus(number=2, state="MERGED").reusable()
    assert not PRStatus(number=3, state="CLOSED").reusable()


def test_a_branch_sharing_no_history_is_not_offered_for_landing() -> None:
    """Both verbs a sweep offers a LAND branch would replay an unrelated tree.

    Neither counter fails without a merge base, so the figures come back
    plausible rather than absent: an adopter repo's survey read 17390 lines
    of "divergence" for branches whose real relationship to the integration
    branch was none.
    """
    verdict = disposition_for(
        "fold-other-library",
        integration="dev",
        current="dev",
        contained_in=[],
        pr=None,
        unique_commits=1,
        related=False,
    )

    assert verdict.status == "UNRELATED"
    assert verdict.reason == "shares no history with dev"


def test_a_reserved_worktree_is_not_spent_work() -> None:
    """The workflow says to create a worktree first and commit into it after.

    Between those two moments the branch has diverged by nothing, and
    containment alone reads that as merged — so a sweep offered to delete
    the workspace the documented workflow had just told the user to make.
    """
    verdict = disposition_for(
        "feat-not-started",
        integration="dev",
        current="dev",
        contained_in=["dev"],
        pr=None,
        unique_commits=0,
        reserved=True,
        worktree="/tree/feat-not-started",
    )

    assert verdict.status == "KEEP"
    assert verdict.reason == "reserved workspace cut from dev"


def test_a_reserved_worktree_holding_work_is_not_somebody_s_next_session() -> None:
    """Reserving a workspace claims nobody has started; a dirty tree denies it.

    Work left uncommitted sits in no commit, on no branch and on no remote,
    so the sweep is the only thing that can mention it — and a verb reading
    "leave it for the next session" is how it goes stale on a base that keeps
    trailing, with nothing anywhere to recover it from.
    """
    verdict = disposition_for(
        "feat-worked-in",
        integration="dev",
        current="dev",
        contained_in=["dev"],
        pr=None,
        unique_commits=0,
        reserved=True,
        worktree="/tree/feat-worked-in",
        changes=WorktreeChanges(modified=3, untracked=0),
    )

    assert verdict.status == "COMMIT"
    assert (
        verdict.reason
        == "reserved workspace cut from dev, holding 3 modified, 0 untracked"
    )


def test_a_reserved_worktree_with_a_clean_tree_is_still_left_alone() -> None:
    """The guard's own case, which reading the dirt must not eat.

    Creating a worktree and committing into it are two moments, and between
    them the tree is clean and the branch has diverged by nothing. That is
    the workspace the documented workflow just told the user to make.
    """
    verdict = disposition_for(
        "feat-not-started",
        integration="dev",
        current="dev",
        contained_in=["dev"],
        pr=None,
        unique_commits=0,
        reserved=True,
        worktree="/tree/feat-not-started",
        changes=WorktreeChanges(modified=0, untracked=0),
    )

    assert verdict.status == "KEEP"


def test_dirt_does_not_move_a_branch_that_already_landed() -> None:
    """Everywhere but a reserved workspace, dirt prices the action.

    A merged branch whose worktree is dirty is still spent: the delete
    refuses until forced rather than becoming a different verb, so reading
    the dirt must not reach past the one guard it was added for.
    """
    verdict = disposition_for(
        "feat-landed",
        integration="dev",
        current="dev",
        contained_in=["dev"],
        pr=None,
        unique_commits=0,
        worktree="/tree/feat-landed",
        changes=WorktreeChanges(modified=5, untracked=2),
    )

    assert verdict.status == "DELETE"


def test_a_merged_branch_still_holding_a_worktree_is_spent() -> None:
    """The ordinary cleanup path, and the case the guard above must not eat.

    A branch that landed diverged and had what it diverged by taken in, so
    its tip is the side parent a merge absorbed rather than a commit standing
    on the integration branch's own history. That is the discriminator, and
    it keeps answering however far that branch travels afterwards.
    """
    verdict = disposition_for(
        "feat-landed",
        integration="dev",
        current="dev",
        contained_in=["dev"],
        pr=None,
        unique_commits=0,
        reserved=False,
        worktree="/tree/feat-landed",
    )

    assert verdict.status == "DELETE"
    assert verdict.reason == "merged into dev"


def build_history(root: Path, launcher: LocalProcessLauncher) -> Path:
    """A repository holding one reserved branch and one whose work landed.

    ``feat-reserved`` is cut from ``dev`` and never committed to, and carries
    the record ``worktree create`` writes when it reserves the workspace.
    ``feat-landed`` diverges and is merged, moving ``dev`` past both.
    ``feat-fastforwarded`` carries the same record, is committed to, and is
    then taken into ``dev`` by fast-forward — so its tip *is* ``dev``'s tip,
    which is exactly where an untouched workspace's tip stands, and the graph
    is left holding nothing that separates the two.
    """
    work = root / "work"
    # Identity per invocation, never `git config` — a persisted setting lands
    # in the shared config every worktree of a real repository inherits.
    who = ("-c", "user.email=branches@example.test", "-c", "user.name=Branch Test")
    git_in = ("git", "-C", str(work))

    def run(*arguments: str) -> str:
        status = launcher.launch(LaunchRequest(arguments=list(arguments), cwd=root))
        if status.code != 0:
            raise AssertionError(status.stderr)
        return status.stdout.strip()

    def reserve(branch: str) -> None:
        """Cut a branch and record where it stood, in the config keys a clone
        whose records were never adopted still carries.
        """
        run(*git_in, "branch", branch)
        run(*git_in, "config", f"branch.{branch}.lup-base", "dev")
        run(
            *git_in,
            "config",
            f"branch.{branch}.lup-base-commit",
            run(*git_in, "rev-parse", branch),
        )

    run("git", "init", "-b", "dev", str(work))
    run(*git_in, *who, "commit", "--allow-empty", "-m", "base")

    reserve("feat-reserved")

    run(*git_in, "checkout", "-b", "feat-landed")
    run(*git_in, *who, "commit", "--allow-empty", "-m", "work")
    run(*git_in, "checkout", "dev")
    run(*git_in, *who, "merge", "--no-ff", "feat-landed", "-m", "merge")

    reserve("feat-fastforwarded")
    run(*git_in, "checkout", "feat-fastforwarded")
    run(*git_in, *who, "commit", "--allow-empty", "-m", "work landed by fast-forward")
    run(*git_in, "checkout", "dev")
    run(*git_in, "merge", "--ff-only", "feat-fastforwarded")
    return work


def test_a_reservation_outlives_the_integration_branch_moving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case a sweep creates for itself, and the reason distance cannot answer.

    Every merge a sweep performs moves the integration branch out from under
    every workspace reserved against it. Reading the reserved branch as spent
    the moment that happens offers to delete a workspace somebody is holding
    — and a sweep merges before it proposes deletions, so the window where a
    tip comparison is right had closed before anyone was asked. Comparing
    against the recorded commit is unmoved by any of it.
    """
    work = build_history(tmp_path, LocalProcessLauncher())
    monkeypatch.chdir(work)

    assert still_at_reservation("feat-reserved")
    assert not still_at_reservation("feat-landed")


def test_work_landed_by_fast_forward_is_not_a_reserved_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The landing no topological question can see, and the whole reason for a record.

    A branch whose work landed through a merge sits on the side parent, off
    the integration branch's first-parent history, which is all a tip
    comparison reads. Rebased and fast-forwarded, the same work leaves
    the branch pointing at the integration branch's own tip — where an
    untouched workspace points too. The two states are then the same graph,
    so the branch is spent and only something written before the work
    existed can still say so.
    """
    work = build_history(tmp_path, LocalProcessLauncher())
    monkeypatch.chdir(work)

    assert not still_at_reservation("feat-fastforwarded")


def test_a_branch_nobody_reserved_is_not_read_as_a_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reserving writes the record, so its absence is not a held session.

    A branch made another way, or made before the record existed, has
    nothing claiming somebody is holding it — while a spent branch read as
    reserved is one nothing offers to clear ever again.
    """
    work = build_history(tmp_path, LocalProcessLauncher())
    monkeypatch.chdir(work)

    assert not still_at_reservation("dev")


def test_re_attaching_a_worktree_does_not_reserve_the_work_already_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record asserts nobody has worked here, so re-attaching cannot write it.

    ``worktree create`` also re-attaches a branch that already exists, and
    records a base wherever one is missing. Recording the tip there would take
    whatever had been committed as the place nothing was committed — reserving
    a branch that holds unlanded work, which then reads as a workspace somebody
    is holding and is never offered for landing again. Work at risk, hidden
    behind a verb meaning the opposite, is the one direction this must not fail
    in.
    """
    work = build_history(tmp_path, LocalProcessLauncher())
    monkeypatch.chdir(work)
    commit_as = (
        "-c",
        "user.email=branches@example.test",
        "-c",
        "user.name=Branch Test",
    )
    for arguments in (
        ["git", "-C", str(work), "checkout", "-b", "feat-already-worked", "dev"],
        ["git", "-C", str(work), *commit_as, "commit", "--allow-empty", "-m", "work"],
    ):
        status = LocalProcessLauncher().launch(
            LaunchRequest(arguments=arguments, cwd=work)
        )
        if status.code != 0:
            raise AssertionError(status.stderr)

    RecordedBase(branch="feat-already-worked", origin="dev", cut_fresh=False).run()

    assert not still_at_reservation("feat-already-worked")


def test_a_branch_this_run_cut_carries_the_record_it_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: cutting the branch is what earns the commit record."""
    work = build_history(tmp_path, LocalProcessLauncher())
    monkeypatch.chdir(work)
    status = LocalProcessLauncher().launch(
        LaunchRequest(
            arguments=["git", "-C", str(work), "branch", "feat-cut", "dev"], cwd=work
        )
    )
    if status.code != 0:
        raise AssertionError(status.stderr)

    RecordedBase(branch="feat-cut", origin="dev", cut_fresh=True).run()

    assert still_at_reservation("feat-cut")


def test_an_undiverged_pointer_with_no_worktree_is_still_spent() -> None:
    """Nothing is reserved, so there is nothing the delete would take away."""
    verdict = disposition_for(
        "feat-abandoned",
        integration="dev",
        current="dev",
        contained_in=["dev"],
        pr=None,
        unique_commits=0,
        reserved=True,
        worktree=None,
    )

    assert verdict.status == "DELETE"


def test_a_leftover_whose_branch_the_run_deleted_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record names every branch a run ever leased; the tree names fewer.

    Cleanup deletes what it can and deactivates every lease regardless, so a
    completed run's record still carries branches that are gone. The survey
    meets a lease through ``refs/heads`` and so never saw them; a report that
    read the record alone listed fifty leases for two runs whose batches had
    already landed, and called them outstanding.
    """
    work = tmp_path / "work"
    who = ("-c", "user.email=leases@example.test", "-c", "user.name=Lease Test")
    git_in = ("git", "-C", str(work))
    launcher = LocalProcessLauncher()
    for arguments in (
        ["git", "init", "-b", "dev", str(work)],
        [*git_in, *who, "commit", "--allow-empty", "-m", "base"],
        [*git_in, "branch", "resolve/run-1/alpha"],
    ):
        status = launcher.launch(LaunchRequest(arguments=arguments, cwd=tmp_path))
        if status.code != 0:
            raise AssertionError(status.stderr)
    monkeypatch.chdir(work)
    state_root = tmp_path / ".lup" / "resolve"
    ResolverStateRepository(state_root, "run-1").save(
        run_state(
            "run-1",
            ResolvePhase.COMPLETE,
            [
                lease("alpha", tmp_path / "leases", active=False),
                lease("beta", tmp_path / "leases", active=False),
            ],
        )
    )

    assert [item.where for item in lease_items(state_root)] == ["resolve/run-1/alpha"]
