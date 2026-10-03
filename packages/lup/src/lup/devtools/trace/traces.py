# lup: ignore[import-re, re-call, string-split]
# This module IS the parser for the repo's own trace-markdown format (the
# legacy fallback beside the .events.jsonl sidecar) — line surgery and the
# fallback patterns are the parse, not a substitute for one, and user search
# queries are regex by contract.
"""Trace display, search, and analysis implementation.

Provides reusable scanner functions (``scan_for_errors``, ``scan_for_capability_gaps``)
consumed by both trace CLI commands and ``feedback/analyze.py``.

Analysis reads the machine-readable ``.events.jsonl`` sidecar that
:class:`lup.observability.trace.TraceLogger` writes beside each ``.md`` trace: typed tool,
error, and capability events, no regex. The line-scan over markdown remains
only as the documented fallback for legacy ``.md`` traces that predate the
sidecar.

Examples::

    $ uv run lup-devtools trace list
    $ uv run lup-devtools trace list --json
    $ uv run lup-devtools trace show my-session-id --full
    $ uv run lup-devtools trace search "confidence.*low" --json
    $ uv run lup-devtools trace capabilities --json
"""

import json
import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from pydantic import BaseModel

import typer

from lup.observability.audit import (
    ChainBreak,
    ObservableEvent,
    chain_break,
    read_observable_events,
)
from lup.observability.blocks import truncate_str
from lup.observability.trace import (
    TraceEvent,
    capability_request_from_text,
    read_trace_events,
    tool_result_ok,
)
from lup.workspace.history import (
    iter_run_dirs,
    iter_session_dirs,
    iter_trace_event_files,
    iter_trace_log_files,
    run_transcript,
    session_backend,
)
from lup.workspace.paths import (
    harness_runs_path,
    parse_timestamp,
    project_root,
    traces_path,
)

from lup.devtools.utils import format_table, output_json, refuse
from lup.policy.kernel.diagnostic import devtools, step

# lup: ignore[constant-declaration] — how much of a trace the default view
# opens with is this command's own presentation, and `--full` is the whole
HEAD_LINES = 100
"""How much of a trace the default view shows before pointing at `--full`.

A trace runs to whatever length the session did, and the common reading is the
opening. The view says how many lines it stood in for and how to get all of
them, which is what separates a head from a trace that appears to end here.
"""


class TraceRef(BaseModel):
    """One discovered trace: which store it came from, its session id, its dir."""

    source: str
    session_id: str
    path: Path


# ── types ─────────────────────────────────────────────────


class TraceErrorSession(TypedDict):
    session_id: str
    error_count: int
    errors: list[str]


class CapabilityRequest(TypedDict):
    text: str
    count: int
    session_ids: list[str]


class SearchMatch(TypedDict):
    file: str
    line: int
    context: list[str]


class TraceRow(TypedDict):
    session_id: str
    source: str
    backend: str | None
    files: int
    size_kb: float


# ── event sourcing: structured sidecar, with a legacy-markdown fallback ────


def events_for_trace(trace_file: Path) -> list[TraceEvent]:
    """Return the typed events for one ``.md`` trace.

    Primary path: read the ``.events.jsonl`` sidecar written beside the trace
    — already-typed tool/error/capability records, no parsing of prose. Only
    when no sidecar exists (a legacy trace predating it) does this fall back
    to :func:`events_from_legacy_markdown`.
    """
    sidecar = trace_file.with_suffix(".events.jsonl")
    if sidecar.exists():
        return read_trace_events(sidecar)
    return events_from_legacy_markdown(trace_file.read_text(encoding="utf-8"))


