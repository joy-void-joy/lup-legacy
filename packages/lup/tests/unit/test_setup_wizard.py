"""What the wizard draws, and what it refuses to be talked into.

The engine is domain-free, so these use a scratch domain of their own rather
than the integrations this library ships — which is also the point being
pinned: nothing in the wizard knows what a token or a service is.

Two guards carry the weight. A page draws only what a step's standing offers
and only the verbs a row allows, but a request is whatever arrived on the
socket, so both are decided again here. A surface that forgot to check would
otherwise be one POST away from running a step it never drew.
"""

from pydantic import BaseModel

from lup.devtools.dashboard.setup_page import EnvScope, guide_lines, integration_step
from lup.devtools.dashboard.wizard import (
    Answer,
    Row,
    RowAct,
    SetupStep,
    StepAnswers,
    StepKind,
    StepOutcome,
    StepStanding,
    Wizard,
)
from lup.devtools.setup import Integration, PromptField


class Scratch(BaseModel, frozen=True):
    """A scope standing for whatever a domain would put here."""

    done: bool = False
    offered: bool = True


class Recording(SetupStep[Scratch], frozen=True):
    """A step that says whether it ran, so a refusal is distinguishable."""

    slug: str = "record"
    title: str = "Record"
    ran: list[str] = []

    def standing(self, scope: Scratch) -> StepStanding:
        return StepStanding(
            done=scope.done,
            offered=scope.offered,
            blocked="" if scope.offered else "Do the step before it first.",
        )

    async def run(self, scope: Scratch, answers: StepAnswers) -> StepOutcome:
        self.ran.append(answers.value("what"))
        return StepOutcome(ok=True, message="ran")


class Listing(SetupStep[Scratch], frozen=True):
    """A step that lists one row offering exactly one verb."""

    slug: str = "listing"
    title: str = "Listing"
    kind: StepKind = "rows"
    acted: list[str] = []

    def standing(self, scope: Scratch) -> StepStanding:
        return StepStanding(done=False)

    async def run(self, scope: Scratch, answers: StepAnswers) -> StepOutcome:
        return StepOutcome(ok=False, message="nothing to run")

    def rows(self, scope: Scratch) -> list[Row]:
        return [
            Row(
                name="ana",
                detail="listed",
                acts=[RowAct(slug="greet", label="Greet", consequence="says hello")],
            )
        ]

    async def act(
        self, scope: Scratch, row: str, slug: str, answer: str
    ) -> StepOutcome:
        self.acted.append(f"{slug}:{row}")
        return StepOutcome(ok=True, message="acted")


def answered(value: str) -> StepAnswers:
    return StepAnswers(answers=[Answer(key="what", value=value)])


def test_a_step_is_drawn_from_its_declaration_and_its_standing() -> None:
    """The two halves the split exists for: what it means, and where it is."""
    step = Recording(blurb="why", guide=["first"], opens="https://example.invalid")
    [view] = Wizard([step]).viewed(Scratch(done=True))

    assert view.slug == "record"
    assert view.blurb == "why"
    assert view.guide == ["first"]
    assert view.opens == "https://example.invalid"
    assert view.standing.done


async def test_a_step_the_page_was_never_shown_cannot_be_run() -> None:
    """A request is whatever arrived on the socket, not what the page drew."""
    step = Recording()
    outcome = await Wizard([step]).run(Scratch(offered=False), "record", answered("x"))

    assert not outcome.ok
    assert outcome.message == "Do the step before it first."
    assert step.ran == []


async def test_a_step_that_is_offered_runs_with_its_answers() -> None:
    step = Recording()
    outcome = await Wizard([step]).run(Scratch(), "record", answered("x"))

    assert outcome.ok
    assert step.ran == ["x"]


async def test_a_slug_that_names_nothing_is_refused() -> None:
    step = Recording()
    outcome = await Wizard([step]).run(Scratch(), "rm -rf", answered("x"))

    assert not outcome.ok
    assert "no step called" in outcome.message
    assert step.ran == []


async def test_a_step_declaring_no_check_says_so_rather_than_passing() -> None:
    """Silence would read as a passing check, which is the worse failure."""
    outcome = await Wizard([Recording()]).test(Scratch(), "record")

    assert not outcome.ok
    assert "nothing to test" in outcome.message


async def test_a_step_declaring_nothing_to_undo_says_so() -> None:
    outcome = await Wizard([Recording()]).reset(Scratch(), "record")

    assert not outcome.ok
    assert "nothing to undo" in outcome.message


