"""A changelog file, as the versions it records rather than as its text.

Two readings of one document meet here. A release is *written* from a model —
a version, a date, a summary, and the details under it — so what a bump
records is decided by fields rather than by whatever a caller managed to
spell. Every release already in the file is *carried*, as the lines it
already occupies, because a changelog holds whatever its authors wrote and a
section rewritten through a model would come back as only the parts the model
has fields for.

Section boundaries come from a markdown parser rather than from scanning for
``## ``. A changelog entry may quote a fenced block, and a line inside one is
not a heading — a scanner would split the document there and write the next
release into the middle of somebody's example.

Neither half of a heading is judged here: ``parse_semver`` decides what counts
as a version and ``date.fromisoformat`` decides what counts as a date, so the
only thing this module does to the line is find where one ends and the other
begins.

A document has at most two open sections, above every release. ``##
Unreleased`` gathers what has landed and shipped nowhere. Beneath it, while
release candidates are being cut, a section is headed by the version they are
heading for and lists each candidate cut toward it: what landed after the
last candidate is not in it, so it opens a fresh ``## Unreleased`` above, and
the next candidate — or the release — folds that in.
"""

import datetime as dt
from collections.abc import Iterator
from itertools import dropwhile
from pathlib import Path
from typing import Annotated

import typer
from markdown_it import MarkdownIt
from packaging.version import Version
from pydantic import BaseModel

from lup.workspace.history import parse_semver

parser = MarkdownIt()

PREAMBLE = """# Changelog

Agent version history. Each version tracks a behavioral change in the agent.

"""
"""What a changelog opens with when a bump is the one that creates it."""


def release_heading(version: str, date: dt.date) -> str:
    """The one line every release in a changelog is found by.

    One writer, because two of them is how a file ends up holding both
    spellings of the same thing: a release closing an open section and a bump
    writing a note are the same heading, and which one a document gets should
    not depend on which command wrote it.

    The version is bare and the date follows a dash, which is what this
    repository's changelog was already written in — a format that predates
    the module, and one a narrower reader could not read at all.
    `ReleaseHeading.read` accepts the parenthesised ``v`` spelling as well,
    so a document in it stays readable and only its new entries are written
    the one way.
    """
    return f"## {version} — {date.isoformat()}"


# lup: ignore[constant-declaration] — a word in a heading grammar this module
# writes and reads back, not a judgement another implementer would make
CANDIDATE = "candidate"
"""What a section's heading closes on where a release's closes on its date.

The last word, as a release's date is, so one reading of a heading tells the
two apart: a version followed by this word is the section release candidates
are being cut toward — open, and not yet any release.
"""


def candidate_heading(target: str) -> str:
    """The line the section a series of candidates is heading for is found by."""
    return f"## {target} — {CANDIDATE}"


# lup: ignore[constant-declaration] — the opening of a line this module writes
# and reads back, part of the same grammar as the heading above it
CANDIDATES = "Candidates:"
"""What opens the line listing every candidate a section went out as."""

# lup: ignore[constant-declaration] — the sub-heading a release writes and a
# later candidate finds again to replace, one grammar with its two readers
ASKS = "What this release asks of a caller"
"""The sub-heading a section gathers what its breaks ask of a caller under."""


class ReleaseNote(BaseModel, frozen=True):
    """One release, as the fields a bump states rather than as markdown.

    ``details`` is a list because a bump names them one at a time. A single
    string split on commas silently shreds any detail whose prose holds one
    and keeps only the last of several — a container deciding its own
    contents from their punctuation.
    """

    version: str
    date: dt.date
    summary: str
    details: list[str] = []

    def heading(self) -> str:
        """The line this release is found by, and where its date is written."""
        return release_heading(self.version, self.date)

    def render(self) -> str:
        """This release as the markdown a changelog carries it in."""
        details = "".join(f"- {detail}\n" for detail in self.details)
        return f"{self.heading()}\n\n{self.summary}\n{details}\n"


