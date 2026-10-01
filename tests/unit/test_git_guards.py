"""What a commit does when a generated artifact is behind its source.

The guard is a refusal, and a refusal is only worth what an attempt proves, so
these tests write a real repository, arm it, and run `git commit` — the whole
point being that reading the installed configuration would have said the guard
was fine on the day it let two stale commits through. The commit runs the hook
`install` writes, which hands the moment to the checkout's own devtools; a
double stands in for those, running the library's own runner over the guards
a test declares, or standing for a revision older than that runner.

The miniature repository copies one source verbatim into one generated file,
which is the shape of the case that actually failed: the policy kernel modules
are copied into both plugin trees byte for byte, so rewording a comment in one
of them makes both trees stale without changing anything either does. The hook
runs the production drift verdict over that copy rather than a stand-in for it.
"""

import io
import sys
from pathlib import Path

import pytest
import sh

from lup.devtools.dev.git_guards import (
    CHECK_COMMAND,
    DECLARED_GUARDS,
    DRIFT_COMMAND,
    GUARD_MARKER,
    DeletionOnly,
    GitGuard,
    GuardConflict,
    HookMoment,
    HookScript,
    Standdown,
    arm,
    fire,
    hook_scripts,
    install_guards,
    read_guards,
    read_hooks,
    uninstall_guards,
)
from lup.devtools.dev.workflow import WorkflowSpec
from lup.formats.banner import REGENERATE_COMMAND
from tests.unit.repos import commit_file, devtools_double, git_in, initialized_repo

RUN = "uv run lup-devtools git hooks run"
"""What every installed hook hands its moment to, whatever revision wrote it."""

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


def armed_repository(
    tmp_path: Path, guards: list[GitGuard] | None, installed: list[GitGuard]
) -> tuple[Path, sh.Command]:
    """A repository whose hooks are installed, committing through a devtools double.

    ``installed`` is the declaration the hooks were installed from, which
    decides only the moments they sit at; ``guards`` is what the checkout's
    own devtools runs there, or None for a revision older than the runner.
    """
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    # `initialized_repo` passes the hooks directory per-invocation; recording it
    # is what lets the install find the directory git will actually read.
    initialized_repo(work, hooks)("config", "core.hooksPath", str(hooks))
    install_guards(installed, work)
    environment = devtools_double(tmp_path / "devtools", guards)
    return work, git_in(work, hooks).bake(_env=environment)


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    guards = [GitGuard(command=f"{sys.executable} {tmp_path / 'repo' / 'guard.py'}")]
    work, git = armed_repository(tmp_path, guards, guards)
    script = work / "guard.py"
    script.write_text(GUARD_SCRIPT, encoding="utf-8")
    (work / "canon.py").write_text(canon("the first wording"), encoding="utf-8")
    repository = Repository(work, git, script)
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
    """An armed clone that upgrades is re-armed by the same idempotent command.

    The older body is one that spelled its guard out, as every hook did
    before they handed the moment to the checkout.
    """
    work = tmp_path / "repo"
    hooks = tmp_path / "hooks"
    git = initialized_repo(work, hooks)
    git("config", "core.hooksPath", str(hooks))
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-commit").write_text(
        f"#!/bin/sh\n# {GUARD_MARKER}: written earlier.\nexec {DRIFT_COMMAND}\n",
        encoding="utf-8",
    )
    guard = GitGuard()

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
    assert len(reading.unarmed()) == len({guard.hook for guard in DECLARED_GUARDS})


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


def test_install_writes_the_trampoline_and_no_guard(tmp_path: Path) -> None:
    """The file git runs names no guard, so any revision can answer it.

    Every worktree of a clone runs the one hooks directory, each at its own
    revision. A body spelling its guards out failed every commit in a
    worktree whose devtools predated an option one of them used, which is
    the skew this shape removes: what is written is the hand-off alone.
    """
    work = tmp_path / "repo"
    initialized_repo(work, tmp_path / "hooks")

    states = install_guards(DECLARED_GUARDS, work)

    assert [state.path.parent for state in states] == [work / ".git" / "hooks"] * 3
    for state in states:
        body = state.path.read_text(encoding="utf-8")
        assert f'{RUN} {state.path.name} -- "$@"\n' in body
        assert not [guard for guard in DECLARED_GUARDS if guard.command in body]
        assert state.path.stat().st_mode & 0o111


