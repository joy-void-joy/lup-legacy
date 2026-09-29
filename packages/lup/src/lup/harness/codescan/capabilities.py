# lup: ignore[set-shape, empty-collection, string-split]
# Graph sets over the shared symbol index are this AST rule's domain, and
# splitting a qualified name to its terminal symbol is how it names one.
"""Project-wide AST enforcement over what a class's base list declares.

Two rules read that list, which is why they are written together: change what
one takes a base to mean and the other is reading the same word.

``abc-capability`` audits every resolved project import for the shape
described in ``docs/architecture.md``: one narrow capability per ABC, no
concrete behavior, no multiple inheritance. Its scope is capability seams,
not every abstract base — a distinction ``docs/patterns.md`` already draws in
prose and this rule reads off the base list. A union whose subtypes answer for
themselves is Closed By Construction and is governed by ``own-model-dispatch``;
it declares ``BaseModel`` beside ``ABC`` and is not judged here.

``abstract-declaration`` is what makes that list worth reading. Pydantic's
metaclass is an ``ABCMeta``, so an ``@abstractmethod`` binds on a model and
the base becomes uninstantiable while the word ``ABC`` never appears — which
is how thirteen of this library's unions are abstract by an incidental
property of a dependency rather than by anything anybody wrote. Declaring an
abstract member and naming ``ABC`` are one act, so this rule requires the
second wherever it finds the first.

The ``lup-devtools dev check --antipatterns`` auditor runs
:func:`audit_capabilities` and :func:`audit_abstract_declarations` across the
tree; the anti-pattern registry re-exports both rule ids so deny messages
name them.

The tree reading, name resolution, and suppression grammar this rule works
from live in :mod:`lup.harness.codescan.project`, which it shares with
:mod:`lup.harness.codescan.dispatch`.
"""

from lup.harness.codescan.common import PythonSource, RuleExample
from lup.harness.codescan.project import (
    ClassSymbol,
    ProjectRule,
    RuleFinding,
    RuleViolation,
    audit_suppressions,
    descendants_of,
    project_index,
)

# lup: ignore[constant-declaration] — the rule's own identity, what a typed
# directive and every deny message name it by
RULE_ID = "abc-capability"

# lup: ignore[constant-declaration] — the rule's own identity, what a typed
# directive and every deny message name it by
ABSTRACT_DECLARATION_RULE_ID = "abstract-declaration"

MODEL_BASES = {"pydantic.BaseModel", "pydantic_settings.BaseSettings"}
"""Roots whose project-defined descendants are variant unions, not capabilities."""

ABC_BASES = {"abc.ABC", "ABC"}
"""Both spellings of the base that says a class is abstract."""

STRUCTURAL_BASES = {"typing.Protocol", "Protocol"}
"""Bases carrying their own abstractness, which ``ABC`` beside them would not
say anything further about: a protocol is satisfied structurally, so naming an
abstract base would make a nominal claim its implementers never register."""


def capability_names(
    symbols: dict[str, ClassSymbol],
    model_bases: set[str] = MODEL_BASES,
    abc_bases: set[str] = ABC_BASES,
) -> set[str]:
    """Find direct and invalid inherited capability ABC declarations.

    A subclass that leaves one of its base capability's abstract operations
    unimplemented is still an ABC even when it does not repeat the
    ``@abstractmethod`` decorator.  The project index therefore computes the
    effective abstract member set instead of looking only at methods declared
    in the subclass body.

    A class that also descends from ``pydantic.BaseModel`` is a variant union
    rather than a capability, and is excluded. The two shapes want opposite
    things from a base: a capability holds no concrete behaviour, because
    shared behaviour belongs on the surface that composes it; a union's base
    carries declining answers on purpose, because that is what lets a walk
    reach a kind written after it. Reading the base list is what tells them
    apart — ``BaseModel`` and ``ABC`` together say *closed set of kinds*,
    ``ABC`` alone says *seam an implementation fills*.
    """
    effective_cache: dict[str, set[str]] = {}

    def effective_abstracts(name: str, visiting: set[str] | None = None) -> set[str]:
        cached = effective_cache.get(name)
        if cached is not None:
            return cached
        if name not in symbols:
            return set()
        active = set() if visiting is None else set(visiting)
        if name in active:
            return set()
        active.add(name)
        symbol = symbols[name]
        inherited = {
            member
            for base in symbol.bases
            if base in symbols
            for member in effective_abstracts(base, active)
        }
        implemented = set(symbol.concrete_callables)
        declared = set(symbol.abstract_methods) | set(symbol.abstract_properties)
        result = (inherited - implemented) | declared
        effective_cache[name] = result
        return result

    unions = descendants_of(symbols, model_bases)
    capabilities = {
        name
        for name, symbol in symbols.items()
        if abc_bases & set(symbol.bases) and name not in unions
    }
    changed = True
    while changed:
        inherited = {
            name
            for name, symbol in symbols.items()
            if any(base in capabilities for base in symbol.bases)
            and effective_abstracts(name)
            and name not in unions
        }
        expanded = capabilities | inherited
        changed = expanded != capabilities
        capabilities = expanded
    return capabilities


