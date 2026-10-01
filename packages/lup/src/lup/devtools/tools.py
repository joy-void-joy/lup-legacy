"""The ``tools`` sub-app: serve the MCP servers lup hosts, and read what their tools did.

The same command :mod:`lup.mcp.serve` runs as ``python -m lup.mcp.serve``,
mounted where a project's composed CLI can reach it, so a generated native
tree starts its servers through the program the project already runs. Each
process it starts writes what its tools recorded to a snapshot of its own,
and ``tools metrics`` is where a person reads them back.
"""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

from lup.devtools.subapps import subapp
from lup.devtools.utils import JSON_OPT, format_table, output_json
from lup.mcp.serve import ServedSessions, serve_command
from lup.observability.metrics import (
    MetricsSnapshot,
    ToolMetricsDict,
    merged_summary,
    read_metrics_snapshots,
)
from lup.workspace.history import iter_session_dirs


class ToolRow(BaseModel, frozen=True):
    """One tool of one server, over every process of that server in scope."""

    server: str
    tool: str
    calls: int
    errors: int
    error_rate: float
    avg_ms: float
    min_ms: float
    max_ms: float

    @classmethod
    def of(cls, server: str, tool: str, data: ToolMetricsDict) -> "ToolRow":
        """One tool's folded counts, as a row."""
        return cls(
            server=server,
            tool=tool,
            calls=data["call_count"],
            errors=data["error_count"],
            error_rate=data["error_count"] / max(1, data["call_count"]),
            avg_ms=data["avg_duration_ms"],
            min_ms=data["min_duration_ms"],
            max_ms=data["max_duration_ms"],
        )

    def cells(self) -> list[str]:
        return [
            self.server,
            self.tool,
            str(self.calls),
            str(self.errors),
            f"{self.error_rate:.1%}",
            f"{self.avg_ms:.1f}",
            f"{self.min_ms:.1f}",
            f"{self.max_ms:.1f}",
        ]


class ToolMetricsReport(BaseModel, frozen=True):
    """What ``tools metrics`` read: from where, how many processes, each tool's row."""

    directories: list[Path]
    processes: int
    first_started: datetime | None = None
    last_updated: datetime | None = None
    tools: list[ToolRow]

    @classmethod
    def of(
        cls, directories: list[Path], snapshots: Sequence[MetricsSnapshot]
    ) -> "ToolMetricsReport":
        """Every server's processes folded into one row per tool, busiest first."""
        servers = sorted({snapshot.server for snapshot in snapshots})
        rows = [
            ToolRow.of(server, tool, data)
            for server in servers
            for tool, data in merged_summary(
                [s.summary for s in snapshots if s.server == server]
            )["by_tool"].items()
        ]
        return cls(
            directories=directories,
            processes=len(snapshots),
            first_started=min((s.started for s in snapshots), default=None),
            last_updated=max((s.updated for s in snapshots), default=None),
            tools=sorted(rows, key=lambda row: (-row.calls, row.server, row.tool)),
        )

    def heading(self) -> str:
        """How many processes in how many directories, over what stretch."""
        count = len(self.directories)
        held = f"{self.processes} server process(es) in {count} session director"
        held += "y" if count == 1 else "ies"
        match self.first_started, self.last_updated:
            case datetime() as first, datetime() as last:
                return (
                    f"{held}, {first.astimezone():%Y-%m-%d %H:%M} to "
                    f"{last.astimezone():%Y-%m-%d %H:%M}"
                )
            case _:
                return held


def metrics_command(
    session: Annotated[
        str | None,
        typer.Option(
            "--session",
            help="Read one session opened in process, by id, not the launched ones",
        ),
    ] = None,
    member: Annotated[
        str | None,
        typer.Option(
            "--member", help="Only the servers of the session with this roster id"
        ),
    ] = None,
    since: Annotated[
        str,
        typer.Option(
            "--since", help="Only processes still writing after this ISO 8601 moment"
        ),
    ] = "",
    as_json: JSON_OPT = False,
) -> None:
    """Show each tool's calls, errors and latency, from the servers sessions here started.

    Every tool-serving process writes a snapshot of what its tools recorded
    under the directory of the session it serves. This reads the sessions
    launched in this checkout — whose servers a runtime's own CLI started,
    all under one shared session directory per agent version — or, with
    `--session`, one session this project opened in process. `--member`
    narrows to one launched session's servers by its roster id.

    A process is counted whole when it was still writing after `--since`,
    because a snapshot is cumulative from the moment its process started.
    The directory keeps 30 days and at most 1000 snapshots, pruned as each
    process starts.
    """
    try:
        moment = datetime.fromisoformat(since).astimezone() if since else None
    except ValueError as problem:
        typer.echo(f"--since {since!r} is not an ISO 8601 moment: {problem}", err=True)
        raise typer.Exit(1) from problem
    directories = (
        list(iter_session_dirs(session_id=session))
        if session is not None
        else ServedSessions().directories()
    )
    snapshots = [
        snapshot
        for directory in directories
        for snapshot in read_metrics_snapshots(directory, moment)
        if member is None or snapshot.member == member
    ]
    report = ToolMetricsReport.of(directories, snapshots)
    if as_json:
        output_json(report)
        return
    if not report.tools:
        where = ", ".join(str(directory) for directory in directories)
        typer.echo(f"No tool-server metrics recorded in {where or 'any session'}")
        return
    typer.echo(report.heading() + "\n")
    typer.echo(
        format_table(
            ("Server", "Tool", "Calls", "Errors", "Err%", "Avg ms", "Min ms", "Max ms"),
            [row.cells() for row in report.tools],
            aligns=(
                "left",
                "left",
                "right",
                "right",
                "right",
                "right",
                "right",
                "right",
            ),
        )
    )


app = typer.Typer(no_args_is_help=True)
app.command("serve")(serve_command)
app.command("metrics")(metrics_command)
SUBAPP = subapp(
    "tools",
    "Serve the MCP servers a launched session declares, and read what their tools did",
    app,
)
