"""Where a `# lup: ignore` may sit, pinned identical across every rule family.

Left to each family, the answers diverge: a rule branching on our own model
types would declare the whole matched span, a capability rule its class header
on top of the member line, and a line rule nothing at all, which is inline-only.
One marker is then valid against one rule and spurious against another — and
what that reads like is a directive going spurious on its own line while the
violation it meant to guard stays missing.

Each case below is the same three placements against a different family: on
the violation's own line, standing alone directly above it, and one line
further up. The first two are accepted everywhere and the third nowhere, and
asserting that as one table is what keeps the answer from drifting apart.

The boundary family reads the same policy and is pinned beside its own rules
in ``tests/unit/test_boundaries.py`` rather than here, because its violations
are found by a scan this module does not call. It is the family most prone to
drift — a directive on a table's closing line is the case a per-rule zone
would accept — so where it is checked is worth knowing.
"""

from pathlib import Path

import pytest

from lup.harness.codescan.antipatterns import audit_text, patterns_for_suffix
from lup.harness.codescan.capabilities import audit_capabilities
from lup.harness.codescan.common import PythonSource
from lup.harness.codescan.dispatch import audit_own_model_dispatch
from lup.policy.kernel.edit import relocated_suppressions, suppression_reaches

DIRECTIVE = "# lup: ignore[{rule}] — pinned by the placement test"
PROJECT_RULES = ("abc-capability", "own-model-dispatch")


def project_findings(text: str, rule_id: str) -> list[str]:
    """Finding kinds one project-wide rule produces for a single module."""
    source = PythonSource(path=Path("sample.py"), module="sample", text=text)
    audit = (
        audit_capabilities
        if rule_id == "abc-capability"
        else (audit_own_model_dispatch)
    )
    return [finding.kind for finding in audit([source])]


def findings_for(text: str, rule_id: str) -> list[str]:
    """Every finding kind a text produces, from whichever scanner owns the rule."""
    if rule_id in PROJECT_RULES:
        return project_findings(text, rule_id)
    return [
        finding.kind
        for finding in audit_text(text, patterns_for_suffix(".py") or [])
        if finding.rule_id in ("", rule_id)
    ]


SPAN_RULE = "own-model-dispatch"
SPAN_BODY = (
    "from pydantic import BaseModel\n"
    "\n"
    "\n"
    "class Thing(BaseModel):\n"
    "    pass\n"
    "\n"
    "\n"
    "class Other(BaseModel):\n"
    "    pass\n"
    "\n"
    "\n"
    "type Either = Thing | Other\n"
    "\n"
    "\n"
    "def check(value: object) -> bool:\n"
)
SPAN_CALL = "    return isinstance(\n        value,\n        Thing,\n    )\n"

MEMBER_RULE = "abc-capability"
MEMBER_BODY = (
    "from abc import ABC, abstractmethod\n"
    "\n"
    "\n"
    "class Capability(ABC):\n"
    "    @abstractmethod\n"
    "    def act(self) -> None: ...\n"
    "\n"
)
MEMBER_DECLARATION = (
    "    @property\n    @abstractmethod\n    def value(self) -> int: ...\n"
)

LINE_RULE = "suppress"
LINE_BODY = "import contextlib\n\n\ndef run() -> None:\n"
LINE_STATEMENT = "    with contextlib.suppress(ValueError):\n        pass\n"


def indent_of(statement: str) -> str:
    """The leading whitespace a hoisted directive inherits from its subject."""
    first = statement.splitlines()[0]
    return first[: len(first) - len(first.lstrip())]


def inline(body: str, statement: str, rule: str) -> str:
    """The directive written on the violation's own line — the canonical form."""
    head, *rest = statement.splitlines(keepends=True)
    return f"{body}{head.rstrip()}  {DIRECTIVE.format(rule=rule)}\n{''.join(rest)}"


def above(body: str, statement: str, rule: str, gap: int = 0) -> str:
    """The directive standing alone `gap` lines further up than directly above."""
    return (
        f"{body}{indent_of(statement)}{DIRECTIVE.format(rule=rule)}\n"
        f"{'\n' * gap}{statement}"
    )


def heading_a_reason(body: str, statement: str, rule: str, lines: int = 1) -> str:
    """The directive heading its own reason, which runs on for `lines` more."""
    indent = indent_of(statement)
    continuation = "".join(
        f"{indent}# and the reason keeps going, line {number + 2} of it\n"
        for number in range(lines)
    )
    return f"{body}{indent}{DIRECTIVE.format(rule=rule)}\n{continuation}{statement}"


PLACEMENTS = [
    pytest.param(SPAN_RULE, SPAN_BODY, SPAN_CALL, id="span-declaring rule"),
    pytest.param(
        MEMBER_RULE, MEMBER_BODY, MEMBER_DECLARATION, id="one-extra-line rule"
    ),
    pytest.param(LINE_RULE, LINE_BODY, LINE_STATEMENT, id="rule declaring nothing"),
]


@pytest.mark.parametrize(("rule", "body", "statement"), PLACEMENTS)
def test_unsuppressed_violation_is_reported(
    rule: str, body: str, statement: str
) -> None:
    assert "missing" in findings_for(body + statement, rule)


@pytest.mark.parametrize(("rule", "body", "statement"), PLACEMENTS)
def test_directive_on_the_violation_line_covers_it(
    rule: str, body: str, statement: str
) -> None:
    assert findings_for(inline(body, statement, rule), rule) == []


