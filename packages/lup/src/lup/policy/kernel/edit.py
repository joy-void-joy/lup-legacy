# lup: ignore[empty-collection, import-re, re-call, set-shape, string-split]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""Edit gates: anti-patterns, protected paths, markers, and size."""

import ast
import io
import posixpath
import re
import tokenize
import difflib
from collections import Counter
from collections.abc import Callable, Iterator, Set as AbstractSet
from functools import cache
from typing import NotRequired, TypedDict

from .decision import KernelDecision, handed_over
from .imports import ResolvedImportRule, resolved_import_rules
from .roles import (
    FOREIGN_REPOSITORY_RECOVERY,
    FOREIGN_REPOSITORY_REFERRAL,
    GENERATED_PLUGIN_RECOVERY,
    GENERATED_PLUGIN_REFUSAL,
    GIT_STATE_RECOVERY,
    GIT_STATE_REFUSAL,
    declared_scratch,
    git_state,
    is_generated_plugin_target,
    normalized_path,
    path_role,
    root_matches,
)
from .rows import (
    AcceptanceGuardRow,
    AntiPatternRow,
    DisplacedTargetRow,
    EditRuleRow,
    ImportBoundaryRow,
    PathRoleRow,
    PathRuleRow,
    ResolutionRow,
)
from .syntax import VerbatimText
from .typescript import (
    TYPESCRIPT_SUFFIXES,
    masked_typescript_lines,
    typescript_comment_columns,
)

MARKER_RE = re.compile(r"(#|//)\s*lup\s*:", re.IGNORECASE)
# A review note is any marker whose keyword is not `ignore`, which is the
# anti-pattern escape hatch rather than feedback — the same split
# `lup.harness.codescan.markers` makes when it gathers notes.
# The whitespace sits inside the lookahead: leaving it outside lets the regex
# backtrack to zero width and match `# lup: ignore` after all.
NOTE_RE = re.compile(r"(#|//)\s*lup\s*:(?!\s*ignore\b)", re.IGNORECASE)
# An open note is one still owed an answer, so `solved` is excluded alongside
# `ignore`: a resolution claim is a note that has been acted on and is waiting
# to be checked, and counting it as open would make converting one read as
# deleting it. `template` is excluded for the reason `ignore` is: it is not
# feedback anybody is owed an answer to. A customization marker is answered by
# writing the domain's own code where the scaffold's placeholder stood, which
# leaves no original ask for a claim to be checked against — so it is removed
# outright, and `/lup:init` removing one must not read as deleting feedback.
OPEN_NOTE_RE = re.compile(
    r"(#|//)\s*lup\s*:(?!\s*(?:ignore|solved|template)\b)", re.IGNORECASE
)
SOLVED_NOTE_RE = re.compile(r"(#|//)\s*lup\s*:\s*solved\b", re.IGNORECASE)
# Everything before a note's own words: the marker, and the kind keyword where
# one is present. `defer` carries an optional bracketed gate that belongs to
# the head rather than the text, so waking a deferral reads as the same note
# it always was instead of as one deleted and another added.
NOTE_HEAD_RE = re.compile(
    r"(#|//)\s*lup\s*:\s*(?:(?:solved|defer(?:\s*\[[^\]]*\])?)\s*:\s*)?",
    re.IGNORECASE,
)
IGNORE_RE = re.compile(
    r"(#|//)\s*lup\s*:\s*ignore\b(?:\s*\[(?P<ids>[^\]]*)\])?",
    re.IGNORECASE,
)
# The file-level form is the same directive standing alone on its own line, so
# the leading anchor is what separates it from a trailing inline one. It may
# carry a reason after the ids, introduced by a dash or a colon the way the
# inline form and `defer[<condition>]:` already do — a suppression that cannot
# say why it exists is the shape these rules were written to discourage. The
# audit reads the same object: a kernel that stopped at `]` would deny every
# added line in a file whose directive explains itself, while `dev check`
# called that file exempt.
FILE_IGNORE_RE = re.compile(
    r"^\s*(#|//)\s*lup\s*:\s*ignore\b(?:\s*\[(?P<ids>[^\]]*)\])?"
    r"\s*(?:[-—–:]\s*(?P<reason>\S.*?))?\s*$",
    re.IGNORECASE,
)

SUPPRESSION_COLUMN_LIMIT = 88
"""How wide a line carrying its own directive may be before the reason moves up.

The formatter's limit is what decides whether the canonical inline placement
can hold a directive and the reason that justifies it at all; past it the
choice is a shorter reason or a shorter identifier, and both are the defect
this budget exists to avoid. A project whose formatter is configured
differently passes its own.
"""

# lup: ignore[constant-declaration] — the kernel carries no config, and the
# hook compiled from it must read the same width the sweep does
ABBREVIATION_CHARS = 16
"""The widest prefix that abbreviates an identifier rather than cutting a text.

A git SHA shortens to seven or twelve, a uuid to sixteen, a content hash to
whatever still collides with nothing; all of them stay the name of the thing
they came from. Nothing anyone wrote survives being shortened this far, so a
bound at or under this is read as naming rather than keeping — which is what
lets `silent-truncation` flag every wider slice without a table of receivers
it would have to guess from.
"""


def written_suppression(
    line: str, comment_column: int | None = None
) -> re.Match[str] | None:
    """The directive this line writes, where it writes one rather than quotes one.

    Every reading of a suppression goes through here, because a directive
    inside a backtick span is prose *about* the syntax and silences nothing:
    a changelog entry saying a rule is suppressed with `# lup: ignore[<rule>]`
    declared a suppression of a rule named ``<rule>``, and the gate fired on
    the sentence explaining itself. :func:`quoted_example` already answers
    this for every prose rule and for the marker scanner; a suppression is
    the one reader that was deciding it alone.

    Language-independent on purpose. A code span is how a reader tells an
    example from an instruction in Markdown, in a Python docstring and in a
    comment alike, and no directive is ever written inside one -- so nothing
    here needs to know which file it is reading.
    """
    if comment_column is not None:
        return IGNORE_RE.match(line, comment_column)
    match = IGNORE_RE.search(line)
    return None if match is None or quoted_example(line, match.start()) else match


def source_suppression(
    line: str, number: int, comment_columns: dict[int, int] | None
) -> re.Match[str] | None:
    """Read a directive only where the lexer says the source comment starts.

    A string may mention a directive before the real comment on the same
    line. Its characters cannot shadow that comment or become its authority.
    Unknown lexical context retains the conservative unparsed-source reading.
    """
    if comment_columns is None:
        return written_suppression(line)
    if number not in comment_columns:
        return None
    return written_suppression(line, comment_columns[number])


def standalone_suppression(line: str) -> re.Match[str] | None:
    """The directive on a line that carries nothing but the directive."""
    match = written_suppression(line)
    return None if match is None or line[: match.start()].strip() else match


def continues_comment_block(line: str) -> bool:
    """Whether a line carries a directive's reason onward rather than ending it.

    A plain comment continues the block a directive heads. A directive of its
    own ends it — two markers cannot share a block, and the nearer takes its
    lines — and so does anything that is not a comment at all.

    Named rather than spelled twice because it is also what bounds a search
    for the directive guarding a line: the first line that does not continue
    the block ends every reach from above it.
    """
    return line.lstrip().startswith(("#", "//")) and written_suppression(line) is None


def suppression_reaches(
    lines: list[str], directive_line: int, violation_line: int
) -> bool:
    """Whether a directive on `directive_line` may silence `violation_line`.

    One policy, for every rule alike: a directive sits on the violation's own
    line — the canonical placement — or heads the comment block standing
    directly above it, which is where a reason too long for the column budget
    goes. No rule widens this and none narrows it, so a marker shape valid
    against one rule is valid against all of them.

    Standing alone is what makes the line above a placement rather than an
    accident: an inline directive guards the line it was written on, and a
    rule tripping the line below cannot quietly borrow it.

    A reason worth reading often outruns one line, so the directive may be
    followed by its own continuation comments before what it guards. They are
    continuations rather than neighbours because a directive of their own ends
    the block: two markers cannot share one, and the nearer takes its lines.
    """
    if directive_line == violation_line:
        return True
    if directive_line < 1 or directive_line >= violation_line:
        return False
    if standalone_suppression(lines[directive_line - 1]) is None:
        return False
    return all(
        continues_comment_block(lines[number - 1])
        for number in range(directive_line + 1, violation_line)
    )


def inline_suppression(code: str, directive: str) -> str:
    """One code line carrying a directive that had been standing above it."""
    return f"{code.rstrip()}  {directive.strip()}"


def hoisted_suppression(line: str, match: re.Match[str]) -> list[str]:
    """The two lines one inline directive becomes with its reason hoisted.

    The directive comes first, taking the code's own indentation, which is
    what makes it read as belonging to the line beneath rather than to the
    block around it.
    """
    indent = line[: len(line) - len(line.lstrip())]
    return [f"{indent}{line[match.start() :].strip()}", line[: match.start()].rstrip()]


def python_parses(source: str) -> bool:
    """Whether source is valid Python, which tokenizing alone does not decide.

    A comment inserted after a backslash continuation still tokenizes and
    still will not parse, so only the grammar can say whether moving a line
    left the file intact.
    """
    try:
        ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    return True


def relocated_suppressions(text: str, limit: int = SUPPRESSION_COLUMN_LIMIT) -> str:
    """Rewrite one file so every directive sits at its canonical placement.

    Both placements are accepted, so this changes nothing about what a
    directive silences — only where it is written. Inline is canonical and
    holds until the reason stops fitting: a comment is the one thing the
    formatter cannot wrap, so past the budget an inline directive costs either
    its reason or its line's width, and the first of those is the pressure
    that ends in a shortened identifier.

    Both directions are what make this a normal form rather than a nudge. An
    over-wide inline directive is hoisted onto the line above; a directive
    standing above a line it would fit on is folded back onto it. Every input
    reaches the same output and reaching it twice changes nothing, which is
    what keeps the placement from needing a sweep each time a tree drifts —
    hoisting alone would leave a directive written above a line it fits on
    correct forever, with nothing that ever moves it.

    Non-Python text is returned untouched, and so is any move that would stop
    the file parsing: a directive trailing a backslash continuation has no
    line above it a comment may occupy.
    """
    columns = python_comment_columns(text)
    if columns is None:
        return text
    lines = text.splitlines()
    file_level = file_level_line(text)

    def directive_at(number: int) -> re.Match[str] | None:
        """The directive one line carries, if a comment really opens there.

        The whole-file opt-out is not one: it governs the file rather than the
        line beneath it, and folding it onto that line would silently narrow
        it to one statement.
        """
        if number < 1 or number > len(lines) or number == file_level:
            return None
        match = source_suppression(lines[number - 1], number, columns)
        if match is None or number not in columns or columns[number] != match.start():
            return None
        return match

    def folds_onto(number: int) -> bool:
        """Whether the line below a standalone directive can take it inline.

        It has to be code — a blank or a comment is not what the directive
        guards — and it has to be carrying no directive of its own, since two
        on one line leaves the second unread.
        """
        below = lines[number] if number < len(lines) else ""
        if not below.strip() or below.lstrip().startswith(("#", "//")):
            return False
        return directive_at(number + 1) is None

    def placed() -> Iterator[str]:
        folded = 0
        for number, line in enumerate(lines, start=1):
            if number == folded:
                continue
            match = directive_at(number)
            if match is None:
                yield line
                continue
            if standalone_suppression(line) is None:
                if len(line) <= limit:
                    yield line
                    continue
                yield from hoisted_suppression(line, match)
                continue
            merged = inline_suppression(
                lines[number] if number < len(lines) else "", line
            )
            if not folds_onto(number) or len(merged) > limit:
                yield line
                continue
            folded = number + 1
            yield merged

    revised = "\n".join(placed()) + ("\n" if text.endswith("\n") else "")
    # Hoisting into a file's opening comment block would land the directive
    # where the whole-file form is read, silently widening one line's excuse
    # to every line. A move that changes which directive heads the file is
    # declined, and the reason stays inline where it was written.
    if file_level_line(revised) != file_level_line(text):
        return text
    return revised if python_parses(revised) else text


def relocated_edit_text(
    after: str, start: int, end: int, limit: int = SUPPRESSION_COLUMN_LIMIT
) -> str | None:
    """What an edit's own span becomes once placement is settled, or ``None``.

    ``start`` and ``end`` bound the region of the finished document the edit
    wrote. Placement has to be decided on the whole document — which line a
    directive guards is the line beneath it, and an edit's fragment need not
    contain it — and the answer is then read back out of that region.

    ``None`` says the edit cannot express the move: either nothing moved, or
    the move reached text this call does not write. Declining is free, because
    both placements are valid and the directive stays where its author put it.
    """
    revised = relocated_suppressions(after, limit)
    if revised == after:
        return None
    tail = len(after) - end
    if (
        revised[:start] != after[:start]
        or revised[len(revised) - tail :] != after[end:]
    ):
        return None
    return revised[start : len(revised) - tail]


def covering_suppression_line(
    lines: list[str], violation_line: int, comment_columns: dict[int, int] | None = None
) -> int:
    """The line holding the directive that covers `violation_line`, or 0.

    Both accepted placements, nearest first, so an inline directive is
    preferred over one standing above and the site a refusal quotes is the one
    whoever wrote it would recognize. Above means the head of the comment
    block, which a multi-line reason puts further up than the line itself.
    """
    for candidate in range(violation_line, 0, -1):
        if (
            source_suppression(lines[candidate - 1], candidate, comment_columns)
            is not None
        ):
            return (
                candidate
                if suppression_reaches(lines, candidate, violation_line)
                else 0
            )
        if candidate != violation_line and not lines[candidate - 1].lstrip().startswith(
            ("#", "//")
        ):
            return 0
    return 0


def suppression_placement(violation_line: int) -> str:
    """Name the lines a refusal expected the directive it did not find on.

    The reported failure this answers is a directive that went spurious while
    the violation it meant to guard stayed missing, with nothing in either
    message saying where the two were supposed to meet.
    """
    if violation_line <= 1:
        return "line 1"
    return f"line {violation_line}, or line {violation_line - 1} directly above it"


@cache
def python_tree(source: str) -> ast.Module | None:
    """One file's parsed syntax tree, or ``None`` where it does not parse.

    Every AST reader below asks the same question of the same text, and a
    parse costs more than the walk that follows it — so the tree is built
    once per source and handed to each of them rather than rebuilt per
    caller. A whole-repository sweep runs nine readers over each file, and
    parsed per caller that is nine parses of every file in the tree.

    Unbounded for the reason the prose map is: the key is text the caller is
    already holding, so evicting an entry frees nothing it was not keeping.
    A source that will not parse is remembered as ``None``, which is the same
    answer every caller would compute separately.
    """
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


@cache
def python_nodes(tree: ast.Module) -> list[ast.AST]:
    """Every node under one module, walked once for all of its readers.

    `ast.walk` is a generic breadth-first generator: it allocates a queue and
    reads each node's fields through `getattr` to find the children, and a
    whole-repository sweep runs about ten of those over every file — the
    matchers, the boundary scans and the import readers all asking the same
    tree for the same nodes. The walk is a pure function of the tree, so it
    happens once and the list is what each of them reads.

    Keyed on the tree because that is what a reader holds, and `python_tree`
    above is the only thing that builds one — so an entry lives exactly as
    long as the tree it belongs to was already going to.
    """
    return list(ast.walk(tree))


@cache
def python_nodes_by_type(tree: ast.Module) -> dict[str, list[ast.AST]]:
    """Every node under one module, grouped by the name of its own type.

    A matcher asks about one shape — subscripts, calls, imports — and a
    sweep runs forty of them over each file. Scanning the whole node list per
    matcher is that file walked forty times to answer forty narrow questions;
    grouped once, each of them is a lookup and a short list.

    Keyed on the tree for the reason the walk above is: `python_tree` is the
    only thing that builds one, so an entry lives exactly as long as the tree
    it belongs to already does.
    """
    grouped: dict[str, list[ast.AST]] = {}
    for node in python_nodes(tree):
        grouped.setdefault(type(node).__name__, []).append(node)
    return grouped


class SitePosition(TypedDict):
    """Where one symbol sits, in the coordinates `ast` reports.

    A TypedDict rather than a model because this is the hermetic kernel and
    there is no pydantic here; a pair rather than two loose fields because
    both halves of a position are meaningless apart.
    """

    line: int
    """1-based, as `ast` reports `lineno`."""

    column: int
    """0-based UTF-8 offset, as `ast` reports `col_offset`."""


class MatchSite(TypedDict):
    """One place a rule fires, in the terms every gate reads it in.

    A rule is about sites, and a site is asked two different questions. The
    hook and the audit ask *which line does this fire on*, which is ``line``
    and is all a text-shaped rule ever has. A rule whose verdict turns on a
    type is also asked *which symbol settles it*, and answering that needs a
    position rather than a line — so the selector that already found the node
    reports the positions too, instead of a second selector rediscovering the
    same nodes to compute them.

    ``member`` is the attribute the site is named for: ``get`` in
    ``payload.get(key)``. Resolving it reaches the class that declares it,
    which follows the inheritance chain for free — a receiver can only reach
    ``dict.get`` by being a dict.

    ``receiver`` is the last name of what the member is read from, and is the
    fallback for the members no source declares: a ``TypedDict``'s ``get`` is
    synthesized, so it resolves to nothing while the receiver still resolves
    to the class. It is absent where the receiver ends in no name at all — a
    subscript, a call — which is why it is the fallback rather than the
    question.

    ``subject`` is the unparsed receiver expression, quoted back as the
    evidence for whatever verdict the resolution reaches.
    """

    line: int
    member: NotRequired[SitePosition]
    receiver: NotRequired[SitePosition]
    subject: NotRequired[str]


