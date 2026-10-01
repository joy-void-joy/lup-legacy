"""The general report surface: everything left to implement, in one place.

Every other surface here answers one question — `dev comments` what is still
being asked, `harness check` whether the generated trees are current, `dev
branches` what has not landed, `resolve supervise` what one run is
doing. None of them answers "what is left", which is the question a session
ends on and the one a report is written to record.

So this composes them rather than recomputing any of it, and shares its shape
with the report skill: the skill writes what a session knows on top of what
this can see, into the same file, rewritten whole.

Examples::

    $ uv run lup-devtools dev report
    $ uv run lup-devtools dev report --json
    $ uv run lup-devtools dev report --write tmp/resolver_remaining.md
"""

from pathlib import Path
from typing import Annotated

import typer

from lup.devtools.harness.composition import NativeTargets
from lup.devtools.harness.drift import RepositoryWriter
from lup.devtools.report.build import authored_headings, build_report
from lup.devtools.report.models import DEFAULT_SCRATCH_ROOT, inside_scratch
from lup.devtools.supervisor.doors import resolve_state_root
from lup.devtools.utils import output_json, refuse
from lup.policy.kernel.diagnostic import devtools, step
from lup.workspace.paths import project_root


def create_report_app(
    native_targets: NativeTargets,
    repository_writers: list[RepositoryWriter],
    scratch_root: Path = DEFAULT_SCRATCH_ROOT,
) -> typer.Typer:
    """Wire the report surface over the artifacts one repository generates.

    The targets and writers are what only an application knows; the root every
    topic and the written path resolve against is found when the command runs,
    because a CLI is imported long before anyone knows where it is pointed. So
    a relative ``--write`` path lands in the tree being reported on rather than
    wherever the command was invoked from.
    """
    app = typer.Typer(invoke_without_command=True, no_args_is_help=False)

    @app.callback()
    def report_cmd(
        as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
        write: Annotated[
            Path | None,
            typer.Option(
                "--write",
                help=f"Rewrite this path under {scratch_root}/ with this report",
            ),
        ] = None,
        force: Annotated[
            bool,
            typer.Option(
                "--force", help="Replace a report carrying a session's own prose"
            ),
        ] = False,
    ) -> None:
        """Report everything left to implement, across every surface."""
        root = project_root()
        if write is not None and not inside_scratch(root, write, scratch_root):
            refuse(
                f"is outside {scratch_root}/, where a report has to be named: "
                "there it is gitignored, so it reaches no diff, no reviewer and no "
                "commit, and anywhere else it lands in the next commit as a "
                "tracking file",
                what=str(write),
                steps=[
                    step(
                        "name it after the work it covers",
                        devtools(
                            "dev", "report", "--write", f"{scratch_root}/<name>.md"
                        ),
                    )
                ],
            )
        report = build_report(
            native_targets.resolve(native_targets.every, root),
            repository_writers,
            resolve_state_root(),
            root,
        )
        if write is not None:
            written = root / write
            standing = written.read_text(encoding="utf-8") if written.is_file() else ""
            authored = authored_headings(standing)
            if authored and not force:
                refuse(
                    f"carries {len(authored)} section(s) this command did not "
                    f"write ({', '.join(authored)}): what one session knew, with "
                    "no other copy in a directory nothing versions, where the "
                    "walked half rebuilds from the tree in a second",
                    what=str(written),
                    steps=[
                        step(
                            "replace the whole file, as the report skill does",
                            devtools("dev", "report", "--write", str(write), "--force"),
                        ),
                        step(
                            "or read the report off stdout and compose the two "
                            "halves yourself",
                            devtools("dev", "report"),
                        ),
                    ],
                )
            written.parent.mkdir(parents=True, exist_ok=True)
            written.write_text(report.markdown(), encoding="utf-8")
            typer.echo(f"{written}: {report.outstanding()} outstanding item(s)")
            return
        if as_json:
            output_json(report)
            return
        typer.echo(report.markdown())

    return app
