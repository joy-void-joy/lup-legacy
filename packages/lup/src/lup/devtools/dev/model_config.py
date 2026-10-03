"""Census pydantic configuration declarations, and prove a rewrite kept them.

Backs the `lup-devtools dev model-config` commands (wired in
`lup.devtools.dev.app`):

- `census` enumerates every `model_config` assignment by the *shape* of its
  right-hand side, from the repository root. It reads the AST rather than
  lines, so a `model_config` named in a docstring or a comment is not a site
  and cannot inflate a count, and a shape nobody anticipated is reported as
  ``other`` instead of being quietly folded into a known one.
- `aliases` lists each shared `ConfigDict` a module binds to a name, with the
  modules that import it — the work-list for retiring one, since an alias is
  not gone until the imports naming it are gone too.
- `convert` is the fixer for the `model-config` rule: it rewrites each
  assignment as class keywords, carrying every key across under its own name.
- `declared` records what each class declares, read from source.
- `snapshot` records the configuration pydantic actually resolved onto every
  model, by importing each module and reading `model_config` off the class.
  The declaration is what a rewrite edits; the resolved mapping is what the
  program runs on, and only the second is worth preserving.
- `declared-at` and `snapshot-at` take those same two readings of a git
  revision instead of the working tree, so a conversion already committed is
  still provable — the tree it replaced is read out of history rather than
  restored over the checkout.
- `compare` diffs two of either. Equal snapshots mean no model lost, gained,
  or changed a key.

The pairing is the point. A census counts declarations and a snapshot reads
behaviour, and neither alone is enough to migrate configuration safely: a
snapshot proves that whatever was converted kept its keys, but a site left
untouched also keeps its keys and so passes just as quietly, and a census
proves coverage but says nothing about whether a converted site still means
what it meant. Completeness is the census's job and the `model-config`
anti-pattern rule's after it; equivalence is the snapshot's. Both, or the
migration is unverified.

Examples::

    $ uv run lup-devtools dev model-config census
    $ uv run lup-devtools dev model-config census --json
    $ uv run lup-devtools dev model-config snapshot tmp/before.json
    $ uv run lup-devtools dev model-config snapshot-at HEAD~1 tmp/before.json
    $ uv run lup-devtools dev model-config compare tmp/before.json tmp/after.json
"""

import ast
import importlib
import os
import sys
import tarfile
import tempfile
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import sh
import typer
from pydantic import BaseModel

from lup.devtools.dev.tracked import tracked_files
from lup.devtools.utils import format_table, output_json
from lup.execution.shell import git
from lup.harness.environment import inherited

type DeclarationShape = Literal[
    "config-dict", "settings-config-dict", "alias", "dict-literal", "other"
]
"""The right-hand side a `model_config` assignment was written with.

``other`` is deliberately a member rather than an error: a census that can
only report the shapes it already knows about is a census that confirms its
own assumptions.
"""

CONFIG_CALLS: dict[str, DeclarationShape] = {  # lup: ignore[library-default]
    "ConfigDict": "config-dict",
    "SettingsConfigDict": "settings-config-dict",
}
"""The constructors that spell a pydantic configuration mapping.

Pydantic's own two spellings, not a choice this project made: a second
implementer reading the same library could not have written different names,
which is what makes this a canonical value rather than an overridable default.
"""


class RenderedConfig(BaseModel, frozen=True):
    """A configuration expression rewritten as class keywords, or why it wasn't.

    ``keywords`` reads as ``frozen=True``, ``extra="forbid"`` — each value
    taken from its own source text, so a rewrite carries what the author wrote
    rather than a re-rendering of it. It is empty exactly when ``hazards``
    names what stopped the rendering.
    """

    keywords: list[str] = []
    hazards: list[str] = []


class Classification(RenderedConfig, frozen=True):
    """A rendered configuration together with the shape it was written in."""

    shape: DeclarationShape


class AliasBinding(BaseModel, frozen=True):
    """A module-level name bound to a shared configuration mapping."""

    name: str
    config: RenderedConfig


class ImportedName(BaseModel, frozen=True):
    """A name as a module spells it locally, and the origin it was imported from."""

    local: str
    qualified: str


def dotted_module(path: str) -> str:
    """The dotted name a repository-relative Python file is imported under.

    Derived by dropping whatever precedes a ``src`` directory, so a package
    laid out as ``packages/<dist>/src/<pkg>/…`` and one laid out as
    ``src/<pkg>/…`` both resolve to the name an `import` statement spells.
    A tree with no ``src`` keeps its own path, which is unique per file and
    is all a cross-module lookup needs.
    """
    parts = PurePosixPath(path).with_suffix("").parts
    rooted = [i for i, part in enumerate(parts) if part == "src"]
    named = list(parts[rooted[-1] + 1 :] if rooted else parts)
    return ".".join(named[:-1] if named[-1:] == ["__init__"] else named)


