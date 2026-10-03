"""Checkout layout: where a repository keeps its sibling worktrees.

Both the worktree workflows and the native launchers resolve the same
``tree/`` directory — the workflows to place a new checkout, the launchers
to widen a sandbox write root over it — so the layout is a fact of its own
rather than a detail of either caller.
"""

from pathlib import Path

from lup.diagnostics import refuse
from lup.workspace.paths import worktrees_directory


def find_tree_dir() -> Path | None:
    """Locate the ``tree/`` directory that holds sibling worktrees, or ``None``.

    Asked from wherever the command runs, the way
    :func:`~lup.workspace.paths.worktrees_directory` answers it for any
    start. ``None`` where the layout does not hold, so a caller that only
    wants the layout when there is one -- a guard over sibling worktrees that
    have none to guard -- reads the absence rather than a raised exit meant
    for a command the layout is a precondition of.
    """
    return worktrees_directory(Path.cwd())


def get_tree_dir() -> Path:
    """The ``tree/`` directory, or an exit for a command that requires one."""
    if tree := find_tree_dir():
        return tree
    refuse(
        "no tree/ directory holds this checkout or sits above it, and this command "
        "keeps sibling worktrees there"
    )
