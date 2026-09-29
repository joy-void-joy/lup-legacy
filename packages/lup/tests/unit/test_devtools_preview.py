"""Behavior tests for asking what landing a branch would change.

A commit that landed through a cherry-pick onto moved context carries a new
patch-id, so containment calls it unique forever. What has to hold: such a
branch reads as nothing new, its commit reads as rewritten rather than new,
a branch that would conflict names the file, a branch with real work names
what it changes, and two branches touching one file are said to share it.
"""

from pathlib import Path

import pytest
import sh

from lup.devtools.dev.preview import preview


def git_in(work: Path) -> sh.Command:
    """A git bound to one checkout, with identity per call and no hooks."""
    return sh.Command("git").bake(
        "-C",
        str(work),
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.email=preview@example.test",
        "-c",
        "user.name=Preview Test",
        "-c",
        f"core.hooksPath={work / '.no-hooks'}",
        _tty_out=False,
    )


BASE = "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\n"
"""Ten lines, so a change three above the last shifts its context and not its merge."""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sh.Command:
    """``main`` and three branches cut from one base.

    - ``rewritten``: its one commit reached ``main`` by a cherry-pick made
      after ``main`` changed a line in its context, so the patch-ids differ.
    - ``clashing``: rewrites the line ``main`` also rewrote.
    - ``fresh``: adds a file nobody else has, and touches ``f.txt`` too.
    """
    work = tmp_path / "repo"
    work.mkdir()
    git = git_in(work)
    git("init", "-b", "main")

    def commit(name: str, content: str, message: str) -> None:
        (work / name).write_text(content, encoding="utf-8")
        git("add", name)
        git("commit", "-m", message)

    commit("f.txt", BASE, "base")
    git("checkout", "-b", "rewritten")
    commit("f.txt", BASE.replace("j", "J"), "feat: the last line")
    git("checkout", "main", "-b", "clashing")
    commit("f.txt", BASE.replace("a", "A2"), "feat: the first line, differently")
    git("checkout", "main", "-b", "fresh")
    commit("new.txt", "new\n", "feat: a file of its own")
    commit("f.txt", BASE.replace("d", "D"), "feat: a middle line")
    git("checkout", "main")
    commit("f.txt", BASE.replace("a", "A"), "fix: the first line")
    commit(
        "f.txt", BASE.replace("a", "A").replace("g", "G"), "fix: a line in its context"
    )
    git("cherry-pick", "rewritten")
    monkeypatch.chdir(work)
    return git


def test_a_cherry_picked_branch_reads_as_nothing_new(repo: sh.Command) -> None:
    result = preview(["rewritten"], "main").landings[0]

    assert result.changes == []
    assert result.conflicts == []
    assert result.verdict().startswith("nothing new")


def test_its_commit_reads_as_rewritten_not_new(repo: sh.Command) -> None:
    (commit,) = preview(["rewritten"], "main").landings[0].unique

    assert commit.subject == "feat: the last line"
    assert commit.twin
    assert commit.reading() == "rewritten"


def test_a_clashing_branch_names_the_file(repo: sh.Command) -> None:
    result = preview(["clashing"], "main").landings[0]

    assert result.conflicts == ["f.txt"]
    assert result.unique[0].reading() == "new"


def test_real_work_names_what_it_would_change(repo: sh.Command) -> None:
    result = preview(["fresh"], "main").landings[0]

    assert result.conflicts == []
    assert {change.path: change.moved() for change in result.changes} == {
        "f.txt": "+1 -1",
        "new.txt": "+1 -0",
    }


def test_two_branches_touching_one_file_share_it(repo: sh.Command) -> None:
    overlaps = preview(["clashing", "fresh", "rewritten"], "main").overlaps

    assert [(o.first, o.second, o.shared) for o in overlaps] == [
        ("clashing", "fresh", ["f.txt"]),
        ("clashing", "rewritten", ["f.txt"]),
        ("fresh", "rewritten", ["f.txt"]),
    ]
