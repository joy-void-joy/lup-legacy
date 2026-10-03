"""Trace formatting, the TraceLogger save path, and the console display.

The trace file is the feedback loop's raw material: if block formatting,
entry accumulation, or the save round-trip break, every downstream
analysis reads garbage. The console is the other half of the same
traversal — one call feeds both sinks — so the display assembly is
covered here too, against the text it actually wrote rather than against
the fact that writing it raised nothing.
"""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from lup.observability.blocks import JsonValue, format_tool_result, truncate_str_fields
from lup.observability.display import (
    TOOL_COLORS,
    ColorAssigner,
    format_duration,
    print_block,
    resolve_color_tag,
)
from lup.observability.trace import (
    TraceLogger,
    format_block_markdown,
    trace_events,
)
from lup.types import (
    LupTextBlock,
    LupThinkingBlock,
    LupToolResultBlock,
    LupToolUseBlock,
)


def test_logger_accumulates_and_saves(tmp_path: Path) -> None:
    trace_path = tmp_path / "logs" / "trace.md"
    logger = TraceLogger(trace_path=trace_path, title="Session X")

    logger.log_block(LupTextBlock(text="hello"))
    logger.log_text("raw note", heading="Note")
    saved = logger.save()

    assert saved == trace_path
    content = trace_path.read_text(encoding="utf-8")
    assert content.startswith("# Trace: Session X")
    assert "hello" in content
    assert "## Note" in content
    # Two header entries plus the two logged entries, indexed in order
    assert [entry.index for entry in logger.entries] == [0, 1, 2, 3]


def test_save_renders_file_from_entries(tmp_path: Path) -> None:
    trace = TraceLogger(trace_path=tmp_path / "deep" / "t.md", title="Session X")
    trace.log_text("hello world", heading="Step")
    trace.log_text("plain text")

    saved = trace.save()

    content = saved.read_text(encoding="utf-8")
    assert content == "\n".join(entry.content for entry in trace.entries)
    assert "# Trace: Session X" in content
    assert "## Step" in content
    assert "plain text" in content


def test_read_entries_slices_with_negative_index(tmp_path: Path) -> None:
    logger = TraceLogger(trace_path=tmp_path / "t.md", title="S")
    for n in range(3):
        logger.log_text(f"entry {n}")

    tail = logger.read_entries(after_n=-2)

    assert len(tail) == 2
    assert "entry 2" in tail[-1].content


def test_read_entries_slices_head_with_before_n(tmp_path: Path) -> None:
    trace = TraceLogger(trace_path=tmp_path / "t.md", title="T")
    for i in range(5):
        trace.log_text(f"entry {i}")

    head = trace.read_entries(before_n=2)
    assert len(head) == 2
    assert head[0].content.startswith("# Trace: T")


def test_block_markdown_fences_by_block_type() -> None:
    tool_use = format_block_markdown(
        LupToolUseBlock(id="t1", name="Read", input={"file_path": "x"})
    )
    tool_result = format_block_markdown(
        LupToolResultBlock(tool_use_id="t1", content="data")
    )
    thinking = format_block_markdown(LupThinkingBlock(thinking="", redacted=True))

    assert "```json" in tool_use
    assert "Tool: Read" in tool_use
    assert "```" in tool_result
    assert "[redacted]" in thinking


# ── console display ───────────────────────────────────────────────────────
#
# What a watching human reads. Rendering the wrong prefix, the wrong tool
# name, or an untruncated payload raises nothing, so these assert the text
# the display wrote. Capture is taken off the process streams, where the
# plain prints and the rich console both land, so it reads what a terminal
# would get and no caller added later can route around it. Behind the text
# sits the pairing state, which must not leak between concurrent streams.


def displayed(capsys: pytest.CaptureFixture[str]) -> str:
    """Everything the display wrote, across both console streams."""
    captured = capsys.readouterr()
    return captured.out + captured.err


def test_duration_keeps_tenths_below_the_minute() -> None:
    assert format_duration(42.34) == "42.3s"


def test_duration_reads_as_minutes_from_the_boundary_up() -> None:
    assert format_duration(60.0) == "1m 0s"
    assert format_duration(187.0) == "3m 7s"


def test_duration_rounds_before_splitting_off_the_minutes() -> None:
    """A remainder that rounds up to a whole minute carries into the minutes
    instead of reading as the ``3m 60s`` no clock shows."""
    assert format_duration(239.7) == "4m 0s"
    assert format_duration(59.7) == "1m 0s"


