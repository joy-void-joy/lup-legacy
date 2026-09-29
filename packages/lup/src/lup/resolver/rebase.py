"""Bringing a run's base, and the leases cut from it, up to its branch.

A run's leases are pinned to the commit the run was created at. Leaving
them there makes a fix landed on the integration branch *specifically to
unblock a parked run* the one thing that run cannot see: its workers read
replaced code and reach confident conclusions contradicting decisions
taken upstream, and every lease fails the same gate on a finding none of
them introduced.

Two moments, because they are not the same decision. A lease being created
has no work to lose, so it takes the current base by default. A lease that
already holds work is a branch somebody is on, so bringing the base into it
is asked for — and answered per lease before it is taken, since the
concerns most likely to conflict with an upstream fix are exactly the ones
editing the files it touched.
"""

from lup.resolver.record import BaseRefreshedEvent, Journal, LeaseRefreshedEvent
from lup.resolver.models import (
    INTEGRATION_CONCERN_ID,
    BaseRefresh,
    LeaseRefresh,
    RefreshReport,
    ResolveState,
    SourceSnapshot,
    WritableRootLease,
)
from lup.resolver.orchestrator import WorktreeOrchestrator
from lup.resolver.run import ResolveRun


class BaseRefresher:
    """Move where this run's leases start, and record what that did."""

    def __init__(
        self, run: ResolveRun, worktrees: WorktreeOrchestrator, journal: Journal
    ) -> None:
        self.run = run
        self.worktrees = worktrees
        self.journal = journal

    def moved_base(self, state: ResolveState, adopt: str = "") -> BaseRefresh:
        """Where this run's base stands, predicted or handed over resolved."""
        source = state.root_base()
        if adopt:
            return self.worktrees.adopted_base(source, adopt)
        return self.worktrees.refreshed_base(source)

    def refreshed(self, state: ResolveState, adopt: str = "") -> ResolveState:
        """Bring what a new lease is cut from up to the branch it came from.

        At lease creation, because that is the one moment a base can move
        with nothing at stake: the worktree does not exist yet, so there is
        no work to conflict with and nothing to re-derive.

        Recorded whether it moved or not. A refresh that could not be made
        cleanly is the reason the leases beside it are still where they
        were, and that is worth more in the record than the silence a
        no-op would leave.
        """
        refresh = self.moved_base(state, adopt)
        self.journal.record(
            BaseRefreshedEvent(
                branch=refresh.branch,
                was=refresh.was,
                commit=refresh.commit,
                conflicts=[path.as_posix() for path in refresh.conflicts],
                reason=refresh.reason,
            )
        )
        if not refresh.moved():
            return state
        moved = state.model_copy(
            update={
                "base": SourceSnapshot(branch=refresh.branch, commit=refresh.commit)
            }
        )
        self.run.persist(moved)
        return self.run.require()

    def report(
        self, state: ResolveState, apply: bool = False, adopt: str = ""
    ) -> RefreshReport:
        """Say what refreshing every live lease would do, and optionally do it.

        A concern whose work is already verified is left alone. Its commit
        is what the run records and joins, and moving its branch under that
        record is how a resume ends up refusing a run over a commit it made
        itself. What it produced reaches the refreshed tree at integration,
        where merging is the work rather than a side effect.
        """
        refresh = self.moved_base(state, adopt)
        settled = {outcome.concern_id for outcome in state.outcomes}
        report = RefreshReport(
            base=refresh,
            leases=[
                self.lease(lease, refresh.commit, apply)
                for lease in state.leases
                if lease.active
                and lease.concern_id not in settled
                and lease.concern_id != INTEGRATION_CONCERN_ID
            ],
            applied=apply,
        )
        if apply:
            self.refreshed(state, adopt)
            self.inherit(report, refresh.commit)
        return report

    def lease(self, lease: WritableRootLease, commit: str, apply: bool) -> LeaseRefresh:
        """Bring one lease's branch up to a commit, or say what stops it."""
        refreshed = self.attempted(lease, commit, apply)
        self.journal.record(
            LeaseRefreshedEvent(
                concern_id=refreshed.concern_id,
                commit=commit,
                applied=refreshed.applied,
                conflicts=[path.as_posix() for path in refreshed.conflicts],
                uncommitted=[path.as_posix() for path in refreshed.uncommitted],
                reason=refreshed.reason,
            )
        )
        return refreshed

    def attempted(
        self, lease: WritableRootLease, commit: str, apply: bool
    ) -> LeaseRefresh:
        """What a refresh of one lease would do, or did."""
        if not lease.root.exists():
            return LeaseRefresh(
                concern_id=lease.concern_id,
                reason="no worktree yet; it is cut from the base when it starts",
            )
        conflicts = self.worktrees.predicted_merge(lease, commit)
        if conflicts:
            return LeaseRefresh(
                concern_id=lease.concern_id,
                conflicts=conflicts,
                reason="this lease edits what the base moved; merge it by hand",
            )
        held = self.worktrees.uncommitted(lease)
        if held:
            return LeaseRefresh(
                concern_id=lease.concern_id,
                uncommitted=held,
                reason=(
                    "this lease holds uncommitted work the prediction cannot "
                    "see; commit or discard it, then refresh: "
                    + ", ".join(path.as_posix() for path in held)
                ),
            )
        if not apply:
            return LeaseRefresh(concern_id=lease.concern_id)
        applied = self.worktrees.merge_into(
            lease, commit, f"resolve: refresh {lease.concern_id} onto the run base"
        )
        return LeaseRefresh(
            concern_id=lease.concern_id,
            applied=applied,
            reason="" if applied else "git refused the merge; nothing was changed",
        )

    def inherit(self, report: RefreshReport, commit: str) -> None:
        """Move a refreshed lease's recorded base to what it now inherits.

        The gate a worker is judged by is scoped to what its own tree
        changed, measured from that base. Left where it was, a refreshed
        lease would be answerable for every upstream change it has just
        taken in — which is the shape of blocker no revision round can
        converge on, and the reason the refresh was wanted at all.
        """
        refreshed = {lease.concern_id for lease in report.leases if lease.applied}
        for base in self.run.require().bases:
            if base.concern_id not in refreshed:
                continue
            combined = self.worktrees.merged_base(
                base.commit,
                commit,
                f"chore(resolve): base {base.concern_id} inherits the run base",
            )
            if combined.moved():
                self.run.replace_dependency_base(
                    base.model_copy(update={"commit": combined.commit})
                )
