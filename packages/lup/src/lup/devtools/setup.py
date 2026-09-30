"""Reusable setup-wizard framework for project integrations.

Walks a project's declared integrations, prompting for what each one
needs, writing the answers to ``.env.local`` — or, for an integration
declared ``host_only``, to the project's host store under the person's lup
config, which no session reads — and reporting what is already configured.

Nothing here names a service: an application declares its own
``Integration`` list and composes the command tree with
:func:`create_setup_app`. Most integrations are pure data — name, command
slug, env keys, intro text, and the ``PromptField`` list to prompt for —
and their subcommand is generated from that. A bespoke flow (OAuth files,
detection, validation) supplies ``setup_func`` instead, and ``status_func``
overrides the display when env-key presence isn't the whole story.
"""

import os
import webbrowser
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Annotated

import typer
from dotenv import dotenv_values, set_key, unset_key
from pydantic import BaseModel, Field
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from lup.devtools.harness.composition import claude_profile_directory
from lup.devtools.harness.profile_app import create_profile_app
from lup.harness.environment import inside_a_container
from lup.launch.secrets import HostSecrets
from lup.providers.profiles import ProfileDirectory
from lup.types import EnvVars
from lup.workspace.paths import project_root

console = Console()


class IntegrationStatus(BaseModel):
    """Whether an integration is configured, with the human-readable detail."""

    ok: bool
    detail: str


PROJECT_ROOT = project_root()
ENV_LOCAL = PROJECT_ROOT / ".env.local"


# =====================================================================
# .env.local helpers
# =====================================================================


def read_env_local() -> EnvVars:
    """Read .env.local values (empty dict when the file is missing).

    ``dotenv`` owns the parse — the same parser pydantic-settings reads the
    file with at runtime, so the wizard sees exactly the values the app sees.
    """
    if not ENV_LOCAL.exists():
        return {}
    return {k: v for k, v in dotenv_values(ENV_LOCAL).items() if v is not None}


def write_env_local(values: EnvVars) -> None:
    """Update keys in .env.local, preserving existing lines, comments, order.

    ``dotenv.set_key`` rewrites a ``KEY=...`` line in place and appends
    missing keys, leaving comments, blank lines, and ordering untouched —
    the write half pydantic-settings does not provide.
    """
    if not values:
        return

    ENV_LOCAL.touch()
    for key, value in values.items():
        set_key(ENV_LOCAL, key, value, quote_mode="never")


def clear_env_file(path: Path, keys: Iterable[str]) -> None:
    """Drop keys from an env file. Idempotent — a no-op for keys it does not hold.

    The inverse of :func:`write_env_local`, preserving what that preserves:
    ``dotenv.unset_key`` removes one ``KEY=...`` line and leaves the comments,
    blank lines, and ordering around it alone. The path is a parameter because
    an application may keep more than one env file — one per profile, say — and
    only it knows which of them a reset is aimed at.
    """
    if not path.exists():
        return
    for key in keys:
        unset_key(path, key)


def host_store() -> HostSecrets:
    """This project's host store, which every worktree of it shares."""
    return HostSecrets.for_checkout(PROJECT_ROOT)


def refused_on_the_host_only(command: str) -> str:
    """Why a host-only write is refused here, naming the host command; empty on the host.

    Asked before anything is typed: a secret entered inside a container
    would land in the container's own configuration, where no companion on
    the host reads it and the session can.
    """
    # lup: ignore[os-environ] — the process's own placement hint is what is read
    if not inside_a_container(dict(os.environ)):
        return ""
    return (
        "This runs inside a lup container, where a host-only secret would land "
        "in the container rather than in the host store. Set it from a "
        f"terminal on the host: `uv run lup-devtools setup {command}`."
    )


def mask(value: str, show: int = 6) -> str:
    """Partially reveal a secret for recognition in the status table.

    Shows the first ``show`` characters so a human can tell *which* token
    is configured at a glance, then hides the rest. This is deliberately
    not :class:`pydantic.SecretStr`, which masks every character — that
    prevents leakage but also makes two different tokens indistinguishable
    in the status display.
    """
    if len(value) <= show:
        return value
    return value[:show] + "..." + "*" * min(8, len(value) - show)


def open_browser(url: str) -> None:
    """Open a URL in the default browser, with fallback message."""
    console.print(f"  Opening [link={url}]{url}[/link]")
    try:
        webbrowser.open(url)
    except (webbrowser.Error, OSError):
        console.print(f"  [dim]Could not open browser. Go to: {url}[/dim]")


