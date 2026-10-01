"""A `dict-get` marker on a receiver the hook cannot resolve stands.

`request.headers` is Starlette's `Headers`, a `typing.Mapping[str, str]`. The
sweep resolves that and demands `# lup: ignore[dict-get]` on a `.get("literal")`
read of it. The edit hook's own checker, answering for one file after a write,
can type nothing for the receiver — and were an unresolved receiver reported
the way a resolved non-mapping is, the post-write repair would delete the
marker as guarding nothing, the next sweep would report the line missing it,
and a session restoring it would meet the repair again.

Both halves are pinned here against the one file: the kernel's judgement of
an edit to an unrelated hunk, and the sweep's audit and repair over the file
with a checker that resolves nothing. Neither may touch the marker.
"""

from pathlib import Path

import pytest

from lup.devtools.dev import antipatterns as sweep
from lup.devtools.project import DevProject
from lup.harness.codescan.antipatterns import PYTHON_ANTI_PATTERNS, audit_text
from lup.harness.codescan.common import PythonSource
from lup.harness.codescan.oracle import (
    ClassDeclaration,
    Declaration,
    SourceBuffer,
    SymbolQuery,
    TypeOracle,
    UnknownDeclaration,
)
from lup.harness.codescan.resolution import refute
from lup.policy.bundle import bundled_antipattern_rows
from lup.policy.kernel.edit import antipattern_decision
from lup.policy.kernel.rows import ResolutionRow
from tests.unit.repos import initialized_repo

MARKED = (
    "from starlette.requests import Request\n"
    "\n"
    "\n"
    "def resume_of(request: Request) -> str:\n"
    '    """Read the resume cursor."""\n'
    '    return request.headers.get("last-event-id", "")  # lup: ignore[dict-get] — open header map\n'
)
"""The Starlette header read, with its marker."""

SITE_LINE = 6
"""Where the `.get("literal")` and its directive sit in the text above."""

UNRELATED_EDIT = MARKED.replace(
    '"""Read the resume cursor."""', '"""Read the cursor the reader last saw."""'
)
"""The same file after an edit that never touches the marked line."""

HEADERS = ClassDeclaration(
    name="Headers",
    bases=["Mapping", "Collection"],
    path=Path("starlette/datastructures.py"),
    line=470,
)
"""What the sweep resolves `request.headers` to: a class in the mapping family."""


class SilentOracle(TypeOracle):
    """A checker that looked and could type nothing — the hook's answer here."""

    def declarations(
        self,
        queries: list[SymbolQuery],
        buffers: list[SourceBuffer] | None = None,
    ) -> list[Declaration]:
        return [UnknownDeclaration() for _ in queries]


class MappingOracle(TypeOracle):
    """A checker that resolves every receiver to Starlette's `Headers`."""

    def declarations(
        self,
        queries: list[SymbolQuery],
        buffers: list[SourceBuffer] | None = None,
    ) -> list[Declaration]:
        return [HEADERS for _ in queries]


def source(text: str) -> PythonSource:
    return PythonSource(path=Path("app.py"), module="app", text=text)


def test_an_edit_to_an_unrelated_hunk_leaves_the_marker_standing() -> None:
    """The kernel, told the receiver is unresolved, admits the edit outright.

    The marked line is not among the added lines, and even when the gate
    rescans it the unresolved verdict neither denies the line nor refuses
    the directive as dead.
    """
    rows = bundled_antipattern_rows()[".py"]
    unresolved = ResolutionRow(refuted={}, unresolved={"dict-get": [SITE_LINE]})

    decision = antipattern_decision(
        MARKED, UNRELATED_EDIT, rows, python_source=True, resolution=unresolved
    )

    assert decision is None or decision.effect == "allow"


def test_the_sweep_told_nothing_about_the_receiver_calls_no_marker_dead() -> None:
    """The repair's own audit, with the checker the hook actually has."""
    refuted = refute([source(MARKED)], SilentOracle(), PYTHON_ANTI_PATTERNS)["app.py"]

    assert [row.settled for row in refuted] == [False]
    assert audit_text(MARKED, PYTHON_ANTI_PATTERNS, refuted) == []


def test_the_sweep_resolving_the_receiver_demands_exactly_that_marker() -> None:
    """The other side of the loop: the sweep's verdict on the same file."""
    bare = MARKED.replace("  # lup: ignore[dict-get] — open header map", "")

    assert refute([source(MARKED)], MappingOracle(), PYTHON_ANTI_PATTERNS) == {}
    assert audit_text(MARKED, PYTHON_ANTI_PATTERNS) == []
    assert [finding.kind for finding in audit_text(bare, PYTHON_ANTI_PATTERNS)] == [
        "missing"
    ]


def test_the_post_write_repair_removes_nothing_it_cannot_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dev check --antipatterns --fix --path app.py`, as the hook runs it.

    The scoped sweep over the written file, with a checker that resolves
    nothing: no finding is spurious, so the repair pass rewrites nothing and
    the file goes on carrying the marker the whole-tree sweep demands.
    """
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    (work / "app.py").write_text(MARKED, encoding="utf-8")
    git("add", "-A")
    monkeypatch.chdir(work)
    monkeypatch.setattr(sweep, "default_oracle", SilentOracle)
    project = DevProject(package="app")

    scan = sweep.scan_antipatterns(project, ["app.py"])
    repaired = sweep.repair_spurious(project, scan.findings)

    assert scan.findings == []
    assert [row.settled for row in scan.refuted] == [False]
    assert repaired == []
    assert (work / "app.py").read_text(encoding="utf-8") == MARKED
