"""Tracing a symbol through this checkout's history, past its own safety net.

`git log --all -S <symbol>` is how a name's history is read, and in a
checkout this library guards it answers with something else entirely. Before
every command the permission dispatcher writes the working tree into a
snapshot under `refs/lup/undo` -- a commit with no parent, so every file in
it reads as an addition and the pickaxe matches every symbol the tree holds.
Measured on this repository: two real commits mention
`undo_retention_count`, and `--all` reports ninety-six, the first of them
being the snapshot taken in front of the search itself.

The snapshot subject is the command that triggered it, which makes the
listing look like it is matching commit messages. It is not -- a pickaxe over
text that appears only in a subject and in no file matches nothing at all.
So keeping the command out of the subject would cost the recovery listing
its one readable field and leave the burial exactly where it is. The
refs are excluded from the traversal instead, and every snapshot keeps
everything it recorded.

Excluding them is also worth a command rather than a remembered flag:
`--exclude` applies to the ref patterns that follow it, so `--all --exclude`
is silently the unfiltered search and `--exclude --all` is the filtered one.
Nothing reports the difference except the results.
"""

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from lup.devtools.dev.undo import UNDO_NAMESPACE
from lup.execution.shell import git

SNAPSHOT_REFS = f"{UNDO_NAMESPACE}/*"
"""The ref pattern kept out of the traversal, asked of the half that writes it.

Derived rather than restated, for the reason the listing derives it: a
namespace this module spelled for itself would stop excluding anything on
the day the writer moved, and an exclusion that excludes nothing looks
exactly like a history with nothing in it.
"""

# lup: ignore[library-default] — the format this asks git for and the fields
# the parser reads back are two halves of one protocol and must spell alike;
# a caller free to change one would silently break the other
HIT_FIELDS = ("%H", "%cI", "%an", "%s")
"""What each matching commit reports, in order, joined by a tab."""


class HistoryHit(BaseModel, frozen=True):
    """One commit whose content answered the search."""

    commit: str
    committed_at: datetime
    author: str
    subject: str = Field(
        description=(
            "The commit's own subject line, which is what a reader scans to "
            "decide which of the matches is the one they are looking for"
        )
    )

    def line(self) -> str:
        """One row of the listing: enough to choose a commit to open."""
        return (
            f"{self.commit[:9]}  {self.committed_at.date()}  "
            f"{self.author}  {self.subject}"
        )

    @classmethod
    def parse(cls, line: str) -> "HistoryHit | None":
        """One `log --format` line, or nothing where it is not one of ours."""
        # lup: ignore[string-split] — git emits the separator this call chose
        # and ships no parser for it; the bound keeps a tab in the subject
        # from being read as a fifth field
        fields = line.split("\t", 3)
        if len(fields) != len(HIT_FIELDS):
            return None
        return cls(
            commit=fields[0],
            committed_at=datetime.fromisoformat(fields[1]),
            author=fields[2],
            subject=fields[3],
        )


def commits_matching(
    root: Path,
    text: str,
    regex: bool = False,
    paths: list[Path] | None = None,
) -> list[HistoryHit]:
    """Every commit that changed how often *text* appears, newest first.

    Every ref this checkout carries except the snapshots, so a symbol is
    traced across branches that were never merged and across the ones that
    were. `--exclude` is spelled ahead of `--all` because that is the only
    order in which it excludes anything.

    ``regex`` reads *text* as a regular expression matched against the diff
    rather than as a literal whose occurrence count changed -- the difference
    between asking where a name was introduced and asking where a shape was
    touched.
    """
    listed = git.lines(
        "-C",
        str(root),
        "log",
        f"--exclude={SNAPSHOT_REFS}",
        "--all",
        f"--format={'%x09'.join(HIT_FIELDS)}",
        f"{'-G' if regex else '-S'}{text}",
        *(["--", *(str(path) for path in paths)] if paths else []),
        _ok_code=[0, 1],
    )
    return [
        parsed
        for line in listed
        if line
        for parsed in [HistoryHit.parse(line)]
        if parsed
    ]
