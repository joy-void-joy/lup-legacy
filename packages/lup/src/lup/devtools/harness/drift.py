"""Console drift reporting and reported generation for generate and check.

Wraps the ``generate`` engine in the console surfaces the CLI shares: drift
summaries, generation summaries, and conflict-aborted generation. Owns the
bodies of the ``generate`` and ``check`` commands, and reaches the rule
reference alongside them so one command settles every generated artifact
rather than leaving the repository-wide one to be remembered separately.

:class:`DriftVerdict` is the single reading every refusing path shares: the
commit hook, continuous integration, and ``dev check`` all ask
:func:`inspect_drift` rather than composing the same two halves themselves,
so no tree can be stale to one of them and current to another.
"""

import shutil
from typing import NoReturn, Protocol, runtime_checkable
from pathlib import Path

import typer
from pydantic import BaseModel

from lup.formats.banner import REGENERATE_COMMAND
from lup.providers.profile_tree import profile_directory
from lup.harness.generate import (
    DeclarationObstruction,
    DriftReport,
    HarnessGenerationConflict,
    NativeHarnessComposition,
    generate as generate_target,
    inspect_generation,
    obstruction_at,
)


@runtime_checkable
class RepositoryWriter(Protocol):
    """One project-owned generated artifact outside a native tree.

    Runtime-checkable so a declaration can carry a list of these: the roster's
    bundle is a model, and pydantic validates a named type by asking whether a
    value is one. For a callback protocol that question is whether the value is
    callable, which is as much as any caller here ever needed to know.
    """

    def __call__(self, root: Path | None = None, *, check: bool = False) -> Path: ...


def refuse_generation(obstruction: DeclarationObstruction) -> NoReturn:
    """Refuse the run over a declaration that would not compile, saying which.

    Every path that compiles one refuses here rather than letting what raised
    travel out as a traceback. The commit guard and ``dev update`` both reach
    generation as a subprocess whose whole output is what their reader gets,
    and an interpreter frame stack is not a fact about any declaration: it
    names the reader that happened to open the file rather than the file that
    was wrong. Nothing has been written when this refuses — the compile is
    what failed, before any proposal existed to materialize.
    """
    for line in obstruction.described():
        typer.echo(line, err=True)
    typer.echo(f"Fix the declaration above, then run `{REGENERATE_COMMAND}`.", err=True)
    raise typer.Exit(1)


def report_generation(target: str, changed: list[Path], removed: list[Path]) -> None:
    typer.echo(
        f"{target} harness ready: {len(changed)} changed, {len(removed)} removed"
    )


def ownership_state(report: DriftReport) -> str:
    """How the proof stands: absent, present but behind, or current."""
    if not report.ownership_present:
        return "missing"
    return "present" if report.manifest_current else "stale"


def report_drift(report: DriftReport, *, paths: bool = False) -> None:
    proposal = report.proposal
    typer.echo(
        f"{report.target}: {len(proposal.writes)} writes, "
        f"{len(proposal.deletes)} deletes, {len(proposal.conflicts)} conflicts, "
        f"ownership={ownership_state(report)}"
    )
    for conflict in proposal.conflicts:
        label = "sensitive local conflict" if conflict.sensitive else conflict.category
        typer.echo(f"  {conflict.path}: {label}")
    if paths:
        for write in proposal.writes:
            typer.echo(f"  + {write.artifact.path}")
        for delete in proposal.deletes:
            typer.echo(f"  - {delete.path}")


def settled(report: DriftReport) -> bool:
    """Whether this tree is already what its source compiles to, proof included."""
    proposal = report.proposal
    return (
        not (proposal.writes or proposal.deletes or proposal.conflicts)
        and ownership_state(report) == "present"
    )


