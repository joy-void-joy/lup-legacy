"""Whose environment a sync is about to write, asked before it writes.

`uv` puts a project's environment where ``UV_PROJECT_ENVIRONMENT`` says, and
an absolute value says *the same directory* for every project on the machine
-- the variable is one string, `uv` interpolates nothing into it, and no
setting in `pyproject.toml` names an environment path instead. `uv sync`
then does exactly what it documents: it makes the environment match the
project, installing that project's dependencies and the project itself, and
uninstalling everything else it finds.

Two projects sharing one absolute value is therefore not a race and not a
bug in `uv`. It is the declared configuration, working. What makes it
expensive is that nothing says so: the sync succeeds, and the other project
finds out later, in a checkout nobody touched, as an import error.

So this asks first. An environment answers for itself -- ``direct_url.json``
is written by every installer and records where a distribution was installed
*from*, with ``dir_info.editable`` saying it is still read from there -- so
there is nothing to stamp, nothing to keep in step, and the reading is
correct for an environment this project has never touched.

What it deliberately does not do is move anybody's environment. Where one
goes is `uv`'s to decide from a variable its owner set, and a tool that
quietly relocated it would leave the shell's value pointing somewhere
nothing writes any more. The remedy belongs to whoever set the variable, and
this says which value did it.
"""

import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

import typer

from lup.devtools.dev.worktree import sync_dependencies
from lup.devtools.launcher import ENVIRONMENT_VARIABLE
from lup.diagnostics import refuse
from lup.policy.assets.host import project_environment
from lup.policy.kernel.diagnostic import devtools, spelled, step
from lup.workspace.paths import project_root


def installed_from(
    environment: Path,
    layouts: tuple[str, ...] = ("lib/python*/site-packages", "Lib/site-packages"),
) -> list[Path]:
    """Every project this environment holds an editable install of.

    Editable alone, because that is what names a checkout. A wheel installed
    from an index records the index, which says nothing about who owns the
    environment; an editable install records the directory its code is still
    being read out of, which is exactly the claim being weighed.

    An unreadable or unparseable record is skipped rather than raised on. The
    question is who else is in here, and a record nobody can read answers it
    no more than a missing one does -- while failing over it would put a
    corrupt file between a project and its own environment.

    ``layouts`` carries both spellings rather than the POSIX one alone, for
    the same reason ``console_script`` asks the running interpreter whether
    its scripts sit in ``bin`` or ``Scripts``: where an environment keeps
    installed distributions is a property of how Python is installed, and a
    reader that knew one spelling would report an environment as empty
    rather than as unreadable. A default rather than a constant, so a layout
    neither of these names is answered by the caller that has one.
    """

    def declared(record: Path) -> Path | None:
        """The checkout one record names, or nothing when it names none."""
        try:
            parsed = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        match parsed:
            case {"url": str(url), "dir_info": {"editable": True}}:
                return Path(unquote(urlsplit(url).path))
        return None

    found = [
        source
        for pattern in layouts
        for record in sorted(environment.glob(f"{pattern}/*.dist-info/direct_url.json"))
        if (source := declared(record)) is not None
    ]
    return list(dict.fromkeys(found))


def foreign_installs(root: Path, environment: Path) -> list[Path]:
    """The projects other than *root* whose editable installs occupy it.

    Containment rather than equality, because a workspace member is its own
    distribution installed from its own directory: this checkout puts both
    itself and ``packages/lup`` in there, and reading the second as somebody
    else's would report every correctly-synced environment as borrowed.
    """
    return [
        source
        for source in installed_from(environment)
        if source != root and root not in source.parents
    ]


def environment_status(root: Path | None = None) -> None:
    """Report where this project's environment is and who is installed in it."""
    where = project_root() if root is None else root
    environment = project_environment(where)
    typer.echo(f"project:     {where}")
    typer.echo(f"environment: {environment}")
    if not environment.is_dir():
        typer.echo(
            "             not built yet — run "
            f"`{spelled(devtools('dev', 'env', 'sync'))}`"
        )
        return
    installed = installed_from(environment)
    for source in installed:
        owned = source == where or where in source.parents
        typer.echo(f"  {'ours   ' if owned else 'foreign'} {source}")
    if not installed:
        typer.echo("  (no editable install records — nothing claims it)")


def sync_environment(take_over: bool = False, root: Path | None = None) -> None:
    """Sync this project's environment, refusing to take over somebody else's.

    The reason to reach for this rather than `uv sync` directly: the same
    install, with the one question `uv` has no way to ask asked first.

    ``take_over`` is the deliberate spelling of the act this refuses, and it
    exists because the refusal would otherwise be a wall rather than a
    question. Somebody moving between two projects that share an environment
    is doing exactly what the configuration says to do, and has to be able to
    say so without editing their shell mid-task.

    The refusal names the variable as well as the directory, because the
    directory is the symptom and the variable is the decision. Whoever reads
    it can act on the second and can only be puzzled by the first.
    """
    where = project_root() if root is None else root
    environment = project_environment(where)
    owners = foreign_installs(where, environment) if environment.is_dir() else []
    if owners and not take_over:
        refuse(
            "currently holds another project, "
            + ", ".join(str(owner) for owner in owners)
            + f", and syncing {where} into it would uninstall theirs, which is "
            "what uv sync means rather than a failure it could report",
            what=str(environment),
            steps=[
                step(
                    f"unset {ENVIRONMENT_VARIABLE}, whose absolute value names one "
                    "directory for every project on this machine, so each checkout "
                    "gets its own .venv"
                ),
                step(
                    f"or give {ENVIRONMENT_VARIABLE} a relative value, which uv "
                    "resolves against whichever project it is running in"
                ),
                step(
                    "or sync anyway, uninstalling theirs",
                    devtools("dev", "env", "sync", "--take-over"),
                ),
            ],
        )
    if owners:
        typer.echo(f"Taking {environment} over from {len(owners)} other project(s).")
    typer.echo(f"Syncing {environment}...")
    sync_dependencies(where)
    typer.echo("Done.")
