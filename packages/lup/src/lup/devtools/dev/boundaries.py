"""Walk repository Python files for the boundary rules in both directions.

Backs ``lup-devtools dev check --boundaries`` and ``--placement``, focused
views of rules the anti-pattern sweep also runs. Inward, every git-tracked
``.py`` file outside the sanctioned homes (the adapters package, tests) runs through
:mod:`lup.harness.codescan.boundaries`; the tree is expected to hold zero breaches, and
this is the regression guard that keeps backend dispatch from creeping back
outside the seam. Outward, every library module is checked for data tables an
adopter cannot replace — those are open placement debt, so that row names the
tables still to be moved rather than asserting zero.
"""

from pathlib import Path

import typer

from pydantic import BaseModel

from lup.harness.codescan.boundaries import (
    KERNEL_ROOT,
    LIBRARY_ROOT,
    BoundaryBreach,
    audit_path_boundaries,
    declared_defaults,
    find_kernel_import_breaches,
    find_library_default_breaches,
    library_placement_path_is_audited,
)
from lup.devtools.dev.tracked import tracked_files
from lup.harness.codescan.common import PythonSource, module_name
from lup.devtools.project import DevProject
from lup.devtools.utils import output_json
from lup.policy.kernel.roles import path_role


class FoundBreach(BoundaryBreach):
    """A :class:`~lup.harness.codescan.boundaries.BoundaryBreach` tagged with its file."""

    file: str


class TrackedSource(BaseModel):
    """One git-tracked Python file, read once for every scan over the tree."""

    rel: str
    path: Path
    text: str


def tracked_python_sources(project: DevProject | None = None) -> list[TrackedSource]:
    """Every production Python file that exists on disk, with its text.

    Listed once each whatever the index holds: a path mid-merge sits there at
    three stages, and is still one file on disk and one module.
    """
    tracked = tracked_files(others=True, suffixes=(".py",))
    return [
        TrackedSource(rel=rel, path=path, text=path.read_text(encoding="utf-8"))
        for rel in tracked
        if (path := Path(rel)).exists()
        and (project is None or path_role(rel, project.path_roles) == "production")
    ]


def library_sources() -> list[TrackedSource]:
    """Every tracked Python file that ships inside ``packages/lup``."""
    return [
        source
        for source in tracked_python_sources()
        if source.rel.startswith(LIBRARY_ROOT)
    ]


def overridable_names(
    sources: list[TrackedSource],
) -> set[str]:  # lup: ignore[set-shape] — name identity membership
    """Constants a caller can replace, pooled across modules by where each is declared."""
    return {
        name
        for source in sources
        for name in declared_defaults(
            PythonSource(
                path=source.path, module=module_name(source.path), text=source.text
            )
        )
    }


def scan_library_placement() -> list[FoundBreach]:
    """Every library data table no adopter can replace, across ``packages/lup``.

    Whether a table is reachable as an overridable default is a property of the
    library as a whole, not of the module that writes it down, so the names
    callers can replace are collected across every library module — adapters
    included — before any one module is judged against them.
    """
    sources = library_sources()
    overridable = overridable_names(sources)
    return [
        FoundBreach(file=source.rel, **breach.model_dump())
        for source in sources
        if library_placement_path_is_audited(source.path)
        for breach in find_library_default_breaches(
            source.text, overridable, module_name(source.path)
        )
    ]


class PortableModule(BaseModel):
    """One application module whose imports never reach the application."""

    file: str
    imports: int


def application_imports(text: str, package: str) -> list[str]:
    """Every import in one module that names the application package."""
    return [
        line
        for line in text.splitlines()
        if line.startswith((f"from {package}", f"import {package}"))
    ]


def scan_application_placement(project: DevProject) -> list[PortableModule]:
    """Every devtools module that could be library code as it stands.

    The library row asks whether a library module baked in a choice an
    adopter cannot replace. This asks the mirror question, and it is the one
    that decides what a downstream project keeps receiving: this template is
    copied and renamed, so a module living here is frozen for every adopter
    at the moment they copy it, while one in ``packages/lup`` reaches them
    through an ordinary dependency bump.

    A module that imports nothing from the application is, by the placement
    test the conventions already state, library code sitting in the
    application. Reported rather than failed, because moving one is a change
    with its own review — this row names the debt and shrinks.

    The declared prose roots are exempt, and not as a concession: a content
    module is this project's own judgement written as data, so importing
    nothing is what it looks like when it is exactly where it belongs. The
    same holds for the declaration this project compiles its harness from.
    """
    package = project.package
    root = f"src/{package}/devtools/"
    exempt = (
        f"{root}harness/content/",
        f"{root}harness/catalog.py",
        f"{root}subapps.py",
    )
    return [
        PortableModule(file=source.rel, imports=0)
        for source in tracked_python_sources(project)
        if source.rel.startswith(root)
        and not source.rel.startswith(exempt)
        and source.path.name != "__init__.py"
        and not application_imports(source.text, package)
    ]


def scan_boundaries(project: DevProject) -> list[FoundBreach]:
    """Every native import, spelling, and kernel-import breach in the tree."""
    found: list[FoundBreach] = []  # lup: ignore[empty-collection]
    roots = project.roots
    boundaries = project.resolved_import_boundaries()
    for source in tracked_python_sources(project):
        found.extend(
            FoundBreach(
                file=source.rel,
                line=finding.line,
                module=finding.module,
                text=finding.text,
            )
            for finding in audit_path_boundaries(
                source.path, source.text, roots, boundaries
            )
            if finding.kind == "missing" and project.rules.keeps(finding.rule_id)
        )
        if source.rel.startswith(KERNEL_ROOT):
            found.extend(
                FoundBreach(file=source.rel, **breach.model_dump())
                for breach in find_kernel_import_breaches(source.text)
            )
    return found


def report(project: DevProject, as_json: bool) -> None:
    """List every breach; exit non-zero when any exist."""
    found = scan_boundaries(project)
    match (as_json, found):
        case (True, _):
            output_json([breach.model_dump() for breach in found])
        case (False, [_, *_]):
            for breach in found:
                typer.echo(f"{breach.file}:{breach.line}  {breach.module}")
        case _:
            typer.echo("seam boundaries: ok")
    if found:
        raise typer.Exit(1)


def report_placement(as_json: bool) -> None:
    """List every baked-in library table; exit non-zero when any exist."""
    found = scan_library_placement()
    match (as_json, found):
        case (True, _):
            output_json([breach.model_dump() for breach in found])
        case (False, [_, *_]):
            for breach in found:
                typer.echo(f"{breach.file}:{breach.line}  {breach.module}")
        case _:
            typer.echo("library placement: ok")
    if found:
        raise typer.Exit(1)
