"""How a package's top-level entries import each other, and which pairs do so both ways.

A package's layering is read at the granularity of its top-level entries:
each one a subject, a foundation, or tooling, and the edges between them the
claim a reader checks a placement against. A pair of entries that import each
other is the shape that says one of them holds something both need — so the
two-way pairs are what a placement question is about, and a count written
into prose beside them is the part that falls behind.

Every import is read with where it runs. One at module scope runs when the
module loads and can take part in an import cycle; one inside a function
runs when the function is called; one under ``if TYPE_CHECKING`` never runs
at all. Whether a deferred import counts as an edge is a question a layering
rule has to answer before it can be written, so the kinds are reported apart
rather than decided here.

``--move`` measures a proposed relocation before anybody makes it: a module
prefix reassigned to another entry is read as belonging there, on both ends
of every import, so what a move would dissolve is a count rather than an
argument.
"""

import ast
from collections.abc import Iterator
from itertools import groupby
from operator import attrgetter
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, Field

ImportKind = Literal["load", "deferred", "typing"]

KINDS: list[ImportKind] = list(get_args(ImportKind))
"""Every place an import can run from, in the order a reader weighs them."""


class Move(BaseModel, frozen=True):
    """A module prefix read as belonging to another entry, for a what-if."""

    prefix: str = Field(description="A dotted module name, and every module beneath it")
    entry: str = Field(description="The entry it is read as belonging to")

    def covers(self, module: str) -> bool:
        """Whether a dotted module name is this prefix or sits beneath it."""
        return module == self.prefix or module.startswith(f"{self.prefix}.")


class Site(BaseModel, frozen=True):
    """One import statement that crosses from one entry into another."""

    importer: str
    imported: str
    path: str = Field(description="The importing file, relative to the package")
    line: int
    kind: ImportKind


class Entry(BaseModel, frozen=True):
    """One top-level entry: how much it holds, and whom it reaches."""

    name: str
    modules: int
    lines: int
    imports: list[str]
    imported_by: list[str]


class Edge(BaseModel, frozen=True):
    """Every import from one entry into another."""

    importer: str
    imported: str
    sites: list[Site]

    def count(self, kind: ImportKind) -> int:
        """How many of this edge's statements run from one place."""
        return sum(1 for site in self.sites if site.kind == kind)

    def spelled(self) -> str:
        """The edge's statements by kind, as a reader scans them."""
        return ", ".join(
            f"{self.count(kind)} {kind}" for kind in KINDS if self.count(kind)
        )


class TwoWay(BaseModel, frozen=True):
    """Two entries that import each other, with both directions' statements."""

    forward: Edge
    back: Edge

    def at_load(self) -> bool:
        """Whether both directions run when their modules load."""
        return bool(self.forward.count("load") and self.back.count("load"))


class Layers(BaseModel, frozen=True):
    """A package's entries, the edges between them, and the pairs that close."""

    package: str
    entries: list[Entry]
    edges: list[Edge]
    two_way: list[TwoWay]


class Module(BaseModel, frozen=True):
    """One source file of the package, named as Python imports it."""

    path: Path
    relative: Path
    dotted: str
    package: list[str] = Field(
        description="The package its relative imports resolve in"
    )


class Imported(BaseModel, frozen=True):
    """One import statement's targets, and where it runs from."""

    line: int
    names: list[str]
    kind: ImportKind


def modules_of(root: Path) -> list[Module]:
    """Every source file beneath a package directory, with its dotted name."""

    def walked() -> Iterator[Module]:
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(root)
            if any(
                part.startswith(".") or part == "__pycache__" for part in relative.parts
            ):
                continue
            parts = [root.name, *relative.with_suffix("").parts]
            inside = parts[:-1] if parts[-1] == "__init__" else parts
            package = inside if relative.name == "__init__.py" else parts[:-1]
            yield Module(
                path=path, relative=relative, dotted=".".join(inside), package=package
            )

    return list(walked())


