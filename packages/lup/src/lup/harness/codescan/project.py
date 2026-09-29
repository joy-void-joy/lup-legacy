# lup: ignore[dict-str-payload, set-shape, empty-collection, string-split]
# Symbol tables, graph sets, and qualified-name resolution are this engine's domain.
"""Typed AST engine shared by every project-wide Lup rule.

A line regex sees characters; the rules that need to tell one construct from a
look-alike need names. This module reads the tree once and answers the two
questions those rules ask: *what does this name resolve to* — through imports,
aliases, and relative packages, into a project-wide class index — and *is this
site suppressed*, through the same `# lup: ignore` grammar the line scanners
use.

`lup.harness.codescan.capabilities` and `lup.harness.codescan.dispatch` are its customers.
Both hand it :class:`PythonSource` values, read :func:`build_symbol_index` for
resolved class shapes, and hand their mechanical violations back to
:func:`audit_suppressions`, which classifies each one as covered, missing,
bare, or guarding nothing at all.
"""

import ast
from collections.abc import Callable, Set as AbstractSet
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

import lup
from lup.harness.codescan.common import (
    NO_APPLICATION,
    ApplicationRoots,
    ProjectRuleFamily,
    PythonContext,
    PythonSource,
    Rule,
    RuleStrength,
    file_level_ignore,
    ignore_rule_ids,
    module_name,
)
from lup.policy.imports import ImportBoundary
from lup.policy.kernel.edit import (
    python_tree,
    suppression_placement,
    suppression_reaches,
)

type FindingKind = Literal["missing", "untyped", "spurious"]
"""How a violation and the suppressions around it ended up related."""


class ClassSymbol(BaseModel, frozen=True):
    """Resolved class shape retained by the project-wide symbol index."""

    qualified_name: str
    module: str
    name: str
    path: Path
    line: int
    bases: list[str]
    abstract_methods: list[str] = []
    abstract_properties: list[str] = []
    concrete_callables: list[str] = []
    member_lines: dict[str, int] = {}


class RuleViolation(BaseModel, frozen=True):
    """One mechanical shape violation before suppression auditing.

    Where its suppression may sit is not a field, and deliberately so: a rule
    that could name its own accepted lines is a rule whose markers read
    differently from every other rule's, which is how one marker shape ends up
    valid here and spurious there. ``line`` is where the violation is, and
    :func:`~lup.policy.kernel.edit.suppression_reaches` decides the rest for
    every rule alike.
    """

    path: Path
    line: int
    message: str


class RuleFinding(BaseModel, frozen=True):
    """One missing, untyped, or spurious suppression verdict for a rule."""

    kind: FindingKind
    path: Path
    line: int
    message: str
    rule_id: str
    text: str = ""
    """The line the finding is about, where the rule kept it, so a report
    shows the site under the verdict; empty where a rule reports a
    declaration by its position alone."""


class Directive(BaseModel, frozen=True):
    """One actual comment suppression that may cover a rule's violations."""

    path: Path
    line: int
    rule_ids: set[str] | None
    file_level: bool = False


class AuditedProject(BaseModel, arbitrary_types_allowed=True):
    """Everything a rule read off the whole project is handed at once.

    ``sources`` is every production Python module, the whole tree rather than
    the files a caller is answerable for, because a class index or the names a
    library's callers can replace are properties of the project and a verdict
    scoped to a subset would differ from the sweep's. ``application`` is what
    the repository declared about itself — where it composes natively, what
    it generates — and ``boundaries`` the import ownership it holds, or
    ``None`` for the library's own.
    """

    sources: list[PythonSource]
    application: ApplicationRoots = NO_APPLICATION
    boundaries: list[ImportBoundary] | None = None


