"""Literal marker text cannot shadow or authorize actual suppression comments."""

from pathlib import Path

import pytest

from lup.harness.codescan.antipatterns import (
    PYTHON_ANTI_PATTERNS,
    TS_ANTI_PATTERNS,
    audit_text,
)
from lup.harness.codescan.common import PythonSource
from lup.harness.codescan.project import directives_for, retired_suppressions
from lup.policy.kernel.edit import (
    antipattern_decision,
    relocated_suppressions,
    source_suppression,
)
from lup.policy.rules import antipattern_row


def python_rules():
    return [rule for rule in PYTHON_ANTI_PATTERNS if rule.id == "import-re"]


@pytest.mark.parametrize(
    "literal", ['"# lup: ignore[unrelated]"', '"`# lup: ignore[unrelated]`"']
)
def test_real_comment_after_literal_is_audited_and_asked(literal: str) -> None:
    text = f"import re; label = {literal}  # lup: ignore[import-re] — grammar\n"
    rules = python_rules()
    assert audit_text(text, rules) == []
    decision = antipattern_decision(
        None, text, [antipattern_row(rule) for rule in rules], True
    )
    assert decision is not None and decision.effect == "ask"
    assert "silences import-re" in decision.reason
    source = PythonSource(path=Path("sample.py"), module="sample", text=text)
    assert [directive.rule_ids for directive in directives_for(source)] == [
        {"import-re"}
    ]


def test_literal_alone_never_suppresses_or_counts_as_a_retired_directive() -> None:
    before = 'label = "# lup: ignore[import-re]"\n'
    after = "import re  # lup: ignore[import-re] — grammar\n"
    decision = antipattern_decision(
        before, after, [antipattern_row(rule) for rule in python_rules()], True
    )
    assert decision is not None and decision.effect == "ask"
    uncovered = 'import re; label = "# lup: ignore[import-re]"\n'
    decision = antipattern_decision(
        None, uncovered, [antipattern_row(rule) for rule in python_rules()], True
    )
    assert decision is not None and decision.effect == "deny"


def test_retiring_real_marker_preserves_literal_and_code() -> None:
    text = 'import re; label = "# lup: ignore[quoted]"  # lup: ignore[import-re] — grammar\n'
    source = PythonSource(path=Path("sample.py"), module="sample", text=text)
    result = retired_suppressions(source, "import-re")
    assert result.text == 'import re; label = "# lup: ignore[quoted]"\n'


def test_relocating_real_marker_preserves_literal_and_scope() -> None:
    statement = '    import re; label = "# lup: ignore[quoted]"'
    text = f"def parse():\n{statement}  # lup: ignore[import-re] — a reason that needs another line\n"
    placed = relocated_suppressions(text, limit=70)
    assert f"\n{statement}\n" in placed
    assert "    # lup: ignore[import-re]" in placed
    assert audit_text(placed, python_rules()) == []
    assert relocated_suppressions(placed, limit=70) == placed


def test_multiline_string_fake_directive_never_hides_a_real_comment() -> None:
    text = 'label = """\n# lup: ignore[import-re]\n"""\nimport re  # lup: ignore[import-re] — grammar\n'
    assert audit_text(text, python_rules()) == []
    source = PythonSource(path=Path("sample.py"), module="sample", text=text)
    assert [directive.line for directive in directives_for(source)] == [4]


def test_typescript_literal_does_not_shadow_its_real_comment() -> None:
    rule = next(rule for rule in TS_ANTI_PATTERNS if rule.strength == "soft")
    sample = next(
        example.code for example in rule.examples if example.verdict == "flagged"
    )
    # Put the literal before the source being audited, on that same source line.
    text = f'const label = "// lup: ignore[quoted]"; {sample} // lup: ignore[{rule.id}] — explicit reason\n'
    findings = audit_text(text, [rule], typescript=True)
    assert not [finding for finding in findings if finding.kind == "missing"]
    decision = antipattern_decision(
        None, text, [antipattern_row(rule)], False, typescript_source=True
    )
    assert decision is not None and decision.effect == "ask"


def test_known_comment_columns_ignore_marker_quoted_inside_comment_prose() -> None:
    line = "# explain `# lup: ignore[import-re]` here"
    assert source_suppression(line, 1, {1: 0}) is None
