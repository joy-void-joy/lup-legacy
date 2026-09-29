"""What only a template adds to the `dev` tree the library already builds.

The workflow commands — the quality gate, review markers, reading the
repository — are the library's, wired over :func:`declared` by the roster
every project inherits.
The one tree added here has template-ness as its subject: renaming the package
an adopter inherits, dropping the demonstrations the scaffold ships of itself,
pointing the lup registration it ships at the template it was generated from,
and naming the commit of that template it was stamped from. They are about
what a project was made from rather than what it does, which is why they are
mounted onto the inherited tree rather than replacing it — a project that
replaced `dev` to add four commands would be restating every argument the
library's own tree takes, which is the drift the roster removes.
"""

from typing import Annotated

import typer

import lup_template.devtools.dev.init as init
import lup.devtools.dev.origin as origin
import lup_template.harness.catalog as catalog
from lup.devtools.dev.declarations import DevDeclarations
from lup.workspace.paths import project_root


def declared() -> DevDeclarations:
    """What this repository tells the dev tree, read where a command runs.

    Every declaration is the catalog's, the test roots included: the policy
    derives the test role from the same list, so the suites the gate runs and
    the files the policy judges as tests are one declaration read twice.
    """
    return DevDeclarations(
        project=catalog.dev_project(),
        hooks=catalog.declared_hook_set(),
        plugin=catalog.declared_plugin(),
        test_roots=catalog.declared_test_roots(),
        spread=catalog.declared_spread(),
        scaffold=catalog.declared_scaffold(),
        release=catalog.declared_release(),
    )


init_app = typer.Typer(no_args_is_help=True)


def extend(app: typer.Typer) -> None:
    """Mount this template's own tree onto the inherited `dev` app."""
    app.add_typer(init_app, name="init", help="Project initialization")


# -- init commands --


@init_app.command("rename-package")
def init_rename_package_cmd(
    new_name: Annotated[
        str,
        typer.Argument(
            help="New package name (valid Python identifier, e.g. 'aib', 'forecast_bot')"
        ),
    ],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", "-n", help="Show what would change without modifying files"
        ),
    ] = False,
) -> None:
    """Rename the lup Python package to a project-specific name."""
    init.rename_package(new_name, dry_run)


@init_app.command("drop-examples")
def init_drop_examples_cmd(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", "-n", help="Show what would change without modifying files"
        ),
    ] = False,
) -> None:
    """Remove the scaffold's demonstrations of itself, which no adopter wants.

    `examples/` composes lup's own runtime against lup's own README, and the
    test modules beside it drive it. A domain that adopted the template is a consumer of
    that library rather than a demonstrator of it, so what it inherits here is
    a directory it will never run and a suite it has to keep green.

    Lines still naming what went are reported rather than rewritten: a link in
    a README its human owner is already rewriting is theirs to remove. So are
    the removed test modules the scaffold declaration does not yet decline,
    which the next `dev update` would otherwise offer back as conflicts.
    """
    root = project_root()
    removed = init.drop_scaffold_demonstrations(root, dry_run)
    if removed:
        typer.echo("Would remove:" if dry_run else "Removed:")
        for line in removed:
            typer.echo(line)
        mentions = init.surviving_mentions(root, init.SCAFFOLD_DEMONSTRATIONS)
        if mentions:
            typer.echo(f"\nStill named in {len(mentions)} line(s) — review manually:")
            for line in mentions:
                typer.echo(line)
    else:
        typer.echo("no scaffold demonstrations left to remove")
    undeclined = init.undeclined_copies(
        init.SCAFFOLD_DEMONSTRATIONS,
        catalog.declared_scaffold(),
        catalog.dev_project().package,
    )
    if undeclined:
        typer.echo(
            "\nThe copied half still carries these; add them to `declined` in "
            "declared_scaffold(), or `dev update` offers each back as a conflict:"
        )
        for path in undeclined:
            typer.echo(f"  {path}")


@init_app.command("upstream")
def init_upstream_cmd(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", "-n", help="Show what would change without modifying files"
        ),
    ] = False,
) -> None:
    """Point the lup registration at the template this repository was generated from.

    `sync.json` ships naming lup's own repository, and a project generated
    from a fork of it builds on the fork. GitHub records which template a
    repository was generated from, so this asks once and writes the answer
    into the shipped registration -- or, where the project pins lup to a
    repository, prints the command that moves the pin, which the
    registration follows. Exits nonzero where anything is left to do.
    """
    project = catalog.declared_scaffold().project
    if not origin.point_at_template(project_root(), project, dry_run):
        raise typer.Exit(1)


@init_app.command("base")
def init_base_cmd() -> None:
    """Name the commit of lup this repository was stamped from: the base.

    Everything initialization settles about lup is taken at one commit -- the
    pin names a branch holding it, and the upstream checkpoint and the
    scaffold branch are rooted at it -- and this checkout's own branch does
    not always say which. A clone of lup carries lup's history, so the commit
    is the newest one it shares; a repository made with GitHub's "Use this
    template" carries only the tree GitHub copied into its one root commit,
    so the commit is the one of lup's that holds that tree. The registration
    is fetched to read lup's history, cloning it the first time. Exits
    nonzero where the base is an estimate or was not found.
    """
    project = catalog.declared_scaffold().project
    if not origin.report_base(project_root(), project):
        raise typer.Exit(1)
