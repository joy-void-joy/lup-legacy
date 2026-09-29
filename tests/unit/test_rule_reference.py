"""Generated Lup rule reference tests."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

import lup.devtools.dev.rules as rules
from lup.providers.harness import claude_prompt_renderer, codex_prompt_renderer
from lup.harness.codescan.antipatterns import (
    PYTHON_ANTI_PATTERNS,
    TS_ANTI_PATTERNS,
    RuleSet,
)
from lup.harness.codescan.common import Rule
from lup.harness.codescan.registry import CLEARED_SEPARATOR, all_rules
from lup.devtools.dev.rules import rule_reference_artifact, rule_reference_document
from lup_template.devtools.main import app


def test_printing_the_reference_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`dev rules` is read to see the table; a read does not rewrite the page."""
    written = Mock()
    monkeypatch.setattr(rules, "write_generated_file", written)

    result = CliRunner().invoke(app, ["dev", "rules"])

    assert result.exit_code == 0, result.output
    assert "`seam-boundary`" in result.output
    written.assert_not_called()


def test_checked_in_rule_reference_matches_canonical_objects() -> None:
    artifact = rule_reference_artifact()

    assert Path("docs/rules.md").read_text(encoding="utf-8") == artifact.content
    for rule in every_declared():
        assert f"`{rule.id}`" in artifact.content


def test_the_reference_names_no_runtime_it_is_rendered_through() -> None:
    """Which renderer writes this page is a non-choice, so it is checked as one.

    The document is prose and tables end to end. Pinning that both vocabularies
    produce the same bytes is what makes picking either honest, rather than a
    dependency nobody noticed the page had acquired.
    """
    document = rule_reference_document()

    assert claude_prompt_renderer().render(document) == (
        codex_prompt_renderer().render(document)
    )


def test_every_card_carries_the_strength_its_rule_declares() -> None:
    """The reference is where a denied contributor learns if a marker helps.

    A card built by listing fields drops the one nobody remembered to list,
    and this projection has already done that twice — so the declared strength
    and the rendered strength are compared rather than assumed equal.
    """
    declared = {rule.id: rule.strength for rule in every_declared()}
    cards = {rule.id: rule.strength for rule in all_rules()}

    for rule_id, strength in declared.items():
        assert cards[rule_id] == strength, rule_id


def test_a_refused_rule_is_rendered_as_refused() -> None:
    artifact = rule_reference_artifact()
    strong = [rule.id for rule in all_rules() if rule.strength == "strong"]

    assert strong, "the strength mechanism has no customer to render"
    for rule_id in strong:
        row = next(
            line
            for line in artifact.content.splitlines()
            if line.startswith(f"| `{rule_id}` |")
        )
        assert "**refused**" in row, rule_id


def test_registry_covers_every_family_with_unique_ids_and_homes() -> None:
    rules = all_rules()

    ids = [rule.id for rule in rules]
    assert len(ids) == len(dict.fromkeys(ids))
    assert {rule.family for rule in rules} == {
        "anti-pattern",
        "boundary",
        "spelling",
        "architecture",
    }
    assert all(rule.defined_in.startswith("lup.harness.codescan.") for rule in rules)


def every_declared() -> list[Rule]:
    """Every rule the set holds, whichever surface decides it."""
    held = RuleSet()
    return [
        *PYTHON_ANTI_PATTERNS,
        *TS_ANTI_PATTERNS,
        *held.project,
        *held.composition,
    ]


def test_no_card_shows_one_snippet_both_flagged_and_cleared() -> None:
    """A verdict that turns on where the code sits shows where.

    `front-door`, `seam-boundary` and `native-spelling` each declare one
    snippet flagged in a neutral module and cleared in an adapter, and the
    card rendered both as the same text: a rule contradicting itself.
    """
    for card in all_rules():
        cleared = card.cleared.split(CLEARED_SEPARATOR) if card.cleared else []
        assert card.example not in cleared, card.id

    spelling = next(card for card in all_rules() if card.id == "native-spelling")
    assert "providers/codex/harness.py" in spelling.cleared
