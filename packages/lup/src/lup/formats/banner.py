"""The one generated-from banner, spelled however a target format admits.

Every generated artifact whose format can hold a comment opens with the same
sentence: what produced it, the command that rebuilds it, and where the
provenance record lives. Only the source, the command, any target-specific
notes, and the comment syntax differ, so the wording is written once here and
each generator supplies its parameters. A format with no comment syntax, and
the two families that deliberately carry no banner, are named by
:class:`BannerExemption` so the absence is declared rather than noticed.
"""

from abc import ABC, abstractmethod
from pathlib import PurePath
from typing import Annotated, Literal

from pydantic import BaseModel, Discriminator, Field

from lup.policy.kernel.diagnostic import devtools, spelled

# lup: ignore[constant-declaration] — the command a reader types, whose words are
# the CLI's own rather than a preference this module holds
REGENERATE_COMMAND = spelled(devtools("harness", "generate", "all"))
"""The devtools command that rebuilds every native harness tree."""

PROVENANCE_RECORD = "docs/harness.md"
"""Where a repository records what each generated tree is compiled from."""


class CommentSyntax(ABC):
    """Spell a block of banner lines as one file format's comment."""

    @abstractmethod
    def wrap(self, lines: list[str]) -> str:
        """Return the comment block, each line terminated."""


class LinePrefixComment(CommentSyntax):
    """A format whose comments open each line with the same marker."""

    def __init__(self, marker: str) -> None:
        if not marker:
            raise ValueError("a line-prefix comment marker cannot be empty")
        self.marker = marker

    def wrap(self, lines: list[str]) -> str:
        return "".join(f"{self.marker} {line}\n" for line in lines)


class DelimitedComment(CommentSyntax):
    """A format that fences a whole block between two delimiters."""

    def __init__(self, opening: str, closing: str) -> None:
        if not opening or not closing:
            raise ValueError("a delimited comment needs both delimiters")
        self.opening = opening
        self.closing = closing

    def wrap(self, lines: list[str]) -> str:
        return f"{self.opening} {' '.join(lines)} {self.closing}\n"


class PathMatcher(ABC):
    """Decide whether one artifact path belongs to a comment route."""

    @abstractmethod
    def matches(self, path: PurePath) -> bool:
        """Return whether this route claims the path."""


class SuffixPathMatcher(PathMatcher):
    """Match a closed set of file suffixes."""

    def __init__(self, suffixes: list[str]) -> None:
        if not suffixes:
            raise ValueError("a suffix path matcher cannot be empty")
        self.suffixes = tuple(suffixes)

    def matches(self, path: PurePath) -> bool:
        return path.suffix in self.suffixes


