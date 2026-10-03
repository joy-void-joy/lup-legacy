# lup: ignore[import-re, set-shape, empty-collection, string-split] — the scanner that enforces these rules is written in the vocabulary they govern
"""Shared scanning core for the review-marker and anti-pattern scanners.

Both `lup.harness.codescan.markers` and `lup.harness.codescan.antipatterns` walk a file line by
line and must tell prose from code: a `#` that opens a real comment, or a
docstring, is where a review note or an `# lup: ignore` directive can live,
while the same characters inside an ordinary string literal are code.
Tokenizing and parsing the Python source answers that question once, here, so
neither scanner re-implements the other's mechanics.

The `# lup: ignore` escape hatch — inline, or as a standalone file-level
opt-out — is matched here too, `LineProjections` holds the token-masked line
views a context-aware rule scans, and `LineCursor` is the shared line walk that
lets a scanner absorb a note's continuation lines without index bookkeeping.

`PythonSource` is the unit whole-project scanners consume, and `Refutation`
the shape a checker returns when it proves a matched line is not what its rule
is about — the one mechanism by which a broad regex hit is dropped with a
reason attached.
"""

import re
from collections.abc import Callable
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Literal, Self, get_args

from pydantic import BaseModel, model_validator

from lup.policy.kernel.edit import (
    FILE_IGNORE_RE,
    MatchSite,
    file_level_line,
    docstring_lines as python_docstring_lines,
    masked_source,
    python_comment_columns,
    source_suppression,
)
from lup.policy.kernel.rows import PathRoleName
from lup.policy.kernel.typescript import typescript_comment_columns

type RuleStrength = Literal["soft", "strong"]
"""Whether a rule admits a reasoned exception, or admits none.

Most rules are ``soft``: they name a shape that is usually wrong and sometimes
is the only thing that works — ``Any`` at an untyped boundary, a vendor's own
constant, a narrowing the type system needs. A suppression there is the
mechanism working, and the audit grades each one missing, bare, or dead.

A ``strong`` rule names a shape whose replacement is right every time. There is
no input for which an `isinstance` chain beats the `match` it compiles to, and
no call site where a bare tuple beats the `TypedDict` naming its fields — so a
suppression there is not a reasoned exception, it is the defect with a comment
on it. Those are refused, and the message says to write the replacement.
"""

type RuleContext = Literal["code", "comment", "prose"]
"""The syntactic surface a scan rule inspects.

``code`` is the masked source, where a name quoted in a sentence trips
nothing. ``comment`` is comment text, which the suppression directives are
written in and which no other rule reads. ``prose`` is what a person reads as
a sentence — comments and docstrings both — because a rule about how prose is
written has no reason to care which of the two carried it.
"""

RULE_CONTEXTS: tuple[RuleContext, ...] = get_args(RuleContext.__value__)
"""Every context a rule may declare, read off the alias that names them.

Taken from the alias rather than spelled again beside it, so a context added
there reaches every scan that projects a line per context without anyone
having to widen a second list.
"""

type ProjectRuleFamily = Literal["boundary", "spelling", "architecture"]
"""What a rule read off the whole project is about, as the reference groups it.

A ``boundary`` rule says where an import or a table may sit, a ``spelling``
rule which words a neutral module may hold, an ``architecture`` rule what a
class's base list or a branch on a type declares.
"""

type RuleFamily = Literal["anti-pattern"] | ProjectRuleFamily
"""Every family the reference indexes: the line rules, and the project ones."""


