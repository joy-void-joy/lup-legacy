"""Bringing a run's base up to the branch it was cut from.

A run pinned to its creation commit cannot see a fix made to unblock it, so
its workers reason about code that has already been replaced and argue,
carefully and wrongly, for reverting decisions the repository has already
taken. These exercise real Git, because what is being claimed is what Git
does with three shapes of moved branch.
"""

from pathlib import Path

import pytest
import typer

from lup.devtools.harness import resolve
from lup.harness.models import ResolveSpec, SkillInvocation
from lup.execution.process import LaunchRequest, LocalProcessLauncher
from lup.resolver.record import Journal, LeaseRefreshedEvent
from lup.resolver.models import (
    AcceptanceCriterion,
    BaseRefresh,
    Concern,
    ConcernOutcome,
    ConcernProgress,
    ConcernStatus,
    RefreshReport,
    ResolvePhase,
    ResolveState,
    SourceSnapshot,
    WritableRootLease,
)
from lup.resolver.orchestrator import WorktreeOrchestrator
from lup.resolver.rebase import BaseRefresher
from lup.resolver.run import ResolveRun
from lup.resolver.state import ResolverStateRepository


class Repository:
    """One throwaway repository, and the few operations these tests need."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.launcher = LocalProcessLauncher()
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-b", "dev")

    def git(self, *arguments: str) -> str:
        # Identity per invocation rather than written once with `git config`:
        # a misbound command then writes nothing, where a persisted setting
        # lands in the shared config every worktree of a real repository
        # inherits, which is what `lup.devtools.gitguard` reports.
        status = self.launcher.launch(
            LaunchRequest(
                arguments=[
                    "git",
                    "-c",
                    "user.email=resolver@example.test",
                    "-c",
                    "user.name=Resolver Test",
                    *arguments,
                ],
                cwd=self.root,
            )
        )
        if status.code != 0:
            raise AssertionError(f"{arguments}: {status.stderr}")
        return status.stdout.strip()

    def commit(self, name: str, content: str) -> str:
        (self.root / name).write_text(content, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-m", f"write {name}")
        return self.git("rev-parse", "HEAD")

    def snapshot(self, note: str) -> str:
        """An unattached commit carrying an uncommitted file, as intake makes.

        Exactly the shape no branch holds: a real commit on top of HEAD that
        the branch itself never reaches, which is why a refresh cannot be a
        matter of taking the newer tip.
        """
        head = self.git("rev-parse", "HEAD")
        (self.root / "notes.md").write_text(note, encoding="utf-8")
        self.git("add", "notes.md")
        tree = self.git("write-tree")
        self.git("reset", "HEAD", "--", "notes.md")
        return self.git("commit-tree", tree, "-p", head, "-m", "source snapshot")

    def orchestrator(self) -> WorktreeOrchestrator:
        return WorktreeOrchestrator(self.launcher, self.root)

    def content(self, commit: str, path: str) -> str:
        return self.git("show", f"{commit}:{path}")

    def merge_commit(self, first: str, second: str, tree_of: str) -> str:
        """A commit holding both parents, resolved to one side's tree."""
        tree = self.git("rev-parse", f"{tree_of}^{{tree}}")
        return self.git(
            "commit-tree", tree, "-p", first, "-p", second, "-m", "resolved by hand"
        )


def conflicting_base(repository: Repository) -> str:
    """A run base and its branch that both wrote one path, differently."""
    repository.commit("a.py", "one\n")
    base = repository.snapshot("from the run\n")
    repository.commit("notes.md", "from the branch\n")
    return base


def test_a_branch_that_has_not_moved_keeps_the_base_it_had(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "source")
    head = repository.commit("a.py", "one\n")

    refreshed = repository.orchestrator().refreshed_base(
        SourceSnapshot(branch="dev", commit=head)
    )

    assert not refreshed.moved()
    assert refreshed.commit == head


def test_a_branch_that_moved_forward_is_taken_outright(tmp_path: Path) -> None:
    """A fix landing while the run is parked moves the base forward to it."""
    repository = Repository(tmp_path / "source")
    started = repository.commit("a.py", "one\n")
    fixed = repository.commit("a.py", "one, fixed\n")

    refreshed = repository.orchestrator().refreshed_base(
        SourceSnapshot(branch="dev", commit=started)
    )

    assert refreshed.moved()
    assert refreshed.commit == fixed
    assert refreshed.was == started


