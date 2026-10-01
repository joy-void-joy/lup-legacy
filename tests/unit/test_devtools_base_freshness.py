"""Behavior tests for the base-freshness probe, the sync, and the gate reading it.

A checkout cannot tell from its own contents that its base has moved, so the
probe asks the remote and every entry point that opens a session asks the
probe. These run against real repositories with a local path as origin: what
is under test is which refs a checkout answers to, what git says about them,
and what the sync then does to the checkout — and a scripted double would only
restate the answers.
"""

from pathlib import Path

import pytest
import sh
import typer

from lup.devtools.dev.branches import (
    BaseFreshness,
    admit_an_unread_base,
    BaseMeasure,
    UpstreamMeasure,
    probe_base_freshness,
    require_fresh_base,
    settle_base_freshness,
    sync_upstream,
)
from lup.devtools.utils import Refusal
from lup.harness.process import LocalProcessLauncher
from tests.unit.repos import TEST_IDENTITY, commit_file, initialized_repo


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A repository other checkouts are cloned from and measured against."""
    work = tmp_path / "origin"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    return work


def repo_git(work: Path) -> sh.Command:
    # Identity per invocation, never `git config` — a misbound command then
    # writes nothing, where a persisted setting lands in the shared config every
    # worktree of a real repository inherits (see `lup.devtools.gitguard`).
    return sh.Command("git").bake(
        "-C",
        str(work),
        *(
            argument
            for setting, value in TEST_IDENTITY.items()
            for argument in ("-c", f"{setting}={value}")
        ),
        _tty_out=False,
    )


def clone_of(origin: Path, into: Path) -> Path:
    """A clone whose checked-out branch tracks the branch it was cloned from."""
    sh.Command("git")("clone", str(origin), str(into), _tty_out=False)
    return into


def worktree_clone(origin: Path, into: Path, branch: str = "feature") -> Path:
    """A clone on a feature branch that records its base, as creation does.

    This is the shape the whole gate is about: `dev worktree create` cuts a
    branch, records what it was cut from, and leaves it tracking nothing until
    something pushes it.
    """
    clone = clone_of(origin, into)
    git = repo_git(clone)
    git("switch", "-c", branch)
    git("config", f"branch.{branch}.lup-base", "main")
    return clone


def advance(origin: Path, name: str) -> None:
    """Commit one more file on the origin, which no clone has yet."""
    commit_file(repo_git(origin), origin, name, name, f"feat: {name}")


def probe(root: Path) -> BaseFreshness:
    return probe_base_freshness(LocalProcessLauncher(), root)


def settle(root: Path, *, publish: bool = False) -> None:
    settle_base_freshness(LocalProcessLauncher(), root, publish=publish)


def head_of(work: Path, revision: str = "HEAD") -> str:
    return str(repo_git(work)("rev-parse", revision)).strip()


def test_the_count_a_checkout_is_behind_its_own_remote_carries_the_pull(
    origin: Path, tmp_path: Path
) -> None:
    clone = clone_of(origin, tmp_path / "clone")
    advance(origin, "one.txt")
    advance(origin, "two.txt")

    freshness = probe(clone)

    assert freshness.upstream == UpstreamMeasure(tracked="origin/main", behind=2)
    assert freshness.base is None
    assert freshness.stale()
    assert freshness.report() == (
        "branch is 2 commit(s) behind origin/main: update with `git pull --ff-only`"
    )
    assert freshness.notice() == freshness.report()


def test_a_checkout_holding_everything_the_remote_holds_is_current(
    origin: Path, tmp_path: Path
) -> None:
    clone = clone_of(origin, tmp_path / "clone")

    freshness = probe(clone)

    assert not freshness.stale()
    assert freshness.report() == "branch is current with origin/main"
    # A command that asked how this checkout stands is answered either way.
    # A launch passing through is not asking, and `branch is current` before
    # every session can only ever mean that the next line is the first one
    # worth reading.
    assert freshness.notice() == ""


def test_a_moved_base_carries_the_merge_that_takes_it_not_a_pull(
    origin: Path, tmp_path: Path
) -> None:
    """The remedy a reading names has to be one that runs where it is printed.

    A feature branch holds commits its base does not, so there is nothing to
    fast-forward: `git pull --ff-only` exits non-zero on the only checkout
    this line was ever shown for.
    """
    clone = worktree_clone(origin, tmp_path / "clone")
    commit_file(repo_git(clone), clone, "mine.txt", "mine", "feat: mine")
    advance(origin, "one.txt")

    freshness = probe(clone)

    assert freshness.base == BaseMeasure(tracked="origin/main", behind=1)
    assert freshness.report().endswith(
        "base is 1 commit(s) behind origin/main: update with `git merge origin/main`"
    )


def test_a_pushed_branch_is_still_measured_against_the_base_it_was_cut_from(
    origin: Path, tmp_path: Path
) -> None:
    """Whether a branch was pushed says nothing about whether its base moved.

    Asking only the first ref that resolves answers a different question in a
    pushed worktree than in an unpushed one, so a base three commits gone
    reported as current — the false negative that hid two stale worktrees.
    """
    clone = worktree_clone(origin, tmp_path / "clone")
    repo_git(clone)("push", "-u", "origin", "feature")
    advance(origin, "one.txt")

    freshness = probe(clone)

    assert freshness.upstream == UpstreamMeasure(tracked="origin/feature", behind=0)
    assert freshness.base == BaseMeasure(tracked="origin/main", behind=1)
    assert freshness.stale()


def test_a_checkout_answering_to_no_remote_branch_says_so(
    origin: Path, tmp_path: Path
) -> None:
    clone = clone_of(origin, tmp_path / "clone")
    repo_git(clone)("switch", "-c", "feature")

    freshness = probe(clone)

    assert freshness.report() == (
        "base freshness unknown: this checkout answers to no remote branch"
    )
    assert not freshness.stale()


def test_an_unreachable_remote_is_stated_rather_than_counted(
    origin: Path, tmp_path: Path
) -> None:
    """An unread base is its own answer: not behind, not current, not nothing."""
    clone = clone_of(origin, tmp_path / "clone")
    repo_git(clone)("remote", "set-url", "origin", str(tmp_path / "gone"))

    freshness = probe(clone)

    assert freshness.unreachable
    assert not freshness.stale()
    assert freshness.unanswered()
    assert freshness.report().startswith("base freshness unknown:")
    # Not knowing is the finding here, and it is the one this whole reading
    # exists to put in front of somebody before a session opens on it.
    assert freshness.notice() == freshness.report()


def test_a_session_with_nobody_in_front_of_it_opens_on_an_unread_base(
    origin: Path, tmp_path: Path
) -> None:
    """Offline works: the prompt is for whoever is there, and here nobody is."""
    clone = clone_of(origin, tmp_path / "clone")
    repo_git(clone)("remote", "set-url", "origin", str(tmp_path / "gone"))

    settle(clone)


def test_a_run_refuses_to_pin_a_base_nothing_could_read() -> None:
    """The gate a launcher asks about, where there is nobody to ask."""
    unread = BaseFreshness(unreachable="ssh said no")

    with pytest.raises(Refusal) as refused:
        require_fresh_base(unread)
    assert "nothing could read" in refused.value.said["why"]


def test_a_checkout_with_no_remote_to_ask_is_not_an_unread_base(
    origin: Path, tmp_path: Path
) -> None:
    """Nothing to ask is not the same as asking and getting no answer."""
    clone = clone_of(origin, tmp_path / "clone")
    repo_git(clone)("switch", "-c", "feature")

    freshness = probe(clone)

    assert not freshness.unanswered()
    require_fresh_base(freshness)


def synced(root: Path, *, publish: bool = False) -> list[str]:
    """Run the sync against whatever the probe says this checkout's own remote is."""
    measure = probe(root).upstream
    assert measure is not None
    return list(sync_upstream(LocalProcessLauncher(), root, measure, publish=publish))


