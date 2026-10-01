"""What the pre-flight gate is answerable for when it runs inside a lease.

A resolver lease holds one concern's changes and is judged by `dev check`. An
unscoped anti-pattern gate makes that verdict depend on the whole repository,
so one finding nobody in the run introduced blocks every lease at once and no
revision round can converge on it. These pin the split.

The split is made by not reading a file rather than by discarding what reading
it found: the sweep's dominant cost is resolving the files it reads, and a
gate that read the whole repository to set most of it aside paid that cost to
reach a verdict it then threw away.

And what it says about a state no file in the tree can settle. A move that
depends on somebody remembering does not happen, so the gate carries the one
still outstanding: it names the command while there is anything to move, and
says nothing once there is not.
"""

from pathlib import Path
from time import perf_counter

import pytest
import typer

from lup.devtools.dev.antipatterns import within_scope
from lup.devtools.dev.check import (
    CheckReport,
    branch_record_reports,
    named_gate_base,
    spent,
)
from lup.execution.process import LaunchRequest, LocalProcessLauncher


def test_a_pending_move_names_the_command_and_counts_the_branches() -> None:
    # A reader meets this cold, so the row has to carry the whole instruction:
    # what is left, what runs, and where it has to be run from.
    [report] = branch_record_reports(["topic", "feat.v2"])
    printed = "\n".join(report.lines)

    assert "branch records: 2 branch(es)" in printed
    assert "`lup-devtools git worktree adopt-records`" in printed
    assert "once per clone" in printed
    assert "on the host" in printed


def test_a_pending_move_advises_rather_than_gates() -> None:
    # Reads fall back to the old keys, so nothing here is a defect — and the
    # host is where the write happens, which no session standing in a
    # worktree can reach. A gating row would be red until somebody left.
    [report] = branch_record_reports(["topic"])

    assert not report.counted
    assert report.passed


def test_a_finished_move_reports_nothing_at_all() -> None:
    # Not a permanent ok: a row that can only pass from here on is a row a
    # reader stops seeing, and this one has an end.
    assert branch_record_reports([]) == []


def test_an_unscoped_gate_reads_every_file() -> None:
    # CI asks whether the tree is clean, so nothing is out of its scope.
    assert within_scope("a.py", None)
    assert within_scope("packages/lup/src/lup/providers/profiles.py", None)


def test_a_file_outside_the_changed_paths_is_not_read() -> None:
    changed = ["packages/lup/src/lup/touched.py"]

    assert within_scope("packages/lup/src/lup/touched.py", changed)
    assert not within_scope("packages/lup/src/lup/providers/profiles.py", changed)


def test_a_lease_that_changed_nothing_the_rules_hit_is_green() -> None:
    # The case that deadlocked run resolve-9e060ad9bb53: every lease read the
    # same pre-split ProfileStore, and the gate failed identically in all of
    # them however much the worker changed elsewhere.
    assert not within_scope(
        "packages/lup/src/lup/providers/profiles.py",
        ["packages/lup/src/lup/devtools/dev/worktree.py"],
    )


def test_a_scope_naming_nothing_reads_nothing() -> None:
    # A tree that changed nothing is answerable for nothing, which is not the
    # same answer as a tree nobody scoped.
    assert not within_scope("a.py", [])


def test_a_named_directory_covers_what_sits_under_it() -> None:
    scope = ["packages/lup/src/lup/devtools"]

    assert within_scope("packages/lup/src/lup/devtools/dev/check.py", scope)
    assert not within_scope("packages/lup/src/lup/devtools_other/check.py", scope)


def test_a_directory_names_one_scope_however_it_is_spelled() -> None:
    # A shell completing a directory writes the trailing separator, so the
    # spelling a caller most easily types is the one that has to work. Compared
    # as text it matched nothing, and a directory full of findings reported
    # clean — a false answer rather than a visibly empty scope.
    under = "packages/lup/src/lup/devtools/dev/check.py"

    assert within_scope(under, ["packages/lup/src/lup/devtools/"])
    assert within_scope(under, ["packages/lup/src/lup/devtools"])
    assert within_scope(under, ["./packages/lup/src/lup/devtools"])
    assert not within_scope(under, ["packages/lup/src/lup/devtools_other/"])