class HeadingWords(BaseModel, frozen=True):
    """A section heading's two ends: the version it opens on, the word it closes on.

    Both headings a release writes are this shape and differ only in the last
    word — a date for a release, :data:`CANDIDATE` for the section candidates
    are cut toward — so the line is split once, here, and each reading judges
    only its own word.
    """

    version: str
    last: str

    @classmethod
    def read(cls, content: str) -> "HeadingWords | None":
        """A heading's own text as its two ends, or None where it opens on no version.

        The ``v`` is optional and the last word may be parenthesised, which is
        how a hand-written heading usually spells a date.
        """
        # lup: ignore[string-split] — the heading is one line of a grammar this
        # module writes and reads; the split only finds where the version ends
        # and the rest begins, and parse_semver judges the version
        name, _, remainder = content.strip().partition(" ")
        version = name.removeprefix("v")
        words = remainder.split()
        if parse_semver(version) is None or not words:
            return None
        return cls(version=version, last=words[-1].removeprefix("(").removesuffix(")"))


class ReleaseHeading(BaseModel, frozen=True):
    """Where one release begins in a document, and what it names."""

    line: int
    version: str
    date: dt.date

    @classmethod
    def read(cls, line: int, content: str) -> "ReleaseHeading | None":
        """One heading's own text as a release, or None where it names none.

        The inverse of :meth:`ReleaseNote.heading`, and the reason a round-trip
        test can hold the two together rather than a convention doing it.

        Read more widely than it is written, because in any repository that
        keeps a changelog by hand the document is older than this module: the
        ``v`` is optional and the date may be parenthesised or introduced by a
        dash, which is how a hand-written entry usually spells it. Reading only
        what this module writes is not a stricter reading but a blinder one --
        a file whose every heading fails to parse comes back as a document with
        no releases in it, and the next note written lands beneath the whole of
        it. Whatever introduces the date, it is the last word on the line, so
        that is what is read and `date.fromisoformat` remains its only judge.
        """
        words = HeadingWords.read(content)
        if words is None:
            return None
        try:
            date = dt.date.fromisoformat(words.last)
        except ValueError:
            return None
        return cls(line=line, version=words.version, date=date)


class CandidateHeading(BaseModel, frozen=True):
    """Where the section candidates are being cut toward begins, and its version."""

    line: int
    target: str

    @classmethod
    def read(cls, line: int, content: str) -> "CandidateHeading | None":
        """One heading's own text as the candidates' section, or None where it is not.

        The inverse of :func:`candidate_heading`, read as widely as a release
        heading is: whatever introduces :data:`CANDIDATE`, it is the last word.
        """
        words = HeadingWords.read(content)
        if words is None or words.last.casefold() != CANDIDATE:
            return None
        return cls(line=line, target=words.version)


class Candidate(BaseModel, frozen=True):
    """One pre-release cut toward a version: which, and when."""

    version: str
    date: dt.date

    def spelled(self) -> str:
        """This candidate as its section's list names it."""
        return f"{self.version} ({self.date.isoformat()})"

    @classmethod
    def read(cls, spelled: str) -> "Candidate":
        """One entry of that list, refused where it is not one the list writes.

        Refused rather than skipped: the list is what says which candidates
        went out, and one dropped from it is a published version the record
        forgets.
        """
        # lup: ignore[string-split] — one entry of a list this module writes
        # and reads; `Version` and `fromisoformat` judge the two halves
        version, _, stamp = spelled.strip().partition(" ")
        try:
            Version(version)
            date = dt.date.fromisoformat(stamp.removeprefix("(").removesuffix(")"))
        except ValueError as error:
            raise ValueError(
                f"{CANDIDATES} lists {spelled.strip()!r}, which is not a "
                "candidate and the date it went out"
            ) from error
        return cls(version=version, date=date)


class ReleaseSection(BaseModel, frozen=True):
    """One release already in the file, kept as the text it occupies."""

    version: str
    date: dt.date | None
    text: str


