# lup: ignore[import-re, re-call, empty-collection]
"""Inline marker scanning for the repo's one marker family, `# lup:`.

- Review notes (`# lup:` / `// lup:`): actionable feedback left in the code;
  the `ignore` keyword — inline or standalone file-level — is the
  anti-pattern escape hatch, never a note and never a reason to hide one. A
  `defer:` head marks the note as parked work rather than open feedback; the
  scanner classifies it and parses out the gate a bracketed one states. The
  `lup-devtools dev comments` scanner uses this to list unresolved feedback;
  the edit-permission hook makes the same note/suppression split, prompting
  whenever an edit changes the note count or adds a suppression.
  A `template:` head marks a customization point instead — a decision this
  scaffold leaves to the domain adopting it, gathered by `lup-devtools dev
  todos` so `/lup:init` walks every one. It is the note flavor whose lifetime
  runs the other way: upstream it is the template's product and belongs in no
  defect count, while downstream it is outstanding work until the domain
  writes its own code where the placeholder stood and drops the marker.

One scan serves every flavor (:func:`find_markers`, parameterized over the
marker regex); :func:`find_feedback` binds it to the review-note rules. The
tokenization, docstring detection, ignore matching, and line cursor the scan
stands on live in :mod:`lup.harness.codescan.common`, shared with the anti-pattern
auditor.

Detection is deliberately liberal — `#` or `//`, any case, optional spaces — so
the same note reads naturally in Python, shell, TypeScript, JSON, or Markdown.
A colon is required so prose like a `## Notes` heading does not match, and the
marker has to sit where its comment opens, so a note may spell out the syntax
it is about without splitting itself in two. A marker is a feedback note unless
its keyword is `ignore`, which stays the anti-pattern escape hatch.

How a file is scanned depends on its language, because where a note can live
does. Python source is parsed so a marker counts only where prose belongs — in a
comment or a docstring. A ``# lup:`` inside an ordinary string literal (such
as a tool's own "no notes" message) is code, not a note, and must not be
reported. The same rule decides the other modes by which introducer can open a
comment at all: in JS only `//` can, and in JSON neither can, so a marker there
is always inside a literal — a note quoted in a recorded trace or a config
string, never feedback. Remaining text has no parser to lean on, so it is
line-scanned; Markdown additionally skips fenced and inline code so notes
quoted in documentation examples are not flagged.
"""

import re
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, model_validator

from lup.harness.codescan.common import LineCursor, PythonContext
from lup.policy.kernel.edit import IGNORE_RE

MARKER_RE = re.compile(r"(#|//)\s*lup\s*:", re.IGNORECASE)