def events_from_legacy_markdown(content: str) -> list[TraceEvent]:
    """Reconstruct :class:`TraceEvent`s from a legacy ``.md`` trace.

    The markdown is structured, not arbitrary prose: ``TraceLogger`` renders
    each block under a ``## <emoji> <label>`` header (``Tool: <name>``,
    ``Result`` with a fenced body, ``Response`` text). This parses that
    structure — pairing each Result with the preceding Tool, and reading
    ``is_error`` from the Result's JSON via :func:`tool_result_ok` — so error
    detection matches the structured path instead of keyword-guessing. The
    one irreducible heuristic, capability phrasing in free-form Response text,
    is shared with the live logger.
    """
    events: list[TraceEvent] = []  # lup: ignore[empty-collection] — block fold
    pending_tool: str | None = None
    # Legacy markdown carries no per-event timestamps; "" marks them unknown.
    for block in iter_markdown_blocks(content):
        body = block.body
        match block.label.split(maxsplit=1):
            case ["Tool:"]:
                pending_tool = "unknown"
            case ["Tool:", name]:
                pending_tool = name.strip()
            case ["Result"]:
                name = pending_tool or "unknown"
                pending_tool = None
                ok = tool_result_ok(body)
                brief = truncate_str(body.strip(), 300)
                events.append(
                    TraceEvent(
                        kind="tool_call", timestamp="", tool=name, ok=ok, brief=brief
                    )
                )
                if not ok:
                    events.append(
                        TraceEvent(kind="error", timestamp="", tool=name, brief=brief)
                    )
            case _:
                request = capability_request_from_text(body)
                if request is not None:
                    events.append(
                        TraceEvent(
                            kind="capability_request", timestamp="", brief=request
                        )
                    )
    return events


class Block(BaseModel):
    """One labelled section of a trace markdown document."""

    label: str
    body: str


def iter_markdown_blocks(
    content: str,
) -> list[Block]:
    """Split a trace markdown document into labelled blocks.

    A block starts at a ``## <emoji> <label>`` header and runs to the next
    header. The body has any surrounding ``` ``` fences stripped, so a Result
    body is the raw JSON the logger fenced — ready to parse.
    """
    blocks: list[Block] = []
    label: str | None = None
    body_lines: list[str] = []  # lup: ignore[empty-collection] — block fold

    def flush() -> None:
        if label is not None:
            blocks.append(
                Block(label=label, body=strip_code_fence("\n".join(body_lines)))
            )

    for line in content.split("\n"):
        if line.startswith("## "):
            flush()
            # "## 🔧 Tool: search" -> "Tool: search"; emoji is the first token.
            heading = line.removeprefix("## ").strip()
            parts = heading.split(" ", 1)
            label = parts[1].strip() if len(parts) > 1 else heading
            body_lines = []
            continue
        if label is not None:
            body_lines.append(line)
    flush()
    return blocks


def strip_code_fence(body: str) -> str:
    """Drop a leading ``` (or ```json) fence and its closing ``` from *body*."""
    lines = body.strip().split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def resolve_trace_paths(effective: list[str] | None) -> list[Path]:
    """Collect .md trace files, optionally filtered by version list."""
    if not traces_path().exists():
        return []
    if effective:
        return [
            path
            for v in effective
            if (ver_dir := traces_path() / v).exists()
            for path in ver_dir.rglob("*.md")
        ]
    return list(traces_path().rglob("*.md"))


def display_path(path: Path) -> str:
    """Project-relative path for display, absolute when outside the project."""
    try:
        return str(path.relative_to(project_root()))
    except ValueError:
        return str(path)


def session_id_from_path(trace_file: Path) -> str:
    """Extract session ID from a trace file path."""
    try:
        rel = trace_file.relative_to(traces_path())
        match rel.parts:
            case [_, "logs" | "sessions", session_id, *_]:
                return session_id
            case [session_id, _]:
                return session_id
            case _:
                return rel.stem
    except ValueError:
        return trace_file.stem


