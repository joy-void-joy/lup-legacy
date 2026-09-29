"""A concern's progress and its outcome are one fact, written once.

Recorded apart, an interruption lands between them: the run then reports a
success no integration can consume, because every surface that counts
progress reads the higher number while the batch that would have gathered
the outcome never returns. These pin the two to a single write.
"""

from pathlib import Path

import pytest

from lup.harness.models import ResolveSpec, SkillInvocation
from lup.resolver.record import Journal
from lup.resolver.models import (
    AcceptanceCriterion,
    Concern,
    ConcernOutcome,
    ConcernProgress,
    ConcernStatus,
    ResolvePhase,
    ResolveState,
    SourceSnapshot,
)
from lup.resolver.run import ResolveRun
from lup.resolver.state import (
    ResolverStateRepository,
    StateTransitionError,
    validate_progress_transition,
)

RUN_ID = "run-durability"


def planned(identifier: str) -> Concern:
    return Concern(
        id=identifier,
        title="work",
        spec="do the thing",
        criteria=[AcceptanceCriterion(id=f"{identifier}-done", description="done")],
    )


def seeded(
    root: Path,
    concerns: list[Concern],
    status: ConcernStatus = ConcernStatus.REVIEWING,
) -> ResolveRun:
    """A run holding persisted state, the way the workers phase finds it.

    Seeded at ``REVIEWING`` because that is where a concern stands when its
    review returns, and the transition table refuses the shortcut.
    """
    repository = ResolverStateRepository(root, RUN_ID)
    repository.root.mkdir(parents=True, exist_ok=True)
    run = ResolveRun(repository, Journal(repository.root))
    run.persist(
        ResolveState(
            config_digest="digest",
            run_id=RUN_ID,
            phase=ResolvePhase.WORKERS,
            source=SourceSnapshot(branch="dev", commit="source"),
            spec=ResolveSpec(
                id="resolve",
                worker_identity="resolver-worker",
                worker_skill=SkillInvocation(plugin="lup", skill="worker"),
                review_skill=SkillInvocation(plugin="lup", skill="review"),
                merge_skill=SkillInvocation(plugin="lup", skill="merge"),
            ),
            concerns=concerns,
            progress=[
                ConcernProgress(concern_id=item.id, status=status) for item in concerns
            ],
        )
    )
    return run


def verified(identifier: str, commit: str = "c0ffee") -> ConcernOutcome:
    return ConcernOutcome(
        concern_id=identifier,
        branch=f"resolve/{RUN_ID}/{identifier}",
        commit=commit,
        head=commit,
        verified=True,
    )


@pytest.mark.asyncio
async def test_a_verified_concern_is_readable_from_disk_with_its_outcome(
    tmp_path: Path,
) -> None:
    """What survives a kill is what a resume and every tally must agree on."""
    run = seeded(tmp_path, [planned("alpha"), planned("beta")])

    await run.settle_concern(verified("alpha"), ConcernStatus.VERIFIED)

    reloaded = ResolverStateRepository(tmp_path, RUN_ID).load()
    statuses = {item.concern_id: item.status for item in reloaded.progress}
    assert statuses["alpha"] == ConcernStatus.VERIFIED
    assert [outcome.concern_id for outcome in reloaded.outcomes] == ["alpha"]
    assert reloaded.outcomes[0].commit == "c0ffee"


@pytest.mark.asyncio
async def test_progress_never_claims_a_success_the_outcomes_cannot_support(
    tmp_path: Path,
) -> None:
    """The skew #95 measured: 20 verified in progress, 16 outcomes on disk."""
    run = seeded(tmp_path, [planned(name) for name in ("alpha", "beta", "gamma")])

    await run.settle_concern(verified("alpha"), ConcernStatus.VERIFIED)
    await run.settle_concern(verified("beta"), ConcernStatus.VERIFIED)

    reloaded = ResolverStateRepository(tmp_path, RUN_ID).load()
    progressed = [
        item.concern_id
        for item in reloaded.progress
        if item.status == ConcernStatus.VERIFIED
    ]
    recorded = [outcome.concern_id for outcome in reloaded.outcomes if outcome.verified]
    assert progressed == recorded


@pytest.mark.asyncio
async def test_re_executing_a_concern_overwrites_its_outcome(tmp_path: Path) -> None:
    """A resumed concern must replace its record, not shadow it with a second."""
    run = seeded(tmp_path, [planned("alpha")])

    await run.settle_concern(verified("alpha", "first"), ConcernStatus.VERIFIED)
    await run.settle_concern(verified("alpha", "second"), ConcernStatus.VERIFIED)

    reloaded = ResolverStateRepository(tmp_path, RUN_ID).load()
    assert [outcome.commit for outcome in reloaded.outcomes] == ["second"]


def test_a_skewed_state_cannot_be_persisted_at_all(tmp_path: Path) -> None:
    """The guard that would have caught #95: the write itself is refused.

    Trusting the one call site that settles a concern leaves the next site
    added free to reintroduce the skew, so the invariant is checked where
    every write passes rather than where this one does.
    """
    run = seeded(tmp_path, [planned("alpha")])
    skewed = run.require().model_copy(
        update={
            "progress": [
                ConcernProgress(concern_id="alpha", status=ConcernStatus.VERIFIED)
            ]
        }
    )

    with pytest.raises(StateTransitionError, match="no recorded outcome"):
        validate_progress_transition(run.require(), skewed)


@pytest.mark.asyncio
async def test_a_failure_is_recorded_the_same_way_as_a_success(tmp_path: Path) -> None:
    """A failed concern is excluded from integration by its outcome, not its status."""
    run = seeded(tmp_path, [planned("alpha")], ConcernStatus.REVISING)
    exhausted = ConcernOutcome(
        concern_id="alpha",
        branch=f"resolve/{RUN_ID}/alpha",
        verified=False,
        failure="revision limit exhausted",
    )

    await run.settle_concern(
        exhausted, ConcernStatus.FAILED, "revision limit exhausted"
    )

    reloaded = ResolverStateRepository(tmp_path, RUN_ID).load()
    statuses = {item.concern_id: item.status for item in reloaded.progress}
    assert statuses["alpha"] == ConcernStatus.FAILED
    assert reloaded.outcomes[0].failure == "revision limit exhausted"
    assert not reloaded.outcomes[0].verified
