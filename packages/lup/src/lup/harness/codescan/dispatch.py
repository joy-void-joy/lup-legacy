# lup: ignore[set-shape, string-split]
# Resolved-name sets over the shared symbol index are this AST rule's domain,
# and splitting a qualified name to its terminal symbol is how it reports one.
"""Project-wide AST enforcement against dispatching on our own model types.

A union we declare answers questions through its members: the base names the
operation and each variant answers or declines it, so a new variant is one
class instead of an edit to every walk that would have to notice it. Branching
on the variant's *type* instead — `isinstance`, a `case ClassName()` arm, an
`assert_never` net — moves that knowledge out into the walks, where a filter
goes stale by omission the moment a variant is added.

Narrowing untyped data is the opposite case and stays legitimate: a vendor
payload, a `JsonValue`, an `ast` node are alternatives that are not ours to
give a method to. A line regex cannot tell the two apart, because they are the
same characters — the difference is only in what the named type *resolves to*.
This rule resolves it, through the project-wide class index in
:mod:`lup.harness.codescan.project`: it fires only when the matched type is a class
this repository defines that inherits `pydantic.BaseModel`, and stays silent
on every builtin, standard-library, and vendor type.

And only when that class has something to be dispatched over: sibling variants
under one project base, or a union it is written into beside another model. A
model with neither is one type, and a `case Capability(built=False)` arm over
it is a value test the class happens to be the subject of — the shape the
conventions prefer to an `if` chain — with no union base for the remedy to
move the operation onto. Measured in a project built on this one, where the
rule refused exactly that arm and offered a fix that could not be carried out.
"""

import ast
from collections import Counter
from collections.abc import Iterator

from lup.harness.codescan.common import PythonSource, RuleExample
from lup.policy.kernel.edit import python_nodes, python_tree
from lup.harness.codescan.project import (
    ClassSymbol,
    ProjectRule,
    RuleFinding,
    RuleViolation,
    audit_suppressions,
    descendants_of,
    dotted_name,
    imported_names,
    named_types,
    project_index,
    resolve_name,
)

# lup: ignore[constant-declaration] — the rule's own identity, what a typed
# directive and every deny message name it by
RULE_ID = "own-model-dispatch"

MODEL_BASES = {"pydantic.BaseModel"}
"""Roots whose project-defined descendants count as models we declare."""

# lup: ignore[library-default] — Python's own narrowing builtins; the value follows the language, not a project's taste
NARROWING_CALLS = {"isinstance", "issubclass"}
"""Builtins that branch on a runtime type rather than asking the value."""

# lup: ignore[constant-declaration] — typing's own name for it, not ours
EXHAUSTIVENESS_CALL = "assert_never"
"""The static net that only exists to catch a union gaining a member."""

# lup: ignore[library-default] — typing's own spellings of a union; the value follows the language, not a project's taste
UNION_SPELLINGS = {"typing.Union", "Union"}
"""The subscript form of "any of these", beside the ``|`` the language spells."""

# lup: ignore[constant-declaration] — the rule's own sentence, declared with what
# it detects rather than chosen per caller
REMEDY = (
    "declare the operation on the union's base and let each variant answer or "
    "decline it"
)


def dispatch_violations(
    sources: list[PythonSource], models: set[str]
) -> list[RuleViolation]:
    """Find every site that branches on a project model's own type."""

    def found() -> Iterator[RuleViolation]:
        for source in sources:
            tree = python_tree(source.text)
            if tree is None:
                continue
            aliases = imported_names(tree, source.module)

            def reported(node: ast.expr | ast.pattern, message: str) -> RuleViolation:
                return RuleViolation(
                    path=source.path, line=node.lineno, message=message
                )

            def matched_model(name: str) -> str | None:
                resolved = resolve_name(name, source.module, aliases)
                return resolved.rsplit(".", 1)[-1] if resolved in models else None

            for node in python_nodes(tree):
                match node:
                    case ast.Call(func=ast.Name(id=call), args=[_, checked, *_]) if (
                        call in NARROWING_CALLS
                    ):
                        for name in named_types(checked):
                            if (model := matched_model(name)) is not None:
                                yield reported(
                                    node,
                                    f"{call} branches on {model}, a model we declare "
                                    f"— {REMEDY}",
                                )
                    case ast.MatchClass(cls=pattern):
                        name = dotted_name(pattern)
                        if (
                            name is not None
                            and (model := matched_model(name)) is not None
                        ):
                            yield reported(
                                node,
                                f"case arm matches {model}, a model we declare "
                                f"— {REMEDY}",
                            )
                    case ast.Call(func=ast.Name(id=call)) if (
                        call == EXHAUSTIVENESS_CALL
                    ):
                        yield reported(
                            node,
                            f"{EXHAUSTIVENESS_CALL} nets a union we declare — a base "
                            "that declines by default leaves nothing to be exhaustive "
                            "about",
                        )

    return list(found())