def test_an_intake_snapshot_is_merged_rather_than_replaced(tmp_path: Path) -> None:
    """A run planned from uncommitted notes must not lose them to a refresh."""
    repository = Repository(tmp_path / "source")
    repository.commit("a.py", "one\n")
    snapshot = repository.snapshot("# lup: fix this\n")
    repository.commit("b.py", "the upstream fix\n")

    refreshed = repository.orchestrator().refreshed_base(
        SourceSnapshot(branch="dev", commit=snapshot)
    )

    assert refreshed.moved()
    assert refreshed.conflicts == []
    assert repository.content(refreshed.commit, "notes.md") == "# lup: fix this"
    assert repository.content(refreshed.commit, "b.py") == "the upstream fix"


def test_a_refresh_that_would_conflict_names_the_paths_and_stays_put(
    tmp_path: Path,
) -> None:
    """What would be lost is reported; the leases beside it keep their base."""
    repository = Repository(tmp_path / "source")
    repository.commit("a.py", "one\n")
    snapshot = repository.snapshot("this run's notes\n")
    (repository.root / "notes.md").write_text("a rival edit\n", encoding="utf-8")
    repository.git("add", "notes.md")
    repository.git("commit", "-m", "upstream writes the same file")

    refreshed = repository.orchestrator().refreshed_base(
        SourceSnapshot(branch="dev", commit=snapshot)
    )

    assert not refreshed.moved()
    assert [path.as_posix() for path in refreshed.conflicts] == ["notes.md"]
    assert "conflicts" in refreshed.reason


def test_a_branch_the_repository_does_not_have_says_so(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "source")
    head = repository.commit("a.py", "one\n")

    refreshed = repository.orchestrator().refreshed_base(
        SourceSnapshot(branch="detached", commit=head)
    )

    assert not refreshed.moved()
    assert "no branch named 'detached'" in refreshed.reason


def test_a_lease_holding_work_is_told_what_a_refresh_would_conflict_on(
    tmp_path: Path,
) -> None:
    """The concerns most likely to conflict edit the files the fix touched."""
    repository = Repository(tmp_path / "source")
    started = repository.commit("a.py", "one\n")
    worktrees = repository.orchestrator()
    lease = WritableRootLease(
        concern_id="alpha", root=tmp_path / "leases" / "alpha", branch="resolve/alpha"
    )
    worktrees.create(lease, started)
    (lease.root / "a.py").write_text("the worker's rewrite\n", encoding="utf-8")
    repository.launcher.launch(
        LaunchRequest(arguments=["git", "commit", "-am", "worker"], cwd=lease.root)
    )
    upstream = repository.commit("a.py", "the upstream fix\n")

    conflicts = worktrees.predicted_merge(lease, upstream)

    assert [path.as_posix() for path in conflicts] == ["a.py"]
    assert not worktrees.merge_into(lease, upstream, "resolve: refresh")


def test_a_lease_that_touched_nothing_contested_merges_the_moved_base(
    tmp_path: Path,
) -> None:
    repository = Repository(tmp_path / "source")
    started = repository.commit("a.py", "one\n")
    worktrees = repository.orchestrator()
    lease = WritableRootLease(
        concern_id="beta", root=tmp_path / "leases" / "beta", branch="resolve/beta"
    )
    worktrees.create(lease, started)
    (lease.root / "b.py").write_text("the worker's own file\n", encoding="utf-8")
    repository.launcher.launch(
        LaunchRequest(arguments=["git", "add", "-A"], cwd=lease.root)
    )
    repository.launcher.launch(
        LaunchRequest(arguments=["git", "commit", "-m", "worker"], cwd=lease.root)
    )
    upstream = repository.commit("c.py", "the upstream fix\n")

    assert worktrees.predicted_merge(lease, upstream) == []
    assert worktrees.merge_into(lease, upstream, "resolve: refresh")
    assert (lease.root / "c.py").read_text(encoding="utf-8") == "the upstream fix\n"
    assert (lease.root / "b.py").exists()


