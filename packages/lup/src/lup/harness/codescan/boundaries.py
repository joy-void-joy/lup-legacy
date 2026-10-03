# lup: ignore[native-spelling]
# This checker necessarily owns the provider spellings it audits.
"""AST boundary rules keeping the library reusable by someone else's project.

Two directions are guarded. *Inward*: named adapter packages, tests, and
explicit application/CLI composition roots may import concrete implementations
and spell provider wire names, while every other module composes only the
portable contracts. *Outward*: a library module may not decide for its
adopters — a declared data table has to be reachable as an overridable
default, so an adopter replaces the vocabulary rather than editing the library
(``library-default``, the mechanical half of the placement criterion in
``docs/library.md``).

The package root faces the other way from every module behind it: it
re-exports the library's public names for its users, so ``front-door`` holds
that nothing inside the library imports from it, and the dependency runs from
the front door inward only.

The same criterion holds beyond the library, where the choice is frozen for
this project's own callers rather than for an adopter, so ``constant-declaration``
judges every other module-level constant by it. The two read one enumeration
and :meth:`ConstantDeclaration.judging_rule` hands each declaration to exactly
one of them, so neither can reach a line the other owns.

A deliberate exception uses the typed ``# lup: ignore[<rule-id>]`` on the
offending line or as a file-level directive, with a reason.
"""

import ast
from collections import deque
from collections.abc import Collection, Iterator, Sequence
from enum import StrEnum
from functools import cache
from pathlib import Path, PurePosixPath
from typing import get_args

from pydantic import BaseModel

from lup.harness.codescan.common import (
    ApplicationRoots,
    LIBRARY_PACKAGE_ROOT,
    NO_APPLICATION,
    PythonContext,
    PythonSource,
    RuleExample,
    file_level_ignore,
    ignore_rule_ids,
)
from lup.harness.codescan.project import (
    AuditedProject,
    FindingKind,
    ProjectRule,
    RuleFinding,
    RuleViolation,
    audit_suppressions,
)
from lup.policy.kernel.edit import (
    python_nodes,
    python_tree,
    suppression_placement,
    suppression_reaches,
)
from lup.harness.contracts import NativeSpellings
from lup.harness.models import PluginLocation, TreeLocation
from lup.policy.kernel.decision import KERNEL_IMPORT_ALLOWLIST
from lup.policy.imports import ImportBoundary
from lup.policy.kernel.imports import import_violations as native_import_violations


class RuleId(StrEnum):
    """Every rule the audits in this module report under, by its own name.

    A rule id is the rule's identity rather than a judgement a caller could
    make differently: it is what a typed directive, a deny message and the
    generated reference all name it by, and a caller passing a different one
    would be silencing a rule nobody can spell. Elsewhere in this package that
    is one ``RULE_ID`` beside the one audit that reports it; here several
    audits share a module, and a constant apiece said the same thing as many
    times in as many suppression comments. The type says it once.
    """

    SEAM = "seam-boundary"
    NATIVE_SPELLING = "native-spelling"
    KERNEL_IMPORTS = "kernel-imports"
    LIBRARY_DEFAULT = "library-default"
    CONSTANT_DECLARATION = "constant-declaration"
    ASSEMBLY = "assembly-boundary"
    FRONT_DOOR = "front-door"


# lup: ignore[constant-declaration] — where this library's own sources sit in the
# repository that publishes it, which is a fact about the distribution
LIBRARY_ROOT = "packages/lup/src/lup/"
KERNEL_ROOT = f"{LIBRARY_ROOT}policy/kernel/"
"""Where the decision kernel ships, derived so it cannot drift from the root."""
# lup: ignore[library-default] — the adapter packages this library ships, so the value follows lup.providers
NATIVE_PREFIXES = ("lup.providers.claude", "lup.providers.codex")
# lup: ignore[library-default] — published provider SDK import namespaces
SDK_PREFIXES = ("claude_agent_sdk", "anthropic", "openai")
# lup: ignore[constant-declaration] — the diagnostic for the seam-boundary rule
IMPORT_BOUNDARY_MESSAGE = (
    "Concrete adapter imports belong in providers or declared composition roots; "
    "provider SDK imports belong in adapter implementations. Use Lup contracts "
    "in shared code; canonical tool grants and provider mentions remain valid."
)
# lup: ignore[library-default] — the library's own modules, whose whole subject is
# one tool group, so the list follows what `lup.tools.toolsets` assembles
ASSEMBLED_TOOL_MODULES = ("lup.coordination.peer_tools", "lup.ledger.tools")
"""The tool-group constructors a declared group is built out of.

Both are a group and nothing else: the roster's verbs with the pulse that
beats beside them, and the ledger's verbs bound to a project's own kinds. A
module with a second surface stays off this list — `lup.tools.lsp.tools` also
renders the tool reference a document reads — because ownership here is by
module and a boundary that refused a doc's import would be refusing the wrong
thing.
"""

ASSEMBLY_ROOT = f"{LIBRARY_ROOT}tools/toolsets.py"
"""Where a session's groups are assembled, and so the one caller of those."""

FRONT_DOOR_PATH = f"{LIBRARY_ROOT}__init__.py"
"""The package root: the one library module that may re-export from the rest."""

# lup: ignore[constant-declaration] — the diagnostic for the assembly-boundary rule
ASSEMBLY_BOUNDARY_MESSAGE = (
    "A tool group's constructor is the assembly's to call: name the group in "
    "this project's toolset declaration instead of building it. Calling it "
    "directly is how a project comes to hold a copy of the wiring — a "
    "companion that was never started, a signature that gained parameters — "
    "which the next dependency bump lands the other half of."
)
# lup: ignore[library-default] — each key is literally what the provider calls the thing
NATIVE_SPELLINGS = {
    "/lup:": "Claude skill invocation",
    "$lup:": "Codex skill invocation",
    "CLAUDE_CONFIG_DIR": "Claude configuration environment",
    "CODEX_HOME": "Codex configuration environment",
    ".claude-plugin": "Claude plugin manifest path",
    ".codex-plugin": "Codex plugin manifest path",
    "PreToolUse": "native hook event",
    "PermissionRequest": "native hook event",
    "PostToolUse": "native hook event",
    "SessionStart": "native hook event",
    "thread/start": "Codex app-server method",
    "thread/resume": "Codex app-server method",
    "thread/fork": "Codex app-server method",
    "turn/start": "Codex app-server method",
    "turn/steer": "Codex app-server method",
    "turn/interrupt": "Codex app-server method",
    "account/rateLimits/read": "Codex app-server method",
    "account/usage/read": "Codex app-server method",
}


