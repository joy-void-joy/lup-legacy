"""What a later review round is still told, and why it has to be told it.

A reviewer's session is deliberately kept across rounds, so the second round
does not re-read the whole concern. The acceptance criteria are the exception:
the guard compares `criteria_met` to their exact ids, so a reviewer that
cannot read them cannot produce an acceptance the guard will honour — and a
round that fails for that reason fails identically however often it is
retried. On a resumed run whose reviewer session did not survive, a reviewer
reconstructs the ids from the concern's answered questions and has the
acceptance it argued for refused.
"""

import ast
from pathlib import Path

from lup.resolver.models import AcceptanceCriterion, Concern
from lup.resolver.turns import criteria_recital


def concern() -> Concern:
    return Concern(
        id="stale-base-guard",
        title="Refuse to plan against a base that moved",
        spec="Guard the base",
        criteria=[
            AcceptanceCriterion(id="sbg-1", description="Both launches report it"),
            AcceptanceCriterion(id="sbg-2", description="The check is shared"),
        ],
    )


def test_the_recital_names_every_criterion_by_the_id_it_is_judged_on() -> None:
    recited = criteria_recital(concern())

    assert "- sbg-1: Both launches report it" in recited
    assert "- sbg-2: The check is shared" in recited
    assert "criteria_met" in recited


def later_round_prompt() -> ast.If:
    """The `round_number > 1` branch of ``review_turn``, as source."""
    from lup.resolver import turns

    tree = ast.parse(Path(turns.__file__).read_text(encoding="utf-8"))
    branches = [
        node
        for function in ast.walk(tree)
        if isinstance(function, ast.AsyncFunctionDef) and function.name == "review_turn"
        for node in ast.walk(function)
        if isinstance(node, ast.If)
    ]
    assert branches, "review_turn no longer branches on the round"
    return branches[0]


def test_a_later_round_still_carries_the_criteria_it_judges_against() -> None:
    """The first round carries the whole concern; this round carries these."""
    called = [
        node.func.id
        for node in ast.walk(later_round_prompt())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]

    assert "criteria_recital" in called
