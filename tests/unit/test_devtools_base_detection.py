"""Behavior tests for base-branch recording and detection.

Worktree creation records its base in ``<common>/lup/branches/<name>.json``,
and detection prefers that record over everything else. Topology alone
cannot recover the creation point —
once branches share tips or the parent merges on, every candidate looks alike
and the nearest one wins regardless of where the branch was really cut.

Between the two sits the cut git logged for itself, which is what a branch
made with plain ``git`` has instead of a record, and which is reported as its
own source because it is a byproduct rather than a statement. It is missing
often enough to be worth pinning missing: a tie that the log resolves and the
same tie with nothing logged are both here.
"""

from pathlib import Path

import pytest
import sh

from lup.devtools.dev import branches
from lup.devtools.dev import records
from lup.devtools.dev import worktree
from lup.devtools.harness.launch import relocation_hint
from tests.unit.repos import commit_file, initialized_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    return work


def repo_git(work: Path) -> sh.Command:
    return sh.Command("git").bake("-C", str(work), _tty_out=False)


def commit_named_file(work: Path, name: str) -> None:
    """Commit a file whose content and message are both its name."""
    commit_file(repo_git(work), work, name, name, f"feat: {name}")


def test_recorded_base_resolves_what_topology_cannot(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = repo_git(repo)
    git("branch", "feature")
    git("switch", "-c", "topic", "feature")
    records.remember("topic", records.BranchRecord(base="feature"), repo)
    commit_named_file(repo, "t1.txt")

    monkeypatch.chdir(repo)
    candidate = branches.detect_base_branch("topic")
    assert candidate.name == "feature"
    assert candidate.source == "recorded"


def test_an_integration_branch_that_moved_on_still_wins_over_a_stale_ancestor(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale sibling never becomes the baseline for a whole branch.

    `stale-sibling` sits at a commit older than the fork point, so its tip is
    inside `topic`'s history and it counts as an ancestor. `main` has taken a
    commit since the cut, so it is not one — which is the ordinary state of an
    integration branch, not a disqualification. Ancestry used as a filter
    rather than a rank drops `main` before distance is consulted and lets the
    far ancestor win: on a real branch that is a base 747 commits off, against
    which the gate reports 137 capabilities gone that nothing touched.

    Distance decides instead, and it is merge-base distance, which needs no
    ancestry to mean anything. The merge base is asserted too, because that is
    the commit a surface is actually judged from.
    """
    git = repo_git(repo)
    git("branch", "stale-sibling")
    commit_named_file(repo, "fork.txt")
    fork_point = str(git("rev-parse", "HEAD")).strip()
    git("switch", "-c", "topic")
    commit_named_file(repo, "t1.txt")
    commit_named_file(repo, "t2.txt")
    git("switch", "main")
    commit_named_file(repo, "m1.txt")

    monkeypatch.chdir(repo)
    candidate = branches.detect_base_branch("topic")
    assert candidate.name == "main"
    assert not candidate.is_ancestor
    assert candidate.merge_base == fork_point
    assert candidate.distance == 2


def test_the_logged_cut_resolves_what_topology_could_not(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main` and `feature` are both one commit behind `topic`, so distance
    cannot choose between them — and git logged which one `topic` was cut
    from, so nothing has to.

    Reported as `created` rather than `recorded`: nobody stated this, git
    wrote it down on the way past, and a reader deciding how much to trust an
    answer needs the two kept apart.
    """
    git = repo_git(repo)
    git("branch", "feature")
    git("switch", "-c", "topic", "feature")
    commit_named_file(repo, "t1.txt")

    monkeypatch.chdir(repo)
    candidate = branches.detect_base_branch("topic")
    assert candidate.name == "feature"
    assert candidate.source == "created"


def test_a_topology_tie_takes_the_integration_branch_and_says_it_did(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tie with nothing logged is two answers, not a reason to refuse.

    `core.logAllRefUpdates` is off for the creation alone, which is the
    condition a bare clone is in by default — so the creation goes unlogged
    while the later commit still logs, exactly as for a branch cut against a
    git directory rather than from one of its worktrees. Topology is then all
    there is, and `main` and `feature` are equally close.

    Exiting there stopped every caller rather than the one that could not
    proceed on a guess, and in this clone that made the quality gate refuse to
    run at all on an ordinary branch. `main` is where work lands, so it
    settles the tie; the tie is still named, and the answer is still reported
    `guessed` so `pr sync-base` still declines to merge onto it.
    """
    git = repo_git(repo)
    git("branch", "feature")
    git("-c", "core.logAllRefUpdates=false", "branch", "topic", "feature")
    git("switch", "topic")
    commit_named_file(repo, "t1.txt")

    monkeypatch.chdir(repo)
    assert branches.created_from("topic") == ""
    candidate = branches.detect_base_branch("topic")

    assert candidate.name == "main"
    assert candidate.source == "guessed"
    complaint = capsys.readouterr().err
    assert "Ambiguous base branch" in complaint
    assert "feature" in complaint
    assert "Taking main" in complaint


def test_stale_record_falls_back_to_guessing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = repo_git(repo)
    git("switch", "-c", "feature")
    commit_named_file(repo, "f1.txt")
    git("switch", "-c", "topic")
    records.remember("topic", records.BranchRecord(base="gone"), repo)
    commit_named_file(repo, "t1.txt")

    monkeypatch.chdir(repo)
    candidate = branches.detect_base_branch("topic")
    assert candidate.name == "feature"
    assert candidate.source == "guessed"


def test_worktree_create_records_the_base(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree_dir = tmp_path / "tree"
    tree_dir.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "get_tree_dir", lambda: tree_dir)

    worktree.create(
        "wt-topic",
        no_sync=True,
        no_copy_data=True,
        base_branch=None,
        launcher=relocation_hint,
    )

    assert records.recorded_base("wt-topic", repo) == "main"
    candidate = branches.detect_base_branch("wt-topic")
    assert candidate.name == "main"
    assert candidate.source == "recorded"
