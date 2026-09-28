"""The guard that catches a suite writing into the checkout it runs inside."""

from pathlib import Path

import pytest
import sh

import lup.devtools.gitguard as gitguard
from lup.devtools.dev.git_guards import (
    DECLARED_GUARDS,
    DRIFT_COMMAND,
    MERGE_STANDDOWN,
    GitGuard,
    hooks_directory,
    install_guards,
    read_hooks,
)
from lup.devtools.gitguard import (
    GIT_ENVIRONMENT,
    GuardVerdict,
    RepositoryWatch,
    Window,
    guard_report,
    moved_refs,
    repository_refs,
    repository_state,
    watched_config,
)
from lup.execution.shell import git
from lup.harness.toolchain import preflight_namespace
from lup.policy.assets.host import undo_namespace
from tests.unit.test_ledger_placement import committed, repository
from tests.unit.test_scaffold import wrote


def test_every_installed_hook_scrubs_the_environment_before_its_check() -> None:
    """A hook is handed this repository in the environment, which outranks `-C`.

    That is the one way in that a suite cannot close from its own side, however
    carefully each helper binds its git, so the hook closes it instead. Before
    the check rather than anywhere inside it: the names have to be gone by the
    time anything the check runs asks git which repository it is in.
    """
    for guard in DECLARED_GUARDS:
        check = guard.check()

        assert guard.environment == GIT_ENVIRONMENT
        assert f"unset {' '.join(guard.environment)}" in check
        assert check.index("unset ") < check.index(f"exec {guard.command}")


def test_the_declared_commit_guard_reads_the_merge_before_it_checks() -> None:
    """The standdown goes after the scrub and before the check, in that order.

    After, because the scrub is what makes `git rev-parse` resolve the
    repository the hook is running in rather than the one a name in the
    environment points at. Before, because a check that has already started
    cannot be stood down.
    """
    for guard in (one for one in DECLARED_GUARDS if one.hook == "pre-commit"):
        check = guard.check()

        assert guard.standdown == MERGE_STANDDOWN
        assert (
            check.index("unset ")
            < check.index("MERGE_HEAD")
            < check.index(f"exec {guard.command}")
        )


def test_the_commit_guard_stands_down_for_a_merge_and_not_for_what_follows(
    tmp_path: Path,
) -> None:
    """Where a generated tree cannot be judged yet, and where it can again.

    A commit concluding a merge is mid-transaction: the trees are compiled
    from declarations the resolution has just rewritten, and the regeneration
    that settles them comes after. Git draws that line itself for the merge it
    completes on its own, which runs `pre-merge-commit` — a moment nothing
    here declares — so this guard was refusing exactly the merges somebody had
    to resolve by hand, and only those. It stands down for one merge commit
    and for nothing else, which is what the commit after it has to show.
    """
    root = repository(tmp_path / "checkout")
    wrote(root, "a.txt", "base\n")
    committed(root, "base")
    git("-C", str(root), "checkout", "-q", "-b", "other")
    wrote(root, "a.txt", "other's\n")
    committed(root, "other's own")
    git("-C", str(root), "checkout", "-q", "main")
    wrote(root, "a.txt", "main's\n")
    committed(root, "main's own")
    install_guards([GitGuard(command="exit 1", standdown=MERGE_STANDDOWN)], root)
    git("-C", str(root), "merge", "--no-edit", "other", _ok_code=[0, 1])
    wrote(root, "a.txt", "both\n")
    git("-C", str(root), "add", "a.txt")

    git("-C", str(root), "commit", "--no-edit", "-q")
    merge = git.out("-C", str(root), "rev-parse", "HEAD")
    wrote(root, "b.txt", "after the merge\n")
    git("-C", str(root), "add", "b.txt")
    git("-C", str(root), "commit", "-q", "-m", "after", _ok_code=[0, 1])

    second_parent = git.out(
        "-C",
        str(root),
        "rev-parse",
        "--verify",
        "--quiet",
        f"{merge}^2",
        _ok_code=[0, 1],
    )
    assert second_parent
    assert git.out("-C", str(root), "rev-parse", "HEAD") == merge
    assert git.lines("-C", str(root), "diff", "--name-only", "--cached") == ["b.txt"]


def test_a_guard_that_wants_nothing_dropped_writes_no_scrub() -> None:
    """The names are a default, so a project can decline them.

    Declining has to leave a hook that still runs, rather than one carrying a
    bare `unset` and a comment explaining a line that is not there.
    """
    check = GitGuard(environment=()).check()

    assert "unset" not in check
    assert check.endswith(f"exec {DRIFT_COMMAND}\n")