# `# lup: defer: <text>` parks work; a `defer[<gate>]: <text>` head parks it
# behind a gate somebody other than this note can check ("until the v2 API
# ships"). A condition spelled the way `lup.devtools.dev.gates` declares is
# resolved on every `dev check` and fails it the run it comes true; anything
# else is prose, carried to a reader and gating nothing. Both are real ways to
# park work, and the bare spelling stays the default. What the bracket must
# never hold either way is a restatement that this code might change again,
# which is invention dressed as a trigger. The optional bracket deliberately
# mirrors the typed `# lup: ignore[rule-id]` escape hatch — which means a
# condition may itself contain brackets (`defer[when ignore[dict-get] sites
# migrate]: ...`), so the head ends at the first `]` that is followed by a
# colon, and the colon is required either way. A head that opens a bracket it
# never closes with `]:` is malformed and the note stays an ordinary (red,
# visible) review note. The head is matched against a note's text (the part
# after the marker), so the `ignore` keyword — which never reaches note
# classification — is untouched, and a condition may run past the line it
# starts on: continuation lines join with a space before this sees them, the
# same way a note's own message runs on. A gate that had to fit one line
# would be written shorter than it needed to be, which is how a real
# externally-checkable condition decays into restating that this code might
# change again.
DEFER_HEAD_RE = re.compile(
    r"^defer\s*(?:\[(?P<condition>.+?)\])?\s*:\s*", re.IGNORECASE
)
# A resolution claim: `# lup: solved: <the note's original text>`. An agent
# that has addressed a note converts it rather than deleting it, so the claim
# is an artifact in the tree instead of an absence nobody can review. No
# bracket, because the head carries no parameter — what it needs to say is
# said by keeping the note's own words after it.
SOLVED_HEAD_RE = re.compile(r"^solved\s*:\s*", re.IGNORECASE)
# `# lup: template: <decision>` marks a customization point — a choice this
# scaffold deliberately leaves to whoever adopts it, gathered by `dev todos`
# so `/lup:init` walks every one. No bracket, because the head carries no
# parameter. Upstream these are the template's product rather than a defect
# list, which is why they count in their own section instead of joining open
# feedback. Like `ignore` and unlike feedback, a customization marker is
# removed outright rather than converted to a claim: it is answered by writing
# the domain's own code where the placeholder stood, which leaves no original
# ask for a claim to be checked against.
TEMPLATE_HEAD_RE = re.compile(r"^template\s*:\s*", re.IGNORECASE)
FENCE_RE = re.compile(r"^\s*(```|~~~)")
COMMENT_PREFIX_RE = re.compile(r"^\s*(#|//)")
# The whole run a line opens its comment with, so a marker written `## lup:`
# or `/// lup:` is read as sitting on that opener rather than inside its prose.
COMMENT_OPENER_RE = re.compile(r"^\s*(#+|//+)")

CONTEXT_BEFORE = 2
CONTEXT_AFTER = 25


class ScanMode:
    """How a file's text is searched for markers, chosen by its language.

    Python source is parsed, so a marker counts only in a comment or a
    docstring. Markdown is line-scanned with code skipped. In the JS family
    `#` opens no comment (`//` does), so a `# lup:` there is always string
    content — a Python marker quoted inside a template — and only `//`
    markers count. JSON has no comment syntax at all, so every marker in one
    is string content: a note quoted inside a recorded trace, a tool message,
    a config value. Everything else is plain line-scanned.
    """

    PYTHON = "python"
    MARKDOWN = "markdown"
    JS = "js"
    JSON = "json"
    TEXT = "text"


# lup: ignore[library-default] — each suffix is its language's own, a fact of
# the format rather than a choice of this library's
SCAN_MODES = {
    ".py": ScanMode.PYTHON,
    ".pyi": ScanMode.PYTHON,
    ".md": ScanMode.MARKDOWN,
    ".markdown": ScanMode.MARKDOWN,
    ".js": ScanMode.JS,
    ".jsx": ScanMode.JS,
    ".ts": ScanMode.JS,
    ".tsx": ScanMode.JS,
    ".mjs": ScanMode.JS,
    ".cjs": ScanMode.JS,
    ".json": ScanMode.JSON,
    ".jsonl": ScanMode.JSON,
}
"""The scan mode each suffix routes to; a suffix absent here is plain text."""


def scan_mode_for(path: Path) -> str:
    """Pick the scan mode for a path from its suffix, the one routing every tracked file takes."""
    return SCAN_MODES.get(path.suffix.lower(), ScanMode.TEXT)


class NoteKind(StrEnum):
    """The closed vocabulary of review-note flavors.

    An ordinary actionable note, parked work — behind a stated gate or simply
    parked — a claim that a note has been addressed and is waiting to be
    checked, or a customization point the scaffold leaves to the domain
    adopting it. A `StrEnum` rather than a `Literal` so the same declaration
    is both the type every consumer dispatches on and the choice list
    `dev comments --kind` offers, which is one fewer place for the vocabulary
    to drift out of agreement with itself.
    """

    note = "note"
    defer = "defer"
    solved = "solved"
    template = "template"


