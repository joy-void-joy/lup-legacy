"""What a commit does when a generated artifact is behind its source.

The guard is a refusal, and a refusal is only worth what an attempt proves, so
these tests write a real repository, arm it, and run `git commit` — the whole
point being that reading the installed configuration would have said the guard
was fine on the day it let two stale commits through.

The miniature repository copies one source verbatim into one generated file,
which is the shape of the case that actually failed: the policy kernel modules
are copied into both plugin trees byte for byte, so rewording a comment in one
of them makes both trees stale without changing anything either does. The hook
runs the production drift verdict over that copy rather than a stand-in for it.
"""

import sys
from pathlib import Path

import pytest
import sh

from lup.devtools.dev.git_guards import (
    CHECK_COMMAND,
    DECLARED_GUARDS,
    DELETION_STANDDOWN,
    DRIFT_COMMAND,
    LEGACY_GUARD_MARKER,
    GitGuard,
    GuardConflict,
    HookScript,
    arm,
    hook_scripts,
    install_guards,
    read_guards,
    read_hooks,
    uninstall_guards,
)
from lup.devtools.dev.workflow import WorkflowSpec
from lup.formats.banner import REGENERATE_COMMAND
from tests.unit.repos import commit_file, initialized_repo

GUARD_SCRIPT = '''"""Refuse a commit while this repository's one generated file is behind."""

import sys
from pathlib import Path

from lup.devtools.harness.drift import inspect_drift, report_stale
from lup.formats.banner import REGENERATE_COMMAND, VERBATIM_COPY
from lup.harness.materialization import write_generated_file
from lup.harness.models import Artifact

ROOT = Path(__file__).parent


def write(root: Path | None = None, *, check: bool = False) -> Path:
    """Write or verify the generated copy, whose source is copied byte for byte."""
    artifact = Artifact(
        path=Path("generated/kernel.py"),
        content=(ROOT / "canon.py").read_text(encoding="utf-8"),
        semantic_id="test.kernel",
        banner=VERBATIM_COPY,
    )
    return write_generated_file(artifact, ROOT, REGENERATE_COMMAND, check=check)


if "--generate" in sys.argv:
    write()
    sys.exit(0)

verdict = inspect_drift([], [write])
if verdict.clean:
    sys.exit(0)
report_stale(verdict)
sys.exit(1)
'''


def canon(note: str) -> str:
    """A kernel-shaped source whose only variable part is one comment."""
    return (
        '"""One kernel module, copied verbatim into the generated tree."""\n'
        "\n"
        f"# {note}\n"
        "LIMIT = 3\n"
    )


class Repository:
    """A throwaway repository with the guard armed over one generated copy."""

    def __init__(self, work: Path, git: sh.Command, script: Path) -> None:
        self.work = work
        self.git = git
        self.script = script

    def regenerate(self) -> None:
        """Bring the generated copy back in step with its source."""
        sh.Command(sys.executable)(str(self.script), "--generate")

    def commit(self, message: str) -> None:
        """Stage everything and commit, the way the failing commits were made."""
        self.git("add", "-A")
        self.git("commit", "-m", message)

    def commits(self) -> int:
        """How many commits history holds, which is what a refusal protects."""
        return int(str(self.git("rev-list", "--count", "HEAD")))


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    # `initialized_repo` passes the hooks directory per-invocation; recording it
    # is what lets the guard find the directory git will actually read.
    git("config", "core.hooksPath", str(hooks))
    script = work / "guard.py"
    script.write_text(GUARD_SCRIPT, encoding="utf-8")
    (work / "canon.py").write_text(canon("the first wording"), encoding="utf-8")
    repository = Repository(work, git, script)
    install_guards(
        [GitGuard(command=f"{sys.executable} {script}")],
        work,
    )
    repository.regenerate()
    repository.commit("a tree whose copy is current")
    return repository


def test_a_comment_only_edit_to_the_source_refuses_the_commit(
    repository: Repository,
) -> None:
    """The case that produced the stale commits: canon reworded, copy untouched."""
    (repository.work / "canon.py").write_text(
        canon("the same rule, reworded"), encoding="utf-8"
    )

    with pytest.raises(sh.ErrorReturnCode) as refusal:
        repository.commit("a reworded comment, without its regenerated copy")

    assert repository.commits() == 1
    assert REGENERATE_COMMAND in refusal.value.stderr.decode()


def test_the_same_edit_commits_once_the_copy_is_regenerated(
    repository: Repository,
) -> None:
    """The refusal is one command away from being settled, and it stays settled."""
    (repository.work / "canon.py").write_text(
        canon("the same rule, reworded"), encoding="utf-8"
    )
    repository.regenerate()

    repository.commit("a reworded comment, with its regenerated copy")

    assert repository.commits() == 2


