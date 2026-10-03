"""Behavior tests for a worktree that exists without being ready.

Creation registers the worktree in one git call and makes it usable in
several more, so any failure between them leaves a directory whose existence
says nothing about its state. What is pinned here is that the difference is
observable and acted on: a re-run finishes what was left, and a run that
cannot finish says which steps did not, rather than reporting the success
that sends an agent off diagnosing phantom errors in correct code.
"""

# lup: defer: tests here errored twice, and one in test_devtools_delete_branch
# once, across four full 16-worker runs of the suite on 2026-09-19, each
# passing alone; every fixture builds its own repository under tmp_path, so it
# is not another session's checkout -- measure it under load if it recurs

from pathlib import Path
from unittest.mock import Mock

import pytest
import sh
import typer

from lup.devtools.dev import records, worktree
from lup.devtools.harness.launch import relocation_hint
from tests.unit.repos import commit_file, initialized_repo


def recorded_base(repo: Path, branch: str) -> str:
    """The base lup records for a branch, read where that record lives."""
    return records.recorded_base(branch, repo)


def record_base(repo: Path, branch: str, base: str) -> None:
    """Write a base as creation does, without running creation."""
    records.remember(branch, records.BranchRecord(base=base), repo)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout with an origin, which is what worktrees are cut in.

    The remote is part of the fixture rather than one test's setup because
    what several cases here assert is that nothing was sent to it. A repo
    with no origin cannot tell a run that declined to publish from one that
    had nowhere to publish to.
    """
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    sh.Command("git")("init", "--bare", str(tmp_path / "origin.git"), _tty_out=False)
    git("remote", "add", "origin", str(tmp_path / "origin.git"))
    git("push", "-u", "origin", "main")
    return work


@pytest.fixture
def tree_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The sibling directory worktrees are created in, as `create` resolves it."""
    tree = tmp_path / "tree"
    tree.mkdir()
    monkeypatch.setattr(worktree, "get_tree_dir", lambda: tree)
    return tree


@pytest.fixture
def the_worktree_holds_its_own_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ask about this worktree's environment, not one shared with it.

    `UV_PROJECT_ENVIRONMENT` moves where a sync lands, which is how one
    environment is shared across worktrees and how the session image puts it
    outside the checkout. That is a fact about the machine and stays set for
    the suite -- but it makes :meth:`SyncedEnvironment.satisfied` read a
    path that is not the one these cases wrote, so every case whose subject
    is what a sync did or did not leave behind takes it away.

    Both directions need it, which is what makes it a fixture rather than a
    line in one test. Where the value is absolute it names a directory the
    image already holds, so an environment reads as built when nothing built
    one; where it is relative -- which is what a contained session sets, so
    `uv` keys the environment per project -- it names a
    sibling of the `.venv` these cases create, so an environment that *was*
    built reads as missing and the sync runs again.
    """
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)


def repo_git(work: Path) -> sh.Command:
    return sh.Command("git").bake("-C", str(work), _tty_out=False)


def create(name: str, no_sync: bool = True, no_copy_data: bool = True) -> None:
    """Create a worktree the way the CLI does, without the slow steps."""
    worktree.create(
        name,
        no_sync=no_sync,
        no_copy_data=no_copy_data,
        base_branch=None,
        launcher=relocation_hint,
    )


def interrupted_creation(repo: Path, tree_dir: Path, name: str) -> Path:
    """The exact half-made worktree an interrupted config write leaves.

    Registered by `worktree add`, then abandoned before anything recorded a
    base or built an environment — which is what a config lock, or any other
    failure between the git call and the rest, leaves on disk.
    """
    repo_git(repo)("worktree", "add", str(tree_dir / name), "-b", name)
    return tree_dir / name


def test_dependency_sync_matches_ci_without_inheriting_the_source_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = Mock()
    invocation = Mock()
    command.return_value = invocation
    monkeypatch.setattr(sh, "Command", command)

    worktree.sync_dependencies(tmp_path)

    command.assert_called_once_with("env")
    invocation.assert_called_once_with(
        "-u",
        "VIRTUAL_ENV",
        "uv",
        "sync",
        "--all-extras",
        _cwd=str(tmp_path),
    )


@pytest.mark.usefixtures("tree_dir")
def test_a_finished_worktree_reports_itself_active(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`already active` is kept for the case where it is true."""
    monkeypatch.chdir(repo)
    create("topic")

    with pytest.raises(typer.Exit) as exit_info:
        create("topic")

    assert exit_info.value.exit_code == 0