def refresher(repository: Repository, state: ResolveState) -> BaseRefresher:
    """One refresher over a run whose state is held in memory."""
    journal = Journal(repository.root / ".lup")
    run = ResolveRun(
        ResolverStateRepository(repository.root / ".lup", "run-1"), journal
    )
    run.state = state
    return BaseRefresher(run, repository.orchestrator(), journal)


def run_state(source: SourceSnapshot, leases: list[WritableRootLease]) -> ResolveState:
    concerns = [
        Concern(
            id=lease.concern_id,
            title=lease.concern_id,
            spec=f"Resolve {lease.concern_id}",
            criteria=[AcceptanceCriterion(id="c1", description="done")],
        )
        for lease in leases
    ]
    return ResolveState(
        config_digest="digest",
        run_id="run-1",
        phase=ResolvePhase.WORKERS,
        source=source,
        spec=ResolveSpec(
            id="resolve",
            worker_identity="resolver-worker",
            worker_skill=SkillInvocation(plugin="lup", skill="worker"),
            review_skill=SkillInvocation(plugin="lup", skill="review"),
            merge_skill=SkillInvocation(plugin="lup", skill="merge"),
        ),
        concerns=concerns,
        progress=[
            ConcernProgress(concern_id=concern.id, status=ConcernStatus.LEASED)
            for concern in concerns
        ],
        leases=leases,
    )


def test_a_lease_created_after_the_branch_moved_is_cut_from_the_fix(
    tmp_path: Path,
) -> None:
    """A run parked for a fix, then resumed, leases no stale tree."""
    repository = Repository(tmp_path / "source")
    started = repository.commit("a.py", "one\n")
    fixed = repository.commit("a.py", "one, fixed\n")
    state = run_state(SourceSnapshot(branch="dev", commit=started), [])

    refreshed = refresher(repository, state).refreshed(state)

    assert refreshed.root_base().commit == fixed
    assert refreshed.source.commit == started


def test_a_refresh_reports_every_live_lease_before_it_touches_one(
    tmp_path: Path,
) -> None:
    repository = Repository(tmp_path / "source")
    started = repository.commit("a.py", "one\n")
    worktrees = repository.orchestrator()
    contested = WritableRootLease(
        concern_id="alpha", root=tmp_path / "leases" / "alpha", branch="resolve/alpha"
    )
    clean = WritableRootLease(
        concern_id="beta", root=tmp_path / "leases" / "beta", branch="resolve/beta"
    )
    for lease, name in ((contested, "a.py"), (clean, "b.py")):
        worktrees.create(lease, started)
        (lease.root / name).write_text("the worker's work\n", encoding="utf-8")
        repository.launcher.launch(
            LaunchRequest(arguments=["git", "add", "-A"], cwd=lease.root)
        )
        repository.launcher.launch(
            LaunchRequest(arguments=["git", "commit", "-m", "worker"], cwd=lease.root)
        )
    repository.commit("a.py", "the upstream fix\n")
    state = run_state(SourceSnapshot(branch="dev", commit=started), [contested, clean])

    report = refresher(repository, state).report(state)

    assert report.base.moved()
    assert not report.applied
    named = {lease.concern_id: lease for lease in report.leases}
    assert [path.as_posix() for path in named["alpha"].conflicts] == ["a.py"]
    assert named["beta"].conflicts == []
    assert not named["beta"].applied
    assert (contested.root / "a.py").read_text(
        encoding="utf-8"
    ) == "the worker's work\n"


def test_applying_a_refresh_merges_only_what_would_not_conflict(
    tmp_path: Path,
) -> None:
    repository = Repository(tmp_path / "source")
    started = repository.commit("a.py", "one\n")
    worktrees = repository.orchestrator()
    contested = WritableRootLease(
        concern_id="alpha", root=tmp_path / "leases" / "alpha", branch="resolve/alpha"
    )
    clean = WritableRootLease(
        concern_id="beta", root=tmp_path / "leases" / "beta", branch="resolve/beta"
    )
    for lease, name in ((contested, "a.py"), (clean, "b.py")):
        worktrees.create(lease, started)
        (lease.root / name).write_text("the worker's work\n", encoding="utf-8")
        repository.launcher.launch(
            LaunchRequest(arguments=["git", "add", "-A"], cwd=lease.root)
        )
        repository.launcher.launch(
            LaunchRequest(arguments=["git", "commit", "-m", "worker"], cwd=lease.root)
        )
    repository.commit("a.py", "the upstream fix\n")
    state = run_state(SourceSnapshot(branch="dev", commit=started), [contested, clean])

    report = refresher(repository, state).report(state, apply=True)

    named = {lease.concern_id: lease for lease in report.leases}
    assert named["beta"].applied
    assert not named["alpha"].applied
    assert (clean.root / "a.py").read_text(encoding="utf-8") == "the upstream fix\n"
    assert (clean.root / "b.py").exists()
    assert (contested.root / "a.py").read_text(
        encoding="utf-8"
    ) == "the worker's work\n"