def write_machine_overlay(composition: NativeHarnessComposition) -> None:
    """Render this machine's overlay beside the composition's tree, replacing the last.

    Named from the profiles this machine keeps, the checkout's own and the
    person's, as a launch resolves one. The directory is the overlay's alone,
    so it is rewritten whole: a profile removed since leaves no name behind.
    """
    overlay = composition.overlay
    if overlay is None:
        return
    root = composition.recipe.root
    profiles = sorted(
        {
            profile.name
            for profile in profile_directory(composition.login, checkout=root).entries()
        }
    )
    directory = root / overlay.directory
    if directory.exists():
        shutil.rmtree(directory)
    for artifact in overlay.render(profiles).artifacts:
        target = root / artifact.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(artifact.content, encoding="utf-8")


def generate_with_report(
    composition: NativeHarnessComposition, in_passing: bool = False
) -> None:
    """Generate one composition's owned artifacts, reporting drift and results.

    ``in_passing`` is generation reached on the way to something else -- a
    launch, which regenerates before it opens a session and is not the thing
    anybody typed. Then a tree that was already current says nothing, because
    `0 writes, 0 deletes, 0 conflicts` followed by `0 changed, 0 removed` is
    two lines reporting that the command did what it always does, at the top
    of the block where a reader is looking for the line that is different
    today. A tree that moved still says so under either.
    """
    recipe = composition.recipe
    report = inspect_generation(recipe)
    quiet = in_passing and settled(report)
    if not quiet:
        report_drift(report, paths=True)
    try:
        materialized = generate_target(recipe)
    except HarnessGenerationConflict as error:
        typer.echo(str(error), err=True)
        typer.echo(
            "Existing unowned files were preserved. Reconcile them explicitly before "
            "adopting generated ownership.",
            err=True,
        )
        raise typer.Exit(1) from error
    if not (quiet and not materialized.changed and not materialized.removed):
        report_generation(recipe.label, materialized.changed, materialized.removed)
    write_machine_overlay(composition)


def repository_staleness(write: RepositoryWriter) -> list[str]:
    """Why one generated file outside the native trees is behind, if it is.

    A writer says "behind" by raising ``RuntimeError``, which is a reading and
    not a failure. Anything else it raises is a declaration it could not
    compile, and that is refused rather than reported as staleness: an
    artifact whose source will not compile is not one a regeneration settles.
    """
    try:
        write(check=True)
    except RuntimeError as error:
        return [str(error)]
    except Exception as refusal:
        refuse_generation(obstruction_at("repository artifacts", refusal))
    return []


class RosterGap(BaseModel, frozen=True):
    """One declaration a target's tree renders nothing for."""

    target: str
    declaration: str

    def describe(self) -> str:
        """One line naming the target and what it left out."""
        return f"{self.target} renders nothing for {self.declaration}"


def roster_gaps(compositions: list[NativeHarnessComposition]) -> list[RosterGap]:
    """Every declaration a composition's desired tree carries no artifact for.

    The parity gate, written as completeness against the shared source rather
    than as a diff between two trees. Both readings catch a target that drops
    a skill the other keeps, but the trees themselves are not comparable: they
    shape a skill as ``commands/<name>.md`` and as ``skills/<name>/SKILL.md``,
    and each legitimately carries files the other has no equivalent for — a
    settings file, a config file. Diffing them would need an exception list
    that grows with every such file and would let a real gap hide in it.

    Measuring each target against :attr:`Harness.declared_ids` needs no
    exceptions: an artifact outside the roster is target-specific by
    construction and never considered, and a roster entry missing from one
    tree is named against that tree. It is also the stronger reading, because
    a declaration both targets dropped is still a gap here, where a diff
    between the two would call it parity.
    """
    return [
        RosterGap(target=composition.recipe.label, declaration=declared)
        for composition in compositions
        for rendered in [
            {artifact.semantic_id for artifact in composition.recipe.desired.artifacts}
        ]
        for declared in composition.recipe.source.rendered_ids
        if declared not in rendered
    ]


