"""The declarations a project settles about itself, read and edited in place.

A seam is a place this library holds an opinion a project is meant to
overrule, and each one lives in that project's own catalog as a typed
declaration. Editing it by hand works, and is what the policy skill does for
the shell vocabulary. What that leaves out is the moment the answers are
actually given: an initialization interview, where somebody has just been
shown a choice and should not then be sent to find a keyword argument in a
file they have never opened. A default nobody was shown is not a decision.

So the seams are also a surface. Read with no answers it says what each one
currently holds and where it is written, which is what makes putting them to
a person possible at all; given an answer it writes the declaration and names
the regeneration that has to follow, because what compiles from a declaration
is the project's own set of trees and a command that guessed at them would be
answering for a layout it does not own.

Edits are located with :mod:`ast` and spliced by the span it reports, so a
declaration is found by being parsed rather than by matching text that
happens to look like it.
"""

import ast
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel

from lup.workspace.paths import project_root


class DeclarationEdit(BaseModel, frozen=True):
    """One span of a source file, and what replaces it."""

    start_line: int
    start_column: int
    end_line: int
    end_column: int
    text: str

    def applied(self, source: str) -> str:
        """That source with this span replaced, across however many lines it spans.

        A line pair rather than a column pair on one line, because a
        declaration worth editing is usually a list literal written over
        several — which is exactly the case a single-line splice cannot
        express.
        """
        lines = source.splitlines(keepends=True)
        head = lines[self.start_line - 1][: self.start_column]
        tail = lines[self.end_line - 1][self.end_column :]
        return "".join(
            [
                *lines[: self.start_line - 1],
                head,
                self.text,
                tail,
                *lines[self.end_line :],
            ]
        )


class SeamValue(BaseModel, frozen=True):
    """What one seam holds, or that it holds whatever the library decided.

    Two answers rather than one and an error, because a project that has not
    written a seam down is in the state an interview exists to change:
    reporting it as a failure would make "you have not decided this yet"
    indistinguishable from "this file is wrong". Each answers for itself, so
    a caller reads a seam without asking which kind it got.
    """

    path: Path
    call: str
    keyword: str

    def described(self, summary: str) -> str:
        """This seam as a person reads it, with what it decides."""
        raise NotImplementedError

    def editable(self) -> "DeclarationSite":
        """This seam as something to write into, or why it is not one."""
        raise ValueError(
            f"{self.path}: {self.call}({self.keyword}=...) is not written down, "
            "so there is nothing to edit. Write the keyword into the "
            "declaration first — an absent one is a decision nobody has made, "
            "not a value to change."
        )


class DeclarationSite(SeamValue, frozen=True):
    """Where one keyword argument of one call is written, and what it holds.

    ``entries`` is what a list seam names, one element at a time, because the
    source segment of a declaration worth reading aloud is mostly the comments
    explaining it — which belong in the file rather than in a report. ``text``
    keeps the whole segment for a seam that is not a list.
    """

    line: int
    text: str
    entries: list[str] = []
    indent: int = 0
    """Columns the line this value starts on is written at.

    Carried because a replacement has to be written at the depth the
    declaration already sits at: a literal spliced at a fixed indentation
    parses fine and reads as a stranger in the file, which is the same defect
    as generated code that does not match what it sits beside."""

    def described(self, summary: str) -> str:
        held = "\n".join(f"    {entry}" for entry in (self.entries or [self.text]))
        return f"{self.keyword} — {summary}  ({self.path}:{self.line})\n{held}"

    def editable(self) -> "DeclarationSite":
        return self

    def paths(self) -> list[str]:
        """The paths a `list[Path]` seam names, as strings."""
        return [
            node.args[0].value
            for node in ast.walk(ast.parse(self.text, mode="eval"))
            if isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]

    def strings(self) -> list[str]:
        """The strings a `list[str]` seam names."""
        return [
            node.value
            for node in ast.walk(ast.parse(self.text, mode="eval"))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]

    def write(self, text: str) -> None:
        """Replace this seam's declared value with the literal given.

        The file is re-parsed to locate the span rather than trusting the one
        read earlier, so an edit made in between cannot land the splice on a
        line that has moved.
        """
        source = self.path.read_text(encoding="utf-8")
        declaration = call_named(ast.parse(source), self.call)
        value = (
            None if declaration is None else keyword_value(declaration, self.keyword)
        )
        if value is None or value.end_lineno is None or value.end_col_offset is None:
            raise ValueError(
                f"{self.path}: {self.call}({self.keyword}=...) is no longer "
                "declared there"
            )
        self.path.write_text(
            DeclarationEdit(
                start_line=value.lineno,
                start_column=value.col_offset,
                end_line=value.end_lineno,
                end_column=value.end_col_offset,
                text=text,
            ).applied(source),
            encoding="utf-8",
        )

    def rewritten_paths(self, paths: list[str]) -> None:
        """Write a `list[Path]` seam, at the depth this declaration sits at."""
        self.write(list_literal([f'Path("{path}")' for path in paths], self.indent))

    def rewritten_strings(self, values: list[str]) -> None:
        """Write a `list[str]` seam, at the depth this declaration sits at."""
        self.write(list_literal([f'"{value}"' for value in values], self.indent))