def type_checking(test: ast.expr) -> bool:
    """Whether an ``if`` guards what only a type checker reads."""
    match test:
        case ast.Name(id="TYPE_CHECKING") | ast.Attribute(attr="TYPE_CHECKING"):
            return True
        case _:
            return False


def imports_in(
    node: ast.AST, module: Module, kind: ImportKind = "load"
) -> Iterator[Imported]:
    """Every import beneath a node, each marked with where it runs from."""
    match node:
        case ast.Import(names=aliases):
            yield Imported(
                line=node.lineno, names=[alias.name for alias in aliases], kind=kind
            )
        case ast.ImportFrom(module=name, level=0) if name:
            yield Imported(line=node.lineno, names=[name], kind=kind)
        case ast.ImportFrom(module=name, level=level) if level > 0:
            base = module.package[: len(module.package) - (level - 1)]
            yield Imported(
                line=node.lineno,
                names=[".".join([*base, name] if name else base)],
                kind=kind,
            )
        case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.Lambda():
            inner: ImportKind = "deferred" if kind == "load" else kind
            for child in ast.iter_child_nodes(node):
                yield from imports_in(child, module, inner)
        case ast.If(test=test, body=body, orelse=orelse) if type_checking(test):
            for child in body:
                yield from imports_in(child, module, "typing")
            for child in orelse:
                yield from imports_in(child, module, kind)
        case _:
            for child in ast.iter_child_nodes(node):
                yield from imports_in(child, module, kind)


def layers(root: Path, moves: list[Move] | None = None) -> Layers:
    """Measure the edges between a package's top-level entries.

    The root's own ``__init__`` is an entry under the package's name, since
    an import of the package itself reaches it.
    """
    package = root.name
    modules = modules_of(root)
    named = sorted({module.relative.parts[0].removesuffix(".py") for module in modules})
    entries = [name for name in named if name != "__init__"]
    reassigned = sorted(moves or [], key=lambda move: len(move.prefix), reverse=True)

    def entry_of(dotted: str) -> str | None:
        """The entry a dotted name belongs to, or None outside the package."""
        moved = next((move.entry for move in reassigned if move.covers(dotted)), None)
        if moved is not None:
            return moved
        if dotted == package:
            return package
        return next(
            (
                name
                for name in entries
                if dotted == f"{package}.{name}"
                or dotted.startswith(f"{package}.{name}.")
            ),
            None,
        )

    def crossing() -> Iterator[Site]:
        for module in modules:
            importer = entry_of(module.dotted)
            if importer is None:
                continue
            tree = ast.parse(module.path.read_text(encoding="utf-8"))
            for found in imports_in(tree, module):
                for name in found.names:
                    imported = entry_of(name)
                    if imported is None or imported == importer:
                        continue
                    yield Site(
                        importer=importer,
                        imported=imported,
                        path=module.relative.as_posix(),
                        line=found.line,
                        kind=found.kind,
                    )

    sites = sorted(crossing(), key=attrgetter("importer", "imported", "path", "line"))
    edges = [
        Edge(importer=importer, imported=imported, sites=list(group))
        for (importer, imported), group in groupby(
            sites, key=attrgetter("importer", "imported")
        )
    ]
    reach = {(edge.importer, edge.imported): edge for edge in edges}
    held = [module for module in modules if entry_of(module.dotted) is not None]
    names = sorted({entry_of(module.dotted) or package for module in held})
    return Layers(
        package=package,
        entries=[
            Entry(
                name=name,
                modules=sum(1 for module in held if entry_of(module.dotted) == name),
                lines=sum(
                    len(module.path.read_text(encoding="utf-8").splitlines())
                    for module in held
                    if entry_of(module.dotted) == name
                ),
                imports=[edge.imported for edge in edges if edge.importer == name],
                imported_by=[edge.importer for edge in edges if edge.imported == name],
            )
            for name in names
        ],
        edges=edges,
        two_way=[
            TwoWay(forward=edge, back=reach[(edge.imported, edge.importer)])
            for edge in edges
            if edge.importer < edge.imported and (edge.imported, edge.importer) in reach
        ],
    )