class Matcher(BaseModel, arbitrary_types_allowed=True):
    """The AST shape one rule selects, where the source parses.

    ``select`` returns the sites carrying the shape — the violations
    themselves, not a net around them. A rule that declares one is decided by
    the tree, and its ``pattern`` becomes the fallback for source no tree can
    be had from: a file mid-edit that will not parse, or a language this
    grammar is not for.

    Sites rather than lines, because a rule is asked two questions and both
    are about the same nodes. Every gate asks which line fires, which is what
    :func:`~lup.policy.kernel.edit.lines_of` projects. A rule whose verdict
    turns on a type is also asked which symbol settles it, and a site carries
    the position for that — so a resolution pass reads the sites this already
    chose instead of walking the tree again to rediscover them. A second
    selector is the same rule stated twice, and two statements of one rule
    are two that can disagree.

    Stating the shape here is what keeps it stated once. A rule whose regex
    nets more than the defect it names would otherwise need a second pass to
    read the tree and take the excess back out — the rule written twice, once
    too widely and once as the correction, with a reader having to hold both
    to know what it refuses. A matcher says it once, in the terms the
    language actually has.

    The kernel owns these functions because the hook applies them with no
    types and no dependencies to hand, and the rule holds the same object so
    what it selects is visible where it is declared.
    """

    select: Callable[[str], list[MatchSite]]


class TypeFamily(BaseModel, frozen=True):
    """A named set of declaring classes a resolved subject is measured against.

    ``classes`` names the declarations that constitute the family. A subject
    belongs when the class it resolves to is one of them, or inherits one —
    which is what carries `os._Environ(MutableMapping[AnyStr, AnyStr])` and a
    project's own `dict` subclass into the mapping family without listing
    either.

    Named classes rather than rendered type strings, because a name a checker
    prints for display is not a contract and a declaration is: `dict` in
    typeshed's `builtins.pyi` is the same declaration however the type
    holding it is spelled at the site.
    """

    name: str
    classes: list[str]


type ExampleVerdict = Literal["flagged", "cleared", "refuted"]
"""What the gates say about one example, in the three answers they can give.

``flagged`` is the shape the rule refuses, reported by every surface.

``cleared`` is the near-miss the tree settles on its own: the same spelling
doing something else — a one-argument `.replace` that renames a file, an
argless `.split` tokenizing prose, a `tuple[X, ...]` that is a sequence. Both
gates stay silent and a directive there would be reported spurious.

``refuted`` is the case no tree can decide, so the two surfaces answer
differently on purpose: the edit hook flags the spelling, and the whole-file
audit takes it back once a type oracle resolves what the receiver actually is.
`.get` on an HTTP client is the one this exists for. It is a third answer
rather than a cleared example, because a contributor who meets the denial
needs to know that waiting for the sweep is the way past it — and that adding
a directive is not.
"""


class RuleExample(BaseModel, frozen=True):
    """One snippet a rule is checked against, and the verdict it must return.

    A rule declares near-misses as well as violations, because one stated only
    by what it catches is one whose reach nobody wrote down — and a false
    positive is a near-miss nobody named. Most of
    these rules turn on what the subject *is* rather than on how it is spelled,
    so the cleared examples are where that is written down: `.replace` renaming
    a path, `.get` reaching a module's own function, `Field(default_factory=…)`
    doing work no literal expresses.

    A cleared snippet is often the replacement the rule's message asks for, so
    the set bounds the rule and shows the way out of it at once.
    """

    code: str
    verdict: ExampleVerdict
    path: str = "sample.py"
    """Where the snippet is taken to be written, for a rule whose verdict turns
    on that: a provider import is sanctioned under the adapters and refused in
    a neutral module, a third-party import is refused in the hermetic kernel
    and ordinary anywhere else. A line rule reads the code alone, and leaves
    the default standing."""

    def shown(self) -> str:
        """The snippet as a reader is shown it, with the path it named, if any.

        A verdict that turns on the path reads as the rule contradicting
        itself when two examples of one snippet are shown bare, one flagged
        and one cleared. The path goes in as a comment, so what is shown is
        still code somebody could paste.
        """
        if "path" not in self.model_fields_set:
            return self.code
        return f"{self.code}  # in {self.path}"