def scan_for_errors(
    effective: list[str] | None = None,
) -> list[TraceErrorSession]:
    """Report failing tool calls per session from structured trace events.

    Reads each trace's typed events (sidecar, or legacy-markdown fallback)
    and keeps the ``error`` ones — real tool failures, distinguished by the
    logged ``is_error`` flag rather than by keyword-scanning prose.
    """
    errors_by_session: defaultdict[str, list[str]] = defaultdict(list)

    for trace_file in resolve_trace_paths(effective):
        try:
            events = events_for_trace(trace_file)
        except OSError:
            continue
        session_id = session_id_from_path(trace_file)
        for event in events:
            if event.kind != "error":
                continue
            errors_by_session[session_id].append(
                f"{event.tool or 'unknown'}: {event.brief}"
            )

    return [
        {"session_id": session_id, "error_count": len(errors), "errors": errors}
        for session_id, errors in sorted(
            errors_by_session.items(), key=lambda x: len(x[1]), reverse=True
        )
    ]


def scan_for_capability_gaps(
    effective: list[str] | None = None,
) -> list[CapabilityRequest]:
    """Report capability requests across traces, deduplicated by text.

    Reads each trace's typed ``capability_request`` events (sidecar, or
    legacy-markdown fallback) and groups identical wishes, most-requested
    first.
    """
    requests_by_text: dict[str, list[str]] = defaultdict(list)

    for trace_file in resolve_trace_paths(effective):
        try:
            events = events_for_trace(trace_file)
        except OSError:
            continue
        session_id = session_id_from_path(trace_file)
        for event in events:
            if event.kind == "capability_request" and event.brief:
                requests_by_text[event.brief].append(session_id)

    return [
        {
            "text": text,
            "count": len(session_ids),
            "session_ids": sorted(dict.fromkeys(session_ids)),
        }
        for text, session_ids in sorted(
            requests_by_text.items(), key=lambda x: -len(x[1])
        )
    ]


# ── helpers ───────────────────────────────────────────────


def find_trace(session_id: str) -> Path | None:
    """Find a showable trace for a session across all versions.

    Prefers the complete log directory, including reasoning and browser events,
    then any ``.md`` in the session dir, then the session dir itself — whose
    JSON ``trace list`` enumerates, so every listed id is showable.
    """
    log_files = list(iter_trace_log_files(session_id=session_id))
    event_files = list(iter_trace_event_files(session_id=session_id))
    versioned_logs = [path for path in log_files if path.parent.parent.name == "logs"]
    log_dirs = {path.parent for path in [*versioned_logs, *event_files]}
    if log_dirs:
        return sorted(log_dirs)[-1]
    if log_files:
        return sorted(log_files)[-1]

    session_dirs = list(iter_session_dirs(session_id=session_id))
    for session_dir in session_dirs:
        md_files = list(session_dir.glob("*.md"))
        if md_files:
            return sorted(md_files)[-1]

    if session_dirs:
        return sorted(session_dirs)[-1]

    return None


def render_json_trace(path: Path) -> str:
    """Render a session JSON file as readable, pretty-printed text."""
    raw = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return json.dumps(parsed, indent=2, ensure_ascii=False)


def load_trace(trace_path: Path) -> str:
    """Load trace content from a file or directory, rendering JSON readably."""
    if trace_path.is_file():
        if trace_path.suffix == ".json":
            return render_json_trace(trace_path)
        return trace_path.read_text(encoding="utf-8")

    if trace_path.is_dir():

        def rendered(f: Path) -> str:
            body = (
                render_json_trace(f)
                if f.suffix == ".json"
                else f.read_text(encoding="utf-8")
            )
            return f"--- {f.name} ---\n{body}"

        return "\n\n".join(
            rendered(f) for f in sorted(trace_path.glob("*")) if f.is_file()
        )

    return ""


