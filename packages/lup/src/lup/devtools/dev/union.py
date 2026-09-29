"""Settling a conflict both sides made by inserting at the same place.

The commonest conflict in a repository that declares things in lists is two
branches each adding an entry at the head of the same one — a changelog line,
a registry row. Git reports it as a conflict because both
changes touch one position, and the resolution is always the same: keep both
entries. Taking either side by hand drops the other's entry, which is the
silent loss the merge guidance warns about, and retyping the union is where a
line goes missing.

So the three index stages are merged again here, line by line, with one rule
git does not have: two insertions at the same base position are combined
rather than refused. Everything else that git would call a conflict — two
sides changing the same base lines, or one inserting inside a span the other
rewrote — is still refused, naming where, because no rule settles it without
reading what each side meant.
"""

import difflib
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import sh
import typer
from pydantic import BaseModel, Field

from lup.devtools.utils import decode_stderr, output_json
from lup.execution.shell import git

Side = Literal["ours", "theirs"]

# lup: ignore[library-default] — git's own index stage numbers for a
# conflicted path, which no project chooses
STAGES: dict[Literal["base", "ours", "theirs"], int] = {
    "base": 1,
    "ours": 2,
    "theirs": 3,
}
"""Which index stage holds each version of a conflicted file.

Git's own numbering, the same in a merge, a rebase and a cherry-pick; what
differs between them is only which branch a rebase calls ``ours``.
"""


class Edit(BaseModel, frozen=True):
    """One side's change to a span of base lines: what it put there instead.

    ``start == stop`` is a pure insertion before base line ``start``.
    """

    side: Side
    start: int
    stop: int
    lines: list[str]

    def inserts(self) -> bool:
        """Whether this edit only adds lines, replacing nothing of the base."""
        return self.start == self.stop

    def span(self) -> str:
        """The base lines this edit touches, counted from 1 as a reader counts."""
        return f"{self.start + 1}-{max(self.stop, self.start + 1)}"

    def repeats(self, other: "Edit") -> bool:
        """Whether this is the same change the other side made."""
        return (self.start, self.stop, self.lines) == (
            other.start,
            other.stop,
            other.lines,
        )


class Combined(BaseModel, frozen=True):
    """A base position both sides inserted at, and how much each put there."""

    line: int = Field(description="The base line both insertions sit before, from 1")
    first: Side = Field(description="The side whose lines come first")
    ours: int = Field(description="Lines ours inserted there")
    theirs: int = Field(description="Lines theirs inserted there")


class Clash(BaseModel, frozen=True):
    """Two edits no union settles: both sides touched the same base lines."""

    ours: str = Field(description="The base lines ours changed, as `first-last`")
    theirs: str = Field(description="The base lines theirs changed, as `first-last`")


class Step(BaseModel, frozen=True):
    """One edit laid down in turn: the lines it places, and what it reports."""

    lines: list[str] = Field(default_factory=list[str])
    combined: Combined | None = None
    clash: Clash | None = None


class UnionResult(BaseModel, frozen=True):
    """What merging the three versions with the union rule produced."""

    path: str
    merged: list[str] = Field(default_factory=list[str], exclude=True)
    combined: list[Combined] = Field(default_factory=list[Combined])
    clashes: list[Clash] = Field(default_factory=list[Clash])

    def settled(self) -> bool:
        """Whether every conflicting edit was one the union rule settles."""
        return not self.clashes


def edits_of(side: Side, base: list[str], version: list[str]) -> list[Edit]:
    """Every span one side changed relative to the base, in base order."""
    matcher = difflib.SequenceMatcher(a=base, b=version, autojunk=False)
    return [
        Edit(side=side, start=start, stop=stop, lines=version[low:high])
        for tag, start, stop, low, high in matcher.get_opcodes()
        if tag != "equal"
    ]


def clash_between(applied: Edit, edit: Edit) -> Clash:
    """The two spans that collided, each under the side that changed it."""
    match edit.side:
        case "theirs":
            return Clash(ours=applied.span(), theirs=edit.span())
        case "ours":
            return Clash(ours=edit.span(), theirs=applied.span())


