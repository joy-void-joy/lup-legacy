"""Project-wide AST enforcement against isinstance chains that want a match.

A single `isinstance` is a narrowing, and CLAUDE.md sanctions it: untyped data
arriving at a boundary has to be told apart somehow. Narrowing one subject
*again*, in a later arm of the same `if`/`elif` chain, is not narrowing — it is
a dispatch written in the older spelling, and `match` is what that spelling
compiles to. The translation is mechanical: each arm becomes a class pattern,
an `and` conjunct becomes the arm's guard, and the fallthrough becomes `case _`.

That totality is why the rule is ``strong``. A soft rule names a shape that is
occasionally the only thing that works, and grades the exceptions; no subject
makes a chain better than the match it compiles to, so a directive here would
record a decision to keep the older spelling rather than a reason to.

Firing on a repeat narrowing, rather than on the characters `isinstance`, is
what keeps that claim honest — and it is why this resolves the tree:

- **One subject.** Arms narrowing different values are not deciding between one
  value's types, and `match` cannot express them without tupling the subjects.
  `isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)` is one
  arm narrowing two subjects, not a dispatch.
- **`and` conjuncts, never `or`.** `isinstance(x, A) and guard(x)` becomes
  `case A() if guard(x)` intact. `isinstance(x, A) or other` does not: the arm
  is reachable by a value that is no `A` at all, and no class pattern says so.
- **Chains only.** `isinstance` in expression position — a comprehension
  filter, a ternary, a `not` guard clause — has no `match` spelling at all,
  because `match` is a statement. Those are not chains and are never reported.

`elif` and a lone `if` nested in an `else` are the same tree, so both are
caught, which a line regex looking for `elif isinstance` would miss.

One shape genuinely resists translation: `isinstance(x, TYPES)` where `TYPES`
holds a tuple at runtime, since a class pattern must name a class. It is
indistinguishable from a class reference in the syntax, and it appears nowhere
in this repository — and a runtime tuple used to *dispatch* wants its types
inlined regardless, which is the same remedy this rule already prints.
"""

import ast
from collections import Counter
from collections.abc import Iterator

from lup.harness.codescan.common import PythonSource, RuleExample
from lup.policy.kernel.edit import python_nodes, python_tree
from lup.harness.codescan.project import (
    ProjectRule,
    RuleFinding,
    RuleViolation,
    audit_suppressions,
)

# lup: ignore[constant-declaration] — the rule's own identity, what a typed
# directive and every deny message name it by
RULE_ID = "isinstance-chain"

# lup: ignore[constant-declaration] — Python's own builtin, named by the language
NARROWING_CALL = "isinstance"
"""The builtin whose repetition over one subject spells a dispatch."""

# lup: ignore[constant-declaration] — the rule's own sentence, declared with what
# it detects rather than chosen per caller
REMEDY = "write the match it compiles to, one case arm per type"


def narrowed_subjects(test: ast.expr) -> Iterator[str]:
    """Every subject an arm's test narrows, dumped for structural comparison.

    Each `and` conjunct is inspected, not just the first, because the order
    two narrowings are written in does not change what the arm decides. A
    disjunct is skipped: its arm survives without the type holding, so no
    class pattern stands in for it.
    """
    match test:
        case ast.Call(func=ast.Name(id=call), args=[subject, _, *_]) if (
            call == NARROWING_CALL
        ):
            yield ast.dump(subject)
        case ast.BoolOp(op=ast.And(), values=values):
            for value in values:
                yield from narrowed_subjects(value)


def chain_arms(head: ast.If) -> Iterator[ast.If]:
    """Every arm of one `if`/`elif` chain, head first.

    An `elif` and a lone `if` inside an `else` are indistinguishable in the
    tree, so following the sole-statement `orelse` walks both spellings.
    """
    yield head
    match head.orelse:
        case [ast.If() as tail]:
            yield from chain_arms(tail)


def source_violations(source: PythonSource) -> Iterator[RuleViolation]:
    """Every chain in one module that narrows a subject in more than one arm."""
    tree = python_tree(source.text)
    if tree is None:
        return

    # An `elif` is an `ast.If` in its own right, so walking every node would
    # report one chain once per arm. Only chain heads are reported.
    continuations = {
        id(arm)
        for node in python_nodes(tree)
        if isinstance(node, ast.If)
        for arm in chain_arms(node)
        if arm is not node
    }

    for node in python_nodes(tree):
        if not isinstance(node, ast.If) or id(node) in continuations:
            continue
        arms = list(chain_arms(node))
        # Deduplicated per arm, so one arm narrowing a subject twice counts
        # once: what makes a chain is a second *arm* deciding on the same value.
        deciding = Counter(
            subject
            for arm in arms
            for subject in dict.fromkeys(narrowed_subjects(arm.test))
        )
        repeated = [count for count in deciding.values() if count > 1]
        if not repeated:
            continue
        yield RuleViolation(
            path=source.path,
            line=node.lineno,
            message=(
                f"{max(repeated)} {NARROWING_CALL} arms decide between one "
                f"subject's types — {REMEDY}"
            ),
        )


def chain_violations(sources: list[PythonSource]) -> list[RuleViolation]:
    """Find every isinstance-chain violation across the supplied modules."""
    return [violation for source in sources for violation in source_violations(source)]


def audit_isinstance_chains(sources: list[PythonSource]) -> list[RuleFinding]:
    """Enforce the rule and report any directive written against it anyway."""
    return audit_suppressions(
        sources, chain_violations(sources), RULE_ID, strength="strong"
    )


CHAIN_RULE = ProjectRule(
    id=RULE_ID,
    family="architecture",
    scope="Python architecture",
    examples=[
        RuleExample(
            code="if isinstance(n, Name):\n    a()\nelif isinstance(n, Attribute):\n    b()",
            verdict="flagged",
        ),
        RuleExample(
            code="if isinstance(n, Name):\n    a()\nelse:\n    b()",
            verdict="cleared",
        ),
    ],
    message=(
        "Narrowing one subject again, in a later arm of the same if/elif chain, "
        "is a dispatch in the older spelling: each arm becomes a case pattern, an "
        "and conjunct becomes its guard, and the fallthrough becomes case _. A "
        "single narrowing is sanctioned and stays silent, as does isinstance in "
        "expression position, where match has no spelling at all."
    ),
    strength="strong",
    audit=lambda audited: audit_isinstance_chains(audited.judged_sources()),
)
"""The isinstance-chain rule, declared beside the audit that decides it."""
