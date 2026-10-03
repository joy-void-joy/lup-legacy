"""Import-safe dispatch for a project's composed ``lup-devtools`` CLI.

Conflict repair and pending-migration reports must run while project source
cannot import, while a session's status line runs at every render and the
runner every installed git hook calls runs at every commit, where loading
the whole application for a line of status or a moment with nothing to
judge would cost seconds. The console script recognizes those routes and
builds only their library-owned command trees. Every other command loads
the project's full Typer application from installed package metadata.

The metadata is materialized when the environment is synced, so reading it
does not parse a currently conflicted ``pyproject.toml``.
"""

from importlib.metadata import entry_points
from pathlib import Path
import sys
import traceback
from typing import Annotated

import typer
from typer.main import get_command


def project_application() -> typer.Typer:
    """Load the one project application registered for this environment."""
    match list(entry_points(group="lup.devtools", name="application")):
        case [registered]:
            application = registered.load()
        case []:
            typer.echo(
                "No installed distribution registers a 'lup.devtools' application, "
                "so there is no project CLI to run: `uv sync` in the project "
                "installs it.",
                err=True,
            )
            raise typer.Exit(1)
        case registrations:
            named = "\n".join(
                f"  {entry.value}, from {entry.dist.name} in {entry.dist.locate_file('')}"
                if entry.dist
                else f"  {entry.value}"
                for entry in registrations
            )
            typer.echo(
                "More than one distribution registers a 'lup.devtools' application, "
                f"where a project's environment holds one:\n{named}\n"
                "The one the project no longer declares, its package under a name it "
                "had before a rename, is one of two leftovers. In the package "
                "source's folder, it is a build's `<name>.egg-info`, which the "
                "editable install reads: delete that folder. In the environment, "
                "`uv run` kept it, since it only adds: `uv sync --reinstall-package "
                "<the project's name>` removes it and keeps the `lup-devtools` "
                "script both installed, which a plain `uv sync` removes with it.",
                err=True,
            )
            raise typer.Exit(1)

    if not isinstance(application, typer.Typer):
        typer.echo(
            "The 'lup.devtools' application entry point must resolve to a "
            "Typer application.",
            err=True,
        )
        raise typer.Exit(1)
    return application


def conflict_application() -> typer.Typer:
    """Build only the library modules needed to repair a conflicted tree."""
    from lup.devtools.dev import conflicts
    from lup.devtools.dev.conflict_app import create_conflict_app

    root_app = typer.Typer(
        help="lup-devtools: conflict-safe repair commands",
        pretty_exceptions_show_locals=False,
        no_args_is_help=True,
    )
    git_app = typer.Typer(no_args_is_help=True)
    git_app.add_typer(
        create_conflict_app(),
        name="conflict",
        help="Merge/rebase conflict resolution",
    )
    root_app.add_typer(
        git_app,
        name="git",
        help="Conflict-safe git repair",
    )
    root_app.callback()(conflicts.report_conflicted_manifest)

    return root_app


def migration_application() -> typer.Typer:
    """Read installed migrations independently of the project's source state."""
    from lup.devtools.dev.migrations import migrate_pending_cmd

    root_app = typer.Typer(
        help="lup-devtools: import-safe migration reports",
        pretty_exceptions_show_locals=False,
        no_args_is_help=True,
    )
    dev_app = typer.Typer(no_args_is_help=True)
    migrate_app = typer.Typer(no_args_is_help=True)
    migrate_app.command("pending")(migrate_pending_cmd)
    dev_app.add_typer(migrate_app, name="migrate", help="Library migration reports")
    root_app.add_typer(dev_app, name="dev", help="Development tools")
    return root_app


def changelog_merge_application() -> typer.Typer:
    """Build only the changelog merge git runs as a merge driver, mid-merge.

    Git runs it once per merge that touches the changelog on both sides, with
    the project in whatever state the merge has reached, so it loads nothing
    of the project and pays for nothing past the changelog's own module.
    """
    from lup.devtools.changelog import merge_changelog_cmd

    root_app = typer.Typer(
        help="lup-devtools: the changelog's merge driver",
        pretty_exceptions_show_locals=False,
        no_args_is_help=True,
    )
    git_app = typer.Typer(no_args_is_help=True)
    git_app.command("merge-changelog")(merge_changelog_cmd)
    root_app.add_typer(git_app, name="git", help="The git command tree")
    return root_app