def architecture_violations(
    symbols: dict[str, ClassSymbol], capabilities: set[str]
) -> list[RuleViolation]:
    """Evaluate every capability declaration and concrete implementation."""
    violations: list[RuleViolation] = []
    typing_bases = ABC_BASES | {"typing.Generic", "Generic"}

    def is_implementation(name: str, seen: set[str] | None = None) -> bool:
        if name in capabilities or name not in symbols:
            return False
        visited = set() if seen is None else seen
        if name in visited:
            return False
        visited.add(name)
        return any(
            base in capabilities or is_implementation(base, visited)
            for base in symbols[name].bases
        )

    for name, symbol in symbols.items():
        if name in capabilities:
            method_count = len(symbol.abstract_methods)
            if method_count == 0 or method_count > 3:
                violations.append(
                    RuleViolation(
                        path=symbol.path,
                        line=symbol.line,
                        message=(
                            f"capability {symbol.name} has {method_count} abstract behavior "
                            "methods; expected one to three"
                        ),
                    )
                )
            for member in symbol.abstract_properties:
                violations.append(
                    RuleViolation(
                        path=symbol.path,
                        line=symbol.member_lines[member],
                        message=f"capability {symbol.name} declares abstract property {member}",
                    )
                )
            for member in symbol.concrete_callables:
                violations.append(
                    RuleViolation(
                        path=symbol.path,
                        line=symbol.member_lines[member],
                        message=f"capability {symbol.name} has concrete callable {member}",
                    )
                )
            capability_bases = [base for base in symbol.bases if base in capabilities]
            if capability_bases:
                violations.append(
                    RuleViolation(
                        path=symbol.path,
                        line=symbol.line,
                        message=(
                            f"capability {symbol.name} inherits capability "
                            f"{capability_bases[0].rsplit('.', 1)[-1]}"
                        ),
                    )
                )
            invalid_bases = [
                base
                for base in symbol.bases
                if base not in typing_bases and base not in capabilities
            ]
            if invalid_bases:
                violations.append(
                    RuleViolation(
                        path=symbol.path,
                        line=symbol.line,
                        message=f"capability {symbol.name} inherits reusable behavior",
                    )
                )
            continue

        direct_capabilities = [base for base in symbol.bases if base in capabilities]
        inherited_implementations = [
            base for base in symbol.bases if is_implementation(base)
        ]
        if len(direct_capabilities) > 1:
            violations.append(
                RuleViolation(
                    path=symbol.path,
                    line=symbol.line,
                    message=f"implementation {symbol.name} implements multiple capabilities",
                )
            )
        if direct_capabilities and len(symbol.bases) > 1:
            violations.append(
                RuleViolation(
                    path=symbol.path,
                    line=symbol.line,
                    message=f"implementation {symbol.name} inherits reusable behavior",
                )
            )
        if inherited_implementations:
            violations.append(
                RuleViolation(
                    path=symbol.path,
                    line=symbol.line,
                    message=f"implementation {symbol.name} inherits an implementation",
                )
            )
    return violations


def audit_capabilities(
    sources: list[PythonSource], judged: list[PythonSource] | None = None
) -> list[RuleFinding]:
    """Build the project index, enforce the rule, and audit its suppressions.

    The index resolves through the library's classes as well, so a class
    descending from a library model reads as the variant union it is; what is
    reported is only what stands in the sources handed in, or in *judged*.
    """
    reported = sources if judged is None else judged
    symbols = project_index(sources)
    scanned = {source.path for source in reported}
    violations = [
        violation
        for violation in architecture_violations(symbols, capability_names(symbols))
        if violation.path in scanned
    ]
    return audit_suppressions(reported, violations, RULE_ID)


