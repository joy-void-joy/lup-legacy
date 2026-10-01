"""Feedback presentation: the command bodies behind ``lup-devtools feedback``.

This is a TEMPLATE script. Run ``/lup:init`` to customize it for your domain.
Everything that formats and prints for the CLI lives here; the loaders live
in ``state``, aggregation in ``metrics``, git commits in ``commits``.

Examples::

    $ uv run lup-devtools feedback status
    $ uv run lup-devtools feedback collect --all-time
    $ uv run lup-devtools feedback commit --dry-run
"""

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import TypedDict

import sh
import typer

from lup.workspace.history import (
    iter_session_dirs,
    resolve_version,
    session_backend,
)
from lup.workspace.paths import agent_version, feedback_path, traces_path
from lup.devtools.feedback.commits import (
    commit_session,
    get_uncommitted_session_ids,
)
from lup.devtools.feedback.metrics import compute_metrics, rollup_costs
from lup.devtools.feedback.models import (
    AgentPrompt,
    ErrorSessionEntry,
    LoadedSession,
    PromptHealthReport,
    PromptSection,
    SessionLoader,
    ToolUsageEntry,
    TrendEntry,
)
from lup.devtools.feedback.state import (
    collect_session_ids,
    load_analyzed,
    load_sessions_for_versions,
    match_outcomes,
    save_analyzed,
)
from lup.devtools.utils import format_table, output_json

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — how long a status report reads is this
# command's own presentation, and every id is one named command away
LISTED_SESSIONS = 20
"""How many unanalyzed session ids the status report prints before summarising.

A status report is read to decide whether to look closer, so it names the
count it held back and the command that prints every one — which is what makes
the shorter list a summary rather than an answer that looks complete.
"""


class ToolBucket(TypedDict):
    """One tool's aggregated call/error/duration tallies."""

    calls: int
    errors: int
    total_ms: float


# Open per-tool aggregation buckets, keyed by whatever tools ran.
type ToolBuckets = dict[str, ToolBucket]


def print_version_info(effective: list[str] | None) -> None:
    typer.echo("\n=== Agent Version ===\n")
    typer.echo(f"Current: {agent_version()}")
    if effective:
        typer.echo(f"Showing: {', '.join(effective)}")


def print_data_availability(
    effective: list[str] | None,
    all_session_ids: list[str],
) -> None:
    typer.echo("\n=== Data Availability ===\n")

    session_count = len(all_session_ids)
    if effective:
        typer.echo(f"Sessions: {session_count} (versions: {', '.join(effective)})")
    else:
        typer.echo(f"Sessions: {session_count} (all versions in {traces_path()})")

    backend_counts: Counter[str] = Counter()
    for session_id in all_session_ids:
        for session_dir in iter_session_dirs(session_id=session_id):
            backend_counts[session_backend(session_dir) or "—"] += 1
    if backend_counts:
        breakdown = ", ".join(
            f"{name}: {count}" for name, count in sorted(backend_counts.items())
        )
        typer.echo(f"Backends: {breakdown}")

    if traces_path().exists():
        version_count = sum(1 for d in traces_path().iterdir() if d.is_dir())
        typer.echo(f"Versions: {version_count} in {traces_path()}")
    else:
        typer.echo(f"Traces: No directory at {traces_path()}")

    if feedback_path().exists():
        feedback_files = list(feedback_path().glob("*_metrics.json"))
        typer.echo(f"Previous feedback collections: {len(feedback_files)}")
        if feedback_files:
            latest = sorted(feedback_files)[-1]
            typer.echo(f"  Latest: {latest.name}")
    else:
        typer.echo("Previous feedback collections: None")


