"""Prepare a local PR head that a forge can merge without custom drivers."""

from collections.abc import Callable
from pathlib import Path

import sh
from pydantic import BaseModel

from lup.execution.git import Repository
from lup.devtools.dev.worktree import OWNERSHIP_MERGE_DRIVER
from lup.devtools.launcher import launcher_invocation
from lup.devtools.utils import decode_stderr
from lup.execution.shell import git
from lup.policy.kernel.diagnostic import devtools, spelled


class PreparedBranch(BaseModel, frozen=True):
    """The exact local base and head checked before a caller publishes them."""

    base: str
    base_commit: str
    head: str
    committed: bool


def prepare(base: str, root: Path, regenerate: Callable[[], None]) -> PreparedBranch:
    """Merge an explicit base, regenerate its merged sources, and commit locally.

    The generator runs after Git settles source, so its fresh process reads the
    combined declarations. Source conflicts remain available to the isolated
    conflict repair commands. No remote ref is changed or silently refreshed.
    """
    if git.lines("-C", str(root), "status", "--porcelain"):
        raise RuntimeError("Commit pending changes before preparing the PR branch.")
    repository = Repository(root)
    branch = repository.branch()
    if not branch:
        raise RuntimeError("Preparing a PR requires an attached feature branch.")
    base_commit = repository.resolves(base)
    if base_commit is None:
        raise RuntimeError(f"{base} names no commit to prepare the PR branch against.")
    try:
        git(
            "-C",
            str(root),
            "-c",
            f"merge.{OWNERSHIP_MERGE_DRIVER}.driver=true",
            "merge",
            "--no-commit",
            "--no-ff",
            base_commit,
        )
    except sh.ErrorReturnCode as error:
        # Git's own words, not only ours. A conflict and a merge that never
        # started both arrive here, and they take opposite next moves —
        # resolve the files, or fix why git refused — so a refusal saying only
        # "needs repair" sends a reader to look for conflicts that a failed
        # merge did not leave. What git printed is the half that tells them
        # apart, and it read as an empty working tree until it was relayed.
        spoken = decode_stderr(error).strip()
        status = devtools(
            "git", "conflict", "status", "--json", program=[launcher_invocation(root)]
        )
        raise RuntimeError(
            f"Base merge needs repair. Run `{spelled(status)}`, resolve sources, "
            "regenerate all harnesses, and complete the merge. No branch was pushed."
            + (f"\ngit said: {spoken}" if spoken else "")
        ) from error
    regenerate()
    merging = repository.merging() is not None
    committed = merging or bool(git.lines("-C", str(root), "status", "--porcelain"))
    if committed:
        git("-C", str(root), "add", "-A")
        git(
            "-C",
            str(root),
            "commit",
            "-m",
            f"chore(git): prepare {branch} against {base}",
        )
    return PreparedBranch(
        base=base,
        base_commit=base_commit,
        head=git.out("-C", str(root), "rev-parse", "HEAD"),
        committed=committed,
    )