def dispatched_models(
    sources: list[PythonSource], symbols: dict[str, ClassSymbol], models: set[str]
) -> set[str]:
    """The models a branch on the type of could go stale: those in a set.

    Two shapes put a model in a set a walk can filter by omission. Sibling
    variants under one project base are the discriminated union the rule
    describes; a union written beside another model, as an alias or inline in
    an annotation, is the same set spelled without a base. A model in neither
    has no variant a later author could add and no base the remedy could name,
    so a branch on it is cleared whether or not the arm carries field
    patterns. The base of a family is not itself a variant: matching it
    narrows to the whole family, which a new member joins rather than escapes.
    """
    # Each declared class's children, counted in one pass over the index:
    # asking every class about every other was quadratic in a tree of
    # thousands, and the slowest thing the rule did.
    children = Counter(
        base
        for symbol in symbols.values()
        for base in set(symbol.bases)
        if base in symbols
    )
    with_siblings = {
        name
        for name in models
        if any(children[base] > 1 for base in symbols[name].bases)
    }

    def unioned() -> Iterator[str]:
        for source in sources:
            tree = python_tree(source.text)
            if tree is None:
                continue
            aliases = imported_names(tree, source.module)
            for node in python_nodes(tree):
                match node:
                    case ast.BinOp(op=ast.BitOr()):
                        spelled = named_types(node)
                    case ast.Subscript(value=value, slice=ast.Tuple() as members) if (
                        union := dotted_name(value)
                    ) is not None and resolve_name(
                        union, source.module, aliases
                    ) in UNION_SPELLINGS:
                        spelled = named_types(members)
                    case _:
                        continue
                declared = [
                    resolved
                    for name in spelled
                    if (resolved := resolve_name(name, source.module, aliases))
                    in models
                ]
                if len(declared) > 1:
                    yield from declared

    return with_siblings | set(unioned())


def audit_own_model_dispatch(sources: list[PythonSource]) -> list[RuleFinding]:
    """Build the project index, enforce the rule, and audit its suppressions.

    The index resolves through the library's classes as well, so a walk in a
    project built on this one that branches on a library variant -- a
    ``TextPart`` among the parts -- is reported there as it is here.
    """
    symbols = project_index(sources)
    models = descendants_of(symbols, MODEL_BASES)
    violations = dispatch_violations(
        sources, dispatched_models(sources, symbols, models)
    )
    return audit_suppressions(sources, violations, RULE_ID)


DISPATCH_RULE = ProjectRule(
    id=RULE_ID,
    family="architecture",
    scope="Python architecture",
    examples=[
        RuleExample(
            code=(
                "from pydantic import BaseModel\n"
                "class Part(BaseModel): ...\n"
                "class TextPart(Part):\n"
                "    text: str\n"
                "class ImagePart(Part):\n"
                "    url: str\n"
                "def render(part: Part) -> str:\n"
                "    if isinstance(part, TextPart):\n"
                "        return part.text\n"
                "    return ''"
            ),
            verdict="flagged",
        ),
        RuleExample(
            code=(
                "from pydantic import BaseModel\n"
                "class Capability(BaseModel):\n"
                "    built: bool\n"
                "def describe(capability: Capability) -> str:\n"
                "    match capability:\n"
                "        case Capability(built=False):\n"
                "            return 'pending'\n"
                "    return 'built'"
            ),
            verdict="cleared",
        ),
    ],
    message=(
        "A union we declare answers through its members: the base names the "
        "operation and each variant answers or declines it. Branching on the "
        "variant's own type — isinstance, a case arm, an assert_never net — "
        "leaves a filter that goes stale the moment a variant is added. "
        "Narrowing untyped data at a boundary is the different case and is "
        "not reported: the rule fires only on project classes that inherit "
        "pydantic.BaseModel and have something to be dispatched over — "
        "sibling variants under one project base, or a union they are "
        "written into beside another model. A model with neither is one "
        "type, and a case arm over it is a value test with no union base "
        "for the remedy to name."
    ),
    audit=lambda audited: audit_own_model_dispatch(audited.sources),
)
"""The own-model-dispatch rule, declared beside the audit that decides it."""
