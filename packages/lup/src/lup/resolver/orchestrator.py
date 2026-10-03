"""Concrete lease, worktree, commit, and dependency-base orchestration."""

import shutil
from itertools import takewhile
from pathlib import Path

from lup.harness.codescan.symbols import DefinedSymbol, defined_symbols, symbols_lost
from lup.execution.git import GitError, Repository
from lup.execution.writability import diagnose_git_admin
from lup.harness.ownership import GeneratedArtifacts, generated_artifacts
from lup.execution.process import ExitStatus, LaunchRequest, ProcessLauncher
from lup.resolver.contracts import WorktreePreparer
from lup.resolver.declaration import declaration_delta, inspect_changes
from lup.resolver.notes import clear_concern_notes
from lup.resolver.models import (
    BaseRefresh,
    Concern,
    DependencyBase,
    DiffValidation,
    DropCandidate,
    NoteClearanceCommit,
    SourceSnapshot,
    WorkerReport,
    WorktreeRemoval,
    WritableRootLease,
)


class LeaseViolationError(RuntimeError):
    """A resolver attempted to share or escape a writable root."""


class WritableRootLeases:
    """Own non-overlapping writable roots for one resolver run."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.leases: dict[str, WritableRootLease] = {}

    def acquire(self, concern_id: str, branch: str) -> WritableRootLease:
        lease = self.plan(concern_id, branch)
        self.leases[concern_id] = lease
        return lease

    def plan(self, concern_id: str, branch: str) -> WritableRootLease:
        """Resolve one non-overlapping writable root without taking it.

        A concern admitted mid-run is checked against the roots this run
        already handed out before anything about the run is written, so an
        overlap is refused at the boundary rather than discovered by a
        worker editing another concern's tree.
        """
        if concern_id in self.leases and self.leases[concern_id].active:
            raise LeaseViolationError(f"concern {concern_id!r} already has a lease")
        candidate = (self.root / concern_id).resolve()
        if not candidate.is_relative_to(self.root):
            raise LeaseViolationError(f"lease root escapes resolver root: {candidate}")
        for existing in self.leases.values():
            if not existing.active:
                continue
            other = existing.root.resolve()
            if (
                candidate == other
                or candidate.is_relative_to(other)
                or other.is_relative_to(candidate)
            ):
                raise LeaseViolationError(
                    f"writable roots overlap for {concern_id!r} and "
                    f"{existing.concern_id!r}"
                )
        return WritableRootLease(
            concern_id=concern_id,
            root=candidate,
            branch=branch,
        )

    def adopt(self, leases: list[WritableRootLease]) -> None:
        """Take a run's persisted active leases as this process's authority.

        Every later acquisition is then checked against roots this run
        already handed out, which is what makes a lease acquired long after
        the first batch — for a concern admitted mid-run — refuse an overlap
        instead of quietly sharing a writable root.
        """
        self.leases = {lease.concern_id: lease for lease in leases if lease.active}

    def assert_path(self, concern_id: str, path: Path) -> None:
        try:
            lease = self.leases[concern_id]
        except KeyError as error:
            raise LeaseViolationError(f"unknown lease {concern_id!r}") from error
        candidate = path.resolve()
        if not lease.active or not candidate.is_relative_to(lease.root.resolve()):
            raise LeaseViolationError(
                f"{candidate} is outside the active lease for {concern_id!r}"
            )


class WorktreeOrchestrator:
    """Run only orchestrator-authorized worktree, diff, and commit operations."""

    def __init__(
        self,
        launcher: ProcessLauncher,
        workspace: Path,
        preparer: WorktreePreparer | None = None,
        generated: GeneratedArtifacts | None = None,
    ) -> None:
        self.launcher = launcher
        self.workspace = workspace
        self.preparer = preparer
        self.generated = generated or generated_artifacts(workspace)

    def config_lock_note(self, root: Path) -> str:
        """What a failed step's mount state says that git's words cannot.

        A run leases a worktree per concern, and every lease writes config, so
        a confinement that owns `config.lock` kills the run at its first one
        with `File exists` — the wording of a stale lock, which is the search
        this sends nobody on. Read only after a step has already failed, and
        appended rather than substituted, because git's own refusal stays the
        record of what it refused.
        """
        try:
            located = self.repository(root).admin_dirs()
        except GitError:
            return ""
        diagnosis = diagnose_git_admin(located)
        return f" — {diagnosis}" if diagnosis else ""

    def repository(self, root: Path) -> Repository:
        """The repository a step runs in, asked through this run's launcher."""
        return Repository(root, self.launcher)

    def checked(
        self, status: ExitStatus, command: list[str], root: Path, failure: str
    ) -> ExitStatus:
        """One step's status, or the run refused in the step's own words.

        A sequence reported under one message cannot say which of its steps
        failed, and a bare status code names neither the step nor anything
        to act on. Raising at the step also stops the sequence there, so a
        later step never runs against a tree an earlier one failed to make.
        """
        if status.code != 0:
            raise RuntimeError(
                f"{failure}: `{' '.join(command)}` exited "
                f"{status.code}: {status.stderr.strip()}"
                f"{self.config_lock_note(root)}"
            )
        return status

    def require(self, root: Path, failure: str, *arguments: str) -> ExitStatus:
        """Run one git step in ``root`` whose failure refuses the run."""
        return self.checked(
            self.repository(root).run(*arguments), ["git", *arguments], root, failure
        )

    def create(self, lease: WritableRootLease, base_commit: str) -> None:
        self.require(
            self.workspace,
            f"failed to create worktree for {lease.concern_id}",
            "worktree",
            "add",
            "-b",
            lease.branch,
            str(lease.root),
            base_commit,
        )
        if self.preparer is not None:
            self.preparer.prepare(lease.root)

    def discard_unregistered(self, root: Path) -> None:
        """Clear a leftover checkout directory git no longer counts as one.

        A run killed mid-read leaves its reading worktree on disk. Git's own
        bookkeeping is pruned, but removal keeps untracked files, and a
        session traces into the tree it runs in — so an ignored telemetry
        file survives, the directory survives with it, and `worktree add`
        refuses the path from then on. Every later resume dies there, on a
        directory that is by construction disposable: a detached read-only
        checkout of a commit that still exists.

        Only when git disowns it. A path git still lists is somebody's
        worktree, and removing one out from under a reader is a different
        act entirely.
        """
        if not root.exists():
            return
        try:
            listed = self.repository(self.workspace).worktrees()
        except GitError:
            listed = []
        if any(worktree.path == root for worktree in listed):
            return
        shutil.rmtree(root)

    def read_only_checkout(self, root: Path, commit: str) -> None:
        """A detached worktree at one commit, for actors that only read it.

        Detached and unnamed because nothing here owns a branch: these exist
        so several readers can examine one tree at once without sharing a
        directory. Reviewers are denied Write and Edit, but a criterion is
        often "the gate is green" or "the drift check settles", and running
        those writes caches and re-renders artifacts. Sharing one checkout
        would have concurrent readers regenerating into each other, and into
        the integration tree the audit depends on being clean.
        """
        self.discard_unregistered(root)
        self.require(
            self.workspace,
            f"failed to check out {commit[:12]} for reading",
            "worktree",
            "add",
            "--detach",
            str(root),
            commit,
        )
        if self.preparer is not None:
            self.preparer.prepare(root)

    def discard_checkout(self, root: Path) -> None:
        """Drop a read-only checkout and whatever running the gate left in it.

        Forced because that is the point: a reader's caches and regenerated
        artifacts are exactly what makes it dirty, and none of it is work
        anybody wanted kept.
        """
        self.repository(self.workspace).run("worktree", "remove", "--force", str(root))

    def clear_notes(
        self, lease: WritableRootLease, concern: Concern, base_commit: str
    ) -> NoteClearanceCommit:
        """Strip this concern's notes from its lease as a distinct commit.

        Committing rather than leaving the strip in the working tree is what
        keeps :meth:`validate_and_commit` strict: the worker's base already
        has the notes gone, so its reported paths still equal the inspected
        diff. A concern whose notes were all absent commits nothing and keeps
        the base it was given.
        """
        clearance = clear_concern_notes(lease.root, concern)
        if not clearance.cleared:
            return NoteClearanceCommit(clearance=clearance, commit=base_commit)
        repository = self.repository(lease.root)
        added = repository.run("add", "-A")
        committed = repository.run(
            "commit", "-m", f"resolve: clear review notes for {concern.id}"
        )
        identified = repository.run("rev-parse", "HEAD")
        commit_lines = identified.stdout.splitlines()
        if (
            added.code != 0
            or committed.code != 0
            or identified.code != 0
            or len(commit_lines) != 1
            or not commit_lines[0]
        ):
            raise RuntimeError(f"failed to clear review notes for {concern.id}")
        return NoteClearanceCommit(clearance=clearance, commit=commit_lines[0])

    def settled_round(
        self,
        concern: Concern,
        report: WorkerReport,
        base_commit: str,
        origin_commit: str,
    ) -> DiffValidation:
        """What an unmoved worktree means, given what the branch already holds.

        Three readings, and only one of them is a fault. A worker that
        reported no change and made none is settled. A revision whose right
        answer was to change the report — because the rejection was for
        something no edit inside this lease could reach — has already
        committed its work in an earlier round, and the branch still holds
        it. Only an empty branch behind a claim of changes is a worker that
        did not do what it said.

        Reading the second as the third is what cost `composition-seam-abc`
        its remaining rounds: complete work, rejected as though absent, until
        the budget ran out with its criteria never evaluated.
        """
        if not report.changed:
            return DiffValidation(concern_id=concern.id, valid=True, commit=base_commit)
        if origin_commit and base_commit != origin_commit:
            return DiffValidation(
                concern_id=concern.id,
                valid=True,
                commit=base_commit,
                reason=(
                    "revision changed the report; the work it describes stands "
                    f"at {base_commit[:12]}"
                ),
            )
        return DiffValidation(
            concern_id=concern.id,
            valid=False,
            reason="worker reported changes but diff is empty",
        )

    def validate_and_commit(
        self,
        concern: Concern,
        report: WorkerReport,
        lease: WritableRootLease,
        base_commit: str,
        leases: WritableRootLeases,
        origin_commit: str = "",
    ) -> DiffValidation:
        try:
            self.branch(lease)
        except RuntimeError:
            return DiffValidation(
                concern_id=concern.id,
                valid=False,
                reason="worker changed branch authority",
            )
        # A HEAD that moved is two different events sharing one symptom. One is
        # a worker rewriting the lease's history out from under the base it was
        # given, which invalidates everything measured from it. The other is a
        # base this check carried while the lease's own branch advanced past it
        # — a commit the run made, or one the turn made on top of the base —
        # which changes nothing about what the diff below measures, because it
        # measures from the base and the base is still in the history. Blaming
        # the worker for both failed a concern for the run's own bookkeeping.
        current = self.head(lease)
        if current != base_commit and not self.already_joined(lease, base_commit):
            return DiffValidation(
                concern_id=concern.id,
                valid=False,
                reason=(
                    f"worker changed commit authority: {base_commit} is no longer "
                    "in the lease's history"
                ),
            )
        inspected = inspect_changes(self.launcher, lease.root, base_commit)
        if inspected.failure:
            return DiffValidation(
                concern_id=concern.id, valid=False, reason=inspected.failure
            )
        repository = self.repository(lease.root)
        if repository.run("diff", "--check", base_commit).code != 0:
            return DiffValidation(
                concern_id=concern.id,
                valid=False,
                reason="diff validation failed",
            )
        changed = inspected.paths
        for path in changed:
            if path.is_absolute() or ".." in path.parts:
                return DiffValidation(
                    concern_id=concern.id,
                    valid=False,
                    reason=f"changed path escapes the worktree: {path}",
                )
            leases.assert_path(concern.id, lease.root / path)
        delta = declaration_delta(
            changed, report.files_changed, report.swept_beyond_scope
        )
        if not delta.settled:
            return DiffValidation(
                concern_id=concern.id,
                valid=False,
                reason=delta.reason,
                declaration=True,
            )
        if not changed:
            return self.settled_round(concern, report, base_commit, origin_commit)
        if not report.changed:
            return DiffValidation(
                concern_id=concern.id,
                valid=False,
                reason="worker reported no change but the worktree is modified",
            )
        added = repository.run("add", "-A")
        # The change this diff measured can already be in the lease's history:
        # the base advanced and the working tree is clean. Nothing is left to
        # commit, and `git commit` refuses an empty one, so committing anyway
        # would fail a concern whose work is present and fully accounted for.
        staged = repository.run("diff", "--cached", "--quiet", "HEAD")
        if added.code == 0 and staged.code == 0:
            return DiffValidation(concern_id=concern.id, valid=True, commit=current)
        committed = repository.run("commit", "-m", f"resolve: {concern.title}")
        if added.code != 0 or committed.code != 0:
            return DiffValidation(
                concern_id=concern.id,
                valid=False,
                reason="orchestrator commit failed",
            )
        identified = repository.run("rev-parse", "HEAD")
        commit_lines = identified.stdout.splitlines()
        if identified.code != 0 or len(commit_lines) != 1 or not commit_lines[0]:
            return DiffValidation(
                concern_id=concern.id,
                valid=False,
                reason="created commit identity was not available",
            )
        return DiffValidation(
            concern_id=concern.id,
            valid=True,
            commit=commit_lines[0],
        )

    def resolved(self, revision: str) -> str:
        """One revision's commit, empty where the repository has no such name."""
        return self.repository(self.workspace).resolves(revision) or ""

    def reachable(self, commit: str, candidate: str) -> bool:
        """Whether ``candidate`` is already reachable from ``commit``.

        Named for the two commits it compares rather than for containment,
        because :meth:`contains` asks the same question of a lease's own HEAD
        and one class cannot answer to both under a single name.
        """
        return self.repository(self.workspace).is_ancestor(candidate, commit)

    def behind(self, commit: str, branch: str) -> int:
        """How many commits ``branch`` holds that ``commit`` does not.

        Zero when the branch cannot be read, because this decorates a
        question rather than deciding anything: a run whose remote is
        unreachable still has an assembly to approve, and a count nobody
        could take is not worth refusing over.
        """
        try:
            return self.repository(self.workspace).count(f"{commit}..{branch}")
        except GitError:
            return 0

    def conflicted_names(self, lines: list[str]) -> list[Path]:
        """The paths a ``merge-tree`` refusal named, before its prose.

        Three sections separated by a blank line: the tree it wrote, the
        paths it could not settle, and the messages it writes for a human.
        Reading past the blank line offers `Auto-merging a.py` as a path,
        which is not one.
        """
        return [Path(line) for line in takewhile(bool, lines[1:])]

    def merged_base(self, base: str, onto: str, message: str) -> BaseRefresh:
        """Where a base stands once it holds ``onto`` as well as itself.

        Three shapes, and only the third costs anything. A base that already
        contains the other keeps the commit it had. A base the other
        contains is superseded by it. Two that contain neither — which is
        what an intake snapshot of uncommitted notes always looks like,
        since it is a commit no branch holds — are merged, so the notes this
        run was planned from survive instead of being dropped in favour of
        the newer tree.

        The merge is predicted before it is made. ``git merge-tree`` answers
        both questions at once: it names the paths that would conflict and,
        when none do, writes the tree the merge would produce — so a
        combination that cannot be made cleanly reports what stands in the
        way rather than leaving a half-merged base behind, and no worktree
        is needed for either answer.
        """
        if base == onto or self.reachable(base, onto):
            return BaseRefresh(was=base, commit=base)
        if self.reachable(onto, base):
            return BaseRefresh(was=base, commit=onto)
        merged = self.repository(self.workspace).run(
            "merge-tree", "--write-tree", "--name-only", base, onto
        )
        lines = merged.stdout.splitlines()
        if merged.code != 0 or not lines:
            return BaseRefresh(
                was=base,
                commit=base,
                conflicts=self.conflicted_names(lines),
                reason=(
                    "combining these bases conflicts: "
                    f"{merged.stderr.strip() or 'see the paths named'}"
                ),
            )
        return BaseRefresh(
            was=base, commit=self.commit_tree(lines[0], [base, onto], message)
        )

    def refreshed_base(self, source: SourceSnapshot) -> BaseRefresh:
        """Where a root concern should start now, given where the run began."""
        tip = self.resolved(source.branch)
        if not tip:
            return BaseRefresh(
                branch=source.branch,
                was=source.commit,
                commit=source.commit,
                reason=f"no branch named {source.branch!r} to refresh from",
            )
        combined = self.merged_base(
            source.commit,
            tip,
            f"chore(resolve): refresh base onto {source.branch}",
        )
        return combined.model_copy(update={"branch": source.branch})

    def adopted_base(self, source: SourceSnapshot, commit: str) -> BaseRefresh:
        """Take a base somebody resolved by hand, once it holds both sides.

        `merged_base` predicts the combine and declines to guess at a
        conflict, which leaves the case a refresh exists for with no route
        at all: a fix landed on the branch to unblock this very run touches
        the files that run's notes are about, so conflicting is the ordinary
        outcome rather than the exceptional one. Resolving it belongs where
        conflicts are resolved — a worktree, with both sides visible — and
        this adopts the commit that came out.

        Containment is what is checked, because it is the half of "nothing
        was dropped" a machine can answer: a resolution reachable from
        neither the base nor the branch settled something else entirely.
        Whether every hunk survived is the reviewer's question, and the
        merge guidance is what asks it.
        """
        resolved = self.resolved(commit)
        if not resolved:
            return BaseRefresh(
                branch=source.branch,
                was=source.commit,
                commit=source.commit,
                reason=f"no commit {commit!r} in this repository to adopt",
            )
        tip = self.resolved(source.branch)
        dropped = [
            name
            for name, side in (("the run's base", source.commit), (source.branch, tip))
            if side and not self.reachable(resolved, side)
        ]
        if dropped:
            return BaseRefresh(
                branch=source.branch,
                was=source.commit,
                commit=source.commit,
                reason=(
                    f"{resolved[:12]} does not contain "
                    + " or ".join(dropped)
                    + "; a resolved base has to hold both sides"
                ),
            )
        return BaseRefresh(branch=source.branch, was=source.commit, commit=resolved)

    def commit_tree(self, tree: str, parents: list[str], message: str) -> str:
        """Record one prepared tree as a commit, without touching a worktree."""
        created = self.require(
            self.workspace,
            "failed to record a merged base",
            "commit-tree",
            tree,
            *(word for parent in parents for word in ("-p", parent)),
            "-m",
            message,
        )
        lines = created.stdout.splitlines()
        if len(lines) != 1 or not lines[0]:
            raise RuntimeError(
                f"failed to record a merged base: `git commit-tree` named "
                f"{len(lines)} commits"
            )
        return lines[0]

    def predicted_merge(self, lease: WritableRootLease, commit: str) -> list[Path]:
        """Which paths merging ``commit`` into this lease would leave conflicted.

        Asked before a refresh touches a branch with work in flight, which
        is the case the answer matters for: the concerns most likely to
        conflict with an upstream fix are the ones editing the files it
        touched.
        """
        head = self.head(lease)
        if self.reachable(head, commit):
            return []
        merged = self.repository(lease.root).run(
            "merge-tree", "--write-tree", "--name-only", head, commit
        )
        if merged.code == 0:
            return []
        return self.conflicted_names(merged.stdout.splitlines())

    def uncommitted(self, lease: WritableRootLease) -> list[Path]:
        """Which paths this lease is holding outside any commit.

        ``predicted_merge`` answers from commits, and the merge it clears
        runs in the working tree — so work in flight is exactly what the
        prediction cannot see. Read here instead of inferred from a merge
        that already failed: git refuses only when the incoming commit
        touches the same paths, so the quieter case is a refresh absorbed
        into edits no worker chose to commit beside it.
        """
        status = self.require(
            lease.root,
            f"failed to read uncommitted work for {lease.concern_id}",
            "status",
            "--porcelain",
        )
        return [Path(line[3:]) for line in status.stdout.splitlines() if line[3:]]

    def merge_into(self, lease: WritableRootLease, commit: str, message: str) -> bool:
        """Bring one commit into a lease's branch, reporting whether it took."""
        repository = self.repository(lease.root)
        if repository.run("merge", "--no-ff", "-m", message, commit).code == 0:
            return True
        repository.run("merge", "--abort")
        return False

    def remove(self, lease: WritableRootLease) -> WorktreeRemoval:
        """Free a lease's worktree, reporting what actually stands in the way.

        An exit status cannot tell a dirty worktree from one that is no
        longer there — `git worktree remove` refuses both — so reading the
        refusal as uncommitted work sent a human to directories that did not
        exist and described work that was not being held. What remains on
        disk is observable, so it is observed rather than inferred, and the
        refusal git gave is carried instead of a guess at it. A prune that
        cannot run leaves the registration standing, so it is retained rather
        than reported freed — a lease recorded as cleaned by a step that
        failed is the same mislabel one layer up.
        """
        workspace = self.repository(self.workspace)
        status = workspace.run("worktree", "remove", str(lease.root))
        notes: list[str] = []
        if status.code != 0:
            if lease.root.exists():
                return WorktreeRemoval(
                    freed=False,
                    detail=status.stderr.strip() + self.config_lock_note(lease.root),
                )
            pruned = workspace.run("worktree", "prune")
            if pruned.code != 0:
                return WorktreeRemoval(
                    freed=False,
                    detail=f"prune failed: {pruned.stderr.strip()}"
                    + self.config_lock_note(self.workspace),
                )
            notes.append("worktree was already gone")
        deleted = workspace.run("branch", "-D", lease.branch)
        if deleted.code != 0:
            notes.append(
                f"branch retained: {deleted.stderr.strip()}"
                + self.config_lock_note(self.workspace)
            )
        return WorktreeRemoval(freed=True, detail="; ".join(notes))

    def restore(self, lease: WritableRootLease) -> None:
        """Restore a persisted branch into its persisted writable root."""
        self.require(
            self.workspace,
            f"failed to restore worktree for {lease.concern_id}",
            "worktree",
            "add",
            str(lease.root),
            lease.branch,
        )
        if self.preparer is not None:
            self.preparer.prepare(lease.root)

    def branch_exists(self, lease: WritableRootLease) -> bool:
        """Report whether a persisted resolver branch exists locally."""
        return (
            self.repository(self.workspace).resolves(f"refs/heads/{lease.branch}")
            is not None
        )

    def merging(self, lease: WritableRootLease) -> str | None:
        """The parent of a merge left in progress, or ``None`` when settled."""
        return self.repository(lease.root).merging()

    def contained_in(
        self, lease: WritableRootLease, commit: str, container: str
    ) -> bool:
        """Report whether one commit is already an ancestor of another."""
        return self.repository(lease.root).is_ancestor(commit, container)

    def already_joined(self, lease: WritableRootLease, commit: str) -> bool:
        """Report whether a parent is already contained in the worktree's HEAD."""
        return self.contained_in(lease, commit, "HEAD")

    def branch(self, lease: WritableRootLease) -> str:
        """Read the current branch for an orchestrated worktree."""
        try:
            named = self.repository(lease.root).branch()
        except GitError as error:
            raise RuntimeError(
                f"worktree branch changed for {lease.concern_id}"
            ) from error
        if named != lease.branch:
            raise RuntimeError(f"worktree branch changed for {lease.concern_id}")
        return named

    def head(self, lease: WritableRootLease) -> str:
        """Read the exact current commit identity for an orchestrated worktree."""
        identified = self.require(
            lease.root,
            f"failed to identify worktree {lease.concern_id}",
            "rev-parse",
            "HEAD",
        )
        lines = identified.stdout.splitlines()
        if len(lines) != 1 or not lines[0]:
            raise RuntimeError(
                f"failed to identify worktree {lease.concern_id}: `git rev-parse "
                f"HEAD` named {len(lines)} commits: {identified.stdout.strip()!r}"
            )
        return lines[0]

    def prepare_join(self, lease: WritableRootLease, parent_commits: list[str]) -> bool:
        """Stage a no-commit merge, reporting whether git had to leave a conflict.

        The answer is what decides whether an agent turn is spent at all.
        Accepting exit 0 and exit 1 identically meant a merger was invoked on
        every parent, handed an already-correct tree it could edit, with
        nothing to decide — ten such turns in a twelve-parent run.
        """
        if len(parent_commits) != 2:
            raise ValueError("one semantic join step requires exactly two commits")
        # Preparing is idempotent: a merge already open against this same parent
        # is the one a previous turn was resolving, and re-running `git merge`
        # over it would fail on the existing MERGE_HEAD anyway. Leaving it in
        # place is what lets a resolution survive the park that interrupted it.
        if self.merging(lease) == parent_commits[1]:
            # A merge left open is one a turn was already resolving, so the
            # question it was invoked over stands whatever git reported then.
            return True
        status = self.repository(lease.root).run(
            "merge", "--no-commit", "--no-ff", parent_commits[1]
        )
        if status.code not in {0, 1}:
            raise RuntimeError(
                f"failed to prepare semantic join for {lease.concern_id}: "
                f"`git merge` exited {status.code}: {status.stderr.strip()}"
            )
        return status.code == 1

    def settle_generated(self, lease: WritableRootLease, regenerate: list[str]) -> bool:
        """Take either side of a rendered conflict, then render it again.

        A generated artifact has one correct content and it is whatever the
        generator emits, so asking a merger to choose between two stale
        renderings is asking it to justify a decision that is not its to
        take — the same argument :meth:`drop_candidates` already makes for
        exempting them from what a merger must account for, applied to what
        it must resolve. Every lease that touches a catalog re-renders both
        plugin trees, so these are most of what a join conflicts over: one
        measured join carried 852 changed lines of `policy_data.py`, twice.

        Taking `--ours` is not a judgement either. It settles the index so
        git can proceed, and the render that follows overwrites whichever
        side was taken.
        """
        rendered = [
            path
            for path in self.conflicted_paths(lease)
            if self.generated.owning(path.as_posix()) is not None
        ]
        if not rendered:
            return False
        self.require(
            lease.root,
            f"failed to settle rendered conflicts for {lease.concern_id}",
            "checkout",
            "--ours",
            "--",
            *(str(p) for p in rendered),
        )
        self.checked(
            self.launcher.launch(LaunchRequest(arguments=regenerate, cwd=lease.root)),
            regenerate,
            lease.root,
            f"failed to regenerate artifacts for {lease.concern_id}",
        )
        self.require(
            lease.root,
            f"failed to stage regenerated artifacts for {lease.concern_id}",
            "add",
            "-A",
        )
        return True

    def conflicted_paths(self, lease: WritableRootLease) -> list[Path]:
        """Every path git left unmerged, which git already knows exactly."""
        return self.repository(lease.root).conflicted()

    def current_branch(self, root: Path) -> str:
        """Which branch a worktree has checked out, empty when detached."""
        try:
            return self.repository(root).branch()
        except GitError:
            return ""

    def fast_forward(self, root: Path, source: str) -> bool:
        """Advance a checked-out branch from inside the worktree holding it.

        Every plumbing route that moves the ref from outside corrupts the
        view: ``git branch -f`` refuses outright, while ``git update-ref``
        and ``git push .`` both report success and leave the standing
        worktree showing a staged modification nobody made, because the ref
        moved and the index did not. Merging from inside moves ref, index
        and working tree together.
        """
        return self.repository(root).run("merge", "--ff-only", source).code == 0

    def merge_base(self, lease: WritableRootLease, left: str, right: str) -> str:
        """Where two commits forked, so a contribution can be read from there."""
        found = self.repository(lease.root).run("merge-base", left, right)
        lines = found.stdout.splitlines()
        if found.code != 0 or not lines:
            raise RuntimeError(f"{left} and {right} share no history")
        return lines[0]

    def changed_between(
        self, lease: WritableRootLease, base: str, commit: str
    ) -> list[Path]:
        """Every path that differs between two commits."""
        named = self.repository(lease.root).run("diff", "--name-only", base, commit)
        return [Path(line) for line in named.stdout.splitlines() if line]

    def authored_between(
        self, lease: WritableRootLease, base: str, commit: str
    ) -> list[Path]:
        """Every changed path this repository writes rather than renders."""
        return [
            path
            for path in self.changed_between(lease, base, commit)
            if self.generated.owning(path.as_posix()) is None
        ]

    def added_lines(
        self, lease: WritableRootLease, base: str, parent: str, path: Path
    ) -> list[str]:
        """Every substantive line one parent adds to one path.

        Blank and near-punctuation lines are excluded because they carry no
        identity: a closing brace surviving proves nothing about the hunk
        that added it, and including them would bury a real loss under noise
        the merger then has to account for.
        """
        diffed = self.repository(lease.root).run(
            "diff", "-U0", base, parent, "--", path.as_posix()
        )
        return [
            stripped
            for line in diffed.stdout.splitlines()
            if line.startswith("+") and not line.startswith("+++")
            for stripped in [line[1:].strip()]
            if len(stripped) > 3 and any(character.isalnum() for character in stripped)
        ]

    def file_at(self, lease: WritableRootLease, commit: str, path: Path) -> str:
        """One path's content at one commit, empty where it does not exist."""
        shown = self.repository(lease.root).run("show", f"{commit}:{path.as_posix()}")
        return shown.stdout if shown.code == 0 else ""

    def drop_candidates(
        self, lease: WritableRootLease, base: str, parent: str, result: str
    ) -> list[DropCandidate]:
        """What this parent contributed that the joined tree no longer holds.

        Line presence rather than hunk identity, so a resolution that merely
        moved code within its file still reads as kept. A line that was
        genuinely rewritten does read as missing — which is the point, since
        a rewrite is exactly the choice the merger has to declare.

        A generated artifact is exempt, because the question this asks does
        not apply to it. Every lease that touches the catalog re-renders both
        plugin trees, so each parent "contributes" lines to files the joined
        tree derives rather than holds, and the merger is asked to justify
        keeping or dropping content whose only correct value is whatever the
        generator emits next. The source those artifacts are rendered from is
        adjudicated normally, which is where the decision actually lives.
        """
        found: list[DropCandidate] = []  # lup: ignore[empty-collection]
        for path in self.authored_between(lease, base, parent):
            contributed = self.added_lines(lease, base, parent, path)
            lost = self.lost_symbols(lease, base, parent, result, path)
            if not contributed and not lost:
                continue
            held = self.file_at(lease, result, path)
            missing = [line for line in contributed if line not in held]
            if missing or lost:
                found.append(
                    DropCandidate(
                        parent=parent, path=path, missing=missing, lost_symbols=lost
                    )
                )
        return found

    def lost_symbols(
        self,
        lease: WritableRootLease,
        base: str,
        parent: str,
        result: str,
        path: Path,
    ) -> list[DefinedSymbol]:
        """Definitions this parent introduced that the joined tree dropped.

        Restricted to what the parent itself added, so a definition removed
        deliberately on the other side is that side's decision and not this
        parent's loss. The convention this repository states for merges is
        exactly this comparison — account for every missing `def` and `class`
        — and stating it in a guidance document made it a step a merger could
        skip, where computing it makes the answer an obligation.
        """
        if path.suffix.lower() not in {".py", ".pyi"}:
            return []
        introduced = symbols_lost(
            self.file_at(lease, parent, path), self.file_at(lease, base, path)
        )
        held = {
            symbol.name for symbol in defined_symbols(self.file_at(lease, result, path))
        }
        return [symbol for symbol in introduced if symbol.name not in held]

    def commit_join(self, lease: WritableRootLease, title: str) -> str:
        """Create and read the orchestrator-owned semantic join commit."""
        repository = self.repository(lease.root)
        checked = repository.run("diff", "--check")
        unresolved = repository.run("diff", "--name-only", "--diff-filter=U")
        # `git diff --check` is the conflict guard: it exits non-zero on a
        # marker left in the content. A path can also sit unmerged in the index
        # while its content is fully resolved, which is what the merger leaves
        # behind when it edits the file without staging it — `git add -A` below
        # settles exactly that, so an unmerged path is only a failure when the
        # content still carries markers. Naming both conditions here rejected
        # resolved work and stopped one line short of the call that would have
        # accepted it.
        if checked.code != 0 or unresolved.code != 0:
            raise RuntimeError(
                f"semantic join for {lease.concern_id} still has "
                f"invalid changes: {checked.stdout.strip() or unresolved.stderr.strip()}"
            )
        status = self.require(
            lease.root,
            f"failed to inspect semantic join for {lease.concern_id}",
            "status",
            "--porcelain",
        )
        if not status.stdout.splitlines() and repository.merging() is None:
            return self.head(lease)
        self.require(
            lease.root,
            f"failed to stage semantic join for {lease.concern_id}",
            "add",
            "-A",
        )
        self.require(
            lease.root,
            f"failed to commit semantic join for {lease.concern_id}",
            "commit",
            "-m",
            title,
        )
        return self.head(lease)


class DependencyBaseBuilder:
    """Build root, single-parent, and semantic multi-parent dependency bases."""

    def __init__(self, source: SourceSnapshot) -> None:
        self.source = source

    def build(
        self,
        concern: Concern,
        parent_commits: dict[str, str],  # lup: ignore[dict-str-payload]
        joined_commit: str | None = None,
    ) -> DependencyBase:
        commits = [parent_commits[parent] for parent in concern.dependencies]
        match commits:
            case []:
                commit = self.source.commit
                semantic_join = False
            case [only]:
                commit = only
                semantic_join = False
            case _:
                if joined_commit is None:
                    raise ValueError(
                        f"concern {concern.id!r} needs a semantic multi-parent join"
                    )
                commit = joined_commit
                semantic_join = True
        return DependencyBase(
            concern_id=concern.id,
            parent_concerns=concern.dependencies,
            parent_commits=commits,
            commit=commit,
            semantic_join=semantic_join,
        )