def generated_tree_paths(
    runtimes: Sequence[NativeSpellings], plugins: Sequence[str]
) -> list[str]:
    """Every path a runtime writes its own tree at, asked rather than listed.

    A generated tree is the rendering of exactly the implementations this rule
    guards, so what sanctions it is that a runtime spells it — and a runtime
    that learns a location sanctions it the same day, with no second copy here
    to keep in step.
    """
    return sorted(
        {
            *(
                runtime.tree(location)
                for runtime in runtimes
                for location in get_args(TreeLocation.__value__)
            ),
            *(
                runtime.plugin(plugin, location, None)
                for runtime in runtimes
                for plugin in plugins
                for location in get_args(PluginLocation.__value__)
            ),
        }
    )


def native_import_boundaries(
    application: ApplicationRoots = NO_APPLICATION,
) -> list[ImportBoundary]:
    """Compile one ownership declaration for audits and both native hooks."""
    providers = f"{LIBRARY_ROOT}providers/"
    sources = [f"{Path(LIBRARY_ROOT).parent.as_posix()}/", *application.source_roots]
    return [
        ImportBoundary(
            modules=list(NATIVE_PREFIXES),
            owners=[providers, *LIBRARY_COMPOSITION, *application.composition],
            source_roots=sources,
            rule_id=RuleId.SEAM,
            message=IMPORT_BOUNDARY_MESSAGE,
        ),
        ImportBoundary(
            modules=list(SDK_PREFIXES),
            owners=[
                providers,
                *application.generated,
                *application.native_dependencies,
            ],
            source_roots=sources,
            rule_id=RuleId.SEAM,
            message=IMPORT_BOUNDARY_MESSAGE,
        ),
        ImportBoundary(
            modules=list(ASSEMBLED_TOOL_MODULES),
            owners=[ASSEMBLY_ROOT],
            source_roots=sources,
            rule_id=RuleId.ASSEMBLY,
            message=ASSEMBLY_BOUNDARY_MESSAGE,
        ),
    ]


# lup: ignore[library-default] — files of this library, which no adopter relocates
LIBRARY_COMPOSITION = (
    FRONT_DOOR_PATH,
    f"{LIBRARY_ROOT}devtools/harness/composition.py",
    f"{LIBRARY_ROOT}devtools/harness/launch.py",
    f"{LIBRARY_ROOT}devtools/harness/resolve.py",
)
"""The library's own composition roots.

A launcher starts a named runtime, the resolver entry drives one, and the
composition builders assemble one, so all three compose concrete adapters the
way an application's own root does. They are listed rather than sanctioned by
directory: the engines beside them — generation, drift, reconciliation — read
a declaration and must stay portable.

The first is the package's front door, and it is why this rule is worth
having. Something has to name an adapter or nothing is constructible at all,
and were nothing here to name one, the naming would happen in every example
instead — the corpus that teaches the library doing at tier 4 what this rule
fails the build over everywhere else. Sanctioning the root is what confines
that to one file a reviewer can read. It defers the import into the call that
needs it, so naming a constructor is what reaches an adapter and `import lup`
still reaches neither.
"""


def composes_natively(rel_path: Path) -> bool:
    """Whether this path is a composition root of the library itself."""
    posix = rel_path.as_posix()
    return "lup/providers/" in posix or posix in LIBRARY_COMPOSITION


# The library's own roots and the adopter's are two tables, and only one of them
# is a model; `ApplicationRoots.sanctions` carries that one's half.
def path_is_sanctioned(
    rel_path: Path, application: ApplicationRoots = NO_APPLICATION
) -> bool:
    """Whether a path may import native implementations as a composition root."""
    return composes_natively(rel_path) or application.sanctions(rel_path)


def library_placement_path_is_audited(rel_path: Path) -> bool:
    """Whether a path is a neutral library module the placement rule audits.

    Adapter packages are exempt: a native spelling is canonical there by
    definition, which is the same reason they own the spellings above.
    """
    posix = rel_path.as_posix()
    return posix.startswith(LIBRARY_ROOT) and "lup/providers/" not in posix


def native_spelling_path_is_sanctioned(
    rel_path: Path, application: ApplicationRoots = NO_APPLICATION
) -> bool:
    """Whether a path may own provider wire spellings without a suppression."""
    return composes_natively(rel_path) or application.sanctions_spelling(rel_path)


class BoundaryBreach(BaseModel):
    """One concrete native import outside a sanctioned composition root."""

    line: int
    module: str
    text: str


class BoundaryAuditFinding(BaseModel):
    """One missing, untyped, or spurious boundary-rule suppression."""

    kind: FindingKind
    line: int
    text: str
    message: str
    rule_id: str
    module: str = ""


class SourceViolation(BaseModel):
    """One unsuppressed source shape before ordinary suppression auditing.

    ``line`` is where the violation is reported, and it is the only thing this
    rule gets to say about its suppression: a declaration heading a fifty-line
    table is excused from the line above it, exactly as every other rule is,
    because a reason that does not fit inline goes to the same place whatever
    is being excused.
    """

    line: int
    text: str
    subject: str
    message: str


class ConstantDeclaration(BaseModel):
    """One module-level constant, and the shape its value is written in.

    ``entries`` counts the members of a written-out collection display — a
    vocabulary, whose judgement is which entries it holds — and is ``None``
    where the value is a single fact rather than a table.
    """

    name: str
    line: int
    end_line: int
    text: str
    entries: int | None = None

    directive_from: int = 0
    """First line a suppression may sit on to excuse this declaration.

    A directive may head its declaration, from anywhere in the comment block
    written directly above it — a reason worth reading rarely fits on one
    line. The block stops at the first line that is not a comment, so the zone
    never reaches the neighbour above: constants sit in runs, and one reason
    covering two of them would strand the second's own marker as a directive
    guarding nothing.
    """

    def judging_rule(self, library_module: bool) -> str:
        """Which of the two constant rules judges this declaration.

        Exactly one does, because this is a single total function over the one
        enumeration both rules read. A vocabulary the library freezes is
        ``library-default``'s, since it reaches an adopter only by their
        editing this repository; every other constant is
        ``constant-declaration``'s. Neither rule can reach a line the other
        owns, so no declaration is reported twice or excused by the wrong
        directive.
        """
        vocabulary = self.entries is not None and self.entries >= 2
        return (
            RuleId.LIBRARY_DEFAULT
            if library_module and vocabulary
            else RuleId.CONSTANT_DECLARATION
        )

    def judgement(self, carved: bool) -> str:
        """Why this frozen constant is reported, and what to do about it."""
        if carved:
            return (
                f"{self.name} exists only to carve a value out of text by hand — "
                "parse the value instead (datetime for a timestamp, urllib.parse "
                "for a URL, pathlib.Path for a path) and the constant goes with "
                "the surgery"
            )
        return (
            f"constant {self.name} is a judgement a second implementer with the "
            "same intent could have made differently, frozen where no caller can "
            "replace it — take it as an overridable default, or suppress it with "
            f"# lup: ignore[{RuleId.CONSTANT_DECLARATION}] and the reason it is "
            "canonical: a provider's wire spelling, a language's own vocabulary, "
            "an identity this repository defines"
        )