def print_aggregate_stats(sessions: list[LoadedSession]) -> None:
    total = len(sessions)
    with_metrics = sum(1 for s in sessions if s.tool_metrics)
    with_tokens = sum(1 for s in sessions if s.token_usage)
    with_outcome = sum(1 for s in sessions if s.outcome is not None)

    typer.echo(f"\n=== Aggregate Stats ({total} sessions with result JSON) ===\n")
    typer.echo(f"With metrics: {with_metrics} ({100 * with_metrics / total:.0f}%)")
    typer.echo(f"With tokens:  {with_tokens} ({100 * with_tokens / total:.0f}%)")
    typer.echo(f"With outcome: {with_outcome} ({100 * with_outcome / total:.0f}%)")

    total_cost = sum(s.cost_usd or 0 for s in sessions)

    if total_cost > 0:
        typer.echo(f"\nTotal cost: ${total_cost:.2f}")
        typer.echo(f"Avg cost/session: ${total_cost / total:.4f}")
        typer.echo("Per-backend rollup: uv run lup-devtools feedback costs")

    total_input = sum(s.token_usage.input_tokens for s in sessions if s.token_usage)
    total_output = sum(s.token_usage.output_tokens for s in sessions if s.token_usage)

    if total_input or total_output:
        typer.echo("\nTokens:")
        typer.echo(f"  Input:  {total_input:,}")
        typer.echo(f"  Output: {total_output:,}")
        typer.echo(f"  Total:  {total_input + total_output:,}")


def costs(
    version: str | None,
    all_versions: bool,
    as_json: bool,
    loader: SessionLoader = load_sessions_for_versions,
) -> None:
    """Per-backend session cost/token rollup from session result JSONs.

    The cross-backend counterpart of ``uv run lup-devtools dev usage claude`` (which is
    Anthropic-OAuth only): codex/openai sessions carry normalized token
    usage and rate-estimated cost in their session JSON, and this is
    where they aggregate.
    """
    scope = resolve_version(version, all_versions)
    effective, ver_warning = scope.versions, scope.warning
    if ver_warning:
        typer.echo(ver_warning)

    sessions = loader(effective)
    rows = rollup_costs(sessions)

    if as_json:
        output_json(rows)
        return
    if not rows:
        typer.echo("No sessions with result JSON found.")
        return

    scope = ", ".join(effective) if effective else "all versions"
    typer.echo(f"=== Cost/token rollup by backend ({scope}) ===\n")
    headers = (
        "backend",
        "sessions",
        "cost_usd",
        "no-cost",
        "input",
        "output",
        "cached",
    )
    table_rows: list[tuple[str, ...]] = []  # lup: ignore[empty-collection]
    for name in sorted(rows):
        row = rows[name]
        cost_display = f"${row['cost_usd']:.2f}" if row["cost_usd"] else "—"
        table_rows.append(
            (
                name,
                str(row["sessions"]),
                cost_display,
                str(row["without_cost"]),
                f"{row['input_tokens']:,}",
                f"{row['output_tokens']:,}",
                f"{row['cache_read_input_tokens']:,}",
            )
        )
    aligns = ("left", "right", "right", "right", "right", "right", "right")
    typer.echo(format_table(headers, table_rows, aligns=aligns))
    total_cost = sum(row["cost_usd"] for row in rows.values())
    no_cost = sum(row["without_cost"] for row in rows.values())
    typer.echo(f"\nTotal: ${total_cost:.2f} across {len(sessions)} sessions")
    if no_cost:
        typer.echo(
            f"({no_cost} session(s) carry tokens but no cost — set "
            "CODEX_USD_PER_MTOK_* rates to estimate codex/openai cost)"
        )


def status(
    version: str | None,
    all_versions: bool,
    loader: SessionLoader = load_sessions_for_versions,
) -> None:
    """Show feedback status: version, data, analysis state, and aggregate stats."""
    scope = resolve_version(version, all_versions)
    effective, ver_warning = scope.versions, scope.warning
    if ver_warning:
        typer.echo(ver_warning)

    print_version_info(effective)

    all_session_ids = collect_session_ids(effective)
    session_count = len(all_session_ids)

    print_data_availability(effective, all_session_ids)

    analyzed = load_analyzed()
    unanalyzed_ids = sorted(i for i in all_session_ids if i not in analyzed)

    typer.echo("\n=== Analysis State ===\n")
    typer.echo(f"Session directories: {session_count}")
    typer.echo(f"Analyzed: {sum(1 for i in all_session_ids if i in analyzed)}")
    typer.echo(f"Unanalyzed: {len(unanalyzed_ids)}")

    sessions = loader(effective)

    if sessions:
        print_aggregate_stats(sessions)

    if unanalyzed_ids:
        typer.echo("\n=== Unanalyzed Sessions ===\n")
        # lup: ignore[silent-truncation] — the count of what is held back and
        # the command that prints all of it are both stated below
        for sid in unanalyzed_ids[:LISTED_SESSIONS]:
            typer.echo(f"  {sid}")
        if len(unanalyzed_ids) > LISTED_SESSIONS:
            typer.echo(f"  ... and {len(unanalyzed_ids) - LISTED_SESSIONS} more")
        typer.echo(
            "\nTo list all unanalyzed IDs: uv run lup-devtools feedback unanalyzed"
        )


