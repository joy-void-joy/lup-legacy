"""A setup wizard as a declaration, and one renderer that draws it.

:class:`lup.devtools.setup.Integration` already declares what a project has to
configure, and this dashboard already serves that declaration as a page. But
the page can draw exactly one thing: a flat list of key-and-value fields.
Anything else — verifying a secret against the service before recording it,
creating something with it afterwards, streaming a browser to whoever is
reading — is a ``setup_func``, and the page's whole answer to a ``setup_func``
is to tell the reader to go and open a terminal.

For some projects every flow that matters is a ``setup_func``, so that page
has nothing to offer them and they would write their own instead. What they
need is not a nicer page: it is a way for a bespoke flow to *declare its
browser shape* — what it asks for, what it opens, how it proves itself, what
undoing it means — so one renderer can draw it. The same
declaration-plus-renderer split the harness uses for prompts: a step says what
is meant, the renderer says how a browser spells it.

Nothing here names a service, a domain, or a project. A domain supplies its own
scope and its own steps, and the page draws them: the ``wizard`` surface Vite
builds into ``lup.web``'s package data, typed against the models below, which
knows no step by name. A test holds the package data to naming every built
bundle, because an installed dashboard that reads an asset the package data
leaves out serves a page with nothing behind it.

Two guards live here rather than in any surface, because a page draws only what
a step offers while a request is whatever arrived on the socket:

- a step whose standing does not offer it cannot be run by naming it, and
- a row's act that was drawn nowhere cannot be performed by asking for it.
"""

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel


class StepField(BaseModel, frozen=True):
    """One value a step asks for."""

    key: str
    """How the answer comes back, and how the step reads it."""

    label: str
    placeholder: str = ""

    secret: bool = False
    """Whether the browser should hide what is typed.

    A property of the field rather than of the widget, so a token is masked
    wherever it is drawn rather than wherever somebody remembered to mask it.
    """


class Answer(BaseModel, frozen=True):
    """One value that came back, against the field that asked for it."""

    key: str
    value: str


class StepAnswers(BaseModel):
    """Everything a page submitted for one step.

    A list of named answers rather than a bare mapping: what arrives on a
    socket is untrusted, and a shape says which keys were expected where a
    mapping would take any of them.
    """

    answers: list[Answer] = []

    def value(self, key: str) -> str:
        """What was given for one field, or "" where it was not answered."""
        return next((each.value for each in self.answers if each.key == key), "")


class StepOutcome(BaseModel, frozen=True):
    """What running, testing, or undoing a step did."""

    ok: bool
    message: str


class StepStanding(BaseModel, frozen=True):
    """Where a step stands right now.

    ``done`` and ``offered`` are separate because they answer different
    questions and disagree in both directions: a finished step is still
    offered when redoing it is how a wrong answer gets fixed, and an unfinished
    one is withheld while the step before it is unanswered.
    """

    done: bool = False
    detail: str = ""

    offered: bool = True
    """Whether this step can be run as things stand."""

    blocked: str = ""
    """Why it cannot, where it cannot — shown instead of the form."""


class RowAct(BaseModel, frozen=True):
    """One verb offered against one row.

    The same shape a terminal menu draws, so a surface cannot offer a verb the
    other surface does not have.
    """

    slug: str
    label: str
    consequence: str
    asks: str = ""
    destructive: bool = False


class Row(BaseModel, frozen=True):
    """One thing a listing step lists, and what may be done to it."""

    name: str
    detail: str = ""
    acts: list[RowAct] = []

    stream: str = ""
    """A socket path this row's screen is streamed over, where it has one.

    Carried by the row rather than looked up by the page, so a row that cannot
    be streamed has nowhere for a viewer to be sent.
    """

    def offers(self, slug: str) -> RowAct | None:
        """The act this row offers under that name, or None."""
        return next((act for act in self.acts if act.slug == slug), None)


type StepKind = Literal["form", "rows"]
"""How a step is drawn: as something to fill in, or as a list to act on."""