# =====================================================================
# Integration registry
# =====================================================================


class PromptField(BaseModel):
    """One env var the wizard prompts for inside a token-based setup."""

    key: str = Field(description="Env var name, e.g. 'SLACK_BOT_TOKEN'")
    prompt: str = Field(description="Prompt label shown to the user")
    secret: bool = Field(
        default=True,
        description="Hide the current value as the prompt default (true for tokens)",
    )
    parse: Callable[[str], object] | None = Field(
        default=None,
        description="Validator called on the entered value; rejects on raise",
    )


class Integration(BaseModel):
    """A single integration the setup wizard can configure.

    Most integrations are *token-based*: they print instructions, maybe
    open a browser, then prompt for a handful of env vars. Those are
    described declaratively via ``intro``/``browser_url``/``fields`` and
    run by :meth:`run` — adding one is filling in this shape, not copying
    a function. Integrations with bespoke flows (OAuth file handling,
    timezone detection) instead supply ``setup_func``.
    """

    name: str = Field(description="Display name (e.g., 'Slack', 'Google')")
    command: str = Field(description="Subcommand slug, e.g. 'slack' or 'api-key'")
    help: str = Field(description="One-line help for the subcommand")
    env_keys: list[str] = Field(description="Env vars to check for status display")
    intro: str | None = Field(
        default=None, description="Instructions printed before prompting"
    )
    browser_url: str | None = Field(
        default=None, description="URL to open while the user follows the intro"
    )
    fields: list[PromptField] = Field(
        default=[], description="Env vars to prompt for, in order"
    )
    setup_func: Callable[[], EnvVars] | None = Field(
        default=None,
        description="Bespoke interactive flow, used instead of the declarative fields",
    )
    status_func: Callable[[EnvVars], IntegrationStatus] | None = Field(
        default=None,
        description="Custom status checker (default: checks env_keys)",
    )
    host_only: bool = Field(
        default=False,
        description=(
            "Keep this integration's keys in the project's host store rather "
            "than .env.local: for a key a host companion is started with and "
            "a session must never hold"
        ),
    )

    def stored(self) -> EnvVars:
        """What this integration's keys are answered from: the host store, or .env.local."""
        return host_store().read() if self.host_only else read_env_local()

    def save(self, values: EnvVars) -> str:
        """Write this integration's answers where it keeps them, answering where that is."""
        if self.host_only:
            store = host_store()
            store.write(values)
            return f"the host store ({store.path()})"
        write_env_local(values)
        return ".env.local"

    def refused_here(self) -> str:
        """Why this integration cannot be answered in this process; empty where it can."""
        return refused_on_the_host_only(self.command) if self.host_only else ""

    def run(self) -> EnvVars:
        """Run the setup flow and return env vars to write."""
        if self.setup_func is not None:
            return self.setup_func()
        return self.run_prompts()

    def run_prompts(self) -> EnvVars:
        """Standard token flow: header, reconfigure check, intro, prompts."""
        console.print()
        console.rule(f"[bold]{self.name}[/]")
        console.print()

        env = self.stored()
        # Gate re-entry only for secrets, which can't be shown as defaults;
        # non-secret fields echo their current value, so re-walking is cheap.
        guards_secret = any(f.secret for f in self.fields)
        configured = all(env.get(k) for k in self.env_keys)
        if guards_secret and configured:
            console.print("[green]Already configured.[/]")
            if not typer.confirm("Reconfigure?", default=False):
                return {}

        if self.intro:
            console.print(self.intro)
        if self.browser_url:
            open_browser(self.browser_url)
        console.print()

        values: EnvVars = {}
        for field in self.fields:
            current = env.get(field.key, "")
            raw = typer.prompt(
                field.prompt,
                default=current,
                show_default=bool(current) and not field.secret,
            ).strip()
            if not raw:
                continue
            if field.parse is not None:
                try:
                    field.parse(raw)
                except ValueError:
                    console.print(f"  [yellow]Skipping {field.key}: invalid value[/]")
                    continue
            values[field.key] = raw
        return values

    def check_status(self, env: EnvVars) -> IntegrationStatus:
        """Whether this integration is configured, and the detail to show.

        An integration declaring no env keys cannot be judged from the
        environment at all — it configures a file, a download, a roster. It
        says so rather than being read as configured: ``all([])`` is true, so
        the obvious spelling reports every such integration green and then
        raises reaching for a value that is not there.
        """
        if self.status_func:
            return self.status_func(env)
        if not self.env_keys:
            return IntegrationStatus(ok=False, detail="nothing in the environment says")
        values = [env.get(k, "") for k in self.env_keys]
        if all(values):
            return IntegrationStatus(ok=True, detail=mask(values[0]))
        return IntegrationStatus(ok=False, detail="not configured")


