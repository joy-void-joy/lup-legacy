"""What a thousand reviews cost to keep, to fold, and to hand the page, bounded.

The fixture is a thousand native reviews of a one-line edit, each binding a
14 KB preimage and a 14 KB after-document of its own, 980 carried out and 20
waiting -- the shape of a busy week in one checkout, where a review's
documents outweigh its call. Built and measured in about two seconds, it
runs with the rest of the suite. Measured on a workstation, with other test
suites running:

- the log holds 2.2 KB per review, where the same reviews kept whole in
  every record, one copy per transition, come to about 100 KB each;
- the first snapshot is 37 KB, where one carrying a row for every review is
  535 KB;
- a fold of the whole log from a reader that has read none of it takes 0.07
  to 0.19 s of the measuring thread's CPU time, and the first snapshot 0.10 to
  0.25 s.

The two sizes are what tell the shapes apart: time alone does not at this
scale, since the whole-copy log of these reviews still parses in about
0.4 s, and a snapshot summarizing every review costs 0.5 to 0.7 s. So each
size is bounded at three to four times what was measured, and each time at
five to eight times the slowest measurement. The times are the measuring
thread's CPU time, so neither a suite running beside others nor a thread an
earlier test left running in this process inflates them; only work growing
faster than the log -- a question
reading the log again, or a review reading a file for each of the others --
exceeds them.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lup.devtools.dashboard.reviews import ReviewStore
from lup.policy.operations import Operation
from lup.policy.relay import CapturedFileReview, PersistentQuestion, QuestionRelay

LOG_BYTES_PER_REVIEW = 8 * 1024
"""The fixture's log, over the reviews it holds."""

SNAPSHOT_BYTES = 128 * 1024
"""The first snapshot of the fixture's queue, as JSON."""

FOLD_SECONDS = 1.0
"""A fold of the whole fixture's log, from a reader that has read none of it, in CPU seconds."""

SNAPSHOT_SECONDS = 2.0
"""The first snapshot of the fixture's queue, from a store that has read none of it, in CPU seconds."""


def timed(action: Callable[[], object]) -> float:
    """The CPU time this thread spends on *action*, which other load does not inflate."""
    started = time.thread_time()
    action()
    return time.thread_time() - started


def review(root: Path, index: int) -> PersistentQuestion:
    """One native review of a rewrite of its own file, bound as a hook binds it."""
    target = root / f"module_{index}.py"
    before = "".join(f"value_{index}_{line} = {line}\n" for line in range(800))
    old, new = f"value_{index}_400 = 400\n", f"value_{index}_400 = 401\n"
    after = before.replace(old, new)
    target.write_text(before)
    operation = Operation(
        id=f"operation-{index}",
        session="bench-session",
        requester="bench-session",
        tool="Edit",
        payload={"file_path": str(target), "old_string": old, "new_string": new},
        cwd=root,
        worktree=root,
    )
    question = PersistentQuestion(
        id=f"review-{index}",
        operation=operation,
        fingerprint="",
        preconditions={target: before},
        file_reviews=[
            CapturedFileReview(
                path=target,
                effect="ask",
                reason="a rewrite",
                rule="edit:bench",
                rules=["edit:bench"],
                before_sha256=None,
                after_sha256=None,
                after=after,
            )
        ],
        resumption="native_retry",
        execution_payload=operation.payload,
        scheme=["file_reviews", "unpreviewed", "segments"],
        reason="a rewrite asks",
        chain_resolved=False,
        resolved={target: target},
        created=datetime.now(UTC),
    )
    return question.model_copy(update={"fingerprint": question.native_fingerprint()})


@pytest.fixture(scope="module")
def checkout(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("relay-benchmark")
    store = QuestionRelay(root / ".lup/questions.jsonl", root / "answers.jsonl")
    for index in range(1000):
        store.record(review(root, index))
    store.transitioned(
        {f"review-{index}": {"state": "dispatched"} for index in range(20, 1000)}
    )
    store.transitioned(
        {
            f"review-{index}": {"state": "completed", "outcome": "carried out"}
            for index in range(20, 1000)
        }
    )
    return root


def test_a_thousand_reviews_are_kept_and_folded_within_the_bound(
    checkout: Path,
) -> None:
    log = checkout / ".lup/questions.jsonl"
    reader = QuestionRelay(log, checkout / "answers.jsonl")

    seconds = timed(reader.questions)

    assert len(reader.questions()) == 1000
    per_review = log.stat().st_size / 1000
    assert per_review < LOG_BYTES_PER_REVIEW, (
        f"the log holds {per_review:.0f} B a review"
    )
    assert seconds < FOLD_SECONDS, f"a fold of 1000 reviews took {seconds:.3f}s"


def test_the_first_snapshot_of_a_thousand_reviews_stays_within_the_bound(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LUP_REVIEW_ANSWERS", str(checkout / "answers"))
    store = ReviewStore(roots=(checkout,))

    seconds = timed(store.snapshot)

    snapshot = store.snapshot()
    size = len(snapshot.model_dump_json())
    assert snapshot.history == 980
    assert len(snapshot.reviews) == 20 + store.recent
    assert size < SNAPSHOT_BYTES, f"the first snapshot is {size} B"
    assert seconds < SNAPSHOT_SECONDS, f"the first snapshot took {seconds:.3f}s"
