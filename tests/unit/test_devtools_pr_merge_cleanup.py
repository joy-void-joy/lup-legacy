"""Behavior tests for the branch cleanup `lup-devtools dev pr merge` performs.

`gh pr merge --delete-branch` runs a plain `git branch -d`, which refuses while
any worktree holds the branch. In a tree of worktrees that is every branch, so
each merge would report a cleanup failure and leave both the branch and its
checkout behind for the caller to clear by hand. These pin that the merge
cleans up through the deletion path that removes the worktree first, and that a
cleanup which cannot finish is still reported rather than raised — the merge
already happened, and re-running it would fail against a PR GitHub already
closed.
"""

from pathlib import Path

import pytest
import sh

from lup.devtools.dev import pr
from lup.devtools.dev import branches, worktree
from lup.coordination.repository import RepositoryPeers
import typer
from tests.unit.repos import git_in, initialized_repo


@pytest.fixture
def merged(tmp_path: Path) -> Path:
    """A repo whose merged branch still has the worktree that built it."""
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    (work / "file.txt").write_text("base\n", encoding="utf-8")
    git("add", "file.txt")
    git("commit", "-m", "chore: base")

    git("worktree", "add", str(tmp_path / "feature"), "-b", "feature")
    feature = git_in(tmp_path / "feature", tmp_path / "no-hooks")
    (tmp_path / "feature" / "extra.txt").write_text("extra\n", encoding="utf-8")
    feature("add", "extra.txt")
    feature("commit", "-m", "feat: extra")

    git("merge", "--no-edit", "feature")
    return work


def branch_names(work: Path) -> list[str]:
    out = sh.Command("git")(
        "-C", str(work), "branch", "--format=%(refname:short)", _tty_out=False
    )
    return str(out).split()


def test_a_merged_branch_goes_even_though_a_worktree_holds_it(
    merged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(merged)

    pr.cleanup_merged_branch("feature")

    assert "feature" not in branch_names(merged)
    assert not (tmp_path / "feature").exists()


def test_the_plain_delete_gh_runs_would_have_refused(
    merged: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The precondition the cleanup path is for, so the test above cannot pass idly."""
    monkeypatch.chdir(merged)

    with pytest.raises(sh.ErrorReturnCode):
        sh.Command("git")("-C", str(merged), "branch", "-d", "feature", _tty_out=False)


@pytest.mark.parametrize("force", [False, True])
def test_live_owner_protects_a_clean_merged_checkout(
    merged: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    monkeypatch.chdir(merged)
    feature = merged.parent / "feature"
    peers = RepositoryPeers(merged)
    peers.join("writer", feature, cli_name="editing-session")
    plan = branches.plan_deletion("feature", force=force)
    assert any(
        action.verdict == "refused" and "editing-session" in action.detail
        for action in plan.actions
    )
    pr.cleanup_merged_branch("feature")
    with pytest.raises(typer.Exit):
        worktree.remove(str(feature), force=force)
    assert feature.is_dir()
    assert "feature" in branch_names(merged)
    peers.leave("writer")
    pr.cleanup_merged_branch("feature")
    assert not feature.exists()


def test_cleanup_rechecks_a_session_arriving_after_preflight(
    merged: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(merged)
    plan = branches.plan_deletion("feature", force=True)
    assert not plan.blocked()
    RepositoryPeers(merged).join("late-writer", merged.parent / "feature")
    with pytest.raises(typer.Exit):
        branches.run_deletion(plan, force=True)
    assert (merged.parent / "feature").is_dir()
    assert "feature" in branch_names(merged)


def test_a_branch_that_is_already_gone_is_left_alone(
    merged: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(merged)

    pr.cleanup_merged_branch("never-existed")

    assert "feature" in branch_names(merged)


def test_a_cleanup_that_cannot_finish_reports_instead_of_raising(
    merged: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the branch you are standing on is refused, and stays refused.

    Raising here would report a merge that happened as a command that failed,
    which is the confusion the whole cleanup path is arranged to avoid.
    """
    monkeypatch.chdir(merged.parent / "feature")

    pr.cleanup_merged_branch("feature")

    assert "feature" in branch_names(merged)
    assert "still here" in capsys.readouterr().err
