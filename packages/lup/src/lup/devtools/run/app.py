"""The command tree for work that outlives the tool call that started it.

A sub-app is a surface of a module, and this is the runs module's: a project
that declines long-running pipelines should stop being offered a way to watch
them, and while ``monitor`` sat under ``dev`` it was owned by ``core`` and
every project had it.

``monitor`` reads only what a runner writes, so a run launched detached,
launched by somebody else, or launched before this shell existed all read back
the same — and reading one cannot perturb it. ``report`` is the other
direction and the only write here: a unit in any language saying how far into
its own work it has got, which is what separates a unit forty percent through
from one spinning at zero.
"""

import json
import os
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

import lup.devtools.dev.monitor as monitor
from lup.diagnostics import refuse
from lup.policy.kernel.diagnostic import step
from lup.runs.directory import WORKSPACE_ENV, RunDirectory
from lup.runs.report import report_progress
from lup.types import JsonValue


class DetailPair(BaseModel, frozen=True):
    """One entry of a unit's own vocabulary, as its command line spells it."""

    key: str
    value: JsonValue


def json_or_text(value: str) -> JsonValue:
    """One flag value as the unit meant it: JSON where it is JSON, text where not.

    ``supports=41`` is a number, ``ok=true`` a flag, ``shape={"k":1}`` an
    object, and ``note=alpha`` — which is no JSON at all — the text itself.
    One flag rather than a second one for the nested case, because the record
    already holds any JSON value and two flags would be two things to keep in
    step with each other.
    """
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def parsed_pair(pair: str) -> DetailPair:
    """One ``key=value`` as this command's flag grammar spells it."""
    key, separator, value = pair.partition("=")  # lup: ignore[string-split] — a flag
    if not separator:
        refuse("--detail takes key=value", what=pair, code=2)
    return DetailPair(key=key, value=json_or_text(value))


def parsed_detail(pairs: list[str]) -> dict[str, JsonValue]:
    """Every ``--detail`` given, in the order the unit put them."""
    return {entry.key: entry.value for entry in map(parsed_pair, pairs)}


def reporting_workspace(named: Path | None) -> Path:
    """Where to report: what was named, else the workspace this unit was given."""
    if named is not None:
        return named
    inherited = os.environ.get(WORKSPACE_ENV, "")  # lup: ignore[os-environ] — a unit's
    if not inherited:
        refuse(
            "no workspace to write the report into",
            steps=[
                step(f"run this inside a step, where {WORKSPACE_ENV} is bound"),
                step("or name one with `--workspace`"),
            ],
            code=2,
        )
    return Path(inherited)


def create_run_app() -> typer.Typer:
    """Wire the command tree for following a background run."""
    app = typer.Typer(no_args_is_help=True)

    @app.command("monitor")
    def monitor_cmd(
        run_directory: Annotated[
            Path,
            typer.Argument(help="A run directory holding manifest.json and units/"),
        ],
        log: Annotated[
            Path | None,
            typer.Option(
                "--log",
                help="The runner's log; defaults to run.log inside the directory",
            ),
        ] = None,
        interval: Annotated[
            float,
            typer.Option("--interval", help="Seconds between readings"),
        ] = 2.0,
        events: Annotated[
            bool,
            typer.Option("--events", help="One line per change, for a watcher"),
        ] = False,
        one_shot: Annotated[
            bool,
            typer.Option("--once", help="Print one reading and exit"),
        ] = False,
        quiet_limit: Annotated[
            float,
            typer.Option(
                "--quiet-limit", help="Seconds of silence that reads as a stall"
            ),
        ] = 900.0,
    ) -> None:
        """Follow a background run: its landed units, their statuses, its heartbeat.

        Works on a run launched detached or from another session, because it
        reads only what the runner writes. `--events` emits one line per thing
        that happens and ends when the run does, which is the shape a watcher
        is woken by; without it the reading is redrawn in place for a person.
        Nothing about the run is touched either way.
        """
        directory = RunDirectory(root=run_directory)
        # A path that is not there is a mistyped one, not a run yet to start,
        # which would otherwise read as 0/0 units and wait forever. A directory
        # that exists may be a run whose runner has not written its manifest
        # yet, so it is followed.
        if not run_directory.is_dir():
            refuse(
                "is not a directory, so it holds no run",
                what=str(run_directory),
                steps=[step("pass the run directory the launch printed")],
            )
        if events:
            monitor.stream(directory, log, interval, quiet_limit)
            return
        if one_shot:
            typer.echo(monitor.once(directory, log))
            return
        typer.echo(monitor.report(directory, log, interval))

    @app.command("report")
    def report_cmd(
        done: Annotated[
            int, typer.Option("--done", help="How much of its work this unit has done")
        ],
        total: Annotated[
            int | None,
            typer.Option("--total", help="How much there is to do, when it is known"),
        ] = None,
        phase: Annotated[
            str,
            typer.Option("--phase", help="Which part of its own work it is in, a word"),
        ] = "",
        detail: Annotated[
            list[str],
            typer.Option(
                "--detail",
                help="key=value in the unit's own vocabulary; the value is read "
                'as JSON where it is JSON (supports=41, ok=true, shape={"k":1}) '
                "and as text where it is not",
            ),
        ] = [],
        workspace: Annotated[
            Path | None,
            typer.Option(
                "--workspace",
                help=f"Where to write; defaults to ${WORKSPACE_ENV}",
            ),
        ] = None,
    ) -> None:
        """Say how far into its own work a unit has got, from any language.

        For a unit that cannot import this library. It writes the same record
        `lup.runs.report.report_progress` does, into the workspace the runtime
        gave the unit, and `run monitor` shows it as a bar beside the run's own.

        A process per report, which is right at one report a second and wrong
        at a thousand: a loop reporting that often should call
        `report_progress` in-process, where a report costs nothing to drop.
        The throttle that drops one here cannot see the report before it,
        every invocation being its own process, so every call writes.
        """
        report_progress(
            reporting_workspace(workspace),
            done=done,
            total=total,
            phase=phase,
            detail=parsed_detail(detail),
        )

    return app
