"""The safety net the permission dispatcher takes before it lets a command run.

The whole case for relaxing a lattice is that a mistake can be put back, so
what is pinned here is the two ways this stops being true silently. It can
capture the wrong thing -- `git stash create` misses exactly the file `rm -rf`
destroys -- and it can stop the command it was protecting, which is worse than
not existing, because a net that breaks the working case is the first thing
somebody turns off.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from lup.execution.shell import git
from lup.policy.assets.host import undo_expire, undo_namespace, undo_snapshot
from lup.devtools.dev.undo import damaged_refs, repair_refs

import pytest
import sh


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A real repository with one commit, since this is git all the way down."""
    git("-C", str(tmp_path), "init", "-q", "-b", "main")
    git("-C", str(tmp_path), "config", "user.email", "test@example.invalid")
    git("-C", str(tmp_path), "config", "user.name", "Test")
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("original\n", encoding="utf-8")
    git("-C", str(tmp_path), "add", "-A")
    git("-C", str(tmp_path), "commit", "-qm", "first")
    return tmp_path


def held(checkout: Path, reference: str, path: str) -> str:
    """What one snapshot holds at a path, read back out of the object store."""
    return git.out("-C", str(checkout), "cat-file", "-p", f"{reference}:{path}").strip()


def test_a_file_written_and_not_yet_added_survives(checkout: Path) -> None:
    """The case `git stash create` misses, and the reason this exists at all.

    A file written thirty seconds ago and not staged is exactly what
    `rm -rf src/` destroys and exactly when somebody reaches for undo.
    """
    (checkout / "new.txt").write_text("just written\n", encoding="utf-8")
    assert held(checkout, undo_snapshot(checkout, "rm -rf ."), "new.txt") == (
        "just written"
    )


def test_the_snapshot_does_not_stage_what_it_captured(checkout: Path) -> None:
    """A net that quietly rewrote the index would break what it was protecting.

    The snapshot runs in front of a command a human may be in the middle of
    composing a commit for. Staging everything as a side effect would change
    what their next `git commit` records, which is a destruction of its own.
    """
    (checkout / "new.txt").write_text("just written\n", encoding="utf-8")
    undo_snapshot(checkout, "rm -rf .")
    assert git.out("-C", str(checkout), "diff", "--cached", "--name-only") == ""


def test_the_snapshot_leaves_the_working_tree_alone(checkout: Path) -> None:
    """The other half of the same promise, asked of the files rather than the index."""
    (checkout / "tracked.txt").write_text("edited\n", encoding="utf-8")
    undo_snapshot(checkout, "git reset --hard")
    assert (checkout / "tracked.txt").read_text() == "edited\n"


def test_an_ignored_file_is_left_out(checkout: Path) -> None:
    """A stated limit rather than a bug: capturing them measured 592 MB.

    Which is why `git clean -fdx` keeps asking. It is the one command whose
    purpose is destroying precisely what this does not hold, so the lattice
    cannot hand it the recoverability argument.
    """
    (checkout / "ignored").mkdir()
    (checkout / "ignored" / "big.bin").write_text("x", encoding="utf-8")
    reference = undo_snapshot(checkout, "git clean -fdx")
    listed = git.out("-C", str(checkout), "ls-tree", "-r", "--name-only", reference)
    assert "ignored/big.bin" not in listed


def test_the_snapshot_lands_outside_every_ref_a_human_reads(checkout: Path) -> None:
    """A branch listing, a push and a fetch must all stay unaware of these."""
    reference = undo_snapshot(checkout, "probe")
    assert reference.startswith(f"{undo_namespace()}/")
    assert git.out("-C", str(checkout), "branch", "--list").strip() == "* main"


def test_a_checkout_that_cannot_answer_yields_no_snapshot(tmp_path: Path) -> None:
    """Silence, not an exception. This runs in front of a command somebody asked for.

    A checkout mid-merge, a locked index and a directory that is not a
    repository are all reasons a snapshot cannot be taken, and none of them is
    a reason to stop the command it was standing in front of.
    """
    assert undo_snapshot(tmp_path / "not-a-repository", "probe") == ""


