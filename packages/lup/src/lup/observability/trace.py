# lup: ignore[import-re, re-call]
# Capability phrasing and sentence bounds are natural-language cues with no
# structured parser — regex IS the tool here, so those rules are opted out
# file-wide.
"""Session trace accumulation for feedback-loop analysis.

``TraceLogger`` collects content blocks during an agent run and writes two
artifacts side by side: a human-readable markdown trace and a machine-readable
``.events.jsonl`` sidecar (one :class:`TraceEvent` per line — tool outcomes,
errors, and capability requests) that analysis loads as validated objects
instead of regex-scanning the markdown.

Typically driven through :func:`lup.observability.display.print_block` with a
*trace* argument for combined display and tracing; the ``log_*`` methods here
support trace-only logging without console output.

Examples:
    Log and save a trace::

        >>> trace = TraceLogger(trace_path=Path("/tmp/trace.md"), title="Session 1")
        >>> trace.log_block(block)
        >>> trace.save()
        PosixPath('/tmp/trace.md')
"""

import json
import logging
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from lup.observability.blocks import extract_block_info, truncate_str
from lup.types import LupContentBlock, normalize_content

logger = logging.getLogger(__name__)


def format_block_markdown(block: LupContentBlock) -> str:
    """Format a content block as markdown for trace logs."""
    info = extract_block_info(block)
    header = f"## {info.emoji} {info.label}"
    fence = block.markdown_fence
    if fence is None:
        return f"{header}\n\n{info.content}\n"
    return f"{header}\n\n```{fence}\n{info.content}\n```\n"


# Capability-request phrasing. Applied to one already-isolated string at the
# moment it is logged (an assistant text or meta thought), not swept across a
# markdown document — so the persisted signal is structured, not regex-derived.
CAPABILITY_PHRASES: re.Pattern[str] = re.compile(
    r"would be useful|would have helped|would benefit from|wish I (had|could)|"
    r"if (only )?I could|need(s|ed)? access to|cannot .* because|"
    r"a tool that|missing (a )?tool",
    re.IGNORECASE,
)


class TraceEvent(BaseModel):
    """A typed, machine-readable record of one thing analysis cares about.

    Written one-per-line to the ``.events.jsonl`` sidecar beside the human
    ``.md`` trace as the trace is built. ``kind`` discriminates the three
    signals the feedback loop reads — tool outcomes, errors/exceptions, and
    capability requests — so analysis loads validated objects instead of
    re-deriving them with regex over free-form markdown.
    """

    kind: Literal["tool_call", "error", "capability_request"]
    timestamp: str = Field(description="ISO timestamp when the event was recorded")
    tool: str | None = Field(
        default=None, description="Tool name (for tool_call events)"
    )
    ok: bool | None = Field(
        default=None, description="Whether the tool call succeeded (for tool_call)"
    )
    brief: str = Field(
        default="", description="Short human summary: tool result, error, or request"
    )


def tool_result_ok(content: str | Sequence[object] | None) -> bool:
    """Decide whether a tool result reports success.

    MCP errors are carried inside the result content as ``is_error``; absent
    that flag a result is treated as successful. Reads the structured payload
    instead of keyword-scanning the rendered text.
    """
    text = normalize_content(content)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return True
    match parsed:
        case {"is_error": err}:
            return not bool(err)
    return True


def capability_request_from_text(text: str) -> str | None:
    """Return the first capability-request sentence in *text*, or None.

    Splits on sentence boundaries and returns the matching fragment so the
    event's ``brief`` is the specific wish, not the whole block.
    """
    sentences = re.split(  # lup: ignore[string-split] — sentence bounds
        r"(?<=[.!?\n])\s+", text
    )
    for fragment in sentences:
        stripped = fragment.strip()
        if stripped and CAPABILITY_PHRASES.search(stripped):
            return truncate_str(stripped, 300)
    return None


def read_trace_events(events_path: Path) -> list[TraceEvent]:
    """Read a ``.events.jsonl`` sidecar into validated :class:`TraceEvent`s.

    Skips blank or malformed lines so a truncated tail (a crash mid-write)
    never loses the events that were flushed before it.
    """
    events: list[TraceEvent] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(TraceEvent.model_validate_json(line))
        except ValueError:
            logger.warning("Skipping malformed trace event line in %s", events_path)
    return events


class TraceEntry(BaseModel):
    """A single indexed entry in a session trace."""

    index: int = Field(description="0-based entry index")
    timestamp: str = Field(description="ISO timestamp when entry was logged")
    content: str = Field(description="Markdown content for this entry")


