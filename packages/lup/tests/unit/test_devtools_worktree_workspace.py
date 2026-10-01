"""A bun workspace the new tree has no copy of is not a restore to attempt.

The workspaces a creation restores are read from the project the command was
run in, while `--base` cuts the tree from any branch — so the two can disagree
about the layout. `worktree create -b dev`, run from a checkout that vendors
the library, can cut a tree from a branch that obtains it as a link instead,
and a step looking for `packages/lup/web` there finds a tree that holds no
`packages/` at all. `bun` handed a working directory that does not exist is
reported by `sh` raising on the `chdir` rather than on the command; that is
neither the failed exit `restore_dependencies` converts nor the `RuntimeError`
the step catches, so it would escape every handler and take down a creation
whose `required()` is already `False` — a worktree and environment already on
disk, with nothing left to want.
"""

from pathlib import Path

from lup.devtools.dev.worktree import RestoredWorkspace


def test_a_workspace_absent_from_the_new_tree_is_nothing_to_restore(
    tmp_path: Path,
) -> None:
    """A declared workspace the tree does not hold leaves the step done.

    Not "done" as a convenience: there is no `package.json` and no lockfile
    there, so nothing describes dependencies that could be behind anything.
    """
    worktree = tmp_path / "tree"
    worktree.mkdir()
    step = RestoredWorkspace(worktree=worktree, workspace=Path("packages/lup/web"))
    assert step.satisfied()


def test_a_workspace_the_tree_holds_is_still_restored_when_behind(
    tmp_path: Path,
) -> None:
    """The step it exists for: a workspace present, its dependencies absent.

    Guards the step against answering "satisfied" for every workspace, which
    would read the same on a creation that skipped the restore it owed.
    """
    worktree = tmp_path / "tree"
    workspace = worktree / "packages" / "lup" / "web"
    workspace.mkdir(parents=True)
    (workspace / "bun.lock").write_text("", encoding="utf-8")
    step = RestoredWorkspace(worktree=worktree, workspace=Path("packages/lup/web"))
    assert not step.satisfied()