class CandidateSection(BaseModel, frozen=True):
    """The section release candidates are being cut toward, while it is open.

    Headed by the version the series is heading for rather than by any one
    candidate, because the candidates are drafts of one release and a reader
    wants the release: what it holds, and which pre-releases it went out as
    first. The list stays when the section closes, as that history.
    """

    target: str
    candidates: list[Candidate] = []
    body: str = ""
    """Everything beneath the list of candidates, as it was written."""

    @classmethod
    def read(cls, target: str, text: str) -> "CandidateSection":
        """The section's text, heading included, as its version, list and entries.

        The list is the section's first paragraph where that paragraph opens
        with :data:`CANDIDATES`; anything else there is an entry, and the
        section lists no candidate yet.
        """
        lines = text.splitlines(keepends=True)[1:]
        tokens = parser.parse("".join(lines))
        match tokens:
            case [opening, inline, *_] if (
                opening.type == "paragraph_open"
                and opening.map is not None
                and inline.content.startswith(CANDIDATES)
            ):
                listed = inline.content.removeprefix(CANDIDATES).strip()
                return cls(
                    target=target,
                    candidates=[
                        Candidate.read(entry)
                        # lup: ignore[string-split] — the list this module
                        # writes, one candidate between each pair of commas
                        for entry in listed.removesuffix(".").split(",")
                    ],
                    body=entries_beneath(lines[opening.map[1] :]),
                )
            case _:
                return cls(target=target, body=entries_beneath(lines))

    def listed(self) -> str:
        """The paragraph naming every candidate, or nothing where none was cut."""
        if not self.candidates:
            return ""
        named = ", ".join(candidate.spelled() for candidate in self.candidates)
        return f"{CANDIDATES} {named}.\n\n"

    def render(self) -> str:
        """This section as the markdown the document carries it in, still open."""
        return f"{candidate_heading(self.target)}\n\n{self.listed()}{self.body}"

    def folded(self, entries: str, asks: list[str]) -> "CandidateSection":
        """This section with more entries in it, and its asks rendered from ``asks``.

        New entries go after the ones already here. The asks are replaced
        whole rather than added to: a release renders them from every break
        still pending, and a candidate cut before this one rendered part of
        that same list, so appending would say it twice.
        """
        block = "".join(f"- {line}\n" for line in asks)
        return self.model_copy(
            update={
                "body": stacked(
                    without_asks(self.body),
                    entries,
                    f"### {ASKS}\n\n{block}" if asks else "",
                )
            }
        )

    def closed(self, version: str, date: dt.date) -> ReleaseSection:
        """This section as the release ``version``, dated, its list kept."""
        return ReleaseSection(
            version=version,
            date=date,
            text=f"{release_heading(version, date)}\n\n{self.listed()}{self.body}",
        )


# lup: ignore[constant-declaration] — the heading Keep a Changelog names, which
# is a convention outside this repository rather than a choice made here
UNRELEASED = "Unreleased"
"""The heading a changelog gathers the next release's entries under.

Not a version, so :class:`ReleaseHeading` reads it as prose and it stays with
the preamble, which is the right answer for every reader but the ones closing
it: a release, which names the entries, and a candidate, which folds them into
the section it is cut toward.
"""