class TraceLogger(BaseModel, arbitrary_types_allowed=True):
    """Accumulates agent reasoning for feedback loop analysis.

    Collects content blocks during agent execution and saves them
    as a markdown trace file for later analysis. ``entries`` is the
    single store: ``save()`` renders the markdown stream from it, and
    ``read_entries()`` slices it for in-session replay.

    Alongside the human ``.md`` trace it appends a machine-readable
    ``.events.jsonl`` sidecar — one :class:`TraceEvent` per line, flushed as
    each block is logged — so analysis reads typed tool/error/capability
    records instead of regex-scanning the markdown. The ``.md`` trace is the
    human view and is never weakened by the sidecar.

    Typically passed to ``print_block(block, trace=trace)`` for combined
    display and tracing. Methods like ``log_block`` and ``log_text``
    support trace-only logging without console output.
    """

    trace_path: Path = Field(description="Path to save the trace file")
    title: str = Field(description="Title for the trace")
    entries: list[TraceEntry] = []
    events: list[TraceEvent] = []
    # Open tool-use-id -> tool-name map, filled as blocks stream in.
    tool_names: dict[str, str] = Field(  # lup: ignore[dict-str-payload]
        default={}, exclude=True
    )

    def model_post_init(self, _context: object) -> None:
        """Initialize the trace with header."""
        if not self.entries:
            self.append_entry(f"# Trace: {self.title}\n")
            self.append_entry(f"*Generated: {datetime.now().isoformat()}*\n\n")

    @property
    def events_path(self) -> Path:
        """Path to the machine-readable JSONL sidecar beside the .md trace."""
        return self.trace_path.with_suffix(".events.jsonl")

    def append_entry(self, content: str) -> None:
        """Create and append a new trace entry."""
        self.entries.append(
            TraceEntry(
                index=len(self.entries),
                timestamp=datetime.now().isoformat(),
                content=content,
            )
        )

    def emit_event(self, event: TraceEvent) -> None:
        """Record a structured event and append it to the JSONL sidecar.

        Appended immediately so the sidecar stays usable even if the run
        crashes before :meth:`save`. A write failure is logged, never raised:
        the sidecar is diagnostic and must not break a live agent.
        """
        self.events.append(event)
        try:
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(event.model_dump_json() + "\n")
        except OSError:
            logger.exception("Failed to append trace event to %s", self.events_path)

    def log_block(self, block: LupContentBlock) -> None:
        """Add a formatted block to the trace and emit its structured event."""
        self.append_entry(format_block_markdown(block))
        self.emit_block_event(block)

    def emit_block_event(self, block: LupContentBlock) -> None:
        """Derive and emit the structured event(s) for a content block.

        Tool-use blocks record their name keyed by id; the paired tool-result
        block emits the ``tool_call`` event with name + ok/error + brief.
        Assistant text that voices a capability request emits one too.
        """
        now = datetime.now().isoformat()
        opened, invoked = block.opens_pairing, block.tool_call_name
        if opened is not None and invoked is not None:
            self.tool_names[opened] = invoked
        if (closed := block.closes_pairing) is not None:
            name = self.tool_names.pop(closed, "unknown")
            payload = block.result_payload
            ok = tool_result_ok(payload)
            brief = truncate_str(normalize_content(payload), 300)
            self.emit_event(
                TraceEvent(
                    kind="tool_call", timestamp=now, tool=name, ok=ok, brief=brief
                )
            )
            if not ok:
                self.emit_event(
                    TraceEvent(kind="error", timestamp=now, tool=name, brief=brief)
                )
        if (spoken := block.spoken_text) is not None:
            request = capability_request_from_text(spoken)
            if request is not None:
                self.emit_event(
                    TraceEvent(kind="capability_request", timestamp=now, brief=request)
                )

    def log_text(self, text: str, heading: str | None = None) -> None:
        """Add raw text to the trace, emitting a capability event if voiced.

        Meta assessments and reflections flow through here; when one names a
        missing tool or access, that wish is captured as a structured event.
        """
        if heading:
            self.append_entry(f"## {heading}\n\n{text}\n")
        else:
            self.append_entry(f"{text}\n")
        request = capability_request_from_text(text)
        if request is not None:
            self.emit_event(
                TraceEvent(
                    kind="capability_request",
                    timestamp=datetime.now().isoformat(),
                    brief=request,
                )
            )

    def read_entries(
        self,
        after_n: int | None = None,
        before_n: int | None = None,
    ) -> list[TraceEntry]:
        """Slice entries by index. Supports negative indexing.

        Lets a persistent agent replay recent trace context (e.g. the
        last N entries) without re-reading the saved file.
        """
        return self.entries[after_n:before_n]

    def save(self) -> Path:
        """Write the accumulated trace to file, rendered from entries.

        Events are appended to the sidecar live by :meth:`emit_event`; here we
        only ensure the sidecar exists so a completed run is always recognized
        as structured (and analysis never falls back to line-scanning it),
        even when it produced no tool calls.
        """
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.trace_path.write_text(
            "\n".join(entry.content for entry in self.entries), encoding="utf-8"
        )
        self.events_path.touch()
        logger.info("Saved trace to %s", self.trace_path)
        return self.trace_path
