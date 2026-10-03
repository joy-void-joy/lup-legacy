"""The emitter writes Python, and Ruff has to accept what it wrote.

A generated data module is machine-written and drift-checked, so a formatter
disagreeing with it fails a gate on a file nobody edited and nobody can fix by
hand — the regeneration produces the same bytes again. A rule message holding
a double quote, escaped by JSON, is one Ruff wants in the single-quoted form.
So the quote choice is pinned here rather than left to coincide.
"""

import ast

import pytest

from lup.policy.bundle import python_literal


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # No quote either way: Ruff's configured default, which is double.
        ("plain prose", '"plain prose"'),
        # An apostrophe alone: double still escapes nothing, so double stays.
        ("the rule's subject", '"the rule\'s subject"'),
        # A double quote alone: single escapes nothing where double escapes
        # one, so single strictly reduces and wins.
        ('the .get("key") form', "'the .get(\"key\") form'"),
        # Both, with the double quotes ahead: the case a rule message carries.
        ('.get("k") is the rule\'s subject', "'.get(\"k\") is the rule\\'s subject'"),
        # Both, with the apostrophes ahead: single would escape more, so the
        # default holds rather than flipping on the mere presence of a quote.
        ("it's the bot's \"name\"", '"it\'s the bot\'s \\"name\\""'),
    ],
)
def test_the_quote_is_the_one_ruff_would_have_chosen(value: str, expected: str) -> None:
    assert python_literal(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "plain prose",
        "the rule's subject",
        '.get("key")',
        '.get("k") is the rule\'s subject',
        "an em dash — and a backslash \\ and a newline \n",
        "\ttab and \"both\" kinds of 'quote'",
    ],
)
def test_every_rendering_parses_back_to_the_value_it_came_from(value: str) -> None:
    """Quoting it differently must not change what it says."""
    assert ast.literal_eval(python_literal(value)) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "None"), (True, "True"), (False, "False"), (3, "3")],
)
def test_a_non_string_is_spelled_as_python_names_it(
    value: bool | int | None, expected: str
) -> None:
    """JSON's `null` and `true` are not Python names, and this writes Python."""
    assert python_literal(value) == expected