def test_every_revision_writes_the_same_hook(tmp_path: Path) -> None:
    """A clone armed from one declaration is armed for any other at its moments.

    Which is the property that lets the shared directory be written once: a
    checkout whose guards have since grown, or shrunk, reads the hook it
    finds as current rather than asking for a reinstall.
    """
    work = tmp_path / "repo"
    initialized_repo(work, tmp_path / "hooks")
    install_guards([GitGuard(command="the check an older revision ran")], work)

    later = [GitGuard(), GitGuard(command=f"{CHECK_COMMAND} --a-later-option")]

    assert read_guards(later, work)[0].armed


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

    installed = [state.path.name for state in install_guards(DECLARED_GUARDS, work)]

    assert installed == ["pre-commit", "post-merge", "post-commit"]
    # Drift, then what a merge left behind, which no merge stands down.
    assert [
        guard.command for guard in DECLARED_GUARDS if guard.hook == "pre-commit"
    ] == [
        DRIFT_COMMAND,
        f"{CHECK_COMMAND} --conflict-markers --staged",
    ]


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

    assert len(reported) == len({guard.hook for guard in DECLARED_GUARDS})
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
    guards = [GitGuard(command="false", hook="pre-push", standdown=DeletionOnly())]
    install_guards(guards, work)
    git = git.bake(_env=devtools_double(tmp_path / "devtools", guards))

    with pytest.raises(sh.ErrorReturnCode):
        git("push", "origin", "main:carries-content")

    git("push", "origin", "--delete", "spent")

    remaining = str(git("ls-remote", "--heads", "origin"))
    assert "spent" not in remaining
    assert "carries-content" not in remaining


def test_the_commit_guard_carries_no_standdown() -> None:
    """Only the push moment describes itself on stdin, so only it stands down.

    A standdown on the commit hook would read a stdin git never fills, find
    nothing that looks like uploaded content, and pass clean — disarming the
    drift guard while still reporting as armed, which is the one failure this
    whole module is built to make impossible.
    """
    assert GitGuard().standdown is None
    assert not HookScript(hook="pre-commit", guards=[GitGuard()]).captures_stdin()


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


def test_a_passing_guard_lets_the_commit_through_the_hook(tmp_path: Path) -> None:
    """The whole path a commit takes: git, the installed hook, the checkout's runner."""
    guards = [GitGuard(command="true")]
    work, git = armed_repository(tmp_path, guards, guards)

    commit_file(git, work, "file.txt", "one\n", "chore: base")

    assert int(str(git("rev-list", "--count", "HEAD"))) == 1


def test_a_refusing_guard_stops_the_commit_and_says_why(tmp_path: Path) -> None:
    """The refusal reaches whoever committed, naming the check and its answer."""
    guards = [GitGuard(command="false", refusal="Settle it the declared way.")]
    work, git = armed_repository(tmp_path, guards, guards)

    with pytest.raises(sh.ErrorReturnCode) as refusal:
        commit_file(git, work, "file.txt", "one\n", "chore: base")

    said = refusal.value.stderr.decode()
    assert "pre-commit refused by `false` (exit 1)" in said
    assert "Settle it the declared way." in said


def test_a_devtools_guard_runs_inside_the_runner_the_hook_started(
    tmp_path: Path,
) -> None:
    """One devtools process per moment: the guard is a call into it, not another.

    A devtools command a guard names runs inside the runner the trampoline
    started, rather than as a process of its own loading the whole
    application again, so a commit pays for one start however many guards
    its moment declares.
    """
    guards = [GitGuard(command="uv run lup-devtools refuse 4", refusal="Settle it.")]
    work, git = armed_repository(tmp_path, guards, guards)

    with pytest.raises(sh.ErrorReturnCode) as refusal:
        commit_file(git, work, "file.txt", "one\n", "chore: base")

    said = refusal.value.stderr.decode()
    assert "refused in process with 4" in said
    assert "pre-commit refused by `uv run lup-devtools refuse 4` (exit 4)" in said
    started = (tmp_path / "devtools" / "started").read_text(encoding="utf-8")
    assert started.splitlines() == ["git hooks run pre-commit --"]


