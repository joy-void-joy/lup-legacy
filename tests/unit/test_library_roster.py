"""The library page's package roster is read from the package it describes.

An authored roster promising "every remaining top-level entry" omits whatever
lands after it is written, because a prose table is a claim about the tree
that nothing reads the tree to check. Walking the tree settles the *names*;
anything said about them that stays authored, in a second file, drifts anyway:
a hand-written row counts the consumers it counted when written, not the ones
the import graph holds.

So the prose lives in each entry's own docstring and the row is derived from it.
One description of one subject, beside the thing it describes. What that leaves
worth pinning is a walk that reads the wrong tree, and an entry with nothing to
say about itself.
"""

import ast
from pathlib import Path

import pytest

from lup.harness.content.docs.library import (
    LIBRARY,
    LIBRARY_PACKAGE,
    Roster,
)

DESCRIBED = '"""A summary line.\n\nWhat it solves, at length.\n"""\n'


def library_at(root: Path, *names: str) -> Path:
    """A throwaway library tree carrying one described module per name."""
    subtree = root / LIBRARY_PACKAGE.name
    subtree.mkdir(parents=True)
    for name in names:
        (subtree / f"{name}.py").write_text(DESCRIBED, encoding="utf-8")
    return subtree


def test_every_entry_the_tree_carries_gets_a_row() -> None:
    """Nothing left to keep in step: the rows are the walk."""
    rows = LIBRARY.table().rows

    assert [cell.text for cell, _ in rows] == [entry.name for entry in LIBRARY.owed()]


def test_a_row_opens_with_the_entry_s_own_summary_line() -> None:
    """The row is the docstring, not a paraphrase somebody has to maintain."""
    for entry in LIBRARY.owed():
        parsed = ast.parse(entry.source.read_text(encoding="utf-8"))
        docstring = ast.get_docstring(parsed)
        assert docstring is not None
        summary = docstring.splitlines()[0]

        assert LIBRARY.solves(entry.source).startswith(summary), entry.name


def test_the_roster_walks_the_package_it_was_imported_from() -> None:
    """The tree read is the reader's own copy, not a path under their checkout.

    What made the page unusable downstream: a project taking lup as a
    dependency has no `packages/lup/src/lup` beneath its root, so a walk
    resolved against the checkout failed generation outright rather than
    describing the library that project actually runs.
    """
    assert LIBRARY.source == LIBRARY_PACKAGE
    assert (LIBRARY.source / "orchestration").is_dir()


def test_an_entry_added_to_the_tree_appears_without_being_declared(
    tmp_path: Path,
) -> None:
    """The property the derivation is for.

    An authored roster would raise here until somebody wrote a row. Derived,
    the entry describes itself, and the page follows the tree by
    construction.
    """
    roster = Roster(source=library_at(tmp_path, "arrived"))

    rows = roster.table().rows

    assert [cell.text for cell, _ in rows] == ["arrived"]
    assert rows[0][1].text == "A summary line. What it solves, at length."


def test_an_entry_with_no_docstring_fails_generation(tmp_path: Path) -> None:
    """Loudly, for the reason an authored roster would fail loudly.

    A page that quietly drops a package reads exactly like a complete one, so
    an entry with nothing to say has to stop the build rather than shorten the
    table. The message names the file and what to write in it.
    """
    source = library_at(tmp_path, "described")
    (source / "silent.py").write_text("value = 1\n", encoding="utf-8")
    roster = Roster(source=source)

    with pytest.raises(ValueError) as raised:
        roster.table()

    assert "silent.py" in str(raised.value)
    assert "docstring" in str(raised.value)


def test_a_tiered_package_is_not_owed_a_row(tmp_path: Path) -> None:
    """A section above the table describes it at length instead."""
    tiered = [entry.package for entry in Roster().tiered]
    roster = Roster(source=library_at(tmp_path, *tiered, "ordinary"))

    assert len(roster.table().rows) == 1


def test_a_dotted_directory_is_owed_no_row(tmp_path: Path) -> None:
    """A tool's scratch directory beside the source is not a package.

    The roster walks the filesystem rather than git, so anything left beside
    the library is visible to it — and a checkout that has run an agent
    carries `.claude` there, gitignored and untracked. Python cannot import a
    dotted name, so no roster could ever owe it a row, and generation never
    asks what an editor's scratch directory solves.
    """
    source = library_at(tmp_path, "ordinary")
    (source / ".claude" / ".cc-writes").mkdir(parents=True)
    (source / ".venv").mkdir()
    roster = Roster(source=source)

    assert [entry.name for entry in roster.owed()] == ["ordinary"]
    assert len(roster.table().rows) == 1


def test_a_directory_whose_package_was_deleted_is_owed_no_row(tmp_path: Path) -> None:
    """A husk left by a deletion is not a package either.

    Git tracks no directories, so removing a package's files leaves the
    directory standing wherever gitignored bytecode still sits inside it.
    Asked what it solves, that directory sends the roster to open an
    `__init__.py` the deletion took, ending generation on a traceback naming
    a path instead of a diagnostic naming the husk.
    """
    source = library_at(tmp_path, "ordinary")
    (source / "emptied" / "__pycache__").mkdir(parents=True)
    (source / "emptied" / "__pycache__" / "gone.pyc").write_bytes(b"")
    roster = Roster(source=source)

    assert [entry.name for entry in roster.owed()] == ["ordinary"]
    assert len(roster.table().rows) == 1


def test_an_import_count_reads_every_statement_that_reaches_the_module(
    tmp_path: Path,
) -> None:
    """A deferred import counts, a sibling sharing the prefix does not.

    The placement prose quotes these counts rather than writing them, so what
    it can be wrong about is the walk: an import inside a function is still
    an edge the entry carries, and ``lup.resolvers`` is not ``lup.resolver``.
    """
    source = library_at(tmp_path)
    package = source.name
    (source / "driver").mkdir()
    (source / "driver" / "reads.py").write_text(
        f"from {package}.resolver.core import run\n"
        f"import {package}.resolver\n"
        "def later():\n"
        f"    from {package}.resolver import models\n",
        encoding="utf-8",
    )
    (source / "driver" / "sibling.py").write_text(
        f"from {package}.resolvers import other\n", encoding="utf-8"
    )
    roster = Roster(source=source)

    assert roster.imports("driver", "resolver") == {source / "driver" / "reads.py": 3}