class ProjectRule(Rule):
    """A rule decided over the whole project, which only the sweep can run.

    ``audit`` is the rule: handed the project, it returns every missing,
    untyped and spurious suppression verdict under this rule's id, the same
    three kinds the line rules report, so one report holds both. The sweep
    runs every project rule the set holds, so declaring one is what wires it
    in — there is no list of calls to be missing from.

    ``family`` and ``scope`` are what the reference card shows; a line rule
    derives both from the table it sits in, a project rule says them.
    """

    family: ProjectRuleFamily
    scope: str
    audit: Callable[[AuditedProject], list[RuleFinding]]

    @property
    def defined_in(self) -> str:
        """The module holding the audit, which is where the rule is enforced."""
        return self.audit.__module__


def dotted_name(node: ast.expr) -> str | None:
    """Return a dotted syntax name without evaluating it."""
    match node:
        case ast.Name(id=name):
            return name
        case ast.Attribute(value=value, attr=attribute):
            parent = dotted_name(value)
            return f"{parent}.{attribute}" if parent is not None else None
        case ast.Subscript(value=value):
            return dotted_name(value)
    return None


def named_types(node: ast.expr) -> list[str]:
    """Every type name one expression names, without evaluating it.

    Handles the tuple and ``|`` spellings of "any of these types", so a
    narrowing call that checks several and an annotation that unions several
    both report each one. A subscript reports the container it names rather
    than its members: `list[TextPart]` is a list.
    """
    match node:
        case ast.Tuple(elts=elements):
            return [name for element in elements for name in named_types(element)]
        case ast.BinOp(left=left, op=ast.BitOr(), right=right):
            return [*named_types(left), *named_types(right)]
    name = dotted_name(node)
    return [] if name is None else [name]


def imported_names(tree: ast.Module, module: str) -> dict[str, str]:
    """Resolve local import aliases to absolute symbols or modules."""
    aliases: dict[str, str] = {}
    package = module.split(".")
    for node in tree.body:
        match node:
            case ast.Import(names=names):
                for item in names:
                    local = item.asname or item.name.split(".")[0]
                    aliases[local] = item.name if item.asname else local
            case ast.ImportFrom(module=target, names=names, level=level):
                if level:
                    prefix = package[:-level]
                    resolved_module = ".".join(
                        [*prefix, *([target] if target is not None else [])]
                    )
                else:
                    resolved_module = target or ""
                for item in names:
                    local = item.asname or item.name
                    aliases[local] = f"{resolved_module}.{item.name}"
    return aliases


def resolve_name(name: str, module: str, aliases: dict[str, str]) -> str:
    """Resolve one base/decorator name through imports and local classes."""
    head, separator, tail = name.partition(".")
    if head in aliases:
        resolved = aliases[head]
        return f"{resolved}.{tail}" if separator else resolved
    if separator:
        return name
    return f"{module}.{name}"


def has_decorator(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    expected: str,
    module: str,
    aliases: dict[str, str],
) -> bool:
    """Recognize a decorator by its resolved terminal symbol."""
    for decorator in node.decorator_list:
        name = dotted_name(decorator)
        if name is None:
            continue
        if resolve_name(name, module, aliases).rsplit(".", 1)[-1] == expected:
            return True
    return False