class MarkerComment(BaseModel):
    """One actionable note: the source span plus a window worth reading.

    ``kind`` classifies the note; a ``defer`` note that stated a gate carries
    it as ``condition``, parsed out of the `defer[...]` head, and ``text``
    holds only the message that follows the head. A defer that stated none —
    the default spelling — parks with ``condition`` absent, read against the
    tree by whoever triages it. An ordinary note has no condition, and a
    ``solved`` note carries the original note's words unchanged — that is what
    makes the claim checkable against what was actually asked.
    """

    start_line: int
    end_line: int
    read_start: int
    read_end: int
    text: str
    kind: NoteKind = NoteKind.note
    condition: str | None = None

    @model_validator(mode="after")
    def coherent_kind(self) -> Self:
        match (self.kind, self.condition):
            case ("note", str()) | ("solved", str()) | ("template", str()):
                raise ValueError("only a defer note carries a wake condition")
            case _:
                return self

    def classify_deferral(self) -> Self:
        """Split a `defer:`, `solved:`, or `template:` head off this note.

        A matching note comes back with the head's kind, the gate a bracketed
        deferral stated parsed out, and ``text`` reduced to the message after
        the head. Any other note — including prose that merely starts with the
        word "defer", a bracket left empty, or a bracket that never closes with
        `]:` — is returned unchanged as an ordinary ``note``, so a malformed
        head degrades to visible open feedback instead of a silently mangled
        condition or a claim nobody made. A bracket opened is a bracket that
        has to say something; writing none at all is the ordinary way to park
        work.
        """
        for kind, head_re in (
            (NoteKind.solved, SOLVED_HEAD_RE),
            (NoteKind.template, TEMPLATE_HEAD_RE),
        ):
            plain = head_re.match(self.text)
            if plain is not None:
                return self.model_copy(
                    update={"kind": kind, "text": self.text[plain.end() :]}
                )
        head = DEFER_HEAD_RE.match(self.text)
        if head is None:
            return self
        match head.group("condition"):
            case None:
                condition = None
            case stated:
                if not stated.strip():
                    return self
                condition = stated.strip()
        return self.model_copy(
            update={
                "kind": NoteKind.defer,
                "condition": condition,
                "text": self.text[head.end() :],
            }
        )

    def deferral_label(self) -> str:
        """How a parked note is labelled in a listing, gate included if stated."""
        return f"deferred[{self.condition}]" if self.condition else "deferred"

    def marker_text(self) -> str:
        """The note body as written after its marker, any head included."""
        match self.kind:
            case NoteKind.defer if self.condition:
                return f"defer[{self.condition}]: {self.text}"
            case NoteKind.defer:
                return f"defer: {self.text}"
            case NoteKind.solved:
                return f"solved: {self.text}"
            case NoteKind.template:
                return f"template: {self.text}"
            case NoteKind.note:
                return self.text


def inside_inline_code(line: str, pos: int) -> bool:
    """Whether character `pos` falls inside a backtick code span (a doc example).

    Odd single-backtick parity catches a marker mid-span; a backtick run
    directly before the marker catches rst-style double-backtick quoting,
    whose even-length run defeats the parity check.
    """
    prefix = line[:pos]
    return prefix.count("`") % 2 == 1 or prefix.endswith("`")


