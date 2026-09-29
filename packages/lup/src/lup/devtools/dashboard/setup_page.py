"""Serve the reusable setup registry through a local web dashboard.

The CLI wizard and the dashboard deliberately take the same ``Integration``
list and the same env-file helpers. A domain customizes setup once; both
interfaces then expose the same fields, status checks, and bespoke-flow
fallbacks.

Here a "fallback" is a step like any other. A declarative integration becomes a
:class:`~lup.devtools.dashboard.wizard.SetupStep` here — the same guide,
external link, and fields it already declares, drawn in a browser instead of a
terminal. An integration with a ``setup_func`` still cannot be run from the
page, because a function that prompts on a terminal has no browser shape to
infer; but it is drawn as a step that says so and names the command, rather
than being a second kind of thing the page has to know about.

A project that wants its bespoke flow in the browser writes its own step and
passes it in. That is the seam this exists for: the page has no opinion about
what a step does, and a project need not leave the dashboard behind the
moment its setup stops being a list of tokens.
"""

from collections.abc import Callable
from functools import partial
from typing import Annotated

import typer
from fastapi import FastAPI
from pydantic import BaseModel
from rich.text import Text

from lup.devtools.dashboard.serve import create_wizard_app
from lup.devtools.dashboard.wizard import (
    ScopeChoice,
    SetupStep,
    StepAnswers,
    StepField,
    StepOutcome,
    StepStanding,
    Wizard,
)
from lup.devtools.setup import Integration, read_env_local, write_env_local
from lup.types import EnvVars
from lup.web.serve import serve_local_page

DASHBOARD_PORT = 8765
"""Where this page listens when nothing says otherwise.

A port is this library's judgement rather than anyone's convention, so it is
the default the ``--port`` flag replaces and the factory parameter an
application overrides — never a value an adopter has to fork to change.
"""

# lup: ignore[constant-declaration] — an identity this module defines, being the
# name its own single scope answers to rather than a judgement a caller could
# hold a different opinion about
ENV_SCOPE = "env"
"""The one scope this library's own setup has: the project's env file.

Named rather than left empty because a scope is what a request carries, and a
name that means something is what makes a stale request distinguishable from
an absent one. A project with several scopes supplies its own.
"""


class EnvScope(BaseModel, frozen=True):
    """What this library's steps configure: one project's ``.env.local``.

    Carries nothing, because the env file is process-global here. It exists so
    the generic engine has a scope to be parametrized by, and so a project with
    a real one has something to replace rather than a special case to remove.
    """


class IntegrationStep(SetupStep[EnvScope], frozen=True):
    """One declared integration, drawn as a step.

    The declaration is already there — name, help, intro, browser URL, fields,
    status — so this is a projection rather than a second description. Nothing
    is restated, which is what stops the terminal and the browser from
    disagreeing about what an integration is called or what it needs.
    """

    integration: Integration

    def standing(self, scope: EnvScope) -> StepStanding:
        status = self.integration.check_status(read_env_local())
        if self.integration.setup_func is not None and not self.integration.fields:
            return StepStanding(
                done=status.ok,
                detail=status.detail,
                offered=False,
                blocked=(
                    "This one prompts on a terminal: run "
                    f"`uv run lup-devtools setup {self.integration.command}`."
                ),
            )
        return StepStanding(done=status.ok, detail=status.detail)

    def answered(self, answers: StepAnswers) -> EnvVars:
        """Whichever of this integration's own fields came back with a value.

        Only its own, because what a page posts is untrusted: an integration
        writes the keys it declared or none. Blank answers are dropped rather
        than written through, so saving a form with one field filled in does
        not erase the others.
        """
        return {
            field.key: answers.value(field.key)
            for field in self.integration.fields
            if answers.value(field.key)
        }

    async def run(self, scope: EnvScope, answers: StepAnswers) -> StepOutcome:
        given = self.answered(answers)
        if not given:
            return StepOutcome(ok=False, message="Nothing was filled in.")
        write_env_local(given)
        return StepOutcome(ok=True, message=f"Saved {', '.join(sorted(given))}.")


def guide_lines(intro: str | None) -> list[str]:
    """One integration's console intro, as lines a browser can show.

    An ``intro`` is written for a Rich console, so it carries console markup —
    ``[bold]…[/]`` and the rest — which a browser renders as literal brackets.
    Rich's own parser is what removes them: the markup is its format, and
    stripping the tags by hand would get exactly the cases where the prose
    contains a bracket for its own reasons.

    The lines are kept apart rather than joined because the intros are already
    written as numbered steps, and a guide is drawn as a list.
    """
    if not intro:
        return []
    plain = Text.from_markup(intro).plain
    return [stripped for line in plain.splitlines() if (stripped := line.strip())]


def integration_step(integration: Integration) -> IntegrationStep:
    """Project one declared integration into the step that draws it."""
    return IntegrationStep(
        slug=integration.command,
        title=integration.name,
        blurb=integration.help,
        guide=guide_lines(integration.intro),
        numbered=False,
        opens=integration.browser_url or "",
        fields=[
            StepField(key=field.key, label=field.prompt, secret=field.secret)
            for field in integration.fields
        ],
        integration=integration,
    )


def create_dashboard(
    url: str,
    integrations: list[Integration],
    steps: list[SetupStep[EnvScope]] | None = None,
) -> FastAPI:
    """Build the local app over the canonical setup registry.

    The page writes environment values, including the fields declared secret,
    so it keeps the supervisor's posture rather than a weaker one: what a
    surface is worth attacking is decided by what it writes, and this one
    writes the user's credentials.
    """
    declared = [integration_step(each) for each in integrations] + (steps or [])
    return create_wizard_app(
        url,
        title="Lup setup",
        lede="What this project has to be configured with, and where each stands.",
        wizard=Wizard(declared),
        resolve=lambda name: EnvScope() if name == ENV_SCOPE else None,
        choices=lambda: [
            ScopeChoice(name=ENV_SCOPE, label="This project", chosen=True)
        ],
    )


def create_dashboard_app(
    integrations: list[Integration],
    default_port: int = DASHBOARD_PORT,
    steps: list[SetupStep[EnvScope]] | None = None,
) -> typer.Typer:
    """Build the dashboard command over a project's declared integrations.

    ``steps`` is where a project puts a flow the registry cannot describe — one
    that verifies a secret against the service before recording it, or creates
    something with it afterwards. They are drawn after the declared ones, which
    is the order somebody meets them in.
    """
    app = typer.Typer(
        help="Host the local setup dashboard",
        invoke_without_command=True,
        no_args_is_help=False,
    )

    @app.callback(invoke_without_command=True)
    def serve_dashboard(
        context: typer.Context,
        host: Annotated[str, typer.Option(help="Interface to bind")] = "127.0.0.1",
        port: Annotated[int, typer.Option(help="TCP port to bind")] = default_port,
        open_page: Annotated[
            bool,
            typer.Option("--open/--no-open", help="Open the dashboard in a browser"),
        ] = True,
    ) -> None:
        """Run the setup dashboard from the same registry as the CLI wizard."""
        if context.invoked_subcommand is not None:
            return
        build: Callable[[str], FastAPI] = partial(
            create_dashboard, integrations=integrations, steps=steps or []
        )
        try:
            serve_local_page(build, "Lup setup dashboard", host, port, open_page)
        except ValueError as error:
            raise typer.BadParameter(str(error)) from error

    return app