def test_a_hand_edited_copy_refuses_the_commit(repository: Repository) -> None:
    """Drift is read over the artifact too, not only over the source."""
    (repository.work / "generated" / "kernel.py").write_text(
        canon("edited where it is generated, not where it is written"),
        encoding="utf-8",
    )

    with pytest.raises(sh.ErrorReturnCode):
        repository.commit("a hand-edited generated file")

    assert repository.commits() == 1


def test_the_guard_leaves_a_hook_it_did_not_write_alone(tmp_path: Path) -> None:
    """A repository with its own pre-commit hook is told, not overwritten."""
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    git("config", "core.hooksPath", str(hooks))
    foreign = hooks / "pre-commit"
    foreign.write_text("#!/bin/sh\nexec ./scripts/mine.sh\n", encoding="utf-8")

    with pytest.raises(GuardConflict):
        install_guards([GitGuard()], work)

    assert read_guards([GitGuard()], work)[0].status == "foreign"
    assert uninstall_guards([GitGuard()], work)[0].status == "foreign"
    assert foreign.is_file()


def test_reinstalling_refreshes_a_body_left_by_an_older_library(
    tmp_path: Path,
) -> None:
    """An armed clone that upgrades is re-armed by the same idempotent command."""
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    git("config", "core.hooksPath", str(hooks))
    guard = GitGuard()
    install_guards([GitGuard(command="an older check")], work)

    assert read_guards([guard], work)[0].status == "stale"
    assert install_guards([guard], work)[0].armed


def test_a_hooks_directory_that_is_gone_reads_as_unreachable(tmp_path: Path) -> None:
    """The condition that silences every guard at once and reports nothing itself.

    A ``core.hooksPath`` outliving the directory it names leaves git running
    no hook and raising no error, so both guards stop firing while every
    other row on the gate goes on passing. It reached this repository: the
    shared config had been left pointing at a fixture's own directory, and
    the guards were off for as long as it took somebody to notice a `/tmp`
    path in the output of an unrelated command.
    """
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "hooks")
    git("config", "core.hooksPath", str(tmp_path / "gone"))

    reading = read_hooks(DECLARED_GUARDS, work)

    assert reading.directory == tmp_path / "gone"
    assert not reading.reachable
    assert len(reading.unarmed()) == len(DECLARED_GUARDS)


def test_an_armed_checkout_reads_as_reachable_with_nothing_unarmed(
    tmp_path: Path,
) -> None:
    """The healthy reading, so the row cannot pass by being unable to fail."""
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    git("config", "core.hooksPath", str(hooks))
    install_guards(DECLARED_GUARDS, work)

    reading = read_hooks(DECLARED_GUARDS, work)

    assert reading.reachable
    assert reading.unarmed() == []


def test_the_installed_hook_runs_the_drift_check(tmp_path: Path) -> None:
    """The body these tests arm a miniature with is the drift check by default."""
    work = tmp_path / "repo"
    initialized_repo(work, tmp_path / "hooks")

    state = install_guards([GitGuard()], work)[0]

    assert state.path == work / ".git" / "hooks" / "pre-commit"
    assert state.path.read_text(encoding="utf-8").endswith(f"exec {DRIFT_COMMAND}\n")
    assert state.path.stat().st_mode & 0o111


def test_the_declared_guards_arm_the_commit_and_the_merge_moments_only(
    tmp_path: Path,
) -> None:
    """The drift check at the commit, the settle where a merge commit is made.

    What earns a hook is the ratio. Reading the generated artifacts back
    costs about a second and catches a staleness that would otherwise be
    written into history, so it is charged to every commit. Regenerating over
    a merge commit is charged only where one is made, and saves the
    regenerate-and-commit every such merge otherwise needed. The whole gate
    costs two minutes and refuses the same work in CI, where a runner pays
    for it instead of the person who is still working.
    """
    work = tmp_path / "repo"
    initialized_repo(work, tmp_path / "hooks")

    installed = {
        state.path.name: state.path.read_text(encoding="utf-8")
        for state in install_guards(DECLARED_GUARDS, work)
    }

    assert list(installed) == ["pre-commit", "post-merge", "post-commit"]
    assert installed["pre-commit"].endswith(f"exec {DRIFT_COMMAND}\n")


def test_a_hook_armed_under_the_previous_marker_is_still_recognized(
    tmp_path: Path,
) -> None:
    """An upgraded checkout is re-armed, not reported as somebody else's.

    The marker is how an installed hook says it is this command's to rewrite,
    and it carries more than one spelling across the versions that wrote it.
    Reading only the current one turns every clone armed by an earlier
    version into one needing `--force` to touch.
    """
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    git("config", "core.hooksPath", str(hooks))
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-commit").write_text(
        f"#!/bin/sh\n# {LEGACY_GUARD_MARKER}: written earlier.\nexec old\n",
        encoding="utf-8",
    )

    assert read_guards([GitGuard()], work)[0].status == "stale"
    assert install_guards([GitGuard()], work)[0].armed


