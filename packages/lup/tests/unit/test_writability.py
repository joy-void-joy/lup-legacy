"""Git's lock protocol read off the filesystem rather than out of its words.

`File exists` is what git says both when a lock is stale and when the path it
names is a device node a sandbox put there, so every test here pins the mount
state and the lock's age — the only things that separate the two — and none of
them pins a message git printed. The pair matters more than either half: the
remedies are opposite, and a diagnosis that names one of them for both sends
half its readers away from the fix.

`/dev/null` is reached through a symlink because that is a device node any
user can point at; bind-mounting one, which is what the sandbox does, needs
privileges a test does not have and produces the same `stat`.
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lup.devtools.utils import clear_stale_config_locks, config_lock_diagnosis
from lup.execution.writability import diagnosis, inspect_git_admin
from lup.execution.process import LaunchRequest, LocalProcessLauncher


def admin_dir(root: Path) -> Path:
    """A writable admin directory holding the config git writes through."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    return root


def aged_lock(root: Path, name: str, age: timedelta) -> Path:
    """A lock file of a given age, written the way git leaves one behind."""
    path = root / name
    path.write_text("")
    stamp = (datetime.now(UTC) - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_a_writable_admin_directory_reports_nothing(tmp_path: Path) -> None:
    assert inspect_git_admin(admin_dir(tmp_path / "admin")) == []
    assert diagnosis(inspect_git_admin(admin_dir(tmp_path / "admin"))) == ""


def linked_worktree(tmp_path: Path) -> Path:
    """A second worktree of a real repository, sharing the one config it has.

    The lease the resolver hands each concern is exactly this: a checkout
    whose own ``.git`` is a file, whose config lives in the checkout that
    made it, and which therefore cannot be diagnosed by looking beside
    itself.
    """
    launcher = LocalProcessLauncher()
    work = tmp_path / "work"
    for arguments in (
        ["git", "init", "-b", "source", str(work)],
        # Identity per invocation, never `git config` — a misbound command then
        # writes nothing, where a persisted setting lands in the shared config
        # every worktree of a real repository inherits (see `lup.devtools.gitguard`).
        [
            "git",
            "-C",
            str(work),
            "-c",
            "user.email=locks@example.test",
            "-c",
            "user.name=Lock Test",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        ["git", "-C", str(work), "worktree", "add", str(tmp_path / "leased")],
    ):
        status = launcher.launch(LaunchRequest(arguments=arguments, cwd=tmp_path))
        if status.code != 0:
            raise AssertionError(status.stderr)
    return tmp_path / "leased"


def test_a_real_repository_is_left_alone(tmp_path: Path) -> None:
    """The layout git actually creates, so the check cannot fire on a normal tree."""
    leased = linked_worktree(tmp_path)

    assert config_lock_diagnosis(tmp_path / "work") == ""
    assert config_lock_diagnosis(leased) == ""


def test_a_leased_worktree_is_diagnosed_by_the_config_it_shares(
    tmp_path: Path,
) -> None:
    """A lease has no config of its own, and the shadowed one is what stops it."""
    leased = linked_worktree(tmp_path)
    (tmp_path / "work" / ".git" / "config.lock").symlink_to(Path("/dev/null"))

    assert "blocked by the sandbox" in config_lock_diagnosis(leased)


def test_a_lease_is_diagnosed_by_its_own_admin_directory_too(tmp_path: Path) -> None:
    """`config.worktree` lands in the lease's own directory, not the shared one."""
    leased = linked_worktree(tmp_path)
    own = tmp_path / "work" / ".git" / "worktrees" / leased.name
    (own / "config.worktree").symlink_to(Path("/dev/null"))

    assert "`config.worktree` is a device node" in config_lock_diagnosis(leased)


def test_a_device_node_lock_names_the_sandbox(tmp_path: Path) -> None:
    root = admin_dir(tmp_path / "admin")
    (root / "config.lock").symlink_to(Path("/dev/null"))

    reported = diagnosis(inspect_git_admin(root))

    assert "`config.lock` is a device node" in reported
    assert "blocked by the sandbox" in reported
    assert "none here to delete" in reported
    assert "Rerun outside the sandbox" in reported


def test_a_confinement_remedy_does_not_end_at_the_escape(tmp_path: Path) -> None:
    """The loop a reader falls into when the escape reports the same refusal.

    A per-call escape re-judges a call and never moves it to another
    filesystem, so a confinement the session mounted refuses the escaped call
    identically. A remedy naming only the escape is then read twice and tried
    twice, and the second identical failure reads as the repository breaking
    rather than as the boundary holding — which is the one reading this whole
    module exists to prevent. So it has to name what the escape does not reach.
    """
    root = admin_dir(tmp_path / "admin")
    (root / "config.lock").symlink_to(Path("/dev/null"))

    reported = diagnosis(inspect_git_admin(root))

    assert "Rerun outside the sandbox" in reported
    assert "same refusal" in reported
    assert "mounts make this directory writable" in reported


def test_the_worktree_config_is_watched_alongside_the_lock(tmp_path: Path) -> None:
    """`config.worktree` is the second path a config write can be shadowed at."""
    root = admin_dir(tmp_path / "admin")
    (root / "config.worktree").symlink_to(Path("/dev/null"))

    assert inspect_git_admin(root) != []


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root creates a file in a mode-555 directory, so the mode cannot "
    "stand in for the mount that would refuse it",
)
def test_an_admin_directory_that_refuses_the_lock_names_the_sandbox(
    tmp_path: Path,
) -> None:
    """What a read-only mount does to the lock protocol: creation is refused."""
    root = admin_dir(tmp_path / "admin")
    root.chmod(0o555)

    reported = diagnosis(inspect_git_admin(root))
    root.chmod(0o755)

    assert "refuses a new `config.lock`" in reported
    assert "Rerun outside the sandbox" in reported


def test_a_directory_that_permits_a_touch_but_refuses_the_lock_is_a_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The confinement shape the permission bits cannot see.

    A sandbox that mediates the syscall above the kernel leaves the mode
    bits and the mount flags saying the directory is writable — a plain
    `touch` there succeeds — while still refusing git's `O_CREAT | O_EXCL`.
    Read off `os.access`, such a directory looks unconfined, so the lock
    beside it is called debris and the reader is told to wait out a holder
    that does not exist. Asking by attempting the create is what separates
    them, and this pins that the classification follows the attempt.
    """
    root = admin_dir(tmp_path / "admin")
    aged_lock(root, "config.lock", timedelta(days=2))
    real_open = os.open

    def refusing_open(path: str | Path, flags: int, mode: int = 0o777) -> int:
        if Path(path).parent == root and flags & os.O_EXCL:
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", refusing_open)
    reported = diagnosis(inspect_git_admin(root))

    assert os.access(root, os.W_OK)
    assert "refuses a new `config.lock`" in reported
    assert "Rerun outside the sandbox" in reported
    assert "removing it is the whole fix" not in reported


def test_the_probe_leaves_the_admin_directory_as_it_found_it(tmp_path: Path) -> None:
    """A diagnosis that littered would be one more thing to explain."""
    root = admin_dir(tmp_path / "admin")
    before = sorted(entry.name for entry in root.iterdir())

    assert inspect_git_admin(root) == []
    assert sorted(entry.name for entry in root.iterdir()) == before


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root writes a mode-444 file, so the mode cannot stand in for a mount",
)
def test_a_config_nobody_may_write_is_not_a_sandbox(tmp_path: Path) -> None:
    """Git renames over `config`; it never writes it, so its mode decides nothing.

    Reading the config's own bits called a plain `chmod 444` a confinement,
    which is the same wrong cause the `File exists` wording gives, arrived at
    from the other side.
    """
    root = admin_dir(tmp_path / "admin")
    (root / "config").chmod(0o444)

    assert inspect_git_admin(root) == []


def test_a_lock_name_the_confinement_shadows_is_the_callers_to_give(
    tmp_path: Path,
) -> None:
    """The watched names are git's, and a caller that knows others says so."""
    root = admin_dir(tmp_path / "admin")
    (root / "index.lock").symlink_to(Path("/dev/null"))

    assert inspect_git_admin(root) == []
    assert inspect_git_admin(root, ["index.lock"]) != []


def test_an_old_lock_on_a_writable_mount_is_the_opposite_diagnosis(
    tmp_path: Path,
) -> None:
    """The failure the sandbox wording sends a reader away from.

    Same `File exists`, nothing confined, and the file really is debris —
    so the message has to deny the sandbox as plainly as the sandbox one
    denies the stale lock.
    """
    root = admin_dir(tmp_path / "admin")
    aged_lock(root, "config.lock", timedelta(minutes=31))

    reported = diagnosis(inspect_git_admin(root))

    assert "31 minutes old" in reported
    assert "Nothing here is confined" in reported
    assert "removing it is the whole fix" in reported
    assert "sandbox" not in reported


def test_a_young_lock_is_left_for_the_git_that_may_hold_it(tmp_path: Path) -> None:
    """Age is the only stand-in for a holder, so below the threshold it declines."""
    root = admin_dir(tmp_path / "admin")
    aged_lock(root, "config.lock", timedelta(seconds=2))

    reported = diagnosis(inspect_git_admin(root))

    assert "Another git may still be holding it" in reported
    assert list(inspect_git_admin(root))[0].clear() is None


def test_the_staleness_threshold_is_the_callers_to_set(tmp_path: Path) -> None:
    """How long a write may plausibly take is a judgement, not git's fact."""
    root = admin_dir(tmp_path / "admin")
    aged_lock(root, "config.lock", timedelta(seconds=30))

    patient = inspect_git_admin(root, stale_after=timedelta(hours=1))
    impatient = inspect_git_admin(root, stale_after=timedelta(seconds=5))

    assert patient[0].clear() is None
    assert impatient[0].clear() is not None


def test_clearing_removes_the_stale_lock_and_says_so(tmp_path: Path) -> None:
    leased = linked_worktree(tmp_path)
    lock = aged_lock(tmp_path / "work" / ".git", "config.lock", timedelta(minutes=31))

    cleared = list(clear_stale_config_locks(leased))

    assert not lock.exists()
    assert len(cleared) == 1
    assert "31 minutes old" in cleared[0]
    assert config_lock_diagnosis(leased) == ""


def test_a_confined_lock_is_never_removed(tmp_path: Path) -> None:
    """The two failures coincide exactly where deleting is impossible.

    A sandboxed git that died is what leaves the lock, so the confined case
    is also the one where a lock looks most abandoned — and where removing
    it is precisely what the mount refuses.
    """
    root = admin_dir(tmp_path / "admin")
    aged_lock(root, "config.lock", timedelta(minutes=31))
    root.chmod(0o555)

    obstructions = inspect_git_admin(root)
    cleared = [obstruction.clear() for obstruction in obstructions]
    root.chmod(0o755)

    assert (root / "config.lock").exists()
    assert cleared == [None]
    assert "Rerun outside the sandbox" in diagnosis(obstructions)


def test_the_config_a_worktree_keeps_is_never_mistaken_for_debris(
    tmp_path: Path,
) -> None:
    """`config.worktree` is configuration; only `*.lock` is git's to recreate."""
    root = admin_dir(tmp_path / "admin")
    kept = aged_lock(root, "config.worktree", timedelta(days=2))

    assert [obstruction.clear() for obstruction in inspect_git_admin(root)] == []
    assert kept.exists()
