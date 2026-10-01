# lup: ignore[empty-collection]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""Shell syntax: one command line read into a tree, by recursive descent.

A reader taking a command's words from a lexer that drops quoting on
the way through, and recovering structure afterwards by matching
keywords at the front of flat segments, loses two facts for good:
whether a `$S` stands inside single quotes, where the shell never expands
it, and which segments a loop, a branch or a case arm actually encloses.
This module keeps both. It knows grammar only -- nothing here judges a
command, binds a variable or reads a filesystem -- and a line it cannot
read comes back as the unjudged decision naming why, never as an
exception.

The grammar is POSIX sh with the bash forms agents actually write: `[[ ]]`,
`$'…'`, `<(…)`, `|&`, `&>`, `;&` and `;;&`, and `function name`.
"""

from collections.abc import Callable
from typing import Literal, TypedDict

from .decision import (
    BACKTICK_REASON,
    BACKTICK_RECOVERY,
    KernelDecision,
    SUBSTITUTION_REASON,
    SUBSTITUTION_RECOVERY,
    SUBSTITUTION_SENTINEL,
    unjudged,
)
from .diagnostic import step

WordPartKind = Literal[
    "literal",
    "escaped",
    "single",
    "double",
    "param",
    "command",
    "arithmetic",
    "process",
    "glob",
    "tilde",
    "dollar_quote",
]


class WordPart(TypedDict):
    """One piece of a word, carrying how the shell will treat it.

    ``text`` is the piece as written: the characters of a literal, escaped
    or single-quoted piece, the whole `$name`/`${…}` of a parameter, the
    inner source of a `$(…)` or `<(…)`, the whole `$((…))` of an arithmetic
    expansion. ``parts`` holds a double-quoted piece's children, and a
    parameter operand's -- `${X:-$(cmd)}` runs `cmd`. ``script`` holds the
    parsed command of a substitution, as a one-element list.

    A ``dollar_quote`` is the unquoted `$` that opens `$'…'` or `$"…"`. The
    quoted piece after it is read as written, but the shell rewrites it --
    escapes such as `\\x2d` in the one, a translation in the other -- so the
    text is not what the program receives, and this piece says so.
    """

    kind: WordPartKind
    text: str
    name: str
    operator: str
    """A parameter's operator: ``""`` for `$X`/`${X}`, ``":="``, ``"-"`` and
    the rest as spelled, ``"length"`` for `${#X}`, ``"complex"`` for a form
    this does not model (`${!X}`, `${a[1]}`)."""
    parts: "list[WordPart]"
    script: "list[Script]"


class Word(TypedDict):
    """One shell word: the parts it is spelled from."""

    parts: list[WordPart]


class Heredoc(TypedDict):
    """A heredoc's delimiter, whether quoting made its body literal, and the body.

    The body is filled in when the lexer reaches the newline that begins it,
    which is after the redirection naming it has been read.
    """

    delimiter: str
    quoted: bool
    body: str


class Redirect(TypedDict):
    """One redirection: its operator with any descriptor, and what it names.

    ``target`` is empty for a descriptor duplication (`2>&1`), for a heredoc
    (whose delimiter is in ``heredoc``), and for an operator the line left
    with nothing to name.
    """

    operator: str
    target: list[Word]
    heredoc: list[Heredoc]


CommandKind = Literal[
    "simple",
    "test",
    "brace",
    "subshell",
    "if",
    "for",
    "select",
    "while",
    "until",
    "case",
    "function",
    "arithmetic",
]


class Clause(TypedDict):
    """A condition list and the body it guards: an `if`/`elif` arm, a loop."""

    condition: "Script"
    body: "Script"


class Arm(TypedDict):
    """One `case` arm: the patterns it matches and the list it runs."""

    patterns: list[Word]
    body: "Script"


class Command(TypedDict):
    """One command of any kind; ``kind`` says which fields carry it.

    ``simple``: ``words`` and ``redirects``. ``test``: the words between
    `[[` and `]]`. ``brace``/``subshell``: ``body``. ``if``: ``clauses``,
    and ``body`` for `else`. ``while``/``until``: one clause. ``for``/
    ``select``: ``name``, ``words`` for the list when ``listed``, ``body``.
    ``case``: ``words`` holding the subject, ``arms``. ``function``:
    ``name``, ``body``. ``arithmetic``: ``words`` holding the expression.
    Every compound command may carry ``redirects`` of its own.
    """

    kind: CommandKind
    words: list[Word]
    redirects: list[Redirect]
    name: str
    listed: bool
    clauses: list[Clause]
    body: "Script"
    arms: list[Arm]
    directory: str | None
    """Where the shell stands when it runs this, spelled from the launch
    directory and filled in by the placing pass in ``lex.py``. The empty
    string is the launch directory itself; ``None`` is a walk that lost
    track of where a ``cd`` left the shell, which makes every path word here
    unresolvable rather than resolved against a directory the command never
    ran in."""


class Pipeline(TypedDict):
    """Commands joined by `|` or `|&`, in order, and whether `!` negates it."""

    commands: list[Command]
    operators: list[str]
    negated: bool


class AndOr(TypedDict):
    """Pipelines joined by `&&` and `||`; every one after the first is conditional."""

    pipelines: list[Pipeline]
    operators: list[str]


class Item(TypedDict):
    """One and-or list and what ended it: `;`, `&`, a newline, or nothing."""

    andor: AndOr
    terminator: str


class Script(TypedDict):
    """A command list: a whole line, a compound command's body, a substitution."""

    items: list[Item]