def sites_at(lines: AbstractSet[int]) -> list[MatchSite]:
    """The sites of a rule that names lines and no symbol to resolve.

    Most rules are one of these: a bare ``except``, an ``import re``, a
    ``# noqa``. Nothing about them turns on what a name means, so they carry
    no position for a checker to answer about, and a resolution pass finds
    nothing to ask.
    """
    return [MatchSite(line=line) for line in sorted(lines)]


def lines_of(sites: list[MatchSite]) -> set[int]:
    """The lines a selector's sites fire on, which is what both gates grade."""
    return {site["line"] for site in sites}


def name_position(node: ast.expr) -> SitePosition | None:
    """Where the last name of an expression sits, or None if it ends in none.

    A checker answers about the symbol a position denotes, and only a name
    denotes one. ``payload`` and ``self.spawned`` end in a name and can be
    asked about; ``payload['outer']`` and ``make()`` end in a bracket, and
    the result they stand for has no position of its own to point at.
    """
    match node:
        case ast.Name(id=name):
            return SitePosition(
                line=node.lineno, column=(node.end_col_offset or len(name)) - 1
            )
        case ast.Attribute(attr=attribute):
            return SitePosition(
                line=node.end_lineno or node.lineno,
                column=(node.end_col_offset or len(attribute)) - 1,
            )
    return None


def nodes_of[NodeT: ast.AST](tree: ast.Module, kind: type[NodeT]) -> list[NodeT]:
    """Every node of one type under a module, read off the grouped index.

    The bucket is keyed by the exact type name, so the narrowing below always
    holds — it is there to say so to the checker rather than to filter.
    """
    grouped = python_nodes_by_type(tree)
    name = kind.__name__
    found = grouped[name] if name in grouped else []
    return [node for node in found if isinstance(node, kind)]


def attribute_call_site(read: ast.Attribute) -> MatchSite:
    """One ``receiver.<attribute>(...)`` call, as a site a checker can settle."""
    line = read.end_lineno or read.value.lineno
    site = MatchSite(
        line=line,
        member=SitePosition(
            line=line, column=(read.end_col_offset or 0) - len(read.attr)
        ),
        subject=ast.unparse(read.value),
    )
    position = name_position(read.value)
    if position is not None:
        site["receiver"] = position
    return site


def attribute_call_sites(
    tree: ast.Module,
    attribute: str,
    keep: Callable[[ast.Call, ast.Attribute], bool],
) -> list[MatchSite]:
    """The sites of every ``receiver.<attribute>(...)`` call a rule keeps.

    The one place the shape "a call on a named member" is spelled. A rule
    supplies the attribute and the condition that is its own — an arity, a
    receiver the tree can already rule out — and takes back sites carrying
    the positions a checker settles them at. Stating the shape once is what
    keeps a resolution pass from asking about calls the rule discarded.
    """
    return [
        attribute_call_site(node.func)
        for node in nodes_of(tree, ast.Call)
        if isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
        and keep(node, node.func)
    ]


@cache
def python_tokens(source: str) -> list[tokenize.TokenInfo] | None:
    """Tokenize Python source, returning ``None`` for incomplete syntax.

    Remembered per source for the reason the tree above is: the four maskers
    and column maps below each tokenize the same text, and one of them
    tokenizes it twice. Callers read the list and never rewrite it, so the
    one instance is shared rather than copied.
    """
    try:
        return list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None


def python_comment_columns(source: str) -> dict[int, int] | None:
    """Map Python line numbers to real comment-token columns."""
    tokens = python_tokens(source)
    if tokens is None:
        return None
    return {
        token.start[0]: token.start[1]
        for token in tokens
        if token.type == tokenize.COMMENT
    }


def docstring_lines(source: str) -> set[int]:
    """Return lines occupied by bare string-expression documentation."""
    tree = python_tree(source)
    if tree is None:
        return set()
    lines: set[int] = set()
    for node in python_nodes(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.end_lineno is not None
        ):
            lines.update(range(node.lineno, node.end_lineno + 1))
    return lines


# lup: ignore[library-default] — Python's own token taxonomy for string text
STRING_TOKEN_TYPES = (
    tokenize.STRING,
    tokenize.FSTRING_START,
    tokenize.FSTRING_MIDDLE,
    tokenize.FSTRING_END,
)
"""Every token type whose characters are string text, not code.

An f-string is not one STRING token from 3.12 on: it lexes as a start
marker, literal middle fragments, and an end marker, with real code tokens
between them for each interpolation. Masking only STRING leaves f-string
prose readable as code — converting a prompt's r-string to an rf-string
exposes its English to the anti-pattern rules — while the interpolations
stay visible here because they are code.
"""


def mask_python_string_literals(source: str) -> list[str]:
    """Blank string-token characters while preserving line and column positions."""
    lines = [list(line) for line in source.splitlines()]
    tokens = python_tokens(source)
    if tokens is None:
        return source.splitlines()
    for token in tokens:
        if token.type not in STRING_TOKEN_TYPES:
            continue
        start_line, start_column = token.start
        end_line, end_column = token.end
        for line_number in range(start_line, end_line + 1):
            line = lines[line_number - 1]
            first = start_column if line_number == start_line else 0
            last = end_column if line_number == end_line else len(line)
            line[first:last] = [" "] * (last - first)
            opens_string = token.type in (tokenize.STRING, tokenize.FSTRING_START)
            if line_number == start_line and last - first >= 2 and opens_string:
                line[first : first + 2] = ["'", "'"]
    return ["".join(line) for line in lines]


def python_code_lines(source: str) -> list[str]:
    """Blank string and comment tokens while preserving line and column positions."""
    lines = [list(line) for line in mask_python_string_literals(source)]
    tokens = python_tokens(source)
    if tokens is None:
        return ["".join(line) for line in lines]
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        start_line, start_column = token.start
        line = lines[start_line - 1]
        line[start_column : token.end[1]] = [" "] * (token.end[1] - start_column)
    return ["".join(line) for line in lines]


def python_prose_lines(source: str) -> list[str]:
    """Each line's sentences, with everything a reader does not read blanked.

    A docstring stands whole and a comment stands from where it opens; code
    goes, so an identifier or a colour literal never reads as something
    somebody wrote in a sentence. Where the text does not tokenize every line
    stands, which over-reports on a fragment rather than going quiet on it.
    """
    lines = source.splitlines()
    columns = python_comment_columns(source)
    if columns is None:
        return lines
    documented = docstring_lines(source)
    kept: list[str] = []
    for number, line in enumerate(lines, start=1):
        if number in documented:
            kept.append(line)
            continue
        opened = columns.get(number)
        kept.append(" " * opened + line[opened:] if opened is not None else "")
    return kept


class MaskedSource(TypedDict):
    """One document's lines as each rule context reads them, and where comments open.

    ``commented`` blanks string text and keeps comments — the surface a
    "comment" rule scans; ``code`` blanks both — the surface a "code" rule
    scans; ``prose`` keeps what a person reads as a sentence, comment and
    docstring alike, and blanks the rest. ``comment_columns`` is where a real
    comment opens on each line, and ``None`` where no grammar mapped the
    text, which is what puts every rule back on the raw line.
    """

    commented: list[str]
    code: list[str]
    prose: list[str]
    comment_columns: dict[int, int] | None


def masked_source(
    source: str, python_source: bool, typescript_source: bool
) -> MaskedSource:
    """Project a document into the surfaces its rules read, by its grammar.

    Python is read through its tokenizer, and where a fragment will not
    tokenize the raw lines stand in with no comment map. The TypeScript family
    is read through :func:`~lup.policy.kernel.typescript.typescript_spans`,
    which has no failure case: what it cannot classify it leaves as code. Text
    of neither family is its own projection, every rule reading the whole line.
    """
    if python_source:
        return MaskedSource(
            commented=mask_python_string_literals(source),
            code=python_code_lines(source),
            prose=python_prose_lines(source),
            comment_columns=python_comment_columns(source),
        )
    if typescript_source:
        commented = masked_typescript_lines(source, comments=False)
        return MaskedSource(
            commented=commented,
            code=masked_typescript_lines(source, comments=True),
            # The family has no docstring, so its prose is its comments.
            prose=commented,
            comment_columns=typescript_comment_columns(source),
        )
    lines = source.splitlines()
    return MaskedSource(commented=lines, code=lines, prose=lines, comment_columns=None)


def quoted_example(line: str, position: int) -> bool:
    """Whether a match at ``position`` sits inside a backtick code span.

    Prose that documents the marker syntax writes it in backticks, which is
    how a reader tells an example from an instruction. Counting those as
    notes made documenting the convention indistinguishable from leaving
    feedback — and made the gate fire on the very text explaining it. Odd
    single-backtick parity catches a marker mid-span; a run directly before
    the marker catches double-backtick quoting, whose even-length run defeats
    the parity check.
    """
    prefix = line[:position]
    return prefix.count("`") % 2 == 1 or prefix.endswith("`")


class LocatedNote(TypedDict):
    """One marker match: where it sits, and the words it carries.

    The line is what lets a vanished note be paired with the code it
    annotated; the text is what lets it be recognised in the next revision.
    A tally supports neither, which is why both are carried rather than
    counted and discarded.
    """

    line: int
    text: str


def notes_outside_examples(
    pattern: re.Pattern[str], text: str, first_line: int = 1
) -> list[LocatedNote]:
    """Locate matches in one chunk of prose, skipping backtick-quoted examples.

    ``first_line`` is where this chunk begins in the file, so a match inside a
    multi-line docstring token reports the line it actually occupies rather
    than the token's.
    """
    return [
        LocatedNote(line=first_line + offset, text=line[match.start() :].strip())
        for offset, line in enumerate(text.splitlines())
        for match in pattern.finditer(line)
        if not quoted_example(line, match.start())
    ]


def notes_in_prose(
    source: str, pattern: re.Pattern[str], python_source: bool = False
) -> list[LocatedNote] | None:
    """Locate pattern matches where prose belongs, not inside ordinary strings.

    `None` where a Python source does not tokenize, because there is then no
    way to tell a comment from a string and the honest answer is that which
    notes the revision holds is unknown. Scanning the whole text instead
    gathers a different population, so differencing it against a tokenised
    one measures the change of regime rather than the change of notes: a
    conflicted file mentioning the marker in a string literal counted one
    higher until its last conflict marker went, and that drop read as
    deleted feedback.
    """
    if not python_source:
        return notes_outside_examples(pattern, source)
    tokens = python_tokens(source)
    if tokens is None:
        return None
    documentation = docstring_lines(source)
    return [
        note
        for token in tokens
        if token.type == tokenize.COMMENT
        or (
            token.type == tokenize.STRING
            and any(
                line in documentation
                for line in range(token.start[0], token.end[0] + 1)
            )
        )
        for note in notes_outside_examples(pattern, token.string, token.start[0])
    ]


def count_in_prose(
    source: str, pattern: re.Pattern[str], python_source: bool = False
) -> int | None:
    """Tally the notes :func:`notes_in_prose` finds, preserving its `None`."""
    located = notes_in_prose(source, pattern, python_source)
    return None if located is None else len(located)


def note_body(text: str) -> str:
    """The words a note carries, with its marker head and kind keyword removed.

    Conversion is what this exists for: `# lup: is parity gated?` and
    `# lup: solved: is parity gated?` are the same ask at two stages, and
    stripping both heads is what lets the second be recognised as the first
    rather than counted as one note lost and one claim gained.
    """
    return NOTE_HEAD_RE.sub("", text, count=1).strip()


def note_subject(lines: list[str], line: int) -> int | None:
    """Which line a note concerns, or `None` where it annotates nothing.

    A note either trails the line it is about or sits above it, so the
    subject is the note's own line when code precedes the marker there, and
    otherwise the next line below carrying anything but another note. Blank
    lines are skipped because a note separated from its subject by one is
    still about it, and stacked notes are skipped because they share the
    subject beneath them.

    Deliberately textual. The kernel has `ast`, but :func:`notes_in_prose`
    already answers `None` for a revision that will not tokenize, and the
    revision that will not tokenize is the one mid-merge — exactly where a
    subject has to be resolvable for the completing edit to land.
    """
    own = lines[line - 1] if 0 < line <= len(lines) else ""
    head = NOTE_RE.search(own)
    if head is None:
        return None
    if own[: head.start()].strip():
        return line
    for offset, candidate in enumerate(lines[line:], start=line + 1):
        if not candidate.strip() or NOTE_RE.search(candidate):
            continue
        return offset
    return None


def deleted_lines(previous: str, updated: str) -> set[int]:
    """Which of ``previous``'s lines this edit removed outright, 1-based.

    Removed, not merely absent. A line rewritten in place reads as gone if
    the revisions are compared as sets, which would make editing the code
    under a note a way to drop the note with it — so `replace` is excluded
    and only `delete` counts. What survives that distinction is the case the
    subject rule is for: code that went away, taking its feedback along.
    """
    matcher = difflib.SequenceMatcher(
        a=previous.splitlines(), b=updated.splitlines(), autojunk=False
    )
    return {
        line
        for tag, start, end, _, _ in matcher.get_opcodes()
        if tag == "delete"
        for line in range(start + 1, end + 1)
    }


def note_bodies(notes: list[LocatedNote]) -> Counter[str]:
    """How many times each note's words appear in a revision.

    A tally rather than a set because the claims an edit *added* are the
    difference between two of these, and a difference needs counts to be
    taken. What reads the result asks only whether a body is present.
    """
    return Counter(note_body(note["text"]) for note in notes)


def spent_notes(
    previous: str,
    updated: str,
    was_open: list[LocatedNote],
    is_open: list[LocatedNote],
    python_source: bool,
) -> list[str]:
    """Every note this edit dropped while its subject stands.

    A note leaves a file honestly three ways: it is still open under the same
    words, its words stand as a `solved:` claim in the revision, or the code
    it annotated went with it. Anything else is feedback stripped off code
    that is still there, which is the one act the gate exists to refuse. All
    of them are returned rather than the first, because the denial quotes
    what was lost and a reader repairing the edit repairs it once.

    Survival is asked of the words, not of each copy of them. A file holding
    the same note twice holds one piece of feedback written in two places, so
    a reader who finds either has it — and counting copies would make tidying
    a duplicate read as a deletion, freezing the code that carried it. The
    same reading spans the lifecycle: a merge conflict can hold one note as
    `defer:` on one side and `solved:` on the other, and resolving it keeps
    one spelling of the same words — the verify pass still finds the claim
    to check, so nothing a reader needs has left the file.
    """
    standing_claims = note_bodies(
        notes_in_prose(updated, SOLVED_NOTE_RE, python_source) or []
    )
    survived = note_bodies(is_open) + standing_claims
    lines = previous.splitlines()
    removed = deleted_lines(previous, updated)
    lost: list[str] = []
    for note in was_open:
        body = note_body(note["text"])
        if survived[body] > 0:
            continue
        subject = note_subject(lines, note["line"])
        # A note annotating nothing in particular is about the file, so only
        # the file's own deletion spends it.
        spent = (
            not updated.strip()
            if subject is None
            else note["line"] in removed and subject in removed
        )
        if not spent:
            lost.append(note["text"])
    return lost


class MarkerVerdict(TypedDict):
    """One marker-gate verdict, and the gate id a project would move it under.

    The gate travels with the decision because the three verdicts this gate
    reaches are about different things — feedback lost, a resolution claim
    lost, feedback added — and a project that moves one of them has said
    nothing about the other two. Handing back the decision alone would leave
    the caller re-deriving which of the three it was from the words in its
    reason.
    """

    gate: str
    decision: KernelDecision