class Rule(BaseModel, arbitrary_types_allowed=True):
    """What every rule declares, whichever surface decides it.

    ``id`` is a stable kebab-case name a typed `# lup: ignore[id]` directive
    targets, so a single site can silence exactly one rule without opting out
    of the rest. It is what a denial, a sweep finding, the generated reference
    and a project's retirement list all name the rule by.

    ``examples`` are the snippets the rule is checked against, and carrying
    them on the declaration is what makes a rule impossible to add untested:
    a table-driven test runs every one of them through the gates that enforce
    the rule, so there is no separate list of covered ids for a new rule to be
    missing from. The generated reference renders them too, which is why they
    are written as code somebody could paste rather than as whatever happens
    to catch them.

    The variants say how the rule is decided. An :class:`AntiPattern` is a
    line rule the hermetic kernel runs on every edit, from a regex and an AST
    selector it can carry with no dependencies. A project rule reads the whole
    tree at once — a class index, the module roles, the roots an application
    declared — which only the repository sweep can hand it. A composition rule
    reads the assembled harness, which only generation ever sees. Each carries
    the fields this base does not, and the reference card, the example test
    and the sweep's call list are all derived from the set that holds them.
    """

    id: str
    examples: list[RuleExample]
    message: str
    remedy: str = ""
    """What to write instead, in one clause: what a denial at the edit quotes.

    The message explains the rule, and the reference page and the audit
    print it whole; a denial quotes this and names the page, so an agent
    reads the fix on every refused line rather than the essay. Empty where
    the message is already that short, and the denial quotes the message.
    """
    refinement: str = ""
    """How resolution sharpens this rule, in the words the reference shows.

    Beside the family it explains, so a change to what the gate resolves and
    a change to what the page claims it resolves are one edit. This is the
    prose a contributor reads after a denial, and the failure it guards
    against is the page describing a verdict the gate stopped giving.
    """

    strength: RuleStrength = "soft"
    """Whether a `# lup: ignore` may silence this rule at all.

    Soft by default, because most of these name a shape that is usually wrong
    and occasionally the only thing that works, and the audit exists to grade
    those exceptions. A rule is ``strong`` only when its replacement is right
    every time — then a suppression is not a reasoned exception but the defect
    with a comment on it, and this refuses to be silenced.
    """

    @model_validator(mode="after")
    def examples_bound_the_rule(self) -> Self:
        """Refuse a rule whose examples state only one half of its reach.

        A rule is two claims — this shape is refused, and that neighbouring
        one is not — and a declaration carrying only violations asserts the
        first while leaving the second to whatever the pattern happens to do.
        Requiring both here rather than in the test is what keeps the answer
        at the declaration: a rule with one polarity missing does not import.

        ``refuted`` is not one of the two. It says the gates answer this site
        differently on purpose, which presumes both of them already have an
        answer to state.
        """
        verdicts = {example.verdict for example in self.examples}
        if not {"flagged", "cleared"} <= verdicts:
            raise ValueError(
                f"rule {self.id} needs at least one example it flags and one it "
                f"clears — the cleared one is what says where the rule stops"
            )
        return self


