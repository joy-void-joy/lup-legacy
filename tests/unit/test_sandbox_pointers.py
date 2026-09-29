"""Behavior tests for the worktree pointer verification.

The escape these guard against is measured, not hypothetical: a container that
rewrites a linked worktree's `.git`, or its administrative entry's `commondir`
or `gitdir`, points *host* git at a gitdir the container built, whose config
runs on the host. A read-only mount over those files closes the hole and breaks
`git worktree remove`, which unlinks them; so the pointer is verified on the
host against the shared directory the launcher trusts, and the same trust makes
the check un-foolable -- a forged pointer naming its own fake shared directory
is measured against the caller's, not against itself.
"""

from pathlib import Path

import pytest
import typer

from lup.execution.shell import git
from lup.sandbox.pointers import (
    PointerDrift,
    gitdir_target,
    pointer_drift,
    refusal,
)
from lup.sandbox.rail import repository_layout


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A bare repository with two linked worktrees beside it, as the rail runs.

    Bare rather than a plain checkout because that is the layout every session
    this repository launches lives in, and the one whose `.git` files are the
    one-line pointers this whole module is about.
    """
    source = tmp_path / "source"
    source.mkdir()
    git("-C", str(source), "init", "-q", "-b", "main")
    git("-C", str(source), "config", "user.email", "test@example.invalid")
    git("-C", str(source), "config", "user.name", "Test")
    (source / "README.md").write_text("readme\n", encoding="utf-8")
    git("-C", str(source), "add", "-A")
    git("-C", str(source), "commit", "-qm", "first")
    bare = tmp_path / "repo.git"
    git("clone", "-q", "--bare", str(source), str(bare))
    git("-C", str(bare), "worktree", "add", "-q", str(tmp_path / "mine"), "-b", "mine")
    git("-C", str(bare), "worktree", "add", "-q", str(tmp_path / "kept"), "-b", "kept")
    return tmp_path


def common_of(repository: Path) -> Path:
    """The trusted shared directory, resolved from a worktree the way lup does."""
    return repository_layout(repository / "mine").common


def evil_gitdir(repository: Path) -> Path:
    """A gitdir the container built, standing in for the one an escape points at."""
    built = repository / "mine" / ".evil"
    (built / "objects").mkdir(parents=True)
    (built / "refs").mkdir()
    (built / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (built / "config").write_text(
        "[core]\n\trepositoryformatversion = 0\n\thooksPath = /evil\n",
        encoding="utf-8",
    )
    return built


def test_a_clean_repository_has_no_pointer_drift(repository: Path) -> None:
    assert pointer_drift(common_of(repository)) == []


def test_a_redirected_dot_git_pointer_is_caught(repository: Path) -> None:
    """The `.git` file rewritten to a container gitdir: the back-pointer breaks.

    The entry's `gitdir` still names `mine/.git`, but `mine/.git` no longer
    names the entry, so host git following the checkout lands in `.evil`.
    """
    common = common_of(repository)
    built = evil_gitdir(repository)
    (repository / "mine" / ".git").write_text(f"gitdir: {built}\n", encoding="utf-8")
    drifts = pointer_drift(common)
    assert [drift.worktree for drift in drifts] == [repository / "mine"]
    assert drifts[0].names == str(built)


def test_a_redirected_commondir_is_caught(repository: Path) -> None:
    """The administrative entry's `commondir` aimed at a container gitdir."""
    built = evil_gitdir(repository)
    common = common_of(repository)
    (common / "worktrees" / "mine" / "commondir").write_text(
        f"{built}\n", encoding="utf-8"
    )
    drifts = pointer_drift(common)
    assert any(
        drift.pointer.name == "commondir" and drift.names == str(built)
        for drift in drifts
    )


def test_the_check_is_anchored_not_bootstrapped(repository: Path) -> None:
    """A forged entry naming its own fake shared directory does not pass.

    The attacker builds a complete fake `<fake>/worktrees/mine` -> `<fake>`
    chain and points the real entry's `commondir` at it. Deriving the truth
    from the pointer under test would call that consistent; measuring it
    against the caller's trusted common calls it what it is.
    """
    common = common_of(repository)
    fake = repository / "mine" / ".evil"
    (fake / "worktrees" / "mine").mkdir(parents=True)
    (common / "worktrees" / "mine" / "commondir").write_text(
        f"{fake}\n", encoding="utf-8"
    )
    drifts = pointer_drift(common)
    assert any(drift.names == str(fake) for drift in drifts)


def test_a_legitimately_created_worktree_is_not_flagged(repository: Path) -> None:
    """`git worktree add` writes the three pointers in-repo, so nothing drifts."""
    git(
        "-C",
        str(repository / "mine"),
        "worktree",
        "add",
        "-q",
        str(repository / "fresh"),
        "-b",
        "fresh",
    )
    assert pointer_drift(common_of(repository)) == []