def source_root(path: str) -> Path:
    """The directory :func:`dotted_module`'s name is resolved against.

    The other half of the same rule, so a module imported here is imported
    under the name the rest of the program spells it with rather than under a
    synthetic one — which is what makes a snapshot's keys comparable at all.
    """
    parts = PurePosixPath(path).parts
    rooted = [i for i, part in enumerate(parts) if part == "src"]
    return Path(*parts[: rooted[-1] + 1]) if rooted else Path()


class Declaration(BaseModel, frozen=True):
    """One `model_config` assignment, located with the class it configures."""

    path: str
    line: int
    end_line: int
    shape: DeclarationShape
    source: str
    keywords: list[str] = []
    class_name: str
    class_line: int
    hazards: list[str] = []

    def location(self) -> str:
        """This declaration's `path:line`, the form an editor can follow."""
        return f"{self.path}:{self.line}"


class Census(BaseModel, frozen=True):
    """Every `model_config` declaration a sweep found, and what stopped it."""

    declarations: list[Declaration] = []
    unparsed: list[str] = []
    alias_index: dict[str, RenderedConfig] = {}
    """Every configuration alias the project declares, by qualified name.

    Kept beside the declarations because deleting an alias is not done until
    the imports that named it are gone too, and this is the list of what has
    to stop existing.
    """

    def by_shape(self) -> Counter[str]:
        """How many declarations each right-hand-side shape accounts for."""
        return Counter(d.shape for d in self.declarations)

    def by_tree(self) -> Counter[str]:
        """How many declarations sit under each top-level tree."""
        return Counter(tree_of(d.path) for d in self.declarations)

    def hazardous(self) -> list[Declaration]:
        """The declarations a mechanical rewrite must not treat as routine."""
        return [d for d in self.declarations if d.hazards]


def tree_of(path: str) -> str:
    """The top-level tree a repository-relative path belongs to.

    Two segments where the first is ``packages`` or ``src``, since those hold
    several distributions whose counts are worth telling apart, and one
    everywhere else.
    """
    match Path(path).parts:
        case ("packages" | "src" as top, nested, *_):
            return str(Path(top) / nested)
        case (top, *_):
            return top
        case _:
            return path


def python_files() -> Iterator[Path]:
    """Every tracked or untracked-but-not-ignored Python file, from the root.

    The same enumeration the anti-pattern auditor sweeps, so a census and the
    rule that later has to report zero violations are looking at one set of
    files rather than two that happen to overlap.
    """
    for rel in tracked_files(others=True, suffixes=(".py", ".pyi")):
        yield Path(rel)


def rendered_keywords(node: ast.expr, text: str) -> RenderedConfig:
    """A configuration expression as class keywords, and what defeated it.

    Values come back as their own source text rather than as a re-rendering
    of a parsed literal, so ``extra="forbid"`` keeps its quotes and anything
    that is not a literal at all survives the trip.
    """
    match node:
        case ast.Call(keywords=keywords):
            rendered = [
                f"{kw.arg}={ast.get_source_segment(text, kw.value)}"
                for kw in keywords
                if kw.arg is not None
            ]
            if len(rendered) != len(keywords):
                return RenderedConfig(hazards=["unpacked-call"])
            return RenderedConfig(keywords=rendered)
        case ast.Dict(keys=keys, values=values):
            named = [
                f"{key.value}={ast.get_source_segment(text, value)}"
                for key, value in zip(keys, values, strict=True)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ]
            if len(named) != len(keys):
                return RenderedConfig(hazards=["unpacked-dict"])
            return RenderedConfig(keywords=named)
        case _:
            return RenderedConfig(hazards=["unresolved-value"])


class SourcePosition(BaseModel, frozen=True):
    """A 1-based line and the 0-based column within it."""

    line: int
    column: int = 0


class Rewrite(BaseModel, ABC, frozen=True):
    """One edit to a file's lines, ordered by where in the file it applies.

    Edits are applied last-first so an earlier one never invalidates a later
    one's line numbers, which is what :meth:`position` sorts on. Each kind
    carries out its own edit rather than being sorted into by a caller that
    reads its type.
    """

    line: int
    """The 1-based line this edit starts at."""

    def position(self) -> SourcePosition:
        """Where this edit sits, as the key that sequences it against others."""
        return SourcePosition(line=self.line)

    @abstractmethod
    def apply(self, lines: list[str]) -> list[str]:
        """This file's lines with the edit made."""


class InsertKeywords(Rewrite, frozen=True):
    """Add configuration keywords to a class header, after its last base."""

    column: int
    text: str

    def position(self) -> SourcePosition:
        """Ordered after any edit earlier on the same line."""
        return SourcePosition(line=self.line, column=self.column)

    def apply(self, lines: list[str]) -> list[str]:
        """The lines with the keywords spliced into the class header."""
        at = self.line - 1
        current = lines[at]
        return [
            *lines[:at],
            current[: self.column] + self.text + current[self.column :],
            *lines[at + 1 :],
        ]


