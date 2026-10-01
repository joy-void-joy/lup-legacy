"""Branches that exist only on a remote, and the clone that cannot see them.

A sweep classifies what ``refs/heads`` holds. A branch whose local copy went
when its work landed is therefore not reported as done — it stops being
reported at all, and the remote keeps it with nothing left to say so. These
pin the two ways that happens: the ref being absent from the survey, and the
clone being unable to fetch it in the first place.
"""

from pathlib import Path

import pytest

from lup.devtools.dev.branches import (
    PRStatus,
    RemoteBranchInfo,
    count_commits_behind,
    disposition_for,
    fetch_remote_tracking,
    parse_remote_branches,
)
from lup.execution.process import LaunchRequest, LocalProcessLauncher

# Identity per invocation, never `git config` — a persisted setting lands in
# the shared config every worktree of a real repository inherits.
WHO = ("-c", "user.email=remote@example.test", "-c", "user.name=Remote Test")


def run(launcher: LocalProcessLauncher, root: Path, arguments: list[str]) -> None:
    status = launcher.launch(LaunchRequest(arguments=arguments, cwd=root))
    if status.code != 0:
        raise AssertionError(status.stderr)


def build_origin(root: Path, launcher: LocalProcessLauncher) -> Path:
    """A remote carrying a slashed branch name beside ordinary ones."""
    origin = root / "origin"
    git_in = ("git", "-C", str(origin))
    for arguments in (
        ["git", "init", "-b", "dev", str(origin)],
        [*git_in, *WHO, "commit", "--allow-empty", "-m", "base"],
        [*git_in, "branch", "feat/slashed"],
        [*git_in, "branch", "fix-landed"],
    ):
        run(launcher, root, list(arguments))
    return origin


def test_a_bare_clone_sees_no_remote_branch_until_the_refspec_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blindness itself, which reads exactly like a repository with no branches.

    ``git clone --bare`` configures no ``remote.origin.fetch``, so the plain
    fetch a survey runs updates ``FETCH_HEAD`` and leaves ``refs/remotes``
    empty. Nothing errors and nothing warns: the survey that follows reports
    a complete-looking picture whose remote half is missing, because a branch
    it cannot see is indistinguishable from one that is not there.
    """
    launcher = LocalProcessLauncher()
    origin = build_origin(tmp_path, launcher)
    clone = tmp_path / "work.git"
    run(launcher, tmp_path, ["git", "clone", "--bare", str(origin), str(clone)])
    monkeypatch.chdir(clone)

    assert parse_remote_branches() == []

    fetch_remote_tracking()

    assert {row["name"] for row in parse_remote_branches()} == {
        "dev",
        "feat/slashed",
        "fix-landed",
    }


def test_a_branch_name_keeps_its_slash_and_the_symref_is_not_a_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ways a ref name misreads when the joined form is taken apart by hand.

    ``origin/feat/slashed`` splits into a remote and a branch at the first
    separator only if the branch is known to hold none, and conventional
    names hold one. ``origin/HEAD`` is a symref naming another ref, so
    counting it reports the default branch twice — the second time under a
    name no push can delete.
    """
    launcher = LocalProcessLauncher()
    origin = build_origin(tmp_path, launcher)
    clone = tmp_path / "work"
    run(launcher, tmp_path, ["git", "clone", str(origin), str(clone)])
    monkeypatch.chdir(clone)

    rows = parse_remote_branches()

    assert all(row["remote"] == "origin" for row in rows)
    assert "feat/slashed" in {row["name"] for row in rows}
    assert "HEAD" not in {row["name"] for row in rows}


def test_a_merged_remote_branch_resolves_to_the_same_verb_as_a_local_one() -> None:
    """One classifier, so the two cannot answer the same question differently.

    A remote-only branch reaches ``disposition_for`` with the fields a ref on
    a remote can actually have. What it cannot have — a worktree, a lease,
    being the branch checked out — is left at the defaults that say so, and
    the verb comes back unchanged.
    """
    verdict = disposition_for(
        "fix-landed",
        integration="dev",
        current="dev-local",
        contained_in=["dev"],
        pr=PRStatus(number=244, state="MERGED", headRefName="fix-landed"),
        unique_commits=0,
    )

    assert verdict.status == "DELETE"
    assert verdict.reason == "merged into dev"


def test_a_delete_names_the_branch_rather_than_the_tracking_ref() -> None:
    """``origin/x`` identifies the ref here; ``x`` is what the remote calls it.

    A push spelled against the tracking name asks the remote to delete a
    branch it has never heard of, which fails rather than deleting the wrong
    thing — but it fails at the point where somebody has already approved it.
    """
    info = RemoteBranchInfo(
        name="feat/slashed",
        remote="origin",
        commit="abc1234",
        contained_in_integration=True,
        pr=None,
        unique_commits=0,
        disposition="DELETE",
        reason="merged into dev",
    )

    assert info.qualified() == "origin/feat/slashed"
    assert info.delete_command() == "git push origin --delete feat/slashed"


def test_a_branch_reports_how_far_the_integration_branch_has_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The figure a reader needs once the verb is settled.

    A clean workspace whose base has gone stale is one nobody has opened,
    and no disposition can say so: reading distance is exactly what the
    reserved-workspace guard must not do, since every merge moves the
    integration branch under every branch at once. Reported, never decisive.
    """
    launcher = LocalProcessLauncher()
    repo = tmp_path / "repo"
    git_in = ("git", "-C", str(repo))
    for arguments in (
        ["git", "init", "-b", "dev", str(repo)],
        [*git_in, *WHO, "commit", "--allow-empty", "-m", "base"],
        [*git_in, "branch", "reserved"],
        [*git_in, *WHO, "commit", "--allow-empty", "-m", "one"],
        [*git_in, *WHO, "commit", "--allow-empty", "-m", "two"],
    ):
        run(launcher, tmp_path, arguments)
    monkeypatch.chdir(repo)

    assert count_commits_behind("reserved", "dev") == 2
    assert count_commits_behind("dev", "dev") == 0


def test_a_branch_that_cannot_be_measured_says_so_rather_than_reading_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero would read as up to date, which is the wrong way to be wrong."""
    launcher = LocalProcessLauncher()
    repo = tmp_path / "repo"
    for arguments in (
        ["git", "init", "-b", "dev", str(repo)],
        ["git", "-C", str(repo), *WHO, "commit", "--allow-empty", "-m", "base"],
    ):
        run(launcher, tmp_path, arguments)
    monkeypatch.chdir(repo)

    assert count_commits_behind("no-such-branch", "dev") == -1
