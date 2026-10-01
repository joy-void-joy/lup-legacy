"""Retiring a concern whose work was settled somewhere other than this run."""

from pathlib import Path

import pytest

from lup.resolver.models import (
    AcceptanceCriterion,
    Concern,
    ConcernProgress,
    ConcernRetirement,
    ConcernStatus,
    ResolvePhase,
    ResolveState,
    ResolverConfig,
    SourceSnapshot,
    VerificationCommand,
)
from tests.unit.test_resolver_core import resolve_spec
from lup.resolver.state import (
    CONCERN_TRANSITIONS,
    ResolverStateRepository,
    StateTransitionError,
    already_settled,
)


def seeded(tmp_path: Path, status: ConcernStatus) -> ResolverStateRepository:
    """A one-concern run standing at `status`, persisted and readable."""
    repository = ResolverStateRepository(tmp_path, "run")
    repository.root.mkdir(parents=True, exist_ok=True)
    repository.write_model(
        "state.json",
        ResolveState(
            run_id="run",
            config_digest="digest",
            config=ResolverConfig(
                state_root=tmp_path,
                workspace=tmp_path,
                worktree_root=tmp_path / "trees",
                run_id="run",
                integration_branch="resolve/run/review",
                verification_commands=[
                    VerificationCommand(name="v", arguments=["git", "status"])
                ],
            ),
            spec=resolve_spec(),
            phase=ResolvePhase.WORKERS,
            source=SourceSnapshot(branch="dev", commit="a" * 40),
            concerns=[
                Concern(
                    id="settled-upstream",
                    title="Already landed",
                    spec="The work this concern was planned for",
                    criteria=[AcceptanceCriterion(id="c1", description="it holds")],
                )
            ],
            progress=[ConcernProgress(concern_id="settled-upstream", status=status)],
        ),
    )
    return repository


def test_retiring_records_where_the_work_went_without_failing_the_concern(
    tmp_path: Path,
) -> None:
    """A concern settled elsewhere must not read as work that did not hold up.

    Without retirement, the only ways out are hand-resolving an add/add
    conflict between two implementations of one thing, letting a worker open
    on a concern whose notes no longer exist in its tree, or aborting the run
    and discarding every settled answer to retire one concern.
    """
    repository = seeded(tmp_path, ConcernStatus.LEASED)

    retired = repository.retire(
        ConcernRetirement(
            concern_id="settled-upstream", reason="landed on dev as d57d0f0e"
        )
    )

    progress = {item.concern_id: item for item in retired.progress}
    assert progress["settled-upstream"].status == ConcernStatus.RETIRED
    assert progress["settled-upstream"].status != ConcernStatus.FAILED
    assert retired.retirements[0].reason == "landed on dev as d57d0f0e"


def test_a_failed_concern_can_still_be_retired(tmp_path: Path) -> None:
    """The concerns most worth retiring are the ones that already failed.

    Three in one run failed for reasons that were settled decisions rather
    than open work, and a run with no retire path can only record them as
    having failed.
    """
    repository = seeded(tmp_path, ConcernStatus.FAILED)

    retired = repository.retire(
        ConcernRetirement(concern_id="settled-upstream", reason="superseded upstream")
    )

    progress = {item.concern_id: item for item in retired.progress}
    assert progress["settled-upstream"].status == ConcernStatus.RETIRED


def test_a_stale_scheduler_save_preserves_live_retirement(tmp_path: Path) -> None:
    repository = seeded(tmp_path, ConcernStatus.LEASED)
    stale = repository.load()
    with repository.exclusive():
        repository.retire(
            ConcernRetirement(concern_id="settled-upstream", reason="upstream")
        )
        saved = repository.save(stale)
    assert saved == repository.load()
    assert saved.progress[0].status == ConcernStatus.RETIRED
    assert saved.retirements[0].reason == "upstream"


def test_a_concern_settled_here_is_refused(tmp_path: Path) -> None:
    """Retiring claims a concern was settled elsewhere, so it must not be."""
    repository = seeded(tmp_path, ConcernStatus.CLEANED)

    with pytest.raises(StateTransitionError) as refused:
        repository.retire(
            ConcernRetirement(concern_id="settled-upstream", reason="elsewhere")
        )

    assert "settled here" in str(refused.value)


def test_a_concern_this_run_never_had_is_refused(tmp_path: Path) -> None:
    repository = seeded(tmp_path, ConcernStatus.LEASED)

    with pytest.raises(StateTransitionError):
        repository.retire(ConcernRetirement(concern_id="typo", reason="elsewhere"))


def test_every_unsettled_status_can_reach_retirement() -> None:
    """Derived rather than listed, so a status added later cannot be forgotten.

    A hand-written transition table cannot report the entry nobody added,
    which is the failure mode this derivation removes.
    """
    unreachable = [
        status
        for status, targets in CONCERN_TRANSITIONS.items()
        if not already_settled(status) and ConcernStatus.RETIRED not in targets
    ]

    assert unreachable == []
    assert CONCERN_TRANSITIONS[ConcernStatus.RETIRED] == []
