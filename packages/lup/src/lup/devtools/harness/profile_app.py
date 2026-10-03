"""The command tree over the registries that hold a project's runtime accounts.

Mounted wherever a project already talks about profiles — beside the native
launchers as ``harness profile``, or inside a setup wizard — so the roster a
launch selects from is curated in one vocabulary no matter which tree the
caller reached it through.

Every command acts on this checkout's own profiles unless ``--global`` names
the ones every checkout shares, the way ``git config`` does.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from lup.devtools.harness.launch import switch_repository_login
from lup.providers.harness import AdapterName
from lup.providers.profile_migration import migrate_profiles
from lup.providers.profiles import Profile, ProfileDirectory, ProfileScope
from lup.providers.user_config import UserConfigFile
from lup.workspace.paths import project_root

GLOBAL_OPT = Annotated[
    bool,
    typer.Option(
        "--global",
        help="Act on the global profiles every checkout shares, beside your "
        "lup config, instead of this checkout's own",
    ),
]


def create_profile_app(directory: ProfileDirectory) -> typer.Typer:
    """Wire the profile command tree over one project's profile registries."""
    app = typer.Typer(
        no_args_is_help=True,
        help="Inspect and curate the accounts a launch can select",
    )

    def acting[Answer](act: Callable[[], Answer]) -> Answer:
        """Answer for what an origin refuses, rather than tracebacking.

        Every refusal arrives already worded: :class:`UnknownProfile` carries
        the roster a launcher reports the same way, :class:`DefaultHomeProfile`
        refuses a profile naming the default home in the words a launch
        refuses it in, and an origin that derives its profiles
        from something else — a directory the project keeps, rather than a
        registry of its own — cannot honour every curation the tree offers
        and says so with a ``ValueError`` whose message is the explanation.
        Rendering theirs is what keeps this tree and the launcher from wording
        the same refusal two ways.
        """
        try:
            return act()
        except (KeyError, ValueError) as error:
            raise typer.BadParameter(str(error)) from error

    def scope(shared: bool) -> ProfileScope:
        """The registry a command acts on: this checkout's unless ``--global``."""
        return "global" if shared else "local"

    def shadowing(entry: Profile) -> str:
        """What answers for this entry's name here, where it is not this entry."""
        if entry.resolved:
            return ""
        winner = directory.profile(entry.name)
        return f"; shadowed by the {winner.scope} {winner.name}"

    @app.command("list")
    def list_command() -> None:
        """Show every profile, local and global, and which one a launch selects.

        ``*`` marks the profile a launch naming none opens. A name kept both
        locally and globally resolves to the local one in this checkout, and
        the global entry says it is shadowed.
        """
        entries = directory.entries()
        if not entries:
            typer.echo("No profiles yet — add one with `profile add`")
            return
        for entry in entries:
            selected = "*" if entry.active else " "
            login = "logged in" if entry.logged_in else "no login yet"
            typer.echo(
                f"{selected} {entry.name}  {entry.scope}  {entry.config_dir}"
                f"  ({login}{shadowing(entry)})"
            )

    @app.command("add")
    def add_command(
        name: Annotated[str, typer.Argument(help="Name for the account")],
        shared: GLOBAL_OPT = False,
        config_dir: Annotated[
            Path | None,
            typer.Option(
                "--config-dir",
                help="Configuration home to register, instead of the one this "
                "project would keep for that name; never the runtime's default "
                "home, which naming no profile already selects",
            ),
        ] = None,
    ) -> None:
        """Register a runtime configuration home under a name, in this checkout."""
        entry = acting(lambda: directory.add(name, config_dir, scope(shared)))
        typer.echo(f"Added {entry.scope} {entry.name}: {entry.config_dir}")
        if not entry.resolved:
            typer.echo(f"Not used here{shadowing(entry)}")
        if not entry.logged_in:
            typer.echo(
                "No login there yet — sign one in by starting the runtime with "
                f"{directory.login.config_home_env}={entry.config_dir}"
            )

    @app.command("use")
    def use_command(
        name: Annotated[str, typer.Argument(help="Profile to select")],
        shared: GLOBAL_OPT = False,
    ) -> None:
        """Select the profile a launch uses when none is named, in this checkout.

        Recorded in this checkout's ``.lup/profiles/.active``, which overrides
        the global selection here; ``--global`` records it as the ``profile``
        in your lup config instead, for every checkout without its own.
        """
        entry = acting(lambda: directory.use(name, scope(shared)))
        reach = "every checkout" if shared else "this checkout"
        typer.echo(
            f"Selected {entry.name} for {reach}: here it opens the {entry.scope} "
            f"{entry.config_dir}"
        )
        if not entry.active:
            typer.echo(
                f"This checkout's own selection, {directory.active_name()}, "
                "still answers here"
            )

    @app.command("remove")
    def remove_command(
        name: Annotated[str, typer.Argument(help="Profile to forget")],
        shared: GLOBAL_OPT = False,
    ) -> None:
        """Forget a profile in this checkout, leaving its configuration home on disk."""
        entry = acting(lambda: directory.remove(name, scope(shared)))
        typer.echo(
            f"Removed {entry.scope} {entry.name} — left {entry.config_dir} on disk"
        )

    @app.command("migrate")
    def migrate_command(
        checkout: Annotated[
            Path | None,
            typer.Option(
                "--checkout",
                help="Checkout whose .lup/profiles to move; default: this one",
            ),
        ] = None,
    ) -> None:
        """Move this checkout's profiles, and the ~/.lup registry's, to global.

        Optional: a checkout's own profiles keep working where they are, and
        moving one shares its login with every checkout instead. The personal
        ``~/.lup/profiles.json`` registry is read only by this command, so its
        accounts reach a launch only once moved.
        """
        migration = migrate_profiles(checkout or project_root(), UserConfigFile())
        for line in migration.lines():
            typer.echo(line)

    @app.command("switch")
    def switch_command(
        name: Annotated[str, typer.Argument(help="Profile to move the sessions onto")],
        runtime: Annotated[
            AdapterName,
            typer.Option("--runtime", help="Whose sessions move: claude or codex"),
        ] = AdapterName.CLAUDE,
    ) -> None:
        """Move this repository's contained sessions of one runtime onto a profile's login.

        Hands the profile's login to the container volume every contained
        session of this repository shares, through the image's own seed
        program. A Claude session takes it at its next request; a Codex
        session keeps its own until it is opened again, and the command that
        reopens it is printed. A host session runs in its own account's home,
        which no volume reaches, so it is answered with the command opening it
        again on the profile. The launch that would otherwise refuse to move
        running sessions — `harness claude|codex` without `--move-sessions` —
        finds the volume on this profile already.
        """
        outcome = acting(
            lambda: switch_repository_login(
                project_root(),
                runtime,
                name,
                directory if directory.login.state_volume == runtime else None,
            )
        )
        for line in outcome.lines():
            typer.echo(line)
        if outcome.held is None:
            raise typer.Exit(1)

    return app