class Changelog(BaseModel, frozen=True):
    """A changelog document: what opens it, and the releases beneath."""

    preamble: str = PREAMBLE
    unreleased: str = ""
    """What stands under ``## Unreleased``, that heading included, or empty.

    Held apart from the preamble because a release does not *write* its
    section so much as close this one: the entries were accumulated as they
    landed, by whoever landed them, and a release names the version they
    turned out to be. Rendering puts it back exactly where it was, so a
    document nobody is releasing round-trips unchanged.
    """

    candidate: CandidateSection | None = None
    """The section release candidates are being cut toward, where one is open.

    Beneath ``unreleased`` and above every release. What lands after a
    candidate is not in it, so it gathers under a fresh ``## Unreleased``
    above until the next candidate or the release folds it in, and the two
    stay apart exactly as long as the work in each has shipped differently.
    """

    sections: list[ReleaseSection] = []

    @classmethod
    def parse(cls, text: str) -> "Changelog":
        """Read a document into its preamble, its open sections, and the releases.

        Each open section runs to the next heading that opens one, or to the
        first release. One standing below the first release is not open,
        whatever it says: it is part of the release above it.
        """
        lines = text.splitlines(keepends=True)
        headings = release_headings(text)
        first = headings[0].line if headings else len(lines)
        opening = unreleased_heading(text)
        cutting = candidate_section_heading(text)
        marks = sorted(
            line
            for line in (opening, cutting.line if cutting is not None else None)
            if line is not None and line < first
        )
        bounds = [*marks, first]

        def span(start: int) -> str:
            return "".join(lines[start : bounds[bounds.index(start) + 1]])

        return cls(
            preamble="".join(lines[: bounds[0]]),
            unreleased=(
                span(opening) if opening is not None and opening in marks else ""
            ),
            candidate=(
                CandidateSection.read(cutting.target, span(cutting.line))
                if cutting is not None and cutting.line in marks
                else None
            ),
            sections=list(sectioned(lines, headings)),
        )

    def released_as(self, version: str, date: dt.date, asks: list[str]) -> "Changelog":
        """This document with its open work closed as ``version``.

        The entries stay as their authors wrote them and the heading above
        them is replaced, because that is what a release is: the same list,
        named. Where candidates were cut toward it, their section is what
        closes, with everything under ``## Unreleased`` folded in — the
        release is cut from here, not from the last candidate. ``asks`` is
        what only the release knows: what the breaks it carries ask of a
        caller.

        A document with nothing open gets an empty section rather than a
        refusal, so a release that happens to carry no entries still records
        that it happened, on the date it happened.
        """
        closing = self.candidate or CandidateSection(target=version)
        closed = closing.folded(without_heading(self.unreleased), asks)
        return self.model_copy(
            update={
                "unreleased": "",
                "candidate": None,
                "sections": [closed.closed(version, date), *self.sections],
            }
        )

    def with_candidate(
        self, target: str, candidate: Candidate, asks: list[str]
    ) -> "Changelog":
        """This document with ``candidate`` cut toward ``target``, its section left open.

        What stands under ``## Unreleased`` is folded in, because the
        candidate is cut from here and carries it. A section whose series was
        re-levelled is retitled rather than replaced, and keeps every
        candidate it lists: each of them was published.
        """
        opened = self.candidate or CandidateSection(target=target)
        folded = opened.folded(without_heading(self.unreleased), asks)
        return self.model_copy(
            update={
                "unreleased": "",
                "candidate": folded.model_copy(
                    update={
                        "target": target,
                        "candidates": [*opened.candidates, candidate],
                    }
                ),
            }
        )

    def promoted(self, version: str, date: dt.date) -> "Changelog":
        """This document with the candidates' section closed as ``version``, as it stood.

        Nothing under ``## Unreleased`` is folded in: a promotion releases the
        candidate's own commit, and what landed after it is not in that.
        """
        closing = self.candidate or CandidateSection(target=version)
        return self.model_copy(
            update={
                "candidate": None,
                "sections": [closing.closed(version, date), *self.sections],
            }
        )

    @classmethod
    def read(cls, path: Path) -> "Changelog":
        """The document at ``path``, or an empty one where none exists yet."""
        return cls.parse(path.read_text()) if path.exists() else cls()

    def dates(self) -> dict[str, dt.date]:
        """When each release this document records was written."""
        return {
            section.version: section.date
            for section in self.sections
            if section.date is not None
        }

    def with_note(self, note: ReleaseNote) -> "Changelog":
        """This document with ``note`` written in, replacing any it supersedes.

        A version already present is rewritten in place rather than added
        again, so a bump repeated after an amended summary leaves one section
        rather than two claiming the same version.
        """
        written = ReleaseSection(
            version=note.version, date=note.date, text=note.render()
        )
        if any(section.version == note.version for section in self.sections):
            return self.model_copy(
                update={
                    "sections": [
                        written if section.version == note.version else section
                        for section in self.sections
                    ]
                }
            )
        return self.model_copy(update={"sections": [written, *self.sections]})

    def render(self) -> str:
        """The whole document, ready to write back.

        The open sections sit where they were read, above every release and
        below the preamble, so a document nobody is releasing comes back
        unchanged.
        """
        return (
            self.preamble
            + self.unreleased
            + (self.candidate.render() if self.candidate is not None else "")
            + "".join(section.text for section in self.sections)
        )


class DocumentHeading(BaseModel, frozen=True):
    """One second-level heading: where it sits, and what it says.

    Both readings below want the same two facts and judge them differently —
    one asks whether the text names a version, the other whether it is the
    open section — so the pair is read once and named rather than returned
    positionally to two callers who would each have to remember the order.
    """

    line: int
    content: str


