"""How this repository's own commits reach a repository built on it.

A scaffold ships through three mechanisms and they differ only in who moves
the change. Imported code arrives by a dependency bump, generated files by a
regeneration, and copied files only when somebody reads upstream commits and
ports them by hand. Which mechanism carried a commit is not a property anybody
writes down — it is decided entirely by which paths the commit touched, so it
can be measured rather than tracked.

The row that matters is neither of the clean ones. A commit touching the
library *and* the copied tree at once is carried half way: the bump lands the
library side and leaves the call site behind, so the adopter receives a
library whose callers are now wrong. That is not a missed review, and reading
commits more carefully does not reduce it — only moving code out of the copied
tree does. Its count is what says whether that is working.

Measured from paths rather than from a checkout of any adopter, so the answer
costs one repository and is the same for every project built on this one.
"""

from pathlib import Path
from typing import Literal

import typer
from pydantic import BaseModel

from lup.execution.shell import git

Carrier = Literal["imported", "generated", "copied", "split", "neither"]


class CarrierNote(BaseModel, frozen=True):
    """One mechanism, and what a reader of the tally needs it to mean."""

    carrier: Carrier
    note: str


CARRIER_NOTES = [
    CarrierNote(carrier="imported", note="a dependency bump alone"),
    CarrierNote(carrier="generated", note="a regeneration alone"),
    CarrierNote(carrier="copied", note="a hand-port alone"),
    CarrierNote(carrier="split", note="a bump that lands half of it"),
    CarrierNote(carrier="neither", note="nothing — it reaches no adopter"),
]
"""Every carrier and what it costs the adopter, in the order a report reads.

Listed rather than derived from the ``Literal`` so the order is the one a
reader wants — free first, then the two that cost somebody an afternoon — and
so a carrier cannot be added without saying what it means for the reader.

Reachable as the default of :func:`report`, so a project shipping through a
mechanism lup has no word for replaces the whole list rather than forking the
module that prints it.
"""


class Spread(BaseModel, frozen=True):
    """Which trees a repository ships from, as prefixes a commit is matched on."""

    library: list[str]
    """Where imported code lives. Empty in a project that only consumes one."""

    copied: list[str]
    """Trees stamped out once and owned by the adopter from then on."""

    generated: list[str]
    """Trees compiled from a declaration, which a regeneration overwrites."""


class Reached(BaseModel, frozen=True):
    """How many commits each mechanism carried, over one window."""

    window: str
    counted: dict[Carrier, int]

    def hand_carried(self) -> int:
        """Commits needing a person, whether or not a bump carried the rest."""
        return self.counted["copied"] + self.counted["split"]


class ModuleCost(BaseModel, frozen=True):
    """What one copied file costs every adopter: the changes they must port."""

    path: str
    commits: int
    lines: int


def touching(window: str, paths: list[str]) -> list[str]:
    """Every commit in the window that touched any of these paths, by hash.

    Hashes rather than a parsed log: the carriers below are read off the
    overlaps between three of these calls, and asking git once per tree is
    what keeps this free of a parser. No line of the output needs splitting,
    because the hash is the whole of each line.

    Filtering by path simplifies history, so a merge that introduced no change
    of its own is absent from every one of these. That is the answer this
    wants: the question is what work an adopter has to carry, and a merge
    carries what its parents already did.
    """
    if not paths:
        return []
    return git.lines("log", f"--since={window}", "--format=%H", "--", *paths)


def carried(window: str, spread: Spread) -> Reached:
    """Tally one window of history by the mechanism that carries each commit."""
    library = {sha for sha in touching(window, spread.library)}
    copied = {sha for sha in touching(window, spread.copied)}
    generated = {sha for sha in touching(window, spread.generated)}
    every = {sha for sha in touching(window, ["."])}
    return Reached(
        window=window,
        counted={
            "imported": len(library - copied),
            "generated": len(generated - library - copied),
            "copied": len(copied - library),
            "split": len(library & copied),
            "neither": len(every - library - copied - generated),
        },
    )


def module_costs(window: str, spread: Spread, root: Path) -> list[ModuleCost]:
    """Every tracked Python file in the copied trees, costliest first.

    Costed in commits rather than lines: an adopter pays for changes to what
    they copied, not for its size. A large file nobody touches is free however
    misplaced it is, and the ranking has to say so.
    """
    tracked = git.lines("ls-files", "--", *spread.copied) if spread.copied else []
    costs = [
        ModuleCost(
            path=path,
            commits=len(touching(window, [path])),
            lines=len((root / path).read_text().splitlines()),
        )
        for path in tracked
        if path.endswith(".py") and (root / path).is_file()
    ]
    return sorted(costs, key=lambda cost: -cost.commits)


def report(
    window: str,
    spread: Spread,
    root: Path,
    limit: int,
    notes: list[CarrierNote] = CARRIER_NOTES,
) -> None:
    """Print how the window's work reached an adopter, and what it cost them."""
    # lup: defer: this only reports; the user's success measure was the split
    # count per year trending to zero and copied-half churn staying under a
    # ceiling, as a gate -- which needs a threshold nobody has chosen. Watch
    # `harness/catalog.py` in the costliest list too: moving composition into
    # the library is wrong if it only relocates the churn there
    reached = carried(window, spread)
    total = sum(reached.counted.values())
    typer.echo(f"{total} commits since {window}, by what carries each one:")
    for row in notes:
        typer.echo(f"  {reached.counted[row.carrier]:>6}  {row.carrier:<11} {row.note}")
    typer.echo(
        f"\n  {reached.hand_carried()} need a person; "
        f"{reached.counted['imported']} arrive free."
    )

    costs = module_costs(window, spread, root)
    spent = sum(cost.commits for cost in costs)
    listed = min(limit, len(costs))
    typer.echo(
        f"\n{len(costs)} copied module(s), {sum(cost.lines for cost in costs)} lines, "
        f"{spent} touches — costliest {listed}:"
    )
    typer.echo(f"  {'commits':>7} {'lines':>6}  file")
    for cost in costs[:limit]:
        typer.echo(f"  {cost.commits:>7} {cost.lines:>6}  {cost.path}")
    if spent:
        shown = sum(cost.commits for cost in costs[:limit])
        typer.echo(f"\n  those {listed} carry {shown / spent:.0%} of it.")