def test_a_checkout_older_than_the_runner_commits_and_says_so(tmp_path: Path) -> None:
    """The one skew the hook tolerates: devtools that run, but predate the verb.

    Such a checkout declares no guard the hook could reach, so refusing its
    commits would protect nothing and leave skipping hooks as the only way
    on. It commits, and each moment says so on one line naming the checkout,
    after the usage error that checkout's own devtools printed: that output
    is streamed as it comes, since holding it back would hold back every
    guard's progress too.
    """
    work, git = armed_repository(tmp_path, None, DECLARED_GUARDS)

    committed = git("commit", "--allow-empty", "-m", "base", _return_cmd=True)

    said = committed.stderr.decode()
    assert "No such command 'run'" in said
    assert [line for line in said.splitlines() if line.startswith(GUARD_MARKER)] == [
        f"{GUARD_MARKER}: {work} predates `{RUN}`, so it ran no "
        f"{moment} guard; merging its base arms them."
        for moment in ("pre-commit", "post-commit")
    ]
    assert int(str(git("rev-list", "--count", "HEAD"))) == 1


def test_a_guard_refusing_with_a_usage_code_still_refuses(tmp_path: Path) -> None:
    """Exit 2 is also a usage error, and the verb being there is what tells them apart."""
    guards = [GitGuard(command="exit 2")]
    work, git = armed_repository(tmp_path, guards, guards)

    with pytest.raises(sh.ErrorReturnCode) as refusal:
        commit_file(git, work, "file.txt", "one\n", "chore: base")

    said = refusal.value.stderr.decode()
    assert "pre-commit refused by `exit 2` (exit 2)" in said
    assert "predates" not in said


def test_a_checkout_whose_devtools_cannot_run_still_refuses(tmp_path: Path) -> None:
    """`uv` reports its own failure as 2 too, and that is no older revision.

    A broken environment is where the guards are least known to hold, so it
    refuses rather than being read as a checkout that has none to run.
    """
    work, git = armed_repository(tmp_path, [GitGuard(command="true")], [GitGuard()])
    (tmp_path / "devtools" / "uv").write_text(
        "#!/bin/sh\necho 'error: the environment could not be synced' >&2\nexit 2\n",
        encoding="utf-8",
    )

    with pytest.raises(sh.ErrorReturnCode):
        commit_file(git, work, "file.txt", "one\n", "chore: base")

    assert not git("rev-parse", "-q", "--verify", "HEAD", _ok_code=[0, 1]).strip()


def test_a_moment_nothing_reads_takes_no_stdin() -> None:
    """Read only where a guard declares it, so a moment fired by hand does not wait."""

    class Unread(io.StringIO):
        def read(self, size: int | None = -1) -> str:
            raise AssertionError("a moment nothing reads read its stdin")

    shared = HookScript(
        hook="pre-commit", guards=[GitGuard(), GitGuard(command="a second check")]
    )

    assert not shared.captures_stdin()
    assert shared.fired((), Path(), Unread()).stdin is None


def shell_only(arguments: tuple[str, ...]) -> int:
    """The devtools runner of a moment whose every guard is a shell line."""
    raise AssertionError(f"a shell line reached the devtools runner: {arguments}")


def fired(
    guards: list[GitGuard],
    hook: str,
    root: Path,
    stdin: str = "",
    arguments: tuple[str, ...] = (),
) -> int:
    """One moment fired as the checkout's runner fires it, every guard a shell line."""
    return fire(guards, hook, arguments, root, io.StringIO(stdin), shell_only)


def test_both_guards_at_one_moment_run_in_the_order_declared(tmp_path: Path) -> None:
    """Declaration order is running order.

    Which is what lets a repository put its nearly-free refusal in front of
    the one that boots an interpreter.
    """
    ran = tmp_path / "ran"
    guards = [
        GitGuard(command=f"echo first >>{ran}"),
        GitGuard(command=f"echo second >>{ran}"),
    ]

    assert fired(guards, "pre-commit", tmp_path) == 0
    assert ran.read_text(encoding="utf-8").split() == ["first", "second"]


def test_the_first_guard_to_refuse_ends_the_moment(tmp_path: Path) -> None:
    """A refusal is the answer, so nothing after it runs."""
    ran = tmp_path / "ran"
    guards = [GitGuard(command="exit 3"), GitGuard(command=f"echo reached >>{ran}")]

    assert fired(guards, "pre-commit", tmp_path) == 3
    assert not ran.exists()