def undeclared_abstractions(
    symbols: dict[str, ClassSymbol],
    abc_bases: set[str] = ABC_BASES,
    structural_bases: set[str] = STRUCTURAL_BASES,
) -> list[RuleViolation]:
    """Every class whose own body declares an abstract member without ``ABC``.

    Read off the class's own body rather than its effective abstract set, so a
    subclass adding an abstract member states it too. Inheriting abstractness
    is exactly the inference this rule exists to stop standing in for a
    declaration — a reader asking whether a class can be constructed should
    not have to walk its hierarchy to find out, any more than they should have
    to know which metaclass pydantic uses.

    Reported at the class rather than at each member, because the remedy is one
    edit to the base list however many members prompted it.

    The index holds a module's top-level classes, so a class nested inside a
    function or another class is out of reach here as it is for every rule
    built on it.
    """
    return [
        RuleViolation(
            path=symbol.path,
            line=symbol.line,
            message=(
                f"{symbol.name} declares abstract {', '.join(declared)}, so it "
                "cannot be constructed — name ABC among its bases and say so"
            ),
        )
        for symbol in symbols.values()
        if (declared := sorted([*symbol.abstract_methods, *symbol.abstract_properties]))
        and not abc_bases & set(symbol.bases)
        and not structural_bases & set(symbol.bases)
    ]


def audit_abstract_declarations(
    sources: list[PythonSource], judged: list[PythonSource] | None = None
) -> list[RuleFinding]:
    """Build the project index, enforce the rule, and audit its suppressions."""
    reported = sources if judged is None else judged
    symbols = project_index(sources)
    scanned = {source.path for source in reported}
    violations = [
        violation
        for violation in undeclared_abstractions(symbols)
        if violation.path in scanned
    ]
    return audit_suppressions(reported, violations, ABSTRACT_DECLARATION_RULE_ID)


CAPABILITY_RULE = ProjectRule(
    id=RULE_ID,
    family="architecture",
    scope="Python architecture",
    examples=[
        RuleExample(
            code=(
                "from abc import ABC, abstractmethod\n"
                "class Reader(ABC):\n"
                "    @abstractmethod\n"
                "    def read(self) -> str: ...\n"
                "class Writer(ABC):\n"
                "    @abstractmethod\n"
                "    def write(self, text: str) -> None: ...\n"
                "class Combined(Reader, Writer):\n"
                "    def read(self) -> str: return ''\n"
                "    def write(self, text: str) -> None: ..."
            ),
            verdict="flagged",
        ),
        RuleExample(
            code=(
                "from abc import ABC, abstractmethod\n"
                "class Reader(ABC):\n"
                "    @abstractmethod\n"
                "    def read(self) -> str: ...\n"
                "class FileReader(Reader):\n"
                "    def read(self) -> str: return ''"
            ),
            verdict="cleared",
        ),
    ],
    message=(
        "Capability ABCs stay independently constructible and cohesive; "
        "implementations do not inherit multiple capabilities or reusable behavior. "
        "A class descending from a pydantic model is a variant union rather than "
        "a capability, and the base is read through the library's own classes as "
        "well as the project's, so a kind declared over a lup model is not judged "
        "a capability."
    ),
    audit=lambda audited: audit_capabilities(audited.sources, audited.judged_sources()),
)
"""The capability rule, as the set that runs it and the reference read it."""

ABSTRACT_DECLARATION_RULE = ProjectRule(
    id=ABSTRACT_DECLARATION_RULE_ID,
    family="architecture",
    scope="Python architecture",
    examples=[
        RuleExample(
            code=(
                "from abc import abstractmethod\n"
                "from pydantic import BaseModel\n"
                "class Part(BaseModel):\n"
                "    @abstractmethod\n"
                "    def render(self) -> str: ..."
            ),
            verdict="flagged",
        ),
        RuleExample(
            code=(
                "from abc import ABC, abstractmethod\n"
                "from pydantic import BaseModel\n"
                "class Part(BaseModel, ABC):\n"
                "    @abstractmethod\n"
                "    def render(self) -> str: ..."
            ),
            verdict="cleared",
        ),
    ],
    message=(
        "A class declaring an abstract member cannot be constructed, and its "
        "bases are where it says so. Pydantic's metaclass is an ABCMeta, so on "
        "a model the member binds and the class turns abstract while the word "
        "ABC never appears — leaving the fact readable only to whoever knows "
        "that about the dependency. Name ABC among the bases: nothing changes "
        "at runtime, and abc-capability reads the same list to tell a "
        "capability seam from a variant union. A Protocol is exempt, being "
        "satisfied structurally rather than by declaration."
    ),
    audit=lambda audited: audit_abstract_declarations(
        audited.sources, audited.judged_sources()
    ),
)
"""The abstract-declaration rule, declared beside the audit that decides it."""
