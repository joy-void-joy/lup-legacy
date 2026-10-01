"""What a launch says, and how urgently it means it.

A session's opening is thirty lines long and every one of them arrives in the
same weight. Somewhere in that block is the sentence that decides whether the
session can do its work -- no forge token, a boundary that did not come up,
a capability the host lacks -- and it sits between a version number and a
path, indistinguishable from both. Colour is not decoration here: it is the
only thing that makes the block skimmable, and skimmable is the difference
between a warning read and a warning scrolled past.

So urgency travels with the sentence rather than being chosen where it is
printed. A caller says what kind of thing it is saying; this decides what
that looks like, once, for every runtime and every surface. The alternative
is `typer.secho` at fifty call sites, which puts the judgement in fifty
places and makes it unreadable by anything that is not a terminal.

Colour is the terminal's business alone: :func:`typer.echo` strips styling
when its stream is not a tty and honours ``NO_COLOR``, so a redirected launch
records the same text with no escapes in it.
"""

from collections.abc import Callable, Iterator
from typing import Literal

import typer
from pydantic import BaseModel, Field

type Urgency = Literal[
    "refusal",
    "warning",
    "progress",
    "ready",
    "artifact",
    "boundary",
    "detail",
]
"""What kind of thing a line is, which is what decides how it is painted.

Seven rather than three, because the three that carry alarm are useless
without the ones they have to stand out *from*. A block where only errors are
coloured is a block where everything else is one undifferentiated wall, and
the eye has no anchor to find the error against.

Which is why each one states its test, and why the pair that decides the
block's temperature states it twice. ``warning`` is for something wrong:
this launch cannot do its work through it, or it is a misconfiguration that
will fail later somewhere that names neither the cause nor the remedy.
``boundary`` is for a declared security posture working exactly as configured.
The two can read alike from inside the module that prints one, which is how a
healthy launch ends up
orange from top to bottom: every author of a notice knows their line is
worth reading, and ``warning`` is the only urgency that says so. It is not.
A reader who has learned that the opening block is orange whatever happened
has been trained out of the one thing the colour is for.
"""


class Ink(BaseModel, frozen=True):
    """How one urgency is painted, in the terminal's own vocabulary."""

    colour: str | int = Field(
        default="",
        description=(
            "A named colour, or a 256-colour index where the sixteen names "
            "have nothing close enough -- orange is the case that forces "
            "this, being the shade a warning wants and one no ANSI name "
            "offers. Empty leaves the foreground alone, for a weight that "
            "is carried by dimming rather than by hue"
        ),
    )
    bold: bool = False
    dim: bool = False

    def paint(self, text: str) -> str:
        """This text in this ink, saying only what this ink actually asks for.

        ``None`` rather than ``False`` for the attributes this ink does not
        want, because the two are different instructions: ``False`` emits the
        turn-it-off code, so every plain line carried two redundant resets
        and a bold line carried a bold and an un-dim. Nothing renders
        differently for it, and everything downstream that reads the escapes
        -- a diff of captured output, a terminal recording -- reads twice the
        noise.
        """
        return typer.style(
            text,
            fg=self.colour or None,
            bold=self.bold or None,
            dim=self.dim or None,
        )


class Palette(BaseModel, frozen=True):
    """Which ink each urgency is written in, as one overridable declaration.

    A default rather than a constant, because a palette is a judgement about
    somebody else's terminal: a light background, a colour-vision difference,
    or a house style are all reasons to disagree, and none of them should
    require forking the code that decides what is a warning.
    """

    refusal: Ink = Ink(colour="red", bold=True)
    warning: Ink = Ink(colour=208)
    progress: Ink = Ink(colour="blue")
    ready: Ink = Ink(colour="green")
    artifact: Ink = Ink(colour="cyan")
    boundary: Ink = Ink(colour="magenta")
    detail: Ink = Ink(dim=True)

    def ink(self, urgency: Urgency) -> Ink:
        """The ink this palette writes one urgency in."""
        match urgency:
            case "refusal":
                return self.refusal
            case "warning":
                return self.warning
            case "progress":
                return self.progress
            case "ready":
                return self.ready
            case "artifact":
                return self.artifact
            case "boundary":
                return self.boundary
            case "detail":
                return self.detail