class UnwrittenSeam(SeamValue, frozen=True):
    """A seam the declaration leaves at the library's default.

    A different fact from one written down, and it inherits the refusal to be
    edited: answering it means adding the keyword, which is a decision
    somebody makes rather than a value to change.
    """

    def described(self, summary: str) -> str:
        return (
            f"{self.keyword} — {summary}\n"
            f"    left at the library's default — write it into "
            f"{self.call}(...) in {self.path} to decide it"
        )


class MissingSeamModule(SeamValue, frozen=True):
    """A seam naming a module that is not there, and the catalog naming it.

    What a renamed package leaves behind when a seam's module was spelled as a
    path: the declaration still exists, under the new name, and the seam goes
    on pointing at the old one. Answered as a value rather than raised,
    because the survey is where somebody is shown every seam at once, and one
    whose file moved is a finding among the rest rather than a reason to show
    none of them.
    """

    declared_in: Path
    """The catalog whose seam roster names the missing module."""

    def described(self, summary: str) -> str:
        return (
            f"{self.keyword} — {summary}\n"
            f"    unreadable: {self.path} does not exist. The seam naming it is "
            f"declared in {self.declared_in}; point its `module` at the file "
            f"holding {self.call}(...)"
        )

    def editable(self) -> "DeclarationSite":
        raise ValueError(self.described(f"{self.call}({self.keyword}=...)"))


def list_literal(items: list[str], indent: int) -> str:
    """A list literal written at the depth the declaration around it is written.

    One entry per line, so a diff names what changed rather than reflowing a
    line somebody has to read twice to see the one word that moved.
    """
    if not items:
        return "[]"
    inner = " " * (indent + 4)
    entries = "".join(f"\n{inner}{item}," for item in sorted(items))
    return f"[{entries}\n{' ' * indent}]"


def call_named(tree: ast.Module, name: str) -> ast.Call | None:
    """The first call to a name anywhere in this module, or nothing."""
    for node in ast.walk(tree):
        match node:
            case ast.Call(func=ast.Name(id=called)) if called == name:
                return node
            case ast.Call(func=ast.Attribute(attr=called)) if called == name:
                return node
    return None


def keyword_value(call: ast.Call, keyword: str) -> ast.expr | None:
    """The value node one keyword argument of a call carries."""
    for argument in call.keywords:
        if argument.arg == keyword:
            return argument.value
    return None


def declaration_indent(source: str, line: int) -> int:
    """How deep one line of that source is written.

    Counted by walking the leading spaces rather than by stripping them,
    because what is wanted is the count and a strip would answer with the
    rest of the line.
    """
    text = source.splitlines()[line - 1]
    return next(
        (column for column, character in enumerate(text) if character != " "),
        len(text),
    )


def read_seam(path: Path, call: str, keyword: str) -> SeamValue:
    """What one seam holds in one declaration file, or that it holds nothing."""
    source = path.read_text(encoding="utf-8")
    declaration = call_named(ast.parse(source), call)
    value = None if declaration is None else keyword_value(declaration, keyword)
    if value is None:
        return UnwrittenSeam(path=path, call=call, keyword=keyword)
    return DeclarationSite(
        path=path,
        call=call,
        keyword=keyword,
        line=value.lineno,
        text=ast.get_source_segment(source, value) or "",
        entries=[
            ast.get_source_segment(source, element) or ""
            for element in (value.elts if isinstance(value, ast.List) else [])
        ],
        indent=declaration_indent(source, value.lineno),
    )


class Seam(BaseModel, frozen=True):
    """One declaration a project is meant to settle about itself."""

    call: str
    keyword: str
    summary: str
    module: Path | None = None
    """Where this declaration is written, when it is not the catalog.

    Most of what a project settles about itself sits in one file, and this
    surface was built on that. It is not a rule: an image is declared where
    the image is composed, and a seam that could only be read out of the
    catalog would have to say a project holds nothing for it -- reporting an
    answer that is written down as one nobody gave.

    Relative to the project root, resolved by the caller that already knows
    where that is."""

    def read(self, catalog: Path) -> SeamValue:
        """What this project holds for this seam.

        The catalog is the default rather than the only answer: a seam naming
        its own module is read there, and the path it names is resolved
        beside the catalog's own root.
        """
        written = catalog if self.module is None else project_root() / self.module
        if not written.is_file():
            return MissingSeamModule(
                path=written, call=self.call, keyword=self.keyword, declared_in=catalog
            )
        return read_seam(written, self.call, self.keyword)