def test_a_lease_holding_uncommitted_work_is_refused_rather_than_merged_over(
    tmp_path: Path,
) -> None:
    """The prediction reads commits; the merge it clears runs in the tree.

    A lease a resume has to refresh can hold uncommitted files, and
    `merge-tree` sees none of them, so it would clear every such lease.
    """
    repository = Repository(tmp_path / "source")
    started = repository.commit("a.py", "one\n")
    worktrees = repository.orchestrator()
    lease = WritableRootLease(
        concern_id="alpha", root=tmp_path / "leases" / "alpha", branch="resolve/alpha"
    )
    worktrees.create(lease, started)
    (lease.root / "b.py").write_text("work still in flight\n", encoding="utf-8")
    repository.commit("c.py", "the upstream fix\n")
    state = run_state(SourceSnapshot(branch="dev", commit=started), [lease])

    report = refresher(repository, state).report(state, apply=True)

    refreshed = report.leases[0]
    assert not refreshed.applied
    assert [path.as_posix() for path in refreshed.uncommitted] == ["b.py"]
    assert "b.py" in refreshed.reason
    assert not (lease.root / "c.py").exists()
    assert (lease.root / "b.py").read_text(encoding="utf-8") == "work still in flight\n"


def test_a_lease_the_refresh_could_not_bring_forward_is_recorded(
    tmp_path: Path,
) -> None:
    """Stdout is not a record: a detached run writes it where nobody looks."""
    repository = Repository(tmp_path / "source")
    started = repository.commit("a.py", "one\n")
    worktrees = repository.orchestrator()
    lease = WritableRootLease(
        concern_id="alpha", root=tmp_path / "leases" / "alpha", branch="resolve/alpha"
    )
    worktrees.create(lease, started)
    (lease.root / "b.py").write_text("work still in flight\n", encoding="utf-8")
    repository.commit("c.py", "the upstream fix\n")
    state = run_state(SourceSnapshot(branch="dev", commit=started), [lease])
    refresh = refresher(repository, state)

    refresh.report(state, apply=True)

    recorded = [
        entry.event
        for entry in refresh.journal.read()
        if isinstance(entry.event, LeaseRefreshedEvent)
    ]
    assert len(recorded) == 1
    assert recorded[0].concern_id == "alpha"
    assert not recorded[0].applied
    assert recorded[0].uncommitted == ["b.py"]