def render_tool_calls(trace_path: Path) -> str:
    """Render a trace's tool-call timeline from its typed events.

    One line per call — ✓/✗ status, tool name, result brief — read from the
    events sidecar (or the legacy-markdown fallback for traces predating it).
    A log directory resolves to its newest reasoning trace. Browser-only logs
    and session JSON carry no tool-call stream; ``--full`` is their view.
    """
    if trace_path.is_dir():
        markdown = sorted(trace_path.glob("*.md"))
        if markdown:
            trace_path = markdown[-1]
    if trace_path.suffix != ".md":
        return "(no tool-call events for this trace format — use --full)"
    lines = [
        f"{'✓' if event.ok else '✗'} {event.tool or 'unknown'}  {event.brief}"
        for event in events_for_trace(trace_path)
        if event.kind == "tool_call"
    ]
    if not lines:
        return "(no tool calls recorded)"
    return "\n".join(lines)


# ── CLI commands ──────────────────────────────────────────


def show(session_id: str, full: bool, tool_calls: bool, as_json: bool) -> None:
    """Show trace for a session."""
    trace_path = find_trace(session_id)

    if not trace_path:
        refuse(
            f"no trace is recorded for this session under {traces_path()}",
            what=session_id,
            steps=[step("list the recorded traces", devtools("trace", "list"))],
        )

    content = load_trace(trace_path)

    match (tool_calls, full):
        case (True, _):
            content = render_tool_calls(trace_path)
        case (False, False):
            lines = content.split("\n")
            if len(lines) > HEAD_LINES:
                # lup: ignore[silent-truncation] — `--full` is the whole trace,
                # and the line below says how much this view is standing in for
                head = "\n".join(lines[:HEAD_LINES])
                content = (
                    f"{head}\n\n… {len(lines) - HEAD_LINES} more line(s) — "
                    f"re-run with --full for the whole trace"
                )
        case _:
            pass

    if as_json:
        output_json(
            {
                "session_id": session_id,
                "path": str(trace_path),
                "content": content,
            }
        )
        return

    typer.echo(f"\n=== Trace for {session_id} ===")
    typer.echo(f"Path: {trace_path}\n")
    typer.echo(content)
    if not full and not tool_calls:
        lines = load_trace(trace_path).split("\n")
        if len(lines) > 100:
            typer.echo(f"\n... ({len(lines) - 100} more lines)")
            typer.echo("Use --full to see complete trace")


def search(pattern: str, context: int, as_json: bool) -> None:
    """Search traces for a pattern."""
    if not traces_path().exists():
        if as_json:
            output_json({"matches": [], "total": 0})
        else:
            typer.echo("0 matches found")
        return

    regex = re.compile(pattern, re.IGNORECASE)
    matches: list[SearchMatch] = []  # lup: ignore[empty-collection] — match fold

    trace_files = list(traces_path().rglob("*.md")) + list(
        traces_path().rglob("*.json")
    )
    for trace_file in trace_files:
        try:
            content = trace_file.read_text(encoding="utf-8")
            lines = content.split("\n")

            for i, line in enumerate(lines):
                if regex.search(line):
                    start = max(0, i - context)
                    end = min(len(lines), i + context + 1)
                    matches.append(
                        {
                            "file": display_path(trace_file),
                            "line": i + 1,
                            "context": lines[start:end],
                        }
                    )
        except OSError as e:
            typer.echo(f"Error reading {trace_file}: {e}", err=True)

    if as_json:
        output_json({"matches": matches, "total": len(matches)})
        return

    for m in matches:
        typer.echo(f"\n--- {m['file']}:{m['line']} ---")
        match_idx = m["line"] - 1
        start_line = match_idx - context
        for j, ctx_line in enumerate(m["context"]):
            actual_line = start_line + j
            prefix = ">>> " if actual_line == match_idx else "    "
            typer.echo(f"{prefix}{ctx_line}")

    typer.echo(f"\n{len(matches)} matches found")


