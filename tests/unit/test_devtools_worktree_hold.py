"""A worktree a session creates is that session's until it leaves the roster.

The session that runs `git worktree create` is rarely standing in what it
creates: it was launched in another checkout and writes into the new one by
absolute path. The roster therefore never named it as that checkout's user,
and once its work was committed and fast-forwarded the checkout read as clean
and spent — so a lander removed it while the session was still writing there,
and the next write failed as though the path had been mistyped.

What is pinned here: creation holds the checkout for the creating session,
`git delete` and `git worktree remove` refuse it by name while that session is
live, no force lifts that, and the hold ends by itself when the session leaves.
"""

from pathlib import Path

import pytest
import sh
import typer

from lup.coordination.identity import MEMBER_ENV
from lup.coordination.repository import RepositoryPeers
from lup.devtools.dev import branches, worktree
from lup.devtools.harness.launch import relocation_hint
from tests.unit.repos import commit_file, initialized_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    return work


@pytest.fixture
def tree_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tree = tmp_path / "tree"
    tree.mkdir()
    monkeypatch.setattr(worktree, "get_tree_dir", lambda: tree)
    return tree


@pytest.fixture
def created(repo: Path, tree_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`feat-x`, created by a live session launched in the main checkout."""
    monkeypatch.chdir(repo)
    RepositoryPeers(repo).join("creator", repo, cli_name="creating-session")
    monkeypatch.setenv(MEMBER_ENV, "creator")
    worktree.create(
        "feat-x",
        no_sync=True,
        no_copy_data=True,
        base_branch=None,
        launcher=relocation_hint,
    )
    monkeypatch.delenv(MEMBER_ENV)
    return tree_dir / "feat-x"


def branch_names(work: Path) -> list[str]:
    out = sh.Command("git")(
        "-C", str(work), "branch", "--format=%(refname:short)", _tty_out=False
    )
    return str(out).split()


@pytest.mark.parametrize("force", [False, True])
def test_a_lander_cannot_delete_a_worktree_its_live_creator_holds(
    created: Path,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    force: bool,
) -> None:
    """Clean, spent by every other reading, and still somebody's."""
    plan = branches.plan_deletion("feat-x", force=force)

    assert [action.verdict for action in plan.blocked()] == ["refused"]
    assert "creating-session" in plan.blocked()[0].detail
    with pytest.raises(typer.Exit):
        branches.delete_branch("feat-x", dry_run=False, force=force)
    assert created.is_dir()
    assert "feat-x" in branch_names(repo)
    assert "creating-session" in capsys.readouterr().err


def test_removing_the_worktree_directly_is_refused_too(created: Path) -> None:
    with pytest.raises(typer.Exit):
        worktree.remove(str(created), force=True)

    assert created.is_dir()


def test_the_hold_ends_when_its_session_leaves(created: Path, repo: Path) -> None:
    RepositoryPeers(repo).leave("creator")

    branches.delete_branch("feat-x", dry_run=False, force=False)

    assert not created.exists()
    assert "feat-x" not in branch_names(repo)


def test_the_creating_session_removes_its_own_worktree(
    created: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold keeps other sessions out, not the one it is for."""
    monkeypatch.setenv(MEMBER_ENV, "creator")

    branches.delete_branch("feat-x", dry_run=False, force=False)

    assert not created.exists()


def test_a_worktree_nobody_on_the_roster_created_is_not_held(
    repo: Path, tree_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a session to hold it for, a checkout is what it always was."""
    monkeypatch.chdir(repo)
    worktree.create(
        "feat-y",
        no_sync=True,
        no_copy_data=True,
        base_branch=None,
        launcher=relocation_hint,
    )

    branches.delete_branch("feat-y", dry_run=False, force=False)

    assert not (tree_dir / "feat-y").exists()
