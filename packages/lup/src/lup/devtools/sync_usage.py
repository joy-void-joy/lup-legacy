"""What the projects built on a package import from it, name by name.

Removing, renaming or moving something in a library is a question about the
projects that import it, and the answer sits in their trees rather than in
this one. A migration declared for a break says what an adopter must do; it
cannot say whether any adopter does the thing at all. That is a count, and
reading it from each tracked checkout is what lets a leaner library be cut
from what is used rather than from what looks unused here.

Only files git tracks are read, so a virtual environment, a cache or a
vendored copy inside a checkout never counts as use.
"""

import ast
from collections.abc import Iterator
from itertools import groupby
from operator import attrgetter
from pathlib import Path

from pydantic import BaseModel, Field

from lup.execution.shell import git


class Imported(BaseModel, frozen=True):
    """One import statement reaching the package, and the names it takes."""

    module: str
    names: list[str] = Field(
        default_factory=list[str],
        description="The names a `from` import takes; empty for a plain import",
    )


class Usage(BaseModel, frozen=True):
    """One module of the package as one project uses it."""

    module: str
    names: list[str]
    statements: int


class ProjectUsage(BaseModel, frozen=True):
    """Everything one tracked project imports from the package."""

    project: str
    checkout: str
    modules: list[Usage]


class Seen(BaseModel, frozen=True):
    """One module one project imports, as the module-by-module reading groups it."""

    module: str
    project: str
    names: list[str]


class ModuleReach(BaseModel, frozen=True):
    """One module of the package, and every project that imports it."""

    module: str
    projects: list[str]
    names: list[str]


class UsageReport(BaseModel, frozen=True):
    """The package's reach across every project that could be read."""

    package: str
    projects: list[ProjectUsage]
    reach: list[ModuleReach]
    unlocated: list[str] = Field(
        default_factory=list[str],
        description="Tracked projects with no checkout on this machine to read",
    )


def reaches(name: str, package: str) -> bool:
    """Whether a dotted module name is the package or sits beneath it."""
    return name == package or name.startswith(f"{package}.")


def statements_in(source: str, package: str) -> Iterator[Imported]:
    """Every import statement in one file that reaches the package."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        match node:
            case ast.ImportFrom(module=str(module), level=0) if reaches(
                module, package
            ):
                yield Imported(
                    module=module, names=[alias.name for alias in node.names]
                )
            case ast.Import(names=aliases):
                yield from (
                    Imported(module=alias.name)
                    for alias in aliases
                    if reaches(alias.name, package)
                )
            case _:
                pass


def usage_in(project: str, checkout: Path, package: str) -> ProjectUsage:
    """Read every tracked Python file of one checkout for its imports of the package."""
    tracked = git.lines("-C", str(checkout), "ls-files", "--", "*.py")
    found = sorted(
        (
            statement
            for relative in tracked
            if (checkout / relative).is_file()
            for statement in statements_in(
                (checkout / relative).read_text(encoding="utf-8", errors="replace"),
                package,
            )
        ),
        key=attrgetter("module"),
    )
    return ProjectUsage(
        project=project,
        checkout=str(checkout),
        modules=[
            Usage(
                module=module,
                names=sorted({name for statement in group for name in statement.names}),
                statements=len(group),
            )
            for module, grouped in groupby(found, key=attrgetter("module"))
            if (group := list(grouped))
        ],
    )


def usage_report(
    located: list[ProjectUsage], unlocated: list[str], package: str
) -> UsageReport:
    """Every project's usage, and the same read module by module."""
    rows = sorted(
        (
            Seen(module=usage.module, project=project.project, names=usage.names)
            for project in located
            for usage in project.modules
        ),
        key=attrgetter("module"),
    )
    return UsageReport(
        package=package,
        projects=located,
        unlocated=unlocated,
        reach=[
            ModuleReach(
                module=module,
                projects=sorted({row.project for row in group}),
                names=sorted({name for row in group for name in row.names}),
            )
            for module, grouped in groupby(rows, key=attrgetter("module"))
            if (group := list(grouped))
        ],
    )
