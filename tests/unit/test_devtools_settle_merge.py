"""A merge commit carries the generated trees its own merged source renders.

The generated trees merge under a driver that keeps one side, which resolves
every artifact only one branch regenerated and cannot resolve the proof both
did: each ownership manifest lists the digests of every file its branch
wrote, so the kept side's manifest is stale for every file the other side
changed. Every merge of two branches that both regenerated therefore needed a
regenerate-and-commit afterwards, by hand. `git settle` is that step, run by
the post-merge and post-commit guards: it regenerates and folds what that
wrote into the merge commit itself, leaving everything else as it found it.
"""

import json
from pathlib import Path

import pytest
import sh

from lup.devtools.dev.git_guards import (
    DECLARED_GUARDS,
    SETTLE_COMMAND,
    GitGuard,
    NoMergeCommit,
    install_guards,
)
from lup.devtools.git.settle import settle
from tests.unit.repos import commit_file, devtools_double, git_in, initialized_repo


def generated(root: Path) -> None:
    """Render each runtime's proof from every source file, as generation does."""
    sources = {path.name: path.read_text() for path in sorted(root.glob("*.txt"))}
    for runtime in (".claude", ".codex"):
        directory = root / runtime
        directory.mkdir(exist_ok=True)
        (directory / ".lup-ownership.json").write_text(json.dumps(sources))


@pytest.fixture
def merged(tmp_path: Path) -> Path:
    """A merge of two branches that both regenerated, under the keep-one-side driver."""
    root = tmp_path / "repo"
    git = initialized_repo(root, tmp_path / "no-hooks")
    commit_file(
        git,
        root,
        ".gitattributes",
        ".claude/** merge=lup-ownership\n.codex/** merge=lup-ownership\n",
        "base",
    )
    git("checkout", "-q", "-b", "feature")
    (root / "feature.txt").write_text("feature work")
    generated(root)
    git("add", "-A")
    git("commit", "-m", "feature")
    git("checkout", "-q", "main")
    (root / "base.txt").write_text("base work")
    generated(root)
    git("add", "-A")
    git("commit", "-m", "base advanced")
    git("-c", "merge.lup-ownership.driver=true", "merge", "--no-edit", "feature")
    return root


def git_at(root: Path) -> sh.Command:
    return sh.Command("git").bake("-C", str(root), _tty_out=False)


def head(root: Path, spelling: str = "HEAD") -> str:
    return str(git_at(root)("rev-parse", spelling)).strip()


def test_the_merge_commit_absorbs_the_proof_both_sides_changed(merged: Path) -> None:
    kept = json.loads((merged / ".claude" / ".lup-ownership.json").read_text())
    assert kept == {"base.txt": "base work"}, "the driver kept one side's proof"
    merge = head(merged)
    parents = (head(merged, "HEAD^1"), head(merged, "HEAD^2"))

    settled = settle(merged, lambda: generated(merged))

    assert settled is not None
    assert settled.merge == merge
    assert sorted(settled.paths) == [
        ".claude/.lup-ownership.json",
        ".codex/.lup-ownership.json",
    ]
    assert head(merged) == settled.head != merge
    assert (head(merged, "HEAD^1"), head(merged, "HEAD^2")) == parents
    proof = json.loads(str(git_at(merged)("show", "HEAD:.claude/.lup-ownership.json")))
    assert proof == {"base.txt": "base work", "feature.txt": "feature work"}
    message = str(git_at(merged)("log", "-1", "--format=%s"))
    assert message.strip() == "Merge branch 'feature'"
    assert not str(git_at(merged)("status", "--porcelain")).strip()


def test_work_outside_what_regeneration_wrote_stays_out(merged: Path) -> None:
    """A merge is allowed over unrelated local changes, and they are nobody's commit."""
    (merged / "notes.md").write_text("mine, untracked\n")
    (merged / "base.txt").write_text("base work, edited after the merge")

    settled = settle(merged, lambda: None)

    assert settled is not None and settled.paths == []
    assert settled.head == settled.merge
    status = str(git_at(merged)("status", "--porcelain"))
    assert "notes.md" in status and "base.txt" in status


def test_a_settled_merge_is_left_as_it_is(merged: Path) -> None:
    settle(merged, lambda: generated(merged))
    before = head(merged)

    settled = settle(merged, lambda: generated(merged))

    assert settled is not None and settled.paths == []
    assert head(merged) == before


def test_a_commit_that_merged_nothing_is_not_asked_about(merged: Path) -> None:
    commit_file(git_in(merged, merged / "no-hooks"), merged, "later.txt", "x", "later")
    ran: list[str] = []

    assert settle(merged, lambda: ran.append("regenerated")) is None
    assert ran == []


def test_a_merge_commit_another_branch_holds_is_not_rewritten(merged: Path) -> None:
    """A fast-forward onto somebody's merge runs the same hook, and it is theirs.

    Rewriting it here would fork this branch from the one it just caught up
    with, over a commit this checkout never made.
    """
    git_at(merged)("branch", "caught-up")
    ran: list[str] = []

    assert settle(merged, lambda: ran.append("regenerated")) is None
    assert ran == []


def test_a_merge_replayed_by_a_rebase_is_not_rewritten_under_it(merged: Path) -> None:
    """The sequencer owns HEAD mid-rebase; moving it there would lose its place."""
    (merged / ".git" / "rebase-merge").mkdir()

    assert settle(merged, lambda: generated(merged)) is None


def test_the_settling_guards_run_on_a_merge_commit_and_nothing_else(
    tmp_path: Path,
) -> None:
    """Armed as git will run them, with the command swapped for a trace."""
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    git("config", "core.hooksPath", str(hooks))
    trace = tmp_path / "settled.log"
    guards = [
        GitGuard(
            command=f"echo {moment} >> {trace}",
            hook=moment,
            standdown=NoMergeCommit(),
        )
        for moment in ("post-merge", "post-commit")
    ]
    install_guards(guards, work)
    git = git.bake(_env=devtools_double(tmp_path / "devtools", guards))
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    git("checkout", "-q", "-b", "side")
    commit_file(git, work, "side.txt", "side\n", "feat: side")
    git("checkout", "-q", "-b", "clash", "main")
    commit_file(git, work, "file.txt", "theirs\n", "feat: clash")
    git("checkout", "-q", "main")
    commit_file(git, work, "file.txt", "ours\n", "feat: main")
    assert not trace.exists(), "an ordinary commit settles nothing"

    git("merge", "--no-edit", "side")
    git("merge", "--no-edit", "clash", _ok_code=[1])
    (work / "file.txt").write_text("both\n", encoding="utf-8")
    git("add", "file.txt")
    git("commit", "--no-edit")

    assert trace.read_text().splitlines() == ["post-merge", "post-commit"]


def test_lup_arms_the_settle_at_both_moments_a_merge_commit_is_made() -> None:
    """A merge git completes runs post-merge; one concluded by hand runs post-commit."""
    moments = {
        guard.hook: guard for guard in DECLARED_GUARDS if guard.hook != "pre-commit"
    }

    assert sorted(moments) == ["post-commit", "post-merge"]
    assert all(guard.command == SETTLE_COMMAND for guard in moments.values())
    assert all(guard.standdown == NoMergeCommit() for guard in moments.values())
