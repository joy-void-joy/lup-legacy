"""A container holds its launch record read-only, and nothing inside can move it.

The ledger (`.lup/preflight`), the destination policies a launch accepted
(`.lup/policy-snapshots`) and the mount table (`.lup/boundary.json`) are what
the session's gates believe; a session that could write them could copy in a
ledger naming a boundary of its own. A read-only mount refuses writes to what
it covers and not a rename of the directory holding it -- measured, `mv .lup
.lup2` succeeded with `.lup/preflight` held inside -- so every directory
between a hold and the writable mount enclosing it is pinned as a mount point
of its own.
"""

import shutil
from pathlib import Path

import pytest
import sh

from lup.execution.shell import git
from lup.harness.egress import SessionEgress
from lup.launch.container import held_lease, record_boundary
from lup.launch.preflight import launch_record, retire_mount_table
from lup.policy.assets.host import boundary_description
from lup.sandbox.rail import Lease, demoted, fleet_lease, rooted, same_path


def repository(path: Path) -> Path:
    """A plain checkout with one commit, so git answers for it."""
    path.mkdir(parents=True)
    git("-C", str(path), "init", "-q", "-b", "main")
    git("-C", str(path), "config", "user.email", "test@example.invalid")
    git("-C", str(path), "config", "user.name", "Test")
    (path / "README.md").write_text("readme\n", encoding="utf-8")
    git("-C", str(path), "add", "-A")
    git("-C", str(path), "commit", "-qm", "first")
    return path


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    """A bare clone keeping its worktrees under `tree/`, as this repository does."""
    source = repository(tmp_path / "source")
    clone = tmp_path / "project.git"
    git("clone", "-q", "--bare", str(source), str(clone))
    for name in ("mine", "other"):
        git("-C", str(clone), "worktree", "add", "-q", str(clone / "tree" / name))
    return clone


def test_the_record_is_made_on_the_host_before_anything_binds_it(
    tmp_path: Path,
) -> None:
    """A bind whose source is missing refuses the whole container."""
    table = tmp_path / ".lup" / "boundary.json"
    table.parent.mkdir()
    table.write_text('{"read_only": []}', encoding="utf-8")

    held = launch_record(tmp_path)

    assert held == [
        tmp_path / ".lup" / "preflight",
        tmp_path / ".lup" / "policy-snapshots",
        table,
    ]
    assert all(path.is_dir() for path in held[:2])
    assert table.read_text(encoding="utf-8") == '{"read_only": []}'


def test_every_directory_between_a_hold_and_its_writable_mount_is_pinned() -> None:
    work = Path("/work")
    lease = Lease(
        writable=same_path([work]),
        read_only=same_path([work / "a" / "b" / "held"]),
    )

    pinned = rooted(lease)

    assert pinned.writable == same_path([work, work / "a", work / "a" / "b"])
    assert pinned.read_only == lease.read_only
    assert rooted(pinned) == pinned


def test_nothing_is_pinned_inside_a_read_only_mount() -> None:
    """Nothing can be renamed there anyway, so a pin would only widen it."""
    lease = Lease(read_only=same_path([Path("/ro"), Path("/ro/a/held")]))

    assert rooted(lease) == lease


def test_a_plain_checkout_holds_its_record_and_pins_what_holds_it(
    tmp_path: Path,
) -> None:
    """`.lup` and `.git` both become mount points, so neither can be renamed."""
    root = repository(tmp_path / "checkout")

    lease = held_lease(root, fleet_lease(root))

    for path in launch_record(root):
        assert lease.read_only[path] == path.as_posix()
    assert root / ".lup" in lease.writable
    assert root / ".git" in lease.writable
    assert lease.read_only[root / ".git" / "config"] == str(root / ".git" / "config")


def test_a_worktree_under_a_shared_tree_pins_itself_and_no_sibling(
    bare: Path,
) -> None:
    """A sibling stays removable from inside, which a mount point in it would stop."""
    mine = bare / "tree" / "mine"
    other = bare / "tree" / "other"

    lease = held_lease(mine, fleet_lease(mine))

    assert mine / ".lup" / "preflight" in lease.read_only
    assert mine in lease.writable and mine / ".lup" in lease.writable
    assert other not in lease.writable
    assert not any(other in path.parents for path in lease.read_only)
    assert not any(other in path.parents for path in lease.writable)


def test_a_lease_that_writes_nothing_holds_nothing(tmp_path: Path) -> None:
    root = repository(tmp_path / "checkout")
    readable = demoted(fleet_lease(root))

    lease = held_lease(root, readable)

    assert lease.writable == {}
    assert root / ".lup" / "preflight" not in lease.read_only


def test_an_uncontained_launch_empties_the_table_rather_than_replacing_it(
    tmp_path: Path,
) -> None:
    """A file bind is detached when the host replaces the file under it.

    A contained session in the same checkout holds the table by its inode,
    so the table is emptied where it stands.
    """
    record_boundary(Lease(writable=same_path([tmp_path])), SessionEgress(), tmp_path)
    table = tmp_path / ".lup" / "boundary.json"
    before = table.stat().st_ino
    assert boundary_description(tmp_path)["writable"] == [str(tmp_path)]

    retire_mount_table(tmp_path)

    assert table.stat().st_ino == before
    assert boundary_description(tmp_path) == {}


def applied(lease: Lease, then: str) -> str:
    """A shell script binding a lease the way an engine does, parent before child."""
    mounts = sorted(
        [
            *((path, "rw") for path in lease.writable),
            *((path, "ro") for path in lease.read_only),
        ],
        key=lambda mount: len(mount[0].parts),
    )
    lines = [
        f"mount --bind '{path}' '{path}'"
        + (f" && mount -o remount,bind,ro '{path}'" if mode == "ro" else "")
        for path, mode in mounts
    ]
    return " && ".join([*lines, then])


def test_a_session_cannot_move_its_record_from_under_the_hold(tmp_path: Path) -> None:
    """Measured with real mounts in a user namespace, both ways round."""
    if shutil.which("unshare") is None:
        pytest.skip("unshare is not on PATH")
    root = repository(tmp_path / "checkout")
    launch_record(root)
    held = Lease(
        writable=same_path([root]), read_only=same_path([root / ".lup" / "preflight"])
    )
    namespace = sh.Command("unshare").bake(
        "--user", "--map-current-user", "--keep-caps", "--mount", "--", "sh", "-c"
    )
    moved = f"mv '{root / '.lup'}' '{root / '.lup2'}'"
    try:
        namespace(applied(held, "true"))
    except sh.ErrorReturnCode as refused:
        pytest.skip(f"this host refuses an unprivileged mount namespace: {refused}")

    unpinned = namespace(applied(held, f"{moved} && echo moved"), _ok_code=[0, 1])
    (root / ".lup2").rename(root / ".lup")
    pinned = namespace(applied(rooted(held), f"{moved} && echo moved"), _ok_code=[0, 1])

    assert "moved" in str(unpinned)
    assert "moved" not in str(pinned)
    assert (root / ".lup" / "preflight").is_dir()
