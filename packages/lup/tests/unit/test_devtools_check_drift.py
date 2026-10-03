"""What the gate's drift row tells a reader who only reads the summary.

Two halves go stale independently — the native trees and the generated
artifacts outside them — and a row counting only the first meets the second,
the failure this repository meets most often, as `FAIL (0 tree(s))` naming
nothing: a true verdict, a useless line. The detail exists, on stderr, three
minutes up a run that prints two thousand other lines.
"""

from lup.devtools.harness.drift import DriftVerdict

WORK_STATUS_STALE = (
    "/repo/docs/work-status.md is stale; run `uv run lup-devtools ledger writeup`"
)


def test_a_current_tree_and_a_current_repository_read_ok() -> None:
    assert DriftVerdict(reports=[], stale_repository=[]).summary == [
        "harness drift: ok"
    ]


def test_a_stale_repository_artifact_is_named_where_no_tree_is() -> None:
    verdict = DriftVerdict(reports=[], stale_repository=[WORK_STATUS_STALE])

    assert verdict.summary == [
        "harness drift: FAIL — 1 stale repository artifact(s)",
        f"  {WORK_STATUS_STALE}",
    ]


def test_the_row_says_which_half_is_behind() -> None:
    # What a reader acts on is not "how much is stale" but "which of the two
    # regenerations do I owe" — `harness generate all` for a tree, the command
    # each repository message names for the other — so the regeneration is
    # named only where a tree is behind.
    verdict = DriftVerdict(reports=[], stale_repository=[WORK_STATUS_STALE])

    assert verdict.summary[0].endswith("— 1 stale repository artifact(s)")
    assert not any("harness generate all" in line for line in verdict.summary)
    assert not verdict.clean
