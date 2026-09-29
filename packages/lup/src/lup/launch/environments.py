"""The host directories a contained session's project environments live in, and their end.

One per project root, under ``~/.cache/lup/environments``, bound over the
root's environment directory inside the container (see
:func:`~lup.launch.container.held_environments`). A worktree lives
for a feature and its environment outlived it: a venv per branch ever opened,
each hundreds of megabytes, with nothing left to say which checkout it had
been — the name carries the directory's last component and a digest of its
whole path, and a digest does not run backwards.

So each environment is claimed when it is made: a file beside it naming the
root it is for (``<name>.root``). An environment whose claimed root is gone
is finished, whether the worktree went through ``git worktree remove``, a
branch deletion, or a plain ``rm -r``. One made before claims existed is
matched against the checkouts it could have been — each sibling of a
repository's worktrees by the name it carries — and is finished only where
the digest proves which path it was for and that path is gone.
"""

import hashlib
import shutil
from pathlib import Path

from pydantic import BaseModel

# lup: ignore[constant-declaration] — the suffix a claim is written under, which
# the launch writing it and every sweep reading it have to agree on
CLAIM_SUFFIX = ".root"
"""What a claim beside an environment directory is called, after the directory's name."""


def environments_home(cache: Path | None = None) -> Path:
    """Where every project environment on this machine is kept."""
    return cache or Path.home() / ".cache" / "lup" / "environments"


def revisions_home(cache: Path | None = None) -> Path:
    """Where the plugin revisions contained sessions run their hooks from are kept.

    A runtime whose hooks run from a copy installed in the session's own
    home has that copy written again here and held read-only for the
    session. On the host and outside every checkout, beside the project
    environments, and mounted by no lease, so nothing a session reaches can
    rewrite one.
    """
    # lup: solved: one revision accumulates here per plugin revision a
    # contained session ran, and nothing sweeps them; `harness clean` should
    # list and remove the ones no running container holds, as it does the
    # environments
    return cache or Path.home() / ".cache" / "lup" / "codex-revisions"


def environment_directory(root: Path, cache: Path | None = None) -> Path:
    """Where one project root's container-side environment lives on the host.

    Per root rather than per repository, unlike the config home, and for
    the opposite reason: that one holds decisions worth sharing across
    worktrees, and this one holds an environment that *is* the checkout it was
    synced from -- a venv records absolute interpreter paths and the project
    installed into it, so two worktrees sharing one is the collision again at
    a smaller scale.

    Named by the directory and a digest of its whole path, because the
    readable half is not unique: the documented workflow makes worktrees named
    for their branch, and two repositories both holding a `dev` would land on
    one directory. The digest settles that, and the name in front is what
    makes a listing legible to whoever has to clear one out.

    Outside every checkout, so nothing here is reachable from a session's own
    tree or visible to the host's git.
    """
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    return environments_home(cache) / f"{root.name}-{digest}"


def claim_of(directory: Path) -> Path:
    """The file beside an environment naming the root it is for."""
    return directory.with_name(directory.name + CLAIM_SUFFIX)


def claimed(root: Path, cache: Path | None = None) -> Path:
    """Make one root's environment, and write down whose it is."""
    directory = environment_directory(root, cache)
    directory.mkdir(parents=True, exist_ok=True)
    claim = claim_of(directory)
    if not claim.is_file() or claim.read_text(encoding="utf-8") != str(root):
        claim.write_text(str(root), encoding="utf-8")
    return directory


def named_for(directory: Path) -> str:
    """The last component of the root an environment directory was named for."""
    # lup: ignore[string-split] — this module's own `<root>-<digest>` spelling,
    # split back at the one separator a hex digest cannot hold
    return directory.name.rsplit("-", 1)[0]


class HeldEnvironment(BaseModel, frozen=True):
    """One environment directory on this machine, and what is known of its root."""

    directory: Path
    root: Path | None = None
    """The root it was made for, where a claim or a digest says so."""

    def finished(self) -> bool:
        """Whether the checkout this environment was made for is gone."""
        return self.root is not None and not self.root.exists()

    def size(self) -> int:
        """The bytes its files hold, links not followed."""
        return sum(
            path.lstat().st_size
            for path in self.directory.rglob("*")
            if path.is_file() and not path.is_symlink()
        )

    def remove(self) -> None:
        """Remove the environment and its claim."""
        shutil.rmtree(self.directory)
        claim_of(self.directory).unlink(missing_ok=True)


def recorded_environments(
    worktrees: list[Path], cache: Path | None = None
) -> list[HeldEnvironment]:
    """Every environment on this machine, with the root each is known to belong to.

    A claimed one names its root. An unclaimed one is matched against the
    paths it could have been made for — each of ``worktrees``, and each name
    it carries under a directory one of them sits in — and only where its
    digest names that exact path.
    """
    home = environments_home(cache)
    if not home.is_dir():
        return []
    directories = sorted(path for path in home.iterdir() if path.is_dir())
    parents = list(dict.fromkeys(path.parent for path in worktrees))
    candidates = [
        *worktrees,
        *(
            parent / named_for(directory)
            for parent in parents
            for directory in directories
        ),
    ]
    matched = {environment_directory(path, cache): path for path in candidates}

    def root_of(directory: Path) -> Path | None:
        claim = claim_of(directory)
        if claim.is_file():
            return Path(claim.read_text(encoding="utf-8").strip())
        return matched.get(directory)

    return [
        HeldEnvironment(directory=directory, root=root_of(directory))
        for directory in directories
    ]


def sweep_environments(
    worktrees: list[Path], cache: Path | None = None
) -> list[HeldEnvironment]:
    """Remove every environment whose checkout is gone, answering what went."""
    finished = [
        held for held in recorded_environments(worktrees, cache) if held.finished()
    ]
    for held in finished:
        held.remove()
    return finished


def remove_worktree_environment(
    worktree: Path, cache: Path | None = None
) -> Path | None:
    """Remove the environment one worktree had, answering where it was, if it had one."""
    held = HeldEnvironment(
        directory=environment_directory(worktree, cache), root=worktree
    )
    if not held.directory.is_dir():
        return None
    held.remove()
    return held.directory
