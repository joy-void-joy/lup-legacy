"""Guide to ``packages/lup``, the reusable provider-neutral library.

The per-package roster below is authored prose keyed by a walked set. What
each package solves is a judgement no walk can make, so it stays written; but
*which* packages there are to describe is the tree's to answer, and asking it
is what closes the gap this page had — a roster promising "every remaining
top-level entry" while silently omitting six of them, `actors` among them
four commits after it was added.
"""

import ast
from collections.abc import Iterator
from functools import cache
from itertools import groupby
from pathlib import Path

from pydantic import BaseModel

import lup.harness.models as models
from lup.harness.content.application import ApplicationLayout
from lup.harness.content.tree import TopLevelEntry, top_level_entries
from lup.formats.markdown import CodeCell, PlainCell

LIBRARY_PACKAGE = Path(__file__).resolve().parents[3]
"""This library's own package directory, wherever the reader installed it from.

Counted from this module's own depth — ``lup/harness/content/docs/`` — so the
answer is the ``lup`` package itself and not whatever sits above it.

Derived rather than resolved against the reading project's checkout, because
the two answers differ for everyone who is not lup itself. A project that
vendors lup at ``packages/lup`` walks the same tree either way; a project that
takes it as a distribution has no such subtree at all, and a page that looked
for one failed generation rather than describing the library that project
actually runs. Asking the imported package where it lives answers for both,
and answers about the code in use rather than about a directory that may be a
different revision — which is the stronger claim the roster wanted anyway.
"""


@cache
def imported_modules(text: str) -> tuple[str, ...]:
    """Every absolute module one source file's text imports, at any depth.

    A deferred import inside a function is still a dependency of the entry
    holding it, so the walk does not stop at module scope.

    Held per text: every composition renders this page, each render counts
    four placements over the same few hundred modules, and parsing and
    walking them again each time was a fifth of what composing a tree cost.
    Keyed by the text rather than the path, so a module that changes is read
    again rather than answered from before.
    """

    def named(node: ast.AST) -> Iterator[str]:
        match node:
            case ast.ImportFrom(module=str(name), level=0):
                yield name
            case ast.Import(names=aliases):
                yield from (alias.name for alias in aliases)
            case _:
                pass

    return tuple(name for node in ast.walk(ast.parse(text)) for name in named(node))


class RosterEntry(BaseModel, frozen=True):
    """One top-level entry, and the authored answer to why it is one."""

    package: str
    solves: str


class TieredEntry(BaseModel, frozen=True):
    """One entry a section above the roster already describes, and which one.

    Carried rather than inferred: a package earns a section of its own by
    being load-bearing enough to need one, which is a judgement. Naming the
    section is what lets the roster send a reader *there* rather than leave a
    package it skips unaccounted for — :meth:`Roster.described` renders that
    sentence from this list, so an entry tiered out of the table appears in
    the line saying where it went by being tiered at all.
    """

    package: str
    section: str


