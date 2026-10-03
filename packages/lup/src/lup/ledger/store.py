# lup: ignore[constant-declaration]
# The directory names here are where every worktree of one repository writes
# one DAG. Two processes that spelled them differently would keep two
# ledgers, so they are an identity of this layout rather than a caller's
# choice.
"""Where one repository's notes live, and the decision a project makes about it per kind.

One log, two journals. Every kind of record a project declares is either
**committed** — its lines sit in a journal inside the worktree, travel with
commits, are reviewed in a diff, and are the same on every machine — or
**local**, under the git directory every worktree of one clone resolves to,
outside all of them, so a branch cannot change what a reader sees and
removing a worktree does not take the notes with it. The two journals are
one log: one id space, edges crossing kinds freely, and every reader folds
both. An edge is committed only where both of its ends are, so git never
carries a reference to a record it does not hold.

Local is the default and the whole answer to a ledger that forks — a copy per
worktree has to be reconciled, and a reconciliation that drops a modification
loses it without saying so. What makes the committed
half viable is the shape already chosen — append-only lines with unique ids —
so two branches appending is exactly what git's own ``union`` merge resolves
losslessly, and a read folds any duplicate by id.

The layout is a declaration in the project's code rather than a path a
worktree could be configured with, so two checkouts of one branch cannot be
set differently: the code that says where each kind goes travels with the
tree that reads it. :mod:`lup.coordination` keeps its roster under the git
directory whichever is chosen, because a roster is about the live sessions
on this machine and nothing else.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, Self

import sh
from pydantic import BaseModel, ImportString, model_validator

from lup.execution.shell import git
from lup.ledger.kinds import kind_of
from lup.ledger.models import LedgerNode, Placement
from lup.workspace.edition import shared_git_directory

STORE_DIR = "lup"
LEDGER_DIR = "ledger"
JOURNAL_FILE = "journal.jsonl"
BLOBS_DIR = "blobs"


class LedgerPlacement(BaseModel, ABC, frozen=True):
    """Where one half of a repository's log is kept; each placement answers for its consequences."""

    kind: str

    @abstractmethod
    def root(self, project: Path) -> Path:
        """The directory this half's journal and blobs live under, given the tree."""

    @abstractmethod
    def problems(self, project: Path) -> list[str]:
        """What this checkout lacks for the placement to hold, in the words to fix it."""

    @abstractmethod
    def describe(self) -> str:
        """Where this half is, as the gate's row reads it."""


class SharedStore(LedgerPlacement, frozen=True):
    """Under the git directory every worktree shares: one copy, and no seam to get wrong."""

    kind: Literal["shared"] = "shared"

    def root(self, project: Path) -> Path:
        return shared_git_directory(project) / STORE_DIR / LEDGER_DIR

    def problems(self, project: Path) -> list[str]:
        del project
        return []

    def describe(self) -> str:
        return "shared under the git directory"


class InTree(LedgerPlacement, frozen=True):
    """Inside the worktree, committed with the code and merged by union.

    ``path`` is relative to the working tree. The journal has to be declared
    ``merge=union`` in ``.gitattributes`` — git's own driver, needing no
    per-clone registration — so two branches that both appended keep both
    sides; :meth:`problems` says so where it is not. A forge merging on its
    server reads no attributes, so there the same two branches show a
    conflict, resolved by taking both sides. Blobs are content-addressed and
    never conflict.
    """

    kind: Literal["in-tree"] = "in-tree"
    path: Path = Path(LEDGER_DIR)

    def root(self, project: Path) -> Path:
        return project / self.path

    def journal(self) -> Path:
        """The journal's path relative to the tree, which the attribute names."""
        return self.path / JOURNAL_FILE

    def problems(self, project: Path) -> list[str]:
        spelled = self.journal().as_posix()
        if merges_by_union(project, self.journal()):
            return []
        return [
            f"{spelled} is not declared `merge=union`; add "
            f'"{spelled} merge=union" to .gitattributes, so two branches that '
            "both appended merge losslessly"
        ]

    def describe(self) -> str:
        return f"in the tree at {self.path.as_posix()}/, merged by union"


