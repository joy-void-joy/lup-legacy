"""Why each file of a review asks, in a few plain words read off its verdict.

A file's header says why it needs approval without opening anything, and a
proposal's context counts its files by that reason. The words come from the
gate that decided and the file's own facts — its line count, the
suppressions it introduces, the protected-path rule it matched — and never
from the reason sentence, which stays one look away.
"""

from collections import Counter
from hashlib import sha256
from pathlib import Path

import pytest

from lup.devtools.review.app import (
    FileReason,
    ReviewAttribution,
    ReviewFile,
    ReviewSuppression,
    file_reason,
)
from lup.policy.relay import CapturedFileReview, ProtectedMatch
from lup.policy.review import ReviewedFile


def asked(rule: str, protected: ProtectedMatch | None = None) -> ReviewAttribution:
    return ReviewAttribution(
        effect="ask",
        reason="a sentence nobody parses",
        rule=rule,
        rules=[rule],
        protected=protected,
    )


def written(lines: int) -> str:
    return "".join(f"line {number}\n" for number in range(lines))


POLICY = ProtectedMatch(
    kind="subtree",
    root="packages/lup/src/lup/policy",
    description="the policy's own code",
)
ASSETS = ProtectedMatch(
    kind="subtree",
    root="packages/lup/src/lup/providers/claude/assets",
    description="packages/lup/src/lup/providers/claude/assets",
)
DEVTOOLS = ProtectedMatch(kind="new_devtools", root="src", description="src")


@pytest.mark.parametrize(
    ("attribution", "words"),
    [
        (asked("edit:protected-path", POLICY), "protected: the policy's own code"),
        (
            asked("edit:protected-path", ASSETS),
            "protected: packages/lup/src/lup/providers/claude/assets",
        ),
        (asked("edit:protected-path", DEVTOOLS), "new devtools module"),
        (asked("edit:protected-path"), "protected"),
        (asked("edit:feedback-added"), "adds a # lup: note"),
        (asked("edit:claim-removed"), "removes a solved: claim"),
        (asked("edit:anti-pattern"), "a shape the rules refuse"),
        (asked("edit:size"), "a large edit, 40 lines added"),
        (asked("edit:destination-policy"), "edit:destination-policy"),
        (asked(""), "no verdict captured"),
        (ReviewAttribution(effect="allow", rule="edit:small-edit"), "automatic"),
        (ReviewAttribution(effect="defer", rule="edit:size"), "automatic"),
    ],
)
def test_a_file_s_words_come_from_its_gate_and_its_own_facts(
    attribution: ReviewAttribution, words: str
) -> None:
    assert file_reason(attribution, written(3), 40, []).words == words


def test_a_whole_write_counts_its_lines_and_an_exception_it_adds_is_named() -> None:
    assert file_reason(asked("edit:full-write"), written(66), 66, []) == FileReason(
        kind="written whole", words="written whole, 66 lines"
    )
    assert (
        file_reason(asked("edit:full-write"), "one\n", 1, []).words
        == "written whole, 1 line"
    )
    added = ReviewSuppression(
        line=4,
        rule_ids=["string-split"],
        reason="a format with no parser",
        introduced=True,
    )
    assert (
        file_reason(asked("edit:anti-pattern"), written(5), 1, [added]).words
        == "adds a rule suppression"
    )


def test_a_proposal_of_eleven_files_counts_three_reasons() -> None:
    files = [
        *[(asked("edit:protected-path", POLICY), written(10)) for _ in range(2)],
        *[(asked("edit:protected-path", ASSETS), written(10)) for _ in range(5)],
        (asked("edit:protected-path", DEVTOOLS), written(30)),
        *[(asked("edit:full-write"), written(lines)) for lines in (66, 132, 46)],
    ]
    reasons = [file_reason(attribution, after, 0, []) for attribution, after in files]
    assert Counter(reason.kind for reason in reasons) == {
        "protected": 7,
        "new devtools module": 1,
        "written whole": 3,
    }
    assert [reason.words for reason in reasons[-3:]] == [
        "written whole, 66 lines",
        "written whole, 132 lines",
        "written whole, 46 lines",
    ]


def test_a_reviewed_file_carries_its_words_from_the_captured_verdict() -> None:
    change = ReviewedFile(path=Path("/repo/a.py"), after=written(12))
    captured = CapturedFileReview(
        path=Path("/repo/a.py"),
        effect="ask",
        reason="/repo/a.py is written whole, 12 lines at once",
        rule="edit:full-write",
        rules=["edit:full-write"],
        before_sha256=None,
        after_sha256=sha256(written(12).encode()).hexdigest(),
        after=written(12),
    )
    shown = ReviewFile.of(change, [captured])
    assert shown.review_label.words == "written whole, 12 lines"
    assert shown.review_reason == "/repo/a.py is written whole, 12 lines at once"
