"""What landing a branch would actually do, asked of content rather than of commits.

Containment is decided by patch-id, and a rebase, a reword or a squash
changes the patch-id of work that already landed — so a branch whose every
change stands in the integration branch can still read as a stack of unique
commits, and a sweep taken before a rewrite presents its whole history as
work at risk. Subject matching narrows that and cannot settle it: a subject
is a claim about a change, not the change.

Content settles it. ``git merge-tree`` performs the merge in memory, touching
neither the index nor any working tree, and the tree it writes compared
against the integration branch's own is exactly what landing would change —
nothing at all for work that already landed under another identity, and
the conflicted files where it would not merge cleanly. Beside that, each
commit the patch-ids leave unmatched is paired with the integration commit
carrying its subject, and the two are compared by the lines they change, so
a rewrite reads as one and a divergence as the other.

Several branches at once are also compared with each other, by the files
each touched since it left the integration branch: that intersection is what
decides the order a sweep lands them in.
"""

import hashlib
from collections.abc import Iterator
from itertools import combinations

import sh
import typer
from pydantic import BaseModel, Field

from lup.devtools.utils import decode_stderr, output_json, short_sha
from lup.execution.shell import git

# lup: ignore[dict-str-payload] — subject → commit; subjects are open text
type SubjectTwins = dict[str, str]


class Logged(BaseModel, frozen=True):
    """One commit as ``git log`` names it."""

    commit: str
    subject: str


class UniqueCommit(BaseModel, frozen=True):
    """A commit no patch-id in the integration branch matches, and its likely twin."""

    commit: str
    subject: str
    twin: str = Field(
        default="",
        description="The integration commit carrying the same subject, if any",
    )
    same_lines: bool = Field(
        default=False,
        description="Whether the twin adds and removes exactly the same lines",
    )

    def reading(self) -> str:
        """How this commit reads against the integration branch."""
        if not self.twin:
            return "new"
        return "rewritten" if self.same_lines else "differs"


class FileChange(BaseModel, frozen=True):
    """One file landing the branch would change, with the lines it would move."""

    path: str
    added: int | None = Field(description="Lines added; None for a binary file")
    removed: int | None = Field(description="Lines removed; None for a binary file")

    def moved(self) -> str:
        """The change as a reader scans a diffstat."""
        if self.added is None or self.removed is None:
            return "binary"
        return f"+{self.added} -{self.removed}"


class Landing(BaseModel, frozen=True):
    """What merging one branch into the integration branch would do."""

    branch: str
    into: str
    matched: int = Field(
        description="Commits a patch-id in the integration branch already matches"
    )
    unique: list[UniqueCommit] = Field(default_factory=list[UniqueCommit])
    conflicts: list[str] = Field(
        default_factory=list[str], description="Files the in-memory merge conflicts on"
    )
    changes: list[FileChange] = Field(
        default_factory=list[FileChange],
        description="What the merged tree changes against the integration branch's",
    )
    touches: list[str] = Field(
        default_factory=list[str],
        description="Files the branch changed since it left the integration branch",
    )

    def verdict(self) -> str:
        """One phrase for the whole answer, in the order a reader decides by."""
        if self.conflicts:
            return f"conflicts in {len(self.conflicts)} file(s)"
        if not self.changes:
            return f"nothing new — {self.into} already holds every line"
        return f"merges cleanly, changing {len(self.changes)} file(s)"


class Overlap(BaseModel, frozen=True):
    """Two branches and the files both of them changed."""

    first: str
    second: str
    shared: list[str]


class Preview(BaseModel, frozen=True):
    """Every branch asked about, and how their changes meet each other."""

    into: str
    landings: list[Landing]
    overlaps: list[Overlap]