def declaration_line(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Where a member's declaration starts, decorators included.

    A decorated ``def`` opens at its first decorator, and that is the line a
    violation about the whole declaration belongs on. Reporting the ``def``
    would leave the accepted placement above it wedged between a decorator and
    the function it decorates — a legal comment nobody would write.
    """
    return min([node.lineno, *(item.lineno for item in node.decorator_list)])


def build_symbol_index(sources: list[PythonSource]) -> dict[str, ClassSymbol]:
    """Build import-resolved class symbols for all parseable supplied modules."""
    symbols: dict[str, ClassSymbol] = {}
    for source in sources:
        tree = python_tree(source.text)
        if tree is None:
            continue
        aliases = imported_names(tree, source.module)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            abstract_methods: list[str] = []
            abstract_properties: list[str] = []
            concrete_callables: list[str] = []
            member_lines: dict[str, int] = {}
            for member in node.body:
                if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                member_lines[member.name] = declaration_line(member)
                abstract = has_decorator(
                    member, "abstractmethod", source.module, aliases
                )
                property_member = has_decorator(
                    member, "property", source.module, aliases
                )
                match (abstract, property_member):
                    case (True, True):
                        bucket = abstract_properties
                    case (True, False):
                        bucket = abstract_methods
                    case _:
                        bucket = concrete_callables
                if member.name not in bucket:
                    bucket.append(member.name)
            bases = [
                resolve_name(name, source.module, aliases)
                for base in node.bases
                if (name := dotted_name(base)) is not None
            ]
            qualified = f"{source.module}.{node.name}"
            symbols[qualified] = ClassSymbol(
                qualified_name=qualified,
                module=source.module,
                name=node.name,
                path=source.path,
                line=node.lineno,
                bases=bases,
                abstract_methods=abstract_methods,
                abstract_properties=abstract_properties,
                concrete_callables=concrete_callables,
                member_lines=member_lines,
            )
    return symbols


@cache
def installed_library_sources() -> list[PythonSource]:
    """Every module of the installed ``lup`` package, read once per process.

    A project built on this library declares classes over its models -- a
    ledger kind over ``LedgerNode``, a part over ``PromptPart`` -- and a rule
    that resolves bases through the project's own files alone sees the
    library's class as a name that resolves to nothing. Measured downstream:
    a kind declared as ``class KnowledgeItem(LedgerNode, ABC)`` was reported
    as a capability inheriting reusable behaviour, because the one base that
    made it a variant union was outside the index. The library's sources are
    what say what its classes are, so they are read beside the project's.

    Read from wherever the package is installed -- an editable checkout or a
    wheel -- and cached, because three rules index the same tree in one run
    and the tree does not move during it.
    """
    root = Path(lup.__file__).resolve().parent
    return [
        PythonSource(
            path=file,
            module=module_name(Path(root.name) / file.relative_to(root)),
            text=file.read_text(encoding="utf-8"),
        )
        for file in sorted(root.rglob("*.py"))
        if "__pycache__" not in file.parts
    ]


@cache
def library_index() -> dict[str, ClassSymbol]:
    """The installed library's class index, built once per process.

    Three rules index the same tree on every scan, and a project's test suite
    scans hundreds of times; parsing three hundred modules for each would
    cost more than the rules themselves.
    """
    return build_symbol_index(installed_library_sources())


def project_index(sources: list[PythonSource]) -> dict[str, ClassSymbol]:
    """The class index a project rule resolves through: the library's, then its own.

    The project's symbols are laid over the library's, keyed by qualified
    name, which is the whole difference between this repository and one built
    on it: here every library module is also a scanned source and its own
    reading wins, there the library arrives from its installation and fills
    in what the project's files cannot say. Rules report only over the
    sources they were handed; what the library contributes is resolution,
    never a finding about the library.
    """
    return {**library_index(), **build_symbol_index(sources)}


def descendants_of(symbols: dict[str, ClassSymbol], ancestors: set[str]) -> set[str]:
    """Every indexed class whose resolved base chain reaches `ancestors`.

    Bases are matched on their resolved qualified name and on their terminal
    symbol, so a class reached through ``from pydantic import BaseModel`` and
    one reached through ``pydantic.BaseModel`` land in the same closure.
    """
    terminals = {name.rsplit(".", 1)[-1] for name in ancestors}
    reached = {
        name
        for name, symbol in symbols.items()
        if any(
            base in ancestors or base.rsplit(".", 1)[-1] in terminals
            for base in symbol.bases
        )
    }
    while True:
        expanded = reached | {
            name
            for name, symbol in symbols.items()
            if any(base in reached for base in symbol.bases)
        }
        if expanded == reached:
            return reached
        reached = expanded


class RetiredDirectives(BaseModel, frozen=True):
    """One file rewritten to stop naming a rule, and the lines that went."""

    text: str
    """The file's revised content, identical where the rule was never named."""

    removed: list[int] = []
    """Line numbers taken out, so a sweep can print what it deleted."""


def retired_suppressions(
    source: PythonSource, rule_id: str, at: AbstractSet[int] | None = None
) -> RetiredDirectives:
    """Take a retired rule out of every directive one file writes.

    Retiring a rule strands every directive naming it: the violation it
    covered stops existing, so the directive covers nothing and is reported
    as spurious. Doing that by hand across a tree is where a reason gets
    deleted along with the id that justified it, which is why the lines that
    go are reported rather than only the text — a caller prints them, and
    nothing is dropped silently.

    A directive naming other rules keeps them and loses only this id. One
    naming this rule alone goes entirely, and the comment lines carrying the
    rest of its reason go with it: a standalone directive heads its own
    block, which is the relationship :func:`suppression_reaches` reads from
    the other side, so prose left behind would be a sentence explaining a
    rule nothing runs any more. Prose written *above* a directive is not its
    reason and stays.

    ``at`` narrows the sweep to directives on the lines named, for the caller
    that is repairing individual findings rather than retiring a rule. The
    difference matters in one direction only: retiring a rule strands every
    directive naming it, while an audit finding says this *one* line does not
    trip it, and a file can hold both that dead directive and a live one
    naming the same rule two functions down. Unnarrowed, repairing the first
    would delete the second and turn a spurious finding into a missing one.

    An empty ``rule_id`` names the bare directive that guards nothing, which
    is the other thing the audit reports spurious and the other thing there
    is no id to strip from.

    A rewrite that would stop the file parsing is returned untouched — the
    same refusal the placement rewriter makes.
    """
    context = PythonContext.parse(source.text)
    lines = source.text.splitlines()

    def carries_directive(number: int) -> bool:
        """Whether a line opens a real comment holding a directive."""
        if number < 1 or number > len(lines):
            return False
        found = context.suppression_at(number, lines[number - 1])
        return found is not None and context.comment_at(number, found.start())

    def reason_below(number: int) -> int:
        """Comment lines under a standalone directive that finish its reason."""
        extra = 0
        while number + extra < len(lines):
            if not lines[number + extra].lstrip().startswith(("#", "//")):
                break
            if carries_directive(number + extra + 1):
                break
            extra += 1
        return extra

    removed: list[int] = []

    def kept():
        resume = 0
        for number, line in enumerate(lines, start=1):
            if number < resume:
                continue
            match = context.suppression_at(number, line)
            if match is None or not carries_directive(number):
                yield line
                continue
            spelled = match.group("ids") or ""
            named = [item.strip() for item in spelled.split(",") if item.strip()]
            if at is not None and number not in at:
                yield line
                continue
            # An empty `rule_id` is the bare directive, which names nothing to
            # strip and goes whole; anything else has to be named to be taken.
            if rule_id not in named if rule_id else named:
                yield line
                continue
            remaining = [item for item in named if item != rule_id]
            if remaining:
                kept_ids = ", ".join(remaining)
                head, tail = line[: match.start()], line[match.end() :]
                yield f"{head}# lup: ignore[{kept_ids}]{tail}"
                continue
            if line[: match.start()].strip():
                removed.append(number)
                yield line[: match.start()].rstrip()
                continue
            span = 1 + reason_below(number)
            removed.extend(range(number, number + span))
            resume = number + span

    revised = "\n".join(kept()) + ("\n" if source.text.endswith("\n") else "")
    try:
        ast.parse(revised)
    except SyntaxError:
        return RetiredDirectives(text=source.text)
    return RetiredDirectives(text=revised, removed=removed)


@cache
def directives_for(source: PythonSource) -> list[Directive]:
    """Collect actual inline and file-level suppression comments.

    Remembered per source because :func:`audit_suppressions` gathers every
    file's directives once for each rule it grades, and which comments a file
    carries is a property of the file rather than of the rule asking. The
    list is read and never rewritten, so the one instance is shared.
    """
    context = PythonContext.parse(source.text)
    file_ignore = file_level_ignore(source.text)
    directives: list[Directive] = []
    if file_ignore is not None:
        directives.append(
            Directive(
                path=source.path,
                line=file_ignore.line,
                rule_ids=file_ignore.rule_ids,
                file_level=True,
            )
        )
    for line_number, line in enumerate(source.text.splitlines(), start=1):
        if file_ignore is not None and line_number == file_ignore.line:
            continue
        match = context.suppression_at(line_number, line)
        if match is None or not context.comment_at(line_number, match.start()):
            continue
        directives.append(
            Directive(
                path=source.path,
                line=line_number,
                rule_ids=ignore_rule_ids(match),
            )
        )
    return directives


def audit_suppressions(
    sources: list[PythonSource],
    violations: list[RuleViolation],
    rule_id: str,
    strength: RuleStrength = "soft",
) -> list[RuleFinding]:
    """Pair a rule's violations with the suppressions written against it.

    A directive covers a violation from the violation's own line or from the
    line standing directly above it, and from nowhere else — the one policy
    :func:`~lup.policy.kernel.edit.suppression_reaches` holds for every rule,
    the line scanners, and the edit hook alike. A refused violation names both
    lines, so a marker written where nothing reads it says where to go.

    Each violation is covered, or reported "missing"; a bare `# lup: ignore`
    that covers one is reported "untyped" once, so the migration to typed
    directives stays visible; a directive naming `rule_id` that guards nothing
    is reported "spurious", so a rule cannot rot behind dead markers.

    A ``strong`` rule admits no directive at all: every violation is reported
    missing, and a directive written anyway covers nothing and so falls to the
    same spurious sweep. This mirrors what :func:`lup.harness.codescan.antipatterns.
    audit_text` does for the line rules, so both surfaces refuse alike.
    """
    directives = [
        directive for source in sources for directive in directives_for(source)
    ]
    lines_of = {source.path: source.text.splitlines() for source in sources}
    used: set[int] = set()
    untyped_reported: set[int] = set()
    findings: list[RuleFinding] = []
    refusal = " (no suppression: write the replacement)" if strength == "strong" else ""
    for violation in violations:
        # A refusal that does not say where the marker belongs is the reported
        # failure itself: a directive goes spurious on one line while the
        # violation stays missing on another, and neither message connects them.
        expected = (
            f" — suppress on {suppression_placement(violation.line)}"
            f": `# lup: ignore[{rule_id}] — <why>`"
        )
        covering = [
            (index, directive)
            for index, directive in enumerate(directives)
            if strength == "soft"
            and directive.path == violation.path
            and (
                directive.file_level
                or suppression_reaches(
                    lines_of[violation.path], directive.line, violation.line
                )
            )
            and (directive.rule_ids is None or rule_id in directive.rule_ids)
        ]
        if not covering:
            findings.append(
                RuleFinding(
                    kind="missing",
                    path=violation.path,
                    line=violation.line,
                    message=f"{violation.message}{refusal or expected}",
                    rule_id=rule_id,
                )
            )
            continue
        # Every directive that reaches the violation, not the first one read.
        # They all guard it, so grading only one leaves the rest answering for
        # nothing where they stand while the violation they cover is silent —
        # a site no edit can clear, because removing the marker called spurious
        # reports the violation instead.
        used.update(position for position, _ in covering)
        index, directive = covering[0]
        if directive.rule_ids is None and index not in untyped_reported:
            findings.append(
                RuleFinding(
                    kind="untyped",
                    path=directive.path,
                    line=directive.line,
                    message=(
                        f"bare suppression covers {rule_id}; use "
                        f"# lup: ignore[{rule_id}] with a reason"
                    ),
                    rule_id=rule_id,
                )
            )
            untyped_reported.add(index)
    for index, directive in enumerate(directives):
        if (
            index in used
            or directive.rule_ids is None
            or rule_id not in directive.rule_ids
        ):
            continue
        findings.append(
            RuleFinding(
                kind="spurious",
                path=directive.path,
                line=directive.line,
                message=f"suppression names {rule_id} but guards no violation",
                rule_id=rule_id,
            )
        )
    return sorted(
        findings, key=lambda item: (item.path.as_posix(), item.line, item.kind)
    )
