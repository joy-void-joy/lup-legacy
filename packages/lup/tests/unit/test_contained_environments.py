"""Which mounted roots get their own environment, and where it is held.

One absolute ``UV_PROJECT_ENVIRONMENT`` and two mounted projects is a single
directory `uv` makes match whichever project was synced last -- measured on uv
0.12.7, an exact sync uninstalls the other project and its dependencies. These
hold the arrangement that avoids it: the value is relative, and each root a
session may sync into has a container-private directory bound at that name.
"""

from pathlib import Path

from lup.launch.container import held_environments
from lup.launch.environments import environment_directory
from lup.execution.shell import git
from lup.sandbox.rail import AccessibleRoot

NAME = ".venv-contained"


def checkout(path: Path) -> Path:
    """A directory standing in for a mounted project root."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_every_writable_root_is_held_apart_from_every_other(tmp_path: Path) -> None:
    """The property the whole change exists for, stated as directories.

    Two roots resolving to one directory would be the collision again with
    more code in front of it.
    """
    own, other = checkout(tmp_path / "repo"), checkout(tmp_path / "other")
    held = held_environments(own, [AccessibleRoot(path=other)], NAME, tmp_path / "c")

    assert set(held) == {own, other}
    assert len(set(held.values())) == 2


def test_a_read_only_root_is_held_nowhere(tmp_path: Path) -> None:
    """`uv sync` writes, so a root nobody may write can hold no environment.

    Left out rather than bound and unused: a directory offered inside a
    read-only tree is a place to sync that refuses the sync when it is tried,
    and the refusal names the filesystem rather than the mode.
    """
    own = checkout(tmp_path / "repo")
    readable = AccessibleRoot(path=checkout(tmp_path / "theirs"), writable=False)
    held = held_environments(own, [readable], NAME, tmp_path / "c")

    assert set(held) == {own}
    assert not (tmp_path / "theirs" / NAME).exists()


def test_a_bare_root_is_held_in_each_of_its_worktrees(tmp_path: Path) -> None:
    """A bare repository has no tree of its own to sync a project in.

    A launch mounts a clone it materialized whole, so the environment belongs
    in every worktree the clone holds -- the one `refs/<name>` names among
    them -- and a directory bound inside the git directory would be a mount
    point nothing syncs into.
    """
    own = checkout(tmp_path / "repo")
    bare = tmp_path / "clone.git"
    git("init", "-q", "--bare", "-b", "main", str(bare))
    trees = [bare / "tree" / branch for branch in ("main", "fix")]
    for tree in trees:
        git(
            "-C",
            str(bare),
            "worktree",
            "add",
            "-q",
            "--orphan",
            "-b",
            tree.name,
            str(tree),
        )

    held = held_environments(own, [AccessibleRoot(path=bare)], NAME, tmp_path / "c")

    assert set(held) == {own, *trees}
    assert not (bare / NAME).exists()


def test_each_directory_exists_before_any_argv_names_it(tmp_path: Path) -> None:
    """A bind whose source is absent is one the engine refuses the container for.

    So the whole launch fails on a missing directory rather than the session
    losing one environment -- which makes creating them the launcher's job and
    not something done lazily beside the mount.
    """
    own = checkout(tmp_path / "repo")
    held = held_environments(own, [], NAME, tmp_path / "c")

    assert held, "a session holds at least its own checkout's environment"
    assert all(directory.is_dir() for directory in held.values())


def test_the_mount_point_is_made_here_rather_than_by_the_engine(tmp_path: Path) -> None:
    """Whoever creates it owns it, and the engine's answer is unusable.

    Measured on rootless podman 6.1.0 with ``--userns=keep-id``: a mount point
    the engine has to create is owned by uid 100000, so the operator's own
    `uv` fails with `Permission denied` on a path inside their checkout, and
    nothing in that message names a mount. Made here, it is theirs.
    """
    own = checkout(tmp_path / "repo")
    held_environments(own, [], NAME, tmp_path / "c")

    assert (own / NAME).is_dir()


def test_two_worktrees_named_alike_do_not_share_one_directory(tmp_path: Path) -> None:
    """The documented workflow names worktrees for their branch.

    Two repositories each holding a `dev` is the ordinary case, not a corner
    one, and a readable name alone puts them in the same directory -- where
    each sync would uninstall the other, the very sharing the per-root
    directory exists to prevent.
    """
    cache = tmp_path / "cache"
    first = environment_directory(tmp_path / "one" / "dev", cache)
    second = environment_directory(tmp_path / "two" / "dev", cache)

    assert first != second
    assert first.name.startswith("dev-") and second.name.startswith("dev-")


def test_an_environment_is_held_outside_every_checkout(tmp_path: Path) -> None:
    """Inside one, it would be reachable from a session's own tree and from git."""
    root = tmp_path / "repo"
    held = environment_directory(root, tmp_path / "cache")

    assert root not in held.parents


def test_the_same_root_is_held_at_the_same_place_each_launch(tmp_path: Path) -> None:
    """A key that moved would hand back an empty environment and resync it."""
    root = tmp_path / "repo"

    assert environment_directory(root, tmp_path / "c") == environment_directory(
        root, tmp_path / "c"
    )