def build_status_table(integrations: list[Integration]) -> Table:
    """Build a rich table showing configuration status, each from where it is kept."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Status", width=3)
    table.add_column("Integration", min_width=30)
    table.add_column("Detail", style="dim")
    for integration in integrations:
        status = integration.check_status(integration.stored())
        status_text = "[green]OK[/]" if status.ok else "[red]--[/]"
        table.add_row(status_text, integration.name, status.detail)
    return table


def make_setup_command(integration: Integration) -> Callable[[], None]:
    """Build a zero-argument command that runs one integration's setup."""

    def run_one() -> None:
        refused = integration.refused_here()
        if refused:
            typer.echo(refused, err=True)
            raise typer.Exit(1)
        values = integration.run()
        if values:
            console.print(f"[green]Saved to {integration.save(values)}[/]")

    return run_one


def create_setup_app(
    integrations: list[Integration],
    profiles: ProfileDirectory | None = None,
) -> typer.Typer:
    """Build the setup command tree over a project's declared integrations.

    Setup curates exactly the roster a launch selects from: ``profiles``
    where a project names an origin of its own, the person's otherwise.
    """
    directory = profiles or claude_profile_directory()
    app = typer.Typer(
        help="Interactive setup wizard",
        pretty_exceptions_show_locals=False,
        invoke_without_command=True,
    )
    app.add_typer(create_profile_app(directory), name="profile")
    # The setup page is this wizard seen through a browser — the same declared
    # integrations rendered for somebody who would rather click than answer
    # prompts — and the dashboard shows it as this repository's pane, by
    # running this command in a checkout of it: only this repository's own
    # CLI knows its integrations. Hidden, since the pane is how it is reached.
    # Imported where it is mounted, because serving it is the `web` extra and
    # a module-level import would make that extra a requirement of running
    # `setup` at all.
    from lup.devtools.dashboard.setup_page import create_setup_page_app

    app.add_typer(
        create_setup_page_app(integrations),
        name="serve",
        hidden=True,
        help="Serve this repository's setup page, the dashboard's setup pane",
    )

    @app.command("status")
    def status() -> None:
        """Show current integration status."""
        console.print()
        console.print(build_status_table(integrations))
        active = profiles.active() if profiles is not None else None
        if active is not None:
            console.print(f"  Active profile: [bold]{active.name}[/] ({active.scope})")
        console.print()

    for integration in integrations:
        app.command(integration.command, help=integration.help)(
            make_setup_command(integration)
        )

    @app.command("secret")
    def secret(
        key: Annotated[
            str, typer.Argument(help="The variable, such as GEMINI_API_KEY")
        ],
        unset: Annotated[
            bool, typer.Option("--unset", help="Remove it from the host store instead")
        ] = False,
    ) -> None:
        """Set a key in this project's host store, which only host companions are handed.

        For a key no integration declares. The store is outside every
        checkout, under your lup config, and no session holds what it keeps.
        """
        refused = refused_on_the_host_only(f"secret {key}")
        if refused:
            typer.echo(refused, err=True)
            raise typer.Exit(1)
        store = host_store()
        if unset:
            store.clear([key])
            console.print(f"[green]Removed {key} from {store.path()}[/]")
            return
        value = typer.prompt(key, hide_input=True).strip()
        if not value:
            console.print("[yellow]Nothing entered; nothing saved.[/]")
            return
        store.write({key: value})
        console.print(f"[green]Saved {key} to {store.path()}[/]")

    @app.callback(invoke_without_command=True)
    def main(context: typer.Context) -> None:
        """Walk through all integrations, skipping configured entries."""
        if context.invoked_subcommand is not None:
            return
        console.print()
        console.print(
            Panel(
                "[bold]Project setup[/]\n"
                "\n"
                "Walking through all integrations.\n"
                "Press [bold]Ctrl+C[/] to exit, [bold]Enter[/] to skip a field.",
                expand=False,
            )
        )
        console.print(build_status_table(integrations))
        for integration in integrations:
            refused = integration.refused_here()
            if refused:
                typer.echo(f"{integration.name}: {refused}", err=True)
                continue
            values = integration.run()
            if values:
                integration.save(values)
        console.print()
        console.rule("[bold green]Setup complete[/]")
        console.print()
        console.print(build_status_table(integrations))
        console.print()

    return app