def second_level_headings(text: str) -> Iterator[DocumentHeading]:
    """Each ``##`` heading's line and its own text, in document order.

    Through the parser rather than by scanning for ``## ``, for the reason
    this module opens with: an entry may quote a fenced block containing one,
    and a scanner would split the document inside somebody's example.
    """
    tokens = parser.parse(text)
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.tag == "h2" and token.map is not None:
            yield DocumentHeading(line=token.map[0], content=tokens[index + 1].content)


def unreleased_heading(text: str) -> int | None:
    """Where the open section starts, or ``None`` where nothing is open.

    Matched without regard to case, because the heading is written by hand by
    whoever adds the first entry after a release, and ``## unreleased`` is the
    same intention.
    """
    return next(
        (
            heading.line
            for heading in second_level_headings(text)
            if heading.content.strip().casefold() == UNRELEASED.casefold()
        ),
        None,
    )


def candidate_section_heading(text: str) -> CandidateHeading | None:
    """Where the section candidates are being cut toward starts, or ``None``."""
    return next(
        (
            found
            for heading in second_level_headings(text)
            if (found := CandidateHeading.read(heading.line, heading.content))
            is not None
        ),
        None,
    )


def blank(line: str) -> bool:
    """Whether a line holds nothing a reader would see."""
    return not line.strip()


def entries_beneath(lines: list[str]) -> str:
    """Those lines as a section's entries, the blank lines above the first dropped.

    The blank line that separated them from whatever stood above goes with
    that, so whatever is written above them next supplies its own and the
    spacing does not depend on how somebody happened to type it.
    """
    entries = "".join(dropwhile(blank, lines))
    return entries if not entries or entries.endswith("\n") else f"{entries}\n"


def without_heading(block: str) -> str:
    """A section's entries, with the heading line above them dropped.

    What a release keeps when it renames the section: the entries are their
    authors', and the heading is the release's to replace.
    """
    return entries_beneath(block.splitlines(keepends=True)[1:])


def without_asks(body: str) -> str:
    """A section's entries without the asks a release rendered into it.

    The block runs from its sub-heading to the next heading at its level or
    above, found through the parser like every other boundary here, so an
    entry written after it survives and a fenced example inside one is not
    mistaken for either end.
    """
    lines = body.splitlines(keepends=True)
    tokens = parser.parse(body)
    starts = [
        DocumentHeading(line=token.map[0], content=tokens[index + 1].content.strip())
        for index, token in enumerate(tokens)
        if token.type == "heading_open"
        and token.tag in ("h1", "h2", "h3")
        and token.map is not None
    ]
    begins = next((start.line for start in starts if start.content == ASKS), None)
    if begins is None:
        return body
    ends = next((start.line for start in starts if start.line > begins), len(lines))
    return "".join([*lines[:begins], *lines[ends:]])


def stacked(*blocks: str) -> str:
    """Blocks of markdown one after another, each followed by one blank line.

    What keeps the end of one section off the heading of the next: a list
    that ended the text it was written into ran straight into whatever
    heading was rendered after it.
    """
    trimmed = [
        list(reversed(list(dropwhile(blank, reversed(list(dropwhile(blank, lines)))))))
        for lines in (block.splitlines() for block in blocks)
    ]
    return "".join("\n".join(lines) + "\n\n" for lines in trimmed if lines)


def sectioned(
    lines: list[str], headings: list[ReleaseHeading]
) -> Iterator[ReleaseSection]:
    """Each release heading paired with the lines beneath it, up to the next."""
    bounds = [*(heading.line for heading in headings[1:]), len(lines)]
    for heading, end in zip(headings, bounds):
        yield ReleaseSection(
            version=heading.version,
            date=heading.date,
            text="".join(lines[heading.line : end]),
        )