class ShellSyntaxError(Exception):
    """A line this grammar will not read, carrying the decision that says so."""

    def __init__(self, decision: KernelDecision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


TokenKind = Literal["word", "op", "redirect", "newline", "eof"]


class Token(TypedDict):
    """One lexed token. A word carries its parts; an operator its spelling."""

    kind: TokenKind
    text: str
    word: list[Word]


# lup: ignore[library-default] — POSIX shell reserved words, bash's `[[` included
RESERVED_WORDS = (
    "if",
    "then",
    "elif",
    "else",
    "fi",
    "do",
    "done",
    "case",
    "esac",
    "while",
    "until",
    "for",
    "select",
    "in",
    "function",
    "{",
    "}",
    "!",
    "[[",
    "]]",
)
# lup: ignore[library-default] — the shell's control operators, longest first
CONTROL_OPERATORS = (";;&", ";;", ";&", "&&", "||", "|&", "((", ";", "|", "(", ")")
# lup: ignore[constant-declaration] — the shell's own special parameter names
SPECIAL_PARAMETERS = "@*#?$!-0123456789"
# lup: ignore[library-default] — bash's parameter-expansion operators, longest first
PARAMETER_OPERATORS = (
    ":=",
    ":-",
    ":+",
    ":?",
    "##",
    "%%",
    "//",
    "^^",
    ",,",
    "=",
    "-",
    "+",
    "?",
    "#",
    "%",
    "/",
    "^",
    ",",
    ":",
)
# lup: ignore[library-default] — POSIX `case` arm terminators, bash's included
CASE_TERMINATORS = (";;", ";&", ";;&")
# lup: ignore[constant-declaration] — a bound on recursion the kernel's own stack
# sets, not a policy anybody would tune
MAXIMUM_NESTING = 64
# lup: ignore[constant-declaration] — how deep a substitution may nest before the
# line is left unjudged; the classifier's reasons name this depth
MAXIMUM_SUBSTITUTION_DEPTH = 2


def part(
    kind: WordPartKind,
    text: str,
    name: str = "",
    operator: str = "",
    parts: list[WordPart] | None = None,
    script: list[Script] | None = None,
) -> WordPart:
    """Build one word part, defaulting what its kind does not carry."""
    return WordPart(
        kind=kind,
        text=text,
        name=name,
        operator=operator,
        parts=parts or [],
        script=script or [],
    )


def empty_script() -> Script:
    """A list holding no command, for a field its kind does not use."""
    return Script(items=[])


def single_command(body: Command) -> Script:
    """A list holding exactly one command."""
    return Script(
        items=[
            Item(
                andor=AndOr(
                    pipelines=[Pipeline(commands=[body], operators=[], negated=False)],
                    operators=[],
                ),
                terminator="",
            )
        ]
    )


def command(
    kind: CommandKind,
    words: list[Word] | None = None,
    name: str = "",
    listed: bool = False,
    clauses: list[Clause] | None = None,
    body: Script | None = None,
    arms: list[Arm] | None = None,
) -> Command:
    """Build one command, defaulting the fields its kind does not use."""
    return Command(
        kind=kind,
        words=words or [],
        redirects=[],
        name=name,
        listed=listed,
        clauses=clauses or [],
        body=body or empty_script(),
        arms=arms or [],
        directory="",
    )


def syntax_error(reason: str = "shell command does not parse") -> ShellSyntaxError:
    """The unjudged verdict for a line this grammar will not read."""
    return ShellSyntaxError(unjudged(reason))


type DelimiterStep = Callable[[str], int]
"""How far one character moves the depth a scan is counting: its pair, or nothing."""

type DelimiterSkip = Callable[[str, int], int | None]
"""What a depth scan steps over rather than counts, asked of one position.

One past the run where a run opens there, ``position`` itself where none
does, and ``None`` where one opens and never closes -- which is already the
scan's answer for a group that never closes, so it travels out unchanged.
"""


def parenthesis_step(character: str) -> int:
    """How far a parenthesis moves the depth a scan is counting."""
    match character:
        case "(":
            return 1
        case ")":
            return -1
    return 0


def brace_step(character: str) -> int:
    """How far a brace moves the depth a scan is counting."""
    match character:
        case "{":
            return 1
        case "}":
            return -1
    return 0


def single_quoted_end(source: str, position: int) -> int | None:
    """One past the `'` closing the literal opened at ``position``."""
    closing = source.find("'", position + 1)
    return None if closing == -1 else closing + 1


def double_quoted_end(source: str, position: int) -> int | None:
    """One past the `"` closing the run opened at ``position``, escapes honoured."""
    length = len(source)
    position += 1
    while position < length and source[position] != '"':
        position += 2 if source[position] == "\\" else 1
    return None if position >= length else position + 1


def no_skip(_source: str, position: int) -> int | None:
    """Step over nothing: every character is the scan's own to count."""
    return position


def quoted_skip(source: str, position: int) -> int | None:
    """Step over the quoting a `(…)` group honours, inside which nothing counts."""
    match source[position]:
        case "'":
            return single_quoted_end(source, position)
        case '"':
            return double_quoted_end(source, position)
        case "\\":
            return position + 2
    return position


def brace_skip(source: str, position: int) -> int | None:
    """Step over what a `${…}` does not count, a whole `$(…)` among it.

    Double quotes are not stepped over, as the shell has it: a `}` written
    inside them closes the expansion.
    """
    match source[position]:
        case "\\":
            return position + 2
        case "'":
            return single_quoted_end(source, position)
        case "$" if source.startswith("$(", position):
            end = balanced_end(source, position + 2)
            return None if end is None else end + 1
    return position


def arithmetic_skip(source: str, position: int) -> int | None:
    """Refuse what an arithmetic interior may not hold, and step over nothing.

    Arithmetic evaluates, it does not run: a backtick or a `$(` inside one is
    a command substitution wearing an expansion, and is denied rather than
    counted.
    """
    if source[position] == "`" or source.startswith("$(", position):
        raise ShellSyntaxError(
            KernelDecision("deny", SUBSTITUTION_REASON, recovery=SUBSTITUTION_RECOVERY)
        )
    return position


def delimiter_end(
    source: str,
    position: int,
    step: DelimiterStep = parenthesis_step,
    depth: int = 1,
    skip: DelimiterSkip = no_skip,
) -> int | None:
    """The index of the delimiter closing a group whose interior starts here.

    One walk serves every delimiter this grammar counts: ``step`` says which
    pair moves the depth and which way, ``depth`` how deep the caller already
    stands -- two for a form a doubled delimiter opened -- and ``skip`` which
    runs are stepped over whole. Nothing but a closing delimiter lowers the
    depth, so where it reaches zero is the index wanted; ``None`` where a
    group never closes.
    """
    length = len(source)
    while position < length:
        stepped = skip(source, position)
        if stepped is None:
            return None
        if stepped != position:
            position = stepped
            continue
        depth += step(source[position])
        if depth == 0:
            return position
        position += 1
    return None


def balanced_end(source: str, position: int) -> int | None:
    """The index of the `)` closing a group whose interior starts at ``position``.

    Quotes and escapes are honoured, so a parenthesis inside them does not
    count. ``None`` where the group never closes.
    """
    return delimiter_end(source, position, skip=quoted_skip)


def brace_end(source: str, position: int) -> int | None:
    """The index of the `}` closing a `${` whose interior starts at ``position``."""
    return delimiter_end(source, position, step=brace_step, skip=brace_skip)


def without_leading_tabs(line: str) -> str:
    """The line with its `<<-`-style leading tab indentation removed."""
    first = next(
        (index for index, character in enumerate(line) if character != "\t"),
        len(line),
    )
    return line[first:]


class ShellLexer:
    """Tokens of one command line, read on demand, one token of lookahead.

    Heredoc bodies are read here rather than by the parser, because they
    begin at the next newline the lexer reaches, wherever the grammar is at
    that moment.
    """

    def __init__(self, source: str, depth: int = 0) -> None:
        self.source = source
        self.position = 0
        self.depth = depth
        self.buffered: list[Token] = []
        self.expecting: list[Heredoc] = []
        self.pending: list[Heredoc] = []

    def peek(self) -> Token:
        """The next token, without consuming it."""
        if not self.buffered:
            self.buffered.append(self.lex())
        return self.buffered[0]

    def take(self) -> Token:
        """The next token, consumed."""
        token = self.peek()
        self.buffered.clear()
        return token

    def at(self, offset: int = 0) -> str:
        """The character ``offset`` past the cursor, or ``""`` past the end."""
        return self.source[self.position + offset : self.position + offset + 1]

    def skip_blanks(self) -> None:
        """Step over blanks, line continuations and a comment up to its newline."""
        source = self.source
        while self.position < len(source):
            match source[self.position]:
                case " " | "\t" | "\r":
                    self.position += 1
                case "\\" if self.at(1) == "\n":
                    self.position += 2
                case "#":
                    newline = source.find("\n", self.position)
                    self.position = len(source) if newline == -1 else newline
                case _:
                    return

    def lex(self) -> Token:
        """Read one token from the cursor."""
        source = self.source
        self.skip_blanks()
        if self.expecting and (
            self.position >= len(source) or source[self.position] in "\n;&|()<>"
        ):
            raise syntax_error("heredoc has no delimiter")
        if self.position >= len(source):
            return Token(kind="eof", text="", word=[])
        character = source[self.position]
        if character == "\n":
            self.position += 1
            self.read_heredoc_bodies()
            return Token(kind="newline", text="\n", word=[])
        for operator in CONTROL_OPERATORS:
            if source.startswith(operator, self.position):
                self.position += len(operator)
                return Token(kind="op", text=operator, word=[])
        if character == "&" and self.at(1) != ">":
            self.position += 1
            return Token(kind="op", text="&", word=[])
        if character == ">" and self.at(1) == "(":
            end = balanced_end(source, self.position + 2)
            written = source[self.position + 2 : end] if end is not None else "..."
            raise ShellSyntaxError(
                KernelDecision(
                    "ask",
                    f"writing into the process substitution >({written}) feeds a"
                    " command nothing checked",
                )
            )
        if character in "<>&" and self.at(1) != "(":
            return self.redirect("")
        start = self.position
        word = self.read_word()
        spelled = source[start : self.position]
        if spelled.isdigit() and self.at() in ("<", ">") and self.at(1) != "(":
            return self.redirect(spelled)
        if self.expecting:
            heredoc = self.expecting.pop()
            heredoc["delimiter"] = word_text(word)
            heredoc["quoted"] = any(
                item["kind"] in ("single", "double", "escaped")
                for item in word["parts"]
            )
            self.pending.append(heredoc)
        return Token(kind="word", text=spelled, word=[word])

    def redirect(self, descriptor: str) -> Token:
        """Read a maximal redirection operator, with the descriptor before it."""
        source = self.source
        start = self.position
        if source[self.position] == "&":
            self.position += 1
        core = source[self.position]
        self.position += 1
        match (core, self.at()):
            case (opener, repeated) if opener == repeated:
                self.position += 1
                if core == "<" and self.at() in ("<", "-"):
                    self.position += 1
            case (">", "|") | ("<", ">"):
                self.position += 1
        if self.at() == "&":
            self.position += 1
            while self.at() and (self.at().isdigit() or self.at() == "-"):
                self.position += 1
        operator = descriptor + source[start : self.position]
        if "<<" in operator and "<<<" not in operator:
            self.expecting.append(Heredoc(delimiter="", quoted=False, body=""))
        return Token(kind="redirect", text=operator, word=[])

    def read_heredoc_bodies(self) -> None:
        """Consume every pending heredoc's body, from the line after the newline.

        A quoted delimiter makes the body literal data. An unquoted one lets
        the shell substitute inside it, so substitution syntax there is
        refused with the quoting recipe.
        """
        source = self.source
        for heredoc in self.pending:
            delimiter = heredoc["delimiter"]
            lines: list[str] = []
            terminated = False
            while self.position <= len(source):
                newline = source.find("\n", self.position)
                end = len(source) if newline == -1 else newline
                line = source[self.position : end]
                self.position = end + 1
                if line == delimiter or without_leading_tabs(line) == delimiter:
                    terminated = True
                    break
                lines.append(line)
                if newline == -1:
                    break
            if not terminated:
                raise syntax_error("heredoc does not terminate")
            body = "".join(f"{line}\n" for line in lines)
            if not heredoc["quoted"] and ("`" in body or "$(" in body):
                raise ShellSyntaxError(
                    KernelDecision(
                        "deny",
                        "an unquoted heredoc runs the commands its body substitutes",
                        recovery=(
                            step(
                                f"quote the delimiter, `<<'{delimiter}'`, to make the"
                                " body literal"
                            ),
                        ),
                        subject=f"<<{delimiter}",
                    )
                )
            heredoc["body"] = body
        self.pending.clear()

    def read_word(self) -> Word:
        """Read one unquoted word's parts, stopping at a metacharacter."""
        source = self.source
        parts: list[WordPart] = []
        literal: list[str] = []

        def flush() -> None:
            if literal:
                parts.append(part("literal", "".join(literal)))
                literal.clear()

        while self.position < len(source):
            character = source[self.position]
            started = bool(parts or literal)
            if character in " \t\r\n;&|)":
                break
            if character == "(":
                if started:
                    raise syntax_error(
                        "shell arrays and function definitions are not classified"
                    )
                break
            if character in "<>":
                if character == ">" or self.at(1) != "(":
                    break
                if started:
                    raise syntax_error(
                        "process substitution inside a word is not classified"
                    )
                parts.append(self.read_process())
                continue
            if character == "'":
                flush()
                closing = source.find("'", self.position + 1)
                if closing == -1:
                    raise syntax_error("shell quoting does not parse")
                parts.append(part("single", source[self.position + 1 : closing]))
                self.position = closing + 1
                continue
            if character == '"':
                flush()
                parts.append(self.read_double())
                continue
            if character == "\\":
                if self.at(1) == "\n":
                    self.position += 2
                    continue
                flush()
                if self.at(1):
                    parts.append(part("escaped", self.at(1)))
                self.position += 2
                continue
            if character == "`":
                raise ShellSyntaxError(
                    KernelDecision("deny", BACKTICK_REASON, recovery=BACKTICK_RECOVERY)
                )
            if character == "$" and self.at(1) in ("'", '"'):
                flush()
                parts.append(part("dollar_quote", character))
                self.position += 1
                continue
            if character == "$":
                expansion = self.read_dollar()
                if expansion["kind"] == "literal":
                    literal.append(expansion["text"])
                else:
                    flush()
                    parts.append(expansion)
                continue
            if character in "*?[":
                flush()
                parts.append(part("glob", character))
                self.position += 1
                continue
            if character == "~" and not started:
                end = self.position + 1
                while end < len(source) and source[end] not in " \t\r\n;&|()<>/'\"$`":
                    end += 1
                parts.append(part("tilde", source[self.position : end]))
                self.position = end
                continue
            literal.append(character)
            self.position += 1
        flush()
        return Word(parts=parts)

    def read_process(self) -> WordPart:
        """Read a `<(…)` process substitution, parsing the command inside."""
        end = balanced_end(self.source, self.position + 2)
        if end is None:
            raise syntax_error("process substitution does not parse")
        if self.depth >= MAXIMUM_SUBSTITUTION_DEPTH:
            raise syntax_error("process substitution nests too deeply")
        inner = self.source[self.position + 2 : end]
        self.position = end + 1
        return part("process", inner, script=[parse_tree(inner, self.depth + 1)])

    def read_quoted(self, closing: str) -> list[WordPart]:
        """Read expanding text up to ``closing``, or the end where that is ``""``.

        Double-quoted text and a parameter operand expand alike: parameters
        and substitutions stay live, and nothing splits or globs. The one
        difference is that an operand may hold quotes of its own.
        """
        source = self.source
        children: list[WordPart] = []
        literal: list[str] = []

        def flush() -> None:
            if literal:
                children.append(part("literal", "".join(literal)))
                literal.clear()

        while self.position < len(source):
            character = source[self.position]
            if closing and character == closing:
                break
            if character == "\\":
                following = self.at(1)
                if following == "\n":
                    self.position += 2
                    continue
                if following and (not closing or following in '$`"\\'):
                    flush()
                    children.append(part("escaped", following))
                    self.position += 2
                    continue
                literal.append(character)
                self.position += 1
                continue
            if character == "`":
                raise ShellSyntaxError(
                    KernelDecision("deny", BACKTICK_REASON, recovery=BACKTICK_RECOVERY)
                )
            if character == "$":
                expansion = self.read_dollar()
                if expansion["kind"] == "literal":
                    literal.append(expansion["text"])
                else:
                    flush()
                    children.append(expansion)
                continue
            if not closing and character == "'":
                flush()
                end = source.find("'", self.position + 1)
                if end == -1:
                    raise syntax_error("shell quoting does not parse")
                children.append(part("single", source[self.position + 1 : end]))
                self.position = end + 1
                continue
            if not closing and character == '"':
                flush()
                children.append(self.read_double())
                continue
            literal.append(character)
            self.position += 1
        flush()
        return children

    def read_double(self) -> WordPart:
        """Read a double-quoted piece, keeping each expansion inside it."""
        self.position += 1
        children = self.read_quoted('"')
        if self.position >= len(self.source):
            raise syntax_error("shell quoting does not parse")
        self.position += 1
        return part("double", "", parts=children)

    def read_dollar(self) -> WordPart:
        """Read what a `$` begins: an expansion, or the literal dollar sign."""
        source = self.source
        start = self.position
        if source.startswith("$((", start):
            return self.read_arithmetic()
        if source.startswith("$(", start):
            end = balanced_end(source, start + 2)
            if end is None:
                raise syntax_error("command substitution does not parse")
            if self.depth >= MAXIMUM_SUBSTITUTION_DEPTH:
                raise syntax_error("command substitution nests too deeply")
            inner = source[start + 2 : end]
            self.position = end + 1
            return part("command", inner, script=[parse_tree(inner, self.depth + 1)])
        if source.startswith("${", start):
            end = brace_end(source, start + 2)
            if end is None:
                raise syntax_error("parameter expansion does not parse")
            self.position = end + 1
            return self.parameter(source[start + 2 : end], source[start : end + 1])
        following = self.at(1)
        if following and (following.isalpha() or following == "_"):
            end = start + 1
            while end < len(source) and (source[end].isalnum() or source[end] == "_"):
                end += 1
            self.position = end
            return part("param", source[start:end], name=source[start + 1 : end])
        if following and following in SPECIAL_PARAMETERS:
            self.position = start + 2
            return part("param", source[start : start + 2], name=following)
        self.position = start + 1
        return part("literal", "$")

    def read_arithmetic(self) -> WordPart:
        """Read a `$((…))` expansion, whose interior may not run a command."""
        source = self.source
        start = self.position
        end = delimiter_end(source, start + 3, depth=2, skip=arithmetic_skip)
        if end is None:
            raise syntax_error("arithmetic expansion does not parse")
        self.position = end + 1
        return part("arithmetic", source[start : end + 1])

    def parameter(self, inner: str, spelled: str) -> WordPart:
        """Read the interior of a `${…}` into its name, operator and operand."""
        if inner.startswith("#") and len(inner) > 1:
            counted = inner[1:]
            if counted.isidentifier() or counted in SPECIAL_PARAMETERS:
                return part("param", spelled, name=counted, operator="length")
        head = 0
        while head < len(inner) and (inner[head].isalnum() or inner[head] == "_"):
            head += 1
        if head == 0 and inner[:1] and inner[0] in SPECIAL_PARAMETERS:
            head = 1
        name = inner[:head]
        rest = inner[head:]
        if not name or not (name.isidentifier() or name.isdigit() or head == 1):
            return part("param", spelled, operator="complex")
        if not rest:
            return part("param", spelled, name=name)
        operator = next(
            (
                candidate
                for candidate in PARAMETER_OPERATORS
                if rest.startswith(candidate)
            ),
            "",
        )
        if not operator:
            return part("param", spelled, name=name, operator="complex")
        operand = ShellLexer(rest[len(operator) :], self.depth)
        return part(
            "param",
            spelled,
            name=name,
            operator=operator,
            parts=operand.read_quoted(""),
        )

    def read_arithmetic_command(self) -> str:
        """Read a `(( … ))` command's expression, the opening `((` already taken."""
        source = self.source
        start = self.position
        end = delimiter_end(source, start, depth=2)
        if end is None:
            raise syntax_error("arithmetic command does not parse")
        self.position = end + 1
        return source[start : end - 1]


class VerbatimText(str):
    """A word's text where every character reaches the program as written.

    Every rule reads a word as a string, and reads a `$` in one as an
    expansion. Quoting says it often is not one: `'a$'`, `'$HOME'` and `\\$x`
    are those characters and nothing more. The tree knows which is which, and
    this carries that fact past :func:`word_text` into the string the rules
    read, without changing a character of it. A reader of the content -- a
    sed script the host runs, the bytes an `echo` writes -- sees exactly the
    text it always saw.

    Anything derived from one is a plain ``str`` again: a slice, a join, a
    normalized path. That is the safe direction, because a plain string is
    read the way every string was read before this existed. A reader that
    keeps a derived piece verbatim says so through :func:`verbatim_piece`.
    """


def verbatim_piece(whole: str, piece: str) -> str:
    """A piece cut from a word, verbatim wherever the word it came from was.

    Every character of a verbatim word is literal, so every run of them is
    too: `--output='a$b'` names the path `a$b` as surely as `-o 'a$b'` does.
    """
    return VerbatimText(piece) if isinstance(whole, VerbatimText) else piece


def verbatim_part(item: WordPart) -> bool:
    """Whether one part reaches the program as exactly the text it reads as.

    Quoting and escaping make a piece literal. Outside them a literal piece is
    literal only where nothing in it opens an expansion this grammar reads as
    plain characters: a `{` a brace expansion opens on, and a `~` bash expands
    after `=` and `:`. A `$` there is literal already, since the lexer reads
    one as a parameter wherever it can open one.
    """
    match item["kind"]:
        case "escaped" | "single":
            return True
        case "double":
            return all(
                child["kind"] in ("literal", "escaped") for child in item["parts"]
            )
        case "literal":
            return not any(character in item["text"] for character in "{~")
        case _:
            return False


def verbatim(word: Word) -> bool:
    """Whether a whole word reaches the program as exactly the text it reads as."""
    return all(verbatim_part(item) for item in word["parts"])


def expands(word: str) -> bool:
    """Whether a word as the rules read it could still become something else.

    A `$` or a backtick left standing in the text is an expansion nothing
    resolved, unless the word is :class:`VerbatimText` and the character was
    quoted into it.
    """
    return not isinstance(word, VerbatimText) and any(marker in word for marker in "$`")


def word_text(word: Word) -> str:
    """The string a word reads as, once quoting is removed.

    What every word-level reader matches on. An expansion nothing resolved
    keeps its spelling, so a `$` still standing is exactly a word that could
    become something else; a command substitution reads as
    :data:`SUBSTITUTION_SENTINEL` and a process substitution as the
    `/dev/fd` path the command is handed. A word nothing in expands comes
    back as :class:`VerbatimText`, so the `$` its quotes held reads as the
    character it is.
    """
    text = "".join(part_text(item) for item in word["parts"])
    return VerbatimText(text) if verbatim(word) else text


def part_text(item: WordPart) -> str:
    """The string one word part reads as, once quoting is removed."""
    match item["kind"]:
        case "double":
            return "".join(part_text(child) for child in item["parts"])
        case "command":
            return SUBSTITUTION_SENTINEL
        case "process":
            return "/dev/fd/63"
        case "param" if item["parts"]:
            return (
                "${"
                + item["name"]
                + item["operator"]
                + "".join(part_text(child) for child in item["parts"])
                + "}"
            )
        case _:
            return item["text"]


def plain_text(word: Word) -> str | None:
    """A word's text where nothing in it is quoted or expanded, else ``None``.

    Reserved words are recognized only in this form: `"if"` is a command
    named `if`, not the start of a conditional.
    """
    if not all(item["kind"] in ("literal", "glob") for item in word["parts"]):
        return None
    return "".join(item["text"] for item in word["parts"])


class ShellParser:
    """The recursive descent over one lexer's tokens."""

    def __init__(self, lexer: ShellLexer) -> None:
        self.lexer = lexer
        self.nesting = 0

    def reserved(self, token: Token) -> str:
        """The reserved word a token spells in command position, or ``""``."""
        if token["kind"] != "word":
            return ""
        spelled = plain_text(token["word"][0])
        return spelled if spelled in RESERVED_WORDS else ""

    def expect_word(self, spelled: str) -> None:
        """Consume a reserved word the grammar requires here."""
        if self.reserved(self.lexer.take()) != spelled:
            raise syntax_error()

    def expect_op(self, spelled: str) -> None:
        """Consume an operator the grammar requires here."""
        token = self.lexer.take()
        if token["kind"] != "op" or token["text"] != spelled:
            raise syntax_error()

    def linebreak(self) -> None:
        """Skip the newlines a list may begin or continue with."""
        while self.lexer.peek()["kind"] == "newline":
            self.lexer.take()

    def stops(
        self, token: Token, stop_words: tuple[str, ...], stop_ops: tuple[str, ...]
    ) -> bool:
        """Whether a token ends the list being read."""
        if token["kind"] == "eof":
            return True
        if token["kind"] == "op":
            return token["text"] in stop_ops
        reserved = self.reserved(token)
        return bool(reserved) and reserved in stop_words

    def script(
        self, stop_words: tuple[str, ...] = (), stop_ops: tuple[str, ...] = ()
    ) -> Script:
        """Read a command list up to a stop word, a stop operator, or the end."""
        self.nesting += 1
        if self.nesting > MAXIMUM_NESTING:
            raise syntax_error("shell command nests too deeply")
        items: list[Item] = []
        self.linebreak()
        while not self.stops(self.lexer.peek(), stop_words, stop_ops):
            andor = self.andor()
            following = self.lexer.peek()
            match following:
                case {"kind": "op", "text": ";" | "&" as terminator}:
                    self.lexer.take()
                    items.append(Item(andor=andor, terminator=terminator))
                case {"kind": "newline"}:
                    self.lexer.take()
                    items.append(Item(andor=andor, terminator="\n"))
                case _:
                    if not self.stops(following, stop_words, stop_ops):
                        raise syntax_error()
                    items.append(Item(andor=andor, terminator=""))
            self.linebreak()
        self.nesting -= 1
        return Script(items=items)

    def andor(self) -> AndOr:
        """Read pipelines joined by `&&` and `||`."""
        pipelines = [self.pipeline()]
        operators: list[str] = []
        while (token := self.lexer.peek())["kind"] == "op" and token["text"] in (
            "&&",
            "||",
        ):
            self.lexer.take()
            operators.append(token["text"])
            self.linebreak()
            pipelines.append(self.pipeline())
        return AndOr(pipelines=pipelines, operators=operators)

    def pipeline(self) -> Pipeline:
        """Read commands joined by `|` and `|&`, after any `!`."""
        negated = False
        while self.reserved(self.lexer.peek()) == "!":
            self.lexer.take()
            negated = True
        commands = [self.command()]
        operators: list[str] = []
        while (token := self.lexer.peek())["kind"] == "op" and token["text"] in (
            "|",
            "|&",
        ):
            self.lexer.take()
            operators.append(token["text"])
            self.linebreak()
            commands.append(self.command())
        return Pipeline(commands=commands, operators=operators, negated=negated)

    def command(self) -> Command:
        """Read one command of whatever kind the next token begins."""
        token = self.lexer.peek()
        match (token["kind"], token["text"], self.reserved(token)):
            case ("op", "(", _):
                self.lexer.take()
                body = self.nonempty(self.script(stop_ops=(")",)))
                self.expect_op(")")
                built = command("subshell", body=body)
            case ("op", "((", _):
                self.lexer.take()
                expression = self.lexer.read_arithmetic_command()
                built = command(
                    "arithmetic", words=[Word(parts=[part("literal", expression)])]
                )
            case ("word", _, "{"):
                self.lexer.take()
                body = self.nonempty(self.script(stop_words=("}",)))
                self.expect_word("}")
                built = command("brace", body=body)
            case ("word", _, "if"):
                built = self.conditional()
            case ("word", _, "for" | "select"):
                built = self.loop_over()
            case ("word", _, "while" | "until"):
                built = self.loop_while()
            case ("word", _, "case"):
                built = self.case()
            case ("word", _, "function"):
                built = self.function_keyword()
            case ("word", _, "[["):
                built = self.test()
            case ("word", _, reserved) if reserved and reserved != "]]":
                raise syntax_error()
            case ("word" | "redirect", _, _):
                return self.simple()
            case _:
                raise syntax_error()
        built["redirects"] = self.redirects()
        return built

    def redirects(self) -> list[Redirect]:
        """Read the redirections a compound command carries after its end."""
        found: list[Redirect] = []
        while self.lexer.peek()["kind"] == "redirect":
            found.append(self.redirect())
        return found

    def redirect(self) -> Redirect:
        """Read one redirection operator and the word it names, if any."""
        operator = self.lexer.take()["text"]
        if "<<" in operator and "<<<" not in operator:
            if self.lexer.take()["kind"] != "word":
                raise syntax_error("heredoc has no delimiter")
            return Redirect(
                operator=operator, target=[], heredoc=[self.lexer.pending[-1]]
            )
        if "&" in operator and (operator[-1].isdigit() or operator[-1] == "-"):
            return Redirect(operator=operator, target=[], heredoc=[])
        if self.lexer.peek()["kind"] != "word":
            return Redirect(operator=operator, target=[], heredoc=[])
        return Redirect(operator=operator, target=self.lexer.take()["word"], heredoc=[])

    def simple(self) -> Command:
        """Read a simple command's words and redirections, or a function definition."""
        words: list[Word] = []
        redirects: list[Redirect] = []
        while True:
            token = self.lexer.peek()
            if token["kind"] == "redirect":
                redirects.append(self.redirect())
                continue
            if token["kind"] != "word":
                break
            self.lexer.take()
            words.append(token["word"][0])
            following = self.lexer.peek()
            if (
                len(words) == 1
                and not redirects
                and following["kind"] == "op"
                and following["text"] == "("
            ):
                return self.function_body(word_text(words[0]))
        built = command("simple", words=words)
        built["redirects"] = redirects
        return built

    def function_body(self, name: str) -> Command:
        """Read `() compound-command` after a function's name."""
        self.expect_op("(")
        self.expect_op(")")
        self.linebreak()
        return command("function", name=name, body=single_command(self.command()))

    def function_keyword(self) -> Command:
        """Read `function name [()] compound-command`."""
        self.lexer.take()
        token = self.lexer.take()
        if token["kind"] != "word":
            raise syntax_error()
        name = word_text(token["word"][0])
        following = self.lexer.peek()
        if following["kind"] == "op" and following["text"] == "(":
            return self.function_body(name)
        self.linebreak()
        return command("function", name=name, body=single_command(self.command()))

    def conditional(self) -> Command:
        """Read `if … then … [elif … then …] [else …] fi`."""
        self.lexer.take()
        clauses: list[Clause] = []
        otherwise = empty_script()
        while True:
            condition = self.nonempty(self.script(stop_words=("then",)))
            self.expect_word("then")
            body = self.nonempty(self.script(stop_words=("elif", "else", "fi")))
            clauses.append(Clause(condition=condition, body=body))
            match self.reserved(self.lexer.take()):
                case "elif":
                    continue
                case "else":
                    otherwise = self.nonempty(self.script(stop_words=("fi",)))
                    self.expect_word("fi")
                case "fi":
                    pass
                case _:
                    raise syntax_error()
            return command("if", clauses=clauses, body=otherwise)

    def nonempty(self, script: Script) -> Script:
        """A list the grammar requires to hold a command."""
        if not script["items"]:
            raise syntax_error()
        return script

    def loop_over(self) -> Command:
        """Read `for`/`select name [in words…]; do … done`."""
        kind = "for" if self.reserved(self.lexer.take()) == "for" else "select"
        token = self.lexer.take()
        name = plain_text(token["word"][0]) if token["kind"] == "word" else None
        if name is None or not name.isidentifier():
            raise syntax_error("loop form is not classified")
        self.linebreak()
        items: list[Word] = []
        listed = self.reserved(self.lexer.peek()) == "in"
        if listed:
            self.lexer.take()
            while self.lexer.peek()["kind"] == "word":
                items.append(self.lexer.take()["word"][0])
        match self.lexer.peek():
            case {"kind": "op", "text": ";"}:
                self.lexer.take()
            case {"kind": token_kind} if listed and token_kind != "newline":
                raise syntax_error()
        self.linebreak()
        self.expect_word("do")
        body = self.nonempty(self.script(stop_words=("done",)))
        self.expect_word("done")
        return command(kind, words=items, name=name, listed=listed, body=body)

    def loop_while(self) -> Command:
        """Read `while`/`until condition; do … done`."""
        kind = "while" if self.reserved(self.lexer.take()) == "while" else "until"
        condition = self.nonempty(self.script(stop_words=("do",)))
        self.expect_word("do")
        body = self.nonempty(self.script(stop_words=("done",)))
        self.expect_word("done")
        return command(kind, clauses=[Clause(condition=condition, body=body)])

    def case(self) -> Command:
        """Read `case word in [(]pattern[|pattern]) list ;; … esac`."""
        self.lexer.take()
        subject = self.lexer.take()
        if subject["kind"] != "word":
            raise syntax_error()
        self.linebreak()
        self.expect_word("in")
        arms: list[Arm] = []
        self.linebreak()
        while self.reserved(self.lexer.peek()) != "esac":
            opening = self.lexer.peek()
            if opening["kind"] == "op" and opening["text"] == "(":
                self.lexer.take()
            patterns = [self.pattern()]
            while (separator := self.lexer.take())["text"] == "|":
                patterns.append(self.pattern())
            if separator["kind"] != "op" or separator["text"] != ")":
                raise syntax_error()
            body = self.script(stop_words=("esac",), stop_ops=CASE_TERMINATORS)
            arms.append(Arm(patterns=patterns, body=body))
            ending = self.lexer.peek()
            match ending:
                case {"kind": "op", "text": text} if text in CASE_TERMINATORS:
                    self.lexer.take()
                    self.linebreak()
                case _:
                    if self.reserved(ending) != "esac":
                        raise syntax_error()
        self.lexer.take()
        return command("case", words=subject["word"], arms=arms)

    def pattern(self) -> Word:
        """Read one `case` pattern word."""
        token = self.lexer.take()
        if token["kind"] != "word":
            raise syntax_error()
        return token["word"][0]

    def test(self) -> Command:
        """Read `[[ … ]]` as the words between, operators inside it ignored."""
        self.lexer.take()
        words: list[Word] = []
        while True:
            token = self.lexer.take()
            if token["kind"] == "eof":
                raise syntax_error("test expression does not parse")
            if token["kind"] != "word":
                continue
            word = token["word"][0]
            if plain_text(word) == "]]":
                return command("test", words=words)
            if any(item["kind"] == "process" for item in word["parts"]):
                raise syntax_error(
                    "process substitution inside [[ ]] is not classified"
                )
            words.append(word)


def finished(lexer: ShellLexer, script: Script) -> Script:
    """A tree the whole line was read into, refusing anything left over."""
    if lexer.peek()["kind"] != "eof":
        raise syntax_error()
    if lexer.expecting:
        raise syntax_error("heredoc has no delimiter")
    if lexer.pending:
        raise syntax_error("heredoc does not terminate")
    return script


def parse_tree(source: str, depth: int = 0) -> Script:
    """Read one command line into its tree, raising where the grammar refuses it."""
    lexer = ShellLexer(source, depth)
    return finished(lexer, ShellParser(lexer).script())


def stronger_lexical_verdict(
    lexer: ShellLexer, decision: KernelDecision
) -> KernelDecision:
    """A refusal the rest of the line carries, over a grammar that stopped early.

    A backtick or a substituting heredoc is refused wherever it stands, and a
    syntax error earlier in the line does not make it any less present. So
    where the grammar only abstained, the remaining source is still lexed,
    and a refusal met there is the verdict instead.
    """
    if decision.effect != "defer":
        return decision
    lexer.buffered.clear()
    lexer.expecting.clear()
    lexer.pending.clear()
    try:
        while lexer.take()["kind"] != "eof":
            continue
    except ShellSyntaxError as later:
        if later.decision.effect in ("deny", "ask"):
            return later.decision
    except RecursionError:
        return decision
    return decision


def parse_script(source: str) -> Script | KernelDecision:
    """Read one command line into its tree, or the decision saying why not.

    Never raises: a line the grammar will not read is a verdict the caller
    returns, whichever of the readers asked.
    """
    lexer = ShellLexer(source)
    try:
        return finished(lexer, ShellParser(lexer).script())
    except ShellSyntaxError as refused:
        return stronger_lexical_verdict(lexer, refused.decision)
    except RecursionError:
        return unjudged("shell command nests too deeply")


def readable_prefix(source: str) -> Script:
    """The complete lines a command holds before the line its grammar stops on.

    For a line :func:`parse_script` refused. The shell parses a whole line
    before it runs any of it, and runs each line before reading the next, so
    every line ended before the one that does not parse has already run by
    the time the shell refuses -- and those are what a classifier still has
    to judge. Commands sharing the refused line run nowhere.
    """
    lexer = ShellLexer(source)
    parser = ShellParser(lexer)
    committed: list[Item] = []
    line: list[Item] = []
    try:
        parser.linebreak()
        while lexer.peek()["kind"] != "eof":
            andor = parser.andor()
            following = lexer.take()
            match (following["kind"], following["text"]):
                case ("op", ";" | "&"):
                    line.append(Item(andor=andor, terminator=following["text"]))
                case ("newline", _):
                    committed.extend([*line, Item(andor=andor, terminator="\n")])
                    line.clear()
                    parser.linebreak()
                case _:
                    break
    except (ShellSyntaxError, RecursionError):
        pass
    return Script(items=committed)