class DriftVerdict(BaseModel, frozen=True):
    """One reading of whether every generated artifact is what its source renders."""

    reports: list[DriftReport]
    """Ownership-aware drift for each native tree inspected."""

    stale_repository: list[str]
    """Why each generated file outside a native tree is behind its source."""

    @property
    def stale_trees(self) -> list[DriftReport]:
        """Every inspected tree holding an artifact its source no longer renders."""
        return [report for report in self.reports if not report.clean]

    @property
    def clean(self) -> bool:
        """Whether nothing generated is behind the source that renders it."""
        return not self.stale_trees and not self.stale_repository

    @property
    def summary(self) -> list[str]:
        """This verdict as a check row prints it, naming what is behind.

        Counted over both halves because the verdict is: a stale artifact
        outside every native tree fails a run whose tree count is zero, and
        a row saying only that tells its reader nothing to act on. Each
        repository message already carries the command that settles it, so
        the row repeats none of them and quotes them whole.
        """
        if self.clean:
            return ["harness drift: ok"]
        return [
            f"harness drift: FAIL ({len(self.stale_trees)} tree(s),"
            f" {len(self.stale_repository)} repository artifact(s))",
            *(f"  stale tree: {report.target}" for report in self.stale_trees),
            *(f"  {message}" for message in self.stale_repository),
        ]


def generate_targets(
    compositions: list[NativeHarnessComposition],
    repository_writers: list[RepositoryWriter],
    in_passing: bool = False,
) -> None:
    """Generate owned artifacts for every composition the selector names.

    ``in_passing`` carries the launch's quiet through both halves: a
    repository artifact behind its source is written and announced, and one
    already current is neither, the same non-event as a native tree that had
    nothing to write. The check is the whole of what a current one costs,
    which matters where the write is a toolchain run.
    """

    def written(write: RepositoryWriter) -> Path:
        """One artifact written, or a refusal naming the declaration behind it."""
        try:
            return write()
        except Exception as refusal:
            refuse_generation(obstruction_at("repository artifacts", refusal))

    for composition in compositions:
        generate_with_report(composition, in_passing)
    for write in repository_writers:
        if in_passing and not repository_staleness(write):
            continue
        typer.echo(f"repository artifact ready: {written(write)}")


def inspect_drift(
    compositions: list[NativeHarnessComposition],
    repository_writers: list[RepositoryWriter],
) -> DriftVerdict:
    """Read every generated artifact against the source that renders it.

    A library upgrade changes what the desired tree compiles to, and a comment
    edited in a source copied verbatim changes it without changing anything it
    does. Both are read the same way, over the bytes on disk rather than over
    what they mean, so an edit that only rewords a kernel comment is as stale
    as one that rewrites its logic.
    """
    return DriftVerdict(
        reports=[inspect_generation(item.recipe) for item in compositions],
        stale_repository=[
            message
            for write in repository_writers
            for message in repository_staleness(write)
        ],
    )


def report_stale(verdict: DriftVerdict) -> None:
    """Name every stale artifact, then the one command that settles them all."""
    for report in verdict.stale_trees:
        if not report.manifest_current:
            typer.echo(f"  stale proof: {report.target} ownership manifest", err=True)
        for write in report.proposal.writes:
            typer.echo(f"  stale: {write.artifact.path}", err=True)
        for delete in report.proposal.deletes:
            typer.echo(f"  orphaned: {delete.path}", err=True)
    for message in verdict.stale_repository:
        typer.echo(f"  {message}", err=True)
    typer.echo(
        f"generated artifacts are behind their source; run `{REGENERATE_COMMAND}` "
        "and include what it writes in this commit",
        err=True,
    )


def check_targets(
    compositions: list[NativeHarnessComposition],
    repository_writers: list[RepositoryWriter],
) -> None:
    """Report drift and roster parity for every selected composition.

    Both refusals are read here rather than in separate commands, because a
    tree can be perfectly current against a source that renders one target a
    skill short. Drift asks whether each tree is what its source renders;
    parity asks whether what it renders is the whole roster.
    """
    verdict = inspect_drift(compositions, repository_writers)
    for report in verdict.reports:
        report_drift(report)
    gaps = roster_gaps(compositions)
    for gap in gaps:
        typer.echo(f"  roster gap: {gap.describe()}", err=True)
    if not verdict.clean:
        report_stale(verdict)
    if not verdict.clean or gaps:
        raise typer.Exit(1)
