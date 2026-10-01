"""Which branch a worktree is cut from, and when nobody can say.

Two subjects, both about the base and both measured rather than imagined.

A `--base` that re-attaching cannot honour is refused, not dropped.
`worktree create` both cuts a new branch and re-attaches an existing one, and
only the first can act on a base. The second takes the branch where it already
stands, so the flag reaches nothing — and dropping it silently is worse than
unhelpful: the caller reads "worktree ready" beside the base they asked for and
starts writing against files that came from somewhere else. A session asking
for `--base feat-boundary` and re-attached to a branch on `dev` notices only
when the files it means to edit are absent.

And a base nobody named is guessed only where guessing cannot be wrong. New
work belongs on the integration branch, a continuation belongs on the
checkout's own branch, and the two arrive through identical arguments: a
branch cut from a feature checkout opens a pull request carrying commits it
never asked for, and one cut from `main` while standing on a release branch
has to be reset onto it by hand. Where the checkout
carries nothing the integration branch lacks the two answers are one line and
the later point on it is taken; where it is ahead, the question is asked before
there is anything to undo.
"""

from pathlib import Path

import pytest
import typer

from lup.devtools.dev.worktree import (
    BranchBase,
    commits_ahead,
    descends_from,
    register_worktree,
)
from lup.harness.process import LaunchRequest, LocalProcessLauncher


def run_git(cwd: Path, *arguments: str) -> None:
    """Run one git command for a fixture repository, failing on its own stderr.

    Identity per invocation, never `git config` — a persisted setting lands
    in the shared config every worktree of a real repository inherits.
    """
    who = ("-c", "user.email=worktree@example.test", "-c", "user.name=Worktree Test")
    status = LocalProcessLauncher().launch(
        LaunchRequest(arguments=["git", *who, *arguments], cwd=cwd)
    )
    if status.code != 0:
        raise AssertionError(status.stderr)


def build_history(root: Path) -> Path:
    """A repository whose two branches sit on genuinely different lines.

    `feature` carries a commit `dev` does not, which is what makes a base
    named against it unanswerable without moving something.
    """
    work = root / "work"
    run_git(root, "init", "-b", "dev", str(work))
    for arguments in (
        ("commit", "--allow-empty", "-m", "base"),
        ("checkout", "-b", "feature"),
        ("commit", "--allow-empty", "-m", "work"),
        ("checkout", "dev"),
    ):
        run_git(work, *arguments)
    return work


def test_a_base_the_existing_branch_is_not_on_stops_the_re_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal this exists for, on the topology that produced it.

    `dev` does not descend from `feature`, so re-attaching `dev` would hand
    back a tree cut from somewhere else entirely. Exiting is the whole point:
    the caller gets to choose, where before the choice was made for them and
    not mentioned.
    """
    work = build_history(tmp_path)
    monkeypatch.chdir(work)

    with pytest.raises(typer.Exit):
        register_worktree("dev", tmp_path / "tree" / "dev", "feature")


def test_a_base_the_existing_branch_already_carries_is_not_worth_refusing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is dropped when the branch is already on the line asked for.

    `feature` descends from `dev`, so `--base dev` is satisfied by where the
    branch stands and re-attaching honours it by doing nothing. Refusing here
    would turn a correct, common invocation into an error — re-running the
    same create after an interrupted setup is the documented recovery.
    """
    work = build_history(tmp_path)
    monkeypatch.chdir(work)

    assert descends_from("feature", "dev")
    assert not descends_from("dev", "feature")


def test_a_branch_ahead_of_the_integration_branch_is_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measurement the question turns on, in both directions.

    `feature` holds work `dev` lacks, so cutting from one or the other gives
    different trees. `dev` holds nothing `feature` lacks, so from there the
    two candidates are one line and there is nothing to ask.
    """
    work = build_history(tmp_path)
    monkeypatch.chdir(work)

    assert commits_ahead("feature", "dev") == 1
    assert commits_ahead("dev", "feature") == 0


def test_no_branch_at_all_is_nothing_to_measure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detached HEAD names nothing, and is refused for that reason instead.

    Counting it as a divergence would answer the wrong question out loud: the
    caller has no current branch to stack on, so the two spellings a contested
    base offers would include one that names nothing.
    """
    work = build_history(tmp_path)
    monkeypatch.chdir(work)

    assert commits_ahead("", "dev") == 0


def test_an_unrelated_history_counts_as_the_whole_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two trees sharing no commit read as maximally apart, not as one line.

    `other..branch` is `branch ^other` and needs no merge base, so the count
    survives a history with nothing in common — which is where guessing a
    base would cost the most.
    """
    work = build_history(tmp_path)
    run_git(work, "checkout", "--orphan", "unrelated")
    run_git(work, "commit", "--allow-empty", "-m", "unrelated")
    monkeypatch.chdir(work)

    assert commits_ahead("unrelated", "dev") == 1


def cutting(
    named: str | None = None,
    fresh: bool = True,
    ahead: int = 1,
) -> BranchBase:
    """The decision a fresh `topic` faces from a checkout ahead of `dev`."""
    return BranchBase(
        branch="topic",
        named=named,
        current="feature",
        integration="dev",
        fresh=fresh,
        ahead=ahead,
    )


def test_two_bases_that_differ_are_asked_about_rather_than_guessed() -> None:
    """Both spellings, so whichever the caller meant is a paste.

    A refusal rather than advice printed after the branch is cut, which no
    reader can act on without an undo.
    """
    asked = cutting().refusal()

    assert "--base feature" in asked
    assert "--base dev" in asked
    assert "topic" in asked


def test_a_named_base_ends_the_question_it_answers() -> None:
    """The flag is the whole answer, so nothing is asked and nothing overrides it."""
    named = cutting(named="feature")

    assert named.refusal() == ""
    assert named.cut_from() == "feature"


def test_a_checkout_holding_nothing_of_its_own_is_not_asked_about() -> None:
    """The ordinary case: a workspace nobody committed to, or work that landed.

    Both candidate bases sit on one line and the integration branch is the
    later point on it, so taking it strands no intent and the caller is left
    alone.
    """
    settled = cutting(ahead=0)

    assert settled.refusal() == ""
    assert settled.cut_from() == "dev"
    assert settled.recorded() == "dev"


def test_a_re_attached_branch_is_cut_from_nothing_so_nothing_is_asked() -> None:
    """`worktree add <path> <branch>` takes a branch where it stands.

    No base can reach it without moving it, and nothing here moves one — so a
    question about which base to cut from has no subject.
    """
    reattached = cutting(fresh=False)

    assert reattached.refusal() == ""
    assert reattached.cut_from() is None
    assert reattached.recorded() == "feature"