def test_a_hook_at_a_moment_nothing_declares_is_reported_then_cleared(
    tmp_path: Path,
) -> None:
    """The half a declaration cannot report on its own.

    Every other reading here starts from the declaration, so a moment leaving
    it takes its own reporting with it: git goes on running the file while
    the gate that would have said so has stopped looking at that path. A
    checkout armed by an older declaration would keep paying for a guard
    nobody asks for, and read as fully armed while doing it.
    """
    sh.Command("git").bake("-C", str(tmp_path), _tty_out=False)("init", "-b", "main")
    declared = [GitGuard()]
    install_guards(
        [*declared, GitGuard(hook="pre-push", command="echo gate")], tmp_path
    )
    hooks = hooks_directory(tmp_path)
    assert (hooks / "pre-push").is_file()

    reading = read_hooks(declared, tmp_path)
    cleared = install_guards(declared, tmp_path)

    assert [state.path.name for state in reading.orphaned] == ["pre-push"]
    assert [state.status for state in cleared] == ["current", "retired"]
    assert not (hooks / "pre-push").exists()
    assert read_hooks(declared, tmp_path).orphaned == []


def test_a_hook_this_did_not_write_is_left_where_it_is(tmp_path: Path) -> None:
    """Clearing a moment is about this command's own files and nothing else.

    A repository may guard a moment lup never declared, for reasons of its
    own. Reading the marker is what tells the two apart, so the sweep that
    retires a dropped guard cannot reach a hook somebody else installed.
    """
    sh.Command("git").bake("-C", str(tmp_path), _tty_out=False)("init", "-b", "main")
    hooks = hooks_directory(tmp_path)
    hooks.mkdir(parents=True, exist_ok=True)
    theirs = hooks / "pre-push"
    theirs.write_text("#!/bin/sh\nexec ./their-own-check\n", encoding="utf-8")

    install_guards([GitGuard()], tmp_path)

    assert read_hooks([GitGuard()], tmp_path).orphaned == []
    assert theirs.read_text(encoding="utf-8") == "#!/bin/sh\nexec ./their-own-check\n"


def test_a_session_that_touched_nothing_reports_nothing() -> None:
    """The quiet case is every run, so it must never cost a false failure."""
    refs = {"refs/heads/dev": "a" * 40}

    assert moved_refs(refs, refs) == []
    assert guard_report(refs, refs) == ""


def test_every_way_a_ref_can_move_is_named() -> None:
    """Moved, created, and deleted are three different accidents to recover from."""
    before = {"refs/heads/dev": "a" * 40, "refs/heads/gone": "b" * 40}
    after = {"refs/heads/dev": "c" * 40, "refs/heads/new": "d" * 40}

    assert moved_refs(before, after) == [
        "refs/heads/dev: aaaaaaaaaaaa -> cccccccccccc",
        "refs/heads/new: created",
        "refs/heads/gone: deleted",
    ]


def test_the_report_names_the_refs_and_how_to_get_them_back() -> None:
    """A developer reading this has a moved branch and no idea which fixture.

    So the report has to carry both halves: which refs moved, and that the
    reflog is where each one is recovered from.
    """
    report = guard_report({"refs/heads/dev": "a" * 40}, {"refs/heads/dev": "b" * 40})

    assert "refs/heads/dev: aaaaaaaaaaaa -> bbbbbbbbbbbb" in report
    assert "git reflog show <ref>" in report


def test_refs_are_read_from_a_real_repository(tmp_path: Path) -> None:
    """Read through git, so a worktree's refs are found where git keeps them."""
    git = sh.Command("git").bake(
        "-C",
        str(tmp_path),
        "-c",
        "user.email=guard@example.test",
        "-c",
        "user.name=Guard",
        _tty_out=False,
    )
    git("init", "-b", "main")
    (tmp_path / "file.txt").write_text("one\n", encoding="utf-8")
    git("add", "file.txt")
    git("commit", "-m", "one")

    before = repository_refs(tmp_path)
    git("branch", "sneaky")

    assert "refs/heads/main" in before
    assert moved_refs(before, repository_refs(tmp_path)) == [
        "refs/heads/sneaky: created"
    ]


def test_a_directory_outside_any_repository_yields_no_refs(tmp_path: Path) -> None:
    """A suite run outside a checkout must not fail on the guard's own footing."""
    assert repository_refs(tmp_path / "nowhere") == {}


def guarded_repository(tmp_path: Path) -> sh.Command:
    """A checkout with one commit, bound to a git that commits as somebody."""
    git = sh.Command("git").bake(
        "-C",
        str(tmp_path),
        "-c",
        "user.email=guard@example.test",
        "-c",
        "user.name=Guard",
        _tty_out=False,
    )
    git("init", "-b", "main")
    git("commit", "--allow-empty", "-m", "one")
    return git