class DeleteLines(Rewrite, frozen=True):
    """Remove a statement, and the blank line that would be left doubled."""

    end_line: int

    def apply(self, lines: list[str]) -> list[str]:
        """The lines with the statement — and any doubled blank — removed."""
        start, stop = self.line - 1, self.end_line
        before, after = lines[:start], lines[stop:]
        doubled = before and after and not before[-1].strip() and not after[0].strip()
        return before + (after[1:] if doubled else after)


class ModuleCensus(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One parsed module, read for the declarations and aliases it holds.

    Aliases are collected before the classes that use them so a name bound to
    a shared `ConfigDict` resolves to the keywords it stands for, which is the
    difference between a rewrite that preserves configuration and one that
    flattens every alias to whatever the most common one happened to be.
    """

    path: str
    text: str
    tree: ast.Module

    def aliases(self) -> dict[str, RenderedConfig]:
        """Module-level names bound to a configuration mapping."""

        def bindings() -> Iterator[AliasBinding]:
            for node in self.tree.body:
                match node:
                    case ast.Assign(targets=[ast.Name(id=name)], value=value) if (
                        config_call(value) is not None
                    ):
                        yield AliasBinding(
                            name=name, config=rendered_keywords(value, self.text)
                        )
                    case _:
                        pass

        return {binding.name: binding.config for binding in bindings()}

    def alias_imports(self) -> Iterator[ImportedName]:
        """Every name this module imports, with the origin it was imported from.

        An alias is shared by importing it, so a site reading `model_config =
        FROZEN` may be resolving a name another module declared. Resolving
        only within the file would classify those sites as unrecognized and,
        worse, leave a rewrite unable to tell which configuration they meant.
        """
        for node in self.tree.body:
            match node:
                case ast.ImportFrom(module=str() as origin, names=names):
                    for alias in names:
                        yield ImportedName(
                            local=alias.asname or alias.name,
                            qualified=f"{origin}.{alias.name}",
                        )
                case _:
                    pass

    def configured_classes(
        self, declared: dict[str, RenderedConfig]
    ) -> Iterator[AliasBinding]:
        """Each class in this module paired with the configuration it declares.

        Both spellings read the same here — an assigned `model_config` and a
        class keyword resolve to one mapping — which is what lets a snapshot
        taken before a rewrite be compared against one taken after it. Keyed
        by file and qualified class name rather than by module, because two
        test trees in this repository both spell themselves `tests.unit`.
        """
        aliases = self.resolved_aliases(declared)
        for klass in ast.walk(self.tree):
            if not isinstance(klass, ast.ClassDef):
                continue
            assigned = [
                self.classify(value, aliases).keywords
                for node in klass.body
                if (value := assigned_config(node)) is not None
            ]
            header = [
                f"{kw.arg}={ast.get_source_segment(self.text, kw.value)}"
                for kw in klass.keywords
                if kw.arg is not None and kw.arg != "metaclass"
            ]
            keywords = [one for group in assigned for one in group] + header
            if keywords:
                yield AliasBinding(
                    name=f"{self.path}::{klass.name}",
                    config=RenderedConfig(keywords=sorted(keywords)),
                )

    def conversion_rewrites(
        self, declared: dict[str, RenderedConfig]
    ) -> Iterator[Rewrite]:
        """Every edit turning this module's assigned configs into class keywords.

        Each site becomes two edits — keywords spliced into the class header,
        and the assignment removed — and each shared alias this module declares
        becomes a third, since an alias nothing reads is exactly what the
        conversion is meant not to leave behind. The imports that named a
        deleted alias are not edited here: they fall out as unused, which
        `ruff --fix` removes without this having to reason about import lists.
        """
        aliases = self.resolved_aliases(declared)
        for klass in ast.walk(self.tree):
            if not isinstance(klass, ast.ClassDef):
                continue
            for node in klass.body:
                value = assigned_config(node)
                if value is None:
                    continue
                keywords = self.classify(value, aliases).keywords
                anchor = header_anchor(klass)
                if not keywords or anchor is None:
                    continue
                yield InsertKeywords(
                    line=anchor.line,
                    column=anchor.column,
                    text=", " + ", ".join(keywords),
                )
                yield DeleteLines(
                    line=node.lineno,
                    end_line=node.end_lineno if node.end_lineno else node.lineno,
                )
        for node in self.tree.body:
            match node:
                case ast.Assign(targets=[ast.Name()], value=value) if (
                    config_call(value) is not None
                ):
                    yield DeleteLines(
                        line=node.lineno,
                        end_line=node.end_lineno if node.end_lineno else node.lineno,
                    )
                case _:
                    pass

    def frozen_propagation_rewrites(self) -> Iterator[Rewrite]:
        """Restate `frozen=True` on each class that inherits a frozen one.

        Pydantic inherits a configuration keyword, but a type checker holds the
        stricter line: a class deriving a frozen model must declare itself
        frozen. Stating it resolves to the configuration the class already had
        — it was frozen by inheritance before and is frozen by inheritance and
        declaration after — so this adds what the checker needs to see without
        moving what the program does.
        """
        classes = {
            node.name: node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.ClassDef)
        }

        def declares_frozen(klass: ast.ClassDef) -> bool:
            return any(kw.arg == "frozen" for kw in klass.keywords)

        def inherits_frozen(klass: ast.ClassDef, depth: int = 0) -> bool:
            if depth > len(classes):
                return False
            return any(
                base in classes
                and (
                    declares_frozen(classes[base])
                    or inherits_frozen(classes[base], depth + 1)
                )
                for base in map(base_name, klass.bases)
            )

        for klass in classes.values():
            anchor = header_anchor(klass)
            if declares_frozen(klass) or anchor is None:
                continue
            if inherits_frozen(klass):
                yield InsertKeywords(
                    line=anchor.line, column=anchor.column, text=", frozen=True"
                )

    def convert(
        self, index: dict[str, RenderedConfig], propagate: bool = False
    ) -> bool:
        """Rewrite this module in place, reporting whether it needed changing."""
        edits = sorted(
            self.frozen_propagation_rewrites()
            if propagate
            else self.conversion_rewrites(index),
            key=lambda one: (one.position().line, one.position().column),
            reverse=True,
        )
        lines = self.text.splitlines()
        for edit in edits:
            lines = edit.apply(lines)
        text = "\n".join(lines) + "\n" if lines else ""
        if text == self.text:
            return False
        Path(self.path).write_text(text, encoding="utf-8")
        return True

    def resolved_aliases(
        self, declared: dict[str, RenderedConfig]
    ) -> dict[str, RenderedConfig]:
        """This module's own configuration aliases plus the ones it imported."""
        return self.aliases() | {
            one.local: declared[one.qualified]
            for one in self.alias_imports()
            if one.qualified in declared
        }

    def declarations(
        self, declared: dict[str, RenderedConfig]
    ) -> Iterator[Declaration]:
        """Every `model_config` assignment in this module, with its class.

        ``declared`` carries every configuration alias the project declares,
        keyed by qualified name, so a name this module imported resolves to
        the configuration it actually stands for.
        """
        aliases = self.resolved_aliases(declared)
        lines = self.text.splitlines()
        for klass in ast.walk(self.tree):
            if not isinstance(klass, ast.ClassDef):
                continue
            for index, node in enumerate(klass.body):
                target = assigned_config(node)
                if target is None:
                    continue
                yield self.declaration(klass, node, target, index, aliases, lines)

    def declaration(
        self,
        klass: ast.ClassDef,
        node: ast.stmt,
        value: ast.expr,
        index: int,
        aliases: dict[str, RenderedConfig],
        lines: list[str],
    ) -> Declaration:
        """One assignment classified by shape, with what blocks a rewrite."""
        found = self.classify(value, aliases)
        return Declaration(
            path=self.path,
            line=node.lineno,
            end_line=node.end_lineno if node.end_lineno else node.lineno,
            shape=found.shape,
            source=ast.get_source_segment(self.text, value) or "",
            keywords=found.keywords,
            class_name=klass.name,
            class_line=klass.lineno,
            hazards=found.hazards + self.site_hazards(klass, node, index, lines),
        )

    def classify(
        self, value: ast.expr, aliases: dict[str, RenderedConfig]
    ) -> Classification:
        """This right-hand side's shape, its keywords, and what defeated them."""
        called = config_call(value)
        if called is not None:
            rendered = rendered_keywords(value, self.text)
            return Classification(
                shape=CONFIG_CALLS[called],
                keywords=rendered.keywords,
                hazards=rendered.hazards,
            )
        match value:
            case ast.Dict():
                rendered = rendered_keywords(value, self.text)
                return Classification(
                    shape="dict-literal",
                    keywords=rendered.keywords,
                    hazards=rendered.hazards,
                )
            case ast.Name(id=name) if name in aliases:
                return Classification(
                    shape="alias",
                    keywords=aliases[name].keywords,
                    hazards=aliases[name].hazards,
                )
            case ast.Name():
                return Classification(shape="alias", hazards=["alias-not-in-module"])
            case _:
                return Classification(shape="other", hazards=["unrecognized-shape"])

    def site_hazards(
        self, klass: ast.ClassDef, node: ast.stmt, index: int, lines: list[str]
    ) -> list[str]:
        """What about a site's surroundings a line-level rewrite must not miss.

        Each of these is a way deleting the statement changes more than the
        statement: a class left with an empty body, an attribute docstring
        left describing nothing, a comment that would vanish with the line,
        or a header with nowhere to put a keyword.
        """

        def found() -> Iterator[str]:
            if not klass.bases and not klass.keywords:
                yield "no-base-list"
            if len(klass.body) == 1:
                yield "sole-body-statement"
            following = klass.body[index + 1] if index + 1 < len(klass.body) else None
            match following:
                case ast.Expr(value=ast.Constant(value=str())):
                    yield "attribute-docstring"
                case _:
                    pass
            if isinstance(node, ast.AnnAssign):
                yield "annotated"
            end = node.end_lineno if node.end_lineno else node.lineno
            if "#" in "".join(lines[node.lineno - 1 : end]):
                yield "trailing-comment"

        return list(found())


def base_name(node: ast.expr) -> str:
    """The bare name a class lists as a base, for matching within a module."""
    match node:
        case ast.Name(id=name):
            return name
        case ast.Attribute(attr=name):
            return name
        case ast.Subscript(value=value):
            return base_name(value)
        case _:
            return ""


def header_anchor(klass: ast.ClassDef) -> SourcePosition | None:
    """Where a keyword is appended in a class header: after its last base.

    ``None`` when the class declares no bases and no keywords at all, since
    there is no parenthesis to append inside and adding one is a different
    edit than this makes. The census reports that case as `no-base-list`.
    """
    ends = [
        SourcePosition(line=node.end_lineno, column=node.end_col_offset)
        for node in [*klass.bases, *klass.keywords]
        if node.end_lineno is not None and node.end_col_offset is not None
    ]
    return max(ends, key=lambda one: (one.line, one.column)) if ends else None


def config_call(node: ast.expr) -> str | None:
    """The configuration constructor this expression calls, if it calls one."""
    match node:
        case ast.Call(func=ast.Name(id=name)) if name in CONFIG_CALLS:
            return name
        case ast.Call(func=ast.Attribute(attr=name)) if name in CONFIG_CALLS:
            return name
        case _:
            return None


def assigned_config(node: ast.stmt) -> ast.expr | None:
    """The value a statement assigns to `model_config`, if it assigns one."""
    match node:
        case ast.Assign(targets=[ast.Name(id="model_config")], value=value):
            return value
        case ast.AnnAssign(target=ast.Name(id="model_config"), value=value) if (
            value is not None
        ):
            return value
        case _:
            return None


class ParsedFile(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One file read for census, or the reason it could not be read."""

    module: ModuleCensus | None = None
    unparsed: str = ""


def parse_source(rel: str, text: str) -> ParsedFile:
    """Parse one file's source under the repository-relative name it carries.

    Separate from :func:`parse_file` because the working tree is not the only
    source a census reads: proving a conversion kept its keys means parsing
    the same paths out of history, which must be keyed identically to be
    comparable at all.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return ParsedFile(unparsed=f"{rel}: {exc}")
    return ParsedFile(module=ModuleCensus(path=rel, text=text, tree=tree))


def parse_file(path: Path) -> ParsedFile:
    """Parse one file, or report why it could not be parsed.

    A file that will not parse is reported rather than skipped: a census whose
    coverage depends on nobody having written a syntax error is a census that
    understates itself exactly when the tree is mid-edit.
    """
    rel = path.as_posix()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ParsedFile(unparsed=f"{rel}: {exc}")
    return parse_source(rel, text)


def take_census(paths: Sequence[Path] | None = None) -> Census:
    """Sweep the repository (or the given files) for configuration declarations.

    Two passes, because an alias is resolvable only once every module's own
    aliases are known: the first collects them into a project-wide index keyed
    by qualified name, and the second classifies each declaration against it.
    A single pass would report every imported alias as unrecognized.
    """
    read = [
        parse_file(path) for path in (paths if paths is not None else python_files())
    ]
    modules = [one.module for one in read if one.module is not None]
    index = {
        f"{dotted_module(one.path)}.{name}": config
        for one in modules
        for name, config in one.aliases().items()
    }
    return Census(
        declarations=[d for one in modules for d in one.declarations(index)],
        unparsed=[one.unparsed for one in read if one.unparsed],
        alias_index=index,
    )


def convert_tree(
    rewrite_only: Sequence[Path] | None = None, propagate: bool = False
) -> list[str]:
    """Rewrite every assigned `model_config` as class keywords, in place.

    ``propagate`` runs the follow-up pass instead: restating `frozen=True` on
    the subclasses that inherit it, which the class-keyword form needs stated
    and the assignment form did not.

    The whole project is always read, whatever ``rewrite_only`` narrows the
    writing to: the alias index has to be complete or a site whose alias is
    declared in an unread module resolves to nothing and converts to a class
    that lost its configuration. Narrowing what is written is safe; narrowing
    what is read is the defect this command exists to avoid.

    Edits are applied last-first within each file so the line numbers the AST
    reported stay true as the file shrinks. Returns the files it changed.
    """
    read = [parse_file(path) for path in python_files()]
    modules = [one.module for one in read if one.module is not None]
    index = {
        f"{dotted_module(one.path)}.{name}": config
        for one in modules
        for name, config in one.aliases().items()
    }
    selected = None if rewrite_only is None else {p.as_posix() for p in rewrite_only}
    return sorted(
        one.path
        for one in modules
        if (selected is None or one.path in selected) and one.convert(index, propagate)
    )


class ModelConfigSnapshot(BaseModel, frozen=True):
    """The configuration pydantic resolved onto every importable model.

    Keys map to the `repr` of their value rather than the value, because the
    comparison this exists for is equality of what was resolved and a `repr`
    compares by that without needing every configuration value to be JSON.
    """

    models: dict[str, dict[str, str]] = {}  # lup: ignore[dict-str-payload]
    """Qualified model name, to config key, to that value's `repr`.

    Both key sets are open: they are read off whatever the tree declares.
    """

    modules: list[str] = []
    failures: dict[str, str] = {}  # lup: ignore[dict-str-payload]
    """Repository path, to the import failure that stopped that file."""

    def compare(self, after: "ModelConfigSnapshot") -> "SnapshotDiff":
        """Diff this snapshot against a later one, modules included.

        A module that imported before and not after is reported even when every
        model the two share matches, because the models it contributed left the
        comparison rather than passing it.
        """
        return SnapshotDiff(
            added=sorted(after.models.keys() - self.models.keys()),
            removed=sorted(self.models.keys() - after.models.keys()),
            changed={
                name: f"{self.models[name]} -> {after.models[name]}"
                for name in sorted(self.models.keys() & after.models.keys())
                if self.models[name] != after.models[name]
            },
            module_delta=sorted(
                [one for one in self.modules if one not in after.modules]
                + [one for one in after.modules if one not in self.modules]
            ),
        )


def declared_snapshot(paths: Sequence[Path] | None = None) -> ModelConfigSnapshot:
    """Every class's declared configuration, read from source without importing.

    The companion to :func:`take_snapshot`, and the one that reaches every
    site: it needs no module to be importable, so both test trees, the
    hermetic asset templates and `examples/` are all covered on equal terms.
    What it cannot see is what pydantic does with a declaration — inheritance
    merging above all — which is what the importing snapshot checks instead.
    Neither alone is the equivalence proof; together they are.
    """
    return declared_configuration(
        [parse_file(path) for path in (paths if paths is not None else python_files())]
    )


def declared_configuration(read: Sequence[ParsedFile]) -> ModelConfigSnapshot:
    """What a set of already-parsed files declares, aliases resolved across them.

    The alias index is built from the same set rather than from the working
    tree, so a revision's `model_config = FROZEN` resolves against the
    ``FROZEN`` that revision declared instead of against whatever the current
    checkout happens to hold.
    """
    modules = [one.module for one in read if one.module is not None]
    index = {
        f"{dotted_module(one.path)}.{name}": config
        for one in modules
        for name, config in one.aliases().items()
    }
    return ModelConfigSnapshot(
        models={
            found.name: {keyword: "declared" for keyword in found.config.keywords}
            for one in modules
            for found in one.configured_classes(index)
        },
        modules=sorted(one.path for one in modules),
        failures={one.unparsed: "unparsed" for one in read if one.unparsed},
    )


def revision_files(revision: str) -> Iterator[str]:
    """Every Python file a git revision holds, repository-relative."""
    for rel in git.lines("ls-tree", "-r", "--name-only", revision):
        if rel.endswith((".py", ".pyi")):
            yield rel


def revision_source(revision: str, rel: str) -> ParsedFile:
    """One file's source as of a git revision, or why it could not be read."""
    try:
        text = str(git("show", f"{revision}:{rel}"))
    except (sh.ErrorReturnCode, UnicodeDecodeError) as exc:
        return ParsedFile(unparsed=f"{rel}: {exc}")
    return parse_source(rel, text)


def declared_snapshot_at(revision: str) -> ModelConfigSnapshot:
    """Every class's declared configuration as of a git revision.

    What makes an already-committed conversion provable: the tree it replaced
    is read out of history rather than restored over the checkout, so the
    before-and-after comparison does not depend on having thought to take a
    snapshot before starting.
    """
    return declared_configuration(
        [revision_source(revision, rel) for rel in revision_files(revision)]
    )


def import_module_at(path: Path) -> None:
    """Import one file under the dotted name the rest of the program uses.

    Its source root goes on `sys.path` first, so a module reaching a sibling
    resolves it exactly as it would at runtime. Importing under a synthetic
    name instead would make every intra-package import fail and, where it
    succeeded, key the snapshot by a module name nothing else spells.
    """
    root = str((Path.cwd() / source_root(path.as_posix())).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    importlib.import_module(dotted_module(path.as_posix()))


def descendant_models(root: type[BaseModel] = BaseModel) -> Iterator[type[BaseModel]]:
    """Every pydantic model class reachable from ``root``, transitively.

    Multiple inheritance reaches one class by more than one path, so a caller
    that needs each class once keys them — :func:`take_snapshot` does, by
    qualified name — rather than this walk carrying a seen-set on its behalf.
    """
    for subclass in root.__subclasses__():
        yield subclass
        yield from descendant_models(subclass)


class ImportAttempt(BaseModel, frozen=True):
    """One file's import, and the failure that stopped it."""

    path: str
    failure: str = ""


def attempt_import(path: Path) -> ImportAttempt:
    """Import one file, recording rather than raising whatever stopped it.

    Every failure is kept: a module that fails to import contributes no
    models, and a comparison that did not notice would read a vanished model
    as an unchanged one.
    """
    try:
        import_module_at(path)
    except Exception as exc:
        return ImportAttempt(
            path=path.as_posix(), failure=f"{type(exc).__name__}: {exc}"
        )
    return ImportAttempt(path=path.as_posix())


def take_snapshot(paths: Sequence[Path] | None = None) -> ModelConfigSnapshot:
    """Import every Python file, then read the resolved config off every model.

    Importing by path first and reading `BaseModel.__subclasses__` afterwards
    means a model is recorded wherever it was defined, including ones a module
    builds rather than declares.

    Models belonging to `__main__` are left out: the only one that can be is
    this module read as a script by :func:`snapshot_at`, and a snapshot that
    recorded the instrument would report it missing from every tree it was not
    measuring.
    """
    attempts = [
        attempt_import(path)
        for path in sorted(paths if paths is not None else python_files())
        if not any(part.startswith(".") for part in path.parts)
    ]
    return ModelConfigSnapshot(
        models=dict(
            sorted(
                (
                    f"{model.__module__}.{model.__qualname__}",
                    {
                        key: repr(value)
                        for key, value in sorted(model.model_config.items())
                    },
                )
                for model in descendant_models()
                if model.__module__ != "__main__"
            )
        ),
        modules=[one.path for one in attempts if not one.failure],
        failures={one.path: one.failure for one in attempts if one.failure},
    )


# lup: ignore[constant-declaration] — the argument this module is invoked with,
# so the spelling is shared with whoever runs it rather than chosen here
EMIT_SNAPSHOT = "--emit-snapshot"
"""The argument this module answers as a script rather than as a command."""


def materialize_revision(revision: str, destination: Path) -> None:
    """Write every file a git revision holds into a directory."""
    archive = destination / "revision.tar"
    git("archive", "--format=tar", "--output", str(archive), revision)
    with tarfile.open(archive) as tar:
        tar.extractall(destination, filter="data")
    archive.unlink()


def revision_paths(root: Path) -> str:
    """An extracted revision's import roots, as a `PYTHONPATH` value.

    Every `src` directory the tree holds, so the subprocess resolves `lup` and
    the application package out of the revision rather than out of whatever is
    installed in the environment running it.
    """
    return os.pathsep.join(
        sorted(str(one) for one in root.rglob("src") if one.is_dir())
    )


def snapshot_at(revision: str) -> ModelConfigSnapshot:
    """The configuration pydantic resolves onto every model at a git revision.

    Taken in a subprocess against an extracted copy, because the models being
    read are `lup` itself: this process has already imported that package from
    the working tree, so importing the revision's files here would resolve
    every one of them to the code already loaded and report the working tree's
    configuration under the revision's name — the one failure that would make
    a before-and-after comparison agree no matter what the rewrite did.
    """
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        materialize_revision(revision, root)
        environment = inherited({"PYTHONPATH": revision_paths(root)})
        return ModelConfigSnapshot.model_validate_json(
            str(
                sh.Command(sys.executable)(
                    __file__, EMIT_SNAPSHOT, _cwd=str(root), _env=environment
                )
            )
        )


class SnapshotDiff(BaseModel, frozen=True):
    """How two snapshots of resolved configuration differ."""

    added: list[str] = []
    removed: list[str] = []
    changed: dict[str, str] = {}  # lup: ignore[dict-str-payload]
    """Qualified model name, to how its configuration moved. An open key set."""

    module_delta: list[str] = []

    def equal(self) -> bool:
        """Whether the two snapshots resolved identical configuration."""
        return not (self.added or self.removed or self.changed or self.module_delta)


def create_model_config_app() -> typer.Typer:
    """The `dev model-config` command group."""
    app = typer.Typer(
        help="Census pydantic configuration declarations and prove a rewrite kept them.",
        no_args_is_help=True,
    )

    @app.command("census")
    def census_command(
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """Enumerate every `model_config` declaration by right-hand-side shape."""
        census = take_census()
        if as_json:
            output_json(census)
            return
        typer.echo(
            format_table(
                ["shape", "count"],
                [[shape, str(n)] for shape, n in sorted(census.by_shape().items())],
                aligns=["left", "right"],
            )
        )
        typer.echo("")
        typer.echo(
            format_table(
                ["tree", "count"],
                [[tree, str(n)] for tree, n in sorted(census.by_tree().items())],
                aligns=["left", "right"],
            )
        )
        typer.echo(f"\ntotal {len(census.declarations)} declarations")
        for line in census.unparsed:
            typer.echo(f"unparsed {line}")
        if census.hazardous():
            typer.echo("")
            typer.echo(
                format_table(
                    ["site", "shape", "hazards"],
                    [
                        [d.location(), d.shape, ",".join(d.hazards)]
                        for d in census.hazardous()
                    ],
                )
            )

    @app.command("aliases")
    def aliases_command() -> None:
        """List every shared configuration alias, and who imports each one.

        The work-list for retiring an alias: the declaration to delete, and
        the modules whose import of it has to go at the same time.
        """
        census = take_census()
        modules = [one.module for one in map(parse_file, python_files()) if one.module]
        typer.echo(
            format_table(
                ["alias", "keywords", "importers"],
                [
                    [
                        qualified,
                        ", ".join(config.keywords),
                        ", ".join(
                            sorted(
                                one.path
                                for one in modules
                                for name in one.alias_imports()
                                if name.qualified == qualified
                            )
                        ),
                    ]
                    for qualified, config in sorted(census.alias_index.items())
                ],
            )
        )

    @app.command("convert")
    def convert_command(
        under: Annotated[
            list[str] | None,
            typer.Option("--under", help="Limit to files under these prefixes."),
        ] = None,
        propagate: Annotated[
            bool,
            typer.Option(
                "--propagate-frozen",
                help="Instead restate frozen=True on subclasses that inherit it.",
            ),
        ] = False,
    ) -> None:
        """Rewrite every assigned `model_config` as class keywords, in place.

        The fixer for the `model-config` rule. Run `ruff check --fix` after it
        to drop the `ConfigDict` and alias imports the rewrite leaves unused,
        then `ruff format` to rewrap the headers it lengthened.

        ``--under`` narrows which files are rewritten, but never which are read:
        an alias is resolved against the whole project either way, so
        converting one subtree cannot flatten a config it could not see.
        """
        changed = convert_tree(
            [
                path
                for path in python_files()
                if not under
                or any(path.as_posix().startswith(prefix) for prefix in under)
            ]
            if under
            else None,
            propagate=propagate,
        )
        for path in changed:
            typer.echo(path)
        typer.echo(f"\n{len(changed)} files converted")

    @app.command("declared")
    def declared_command(destination: Path) -> None:
        """Record every class's declared configuration, without importing it."""
        snapshot = declared_snapshot()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        typer.echo(
            f"{len(snapshot.models)} configured classes from "
            f"{len(snapshot.modules)} files -> {destination}"
        )
        for rel in sorted(snapshot.failures):
            typer.echo(f"unparsed {rel}")

    @app.command("declared-at")
    def declared_at_command(revision: str, destination: Path) -> None:
        """Record what every class declared as of a git revision."""
        snapshot = declared_snapshot_at(revision)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        typer.echo(
            f"{len(snapshot.models)} configured classes from "
            f"{len(snapshot.modules)} files at {revision} -> {destination}"
        )
        for rel in sorted(snapshot.failures):
            typer.echo(f"unparsed {rel}")

    @app.command("snapshot")
    def snapshot_command(destination: Path) -> None:
        """Record the configuration pydantic resolved onto every model."""
        snapshot = take_snapshot()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        typer.echo(
            f"{len(snapshot.models)} models from {len(snapshot.modules)} modules "
            f"-> {destination}"
        )
        for rel, reason in sorted(snapshot.failures.items()):
            typer.echo(f"unimported {rel}: {reason}")

    @app.command("snapshot-at")
    def snapshot_at_command(revision: str, destination: Path) -> None:
        """Record the configuration pydantic resolved at a git revision."""
        snapshot = snapshot_at(revision)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        typer.echo(
            f"{len(snapshot.models)} models from {len(snapshot.modules)} modules "
            f"at {revision} -> {destination}"
        )
        for rel, reason in sorted(snapshot.failures.items()):
            typer.echo(f"unimported {rel}: {reason}")

    @app.command("compare")
    def compare_command(before: Path, after: Path) -> None:
        """Diff two snapshots; exit non-zero when any model's config moved."""
        diff = ModelConfigSnapshot.model_validate_json(
            before.read_text(encoding="utf-8")
        ).compare(
            ModelConfigSnapshot.model_validate_json(after.read_text(encoding="utf-8"))
        )
        if diff.equal():
            typer.echo("identical: every model resolved the same configuration")
            return
        output_json(diff)
        raise typer.Exit(1)

    return app


if __name__ == "__main__" and EMIT_SNAPSHOT in sys.argv:
    # The other half of `snapshot_at`: this module run against an extracted
    # revision, whose files are enumerated from the tree rather than from git
    # because an extracted revision is a directory and not a repository.
    typer.echo(take_snapshot(sorted(Path().rglob("*.py"))).model_dump_json(indent=2))
