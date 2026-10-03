"""The resolver's read-only journal timing report."""

from datetime import timedelta
from typing import Annotated

import typer

from lup.devtools.supervisor.doors import resolve_state_root
from lup.devtools.utils import format_table
from lup.diagnostics import refuse
from lup.resolver.cost import CostReport, read_cost
from lup.resolver.status import compact_interval


def rendered_cost(report: CostReport) -> list[str]:
    """Render every measured group and long idle interval without hiding evidence."""
    if report.started_at is None or report.ended_at is None:
        return ["The journal contains no events; no elapsed time is recorded."]

    def duration(seconds: float) -> str:
        return compact_interval(timedelta(seconds=seconds))

    lines = [
        f"Observed: {report.started_at.isoformat()} → {report.ended_at.isoformat()}",
        f"Wall {duration(report.wall_seconds)} · active {duration(report.active_seconds)} "
        f"· idle {duration(report.idle_seconds)} · uncertain {duration(report.uncertain_seconds)} "
        f"· peak concurrent completed turns {report.peak_concurrency}",
        "Active time and peak concurrency use matched starts/completions; actor totals include overlap.",
        "Unresolved intervals supply no activity proof. Their time outside completed turns is uncertain.",
        format_table(
            (
                "Actor",
                "Completed",
                "Mean",
                "Max",
                "Total",
                "Completed peak",
                "Unfinished",
                "Interrupted",
            ),
            [
                [
                    actor.kind,
                    str(actor.completed),
                    duration(actor.mean_seconds)
                    if actor.mean_seconds is not None
                    else "—",
                    duration(actor.maximum_seconds),
                    duration(actor.total_seconds),
                    str(actor.peak_concurrency),
                    str(actor.unfinished),
                    str(actor.interrupted),
                ]
                for actor in report.actors
            ],
        ),
        "Run failures (exact recorded reason):",
        *[f"  {failure.count} × {failure.reason}" for failure in report.failures],
        f"Idle gaps longer than {duration(report.gap_threshold_seconds)}:",
        *[
            f"  {gap.started_at.isoformat()} → {gap.ended_at.isoformat()} "
            f"({duration(gap.seconds)}), after #{gap.preceding.seq} "
            f"{gap.preceding.actor.label()} {gap.preceding.event.type}: "
            f"{gap.preceding.event.model_dump_json()}"
            for gap in report.idle_gaps
        ],
        "Unresolved turn bounds (may overlap completed activity):",
        *[
            f"  {interval.key.actor.label()} {interval.key.identifiers.model_dump_json()} "
            f"{interval.outcome}: {interval.started.at.isoformat()} → {interval.ended.at.isoformat()} "
            f"(#{interval.started.seq} → #{interval.ended.seq}); "
            f"from {interval.started.event.model_dump_json()} "
            f"through {interval.ended.event.model_dump_json()}"
            for interval in report.unresolved_intervals
        ],
        *[f"Evidence anomaly: {message}" for message in report.anomalies],
    ]
    return lines


def show_cost(
    run_id: Annotated[str, typer.Option("--run-id", help="Run whose journal to read")],
    gap_seconds: Annotated[
        float,
        typer.Option("--gap-seconds", min=0, help="Minimum idle-gap duration to list"),
    ] = 600,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit typed timing evidence")
    ] = False,
) -> None:
    """Report journal timing, unresolved intervals, actor turns, failures, and idle gaps."""
    try:
        report = read_cost(
            resolve_state_root() / run_id, timedelta(seconds=gap_seconds)
        )
    except (FileNotFoundError, ValueError) as error:
        refuse(str(error), what=run_id, code=2)
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        for line in rendered_cost(report):
            typer.echo(line)