class SetupStep[Scope](BaseModel, ABC, frozen=True):
    """One step of a walkthrough, and everything needed to draw it.

    A kind of step declares how it reads, says where it stands, and does the
    thing. ``standing`` is abstract rather than defaulted: a step that does not
    say whether it is done would be drawn as unfinished forever, and a default
    would make that the quiet outcome of forgetting rather than a refusal to
    build the step at all.
    """

    slug: str
    """Stable identity, so a request can name this step."""

    title: str
    blurb: str = ""
    """One paragraph saying why this step exists, in the reader's terms."""

    guide: list[str] = []
    """The how-to, for the parts no API can do on somebody's behalf."""

    numbered: bool = True
    """Whether the guide is drawn as an ordered list.

    Prose written for a terminal often carries its own "1." and "2.", and
    drawing that inside an ordered list numbers every line twice. A step whose
    guide is already enumerated — or whose lines are continuations rather than
    steps — says so here rather than having the numbers stripped back out of
    it, which would go wrong for every line that begins with a figure for its
    own reasons.
    """

    opens: str = ""
    """An external page this step sends somebody to, where it does."""

    fields: list[StepField] = []
    submit: str = "Save"
    kind: StepKind = "form"

    tests: str = ""
    """What a live check is called here; empty where there is nothing to check.

    The label is the declaration. Asking the class whether it overrode
    :meth:`test` would put the answer somewhere a reader cannot see it, and a
    step that grew a check would still be drawn without a button until
    somebody noticed.
    """

    undoes: str = ""
    """What clearing this step is called; empty where nothing can be cleared."""

    @abstractmethod
    def standing(self, scope: Scope) -> StepStanding:
        """Where this step stands for this scope."""

    @abstractmethod
    async def run(self, scope: Scope, answers: StepAnswers) -> StepOutcome:
        """Do the step, and say what happened."""

    async def test(self, scope: Scope) -> StepOutcome | None:
        """Prove what was recorded actually works, where that is possible.

        None means there is nothing to prove. A step that *can* be tested
        should be, because a value that was accepted and never exercised fails
        later, in a log, to nobody.
        """
        return None

    async def reset(self, scope: Scope) -> StepOutcome | None:
        """Undo what this step recorded, where undoing it means something.

        None means there is nothing to clear. Where there is, this is the only
        way a wrong answer gets corrected without somebody editing a file by
        hand — which is how a deployment ends up with a token nobody can
        account for.
        """
        return None

    def rows(self, scope: Scope) -> list[Row]:
        """What this step lists, where it lists anything."""
        return []

    async def act(self, scope: Scope, row: str, slug: str, answer: str) -> StepOutcome:
        """Perform one of a row's acts.

        Refuses by default, so a step that lists nothing can never be talked
        into doing something to a row it never drew. Only a listing step
        answers this, and :func:`act_on_row` has already checked that the row
        exists and offers the act by the time it arrives.
        """
        return StepOutcome(ok=False, message=f"{self.title} has no rows to act on.")


class StepView(BaseModel, frozen=True):
    """One step as the page draws it: the declaration, plus where it stands."""

    slug: str
    title: str
    blurb: str = ""
    guide: list[str] = []
    numbered: bool = True
    opens: str = ""
    kind: StepKind = "form"
    fields: list[StepField] = []
    submit: str = "Save"
    standing: StepStanding = StepStanding()
    rows: list[Row] = []
    tests: str = ""
    undoes: str = ""


class ScopeChoice(BaseModel, frozen=True):
    """One of the things this wizard can be pointed at."""

    name: str
    label: str
    detail: str = ""
    chosen: bool = False


class WizardView(BaseModel, frozen=True):
    """Everything the page needs to draw itself, in one reply."""

    title: str = ""
    lede: str = ""
    scope_label: str = ""
    """What a scope is called here, so the page's own words are the domain's."""

    scopes: list[ScopeChoice] = []
    chosen: str = ""
    steps: list[StepView] = []

    creates: str = ""
    """What making another scope is called; empty where they cannot be made.

    The label is the declaration, as it is for a step's check and undo. A
    project whose scopes come from somewhere else — one env file, a directory
    somebody populates by hand — leaves it empty and the page offers nothing.
    """

    create_asks: str = ""
    """The placeholder for the name a new scope is given."""

    notice: str = ""
    """Something to say above the steps — no scope yet, say, or one refused."""


