"""Behavior tests for the snapshot that makes a destructive command undoable.

The test that carries the module is `test_an_untracked_file_survives`: it is
the case `git stash create` misses, it is what `rm -rf` destroys, and it is
the whole reason this does not simply call the primitive the design named.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lup.devtools.dev import undo
from lup.execution.shell import git


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A real repository with one commit, since this module is git all the way."""
    git("-C", str(tmp_path), "init", "-q", "-b", "main")
    git("-C", str(tmp_path), "config", "user.email", "test@example.invalid")
    git("-C", str(tmp_path), "config", "user.name", "Test")
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("original\n", encoding="utf-8")
    git("-C", str(tmp_path), "add", "-A")
    git("-C", str(tmp_path), "commit", "-qm", "first")
    return tmp_path


def taken(root: Path, reason: str) -> undo.UndoPoint:
    """One snapshot, insisting it happened.

    Every test below is about what a snapshot *holds*, so "no snapshot" is
    never the subject and asserting it here keeps that assertion out of each
    of them. The one test that is about failing does not call this.
    """
    point = undo.snapshot(root, reason)
    assert point is not None
    return point


def test_an_untracked_file_survives_a_snapshot(checkout: Path) -> None:
    """The case `git stash create` misses, and the reason this module exists.

    A file written and not yet added is exactly what `rm -rf` destroys and
    exactly when somebody reaches for undo. `stash create` captures tracked
    modifications only, so relying on it would have produced a safety net
    with a hole where the accidents are.
    """
    (checkout / "new.txt").write_text("just written\n", encoding="utf-8")
    point = taken(checkout, "about to rm -rf")
    held = git.out("-C", str(checkout), "cat-file", "-p", f"{point.commit}:new.txt")
    assert held.strip() == "just written"


def test_a_modified_tracked_file_survives_a_snapshot(checkout: Path) -> None:
    (checkout / "tracked.txt").write_text("edited\n", encoding="utf-8")
    point = taken(checkout, "about to reset")
    held = git.out("-C", str(checkout), "cat-file", "-p", f"{point.commit}:tracked.txt")
    assert held.strip() == "edited"


def test_an_ignored_file_is_left_out(checkout: Path) -> None:
    """Stated as a limit rather than a bug: capturing them costs 592 MB here.

    Which is why `git clean -fdx` keeps asking -- it is the one command whose
    purpose is destroying exactly what this does not hold.
    """
    (checkout / "ignored").mkdir()
    (checkout / "ignored" / "big.bin").write_text("x", encoding="utf-8")
    point = taken(checkout, "about to clean")
    listed = git.out("-C", str(checkout), "ls-tree", "-r", "--name-only", point.commit)
    assert "ignored/big.bin" not in listed


def test_a_snapshot_leaves_the_real_index_and_working_tree_alone(
    checkout: Path,
) -> None:
    """A safety net that stages things would rewrite what a human was composing."""
    (checkout / "new.txt").write_text("unstaged\n", encoding="utf-8")
    undo.snapshot(checkout, "probe")
    assert git.out("-C", str(checkout), "status", "--porcelain").strip() == "?? new.txt"


def test_snapshots_list_newest_first_with_the_reason_they_were_taken(
    checkout: Path,
) -> None:
    """A list of timestamps is not something anybody can choose from."""
    (checkout / "a.txt").write_text("a\n", encoding="utf-8")
    undo.snapshot(checkout, "first reason")
    (checkout / "b.txt").write_text("b\n", encoding="utf-8")
    undo.snapshot(checkout, "second reason")
    found = undo.points(checkout)
    assert [item.reason for item in found] == ["second reason", "first reason"]


def test_a_snapshot_names_the_command_it_can_be_restored_with(checkout: Path) -> None:
    """Printed rather than run: restoring overwrites present work with past work."""
    point = taken(checkout, "probe")
    assert point.restore_command() == (
        f"git restore --source {point.commit} --worktree ."
    )


def test_expiry_drops_only_what_is_past_the_window(checkout: Path) -> None:
    """A net that fills the disk is a different kind of hazard."""
    (checkout / "a.txt").write_text("a\n", encoding="utf-8")
    undo.snapshot(checkout, "recent")
    assert undo.expire(checkout, keep_days=7) == []
    later = datetime.now(UTC) + timedelta(days=8)
    gone = undo.expire(checkout, keep_days=7, now=later)
    assert [item.reason for item in gone] == ["recent"]
    assert undo.points(checkout) == []


def test_a_checkout_with_no_snapshots_reports_none(checkout: Path) -> None:
    assert undo.points(checkout) == []


def test_a_failed_snapshot_is_reported_rather_than_raised(tmp_path: Path) -> None:
    """A net that stops the thing it protects is worse than no net."""
    spoken = undo.snapshot_quietly(tmp_path / "not-a-repository", "probe")
    assert spoken.startswith("no snapshot taken")


def test_an_unrecognisable_ref_line_is_skipped_rather_than_crashing() -> None:
    """`for-each-ref` output is a format this module asked for, not a contract."""
    assert undo.UndoPoint.parse("only\ttwo") is None