def collect(
    since: str | None,
    all_time: bool,
    version: str | None,
    all_versions: bool,
    output: Path | None,
    *,
    dry_run: bool = False,
    loader: SessionLoader = load_sessions_for_versions,
) -> None:
    """Collect feedback metrics from sessions."""
    if since and all_time:
        typer.echo("Error: --since and --all-time are mutually exclusive", err=True)
        raise typer.Exit(1)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    scope = resolve_version(version, all_versions)
    effective, ver_warning = scope.versions, scope.warning
    if ver_warning:
        typer.echo(ver_warning)

    since_dt: datetime | None = None
    if not all_time and since:
        since_dt = datetime.fromisoformat(since)

    logger.info(
        "Collecting feedback since %s",
        since_dt.isoformat() if since_dt else "all time",
    )

    sessions = loader(effective)
    if since_dt:
        sessions = [
            s
            for s in sessions
            if not s.timestamp or datetime.fromisoformat(s.timestamp) >= since_dt
        ]

    if not sessions:
        typer.echo("No sessions found. Nothing to collect.")
        typer.echo('Run agent sessions first: uv run lup run "task"')
        return

    logger.info("Found %d sessions", len(sessions))

    results = match_outcomes(sessions)
    feedback = compute_metrics(results)

    typer.echo("\n" + "=" * 60)
    typer.echo("FEEDBACK COLLECTION SUMMARY")
    typer.echo("=" * 60)
    typer.echo(f"Total sessions: {feedback.total_sessions}")
    typer.echo(f"Sessions with outcomes: {feedback.sessions_with_outcomes}")

    if dry_run:
        typer.echo("\n(dry run — no files written)")
        return

    if output is None:
        feedback_path().mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output = feedback_path() / f"{timestamp}_metrics.json"

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(feedback.model_dump_json(indent=2))
    typer.echo(f"\nMetrics saved to: {output}")


def tools(
    version: str | None,
    all_versions: bool,
    as_json: bool,
    loader: SessionLoader = load_sessions_for_versions,
) -> None:
    """Show tool usage aggregates."""
    scope = resolve_version(version, all_versions)
    effective, warning = scope.versions, scope.warning
    if warning:
        typer.echo(warning)
    sessions = loader(effective)
    if not sessions:
        if as_json:
            output_json([])
        else:
            typer.echo("No sessions found")
        return

    tool_stats: ToolBuckets = defaultdict(
        lambda: {"calls": 0, "errors": 0, "total_ms": 0}
    )

    for s in sessions:
        if s.tool_metrics is None:
            continue
        for tool_name, data in s.tool_metrics["by_tool"].items():
            tool_stats[tool_name]["calls"] += data["call_count"]
            tool_stats[tool_name]["errors"] += data["error_count"]
            tool_stats[tool_name]["total_ms"] += (
                data["avg_duration_ms"] * data["call_count"]
            )

    if not tool_stats:
        if as_json:
            output_json([])
        else:
            typer.echo("No tool metrics found")
        return

    def usage_entry(tool_name: str) -> ToolUsageEntry:
        stats = tool_stats[tool_name]
        calls = int(stats["calls"])
        errs = int(stats["errors"])
        return {
            "name": tool_name,
            "calls": calls,
            "errors": errs,
            "error_rate": (errs / calls) if calls > 0 else 0.0,
            "avg_ms": stats["total_ms"] / calls if calls > 0 else 0.0,
        }

    entries = [
        usage_entry(name)
        for name in sorted(tool_stats.keys(), key=lambda t: -tool_stats[t]["calls"])
    ]

    if as_json:
        output_json(entries)
        return

    typer.echo("\n=== Tool Usage Summary ===\n")

    def summary_cells(e: ToolUsageEntry) -> list[str]:
        err_pct = e["error_rate"] * 100
        err_indicator = " !" if err_pct > 10 else ""
        return [
            e["name"],
            str(e["calls"]),
            str(e["errors"]),
            f"{err_pct:.1f}%{err_indicator}",
            f"{e['avg_ms']:.0f}",
        ]

    rows = [summary_cells(e) for e in entries]
    headers = ("Tool", "Calls", "Errors", "Err%", "Avg ms")
    aligns = ("left", "right", "right", "right", "right")
    typer.echo(format_table(headers, rows, aligns=aligns))