def errors_in_traces(
    limit: int,
    effective: list[str] | None,
    as_json: bool,
) -> None:
    """Show sessions with errors found by regex in trace markdown files."""
    results = scan_for_errors(effective)

    if as_json:
        output_json({"sessions": results[:limit], "total": len(results)})
        return

    if not results:
        typer.echo("No errors found in traces")
        return

    typer.echo(f"\n=== Trace Errors ({len(results)} sessions) ===\n")

    for entry in results[:limit]:
        typer.echo(f"{entry['session_id']}: {entry['error_count']} errors")
        for line in entry["errors"][:3]:
            typer.echo(f"  - {line}")
        if len(entry["errors"]) > 3:
            typer.echo(f"  ... and {len(entry['errors']) - 3} more")
        typer.echo()


def entry_recency(path: Path) -> datetime:
    """Newest timestamp parsed from contained filenames, falling back to mtime."""
    try:
        names = [f.name for f in path.iterdir()]
    except OSError:
        names = []
    stamps: list[datetime] = []
    for name in names:
        try:
            stamps.append(parse_timestamp(name))
        except ValueError:
            continue
    if stamps:
        return max(stamps)
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return datetime.min


def list_traces(limit: int, effective: list[str] | None, as_json: bool) -> None:
    """List available traces, most recent first."""
    raw: list[TraceRef] = []  # lup: ignore[empty-collection] — discovery fold

    versions_iter = effective if effective else [None]
    for ver in versions_iter:
        for session_dir in iter_session_dirs(version=ver):
            raw.append(
                TraceRef(
                    source="sessions", session_id=session_dir.name, path=session_dir
                )
            )
        for log_file in iter_trace_log_files(version=ver):
            raw.append(
                TraceRef(
                    source="logs", session_id=log_file.parent.name, path=log_file.parent
                )
            )
        for event_file in iter_trace_event_files(version=ver):
            raw.append(
                TraceRef(
                    source="logs",
                    session_id=event_file.parent.name,
                    path=event_file.parent,
                )
            )

    if not raw:
        if as_json:
            output_json({"traces": [], "total": 0})
        else:
            typer.echo("No traces found")
            typer.echo(f"Checked: {traces_path()}")
        return

    seen: dict[str, TraceRef] = {}  # lup: ignore[empty-collection] — dedup fold
    for ref in raw:
        if ref.session_id not in seen or ref.source == "logs":
            seen[ref.session_id] = ref

    unique = sorted(
        seen.values(), key=lambda row: entry_recency(row.path), reverse=True
    )

    def trace_row(ref: TraceRef) -> TraceRow:
        files = list(ref.path.glob("*"))
        size = sum(f.stat().st_size for f in files if f.is_file())
        return {
            "session_id": ref.session_id,
            "source": ref.source,
            "backend": session_backend(ref.path),
            "files": len(files),
            "size_kb": round(size / 1024, 1),
        }

    entries = [trace_row(ref) for ref in unique[:limit]]

    if as_json:
        output_json({"traces": entries, "total": len(unique)})
        return

    typer.echo(f"\n=== Available Traces ({len(unique)} total) ===\n")
    for e in entries:
        backend = e["backend"] or "—"
        typer.echo(
            f"{e['session_id']} ({e['source']}, {backend}): "
            f"{e['files']} files, {e['size_kb']}KB"
        )


def capabilities(as_json: bool) -> None:
    """Surface the tools and capabilities the agent wished it had.

    Scans trace logs for moments where the agent reached for something it
    could not do ("would be useful", "if I could", "tool that ...", "need
    access to ...") and reports them deduplicated, most-requested first.

    This is the feedback loop's primary lever: the Bitter Lesson says the agent
    improves by gaining tools, not rules (CLAUDE.md), so a recurring capability
    request across sessions is the strongest signal of a missing tool to build.
    The phrasing scan is heuristic; treat the output as leads to read in full
    via ``trace show``, not a finished backlog.
    """
    results = scan_for_capability_gaps()

    if as_json:
        output_json({"requests": results, "total": len(results)})
        return

    if not results:
        typer.echo("No capability requests found in traces")
        return

    typer.echo(f"\n=== Capability Requests ({len(results)} found) ===\n")

    for req in results:
        typer.echo(f"- {req['text']}")


# ── launch transcripts: the hash-chained journal, checked before it is read ──


