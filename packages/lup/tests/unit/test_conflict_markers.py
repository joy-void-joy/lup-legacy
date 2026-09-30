"""A conflict a merge left in a tracked file is refused before it is committed.

A merge committed its markers into a markdown passage and the page generated
from it, and the whole gate passed 23 of 23: nothing looked for them.
"""

import shutil
from pathlib import Path

import pytest
import typer

import lup.devtools.dev.check as check
from lup.devtools.dev.check import conflict_marker_report
from lup.devtools.dev.conflicts import (
    committing,
    conflict_blocks,
    staged_paths,
    staged_text,
)
from lup.devtools.dev.git_guards import DECLARED_GUARDS
from lup.execution.shell import git

CONFLICT = "<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> feature\n"


def test_a_block_is_named_by_the_line_it_opens_on() -> None:
    assert conflict_blocks(f"intro\n\n{CONFLICT}") == [3]


def test_a_block_left_by_a_three_way_merge_is_one_block() -> None:
    three_way = (
        "<<<<<<< HEAD\nx = 1\n||||||| base\nx = 0\n=======\nx = 2\n>>>>>>> feature\n"
    )

    assert conflict_blocks(three_way) == [1]


@pytest.mark.parametrize(
    "text",
    [
        "<<<<<<< HEAD\nx = 1\n",
        "=======\n>>>>>>> feature\n<<<<<<< HEAD\n",
        "a heading\n=======\n",
        'quoted = "<<<<<<< HEAD"\n',
    ],
)
def test_markers_that_are_not_a_block_in_order_are_not_one(text: str) -> None:
    """A lone marker, the three out of order, a setext heading, a quote."""
    assert conflict_blocks(text) == []


def test_a_directive_heading_the_block_s_paragraph_excuses_it() -> None:
    """The one exemption is a marker in the file, scoped to the block it heads.

    A fixture holding a conflict on purpose writes it inside a string, where
    no comment can share the marker's line; the paragraph the directive heads
    is how far it reaches, so a real conflict elsewhere in that file is still
    found.
    """
    fixture = (
        "# lup: ignore[conflict-marker] — a fixture holding one on purpose\n"
        'CONFLICTED = """\n'
        f"{CONFLICT}"
        '"""\n'
        "\n"
        f"{CONFLICT}"
    )

    assert conflict_blocks(fixture) == [10]


def test_the_report_names_each_file_and_line(tmp_path: Path) -> None:
    (tmp_path / "page.md").write_text(f"# Page\n\n{CONFLICT}", encoding="utf-8")
    (tmp_path / "clean.md").write_text("# Clean\n", encoding="utf-8")

    report = conflict_marker_report(
        ["page.md", "clean.md"], lambda path: (tmp_path / path).read_text()
    )

    assert not report.passed
    assert report.lines[1:-1] == ["  page.md:3"]


def test_a_tree_without_blocks_passes(tmp_path: Path) -> None:
    (tmp_path / "clean.md").write_text("# Clean\n", encoding="utf-8")

    report = conflict_marker_report(
        ["clean.md"], lambda path: (tmp_path / path).read_text()
    )

    assert report.passed


def test_the_staged_content_is_what_a_commit_is_judged_by(tmp_path: Path) -> None:
    """What the commit will hold, not what the working tree holds now."""
    git("init", "-q", "-b", "main", str(tmp_path))
    page = tmp_path / "page.md"
    page.write_text(CONFLICT, encoding="utf-8")
    git("-C", str(tmp_path), "add", "page.md")
    page.write_text("resolved\n", encoding="utf-8")

    assert conflict_blocks(staged_text(tmp_path, None, "page.md") or "") == [1]


def test_a_commit_holding_a_block_is_refused_at_pre_commit() -> None:
    """Merges included: a merge's commit is where markers are committed."""
    guards = [
        guard
        for guard in DECLARED_GUARDS
        if guard.hook == "pre-commit" and "--conflict-markers" in guard.command
    ]

    assert len(guards) == 1
    assert "--staged" in guards[0].command
    assert guards[0].standdown is None


def test_the_index_a_commit_is_made_from_is_the_one_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`git commit -a` commits from an index git made for it, named only to the hook.

    The checkout's own index holds the file clean; the index being committed
    holds the block. Read from the checkout's, the guard passed a commit that
    was about to record the block.
    """
    git("init", "-q", "-b", "main", str(tmp_path))
    page = tmp_path / "page.md"
    page.write_text("clean\n", encoding="utf-8")
    git("-C", str(tmp_path), "add", "page.md")
    page.write_text(CONFLICT, encoding="utf-8")
    index = tmp_path / ".git" / "next-index"
    shutil.copyfile(tmp_path / ".git" / "index", index)
    git("-C", str(tmp_path), "add", "page.md", _env=committing(index))

    assert staged_text(tmp_path, None, "page.md") == "clean\n"
    assert conflict_blocks(staged_text(tmp_path, index, "page.md") or "") == [1]
    assert staged_paths(tmp_path, index) == ["page.md"]

    monkeypatch.setattr(check, "project_root", lambda: tmp_path)
    check.run_conflict_markers(True)
    with pytest.raises(typer.Exit):
        check.run_conflict_markers(True, Path(".git/next-index"))
    assert "  page.md:1" in capsys.readouterr().out.splitlines()