def test_tool_use_takes_the_next_color_and_its_result_pops_it() -> None:
    colors = ColorAssigner(palette=["cyan", "green"])

    first = resolve_color_tag(LupToolUseBlock(id="a", name="Read"), colors)
    second = resolve_color_tag(LupToolUseBlock(id="b", name="Grep"), colors)
    paired = resolve_color_tag(LupToolResultBlock(tool_use_id="a", content="x"), colors)

    assert first is not None and second is not None and paired is not None
    assert [first.color, second.color] == ["cyan", "green"]
    assert paired.color == first.color
    # The closing block pops its pairing; the still-open one keeps its color.
    assert colors.by_id == {"b": "green"}


def test_print_block_passes_the_resolved_color_to_the_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The color resolved for a tag reaches the console rendering boundary."""
    from lup.observability import display

    rendered = Mock()
    monkeypatch.setattr(display, "console", rendered)
    print_block(
        LupToolUseBlock(id="a", name="Read"),
        colors=ColorAssigner(palette=["red"]),
    )

    rendered.print.assert_called_once_with("[a]", style="red")


def test_result_without_an_open_call_falls_back_to_default() -> None:
    colors = ColorAssigner()

    orphan = resolve_color_tag(
        LupToolResultBlock(tool_use_id="never-opened", content="x"), colors
    )

    assert orphan is not None
    assert orphan.color == "default"
    # A block that neither opens nor closes a pairing carries no tag at all.
    assert resolve_color_tag(LupTextBlock(text="plain"), colors) is None


def test_palette_wraps_at_its_end() -> None:
    colors = ColorAssigner()

    rotation = [
        resolve_color_tag(LupToolUseBlock(id=f"t{n}", name="Read"), colors)
        for n in range(len(TOOL_COLORS) + 1)
    ]

    assert [tag.color for tag in rotation if tag] == [*TOOL_COLORS, TOOL_COLORS[0]]


def test_assigners_rotate_and_pair_independently() -> None:
    """Concurrent streams each hold their own rotation: two assigners open at
    the same color, and a result resolved against the wrong one can't pair."""
    own = ColorAssigner()
    other = ColorAssigner()

    mine = resolve_color_tag(LupToolUseBlock(id="iso-1", name="Read"), own)
    theirs = resolve_color_tag(LupToolUseBlock(id="iso-2", name="Read"), other)
    crossed = resolve_color_tag(
        LupToolResultBlock(tool_use_id="iso-1", content="x"), other
    )
    paired = resolve_color_tag(
        LupToolResultBlock(tool_use_id="iso-1", content="x"), own
    )

    assert mine is not None and theirs is not None
    assert crossed is not None and paired is not None
    # Neither assigner advanced the other's cycle, so both opened at the head.
    assert mine.color == theirs.color == TOOL_COLORS[0]
    assert crossed.color == "default"
    assert paired.color == mine.color


def test_orphan_result_still_renders(capsys: pytest.CaptureFixture[str]) -> None:
    print_block(
        LupToolResultBlock(tool_use_id="never-opened", content="data"),
        colors=ColorAssigner(),
    )

    shown = displayed(capsys)
    assert "📋 Result" in shown
    assert "[never-opened]" in shown
    assert "data" in shown


def test_tool_use_renders_prefix_name_and_pairing_tag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_block(
        LupToolUseBlock(id="call-1", name="Read", input={"file_path": "x.py"}),
        prefix="  ",
        colors=ColorAssigner(),
    )

    shown = displayed(capsys)
    assert shown.startswith("  🔧 Tool: Read ")
    assert "[call-1]" in shown
    assert '"file_path": "x.py"' in shown


def test_tool_result_renders_formatted_and_truncated_payload(
    capsys: pytest.CaptureFixture[str],
) -> None:
    colors = ColorAssigner()
    payload = json.dumps({"body": "x" * 600})

    print_block(LupToolUseBlock(id="call-1", name="Read"), colors=colors)
    print_block(
        LupToolResultBlock(tool_use_id="call-1", content=payload), colors=colors
    )

    shown = displayed(capsys)
    # The result carries the tag of the call it closes, so a reader pairs the
    # two by eye.
    assert shown.count("[call-1]") == 2
    assert "📋 Result" in shown
    # JSON is re-indented rather than echoed as the one line it arrived on,
    # and its long field is cut back to a truncation signal.
    assert '{\n  "body"' in shown
    assert "..." in shown
    assert "x" * 600 not in shown


def test_prose_blocks_render_their_glyph_and_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_block(LupTextBlock(text="hello there"), prefix="| ")
    print_block(LupThinkingBlock(thinking="weighing it"))
    print_block(LupThinkingBlock(thinking="", redacted=True))

    shown = displayed(capsys)
    assert "| 💬 hello there" in shown
    assert "💭 weighing it" in shown
    assert "💭 [redacted]" in shown