class Notice(BaseModel, frozen=True):
    """One line a launch says, carrying what kind of thing it is.

    ``indent`` is here rather than baked into the text because a continuation
    line is subordinate *and* still has its own urgency -- the cause under a
    failed requirement is part of a refusal, and reading it as a separate
    sentence at full weight is how a three-line finding becomes three
    findings.
    """

    text: str = Field(description="The sentence, without leading whitespace")
    urgency: Urgency = Field(
        default="detail", description="What kind of thing this line is"
    )
    indent: int = Field(
        default=0, description="Levels of subordination to the line above"
    )

    def painted(self, palette: Palette = Palette()) -> str:
        """This notice as a terminal receives it, indentation included."""
        return "    " * self.indent + palette.ink(self.urgency).paint(self.text)

    def say(self, palette: Palette = Palette()) -> None:
        """Print it, letting the stream decide whether the colour survives."""
        typer.echo(self.painted(palette))


class Band(BaseModel, frozen=True):
    """One heading in a launch's opening, and what belongs under it."""

    heading: str = Field(description="The line that introduces this band")
    urgency: Urgency = Field(
        description="What the heading itself is, which is how it is painted"
    )
    carries: list[Urgency] = Field(
        description="Urgencies whose unindented notices belong under this heading"
    )


class Banner(BaseModel):
    """Every line a launch has to say, held until it can be said in order.

    A launch's opening is assembled by half a dozen components in the order
    the launch happens to need them -- the egress before the image, the
    browser before the lease, the forge after both -- and printed in that
    same order, which is an order about the launcher's internals. What the
    reader wants is an order about *them*: whether anything is blocking, then
    what this session can and cannot do, then where to look afterwards. Those
    two orders have nothing to do with each other, and thirty lines printed
    in the first one is where a warning goes to be scrolled past.

    So the notices are collected rather than printed, and the bands decide
    the order once. That is the same move :class:`Palette` makes and for the
    same reason: the component that knows what it is saying should not also
    have to know where in somebody's terminal it belongs.

    Mutable, alone among the models here, because it is an accumulator and
    the alternative is every caller threading a list back out. The bands it
    renders through are not: they are a judgement about presentation, so they
    are an overridable default like the palette beside them.

    Nothing added is ever dropped. A notice no band claims is said last under
    no heading at all, which is the difference between a hierarchy and a
    filter -- a band list that forgot an urgency would otherwise swallow
    every line carrying it, and the launch would read as healthy for the
    reason that made it not.
    """

    notices: list[Notice] = Field(
        default=[], description="What has been said to this launch so far"
    )
    bands: list[Band] = Field(
        default=[
            Band(heading="Ready", urgency="ready", carries=["ready"]),
            Band(
                heading="Action required",
                urgency="warning",
                carries=["refusal", "warning"],
            ),
            Band(
                heading="Session access — informational",
                urgency="boundary",
                carries=["boundary"],
            ),
            Band(heading="Artifacts", urgency="artifact", carries=["artifact"]),
        ],
        description=(
            "The headings, in the order they are printed. Blockers before "
            "facts and facts before paths, because a reader who stops after "
            "one band should have stopped after the one that could change "
            "what they do next"
        ),
    )

    def add(self, notices: list[Notice]) -> None:
        """Hold these until the banner is said."""
        self.notices.extend(notices)

    def claimed(self, urgency: Urgency) -> bool:
        """Whether any band carries lines of this kind."""
        return any(urgency in band.carries for band in self.bands)

    def held(self, wanted: Callable[[Urgency], bool]) -> Iterator[Notice]:
        """Every line whose parent this wants, subordinate lines kept with it.

        A notice indented under another is part of what that one is saying --
        the cause under a refusal, the remediation under a degradation -- so
        it follows its parent's band rather than its own urgency. Sorting by
        urgency alone is what would separate a remedy from the problem it
        remedies and file it under a heading where it makes no sense.

        Which is also why this walks rather than filters: a subordinate line
        carries no record of what it is subordinate to, so the only thing
        that knows is the position, and the last unindented line before it is
        the answer.
        """
        carried = False
        for notice in self.notices:
            if notice.indent == 0:
                carried = wanted(notice.urgency)
            if carried:
                yield notice

    def under(self, band: Band) -> list[Notice]:
        """Every line this band carries."""
        return list(self.held(lambda urgency: urgency in band.carries))

    def unbanded(self) -> list[Notice]:
        """Whatever no band claimed, so that nothing added is ever lost."""
        return list(self.held(lambda urgency: not self.claimed(urgency)))

    def say(self, palette: Palette = Palette()) -> None:
        """Print the whole opening, one heading per band that has anything to say.

        An empty band prints nothing at all, heading included, which is what
        makes the shape informative: a launch showing no `Action required`
        has nothing requiring action, rather than a heading over a blank.
        """
        for band in self.bands:
            held = self.under(band)
            if not held:
                continue
            Notice(text=band.heading, urgency=band.urgency).say(palette)
            for notice in held:
                notice.model_copy(update={"indent": notice.indent + 1}).say(palette)
        for notice in self.unbanded():
            notice.say(palette)
