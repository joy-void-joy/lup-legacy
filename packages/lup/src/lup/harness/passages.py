"""Prose a content module authors as Markdown, in the file beside the module.

Not an ``r\"\"\"…\"\"\"`` in Python with every path, count and description spliced
in by f-string. Two things are wrong with that, and only one of them is about
readability.

The first is that an f-string has no choke point. A value entering prose that
way arrives however it was spelled, and a backtick closes the code span it
landed in, a newline ends the heading, a pipe breaks the table row — silently,
in a file nobody reads until a model does. Here every value is a node or a
part, rendered on the way in, so entering escaped is the only way to enter.

The second is that prose belongs in a file that is prose. Markdown beside the
module is edited as Markdown, diffed as Markdown, and carries no Python
escaping around what a reader is meant to see.

One file holds all of a module's prose, because a subject a reader has to open
in eleven places is one they read in none. A module composing several passages
marks them off inside that file with :data:`SECTION_OPEN`, and its declaration
names which one it is placing — so the division is a table of contents rather
than a scattering, and which passage goes where stays in Python where it is
typed.

What a passage is *not* is a program. ``{%`` and ``{#`` are refused by
:func:`prose_without_logic`, so a template can name a value and nothing else:
prose that varies by more than a value is two sections, or a declaration in
Python saying which one is read — where it is typed and reviewed.
"""

import importlib.util
from functools import cache
from pathlib import Path, PurePath

from jinja2 import Environment, StrictUndefined, meta
from markdown_it import MarkdownIt
from pydantic import BaseModel

from lup.types import StringMap

# lup: ignore[constant-declaration] — the pairing this repository defines
# between a declaration and the prose beside it
PASSAGE_SUFFIX = ".passage.md"
"""How a passage file is named: its module's own stem, then this.

Markdown's extension last, so an editor highlights it as the Markdown it is
and the sweeps that read prose by suffix — the written-command check, the
review-marker scan — keep reaching it now that the words live here rather
than in the module. `passage` before it, because the file is a template with
values still to place and not the finished page.
"""

# lup: ignore[constant-declaration] — the marker syntax this module defines,
# which a passage file spells and this module reads back
SECTION_OPEN = "<!-- passage:"
"""How a section inside a passage file announces which passage it is.

An HTML comment, so the file stays Markdown a reader can render: the marker is
invisible wherever the prose is previewed, and the division takes it away
before a section is ever placed. Read off the Markdown parser's own token
rather than matched against the raw text, so a marker written inside a fenced
block is the code sample it looks like and divides nothing.
"""

# lup: ignore[constant-declaration] — the other half of that marker syntax
SECTION_CLOSE = "-->"
"""Where a marker ends, so a comment that is not one divides nothing."""


class PassageSection(BaseModel, frozen=True):
    """One passage inside its module's file, and the words it holds."""

    name: str
    text: str


class PassageMark(BaseModel, frozen=True):
    """Where one marker sits, in the two lines a division is measured by."""

    name: str
    marker: int
    """The line the marker itself is on, which ends the section above it."""

    body: int
    """The line after it, where the words this marker names begin."""


def prose_beside(module_file: str) -> str:
    """What the prose for one module file is called, in the same directory.

    The naming rule on its own, one file name to another. :func:`passage_path`
    resolves a module to its file and asks this; the scaffold's coherence
    check walks another repository's extracted tree and asks the same question
    of a path it has no way to import. One definition, so the two cannot come
    to disagree about which files are the halves of one declaration.
    """
    return f"{PurePath(module_file).stem}{PASSAGE_SUFFIX}"


def passage_path(module: str) -> Path:
    """Where this module's prose is authored.

    Beside the module, under its own stem, so the two halves of a declaration
    are one file apart and neither is found without the other.
    """
    spec = importlib.util.find_spec(module)
    if spec is None or spec.origin is None:
        raise ValueError(f"no module {module} to read a passage beside")
    beside = Path(spec.origin)
    return beside.parent / prose_beside(beside.name)


def section_named(content: str) -> str:
    """The passage this comment announces, or empty where it announces none.

    An ordinary comment in the prose is not a marker, so a file may carry both
    and only the ones spelling a passage divide it.
    """
    stripped = content.strip()
    if not stripped.startswith(SECTION_OPEN) or not stripped.endswith(SECTION_CLOSE):
        return ""
    return stripped.removeprefix(SECTION_OPEN).removesuffix(SECTION_CLOSE).strip()