def test_trace_argument_accumulates_what_it_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One call feeds both sinks: every block reaches the trace in the order
    the console showed it, each sink rendering that block by its own rule."""
    trace = TraceLogger(trace_path=tmp_path / "t.md", title="S")
    header = len(trace.entries)
    payload = "y" * 600
    blocks = [
        LupTextBlock(text="reading it"),
        LupToolUseBlock(id="fan-1", name="Read", input={"file_path": "x.py"}),
        LupToolResultBlock(tool_use_id="fan-1", content=payload),
    ]
    colors = ColorAssigner()

    for block in blocks:
        print_block(block, trace=trace, colors=colors)

    shown = displayed(capsys)
    # One entry per block, paired positionally: a trace written from its own
    # second walk could drift in count or order without the console noticing.
    for block, entry in zip(blocks, trace.entries[header:], strict=True):
        assert block.display_emoji in shown
        assert block.display_label in entry.content
        assert block.display_body in entry.content
    # The sinks part only where their jobs do: a reader gets the payload cut
    # to a signal, the archive keeps it whole. A payload short enough to
    # survive truncation would let the two agree by accident.
    assert payload not in shown
    assert payload[:500] + "..." in shown


def test_tool_result_formatting_truncates_inside_json() -> None:
    long_value = "x" * 600
    formatted = format_tool_result(f'{{"key": "{long_value}"}}')

    assert formatted.startswith("{")
    assert "..." in formatted
    assert len(formatted) < 600


def test_truncate_str_fields_caps_lists() -> None:
    truncated = truncate_str_fields([str(n) for n in range(50)], max_len_list=10)

    assert isinstance(truncated, list)
    assert len(truncated) == 10


def test_truncate_keeps_list_limit_in_nested_structures() -> None:
    data: JsonValue = {"outer": [[f"x{i}" for i in range(20)]]}

    result = truncate_str_fields(data, max_len=500, max_len_list=3)

    assert isinstance(result, dict)
    outer = result["outer"]
    assert isinstance(outer, list)
    inner = outer[0]
    assert isinstance(inner, list)
    assert len(inner) == 3


def test_truncate_shortens_nested_strings() -> None:
    data: JsonValue = {"a": {"b": ["y" * 100, 7]}}

    result = truncate_str_fields(data, max_len=10, max_len_list=5)

    assert isinstance(result, dict)
    a = result["a"]
    assert isinstance(a, dict)
    b = a["b"]
    assert isinstance(b, list)
    assert b[0] == "y" * 10 + "..."
    # A scalar has no length to cap, so it passes through as itself.
    assert b[1] == 7


# ── structured JSONL sidecar ──────────────────────────────────────────────
#
# Analysis reads this sidecar instead of regex-scanning markdown, so the
# write→read-back round-trip is the contract that must hold: events appended
# live must parse back to the same typed objects, with tool name/ok and
# capability requests preserved.


def test_sidecar_roundtrips_events(tmp_path: Path) -> None:
    trace = TraceLogger(trace_path=tmp_path / "logs" / "t.md", title="S")

    trace.log_block(LupToolUseBlock(id="a", name="search", input={"q": "x"}))
    trace.log_block(LupToolResultBlock(tool_use_id="a", content='{"ok": true}'))
    trace.log_block(LupToolUseBlock(id="b", name="fetch", input={}))
    trace.log_block(LupToolResultBlock(tool_use_id="b", content='{"is_error": true}'))
    trace.log_block(LupTextBlock(text="A tool that lints would be useful."))

    # Round-trip: read the sidecar back into typed events.
    events = trace_events(trace.events_path).read_all()
    assert events == trace.events

    by_kind = {e.kind for e in events}
    assert by_kind == {"tool_call", "error", "capability_request"}

    ok_call = next(e for e in events if e.kind == "tool_call" and e.tool == "search")
    assert ok_call.ok is True
    error = next(e for e in events if e.kind == "error")
    assert error.tool == "fetch"
    request = next(e for e in events if e.kind == "capability_request")
    assert "lints" in request.brief


def test_sidecar_appends_live_before_save(tmp_path: Path) -> None:
    """A crash before save() still leaves the flushed events readable."""
    trace = TraceLogger(trace_path=tmp_path / "t.md", title="S")
    trace.log_block(LupToolUseBlock(id="a", name="search", input={}))
    trace.log_block(LupToolResultBlock(tool_use_id="a", content="done"))

    # No save() called — sidecar already has the line.
    events = trace_events(trace.events_path).read_all()
    assert [e.kind for e in events] == ["tool_call"]


def test_save_touches_empty_sidecar(tmp_path: Path) -> None:
    """A run with no tool calls still gets a sidecar, so analysis treats it
    as structured rather than falling back to line-scanning its markdown."""
    trace = TraceLogger(trace_path=tmp_path / "t.md", title="S")
    trace.log_text("just prose, nothing to record")
    trace.save()

    assert trace.events_path.exists()
    assert trace_events(trace.events_path).read_all() == []