class LedgerLayout(BaseModel, frozen=True):
    """One log in two journals, and which of a project's kinds go to which.

    ``committed`` is the half git carries, or none; ``local`` the half under
    the git directory. ``placements`` says for each declared kind which half
    its records are written to, and a kind it does not name is local — so a
    project that never declares a committed kind keeps one journal under the
    git directory and nothing else changes for it. A kind placed committed
    with no committed half to go to is refused here, at declaration, rather
    than at the first record.
    """

    committed: InTree | None = None
    local: SharedStore = SharedStore()
    placements: dict[ImportString[type[LedgerNode]], Placement] = {}
    """Where each kind goes, keyed by its class — an import path once serialized,
    so a layout crosses a process boundary as the value it is."""

    @model_validator(mode="after")
    def committed_kinds_have_somewhere_to_go(self) -> Self:
        stranded = [
            kind_of(declared)
            for declared, where in self.placements.items()
            if where == "committed"
        ]
        if stranded and self.committed is None:
            raise ValueError(
                f"{', '.join(stranded)} placed committed with no committed half;"
                " declare `committed=InTree()` or place them local"
            )
        return self

    def placement(self, kind: str) -> Placement:
        """Which half records of one kind are written to: local unless declared committed."""
        by_kind: dict[str, Placement] = {
            kind_of(declared): where for declared, where in self.placements.items()
        }
        return by_kind.get(kind, "local")

    def placement_of(self, kinds: list[str]) -> Placement:
        """Where a record decided by these kinds is written.

        Committed only where every one of them is, which for a node is its
        own kind and for an edge is both of its ends' — so an edge touching
        a local node stays local, and git never carries a reference to a
        record it does not hold. No kind at all is a record the log cannot
        place, and that goes local too.
        """
        return "committed" if kinds and self.tracked(kinds) else "local"

    def tracked(self, kinds: Iterable[str]) -> bool:
        """Whether records of every one of these kinds are the same on every machine.

        What two things downstream turn on: a document rendered from these
        kinds can be drift-checked only where every machine renders the same
        one, and a snapshot branch is worth taking only over what nothing
        else keeps. No kinds at all are trivially the same everywhere.
        """
        return all(self.placement(kind) == "committed" for kind in kinds)

    def roots(self, project: Path) -> dict[Placement, Path]:
        """The directory each half's journal and blobs live under, given the tree.

        The committed half first where there is one, so a fold that breaks a
        tie by position reads the local copy as the later.
        """
        halves: dict[Placement, Path] = {}
        if self.committed is not None:
            halves["committed"] = self.committed.root(project)
        halves["local"] = self.local.root(project)
        return halves

    def problems(self, project: Path) -> list[str]:
        """What this checkout lacks for the layout to hold; only the committed half can lack anything."""
        return self.committed.problems(project) if self.committed is not None else []

    def describe(self) -> str:
        """Where each half is, as the gate's row reads it."""
        if self.committed is None:
            return f"every kind {self.local.describe()}"
        return (
            f"committed kinds {self.committed.describe()};"
            f" local kinds {self.local.describe()}"
        )


def merges_by_union(project: Path, path: Path) -> bool:
    """Whether git resolves the ``union`` merge driver for one path in this checkout.

    Asked of git rather than read from ``.gitattributes``, because git is the
    reader whose answer matters and more than one file in a tree may set it.
    ``-z`` frames the answer as NUL-terminated fields — path, attribute, value
    — so the one answer that satisfies is a whole string to compare against,
    and nothing is parsed. Outside a repository there is no answer, which
    reads as undeclared.
    """
    try:
        framed = str(
            git("check-attr", "-z", "merge", "--", path.as_posix(), _cwd=str(project))
        )
    except sh.ErrorReturnCode:
        return False
    return framed == f"{path.as_posix()}\0merge\0union\0"