class TranscriptCheck(BaseModel, frozen=True):
    """One launch transcript's chain, as it reads now."""

    run: str
    """The run directory's name: when it opened, which runtime, and its id."""

    path: Path
    records: int
    broken: ChainBreak | None = None

    def verdict(self) -> str:
        """``intact``, or where and how the chain breaks."""
        return "✓ intact" if self.broken is None else f"✗ {self.broken.explained()}"


class TranscriptEvents(BaseModel, frozen=True):
    """One transcript read whole: its chain, and the records asked for."""

    check: TranscriptCheck
    events: list[ObservableEvent]


def checked(path: Path, events: Sequence[ObservableEvent]) -> TranscriptCheck:
    """One transcript's records, verified."""
    return TranscriptCheck(
        run=path.parent.name,
        path=path,
        records=len(events),
        broken=chain_break(events),
    )


def transcript_of(reference: str) -> Path:
    """The transcript a reference names: a run id, a run directory, or the file.

    A run id is looked for under the default runs root, where a launch writes
    unless its mode named another; a run kept anywhere else is named by path.
    """
    named = Path(reference)
    if named.is_file():
        return named
    if named.is_dir():
        return run_transcript(named)
    found = [run_transcript(run) for run in iter_run_dirs(run_id=reference)]
    match found:
        case [path] if path.is_file():
            return path
        case [path]:
            refusal = f"run {reference} wrote no transcript at {path}"
        case _:
            refusal = (
                f"no launch run named {reference} under {harness_runs_path()}; "
                "`trace verify` lists every run there, and a run kept elsewhere "
                "is named by its path"
            )
    typer.echo(refusal, err=True)
    raise typer.Exit(1)


def verify_transcripts(references: Sequence[str], as_json: bool) -> None:
    """Check each named transcript's chain, or every launch's, and fail on a break."""
    paths = (
        [transcript_of(reference) for reference in references]
        if references
        else [
            path for run in iter_run_dirs() if (path := run_transcript(run)).is_file()
        ]
    )
    checks = [checked(path, read_observable_events(path)) for path in paths]
    intact = [check for check in checks if check.broken is None]
    match as_json, checks:
        case True, _:
            output_json([check.model_dump(mode="json") for check in checks])
        case False, []:
            typer.echo(f"No launch transcripts under {harness_runs_path()}")
        case False, _:
            rows = [
                [check.run, str(check.records), check.verdict()] for check in checks
            ]
            typer.echo(
                format_table(
                    ("Run", "Records", "Chain"),
                    rows,
                    aligns=("left", "right", "left"),
                )
            )
            typer.echo(f"\n{len(intact)} of {len(checks)} transcript(s) verify")
    if len(intact) < len(checks):
        raise typer.Exit(1)


def transcript_events(reference: str, kinds: Sequence[str], as_json: bool) -> None:
    """Read one transcript's records, its chain checked and said first.

    Where the chain breaks, a line says so at the record it breaks on, so a
    reader sees which of what follows was vouched for and which was not.
    """
    path = transcript_of(reference)
    every = read_observable_events(path)
    check = checked(path, every)
    if as_json:
        selected = [event for event in every if not kinds or event.kind in kinds]
        output_json(TranscriptEvents(check=check, events=selected))
        return
    boundary = check.broken.position if check.broken is not None else None
    typer.echo(f"{check.path}\n{check.records} record(s), chain {check.verdict()}\n")
    for position, event in enumerate(every):
        if position == boundary:
            typer.echo(
                f"── the chain breaks here, at record {position}: what follows "
                "is not vouched for by what came before ──"
            )
        if kinds and event.kind not in kinds:
            continue
        moment = datetime.fromisoformat(event.timestamp)
        payload = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
        typer.echo(
            f"{event.seq:>6} {moment:%Y-%m-%d %H:%M:%S} "
            f"{event.actor.kind}:{event.actor.name} {event.kind} {payload}"
        )