def test_arming_a_checkout_it_cannot_write_reports_instead_of_failing(
    tmp_path: Path,
) -> None:
    """Worktree creation survives a sandbox that will not let the hook be written.

    The pipeline refuses the same drift on the way in, so a checkout without
    the hook is still a working checkout — failing setup over the second line
    of defence would cost more than it buys.
    """
    outside = tmp_path / "not-a-repository"
    outside.mkdir()

    reported = arm(DECLARED_GUARDS, outside)

    assert len(reported) == len(DECLARED_GUARDS)
    assert all("not installed" in line for line in reported)


def test_the_pipeline_runs_the_command_the_hook_installs() -> None:
    """A contributor who never armed the hook meets the same command in CI."""
    assert DRIFT_COMMAND in WorkflowSpec().document().text()


def test_a_push_that_only_deletes_refs_stands_the_gate_down(tmp_path: Path) -> None:
    """The gate judges a tree, and a deletion uploads none.

    Deleting a merged branch ran the whole suite to decide whether a tree the
    push does not touch is sound. The cost was not only wasted: it was long
    enough to time the delete out midway, leaving the local branch gone and
    origin's copy standing, which is the half-completed state somebody then
    has to recognize and finish by hand.

    Pushed content is still refused, in the same repository and the same
    arming, because that is the half a blanket skip would have thrown away.
    """
    origin = tmp_path / "origin.git"
    sh.Command("git")("init", "--bare", "-b", "main", str(origin))
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    git("config", "core.hooksPath", str(hooks))
    git("remote", "add", "origin", str(origin))
    commit_file(git, work, "file.txt", "one\n", "chore: base")
    git("push", "origin", "main", "main:spent")
    install_guards(
        [GitGuard(command="false", hook="pre-push", standdown=DELETION_STANDDOWN)],
        work,
    )

    with pytest.raises(sh.ErrorReturnCode):
        git("push", "origin", "main:carries-content")

    git("push", "origin", "--delete", "spent")

    remaining = str(git("ls-remote", "--heads", "origin"))
    assert "spent" not in remaining
    assert "carries-content" not in remaining


def test_the_commit_guard_carries_no_standdown(tmp_path: Path) -> None:
    """Only the push moment describes itself on stdin, so only it stands down.

    A standdown on the commit hook would read a stdin git never fills, find
    nothing that looks like uploaded content, and exit clean — disarming the
    drift guard while still reporting as armed, which is the one failure this
    whole module is built to make impossible.
    """
    work = tmp_path / "repo"
    initialized_repo(work, tmp_path / "hooks")

    installed = install_guards([GitGuard()], work)[0].path.read_text(encoding="utf-8")

    assert GitGuard().standdown == ""
    assert "read -r" not in installed
    assert installed.endswith(f"exec {DRIFT_COMMAND}\n")


def test_guards_group_into_the_moments_git_runs_them_at() -> None:
    """A moment is the installed unit, because a guard is not what git names."""
    scripts = hook_scripts(
        [
            GitGuard(),
            GitGuard(command=CHECK_COMMAND, hook="pre-push"),
            GitGuard(command="a second commit check"),
        ]
    )

    assert [script.hook for script in scripts] == ["pre-commit", "pre-push"]
    assert [guard.command for guard in scripts[0].guards] == [
        DRIFT_COMMAND,
        "a second commit check",
    ]
    assert [guard.command for guard in scripts[1].guards] == [CHECK_COMMAND]


def test_a_moment_with_one_guard_is_the_script_it_was_before_moments_shared() -> None:
    """A lone guard shares with nobody, so nothing is framed around its check.

    Worth pinning rather than left to fall out: the hook process becomes the
    check, which is what lets a push guard read git's own stdin without
    anything in between, and it means arming a repository that declares one
    guard per moment rewrites nothing when the library learns to share one.
    """
    body = hook_scripts([GitGuard(command=CHECK_COMMAND, hook="pre-push")])[0].body()

    assert body.endswith(f"exec {CHECK_COMMAND}\n")
    assert "guarded_stdin" not in body
    assert "exit $?" not in body


def test_a_shared_moment_no_guard_reads_captures_no_stdin() -> None:
    """The capture is a `cat`, and one where nothing reads buys the moment nothing."""
    shared = HookScript(
        hook="pre-commit", guards=[GitGuard(), GitGuard(command="a second check")]
    )

    assert not shared.replayed
    assert "guarded_stdin" not in shared.body()


