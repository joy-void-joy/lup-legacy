"""Telling a sibling worktree's commit apart from a fixture escaping.

Every worktree cut from a repository shares its ref store, so the guard
reading `for-each-ref` in one of them sees every branch the repository holds.
Somebody committing in a sibling while the suite runs moves a ref for real,
and blaming this run for it turns a routine event into a failure that reads
exactly like the accident the guard exists for — which is how a real one comes
to be waved through. The stash is the same event with less to go on: one ref
every worktree pushes onto, attributed from what git recorded of each entry.
"""

import os
from pathlib import Path

import sh

from lup.execution.git import Worktree
from lup.devtools.gitguard import (
    TEST_IDENTITY,
    ForeignCheckouts,
    RepositoryWatch,
    StashEntry,
    repository_state,
)


def committed(where: Path, message: str) -> None:
    """One empty commit in ``where``, as the suite's own identity."""
    sh.Command("git")(
        "-C",
        str(where),
        "commit",
        "--allow-empty",
        "-m",
        message,
        _tty_out=False,
        _env={**os.environ, **TEST_IDENTITY.environment()},
    )


def repository_with_a_sibling(tmp_path: Path) -> Path:
    """A checkout with one worktree beside it, each on its own branch."""
    main = tmp_path / "main"
    main.mkdir()
    git = sh.Command("git").bake("-C", str(main), _tty_out=False)
    git("init", "-b", "trunk")
    committed(main, "root")
    git("worktree", "add", "-b", "sibling", str(tmp_path / "sibling"))
    return main


def test_a_sibling_branch_is_attributed_to_the_worktree_holding_it(
    tmp_path: Path,
) -> None:
    foreign = ForeignCheckouts.beside(repository_with_a_sibling(tmp_path))

    assert foreign.holder("refs/heads/sibling") == str((tmp_path / "sibling").resolve())
    assert foreign.holder("refs/heads/trunk") is None


def test_a_commit_in_a_sibling_is_noticed_without_failing(tmp_path: Path) -> None:
    """The case that made this necessary: concurrent work, not an escape."""
    main = repository_with_a_sibling(tmp_path)
    foreign = ForeignCheckouts.beside(main)
    before = repository_state(main)
    committed(tmp_path / "sibling", "their work")

    verdict = foreign.verdict(before, repository_state(main))

    assert verdict.failure == ""
    assert "refs/heads/sibling" in verdict.notice
    assert "not this run" in verdict.notice


def test_the_branch_this_checkout_holds_still_fails(tmp_path: Path) -> None:
    """Narrowing who is answerable must not narrow what is caught."""
    main = repository_with_a_sibling(tmp_path)
    foreign = ForeignCheckouts.beside(main)
    before = repository_state(main)
    committed(main, "an escaped fixture")

    verdict = foreign.verdict(before, repository_state(main))

    assert "refs/heads/trunk" in verdict.failure
    assert "modified the repository it is running inside" in verdict.failure


def test_a_ref_that_appeared_from_nowhere_is_nobody_elses(tmp_path: Path) -> None:
    """No worktree holds a branch a fixture just created, so it is this run's."""
    main = repository_with_a_sibling(tmp_path)
    foreign = ForeignCheckouts.beside(main)
    before = repository_state(main)
    sh.Command("git")("-C", str(main), "branch", "invented", _tty_out=False)

    verdict = foreign.verdict(before, repository_state(main))

    assert "refs/heads/invented: created" in verdict.failure


def test_config_is_never_another_worktrees_to_have_written(tmp_path: Path) -> None:
    """The quieter half is shared outright, so no worktree can be blamed for it."""
    main = repository_with_a_sibling(tmp_path)
    foreign = ForeignCheckouts.beside(main)
    before = repository_state(main)
    sh.Command("git")(
        "-C", str(main), "config", "user.email", "fixture@example.test", _tty_out=False
    )

    verdict = foreign.verdict(before, repository_state(main))

    assert "config user.email: created" in verdict.failure


