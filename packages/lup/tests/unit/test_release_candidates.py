"""Release candidates: how one is numbered, when one is promoted, and when not.

A candidate is a release published as a pre-release — ``vX.Y.ZrcN`` — so a
project can take it on purpose before everybody takes it by default. What is
pinned here is the arithmetic that decides which candidate comes next, and the
one question a promotion asks: whether the release branch still holds exactly
what the candidate shipped. Where it does, the final version is the same
commit under a second tag; where it does not, the command says so and names
both ways on rather than choosing between them.
"""

import datetime as dt

import pytest

from lup.devtools.changelog import Changelog
from lup.devtools.dev.release import (
    Landing,
    PendingBreaks,
    ReleasePlan,
    ReleaseRefused,
    ReleaseRequest,
    ReleaseSpec,
    ReleaseState,
    candidate_version,
    level_of,
)
from lup.devtools.utils import short_sha

DAY = dt.date(2026, 9, 28)

SPEC = ReleaseSpec()

OPEN = Changelog.parse("# Changelog\n\n## Unreleased\n\n- something landed\n")

PENDING = PendingBreaks(lines=["gone — a reason", "  do this instead"], count=1)


def planned(
    state: ReleaseState,
    request: ReleaseRequest,
    log: Changelog = OPEN,
    pending: PendingBreaks = PENDING,
) -> ReleasePlan:
    """The plan a request makes of a state, on the one day these tests run."""
    return state.planned(request, DAY, log, pending, SPEC)


def held(carried: list[str] | None = None, since: int = 0) -> Landing:
    """A release branch standing exactly where the newest candidate put it."""
    return Landing(
        commit="c" * 40,
        branch="origin/main",
        held=True,
        carried=carried or [],
        since=since,
    )


# -- numbering --


def test_the_first_candidate_of_a_version_is_its_rc1() -> None:
    plan = planned(
        ReleaseState(manifest="0.4.0", tagged=["0.3.0", "0.4.0"]),
        ReleaseRequest(level="minor", pre=True),
    )

    assert plan.kind == "candidate"
    assert (plan.previous, plan.version, plan.target) == ("0.4.0", "0.5.0rc1", "0.5.0")
    assert plan.tag == "v0.5.0rc1"
    assert plan.level == "minor"


def test_the_next_candidate_counts_up_and_keeps_the_series_level() -> None:
    """The level is settled once; a later candidate is asked for without one."""
    plan = planned(
        ReleaseState(manifest="0.5.0", tagged=["0.4.0", "0.5.0rc1"]),
        ReleaseRequest(pre=True),
    )

    assert (plan.previous, plan.version) == ("0.5.0rc1", "0.5.0rc2")
    assert plan.level == "minor"
    assert plan.candidates == ["0.5.0rc1"]


def test_naming_the_series_own_level_again_is_the_same_series() -> None:
    plan = planned(
        ReleaseState(manifest="0.5.0", tagged=["0.4.0", "0.5.0rc1", "0.5.0rc2"]),
        ReleaseRequest(level="minor", pre=True),
    )

    assert plan.version == "0.5.0rc3"
    assert plan.relevelled == ""


def test_relevelling_up_opens_a_series_for_the_new_version() -> None:
    plan = planned(
        ReleaseState(manifest="0.4.1", tagged=["0.4.0", "0.4.1rc1", "0.4.1rc2"]),
        ReleaseRequest(level="minor", pre=True),
    )

    assert plan.version == "0.5.0rc1"
    assert plan.relevelled == "0.4.1"


def test_relevelling_down_counts_from_the_last_release() -> None:
    plan = planned(
        ReleaseState(manifest="0.5.0", tagged=["0.4.0", "0.5.0rc1"]),
        ReleaseRequest(level="patch", pre=True),
    )

    assert plan.version == "0.4.1rc1"
    assert plan.relevelled == "0.5.0"


def test_numbering_is_per_target_and_survives_an_abandoned_series() -> None:
    """N counts from 1 per version, and a version already cut keeps counting.

    PyPI accepts a version once, so a number a tag already spent is never
    handed out again — even when the series that spent it was re-levelled
    away and is now being returned to.
    """
    plan = planned(
        ReleaseState(manifest="0.5.0", tagged=["0.4.0", "0.4.1rc1", "0.5.0rc1"]),
        ReleaseRequest(level="patch", pre=True),
    )

    assert plan.version == "0.4.1rc2"


def test_opening_a_series_needs_a_level() -> None:
    with pytest.raises(ReleaseRefused, match="level"):
        planned(
            ReleaseState(manifest="0.4.0", tagged=["0.4.0"]), ReleaseRequest(pre=True)
        )


def test_a_tag_that_is_not_a_release_or_a_candidate_is_not_read_as_one() -> None:
    """A hand-made beta, a nightly, a word: none of them opens a series."""
    plan = planned(
        ReleaseState(manifest="0.4.0", tagged=["0.4.0", "0.5.0b1", "nightly"]),
        ReleaseRequest(level="patch"),
    )

    assert (plan.kind, plan.version) == ("release", "0.4.1")


def test_each_version_names_its_own_level() -> None:
    assert [level_of(v) for v in ("0.4.1", "0.5.0", "1.0.0")] == [
        "patch",
        "minor",
        "major",
    ]
    assert candidate_version("0.5.0", 3) == "0.5.0rc3"