def test_both_guards_at_one_moment_run_in_the_order_declared(tmp_path: Path) -> None:
    """Git runs one script per moment, so a moment guarded twice is one file.

    Declaration order is running order, which is what lets a repository put
    its nearly-free refusal in front of the one that boots an interpreter.
    """
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    git("config", "core.hooksPath", str(hooks))
    ran = tmp_path / "ran"
    install_guards(
        [
            GitGuard(command=f"sh -c 'echo first >>{ran}'"),
            GitGuard(command=f"sh -c 'echo second >>{ran}'"),
        ],
        work,
    )

    commit_file(git, work, "file.txt", "one\n", "chore: base")

    assert ran.read_text(encoding="utf-8").split() == ["first", "second"]


def test_the_first_guard_to_refuse_ends_the_moment(tmp_path: Path) -> None:
    """A refusal is the answer, so nothing after it runs and nothing is committed."""
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    git("config", "core.hooksPath", str(hooks))
    ran = tmp_path / "ran"
    install_guards(
        [
            GitGuard(command="false"),
            GitGuard(command=f"sh -c 'echo reached >>{ran}'"),
        ],
        work,
    )

    with pytest.raises(sh.ErrorReturnCode):
        commit_file(git, work, "file.txt", "one\n", "chore: base")

    assert not ran.exists()


def test_a_standdown_stands_its_own_guard_down_and_not_the_moment(
    tmp_path: Path,
) -> None:
    """The subshell is what makes an `exit 0` mean this guard rather than the hook.

    Without it the first guard's standdown would take the whole moment with
    it, disarming every guard declared after it while the hook still reported
    as armed — the silent-disarm failure this module exists to make
    impossible, arriving through the field that was meant to be safe.
    """
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    git("config", "core.hooksPath", str(hooks))
    install_guards(
        [
            GitGuard(command="false", standdown="exit 0\n"),
            GitGuard(command="false"),
        ],
        work,
    )

    with pytest.raises(sh.ErrorReturnCode):
        commit_file(git, work, "file.txt", "one\n", "chore: base")

    install_guards(
        [
            GitGuard(command="false", standdown="exit 0\n"),
            GitGuard(command="true"),
        ],
        work,
    )
    commit_file(git, work, "file.txt", "one\n", "chore: base")

    assert int(str(git("rev-list", "--count", "HEAD"))) == 1


def test_both_guards_at_a_shared_push_moment_read_the_same_ref_list(
    tmp_path: Path,
) -> None:
    """Git delivers a moment's stdin once, and each guard here reads it whole.

    The reason the capture exists. A push moment carrying a data check and a
    gate has two guards that both parse git's ref list, and whichever ran
    first would drain it — leaving the second judging what looks like a push
    of nothing, which for a standdown reads as a deletion and stands the gate
    down on every push there is.
    """
    origin = tmp_path / "origin.git"
    sh.Command("git")("init", "--bare", "-b", "main", str(origin))
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    git("config", "core.hooksPath", str(hooks))
    git("remote", "add", "origin", str(origin))
    commit_file(git, work, "file.txt", "one\n", "chore: base")
    first = tmp_path / "first"
    second = tmp_path / "second"
    install_guards(
        [
            GitGuard(
                command=f"sh -c 'cat >{first}'", hook="pre-push", reads_stdin=True
            ),
            GitGuard(
                command=f"sh -c 'cat >{second}'", hook="pre-push", reads_stdin=True
            ),
        ],
        work,
    )

    git("push", "origin", "main")

    assert "refs/heads/main" in first.read_text(encoding="utf-8")
    assert second.read_text(encoding="utf-8") == first.read_text(encoding="utf-8")


def test_a_shared_push_moment_still_stands_down_on_a_deletion(tmp_path: Path) -> None:
    """The replay preserves an empty stdin as empty, not as one blank line.

    Command substitution strips trailing newlines, so a capture replayed with
    a bare `printf` hands a guard one blank line where git handed it nothing.
    A blank line parses as no ref update, which is exactly what a deletion
    standdown reads to decide there is nothing to judge — so the bug would
    have shown up as a gate that runs on deletions again, in the one case the
    standdown was written for.
    """
    origin = tmp_path / "origin.git"
    sh.Command("git")("init", "--bare", "-b", "main", str(origin))
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    git("config", "core.hooksPath", str(hooks))
    git("remote", "add", "origin", str(origin))
    commit_file(git, work, "file.txt", "one\n", "chore: base")
    git("push", "origin", "main", "main:spent")
    install_guards(
        [
            GitGuard(command="true", hook="pre-push", reads_stdin=True),
            GitGuard(
                command="false",
                hook="pre-push",
                standdown=DELETION_STANDDOWN,
                reads_stdin=True,
            ),
        ],
        work,
    )

    git("push", "origin", "--delete", "spent")

    with pytest.raises(sh.ErrorReturnCode):
        git("push", "origin", "main:carries-content")