def union_merge(
    path: str,
    base: list[str],
    ours: list[str],
    theirs: list[str],
    first: Side = "ours",
) -> UnionResult:
    """Merge two versions of a file over their base, combining same-place insertions.

    Edits from the two sides are laid out in base order and applied in turn.
    At one base position an insertion goes before a replacement starting
    there, and two insertions go ``first``'s before the other's. An edit
    both sides made identically is applied once, which is how git treats it
    too. An edit starting inside a span the other side already rewrote is a
    clash, and the result then carries no merged text.
    """
    rank: dict[Side, int] = {first: 0, "theirs" if first == "ours" else "ours": 1}
    laid = sorted(
        [*edits_of("ours", base, ours), *edits_of("theirs", base, theirs)],
        key=lambda edit: (edit.start, not edit.inserts(), rank[edit.side]),
    )

    def combined(applied: Edit, edit: Edit) -> Combined | None:
        """Both sides' insertions at one position, where that is what met."""
        if applied.side == edit.side or not (applied.inserts() and edit.inserts()):
            return None
        if applied.start != edit.start:
            return None
        counts: dict[Side, int] = {
            applied.side: len(applied.lines),
            edit.side: len(edit.lines),
        }
        return Combined(
            line=edit.start + 1,
            first=applied.side,
            ours=counts["ours"],
            theirs=counts["theirs"],
        )

    def walk() -> Iterator[Step]:
        cursor = 0
        applied: Edit | None = None
        for edit in laid:
            if applied is not None and edit.repeats(applied):
                continue
            if applied is not None and edit.start < cursor:
                yield Step(clash=clash_between(applied, edit))
                continue
            yield Step(
                lines=[*base[cursor : edit.start], *edit.lines],
                combined=None if applied is None else combined(applied, edit),
            )
            cursor = edit.stop
            applied = edit
        yield Step(lines=base[cursor:])

    steps = list(walk())
    clashes = [step.clash for step in steps if step.clash is not None]
    return UnionResult(
        path=path,
        merged=[] if clashes else [line for step in steps for line in step.lines],
        combined=[step.combined for step in steps if step.combined is not None],
        clashes=clashes,
    )


def staged(path: str, stage: int) -> list[str]:
    """One index stage of a conflicted file, line endings kept."""
    return str(git("show", f":{stage}:{path}")).splitlines(keepends=True)


def union_file(path: Path, first: Side, dry_run: bool) -> UnionResult:
    """Merge one conflicted file from its stages and, unless dry, write and stage it."""
    root = Path(git.out("rev-parse", "--show-toplevel"))
    named = path.resolve().relative_to(root).as_posix()
    try:
        versions = {name: staged(named, stage) for name, stage in STAGES.items()}
    except sh.ErrorReturnCode as error:
        typer.echo(
            f"{named}: not a conflict holding all three stages — a union needs "
            f"the base and both sides ({decode_stderr(error)})",
            err=True,
        )
        raise typer.Exit(1) from error
    result = union_merge(
        named, versions["base"], versions["ours"], versions["theirs"], first
    )
    if result.settled() and not dry_run:
        (root / named).write_text("".join(result.merged), encoding="utf-8", newline="")
        git("add", "--", str(root / named))
    return result


def conflict_union(
    paths: list[Path], first: Side, dry_run: bool, as_json: bool
) -> None:
    """Settle each file whose conflict is same-place insertions, and report it."""
    results = [union_file(path, first, dry_run) for path in paths]
    if as_json:
        output_json([result.model_dump() for result in results])
    else:
        verb = "would combine" if dry_run else "combined"
        for result in results:
            if not result.settled():
                typer.echo(f"{result.path}: left as git left it — both sides changed:")
                for clash in result.clashes:
                    typer.echo(
                        f"    base lines {clash.ours} (ours) and {clash.theirs} (theirs)"
                    )
                continue
            if not result.combined:
                typer.echo(f"{result.path}: nothing to combine; merged as git would")
            for block in result.combined:
                typer.echo(
                    f"{result.path}: {verb} {block.ours} line(s) of ours and "
                    f"{block.theirs} of theirs before base line {block.line}, "
                    f"{block.first} first"
                )
    if not all(result.settled() for result in results):
        raise typer.Exit(1)