def test_a_clean_checkout_behind_its_own_remote_is_fast_forwarded(
    origin: Path, tmp_path: Path
) -> None:
    """The whole point of the reading: pulling first is free and prevents divergence."""
    clone = clone_of(origin, tmp_path / "clone")
    advance(origin, "one.txt")

    settle(clone)

    assert head_of(clone) == head_of(origin)


def test_local_commits_are_named_rather_than_sent(origin: Path, tmp_path: Path) -> None:
    """Settling takes what the remote holds; handing work back is a separate ask.

    The direction that earns being done unasked is the one that only changes
    this checkout. A push puts the work somewhere others read it and runs
    whatever the hooks on either end run, which is not a thing to do to
    somebody who typed a command about opening a session.
    """
    clone = worktree_clone(origin, tmp_path / "clone")
    repo_git(clone)("push", "-u", "origin", "feature")
    commit_file(repo_git(clone), clone, "mine.txt", "mine", "feat: mine")
    published = head_of(origin, "refs/heads/feature")

    lines = synced(clone)

    assert lines == ["1 commit(s) origin/feature does not have; `git push` sends them"]
    assert head_of(origin, "refs/heads/feature") == published


def test_a_caller_that_asks_to_publish_gets_the_push(
    origin: Path, tmp_path: Path
) -> None:
    """And the ask is all that changed: the push itself is the one it always was."""
    clone = worktree_clone(origin, tmp_path / "clone")
    repo_git(clone)("push", "-u", "origin", "feature")
    commit_file(repo_git(clone), clone, "mine.txt", "mine", "feat: mine")

    settle(clone, publish=True)

    assert head_of(clone) == head_of(origin, "refs/heads/feature")


