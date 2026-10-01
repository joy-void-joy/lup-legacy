"""Taking a library table as offered, and saying only what differs from it.

The three tables this generalizes had the same defect in three shapes: the
only way to disagree with one entry was to restate the table around it. What
is pinned here is the resolution itself — that retiring, replacing, and adding
compose the way a reader of one short declaration would expect, and that a
project which says nothing gets the library's table unchanged.
"""

from lup.policy.edit_rules import EditRule
from lup.policy.kernel.decision import DecisionEffect
from lup.seams import Selection


def rule(name: str, effect: DecisionEffect = "allow") -> EditRule:
    """One rule that decides, named so a selection can reach it."""
    return EditRule(name=name, effect=effect, reason=f"{name} decided")


DEFAULTS = [rule("first"), rule("second"), rule("third")]


def names(rules: list[EditRule]) -> list[str]:
    return [item.name for item in rules]


def test_an_empty_selection_is_the_library_table() -> None:
    """What a project that selects nothing gets: the library's table whole."""
    assert names(Selection[EditRule]().over(DEFAULTS)) == ["first", "second", "third"]


def test_retiring_drops_one_rule_and_keeps_the_rest() -> None:
    selection = Selection[EditRule](retired=["second"])

    assert names(selection.over(DEFAULTS)) == ["first", "third"]


def test_an_override_replaces_the_library_rule_of_its_name() -> None:
    """Two rules under one name is the ambiguity this exists to remove."""
    mine = rule("second", effect="deny")
    resolved = Selection[EditRule](overrides=[mine]).over(DEFAULTS)

    assert names(resolved) == ["first", "third", "second"]
    assert resolved[-1].effect == "deny"
    assert sum(1 for item in resolved if item.name == "second") == 1


def test_a_new_rule_follows_the_library_table() -> None:
    """Order is the semantics for a last-match table, so additions come last."""
    resolved = Selection[EditRule](overrides=[rule("fourth")]).over(DEFAULTS)

    assert names(resolved) == ["first", "second", "third", "fourth"]


def test_retiring_and_overriding_compose() -> None:
    selection = Selection[EditRule](
        retired=["first"], overrides=[rule("third", effect="ask"), rule("fourth")]
    )

    assert names(selection.over(DEFAULTS)) == ["second", "third", "fourth"]


def test_overrides_keep_the_order_they_were_declared_in() -> None:
    selection = Selection[EditRule](overrides=[rule("z"), rule("a")])

    assert names(selection.over([])) == ["z", "a"]
