"""The cut git logged, and everything it cannot answer for.

lup's own base record is written by lup's own worktree command and by nothing
else, so a branch made with plain git carried no base at all: detection fell
through to topology, and for one task two siblings tied there and the quality
gate refused to run on an ordinary branch.

Git logs the cut itself, in the branch's first reflog entry, and every command
that makes a branch writes it. That is what this reads. It is evidence and not
authority, so what is pinned here is both halves: the answer where git has one,
and an empty answer — unremarkable, never an error — in each of the ways it
has none.
"""

from pathlib import Path

import pytest

from lup.devtools.dev import worktree
from lup.devtools.dev.branches import created_from
from lup.devtools.dev.records import log_ref_updates
from lup.devtools.harness.launch import relocation_hint
from lup.devtools.sync import clone_bare
from lup.execution.process import LaunchRequest, LocalProcessLauncher


def run_git(cwd: Path, *arguments: str) -> None:
    """Run one git command for a fixture repository, failing on its own stderr.

    Identity per invocation, never `git config` — a persisted setting lands
    in the shared config every worktree of a real repository inherits.
    """
    who = ("-c", "user.email=cut@example.test", "-c", "user.name=Cut Test")
    status = LocalProcessLauncher().launch(
        LaunchRequest(arguments=["git", *who, *arguments], cwd=cwd)
    )
    if status.code != 0:
        raise AssertionError(status.stderr)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout on `dev` with one commit, and a remote holding a copy of it.

    The remote is part of the fixture because a remote-tracking ref is one of
    the spellings a creation entry can carry, and a repository with no remote
    cannot tell that reading from a reading that never had one to try.
    """
    work = tmp_path / "work"
    run_git(tmp_path, "init", "-q", "-b", "dev", str(work))
    run_git(work, "commit", "-q", "--allow-empty", "-m", "base")
    run_git(tmp_path, "init", "-q", "--bare", str(tmp_path / "origin.git"))
    run_git(work, "remote", "add", "origin", str(tmp_path / "origin.git"))
    run_git(work, "push", "-q", "origin", "dev")
    return work


def test_a_branch_switched_into_being_names_what_it_was_cut_from(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_git(repo, "switch", "-q", "-c", "topic", "dev")
    monkeypatch.chdir(repo)

    assert created_from("topic") == "dev"


def test_a_branch_a_worktree_was_cut_for_names_it_too(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch made outside lup, which writes no base record of its own.

    `git worktree add -b` is how a branch is made without going through lup,
    and it logs the cut exactly as `git branch` and `git switch -c` do — which
    is what makes a base stop depending on which command cut the branch.
    """
    run_git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "topic", "dev")
    monkeypatch.chdir(repo)

    assert created_from("topic") == "dev"