def release_headings(text: str) -> list[ReleaseHeading]:
    """Every release heading in document order.

    A second-level heading naming no parseable version stays with whatever it
    already belonged to, which is how a document's own prose headings survive
    a bump instead of being read as releases with peculiar names.
    """
    tokens = parser.parse(text)
    found = [
        ReleaseHeading.read(token.map[0], tokens[index + 1].content)
        for index, token in enumerate(tokens)
        if token.type == "heading_open" and token.tag == "h2" and token.map is not None
    ]
    return [heading for heading in found if heading is not None]


class Block(BaseModel, frozen=True):
    """One unit a changelog is merged in: the heading it is found by, and its text from that heading to the next.

    An entry of the open section is found by its ``###`` heading's own text,
    a release by its version.
    """

    heading: str
    text: str


class OpenSection(BaseModel, frozen=True):
    """What stands under ``## Unreleased``, read as the entries landed into it.

    Each entry is the block from its ``###`` heading to the next, found
    through the parser for the reason this module opens with. ``intro`` is
    whatever stands between the section's own heading and its first entry.
    """

    heading: str = f"## {UNRELEASED}"
    intro: str = ""
    entries: list[Block] = []

    @classmethod
    def read(cls, text: str) -> "OpenSection | None":
        """An open section's text, its heading included, as its entries; None where there is none."""
        if not text:
            return None
        lines = text.splitlines(keepends=True)
        body = lines[1:]
        tokens = parser.parse("".join(body))
        starts = [
            DocumentHeading(
                line=token.map[0], content=tokens[index + 1].content.strip()
            )
            for index, token in enumerate(tokens)
            if token.type == "heading_open"
            and token.tag == "h3"
            and token.map is not None
        ]
        bounds = [*(start.line for start in starts[1:]), len(body)]
        return cls(
            heading=text.splitlines()[0],
            intro="".join(body[: starts[0].line if starts else len(body)]),
            entries=[
                Block(heading=start.content, text="".join(body[start.line : end]))
                for start, end in zip(starts, bounds)
            ],
        )


class MergedBlock(BaseModel, frozen=True):
    """One block of a merged changelog, and whether its two sides still disagree."""

    text: str
    conflicted: bool = False


class ChangelogMerge(BaseModel, frozen=True):
    """A changelog merged from both sides, and whether any block of it is a conflict left to resolve."""

    text: str
    conflicted: bool


def merged_block(
    base: str | None, ours: str | None, theirs: str | None
) -> MergedBlock | None:
    """One block, three-way: the side that changed it, or a conflict where both did differently.

    ``None`` on a side is a block it does not have, so a block one side added
    and one side removed is settled the same way as a changed one. ``None``
    comes back where the merge keeps no block at all.
    """
    if ours == theirs or theirs == base:
        return MergedBlock(text=ours) if ours is not None else None
    if ours == base:
        return MergedBlock(text=theirs) if theirs is not None else None
    return MergedBlock(
        text="<<<<<<< ours\n"
        + stacked(ours or "").removesuffix("\n")
        + "=======\n"
        + stacked(theirs or "").removesuffix("\n")
        + ">>>>>>> theirs\n\n",
        conflicted=True,
    )


class BlockKey(BaseModel, frozen=True):
    """Which block a side's block is: its heading, and which occurrence of that heading on its side.

    The occurrence keeps two blocks one side gave the same heading apart, so
    neither is folded into the other.
    """

    heading: str
    occurrence: int


def keyed(blocks: list[Block]) -> dict[BlockKey, str]:
    """Each block's text under its key, in document order."""
    return {
        BlockKey(
            heading=block.heading,
            occurrence=sum(
                1 for earlier in blocks[:index] if earlier.heading == block.heading
            ),
        ): block.text
        for index, block in enumerate(blocks)
    }


def merged_blocks(
    base: dict[BlockKey, str], ours: dict[BlockKey, str], theirs: dict[BlockKey, str]
) -> list[MergedBlock]:
    """Every block of either side, each merged three-way, in our order with theirs' new ones placed.

    A block only theirs has goes right after the nearest block before it on
    their side that ours has too, or first where there is none: an entry
    added at the top of a section on their side stays at the top, above the
    ones added on ours.
    """
    listed = list(theirs)
    anchors = {
        key: next((kept for kept in reversed(listed[:index]) if kept in ours), None)
        for index, key in enumerate(listed)
        if key not in ours
    }
    order = [
        *(key for key, anchor in anchors.items() if anchor is None),
        *(
            placed
            for mine in ours
            for placed in (
                mine,
                *(key for key, anchor in anchors.items() if anchor == mine),
            )
        ),
    ]
    merged = [
        merged_block(
            base[key] if key in base else None,
            ours[key] if key in ours else None,
            theirs[key] if key in theirs else None,
        )
        for key in order
    ]
    return [block for block in merged if block is not None]


