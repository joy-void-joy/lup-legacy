"""What a thousand reviews cost to keep, to fold, and to hand the page, bounded.

The fixture is a thousand native reviews of a one-line edit, each binding a
14 KB preimage and a 14 KB after-document of its own, 980 carried out and 20
waiting -- the shape of a busy week in one checkout, where a review's
documents outweigh its call. Built and measured in about two seconds, it
runs with the rest of the suite.

What goes wrong at this scale is work done once per review: a question
reading the log again, a review reading a file for each of the others, a
page carrying a row for every review. Each multiplies something countable
by about a thousand, so what is bounded is counted -- bytes kept, and files
opened -- and each bound is about three times what is measured, but the
log's, which each action opens once:

- the log holds 2.3 KB a review, bounded at 8 KB, where the same reviews
  with both documents copied into each of their three records come to about
  100 KB each;
- the first snapshot is 37 KB, bounded at 128 KB, where one carrying a row
  for every review is 539 KB;
- a fold of the whole log, from a reader that has read none of it, opens two
  files: the log, once, and the host's answers. It is bounded at six, the
  log exactly once; a fold that opens the log again for each question opens
  it 1001 times and 2002 files in all;
- the first snapshot opens 165 files: the two documents of each of the 70
  rows it hands the page (20 waiting, History's first 50), the file each
  waiting review rewrites, the archive, the boot id twice, and the log and
  the host's answers once each, the queue's questions, remarks and replies
  all made from that one read. It is bounded at 500, the log at once; one
  carrying a row for every review opens 2025.

The counts are the same on every run and under any load, which time is not:
under shared cores and caches even the measuring thread's own CPU time
inflates. Alone, the fold takes 0.06 to 0.47 s of it and the first snapshot
0.11 to 0.34 s; with 32 copies of the measurement on 32 cores the fold takes
up to 0.63 s, and beside several sessions' test suites 1.5 s -- while the
fold opening the log for each question takes 0.25 s, no slower than the
fold it regresses. So time is bounded only as a backstop against a blow-up
that opens no file, such as a scan of every review against every other in
memory: at 15 s for each, ten times the slowest either has measured.
"""

import os
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

from lup.devtools.dashboard.reviews import ReviewStore
from lup.policy.operations import Operation
from lup.policy.relay import (
    CapturedFileReview,
    PersistentQuestion,
    QuestionRelay,
    RecordedQuestion,
)

LOG_BYTES_PER_REVIEW = 8 * 1024
"""The fixture's log, over the reviews it holds."""

SNAPSHOT_BYTES = 128 * 1024
"""The first snapshot of the fixture's queue, as JSON."""

FOLD_OPENS = 6
"""Files a fold of the whole fixture's log opens, from a reader that has read none of it."""

SNAPSHOT_OPENS = 500
"""Files the first snapshot of the fixture's queue opens, from a store that has read none of it."""

SNAPSHOT_LOG_OPENS = 1
"""Times the first snapshot of the fixture's queue opens the log: once, as a fold does."""

FOLD_SECONDS = 15.0
"""A fold of the whole fixture's log, in the measuring thread's CPU seconds: a backstop load cannot reach."""

SNAPSHOT_SECONDS = 15.0
"""The first snapshot of the fixture's queue, in the measuring thread's CPU seconds: the same backstop."""


class Measured(BaseModel):
    """What one action cost the thread that ran it: each file it opened, in order, and its CPU time."""

    opened: list[Path]
    seconds: float

    def opens(self, path: Path) -> int:
        """How many times the action opened *path*."""
        return sum(each.resolve() == path.resolve() for each in self.opened)

    def described(self, log: Path) -> str:
        """What the action cost, as an assertion says it."""
        return f"{len(self.opened)} files opened, the log {self.opens(log)} times, in {self.seconds:.3f}s"