class AntiPattern(Rule):
    """One forbidden code shape: a stable id, the regex that detects it, and why.

    Declared here rather than beside the tables that use it, because a project
    composing its own rules holds this and the harness declaration that carries
    them, and the tables sit above both. Ids are pinned alongside the pattern
    and message by ``tests/unit/test_antipatterns.py`` and must stay in step
    with the hook.

    ``context`` declares the syntactic surface the pattern inspects. A "code"
    rule is matched against token-masked source — string literals and comments
    both blanked — so an identifier quoted in prose never trips it; a
    "comment" rule targets comment directives (`# type: ignore`, `# noqa`) and
    is matched with comments intact. Python is masked through its tokenizer
    and the TypeScript family through the kernel's own span scan; where
    neither applies (text of another family, a Python fragment that fails to
    tokenize) every rule scans the raw line.

    ``matcher`` is present when the tree decides the rule outright: it selects
    the violating lines itself, and ``pattern`` is what the gate falls back to
    where no tree can be had — a file mid-edit that will not parse, or the
    TypeScript-family table, which this grammar is not for. Where the regex
    is wider than the defect the rule names, the matcher subtracts the excess
    itself rather than leaving a second pass to take it back out, so the rule
    is stated once and a reader has one place to read it.
    """

    pattern: re.Pattern[str]
    context: RuleContext = "code"
    matcher: Matcher | None = None
    roles: list[PathRoleName] = ["production"]
    """The path roles whose files this rule judges, at the hook and in the audit.

    Production for nearly every rule: a convention about how code reads is
    about the code other code reads, and a test's subject is production's
    behaviour rather than its own shape. A rule about how prose is written
    reaches a test as well, because a test's docstring is read as the spec of
    what it pins. Scratch is reached by none, being disposable by
    construction.
    """
    family: TypeFamily | None = None
    """The classes a site's subject must resolve into for this rule to stand.

    Present when the shape alone cannot settle the rule and only the type of
    what it is written on can: `.get` on a mapping hides a schema, the same
    spelling on an HTTP client is a request. The sites the ``matcher`` chose
    carry the positions, this names what they must resolve to, and
    `lup.harness.codescan.resolution` refutes every site shown to be outside it.

    Declared here rather than in a table keyed by rule id, because a rule is
    one thing. A second list saying which rules resolve is a list that can
    disagree with the rules it describes, and every gate that needed to know
    — the hermetic row, the reference page, the audit — had to perform the
    same join to find out.

    A rule with no family never reaches a checker at all, which is most of
    them: nothing about a bare `except` or an `import re` turns on a type.
    """

    @model_validator(mode="after")
    def a_family_reaches_the_sites_it_judges(self) -> Self:
        """Refuse a family declared on a rule whose sites carry no symbol.

        A family is measured against what a site's subject resolves to, and
        only a selector that recorded a position has given anything to
        resolve. Declared on a rule whose matcher yields bare lines, it is
        silently inert: nothing is asked, nothing is refuted, and the rule
        goes on giving the broad verdict while its reference page describes
        the narrowing that never happens.

        Checked against the rule's own flagged examples, which is the one
        thing on hand that is guaranteed to be a site this rule fires on.
        """
        if self.family is None:
            return self
        if self.matcher is None:
            raise ValueError(
                f"rule {self.id} declares the {self.family.name} family but no "
                f"matcher — a family is measured against sites, and only a "
                f"matcher selects them"
            )
        flagged = [
            example.code for example in self.examples if example.verdict == "flagged"
        ]
        if not any(
            "member" in site
            for code in flagged
            for site in self.matcher.select(f"{code}\n")
        ):
            raise ValueError(
                f"rule {self.id} declares the {self.family.name} family, but its "
                f"matcher records no symbol to resolve on any example it flags "
                f"— nothing would ever be asked about, and the refinement its "
                f"reference page promises would never happen"
            )
        return self


class RuleSelection(BaseModel, frozen=True):
    """Which of the rules this library ships a project holds itself to.

    A rule is a convention written down and a convention is a judgement, so a
    repository that settled one differently is not defective there — it is
    answering a question this library had no standing to close. The tables
    reach a project as a starting point rather than as a fixture, named by the
    same ids a directive, a denial, and the generated reference already use.

    Subtractive, because a project disagreeing with three rules should say
    those three rather than restate the thirty it still keeps: a replacement
    table would have to be re-copied every time the library adds a rule, and
    the copy that fell behind would look like a decision.

    One selection reaches the sweep, the edit hook, and the generated
    reference together, so the three cannot disagree about which rules are
    live here.
    """

    retired: list[str] = []
    """Rule ids this project does not hold itself to."""

    def keeps(self, rule_id: str) -> bool:
        """Whether a rule is live here, for a scan deciding to run it."""
        return rule_id not in self.retired


# Both directive spellings come from the kernel rather than being restated
# here. The hook and this audit have to agree on what a directive *is* before
# they can agree on what it silences, and two regexes drifted apart is exactly
# how a file ends up exempt to one gate and denied by the other.


def ignore_rule_ids(match: re.Match[str]) -> set[str] | None:
    """Rule ids a matched `IGNORE_RE`/`FILE_IGNORE_RE` directive names.

    ``None`` is the bare, untyped `# lup: ignore` that silences every rule; a
    set names exactly the rules a typed `# lup: ignore[a, b]` silences (empty
    brackets yield an empty set — a typed directive that names nothing).
    """
    raw = match.group("ids")
    if raw is None:
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


class FileIgnore(BaseModel):
    """A file-level `# lup: ignore` near a file's top and what it disables.

    ``rule_ids`` is ``None`` for a bare `# lup: ignore` that disables every
    rule for the whole file; a set names the rules a typed
    `# lup: ignore[rule-id]` disables file-wide. ``line`` is 1-based.
    """

    line: int
    rule_ids: set[str] | None


