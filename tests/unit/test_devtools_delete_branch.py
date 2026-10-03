"""Behavior tests for `lup-devtools dev delete`.

Deletion is preflighted: every precondition is evaluated before anything is
touched, so a dry run reports what the real path would go on to check, and a run
that cannot finish changes nothing. These pin both halves — that a blocked
step is named as blocked rather than annotated with the flag that was passed,
that a refusal leaves the checkout standing without having attempted its
removal, and that a worktree stranded by an earlier failure is pruned rather
than left for the caller to discover.
"""

import shutil
from pathlib import Path

import pytest
import sh
import typer

from lup.devtools.dev import branches
from lup.diagnostics import Refusal
from tests.unit.repos import commit_file, initialized_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    git("worktree", "add", str(tmp_path / "feature"), "-b", "feature")
    (tmp_path / "feature" / "file.txt").write_text("dirty\n", encoding="utf-8")
    return work


@pytest.fixture
def pushed(tmp_path: Path) -> Path:
    """A repo with an origin holding both a merged and an unmerged branch."""
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    sh.Command("git")("init", "--bare", "-q", str(work.parent / "origin.git"))
    git("remote", "add", "origin", str(work.parent / "origin.git"))
    git("branch", "spent")
    git("checkout", "-q", "-b", "solo")
    commit_file(git, work, "extra.txt", "extra\n", "feat: extra")
    git("checkout", "-q", "main")
    git("push", "-q", "origin", "main", "spent", "solo")
    return work


def remote_branch_names(work: Path) -> list[str]:
    out = sh.Command("git")(
        "-C",
        str(work.parent / "origin.git"),
        "branch",
        "--format=%(refname:short)",
        _tty_out=False,
    )
    return str(out).split()


def branch_names(work: Path) -> list[str]:
    out = sh.Command("git")(
        "-C", str(work), "branch", "--format=%(refname:short)", _tty_out=False
    )
    return str(out).split()