# -- promotion --


def test_an_unmoved_release_branch_promotes_the_candidate_as_it_is() -> None:
    """Same commit, second tag: nothing is rebuilt, so nothing can differ."""
    plan = planned(
        ReleaseState(
            manifest="0.5.0",
            tagged=["0.4.0", "0.5.0rc1", "0.5.0rc2"],
            landing=held(carried=["taken.toml"]),
        ),
        ReleaseRequest(),
    )

    assert plan.kind == "promotion"
    assert (plan.previous, plan.version, plan.tag) == ("0.5.0rc2", "0.5.0", "v0.5.0")
    assert plan.commit == "c" * 40
    assert plan.candidates == ["0.5.0rc1", "0.5.0rc2"]
    assert plan.carried == ["taken.toml"]
    assert plan.breaks == 1


def test_a_moved_release_branch_asks_rather_than_promotes() -> None:
    """Both ways on are named; which one is taken is not this command's call."""
    moved = Landing(commit="c" * 40, branch="origin/main", held=False, moved=3)

    with pytest.raises(ReleaseRefused) as refused:
        planned(
            ReleaseState(manifest="0.5.0", tagged=["0.4.0", "0.5.0rc2"], landing=moved),
            ReleaseRequest(),
        )

    said = str(refused.value)
    assert "moved since 0.5.0rc2" in said
    assert "3 commit(s)" in said
    assert "--pre" in said
    assert "--direct" in said


def test_a_candidate_the_release_branch_does_not_hold_yet_is_not_promoted() -> None:
    behind = Landing(commit="c" * 40, branch="origin/main", held=False, moved=0)

    with pytest.raises(ReleaseRefused, match="land it"):
        planned(
            ReleaseState(
                manifest="0.5.0", tagged=["0.4.0", "0.5.0rc1"], landing=behind
            ),
            ReleaseRequest(),
        )


def test_no_release_branch_to_read_is_no_promotion() -> None:
    nowhere = Landing(commit="c" * 40)

    with pytest.raises(ReleaseRefused, match="main"):
        planned(
            ReleaseState(manifest="0.5.0", tagged=["0.5.0rc1"], landing=nowhere),
            ReleaseRequest(),
        )


def test_direct_releases_what_the_branch_holds_over_an_open_series() -> None:
    plan = planned(
        ReleaseState(
            manifest="0.5.0",
            tagged=["0.4.0", "0.5.0rc1"],
            landing=Landing(commit="c" * 40, branch="main", moved=2),
        ),
        ReleaseRequest(direct=True),
    )

    assert plan.kind == "release"
    assert (plan.previous, plan.version, plan.tag) == ("0.5.0rc1", "0.5.0", "v0.5.0")
    assert plan.migrations == PENDING.lines


def test_a_release_at_another_level_relevels_instead_of_promoting() -> None:
    plan = planned(
        ReleaseState(manifest="0.5.0", tagged=["0.4.0", "0.5.0rc1"], landing=held()),
        ReleaseRequest(level="patch"),
    )

    assert (plan.kind, plan.version, plan.relevelled) == ("release", "0.4.1", "0.5.0")


def test_a_candidate_and_no_candidate_at_once_is_refused() -> None:
    with pytest.raises(ReleaseRefused, match="--pre"):
        planned(
            ReleaseState(manifest="0.4.0"),
            ReleaseRequest(level="minor", pre=True, direct=True),
        )


def test_a_release_with_no_series_open_is_the_ordinary_one() -> None:
    plan = planned(
        ReleaseState(manifest="0.4.0", tagged=["0.4.0"]),
        ReleaseRequest(level="minor"),
    )

    assert (plan.kind, plan.previous, plan.version) == ("release", "0.4.0", "0.5.0")
    assert plan.breaks == 1


def test_a_release_with_no_level_and_nothing_to_promote_is_refused() -> None:
    with pytest.raises(ReleaseRefused, match="level"):
        planned(ReleaseState(manifest="0.4.0", tagged=["0.4.0"]), ReleaseRequest())


# -- the plan a dry run prints --


def test_a_candidate_plan_says_what_it_publishes_and_what_stays_open() -> None:
    spelled = planned(
        ReleaseState(manifest="0.4.0", tagged=["0.4.0"]),
        ReleaseRequest(level="minor", pre=True),
    ).spelled()
    said = "\n".join(spelled)

    assert "0.4.0 → 0.5.0rc1, tagged v0.5.0rc1" in said
    assert "pre-release of 0.5.0" in said
    assert "stays open" in said
    assert "stay pending until 0.5.0 is released" in said


def test_a_promotion_plan_names_the_commit_and_rebuilds_nothing() -> None:
    spelled = planned(
        ReleaseState(
            manifest="0.5.0",
            tagged=["0.4.0", "0.5.0rc1", "0.5.0rc2"],
            landing=held(carried=["taken.toml"], since=4),
        ),
        ReleaseRequest(),
    ).spelled()
    said = "\n".join(spelled)

    assert "0.5.0rc2 → 0.5.0, tagged v0.5.0" in said
    assert "nothing rebuilt" in said
    assert f"({short_sha('c' * 40)})" in said
    assert "0.5.0rc1, 0.5.0rc2" in said
    assert "4 commit(s)" in said
