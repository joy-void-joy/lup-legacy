"""One unlanded branch inside another, and what the survey says about it.

A branch every commit of which a sibling already carries reads, by the
figures alone, as a second branch of the same size: two lines, one backlog.
A fix branch merged into the feature branch that continues it is the common
shape, and a report listing both with identical counts would tell a reader
there is twice as much unlanded as there is.
"""

from pathlib import Path

import pytest

import lup.devtools.dev.branches as branches
from lup.devtools.dev.branches import unlanded_siblings
from lup.execution.process import LaunchRequest, LocalProcessLauncher


def build_siblings(root: Path, launcher: LocalProcessLauncher, wider: bool) -> Path:
    """``feat-a`` and ``fix-b`` on one commit past ``dev``; ``feat-c`` past both.

    ``fix-b`` names the same commit as ``feat-a``, so each is an ancestor of
    the other. With ``wider``, ``feat-c`` continues from there, carrying every
    commit of both.
    """
    work = root / "work"
    # Identity per invocation, never `git config` — a persisted setting lands
    # in the shared config every worktree of a real repository inherits.
    who = ("-c", "user.email=siblings@example.test", "-c", "user.name=Sibling Test")
    git_in = ("git", "-C", str(work))
    steps = [
        ["git", "init", "-b", "dev", str(work)],
        [*git_in, *who, "commit", "--allow-empty", "-m", "base"],
        [*git_in, "checkout", "-q", "-b", "feat-a"],
        [*git_in, *who, "commit", "--allow-empty", "-m", "a"],
        [*git_in, "branch", "fix-b"],
    ]
    if wider:
        steps.extend(
            [
                [*git_in, "checkout", "-q", "-b", "feat-c"],
                [*git_in, *who, "commit", "--allow-empty", "-m", "c"],
            ]
        )
    steps.append([*git_in, "checkout", "-q", "dev"])
    for arguments in steps:
        status = launcher.launch(LaunchRequest(arguments=arguments, cwd=root))
        if status.code != 0:
            raise AssertionError(status.stderr)
    return work


def test_a_branch_carried_whole_by_a_wider_sibling_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = build_siblings(tmp_path, LocalProcessLauncher(), wider=True)
    monkeypatch.chdir(work)
    monkeypatch.setattr(branches, "project_root", lambda: tmp_path)

    by_name = {branch.name: branch for branch in unlanded_siblings()}

    assert by_name["feat-c"].contained_by is None
    assert by_name["feat-a"].contained_by == "feat-c"
    # The chain names its top: fix-b sits inside feat-a, which sits inside feat-c.
    assert by_name["fix-b"].contained_by == "feat-c"
    assert by_name["fix-b"].standing() == "every commit already inside feat-c"
    assert by_name["feat-c"].standing().startswith("2 commit(s)")


def test_two_names_on_one_commit_are_held_by_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each is an ancestor of the other, and neither may hide the pair."""
    work = build_siblings(tmp_path, LocalProcessLauncher(), wider=False)
    monkeypatch.chdir(work)
    monkeypatch.setattr(branches, "project_root", lambda: tmp_path)

    by_name = {branch.name: branch for branch in unlanded_siblings()}

    assert by_name["feat-a"].contained_by is None
    assert by_name["fix-b"].contained_by == "feat-a"