def test_a_snapshot_taken_while_the_suite_runs_is_not_the_suites_doing(
    tmp_path: Path,
) -> None:
    """The dispatcher writes one of these in front of every command it allows.

    So an agent running the suite has refs appearing under that namespace
    throughout, from outside the suite and on a schedule it does not control.
    Failing on them made every agent-run check report eight teardown failures,
    one per worker, naming refs no fixture had touched — which is the guard
    crying wolf on exactly the runs somebody was watching it.
    """
    git = guarded_repository(tmp_path)
    before = repository_state(tmp_path)
    git("update-ref", f"{undo_namespace()}/20260822T030458856293-25ed70890b45", "HEAD")

    assert moved_refs(before, repository_state(tmp_path)) == []


def test_a_preflight_probe_beside_the_suite_is_not_the_suites_doing(
    tmp_path: Path,
) -> None:
    """`dev check` probes the checkpoint store while the suites run beside it.

    The probe writes a ref named for its process under the preflight
    namespace and deletes it again, so whichever worker happened to be
    between tests just then reported a ref created that no fixture wrote.
    """
    git = guarded_repository(tmp_path)
    before = repository_state(tmp_path)
    git("update-ref", f"{preflight_namespace()}/45421", "HEAD")

    assert moved_refs(before, repository_state(tmp_path)) == []


def test_a_branch_moving_beside_a_snapshot_is_still_caught(tmp_path: Path) -> None:
    """Narrowing what is watched must not narrow what the narrowing was for."""
    git = guarded_repository(tmp_path)
    before = repository_state(tmp_path)
    git("update-ref", f"{undo_namespace()}/20260822T030502482427-25ed70890b45", "HEAD")
    git("branch", "escaped")

    assert moved_refs(before, repository_state(tmp_path)) == [
        "refs/heads/escaped: created"
    ]


def test_a_suite_watching_its_own_namespace_still_watches_the_real_one(
    tmp_path: Path,
) -> None:
    """`undo_snapshot` takes a namespace, so the guard has to take the same one.

    A suite exercising snapshots points them somewhere of its own; the refs it
    must still be answerable for are the ones the dispatcher would have
    written, which is the namespace it is not using.
    """
    git = guarded_repository(tmp_path)
    before = repository_state(tmp_path, namespace="refs/lup/undo-under-test")
    git("update-ref", f"{undo_namespace()}/20260822T030505499617-25ed70890b45", "HEAD")

    assert moved_refs(
        before, repository_state(tmp_path, "refs/lup/undo-under-test")
    ) == [f"{undo_namespace()}/20260822T030505499617-25ed70890b45: created"]


def test_a_fixture_that_writes_a_committer_identity_is_caught(tmp_path: Path) -> None:
    """The quieter half, and the one that actually bit.

    A fixture setting `user.email` on the enclosing repository is inherited by
    every worktree cut from it, so work committed hours later in another
    session carries that author. Nothing about it is visible at the time.
    """
    git = sh.Command("git").bake("-C", str(tmp_path), _tty_out=False)
    git("init", "-b", "main")
    before = repository_state(tmp_path)
    git("config", "user.email", "fixture@example.test")

    assert watched_config(tmp_path) == {"config user.email": "fixture@example.test"}
    assert moved_refs(before, repository_state(tmp_path)) == [
        "config user.email: created"
    ]


def test_a_fixture_that_writes_a_hooks_path_is_caught(tmp_path: Path) -> None:
    """The half that takes the alarm out with it.

    `core.hooksPath` in the shared config points every worktree cut from the
    repository at a directory a fixture built, so the checkout runs no hooks
    at all — and one whose guards are gone reports exactly what one whose
    guards pass reports. Watched here because nothing else would say so: the
    guards cannot report their own absence.
    """
    git = sh.Command("git").bake("-C", str(tmp_path), _tty_out=False)
    git("init", "-b", "main")
    before = repository_state(tmp_path)
    git("config", "core.hooksPath", str(tmp_path / "hooks"))

    assert moved_refs(before, repository_state(tmp_path)) == [
        "config core.hooksPath: created"
    ]


def test_a_repository_leaving_identity_to_the_global_config_reads_empty(
    tmp_path: Path,
) -> None:
    """Absent is the normal case, so it must not read as a change from nothing."""
    sh.Command("git").bake("-C", str(tmp_path), _tty_out=False)("init", "-b", "main")

    assert watched_config(tmp_path) == {}


def test_watched_settings_come_back_under_their_declared_spelling(
    tmp_path: Path,
) -> None:
    """One listing serves every setting, and git's lowercasing does not leak.

    The value is chosen to hold the characters the listing's own delimiters
    would have tripped on had it been parsed as `key=value` lines.
    """
    git = sh.Command("git").bake("-C", str(tmp_path), _tty_out=False)
    git("init", "-b", "main")
    git("config", "core.hooksPath", str(tmp_path / "hooks"))
    git("config", "user.name", "A = B")

    assert watched_config(tmp_path) == {
        "config core.hooksPath": str(tmp_path / "hooks"),
        "config user.name": "A = B",
    }


