"""Definitions are the unit a merge loss is actually about.

Line presence answers a different question badly in both directions, so
these pin the two failures that motivated a symbol pass: a rename that makes
untouched code read as lost, and a deletion whose body text survives
elsewhere and reads as kept.
"""

from lup.harness.codescan.symbols import defined_symbols, symbols_lost

MODULE = '''\
CONSTANT = 1


class Holder:
    """Doc."""

    field: int = 2

    def method(self) -> None:
        def nested() -> None:
            pass


async def top() -> None:
    pass
'''


def test_every_definition_is_found_qualified_by_its_scope() -> None:
    found = {symbol.name for symbol in defined_symbols(MODULE)}

    assert found == {
        "CONSTANT",
        "Holder",
        "Holder.field",
        "Holder.method",
        "Holder.method.nested",
        "top",
    }


def test_a_working_variable_is_not_a_definition() -> None:
    """A local is an implementation detail; losing one is not losing anything."""
    source = "def run() -> int:\n    total = 1\n    return total\n"

    assert [symbol.name for symbol in defined_symbols(source)] == ["run"]


def test_a_definition_carries_the_line_that_defines_it() -> None:
    lines = {symbol.name: symbol.line for symbol in defined_symbols(MODULE)}

    assert lines["Holder"] == 4
    assert lines["Holder.method"] == 9


def test_unparseable_text_defines_nothing() -> None:
    assert defined_symbols("def broken(:\n") == []
    assert defined_symbols("# not python at all\n<<<<<<< HEAD\n") == []


def test_a_rename_inside_a_body_loses_nothing() -> None:
    """What line presence gets wrong in the noisy direction."""
    before = "def keep() -> int:\n    total = 1\n    return total\n"
    after = "def keep() -> int:\n    amount = 1\n    return amount\n"

    assert symbols_lost(before, after) == []


def test_a_deleted_function_is_lost_even_when_its_body_survives() -> None:
    """What line presence gets wrong in the direction that matters."""
    before = "def gone() -> int:\n    return compute(1)\n"
    after = "def other() -> int:\n    return compute(1)\n"
    lost = symbols_lost(before, after)

    assert [symbol.name for symbol in lost] == ["gone"]


def test_a_method_is_qualified_so_a_namesake_does_not_excuse_it() -> None:
    before = "class A:\n    def run(self) -> None:\n        pass\n"
    after = (
        "class A:\n    pass\n\n\nclass B:\n    def run(self) -> None:\n        pass\n"
    )
    lost = symbols_lost(before, after)

    assert [symbol.name for symbol in lost] == ["A.run"]


def test_a_type_alias_is_a_definition_like_the_assignment_it_replaced() -> None:
    """`type X = ...` parses to its own node, so a walk over assignments misses it.

    The other spelling of the same declaration, `X = Literal[...]`, is an
    ordinary assignment. Two ways to write one thing are both tracked, so
    which one you wrote changes nothing.
    """
    source = "type Urgency = str\nNetworkMode = str\n"

    assert {symbol.name for symbol in defined_symbols(source)} == {
        "Urgency",
        "NetworkMode",
    }


def test_a_dropped_type_alias_is_reported_lost() -> None:
    """A lost alias is reported, in the direction this module exists for.

    Unread, a merge taking the side without the alias would report having
    lost nothing, leaving the rule against silently dropping code during
    conflict resolution no instrument for this whole class of declaration.
    """
    before = "type Verdict = str\n\n\ndef judge() -> None:\n    pass\n"
    after = "def judge() -> None:\n    pass\n"
    lost = symbols_lost(before, after)

    assert [symbol.name for symbol in lost] == ["Verdict"]


def test_a_type_alias_carries_the_line_that_defines_it() -> None:
    """Whoever is told one went missing has to find where it was."""
    lines = {
        symbol.name: symbol.line for symbol in defined_symbols("\n\ntype X = int\n")
    }

    assert lines["X"] == 3


def test_a_generic_type_alias_is_named_by_its_own_name() -> None:
    """`type Pair[T] = ...` binds `Pair`; `T` is a parameter, not a definition."""
    found = {symbol.name for symbol in defined_symbols("type Pair[T] = tuple[T, T]\n")}

    assert found == {"Pair"}
