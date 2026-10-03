# lup: ignore[empty-collection]
# The dependency-free runtime deliberately uses primitive rows and stdlib scanners.
"""TypeScript syntax: which files are the family, and where their prose sits.

Python's prose is read out by the standard library's own tokenizer, and this
family has none to reach for: the kernel's allowlist admits no parser, and the
standard library carries no reader for this grammar. So the one a rule needs is
written here — a single pass returning every string, template, regex and
comment run as an offset span, which the maskers below blank while every line
and column stays where it was.
"""

from typing import TypedDict


# lup: ignore[library-default] — the suffixes the TypeScript-family rule table reads; the kernel carries no config
TYPESCRIPT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte")
"""The files whose text the TypeScript-family maskers below read.

The one family `lup.harness.codescan.antipatterns` checks against its
TypeScript table: the hook deriving the language from this tuple and the
audit from another would mask the same file two ways and judge it twice.
"""


class ScriptSpan(TypedDict):
    """One run of TypeScript-family text that is not code.

    ``start`` and ``end`` are character offsets into the source, half-open.
    ``kind`` is "string" for a string, template or regex literal and
    "comment" for a `//` or `/* */` comment.
    """

    start: int
    end: int
    kind: str


def typescript_spans(
    source: str,
    expression_openers: str = "(,=:[!&|?{};+-*%<>~^",
    expression_words: tuple[str, ...] = (
        "return",
        "typeof",
        "case",
        "do",
        "else",
        "in",
        "of",
        "instanceof",
        "new",
        "delete",
        "void",
        "throw",
        "yield",
        "await",
    ),
) -> list[ScriptSpan]:
    """Every string, template, regex and comment run of TypeScript-family text.

    A scan rule reads code, and in this family the prose sits where the
    Python tokenizer cannot see it: `//` and `/* */` comments, `'`, `"` and
    backtick literals, and the body of a regex literal. Each is returned as
    an offset span so a masker can blank it while every line and column
    stays where it was.

    One pass, character by character. A template literal's `${ }` holes are
    code, so the literal is cut around them, and the braces inside a hole
    are counted so its closing brace is told from one of its own. A `/` is a
    regex literal where an expression can start — after an operator, an
    opening bracket, a separator, or one of the words the grammar puts before
    an expression — and a division everywhere else, the reading every lexer
    without a parser settles for; a `/` misread as a regex blanks the rest of
    its line and never more. A `'` or `"` literal ends at its line, as the
    grammar has it, so an unbalanced quote in a `.vue` template costs one
    line rather than the file.
    """
    spans: list[ScriptSpan] = []
    holes: list[int] = []
    length = len(source)
    # The last code character and the word it ends, which is all a `/` needs
    # to know whether an expression can start where it stands.
    last_code = ""
    word = ""

    def literal_end(quote: str, start: int) -> int:
        """One past the quote closing a literal opened at `start`, or its line's end."""
        index = start + 1
        while index < length:
            match source[index]:
                case "\\":
                    index += 2
                case "\n":
                    return index
                case character:
                    if character == quote:
                        return index + 1
                    index += 1
        return length

    def regex_end(start: int) -> int:
        """One past the flags of a regex literal opened at `start`, or its line's end."""
        index = start + 1
        in_class = False
        while index < length:
            match source[index]:
                case "\\":
                    index += 2
                case "\n":
                    return index
                case "[":
                    in_class = True
                    index += 1
                case "]":
                    in_class = False
                    index += 1
                case "/" if not in_class:
                    index += 1
                    while index < length and source[index].isalpha():
                        index += 1
                    return index
                case _:
                    index += 1
        return length

    def template_piece(start: int) -> int:
        """Record the template text from `start` and return where code resumes.

        `start` is the opening backtick or the `}` closing a hole; the piece
        runs to the closing backtick, taken with it, or to the `${` opening
        the next hole, which is left to the code scan.
        """
        index = start + 1
        while index < length:
            match source[index]:
                case "\\":
                    index += 2
                case "`":
                    spans.append(ScriptSpan(start=start, end=index + 1, kind="string"))
                    return index + 1
                case "$" if source.startswith("${", index):
                    spans.append(ScriptSpan(start=start, end=index, kind="string"))
                    holes.append(0)
                    return index + 2
                case _:
                    index += 1
        spans.append(ScriptSpan(start=start, end=length, kind="string"))
        return length

    position = 0
    while position < length:
        character = source[position]
        match character:
            case "/" if source.startswith("//", position):
                newline = source.find("\n", position)
                end = length if newline < 0 else newline
                spans.append(ScriptSpan(start=position, end=end, kind="comment"))
                position = end
            case "/" if source.startswith("/*", position):
                close = source.find("*/", position + 2)
                end = length if close < 0 else close + 2
                spans.append(ScriptSpan(start=position, end=end, kind="comment"))
                position = end
            case "'" | '"':
                end = literal_end(character, position)
                spans.append(ScriptSpan(start=position, end=end, kind="string"))
                position = end
                last_code, word = character, ""
            case "`":
                position = template_piece(position)
                last_code, word = character, ""
            case "/" if (
                not last_code
                or last_code in expression_openers
                or word in expression_words
            ):
                end = regex_end(position)
                spans.append(ScriptSpan(start=position, end=end, kind="string"))
                position = end
                last_code, word = "/", ""
            case "{" if holes:
                holes[-1] += 1
                position += 1
                last_code, word = character, ""
            case "}" if holes and not holes[-1]:
                holes.pop()
                position = template_piece(position)
                last_code, word = "`", ""
            case "}" if holes:
                holes[-1] -= 1
                position += 1
                last_code, word = character, ""
            case _:
                position += 1
                if not character.isspace():
                    last_code = character
                    word = (
                        word + character
                        if character.isalnum() or character in "_$"
                        else ""
                    )
    return spans


