"""Keep a worktree's session evidence when the worktree itself is going away.

The notes directory is ignored unless a repository opts into committing session
data, so a worktree routinely carries the only copy of what its sessions did.
Deleting the worktree destroys it, and a feedback loop reading what remains sees
sessions that never happened rather than evidence that was thrown away -- the
one failure shape that cannot be noticed after the fact.

So the archive runs from ``delete_branch`` rather than from a step someone
remembers: the loss happens at deletion, which is where the guard belongs. A
deletion whose archive fails is refused, because continuing would produce the
exact outcome being guarded against.

Everything under that directory is copied, the harness journal mirror included.
The mirror reads as a derived artifact whose source is safe -- it mirrors what a
native CLI writes under its own configuration home -- and that holds because
the home is outside the checkout: the operator's own, or a profile kept beside
their lup config, so the record a mirror copies survives the deletion.

The profile home itself is not archived. It belongs to the person rather than
to the branch being deleted -- one home serves sessions run from every checkout
-- and it holds the account's credentials beside its transcripts.

The archive sits beside the repository's common directory rather than inside a
worktree, for the reason the whole module exists: an archive kept inside a
worktree dies with the worktree it was meant to outlive. Being outside every
checkout also puts it beyond any commit, which matters wherever a commit loop
stages a notes directory unattended.
"""

import logging
import shutil
from pathlib import Path

import typer
from pydantic import BaseModel

from lup.devtools.utils import output_json
from lup.diagnostics import refuse
from lup.execution.git import GitError, Repository
from lup.workspace.paths import notes_path, project_root
from lup.workspace.shared_directory import ARCHIVE_DIRECTORY_NAME

logger = logging.getLogger(__name__)


class ArchivedTraces(BaseModel):
    """What one archive pass found, and what it wrote.

    ``copied`` and ``present`` are separate because a second pass over the same
    worktree is expected: a sweep may archive, stop for a decision, and archive
    again. Reporting the whole tree as copied each time would say the archive
    grew when it did not.
    """

    branch: str
    worktree: str | None
    source: str | None
    destination: str
    copied: int
    present: int
    bytes_copied: int
    dry_run: bool

    def summary(self) -> str:
        """One line saying what this pass did, for a caller that reports."""
        if self.copied == 0 and self.present == 0:
            return f"{self.branch}: no traces to archive"
        verb = "would copy" if self.dry_run else "archived"
        return (
            f"{self.branch}: {verb} {self.copied} file(s), "
            f"{self.bytes_copied} bytes, {self.present} already archived "
            f"-> {self.destination}"
        )


def archive_root(name: str = ARCHIVE_DIRECTORY_NAME) -> Path:
    """Where archives live: inside the common directory, outside every worktree.

    ``--git-common-dir`` is the shared directory every worktree points at, so it
    names the one location all of them agree on and none of them can take with
    it. Inside rather than beside, because that lands correctly in both layouts
    a repository can have: a bare root with worktrees under it puts the archive
    at the top, next to them, and an ordinary checkout puts it within ``.git``
    -- where no worktree removal reaches it and no commit can pick it up.
    """
    try:
        common = Repository(Path.cwd()).common_dir()
    except GitError as error:
        logger.exception("Could not locate the repository's common directory")
        refuse(
            f"could not locate the repository's common directory: {error.status.stderr.strip()}"
        )
    return common / name


def notes_within(worktree: Path) -> Path:
    """The notes directory of another worktree, by this project's own layout.

    The relative segment is taken from the running configuration rather than
    hardcoded, so a tree that moved its notes directory archives from the place
    it actually writes.

    The whole directory rather than one store inside it, so a session record
    this module was never told about is kept by sitting where the others do.
    Naming the stores would make each new one a line somebody has to remember,
    and the failure that omission produces is the silent one this module exists
    to prevent.
    """
    return worktree / notes_path().relative_to(project_root())


def archive(branch: str, dry_run: bool) -> ArchivedTraces:
    """Copy one worktree's session records into the archive, skipping what is there.

    Files already archived are left untouched rather than overwritten. A record
    is append-only evidence of a finished session, so a byte that differs from
    its archived copy is corruption somewhere, and silently replacing the
    archived one would destroy the better of the two.

    A branch with no worktree reports nothing to archive rather than failing:
    the caller is on its way to deleting a branch, and a branch never checked
    out anywhere wrote nothing to lose.
    """
    # Imported here because the deletion path archives before it deletes, and a
    # module-level import in both directions is a cycle.
    from lup.devtools.dev.branches import get_branch_worktree

    destination = archive_root() / branch
    located = get_branch_worktree(branch)
    if located is None:
        return ArchivedTraces(
            branch=branch,
            worktree=None,
            source=None,
            destination=str(destination),
            copied=0,
            present=0,
            bytes_copied=0,
            dry_run=dry_run,
        )

    worktree = Path(located)
    source = notes_within(worktree)
    originals = (
        [path for path in sorted(source.rglob("*")) if path.is_file()]
        if source.is_dir()
        else []
    )
    pending = [
        path
        for path in originals
        if not (destination / path.relative_to(source)).is_file()
    ]
    if not dry_run:
        for path in pending:
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

    return ArchivedTraces(
        branch=branch,
        worktree=str(worktree),
        source=str(source),
        destination=str(destination),
        copied=len(pending),
        present=len(originals) - len(pending),
        bytes_copied=sum(path.stat().st_size for path in pending),
        dry_run=dry_run,
    )


def keep_before_deleting(branch: str) -> None:
    """Archive one branch's traces, refusing the deletion if that fails.

    Called from the deletion path rather than offered as a step to remember.
    An archive that cannot be written is reported as a refusal, because letting
    the deletion continue would produce exactly the loss this prevents.
    """
    try:
        result = archive(branch, dry_run=False)
    except OSError as error:
        logger.exception("Archiving traces failed")
        refuse(
            "is not deleted: its traces could not be archived, and removing the "
            f"worktree would destroy them: {error}",
            what=branch,
        )
    if result.copied:
        typer.echo(result.summary())


def report(branch: str, dry_run: bool, as_json: bool) -> None:
    """Archive one branch's traces and say what happened."""
    result = archive(branch, dry_run)
    if as_json:
        output_json(result)
        return
    typer.echo(result.summary())