def test_a_repository_git_cannot_read_blames_the_suite_for_everything(
    tmp_path: Path,
) -> None:
    """The narrower question failing must not answer the wider one yes."""
    assert ForeignCheckouts.beside(tmp_path / "nowhere").holders == {}


def test_a_detached_or_bare_entry_claims_no_ref() -> None:
    """Only a `branch` line names a ref; the others hold none to attribute."""
    listing = [
        Worktree(path=Path("/a"), head="abc", detached=True),
        Worktree(path=Path("/b"), head="def", branch="held"),
        Worktree(path=Path("/c"), bare=True),
    ]

    assert ForeignCheckouts.declared(listing, Path("/other")) == {
        "refs/heads/held": str(Path("/b").resolve())
    }


def test_the_checkout_under_test_never_counts_as_foreign() -> None:
    """Its own branch is exactly the one the guard must keep answering for."""
    listing = [Worktree(path=Path("/a"), head="abc", branch="mine")]

    assert ForeignCheckouts.declared(listing, Path("/a").resolve()) == {}


def bare_remote(tmp_path: Path) -> Path:
    """Somewhere for a sibling to push to."""
    origin = tmp_path / "origin.git"
    sh.Command("git")("init", "--bare", "-b", "trunk", str(origin), _tty_out=False)
    return origin


def repository_with_a_remote(tmp_path: Path) -> Path:
    """A checkout and its sibling, with a remote both can push to."""
    main = repository_with_a_sibling(tmp_path)
    sh.Command("git")(
        "-C",
        str(main),
        "remote",
        "add",
        "origin",
        str(bare_remote(tmp_path)),
        _tty_out=False,
    )
    return main


def pushed(where: Path, refspec: str) -> None:
    """Push ``refspec`` from ``where``, without setting anything up first."""
    sh.Command("git")("-C", str(where), "push", "origin", refspec, _tty_out=False)


def test_a_siblings_first_push_is_noticed_without_failing(tmp_path: Path) -> None:
    """The half the branch relation misses: the config arrives with the push.

    Nothing tracks anything when the guard reads the repository, so a branch's
    upstream cannot name the ref that is about to appear. The correspondence
    `git push` uses has to be claimed ahead of it or the routine event fails
    the run, which is the whole complaint.
    """
    main = repository_with_a_remote(tmp_path)
    foreign = ForeignCheckouts.beside(main)
    before = repository_state(main)
    pushed(tmp_path / "sibling", "sibling")

    verdict = foreign.verdict(before, repository_state(main))

    assert verdict.failure == ""
    assert "refs/remotes/origin/sibling" in verdict.notice


def test_a_remote_ref_for_the_branch_this_checkout_holds_still_fails(
    tmp_path: Path,
) -> None:
    """Narrowing who answers for a push must not excuse this checkout's own."""
    main = repository_with_a_remote(tmp_path)
    foreign = ForeignCheckouts.beside(main)
    before = repository_state(main)
    pushed(main, "trunk")

    verdict = foreign.verdict(before, repository_state(main))

    assert "refs/remotes/origin/trunk" in verdict.failure


def test_a_remote_ref_matching_no_sibling_branch_still_fails(tmp_path: Path) -> None:
    """A ref that appeared from nowhere is nobody else's, remote or not."""
    main = repository_with_a_remote(tmp_path)
    foreign = ForeignCheckouts.beside(main)
    before = repository_state(main)
    sh.Command("git")(
        "-C",
        str(main),
        "update-ref",
        "refs/remotes/origin/nobody",
        "HEAD",
        _tty_out=False,
    )

    verdict = foreign.verdict(before, repository_state(main))

    assert "refs/remotes/origin/nobody" in verdict.failure


def test_a_branch_tracking_a_differently_named_remote_is_attributed(
    tmp_path: Path,
) -> None:
    """Why `upstream` is asked for rather than the two names being joined."""
    main = repository_with_a_remote(tmp_path)
    sibling = tmp_path / "sibling"
    sh.Command("git")(
        "-C", str(sibling), "push", "-u", "origin", "sibling:renamed", _tty_out=False
    )

    foreign = ForeignCheckouts.beside(main)

    assert foreign.holder("refs/remotes/origin/renamed") == str(sibling.resolve())


