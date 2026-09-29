"""Color-coded console display of agent content blocks.

Real-time output for interactive sessions: tool use and tool result blocks
are linked by a rotating color so a result visibly pairs with its call. Pass
a ``TraceLogger`` via *trace* to also accumulate a markdown trace in the same
call, or use the logger's methods directly for trace-only logging.

Examples:
    Display a block with color-coded tool pairing::

        >>> print_block(block, prefix="  ")

    Display and trace together::

        >>> trace = TraceLogger(trace_path=Path("/tmp/trace.md"), title="Session 1")
        >>> print_block(block, trace=trace)
"""

import itertools
import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel
from rich.console import Console

from lup.observability.blocks import extract_block_info, format_tool_result
from lup.types import LupContentBlock

if TYPE_CHECKING:
    from lup.observability.trace import TraceLogger

TOOL_COLORS = [
    # The rotation a console gets when its caller expresses no preference:
    # every terminal-safe hue, ordered so adjacent tool pairings contrast.
    "cyan",
    "green",
    "yellow",
    "magenta",
    "blue",
    "red",
    "bright_cyan",
    "bright_green",
    "bright_yellow",
    "bright_magenta",
    "bright_blue",
    "bright_red",
]


def format_duration(seconds: float) -> str:
    """Format a duration for console display.

    Durations that round to under a minute keep tenths (``42.3s``);
    anything longer reads as minutes plus whole seconds (``3m 7s``) —
    session runs routinely cross the minute mark, where raw seconds stop
    being scannable. Rounding precedes the split, so no remainder reads
    as a full ``60s`` and ``59.7`` crosses over to ``1m 0s``.
    """
    minutes, remainder = divmod(round(seconds), 60)
    if minutes >= 1:
        return f"{int(minutes)}m {remainder:.0f}s"
    return f"{seconds:.1f}s"


class ColorAssigner:
    """Rotating tool-color state for tool use / result pairing.

    One instance per concurrent output stream: sharing an assigner
    across sessions interleaves the palette rotation. ToolUseBlock
    assigns a color; the matching ToolResultBlock pops it. The
    module-level :data:`DEFAULT_COLORS` keeps the single-session
    default; pass a dedicated instance to ``print_block`` for concurrent
    streams (background agents, relay watchers) so their pairings can't
    cross.
    """

    def __init__(self, palette: list[str] | None = None) -> None:
        self.cycle = itertools.cycle(palette or TOOL_COLORS)
        # Open tool-use-id -> color map, filled as blocks arrive.
        self.by_id: dict[str, str] = {}  # lup: ignore[dict-str-payload]


DEFAULT_COLORS = ColorAssigner()
console = Console(highlight=False, markup=False)
stream_log = logging.getLogger("lup.agent.stream")


class ColorTag(BaseModel):
    """Color-coded identifier for tool use / result pairing."""

    id: str
    color: str


def resolve_color_tag(
    block: LupContentBlock, colors: ColorAssigner = DEFAULT_COLORS
) -> ColorTag | None:
    """Assign or retrieve a color for tool use/result pairing.

    A block that opens a pairing takes a fresh color from the rotating
    palette, stored by the id it opened; the block that closes that pairing
    pops the same color back. A block that does neither has no colored tag.
    """
    if (opened := block.opens_pairing) is not None:
        color = next(colors.cycle)
        colors.by_id[opened] = color
        return ColorTag(id=opened, color=color)
    if (closed := block.closes_pairing) is not None:
        return ColorTag(id=closed, color=colors.by_id.pop(closed, "default"))
    return None


def print_block(
    block: LupContentBlock,
    prefix: str = "",
    trace: "TraceLogger | None" = None,
    colors: ColorAssigner | None = None,
) -> None:
    """Print a content block with color-coded tool use/result pairing.

    LupToolUseBlock and LupToolResultBlock are linked by color: when a
    tool use is printed, its ID is assigned a color from a rotating
    palette. When the corresponding result arrives, the same color is
    used, making it easy to visually pair them. Concurrent streams pass
    their own :class:`ColorAssigner` so pairings can't cross.

    If *trace* is provided, the block is also logged to the trace.
    """
    info = extract_block_info(block)
    tag = resolve_color_tag(block, colors or DEFAULT_COLORS)

    payload = block.result_payload
    display_content = info.content if payload is None else format_tool_result(payload)

    if tag:
        print(f"{prefix}{info.emoji} {info.label} ", end="")
        console.print(f"[{tag.id}]", style=tag.color)
        if display_content:
            print(display_content)
    else:
        print(f"{prefix}{info.emoji} {display_content}")

    stream_log.info("%s%s", prefix, block.log_summary(display_content))

    if trace:
        trace.log_block(block)