class MarkerScan:
    """One left-to-right pass over a file's lines, yielding each marker note.

    A note is a marker line plus the contiguous same-style comment lines below
    it, merged into one item; the run ends at a decoration line (no letters or
    digits, e.g. the edge of a `# ====` banner), a foreign comment style, a
    marker opening its own comment, or prose outside a comment. A marker
    quoted inside a comment's prose ends no run, so a note is free to write
    out the spelling it is about. Lines whose marker also matches
    `ignore`, fenced code, and backtick spans are skipped. In Python mode a
    marker counts only inside a comment or docstring, so marker text in an
    ordinary string literal is left alone.
    """

    def __init__(
        self,
        text: str,
        mode: str,
        *,
        marker: re.Pattern[str],
        ignore: re.Pattern[str] | None,
    ) -> None:
        self.mode = mode
        self.marker = marker
        self.ignore = ignore
        self.is_markdown = mode == ScanMode.MARKDOWN
        match mode:
            case ScanMode.PYTHON:
                self.context = PythonContext.parse(text)
            case ScanMode.JS:
                self.context = PythonContext.parse_typescript(text)
            case _:
                self.context = None
        self.lines = text.splitlines()
        self.total = len(self.lines)
        self.cursor = LineCursor(self.lines)
        self.in_fence = False

    def in_note_context(self, line_no: int, col: int) -> bool:
        return self.context is None or self.context.is_note_context(line_no, col)

    def on_comment_opener(self, line: str, match: re.Match[str]) -> bool:
        """Whether the marker sits on the run of `#` or `/` that opens the line.

        Exact wherever a line opens with a comment at all, which is every
        continuation line and most notes. A marker trailing anything else is
        taken at its word: nothing here can tell a comment's `//` from the one
        in `https://`, and reading a real note as a mention loses it silently.
        """
        opener = COMMENT_OPENER_RE.match(line)
        return opener is None or match.start() < opener.end()

    def at_comment_start(self, line_no: int, line: str, match: re.Match[str]) -> bool:
        """Whether the marker at `match` is where its own comment begins.

        A marker written into a comment's prose — a note quoting the `ignore`
        hatch, or writing about this scanner at all — mentions the spelling
        rather than using it, and opens nothing. Python's tokenizer says
        exactly where a comment opens, and says yes everywhere in a file it
        could not parse, keeping :class:`PythonContext`'s promise that a note
        is never missed. Without one, the marker's own position against the
        line's comment opener is the whole reading.
        """
        context = self.context
        if context is not None and context.comment_at(line_no, match.start()):
            return True
        return self.on_comment_opener(line, match)

    def opens_note(self, line_no: int, line: str, match: re.Match[str]) -> bool:
        """Whether a marker at `match` starts a real note under the active mode."""
        if self.in_fence:
            return False
        if (
            self.marker is IGNORE_RE
            and self.context is not None
            and not self.context.comment_at(line_no, match.start())
        ):
            return False
        if self.ignore is not None and self.ignore.match(line, match.start()):
            return False
        if self.mode == ScanMode.JS and match.group(1) == "#":
            return False
        if self.mode == ScanMode.JSON:
            return False
        if inside_inline_code(line, match.start()) and not (
            self.context is not None and self.context.comment_at(line_no, match.start())
        ):
            return False
        if not self.at_comment_start(line_no, line, match):
            return False
        return self.in_note_context(line_no, match.start())

    def continuation(self, intro: str) -> Callable[[int, str], str | None]:
        """A mapper yielding a continuation line's text, or ``None`` to end the run."""

        def content_of(line_no: int, line: str) -> str | None:
            prefix = COMMENT_PREFIX_RE.match(line)
            if prefix is None or prefix.group(1) != intro:
                return None
            if not self.in_note_context(line_no, prefix.start(1)):
                return None
            marker = self.marker.search(line)
            if marker is not None and self.on_comment_opener(line, marker):
                return None
            content = line[prefix.end() :].strip()
            if content and not any(ch.isalnum() for ch in content):
                return None
            return content

        return content_of

    def notes(
        self, before: int = CONTEXT_BEFORE, after: int = CONTEXT_AFTER
    ) -> list[MarkerComment]:
        found: list[MarkerComment] = []
        for numbered in self.cursor:
            line_no = numbered.number
            line = numbered.text
            if self.is_markdown and FENCE_RE.match(line):
                self.in_fence = not self.in_fence
                continue

            match = next(
                (
                    candidate
                    for candidate in self.marker.finditer(line)
                    if self.opens_note(line_no, line, candidate)
                ),
                None,
            )
            if match is None:
                continue

            parts = [line[match.end() :].strip()]
            end_line = line_no

            if not self.is_markdown and line[: match.start()].strip() == "":
                for continuation in self.cursor.take_mapping(
                    self.continuation(match.group(1))
                ):
                    parts.append(continuation.value)
                    end_line = continuation.number

            found.append(
                MarkerComment(
                    start_line=line_no,
                    end_line=end_line,
                    read_start=max(1, line_no - before),
                    read_end=min(self.total, end_line + after),
                    text=" ".join(part for part in parts if part),
                )
            )
        return found


