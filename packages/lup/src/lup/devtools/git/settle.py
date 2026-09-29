"""Fold what regeneration writes after a merge into the merge commit itself.

The generated trees merge under a driver that keeps one side. That resolves
every artifact only one branch regenerated — git takes the changed side and
never asks the driver — and it cannot resolve the proof both branches wrote:
each ownership manifest lists the digest of every file its branch generated,
so the side kept is stale for every file the other side changed, and a
digest of declarations neither branch had alone is one only a regeneration
can compute. So every merge of two branches that both regenerated left a
drift the next commit refused, and a regenerate-and-commit followed nearly
every merge by hand.

The merged source is on disk once the merge commit exists, which is the
earliest moment anything can render it, and the latest before anybody builds
on that commit. So this runs there, from the post-merge and post-commit
guards, and replaces the merge commit with one carrying what regeneration
wrote: same parents, same message, same author. It does so through plumbing
rather than an amend, because `git merge` still holds its merge state while
its post-merge hook runs and an amend is refused then.

It touches nothing it did not write. A merge may be made over unrelated local
changes, and those are measured before regeneration and left out of the
commit.
"""

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from lup.execution.shell import git

# lup: ignore[library-default] — git's own state files, which say a sequencer
# is replaying commits and owns HEAD until it finishes: a canonical table
SEQUENCER_STATE = (
    "rebase-merge",
    "rebase-apply",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "sequencer",
)


class Settled(BaseModel, frozen=True):
    """What settling a merge commit did."""

    merge: str
    """The merge commit as the merge made it."""

    head: str
    """The merge commit now, the same one where regeneration wrote nothing."""

    paths: list[str]
    """What regeneration wrote, which the commit now carries."""

    def report(self) -> str:
        """One line for whoever just merged."""
        if not self.paths:
            return "The merge commit already carries what its source generates."
        return (
            f"Settled {len(self.paths)} generated file(s) into the merge commit "
            f"{self.head[:12]}, replacing {self.merge[:12]}."
        )


def settle(root: Path, regenerate: Callable[[], None]) -> Settled | None:
    """Regenerate over a merge commit this checkout just made, and fold it in.

    ``None`` where HEAD is no merge commit, where a sequencer is replaying
    commits and owns HEAD, or where another branch already holds HEAD — a
    fast-forward onto somebody's merge runs the same hook, and rewriting
    their commit here would fork this branch from theirs.
    """
    here = ("-C", str(root))
    parents = git.out(*here, "log", "-1", "--format=%P", "HEAD").split()
    if len(parents) < 2:
        return None
    state = Path(git.out(*here, "rev-parse", "--absolute-git-dir"))
    if any((state / name).exists() for name in SEQUENCER_STATE):
        return None
    branch = git.out(*here, "symbolic-ref", "-q", "HEAD", _ok_code=[0, 1])
    holders = git.lines(
        *here,
        "for-each-ref",
        "--contains",
        "HEAD",
        "--format=%(refname)",
        "refs/heads",
        "refs/remotes",
        "refs/tags",
    )
    if not branch or any(ref != branch for ref in holders):
        return None

    def changed() -> list[str]:
        """Every path differing from the index, or untracked and not ignored."""
        tracked = git.out(*here, "diff", "--name-only", "-z")
        untracked = git.out(*here, "ls-files", "--others", "--exclude-standard", "-z")
        return [
            *tracked.split("\x00"),  # lup: ignore[string-split] — NUL records
            *untracked.split("\x00"),  # lup: ignore[string-split] — NUL records
        ]

    merge = git.out(*here, "rev-parse", "HEAD")
    before = changed()
    regenerate()
    written = sorted(path for path in changed() if path and path not in before)
    if not written:
        return Settled(merge=merge, head=merge, paths=[])
    git(*here, "add", "--all", "--", *written)
    name, email = git.lines(*here, "log", "-1", "--format=%an%n%ae", "HEAD")
    settled = git.out(
        *here,
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "commit-tree",
        git.out(*here, "write-tree"),
        *(argument for parent in parents for argument in ("-p", parent)),
        _in=git.out(*here, "log", "-1", "--format=%B", "HEAD") + "\n",
    )
    git(
        *here,
        "update-ref",
        "-m",
        "settle: regenerated trees folded into the merge",
        "HEAD",
        settled,
        merge,
    )
    return Settled(merge=merge, head=settled, paths=written)