class OpenWatch:
    """The files one thread opens while armed, heard through the interpreter's audit hook.

    An audit hook is never removed, so this one is installed once and records
    only while :meth:`measured` runs, and only what the thread that called it
    opens: a thread an earlier test left running adds nothing to the count.
    """

    def __init__(self) -> None:
        self.thread: int | None = None
        self.opened: list[Path] = []
        sys.addaudithook(self.heard)

    def heard(self, event: str, arguments: tuple[object, ...]) -> None:
        """Keep the path of each open made on the armed thread."""
        match event, arguments:
            case "open", (str() | bytes() | os.PathLike() as path, *_) if (
                threading.get_ident() == self.thread
            ):
                self.opened.append(Path(os.fsdecode(path)))

    def measured(self, action: Callable[[], object]) -> Measured:
        """The files *action* opens and the CPU time it spends, on this thread alone."""
        self.opened, self.thread = [], threading.get_ident()
        started = time.thread_time()
        try:
            action()
        finally:
            self.thread = None
        return Measured(opened=self.opened, seconds=time.thread_time() - started)


def fold_within_bounds(fold: Measured, log: Path) -> bool:
    """Whether a fold of the fixture's log opened it exactly once, and few files in all."""
    return fold.opens(log) == 1 and len(fold.opened) <= FOLD_OPENS


def snapshot_within_bounds(first: Measured, log: Path) -> bool:
    """Whether the first snapshot of the fixture's queue opened few files, the log among them once."""
    return (
        first.opens(log) <= SNAPSHOT_LOG_OPENS and len(first.opened) <= SNAPSHOT_OPENS
    )


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


@pytest.fixture(scope="module")
def watch() -> OpenWatch:
    """The module's one audit hook, armed only around what a test measures."""
    return OpenWatch()


def test_a_thousand_reviews_are_kept_and_folded_within_the_bound(
    checkout: Path, watch: OpenWatch
) -> None:
    log = checkout / ".lup/questions.jsonl"
    reader = QuestionRelay(log, checkout / "answers.jsonl")

    fold = watch.measured(reader.questions)

    assert len(reader.questions()) == 1000
    per_review = log.stat().st_size / 1000
    assert per_review < LOG_BYTES_PER_REVIEW, (
        f"the log holds {per_review:.0f} B a review"
    )
    said = f"a fold of 1000 reviews: {fold.described(log)}"
    assert fold_within_bounds(fold, log), said
    assert fold.seconds < FOLD_SECONDS, said


def test_the_first_snapshot_of_a_thousand_reviews_stays_within_the_bound(
    checkout: Path, watch: OpenWatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LUP_REVIEW_ANSWERS", str(checkout / "answers"))
    store = ReviewStore(roots=(checkout,))

    first = watch.measured(store.snapshot)

    snapshot = store.snapshot()
    size = len(snapshot.model_dump_json())
    assert snapshot.history == 980
    assert len(snapshot.reviews) == 20 + store.recent
    assert size < SNAPSHOT_BYTES, f"the first snapshot is {size} B"
    said = f"the first snapshot: {first.described(checkout / '.lup/questions.jsonl')}"
    assert snapshot_within_bounds(first, checkout / ".lup/questions.jsonl"), said
    assert first.seconds < SNAPSHOT_SECONDS, said


def test_a_fold_reading_the_log_again_for_each_question_exceeds_the_bound(
    checkout: Path, watch: OpenWatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    settled = QuestionRelay.settled

    def again(relay: QuestionRelay, question: str) -> RecordedQuestion | None:
        relay.refreshed()
        return settled(relay, question)

    monkeypatch.setattr(QuestionRelay, "settled", again)
    log = checkout / ".lup/questions.jsonl"

    fold = watch.measured(QuestionRelay(log, checkout / "answers.jsonl").questions)

    assert not fold_within_bounds(fold, log), fold.described(log)


def test_a_snapshot_carrying_a_row_for_every_review_exceeds_the_bound(
    checkout: Path, watch: OpenWatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LUP_REVIEW_ANSWERS", str(checkout / "answers"))
    store = ReviewStore(roots=(checkout,), recent=1000)

    first = watch.measured(store.snapshot)

    log = checkout / ".lup/questions.jsonl"
    assert not snapshot_within_bounds(first, log), first.described(log)