def find_markers(
    text: str,
    mode: str = ScanMode.TEXT,
    *,
    marker: re.Pattern[str],
    ignore: re.Pattern[str] | None = None,
) -> list[MarkerComment]:
    """Extract one marker family's notes from a file's text under a `ScanMode`."""
    return MarkerScan(text, mode, marker=marker, ignore=ignore).notes()


def find_feedback(text: str, mode: str = ScanMode.TEXT) -> list[MarkerComment]:
    """Extract `# lup:` review notes from a file's text.

    Binds :func:`find_markers` to the review-note rules: `ignore` directives
    are skipped — they are the anti-pattern escape hatch, not feedback. That
    covers the standalone file-level `# lup: ignore` too: it disables
    anti-pattern checks (see `lup.harness.codescan.antipatterns`), never note gathering,
    so feedback in an opted-out file still surfaces. Each surviving note is
    then classified through :meth:`MarkerComment.classify_deferral`, so parked
    work is parked in the data rather than only in the prose.
    """
    notes = find_markers(text, mode, marker=MARKER_RE, ignore=IGNORE_RE)
    return [note.classify_deferral() for note in notes]


class NoteTarget(BaseModel):
    """One note a caller means to remove, by recorded position and body.

    ``text`` is the body as :meth:`MarkerComment.marker_text` spells it.
    Supplying it makes the match identity-bearing: the target still finds its
    note after surrounding lines drifted, and never removes a different note
    that merely sits at the recorded line. Omitting it matches on position
    alone, which is all a `file:line` caller can offer.
    """

    line: int
    text: str | None = None

    def resolve(self, candidates: list[MarkerComment]) -> MarkerComment | None:
        """Find the note this target names, tolerating drift when it carries text.

        A text-bearing target picks the nearest candidate whose body matches
        exactly, so an unchanged line scores zero and wins outright while a note
        pushed up or down by an earlier edit is still found.
        """
        if self.text is None:
            return next(
                (note for note in candidates if note.start_line == self.line), None
            )
        return min(
            [note for note in candidates if note.marker_text() == self.text],
            key=lambda note: abs(note.start_line - self.line),
            default=None,
        )


class NoteRemoval(BaseModel):
    """Rewritten text, the notes actually removed, and the targets not found."""

    text: str
    removed: list[MarkerComment]
    missing: list[NoteTarget]


def without_note(lines: list[str], note: MarkerComment) -> None:
    """Drop a standalone note whole; leave an inline note's code behind."""
    head = lines[note.start_line - 1]
    match = MARKER_RE.search(head)
    head_code = head[: match.start()] if match is not None else ""
    if match is not None and head_code.strip():
        lines[note.start_line - 1] = head_code.rstrip()
    else:
        del lines[note.start_line - 1 : note.end_line]


def remove_notes(
    text: str, mode: str, targets: list[NoteTarget], *, wake: bool = False
) -> NoteRemoval:
    """Strip each target's note from one file's text.

    A `defer` note is parked work rather than open feedback, whether or not it
    stated a gate, so a target landing on one leaves it in place unless *wake*
    is set. A target whose note is absent is reported rather than raised — the
    code a note sat on may already be gone, which is an outcome to record, not
    a failure.
    """
    candidates = find_feedback(text, mode)
    lines = text.splitlines()
    claimed: list[MarkerComment] = []
    missing: list[NoteTarget] = []
    for target in targets:
        note = target.resolve([note for note in candidates if note not in claimed])
        if note is None or (note.kind == "defer" and not wake):
            missing.append(target)
            continue
        claimed.append(note)
    for note in sorted(claimed, key=lambda note: note.start_line, reverse=True):
        without_note(lines, note)
    trailing = "\n" if text.endswith("\n") else ""
    return NoteRemoval(
        text="\n".join(lines) + trailing, removed=claimed, missing=missing
    )