def test_a_guard_sees_what_git_passed_the_hook(tmp_path: Path) -> None:
    """The positional parameters a line in the hook script itself would have seen."""
    seen = tmp_path / "seen"
    guards = [GitGuard(hook="post-merge", command=f'echo "$0 $1" >{seen}')]

    fired(guards, "post-merge", tmp_path, arguments=("0",))

    assert seen.read_text(encoding="utf-8") == "post-merge 0\n"


class Always(Standdown, frozen=True):
    """A moment that always says it has nothing for its guard to judge."""

    def applies(self, moment: HookMoment) -> bool:
        return True


def test_a_standdown_stands_its_own_guard_down_and_not_the_moment(
    tmp_path: Path,
) -> None:
    """A standdown answers for the guard carrying it and no other.

    Were it to end the moment, the first guard's standdown would disarm
    every guard declared after it while the hook still reported as armed —
    the silent-disarm failure this module exists to make impossible,
    arriving through the field that was meant to be safe.
    """
    stood_down = GitGuard(command="false", standdown=Always())

    assert fired([stood_down, GitGuard(command="false")], "pre-commit", tmp_path) == 1
    assert fired([stood_down, GitGuard(command="true")], "pre-commit", tmp_path) == 0


def test_both_guards_at_a_shared_push_moment_read_the_same_ref_list(
    tmp_path: Path,
) -> None:
    """Git delivers a moment's stdin once, and each guard here reads it whole.

    The reason it is read once. A push moment carrying a data check and a
    gate has two guards that both parse git's ref list, and whichever ran
    first would drain it — leaving the second judging what looks like a push
    of nothing, which for a standdown reads as a deletion and stands the gate
    down on every push there is.
    """
    first = tmp_path / "first"
    second = tmp_path / "second"
    guards = [
        GitGuard(command=f"cat >{first}", hook="pre-push", reads_stdin=True),
        GitGuard(command=f"cat >{second}", hook="pre-push", reads_stdin=True),
    ]
    pushed = f"refs/heads/main {'a' * 40} refs/heads/main {'0' * 40}\n"

    fired(guards, "pre-push", tmp_path, pushed, ("origin", "url"))

    assert first.read_text(encoding="utf-8") == pushed
    assert second.read_text(encoding="utf-8") == pushed


def test_a_guard_that_declares_no_read_is_handed_no_stdin(tmp_path: Path) -> None:
    """What git handed the moment goes to the guards only where one asks for it."""
    seen = tmp_path / "seen"
    guards = [GitGuard(command=f"cat >{seen}", hook="pre-push")]

    fired(guards, "pre-push", tmp_path, "refs/heads/main\n")

    assert seen.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("pushed", "stands_down"),
    [
        pytest.param("", True, id="nothing"),
        pytest.param(
            f"(delete) {'0' * 40} refs/heads/gone {'b' * 40}\n", True, id="deletion"
        ),
        pytest.param(
            f"refs/heads/main {'a' * 40} refs/heads/main {'0' * 40}\n",
            False,
            id="content",
        ),
        pytest.param(
            f"(delete) {'0' * 40} refs/heads/gone {'b' * 40}\n"
            f"refs/heads/main {'a' * 40} refs/heads/main {'0' * 40}\n",
            False,
            id="both",
        ),
        pytest.param("\n", False, id="blank-line"),
        pytest.param("unparsed\n", False, id="unparsed"),
    ],
)
def test_a_deletion_standdown_reads_only_an_all_zero_local_oid_as_nothing(
    pushed: str, stands_down: bool
) -> None:
    """Content is the default, so a line that does not parse is judged.

    Trusting an unreadable line into a standdown would let a push through
    unjudged on the day git's protocol, or a test's double, says something
    this did not expect.
    """
    moment = HookMoment(hook="pre-push", root=Path(), stdin=pushed)

    assert DeletionOnly().applies(moment) is stands_down


def test_a_deletion_standdown_asks_for_the_stdin_itself() -> None:
    """A guard cannot carry it and forget to ask for its input.

    An empty stdin reads as a push of nothing, so a standdown handed none
    would stand its guard down on every push while it still reported armed.
    """
    moment = HookScript(
        hook="pre-push",
        guards=[GitGuard(command="false", hook="pre-push", standdown=DeletionOnly())],
    )

    assert moment.captures_stdin()