def file_level_ignore(text: str, max_lines: int = 10) -> FileIgnore | None:
    """The file-level `# lup: ignore` in the header block, or ``None``.

    A standalone bare `# lup: ignore` opts the whole file out of anti-pattern
    checks; the typed `# lup: ignore[rule-id]` form opts out only the named
    rules. Feedback-note scanning never consults this — an opted-out file still
    surfaces its `# lup:` notes (see `lup.harness.codescan.markers.find_feedback`).
    """
    number = file_level_line(text, max_lines)
    if number == 0:
        return None
    match = FILE_IGNORE_RE.match(text.splitlines()[number - 1])
    return FileIgnore(
        line=number, rule_ids=ignore_rule_ids(match) if match is not None else None
    )


# lup: ignore[constant-declaration] — this library's own import root, fixed by the
# package its distribution installs rather than by any adopter's taste.
LIBRARY_PACKAGE_ROOT = "lup"

PACKAGE_ROOTS = frozenset({LIBRARY_PACKAGE_ROOT})  # lup: ignore[frozenset-shape]
"""The import roots this library publishes, which a walk of the surface a
checkout offers covers while it vendors the library. An application adds the
package it publishes, whose name it alone knows — initialization renames it, so
a value written down here would go on naming a package that no longer exists.
A module's *name* reads none of this: :func:`module_name` takes the segment
``src`` introduces, whichever package that is."""


class PythonSource(BaseModel, frozen=True):
    """One import-resolvable Python module a project-wide scanner reads.

    The unit every whole-project scan consumes: the architecture audit builds
    its symbol index from these, and the typed grammar parses them for the
    sites it judges.
    """

    path: Path
    module: str
    text: str


class ApplicationRoots(BaseModel, frozen=True):
    """Where one application composes concrete implementations of the seams.

    The library guards its own package and can name nothing beyond it: an
    adopter renames the application package before writing a line, so a path
    written down here would go on naming a package that no longer exists and
    silently sanction nothing.
    """

    composition: list[str] = []
    """Repository-relative files, or directory prefixes ending in ``/``."""

    portable_prose: list[str] = []
    """Those composition roots whose prose must still name no provider — a
    declaration every tree renders is written in one of them."""

    generated: list[str] = []
    """Directory prefixes a runtime writes its own tree at.

    Nothing under one is authored, so a rule about a choice has nothing to say
    there: the artifact renders a judgement made in the declaration it is
    compiled from, which is both where the rule can report it and the only
    place a fix survives the next generation."""

    native_dependencies: list[str] = []
    """Additional owners of provider SDK imports, such as integration fixtures."""

    runtime_trees: list[str] = []
    """Each runtime's own tree, as its adapter spells the tree's root.

    Asked of the runtimes rather than listed, as ``generated`` is. A neutral
    module naming one as a path states what only that runtime's adapter may,
    so the native-spelling rule refuses it there; nothing is refused for an
    application that names none."""

    source_roots: list[str] = []
    """Python source directories used to resolve relative imports statically."""

    def sanctions(self, rel_path: Path) -> bool:
        """Whether this application composes natively at that path."""
        posix = rel_path.as_posix()
        return any(
            posix.startswith(root) if root.endswith("/") else posix == root
            for root in self.composition
        )

    def renders(self, rel_path: Path) -> bool:
        """Whether that path is compiled rather than written."""
        return rel_path.as_posix().startswith(tuple(self.generated))

    def sanctions_spelling(self, rel_path: Path) -> bool:
        """Whether that path may also own a provider's own wire words."""
        return self.sanctions(rel_path) and not rel_path.as_posix().startswith(
            tuple(self.portable_prose)
        )


NO_APPLICATION = ApplicationRoots()
"""What an adopter sanctions before it says so: nothing beyond the library."""


