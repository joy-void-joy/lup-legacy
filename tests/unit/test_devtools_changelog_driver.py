"""Two branches each adding a changelog entry at one place, merged by git.

`.gitattributes` leaves the changelog on git's own `union` driver, which needs
no registration, so every clone and forge merges it without a conflict. Union
lines the two entries' text up, though: a line both share is kept once, and
the first entry's last line runs into the second's heading. A clone that
registered its merge drivers hands the changelog to `lup-changelog` through
its own attributes instead, which merges entry by entry, so both stay whole
and set off, and only an entry both branches changed stops the merge.
"""

import sys
from pathlib import Path

import pytest
import sh

from lup.devtools.changelog import merged_changelog
from lup.devtools.dev import worktree
from tests.unit.repos import initialized_repo

ROOT = Path(__file__).parents[2]
SHARED = "What changes for a session: retry it once."


def entry(heading: str, body: str) -> str:
    """One entry as an author writes it, set off from whatever follows."""
    return f"### {heading}\n\n{body}\n\n"


def changelog(*entries: str) -> str:
    """A changelog whose open section holds *entries*, above one release."""
    return (
        "# Changelog\n\n## Unreleased\n\n"
        + "".join(entries)
        + "## 1.0.0 — 2026-01-01\n\n- First.\n"
    )


OLDER = entry("An older entry", "It was already here.")
OURS = entry("Ours", f"Our change.\n\n{SHARED}")
THEIRS = entry("Theirs", f"Their change.\n\n{SHARED}")


def bare_git(work: Path) -> sh.Command:
    return sh.Command("git").bake(
        "-C", str(work), "-c", "commit.gpgsign=false", _tty_out=False
    )


def committed(work: Path, branch: str, document: str) -> None:
    """*document* as *branch*'s changelog, committed on a branch cut from main."""
    git = bare_git(work)
    git("switch", "-c", branch, "main")
    (work / "CHANGELOG.md").write_text(document, encoding="utf-8")
    git("commit", "-am", f"docs: {branch}'s entry")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository carrying this one's attributes, with two branches off one changelog."""
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    (work / ".gitattributes").write_text(
        (ROOT / ".gitattributes").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (work / "CHANGELOG.md").write_text(changelog(OLDER), encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "chore: base")
    committed(work, "ours", changelog(OURS, OLDER))
    committed(work, "theirs", changelog(THEIRS, OLDER))
    bare_git(work)("switch", "ours")
    monkeypatch.chdir(work)
    return work


def registered(work: Path) -> None:
    """Register the drivers as `git merge-driver` does, run through this environment's own script.

    The registered command is `uv run lup-devtools …`, which finds the
    project from the checkout it runs in; a scratch repository holds none,
    so the test runs the same command through the script `uv run` would.
    """
    worktree.register_merge_driver()
    script = Path(sys.executable).parent / "lup-devtools"
    bare_git(work)(
        "config",
        f"merge.{worktree.CHANGELOG_MERGE_DRIVER}.driver",
        f"{script} git merge-changelog %O %A %B",
    )


def test_registration_hands_the_changelog_to_its_driver(repo: Path) -> None:
    assert sorted(worktree.unregistered_merge_drivers()) == sorted(
        [worktree.OWNERSHIP_MERGE_DRIVER, worktree.CHANGELOG_MERGE_DRIVER]
    )
    assert str(bare_git(repo)("check-attr", "merge", "--", "CHANGELOG.md")) == (
        "CHANGELOG.md: merge: union\n"
    )

    worktree.register_merge_driver()

    assert worktree.unregistered_merge_drivers() == []
    assert str(bare_git(repo)("check-attr", "merge", "--", "CHANGELOG.md")) == (
        f"CHANGELOG.md: merge: {worktree.CHANGELOG_MERGE_DRIVER}\n"
    )
    assert str(
        bare_git(repo)(
            "config", "--get", f"merge.{worktree.CHANGELOG_MERGE_DRIVER}.driver"
        )
    ) == ("uv run lup-devtools git merge-changelog %O %A %B\n")
    worktree.register_merge_driver()
    assert (
        worktree.clone_attributes()
        .read_text()
        .splitlines()
        .count(worktree.changelog_attribute())
        == 1
    )


def test_two_entries_added_at_one_place_merge_whole_and_set_off(repo: Path) -> None:
    registered(repo)

    bare_git(repo)("merge", "--no-edit", "theirs")

    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == changelog(
        THEIRS, OURS, OLDER
    )


def test_without_the_driver_union_merges_but_drops_a_line_both_entries_share(
    repo: Path,
) -> None:
    """Pinned rather than left implied: the fallback a clone that never registered keeps."""
    bare_git(repo)("merge", "--no-edit", "theirs")

    merged = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "<<<<<<<" not in merged
    assert merged.count(SHARED) == 1


def test_an_entry_both_branches_changed_stops_the_merge(repo: Path) -> None:
    registered(repo)
    git = bare_git(repo)
    for branch, document in (
        ("theirs", changelog(THEIRS, entry("An older entry", "Theirs says that."))),
        ("ours", changelog(OURS, entry("An older entry", "Ours says this."))),
    ):
        git("switch", branch)
        (repo / "CHANGELOG.md").write_text(document, encoding="utf-8")
        git("commit", "-am", f"docs: {branch} rewords the older entry")

    with pytest.raises(sh.ErrorReturnCode):
        git("merge", "--no-edit", "theirs")

    assert str(git("diff", "--name-only", "--diff-filter=U")).split() == [
        "CHANGELOG.md"
    ]
    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == changelog(
        THEIRS,
        OURS,
        "<<<<<<< ours\n### An older entry\n\nOurs says this.\n=======\n"
        "### An older entry\n\nTheirs says that.\n>>>>>>> theirs\n\n",
    )


def test_this_repository_s_changelog_merges_to_itself() -> None:
    """Every entry and release here is set off as the driver renders it, so no merge rewrites what nobody changed."""
    document = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert merged_changelog(document, document, document).text == document