def test_no_root_yields_no_snapshot() -> None:
    """A hook is promised no working directory, so this has to be an answer."""
    assert undo_snapshot(None, "probe") == ""


def test_the_environment_the_snapshot_needs_does_not_replace_the_rest(
    checkout: Path,
) -> None:
    """Pointing GIT_INDEX_FILE elsewhere must not drop PATH and HOME with it.

    A replaced environment fails for a reason that has nothing to do with what
    was asked, and the failure is silent here by design -- so it would read as
    "this checkout cannot be snapshotted" forever.
    """
    (checkout / "new.txt").write_text("present\n", encoding="utf-8")
    assert undo_snapshot(checkout, "probe") != ""


def test_a_command_that_changed_nothing_leaves_no_new_snapshot(
    checkout: Path,
) -> None:
    """Why this can afford to run before *every* command rather than some.

    Git addresses content, so an unchanged tree writes a byte-identical tree
    object -- and naming the ref after that tree makes the second write an
    overwrite rather than an addition. Measured against the alternative: a
    trigger firing on the classifier's verdict produces dozens of refs in one
    session, nearly all of them in front of a `grep`.
    """
    (checkout / "new.txt").write_text("present\n", encoding="utf-8")
    undo_snapshot(checkout, "one")
    undo_snapshot(checkout, "two")
    assert len(git.lines("-C", str(checkout), "for-each-ref", undo_namespace())) == 1


def test_the_surviving_note_is_the_latest_thing_about_to_happen(
    checkout: Path,
) -> None:
    """What a reader looks for is the snapshot from before the thing that broke.

    Sixty reads between two edits collapse to one ref, and the note on it has
    to be the last command rather than the first, or the listing points at the
    wrong moment while holding the right tree.
    """
    (checkout / "new.txt").write_text("present\n", encoding="utf-8")
    undo_snapshot(checkout, "grep something")
    reference = undo_snapshot(checkout, "rm -rf src")
    subject = git.out("-C", str(checkout), "log", "-1", "--format=%s", reference)
    assert subject.strip() == "lup undo: rm -rf src"


def test_a_changed_tree_gets_a_snapshot_of_its_own(checkout: Path) -> None:
    """Dedup must not swallow a state worth returning to."""
    first = undo_snapshot(checkout, "before")
    (checkout / "new.txt").write_text("written since\n", encoding="utf-8")
    assert undo_snapshot(checkout, "after") != first


def test_deduplication_is_atomic_when_a_retired_ref_is_locked(checkout: Path) -> None:
    first = undo_snapshot(checkout, "before")
    original = git.out("-C", str(checkout), "rev-parse", first)
    lock = checkout / ".git" / f"{first}.lock"
    lock.write_text("another writer holds this ref\n")
    assert undo_snapshot(checkout, "replacement") == ""
    assert git.out("-C", str(checkout), "rev-parse", first) == original
    assert git.lines(
        "-C", str(checkout), "for-each-ref", "--format=%(refname)", undo_namespace()
    ) == [first]
    assert lock.read_text() == "another writer holds this ref\n"
    git("-C", str(checkout), "fsck", "--no-dangling")


@pytest.mark.parametrize("contents", [b"", b"0" * 40 + b"\n"])
def test_broken_undo_refs_are_quarantined_and_fetch_recovers(
    checkout: Path, contents: bytes
) -> None:
    # Beside the checkout rather than inside it, and named for it: the
    # `checkout` fixture *is* `tmp_path`, so its parent is the runner's own
    # root and a bare `origin.git` there is a path every case in this worker
    # would clone to. The second one met `destination path already exists`.
    origin = checkout.parent / f"{checkout.name}-origin.git"
    git("clone", "--bare", str(checkout), str(origin))
    git("-C", str(checkout), "remote", "add", "origin", str(origin))
    first = undo_snapshot(checkout, "good state")
    saved = git.out("-C", str(checkout), "rev-parse", first)
    broken = checkout / ".git" / undo_namespace() / "broken"
    broken.write_bytes(contents)
    with pytest.raises(sh.ErrorReturnCode):
        git("-C", str(checkout), "fetch", "origin")
    assert [damage.path for damage in damaged_refs(checkout)] == [broken]
    recovered = repair_refs(checkout)
    assert len(recovered) == 1 and recovered[0].read_bytes() == contents
    assert not broken.exists()
    assert git.out("-C", str(checkout), "rev-parse", first) == saved
    git("-C", str(checkout), "fetch", "origin")
    git("-C", str(checkout), "fsck", "--no-dangling")