def marker_decision(
    previous: str, updated: str, python_source: bool
) -> MarkerVerdict | None:
    """Judge what this edit did to the file's review notes.

    Deleting feedback is denied rather than asked. An ask is something an
    agent argues its way through in the same turn that wanted the deletion,
    and the deletion is exactly the act nobody can review afterwards — the
    note is gone, so its absence looks identical to a note that never
    existed. What the agent does instead is convert it: `# lup: solved:` in
    front of the original words leaves the claim in the tree, against the
    text it claims to answer, for a later pass to check.

    That later pass retires a claim — or sends it back to being open
    feedback — through its own instrument, `dev comments --retire` and
    `--restore`, which touches only `solved:` claims. No session's edits are
    exempt here: an environment cannot carry this authority, so a grant that
    claims to is ignored.

    Notes are matched by their words rather than tallied, because a tally
    answers only "how many", and four different acts spend a note: dropping
    the feedback, converting it, deleting the code it annotated, and moving
    that code elsewhere. Only the first is the act worth denying, and a
    difference of counts cannot tell them apart — nor can it tell a real
    deletion from one hidden inside a conversion, where the deltas cancel
    and the edit reads as though nothing left.

    So a vanished note is judged against its subject: gone with the code it
    annotated, it was spent by that deletion and nothing is owed. Standing
    code with the note stripped off it is the deletion this gate exists for.
    Where the subject merely moved within the file the note should have
    moved too, so presence anywhere in the revision counts as standing.
    """
    # A revision whose notes could not be established has not been shown to
    # have lost anything, and denying on an unmeasurable difference is what
    # blocked the completing step of every merge resolution: removing a
    # file's last conflict marker is what makes it parse for the first time.
    was_open = notes_in_prose(previous, OPEN_NOTE_RE, python_source)
    is_open = notes_in_prose(updated, OPEN_NOTE_RE, python_source)
    claimed_now = notes_in_prose(updated, SOLVED_NOTE_RE, python_source)
    claimed_before = notes_in_prose(previous, SOLVED_NOTE_RE, python_source)
    if (
        was_open is None
        or is_open is None
        or claimed_now is None
        or claimed_before is None
    ):
        return None
    lost = spent_notes(previous, updated, was_open, is_open, python_source)
    if lost:
        spelled = "\n".join(lost)
        removes = (
            "this edit removes inline review feedback that still has a subject"
            if len(lost) == 1
            else f"this edit removes {len(lost)} pieces of inline review"
            " feedback that still have a subject"
        )
        return MarkerVerdict(
            gate="feedback-removed",
            decision=KernelDecision(
                "deny",
                f"{removes} — {spelled}",
                recovery="Resolving a note means replacing `# lup:` with"
                " `# lup: solved:` and keeping its text, so the claim can be"
                " checked against what was asked; deleting it leaves nothing to"
                " check. Where the note was mistaken rather than answered,"
                " withdraw it with `dev comments --withdraw file:line --reason`.",
            ),
        )
    # Asked of the words, as survival is for open notes: a claim is lost when
    # nothing in the revision still carries it. A second copy tidied away
    # retires nothing, because the review pass still finds the claim standing
    # and can still check it against what was asked. An open note with the
    # same words counts too — dropping the claim then is not retiring the
    # ask but re-opening it, which is the stronger obligation and the other
    # honest way out of a conflict holding one note in two lifecycle states.
    surviving_claims = note_bodies(claimed_now) + note_bodies(is_open)
    dropped_claims = [
        note["text"]
        for note in claimed_before
        if surviving_claims[note_body(note["text"])] == 0
    ]
    if dropped_claims:
        spelled = "\n".join(dict.fromkeys(dropped_claims))
        claims = (
            "a `# lup: solved:` claim"
            if len(dropped_claims) == 1
            else (f"{len(dropped_claims)} `# lup: solved:` claims")
        )
        return MarkerVerdict(
            gate="claim-removed",
            decision=KernelDecision(
                "deny",
                f"this edit removes {claims} — {spelled}",
                recovery="Only the review pass retires one: it either confirms"
                " the claim and removes the note (`dev comments --retire"
                " file:line`), or restores it to open feedback (`dev comments"
                " --restore file:line`).",
            ),
        )
    added_bodies = note_bodies(is_open) - note_bodies(was_open)
    if added_bodies:
        added = [
            f"line {note['line']}: {note['text']}"
            for note in is_open
            if added_bodies[note_body(note["text"])] > 0
        ]
        return MarkerVerdict(
            gate="feedback-added",
            decision=KernelDecision(
                "ask",
                "edit adds inline review feedback — " + "; ".join(added),
            ),
        )
    return None


def empty_collection_literal(node: ast.expr | None) -> bool:
    """Whether an expression builds an empty dict, list, or set on the spot.

    The one question two rules ask: `empty-collection` to tell a deliberate
    default from a build-then-append seed, and `default-factory` to tell a
    factory an annotated literal could replace from one that does real work.
    Answering it in one place is what keeps the two from disagreeing about the
    same line.
    """
    match node:
        case ast.Dict(keys=[]) | ast.List(elts=[]):
            return True
        case ast.Call(func=ast.Name(id="set"), args=[], keywords=[]):
            return True
    return False