def test_a_capability_is_declared_rather_than_inferred() -> None:
    """The label is what the page draws a button from, so it is the declaration."""
    [plain] = Wizard([Recording()]).viewed(Scratch())
    [checked] = Wizard([Recording(tests="Check", undoes="Clear")]).viewed(Scratch())

    assert (plain.tests, plain.undoes) == ("", "")
    assert (checked.tests, checked.undoes) == ("Check", "Clear")


async def test_a_row_act_that_was_drawn_nowhere_is_refused() -> None:
    """The guard that stops a destructive verb being reachable by naming it."""
    step = Listing()
    outcome = await Wizard([step]).act(Scratch(), "listing", "ana", "delete", "")

    assert not outcome.ok
    assert "cannot do that" in outcome.message
    assert step.acted == []


async def test_a_row_the_step_never_listed_is_refused() -> None:
    step = Listing()
    outcome = await Wizard([step]).act(Scratch(), "listing", "stranger", "greet", "")

    assert not outcome.ok
    assert "does not list" in outcome.message
    assert step.acted == []


async def test_an_offered_row_act_reaches_the_step() -> None:
    step = Listing()
    outcome = await Wizard([step]).act(Scratch(), "listing", "ana", "greet", "")

    assert outcome.ok
    assert step.acted == ["greet:ana"]


async def test_a_step_that_lists_nothing_refuses_every_row_act() -> None:
    """A form step can never be talked into acting on a row it never drew."""
    outcome = await Wizard([Recording()]).act(Scratch(), "record", "ana", "greet", "")

    assert not outcome.ok
    assert "does not list" in outcome.message


def test_a_step_must_say_where_it_stands_and_what_it_does() -> None:
    """Defaulting either would make forgetting quiet rather than a refusal.

    A step that did not say where it stands would be drawn as unfinished
    forever, and one that did nothing would be a button that silently is not a
    button. Both are abstract so neither can be built.
    """
    assert SetupStep.__abstractmethods__ == frozenset({"standing", "run"})


def test_a_declared_integration_becomes_a_form_without_restating_it() -> None:
    """The projection is the point: one declaration, drawn twice."""
    step = integration_step(
        Integration(
            name="Example",
            command="example",
            help="Set up Example.",
            env_keys=["EXAMPLE_KEY"],
            intro="  [bold]Do this:[/]\n  1. Go there\n\n  2. Copy the key\n",
            browser_url="https://example.invalid",
            fields=[PromptField(key="EXAMPLE_KEY", prompt="EXAMPLE_KEY")],
        )
    )

    assert step.slug == "example"
    assert step.title == "Example"
    assert step.opens == "https://example.invalid"
    assert [field.key for field in step.fields] == ["EXAMPLE_KEY"]


def test_console_markup_does_not_reach_the_browser() -> None:
    """An intro is written for Rich, and a browser shows its tags literally."""
    assert guide_lines("[bold]Do this:[/]\n  1. Go there\n\n  2. Copy it\n") == [
        "Do this:",
        "1. Go there",
        "2. Copy it",
    ]


def test_prose_that_numbers_itself_is_not_numbered_again() -> None:
    """Drawing pre-numbered lines in an ordered list numbers each one twice."""
    step = integration_step(
        Integration(name="E", command="e", help="h", env_keys=[], intro="1. One")
    )
    assert not step.numbered


async def test_a_terminal_only_flow_is_drawn_as_one_and_cannot_be_run() -> None:
    """It has no browser shape to infer, so the page says so instead of hiding it."""
    integration = Integration(
        name="Google",
        command="google",
        help="OAuth.",
        env_keys=[],
        setup_func=dict[str, str],
    )
    step = integration_step(integration)
    standing = step.standing(EnvScope())

    assert not standing.offered
    assert "lup-devtools setup google" in standing.blocked

    outcome = await Wizard([step]).run(EnvScope(), "google", StepAnswers())
    assert not outcome.ok


def test_an_integration_with_no_env_keys_is_not_read_as_configured() -> None:
    """`all([])` is true, so the obvious spelling calls every one of them green.

    An integration that configures a download, a file, or a roster declares no
    env keys. Reporting it configured is wrong twice over: the status is a lie,
    and the detail it then reaches for is not there at all.
    """
    status = Integration(
        name="Browser", command="browser", help="Install it.", env_keys=[]
    ).check_status({})

    assert not status.ok
    assert status.detail