@pytest.mark.parametrize(("rule", "body", "statement"), PLACEMENTS)
def test_directive_standing_directly_above_covers_it(
    rule: str, body: str, statement: str
) -> None:
    assert findings_for(above(body, statement, rule), rule) == []


@pytest.mark.parametrize(("rule", "body", "statement"), PLACEMENTS)
def test_a_reason_spanning_more_than_one_line_still_covers_it(
    rule: str, body: str, statement: str
) -> None:
    """The placement a reason too long for the column budget has to take.

    Every rule family, because they must agree: a family reading from a fixed
    pair of candidate lines cannot see a directive two lines up, while one
    deciding coverage through `suppression_reaches` accepts it — so one marker
    would be reported spurious and its violation missing, at once, which is
    the failure this whole placement policy exists to remove.
    """
    for length in (1, 2, 3):
        assert findings_for(heading_a_reason(body, statement, rule, length), rule) == []


@pytest.mark.parametrize(("rule", "body", "statement"), PLACEMENTS)
def test_directive_further_up_reaches_nothing(
    rule: str, body: str, statement: str
) -> None:
    kinds = findings_for(above(body, statement, rule, gap=1), rule)
    assert "missing" in kinds
    assert "spurious" in kinds


def test_refusal_names_both_lines_a_directive_may_sit_on() -> None:
    text = LINE_BODY + LINE_STATEMENT
    refusal = next(
        finding
        for finding in audit_text(text, patterns_for_suffix(".py") or [])
        if finding.kind == "missing"
    )
    assert (
        f"line {refusal.line}, or line {refusal.line - 1} directly above it"
        in refusal.message
    )


LONG_REASON = "a reason long enough that keeping it inline would outgrow the budget"
LONG_DIRECTIVE = f"# lup: ignore[{LINE_RULE}] — {LONG_REASON}"


def test_an_overflowing_inline_directive_is_hoisted_above_its_line() -> None:
    text = f"import contextlib\nx = contextlib.suppress  {LONG_DIRECTIVE}\n"
    assert relocated_suppressions(text) == (
        f"import contextlib\n{LONG_DIRECTIVE}\nx = contextlib.suppress\n"
    )


def test_hoisting_keeps_the_directive_covering_the_same_line() -> None:
    head, tail = LINE_STATEMENT.splitlines(keepends=True)
    text = f"{LINE_BODY}{head.rstrip()}  {LONG_DIRECTIVE}\n{tail}"
    assert findings_for(text, LINE_RULE) == []
    assert findings_for(relocated_suppressions(text), LINE_RULE) == []


def test_a_directive_that_fits_is_left_where_it_was_written() -> None:
    text = f"import contextlib\nx = contextlib.suppress  # lup: ignore[{LINE_RULE}]\n"
    assert relocated_suppressions(text) == text


SHORT_DIRECTIVE = f"# lup: ignore[{LINE_RULE}] — fits inline"
FOLDABLE = f"import contextlib\n{SHORT_DIRECTIVE}\nx = contextlib.suppress\n"


def test_an_above_line_directive_that_fits_is_folded_onto_its_line() -> None:
    # The shape the tree does not yet contain: an agent copying a nearby
    # above-line marker writes one whose reason fits, and only the fold
    # direction ever moves it.
    assert relocated_suppressions(FOLDABLE) == (
        f"import contextlib\nx = contextlib.suppress  {SHORT_DIRECTIVE}\n"
    )


def test_folding_keeps_the_directive_covering_the_same_line() -> None:
    head, tail = LINE_STATEMENT.splitlines(keepends=True)
    text = f"{LINE_BODY}    {SHORT_DIRECTIVE}\n{head}{tail}"
    assert findings_for(text, LINE_RULE) == []
    assert findings_for(relocated_suppressions(text), LINE_RULE) == []


@pytest.mark.parametrize(
    "text",
    [
        FOLDABLE,
        f"import contextlib\nx = contextlib.suppress  {LONG_DIRECTIVE}\n",
        f"import contextlib\nx = contextlib.suppress  {SHORT_DIRECTIVE}\n",
    ],
    ids=["folds", "hoists", "already canonical"],
)
def test_placement_is_a_normal_form(text: str) -> None:
    once = relocated_suppressions(text)
    assert relocated_suppressions(once) == once


def test_a_directive_is_not_folded_onto_a_line_that_carries_one() -> None:
    # Two directives on one line leaves the second unread.
    text = (
        f"import contextlib\n{SHORT_DIRECTIVE}\n"
        f"x = contextlib.suppress  # lup: ignore[{LINE_RULE}]\n"
    )
    assert relocated_suppressions(text) == text


def test_the_whole_file_directive_is_never_folded_onto_the_line_below() -> None:
    text = f"# lup: ignore[{LINE_RULE}]\nimport contextlib\nx = contextlib.suppress\n"
    assert relocated_suppressions(text) == text


def test_the_line_above_the_first_line_is_not_the_last_line_of_the_file() -> None:
    # Line 1 has nothing above it, and counting back from it must not wrap
    # onto the file's final line.
    lines = ["x = 1", SHORT_DIRECTIVE]
    assert not suppression_reaches(lines, 0, 1)


def test_a_hoist_into_the_header_block_is_declined() -> None:
    # Hoisting here would put the directive where the whole-file form is read,
    # turning one line's excuse into every line's.
    text = f"# a licence header\nimport contextlib  {LONG_DIRECTIVE}\n"
    assert relocated_suppressions(text) == text


def test_a_hoist_that_would_not_parse_is_declined() -> None:
    text = f"import contextlib\nx = 1 + \\\n    2  {LONG_DIRECTIVE}\n"
    assert relocated_suppressions(text) == text