def errors(
    limit: int,
    version: str | None,
    all_versions: bool,
    as_json: bool,
    loader: SessionLoader = load_sessions_for_versions,
) -> None:
    """Show sessions with high error rates from structured metrics."""
    scope = resolve_version(version, all_versions)
    effective, warning = scope.versions, scope.warning
    if warning:
        typer.echo(warning)
    sessions = loader(effective)
    if not sessions:
        if as_json:
            output_json([])
        else:
            typer.echo("No sessions found")
        return

    def error_entry(s: LoadedSession) -> ErrorSessionEntry | None:
        metrics = s.tool_metrics
        if metrics is None or metrics["total_errors"] <= 0:
            return None
        return {
            "session_id": s.source_session_id,
            "errors": metrics["total_errors"],
            "by_tool": metrics["by_tool"],
        }

    with_errors = [e for s in sessions if (e := error_entry(s)) is not None]

    if not with_errors:
        if as_json:
            output_json([])
        else:
            typer.echo("No sessions with errors found")
        return

    with_errors.sort(key=lambda x: -x["errors"])

    if as_json:
        output_json(with_errors[:limit])
        return

    typer.echo(f"\n=== Sessions with Errors ({len(with_errors)} total) ===\n")

    for item in with_errors[:limit]:
        typer.echo(f"Session {item['session_id']}: {item['errors']} errors")
        for tool_name, tool_data in item["by_tool"].items():
            if tool_data["error_count"] > 0:
                typer.echo(f"  - {tool_name}: {tool_data['error_count']}")


def trends(
    window: int,
    version: str | None,
    all_versions: bool,
    as_json: bool,
    loader: SessionLoader = load_sessions_for_versions,
) -> None:
    """Show metric trends over time."""
    scope = resolve_version(version, all_versions)
    effective, warning = scope.versions, scope.warning
    if warning:
        typer.echo(warning)
    sessions = loader(effective)
    if not sessions:
        if as_json:
            output_json([])
        else:
            typer.echo("No sessions found")
        return

    sessions_with_ts = [s for s in sessions if s.timestamp]
    sessions_with_ts.sort(key=lambda s: s.timestamp)

    if len(sessions_with_ts) < window:
        if as_json:
            output_json([])
        else:
            typer.echo(f"Need at least {window} sessions for trend analysis")
            typer.echo(f"Have: {len(sessions_with_ts)}")
        return

    entries: list[TrendEntry] = []  # lup: ignore[empty-collection] — window fold
    for i in range(window - 1, len(sessions_with_ts)):
        window_sessions = sessions_with_ts[i - window + 1 : i + 1]

        total_calls = 0
        total_errs = 0
        for ws in window_sessions:
            if ws.tool_metrics:
                total_calls += ws.tool_metrics["total_tool_calls"]
                total_errs += ws.tool_metrics["total_errors"]
        avg_calls = total_calls / window
        error_rate = total_errs / max(1, total_calls)

        total_cost = sum(s.cost_usd or 0 for s in window_sessions)
        avg_cost = total_cost / window

        latest_ts = window_sessions[-1].timestamp[:10]
        entries.append(
            {
                "date": latest_ts,
                "avg_calls": round(avg_calls, 1),
                "error_rate": round(error_rate, 4),
                "avg_cost": round(avg_cost, 4),
            }
        )

    if as_json:
        output_json(entries)
        return

    typer.echo(f"\n=== Trends (rolling {window}-session window) ===\n")
    for e in entries:
        typer.echo(
            f"{e['date']}: calls={e['avg_calls']:.1f}/session, "
            f"errors={e['error_rate']:.1%}, cost=${e['avg_cost']:.4f}/session"
        )


