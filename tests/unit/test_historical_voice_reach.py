"""Where the historical-voice rule reaches, and the quoted shapes it leaves alone.

A sentence about how code came to be dates the moment it is read wherever it
is written: a test's docstring is read as the spec of what the test pins, a
passage renders into every page and prompt generated from it, and a script's
comment reads like any other. These pin each reach at the edit hook, which
is the generated dispatcher, and in the whole-file audit, along with what
the rule leaves alone: a phrase quoted in a code span or a fenced block,
another project's issue cited as `owner/repo#123`, a string in code, and a
changelog, whose subject is history.
"""

from pathlib import Path

import pytest

from lup.harness.codescan.antipatterns import (
    MARKDOWN_ANTI_PATTERNS,
    PYTHON_ANTI_PATTERNS,
    TS_ANTI_PATTERNS,
    audit_text,
)
from lup.harness.codescan.registry import all_rules
from tests.unit.test_claude_dispatcher import decide, edit_payload

TEST_FILE = "tests/unit/test_supervisor.py"
"""A test module the edits below land in, beside one of its own functions."""

TEST_ANCHOR = "def test_liveness_is_derived_from_activity_not_from_a_lock() -> None:"

PASSAGE = "packages/lup/src/lup/harness/content/docs/contributing.passage.md"
"""A passage the edits below append to, at its first line."""

SCRIPT = "packages/lup/web/src/supervisor/filters.ts"
"""A TypeScript module the edits below land in, above its first comment."""

SCRIPT_ANCHOR = "// What the trace shows and how it reads an entry"


def reason_of(decision: dict[str, object]) -> str:
    """The reason the dispatcher gave, or empty where it said nothing."""
    specific = (
        decision["hookSpecificOutput"] if "hookSpecificOutput" in decision else {}
    )
    assert isinstance(specific, dict)
    reason = (
        specific["permissionDecisionReason"]
        if "permissionDecisionReason" in specific
        else ""
    )
    return str(reason)


def first_line(path: str) -> str:
    """The opening line of a tracked file, which an edit anchors on."""
    return Path(path).read_text(encoding="utf-8").splitlines()[0]


@pytest.mark.parametrize(
    ("prose", "refused"),
    [
        ('    """The bug this fixes: a run read as live after it stopped."""', True),
        ('    """Issue #503: a stopped run reads as running."""', True),
        ('    """A stopped run reads as stopped, whatever its lock says."""', False),
        ("    # until now the fold answered for a run with no mailbox", True),
        ("    # the fold answers for nothing a mailbox holds", False),
    ],
    ids=["fix-told", "issue-number", "invariant", "comment-told", "comment-clean"],
)
def test_a_test_is_held_to_the_prose_rule(prose: str, refused: bool) -> None:
    """A test's docstring is the spec of what it pins, so history is refused there."""
    new = f"{TEST_ANCHOR}\n{prose}"
    decision = decide(edit_payload(TEST_FILE, TEST_ANCHOR, new, False))

    assert ("historical-voice" in reason_of(decision)) is refused


def test_a_test_is_held_to_no_rule_about_code() -> None:
    """Only the prose rule reaches a test: its subject is production's behaviour."""
    new = f"{TEST_ANCHOR}\n    held: Any = None"
    decision = decide(edit_payload(TEST_FILE, TEST_ANCHOR, new, False))

    assert "any-type" not in reason_of(decision)


@pytest.mark.parametrize(
    ("page", "refused"),
    [
        ("Measured in #202: two workers blocked on one question.", True),
        ("Until now no page said which home a launch selects.", True),
        ("Two workers block on one question.", False),
        ("A sentence saying `previously` is quoted, not said.", False),
        ("```\nthe home a launch selects, previously read elsewhere\n```", False),
        ("Codex drops the field (openai/codex#21639), so it is sent twice.", False),
    ],
    ids=["issue", "until-now", "clean", "code-span", "fenced", "tracker"],
)
def test_a_passage_is_held_to_the_prose_rule(page: str, refused: bool) -> None:
    """A passage renders into every page generated from it, so it is read there."""
    anchor = first_line(PASSAGE)
    decision = decide(edit_payload(PASSAGE, anchor, f"{page}\n\n{anchor}", False))

    assert ("historical-voice" in reason_of(decision)) is refused


@pytest.mark.parametrize(
    ("script", "refused"),
    [
        ("// the selection previously came from the URL", True),
        ("// the selection comes from the URL", False),
        ('const said = "previously";', False),
    ],
    ids=["comment-told", "comment-clean", "string"],
)
def test_a_script_comment_is_held_to_the_prose_rule(script: str, refused: bool) -> None:
    """A TypeScript comment is prose, and a string in its code is not."""
    new = f"{script}\n{SCRIPT_ANCHOR}"
    decision = decide(edit_payload(SCRIPT, SCRIPT_ANCHOR, new, False))

    assert ("historical-voice" in reason_of(decision)) is refused


def test_the_changelog_is_not_held_to_the_prose_rule() -> None:
    """History is a changelog's subject, so its role is data and no rule reaches it."""
    anchor = first_line("CHANGELOG.md")
    entry = "- The receipt previously read from the environment (#436) is gone."
    decision = decide(edit_payload("CHANGELOG.md", anchor, f"{anchor}\n{entry}", False))

    assert "historical-voice" not in reason_of(decision)


def test_the_audit_reads_a_docstring_for_prose_alone() -> None:
    """A docstring is prose to the prose rule, and nothing to a rule about code."""
    source = (
        "def gate() -> None:\n"
        '    """Previously this returned None, and Any was its type."""\n'
    )

    findings = audit_text(source, PYTHON_ANTI_PATTERNS)

    assert [(finding.rule_id, finding.line) for finding in findings] == [
        ("historical-voice", 2)
    ]


def test_the_audit_reads_a_page_around_the_code_it_quotes() -> None:
    page = (
        "The home a launch selects.\n"
        "A phrase like `previously` is quoted here.\n"
        "```\n"
        "# previously read from the environment\n"
        "```\n"
        "It was previously read from the environment.\n"
    )

    findings = audit_text(page, MARKDOWN_ANTI_PATTERNS, markdown=True)

    assert [(finding.rule_id, finding.line) for finding in findings] == [
        ("historical-voice", 6)
    ]


def test_the_audit_reads_a_scripts_comments_not_its_strings() -> None:
    script = 'const said = "previously";\n// the URL previously chose it\n'

    findings = audit_text(script, TS_ANTI_PATTERNS, typescript=True)

    assert [(finding.rule_id, finding.line) for finding in findings] == [
        ("historical-voice", 2)
    ]


def test_the_reference_lists_the_rule_once_for_every_language_it_reads() -> None:
    """One rule read three ways is one card, naming each scope."""
    cards = [card for card in all_rules() if card.id == "historical-voice"]

    assert len(cards) == 1
    assert cards[0].scope == "Python, TypeScript, Markdown"


def test_a_test_files_directive_for_a_rule_about_code_is_not_graded() -> None:
    """A test is held to the prose rule alone, so a code rule's marker there is not judged.

    The edit hook judges only the rules that reach a file, and the audit says
    the same: whether a directive for a rule about code guards anything is a
    question about a rule the test is not held to.
    """
    prose_rule = [
        rule for rule in PYTHON_ANTI_PATTERNS if rule.id == "historical-voice"
    ]
    source = "held = 1  # lup: ignore[any-type] — a fixture's own shape\n"

    assert audit_text(source, prose_rule, graded=["historical-voice"]) == []
    assert [finding.kind for finding in audit_text(source, prose_rule)] == ["spurious"]