def test_a_file_named_absolutely_names_the_same_scope() -> None:
    # A caller holding a path holds an absolute one — a hook handed the file
    # that was written, a script resolving its own argument. Compared as
    # written it matched nothing, so the sweep answered "clean" for a file it
    # never read: the same false answer the trailing separator gave.
    under = "packages/lup/src/lup/devtools/dev/check.py"

    assert within_scope(under, [str(Path.cwd() / under)])
    assert within_scope(under, [str(Path.cwd() / "packages/lup/src/lup/devtools")])


def test_a_file_outside_this_checkout_still_names_nothing() -> None:
    # Not the same bug: no relative spelling of it would be true, and the
    # sweep is not answerable for a file outside the tree it walks.
    assert not within_scope(
        "packages/lup/src/lup/devtools/dev/check.py", ["/elsewhere/repo/src"]
    )


def test_the_cost_line_ranks_every_timed_check() -> None:
    # The gate runs its tools at once, so its wall time is waiting on exactly
    # one of them. The ranking says which, and what it would wait on next.
    reports = [
        CheckReport(name="ruff check", lines=[], elapsed=0.4),
        CheckReport(name="pytest", lines=[], elapsed=136.2),
        CheckReport(name="pyright", lines=[], elapsed=124.0),
    ]

    line = spent(reports, perf_counter() - 137.0)

    assert "pytest 136s, pyright 124s, ruff check 0s" in line
    assert line.startswith(" in 137s")


def test_an_untimed_row_is_left_out_rather_than_called_free() -> None:
    # A sweep costs what the gate's own wall time already accounts for, and a
    # zero beside the tools invites somebody to optimise a number that
    # measures nothing.
    reports = [
        CheckReport(name="pytest", lines=[], elapsed=90.0),
        CheckReport(name="antipatterns", lines=[]),
    ]

    line = spent(reports, perf_counter() - 91.0)

    assert "antipatterns" not in line
    assert "pytest 90s" in line


def test_a_gate_that_timed_nothing_still_says_what_it_cost() -> None:
    # `--no-test` and the sweep-only modes run no external tool at all, and
    # the wall time is the whole of what such a run has to report.
    line = spent([CheckReport(name="antipatterns", lines=[])], perf_counter() - 3.0)

    assert line == " in 3s"


def base_history(root: Path) -> Path:
    """A `topic` cut from `main`, with `main` moving on afterwards."""
    work = root / "work"
    who = ("-c", "user.email=gate@example.test", "-c", "user.name=Gate Test")
    launcher = LocalProcessLauncher()
    for arguments in (
        ["git", "init", "-q", "-b", "main", str(work)],
        ["git", "-C", str(work), *who, "commit", "-q", "--allow-empty", "-m", "base"],
        ["git", "-C", str(work), "switch", "-q", "-c", "topic"],
        ["git", "-C", str(work), *who, "commit", "-q", "--allow-empty", "-m", "mine"],
    ):
        status = launcher.launch(LaunchRequest(arguments=arguments, cwd=root))
        if status.code != 0:
            raise AssertionError(status.stderr)
    return work


def test_a_named_base_is_taken_as_the_merge_base_not_the_tip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override answers where the branch started, not where the base is.

    A base that has moved on carries commits this branch never made, and read
    against its tip they come back as capabilities the branch removed — which
    is how naming `dev` directly reported 504 gone on a branch that had
    removed none.
    """
    work = base_history(tmp_path)
    monkeypatch.chdir(work)
    fork = LocalProcessLauncher().launch(
        LaunchRequest(arguments=["git", "rev-parse", "main"], cwd=work)
    )

    assert named_gate_base("main") == fork.stdout.strip()


def test_a_named_base_nothing_resolves_refuses_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Answering nothing would read exactly like a branch that removed nothing.

    Which is the reading a mistyped ref most wants to be mistaken for, so the
    run stops and names what it could not resolve.
    """
    monkeypatch.chdir(base_history(tmp_path))

    with pytest.raises(typer.BadParameter, match="shares no history"):
        named_gate_base("no-such-branch")