def test_the_report_says_where_the_change_was_noticed() -> None:
    """Under xdist the reader's first question is which worker, during what."""
    window = Window(worker="gw3", test="tests/unit/test_x.py::test_y")

    report = guard_report(
        {"refs/heads/dev": "a" * 40}, {"refs/heads/dev": "b" * 40}, window
    )

    assert "noticed on worker gw3, during tests/unit/test_x.py::test_y" in report
    assert "bystander" in report


def test_a_watch_lays_a_change_at_the_door_of_the_test_that_saw_it(
    tmp_path: Path,
) -> None:
    """The window that saw the change answers for it; the ones after do not.

    A difference closed once per session lands on whichever test the worker
    ran last, which is how a policy row about `gh pr create` was blamed for
    a branch. Settling per test names the window, and moves the baseline so
    the next window is not blamed for the same branch again.
    """
    git = guarded_repository(tmp_path)
    watch = RepositoryWatch.armed(tmp_path, worker="gw7")

    assert watch.after("tests/test_one.py::test_first") == GuardVerdict()
    git("branch", "escaped")
    verdict = watch.after("tests/test_one.py::test_second")
    assert "refs/heads/escaped: created" in verdict.failure
    assert (
        "noticed on worker gw7, during tests/test_one.py::test_second"
        in verdict.failure
    )
    assert watch.after("tests/test_one.py::test_third") == GuardVerdict()


def storing_every_way(tmp_path: Path) -> sh.Command:
    """A checkout holding a ref each way git stores one: loose, and packed.

    Two commits, so a ref can move to the other one: `side` is loose at the
    first, and `packed` sits in the packed list at the first.
    """
    git = guarded_repository(tmp_path)
    git("branch", "packed")
    git("pack-refs", "--all")
    git("branch", "side")
    git("commit", "--allow-empty", "-m", "two")
    return git


@pytest.mark.parametrize(
    ("write", "moved"),
    [
        pytest.param(("branch", "escaped"), "refs/heads/escaped: created", id="create"),
        pytest.param(
            ("update-ref", "refs/heads/side", "main"),
            "refs/heads/side: ",
            id="move-loose",
        ),
        pytest.param(
            ("update-ref", "refs/heads/packed", "main"),
            "refs/heads/packed: ",
            id="move-packed",
        ),
        pytest.param(
            ("branch", "-D", "packed"), "refs/heads/packed: deleted", id="delete-packed"
        ),
        pytest.param(
            ("branch", "-D", "side"), "refs/heads/side: deleted", id="delete-loose"
        ),
        pytest.param(
            ("config", "core.hooksPath", "/x"),
            "config core.hooksPath: created",
            id="config",
        ),
    ],
)
def test_a_watch_reading_files_first_still_catches_every_write(
    tmp_path: Path, write: tuple[str, ...], moved: str
) -> None:
    """The cheap reading has to notice whatever the full one would report.

    A ref moved in place is the case a size or a timestamp could miss: the
    file keeps its length, and two writes a few milliseconds apart can share
    a timestamp. Git renames a new file over the old one, so it is the inode
    that tells them apart.
    """
    git = storing_every_way(tmp_path)
    watch = RepositoryWatch.armed(tmp_path, worker="gw1")
    git(*write)

    assert moved in watch.after("tests/test_one.py::test_writing").failure


def test_a_quiet_window_asks_git_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test that wrote nothing costs no process, snapshots beside it included.

    The snapshot namespace is written in front of every command an agent is
    allowed, so a watch that went to git for those would go on every test of
    an agent-run suite, which is the cost the file reading exists to spare.
    """
    git = storing_every_way(tmp_path)
    watch = RepositoryWatch.armed(tmp_path, worker="gw1")

    def unasked(root: Path, namespace: str = "") -> dict[str, str]:
        raise AssertionError(f"the state of {root} was read")

    monkeypatch.setattr(gitguard, "repository_state", unasked)
    git("update-ref", f"{undo_namespace()}/20260928T170000000000-25ed70890b45", "HEAD")

    assert watch.after("tests/test_one.py::test_quiet") == GuardVerdict()


def test_the_report_names_the_reading_the_refs_cannot_rule_out() -> None:
    """A commit made here mid-run and a stray fixture are the same event to a ref.

    Asserting the fixture reasons that a developer can rule out having moved
    a branch themselves. Where several sessions share a clone that stops
    holding, and a reader handed only that reading spends the length of a
    gate hunting a fixture that is not there — measured on this repository,
    twice in one session.
    """
    said = guard_report(
        {"refs/heads/feat-a": "1111111"}, {"refs/heads/feat-a": "2222222"}
    )

    assert "find the fixture" in said
    assert "reflog show" in said
    assert "stopped moving" in said