class ClaimRevision(BaseModel):
    """Rewritten text, the claims acted on, and what could not be.

    ``missing`` names targets that resolved to no note at all; ``refused``
    names notes a target landed on that are not `solved:` claims, which the
    claim instruments decline to touch.
    """

    text: str
    revised: list[MarkerComment]
    missing: list[NoteTarget]
    refused: list[MarkerComment]


class ClaimSelection(BaseModel):
    """Targets resolved into claims, absent targets, and refused notes."""

    claimed: list[MarkerComment]
    missing: list[NoteTarget]
    refused: list[MarkerComment]


def solved_claims_only(
    candidates: list[MarkerComment], targets: list[NoteTarget]
) -> ClaimSelection:
    """Resolve targets to claims, refusing any note that is not one.

    The refusal is the claim instruments' whole safety: open feedback and
    parked work stay untouchable through this path no matter what a target
    names.
    """
    claimed: list[MarkerComment] = []
    missing: list[NoteTarget] = []
    refused: list[MarkerComment] = []
    for target in targets:
        note = target.resolve([note for note in candidates if note not in claimed])
        if note is None:
            missing.append(target)
            continue
        match note.kind:
            case "solved":
                claimed.append(note)
            case _:
                refused.append(note)
    return ClaimSelection(claimed=claimed, missing=missing, refused=refused)


def retire_claims(text: str, mode: str, targets: list[NoteTarget]) -> ClaimRevision:
    """Delete each target's `solved:` claim — the verify pass's confirming act.

    Deleting a claim through the edit gate is denied for every session, so
    this is the one designed instrument that removes one, and it removes
    only claims: a target landing on open feedback or parked work is
    refused, so the reviewer's instrument structurally cannot delete what
    was never claimed solved.
    """
    selection = solved_claims_only(find_feedback(text, mode), targets)
    lines = text.splitlines()
    for note in sorted(
        selection.claimed, key=lambda note: note.start_line, reverse=True
    ):
        without_note(lines, note)
    trailing = "\n" if text.endswith("\n") else ""
    return ClaimRevision(
        text="\n".join(lines) + trailing,
        revised=selection.claimed,
        missing=selection.missing,
        refused=selection.refused,
    )


def reopened_head(line: str) -> str:
    """One claim's head line with the `solved:` head stripped back off."""
    marker = MARKER_RE.search(line)
    if marker is None:
        return line
    remainder = line[marker.end() :].lstrip()
    head = SOLVED_HEAD_RE.match(remainder)
    body = remainder[head.end() :] if head is not None else remainder
    return f"{line[: marker.end()]} {body}"


def restore_claims(
    text: str, mode: str, targets: list[NoteTarget], narrowed: str | None = None
) -> ClaimRevision:
    """Reopen each target's claim as ordinary feedback, optionally narrowed.

    A plain restore strips `solved: ` from the head line and keeps every
    continuation line untouched, so the note reads exactly as it was first
    written. A narrowed restore replaces the whole note with the outstanding
    part, which is the partly-resolved verdict.
    """
    selection = solved_claims_only(find_feedback(text, mode), targets)
    lines = text.splitlines()
    for note in sorted(
        selection.claimed, key=lambda note: note.start_line, reverse=True
    ):
        if narrowed is None:
            lines[note.start_line - 1] = reopened_head(lines[note.start_line - 1])
            continue
        head = lines[note.start_line - 1]
        marker = MARKER_RE.search(head)
        end = marker.end() if marker is not None else 0
        lines[note.start_line - 1 : note.end_line] = [f"{head[:end]} {narrowed}"]
    trailing = "\n" if text.endswith("\n") else ""
    return ClaimRevision(
        text="\n".join(lines) + trailing,
        revised=selection.claimed,
        missing=selection.missing,
        refused=selection.refused,
    )
