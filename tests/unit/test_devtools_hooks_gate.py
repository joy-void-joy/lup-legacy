"""Behavior tests for the one hooks write left in making a worktree.

`<common>/hooks/` holds scripts git executes on the host, so it is held
read-only inside the writable share — the same door `config` opens, with no
key in between. Holding it costs nothing because arming is a once-per-clone
act: `git rev-parse --git-path hooks` in a linked worktree names the shared
directory, so a guard armed once is armed for every worktree cut afterwards.

Which is what the diagnosis in front of it has to know. Asked about the
outstanding arming, it says nothing to a clone whose guards are already
current, and names the moment for one whose guards are not — while leaving
the worktree to be cut either way, because a checkout withheld here arms
nothing that was not already unarmed everywhere else in the clone.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from lup.devtools.dev import worktree
from lup.devtools.dev.git_guards import DECLARED_GUARDS, blocked_arming, install_guards
from lup.devtools.harness.launch import relocation_hint
from tests.unit.repos import commit_file, initialized_repo

needs_a_mode_that_refuses = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root creates a file in a mode-555 directory, so the mode cannot "
    "stand in for the mount that would refuse it",
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout with one commit, standing where the command would run."""
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def tree_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The sibling directory worktrees are created in, as `create` resolves it."""
    tree = tmp_path / "tree"
    tree.mkdir()
    monkeypatch.setattr(worktree, "get_tree_dir", lambda: tree)
    return tree


@pytest.fixture
def hooks(repo: Path) -> Iterator[Path]:
    """This clone's hooks directory, with whatever a test held it by undone.

    Restored on the way out, or the temporary tree cannot be removed.
    """
    directory = repo / ".git" / "hooks"
    directory.mkdir(parents=True, exist_ok=True)
    yield directory
    directory.chmod(0o755)


def hold(directory: Path) -> None:
    """Hold a directory the way a read-only mount does.

    A mode this process cannot create inside stands in for the mount: it is
    the same refusal of the same syscall, and the one shape a test can take
    without a mount namespace of its own.
    """
    directory.chmod(0o555)


def arm_on_the_host(repo: Path) -> None:
    """Arm the declared guards as a clone's one host act does."""
    install_guards(DECLARED_GUARDS, repo)


@needs_a_mode_that_refuses
def test_an_armed_clone_is_not_stopped_by_hooks_it_cannot_write(
    repo: Path, hooks: Path
) -> None:
    """The write is outstanding for nobody here, so nothing is refused."""
    arm_on_the_host(repo)
    hold(hooks)

    worktree.report_a_blocked_arming()


@needs_a_mode_that_refuses
def test_an_unarmed_clone_is_told_which_moment_is_outstanding(
    repo: Path, hooks: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The case the diagnosis exists for, said before anything is cut.

    Said, and not enforced. The worktree about to be cut resolves its hooks
    through the same shared directory every existing worktree of this clone
    already commits under, so refusing it withholds a checkout without arming
    anything — and where that directory cannot be written from here at all,
    it withholds every checkout, permanently.
    """
    hold(hooks)

    worktree.report_a_blocked_arming()

    reported = capsys.readouterr().err
    assert "guard not installed" in reported
    assert "git hooks install" in reported


@needs_a_mode_that_refuses
def test_an_install_refused_here_names_the_command_the_host_runs(
    repo: Path, hooks: Path
) -> None:
    """Installing is a host step, so the refusal hands over the host's exact command.

    The same answer `git hooks install` gives inside a contained session,
    which holds the shared hooks directory read-only: the checkout to run it
    in, and the command, rather than an errno about a busy device.
    """
    hold(hooks)

    reported = blocked_arming(DECLARED_GUARDS, repo)

    assert f"`cd {repo} && uv run lup-devtools git hooks install`" in reported


@needs_a_mode_that_refuses
def test_an_armed_clone_installs_again_without_writing(repo: Path, hooks: Path) -> None:
    """Nothing outstanding writes nothing, so an install where all is current passes.

    Which is what lets it be run inside a session holding the directory
    read-only, where it answers that the hooks are armed.
    """
    arm_on_the_host(repo)
    hold(hooks)

    assert all(state.armed for state in install_guards(DECLARED_GUARDS, repo))


@needs_a_mode_that_refuses
def test_a_stale_guard_is_named_rather_than_left_standing(
    repo: Path, hooks: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hook git runs is not the one the declaration describes.

    The case an answer keyed on presence cannot cover: the file is there and
    git runs it, so the moment reads as guarded while the body executing is
    one nobody declared.
    """
    arm_on_the_host(repo)
    installed = hooks / "pre-commit"
    installed.write_text(
        installed.read_text(encoding="utf-8") + "# an older body\n", encoding="utf-8"
    )
    hold(hooks)

    worktree.report_a_blocked_arming()

    assert "is an older body" in capsys.readouterr().err


def test_an_unarmed_clone_that_can_write_is_let_through(repo: Path) -> None:
    """Nothing is refused where the arming it guards would simply happen."""
    worktree.report_a_blocked_arming()


@needs_a_mode_that_refuses
def test_a_worktree_is_still_made_where_the_hooks_directory_is_held(
    repo: Path, tree_dir: Path, hooks: Path
) -> None:
    """The whole point, end to end: a cut worktree inherits the armed guard.

    Hooks resolve through the shared directory, so the worktree this makes
    reads its `pre-commit` out of the same place the host armed it, and setup
    writes nothing into a directory it may not write.
    """
    arm_on_the_host(repo)
    hold(hooks)

    worktree.create(
        "topic",
        no_sync=True,
        no_copy_data=True,
        base_branch=None,
        launcher=relocation_hint,
    )

    assert (tree_dir / "topic").is_dir()


@needs_a_mode_that_refuses
def test_a_worktree_is_still_made_where_the_guard_could_not_be_armed(
    repo: Path,
    tree_dir: Path,
    hooks: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing armed this clone, and the worktree is cut regardless.

    Naming the outstanding moment is the most that can truthfully be done
    from here: the guard is armed on the host or not at all, and the worktree
    being cut resolves its hooks through the same shared directory this
    repository is already committing under. Withholding the checkout as well
    would leave the operator holding neither.
    """
    hold(hooks)

    worktree.create(
        "topic",
        no_sync=True,
        no_copy_data=True,
        base_branch=None,
        launcher=relocation_hint,
    )

    assert (tree_dir / "topic").is_dir()
    assert "git hooks install" in capsys.readouterr().err
