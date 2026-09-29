"""Behavior tests for settling a conflict made of two same-place insertions.

Two branches each prepend an entry to one declared list — the recurring shape
of a changelog or registry conflict. What has to hold: both entries survive
in a stated order, an edit elsewhere in the file rides along, a change that
both sides made alike lands once, and two different changes to one line are
refused with the file left exactly as git left it.
"""

from pathlib import Path

import pytest
import sh
import typer

from lup.devtools.dev.union import conflict_union, union_merge


def git_in(work: Path) -> sh.Command:
    """A git bound to one checkout, with identity per call and no hooks."""
    return sh.Command("git").bake(
        "-C",
        str(work),
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.email=union@example.test",
        "-c",
        "user.name=Union Test",
        "-c",
        f"core.hooksPath={work / '.no-hooks'}",
        _tty_out=False,
    )


DECLARED = "DECLARED = [\n    'first',\n]\n"
"""A list both branches prepend to, as a registry is."""


def lines(text: str) -> list[str]:
    """A version of a file as the merge reads it."""
    return text.splitlines(keepends=True)


def test_two_insertions_at_one_place_keep_both_ours_first() -> None:
    merged = union_merge(
        "m.py",
        lines(DECLARED),
        lines("DECLARED = [\n    'ours',\n    'first',\n]\n"),
        lines("DECLARED = [\n    'theirs',\n    'first',\n]\n"),
    )

    assert merged.settled()
    assert "".join(merged.merged) == (
        "DECLARED = [\n    'ours',\n    'theirs',\n    'first',\n]\n"
    )
    assert [(block.line, block.ours, block.theirs) for block in merged.combined] == [
        (2, 1, 1)
    ]


def test_theirs_first_reverses_the_order_and_says_so() -> None:
    merged = union_merge(
        "m.py",
        lines(DECLARED),
        lines("DECLARED = [\n    'ours',\n    'first',\n]\n"),
        lines("DECLARED = [\n    'theirs',\n    'first',\n]\n"),
        first="theirs",
    )

    assert "".join(merged.merged).startswith("DECLARED = [\n    'theirs',\n    'ours',")
    assert merged.combined[0].first == "theirs"


def test_an_edit_beside_the_insertion_rides_along() -> None:
    """The shape git refuses and a hand-resolution drops a side of."""
    base = "HEAD\nA\nB\n"

    merged = union_merge(
        "m.py", lines(base), lines("HEAD\nX\nA\nB\n"), lines("HEAD\nA2\nB\n")
    )

    assert merged.settled()
    assert "".join(merged.merged) == "HEAD\nX\nA2\nB\n"


def test_a_change_both_sides_made_alike_lands_once() -> None:
    same = "DECLARED = [\n    'both',\n    'first',\n]\n"

    merged = union_merge("m.py", lines(DECLARED), lines(same), lines(same))

    assert "".join(merged.merged) == same
    assert merged.combined == []


def test_two_different_changes_to_one_line_are_refused() -> None:
    merged = union_merge(
        "m.py", lines("a\nb\nc\n"), lines("a\nB1\nc\n"), lines("a\nB2\nc\n")
    )

    assert not merged.settled()
    assert merged.merged == []
    assert [(clash.ours, clash.theirs) for clash in merged.clashes] == [("2-2", "2-2")]


def test_an_insertion_inside_a_rewritten_span_is_refused() -> None:
    merged = union_merge(
        "m.py", lines("a\nb\nc\nd\n"), lines("a\nb\nX\nc\nd\n"), lines("a\nB\nC\nd\n")
    )

    assert not merged.settled()


@pytest.fixture
def prepended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout mid-merge where both branches prepended to one list."""
    work = tmp_path / "repo"
    work.mkdir()
    git = git_in(work)
    git("init", "-b", "main")
    (work / "declared.py").write_text(DECLARED, encoding="utf-8")
    git("add", "declared.py")
    git("commit", "-m", "base")
    git("checkout", "-b", "topic")
    (work / "declared.py").write_text(
        "DECLARED = [\n    'topic',\n    'first',\n]\n", encoding="utf-8"
    )
    git("commit", "-am", "topic entry")
    git("checkout", "main")
    (work / "declared.py").write_text(
        "DECLARED = [\n    'main',\n    'first',\n]\n", encoding="utf-8"
    )
    git("commit", "-am", "main entry")
    with pytest.raises(sh.ErrorReturnCode):
        git("merge", "topic")
    monkeypatch.chdir(work)
    return work


def test_a_real_merge_is_settled_and_staged(prepended: Path) -> None:
    conflict_union([Path("declared.py")], "ours", dry_run=False, as_json=True)

    assert (prepended / "declared.py").read_text(encoding="utf-8") == (
        "DECLARED = [\n    'main',\n    'topic',\n    'first',\n]\n"
    )
    unmerged = str(git_in(prepended)("diff", "--name-only", "--diff-filter=U"))
    assert unmerged.strip() == ""


def test_a_dry_run_writes_nothing(prepended: Path) -> None:
    before = (prepended / "declared.py").read_text(encoding="utf-8")

    conflict_union([Path("declared.py")], "ours", dry_run=True, as_json=True)

    assert (prepended / "declared.py").read_text(encoding="utf-8") == before


def test_a_real_clash_leaves_the_markers_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "repo"
    work.mkdir()
    git = git_in(work)
    git("init", "-b", "main")
    (work / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-m", "base")
    git("checkout", "-b", "topic")
    (work / "f.txt").write_text("a\nB1\nc\n", encoding="utf-8")
    git("commit", "-am", "topic")
    git("checkout", "main")
    (work / "f.txt").write_text("a\nB2\nc\n", encoding="utf-8")
    git("commit", "-am", "main")
    with pytest.raises(sh.ErrorReturnCode):
        git("merge", "topic")
    monkeypatch.chdir(work)
    left = (work / "f.txt").read_text(encoding="utf-8")

    with pytest.raises(typer.Exit):
        conflict_union([Path("f.txt")], "ours", dry_run=False, as_json=True)

    assert (work / "f.txt").read_text(encoding="utf-8") == left