def module_name(path: Path) -> str:
    """Infer a dotted module name from a repository-relative Python path.

    The segment a ``src`` directory introduces is an import root, whatever it
    is called and however deep the ``src`` sits: that is what a ``src`` layout
    means, and it is the one fact the library under ``packages/lup/src/``, the
    application under ``src/`` and a nested project under ``studio/src/`` all
    share. A distribution repeats its name above ``src``, so the first segment
    that merely matches a package name is the wrong one —
    ``packages/lup/src/lup/harness/models.py`` read that way is
    ``lup.src.lup.harness.models``, which no import ever names. Nor can the
    roots be a list of packages somebody declared: a nested project's modules
    read ``studio.src.studio.*`` while every importer spelled
    ``studio.eyes.grade``, so each cross-module lookup against them silently
    missed and a rule reported less than it should.

    The first ``src`` is the one, so a package holding a subpackage that
    happens to be called ``src`` keeps it in its module path. A path with no
    ``src`` above it is named from the repository top, the way a test tree or
    an example is imported from there.
    """
    parts = list(PurePosixPath(path.as_posix()).parts)
    introduced = next(
        (
            index
            for index in range(1, len(parts))
            if parts[index - 1] == "src" and parts[index] != "__init__.py"
        ),
        0,
    )
    selected = parts[introduced:]
    if selected[-1] == "__init__.py":
        selected = selected[:-1]
    else:
        selected[-1] = PurePosixPath(selected[-1]).stem
    return ".".join(selected)


def sources_from_paths(paths: list[Path]) -> list[PythonSource]:
    """Read source files and assign import-resolvable module names."""
    return [
        PythonSource(
            path=path,
            module=module_name(path),
            text=path.read_text(encoding="utf-8"),
        )
        for path in paths
    ]


class Refutation(BaseModel, frozen=True):
    """One rule hit something proved does not apply, and the proof.

    A broad line rule is sharpened by what reads more than the line does: the
    regex says the shape is present, and the tree or a checker says this
    instance is not what the rule is about. The AST exemptions for deliberate
    empty-collection defaults and the receiver resolution both speak this
    shape, so the audit
    has one mechanism for "matched, but refuted" — and a `# lup: ignore` left
    guarding a settled line becomes a dead directive the audit reports.

    ``subject`` is the source expression the verdict is about and ``evidence``
    the sentence that justifies it, so a dropped finding is always accountable.
    """

    rule_id: str
    line: int
    subject: str
    evidence: str
    settled: bool
    """Whether a declaration settled the line, or nothing could be shown about it.

    Settled, the subject resolved to a declaration outside the family: the
    line trips nothing, and a directive naming the rule there guards nothing
    and is reported spurious. Unsettled, the checker inferred no type for the
    subject: the rule demands no directive there, and one written there
    stands, because what a checker failed to learn is no evidence against
    the marker either — a gate deleting it on that would be answering from
    its own blindness, and a fuller sweep resolving the receiver would demand
    it straight back. A line carrying several sites of one rule is settled
    only when every one of them is.
    """


class PythonContext(BaseModel):
    """Where prose can live in a source file: comment columns and docstrings.

    Built once per file by :meth:`parse` for Python, whose tokenizer fills
    both fields, or :meth:`parse_typescript` for that family, which has
    comments and no docstrings. ``comment_columns is None`` means Python
    source did not tokenize; both queries then fall back to treating a
    position as prose so a note is never missed.
    """

    comment_columns: dict[int, int] | None
    docstring_lines: set[int]

    @classmethod
    @cache
    def parse(cls, text: str) -> Self:
        """One file's prose map, remembered for the audits that follow.

        Where prose lives is a pure function of the text, and a sweep asks
        several audits about the same file — so computed per caller it
        tokenizes every file once per audit that reads it. The instance is
        shared rather than copied, which is safe because both queries below
        only read it and nothing else reaches the fields.

        Unbounded because a bound would save nothing: the key is text a sweep
        is already holding for every file it walks, so evicting an entry
        keeps no memory that the caller was not keeping anyway.
        """
        return cls(
            comment_columns=python_comment_columns(text),
            docstring_lines=python_docstring_lines(text),
        )

    @classmethod
    def parse_markdown(cls) -> Self:
        """A Markdown page's prose map: no comment, so no directive opens in it.

        The page is prose throughout, and nothing in it is a comment a
        directive could be written in, so every line reads as prose and none
        holds a suppression.
        """
        return cls(comment_columns={}, docstring_lines=set())

    @classmethod
    @cache
    def parse_typescript(cls, text: str) -> Self:
        """One TypeScript-family file's prose map, remembered like Python's.

        Where `//` and `/* */` open is the whole of it: the family has no
        docstring, and its span scan has no failure case, so the map is
        never ``None``.
        """
        return cls(
            comment_columns=typescript_comment_columns(text), docstring_lines=set()
        )

    def suppression_at(self, line_no: int, text: str) -> re.Match[str] | None:
        """The directive at a real comment opening, independent of earlier strings."""
        return source_suppression(text, line_no, self.comment_columns)

    def comment_at(self, line_no: int, col: int) -> bool:
        """Whether a real `#` comment opens at (`line_no`, `col`)."""
        if self.comment_columns is None:
            return True
        return self.comment_columns.get(line_no) == col

    def is_note_context(self, line_no: int, col: int) -> bool:
        """Whether (`line_no`, `col`) sits in a comment or inside a docstring."""
        return self.comment_at(line_no, col) or line_no in self.docstring_lines


