"""The file-declaration contract, and the one reading that decides it.

A worker accounts for its turn by naming what it changed and what it swept
beyond its concern's scope. The account is an attestation rather than data:
git already knows which paths moved, and what the contract asks is whether
the worker knows too — a file a tool rewrote unnoticed is exactly what a
derived list would hide.

Attesting is not the same as guessing, though, and a worker that can only
learn the boundary by crossing it guesses. So the reading that judges the
account lives here rather than inside the judge, and the worker runs the
same one before it submits.
"""

from pathlib import Path

from pydantic import BaseModel

from lup.execution.process import LaunchRequest, ProcessLauncher


class InspectedChanges(BaseModel, frozen=True):
    """What git reports one worktree has changed against one commit."""

    paths: list[Path] = []
    failure: str = ""
    """Why the reading did not happen, empty when it did."""


class DeclarationDelta(BaseModel, frozen=True):
    """The two ways an account can disagree with the worktree it describes.

    Both directions are carried together because satisfying one is what
    violates the other: a worker told only that it under-declared corrects
    by declaring the set it expected to touch, and hears about the
    over-declaration a round later. Reporting one at a time is what turned
    a two-sided contract into an oscillation.
    """

    undeclared: list[str] = []
    """Changed, and named in no part of the account."""

    unswept: list[str] = []
    """Claimed as swept beyond scope, and not changed at all."""

    @property
    def settled(self) -> bool:
        """Whether the account and the worktree already agree."""
        return not self.undeclared and not self.unswept

    @property
    def reason(self) -> str:
        """Name exactly which paths a worker must reconcile to pass this gate.

        A revision round can only converge on something it can read. The
        prior wording named no path at all, so a worker re-derived the same
        report and failed identically until the round budget ran out.
        """
        return "; ".join(
            [
                *(
                    [f"changed but not declared: {', '.join(self.undeclared)}"]
                    if self.undeclared
                    else []
                ),
                *(
                    [f"declared swept but not changed: {', '.join(self.unswept)}"]
                    if self.unswept
                    else []
                ),
            ]
        )


def inspect_changes(
    launcher: ProcessLauncher, root: Path, base_commit: str
) -> InspectedChanges:
    """Read every path one worktree has moved since a commit.

    ``add -N`` first, because a path that exists in no commit yet is
    invisible to ``diff`` until its intent is recorded — and a worker's new
    files are most of what it must account for.
    """
    intent = launcher.launch(
        LaunchRequest(arguments=["git", "add", "-N", "."], cwd=root)
    )
    if intent.code != 0:
        return InspectedChanges(failure="new paths could not be inspected")
    named = launcher.launch(
        LaunchRequest(
            arguments=["git", "diff", "--name-only", base_commit],
            cwd=root,
        )
    )
    if named.code != 0:
        return InspectedChanges(failure="changed paths could not be inspected")
    return InspectedChanges(
        paths=[Path(line) for line in named.stdout.splitlines() if line]
    )


def declaration_delta(
    changed: list[Path], files_changed: list[Path], swept_beyond_scope: list[Path]
) -> DeclarationDelta:
    """Compare one account against the paths a worktree actually moved.

    The safety property is that nothing changes undeclared, which is
    containment and not equality. Requiring equality also punishes a worker
    for a stale path it believes it touched, at a cost of 71 files of
    correct work over two such entries — nothing hidden in either. Over-
    reporting is named back rather than rejected, because a reason that
    names no path cannot converge: every retry re-derives the same report
    and fails identically until the budget is spent.
    """
    actual = {path.as_posix() for path in changed}
    reported = {path.as_posix() for path in files_changed}
    swept = {path.as_posix() for path in swept_beyond_scope}
    return DeclarationDelta(
        undeclared=sorted(actual - reported), unswept=sorted(swept - actual)
    )