def masked_typescript_lines(source: str, comments: bool) -> list[str]:
    """The lines of TypeScript-family text with its prose blanked, columns kept.

    String, template and regex text always goes, leaving the character that
    opened it so a rule can still see that a literal stood there; ``comments``
    says whether `//` and `/* */` runs go too, which is the difference
    between what a "code" rule and a "comment" rule read.
    """
    spans = [
        span
        for span in typescript_spans(source)
        if comments or span["kind"] == "string"
    ]
    lines: list[str] = []
    offset = 0
    index = 0
    for line in source.splitlines(keepends=True):
        content = line.splitlines()[0]
        chars = list(content)
        stop = offset + len(content)
        while index < len(spans) and spans[index]["end"] <= offset:
            index += 1
        cursor = index
        while cursor < len(spans) and spans[cursor]["start"] < stop:
            span = spans[cursor]
            kept = 1 if span["kind"] == "string" else 0
            first = max(span["start"] + kept, offset)
            last = min(span["end"], stop)
            if first < last:
                chars[first - offset : last - offset] = [" "] * (last - first)
            cursor += 1
        lines.append("".join(chars))
        offset += len(line)
    return lines


def typescript_comment_columns(source: str) -> dict[int, int]:
    """Map TypeScript-family line numbers to the column a comment opens at.

    The last opening on each line, because a `//` runs to the end of its
    line and nothing opens after it: a directive is written with `//`, so
    the column that can hold one is the last. A block comment is recorded
    where it opens and on no later line, so a `//` written inside one is
    comment text rather than a comment.
    """
    columns: dict[int, int] = {}
    openers = [span for span in typescript_spans(source) if span["kind"] == "comment"]
    offset = 0
    cursor = 0
    for number, line in enumerate(source.splitlines(keepends=True), start=1):
        stop = offset + len(line)
        while cursor < len(openers) and openers[cursor]["start"] < stop:
            columns[number] = openers[cursor]["start"] - offset
            cursor += 1
        offset = stop
    return columns


def typescript_comment_lines(source: str) -> list[str]:
    """The lines of TypeScript-family text with everything but its comments blanked.

    What a person reads as a sentence in a family with no docstring: a `//` or
    `/* */` run keeps its characters, while code and literal text go, and
    every column stays where it was so a finding still names its line.
    """
    comments = [span for span in typescript_spans(source) if span["kind"] == "comment"]
    lines: list[str] = []
    offset = 0
    index = 0
    for line in source.splitlines(keepends=True):
        content = line.splitlines()[0]
        stop = offset + len(content)
        kept = [" "] * len(content)
        while index < len(comments) and comments[index]["end"] <= offset:
            index += 1
        cursor = index
        while cursor < len(comments) and comments[cursor]["start"] < stop:
            first = max(comments[cursor]["start"], offset)
            last = min(comments[cursor]["end"], stop)
            kept[first - offset : last - offset] = content[
                first - offset : last - offset
            ]
            cursor += 1
        lines.append("".join(kept))
        offset += len(line)
    return lines
