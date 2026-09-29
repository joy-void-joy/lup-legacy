"""Import-safe dispatch for a project's composed ``lup-devtools`` CLI.

The conflict workflow repairs source that may not import. Its console script
therefore enters this library module, recognizes that one command family, and
builds only the library-owned repair tree. Every other command loads the
project's full Typer application from installed package metadata.

The metadata is materialized when the environment is synced, so reading it
does not parse a currently conflicted ``pyproject.toml``.
"""

from importlib.metadata import entry_points
import sys

import typer


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
    from lup.workspace.paths import find_nearest_pyproject

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

    @root_app.callback()
    def report_conflicted_manifest() -> None:
        """Name the launcher that remains available when the manifest is broken."""
        project_root = find_nearest_pyproject()
        if project_root is not None and conflicts.manifest_conflicted(project_root):
            typer.echo(conflicts.conflicted_manifest_notice(project_root), err=True)

    return root_app


def main() -> None:
    """Dispatch conflict repair without importing the project's application."""
    match sys.argv:
        case [_, "git", "conflict", *_]:
            conflict_application()()
        case _:
            project_application()()
