"""A recorded base whose branch was deleted, and whether the caller can tell.

The record holds a branch name, so it answers only while something still
carries that name. Deleting the base leaves the record in place pointing at
nothing, and detection falls through to the topological guess the record
exists to avoid -- reporting ``guessed``, which is exactly what a branch that
never had a record reports. A base that decayed and a base nobody wrote became
the same answer, and the difference reached the reader as a refusal several
commands later, phrased as though no record had ever been made.
"""

from pathlib import Path

import pytest

from lup.devtools.dev.branches import decayed_base_complaint, detect_base_branch
from lup.execution.process import LaunchRequest, LocalProcessLauncher


def build_history(root: Path) -> Path:
    """A branch recording a base that is about to stop existing.

    ``feat-x`` is cut from ``feat-parent`` and records it, as worktree
    creation does. ``feat-parent`` is then deleted -- the ordinary end of a
    feature branch whose work landed -- leaving ``dev`` as the only thing
    topology can offer.
    """
    work = root / "work"
    launcher = LocalProcessLauncher()
    # Identity per invocation, never `git config` -- a persisted setting lands
    # in the shared config every worktree of a real repository inherits.
    who = ("-c", "user.email=base@example.test", "-c", "user.name=Base Test")
    git_in = ("git", "-C", str(work))
    for arguments in (
        ["git", "init", "-b", "dev", str(work)],
        [*git_in, *who, "commit", "--allow-empty", "-m", "base"],
        [*git_in, "checkout", "-b", "feat-parent"],
        [*git_in, *who, "commit", "--allow-empty", "-m", "parent work"],
        [*git_in, "checkout", "-b", "feat-x"],
        [*git_in, *who, "commit", "--allow-empty", "-m", "own work"],
        [*git_in, "config", "branch.feat-x.lup-base", "feat-parent"],
        [*git_in, "checkout", "dev"],
    ):
        status = launcher.launch(LaunchRequest(arguments=arguments, cwd=root))
        if status.code != 0:
            raise AssertionError(status.stderr)
    return work


def delete_branch(work: Path, branch: str) -> None:
    status = LocalProcessLauncher().launch(
        LaunchRequest(
            arguments=["git", "-C", str(work), "branch", "-D", branch], cwd=work
        )
    )
    if status.code != 0:
        raise AssertionError(status.stderr)


def test_a_base_still_carried_by_a_branch_answers_without_complaint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The undecayed case, so the complaint is known to be about decay."""
    monkeypatch.chdir(build_history(tmp_path))

    candidate = detect_base_branch("feat-x")

    assert candidate.name == "feat-parent"
    assert candidate.source == "recorded"
    assert capsys.readouterr().err == ""


def test_a_recorded_base_whose_branch_is_gone_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The decay itself, named where it happens rather than downstream.

    Detection still answers -- a guess is better than nothing -- but the
    answer no longer reflects the record, and the reader has to be told that
    at the point the record was consulted and dropped.
    """
    work = build_history(tmp_path)
    delete_branch(work, "feat-parent")
    monkeypatch.chdir(work)

    candidate = detect_base_branch("feat-x")
    complaint = capsys.readouterr().err

    assert candidate.source == "guessed"
    assert "feat-x records feat-parent as its base" in complaint
    assert "no longer exists" in complaint
    assert "--base <branch>" in complaint


def test_the_complaint_separates_a_missing_branch_from_an_unmeasurable_one() -> None:
    """Two ways a record stops answering, which want different next moves.

    A branch that is gone is re-recorded or named; one that shares no history
    was never this branch's base, and re-recording the same name would only
    reproduce the failure.
    """
    gone = decayed_base_complaint("feat-x", "feat-parent", present=False)
    unrelated = decayed_base_complaint("feat-x", "feat-parent", present=True)

    assert "no longer exists" in gone
    assert "shares no history" in unrelated