def test_no_remote_is_claimed_when_git_cannot_say(tmp_path: Path) -> None:
    """The module's rule: a guard that cannot answer fails on everything."""
    assert ForeignCheckouts.remotes(tmp_path / "nowhere") == []


def worktree_cut(main: Path, name: str, at: Path) -> None:
    """What a sibling session's `worktree create` does to the shared ref store."""
    sh.Command("git")(
        "-C", str(main), "worktree", "add", "-b", name, str(at), _tty_out=False
    )


def test_a_worktree_cut_while_the_suite_runs_holds_its_branch(tmp_path: Path) -> None:
    """A sibling session's `worktree create`, partway through the run.

    A map read before the worktree existed holds nothing for its branch, so
    the branch reads as appearing from nowhere. Joined with a map read after
    the change, the worktree answers for it and the run is told, not failed.
    """
    main = repository_with_a_sibling(tmp_path)
    foreign = ForeignCheckouts.beside(main)
    before = repository_state(main)
    worktree_cut(main, "late", tmp_path / "late")
    after = repository_state(main)

    assert "refs/heads/late: created" in foreign.verdict(before, after).failure
    verdict = foreign.joined(ForeignCheckouts.beside(main)).verdict(before, after)
    assert verdict.failure == ""
    assert "refs/heads/late" in verdict.notice


def test_a_watch_tells_of_a_sibling_cut_mid_run_without_failing(
    tmp_path: Path,
) -> None:
    """The same case through the watch a suite actually arms, window named."""
    main = repository_with_a_sibling(tmp_path)
    watch = RepositoryWatch.armed(main, worker="gw2")
    worktree_cut(main, "late", tmp_path / "late")

    verdict = watch.after("tests/test_matrix.py::test_row[gh pr create]")

    assert verdict.failure == ""
    assert "refs/heads/late" in verdict.notice
    assert (
        "noticed on worker gw2, during tests/test_matrix.py::test_row[gh pr create]"
        in verdict.notice
    )


def git_in(where: Path, *arguments: str) -> None:
    """Run git bound to ``where``, as the suite's own identity."""
    sh.Command("git")(
        "-C",
        str(where),
        *arguments,
        _tty_out=False,
        _env={**os.environ, **TEST_IDENTITY.environment()},
    )


def stashed(where: Path, tag: str) -> None:
    """Leave work in ``where`` and stash it, as a session setting it aside does."""
    (where / f"{tag}.txt").write_text("work in progress\n")
    git_in(where, "stash", "push", "-u", "-m", tag)


def test_a_siblings_stash_during_a_watch_is_noticed_without_failing(
    tmp_path: Path,
) -> None:
    """The case that made this necessary: one stash, pushed onto by every worktree.

    Both checkouts stand on one commit here, as every worktree does just after
    `worktree create`, so the entry's parent names both and its subject is
    what says the sibling made it.
    """
    main = repository_with_a_sibling(tmp_path)
    watch = RepositoryWatch.armed(main, worker="gw1")
    stashed(tmp_path / "sibling", "supervision-page-wip")

    verdict = watch.after("tests/test_matrix.py::test_row[gh pr create]")

    assert verdict.failure == ""
    assert "refs/stash: created" in verdict.notice
    assert "Another session stashing in a sibling worktree" in verdict.notice


def test_a_stash_in_the_watched_checkout_fails_over_a_siblings(
    tmp_path: Path,
) -> None:
    """A sibling's earlier entry excuses nothing stacked on it afterwards.

    The stash has no standing holder: the worktree that pushed last time says
    nothing about who pushed this time, so this checkout's push fails even in
    the window straight after a sibling's push was told rather than failed.
    """
    main = repository_with_a_sibling(tmp_path)
    watch = RepositoryWatch.armed(main, worker="gw1")
    stashed(tmp_path / "sibling", "theirs")
    assert watch.after("tests/test_a.py::test_first").failure == ""
    stashed(main, "an-escaped-fixture")

    verdict = watch.after("tests/test_a.py::test_second")

    assert "refs/stash: " in verdict.failure
    assert "stash every worktree shares" in verdict.failure