def test_a_checkout_with_work_in_it_is_left_exactly_as_it_was(
    origin: Path, tmp_path: Path
) -> None:
    """A clean tree is what makes the sync safe, so a dirty one is not touched."""
    clone = clone_of(origin, tmp_path / "clone")
    (clone / "file.txt").write_text("edited\n", encoding="utf-8")
    advance(origin, "one.txt")
    before = head_of(clone)

    lines = synced(clone)

    assert lines == ["not synced with origin/main: the working tree has changes"]
    assert head_of(clone) == before
    assert (clone / "file.txt").read_text(encoding="utf-8") == "edited\n"


def test_a_diverged_branch_stops_after_the_pull_it_could_not_fast_forward(
    origin: Path, tmp_path: Path
) -> None:
    """Pushing on top of a divergence the pull just failed to close helps nobody."""
    clone = clone_of(origin, tmp_path / "clone")
    commit_file(repo_git(clone), clone, "mine.txt", "mine", "feat: mine")
    advance(origin, "one.txt")
    before = head_of(clone)

    lines = synced(clone)

    assert len(lines) == 1
    assert lines[0].startswith("not synced with origin/main: ")
    assert head_of(clone) == before


def test_a_moved_base_is_reported_and_the_session_opens_anyway(
    origin: Path, tmp_path: Path
) -> None:
    """Being behind a base is not grounds for refusing to open a session."""
    clone = worktree_clone(origin, tmp_path / "clone")
    commit_file(repo_git(clone), clone, "mine.txt", "mine", "feat: mine")
    advance(origin, "one.txt")
    before = head_of(clone)

    settle(clone)

    assert head_of(clone) == before
    assert probe(clone).base == BaseMeasure(tracked="origin/main", behind=1)


def test_a_run_refuses_to_pin_a_base_the_remote_has_moved_past() -> None:
    """A run cuts every lease from one base, so it never starts on a stale one."""
    stale = BaseFreshness(base=BaseMeasure(tracked="origin/main", behind=10))

    with pytest.raises(Refusal) as refused:
        require_fresh_base(stale)
    assert [way["run"] for way in refused.value.said["steps"]] == [
        ["git", "merge", "origin/main"]
    ]


class Terminal:
    """Stand in for a person at the keyboard, which pytest's stdin is not."""

    def isatty(self) -> bool:
        return True


def test_a_person_at_the_terminal_decides_whether_the_session_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole of what the printed line could not do: wait for an answer."""
    monkeypatch.setattr("sys.stdin", Terminal())
    asked: list[str] = []

    def decline(text: str, *arguments: object, **named: object) -> bool:
        asked.append(text)
        return False

    monkeypatch.setattr(typer, "confirm", decline)

    with pytest.raises(typer.Abort):
        admit_an_unread_base()

    assert asked == ["Could not check the remote. Continue opening the session?"]


def test_a_person_who_says_yes_gets_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline is a way of working, so the answer is theirs to give either way."""
    monkeypatch.setattr("sys.stdin", Terminal())
    monkeypatch.setattr(typer, "confirm", lambda *arguments, **named: True)

    admit_an_unread_base()