class BoundaryDirective(BaseModel):
    """One parsed inline or file-wide suppression directive."""

    line: int
    rule_ids: set[str] | None  # lup: ignore[set-shape] — rule identity membership
    file_level: bool = False


def import_violations(
    text: str,
    rel_path: Path = Path(""),
    application: ApplicationRoots = NO_APPLICATION,
    boundaries: list[ImportBoundary] | None = None,
) -> list[SourceViolation]:
    """Find native adapter imports through Python syntax before suppression."""
    lines = text.splitlines()
    return [
        SourceViolation(
            line=violation["line"],
            subject=violation["module"],
            text=lines[violation["line"] - 1].strip(),
            message=f"{violation['module']}: {violation['message']}",
        )
        for violation in native_import_violations(
            rel_path.as_posix(),
            text,
            [
                boundary.erased()
                for boundary in (
                    native_import_boundaries(application)
                    if boundaries is None
                    else boundaries
                )
            ],
        )
    ]


def kernel_import_violations(text: str) -> list[SourceViolation]:
    """Find imports outside the hermetic policy kernel's pinned stdlib set."""
    tree = python_tree(text)
    if tree is None:
        return []
    lines = text.splitlines()
    violations: list[SourceViolation] = []  # lup: ignore[empty-collection]
    for node in python_nodes(tree):
        modules: list[str]
        match node:
            case ast.Import(names=names):
                modules = [
                    item.name
                    for item in names
                    if item.name not in KERNEL_IMPORT_ALLOWLIST
                ]
            # A relative import names a sibling kernel module, which carries
            # the same hermetic guarantee this rule enforces.
            case ast.ImportFrom(level=int(level)) if level > 0:
                continue
            case ast.ImportFrom(module=str(module)) if (
                module not in KERNEL_IMPORT_ALLOWLIST
            ):
                modules = [module]
            case _:
                continue
        line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        violations.extend(
            SourceViolation(
                line=node.lineno,
                subject=module,
                text=line.strip(),
                message=f"policy kernel imports non-hermetic module {module}",
            )
            for module in modules
        )
    return violations


# Its keys are as open as what the root binds, spelled the way Python spells it.
# lup: ignore[dict-str-payload] — a re-exported name, to the module defining it
type FrontDoorExports = dict[str, str]
"""Each name the package root re-exports, keyed to the module that defines it."""