def history(limit: int) -> None:
    """Show previous feedback collection runs."""
    if not feedback_path().exists():
        typer.echo("No feedback history found")
        return

    metrics_files = sorted(feedback_path().glob("*_metrics.json"), reverse=True)
    if not metrics_files:
        typer.echo("No metrics files found")
        return

    typer.echo("\n=== Feedback Collection History ===\n")

    for f in metrics_files[:limit]:
        try:
            match json.loads(f.read_text()):
                case {"total_sessions": int(total), "sessions_with_outcomes": int(w)}:
                    typer.echo(f"{f.name}: {total} sessions, {w} with outcomes")
                case _:
                    typer.echo(f"{f.name}: (unexpected format)")
        except (json.JSONDecodeError, OSError):
            typer.echo(f"{f.name}: (error reading)")


def mark(session_ids: list[str]) -> None:
    """Mark sessions as analyzed in the feedback loop."""
    analyzed = load_analyzed()
    new_ids = [i for i in dict.fromkeys(session_ids) if i not in analyzed]
    if not new_ids:
        typer.echo("All specified sessions already marked")
        return
    save_analyzed(analyzed + new_ids)
    typer.echo(f"Marked {len(new_ids)} sessions as analyzed")


def unmark(session_ids: list[str]) -> None:
    """Remove analysis marks from sessions."""
    analyzed = load_analyzed()
    removed = [i for i in analyzed if i in session_ids]
    if not removed:
        typer.echo("None of the specified sessions were marked")
        return
    save_analyzed([i for i in analyzed if i not in removed])
    typer.echo(f"Unmarked {len(removed)} sessions")


def prompt_health(prompt: AgentPrompt, as_json: bool) -> None:
    """Analyze the agent prompt structure and size.

    Weighs the prompt the application assembled, not the source file it was
    authored in, and breaks the total down by named section.
    """
    prompts_file = prompt.source
    char_count = len(prompt.rendered)
    estimated_tokens = char_count // 4

    def section_report(section_text: str) -> PromptSection:
        prose = section_text.strip()
        first_line = prose.splitlines()[0] if prose else "(empty)"
        return {
            "name": first_line,
            "lines": len(section_text.splitlines()),
            "characters": len(section_text),
        }

    section_reports = [section_report(s) for s in prompt.sections]

    report: PromptHealthReport = {
        "file": str(prompts_file) if prompts_file else "unknown",
        "rendered_characters": char_count,
        "estimated_tokens": estimated_tokens,
        "sections": section_reports,
    }

    if as_json:
        output_json(report)
        return

    typer.echo("\n=== Prompt Health ===\n")
    if prompts_file:
        typer.echo(f"File: {prompts_file}")
    typer.echo(f"Rendered: {char_count:,} chars (~{estimated_tokens:,} tokens)")
    typer.echo(f"Sections: {len(section_reports)}")

    typer.echo()
    rows = [(s["name"], str(s["lines"]), str(s["characters"])) for s in section_reports]
    typer.echo(
        format_table(
            ("Section", "Lines", "Chars"),
            rows,
            aligns=("left", "right", "right"),
        )
    )


def unanalyzed(version: str | None, all_versions: bool) -> None:
    """List unanalyzed session IDs, one per line."""
    scope = resolve_version(version, all_versions)
    effective, ver_warning = scope.versions, scope.warning
    if ver_warning:
        typer.echo(ver_warning)

    all_session_ids = collect_session_ids(effective)
    analyzed = load_analyzed()

    for sid in sorted(i for i in all_session_ids if i not in analyzed):
        typer.echo(sid)


def commit(dry_run: bool) -> None:
    """Commit all uncommitted session result files, one commit per session."""
    session_ids = get_uncommitted_session_ids()

    if not session_ids:
        typer.echo("Nothing to commit.")
        return

    typer.echo(f"Found {len(session_ids)} session(s) with uncommitted files")

    committed = 0
    for session_id in sorted(session_ids):
        try:
            if commit_session(session_id, dry_run=dry_run):
                committed += 1
        except sh.ErrorReturnCode as e:
            typer.echo(f"  Failed {session_id}: {e}", err=True)

    if dry_run:
        typer.echo(f"\nWould commit {committed} session(s)")
    else:
        typer.echo(f"\nCommitted {committed} session(s)")