def test_force_removes_a_worktree_holding_changes(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    branches.delete_branch("feature", dry_run=False, force=True)

    assert "feature" not in branch_names(repo)
    assert not (repo.parent / "feature").exists()


def test_a_locked_worktree_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The claim a clean checkout cannot make for itself.

    Dirt says a worktree holds work. A lock says somebody is using it, which
    is the part no inspection of the tree recovers: a session that has just
    committed looks exactly like an abandoned one.
    """
    sh.Command("git")("-C", str(repo), "worktree", "lock", str(repo.parent / "feature"))
    monkeypatch.chdir(repo)

    with pytest.raises(typer.Exit):
        branches.delete_branch("feature", dry_run=False, force=False)

    assert (repo.parent / "feature").exists()
    assert "feature" in branch_names(repo)
    assert "refused" in capsys.readouterr().err


def test_forcing_does_not_lift_a_lock(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one verdict no flag answers, on the tree where force answers the rest.

    This worktree is dirty as well, which `--force` does resolve. It cannot
    resolve the lock: git refuses a locked checkout to every force this
    command passes, so the step stays refused, and the offer to force is
    withheld because there is nothing left for it to lift.
    """
    sh.Command("git")("-C", str(repo), "worktree", "lock", str(repo.parent / "feature"))
    monkeypatch.chdir(repo)

    with pytest.raises(Refusal) as refused:
        branches.delete_branch("feature", dry_run=False, force=True)

    assert (repo.parent / "feature").exists()
    assert "feature" in branch_names(repo)

    # Refused before the removal rather than by git during it: forcing past
    # the dirt carries the run as far as the destructive step, where the lock
    # stops it with the branch half-judged.
    err = capsys.readouterr().err
    assert refused.value.said["what"] == "feature"
    assert "nothing was deleted" in refused.value.said["why"]
    assert "worktree removal failed" not in err
    assert refused.value.said["steps"] == []


def test_the_lock_reason_reaches_the_refusal(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whoever locked it said why, and that is the whole of what a reader needs."""
    sh.Command("git")(
        "-C",
        str(repo),
        "worktree",
        "lock",
        "--reason",
        "session 7 is working here",
        str(repo.parent / "feature"),
    )
    monkeypatch.chdir(repo)

    with pytest.raises(typer.Exit):
        branches.delete_branch("feature", dry_run=False, force=False)

    assert "session 7 is working here" in capsys.readouterr().err


def test_without_force_a_dirty_worktree_survives(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)
    with pytest.raises(Refusal) as refused:
        branches.delete_branch("feature", dry_run=False, force=False)

    assert "feature" in branch_names(repo)
    survivor = repo.parent / "feature" / "file.txt"
    assert survivor.read_text(encoding="utf-8") == "dirty\n"

    # The refusal precedes the removal rather than reporting it after the fact:
    # the destructive step is never attempted, so there is nothing to warn about.
    err = capsys.readouterr().err
    assert refused.value.said["what"] == "feature"
    assert "nothing was deleted" in refused.value.said["why"]
    assert "worktree removal failed" not in err


def test_dry_run_reports_a_dirty_worktree_as_blocked(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)
    branches.delete_branch("feature", dry_run=True, force=False)

    out = capsys.readouterr().out
    assert "Remove worktree" in out
    assert "blocked: holds 1 modified, 0 untracked" in out
    assert "feature" in branch_names(repo)


def test_dry_run_under_force_says_what_it_would_discard(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)
    branches.delete_branch("feature", dry_run=True, force=True)

    assert "force: discards 1 modified, 0 untracked" in capsys.readouterr().out
    assert "feature" in branch_names(repo)


def test_dry_run_reports_an_unmerged_branch_as_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    git("checkout", "-b", "solo")
    commit_file(git, work, "extra.txt", "extra\n", "feat: extra")
    git("checkout", "main")
    monkeypatch.chdir(work)

    branches.delete_branch("solo", dry_run=True, force=False)

    assert "blocked: branch is unmerged" in capsys.readouterr().out
    assert "solo" in branch_names(work)


def test_a_branch_whose_commits_were_cherry_picked_deletes_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reading a sweep needs, which ancestry answers wrongly.

    Work that landed through a rebase or a cherry-pick is in the integration
    branch by content and reachable from it by no commit, so
    `merge-base --is-ancestor` calls the branch unmerged and a plain delete
    refused every branch a sweep had just landed. Containment is patch-id
    here, as it is everywhere else this module decides the same question.
    """
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    git("checkout", "-q", "-b", "rebased")
    commit_file(git, work, "extra.txt", "extra\n", "feat: extra")
    git("checkout", "-q", "main")
    git("cherry-pick", "rebased")
    monkeypatch.chdir(work)

    branches.delete_branch("rebased", dry_run=False, force=False)

    assert "rebased" not in branch_names(work)


def test_a_refused_delete_names_the_commits_that_look_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """What the reader needs next, at the moment the refusal is read.

    A subject the integration branch also carries is the trace a rebase
    leaves, and it is a signal rather than proof — so the refusal names how
    many there are and the command that settles it, and goes on refusing.
    """
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    git("checkout", "-q", "-b", "reworded")
    commit_file(git, work, "extra.txt", "extra\n", "feat: extra")
    git("checkout", "-q", "main")
    commit_file(git, work, "extra.txt", "different\n", "feat: extra")
    monkeypatch.chdir(work)

    branches.delete_branch("reworded", dry_run=True, force=False)

    said = capsys.readouterr().out
    assert "1 of 1 unique commit(s) share a subject with main" in said
    assert "git cherry -v main reworded" in said
    assert "reworded" in branch_names(work)


def test_a_merged_branch_takes_origin_s_copy_with_it(
    pushed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy is spent: its commits are in the branch that absorbed it."""
    monkeypatch.chdir(pushed)
    branches.delete_branch("spent", dry_run=False, force=False)

    assert "spent" not in branch_names(pushed)
    assert "spent" not in remote_branch_names(pushed)


def test_an_unmerged_branch_leaves_origin_s_copy_standing(
    pushed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pushing before deleting is what preserves the work; this took it back.

    The local branch is the one being retired, and origin's copy is the
    reason retiring it is survivable. Deleting both because both existed
    made a push-then-clean sequence destroy exactly what the push was for.
    """
    monkeypatch.chdir(pushed)
    branches.delete_branch("solo", dry_run=False, force=True)

    assert "solo" not in branch_names(pushed)
    assert "solo" in remote_branch_names(pushed)
    assert "Kept remote branch: origin/solo" in capsys.readouterr().out


def test_a_branch_another_pr_is_based_on_is_blocked(
    pushed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stacked PR names its parent as its base, and GitHub closes it.

    The parent is spent exactly when its own PR merged, which is when this
    command is called on it — so the ordinary end of the parent's life
    closes the child, and the child's branch survives, leaving a closure
    that reads as work lost.
    """
    monkeypatch.chdir(pushed)
    monkeypatch.setattr(branches, "dependent_pulls", lambda name: [161])

    branches.delete_branch("spent", dry_run=True, force=False)

    assert "#161 targets this branch and would be closed" in capsys.readouterr().out
    assert "spent" in remote_branch_names(pushed)


def test_forcing_past_a_dependent_says_which_pr_it_closes(
    pushed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(pushed)
    monkeypatch.setattr(branches, "dependent_pulls", lambda name: [161, 162])

    branches.delete_branch("spent", dry_run=True, force=True)

    assert "force: closes #161, #162" in capsys.readouterr().out


def test_origin_s_copy_of_unmerged_work_goes_only_when_named(
    pushed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Asked for in so many words, and told what it costs."""
    monkeypatch.chdir(pushed)
    branches.delete_branch("solo", dry_run=False, force=True, remote=True)

    assert "solo" not in remote_branch_names(pushed)
    assert "the work is in no branch" in capsys.readouterr().err


def test_work_a_caller_already_parked_is_not_mourned(
    pushed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`dev retire` deletes only after a closed request holds the commits.

    Warning there says the work is in no branch immediately after the step
    that put it in one, and tells the operator to run the command they are
    already inside — so the reader has to go and check whether the retirement
    they just watched succeed actually preserved anything.
    """
    monkeypatch.chdir(pushed)
    branches.delete_branch(
        "solo", dry_run=False, force=True, remote=True, preserved="refs/pull/7/head"
    )

    assert "solo" not in remote_branch_names(pushed)
    assert "the work is in no branch" not in capsys.readouterr().err


def test_a_merged_branch_can_still_keep_its_remote(
    pushed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(pushed)
    branches.delete_branch("spent", dry_run=False, force=False, remote=False)

    assert "spent" not in branch_names(pushed)
    assert "spent" in remote_branch_names(pushed)


@pytest.fixture
def outgrown(tmp_path: Path) -> Path:
    """A tracking branch whose work reached main by a merge, not by a push.

    What a worktree looks like at the end of its life: created with an
    upstream, committed to, and landed by merging into the integration
    branch. The branch is then ahead of the remote copy it tracks, which is
    the state `git branch -d` refuses however thoroughly HEAD contains it.
    """
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    sh.Command("git")("init", "--bare", "-q", str(work.parent / "origin.git"))
    git("remote", "add", "origin", str(work.parent / "origin.git"))
    git("push", "-q", "origin", "main")

    git("checkout", "-q", "-b", "topic")
    git("push", "-q", "-u", "origin", "topic")
    commit_file(git, work, "extra.txt", "extra\n", "feat: extra")
    git("checkout", "-q", "main")
    git("merge", "--no-edit", "-q", "topic")
    return work


def test_the_plain_delete_refuses_a_branch_ahead_of_its_upstream(
    outgrown: Path,
) -> None:
    """The precondition the preflight has to model, pinned so it stays true."""
    with pytest.raises(sh.ErrorReturnCode):
        sh.Command("git")("-C", str(outgrown), "branch", "-d", "topic", _tty_out=False)


def test_a_branch_ahead_of_its_upstream_is_deleted_anyway(
    outgrown: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is discarded: the branch step got here because HEAD holds it all."""
    monkeypatch.chdir(outgrown)

    branches.delete_branch("topic", dry_run=False, force=False)

    assert "topic" not in branch_names(outgrown)
    assert "topic" not in remote_branch_names(outgrown)


def test_the_dry_run_names_the_upstream_as_what_forces_it(
    outgrown: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A probe result, not a restatement of a flag the caller never passed."""
    monkeypatch.chdir(outgrown)

    branches.delete_branch("topic", dry_run=True, force=False)

    assert "force: ahead of origin/topic" in capsys.readouterr().out


def test_a_tracking_branch_head_lacks_is_blocked_like_any_other(
    outgrown: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The combination the force must never reach: upstream set, work not in.

    Being ahead of an upstream only excuses the plain delete once HEAD holds
    every commit. Read without that guard it would describe this branch too,
    and deleting it would take the only copy of `later` with it.
    """
    git = initialized_repo(outgrown, outgrown.parent / "no-hooks")
    git("checkout", "-q", "-b", "later")
    git("push", "-q", "-u", "origin", "later")
    commit_file(git, outgrown, "later.txt", "later\n", "feat: later")
    git("checkout", "-q", "main")
    monkeypatch.chdir(outgrown)

    with pytest.raises(typer.Exit):
        branches.delete_branch("later", dry_run=False, force=False)

    assert "later" in branch_names(outgrown)
    assert "branch is unmerged" in capsys.readouterr().err


def test_a_branch_head_lacks_is_blocked_upstream_or_not(
    pushed: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The safety the upstream case must not spend: unmerged still means blocked.

    `solo` holds a commit main does not, so no reading of its upstream may
    promote it into a deletion — that is the one case the force is for.
    """
    monkeypatch.chdir(pushed)

    with pytest.raises(typer.Exit):
        branches.delete_branch("solo", dry_run=False, force=False)

    assert "solo" in branch_names(pushed)
    assert "branch is unmerged" in capsys.readouterr().err


def test_landed_work_is_deleted_from_a_worktree_standing_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Containment is read off the branch work lands on, not off HEAD.

    Deletion runs from wherever the caller is standing, which right after a
    merge is routinely some other feature branch that never held the work.
    Read from there, a branch that has landed is unmerged, and the cleanup
    its own merge just asked for is refused as though something were at
    stake — the one case where the force discards nothing at all.
    """
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    base = str(git("rev-parse", "HEAD", _tty_out=False)).strip()
    git("checkout", "-q", "-b", "landed")
    commit_file(git, work, "landed.txt", "landed\n", "feat: landed")
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-edit", "landed")
    git("checkout", "-q", "-b", "elsewhere", base)
    monkeypatch.chdir(work)

    branches.delete_branch("landed", dry_run=False, force=False)

    assert "landed" not in branch_names(work)


def test_a_stranded_worktree_is_pruned_and_the_branch_deleted(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The state an interrupted removal leaves behind: the checkout is gone but
    # git still registers it, which by itself makes the branch undeletable.
    shutil.rmtree(repo.parent / "feature")
    monkeypatch.chdir(repo)

    branches.delete_branch("feature", dry_run=False, force=False)

    assert "Pruned stranded worktree" in capsys.readouterr().out
    assert "feature" not in branch_names(repo)


@pytest.fixture
def remote_only(tmp_path: Path) -> Path:
    """An origin carrying branches no local ref corresponds to.

    What the end of a landed branch's life leaves behind: the local copy went
    when the work merged, origin's did not, and the name a caller hands this
    command resolves to nothing under `refs/heads`. `spent` is contained in
    main; `solo` holds a commit main does not.
    """
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    sh.Command("git")("init", "--bare", "-q", str(work.parent / "origin.git"))
    git("remote", "add", "origin", str(work.parent / "origin.git"))
    git("branch", "spent")
    git("checkout", "-q", "-b", "solo")
    commit_file(git, work, "extra.txt", "extra\n", "feat: extra")
    git("checkout", "-q", "main")
    git("push", "-q", "origin", "main", "spent", "solo")
    git("branch", "-D", "spent")
    git("branch", "-D", "solo")
    return work


def test_a_remote_only_branch_is_deleted_where_it_lives(
    remote_only: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The survey classifies it DELETE, and this is the verb that acts on it."""
    monkeypatch.chdir(remote_only)

    branches.delete_branch("spent", dry_run=False, force=False)

    assert "spent" not in remote_branch_names(remote_only)


def test_a_remote_only_branch_is_reported_as_one(
    remote_only: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The plan names what is there rather than a local branch that is not."""
    monkeypatch.chdir(remote_only)

    branches.delete_branch("spent", dry_run=True, force=False)

    out = capsys.readouterr().out
    assert "Delete remote branch: origin/spent" in out
    assert "Delete local branch" not in out
    assert "unmerged" not in out


def test_an_unmerged_remote_only_branch_is_blocked_on_what_it_holds(
    remote_only: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The containment check run against the ref that exists, not one that does not.

    Origin's copy is the only copy, so the containment question is asked of
    it. Answering from a local branch that is not there makes the refusal say
    `--force` would discard unmerged work in the one case where nothing is at
    stake, and stay silent in the case where everything is.
    """
    monkeypatch.chdir(remote_only)

    with pytest.raises(typer.Exit):
        branches.delete_branch("solo", dry_run=False, force=False)

    assert "solo" in remote_branch_names(remote_only)
    assert "origin/solo holds commits main does not" in capsys.readouterr().err


def test_a_name_that_is_no_branch_anywhere_is_refused_as_such(
    remote_only: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing to delete is its own answer, not a verdict about mergedness."""
    monkeypatch.chdir(remote_only)

    with pytest.raises(typer.Exit):
        branches.delete_branch("never-existed", dry_run=False, force=False)

    err = capsys.readouterr().err
    assert "no branch by that name" in err
    assert "unmerged" not in err


@pytest.fixture
def carried(tmp_path: Path) -> Path:
    """A project whose scaffold carrier `dev update` has already merged.

    The shape every adopter reaches after its first update: the carrier's tip
    is an ancestor of the integration branch, which is exactly what reads as a
    spent branch to anything that does not know what the carrier is for.
    """
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    git("checkout", "-q", "-b", "lup-scaffold")
    commit_file(git, work, "copied.txt", "copied\n", "chore: compile the scaffold")
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "--no-edit", "lup-scaffold")
    git("branch", "develop")
    return work


def test_the_scaffold_carrier_is_refused_even_forced(
    carried: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Its tip is the next update's merge base, so no flag makes deleting it safe."""
    monkeypatch.chdir(carried)

    with pytest.raises(typer.Exit):
        branches.delete_branch(
            "lup-scaffold", dry_run=False, force=True, scaffold="lup-scaffold"
        )

    assert "lup-scaffold" in branch_names(carried)
    assert "scaffold carrier" in capsys.readouterr().err


def test_a_protected_branch_is_refused_even_forced(
    carried: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The names no sweep offers to delete are no verb's to delete either."""
    monkeypatch.chdir(carried)

    with pytest.raises(typer.Exit):
        branches.delete_branch("develop", dry_run=False, force=True)

    assert "develop" in branch_names(carried)
    assert "protected" in capsys.readouterr().err


def test_the_scaffold_carrier_is_refused_retirement(
    carried: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Retiring ends in the same deletion, so it is refused before anything moves."""
    monkeypatch.chdir(carried)

    with pytest.raises(typer.Exit):
        branches.retire_branch(
            "lup-scaffold", "abandoned", dry_run=False, scaffold="lup-scaffold"
        )

    assert "lup-scaffold" in branch_names(carried)
    assert "scaffold carrier" in capsys.readouterr().err


def test_a_protected_branch_is_refused_retirement(
    carried: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(carried)

    with pytest.raises(typer.Exit):
        branches.retire_branch("develop", "abandoned", dry_run=False)

    assert "develop" in branch_names(carried)
    assert "protected" in capsys.readouterr().err


def test_a_dry_run_retirement_of_the_carrier_says_it_is_refused(
    carried: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(carried)

    branches.retire_branch(
        "lup-scaffold", "abandoned", dry_run=True, scaffold="lup-scaffold"
    )

    assert "scaffold carrier" in capsys.readouterr().out


def test_a_merged_carrier_the_project_does_not_declare_deletes(
    carried: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The precondition, so the refusal above cannot pass for the wrong reason."""
    monkeypatch.chdir(carried)

    branches.delete_branch("lup-scaffold", dry_run=False, force=False)

    assert "lup-scaffold" not in branch_names(carried)


@pytest.fixture
def merged_on_the_forge(tmp_path: Path) -> Path:
    """A branch whose pull request merged on the forge, which local main never pulled.

    The merge happens in a second clone and is pushed, as a forge's merge
    button does; this checkout then fetches, so `origin/main` holds the merge
    and its own `main` does not.
    """
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    origin = tmp_path / "origin.git"
    sh.Command("git")("init", "--bare", "-q", "-b", "main", str(origin))
    git("remote", "add", "origin", str(origin))
    git("checkout", "-q", "-b", "topic")
    commit_file(git, work, "extra.txt", "extra\n", "feat: extra")
    git("checkout", "-q", "main")
    git("push", "-q", "origin", "main", "topic")

    forge = tmp_path / "forge"
    sh.Command("git")("clone", "-q", str(origin), str(forge), _tty_out=False)
    merger = initialized_repo(forge, tmp_path / "no-hooks")
    merger("merge", "--no-ff", "-m", "Merge pull request #1", "origin/topic")
    merger("push", "-q", "origin", "main")

    git("fetch", "-q", "origin")
    return work


def test_a_branch_merged_on_the_forge_deletes_though_main_is_stale(
    merged_on_the_forge: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Origin's integration branch holds it, so nothing is discarded.

    Judged against the local integration branch alone, a merge that happened
    on the forge reads as unmerged until somebody pulls, and a refusal there
    offers `--force` — the instrument for discarding work, handed out where
    none is at stake.
    """
    monkeypatch.chdir(merged_on_the_forge)

    branches.delete_branch("topic", dry_run=False, force=False)

    assert "topic" not in branch_names(merged_on_the_forge)
    assert "topic" not in remote_branch_names(merged_on_the_forge)
    assert "origin/main" in capsys.readouterr().out


def test_the_dry_run_says_which_copy_of_main_holds_it(
    merged_on_the_forge: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(merged_on_the_forge)

    branches.delete_branch("topic", dry_run=True, force=False)

    out = capsys.readouterr().out
    assert "unmerged" not in out
    assert "git pull --ff-only" in out