class LineProjections(BaseModel):
    """Per-context views of one file's lines for syntax-aware rule scanning.

    ``code`` blanks string-literal and comment tokens — the surface a
    "code"-context rule scans, so identifiers quoted in prose never trip it.
    ``commented`` blanks only string literals, keeping comments visible for
    "comment"-context directive rules. Python is read through its tokenizer
    and the TypeScript family, asked for by ``typescript``, through the
    kernel's span scan; Markdown, asked for by ``markdown``, is prose less
    the code it quotes. When Python text does not tokenize — an incomplete
    fragment — both views fall back to the raw lines and ``tokenized`` is
    False, so a scanner can keep the conservative whole-line scan.
    """

    tokenized: bool
    code: list[str]
    commented: list[str]
    prose: list[str]

    @classmethod
    def parse(cls, text: str, typescript: bool = False, markdown: bool = False) -> Self:
        masked = masked_source(
            text, not typescript and not markdown, typescript, markdown
        )
        return cls(
            tokenized=masked["comment_columns"] is not None,
            code=masked["code"],
            commented=masked["commented"],
            prose=masked["prose"],
        )

    def scan_text(self, line_no: int, context: RuleContext) -> str:
        """The stripped text a rule of `context` scans at 1-based `line_no`."""
        match context:
            case "code":
                lines = self.code
            case "prose":
                lines = self.prose
            case _:
                lines = self.commented
        return lines[line_no - 1].strip()


class NumberedLine(BaseModel):
    """One line of a scanned file, and its 1-based number."""

    number: int
    text: str


class MappedLine[T](BaseModel):
    """What a mapper made of one line, and the 1-based number it came from."""

    number: int
    value: T


class LineCursor:
    """Forward cursor over a file's lines with a take-a-run helper.

    Iterating yields ``(line_no, line)`` with a 1-based number.
    :meth:`take_mapping` pulls the run of following lines a mapper accepts,
    stopping before the first it rejects so iteration resumes there — the shape
    a marker note uses to absorb its continuation lines with no index
    bookkeeping in the caller.
    """

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.pos = 0

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> NumberedLine:
        if self.pos >= len(self.lines):
            raise StopIteration
        line = self.lines[self.pos]
        self.pos += 1
        return NumberedLine(number=self.pos, text=line)

    def take_mapping[T](
        self, mapper: Callable[[int, str], T | None]
    ) -> list[MappedLine[T]]:
        """Consume and map following lines until `mapper` returns ``None``.

        The rejecting line is left unconsumed for the next `__next__`. A mapper
        may return a falsy-but-not-``None`` value (an empty continuation line),
        which is kept; only ``None`` ends the run.
        """
        taken: list[MappedLine[T]] = []
        while self.pos < len(self.lines):
            line_no = self.pos + 1
            mapped = mapper(line_no, self.lines[self.pos])
            if mapped is None:
                break
            self.pos += 1
            taken.append(MappedLine(number=line_no, value=mapped))
        return taken