class CommentRoute(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One immutable matcher and the comment syntax it selects."""

    name: str
    matcher: PathMatcher
    syntax: CommentSyntax


class CommentRouter:
    """Select the first route whose matcher claims a path."""

    def __init__(self, routes: list[CommentRoute]) -> None:
        names = [route.name for route in routes]
        if len(names) != len(dict.fromkeys(names)):
            raise ValueError("comment route names must be unique")
        self.routes = tuple(routes)

    def route_for(self, path: PurePath) -> CommentRoute | None:
        """The claiming route, or ``None`` where the format admits no comment."""
        return next(
            (route for route in self.routes if route.matcher.matches(path)), None
        )

    def resolve(self, path: PurePath) -> CommentSyntax:
        selected = self.route_for(path)
        if selected is None:
            raise LookupError(f"no comment syntax for artifact {path.as_posix()!r}")
        return selected.syntax


# lup: ignore[library-default] — one route per comment syntax the formats
# themselves define, so the table follows the languages rather than a taste
ARTIFACT_COMMENT_ROUTER = CommentRouter(
    [
        CommentRoute(
            name="markdown",
            matcher=SuffixPathMatcher([".md"]),
            syntax=DelimitedComment("<!--", "-->"),
        ),
        CommentRoute(
            name="hash",
            matcher=SuffixPathMatcher(
                [".py", ".toml", ".rules", ".sh", ".yml", ".yaml"]
            ),
            syntax=LinePrefixComment("#"),
        ),
        CommentRoute(
            name="slash",
            matcher=SuffixPathMatcher([".js", ".ts"]),
            syntax=LinePrefixComment("//"),
        ),
    ]
)
"""Which comment syntax each generated file format is written in."""


class BannerPlacement(BaseModel, frozen=True):
    """Where a banner sits in one file's text.

    An interpreter line has to stay the first line of the file, so it is the
    one thing a banner opens beneath rather than above.
    """

    interpreter: str
    body: str

    @classmethod
    def of(cls, text: str) -> "BannerPlacement":
        """Read the placement from text, whether or not it carries a banner."""
        if not text.startswith("#!"):
            return cls(interpreter="", body=text)
        interpreter, *rest = text.splitlines(keepends=True)
        return cls(interpreter=interpreter, body="".join(rest))


type BannerExemptionReason = Literal[
    "prompt_text", "verbatim_copy", "comment_free_format"
]
"""Why one artifact carries no rendered generated-from banner.

The first two are formats that *could* hold a comment and must not: model-
facing prompt text, where the banner would enter every prompt, and a verbatim
copy, where it would break the diff proving the copy faithful. The third is a
format with no comment at all — JSON — said here rather than by declaring no
banner, so that an artifact's provenance is carried in every case and only its
rendering varies."""


class BannerExemption(BaseModel, frozen=True):
    """One artifact's declared reason for carrying no generated-from banner.

    ``prompt_text`` is verbatim model-facing text after its frontmatter, where
    a banner would be injected into every prompt. ``verbatim_copy`` is a
    byte-identical copy of a canonical source, where a banner would break the
    diff that proves the copy faithful.

    An exemption says the provenance cannot be *printed*, never that there is
    none: the artifact came from somewhere either way, and a reader asking
    what compiles a skill file asks the same question the banner answers for
    the file beside it. So the source is carried and simply not rendered.
    """

    type: Literal["exempt"] = "exempt"
    reason: BannerExemptionReason

    source: str = ""
    """What this artifact is compiled from, carried but never rendered.

    Empty on the shared declaration a compile site starts from, and filled by
    :meth:`compiled_from` at the site that knows which declaration it is
    rendering. Left unset it states nothing, which is the whole of what an
    exemption stated before it could carry one.
    """

    def opens(self, path: PurePath, content: str) -> bool:
        """An exemption states nothing the content could fail to carry."""
        return True

    def attribution(self) -> str:
        """Where this artifact came from, whether or not it can say so."""
        return self.source

    def applied_to(self, path: PurePath, body: str) -> str:
        """The body as it stands, an exemption being the absence of a banner.

        Answered so a caller composing an artifact asks the union rather than
        which member it holds — the same reason :meth:`opens` is answered
        here and says nothing.
        """
        return body

    def compiled_from(self, source: str) -> "BannerExemption":
        """This exemption, carrying the source the compiling site knows."""
        return self.model_copy(update={"source": source})


class GeneratedBanner(BaseModel, frozen=True):
    """What produced one artifact, and the exact command that rebuilds it."""

    type: Literal["banner"] = "banner"
    source: str = Field(min_length=1)
    """The canonical source this artifact is compiled from: an importable
    module for content a module holds, or the declaration id for content a
    typed declaration holds."""

    command: str = Field(min_length=1)
    """The command a reader runs to rebuild this artifact, exactly as typed."""

    notes: list[str] = []
    """Anything else a reader of this one target needs, such as where the
    personal half of a generated configuration lives."""

    record: str = PROVENANCE_RECORD
    """Where this repository records what every generated tree is built from,
    for the artifacts whose format can hold no banner at all."""

    def attribution(self) -> str:
        """Where this artifact came from, the same question an exemption answers."""
        return self.source

    def lines(self) -> list[str]:
        """The banner sentences, before any format spells them as a comment."""
        return [
            f"Generated from {self.source} by `{self.command}` — "
            "edit the source, not this file.",
            f"See {self.record}.",
            *self.notes,
        ]

    def render(self, path: PurePath) -> str:
        """The comment block, and the blank line that separates it from the body."""
        return ARTIFACT_COMMENT_ROUTER.resolve(path).wrap(self.lines()) + "\n"

    def applied_to(self, path: PurePath, body: str) -> str:
        """Open ``body`` with this banner, below any interpreter line."""
        placement = BannerPlacement.of(body)
        return placement.interpreter + self.render(path) + placement.body

    def opens(self, path: PurePath, content: str) -> bool:
        """Whether ``content`` already opens with exactly this banner."""
        return BannerPlacement.of(content).body.startswith(self.render(path))


PROMPT_TEXT = BannerExemption(reason="prompt_text")
"""Declared by every artifact that is verbatim model-facing prompt text."""

VERBATIM_COPY = BannerExemption(reason="verbatim_copy")
"""Declared by every artifact copied byte-identically from its source."""

COMMENT_FREE = BannerExemption(reason="comment_free_format")
"""Declared by every artifact whose format holds no comment to put one in."""


type ArtifactBanner = Annotated[
    GeneratedBanner | BannerExemption, Discriminator("type")
]
"""Every generated artifact either states its provenance or why it cannot.

Both members answer ``opens`` and ``attribution``, so a caller holding the
union asks it directly rather than narrowing to a variant; a variant that
stopped answering would fail at every call site instead of falling through a
match arm."""