def test_a_half_made_worktree_is_finished_by_re_running(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The failure a bare success message hides: setup that never ran.

    The base is the observable half — `uv sync` is not run in tests — and it
    is the record an interruption between `worktree add` and the rest of
    setup leaves unwritten.
    """
    path = interrupted_creation(repo, tree_dir, "topic")
    monkeypatch.chdir(repo)
    assert path.exists()

    create("topic")

    assert recorded_base(repo, "topic") == "main"
    assert "setup never finished" in capsys.readouterr().out


def test_a_half_made_worktree_does_not_report_success(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    the_worktree_holds_its_own_environment: None,
) -> None:
    """An unfinishable step exits non-zero rather than claiming the worktree is ready.

    The environment stands in for every step that can fail: `uv sync` is
    stubbed to leave no `.venv`, which is precisely the state that makes
    pyright report errors in code nobody touched.
    """
    interrupted_creation(repo, tree_dir, "topic")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "sync_dependencies", lambda _path: None)

    with pytest.raises(typer.Exit) as exit_info:
        create("topic", no_sync=False)

    assert exit_info.value.exit_code == 1


def test_the_steps_that_did_not_run_are_named(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    the_worktree_holds_its_own_environment: None,
) -> None:
    """Naming the missing step is what costs less than the diagnosis it prevents."""
    interrupted_creation(repo, tree_dir, "topic")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(worktree, "sync_dependencies", lambda _path: None)

    with pytest.raises(typer.Exit):
        create("topic", no_sync=False)

    reported = capsys.readouterr().out
    assert "not ready" in reported
    assert "the synced environment (.venv)" in reported


def test_an_environment_that_was_built_is_not_rebuilt(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    the_worktree_holds_its_own_environment: None,
) -> None:
    """Readiness is read off the worktree, so a finished step is not repeated."""
    (interrupted_creation(repo, tree_dir, "topic") / ".venv").mkdir()
    monkeypatch.chdir(repo)

    def refuse(_worktree_path: Path) -> None:
        raise AssertionError("a synced environment was rebuilt")

    monkeypatch.setattr(worktree, "sync_dependencies", refuse)

    create("topic", no_sync=False)


def test_an_environment_that_only_links_to_a_sibling_is_rebuilt(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    the_worktree_holds_its_own_environment: None,
) -> None:
    """A link to another worktree's environment is not an environment.

    `is_dir()` follows it, so readiness read off the link alone reports a
    worktree that is set up and skips the sync. What syncing through the link
    would then do is the expensive half: `uv` resolves it, finds the sibling's
    environment at the far end, and repoints *that* worktree's editable
    install at this one's source — two checkouts importing one tree, the
    branch under test being whichever synced last, and nothing said.

    So the link is cleared and a real environment built where it stood. What
    it pointed at belongs to another worktree and is left as it was.
    """
    worktree_path = interrupted_creation(repo, tree_dir, "topic")
    sibling = tree_dir / "sibling-environment"
    sibling.mkdir()
    (worktree_path / ".venv").symlink_to(sibling)
    monkeypatch.chdir(repo)

    built: list[Path] = []  # lup: ignore[empty-collection] — sync call record

    def build(path: Path) -> None:
        (path / ".venv").mkdir()
        built.append(path)

    monkeypatch.setattr(worktree, "sync_dependencies", build)

    create("topic", no_sync=False)

    assert built == [worktree_path]
    assert not (worktree_path / ".venv").is_symlink()
    assert sibling.is_dir()


def test_an_opted_out_step_is_not_owed(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    the_worktree_holds_its_own_environment: None,
) -> None:
    """`--no-sync` removes the step, so its absence cannot make a worktree unready."""
    interrupted_creation(repo, tree_dir, "topic")
    monkeypatch.chdir(repo)

    create("topic", no_sync=True)

    assert not (tree_dir / "topic" / ".venv").exists()


def create_with_workspace(name: str, no_sync: bool = False) -> None:
    """Create a worktree declaring one bun workspace at `web`, as the CLI would."""
    worktree.create(
        name,
        no_sync=no_sync,
        no_copy_data=True,
        base_branch=None,
        launcher=relocation_hint,
        workspaces=[Path("web")],
    )


def holding_the_workspace(worktree_path: Path) -> Path:
    """The declared workspace, present in the tree the way a real base holds it.

    A tree cut from a base whose layout lacks the workspace has nothing to
    restore and is left alone, which is a case of its own — so a test about
    restoring one gives the tree the directory rather than asking the step to
    restore a path that is not there.
    """
    (worktree_path / "web").mkdir(parents=True, exist_ok=True)
    return worktree_path


def built_environment(path: Path) -> None:
    """Stand in for `uv sync`, leaving the environment it would have built."""
    (path / ".venv").mkdir()


def test_the_bun_workspace_is_restored_beside_the_environment(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    the_worktree_holds_its_own_environment: None,
) -> None:
    """One flag, two toolchains: a worktree readied with its environment has
    its bun workspace restored too, and the step is read off the worktree the
    way the sync is, so a finished restore is not repeated."""
    worktree_path = holding_the_workspace(interrupted_creation(repo, tree_dir, "topic"))
    monkeypatch.chdir(repo)
    restored: list[Path] = []  # lup: ignore[empty-collection] — restore record

    def restore(workspace: Path) -> bool:
        (workspace / "node_modules").mkdir(parents=True)
        restored.append(workspace)
        return True

    monkeypatch.setattr(worktree, "sync_dependencies", built_environment)
    monkeypatch.setattr(worktree, "restore_dependencies", restore)

    create_with_workspace("topic")

    assert restored == [worktree_path / "web"]
    assert worktree.RestoredWorkspace(
        worktree=worktree_path, workspace=Path("web")
    ).satisfied()


def test_no_sync_skips_the_workspace_restore_too(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    the_worktree_holds_its_own_environment: None,
) -> None:
    interrupted_creation(repo, tree_dir, "topic")
    monkeypatch.chdir(repo)

    def refuse(_workspace: Path) -> bool:
        raise AssertionError("a bun workspace was restored under --no-sync")

    monkeypatch.setattr(worktree, "restore_dependencies", refuse)

    create_with_workspace("topic", no_sync=True)

    assert not (tree_dir / "topic" / "web" / "node_modules").exists()


def test_a_workspace_that_cannot_be_restored_leaves_the_worktree_usable(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    the_worktree_holds_its_own_environment: None,
) -> None:
    """The gate restores what it finds behind, so a restore the registry
    refused is said out loud and the worktree is handed over anyway."""
    holding_the_workspace(interrupted_creation(repo, tree_dir, "topic"))
    monkeypatch.chdir(repo)

    def refuse(workspace: Path) -> bool:
        raise RuntimeError(
            f"`bun install --frozen-lockfile` in {workspace} failed:\n"
            "error: ConnectionRefused downloading tarball js-yaml@4.3.2"
        )

    monkeypatch.setattr(worktree, "sync_dependencies", built_environment)
    monkeypatch.setattr(worktree, "restore_dependencies", refuse)

    create_with_workspace("topic")

    printed = capsys.readouterr().out
    assert "Warning: `bun install --frozen-lockfile`" in printed
    assert "ConnectionRefused" in printed
    assert "Without the restored bun workspace (web)" in printed


def test_the_template_declares_its_bun_workspace_for_restoring() -> None:
    from lup_template.devtools.dev.app import declared

    assert declared().restored_workspaces() == [Path("packages/lup/web")]


def test_a_recorded_base_is_left_as_it_was(
    repo: Path, tree_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finishing a worktree completes what is missing and rewrites nothing."""
    interrupted_creation(repo, tree_dir, "topic")
    record_base(repo, "topic", "deliberate")
    monkeypatch.chdir(repo)

    create("topic")

    assert recorded_base(repo, "topic") == "deliberate"


def test_the_gitignored_extras_are_finished_too(
    repo: Path, tree_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree missing the source tree's local settings is not ready either."""
    path = interrupted_creation(repo, tree_dir, "topic")
    (repo / ".env.local").write_text("SECRET=1\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    create("topic", no_copy_data=False)

    assert (path / ".env.local").read_text(encoding="utf-8") == "SECRET=1\n"


@pytest.mark.usefixtures("tree_dir")
def test_a_new_branch_is_published_nowhere_until_it_carries_work(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch with no commits of its own has nothing origin does not hold.

    Publishing one leaves a ref at the base's tip: noise to anybody reading
    the remote, and an obstacle to the branch it is named after. Rebuilding
    the branch on a different base meets that ref as a non-fast-forward, and
    the force-push that answers is a decision nobody wanted to make about a
    branch that had never been pushed with work on it.

    What the ref claimed to buy was something to be kept level with, and
    level with a copy of the base is a reading with no content. The base
    itself is what a checkout is kept level with, and `RecordedBase` writes
    that down. The tracking relationship arrives with the first push that
    carries something, which `dev pr push` makes and the pre-push guard
    judges.
    """
    monkeypatch.chdir(repo)

    create("topic")

    published = repo_git(repo)("ls-remote", "--heads", "origin", "topic")
    assert str(published).strip() == ""
    tracked = repo_git(repo)("config", "--get", "branch.topic.merge", _ok_code=[0, 1])
    assert str(tracked).strip() == ""


def test_a_checkout_with_no_remote_is_still_handed_over(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A checkout made offline is a checkout to work in.

    Nothing in setup reaches origin, so a repository that has no remote at
    all takes the same path as one that has: every step answers for whether
    the checkout is usable, and none of them for whether a remote was told
    about it.
    """
    repo_git(repo)("remote", "remove", "origin")
    monkeypatch.chdir(repo)

    create("topic")

    assert "not ready" not in capsys.readouterr().out
    assert (tree_dir / "topic").is_dir()


def test_commits_no_remote_holds_are_left_for_the_gated_push(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finishing a worktree does not publish the work found in it.

    The pre-push guard runs the whole gate on any push, and setting a
    checkout up is not where somebody's commits are sent through it. A branch
    that has advanced is `dev pr push`'s to send, with the guard doing what
    it is there for — which is also what gives the branch its upstream.
    """
    interrupted_creation(repo, tree_dir, "topic")
    commit_file(
        repo_git(tree_dir / "topic"),
        tree_dir / "topic",
        "mine.txt",
        "mine",
        "feat: mine",
    )
    monkeypatch.chdir(repo)

    create("topic")

    published = repo_git(repo)("ls-remote", "--heads", "origin", "topic")
    assert str(published).strip() == ""


def test_a_base_nobody_can_name_is_refused_before_the_worktree_exists(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A detached HEAD has no branch to read, so there is nothing to record.

    A fallback that writes nothing and says nothing surfaces the loss much
    later, as a base guessed from a topology that has moved. Refused at
    creation instead, where naming the base is one flag.
    """
    repo_git(repo)("checkout", "--detach")
    monkeypatch.chdir(repo)

    with pytest.raises(typer.Exit) as exit_info:
        create("topic")

    assert exit_info.value.exit_code == 1
    assert "not on a branch" in capsys.readouterr().err
    assert not (tree_dir / "topic").exists()


def test_a_base_nobody_can_name_is_created_anyway_when_asked_deliberately(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--no-record` is the deliberate answer, and it makes the worktree."""
    repo_git(repo)("checkout", "--detach")
    monkeypatch.chdir(repo)

    worktree.create(
        "topic",
        no_sync=True,
        no_copy_data=True,
        base_branch=None,
        launcher=relocation_hint,
        no_record=True,
    )

    assert (tree_dir / "topic").is_dir()
    assert recorded_base(repo, "topic") == ""


def test_a_named_base_is_recorded_from_a_detached_head(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naming the base is the other answer, and it records what was named."""
    repo_git(repo)("checkout", "--detach")
    monkeypatch.chdir(repo)

    worktree.create(
        "topic",
        no_sync=True,
        no_copy_data=True,
        base_branch="main",
        launcher=relocation_hint,
    )

    assert recorded_base(repo, "topic") == "main"


def on_a_feature_branch(repo: Path) -> str:
    """Put the checkout on a branch of its own, and report the tip work lands on."""
    git = repo_git(repo)
    landing = str(git("rev-parse", "main")).strip()
    git("checkout", "-q", "-b", "feature")
    commit_file(git, repo, "feature.txt", "feature\n", "feat: feature")
    return landing


def on_a_reserved_workspace(repo: Path) -> str:
    """Put the checkout on a branch nobody committed to, past which work landed.

    Where a session stands right after cutting its own worktree, and the case
    in which the two candidate bases are one line: the branch carries nothing
    `main` lacks, so `main`'s tip is the later point on it and no intent can
    be stranded by taking it.
    """
    git = repo_git(repo)
    git("checkout", "-q", "-b", "reserved")
    git("checkout", "-q", "main")
    commit_file(git, repo, "landed.txt", "landed\n", "feat: landed")
    landing = str(git("rev-parse", "main")).strip()
    git("checkout", "-q", "reserved")
    return landing


def test_a_branch_with_nothing_of_its_own_is_cut_from_where_work_lands(
    repo: Path, tree_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not from whichever checkout the command was run in, where the two differ
    only in how far along one line they sit.

    A worktree is routinely created from another worktree. While that one
    holds nothing the integration branch lacks, cutting from it would only
    start the new branch further back, and the integration branch's tip is
    the answer both readings give.
    """
    landing = on_a_reserved_workspace(repo)
    monkeypatch.chdir(repo)

    create("topic")

    tip = str(repo_git(repo)("rev-parse", "topic")).strip()
    assert tip == landing
    assert recorded_base(repo, "topic") == "main"


def test_a_checkout_ahead_of_where_work_lands_is_asked_which_base(
    repo: Path,
    tree_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Continuing this work and starting new work arrive as identical arguments.

    Either default strands the other intent — one branch carries commits its
    pull request never asked for, the other lacks the code it was written
    against — so where the two bases give different trees the question is
    asked instead, and asked while the answer is still a flag rather than a
    reset. Nothing is created: advice printed after the branch exists is
    advice nobody can act on without an undo.
    """
    on_a_feature_branch(repo)
    monkeypatch.chdir(repo)

    with pytest.raises(typer.Exit) as exit_info:
        create("topic")

    assert exit_info.value.exit_code == 1
    reported = capsys.readouterr().err
    assert "--base feature" in reported
    assert "--base main" in reported
    assert not (tree_dir / "topic").exists()
    assert str(repo_git(repo)("branch", "--list", "topic")).strip() == ""


def test_a_named_base_is_taken_over_the_integration_branch(
    repo: Path, tree_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag is the whole answer to stacking, so it wins where it is passed."""
    on_a_feature_branch(repo)
    monkeypatch.chdir(repo)

    worktree.create(
        "topic",
        no_sync=True,
        no_copy_data=True,
        base_branch="feature",
        launcher=relocation_hint,
    )

    tip = str(repo_git(repo)("rev-parse", "topic")).strip()
    assert tip == str(repo_git(repo)("rev-parse", "feature")).strip()


def test_the_integration_branch_named_from_a_feature_checkout_is_honoured(
    repo: Path, tree_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of what the refusal offers, so pasting it works.

    A refusal whose advice does not run is worse than the guess it replaced,
    and this is the spelling most callers will paste: start fresh from where
    work lands, said out loud from a checkout that is ahead of it.
    """
    landing = on_a_feature_branch(repo)
    monkeypatch.chdir(repo)

    worktree.create(
        "topic",
        no_sync=True,
        no_copy_data=True,
        base_branch="main",
        launcher=relocation_hint,
    )

    assert str(repo_git(repo)("rev-parse", "topic")).strip() == landing
    assert recorded_base(repo, "topic") == "main"


def test_re_attaching_leaves_a_branch_where_it_stands(
    repo: Path, tree_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A default base applies to a branch being cut, and there is nothing to cut.

    `worktree add <path> <branch>` takes a branch where it already is, so a
    base could only reach it by moving it — and whatever sits on it would go.
    """
    on_a_feature_branch(repo)
    tip = str(repo_git(repo)("rev-parse", "feature")).strip()
    repo_git(repo)("checkout", "-q", "main")
    monkeypatch.chdir(repo)

    create("feature")

    assert str(repo_git(repo)("rev-parse", "feature")).strip() == tip


def test_a_branch_rebuilt_on_another_base_still_pushes_forward(
    repo: Path, tree_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The collision a published placeholder would guarantee, and its whole cost.

    Discovering that the code being changed lives on another branch, and
    resetting the new branch onto it, is an ordinary correction. Were
    creation to publish the first base's tip, that correction would make
    every later push a non-fast-forward — on a branch nobody ever pushed work
    from, so the force-push answering it would be a decision about nothing.
    """
    git = repo_git(repo)
    git("checkout", "-q", "-b", "other")
    commit_file(git, repo, "other.txt", "other\n", "feat: other")
    git("checkout", "-q", "main")
    commit_file(git, repo, "main.txt", "main\n", "feat: main")
    git("push", "-q", "origin", "main", "other")
    monkeypatch.chdir(repo)

    create("topic")

    rebuilt = repo_git(tree_dir / "topic")
    rebuilt("reset", "-q", "--hard", "other")
    # Past the guards creation armed, as the push is: the commit is setting
    # the scene, and the drift check it would run is a harness composed twice
    # in a subprocess, five seconds of a test about where the push lands.
    commit_file(
        rebuilt.bake("-c", f"core.hooksPath={repo.parent / 'no-hooks'}"),
        tree_dir / "topic",
        "mine.txt",
        "mine\n",
        "feat: mine",
    )

    rebuilt("push", "--no-verify", "-u", "origin", "topic")
