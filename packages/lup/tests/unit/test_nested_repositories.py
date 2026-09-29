"""A repository kept inside the checkout is held the way the checkout's own is.

The host runs git there too, and its `config` and `hooks/` sit under the
checkout's writable mount, so a contained session could plant what the
operator's next `git` in it runs. A declared nested repository has both held
read-only, as a plain checkout's are, the directories holding them pinned,
and its pointers verified on the host at every launch, as every worktree's
are: a planted `commondir` would have git read them from elsewhere.
"""

import shutil
from pathlib import Path, PurePosixPath

import pytest
import sh
from pydantic import ValidationError

from lup.execution.shell import git
from lup.launch.container import held_lease, readied_nested
from lup.launch.declaration import InnerSandbox, NoSandbox, OuterContainer
from lup.launch.refusal import LaunchRefused
from lup.sandbox.rail import Lease, NestedRepository, fleet_lease, same_path


def repository(path: Path) -> Path:
    """A plain checkout with one commit, so git answers for it."""
    path.mkdir(parents=True, exist_ok=True)
    git("-C", str(path), "init", "-q", "-b", "main")
    git("-C", str(path), "config", "user.email", "test@example.invalid")
    git("-C", str(path), "config", "user.name", "Test")
    (path / "README.md").write_text("readme\n", encoding="utf-8")
    git("-C", str(path), "add", "-A")
    git("-C", str(path), "commit", "-qm", "first")
    return path


@pytest.mark.parametrize("spelled", ["/works", "../works", ".", "a/../../b"])
def test_a_nested_repository_names_a_directory_inside_the_checkout(
    spelled: str,
) -> None:
    with pytest.raises(ValidationError, match="inside the checkout"):
        NestedRepository(path=PurePosixPath(spelled))


def test_only_the_container_declares_nested_repositories() -> None:
    works = NestedRepository(path=PurePosixPath("works"), create=True)

    assert OuterContainer(nested_repositories=[works]).nested() == [works]
    assert OuterContainer().nested() == []
    assert InnerSandbox().nested() == []
    assert NoSandbox().nested() == []


def test_a_nested_repository_is_held_where_git_reads_it(tmp_path: Path) -> None:
    root = repository(tmp_path / "checkout")
    works = repository(root / "works")
    declared = [NestedRepository(path=PurePosixPath("works"))]

    said = readied_nested(root, declared)
    lease = held_lease(root, fleet_lease(root), declared)

    assert said == []
    for name in ("config", "hooks"):
        assert works / ".git" / name in lease.read_only
    assert works in lease.writable and works / ".git" in lease.writable
    assert not (works / ".git" / "commondir").exists()


def test_an_absent_repository_is_said_or_made_on_the_host(tmp_path: Path) -> None:
    root = repository(tmp_path / "checkout")
    absent = [NestedRepository(path=PurePosixPath("later"))]
    made = [NestedRepository(path=PurePosixPath("works"), create=True)]

    said = readied_nested(root, absent)
    held = held_lease(root, fleet_lease(root), absent)

    assert "later" in said[0].text and "nothing held" in said[0].text
    assert not any(root / "later" in path.parents for path in held.read_only)
    assert "initialized on the host" in readied_nested(root, made)[0].text
    assert (root / "works" / ".git" / "config") in held_lease(
        root, fleet_lease(root), made
    ).read_only


def test_a_redirected_commondir_refuses_the_launch(tmp_path: Path) -> None:
    """Holding a redirection read-only would keep it; the operator removes it."""
    root = repository(tmp_path / "checkout")
    works = repository(root / "works")
    (works / ".git" / "commondir").write_text(str(tmp_path / "elsewhere"))

    with pytest.raises(LaunchRefused, match="commondir"):
        readied_nested(root, [NestedRepository(path=PurePosixPath("works"))])


def test_a_hold_outside_what_the_lease_writes_is_not_made(tmp_path: Path) -> None:
    root = repository(tmp_path / "checkout")
    repository(root / "works")
    declared = [NestedRepository(path=PurePosixPath("works"))]
    readied_nested(root, declared)

    lease = held_lease(root, Lease(read_only=same_path([root])), declared)

    assert lease.writable == {}


def test_a_session_cannot_plant_what_the_host_runs_in_it(tmp_path: Path) -> None:
    """Measured with real mounts in a user namespace, as the engine binds them."""
    if shutil.which("unshare") is None:
        pytest.skip("unshare is not on PATH")
    root = repository(tmp_path / "checkout")
    works = repository(root / "works")
    declared = [NestedRepository(path=PurePosixPath("works"))]
    readied_nested(root, declared)
    lease = held_lease(root, Lease(writable=same_path([root])), declared)
    mounts = sorted(
        [
            *((path, "rw") for path in lease.writable),
            *((path, "ro") for path in lease.read_only),
        ],
        key=lambda mount: len(mount[0].parts),
    )
    binds = " && ".join(
        f"mount --bind '{path}' '{path}'"
        + (f" && mount -o remount,bind,ro '{path}'" if mode == "ro" else "")
        for path, mode in mounts
    )
    attempts = (
        f"(echo '[alias] x = !true' >> '{works / '.git' / 'config'}' || echo config-refused)"
        f" && (mv '{works / '.git'}' '{works / 'moved'}' || echo move-refused)"
    )
    try:
        said = str(
            sh.Command("unshare")(
                "--user",
                "--map-current-user",
                "--keep-caps",
                "--mount",
                "--",
                "sh",
                "-c",
                f"{binds} && {attempts}",
            )
        )
    except sh.ErrorReturnCode as refused:
        pytest.skip(f"this host refuses an unprivileged mount namespace: {refused}")

    assert said.split() == ["config-refused", "move-refused"]
    assert "alias" not in (works / ".git" / "config").read_text(encoding="utf-8")


def test_a_repository_reached_through_a_pointer_is_refused(tmp_path: Path) -> None:
    """Its configuration is wherever the pointer leads, which nothing here vouches for."""
    root = repository(tmp_path / "checkout")
    (root / "works").mkdir()
    (root / "works" / ".git").write_text(f"gitdir: {tmp_path / 'elsewhere'}\n")

    with pytest.raises(LaunchRefused, match="not a git directory of its own"):
        readied_nested(root, [NestedRepository(path=PurePosixPath("works"))])