def empty_collection_exempt_lines(source: str) -> set[int]:
    """Return empty-collection lines whose AST context makes the seed deliberate."""
    tree = python_tree(source)
    if tree is None:
        return set()

    def is_self_attribute(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        )

    exempt: set[int] = set()

    def mark(value: ast.expr | None) -> None:
        if value is not None and empty_collection_literal(value):
            exempt.add(value.lineno)

    def is_loop(node: ast.AST) -> bool:
        return isinstance(node, ast.For | ast.AsyncFor | ast.While)

    def mutated_name(node: ast.AST) -> str | None:
        match node:
            case ast.Call(func=ast.Attribute(value=ast.Name(id=name), attr=attr)) if (
                attr
                in (
                    "append",
                    "appendleft",
                    "extend",
                    "add",
                    "update",
                    "insert",
                    "setdefault",
                )
            ):
                return name
            case ast.Assign(targets=[ast.Subscript(value=ast.Name(id=name))]):
                return name
            case ast.AugAssign(
                target=ast.Name(id=name) | ast.Subscript(value=ast.Name(id=name))
            ):
                return name
        return None

    def exempt_scope_seeds(scope: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        feeding: dict[str, list[bool]] = {}
        for loop in ast.walk(scope):
            if not is_loop(loop):
                continue
            # One walk answers both questions the loop is asked — whether it
            # tolerates a failure, and which names it feeds — and a nested
            # loop is walked once per ancestor already.
            inside = list(ast.walk(loop))
            tolerant = any(isinstance(inner, ast.Try) for inner in inside)
            for inner in inside:
                name = mutated_name(inner)
                if name is not None:
                    feeding.setdefault(name, []).append(tolerant)

        def visit(node: ast.AST, in_loop: bool) -> None:
            for child in ast.iter_child_nodes(node):
                match child:
                    case (
                        ast.FunctionDef()
                        | ast.AsyncFunctionDef()
                        | ast.ClassDef()
                        | ast.Lambda()
                    ):
                        continue
                    case (
                        ast.Assign(targets=[ast.Name(id=name)], value=value)
                        | ast.AnnAssign(target=ast.Name(id=name), value=value)
                    ) if empty_collection_literal(value):
                        loops = feeding[name] if name in feeding else None
                        marked = (
                            loops is not None
                            if in_loop
                            else loops is None or all(loops)
                        )
                        if marked:
                            mark(value)
                visit(child, in_loop or is_loop(child))

        visit(scope, False)

    for node in python_nodes(tree):
        match node:
            case ast.Call(keywords=keywords):
                for keyword in keywords:
                    mark(keyword.value)
            case ast.ExceptHandler(body=handler_body):
                for statement in handler_body:
                    match statement:
                        case ast.Assign(value=value) | ast.AnnAssign(value=value):
                            mark(value)
            case ast.Module(body=body) | ast.ClassDef(body=body):
                for statement in body:
                    if isinstance(statement, ast.AnnAssign):
                        mark(statement.value)
            case ast.FunctionDef() | ast.AsyncFunctionDef():
                if node.name == "__init__":
                    for statement in ast.walk(node):
                        match statement:
                            case ast.Assign(targets=targets, value=value) if all(
                                is_self_attribute(target) for target in targets
                            ):
                                mark(value)
                            case ast.AnnAssign(target=target, value=value) if (
                                is_self_attribute(target)
                            ):
                                mark(value)
                exempt_scope_seeds(node)
    return exempt


def empty_collection_factory(node: ast.expr) -> bool:
    """Whether a ``default_factory=`` argument only ever returns an empty collection.

    ``list``/``dict``/``set`` named as the factory, and the lambda spellings of
    the same thing, are what an annotated literal default says instead. The
    literal question is :func:`empty_collection_literal`'s, asked once.
    """
    match node:
        case ast.Name(id="list" | "dict" | "set"):
            return True
        case ast.Lambda(body=body):
            return empty_collection_literal(body)
    return False


def slice_exempt_lines(source: str) -> set[int]:
    """Return bounded-prefix-slice lines the tree shows to be something else.

    The pattern reads every ``[:n]``, because what separates an honest prefix
    from a destroyed tail is what becomes of the remainder, and no character
    class can see that. Four contexts settle it without types.

    A slice of a digest derives an identifier. ``hexdigest()[:12]`` shortens a
    hash whose prefix identifies the same thing the whole of it did, and there
    is no content behind it to lose.

    A bound at or under ``ABBREVIATION_CHARS`` abbreviates rather than cuts.
    Nothing anyone wrote survives being shortened to sixteen characters, so a
    slice that narrow is never someone keeping part of a text — it is a git
    SHA, a uuid, a short key. Reading the width rather than the receiver's
    name is what makes this decidable: ``commit[:12]`` carries no type saying
    it is a hash, and a rule that guessed from the identifier would clear a
    variable called ``commit_message`` for the same reason.

    A line carrying the complementary slice is a split. ``x[:n]`` beside
    ``x[n:]`` keeps everything it started with, which is this rule's remedy
    rather than its defect.

    A slice a test reads is sniffing. ``raw[:100].startswith(...)`` asks a
    question of a prefix and stores nothing, so whatever it came from is still
    whole wherever it lives.

    Source that does not parse clears nothing. The rule is soft, so the most an
    unrefined finding costs is a directive carrying a reason.
    """
    tree = python_tree(source)
    if tree is None:
        return set()

    def prefix_slices(node: ast.AST) -> Iterator[ast.Subscript]:
        """Every ``[:n]`` at or under this node."""
        for inner in ast.walk(node):
            match inner:
                case ast.Subscript(slice=ast.Slice(lower=None, upper=upper)) if (
                    upper is not None
                ):
                    yield inner

    def spanned(node: ast.Subscript) -> range:
        """Every line the sliced expression covers.

        A chain that opens on one line and closes with its ``[:n]`` on another
        reports at the bracket while the node starts at the receiver, so
        clearing only ``lineno`` clears a line the pattern never matched and
        leaves the one it did.
        """
        return range(node.lineno, (node.end_lineno or node.lineno) + 1)

    def derives_a_digest(node: ast.expr) -> bool:
        """Whether the sliced value came from a hash or a uuid."""
        return any(
            isinstance(inner, ast.Attribute) and inner.attr in ("hexdigest", "hex")
            for inner in ast.walk(node)
        )

    def abbreviates(bounds: ast.expr | None) -> bool:
        """Whether the bound is too narrow to be a portion of anything written."""
        return (
            isinstance(bounds, ast.Constant)
            and isinstance(bounds.value, int)
            and bounds.value <= ABBREVIATION_CHARS
        )

    split_lines = {
        node.lineno
        for node in nodes_of(tree, ast.Subscript)
        if isinstance(node.slice, ast.Slice)
        and node.slice.lower is not None
        and node.slice.upper is None
    }

    def tested() -> Iterator[int]:
        """Lines whose prefix slice is read by a comparison rather than kept."""
        for node in python_nodes(tree):
            match node:
                case (
                    ast.Compare()
                    | ast.Call(func=ast.Attribute(attr="startswith" | "endswith"))
                ):
                    for found in prefix_slices(node):
                        yield from spanned(found)

    def settled() -> Iterator[int]:
        """Lines whose prefix slice is a digest, a split, or an abbreviation."""
        for node in prefix_slices(tree):
            if (
                node.lineno in split_lines
                or derives_a_digest(node.value)
                or (isinstance(node.slice, ast.Slice) and abbreviates(node.slice.upper))
            ):
                yield from spanned(node)

    return set(tested()) | set(settled())


def imports_of(tree: ast.Module, modules: AbstractSet[str]) -> set[int]:
    """Lines importing one of `modules`, by either spelling.

    ``import re``, ``import re as r``, ``from re import sub`` and
    ``from re.x import y`` all name ``re``, and a dotted module is named by
    its root as well as by itself — ``rich.progress`` is reached by importing
    ``rich`` and by importing ``rich.progress``, and a rule about the latter
    means both. A relative import names no module and is never one of these.
    """

    def names(target: str) -> bool:
        return target in modules or any(
            target.startswith(f"{module}.") for module in modules
        )

    def found() -> Iterator[int]:
        for node in python_nodes(tree):
            match node:
                case ast.Import(names=aliases):
                    if any(names(alias.name) for alias in aliases):
                        yield node.lineno
                case ast.ImportFrom(module=str() as module, level=0):
                    if names(module):
                        yield node.lineno

    return set(found())


def imported_symbols_of(
    tree: ast.Module, module: str, symbols: AbstractSet[str]
) -> set[int]:
    """Lines taking one of `symbols` out of `module` by name."""
    return {
        node.lineno
        for node in nodes_of(tree, ast.ImportFrom)
        if node.module == module and any(alias.name in symbols for alias in node.names)
    }


def dotted_of(node: ast.expr) -> str:
    """The dotted spelling of a name or attribute chain, or ``""``.

    ``os.path.join`` reads back as it was written, which is what lets a rule
    name the function it refuses in the words its diagnostic uses. Anything
    with a call or a subscript in the chain has no dotted spelling and is not
    one of these.
    """
    match node:
        case ast.Name(id=name):
            return name
        case ast.Attribute(value=value, attr=attr):
            root = dotted_of(value)
            return f"{root}.{attr}" if root else ""
    return ""


def calls_of(tree: ast.Module, dotted: AbstractSet[str]) -> set[int]:
    """Lines calling one of `dotted`, by the spelling the call was written in.

    The call's own line rather than the enclosing statement's: an argument
    list spanning lines reports where the callee is, which is where a reader
    looks and where a directive goes.
    """
    return {
        node.func.lineno
        for node in nodes_of(tree, ast.Call)
        if dotted_of(node.func) in dotted
    }


def methods_of(tree: ast.Module, attrs: AbstractSet[str]) -> set[int]:
    """Lines calling one of `attrs` as a method on something.

    The receiver is whatever it is — no tree says whether it is a string —
    so this selects the call shape and leaves the receiver's type to the
    oracle where a rule asks for one.
    """
    return {
        node.func.end_lineno or node.func.value.lineno
        for node in nodes_of(tree, ast.Call)
        if isinstance(node.func, ast.Attribute) and node.func.attr in attrs
    }


def attributes_of(tree: ast.Module, dotted: AbstractSet[str]) -> set[int]:
    """Lines reaching one of `dotted` as an attribute path, called or not.

    ``os.environ`` is the subject wherever it is written — subscripted,
    passed, or only read — so this selects the path itself rather than a call
    around it.
    """
    return {
        node.lineno
        for node in nodes_of(tree, ast.Attribute)
        if dotted_of(node) in dotted
    }


# lup: ignore[library-default] — the extraction libraries the rule's own pattern already named; the kernel carries no config
PDF_LIBRARIES = frozenset(  # lup: ignore[frozenset-shape] — a membership test, no keys
    {
        "fitz",
        "pymupdf",
        "pypdf",
        "PyPDF2",
        "PyPDF4",
        "pdfplumber",
        "pdfminer",
        "pypdfium2",
    }
)
"""Every PDF text-extraction library the rule names, by import root."""


# lup: ignore[library-default] — the regex module's own entry points, fixed by what it exports; the kernel carries no config
RE_FUNCTIONS = frozenset(  # lup: ignore[frozenset-shape] — a membership test, no keys
    {"compile", "search", "match", "fullmatch", "sub", "findall", "split"}
)
"""The regex-module entry points the rule names, unqualified."""

# lup: ignore[library-default] — os's own spellings for handing a command to a shell; the kernel carries no config
OS_SHELL_CALLS = frozenset(  # lup: ignore[frozenset-shape] — a membership test, no keys
    {
        "os.system",
        "os.popen",
        "os.execl",
        "os.execle",
        "os.execlp",
        "os.execlpe",
        "os.execv",
        "os.execve",
        "os.execvp",
        "os.execvpe",
    }
)
"""Every `os` entry point that hands a command to a shell or replaces the process."""

# lup: ignore[library-default] — os's own filesystem calls, fixed by what pathlib re-spells; the kernel carries no config
OS_FILE_CALLS = frozenset(  # lup: ignore[frozenset-shape] — a membership test, no keys
    {
        "getcwd",
        "chdir",
        "listdir",
        "scandir",
        "walk",
        "mkdir",
        "makedirs",
        "rmdir",
        "removedirs",
        "remove",
        "unlink",
        "rename",
        "renames",
        "replace",
        "link",
        "symlink",
        "readlink",
        "stat",
        "lstat",
        "chmod",
        "chown",
    }
)
"""Every `os` filesystem call `pathlib` has its own spelling for."""


def subscripts_of(tree: ast.Module, names: AbstractSet[str]) -> set[int]:
    """Lines subscripting one of `names`, however the name was reached.

    ``Generic[T]`` and ``typing.Generic[T]`` are the same declaration, so the
    terminal name is what decides rather than the path taken to it.
    """
    return {
        node.lineno
        for node in nodes_of(tree, ast.Subscript)
        if dotted_of(node.value).rsplit(".", 1)[-1] in names
    }


# lup: ignore[library-default] — the typing aliases PEP 604 and PEP 585 replaced; the kernel carries no config
CAPITALIZED_GENERICS = frozenset(  # lup: ignore[frozenset-shape] — a membership test
    {"List", "Dict", "Tuple", "Set"}
)
"""The capitalized `typing` aliases the builtin generics replaced."""

# lup: ignore[library-default] — the scalar builtins a payload map's value would be; the kernel carries no config
SCALAR_TYPES = frozenset(  # lup: ignore[frozenset-shape] — a membership test, no keys
    {"str", "int", "float", "bool", "bytes", "complex"}
)
"""Every builtin whose values carry no fields of their own."""


def annotations_of(tree: ast.Module) -> Iterator[ast.expr]:
    """Every expression written where a type annotation goes.

    A variable's declared type, a parameter's, and a return — the three
    places a name means "the type of this" rather than a value. Reading them
    is what separates ``x: object`` from a dict entry keyed ``"x"`` whose
    value is the builtin, which a colon in a character class cannot do.

    An unused parameter is not one of these. Its annotation belongs to
    somebody else's callback signature rather than to a value this code was
    going to narrow, and the leading underscore is this repository's own mark
    for one — the same convention that exempts it from the privacy rule.
    """
    for node in python_nodes(tree):
        match node:
            case ast.AnnAssign(annotation=annotation):
                yield annotation
            case ast.arg(arg=name, annotation=ast.expr() as annotation) if (
                not name.startswith("_")
            ):
                yield annotation
            case (
                ast.FunctionDef(returns=ast.expr() as annotation)
                | ast.AsyncFunctionDef(returns=ast.expr() as annotation)
            ):
                yield annotation


def annotated_exactly(tree: ast.Module, names: AbstractSet[str]) -> set[int]:
    """Lines annotating something as exactly one of `names`.

    Exactly, so a name that is only a member of the annotation does not
    count: ``list[object]`` says what it holds, and ``X | BaseModel`` names a
    union that the bare form does not. That is the reach the patterns bought
    with a lookahead for a closing bracket or a pipe, said directly.
    """
    return {
        annotation.lineno
        for annotation in annotations_of(tree)
        if dotted_of(annotation).rsplit(".", 1)[-1] in names
    }


def any_type_sites(source: str) -> list[MatchSite]:
    """Lines reaching `Any`, as an annotation, inside one, or by import.

    Every use, because the rule refuses the type rather than one position of
    it: `list[Any]` and `cast(Any, x)` say exactly what `x: Any` says, and
    importing it is reaching for it too.
    """
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(
        imported_symbols_of(tree, "typing", {"Any"})
        | {node.lineno for node in nodes_of(tree, ast.Name) if node.id == "Any"}
    )


def bare_object_sites(source: str) -> list[MatchSite]:
    """Lines annotating something as bare `object`."""
    tree = python_tree(source)
    return sites_at(set() if tree is None else annotated_exactly(tree, {"object"}))


def bare_basemodel_sites(source: str) -> list[MatchSite]:
    """Lines annotating a parameter or return as exactly `BaseModel`."""
    tree = python_tree(source)
    return sites_at(set() if tree is None else annotated_exactly(tree, {"BaseModel"}))


def frozenset_shape_sites(source: str) -> list[MatchSite]:
    """Lines declaring or constructing a frozenset.

    The annotation, the subscripted shape, and the constructor alike — each
    one is the declaration the rule is about, and a comprehension handed to
    the constructor is still a frozenset being declared.
    """
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(
        (
            annotated_exactly(tree, {"frozenset"})
            | subscripts_of(tree, {"frozenset"})
            | calls_of(tree, {"frozenset"})
        )
    )


def set_shape_sites(source: str) -> list[MatchSite]:
    """Lines declaring or constructing a `set`.

    The declaration is the subject, so an annotation, a subscripted shape and
    a call to the constructor all count, while a set comprehension does not —
    the rule's own remedy is to reach for one of those locally instead of
    declaring the set as the interface.
    """
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(
        (
            annotated_exactly(tree, {"set"})
            | subscripts_of(tree, {"set"})
            | calls_of(tree, {"set"})
        )
    )


def bare_except_sites(source: str) -> list[MatchSite]:
    """Lines opening an `except:` that names no exception."""
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(
        {node.lineno for node in nodes_of(tree, ast.ExceptHandler) if node.type is None}
    )


def except_baseexception_sites(source: str) -> list[MatchSite]:
    """Lines catching `BaseException`, alone or among others."""
    tree = python_tree(source)
    if tree is None:
        return []

    def caught(node: ast.expr | None) -> Iterator[str]:
        match node:
            case ast.Tuple(elts=elts):
                for element in elts:
                    yield from caught(element)
            case ast.expr():
                yield dotted_of(node).rsplit(".", 1)[-1]

    return sites_at(
        {
            node.lineno
            for node in nodes_of(tree, ast.ExceptHandler)
            if "BaseException" in set(caught(node.type))
        }
    )


def global_statement_sites(source: str) -> list[MatchSite]:
    """Lines declaring a name `global`."""
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at({node.lineno for node in nodes_of(tree, ast.Global)})


def chain_continuation(node: ast.If) -> ast.If | None:
    """The `if` an `else` holds alone, which is what an `elif` parses to.

    Both spellings are one tree, so a lone `if` nested in an `else` is the
    same chain written deeper, and this reads them alike.
    """
    match node.orelse:
        case [ast.If() as tail]:
            return tail
        case _:
            return None


def elif_chain_sites(source: str) -> list[MatchSite]:
    """Lines heading an `if` whose `else` holds nothing but another `if`.

    Reported at the head rather than at every arm, because the remedy is one
    rewrite of the whole chain, and a directive keeping one stands at one line.
    """
    tree = python_tree(source)
    if tree is None:
        return []
    branches = nodes_of(tree, ast.If)
    continued = {
        id(tail) for node in branches if (tail := chain_continuation(node)) is not None
    }
    return sites_at(
        {
            node.lineno
            for node in branches
            if id(node) not in continued and chain_continuation(node) is not None
        }
    )


def irrefutable(pattern: ast.pattern) -> bool:
    """Whether a case pattern matches every subject, binding at most a name.

    `_` and a bare capture are the two spellings; an or-pattern ending in one
    is one too, since its last alternative catches whatever the rest let by.
    """
    match pattern:
        case ast.MatchAs(pattern=None):
            return True
        case ast.MatchAs(pattern=ast.pattern() as inner):
            return irrefutable(inner)
        case ast.MatchOr(patterns=[*_, last]):
            return irrefutable(last)
        case _:
            return False


def wildcard_guard_sites(source: str) -> list[MatchSite]:
    """Lines opening a guarded `case` whose pattern matches anything.

    `case _ if …` and `case name if …` bind nothing the guard could not have
    read off the subject itself, so the arm decides on the guard alone.
    """
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(
        {
            arm.pattern.lineno
            for arm in nodes_of(tree, ast.match_case)
            if arm.guard is not None and irrefutable(arm.pattern)
        }
    )


def all_export_sites(source: str) -> list[MatchSite]:
    """Lines binding `__all__`.

    A module's export list wherever it is bound — assigned, annotated, or
    appended to — rather than wherever those eight characters sit next to an
    equals sign.
    """
    tree = python_tree(source)
    if tree is None:
        return []

    def binds(node: ast.AST) -> Iterator[ast.expr]:
        match node:
            case ast.Assign(targets=targets):
                yield from targets
            case ast.AnnAssign(target=target) | ast.AugAssign(target=target):
                yield target

    return sites_at(
        {
            target.lineno
            for node in python_nodes(tree)
            for target in binds(node)
            if dotted_of(target) == "__all__"
        }
    )


def model_config_sites(source: str) -> list[MatchSite]:
    """Lines binding `model_config` in a class body.

    The class body is what makes it pydantic's configuration rather than an
    ordinary name, and the tree says which statements are in one — where the
    pattern had to settle for the name sitting at the start of a line.
    """
    tree = python_tree(source)
    if tree is None:
        return []

    def bound(node: ast.stmt) -> Iterator[ast.expr]:
        match node:
            case ast.Assign(targets=targets):
                yield from targets
            case ast.AnnAssign(target=target):
                yield target

    return sites_at(
        {
            target.lineno
            for node in nodes_of(tree, ast.ClassDef)
            for statement in node.body
            for target in bound(statement)
            if dotted_of(target) == "model_config"
        }
    )


def private_function_sites(source: str) -> list[MatchSite]:
    """Lines defining a function whose name claims to be private.

    A dunder is the language's own protocol rather than a privacy claim, and
    a lone underscore names something deliberately unused, so neither is one
    of these.
    """
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(
        {
            node.lineno
            for node in [
                *nodes_of(tree, ast.FunctionDef),
                *nodes_of(tree, ast.AsyncFunctionDef),
            ]
            if node.name.startswith("_")
            and not node.name.startswith("__")
            and len(node.name) > 1
        }
    )


def private_class_sites(source: str) -> list[MatchSite]:
    """Lines defining a class whose name claims to be private."""
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(
        {
            node.lineno
            for node in nodes_of(tree, ast.ClassDef)
            if node.name.startswith("_")
            and not node.name.startswith("__")
            and len(node.name) > 1
        }
    )


def private_variable_sites(source: str) -> list[MatchSite]:
    """Lines binding a module-level name that claims to be private.

    Module level, because that is the scope a name is published from: a local
    called ``_seen`` is nobody's interface, and the pattern reached for the
    same thing by requiring the name to start its line. A tuple unpacking
    binds several names at once and is not a declaration of any one of them.
    """
    tree = python_tree(source)
    if tree is None:
        return []

    def bound(node: ast.stmt) -> Iterator[ast.expr]:
        match node:
            case ast.Assign(targets=[ast.Name() as target]):
                yield target
            case ast.AnnAssign(target=ast.Name() as target):
                yield target

    return sites_at(
        {
            target.lineno
            for statement in tree.body
            for target in bound(statement)
            if isinstance(target, ast.Name)
            and target.id.startswith("_")
            and not target.id.startswith("__")
            and len(target.id) > 1
        }
    )


def namedtuple_sites(source: str) -> list[MatchSite]:
    """Lines reaching a named tuple, by either spelling.

    `typing.NamedTuple` is subclassed and `collections.namedtuple` is called,
    so the rule is about the name wherever it is reached — the base, the
    call, and the import that brings either in.
    """
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(
        (
            imported_symbols_of(tree, "typing", {"NamedTuple"})
            | imported_symbols_of(tree, "collections", {"namedtuple"})
            | {
                node.lineno
                for node in nodes_of(tree, ast.Name)
                if node.id in ("NamedTuple", "namedtuple")
            }
        )
    )


def generic_base_sites(source: str) -> list[MatchSite]:
    """Lines declaring a `Generic[...]` base."""
    tree = python_tree(source)
    return sites_at(set() if tree is None else subscripts_of(tree, {"Generic"}))


def typing_union_sites(source: str) -> list[MatchSite]:
    """Lines spelling a union as `Optional[...]` or `Union[...]`."""
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(subscripts_of(tree, {"Optional", "Union"}))


def typing_generics_sites(source: str) -> list[MatchSite]:
    """Lines subscripting a capitalized `typing` alias."""
    tree = python_tree(source)
    return sites_at(
        set() if tree is None else subscripts_of(tree, CAPITALIZED_GENERICS)
    )


def mapping_value_lines(source: str, values: AbstractSet[str]) -> set[int]:
    """Lines declaring a string-keyed mapping whose values are in `values`.

    A union counts as its members: ``dict[str, str | None]`` is the same open
    map of scalars that ``dict[str, str]`` is, and the annotation reaching a
    scalar through a union is what the pattern's word boundary happened to
    catch and what reading the tree states outright.
    """
    tree = python_tree(source)
    if tree is None:
        return set()

    def reaches(node: ast.expr) -> bool:
        match node:
            case ast.BinOp(left=left, right=right):
                return reaches(left) or reaches(right)
            case ast.Constant(value=None):
                return False
        return dotted_of(node).rsplit(".", 1)[-1] in values

    return {
        node.lineno
        for node in nodes_of(tree, ast.Subscript)
        if dotted_of(node.value).rsplit(".", 1)[-1]
        in ("dict", "Mapping", "MutableMapping")
        and isinstance(node.slice, ast.Tuple)
        and len(node.slice.elts) == 2
        and dotted_of(node.slice.elts[0]).rsplit(".", 1)[-1] == "str"
        and reaches(node.slice.elts[1])
    }


def dict_str_object_sites(source: str) -> list[MatchSite]:
    """Lines declaring a string-keyed map of bare `object`."""
    return sites_at(mapping_value_lines(source, {"object"}))


def dict_str_payload_sites(source: str) -> list[MatchSite]:
    """Lines declaring a string-keyed map of scalars."""
    return sites_at(mapping_value_lines(source, SCALAR_TYPES))


def re_call_sites(source: str) -> list[MatchSite]:
    """Lines calling a regex-module entry point."""
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(calls_of(tree, {f"re.{name}" for name in RE_FUNCTIONS}))


def cast_sites(source: str) -> list[MatchSite]:
    """Lines calling `cast`."""
    tree = python_tree(source)
    return sites_at(set() if tree is None else calls_of(tree, {"cast", "typing.cast"}))


def eval_exec_sites(source: str) -> list[MatchSite]:
    """Lines calling `eval` or `exec` as builtins.

    A method of the same name is somebody else's API — a database cursor, a
    template engine — and never the builtin this refuses.
    """
    tree = python_tree(source)
    return sites_at(set() if tree is None else calls_of(tree, {"eval", "exec"}))


def utcnow_sites(source: str) -> list[MatchSite]:
    """Lines calling `utcnow`, however the module is spelled."""
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(methods_of(tree, {"utcnow"}) | calls_of(tree, {"utcnow"}))


def os_shell_sites(source: str) -> list[MatchSite]:
    """Lines handing a command to a shell through `os`."""
    tree = python_tree(source)
    return sites_at(set() if tree is None else calls_of(tree, OS_SHELL_CALLS))


def os_file_ops_sites(source: str) -> list[MatchSite]:
    """Lines reaching a filesystem operation `pathlib` already spells."""
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(calls_of(tree, {f"os.{name}" for name in OS_FILE_CALLS}))


def os_path_sites(source: str) -> list[MatchSite]:
    """Lines reaching `os.path`, called or only named."""
    tree = python_tree(source)
    return sites_at(set() if tree is None else attributes_of(tree, {"os.path"}))


def os_environ_sites(source: str) -> list[MatchSite]:
    """Lines reaching the process environment through `os`."""
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(attributes_of(tree, {"os.environ", "os.getenv"}))


def suppress_sites(source: str) -> list[MatchSite]:
    """Lines reaching `contextlib.suppress` by its qualified name."""
    tree = python_tree(source)
    return sites_at(
        set() if tree is None else attributes_of(tree, {"contextlib.suppress"})
    )


def comment_directive_lines(source: str, directive: re.Pattern[str]) -> set[int]:
    """Lines whose own comment opens with one suppression directive.

    A comment is the one construct the parser drops, so the three rules about
    suppression spellings read the token stream instead. That is the same
    lexer the parser runs, kept per source here, so it answers what a `#`
    actually opens: characters inside a string literal are a STRING token and
    were never a comment to begin with.

    The pattern is anchored at the comment's own opening, which is what tells
    a suppression from prose about one. `# never write # noqa` is a sentence
    with the spelling in it and silences nothing; searching the whole line
    reported it, and the only way past a denial like that was a directive
    guarding a line that guarded nothing.
    """
    tokens = python_tokens(source)
    if tokens is None:
        return set()
    return {
        token.start[0]
        for token in tokens
        if token.type == tokenize.COMMENT and directive.match(token.string) is not None
    }


# The three below bind one directive each to the one-argument shape a matcher
# is resolved and called through. `functools.partial` would say the same thing
# and lose the name, which is what a row carries a matcher as.
TYPE_IGNORE_DIRECTIVE_RE = re.compile(r"#\s*type:\s*ignore\b")
PYRIGHT_IGNORE_DIRECTIVE_RE = re.compile(r"#\s*pyright:\s*ignore\b")
NOQA_DIRECTIVE_RE = re.compile(r"#\s*noqa\b")

HISTORICAL_VOICE_RE = re.compile(
    r"\bused to be\b|\bpreviously\b|\bformerly\b|\brenamed from\b"
    r"|\bin the past\b|\bbefore this change\b"
    r"|\b(?:it|this|that|they|we|which|one|there) used to\b"
    r"|\bthe old (?:rule|refusal|comparison|arrangement|fallback"
    r"|behaviour|behavior|design|version)\b"
    r"|\bduring the \w+ migration\b|\bfor compatibility (?:consumers|reasons)\b"
    r"|(?<!&)#\d{2,5}(?![0-9A-Fa-f;])",
    re.IGNORECASE,
)
"""The spellings that record a change rather than state what is.

Deliberately narrow, because a rule that cries wolf is read as noise and then
as nothing. Bare `used to` is not here and neither is `no longer`: both are
overwhelmingly present tense in this tree — a key *used to select* a home, a
record that *no longer grants* authority — so flagging them would bury the
handful of real ones under readings that were never about history at all.

What decides it is the subject. `used to` after a pronoun can only be past
habitual, because the present reading needs an auxiliary the pronoun form has
nowhere to put: *it used to filter* is history and *a key used to select* is
not, and no noun tells the two apart. So the pronouns are refused and the
nouns are left, which loses a reading the rule cannot have without also
losing the ones it is for.

The rest is unambiguous: a phrase that can only be about a prior state, a
design named as the one before this one, a migration something is *during*,
an audience described as needing compatibility, and a bare issue number,
which narrates the change that produced the code instead of the code. An
entity (`&#124;`) and a colour (`#264F78`) are excluded by shape rather than
by hoping they stay out of prose.
"""


def type_ignore_sites(source: str) -> list[MatchSite]:
    """Lines carrying a `# type: ignore` suppression.

    Python does model this one — `ast.parse(source, type_comments=True)`
    reports it as `Module.type_ignores` — and reading it there is worse than
    reading the comment. That flag makes the parser stricter than the one
    every other matcher here shares: `class A:  # type: int` parses normally
    and raises under it, so a single misplaced type comment anywhere would
    make this rule silently blind for the whole file. The token carries the
    same answer with nothing to trip over.
    """
    return sites_at(comment_directive_lines(source, TYPE_IGNORE_DIRECTIVE_RE))


def pyright_ignore_sites(source: str) -> list[MatchSite]:
    """Lines carrying a `# pyright: ignore` suppression."""
    return sites_at(comment_directive_lines(source, PYRIGHT_IGNORE_DIRECTIVE_RE))


def noqa_sites(source: str) -> list[MatchSite]:
    """Lines carrying a `# noqa` suppression."""
    return sites_at(comment_directive_lines(source, NOQA_DIRECTIVE_RE))


class ProseSpan(TypedDict):
    """One line's prose: where it sits, and the text a reader reads."""

    line: int
    text: str


def prose_spans(source: str) -> Iterator[ProseSpan]:
    """Every span of this file a person reads as a sentence.

    A docstring is prose whole; a comment is prose from where it opens, and
    the code before it is not. Scanning the raw line instead reads an
    identifier or a colour literal as though somebody had written it in a
    sentence. Where the source does not tokenize the whole line is offered,
    which over-reports rather than going quiet on the file being edited.
    """
    documented = docstring_lines(source)
    columns = python_comment_columns(source)
    for number, line in enumerate(source.splitlines(), start=1):
        if number in documented or columns is None:
            yield ProseSpan(line=number, text=line)
            continue
        opened = columns.get(number)
        if opened is not None:
            yield ProseSpan(line=number, text=line[opened:])


def historical_voice_sites(source: str) -> list[MatchSite]:
    """Prose recording how the code came to be rather than what it is."""
    return sites_at(
        {
            span["line"]
            for span in prose_spans(source)
            if HISTORICAL_VOICE_RE.search(span["text"])
        }
    )


# lup: ignore[library-default] — Python's own two spellings of a file rename; the kernel carries no config
RENAME_CALLS = frozenset(  # lup: ignore[frozenset-shape] — a membership test, no keys
    {"os.replace", "Path.replace", "pathlib.Path.replace", "PurePath.replace"}
)
"""The two-argument `replace` spellings that move a file rather than edit text.

A bound rename takes one argument — the destination — so arity tells it from
string surgery on its own. Only the unbound spellings need naming, because
``Path.replace(instance, target)`` passes the receiver as the first argument
and so wears exactly the arity of ``str.replace(old, new)``.
"""


def string_replace_sites(source: str) -> list[MatchSite]:
    """Sites substituting one piece of text for another inside a string.

    Arity is what separates this from the rename that wears the same name:
    ``str.replace`` takes the old text and the new, while a bound
    ``Path.replace`` takes a destination and moves a file. Reading the call's
    shape decides that for every receiver, where a spelling to look for — a
    name not ending in ``path`` — lets a genuine string replace through
    whenever the variable holding the string is called one.

    The unbound spellings in `RENAME_CALLS` are the exception arity cannot
    reach, and they are named rather than guessed at.

    What arity cannot reach at all is a two-argument ``replace`` on a value
    that is not text: a dataframe filling missing values, a vendor object
    with a ``replace`` of its own. Only the receiver's own type says so, and
    the sites carry the positions `lup.harness.codescan.resolution` asks about.
    """
    tree = python_tree(source)
    if tree is None:
        return []

    def substitutes_text(call: ast.Call, read: ast.Attribute) -> bool:
        """Whether one ``.replace(`` has the arity and spelling of text surgery."""
        return len(call.args) >= 2 and dotted_of(read) not in RENAME_CALLS

    return attribute_call_sites(tree, "replace", substitutes_text)


def string_split_sites(source: str) -> list[MatchSite]:
    """Lines splitting or partitioning a string on a separator.

    A bare `.split()` on whitespace is the tokenizing the rule allows, so an
    empty argument list is not one of these; a separator passed to it is.
    """
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(
        {
            node.func.end_lineno or node.func.value.lineno
            for node in nodes_of(tree, ast.Call)
            if isinstance(node.func, ast.Attribute)
            and (
                node.func.attr in ("partition", "rpartition")
                or (node.func.attr in ("split", "rsplit") and bool(node.args))
            )
        }
    )


def string_strip_sites(source: str) -> list[MatchSite]:
    """Lines stripping named characters off a string.

    A bare `.strip()` takes whitespace off and is what the rule leaves alone;
    an argument names characters, which is the shape that hides a parser.
    """
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(
        {
            node.func.end_lineno or node.func.value.lineno
            for node in nodes_of(tree, ast.Call)
            if isinstance(node.func, ast.Attribute)
            and node.func.attr in ("strip", "lstrip", "rstrip")
            and node.args
        }
    )


def import_re_sites(source: str) -> list[MatchSite]:
    """Lines importing the regex module."""
    tree = python_tree(source)
    return sites_at(set() if tree is None else imports_of(tree, {"re"}))


def subprocess_sites(source: str) -> list[MatchSite]:
    """Lines importing the subprocess module."""
    tree = python_tree(source)
    return sites_at(set() if tree is None else imports_of(tree, {"subprocess"}))


def argparse_sites(source: str) -> list[MatchSite]:
    """Lines importing argparse."""
    tree = python_tree(source)
    return sites_at(set() if tree is None else imports_of(tree, {"argparse"}))


def pdf_extraction_sites(source: str) -> list[MatchSite]:
    """Lines importing a PDF text-extraction library."""
    tree = python_tree(source)
    return sites_at(set() if tree is None else imports_of(tree, PDF_LIBRARIES))


def rich_progress_sites(source: str) -> list[MatchSite]:
    """Lines importing `rich.progress` or reaching it through `rich`.

    Both spellings, because the module is the subject either way: taken out
    of `rich` by name, or reached as an attribute of it.
    """
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(
        imports_of(tree, {"rich.progress"})
        | {
            node.lineno
            for node in nodes_of(tree, ast.Attribute)
            if node.attr == "progress"
            and isinstance(node.value, ast.Name)
            and node.value.id == "rich"
        }
    )


def suppress_import_sites(source: str) -> list[MatchSite]:
    """Lines taking `suppress` out of contextlib."""
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(imported_symbols_of(tree, "contextlib", {"suppress"}))


def dataclass_sites(source: str) -> list[MatchSite]:
    """Lines importing dataclasses, or decorating a class with `@dataclass`.

    Two spellings of one subject: the module a project reaches for, and the
    decorator that is the whole reason to. The decorator is selected wherever
    it is written, bare or called, and through an attribute of the module.
    """
    tree = python_tree(source)
    if tree is None:
        return []

    def decorates(node: ast.expr) -> bool:
        target = node.func if isinstance(node, ast.Call) else node
        match target:
            case ast.Name(id="dataclass") | ast.Attribute(attr="dataclass"):
                return True
        return False

    return sites_at(
        imports_of(tree, {"dataclasses"})
        | {
            decorator.lineno
            for node in nodes_of(tree, ast.ClassDef)
            for decorator in node.decorator_list
            if decorates(decorator)
        }
    )


def tuple_shape_sites(source: str) -> list[MatchSite]:
    """Return the lines carrying a fixed-arity ``tuple[...]`` annotation.

    Fixed arity is the whole of what the rule names: positions with no names
    on them. ``tuple[X, ...]`` is an immutable sequence and was never the
    subject, so it is not netted and then cleared — it simply is not selected.

    A line carrying both keeps its finding, which falls out of selecting the
    fixed ones rather than having to be said: the variadic neighbour is not
    in the set to begin with, so it cannot clear anything.

    Nesting is why this wants the tree. In ``tuple[dict[str, int], ...]`` the
    trailing ellipsis sits behind a bracket no character class can step over.
    """
    tree = python_tree(source)
    if tree is None:
        return []

    def variadic(node: ast.Subscript) -> bool:
        """Whether the last position is the ellipsis that makes it a sequence."""
        if not isinstance(node.slice, ast.Tuple) or not node.slice.elts:
            return False
        last = node.slice.elts[-1]
        return isinstance(last, ast.Constant) and last.value is Ellipsis

    return sites_at(
        {
            node.lineno
            for node in nodes_of(tree, ast.Subscript)
            if isinstance(node.value, ast.Name)
            and node.value.id == "tuple"
            and not variadic(node)
        }
    )


def default_factory_sites(source: str) -> list[MatchSite]:
    """Return ``default_factory=`` lines an annotated literal would replace.

    The rule's replacement is ``items: list[B] = []``: the annotation carries
    the type and pydantic copies the literal per instance. That says the same
    thing exactly where the factory builds an empty collection, so those are
    the lines selected — a factory that reads another declaration, stamps a
    value, or constructs a model does work no literal expresses and is not a
    site of this rule at all.
    """
    tree = python_tree(source)
    if tree is None:
        return []
    return sites_at(
        {
            keyword.lineno
            for node in nodes_of(tree, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "default_factory"
            and empty_collection_factory(keyword.value)
        }
    )


def dict_get_sites(source: str) -> list[MatchSite]:
    """Return the sites where ``.get(`` reads a named field out of a mapping.

    Three shapes the tree alone rules out, all decidable without types — which
    is what lets a suppression at any of them be retired rather than demanded
    by the kernel and reported spurious by the audit resolving the receiver.

    A key computed at runtime is not a hidden schema. The defect this rule is
    about is a *field name* the author knew and the type does not carry, and
    such a name is always a literal; ``registry.get(session)`` reads a map
    whose keys are data, where modelling the keys is not a thing anyone could
    do. Narrowing to the literal is what stopped every open registry in the
    tree from paying a directive for a rule that was never about it.

    A decorator is not payload access: ``@app.get("/path")`` names a route on
    a framework object, and no schema is read out of a dict.

    Neither is a call on a module. ``httpx.get(url)`` reaches a function the
    module declares, and a module is not a mapping whatever it contains. The
    receiver has to be the bare imported name for that: ``os.environ.get`` is
    a genuine keyed lookup reached *through* a module, so only a name bound
    directly by ``import`` is ruled out, never an attribute of one.

    Both exclusions are made once, here, and the sites that survive them are
    the sites a type oracle is asked about. A resolution pass that selected
    its own would be this rule stated twice, and would spend a checker
    session deciding what the tree has already settled.

    What no tree can settle is what an imported class *is*, and that is what
    the sites carry positions for: `lup.harness.codescan.resolution` resolves them
    and refutes a receiver outside the mapping family.
    """
    tree = python_tree(source)
    if tree is None:
        return []
    modules: set[str] = set()
    decorated: set[int] = set()
    for node in python_nodes(tree):
        match node:
            case ast.Import(names=names):
                modules.update(
                    alias.asname or alias.name.partition(".")[0] for alias in names
                )
            case (
                ast.FunctionDef(decorator_list=decorators)
                | ast.AsyncFunctionDef(decorator_list=decorators)
                | ast.ClassDef(decorator_list=decorators)
            ):
                decorated.update(
                    line
                    for decorator in decorators
                    for line in range(
                        decorator.lineno, (decorator.end_lineno or decorator.lineno) + 1
                    )
                )

    def names_a_field(call: ast.Call) -> bool:
        """Whether this call reads a *named field* rather than looks a key up.

        The discriminator between the defect and the shape that resembles it.
        A hidden field name is always spelled as a literal, because the author
        knew it while writing: ``payload.get("name")`` is a schema held in
        somebody's head and nowhere in the type. A key computed at runtime is
        the opposite case — ``registry.get(session)`` reads a map whose keys
        are data, and there is no schema being hidden because there is none.
        """
        match call.args:
            case [ast.Constant(value=str()), *_]:
                return True
            case _:
                return False

    def keyed_lookup(call: ast.Call, read: ast.Attribute) -> bool:
        """Whether one ``.get(`` is a field read rather than a near-miss."""
        return (
            names_a_field(call)
            and not (isinstance(read.value, ast.Name) and read.value.id in modules)
            and (read.end_lineno or read.value.lineno) not in decorated
        )

    return attribute_call_sites(tree, "get", keyed_lookup)


def derived_interpolation_sites(source: str) -> list[MatchSite]:
    """Return the lines where a generated artifact's body is interpolated.

    The shape is a value spliced into text that becomes a file: an f-string, a
    ``+`` with a string in it, a literal's ``.format`` or ``.join``, handed as
    the ``body`` or ``content`` of an ``Artifact`` constructed directly. Each
    is a derived value entering a container through the one route that cannot
    check it — the per-format constructors take a document instead, which is
    emitted and parsed back before the file exists.

    A piece fixed where it is written is not derived: a literal, or a name
    spelled as a constant — the command a banner already tells a reader to
    type, the exit status a guard script returns. What a declaration carries
    varies by declaration, and that is the value this is about. Nor is a
    value with only whitespace around it inside anything: a whole rendered
    page with its closing newline appended is still just the page.

    ``cls(...)`` inside the constructors is not the route this names, and is
    not matched: the per-format constructors are where a document becomes
    text, which is the one place that is supposed to happen.
    """
    tree = python_tree(source)
    if tree is None:
        return []

    def fixed(piece: ast.expr) -> bool:
        """Whether a spliced piece reads the same in every generated file."""
        match piece:
            case ast.Constant():
                return True
            case ast.Name(id=name) | ast.Attribute(attr=name):
                return name.isupper()
            case _:
                return False

    def summands(value: ast.expr) -> list[ast.expr]:
        """The operands of a chain of ``+``, left to right."""
        match value:
            case ast.BinOp(op=ast.Add(), left=left, right=right):
                return [*summands(left), *summands(right)]
            case _:
                return [value]

    def textual(piece: ast.expr) -> bool:
        """Whether a piece is written text that could hold a value inside it.

        Whitespace holds nothing: a rendered page with its closing newline
        appended is the page, not a value in a container, and a separator is
        where a container would have to be written for there to be one.
        """
        match piece:
            case ast.Constant(value=str(text)):
                return bool(text.strip())
            case ast.JoinedStr(values=parts):
                return any(textual(part) for part in parts)
            case _:
                return False

    def spliced(value: ast.expr) -> bool:
        """Whether this body splices a value that varies into written text."""
        match value:
            case ast.JoinedStr(values=parts):
                return textual(value) and any(
                    not fixed(part.value)
                    for part in parts
                    if isinstance(part, ast.FormattedValue)
                )
            case ast.BinOp(op=ast.Add()):
                pieces = summands(value)
                return any(textual(piece) for piece in pieces) and any(
                    spliced(piece) or not (fixed(piece) or textual(piece))
                    for piece in pieces
                )
            case ast.Call(
                func=ast.Attribute(value=ast.Constant(value=str()), attr="format"),
                args=args,
                keywords=keywords,
            ):
                return any(
                    not fixed(given)
                    for given in [*args, *(keyword.value for keyword in keywords)]
                )
            case ast.Call(
                func=ast.Attribute(value=ast.Constant(value=str()), attr="join"),
                args=[joined],
            ):
                return not fixed(joined)
            case _:
                return False

    def bodies(call: ast.Call) -> list[ast.expr]:
        """The text a document is handed directly, by either route into one.

        An ``Artifact`` built rather than composed through the constructor
        for its format, and a ``TextPart`` holding prose: the first is a
        whole file and the second a paragraph of one, and a value spliced
        into either lands in a container nothing checked. Prose with values
        in it is a passage, whose values are parts — so neither route has a
        reason to take a string built at the call.
        """
        match call.func:
            case ast.Name(id="Artifact") | ast.Attribute(value=ast.Name(id="Artifact")):
                return [
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg in ("body", "content")
                ]
            case ast.Name(id="TextPart") | ast.Attribute(attr="TextPart"):
                return [
                    keyword.value for keyword in call.keywords if keyword.arg == "text"
                ]
            case _:
                return []

    return sites_at(
        {
            body.lineno
            for node in python_nodes(tree)
            if isinstance(node, ast.Call)
            for body in bodies(node)
            if spliced(body)
        }
    )


def empty_collection_sites(source: str) -> list[MatchSite]:
    """Return the lines seeding an empty collection a loop then fills.

    Every empty-collection binding the tree carries, less the ones
    :func:`empty_collection_exempt_lines` shows to be deliberate defaults —
    ``__init__`` state, call keywords, annotated module and class
    declarations, and a seed whose every feeding loop tolerates a failure.

    Stated as a difference because that is what it is: the defect is a seed
    the code goes on to append to, and "goes on to append to" is a property
    of the surrounding scope rather than of the assignment. Both halves are
    the tree's answer rather than a pattern's, so a ``= []`` written inside a
    string or spelled across a line break is counted exactly as the language
    reads it.
    """
    tree = python_tree(source)
    if tree is None:
        return []
    seeded = {
        node.value.lineno
        for node in [*nodes_of(tree, ast.Assign), *nodes_of(tree, ast.AnnAssign)]
        if node.value is not None and empty_collection_literal(node.value)
    }
    return sites_at(seeded - empty_collection_exempt_lines(source))


def silent_truncation_sites(source: str) -> list[MatchSite]:
    """Return the prefix slices that keep a chosen size of somebody's content.

    A bound is this rule's subject when it is a size somebody chose: a literal
    of two digits or more, or a shouty constant naming one. A single digit is
    a parser bound — ``rest[:1]``, ``data[:2]`` — and a lowercase name is what
    a caller passed, which makes ``results[:limit]`` a request rather than a
    cut. Reading the bound is what makes this decidable at all: no type says
    whether the receiver held content.

    Less what :func:`slice_exempt_lines` settles — a digest, a split, an
    abbreviation, or a prefix a comparison only asks a question of.
    """
    tree = python_tree(source)
    if tree is None:
        return []

    def chosen_size(bound: ast.expr | None) -> bool:
        match bound:
            case ast.Constant(value=int() as size):
                return not isinstance(bound.value, bool) and size >= 10
            case ast.Name(id=name):
                return len(name) >= 3 and name == name.upper() and name[0].isalpha()
        return False

    sliced = {
        line
        for node in nodes_of(tree, ast.Subscript)
        if isinstance(node.slice, ast.Slice)
        and node.slice.lower is None
        and chosen_size(node.slice.upper)
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    return sites_at(sliced - slice_exempt_lines(source))


def matcher_named(name: str) -> Callable[[str], list[MatchSite]] | None:
    """The AST selector one row names, where the row names one.

    A rule earns a matcher when the shape it refuses is one the grammar has a
    word for, which is most of them. Which rule has which lives at the
    declaration in `lup.harness.codescan.antipatterns` and travels in the row, because
    a row projected into the hermetic runtime is primitive and cannot carry a
    callable: this side holds only the functions a row may name, so a rule
    that gains one reaches the gate by construction rather than by someone
    also remembering to widen a list of ids here.
    """
    match name:
        case "import_re_sites":
            return import_re_sites
        case "subprocess_sites":
            return subprocess_sites
        case "argparse_sites":
            return argparse_sites
        case "pdf_extraction_sites":
            return pdf_extraction_sites
        case "rich_progress_sites":
            return rich_progress_sites
        case "suppress_import_sites":
            return suppress_import_sites
        case "dataclass_sites":
            return dataclass_sites
        case "derived_interpolation_sites":
            return derived_interpolation_sites
        case "re_call_sites":
            return re_call_sites
        case "cast_sites":
            return cast_sites
        case "eval_exec_sites":
            return eval_exec_sites
        case "utcnow_sites":
            return utcnow_sites
        case "os_shell_sites":
            return os_shell_sites
        case "os_file_ops_sites":
            return os_file_ops_sites
        case "os_path_sites":
            return os_path_sites
        case "os_environ_sites":
            return os_environ_sites
        case "suppress_sites":
            return suppress_sites
        case "string_replace_sites":
            return string_replace_sites
        case "string_split_sites":
            return string_split_sites
        case "string_strip_sites":
            return string_strip_sites
        case "generic_base_sites":
            return generic_base_sites
        case "typing_union_sites":
            return typing_union_sites
        case "typing_generics_sites":
            return typing_generics_sites
        case "dict_str_object_sites":
            return dict_str_object_sites
        case "dict_str_payload_sites":
            return dict_str_payload_sites
        case "any_type_sites":
            return any_type_sites
        case "bare_object_sites":
            return bare_object_sites
        case "bare_basemodel_sites":
            return bare_basemodel_sites
        case "frozenset_shape_sites":
            return frozenset_shape_sites
        case "set_shape_sites":
            return set_shape_sites
        case "bare_except_sites":
            return bare_except_sites
        case "except_baseexception_sites":
            return except_baseexception_sites
        case "elif_chain_sites":
            return elif_chain_sites
        case "wildcard_guard_sites":
            return wildcard_guard_sites
        case "global_statement_sites":
            return global_statement_sites
        case "all_export_sites":
            return all_export_sites
        case "model_config_sites":
            return model_config_sites
        case "private_function_sites":
            return private_function_sites
        case "private_class_sites":
            return private_class_sites
        case "private_variable_sites":
            return private_variable_sites
        case "namedtuple_sites":
            return namedtuple_sites
        case "tuple_shape_sites":
            return tuple_shape_sites
        case "default_factory_sites":
            return default_factory_sites
        case "dict_get_sites":
            return dict_get_sites
        case "empty_collection_sites":
            return empty_collection_sites
        case "silent_truncation_sites":
            return silent_truncation_sites
        case "type_ignore_sites":
            return type_ignore_sites
        case "pyright_ignore_sites":
            return pyright_ignore_sites
        case "noqa_sites":
            return noqa_sites
        case "historical_voice_sites":
            return historical_voice_sites
    return None


def matched_lines(source: str, rows: list[AntiPatternRow]) -> dict[str, set[int]]:
    """Which lines each matched rule selects in this source, computed once.

    Where a tree can be had, the selector answers and the pattern is not
    consulted. Where none can — a file caught mid-edit, an indented fragment —
    what happens next is the rule's own strength, because that is what decides
    the cost of guessing.

    A soft rule falls back to its pattern. Guessing wide there costs a
    directive carrying a reason, which is a sentence somebody writes and the
    audit then grades, so the conservative net is affordable.

    A strong rule fires nowhere, which is what the empty entry says. No
    directive may silence one, so a verdict from a pattern the tree never
    confirmed would be a denial with no escape — and "cannot tell" has to
    fail toward silence. The site is seen again the moment the file parses.

    A row without the field reads as declaring no matcher, which is what an
    empty name already means here. This gate is compiled into the dispatcher
    that decides every edit, so a table from an older branch has to leave
    that dispatcher able to regenerate it rather than taking it down.
    """
    parses = python_tree(source) is not None
    found: dict[str, set[int]] = {}
    for row in rows:
        matcher = matcher_named(row["matcher"] if "matcher" in row else "")
        if matcher is None:
            continue
        if parses:
            found[row["id"]] = lines_of(matcher(source))
            continue
        if row["strength"] == "strong":
            found[row["id"]] = set()
    return found


def added_line_numbers(before: str | None, after: str | None) -> dict[int, bool]:
    """Identify added line positions with duplicate-line accounting."""
    remaining = before.splitlines() if before is not None else []
    added: dict[int, bool] = {}
    for number, line in enumerate((after or "").splitlines(), start=1):
        if line in remaining:
            remaining.remove(line)
        else:
            added[number] = True
    return added


def added_lines(before: str | None, after: str | None) -> list[str]:
    """Return added lines with duplicate-line accounting."""
    remaining = before.splitlines() if before is not None else []
    added: list[str] = []
    for line in (after or "").splitlines():
        if line in remaining:
            remaining.remove(line)
        else:
            added.append(line)
    return added


def removed_lines(before: str | None, after: str | None) -> list[str]:
    """Return lines the edit took away, with duplicate-line accounting."""
    remaining = (after or "").splitlines()
    removed: list[str] = []
    for line in (before or "").splitlines():
        if line in remaining:
            remaining.remove(line)
        else:
            removed.append(line)
    return removed


def resites_a_suppression(line: str, gone: list[str]) -> bool:
    """Whether this added directive only re-sites one the same edit removed.

    A suppression gate that reads the added line alone cannot tell
    `# lup: ignore[a, b]` becoming `# lup: ignore[a]` from a suppression
    appearing out of nowhere, and asks about both. The first is the edit the
    audit *demands* when it reports a directive spurious, so asking to approve
    it makes one gate request what the other grants — the same split a
    matcher stating its rule once exists to avoid.

    Moving one is the same shape and is refused for the same reason. Adopting
    a placement policy necessarily rewrites the markers the policy it replaces
    allowed, so a concern approved to do exactly that meets a gate demanding
    `antipattern-suppression` — an allowance that also authorizes genuinely new
    suppressions, which is a narrow action buying a wide permission.

    So the question asked is which *rules* this file suppresses, not which
    lines carry the directives: an added directive naming only ids some removed
    directive named suppresses nothing here that was not already suppressed.
    ``None`` is the bare directive, covering every rule. A new id, a widened
    list and a typed list going bare all still reach the gate.

    Two limits, named rather than implied. Re-siting a rule's directive onto a
    *different* violation of that same rule passes, because rule ids are the
    granularity a per-file text gate can honestly compute — the audit reports
    that one afterwards. And both halves must be one edit: split across two,
    the adding half sees nothing removed and asks.
    """
    match = written_suppression(line)
    if match is None:
        return False
    kept = ignore_rule_ids(match)
    for previous in gone:
        earlier = written_suppression(previous)
        if earlier is None:
            continue
        covered = ignore_rule_ids(earlier)
        if covered is None:
            return True
        if kept is not None and set(kept) <= set(covered):
            return True
    return False


def is_record_class(node: ast.ClassDef) -> bool:
    """Recognize a declarative record whose field lines are not real changes."""
    records = ("BaseModel", "TypedDict", "Enum", "IntEnum", "StrEnum", "NamedTuple")
    return any(
        (isinstance(base, ast.Name) and base.id in records)
        or (isinstance(base, ast.Attribute) and base.attr in records)
        for base in node.bases
    )


def non_real_lines(source: str) -> set[int]:
    """Return line numbers that do not count toward the small-change gate.

    Blank lines, comments, string and docstring bodies (already blanked by
    ``python_code_lines``), imports, and the field declarations of a pydantic
    or ``TypedDict`` record are boilerplate rather than logic.
    """
    code = python_code_lines(source)
    excluded = {
        number
        for number, line in enumerate(code, start=1)
        if not line or line.isspace()
    }
    tree = python_tree(source)
    if tree is None:
        return excluded
    for node in python_nodes(tree):
        if isinstance(node, ast.Import | ast.ImportFrom) and node.end_lineno:
            excluded.update(range(node.lineno, node.end_lineno + 1))
        if isinstance(node, ast.ClassDef) and is_record_class(node):
            for statement in node.body:
                if (
                    isinstance(statement, ast.AnnAssign | ast.Assign)
                    and statement.end_lineno
                ):
                    excluded.update(range(statement.lineno, statement.end_lineno + 1))
    return excluded


def real_added_line_count(
    before: str | None, after: str | None, python_source: bool
) -> int:
    """Count added lines that carry real code, ignoring boilerplate."""
    added = added_line_numbers(before, after)
    lines = (after or "").splitlines()
    if not python_source:
        return sum(
            1
            for number in added
            if lines[number - 1] and not lines[number - 1].isspace()
        )
    excluded = non_real_lines(after or "")
    return sum(1 for number in added if number not in excluded)


def ignore_rule_ids(match: re.Match[str]) -> tuple[str, ...] | None:
    """Return typed ids, or ``None`` for a bare suppression."""
    raw = match.group("ids")
    if raw is None:
        return None
    return tuple(part.strip() for part in raw.split(",") if part.strip())


class FileIgnore(TypedDict):
    """Whether a file-level suppression exists, and the ids it names.

    ``ids`` is ``None`` for the bare directive, which disables every rule, and
    distinguishing that from the empty tuple of the no-directive case is why
    ``present`` is carried rather than inferred.
    """

    present: bool
    ids: tuple[str, ...] | None


def file_level_line(source: str, max_lines: int = 10) -> int:
    """The line holding the whole-file directive, or 0 where there is none.

    A file-level opt-out lives in the header, before anything it could be
    mistaken for governing: the run of comments and blank lines a file opens
    with. That boundary is what keeps it apart from a directive standing above
    the one line it guards, which is the same characters in the same shape and
    stops being distinguishable the moment code has intervened.
    """
    for number, line in enumerate(source.splitlines()[:max_lines], start=1):
        if FILE_IGNORE_RE.match(line) is not None:
            return number
        if line.strip() and not line.lstrip().startswith(("#", "//")):
            return 0
    return 0


def file_ignore(source: str) -> FileIgnore:
    """Return whether a file-level suppression exists and the ids it names."""
    number = file_level_line(source)
    if number == 0:
        return FileIgnore(present=False, ids=())
    match = FILE_IGNORE_RE.match(source.splitlines()[number - 1])
    return FileIgnore(present=True, ids=ignore_rule_ids(match) if match else None)


def suppression_site(
    number: int, line: str, comment_columns: dict[int, int] | None = None
) -> str:
    """One suppression, located, named, and quoted before it is approved.

    The rules are named rather than left to be read back out of the quote, so
    an approval states what it approves without anyone parsing the line again.
    The line itself is quoted whole: a directive is written at the end of what
    it guards, which is the end a cut would take first.
    """
    match = source_suppression(line, number, comment_columns)
    named = ignore_rule_ids(match) if match is not None else None
    silenced = ", ".join(named) if named else "every rule"
    return f"line {number} silences {silenced}: {line.strip()}"


def suppression_reason(sites: list[str], creation: bool = False) -> str:
    """Name every suppression this edit declares, not merely that it declares one.

    A verdict that said only what kind of thing happened left the reviewer to
    find the line themselves — in a diff they were being asked to approve
    precisely because it needed reading. Every site is listed rather than the
    first, since approving is one decision over the whole batch.

    Where the sites are read is the runtime's to answer and not this
    function's. One of them puts the reason in the prompt for a command and
    drops it in the dialog for a write, so the adapter repeats what its own
    prompt will not carry; another writes the reason out whole and needs
    nothing. What is owed here is that the words name the sites, in terms any
    of them can render.

    A creation is the case where that matters most and reads least. The whole
    file arrives at once, so its directives are approved along with everything
    else about it — the layout and the shape, which is what a reviewer is
    reading a new file for — and a line in the middle of a new module is the
    easiest thing in an edit to approve without having seen.
    """
    header = (
        "this new file arrives carrying antipattern suppressions, and writing "
        "it approves them"
        if creation
        else "edit introduces an antipattern suppression"
    )
    return header + "\n" + "\n".join(sites)


class AntiPatternHit(TypedDict):
    """One added line and the rule it matched, before any suppression applies."""

    line: int
    row: AntiPatternRow


class WithdrawnGuard(TypedDict):
    """One line this edit stopped suppressing, and the rule it stopped for."""

    rule: str
    text: str


def withdrawn_guards(
    before: str | None,
    after: str,
    known_ids: list[str],
    comment_columns: dict[int, int] | None = None,
) -> Iterator[WithdrawnGuard]:
    """Every line whose suppression this edit took away, once per rule.

    Deleting a directive adds no line, so the violation it was covering is
    byte-identical across the edit and the added-line scan never reaches it —
    the one way past this gate that costs nothing to write, since the guard
    and the guarded thing are separate lines and only one of them has to move.
    What the directive reached is read from *before*, where it still stands,
    through `suppression_reaches` — the same placement policy every other
    reader here consults, so the marker shape valid on the way in is the shape
    whose removal is noticed on the way out.

    Lines are carried as text rather than as numbers because an edit moves
    them: an insertion above renumbers everything below it, and matching what
    a violation says survives that where matching where it sat does not.

    A bare directive named no rule and silenced all of them, so it comes back
    once per rule these rows carry. A directive whose text still stands in
    *after* was re-sited rather than withdrawn, which `resites_a_suppression`
    already governs and this leaves to it.
    """
    previous = (before or "").splitlines()
    remaining = after.splitlines()
    for number, line in enumerate(previous, start=1):
        if line in remaining:
            remaining.remove(line)
            continue
        directive = source_suppression(line, number, comment_columns)
        if directive is None:
            continue
        named = ignore_rule_ids(directive)
        for candidate in range(number, len(previous) + 1):
            if not suppression_reaches(previous, number, candidate):
                continue
            for rule in named if named is not None else known_ids:
                yield WithdrawnGuard(rule=rule, text=previous[candidate - 1])


def anti_pattern_hits(
    added: dict[int, bool],
    rows: list[AntiPatternRow],
    code_lines: list[str],
    scanned_lines: list[str],
    prose_lines: list[str],
    exempt: dict[str, set[int]],
    tokenized: bool,
    matched: dict[str, set[int]] | None = None,
) -> Iterator[AntiPatternHit]:
    """Every added line and the rule it matches, before suppressions apply.

    Separating matching from deciding is what lets the strong rules be
    consulted ahead of the suppression gate without the match logic existing
    twice — two copies being how a gate starts disagreeing with itself.

    A rule the caller resolved a selector for is decided by that selector and
    its pattern is not consulted: the tree already said which lines carry the
    shape, and re-reading the text could only disagree with it. ``matched``
    carries no entry for a rule whose source would not parse, which is what
    puts that rule back on its pattern.
    """
    selected = matched or {}
    for number in added:
        masked = scanned_lines[number - 1].strip()
        if not tokenized and masked.startswith("#") and "type:" not in masked:
            continue
        code = code_lines[number - 1].strip()
        for row in rows:
            if row["id"] in exempt and number in exempt[row["id"]]:
                continue
            if row["id"] in selected:
                if number in selected[row["id"]]:
                    yield AntiPatternHit(line=number, row=row)
                continue
            stripped = masked
            if tokenized:
                match row["context"]:
                    case "code":
                        stripped = code
                    case "prose":
                        stripped = prose_lines[number - 1].strip()
            if not stripped:
                continue
            if re.search(row["pattern"], stripped) is not None:
                yield AntiPatternHit(line=number, row=row)


def anti_pattern_denial(number: int, row: AntiPatternRow) -> KernelDecision:
    """Deny one matched line, saying whether a directive could have helped."""
    placement = (
        "No suppression is accepted: write the replacement."
        if row["strength"] == "strong"
        else f"Suppress on {suppression_placement(number)}."
    )
    return KernelDecision(
        "deny",
        f"line {number}: {row['message']} (rule {row['id']})",
        recovery=f"{placement} See docs/rules.md.",
    )


def withdrawn_suppression_denial(number: int, row: AntiPatternRow) -> KernelDecision:
    """Deny a line the edit stopped covering rather than one it wrote.

    Said differently from the ordinary denial because the remedy is: the line
    is not new, so reading it as one sends whoever gets this looking at code
    they did not touch. What they did touch is the directive, and either half
    of the pair they broke is a fix — put the marker back, or make the line it
    was covering stop tripping the rule.
    """
    return KernelDecision(
        "deny",
        f"line {number}: this edit removes the `# lup: ignore[{row['id']}]` "
        f"covering it, and the line still trips the rule: {row['message']} "
        f"(rule {row['id']})",
        recovery="Restore the directive or clear what it was silencing. See"
        " docs/rules.md.",
    )


def awaits_resolution(
    before: str | None, after: str, rows: list[AntiPatternRow], python_source: bool
) -> bool:
    """Whether this edit trips a rule whose verdict turns on a declaration.

    The one question a caller needs answered before paying for a checker
    session, and it is answerable without one: the regex and the tree already
    say which lines are candidates, and only those can be decided differently
    once a receiver is resolved. Every other edit — most of them — is judged
    without one and costs nothing.

    A line a directive already covers still counts. Resolving it is not waste:
    if the declaration turns out to be outside the rule's family, the audit
    calls that directive dead, and a gate that skipped the lookup would admit
    a marker `dev check` immediately reports.
    """
    if not python_source:
        return False
    original_lines = after.splitlines()
    return any(
        hit["row"]["resolution"] == "required"
        for hit in anti_pattern_hits(
            added_line_numbers(before, after),
            rows,
            python_code_lines(after),
            mask_python_string_literals(after),
            python_prose_lines(after),
            {},
            python_comment_columns(after) is not None,
            matched_lines(after, rows),
        )
        if hit["line"] <= len(original_lines)
    )


def unresolved_anti_pattern_ask(number: int, row: AntiPatternRow) -> KernelDecision:
    """Ask about a line whose rule needs a resolution this gate did not get.

    The message the rule carries tells an author to add a directive on an open
    mapping and to add nothing on a typed non-mapping receiver, because the
    audit resolves that one and reports a marker there as spurious. Repeating
    it under a denial is what closed the recovery in both directions: a
    directive the audit calls dead, or a line this gate will not admit.

    So the question says what is not known rather than what to write, and
    leaves the answer to whoever can see the receiver's type. Approving is not
    approving a defect — it admits a line the audit still judges, on evidence
    this gate did not have.
    """
    return KernelDecision(
        "ask",
        f"line {number} may break rule {row['id']}; approve if its receiver is"
        " typed and not a mapping, and `dev check` will confirm it",
        recovery=f"{row['message']} The gate could not resolve what the receiver"
        " is declared on, so it cannot tell the defect from the shape the rule"
        " permits.",
    )


def spurious_refusal(number: int, dead: list[str], live: list[str]) -> KernelDecision:
    """Refuse a directive for the rules it names that nothing it guards trips.

    Asking spends a human turn on ids that were never going to silence
    anything, and approving them is worse than refusing: they read as reviewed
    exceptions while the audit is already calling them dead.

    The remedy is stated as dropping the ids rather than the directive,
    because the rest of one may be doing its job and dropping the whole would
    resurface the denial it was silencing. The reach is stated with it, since
    a directive written one line off from its violation is dead for a reason
    that wants it moved instead. What the line trips is named too, because
    that is the directive the site actually wanted.
    """
    named = ", ".join(dead)
    instead = f" — the line trips {', '.join(live)} instead" if live else ""
    return KernelDecision(
        "deny",
        f"line {number}: this suppression names {named}, which nothing it guards "
        f"trips{instead}",
        recovery=f"Drop {named} from it: a rule that does not fire is silenced by"
        f" nothing, and the audit reports the directive spurious. One written on"
        f" line {number} reaches that line, and where it stands alone the line"
        " beneath its comment block. See docs/rules.md.",
    )


def antipattern_decision(
    before: str | None,
    after: str,
    rows: list[AntiPatternRow],
    python_source: bool,
    allowances: list[str] | None = None,
    resolution: ResolutionRow | None = None,
    resolved: list[ResolvedImportRule] | None = None,
    typescript_source: bool = False,
) -> KernelDecision | None:
    """Reject newly added unsuppressed anti-patterns and ask on suppressions.

    Each row carries the syntactic context it inspects: a "code" rule is
    matched against masked source (string literals and comments both
    blanked) so prose never trips it, while a "comment" rule targets comment
    directives and sees comments intact. Python is masked through its
    tokenizer and the TypeScript family through
    :func:`~lup.policy.kernel.typescript.typescript_spans`; without either (a
    file of neither family, a Python fragment that fails to tokenize) every
    rule scans the raw line.

    A declared suppression is judged before it is asked about. One naming a
    rule that nothing it guards trips is refused outright: it silences
    nothing, so the ask would spend a human turn admitting a marker the audit
    reports spurious the moment it lands — and a marker that suppresses
    nothing is the cheapest way past a gate that asks about every directive
    alike. Only the rules these rows carry are judged; another scanner owns
    `abc-capability` and its family, and a verdict this gate cannot reach is
    not one it may refuse over.

    A violation nothing covers is denied whatever else the edit declares, and
    only then are the declared suppressions asked about. Both halves of that
    order matter: without the first, a directive covering one line bought
    approval for every unsuppressed line beside it; without the second, an
    edit whose violations are all covered would be refused for suppressing
    them, which is the ordinary and approved way to write one.

    A granted ``antipattern-suppression`` allowance turns the two suppression
    asks into allows, because a human already approved the plan that needs
    them. It reaches neither denial: an allowance justifies a typed, argued
    suppression, never a bare anti-pattern and never a dead directive.

    ``resolution`` is what a checker settled about the receivers this text
    names, where one ran. A line it refuted resolved outside its rule's
    family: it trips nothing, and a directive naming the rule there is
    refused as dead. A line it left unresolved is one nothing could be shown
    about: the rule demands no directive there and this gate denies none,
    and a directive written there stands, because a verdict nobody can
    substantiate is no verdict against the marker either. The audit reads
    the same two answers off the same resolution, so neither gate can call
    dead what the other demands. None means no checker answered, and a
    resolution-required rule is then asked about rather than decided.
    """
    suppression = "allow" if "antipattern-suppression" in (allowances or []) else "ask"
    added = added_line_numbers(before, after)
    original_lines = after.splitlines()
    masked = masked_source(after, python_source, typescript_source)
    scanned_lines = masked["commented"]
    prose_lines = masked["prose"]
    code_lines = masked["code"]
    exempt: dict[str, set[int]] = {}
    matched = matched_lines(after, rows) if python_source else {}
    rows = [*rows, *(selection["row"] for selection in resolved or [])]
    matched.update(
        {
            selection["row"]["id"]: set(selection["spans"])
            for selection in resolved or []
        }
    )
    refuted = resolution["refuted"] if resolution is not None else {}
    for rule_id, lines in refuted.items():
        exempt[rule_id] = (
            exempt[rule_id] | set(lines) if rule_id in exempt else set(lines)
        )
    # Hits nothing could be shown about. They stay hits, so a directive over
    # one is read as guarding something, and they are neither denied nor asked.
    unresolved = resolution["unresolved"] if resolution is not None else {}
    open_lines = {
        (rule_id, line) for rule_id, lines in unresolved.items() for line in lines
    }
    comment_columns = masked["comment_columns"]
    previous_columns = masked_source(before or "", python_source, typescript_source)[
        "comment_columns"
    ]

    def directive_at(number: int) -> re.Match[str] | None:
        return source_suppression(original_lines[number - 1], number, comment_columns)

    file_level = file_ignore(after)
    has_file_ignore = file_level["present"]
    disabled_ids = file_level["ids"]
    changed = set(added)
    for selection in resolved or []:
        rule_id = selection["row"]["id"]
        for line, end_line in selection["spans"].items():
            holder = covering_suppression_line(original_lines, line, comment_columns)
            directive = directive_at(holder) if holder else None
            covered_ids = ignore_rule_ids(directive) if directive is not None else []
            covered = (
                has_file_ignore and (disabled_ids is None or rule_id in disabled_ids)
            ) or (
                directive is not None
                and (covered_ids is None or rule_id in covered_ids)
            )
            if not covered or any(
                number in changed for number in range(line, end_line + 1)
            ):
                added[line] = True
    removed = removed_lines(before, after)
    gone = [
        line[directive.start() :]
        for number, line in enumerate((before or "").splitlines(), start=1)
        if line in removed
        and (directive := source_suppression(line, number, previous_columns))
        is not None
    ]
    declared: list[int] = []
    for number in added:
        original = original_lines[number - 1]
        directive = directive_at(number)
        if (
            directive is not None
            and not resites_a_suppression(original[directive.start() :], gone)
            and (
                comment_columns is None
                or (
                    number in comment_columns
                    and comment_columns[number] == directive.start()
                )
            )
        ):
            declared.append(number)
    # Which of them this gate may judge for suppressing nothing. The
    # whole-file form governs lines an edit's own text need not contain, so it
    # is asked about like any other and judged by none of what follows.
    # Judging the rest rests on reading the tree: where the document has none
    # a matched rule is answering from its pattern or not at all, and calling
    # a directive dead on either would refuse it for this gate's own
    # blindness.
    decidable = not declared or not python_source or python_parses(after)
    whole_file = file_level_line(after)
    judged = [
        number for number in (declared if decidable else []) if number != whole_file
    ]
    tokenized = comment_columns is not None
    hits = list(
        anti_pattern_hits(
            added,
            rows,
            code_lines,
            scanned_lines,
            prose_lines,
            exempt,
            tokenized,
            matched,
        )
    )

    def guarded_hits(number: int) -> list[AntiPatternHit]:
        """Every rule tripped by the lines one directive is written to guard.

        Read from the directive's side, through the one placement policy, and
        asked of it rather than guessed: `suppression_reaches` decides, and
        this only offers it the lines below. Offering a fixed pair was exactly
        complete while the policy stopped at the next line, and left a
        directive whose reason spans two lines guarding nothing it could see —
        so the forward check admitted an edit this one then called spurious.

        The rows and the refined exemptions are the ones the gate matched with
        above, so what counts as a trip here is what counts as a trip
        everywhere.
        """
        guarded = {
            candidate: True
            for candidate in range(number, len(original_lines) + 1)
            if suppression_reaches(original_lines, number, candidate)
        }
        return list(
            anti_pattern_hits(
                guarded,
                rows,
                code_lines,
                scanned_lines,
                prose_lines,
                exempt,
                tokenized,
                matched,
            )
        )

    # A strong rule outranks every suppression below it, including the declared
    # gate: its replacement is right every time, so a directive beside it
    # expresses nothing a human should be asked to approve — and approving one
    # would admit an edit `dev check` then refuses.
    for hit in hits:
        if hit["row"]["strength"] == "strong":
            return anti_pattern_denial(hit["line"], hit["row"])

    known_ids = {row["id"] for row in rows}
    for number in judged:
        directive = directive_at(number)
        named = ignore_rule_ids(directive) if directive is not None else None
        # Two directives are skipped, both because what they silence is not
        # visible from here. A bare one names no rule and covers every rule
        # there is, including those another scanner owns and these rows cannot
        # see, so nothing here can prove it guards nothing. One standing at the
        # end of the text is the same limit in a different form: the line it
        # guards is not in the text this gate was handed.
        alone = standalone_suppression(original_lines[number - 1]) is not None
        if named is None or (alone and number == len(original_lines)):
            continue
        fired = {hit["row"]["id"] for hit in guarded_hits(number)}
        dead = [rule for rule in named if rule in known_ids and rule not in fired]
        if dead:
            return spurious_refusal(
                number, dead, [rule for rule in sorted(fired) if rule not in named]
            )

    # The same precedence the strong-rule loop above takes, applied to the rest
    # of the table: a violation nothing covers outranks every suppression the
    # edit declares. Deciding the declared ask first left this loop unreachable
    # for any edit that added a directive at all, so one legitimate suppression
    # carried the unsuppressed violations beside it through on its approval —
    # an approval whose reason named only the directive.
    covering: dict[int, bool] = {}
    for hit in hits:
        number = hit["line"]
        rule_id = hit["row"]["id"]
        if has_file_ignore and (disabled_ids is None or rule_id in disabled_ids):
            continue
        holder = covering_suppression_line(original_lines, number, comment_columns)
        original = original_lines[holder - 1] if holder else ""
        directive = directive_at(holder) if holder else None
        if directive is not None:
            covered = ignore_rule_ids(directive)
            if covered is None or rule_id in covered:
                # Both suppression asks need the same exemption. A re-sited
                # marker lands on the very line it was moved to cover, so
                # that line is added, trips its rule, and reaches here even
                # when the declaration gate above let it through.
                if resites_a_suppression(original[directive.start() :], gone):
                    continue
                covering[holder] = True
                continue
        # A receiver the checker looked at and could not type is not a
        # violation this gate can state: the audit demands no directive there,
        # so denying one here would be the split this gate exists to prevent.
        if (rule_id, number) in open_lines:
            continue
        # A verdict this gate cannot support is asked about rather than
        # stated. The regex is wider than the defect for a resolution-required
        # rule, and what settles the difference is a declaration nothing here
        # resolved — so a denial would be the audit's opposite, and the two
        # would block on states no version of the file satisfies at once.
        if resolution is None and hit["row"]["resolution"] == "required":
            return unresolved_anti_pattern_ask(number, hit["row"])
        return anti_pattern_denial(number, hit["row"])

    # The mirror of the loop above, for the violation an edit exposes without
    # touching it. Withdrawing a directive leaves the line it covered exactly
    # as it was, so `added` is empty of it and every scan keyed on added lines
    # is blind to it by construction — the file goes out tripping a rule that
    # `dev check` then reports missing, which is the split this gate exists to
    # prevent.
    #
    # Scoped twice over, because a rescan is the one reader here that can see
    # lines the edit never proposed. Only the rules some withdrawn directive
    # named are consulted, and within those only the lines that directive was
    # actually covering — so debt the file was already carrying, uncovered
    # before this edit and uncovered after it, is left to the audit that owns
    # it rather than charged to whoever happened to edit the file next.
    withdrawn = list(
        withdrawn_guards(before, after, [row["id"] for row in rows], previous_columns)
    )
    if withdrawn:
        everywhere = {number: True for number in range(1, len(original_lines) + 1)}
        for hit in anti_pattern_hits(
            everywhere,
            rows,
            code_lines,
            scanned_lines,
            prose_lines,
            exempt,
            tokenized,
            matched,
        ):
            number = hit["line"]
            rule_id = hit["row"]["id"]
            text = original_lines[number - 1]
            if not any(
                guard["rule"] == rule_id and guard["text"] == text
                for guard in withdrawn
            ):
                continue
            if has_file_ignore and (disabled_ids is None or rule_id in disabled_ids):
                continue
            holder = covering_suppression_line(original_lines, number, comment_columns)
            directive = directive_at(holder) if holder else None
            if directive is not None:
                covered = ignore_rule_ids(directive)
                if covered is None or rule_id in covered:
                    continue
            if (rule_id, number) in open_lines:
                continue
            return withdrawn_suppression_denial(number, hit["row"])

    def sites_at(numbers: list[int]) -> list[str]:
        """The directives written on these lines, rendered for the prompt."""
        return [
            suppression_site(number, original_lines[number - 1], comment_columns)
            for number in numbers
        ]

    def silences_nothing(number: int) -> bool:
        """Whether the bare directive on this line guards no rule these rows see.

        Only the untyped form is answerable here. A typed directive naming an
        id no row owns belongs to the scanner that does own it, and the
        refusal loop above has already taken out every typed one this table
        can call dead — so what is left is the bare form, which the audit
        reports spurious on exactly this test and `--fix` then deletes.

        Asked only where the tree parsed, because a rule that reads the tree
        contributes no hit when it did not: without that guard a directive
        standing over a live violation would look dead for the gate's own
        blindness, and be dropped from the prompt that exists to name it.
        """
        directive = directive_at(number)
        if directive is None or ignore_rule_ids(directive) is not None:
            return False
        return not guarded_hits(number)

    # Every violation the edit added is covered, so what is left to decide is
    # the suppressions themselves: the ones this edit declares, or the standing
    # one an added line has moved under. A directive that silences nothing is
    # not among them: naming it would spend the reader's attention on a line
    # the audit deletes unread, and the listing exists so that what is read
    # there is what the approval is actually about.
    listed = [
        number for number in declared if not (decidable and silences_nothing(number))
    ]
    if listed:
        return KernelDecision(
            suppression, suppression_reason(sites_at(listed), before is None)
        )
    if covering:
        return KernelDecision(suppression, suppression_reason(sites_at(list(covering))))
    return None


def yields_to_scratch(
    path: str, row: PathRuleRow, path_roles: list[PathRoleRow]
) -> bool:
    """Whether a rule naming a file wherever it sits gives way to scratch.

    A root spelled from anywhere (`**/pyproject.toml`) names a file by what it
    is, which is right in any package that holds one and wrong in a scratch
    root, which holds only disposable copies: a project scaffolded under
    `tmp/` carries its own manifest, and nothing installs from it. A rule
    naming the scratch root itself (`tmp`) is about scratch, so it holds.
    Every gate reads a protected path through this, so a shell write and an
    edit of the same file get one answer.
    """
    return (
        row["kind"] == "contains_part"
        and path_role(path, path_roles) == "scratch"
        and path_role(row["value"], path_roles) != "scratch"
    )


def path_rule_matches(path: str, path_exists: bool, row: PathRuleRow) -> bool:
    """Evaluate one primitive protected-path rule.

    The two directory shapes are :func:`root_matches`, which the role table
    reads through as well, so a rule protecting a tree and a role classifying
    the same tree cannot come to disagree about which paths are in it.
    """
    kind = row["kind"]
    value = row["value"]
    portable = normalized_path(path)
    expected = normalized_path(value)
    parts = tuple(part for part in portable.split("/") if part)
    match kind:
        case "exact":
            return portable == expected
        case "subtree":
            return root_matches(path, value, "subtree")
        case "name_prefix":
            return posixpath.basename(portable).startswith(value)
        case "new_subtree":
            return root_matches(path, value, "subtree") and not path_exists
        case "contains_part":
            return root_matches(path, value, "contains_part")
        case "new_devtools":
            return (
                any(
                    parts[index] == "src"
                    and index + 2 < len(parts)
                    and parts[index + 2] == "devtools"
                    for index in range(len(parts))
                )
                and not path_exists
            )
        case _:
            raise ValueError(f"invalid path rule kind {kind!r}")


def protected_path_reason(path: str, matched: PathRuleRow) -> str:
    """One protected-path question, naming the path and the rule it tripped.

    The row's reason states the category and the path is what tripped it; the
    pattern is named only where it differs from the path, since a reviewer
    told that ``README.md`` matched the rule ``README.md`` learned nothing
    from the second mention. Beside :func:`path_rule_matches` so the words a
    question uses and the match that raised it come from one module, for the
    edit gate and the shell path alike.

    A rule spelled from anywhere names the file itself as often as a directory
    holding it: `packages/app/uv.lock` *is* one of `**/uv.lock` rather than
    under it, and is named as a match of the pattern it tripped.
    """
    reason = matched["reason"]
    value = matched["value"]
    if path == value:
        return reason if path in reason else f"{path}: {reason}"
    if matched["kind"] == "contains_part" and not root_matches(
        posixpath.dirname(normalized_path(path)), value, "contains_part"
    ):
        return f"{path} matches **/{value}: {reason}"
    return f"{path} is under {value}: {reason}"


PACKAGE_MARKER_FILES = ("__init__.py",)
"""Files whose name is their whole content, when they carry nothing else.

The full-write gate exists because creating a file asks a reviewer to read all
of it. A package marker is the case where there is nothing to read: the name
declares a package, and the conventions here say an internal one holds its
docstring and nothing more. A project that marks its packages differently
passes its own names.
"""


def documentation_only(source: str) -> bool:
    """Whether a Python source states nothing beyond what it is.

    An empty file and a lone docstring both qualify. Anything else — an
    import, an assignment, a re-export — is content somebody has to read, so
    the file stops being a marker and is judged as the new module it is.
    Source that does not parse is not a marker either: what it says is exactly
    what could not be established.
    """
    tree = python_tree(source)
    if tree is None:
        return False
    match tree.body:
        case [] | [ast.Expr(value=ast.Constant(value=str()))]:
            return True
    return False


def acceptance_guard_decision(
    guard: AcceptanceGuardRow, autonomous: bool
) -> KernelDecision:
    """Judge one edit to a test-role path against the declared guard.

    This is the one gate where an autonomous identity is held to *more* than
    an ordinary session rather than less, and the inversion is the point
    rather than an oversight. Everywhere else, autonomy means the caller
    reviews its own edits, so a question it would only answer itself is
    dropped. Here the caller's whole contract is to satisfy these tests, so
    it is the one caller for whom editing them is never the right move —
    a human weighing whether a test encodes the wrong behaviour is exactly
    who the ordinary ask reaches, and exactly who an implementer is not.
    """
    if autonomous:
        return KernelDecision("deny", guard["autonomous_reason"])
    return KernelDecision("ask", guard["ask_reason"])


def edit_rule_matches(
    row: EditRuleRow, gate: str, suffix: str, role: str, operation: str
) -> bool:
    """Whether one declared rule speaks about this change at this gate.

    An empty axis means the rule is silent about it and matches every value,
    so a rule constrains exactly what it names and a table of one rule saying
    nothing but ``effect`` moves every gate at once — which is the shortest
    way to state a project that reviews nothing at the hook.
    """
    return (
        (not row["gates"] or gate in row["gates"])
        and (not row["suffixes"] or suffix in row["suffixes"])
        and (not row["roles"] or role in row["roles"])
        and (not row["operations"] or operation in row["operations"])
    )


def edit_verdict(
    rows: list[EditRuleRow],
    gate: str,
    suffix: str,
    role: str,
    operation: str,
    default: KernelDecision,
) -> KernelDecision:
    """What one gate decides here: the last rule that moves it, or the kernel's own.

    Last match rather than first, and rather than most specific, because these
    rules overlap on purpose: a project states the broad case and then carves
    exceptions out of it, exactly as `.gitignore` is written and read. Most
    specific would make a table's meaning depend on a specificity ordering
    nobody wrote down, and would have no answer at all for two rules of equal
    reach.

    A rule that states no effect is not a match here however well its axes fit
    — it moves the threshold and nothing else, so it must not shadow a rule
    behind it that does decide.
    """
    stated = [
        row
        for row in rows
        if row["effect"] and edit_rule_matches(row, gate, suffix, role, operation)
    ]
    if not stated:
        return default
    decided = stated[-1]
    effect = decided["effect"]
    if effect not in ("allow", "ask", "deny", "defer"):
        return default
    return default.revised(effect=effect, reason=decided["reason"] or default.reason)


def edit_threshold(
    rows: list[EditRuleRow],
    suffix: str,
    role: str,
    operation: str,
    default: int,
) -> int:
    """How many added lines count as small here, by the same last-match rule.

    Separate from :func:`edit_verdict` because the two move independently: a
    project widening the size gate for one suffix is not restating who decides
    when it trips, and one that redirects the verdict has said nothing about
    how much is too much.
    """
    stated = [
        row["maximum_added_lines"]
        for row in rows
        if row["maximum_added_lines"] is not None
        and edit_rule_matches(row, "size", suffix, role, operation)
    ]
    return stated[-1] if stated else default


def decide_edit(
    path: str,
    before: str | None,
    after: str | None,
    *,
    path_exists: bool,
    path_rules: list[PathRuleRow],
    antipattern_rows: list[AntiPatternRow],
    path_roles: list[PathRoleRow] | None = None,
    maximum_added_lines: int = 3,
    autonomous: bool = False,
    allowances: list[str] | None = None,
    python_source: bool = False,
    acceptance_guard: AcceptanceGuardRow | None = None,
    marker_files: tuple[str, ...] = PACKAGE_MARKER_FILES,
    resolution: ResolutionRow | None = None,
    suffix: str = "",
    operation: str = "modify",
    edit_rules: list[EditRuleRow] | None = None,
    foreign: bool = False,
    outside_project: bool = False,
    checkout_path: str = "",
    displaced: DisplacedTargetRow | None = None,
    import_boundaries: list[ImportBoundaryRow] | None = None,
) -> KernelDecision:
    """Apply anti-pattern, path, marker, full-write, deletion, and size gates.

    ``allowances`` names what a human already approved for the concern this
    edit belongs to. A grant releases exactly the gate it names and nothing
    adjacent, so an ungranted session sees the unchanged lattice.

    The two marker families are gated in opposite directions. Feedback is
    judged by :func:`marker_decision`: adding it asks, deleting it is denied,
    and the way through is to convert a note into a claim the review pass can
    check. A suppression is gated where it is declared — an added line
    carrying an `ignore` directive, which :func:`antipattern_decision` asks
    about — while retiring one needs no gate at all: the same decision
    re-reads the uncovered line, allows it where nothing trips, and denies
    where the violation is still live. What makes that verdict trustworthy is
    a matcher subtracting its own excess, without which a rule broader than
    the defect it names holds its own suppression in place forever.

    ``acceptance_guard`` is the one gate that answers before the relaxations
    below rather than through them, because it asks whether the file may be
    edited at all. Undeclared, a project judges its tests by the same
    lattice as anything else, which is what every project did before the
    guard existed.

    Each gate reaches as far as its own reason. Anti-patterns, the size gate
    and the full-write gate are all about how production code reads and how
    much of it a reviewer can hold at once, so all three stop at production;
    the marker gate follows the feedback instead and stops where nobody of
    ours reads it — at scratch, where nothing persists, and at
    ``outside_project``, a tree this repository's review passes never walk.
    That fact says whose code a file is and nothing about what may be done to
    it, so the gates below answer without consulting it. A full write only
    ever asks about creating a file — an overwrite carries its predecessor as
    ``before`` — and creating one where the conventions do not reach costs a
    reviewer nothing, which pure deletion already assumed everywhere.
    ``marker_files`` is the other end of that same reasoning: a file whose
    content is nothing but its own docstring costs a reviewer nothing either,
    wherever it sits.

    ``checkout_path`` is the same file as the session's own checkout spells
    it, and empty where that checkout does not hold the file. It differs from
    ``path`` only where a repository nested inside the checkout holds the
    file, and it answers one question, which two gates below defer to:
    whether the file lies under a root this checkout declares scratch.

    Both spellings name a file that exists or is about to, never a word a
    shell has yet to expand: a native edit's path is handed over literal, and
    a command's write reaches this gate only once its target was read as one
    (:func:`~lup.policy.kernel.lex.carried_writes` drops any that was not).
    So a `$` in either is the character it is -- `tmp/a$b` is scratch -- and
    every reader below is told so, as the shell kernel tells its own.
    """
    path = VerbatimText(path)
    checkout_path = VerbatimText(checkout_path)
    # Scratch this checkout declares, read off the checkout's own spelling
    # alone. That is empty wherever the checkout does not hold the file -- a
    # sibling worktree, a `refs/` link landing in another project, the
    # machine's temporary root -- so none of those can claim it, and another
    # repository's own `tmp/` is never read as this checkout's.
    scratch_here = declared_scratch(checkout_path, path_roles or [])
    # It outranks the referral below. A repository nested under that scratch
    # -- a probe kit given its own `git init` under `tmp/` -- is as disposable
    # as the tree around it: its `.git` makes it a project root for a runtime
    # launched inside, not somebody else's code to defer to, so the file is
    # judged as this checkout spells it. Only a foreign file is re-read: a
    # worktree of this repository placed under `tmp/` is still this
    # repository's code.
    if foreign and scratch_here:
        path, foreign = checkout_path, False
    granted = allowances or []
    previous = before or ""
    updated = after or ""
    role = path_role(path, path_roles or [])
    rows = edit_rules or []

    def judged(gate: str, default: KernelDecision) -> KernelDecision:
        """This gate's verdict, as the project's declared table resolved it.

        Every gate below states the verdict the kernel reaches on its own and
        hands it here, so an empty table decides exactly what this function
        decided before a table existed — and a project moving one gate has to
        name it, rather than inheriting a shift it never asked for.

        The gate's name is stamped on as the rule id here rather than repeated
        at each branch, because it is already the one thing every branch
        supplies. Without it an edit verdict said what it decided and never
        which gate decided it, and a native tool name — `Edit`, `Write` — is
        the same answer for all of them.
        """
        settled = edit_verdict(rows, gate, suffix, role, operation, default)
        return settled.revised(rule=f"edit:{gate}", evaluator="edit-gate")

    # A compiled plugin tree is a build product, and this is the same refusal
    # the shell path already gives for writing one: the change is reverted by
    # the next generation and never reaches the runtime that already loaded
    # it. Judged here rather than by the lattice below, because every verdict
    # the lattice can reach is wrong for this file — an allow writes something
    # that will be overwritten, and an ask puts a question to a human whose
    # only correct answer is "edit the source instead".
    #
    # This checkout's scratch is the one place it does not reach. Nothing this
    # project generates lands there, so a plugin tree under it is somebody's
    # own -- a probe kit's hand-written plugin -- and editing it edits no
    # build product. The checkout's spelling is of where the file lands, the
    # host having resolved it, so a link planted in scratch reaches no
    # generated tree through this.
    if is_generated_plugin_target(path) and not scratch_here:
        return KernelDecision(
            "deny",
            GENERATED_PLUGIN_REFUSAL,
            cause="deliberate",
            hard=True,
            rule="edit:generated-plugin",
            evaluator="edit-gate",
            recovery=GENERATED_PLUGIN_RECOVERY,
        )
    # Git's own pointers and refs, on the same footing: every verdict the
    # lattice can reach is wrong for them. An allow lets a session choose what
    # host git reads next, and an ask puts a question whose right answer is
    # "let git write it". No scratch exception, because what a pointer points
    # host git at does not depend on where the pointer sits.
    if git_state(path) is not None:
        return KernelDecision(
            "deny",
            GIT_STATE_REFUSAL,
            cause="deliberate",
            hard=True,
            rule="edit:git-state",
            evaluator="edit-gate",
            recovery=GIT_STATE_RECOVERY,
        )

    # Whether this path is the file it names is prior to every gate below,
    # which all read the role off the spelling. A symlink is what separates
    # the two, and the host is what resolved it: the relaxations a role grants
    # would otherwise be read off one file and spent on another.
    if displaced is not None:
        return judged(
            "displaced-path",
            KernelDecision(
                "ask",
                f"{path} is a symlink to {displaced['lands']}, so this edit was"
                " checked against the wrong file's rules",
                purpose="quality_review",
            ),
        )

    # Below the plugin refusal, which is about a universal fact -- a build
    # product is overwritten by the next generation, in anybody's repository
    # -- and above everything else, which is not. The gates that follow
    # describe how *this* project's code should read, and applying them to
    # another repository's files judges that repository by conventions it
    # never adopted: measured, one session produced dozens of denials naming
    # lup rules against a checkout that had its own hooks, its own size
    # budget, and no lup rule checker to read a suppression directive. The
    # edit was refused until the other repository's code had been restyled
    # into this one's conventions, inside a diff whose subject was something
    # else entirely. So the honest answer is that this policy has nothing to
    # say about the file, and the human who launched a session here decides.
    if foreign:
        return judged(
            "foreign-repository",
            KernelDecision(
                "ask",
                FOREIGN_REPOSITORY_REFERRAL,
                purpose="policy_override",
                recovery=FOREIGN_REPOSITORY_RECOVERY,
            ),
        )

    # Whether this file may be edited at all is prior to how the edit reads,
    # so the guard answers ahead of every gate below — including pure
    # deletion, which would otherwise allow removing the test outright, and
    # the protected-path rules, whose autonomous release must not survive a
    # refusal aimed at exactly that caller.
    if role == "test" and acceptance_guard is not None:
        # The person who set the target is the one who may move it, so this
        # question is not routed past them to a supervisor.
        return judged(
            "acceptance-guard",
            acceptance_guard_decision(acceptance_guard, autonomous).revised(
                purpose="quality_review"
            ),
        )
    # The conventions describe how production code should read. A test's
    # subject is production's behaviour, and scratch is disposable, so
    # neither is judged against them.
    if after is not None and role == "production":
        antipattern = antipattern_decision(
            before,
            after,
            antipattern_rows,
            python_source,
            granted,
            resolution,
            resolved_import_rules(path, after, import_boundaries or [])
            if python_source and not outside_project
            else None,
            typescript_source=suffix in TYPESCRIPT_SUFFIXES,
        )
        # A granted suppression answers this gate and no other, so an allow
        # falls through to the rest of the lattice rather than ending it.
        if antipattern is not None and antipattern.effect != "allow":
            # A suppression is argued in code, and reading the argument is
            # reading code — which is what a supervisor does.
            return judged(
                "anti-pattern",
                antipattern.revised(
                    purpose="quality_review", reviewer="supervisor_allowed"
                ),
            )
    protected = next(
        (
            row
            for row in path_rules
            if path_rule_matches(path, path_exists, row)
            and not yields_to_scratch(path, row, path_roles or [])
            and not (row["kind"] == "new_devtools" and "new-devtools-module" in granted)
        ),
        None,
    )
    if protected is not None and not (autonomous and protected["allow_autonomous"]):
        # A protected path says a person owns this file, which is the whole
        # of the rule: routing it to a supervisor answers past exactly the
        # person the declaration names.
        return judged(
            "protected-path",
            KernelDecision(
                "ask",
                protected_path_reason(path, protected),
                purpose="quality_review",
                recovery=protected["recovery"],
            ),
        )
    # Feedback is feedback wherever it is left, so this gate follows the file
    # rather than the conventions: a note on a test still names work somebody
    # owes. Two exceptions, and both are about who reads the note rather than
    # about how the code reads. Scratch, because nothing there persists to be
    # read. And a file outside this project, because a `# lup:` marker there
    # sits in a tree `dev check` and `dev comments` never walk: no pass of
    # ours will surface it, so there is no reader to protect and nothing the
    # claim could be checked against. Held to paths settled as outside — an
    # absolute or `..` spelling of a file in this repository relativizes back
    # inside and is judged here like any other.
    if role != "scratch" and not outside_project:
        marker = marker_decision(previous, updated, python_source)
        if marker is not None:
            return judged(
                marker["gate"], marker["decision"].revised(purpose="quality_review")
            )
    # A whole-file write is one the caller named as such, or one that arrived
    # with no preimage at all. Both spellings are kept because they answer
    # different callers: an adapter that knows the native call says so, and
    # one that only has the documents falls back to the absence, which alone
    # would be the whole test. Keying on the operation is what survives an
    # adapter learning to carry a file's current text as the preimage —
    # without it, teaching `Write` to do that would silently move every
    # overwrite from this gate to the size gate below.
    whole_file = operation in ("create", "overwrite") or before is None
    if whole_file and role == "production":
        if autonomous:
            return judged(
                "autonomous-full-write",
                KernelDecision("allow", "reviewed autonomous full write"),
            )
        if posixpath.basename(path) in marker_files and documentation_only(updated):
            return judged(
                "package-marker",
                KernelDecision("allow", "a package marker states nothing to review"),
            )
        # A quality checkpoint: what is being reviewed is how the code
        # reads, which is what a supervisor reads. The classification is
        # semantic and independent of which native call carried the write —
        # a whole file arriving at once is what makes it a checkpoint.
        count = len(updated.splitlines())
        arriving = "1 line" if count == 1 else f"{count} lines"
        return judged(
            "full-write",
            KernelDecision(
                "ask",
                f"{path} is written whole, {arriving} at once",
                purpose="quality_review",
                reviewer="supervisor_allowed",
            ),
        )
    if after is None or after == "":
        return judged("pure-deletion", KernelDecision("allow", "pure deletion"))
    if role == "production" and real_added_line_count(
        before, after, python_source
    ) > edit_threshold(rows, suffix, role, operation, maximum_added_lines):
        if autonomous:
            return judged(
                "autonomous-edit", KernelDecision("allow", "reviewed autonomous edit")
            )
        # A deliberate handoff and not a gap: a large ordinary edit is
        # exactly what a native auto-accept mode exists for, and this
        # policy interposing would replace a decision an operator already
        # made. It reached the same word as a parser gap before, which is
        # what let a gap inherit provider auto-mode.
        return judged(
            "size",
            handed_over("edit exceeds the small-change gate"),
        )
    return judged("small-edit", KernelDecision("allow", "small safe edit"))
