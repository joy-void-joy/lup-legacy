"""The permissions page's settlement order is read off the order itself.

The page carried the kernel's names and the kernel's claims, written a second
time in a second file with nothing holding the two together — so a row moved,
added or dropped changed the precedence without changing the page that states
it, and a page confidently wrong about precedence is worse than one that had
to be looked up.

What that leaves worth pinning is the walk reading the real order, the id a
settled verdict cites reaching the page a reader takes it to, the claim being
each rule's own words rather than a paraphrase, and a rule with nothing to say
about itself failing generation rather than rendering blank.
"""

import inspect

import pytest

from lup.harness.content.docs.permissions import settlement_table
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.settlement import (
    SETTLEMENT_ORDER,
    SettlementFacts,
    SettlementRule,
)


class Undescribed(SettlementRule):
    __doc__ = None

    def reached(self, facts: SettlementFacts) -> KernelDecision | None:
        return None


def test_every_row_the_order_reads_gets_a_row_here() -> None:
    """Nothing left to keep in step: the rows are the order."""
    rows = settlement_table().rows

    assert [cell.text for _, cell, _ in rows] == [
        type(rule).__name__ for rule in SETTLEMENT_ORDER
    ]


def test_the_id_a_settled_verdict_cites_is_what_the_page_is_keyed_on() -> None:
    """A reader arrives with the id, so it is the column they arrive at.

    `unleased-write` and `contained-effects` reach a session in the verdict
    that cites them, and a kernel helper returns exactly these ids "for the
    reference the docs render" — which is this table, so it renders them.
    """
    rows = settlement_table().rows

    assert [cell.text for cell, _, _ in rows] == [rule.id for rule in SETTLEMENT_ORDER]


def test_a_row_says_what_its_own_docstring_says() -> None:
    """The claim is the rule's summary line, not a paraphrase of it."""
    for (_, _, claim), rule in zip(settlement_table().rows, SETTLEMENT_ORDER):
        docstring = type(rule).__doc__
        assert docstring is not None

        assert claim.text == inspect.cleandoc(docstring).splitlines()[0], type(
            rule
        ).__name__


def test_a_rule_with_nothing_to_say_fails_generation() -> None:
    """A row saying nothing must not inherit the seam's own description.

    ``inspect.getdoc`` walks up to the base class, and the base here is the
    seam every row implements — so the undescribed row would have rendered
    "one row of the settlement order" and read exactly like a described one.
    """
    with pytest.raises(ValueError, match="carries no docstring"):
        settlement_table([Undescribed()])