def front_door_exports(text: str) -> FrontDoorExports:
    """Each name the package root re-exports, and the module that defines it.

    Read off the root's own imports — the ``TYPE_CHECKING`` block's among
    them, which is where the names it resolves lazily are named — so a name
    the root starts exporting is known here the day it does, and the module a
    diagnostic sends a reader to is the one the root itself imports from.
    """
    tree = python_tree(text)
    if tree is None:
        return {}
    return {
        alias.asname or alias.name: (
            f"{LIBRARY_PACKAGE_ROOT}.{node.module}" if node.level else node.module
        )
        for node in python_nodes(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.level < 2
        for alias in node.names
    }


def front_door_violations(
    source: PythonSource, exports: FrontDoorExports
) -> Iterator[RuleViolation]:
    """Every name one library module reads through the package root.

    Both spellings reach it: ``from lup import Agent``, or the relative import
    that climbs to the root, and ``lup.Agent`` wherever an ``import lup`` bound
    the package. ``import lup`` alone reads no name — a module locating the
    package by ``lup.__file__`` is not walking through the front door — so the
    attribute form is judged against the names the root exports, which is
    also what lets a diagnostic name the module each one is defined in.
    """
    tree = python_tree(source.text)
    if tree is None:
        return
    # A from-import reaches the root by naming it, or relatively: naming no
    # module, and climbing one package per directory between it and this file.
    reaching = [
        (0, LIBRARY_PACKAGE_ROOT),
        (len(source.path.relative_to(LIBRARY_ROOT).parts), None),
    ]

    def binding(alias: ast.alias) -> list[str]:
        """The name one ``import`` binds to the root: none, or exactly one."""
        match alias:
            case ast.alias(name=name, asname=None) if (
                name == LIBRARY_PACKAGE_ROOT
                or name.startswith(f"{LIBRARY_PACKAGE_ROOT}.")
            ):
                return [LIBRARY_PACKAGE_ROOT]
            case ast.alias(name=name, asname=str(asname)) if (
                name == LIBRARY_PACKAGE_ROOT
            ):
                return [asname]
        return []

    bound = [
        held
        for node in python_nodes(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        for held in binding(alias)
    ]

    def refusal(read: str, name: str) -> str:
        remedy = (
            f"import it where it is defined: from {exports[name]} import {name}"
            if name in exports
            else f"import {name} from the module that defines it"
        )
        return (
            f"{read} reads the package root, the library's front door for its "
            f"users — {remedy}"
        )

    for node in python_nodes(tree):
        match node:
            case ast.ImportFrom(names=names) if (node.level, node.module) in reaching:
                reads = [
                    (f"from {LIBRARY_PACKAGE_ROOT} import {alias.name}", alias.name)
                    for alias in names
                ]
            case ast.Attribute(value=ast.Name(id=held), attr=name) if (
                held in bound and name in exports
            ):
                reads = [(f"{held}.{name}", name)]
            case _:
                continue
        yield from (
            RuleViolation(
                path=source.path, line=node.lineno, message=refusal(read, name)
            )
            for read, name in reads
        )


def literal_string(node: ast.AST) -> str | None:
    """Fold only statically known string syntax for the spelling audit."""
    match node:
        case ast.Constant(value=str(value)):
            return value
        case ast.BinOp(left=left, op=ast.Add(), right=right):
            before = literal_string(left)
            after = literal_string(right)
            return before + after if before is not None and after is not None else None
        case ast.JoinedStr(values=values):
            parts = [
                part for value in values if (part := literal_string(value)) is not None
            ]
            return "".join(parts) if len(parts) == len(values) else None
        case ast.FormattedValue(value=ast.Constant(value=str(value))):
            return value
    return None


def unfolded_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Every node, never descending into one its parent already reads whole.

    `literal_string` folds a `BinOp` or a `JoinedStr` into the single string
    the source writes, so that node's descendants are parts of a value already
    judged rather than values of their own. Declining to descend says that
    once. Walking in and marking each descendant folded says it once per
    ancestor above it, and a concatenation chain has an ancestor per line —
    which is quadratic in exactly the long messages this rule exists to read.

    Breadth-first, like the `ast.walk` it stands in for, so a file's
    violations come back in the order `ast.walk` gives them.
    """
    pending = deque([tree])
    while pending:
        node = pending.popleft()
        yield node
        if not (
            isinstance(node, ast.BinOp | ast.JoinedStr)
            and literal_string(node) is not None
        ):
            pending.extend(ast.iter_child_nodes(node))


def runtime_tree_named(value: str, trees: Collection[str]) -> str | None:
    """The runtime tree a path-shaped string names, where it names one.

    Only a string with no whitespace is read as a path. A sentence naming a
    tree for its reader -- a message, a document's prose -- says where
    something is rather than going there, and is the runtime's to describe
    wherever it is read.
    """
    if not trees or any(character.isspace() for character in value):
        return None
    names = {PurePosixPath(tree).name for tree in trees}
    return next((part for part in PurePosixPath(value).parts if part in names), None)


def native_spelling_violations(
    text: str, trees: Collection[str] = ()
) -> list[SourceViolation]:
    """Find provider wire spellings in code strings outside native ownership.

    ``trees`` are the runtimes' own trees (:attr:`ApplicationRoots.runtime_trees`):
    a path naming one is a spelling only that runtime's adapter states.
    """
    tree = python_tree(text)
    if tree is None:
        return []
    lines = text.splitlines()
    context = PythonContext.parse(text)
    violations: list[SourceViolation] = []  # lup: ignore[empty-collection]
    for node in unfolded_nodes(tree):
        value = literal_string(node)
        line_number = getattr(node, "lineno", 0)
        if value is None or line_number in context.docstring_lines:
            continue
        line = lines[line_number - 1] if 0 < line_number <= len(lines) else ""
        for spelling, description in NATIVE_SPELLINGS.items():
            if spelling not in value:
                continue
            violations.append(
                SourceViolation(
                    line=line_number,
                    text=line.strip(),
                    subject=spelling,
                    message=(
                        f"neutral module contains {description} spelling {spelling!r}"
                    ),
                )
            )
        named = runtime_tree_named(value, trees)
        if named is not None:
            violations.append(
                SourceViolation(
                    line=line_number,
                    text=line.strip(),
                    subject=named,
                    message=(
                        f"neutral module spells {named!r}, a runtime's own tree, "
                        "which only its adapter states"
                    ),
                )
            )
    return violations


def collection_entries(node: ast.expr) -> int | None:
    """How many entries a collection display holds, or ``None`` if it is not one.

    Only a written-out display counts. A comprehension is derived from another
    value rather than declared, and a scalar is a single fact, not a table.

    A constructor wrapping one display is that display: ``dict.fromkeys([...])``
    and ``frozenset({...})`` write down the same vocabulary a bare display
    does, and a table that escaped judgement by naming its own container would
    be the easiest thing in the world to reach for.
    """
    match node:
        case ast.List(elts=elts) | ast.Tuple(elts=elts) | ast.Set(elts=elts):
            return len(elts)
        case ast.Dict(keys=keys):
            return len(keys)
        case ast.Call(args=[ast.expr() as only]):
            return collection_entries(only)
    return None


def frozen_literal(node: ast.expr) -> bool:
    """Whether a scalar value is decided here rather than derived from a name.

    A value that names another symbol — a constant, a call, an attribute, an
    interpolated string — follows from that symbol, so the choice it embodies
    was made where the symbol was declared and is judged there instead. A
    collection display never reaches this test: its judgement is which entries
    it holds, and that is chosen here however each entry is spelled.
    """
    match node:
        case ast.Constant():
            return True
        case ast.UnaryOp(operand=operand):
            return frozen_literal(operand)
        # A bare constructor over literals writes down a value the same way:
        # the characters `set("$*?")` holds are as much a choice as a
        # display's entries, and a name that is only the container is no name.
        # Only a plain name counts as the constructor — an attribute chain
        # such as `resources.files(...).read_text(...)` takes literal
        # arguments while its value comes from somewhere else entirely.
        case ast.Call(func=ast.Name(), args=args, keywords=keywords) if (
            args and not keywords
        ):
            return all(frozen_literal(argument) for argument in args)
    return False


def constant_declarations(text: str) -> list[ConstantDeclaration]:
    """Every module-level shouty constant whose value is a choice made here."""
    tree = python_tree(text)
    if tree is None:
        return []
    lines = text.splitlines()

    def declared(node: ast.stmt) -> ConstantDeclaration | None:
        match node:
            case (
                ast.Assign(targets=[ast.Name(id=name)], value=value)
                | ast.AnnAssign(target=ast.Name(id=name), value=ast.expr() as value)
            ):
                entries = collection_entries(value)
            case _:
                return None
        if not name.isupper() or (entries is None and not frozen_literal(value)):
            return None
        line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        return ConstantDeclaration(
            name=name,
            line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            text=line.strip(),
            entries=entries,
        )

    found = [
        declaration for node in tree.body if (declaration := declared(node)) is not None
    ]
    occupied = {
        line
        for declaration in found
        for line in range(declaration.line, declaration.end_line + 1)
    }

    def heading_from(line: int) -> int:
        if line - 1 in occupied:
            return line
        start = line - 1
        while (
            start > 1
            and lines[start - 1].strip().startswith("#")
            and lines[start - 2].strip().startswith("#")
        ):
            start -= 1
        return start

    return [
        declaration.model_copy(
            update={"directive_from": heading_from(declaration.line)}
        )
        for declaration in found
    ]


@cache
def default_position_names(
    text: str,
) -> set[str]:  # lup: ignore[set-shape] — name identity membership
    """Constant names one module reaches as a caller-replaceable default.

    Four spellings count, and only these: a parameter default in a signature,
    a field default in a class body — written plainly or through pydantic's
    ``Field`` (or a ``default_factory`` lambda returning the constant) — and
    the two shapes a mutable default is written as, the ``TABLE if argument is
    None else argument`` sentinel and the ``argument or TABLE`` fallback.

    The last two count only where ``argument`` is what a caller hands in: a
    parameter of a function enclosing the expression, or an attribute of one,
    which is how a model's field reaches its methods. Were any operand to
    count, ``something or TABLE`` over a local, a global or a call would exempt
    the table from both rules asking for a default, while no caller could
    reach it.
    """
    tree = python_tree(text)
    if tree is None:
        return set()  # lup: ignore[set-shape] — an unparseable module names nothing

    def supplied(node: ast.expr, parameters: Collection[str]) -> bool:
        match node:
            case ast.Name(id=name):
                return name in parameters
            case ast.Attribute(value=value):
                return supplied(value, parameters)
        return False

    def parameters(node: ast.AST) -> list[str]:
        match node:
            case (
                ast.FunctionDef(args=args)
                | ast.AsyncFunctionDef(args=args)
                | ast.Lambda(args=args)
            ):
                return [
                    argument.arg
                    for argument in [
                        *args.posonlyargs,
                        *args.args,
                        *args.kwonlyargs,
                        *filter(None, [args.vararg, args.kwarg]),
                    ]
                ]
        return []

    def fallbacks(node: ast.AST, given: Collection[str]) -> list[ast.expr | None]:
        match node:
            case ast.IfExp(
                test=ast.Compare(
                    left=left,
                    ops=[ast.Is() | ast.IsNot()],
                    comparators=[ast.Constant(value=None)],
                ),
                body=body,
                orelse=orelse,
            ) if supplied(left, given):
                return [body, orelse]
            case ast.BoolOp(op=ast.Or(), values=[*passed, last]) if all(
                supplied(value, given) for value in passed
            ):
                return [last]
        return []

    def reached(node: ast.expr | None) -> list[str]:
        match node:
            case ast.Name(id=name) | ast.Lambda(body=ast.Name(id=name)):
                return [name]
        return []

    def assigned(node: ast.stmt) -> list[ast.expr | None]:
        match node:
            case ast.AnnAssign(value=value) | ast.Assign(value=value):
                return [value]
        return []

    def defaults(node: ast.AST) -> list[ast.expr | None]:
        match node:
            case ast.FunctionDef(args=args) | ast.AsyncFunctionDef(args=args):
                return [*args.defaults, *args.kw_defaults]
            # A field a class body assigns is the plain spelling of the same
            # override a `Field(default=...)` writes out, and the one a model
            # is usually written with; a subclass replaces either.
            case ast.ClassDef(body=body):
                return [value for member in body for value in assigned(member)]
            case ast.Call(keywords=keywords):
                return [
                    keyword.value
                    for keyword in keywords
                    if keyword.arg in ("default", "default_factory")
                ]
        return []

    def scoped(node: ast.AST) -> list[ast.stmt | ast.expr]:
        match node:
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.Lambda():
                return [node]
        return []

    def candidate(node: ast.AST) -> list[ast.expr]:
        match node:
            case ast.IfExp() | ast.BoolOp():
                return [node]
        return []

    functions = [
        function
        for node in python_nodes(tree)
        for function in scoped(node)
        if parameters(function)
    ]

    def enclosing(node: ast.expr) -> list[str]:
        """Every parameter of each function the expression is written inside."""
        return [
            name
            for function in functions
            if (function.lineno, function.col_offset) <= (node.lineno, node.col_offset)
            and (node.end_lineno or 0, node.end_col_offset or 0)
            <= (function.end_lineno or 0, function.end_col_offset or 0)
            for name in parameters(function)
        ]

    return {
        *(
            name
            for node in python_nodes(tree)
            for default in defaults(node)
            for name in reached(default)
        ),
        *(
            name
            for node in python_nodes(tree)
            for site in candidate(node)
            for fallback in fallbacks(site, enclosing(site))
            for name in reached(fallback)
        ),
    }


def declared_defaults(
    source: PythonSource,
) -> set[str]:  # lup: ignore[set-shape] — qualified name identity membership
    """The constants one module reaches as a caller-replaceable default, by where each is declared.

    A name is the module's own unless a ``from ... import`` brought it in, in
    which case it is the imported module's — so two modules each declaring a
    constant under one name are kept apart, where pooling bare names let one
    module's default excuse the other's constant. Relative imports resolve
    against the module's own package. A name reached through a module that
    re-exported it names that module, which is where the convention of
    importing from the defining module already sends it.
    """
    tree = python_tree(source.text)
    if tree is None:
        return set()  # lup: ignore[set-shape] — an unparseable module names nothing
    # lup: ignore[string-split] — a dotted module name is Python's own grammar
    parts = source.module.split(".") if source.module else []
    package = parts if source.path.name == "__init__.py" else parts[:-1]

    def origin(node: ast.ImportFrom) -> str:
        """The dotted module an import names, relative ones resolved here."""
        if node.level == 0:
            return node.module or ""
        base = package[: len(package) - (node.level - 1)]
        return ".".join([*base, *([node.module] if node.module else [])])

    imported = {
        alias.asname or alias.name: f"{origin(node)}.{alias.name}"
        for node in python_nodes(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    return {
        imported[name] if name in imported else f"{source.module}.{name}"
        for name in default_position_names(source.text)
    }


# lup: ignore[library-default] — the string methods that carve a value out of
# text, a set the language fixes rather than this project
CARVING_CALLS = (
    "removeprefix",
    "removesuffix",
    "partition",
    "rpartition",
    "split",
    "rsplit",
    "strip",
    "lstrip",
    "rstrip",
    "replace",
)


def carved_names(
    text: str,
) -> set[str]:  # lup: ignore[set-shape] — name identity membership
    """Constant names one module hands to a string-surgery call.

    A constant reached this way exists only because a structured value is
    carved out of text by hand — the suffix a timestamp ends in, the separator
    a path is split on. Parametrizing it would freeze the surgery behind a
    nicer name, so the rule steers these to the parser that already knows the
    format instead.
    """
    tree = python_tree(text)
    if tree is None:
        return set()  # lup: ignore[set-shape] — an unparseable module carves nothing

    def carved(node: ast.AST) -> list[str]:
        match node:
            case ast.Call(func=ast.Attribute(attr=attribute), args=args) if (
                attribute in CARVING_CALLS
            ):
                return [
                    argument.id for argument in args if isinstance(argument, ast.Name)
                ]
        return []

    return {name for node in python_nodes(tree) for name in carved(node)}


def library_default_violations(
    text: str,
    overridable: Collection[str],
    module: str,
) -> list[SourceViolation]:
    """Find declared library tables no caller can replace with their own.

    ``overridable`` names each constant as ``module.NAME``, where it is
    declared, as :func:`declared_defaults` resolves a default to; ``module``
    is the one ``text`` is.
    """
    return [
        SourceViolation(
            line=constant.line,
            text=constant.text,
            subject=constant.name,
            message=(
                f"library table {constant.name} ({constant.entries} entries) is a "
                "project choice no caller can replace — take it as a default"
            ),
        )
        for constant in constant_declarations(text)
        if f"{module}.{constant.name}" not in overridable
        and constant.judging_rule(library_module=True) == RuleId.LIBRARY_DEFAULT
    ]


def constant_declaration_violations(
    rel_path: Path,
    text: str,
    overridable: Collection[str],
    carved: Collection[str],
    module: str,
    application: ApplicationRoots = NO_APPLICATION,
) -> list[SourceViolation]:
    """Find frozen constants at one path that no caller can replace.

    ``overridable`` holds module-qualified names, as
    :func:`library_default_violations` reads them.
    """
    if application.renders(rel_path):
        return []
    library_module = library_placement_path_is_audited(rel_path)
    return [
        SourceViolation(
            line=constant.line,
            text=constant.text,
            subject=constant.name,
            message=constant.judgement(constant.name in carved),
        )
        for constant in constant_declarations(text)
        if f"{module}.{constant.name}" not in overridable
        and constant.judging_rule(library_module) == RuleId.CONSTANT_DECLARATION
    ]


def audit_constant_declarations(
    sources: list[PythonSource],
    application: ApplicationRoots = NO_APPLICATION,
    judged: list[PythonSource] | None = None,
) -> list[RuleFinding]:
    """Judge every frozen constant in a project against how it is reached.

    Whether a caller can replace a constant, and whether it exists only to
    carve text by hand, are properties of the project rather than of the
    module that writes the value down — so both are pooled across every source
    before any one declaration is judged, the way the library placement sweep
    already pools the names its own callers can replace.

    A generated tree is read for what it reaches and never judged or audited:
    its files are compiled from the declarations above, so a directive in one
    is a copy of a directive that already answers for itself where it was
    written, and reporting the copy would ask for a second one nothing can act
    on.
    """
    # lup: solved: pooled by bare name, so one module's parameter default
    # named MANIFEST made an unrelated MANIFEST in devtools/dev/conflicts.py
    # read as caller-replaceable, and its reasoned ignore as spurious; a
    # module-qualified name (where the default resolves to) would keep two
    # modules' constants apart
    overridable = {name for source in sources for name in declared_defaults(source)}
    carved = {name for source in sources for name in carved_names(source.text)}
    reported = sources if judged is None else judged
    authored = [source for source in reported if not application.renders(source.path)]
    violations = [
        RuleViolation(
            path=source.path,
            line=violation.line,
            message=violation.message,
        )
        for source in authored
        for violation in constant_declaration_violations(
            source.path, source.text, overridable, carved, source.module, application
        )
    ]
    return audit_suppressions(authored, violations, RuleId.CONSTANT_DECLARATION)


def audit_rule(
    text: str, rule_id: str, violations: list[SourceViolation]
) -> list[BoundaryAuditFinding]:
    """Apply ordinary inline/file suppression auditing to one boundary rule."""
    context = PythonContext.parse(text)
    file_ignore = file_level_ignore(text)
    lines = text.splitlines()
    directives: list[BoundaryDirective] = []  # lup: ignore[empty-collection]
    if file_ignore is not None:
        directives.append(
            BoundaryDirective(
                line=file_ignore.line,
                rule_ids=file_ignore.rule_ids,
                file_level=True,
            )
        )
    for line_number, line in enumerate(lines, start=1):
        if file_ignore is not None and line_number == file_ignore.line:
            continue
        match = context.suppression_at(line_number, line)
        if match is None or not context.comment_at(line_number, match.start()):
            continue
        directives.append(
            BoundaryDirective(
                line=line_number,
                rule_ids=ignore_rule_ids(match),
            )
        )

    used: set[int] = set()  # lup: ignore[set-shape,empty-collection]
    untyped: set[int] = set()  # lup: ignore[set-shape,empty-collection]
    findings: list[BoundaryAuditFinding] = []  # lup: ignore[empty-collection]
    for violation in violations:
        candidates = [
            (index, directive)
            for index, directive in enumerate(directives)
            if (
                directive.file_level
                or suppression_reaches(lines, directive.line, violation.line)
            )
            and (directive.rule_ids is None or rule_id in directive.rule_ids)
        ]
        if not candidates:
            findings.append(
                BoundaryAuditFinding(
                    kind="missing",
                    line=violation.line,
                    text=violation.text,
                    message=(
                        f"{violation.message} — suppress on "
                        f"{suppression_placement(violation.line)}"
                    ),
                    rule_id=rule_id,
                    module=violation.subject,
                )
            )
            continue
        index, directive = candidates[0]
        used.add(index)
        if directive.rule_ids is None and index not in untyped:
            findings.append(
                BoundaryAuditFinding(
                    kind="untyped",
                    line=directive.line,
                    text=violation.text,
                    message=(
                        f"bare suppression covers {rule_id}; use "
                        f"# lup: ignore[{rule_id}] with a reason"
                    ),
                    rule_id=rule_id,
                    module=violation.subject,
                )
            )
            untyped.add(index)
    for index, directive in enumerate(directives):
        rule_ids = directive.rule_ids
        if index in used or rule_ids is None or rule_id not in rule_ids:
            continue
        findings.append(
            BoundaryAuditFinding(
                kind="spurious",
                line=directive.line,
                text="",
                message=f"suppression names {rule_id} but guards no violation",
                rule_id=rule_id,
            )
        )
    return findings


def audit_boundaries(text: str) -> list[BoundaryAuditFinding]:
    """Audit native imports, native spellings, and both rule suppressions."""
    return [
        *audit_rule(text, RuleId.SEAM, import_violations(text)),
        *audit_rule(
            text,
            RuleId.NATIVE_SPELLING,
            native_spelling_violations(text),
        ),
    ]


def audit_path_boundaries(
    rel_path: Path,
    text: str,
    application: ApplicationRoots = NO_APPLICATION,
    boundaries: list[ImportBoundary] | None = None,
) -> list[BoundaryAuditFinding]:
    """Audit only the boundary rules that apply at one repository path."""
    declared = (
        native_import_boundaries(application) if boundaries is None else boundaries
    )
    findings = [
        finding
        for rule_id in dict.fromkeys(boundary.rule_id for boundary in declared)
        for finding in audit_rule(
            text,
            rule_id,
            import_violations(
                text,
                rel_path,
                application,
                [boundary for boundary in declared if boundary.rule_id == rule_id],
            ),
        )
    ]
    if not native_spelling_path_is_sanctioned(rel_path, application):
        findings.extend(
            audit_rule(
                text,
                RuleId.NATIVE_SPELLING,
                native_spelling_violations(text, application.runtime_trees),
            )
        )
    return findings


def audit_kernel_imports(text: str) -> list[BoundaryAuditFinding]:
    """Audit the canonical kernel against its pinned dependency allowlist."""
    return audit_rule(text, RuleId.KERNEL_IMPORTS, kernel_import_violations(text))


def audit_library_defaults(
    text: str,
    overridable: Collection[str],
    module: str,
) -> list[BoundaryAuditFinding]:
    """Audit one library module's tables against the names callers can replace."""
    return audit_rule(
        text,
        RuleId.LIBRARY_DEFAULT,
        library_default_violations(text, overridable, module),
    )


def find_boundary_breaches(text: str) -> list[BoundaryBreach]:
    """Find native adapter imports through Python syntax, honoring suppressions."""
    return [
        BoundaryBreach(line=item.line, module=item.module, text=item.text)
        for item in audit_rule(text, RuleId.SEAM, import_violations(text))
        if item.kind == "missing"
    ]


def find_native_spelling_breaches(text: str) -> list[BoundaryBreach]:
    """Find native wire spellings in neutral code, honoring suppressions."""
    return [
        BoundaryBreach(line=item.line, module=item.module, text=item.text)
        for item in audit_rule(
            text,
            RuleId.NATIVE_SPELLING,
            native_spelling_violations(text),
        )
        if item.kind == "missing"
    ]


def find_library_default_breaches(
    text: str,
    overridable: Collection[str],
    module: str,
) -> list[BoundaryBreach]:
    """Find unsuppressed baked-in library tables, honoring suppressions."""
    return [
        BoundaryBreach(line=item.line, module=item.module, text=item.text)
        for item in audit_library_defaults(text, overridable, module)
        if item.kind == "missing"
    ]


def find_kernel_import_breaches(text: str) -> list[BoundaryBreach]:
    """Find unsuppressed non-hermetic imports in the policy kernel."""
    return [
        BoundaryBreach(line=item.line, module=item.module, text=item.text)
        for item in audit_kernel_imports(text)
        if item.kind == "missing"
    ]


def rule_finding(path: Path, finding: BoundaryAuditFinding) -> RuleFinding:
    """One boundary verdict in the shape every project rule reports."""
    return RuleFinding(
        kind=finding.kind,
        path=path,
        line=finding.line,
        message=finding.message,
        rule_id=finding.rule_id,
        text=finding.text,
    )


def import_boundary_findings(
    audited: AuditedProject, rule_id: str
) -> list[RuleFinding]:
    """One import rule's verdicts across the project, from the boundaries it owns.

    The declared boundaries are split by the rule id each carries, so the seam
    rule judges the adapter and SDK families and the assembly rule the tool
    constructors, each reporting under its own id and grading only the
    directives that name it.
    """
    declared = (
        native_import_boundaries(audited.application)
        if audited.boundaries is None
        else audited.boundaries
    )
    owned = [boundary for boundary in declared if boundary.rule_id == rule_id]
    return [
        rule_finding(source.path, finding)
        for source in audited.judged_sources()
        for finding in audit_rule(
            source.text,
            rule_id,
            import_violations(source.text, source.path, audited.application, owned),
        )
    ]


def native_spelling_findings(audited: AuditedProject) -> list[RuleFinding]:
    """Every native spelling in a module the application did not sanction."""
    return [
        rule_finding(source.path, finding)
        for source in audited.judged_sources()
        if not native_spelling_path_is_sanctioned(source.path, audited.application)
        for finding in audit_rule(
            source.text,
            RuleId.NATIVE_SPELLING,
            native_spelling_violations(source.text, audited.application.runtime_trees),
        )
    ]


def kernel_import_findings(audited: AuditedProject) -> list[RuleFinding]:
    """Every import outside the pinned allowlist, in the kernel's own files."""
    return [
        rule_finding(source.path, finding)
        for source in audited.judged_sources()
        if source.path.as_posix().startswith(KERNEL_ROOT)
        for finding in audit_kernel_imports(source.text)
    ]


def front_door_findings(audited: AuditedProject) -> list[RuleFinding]:
    """Every read of the package root from inside the library it fronts.

    What the root exports is read off the root among the audited sources, so
    the rule judges the front door the tree actually has. A fragment holding
    no root still refuses every ``from lup import``; only the module a
    diagnostic names, and the ``lup.<name>`` form, need the root beside it.
    """
    exports = {
        name: module
        for source in audited.sources
        if source.path.as_posix() == FRONT_DOOR_PATH
        for name, module in front_door_exports(source.text).items()
    }
    return audit_suppressions(
        audited.judged_sources(),
        [
            violation
            for source in audited.judged_sources()
            if source.path.as_posix().startswith(LIBRARY_ROOT)
            and source.path.as_posix() != FRONT_DOOR_PATH
            for violation in front_door_violations(source, exports)
        ],
        RuleId.FRONT_DOOR,
        strength="strong",
    )


def library_default_findings(audited: AuditedProject) -> list[RuleFinding]:
    """Every library table no adopter can replace, judged against the whole library.

    Whether a table is reachable as an overridable default is a property of
    the library as a whole, so the names callers can replace are pooled across
    every library module — adapters included — before any one neutral module
    is judged against them.
    """
    library = [
        source
        for source in audited.sources
        if source.path.as_posix().startswith(LIBRARY_ROOT)
    ]
    overridable = {name for source in library for name in declared_defaults(source)}
    judged = {source.path for source in audited.judged_sources()}
    return [
        rule_finding(source.path, finding)
        for source in library
        if source.path in judged and library_placement_path_is_audited(source.path)
        for finding in audit_library_defaults(source.text, overridable, source.module)
    ]


NEUTRAL_MODULE = f"{LIBRARY_ROOT}sessions/client.py"
ADAPTER_MODULE = f"{LIBRARY_ROOT}providers/codex/harness.py"
KERNEL_MODULE = f"{KERNEL_ROOT}sample.py"
"""Where the examples below are taken to be written: a neutral library module,
an adapter, and a kernel file, for the rules whose verdict turns on that."""

SEAM_RULE = ProjectRule(
    id=RuleId.SEAM,
    family="boundary",
    scope="Neutral Python modules",
    examples=[
        RuleExample(
            code="from lup.providers.codex import Codex",
            verdict="flagged",
            path=NEUTRAL_MODULE,
        ),
        RuleExample(
            code='tools = ["Read", "WebSearch"]; runtime = "codex"',
            verdict="cleared",
            path=NEUTRAL_MODULE,
        ),
        RuleExample(
            code="from lup.providers.codex import Codex",
            verdict="cleared",
            path=ADAPTER_MODULE,
        ),
    ],
    message=IMPORT_BOUNDARY_MESSAGE,
    audit=lambda audited: import_boundary_findings(audited, RuleId.SEAM),
)
"""The seam rule: adapter and SDK imports stay where the declaration owns them."""

ASSEMBLY_RULE = ProjectRule(
    id=RuleId.ASSEMBLY,
    family="boundary",
    scope="Neutral Python modules",
    examples=[
        RuleExample(
            code="from lup.coordination.peer_tools import create_peer_tools",
            verdict="flagged",
            path=NEUTRAL_MODULE,
        ),
        RuleExample(
            code="groups = [coordination_group(), ledger_group(NODE_KINDS, ...)]",
            verdict="cleared",
            path=NEUTRAL_MODULE,
        ),
    ],
    message=ASSEMBLY_BOUNDARY_MESSAGE,
    audit=lambda audited: import_boundary_findings(audited, RuleId.ASSEMBLY),
)
"""The assembly rule: a tool group's constructor is the toolset's to call."""

FRONT_DOOR_RULE = ProjectRule(
    id=RuleId.FRONT_DOOR,
    family="boundary",
    scope="Library modules",
    examples=[
        RuleExample(
            code="from lup import Claude", verdict="flagged", path=NEUTRAL_MODULE
        ),
        RuleExample(
            code="from lup.sessions.surface import Agent",
            verdict="cleared",
            path=NEUTRAL_MODULE,
        ),
        RuleExample(
            code="from lup import Claude",
            verdict="cleared",
            path="examples/quickstart.py",
        ),
    ],
    message=(
        "The package root is the library's front door: it re-exports the public "
        "names for the library's users and resolves the agents and the launch "
        "vocabulary on first access, so importing lup reaches no provider and no "
        "launch machinery until one of them is named. Inside the library an import from it runs the dependency backwards — a "
        "module the root is still loading meets a half-built package, and an "
        "agent taken through it reaches its adapter under a name the seam rule "
        "never sees. Import each name from the module that defines it; the "
        "application, the examples and the tests are the front door's users, "
        "and keep importing from it."
    ),
    strength="strong",
    audit=front_door_findings,
)
"""The front-door rule: the root re-exports, and nothing behind it imports it."""

NATIVE_SPELLING_RULE = ProjectRule(
    id=RuleId.NATIVE_SPELLING,
    family="spelling",
    scope="Neutral Python modules",
    examples=[
        RuleExample(
            code='instruction = "$lup:commit"', verdict="flagged", path=NEUTRAL_MODULE
        ),
        RuleExample(
            code='instruction = "$lup:commit"', verdict="cleared", path=ADAPTER_MODULE
        ),
    ],
    message=(
        "Provider command, event, environment, and manifest spellings, and each "
        "runtime's own tree, stay at the native adapter boundary: read a tree's "
        "paths off its adapter's spellings."
    ),
    audit=native_spelling_findings,
)
"""The native-spelling rule: a wire word is the adapter's to spell."""

KERNEL_IMPORTS_RULE = ProjectRule(
    id=RuleId.KERNEL_IMPORTS,
    family="boundary",
    scope="Policy kernel",
    examples=[
        RuleExample(
            code="from pydantic import BaseModel", verdict="flagged", path=KERNEL_MODULE
        ),
        RuleExample(
            code="from pathlib import Path", verdict="cleared", path=KERNEL_MODULE
        ),
    ],
    message=(
        "The copied hook kernel imports only its pinned standard-library allowlist."
    ),
    audit=kernel_import_findings,
)
"""The kernel-imports rule: the copied kernel carries no dependency."""

LIBRARY_DEFAULT_RULE = ProjectRule(
    id=RuleId.LIBRARY_DEFAULT,
    family="boundary",
    scope="Neutral library modules",
    examples=[
        RuleExample(
            code='READ_ONLY_COMMANDS = ("ls", "cat", "grep")',
            verdict="flagged",
            path=NEUTRAL_MODULE,
        ),
        RuleExample(
            code=(
                'READ_ONLY_COMMANDS = ("ls", "cat", "grep")\n'
                "def run(commands: tuple[str, ...] = READ_ONLY_COMMANDS) -> None: ..."
            ),
            verdict="cleared",
            path=NEUTRAL_MODULE,
        ),
    ],
    message=(
        "A data table a library declares is a choice made for every adopter: it "
        "reaches them as an overridable default — a parameter default, a pydantic "
        "field default, or the sentinel a mutable default is written as — so they "
        "replace the vocabulary instead of editing the library. Suppress only a "
        "canonical table, whose value is fixed outside this repository."
    ),
    audit=library_default_findings,
)
"""The library-default rule: a table reaches an adopter as a default."""

CONSTANT_DECLARATION_RULE = ProjectRule(
    id=RuleId.CONSTANT_DECLARATION,
    family="architecture",
    scope="Python constants",
    examples=[
        RuleExample(code="SNIPPET_LENGTH = 500", verdict="flagged"),
        RuleExample(
            code=(
                "SNIPPET_LENGTH = 500\n"
                "def snippet(text: str, length: int = SNIPPET_LENGTH) -> str: ..."
            ),
            verdict="cleared",
        ),
    ],
    message=(
        "A constant is a judgement a second implementer with the same intent "
        "could have made differently — a ceiling, a retry count, an allowlist — "
        "frozen where no caller can replace it. It reaches them as an overridable "
        "default instead: a parameter default, a pydantic field default, or the "
        "sentinel a mutable default is written as. Suppress only a canonical "
        "value — a provider's wire spelling, a language's own vocabulary, an "
        "identity this repository defines. A constant that exists to carve "
        "text by hand is steered to the parser rather than to a parameter, "
        "because parametrizing it would keep the surgery."
    ),
    refinement=(
        "The library's own multi-entry tables are library-default's instead, so "
        "the two partition every declaration and neither reaches the other's."
    ),
    audit=lambda audited: audit_constant_declarations(
        audited.sources, audited.application, audited.judged_sources()
    ),
)
"""The constant-declaration rule: a judgement reaches its callers as a default."""