def test_a_stash_on_a_branch_nobody_holds_fails(tmp_path: Path) -> None:
    """A branch no worktree holds at either reading is nobody else's to stash on.

    The sibling stashes on a branch of its own and leaves it, so no checkout
    stands on the entry's parent and the branch its subject names is held by
    nobody when the watch reads.
    """
    main = repository_with_a_sibling(tmp_path)
    sibling = tmp_path / "sibling"
    git_in(sibling, "switch", "-c", "loose")
    committed(sibling, "loose work")
    git_in(sibling, "switch", "sibling")
    watch = RepositoryWatch.armed(main, worker="gw1")
    git_in(sibling, "switch", "loose")
    stashed(sibling, "left-behind")
    git_in(sibling, "switch", "sibling")

    verdict = watch.after("tests/test_a.py::test_row")

    assert "refs/stash: created" in verdict.failure


def test_a_siblings_pop_still_fails(tmp_path: Path) -> None:
    """Git records where an entry was made, never who removed it."""
    main = repository_with_a_sibling(tmp_path)
    sibling = tmp_path / "sibling"
    stashed(sibling, "theirs")
    watch = RepositoryWatch.armed(main, worker="gw1")
    git_in(sibling, "stash", "pop")

    verdict = watch.after("tests/test_a.py::test_row")

    assert "refs/stash: deleted" in verdict.failure


def listed(path: str, head: str, branch: str = "") -> Worktree:
    """One checkout as a listing names it, detached where it holds no branch."""
    return Worktree(path=Path(path), head=head, branch=branch, detached=not branch)


def test_the_one_checkout_on_the_parent_made_the_entry() -> None:
    """The parent decides where it names one checkout, whatever the subject says."""
    listing = [listed("/own", "c1", "trunk"), listed("/sibling", "c2", "sibling")]
    theirs = str(Path("/sibling").resolve())
    own = Path("/own").resolve()

    sibling_made = StashEntry(parent="c2", subject="On trunk: x")
    own_made = StashEntry(parent="c1", subject="On sibling: x")

    assert sibling_made.maker(listing, own, {"refs/heads/sibling": theirs}) == theirs
    assert own_made.maker(listing, own, {"refs/heads/sibling": theirs}) is None


def test_a_detached_checkout_on_the_parent_names_no_maker() -> None:
    """A checkout holding no branch leaves no sibling to answer for its entry."""
    listing = [listed("/own", "c1", "trunk"), listed("/loose", "c2")]
    entry = StashEntry(parent="c2", subject="On (no branch): x")

    assert entry.maker(listing, Path("/own").resolve(), {}) is None


def test_the_subject_decides_where_the_parent_names_several_or_none() -> None:
    """Checkouts sharing a tip, or a stasher that has committed since."""
    listing = [listed("/own", "c1", "trunk"), listed("/sibling", "c1", "sibling")]
    theirs = str(Path("/sibling").resolve())
    holds = {"refs/heads/sibling": theirs}
    own = Path("/own").resolve()

    shared = StashEntry(parent="c1", subject="WIP on sibling: c1 root")
    moved_on = StashEntry(parent="c0", subject="On sibling: x")
    own_made = StashEntry(parent="c1", subject="On trunk: x")

    assert shared.maker(listing, own, holds) == theirs
    assert moved_on.maker(listing, own, holds) == theirs
    assert own_made.maker(listing, own, holds) is None


def test_a_branch_name_is_matched_whole() -> None:
    """``feat`` is not the branch an entry made on ``feat/x`` was made on."""
    entry = StashEntry(parent="", subject="On feat/x: draft")

    assert entry.made_on("feat/x")
    assert not entry.made_on("feat")