def test_the_console_refresh_reports_the_move_and_each_lease(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command an operator actually runs, against a run on disk."""
    repository = Repository(tmp_path / "source")
    started = repository.commit("a.py", "one\n")
    worktrees = repository.orchestrator()
    lease = WritableRootLease(
        concern_id="alpha", root=tmp_path / "leases" / "alpha", branch="resolve/alpha"
    )
    worktrees.create(lease, started)
    (lease.root / "a.py").write_text("the worker's rewrite\n", encoding="utf-8")
    repository.launcher.launch(
        LaunchRequest(arguments=["git", "commit", "-am", "worker"], cwd=lease.root)
    )
    repository.commit("a.py", "the upstream fix\n")
    state_root = repository.root / ".lup" / "resolve"
    ResolverStateRepository(state_root, "run-1").save(
        run_state(SourceSnapshot(branch="dev", commit=started), [lease])
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(resolve, "project_root", lambda: repository.root)
        resolve.refresh_run(run_id="run-1", apply=False, base="")
        reported = capsys.readouterr().out
        with pytest.raises(typer.BadParameter, match="no resolver run"):
            resolve.refresh_run(run_id="ghost", apply=False, base="")

    assert "base would move onto dev" in reported
    assert "alpha: conflicts on a.py" in reported
    assert (lease.root / "a.py").read_text(encoding="utf-8") == "the worker's rewrite\n"


def test_a_verified_concern_is_left_where_its_recorded_commit_says(
    tmp_path: Path,
) -> None:
    """Moving its branch is how a resume refuses a run over its own commit."""
    repository = Repository(tmp_path / "source")
    started = repository.commit("a.py", "one\n")
    worktrees = repository.orchestrator()
    lease = WritableRootLease(
        concern_id="alpha", root=tmp_path / "leases" / "alpha", branch="resolve/alpha"
    )
    worktrees.create(lease, started)
    repository.commit("b.py", "the upstream fix\n")
    state = run_state(SourceSnapshot(branch="dev", commit=started), [lease])
    verified = state.model_copy(
        update={
            "outcomes": [
                ConcernOutcome(
                    concern_id="alpha",
                    branch=lease.branch,
                    commit=started,
                    head=started,
                    verified=True,
                )
            ]
        }
    )

    report = refresher(repository, verified).report(verified, apply=True)

    assert report.leases == []
    assert not (lease.root / "b.py").exists()


def test_a_conflicted_base_names_the_paths_it_could_not_settle() -> None:
    # The refusal says "see the paths named", so it has to name them: a
    # human deciding whether to resolve the base by hand is choosing between
    # one resolution here and the same one repeated in every lease at land.
    report = RefreshReport(
        base=BaseRefresh(
            branch="dev",
            was="a" * 40,
            commit="a" * 40,
            conflicts=[Path("packages/lup/src/lup/devtools/dev/worktree.py")],
            reason="combining these bases conflicts: see the paths named",
        )
    )

    assert resolve.describe_refresh(report) == [
        "base unchanged: combining these bases conflicts: see the paths named",
        "  packages/lup/src/lup/devtools/dev/worktree.py",
        "Merge aaaaaaaaaaaa with dev in a worktree, resolve it there, then "
        "adopt the result: --base <commit> --apply.",
    ]


def test_a_base_already_current_names_nothing() -> None:
    report = RefreshReport(
        base=BaseRefresh(branch="dev", was="a" * 40, commit="a" * 40)
    )

    assert resolve.describe_refresh(report) == ["base is current with dev"]


def test_a_conflicting_combine_is_reported_rather_than_guessed(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "source")
    base = conflicting_base(repository)

    refreshed = repository.orchestrator().refreshed_base(
        SourceSnapshot(branch="dev", commit=base)
    )

    assert not refreshed.moved()
    assert [path.as_posix() for path in refreshed.conflicts] == ["notes.md"]


def test_a_base_resolved_by_hand_is_adopted(tmp_path: Path) -> None:
    # The route out of the deadlock: the combine conflicts, somebody resolves
    # it once where both sides are visible, and the run takes that commit
    # instead of meeting the same conflict again in every lease.
    repository = Repository(tmp_path / "source")
    base = conflicting_base(repository)
    tip = repository.git("rev-parse", "dev")
    resolved = repository.merge_commit(base, tip, tip)

    adopted = repository.orchestrator().adopted_base(
        SourceSnapshot(branch="dev", commit=base), resolved
    )

    assert adopted.commit == resolved
    assert adopted.moved()
    assert adopted.reason == ""


def test_a_base_that_dropped_one_side_is_refused(tmp_path: Path) -> None:
    # The branch tip holds the branch and not the run's base, which is what a
    # resolution that took one side and discarded the other looks like.
    repository = Repository(tmp_path / "source")
    base = conflicting_base(repository)
    tip = repository.git("rev-parse", "dev")

    adopted = repository.orchestrator().adopted_base(
        SourceSnapshot(branch="dev", commit=base), tip
    )

    assert not adopted.moved()
    assert "the run's base" in adopted.reason
    assert "has to hold both sides" in adopted.reason


def test_a_commit_this_repository_does_not_have_is_refused(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "source")
    base = conflicting_base(repository)

    adopted = repository.orchestrator().adopted_base(
        SourceSnapshot(branch="dev", commit=base), "f" * 40
    )

    assert not adopted.moved()
    assert "to adopt" in adopted.reason