def changed_lines(commit: str) -> str:
    """A digest of the lines one commit adds and removes, context and headers aside.

    Two commits carrying one change through a rebase differ in their context
    lines, hunk offsets and index lines, and in nothing else; this compares
    the part that is the change.
    """
    patch = git.lines("show", "--format=", "--no-renames", "--no-color", commit)
    body = [
        line
        for line in patch
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    return hashlib.sha256("\n".join(body).encode()).hexdigest()


def logged(*selection: str) -> Iterator[Logged]:
    """Each commit ``git log`` selects, as its id and its subject."""
    for row in git.lines("log", "--format=%H%x00%s", *selection):
        # lup: ignore[string-split] — the NUL git's --format puts between the two fields
        commit, _, subject = row.partition("\x00")
        yield Logged(commit=commit, subject=subject)


def subjects_in(into: str) -> SubjectTwins:
    """The newest integration commit carrying each subject."""
    return {entry.subject: entry.commit for entry in reversed(list(logged(into)))}


def unique_commits(
    branch: str, into: str, twins: SubjectTwins
) -> Iterator[UniqueCommit]:
    """The branch's commits no patch-id matches, each paired with its subject twin."""
    unmatched = logged(
        "--cherry-pick", "--right-only", "--no-merges", f"{into}...{branch}"
    )
    for entry in unmatched:
        twin = twins[entry.subject] if entry.subject in twins else ""
        yield UniqueCommit(
            commit=entry.commit,
            subject=entry.subject,
            twin=twin,
            same_lines=bool(twin)
            and changed_lines(twin) == changed_lines(entry.commit),
        )


def numstat(base: str, tree: str) -> list[FileChange]:
    """Per-file line counts between two trees, a binary file's counted as unknown."""

    def count(field: str) -> int | None:
        return None if field == "-" else int(field)

    rows = git.lines("diff", "--numstat", "--no-renames", base, tree)
    # lup: ignore[string-split] — numstat's tab-separated columns
    fields = [row.split("\t", 2) for row in rows if row]
    return [
        FileChange(path=path, added=count(added), removed=count(removed))
        for added, removed, path in fields
    ]


def landing(branch: str, into: str, twins: SubjectTwins) -> Landing:
    """Merge one branch in memory and say what it would change."""
    merged = git.lines(
        "merge-tree",
        "--write-tree",
        "--name-only",
        "--no-messages",
        into,
        branch,
        _ok_code=[0, 1],
    )
    tree, conflicted = merged[0], [name for name in merged[1:] if name]
    unique = list(unique_commits(branch, into, twins))
    listed = git.out("rev-list", "--count", "--no-merges", f"{into}..{branch}")
    return Landing(
        branch=branch,
        into=into,
        matched=int(listed) - len(unique),
        unique=unique,
        conflicts=conflicted,
        changes=numstat(f"{into}^{{tree}}", tree),
        touches=git.lines("diff", "--name-only", "--no-renames", f"{into}...{branch}"),
    )


def preview(branches: list[str], into: str) -> Preview:
    """Every branch's landing, and the files each pair of them shares."""
    twins = subjects_in(into)
    landings = [landing(branch, into, twins) for branch in branches]
    return Preview(
        into=into,
        landings=landings,
        overlaps=[
            Overlap(first=a.branch, second=b.branch, shared=shared)
            for a, b in combinations(landings, 2)
            if (shared := [path for path in a.touches if path in b.touches])
        ],
    )


def show(result: Preview) -> None:
    """Print a preview for a reader deciding what to land, and in what order."""
    for item in result.landings:
        typer.echo(f"=== {item.branch} into {item.into}: {item.verdict()}")
        typer.echo(
            f"    {item.matched} commit(s) matched by patch-id, "
            f"{len(item.unique)} unmatched"
        )
        for commit in item.unique:
            twin = f" -> {short_sha(commit.twin)}" if commit.twin else ""
            typer.echo(
                f"      {commit.reading():<9} {short_sha(commit.commit)}{twin}"
                f"  {commit.subject}"
            )
        for path in item.conflicts:
            typer.echo(f"    conflict  {path}")
        for change in item.changes:
            typer.echo(f"    changes   {change.path} ({change.moved()})")
    for overlap in result.overlaps:
        typer.echo(
            f"=== {overlap.first} and {overlap.second} "
            f"share {len(overlap.shared)} file(s)"
        )
        for path in overlap.shared:
            typer.echo(f"    {path}")


def run_preview(branches: list[str], into: str, as_json: bool) -> None:
    """Preview each branch against ``into``, as JSON or for reading."""
    try:
        result = preview(branches, into)
    except sh.ErrorReturnCode as error:
        typer.echo(f"git refused the preview: {decode_stderr(error)}", err=True)
        raise typer.Exit(1) from error
    if as_json:
        output_json(result)
        return
    show(result)