class Roster(BaseModel, frozen=True):
    """A package roster, and the tree it must agree with.

    Every judgement here is a field default rather than a module constant, so
    a project describing its own library replaces the values instead of
    forking the check that reads them.
    """

    subtree: str = "packages/lup/src/lup"
    """Where the package this describes lives inside lup's own repository.

    Prose only: what the walk reads is :attr:`source`, which is where the
    reader's own copy actually sits. This is the path a reader opens to find
    the source, which is a fact about lup's repository rather than theirs.
    """

    source: Path = LIBRARY_PACKAGE
    """The package directory this roster is checked against.

    A default rather than a constant, for the reason every default here is
    one: a project that vendors lup somewhere else describes the copy it
    actually has. It is also what lets the drift check be tested at all — a
    walk that could only read its own installation has no way to be shown a
    tree that has drifted.
    """

    tiered: list[TieredEntry] = [
        TieredEntry(package="types", section="Layering"),
        TieredEntry(package="sessions", section="The packages"),
        TieredEntry(package="harness", section="The packages"),
        TieredEntry(package="policy", section="The packages"),
        TieredEntry(package="resolver", section="The packages"),
        TieredEntry(package="providers", section="The packages"),
    ]
    """Entries a section above the roster describes at length instead.

    In the order a reader meets them, because :meth:`described` renders the
    sentence that sends them there from this list and nothing sorts it after.
    """

    def owed(self) -> list[TopLevelEntry]:
        """Every entry in the library's own package this roster has to describe."""
        elsewhere = {entry.package: entry.section for entry in self.tiered}
        return [
            entry
            for entry in top_level_entries(self.source.parent, self.source.name)
            if entry.name not in elsewhere
        ]

    def described(self) -> str:
        """Where each entry this roster skips is described at length instead.

        Rendered from :attr:`tiered` rather than restated in the prose beside
        it, which is the whole reason the section is carried: the sentence
        naming what the table omits and the filter that omits it are one list,
        so neither can name a package the other has forgotten. The sentence it
        replaced named `types` and left the other five unaccounted for.

        Grouped by consecutive run rather than by sorted key, so the order is
        the one a reader meets the sections in; two runs of one section read
        as two clauses, which is what interleaving them would deserve.
        """

        def listed(packages: list[str]) -> str:
            """A short run of names, read as a clause rather than as a row."""
            if len(packages) == 1:
                return packages[0]
            return f"{', '.join(packages[:-1])} and {packages[-1]}"

        return "; ".join(
            f"{listed([f'`{entry.package}`' for entry in run])} in **{section}**"
            for section, run in groupby(self.tiered, key=lambda entry: entry.section)
        )

    def solves(self, source: Path) -> str:
        """What one entry says it solves, read from its own docstring.

        The summary line and the paragraph beneath it, joined. PEP 257 puts a
        short summary first, which is what a reader of the module wants and is
        too short to be a roster row on its own; the paragraph under it is
        where an entry says why it is an entry at all. Anything further down is
        the module explaining itself to somebody already inside it.

        Read from the code rather than restated here, and that is the point.
        An authored string per name is two descriptions of one subject, in
        two files, with nothing holding them together, and the hand-written
        half drifts: a `channels` row naming six consumers where the import
        graph counts eleven, and a page confidently wrong is worse than one
        that has to be looked up.
        """
        docstring = ast.get_docstring(ast.parse(source.read_text(encoding="utf-8")))
        if not docstring:
            raise ValueError(
                f"{source} carries no docstring, so this page has nothing to "
                "say about it: open the module with a summary line and a "
                "paragraph saying what it solves"
            )

        def opening() -> Iterator[str]:
            """The summary and the one paragraph under it, line by line.

            Walked rather than split on the blank line, because what is being
            read is prose: there is no parser for "the first two paragraphs",
            and a loop takes them without pretending otherwise.
            """
            blanks = 0
            for line in docstring.splitlines():
                if not line.strip():
                    blanks += 1
                    if blanks == 2:
                        return
                    continue
                yield line.strip()

        return " ".join(" ".join(opening()).split())

    def table(self) -> models.MarkdownTable:
        """The roster as a table, derived from the tree it describes.

        Nothing left to fall behind: every row is the entry's own docstring, so
        an entry added to the library arrives here by existing and one deleted
        leaves by the same route. The walk is the list; a hand-kept one
        checked against it is the second copy that falls behind.

        A missing docstring fails generation loudly, for the reason an
        authored roster would: a page that quietly drops a package reads exactly
        like a complete one.
        """
        return models.MarkdownTable(
            headers=["Package", "Solves"],
            rows=[
                [CodeCell(text=entry.name), PlainCell(text=self.solves(entry.source))]
                for entry in self.owed()
            ],
        )

    def imports(self, importer: str, imported: str) -> dict[Path, int]:
        """How many import statements in each module under one path reach one other.

        ``importer`` is a path inside the package, a directory or a module
        without its suffix; ``imported`` is a dotted name beneath the package.
        Only modules holding at least one such statement appear, so the keys
        are the modules that read it and the values sum to the statements.

        Counted here rather than quoted in the placement prose, because a count
        written into a sentence was the part of that sentence that fell behind:
        it named a package that had since moved into another entry.
        """
        root = self.source / importer
        sources = (
            sorted(root.rglob("*.py")) if root.is_dir() else [root.with_suffix(".py")]
        )
        prefix = f"{self.source.name}.{imported}"
        counts = {
            source: sum(
                1
                for name in imported_modules(source.read_text(encoding="utf-8"))
                if name == prefix or name.startswith(f"{prefix}.")
            )
            for source in sources
        }
        return {source: count for source, count in counts.items() if count}


LIBRARY = Roster()
"""This library's own roster, checked against this library's own tree."""


def document(layout: ApplicationLayout) -> models.PromptDocument:
    """The library guide, naming the application half by its own name.

    Takes no checkout, because the one tree it walks is the library's own and
    :data:`LIBRARY_PACKAGE` already names it. The filesystem is still read at
    call time rather than at import, so composing this module stays possible
    from anywhere.
    """
    return models.PromptDocument(
        source=__name__,
        parts=[
            models.Passage(
                module=__name__,
                values={
                    "project_directory": models.code(layout.directory()),
                    "library_described": models.plain(LIBRARY.described()),
                    "subtree": models.code(LIBRARY.subtree),
                    # The package is here whatever a project took; the page on
                    # its lifecycle is the resolver module's.
                    "resolver_lifecycle": models.WhereTaken(
                        module="resolver",
                        parts=[
                            models.TextPart(
                                text=" [resolver.md](resolver.md) covers the lifecycle."
                            )
                        ],
                    ),
                },
            ),
            LIBRARY.table(),
            models.Passage(
                module=__name__,
                name="what-is-left-to-place",
                values={
                    "value": models.code(layout.path()),
                    "resolver_readers": models.counted(
                        len(LIBRARY.imports("devtools", "resolver"))
                    ),
                    "coordination_reach": models.counted(
                        sum(LIBRARY.imports("resolver", "coordination").values())
                    ),
                    "harness_reach": models.counted(
                        sum(LIBRARY.imports("resolver", "harness").values())
                    ),
                    "edit_readers": models.counted(
                        len(LIBRARY.imports("harness/codescan", "policy.kernel.edit"))
                    ),
                },
            ),
        ],
    )