def prose_without_logic(path: Path, text: str) -> str:
    """This prose, refused where it holds a statement or a comment tag.

    Either would make the passage a program: what the document says would
    depend on state no type checker reads and no reviewer sees rendered.
    Refused where the file is read rather than by a scan somewhere else,
    because this is the one place every passage passes through.
    """
    for tag in ("{%", "{#"):
        if tag in text:
            raise ValueError(
                f"{path} holds `{tag}`: a passage names values and nothing else. "
                "Prose that varies by more than a value is two sections, or a "
                "declaration in Python saying which one is read"
            )
    return text


def section_body(lines: list[str], start: int, stop: int) -> str:
    """One section's words, taken back out of the file that carries them.

    A marker needs a line of its own, so writing one costs the section above
    it a newline it did not necessarily have — nine passages here end mid
    sentence on purpose, joined to the part that follows them. Exactly one is
    added when a section is written and exactly one is taken away when it is
    read, which recovers both the passage that ended on a blank line and the
    passage that ended on a word. The last section is written with nothing
    after it and so has nothing to give back.
    """
    body = "".join(lines[start:stop])
    return body.removesuffix("\n") if stop < len(lines) else body


def passage_sections(path: Path, text: str) -> list[PassageSection]:
    """Every passage this file holds, in the order it holds them.

    Whatever stands before the first marker is the module's own passage, the
    one its declaration places by naming nothing — so a module composing a
    single document writes a file with no marker in it at all, and a module
    composing several writes that same opening and marks off what follows.

    Divided by the markers and by nothing else: the line ranges come from the
    Markdown parser's own tokens, so a marker written inside a fenced block is
    the code sample it looks like. What a section holds is every line between
    its marker and the next, unaltered, because a file that is read back
    differently from how it was written is one the drift check would chase
    forever.
    """
    lines = text.splitlines(keepends=True)
    fenced = {
        number
        for token in MarkdownIt().parse(text)
        if token.type in ("fence", "code_block") and token.map
        for number in range(*token.map)
    }
    marks = [
        PassageMark(name=name, marker=number, body=number + 1)
        for number, line in enumerate(lines)
        if number not in fenced
        for name in [section_named(line)]
        if name
    ]
    if not marks:
        return [PassageSection(name="", text=text)]
    spans = [
        ("", 0, marks[0].marker),
        *[
            (mark.name, mark.body, stop)
            for mark, stop in zip(
                marks, [*(after.marker for after in marks[1:]), len(lines)], strict=True
            )
        ],
    ]
    held = [
        PassageSection(name=name, text=section_body(lines, start, stop))
        for name, start, stop in spans
        if name or start < stop
    ]
    for section in held:
        if sum(other.name == section.name for other in held) > 1:
            raise ValueError(
                f"{path} marks `{section.name}` twice; a passage is named once"
            )
    return held


@cache
def passage_text(module: str, name: str = "") -> str:
    """The Markdown authored for one declaration, read once and held."""
    path = passage_path(module)
    held = passage_sections(
        path, prose_without_logic(path, path.read_text(encoding="utf-8"))
    )
    found = next((section for section in held if section.name == name), None)
    if found is not None:
        return found.text
    marked = ", ".join(f"`{section.name}`" for section in held if section.name)
    raise ValueError(
        f"{path} holds no `{name}` passage. "
        + (
            f"It marks {marked}."
            if marked
            else "It marks none, so it is the one unnamed passage of its module."
        )
    )


def rendered(module: str, name: str, values: StringMap) -> str:
    """One passage with its values placed, each already spelled by its own kind.

    The values arrive rendered: a node has escaped itself and a part has been
    spelled in the vocabulary of the runtime reading it, so what happens here
    is placement and nothing else.

    A value handed over and never placed is refused, the mirror of the name
    placed and never handed over that ``StrictUndefined`` refuses. It is not
    tidiness: a value is a part, and whatever a part declares — an invocation
    of another module's skill, a command the skill must be granted — counts
    as said by the declaration holding it, whether or not a reader ever sees
    it. A pointer nobody renders still ties one module to another.
    """
    text = passage_text(module, name)
    placed = meta.find_undeclared_variables(environment().parse(text))
    unplaced = [value for value in sorted(values) if value not in placed]
    if unplaced:
        passage = f"the {name!r} passage" if name else "the passage"
        raise ValueError(
            f"{passage_path(module)} is handed {unplaced} for {passage}, which "
            "places none of them: place each, or stop handing it over"
        )
    return environment().from_string(text).render(values)


@cache
def environment() -> Environment:
    """The one environment every passage renders through.

    ``StrictUndefined`` is what makes a misspelled name a failure rather than
    a blank: generation runs inside `dev check`, so the name that never
    reached the context is reported where it was written instead of leaving a
    hole in a shipped prompt. Autoescaping is off because escaping here is a
    Markdown question, answered by the node that carries the value rather
    than by an HTML rule that would corrupt the prose around it.
    """
    return Environment(undefined=StrictUndefined, keep_trailing_newline=True)
