"""Telling a sibling worktree's commit apart from a fixture escaping.

Every worktree cut from a repository shares its ref store, so the guard
reading `for-each-ref` in one of them sees every branch the repository holds.
Somebody committing in a sibling while the suite runs moves a ref for real,
and blaming this run for it turns a routine event into a failure that reads
exactly like the accident the guard exists for — which is how a real one comes
to be waved through.
"""

import os
from pathlib import Path

import sh

from lup.devtools.gitguard import (
    TEST_IDENTITY,
    ForeignCheckouts,
    RepositoryWatch,
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
    listing = (
        "worktree /a\nHEAD abc\ndetached\n\n"
        "worktree /b\nHEAD def\nbranch refs/heads/held\n\n"
        "worktree /c\nbare\n"
    )

    assert ForeignCheckouts.declared(listing, Path("/other")) == {
        "refs/heads/held": str(Path("/b").resolve())
    }


def test_the_checkout_under_test_never_counts_as_foreign() -> None:
    """Its own branch is exactly the one the guard must keep answering for."""
    listing = "worktree /a\nHEAD abc\nbranch refs/heads/mine\n"

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