def merged_changelog(base: str, ours: str, theirs: str) -> ChangelogMerge:
    """Two sides' changelogs as one, each entry and release merged as the unit it is.

    What a line-based merge cannot do: two branches that each add an entry at
    the top of ``## Unreleased`` meet on the same lines, and git's union
    merge keeps both sides by interleaving them -- a line both entries share
    is kept once, under the second, and the first entry's last line runs
    into the second's heading. Here each entry is a block under its own
    heading, merged three-way against the base: what one side added, changed
    or removed is taken from that side, and only a block both changed
    differently is a conflict, marked where it stands. Every block -- each
    entry, the open section, each release -- is followed by one blank line
    (:func:`stacked`), so spacing never depends on how a merge lined the
    text up.
    """
    was, mine, other = (Changelog.parse(text) for text in (base, ours, theirs))
    was_open, mine_open, other_open = (
        OpenSection.read(document.unreleased) for document in (was, mine, other)
    )

    def entries_of(section: OpenSection | None) -> dict[BlockKey, str]:
        return keyed(section.entries if section is not None else [])

    def releases_of(document: Changelog) -> dict[BlockKey, str]:
        return keyed(
            [
                Block(heading=section.version, text=section.text)
                for section in document.sections
            ]
        )

    def candidate_of(document: Changelog) -> str | None:
        return document.candidate.render() if document.candidate is not None else None

    def intro_of(section: OpenSection | None) -> str | None:
        return section.intro if section is not None else None

    def opened() -> MergedBlock | None:
        """The open section merged, or None where the merge keeps none.

        A side that closed it into a release has none; it stays closed unless
        the other side added an entry since.
        """
        heading = next(
            (
                section.heading
                for section in (mine_open, other_open)
                if section is not None
            ),
            None,
        )
        entries = merged_blocks(
            entries_of(was_open), entries_of(mine_open), entries_of(other_open)
        )
        intro = merged_block(
            intro_of(was_open), intro_of(mine_open), intro_of(other_open)
        )
        said = [*([intro] if intro is not None else []), *entries]
        if heading is None or not (
            entries or (mine_open is not None and other_open is not None)
        ):
            return None
        return MergedBlock(
            text=f"{heading}\n\n" + stacked(*(block.text for block in said)),
            conflicted=any(block.conflicted for block in said),
        )

    blocks = [
        merged_block(was.preamble, mine.preamble, other.preamble),
        opened(),
        merged_block(candidate_of(was), candidate_of(mine), candidate_of(other)),
        *merged_blocks(releases_of(was), releases_of(mine), releases_of(other)),
    ]
    kept = [block for block in blocks if block is not None]
    return ChangelogMerge(
        text=stacked(*(block.text for block in kept)).removesuffix("\n"),
        conflicted=any(block.conflicted for block in kept),
    )


def merge_changelog_cmd(
    base: Annotated[
        Path, typer.Argument(help="The common ancestor's changelog (git's %O)")
    ],
    ours: Annotated[
        Path,
        typer.Argument(
            help="This side's changelog, which the merge is written over (git's %A)"
        ),
    ],
    theirs: Annotated[
        Path, typer.Argument(help="The other side's changelog (git's %B)")
    ],
) -> None:
    """Merge a changelog entry by entry: git's `lup-changelog` merge driver.

    Writes the merge over OURS, and ends 1 where an entry or release both
    sides changed differently is left marked, which git reports as a
    conflict in the changelog.
    """
    merged = merged_changelog(
        base.read_text(encoding="utf-8"),
        ours.read_text(encoding="utf-8"),
        theirs.read_text(encoding="utf-8"),
    )
    ours.write_text(merged.text, encoding="utf-8", newline="")
    if merged.conflicted:
        raise typer.Exit(1)