def dashboard_line(pulse: Path) -> None:
    """Print a session's status line from the dashboard's pulse and its runtime's input, loading nothing else.

    The route `dashboard line <pulse>` takes where a status line runs it,
    answered as the project's own `dashboard line` answers it: in colour,
    which the runtime paints though it captures what the command prints.
    """
    # lup: defer: most of what this route still costs (~0.3 s measured) is
    # `import lup`, whose front door binds the session vocabulary on load
    # (~0.22 s, `lup.sessions.events` the bulk); a status line re-run every
    # few seconds pays it each time, in every session
    from lup.devtools.dashboard.pulse import answered

    typer.echo(answered(pulse, sys.stdin), color=True)


def in_process(arguments: tuple[str, ...]) -> int:
    """Run one ``lup-devtools`` invocation in this process, answering its status.

    How a hook's guards that are devtools commands run: the project's
    application loads on the first of them and each is a call into it,
    rather than a process paying for its own load.
    """
    return invoked(project_application(), arguments)


def invoked(application: typer.Typer, arguments: tuple[str, ...]) -> int:
    """Run one invocation of ``application`` in this process, answering its status.

    It ends as its own process would have, because it is run the way its own
    process runs it: standalone, so the CLI shows its own usage errors and
    ends every invocation in the exit its process would take, which is caught
    here rather than taken. Anything it leaves uncaught is printed whole and
    answered as 1, which a hook reports as that guard's refusal rather than
    dying before it can say which guard it was.
    """
    command = get_command(application)
    try:
        command.main(args=list(arguments), prog_name="lup-devtools")
    except SystemExit as ended:
        match ended.code:
            case int(code):
                return code
            case None:
                return 0
            case said:
                typer.echo(said, err=True)
                return 1
    except Exception:
        typer.echo(traceback.format_exc(), err=True)
        return 1
    return 0


def hook_route() -> typer.Typer | None:
    """`git hooks run` over the guards this checkout compiled, None where it has none.

    The route every installed hook takes. The guards compiled into the
    checkout's manifest are judged against the moment here, before anything
    of the project loads, so a moment where each stands down — a plain
    commit's settle, a deletion-only push — ends in the time this module and
    the guards' own take to import. A guard left standing that is a devtools
    command loads the application then, once, through :func:`in_process`.
    None sends the moment to the application's own `git hooks run`, which
    reads the declaration itself.
    """
    from lup.devtools.dev.git_guards import compiled_guards, fire

    guards = compiled_guards(Path.cwd())
    if guards is None:
        return None
    root_app = typer.Typer(
        help="lup-devtools: the runner every installed git hook calls",
        pretty_exceptions_show_locals=False,
        no_args_is_help=True,
    )
    git_app = typer.Typer(no_args_is_help=True)
    hooks_app = typer.Typer(no_args_is_help=True)
    root_app.add_typer(git_app, name="git", help="The git command tree")
    git_app.add_typer(hooks_app, name="hooks", help="The installed git hooks")

    @hooks_app.command("run")
    def run(
        hook: Annotated[str, typer.Argument(help="The git hook that fired")],
        arguments: Annotated[
            list[str] | None, typer.Argument(help="What git passed the hook")
        ] = None,
    ) -> None:
        """Run the guards this checkout compiled at one git hook."""
        status = fire(
            guards, hook, tuple(arguments or ()), Path.cwd(), sys.stdin, in_process
        )
        raise typer.Exit(status)

    return root_app


def main() -> None:
    """Dispatch import-safe library routes before the project's application."""
    match sys.argv:
        case [_, "git", "conflict", *_]:
            conflict_application()()
        case [_, "dev", "migrate", "pending", *_]:
            migration_application()()
        case [_, "git", "merge-changelog", *_]:
            changelog_merge_application()()
        case [_, "dashboard", "line", pulse] if not pulse.startswith("-"):
            dashboard_line(Path(pulse))
        case [_, "git", "hooks", "run", *_] if (route := hook_route()) is not None:
            route()
        case _:
            project_application()()
