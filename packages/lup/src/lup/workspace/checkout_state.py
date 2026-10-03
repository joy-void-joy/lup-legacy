"""What lup keeps beneath one checkout's ``.lup/``, declared once.

Each path here is written by one module and read by another that may never
import it: a launch's measurement and the gate that believes it, a parked
review and the waiter that carries it out, a resolver run and the supervisor
following it. :mod:`lup.workspace.paths` names every other place a project
keeps something; this names the state a checkout accumulates, so a rename
reaches the writer and the reader together instead of leaving one of them on
a file nothing writes.

Who writes a path decides whether a session may. :meth:`CheckoutState.hook_state`
is what the library writes from outside the session — a launch's measured
ledger, the policy snapshots a capture restores from, the review queue a hook
parks a question in, the documents and archive it keeps beside it, and the
claims that spend an approved answer once — and the policy protects exactly
that list, derived from here rather than re-listed beside it. The rest is the
session's own working state.

The bare hook script spells its own copies, since it runs with no ``lup`` to
import; a test holds them to this declaration.
"""

from pathlib import Path

from pydantic import BaseModel


class CheckoutState(BaseModel, frozen=True):
    """Every path lup keeps under one checkout's ``.lup/``."""

    root: Path

    def directory(self) -> Path:
        """The one directory all of it sits in, ignored by every checkout lup makes."""
        return self.root / ".lup"

    def preflight(self) -> Path:
        """A launch's measured boundary, one ledger per launch, named for it."""
        return self.directory() / "preflight"

    def boundary(self) -> Path:
        """The mount table a launch measured, for its session's gates to believe."""
        return self.directory() / "boundary.json"

    def policy_snapshots(self) -> Path:
        """The accepted policy each launch restores from."""
        return self.directory() / "policy-snapshots"

    def questions(self) -> Path:
        """The review queue: every question a hook parked, and what came of it."""
        return self.directory() / "questions.jsonl"

    def reviews(self) -> Path:
        """The documents the review queue names by digest, and settled reviews' archive."""
        return self.directory() / "reviews"

    def review_claims(self) -> Path:
        """One claim per approved answer, so each is spent once."""
        return self.directory() / "review-claims"

    def review_stage_claims(self) -> Path:
        """One claim per approved stage of a review carried out in stages."""
        return self.directory() / "review-stage-claims"

    def review_waiters(self) -> Path:
        """One lock per review a waiter is holding open."""
        return self.directory() / "review-waiters"

    def review_notifications(self) -> Path:
        """Each review's notification diagnostics, which grant no authority."""
        return self.directory() / "review-notifications"

    def hook_corpus(self) -> Path:
        """Every command the dispatcher declined to interrupt about."""
        return self.directory() / "hooks" / "learned.jsonl"

    def hook_approvals(self) -> Path:
        """Each approval a person gave through the dispatcher, latest line per command."""
        return self.directory() / "hooks" / "approvals.jsonl"

    def script_runs(self) -> Path:
        """How often each scratch script has run, to say when one became a tool."""
        return self.directory() / "script-runs.json"

    def referrals(self) -> Path:
        """Which repositories each session was already referred to."""
        return self.directory() / "referrals.json"

    def resolve(self) -> Path:
        """Every resolver run: its state, journal, leases and offers."""
        return self.directory() / "resolve"

    def reconcile(self) -> Path:
        """Each reconciliation proposal, kept immutable with its preimage."""
        return self.directory() / "reconcile"

    def profiles(self) -> Path:
        """The accounts a project keeps as directories, one per name."""
        return self.directory() / "profiles"

    def conversations(self) -> Path:
        """The browser profiles conversation retainers sign in with."""
        return self.directory() / "conversations"

    def sessions(self) -> Path:
        """The configuration homes sessions derived for this checkout run under."""
        return self.directory() / "sessions"

    def lease(self) -> Path:
        """Who holds this checkout's checkpoints, so a dead holder is told from a working one."""
        return self.directory() / "lease"

    @classmethod
    def hook_state(cls) -> list[str]:
        """What the library writes from outside a session, relative to any checkout.

        A session writing one is the confined thing recording what confines
        it — its own measurement, its own answer, a spent approval put back —
        so the policy asks about every one, whatever a project declares.
        """
        anywhere = cls(root=Path())
        return [
            held.as_posix()
            for held in (
                anywhere.preflight(),
                anywhere.policy_snapshots(),
                anywhere.questions(),
                anywhere.reviews(),
                anywhere.review_claims(),
                anywhere.review_stage_claims(),
            )
        ]