def test_undo_repair_respects_another_writers_lock(checkout: Path) -> None:
    broken = checkout / ".git" / undo_namespace() / "broken"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"")
    lock = broken.with_suffix(".lock")
    lock.write_text("owned")
    with pytest.raises(FileExistsError):
        repair_refs(checkout)
    assert broken.exists() and lock.read_text() == "owned"


def test_an_earlier_state_survives_the_states_that_followed_it(
    checkout: Path,
) -> None:
    """The point of keeping one ref per state rather than only the newest."""
    (checkout / "new.txt").write_text("first\n", encoding="utf-8")
    reference = undo_snapshot(checkout, "before the edit")
    (checkout / "new.txt").write_text("second\n", encoding="utf-8")
    undo_snapshot(checkout, "after the edit")
    assert held(checkout, reference, "new.txt") == "first"


def aged(checkout: Path, days: int, tree: str) -> str:
    """A snapshot ref stamped as though it were taken `days` ago.

    Written by hand rather than by waiting, and given a tree part of its own
    so the dedup pass -- which retires every earlier ref holding the tree the
    new snapshot holds -- cannot be what removed it.
    """
    commit = git.out("-C", str(checkout), "rev-parse", "HEAD").strip()
    stamp = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y%m%dT%H%M%S%f")
    reference = f"{undo_namespace()}/{stamp}-{tree}"
    git("-C", str(checkout), "update-ref", reference, commit)
    return reference


def references(checkout: Path) -> list[str]:
    """Every snapshot ref this checkout currently holds."""
    listed = git.out(
        "-C", str(checkout), "for-each-ref", "--format=%(refname)", undo_namespace()
    )
    return listed.splitlines()


def test_the_first_snapshot_of_a_session_retires_what_outlived_the_window(
    checkout: Path,
) -> None:
    """The retention window is a fact about the namespace, not a docstring.

    Nothing else runs the expiry: the command that offers it does so behind a
    flag, so a window nobody passes that flag to holds every snapshot ever
    taken.
    """
    old = aged(checkout, 30, "aaaaaaaaaaaa")
    recent = aged(checkout, 1, "bbbbbbbbbbbb")
    undo_snapshot(checkout, "probe", session="cold")
    assert old not in references(checkout)
    assert recent in references(checkout)


def test_a_later_snapshot_in_one_session_does_not_pay_for_the_sweep(
    checkout: Path,
) -> None:
    """Once a session, because the net is paid for on every mutating command.

    The session's index is what says which call this is, so the sweep lands on
    the one call already paying for a cold index rather than on all of them.
    """
    undo_snapshot(checkout, "first", session="warm")
    old = aged(checkout, 30, "aaaaaaaaaaaa")
    (checkout / "new.txt").write_text("written since\n", encoding="utf-8")
    undo_snapshot(checkout, "second", session="warm")
    assert old in references(checkout)


def test_the_cap_retires_the_oldest_past_it(checkout: Path) -> None:
    """The window cannot bound a burst, which is why there are two bounds.

    Every ref here sits well inside the retention window, so age retires none
    of them and what is left is the cap alone.
    """
    written = [aged(checkout, 5 - index, f"{index:012d}") for index in range(5)]

    undo_expire(checkout, keep_most=3)

    assert references(checkout) == sorted(written[-3:])


def test_the_cap_counts_the_snapshot_being_taken_beside_it(checkout: Path) -> None:
    """Swept after the ref is written, so "at most three" means three.

    Sweeping first would count a namespace the new snapshot is not in yet and
    leave the cap meaning one thing here and another to `dev undo --keep`.
    """
    for index in range(4):
        aged(checkout, 4 - index, f"{index:012d}")

    undo_expire(checkout, keep_most=3)

    assert len(references(checkout)) == 3
