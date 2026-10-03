"""A merge onto a base nobody refreshed, and whether the caller can tell.

`sync_base` fetches the base and merges it, and the fetch is the half that can
fail on its own -- a read-only sibling worktree inside a container refuses the
`FETCH_HEAD` write, and the merge that follows succeeds anyway. Both outcomes
print `merged`, so a JSON answer saying nothing else would read identically for
a current base and a stale one — and the rebase workflow resets onto that
answer.
"""

import json
from pathlib import Path

import pytest

from lup.devtools.dev import pr
from lup.execution.process import LaunchRequest, LocalProcessLauncher


def build_history(root: Path) -> Path:
    """A repository with a `feature` branch standing ahead of `dev`.

    Ahead rather than diverged, so the merge under test resolves without a
    merge commit. An identity is deliberately not configured here -- needing
    one would make this a test of git configuration rather than of what the
    result reports.
    """
    work = root / "work"
    launcher = LocalProcessLauncher()
    who = ("-c", "user.email=sync@example.test", "-c", "user.name=Sync Test")
    git_in = ("git", "-C", str(work))
    for arguments in (
        ["git", "init", "-b", "dev", str(work)],
        [*git_in, *who, "commit", "--allow-empty", "-m", "base"],
        [*git_in, "checkout", "-b", "feature"],
        [*git_in, *who, "commit", "--allow-empty", "-m", "work"],
    ):
        status = launcher.launch(LaunchRequest(arguments=arguments, cwd=root))
        if status.code != 0:
            raise AssertionError(status.stderr)
    return work


def test_a_base_nothing_fetched_is_merged_and_reported_as_unrefreshed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The JSON carries whether the base was refreshed, on the shape that hides it.

    No worktree holds the base here, so nothing fetched it and nothing knows
    it is current -- the same position a contained session is in when the
    boundary refuses the fetch. The merge still happens, because it is still
    the merge that was asked for and still correct against the base as it
    stands; and the answer says the base went unrefreshed.
    """
    monkeypatch.chdir(build_history(tmp_path))

    pr.sync_base("dev", as_json=True)

    reported = json.loads(capsys.readouterr().out)
    assert reported["merged"] is True
    assert reported["base_synced"] is False
    assert reported["sync_complaint"]
    assert "dev" in reported["sync_complaint"]


def test_a_directory_named_after_the_base_is_not_taken_for_the_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Which worktree holds the base is git's answer, not the path's spelling.

    A sibling directory is named for the branch it was created to hold, and
    goes on answering to that name after somebody checks something else out
    in it. Reading the name as the answer pulls -- and pushes -- whichever
    branch is standing there, which is how syncing `dev` reaches for a
    feature branch that merely lives at `tree/dev`.
    """
    work = build_history(tmp_path)
    decoy = work / "tree" / "dev"
    launcher = LocalProcessLauncher()
    for arguments in (
        ["git", "-C", str(work), "branch", "decoy"],
        ["git", "-C", str(work), "worktree", "add", str(decoy), "decoy"],
    ):
        status = launcher.launch(LaunchRequest(arguments=arguments, cwd=tmp_path))
        if status.code != 0:
            raise AssertionError(status.stderr)
    monkeypatch.chdir(work)

    pr.sync_base("dev", as_json=True)

    reported = json.loads(capsys.readouterr().out)
    assert reported["base_synced"] is False
    assert reported["sync_complaint"] == (
        "no worktree for dev, so it was merged as it stands"
    )
