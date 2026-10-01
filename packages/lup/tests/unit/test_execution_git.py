"""One repository, each question asked one way, against a real git.

The bare copies that cannot import this module — the policy host's and the
drift fold's — are pinned to its answers here, since no type checker spans
the boundary between them.
"""

import os
from pathlib import Path

import pytest
import sh

from lup.devtools.dev import drift_fold
from lup.devtools.gitguard import TEST_IDENTITY
from lup.execution.git import GitError, Repository
from lup.execution.process import LocalProcessLauncher
from lup.policy.assets import host
from lup.resolver.orchestrator import WorktreeOrchestrator


def git(where: Path, *arguments: str) -> str:
    """One git command bound to ``where``, as the suite's own identity."""
    return str(
        sh.Command("git")(
            "-C",
            str(where),
            *arguments,
            _tty_out=False,
            _env={**os.environ, **TEST_IDENTITY.environment()},
        )
    ).strip()


def committed(where: Path, name: str, content: str, message: str) -> str:
    """One file written and committed, and the commit it made."""
    (where / name).write_text(content)
    git(where, "add", name)
    git(where, "commit", "-q", "-m", message)
    return git(where, "rev-parse", "HEAD")


@pytest.fixture
def main(tmp_path: Path) -> Path:
    """A repository on `trunk` with one commit, and a sibling worktree whose path has a space."""
    root = tmp_path / "main"
    root.mkdir()
    git(root, "init", "-q", "-b", "trunk")
    committed(root, "a.txt", "one\n", "root")
    git(root, "worktree", "add", "-q", "-b", "side", str(tmp_path / "a sibling"))
    return root


def test_every_location_is_absolute_from_a_linked_worktree(
    main: Path, tmp_path: Path
) -> None:
    sibling = Repository(tmp_path / "a sibling")

    assert sibling.top() == (tmp_path / "a sibling").resolve()
    assert sibling.common_dir() == (main / ".git").resolve()
    assert sibling.git_dir().parent == (main / ".git" / "worktrees").resolve()
    assert sibling.admin_dirs() == [sibling.git_dir(), sibling.common_dir()]
    assert Repository(main).admin_dirs() == [(main / ".git").resolve()]


def test_a_question_with_no_natural_no_raises_outside_a_repository(
    tmp_path: Path,
) -> None:
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()

    with pytest.raises(GitError, match="exited 128"):
        Repository(nowhere).top()
    with pytest.raises(GitError):
        Repository(nowhere).branch()


def test_a_directory_that_is_not_there_is_asked_like_one_outside_a_repository(
    tmp_path: Path,
) -> None:
    gone = Repository(tmp_path / "gone")

    with pytest.raises(GitError, match="no such directory"):
        gone.top()
    assert gone.resolves("HEAD") is None


def test_a_question_with_a_natural_no_answers_it(main: Path) -> None:
    repository = Repository(main)

    assert repository.resolves("no-such-branch") is None
    assert repository.merging() is None
    assert repository.remote_url("origin") is None
    assert not repository.is_ancestor("no-such-branch", "trunk")


def test_refs_ancestry_and_counts(main: Path) -> None:
    repository = Repository(main)
    first = git(main, "rev-parse", "HEAD")
    second = committed(main, "b.txt", "two\n", "second")

    assert repository.branch() == "trunk"
    assert repository.resolves("trunk") == second
    assert repository.resolves("refs/heads/side") == first
    assert repository.is_ancestor(first, second)
    assert not repository.is_ancestor(second, first)
    assert repository.count(f"{first}..{second}") == 1
    assert repository.count("trunk") == 2


def test_a_detached_head_is_on_no_branch(main: Path) -> None:
    git(main, "checkout", "-q", "--detach")

    assert Repository(main).branch() == ""


def test_a_conflicted_merge_names_its_paths_and_its_parent(main: Path) -> None:
    git(main, "checkout", "-q", "-b", "theirs")
    theirs = committed(main, "a.txt", "theirs\n", "theirs")
    git(main, "checkout", "-q", "trunk")
    committed(main, "a.txt", "ours\n", "ours")
    sh.Command("git")(
        "-C",
        str(main),
        "merge",
        "-q",
        "theirs",
        _tty_out=False,
        _ok_code=[1],
        _env={**os.environ, **TEST_IDENTITY.environment()},
    )
    repository = Repository(main)

    assert repository.conflicted() == [Path("a.txt")]
    assert repository.merging() == theirs


def test_a_remote_is_where_it_fetches_from(main: Path, tmp_path: Path) -> None:
    git(main, "remote", "add", "origin", str(tmp_path / "upstream.git"))

    assert Repository(main).remote_url("origin") == str(tmp_path / "upstream.git")


def test_worktrees_keep_a_space_and_a_quoted_lock_reason_whole(
    main: Path, tmp_path: Path
) -> None:
    reason = '{"session": "a b", "held": true}'
    git(main, "worktree", "lock", "--reason", reason, str(tmp_path / "a sibling"))
    git(main, "worktree", "add", "-q", "--detach", str(tmp_path / "detached"))

    listed = {worktree.path: worktree for worktree in Repository(main).worktrees()}

    sibling = listed[(tmp_path / "a sibling").resolve()]
    assert sibling.branch == "side"
    assert sibling.locked and sibling.lock_reason == reason
    assert listed[main.resolve()].branch == "trunk"
    detached = listed[(tmp_path / "detached").resolve()]
    assert detached.detached and detached.branch == ""


def test_an_environment_reaches_every_question(main: Path, tmp_path: Path) -> None:
    index = tmp_path / "private.index"
    private = Repository(main, LocalProcessLauncher(), {"GIT_INDEX_FILE": str(index)})

    private.answer("read-tree", "HEAD")

    assert index.is_file()


def test_a_checkout_whose_path_has_a_space_is_not_taken_for_debris(
    main: Path, tmp_path: Path
) -> None:
    """The resolver read `git worktree list` by its first word, which a space ends."""
    stray = tmp_path / "a stray"
    stray.mkdir()
    orchestrator = WorktreeOrchestrator(LocalProcessLauncher(), main)

    orchestrator.discard_unregistered((tmp_path / "a sibling").resolve())
    orchestrator.discard_unregistered(stray)

    assert (tmp_path / "a sibling").is_dir()
    assert not stray.exists()


def test_the_policy_hosts_bare_answers_agree(main: Path, tmp_path: Path) -> None:
    sibling = tmp_path / "a sibling"
    repository = Repository(sibling)

    assert host.shared_git_directory(str(sibling)) == str(
        repository.common_dir().resolve()
    )
    assert host.git_answers(["rev-parse", "--absolute-git-dir"], sibling) == [
        str(repository.git_dir())
    ]
    assert host.sibling_worktrees(main) == [
        str(worktree.path)
        for worktree in Repository(main).worktrees()
        if not worktree.bare and worktree.path.resolve() != main.resolve()
    ]


def test_the_drift_folds_bare_answer_agrees(
    main: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path / "a sibling")

    assert drift_fold.asked("rev-parse", "--show-toplevel") == str(
        Repository(Path.cwd()).top()
    )