def test_worktree_remove_still_works_and_leaves_no_drift(repository: Path) -> None:
    """The guard pins nothing, so removal -- which unlinks the pointers -- runs.

    The one workflow a read-only mount over the pointers would break; here it
    completes and the remaining set stays clean.
    """
    common = common_of(repository)
    git("-C", str(repository / "mine"), "worktree", "remove", str(repository / "kept"))
    assert not (common / "worktrees" / "kept").exists()
    assert pointer_drift(common) == []


def test_a_half_made_entry_is_left_unjudged(repository: Path) -> None:
    """An entry missing its pointer files is a worktree mid-add, not an attack."""
    (common_of(repository) / "worktrees" / "half").mkdir()
    assert pointer_drift(common_of(repository)) == []


def test_gitdir_target_is_none_for_a_real_git_directory(tmp_path: Path) -> None:
    """A plain checkout's `.git` is a directory, which cannot be redirected."""
    (tmp_path / ".git").mkdir()
    assert gitdir_target(tmp_path / ".git") is None


def test_refusal_names_every_redirected_worktree(repository: Path) -> None:
    """Full, not truncated: a dropped worktree is one host git would still enter."""
    drift = PointerDrift(
        worktree=repository / "mine",
        pointer=repository / "repo.git" / "worktrees" / "mine" / "commondir",
        names="/evil",
        expected=str(repository / "repo.git"),
    )
    message = refusal([drift])
    assert str(repository / "mine") in message
    assert "/evil" in message
    assert refusal([]) == ""


def test_the_worktree_lifecycle_refuses_a_redirected_pointer(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dev git worktree` operations refuse before host git enters a moved set.

    Run from a worktree beside its repository, which no path leads back to:
    the first run remembers the repository from the host, and the second
    finds the redirected `commondir` against it rather than the one it names.
    """
    from lup.devtools.dev import worktree

    common = common_of(repository)
    monkeypatch.chdir(repository / "mine")
    monkeypatch.setattr(worktree, "find_tree_dir", lambda: None)
    worktree.refuse_redirected_pointers()

    built = evil_gitdir(repository)
    (common / "worktrees" / "mine" / "commondir").write_text(
        f"{built}\n", encoding="utf-8"
    )
    with pytest.raises(typer.Exit):
        worktree.refuse_redirected_pointers()


def test_a_layout_with_no_tree_directory_is_a_silent_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain checkout has no sibling worktree to redirect, so nothing refuses.

    This is what lets the guard sit in front of every git-workflow command
    rather than only the worktree ones.
    """
    from lup.devtools.dev import worktree

    monkeypatch.chdir(tmp_path)
    worktree.refuse_redirected_pointers()


def test_the_git_command_tree_guards_every_subcommand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `dev git` app runs the pointer guard before any subcommand body.

    Wired as one callback rather than per command, so a command added later
    is guarded without anyone remembering to. The guard is stubbed to a
    refusal here: reaching it is the whole assertion, and a subcommand body
    that never runs needs no repository.
    """
    from typer.testing import CliRunner

    from lup.devtools.dev import worktree
    from lup.devtools.git.app import create_git_app

    def refuse() -> None:
        raise typer.Exit(7)

    monkeypatch.setattr(worktree, "refuse_redirected_pointers", refuse)
    app = create_git_app(declared=lambda: None)  # type: ignore[arg-type]
    result = CliRunner().invoke(app, ["worktree", "list"])
    assert result.exit_code == 7
    assert CliRunner().invoke(app, ["--help"]).exit_code == 0


def test_the_launcher_guards_pointers_on_the_way_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pointers are verified ahead of every step of the launch that runs host git.

    Settling base freshness, regenerating the trees, the preflight probes and
    status all run host git after the guard, so it precedes them. A
    generate-only invocation runs no steps and is not gated, since it opens
    no session.
    """
    from unittest.mock import Mock

    from lup.devtools.harness import launch
    from lup.harness.generate import NativeHarnessComposition

    composition = Mock(spec=NativeHarnessComposition)
    composition.recipe = Mock(root=Path("/work"))
    generation = launch.TreesGenerated(composition=composition)
    kinds = [type(step) for step in launch.workflow_steps("claude", generation, None)]

    assert kinds.index(launch.PointersVerified) < kinds.index(launch.BaseSettled)
    assert kinds.index(launch.BaseSettled) < kinds.index(launch.TreesGenerated)

    def refuse() -> None:
        raise typer.Exit(9)

    monkeypatch.setattr(launch, "refuse_redirected_pointers", refuse)
    with pytest.raises(typer.Exit):
        launch.PointersVerified().before()