class Wizard[Scope]:
    """A walkthrough, and the one door every surface drives it through.

    Holding the steps rather than passing them to each call is what gives the
    guards a single home. A surface asks this to draw, to run, to check, to
    undo, and to act on a row; none of those take the surface's word for what
    is allowed, so no surface can forget to ask.

    A plain class rather than a model, because its one field is a list of
    abstract steps: validating that field would mean constructing
    :class:`SetupStep` itself, which is abstract precisely so that it cannot be.
    Nothing here crosses a wire — the shapes that do are models above.
    """

    def __init__(self, steps: list[SetupStep[Scope]]) -> None:
        self.steps = steps

    def named(self, slug: str) -> SetupStep[Scope] | None:
        """The step a request names, or None where it names nothing."""
        return next((step for step in self.steps if step.slug == slug), None)

    def missing(self, slug: str) -> StepOutcome:
        """What every entry point says about a step that is not here."""
        return StepOutcome(ok=False, message=f"There is no step called {slug!r}.")

    def viewed(self, scope: Scope) -> list[StepView]:
        """Draw every step against one scope, asking each where it stands.

        Nothing here reaches the network. Where a step stands is read off what
        has already been recorded, so opening the page costs nothing; proving
        that a recorded value still works is what the live check is for, and
        somebody asks for that.
        """
        return [
            StepView(
                slug=step.slug,
                title=step.title,
                blurb=step.blurb,
                guide=step.guide,
                numbered=step.numbered,
                opens=step.opens,
                kind=step.kind,
                fields=step.fields,
                submit=step.submit,
                standing=step.standing(scope),
                rows=step.rows(scope),
                tests=step.tests,
                undoes=step.undoes,
            )
            for step in self.steps
        ]

    async def run(self, scope: Scope, slug: str, answers: StepAnswers) -> StepOutcome:
        """Run one step, refusing one this scope was never offered.

        Whether the step is offered is decided here rather than taken from what
        the page posted. The page draws only what a scope allows, but a request
        is whatever arrived on the socket, and a step withheld from the browser
        must not be reachable by asking for it directly.
        """
        step = self.named(slug)
        if step is None:
            return self.missing(slug)
        standing = step.standing(scope)
        if not standing.offered:
            return StepOutcome(
                ok=False,
                message=(
                    standing.blocked or f"{step.title} cannot be run as things stand."
                ),
            )
        return await step.run(scope, answers)

    async def test(self, scope: Scope, slug: str) -> StepOutcome:
        """Exercise what a step recorded, saying so when there is nothing to."""
        step = self.named(slug)
        if step is None:
            return self.missing(slug)
        outcome = await step.test(scope)
        return outcome or StepOutcome(
            ok=False, message=f"{step.title} has nothing to test."
        )

    async def reset(self, scope: Scope, slug: str) -> StepOutcome:
        """Clear what a step recorded, saying so when there is nothing to."""
        step = self.named(slug)
        if step is None:
            return self.missing(slug)
        outcome = await step.reset(scope)
        return outcome or StepOutcome(
            ok=False, message=f"{step.title} has nothing to undo."
        )

    async def act(
        self, scope: Scope, slug: str, row: str, act: str, answer: str
    ) -> StepOutcome:
        """Perform one of a row's acts, refusing one that row does not offer.

        The guard is here for the reason the step guard is: a page draws the
        verbs a row allows, and a destructive verb it was never shown must not
        be reachable by naming it. Doing it once, here, is what stops each
        listing step from having to remember.
        """
        step = self.named(slug)
        if step is None:
            return self.missing(slug)
        listed = next((each for each in step.rows(scope) if each.name == row), None)
        if listed is None:
            return StepOutcome(ok=False, message=f"{step.title} does not list {row!r}.")
        if listed.offers(act) is None:
            return StepOutcome(
                ok=False, message=f"{row} cannot do that as things stand."
            )
        return await step.act(scope, row, act, answer)