DECLARED_SEAMS: list[Seam] = [
    Seam(
        call="HookSet",
        keyword="human_owned_files",
        summary="files the agent proposes rather than writes",
    ),
    Seam(
        call="HookSet",
        keyword="protected_edit_roots",
        summary="trees an edit needs approval into",
    ),
    Seam(
        call="HookSet",
        keyword="path_roles",
        summary="what each tree is for, which every gate reads",
    ),
    Seam(
        call="RuleSelection",
        keyword="retired",
        summary="library scan rules this project does not hold itself to",
    ),
]
"""The seams a surface exists over, as an overridable default.

Which declarations a project is asked about is a judgement rather than a
fixed fact: a domain that added seams of its own passes its own list rather
than forking this one.
"""


def declared_seam(seams: list[Seam], call: str, keyword: str) -> Seam:
    """The one seam a project declares for a call and keyword.

    Every project is asked about the library's seams, so one is always there
    to find; a project that overrode it declares the same call and keyword at
    a different module, and that is the one to write.
    """
    matches = [seam for seam in seams if seam.call == call and seam.keyword == keyword]
    if not matches:
        raise ValueError(
            f"this project declares no seam for {call}({keyword}=...); the "
            "library's default is one, so a roster without it dropped one"
        )
    return matches[0]


def survey(catalog: Path | None, seams: list[Seam] = DECLARED_SEAMS) -> list[str]:
    """Every seam, what it holds, and where it is written.

    A project declaring no catalog is told so rather than reported empty:
    curating declarations by hand is a way to keep them, and an empty survey
    would read as "you have decided nothing".
    """
    if catalog is None:
        return [
            "This project declares no catalog path, so there is nothing to read"
            " seams from. Name one on its `DevProject` to use this."
        ]
    return [seam.read(catalog).described(seam.summary) for seam in seams]


class Answers(BaseModel, frozen=True):
    """What one invocation was asked to settle."""

    own: list[str] = []
    disown: list[str] = []
    retire: list[str] = []
    keep: list[str] = []
    retire_all: bool = False

    def given(self) -> bool:
        """Whether anything was asked for, as opposed to a look at the seams."""
        return bool(
            self.own or self.disown or self.retire or self.keep or self.retire_all
        )

    def ownership(self, catalog: Path, seams: list[Seam]) -> Iterator[str]:
        """Write who owns which files, and say what changed."""
        if not self.own and not self.disown:
            return
        site = (
            declared_seam(seams, "HookSet", "human_owned_files")
            .read(catalog)
            .editable()
        )
        held = site.paths()
        updated = [
            *[path for path in held if path not in self.disown],
            *[path for path in self.own if path not in held],
        ]
        site.rewritten_paths(updated)
        yield f"human_owned_files: {', '.join(sorted(updated)) or 'nothing'}"

    def rules(
        self, catalog: Path, every: list[str], seams: list[Seam]
    ) -> Iterator[str]:
        """Write which rules this project holds itself to, and say what changed.

        ``retire_all`` names every id the library ships rather than adding a
        second way of saying "all of them": the selection is subtractive, so a
        project that drops the family and one that dropped thirty rules a
        denial at a time are the same project — and a rule the library adds
        later is one this declaration has visibly not answered for.
        """
        if not self.retire and not self.keep and not self.retire_all:
            return
        site = declared_seam(seams, "RuleSelection", "retired").read(catalog).editable()
        held = site.strings()
        asked = [*self.retire, *(every if self.retire_all else [])]
        updated = [
            *[rule for rule in held if rule not in self.keep],
            *[rule for rule in asked if rule not in held],
        ]
        site.rewritten_strings(updated)
        yield f"retired rules: {len(updated)} of {len(every)}"

    def settled(
        self, catalog: Path, every: list[str], seams: list[Seam] = DECLARED_SEAMS
    ) -> list[str]:
        """Apply what was asked, and name the regeneration that has to follow.

        Written through the project's own seams rather than the library's
        list, so a project that declared a seam somewhere other than the
        catalog is written there: reading a seam from one file and writing it
        into another is how a retirement reached the hooks and never the
        guidance that teaches the rule.
        """
        return [
            *self.ownership(catalog, seams),
            *self.rules(catalog, every, seams),
            "Run `lup-devtools harness generate all` so the compiled trees agree.",
        ]
