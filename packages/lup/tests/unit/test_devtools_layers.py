"""Behavior tests for measuring how a package's entries import each other.

What has to hold: an import is counted against the entry that holds it and
the entry it reaches, relative imports included; where it runs — at load,
inside a function, under a type checker — is kept apart; a pair importing
each other is reported once, closing at load only where both directions
run then; and a proposed move is measured on both ends of every import.
"""

from pathlib import Path

import pytest

from lup.devtools.py.layers import Move, layers


def write(root: Path, relative: str, text: str) -> None:
    """One source file of the throwaway package, parents created."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """``pkg`` with three entries and one helper that could move.

    - ``alpha`` imports ``beta`` at load, relatively from a subpackage.
    - ``beta`` imports ``alpha`` only inside a function.
    - ``gamma`` imports ``alpha`` only for the type checker, and ``beta`` at
      load through ``beta.shared``, the helper a move could relocate.
    """
    root = tmp_path / "pkg"
    write(root, "__init__.py", '"""The package."""\n')
    write(root, "alpha/__init__.py", '"""Alpha."""\n')
    write(root, "alpha/inner.py", "from ..beta import thing\n")
    write(
        root,
        "beta.py",
        "def late():\n    from pkg.alpha import inner\n    return inner\n\nthing = 1\n",
    )
    write(root, "beta_shared.py", "")
    write(
        root,
        "gamma.py",
        "from typing import TYPE_CHECKING\n"
        "from pkg.beta_shared import *\n"
        "if TYPE_CHECKING:\n    import pkg.alpha\n",
    )
    return root


def test_each_import_counts_against_both_entries(package: Path) -> None:
    measured = layers(package)

    reach = {entry.name: entry.imports for entry in measured.entries}
    assert reach["alpha"] == ["beta"]
    assert reach["beta"] == ["alpha"]
    assert reach["gamma"] == ["alpha", "beta_shared"]


def test_where_an_import_runs_is_kept_apart(package: Path) -> None:
    edges = {(edge.importer, edge.imported): edge for edge in layers(package).edges}

    assert edges[("alpha", "beta")].count("load") == 1
    assert edges[("beta", "alpha")].count("deferred") == 1
    assert edges[("gamma", "alpha")].count("typing") == 1


def test_a_pair_closing_through_a_deferred_import_says_so(package: Path) -> None:
    (pair,) = layers(package).two_way

    assert (pair.forward.importer, pair.forward.imported) == ("alpha", "beta")
    assert not pair.at_load()


def test_a_move_is_read_on_both_ends(package: Path) -> None:
    measured = layers(package, [Move(prefix="pkg.beta_shared", entry="beta")])

    reach = {entry.name: entry.imports for entry in measured.entries}
    assert "beta_shared" not in reach
    assert reach["gamma"] == ["alpha", "beta"]