def test_a_remote_tracking_ref_answers_as_the_branch_it_tracks(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`origin/dev` is somebody's copy of `dev`, and `dev` is the usable name.

    A base is fetched and merged by name, so the answer has to be a branch
    this clone carries. The remote's own prefix is stripped from the list of
    remotes rather than at the first slash: `feat/x` is one branch name.
    """
    run_git(repo, "switch", "-q", "-c", "topic", "origin/dev")
    monkeypatch.chdir(repo)

    assert created_from("topic") == "dev"


def test_a_branch_cut_where_nothing_logged_it_answers_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case that makes this evidence rather than authority.

    `core.logAllRefUpdates` is false without a working tree, so a bare clone
    — which is how a machine holds one project as a git directory with
    worktrees beside it — logs no creation for a branch cut against the git
    directory itself. Measured here by turning the setting off, which is the
    same condition by the same switch.
    """
    run_git(repo, "-c", "core.logAllRefUpdates=false", "branch", "topic", "dev")
    monkeypatch.chdir(repo)

    assert created_from("topic") == ""


def test_a_cut_from_head_names_nothing_a_later_reader_can_measure(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git switch -c topic` logs `Created from HEAD`, which is not a base.

    HEAD named wherever the creating checkout stood, and that is exactly the
    fact a base record exists to preserve — so an entry carrying it is no
    answer and topology is asked instead.
    """
    run_git(repo, "switch", "-q", "-c", "topic")
    monkeypatch.chdir(repo)

    assert created_from("topic") == ""


def test_a_cut_from_a_bare_commit_is_no_base_either(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit is a point, and `pr sync-base` fetches and merges a branch."""
    tip = LocalProcessLauncher().launch(
        LaunchRequest(arguments=["git", "rev-parse", "dev"], cwd=repo)
    )
    run_git(repo, "branch", "topic", tip.stdout.strip())
    monkeypatch.chdir(repo)

    assert created_from("topic") == ""


def read_setting(repository: Path) -> str:
    """The reflog setting a repository's git directory carries, empty where unset."""
    return (
        LocalProcessLauncher()
        .launch(
            LaunchRequest(
                arguments=["git", "config", "--get", "core.logAllRefUpdates"],
                cwd=repository,
            )
        )
        .stdout.strip()
    )


@pytest.fixture
def hermetic(monkeypatch: pytest.MonkeyPatch) -> None:
    """No global or system git setting, so a repository's own config decides.

    The setting is answered across every scope, so a machine whose own global
    config names it would otherwise decide every case below.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def bare(repo: Path, tmp_path: Path, hermetic: None) -> Path:
    """A bare clone of the fixture's checkout, made with plain git."""
    clone = tmp_path / "project.git"
    run_git(tmp_path, "clone", "-q", "--bare", str(repo), str(clone))
    return clone


def test_a_bare_clone_nobody_prepared_logs_no_cut(
    bare: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git's default, measured, so the cases below measure lup's change to it."""
    run_git(bare, "branch", "topic", "dev")
    monkeypatch.chdir(bare)

    assert created_from("topic") == ""


def test_a_bare_clone_sync_makes_logs_the_cut_of_a_plain_git_branch(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hermetic: None
) -> None:
    """The cache clone sync makes, then a branch cut against its git directory.

    `git branch` run in the bare directory is exactly the command a bare clone
    left at git's default logs nothing for; the clone sync makes answers
    through the reflog like any other.
    """
    clone = tmp_path / "cache" / "project.git"
    clone_bare(str(repo), clone, lambda _: None)
    run_git(clone, "branch", "topic", "dev")
    monkeypatch.chdir(clone)

    assert read_setting(clone) == "true"
    assert created_from("topic") == "dev"


def test_a_worktree_cut_from_a_bare_clone_turns_its_reflog_on(
    bare: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step `git worktree create` runs, from inside one of the clone's worktrees.

    Written to the shared config rather than the worktree's own, since the
    git directory itself is where the cut went unlogged.
    """
    run_git(bare, "worktree", "add", "-q", str(bare / "tree" / "dev"), "dev")
    monkeypatch.chdir(bare / "tree" / "dev")
    step = worktree.LoggedRefUpdates()

    assert not step.satisfied()
    step.run()
    assert step.satisfied()
    run_git(bare, "branch", "topic", "dev")
    assert read_setting(bare) == "true"
    assert created_from("topic") == "dev"


def test_an_explicit_setting_is_left_as_it_was(bare: Path) -> None:
    """Off, said on purpose, is the owner's call: nothing is written over it."""
    run_git(bare, "config", "core.logAllRefUpdates", "false")

    assert not log_ref_updates(bare)
    assert read_setting(bare) == "false"


def test_turning_it_on_twice_writes_once(bare: Path) -> None:
    assert log_ref_updates(bare)
    assert not log_ref_updates(bare)
    assert read_setting(bare) == "true"


def test_a_clone_with_a_working_tree_is_left_at_its_default(
    repo: Path, hermetic: None
) -> None:
    """Git already logs there, so the setting would say nothing git does not.

    `git init` writes it explicitly into a clone with a working tree, so it is
    removed first: what is measured is that the default is left to git.
    """
    run_git(repo, "config", "--unset", "core.logAllRefUpdates")

    assert not log_ref_updates(repo)
    assert read_setting(repo) == ""


def test_a_worktree_created_in_a_bare_clone_leaves_its_reflog_on(
    bare: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole command, from the checkout a machine keeps beside the clone."""
    run_git(bare, "worktree", "add", "-q", str(bare / "tree" / "dev"), "dev")
    monkeypatch.chdir(bare / "tree" / "dev")
    monkeypatch.setattr(worktree, "get_tree_dir", lambda: bare / "tree")

    worktree.create(
        "topic",
        no_sync=True,
        no_copy_data=True,
        base_branch=None,
        launcher=relocation_hint,
    )
    run_git(bare, "branch", "other", "dev")

    assert read_setting(bare) == "true"
    assert created_from("other") == "dev"


def confine(bare: Path) -> None:
    """Hold the clone's `config.lock` the way a sandbox does, without a mount.

    A device node where git expects a file of its own refuses the exclusive
    create every config write begins with, so config is all it blocks.
    """
    (bare / "config.lock").symlink_to(Path("/dev/null"))


def test_a_clone_that_cannot_take_it_is_told_the_host_command(
    bare: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Named before anything is made, with the command that settles it."""
    confine(bare)
    monkeypatch.chdir(bare)

    assert worktree.report_a_blocked_reflog(diagnosed=False) is True
    said = capsys.readouterr().err
    assert "blocked by the sandbox" in said
    assert f"git -C {bare} config core.logAllRefUpdates true" in said


def test_a_blockage_already_diagnosed_is_not_diagnosed_twice(
    bare: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    confine(bare)
    monkeypatch.chdir(bare)

    assert worktree.report_a_blocked_reflog(diagnosed=True) is True
    said = capsys.readouterr().err
    assert "blocked by the sandbox" not in said
    assert "core.logAllRefUpdates true" in said


def test_a_clone_that_can_take_it_hears_nothing(
    bare: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(bare)

    assert worktree.report_a_blocked_reflog(diagnosed=False) is False
    assert capsys.readouterr().err == ""
