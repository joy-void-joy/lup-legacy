"""Pure rendering for the usage display: pacing bars, days, and the panel.

Formats bars, labels, window sections, the daily breakdown, and the assembled
panel from an already-read :class:`UsageReport` — no I/O and no runtime's own
vocabulary. Which windows exist, what a day cost, and how a model family is
named are all answered by whichever reader filled the report.
"""

from collections import Counter
from datetime import date, datetime, timedelta
from itertools import accumulate

from pydantic import BaseModel
from rich.panel import Panel
from rich.text import Text

from lup.observability.usage.models import (
    DayUsage,
    ModelTokens,
    PacingWindow,
    SpendWindow,
    UsageReport,
)

DAY_NAMES = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
"""Two letters per weekday, from Monday, which a caller may spell otherwise."""

BAR_INDENT = 8
"""Matches the daily bar's prefix, so every bar starts at one column."""


class PaceLabel(BaseModel):
    word: str
    style: str


class PaceThreshold(BaseModel):
    """A pace label that applies up to (and including) a usage ratio."""

    up_to: float
    label: PaceLabel


PACE_LABEL_THRESHOLDS: list[PaceThreshold] = [
    PaceThreshold(
        up_to=0.5, label=PaceLabel(word="cruising", style="bold bright_green")
    ),
    PaceThreshold(
        up_to=0.85, label=PaceLabel(word="on track", style="bold bright_cyan")
    ),
    PaceThreshold(up_to=1.0, label=PaceLabel(word="on pace", style="bold bright_cyan")),
    PaceThreshold(up_to=1.3, label=PaceLabel(word="ahead", style="bold bright_yellow")),
    PaceThreshold(
        up_to=1.6, label=PaceLabel(word="running hot", style="bold bright_red")
    ),
]
PACE_LABEL_DEFAULT = PaceLabel(word="heavy usage", style="bold red")


# ── machine-readable snapshot ──────────────────────────────


class WindowSnapshot(BaseModel):
    """One metered window as counts and limits an agent can act on."""

    name: str
    utilization_pct: float
    pace: str
    resets_at: str
    resets_in_seconds: int


class SpendSnapshot(BaseModel):
    name: str
    used: float
    limit: float
    utilization_pct: float


class DaySnapshot(BaseModel):
    date: str
    total_tokens: int
    by_model: list[ModelTokens] = []
    message_count: int


class UsageSnapshot(BaseModel):
    """Full machine-readable usage state for ``--json`` consumers."""

    runtime: str
    windows: list[WindowSnapshot]
    spend: SpendSnapshot | None
    daily: list[DaySnapshot]
    by_model: list[ModelTokens]
    fresh_through: str | None


# ── formatting helpers ─────────────────────────────────────


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_countdown(dt: datetime) -> str:
    total_seconds = (dt - datetime.now(dt.tzinfo)).total_seconds()
    if total_seconds <= 0:
        return "now"
    h = int(total_seconds // 3600)
    m = int((total_seconds % 3600) // 60)
    if h >= 48:
        return f"{h // 24}d {h % 24}h"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


# `beyond` is what the lookup answers with past the last threshold, so the
# PaceLabel here is a result the caller supplies rather than a value acted on.
def pace_label(
    ratio: float,
    thresholds: list[PaceThreshold] = PACE_LABEL_THRESHOLDS,
    beyond: PaceLabel = PACE_LABEL_DEFAULT,
) -> PaceLabel:
    """Say where this ratio stands, in whatever words the caller grades with."""
    for threshold in thresholds:
        if ratio <= threshold.up_to:
            return threshold.label
    return beyond


def pace_color(ratio: float) -> str:
    return pace_label(ratio).style.split()[-1]


class WindowPace(BaseModel):
    """Where a window stands against even pace."""

    linear_pct: float
    ratio: float


def window_pace(window: PacingWindow) -> WindowPace:
    """Even-pace percent and the utilization-to-pace ratio for a window."""
    linear_pct = window.even_pct(datetime.now(window.resets_at.tzinfo))
    ratio = (window.utilization_pct / linear_pct) if linear_pct > 0 else 0
    return WindowPace(linear_pct=linear_pct, ratio=ratio)


def day_name(day: date, names: list[str] = DAY_NAMES) -> str:
    """Abbreviate one weekday, in whatever words the caller labels days with."""
    return names[day.weekday()]


def place_label(text: str, position: int, line_width: int) -> str:
    """Place a text label at a horizontal position in a fixed-width line."""
    line = [" "] * line_width
    for j, ch in enumerate(text):
        pos = position + j
        if 0 <= pos < line_width:
            line[pos] = ch
    return "".join(line)


# ── rendering ──────────────────────────────────────────────


def render_bar(
    out: Text,
    utilization: float,
    linear_pct: float,
    bar_width: int,
    indent: int = BAR_INDENT,
) -> None:
    """Render a pacing bar with actual fill and a linear-pace marker."""
    actual_frac = utilization / 100.0
    linear_frac = linear_pct / 100.0
    fill_color = pace_color(actual_frac / linear_frac if linear_frac > 0 else 0)

    actual_pos = min(int(actual_frac * bar_width), bar_width)
    linear_pos = min(int(linear_frac * bar_width), bar_width - 1)

    def cell(column: int) -> Text:
        """One column: the pace marker, the fill up to actual use, or empty room."""
        if column == linear_pos:
            return Text("▎", style="bright_black")
        if column < actual_pos:
            return Text("█", style=fill_color)
        return Text("░", style="bright_black")

    out.append(" " * indent)
    for column in range(bar_width):
        out.append(cell(column))
    out.append("\n")


def render_window(
    out: Text, window: PacingWindow, bar_width: int, indent: int = BAR_INDENT
) -> None:
    """Render one metered window: label, pacing bar, annotations."""
    utilization = window.utilization_pct
    pacing = window_pace(window)
    linear_pct, ratio = pacing.linear_pct, pacing.ratio

    pace = pace_label(ratio)

    out.append(f"  {window.label}", style="bold bright_white")
    out.append(f"  {utilization:.0f}%", style="bold")
    out.append(f"  ◆ {pace.word}", style=pace.style)
    out.append(f"  resets in {fmt_countdown(window.resets_at)}", style="dim")
    out.append("\n")

    render_bar(out, utilization, linear_pct, bar_width, indent)

    line_width = indent + bar_width

    you_text = f"↑ you ({utilization:.0f}%)"
    you_bar = min(int((utilization / 100) * bar_width), bar_width - len(you_text))
    out.append(place_label(you_text, indent + you_bar, line_width), style="dim")
    out.append("\n")

    pace_text = f"↑ even ({linear_pct:.0f}%)"
    pace_bar = min(int((linear_pct / 100) * bar_width), bar_width - len(pace_text))
    out.append(place_label(pace_text, indent + pace_bar, line_width), style="dim")
    out.append("\n")


def render_spend(
    out: Text, spend: SpendWindow, bar_width: int, indent: int = BAR_INDENT
) -> None:
    """Render the metered-spend section above the plan."""
    out.append(f"  {spend.label}", style="bold bright_white")
    out.append(f"  ${spend.used:.2f}", style="bold")
    out.append(f" / ${spend.limit:.2f}", style="dim")
    out.append(f"  ({spend.utilization_pct:.0f}%)", style="bold")
    out.append("\n")

    frac = spend.utilization_pct / 100
    fill_color = pace_color(frac)
    filled = min(int(frac * bar_width), bar_width)
    out.append(" " * indent)
    for i in range(bar_width):
        if i < filled:
            out.append("█", style=fill_color)
        else:
            out.append("░", style="bright_black")
    out.append("\n\n")


class EstimatedDay(BaseModel):
    """The one day a report that stops short has to infer rather than read."""

    index: int
    weight: float
    total_tokens: int


def estimated_today(
    daily: list[DayUsage], fresh_through: date, window_start: datetime
) -> EstimatedDay | None:
    """Infer today from what the report does cover, when it stops short.

    Assume the account spent at a steady rate across the window: the days the
    report covers then imply what the rest of it holds. That assumption is
    what breaks the circle between a budget scaled by the plan's utilization
    and a today whose weight is part of what the plan already counted.
    """
    now = datetime.now(window_start.tzinfo)
    today = now.date()
    if fresh_through >= today:
        return None
    standing = [
        index for index, day in enumerate(daily) if day.day == today and day.weight == 0
    ]
    if not standing:
        return None

    elapsed_h = (now - window_start).total_seconds() / 3600
    covered = [day for day in daily if day.day <= fresh_through]
    covered_h = sum(1 for day in covered if day.weight > 0) * 24.0
    covered_weight = sum(day.weight for day in covered)
    if not (covered_h > 0 and elapsed_h > covered_h and covered_weight > 0):
        return None

    unseen = (elapsed_h - covered_h) / covered_h
    return EstimatedDay(
        index=standing[0],
        weight=covered_weight * unseen,
        total_tokens=int(sum(day.total_tokens for day in covered) * unseen),
    )


def rolling_budgets(
    weights: list[float], days: list[date], total: float, today: date
) -> list[float]:
    """Split a period's budget across its days, carrying each day's surplus.

    Each day gets an even share plus whatever the days before it left unspent,
    so a heavy day eats into the days after it and a light one banks room —
    which is what makes the bars answer "can I keep going" rather than only
    "what did I use". Days still ahead spend nothing, so they carry the
    surplus forward untouched.

    ``today`` is taken rather than read, because the caller reads it in the
    window's own timezone: deciding it again here would put a day on the far
    side of midnight from the row the caller is drawing for it.
    """
    even = total / len(days) if days else 0.0
    spent = [
        weight if day <= today else None
        for weight, day in zip(weights, days, strict=True)
    ]
    surpluses = list(
        accumulate(
            spent,
            lambda carried, weight: (
                carried if weight is None else even + carried - weight
            ),
            initial=0.0,
        )
    )
    return [even + surplus for surplus in surpluses[: len(days)]]


def render_daily_breakdown(
    out: Text,
    report: UsageReport,
    window_end: datetime,
    plan_utilization: float,
    bar_width: int,
) -> None:
    """Render the per-day weighted breakdown over the reported window.

    ``window_end`` anchors the window (the plan's reset when known, otherwise
    now) and ``plan_utilization`` scales the budget: 0 falls back to weighing
    the period against itself, which still ranks the days against each other.
    """
    if not any(day.total_tokens > 0 for day in report.daily):
        return

    today = datetime.now(window_end.tzinfo).date()
    window_start = window_end - timedelta(days=len(report.daily))
    fresh_through = report.fresh_through
    stale = fresh_through is not None and fresh_through < today

    out.append("  per day", style="bold bright_white")
    if stale and fresh_through is not None:
        out.append(f"  (cache: {fresh_through.isoformat()})", style="dim italic")
    out.append("\n")

    estimate = (
        None
        if fresh_through is None
        else estimated_today(report.daily, fresh_through, window_start)
    )
    daily = [
        day
        if estimate is None or index != estimate.index
        else day.model_copy(
            update={"weight": estimate.weight, "total_tokens": estimate.total_tokens}
        )
        for index, day in enumerate(report.daily)
    ]

    weights = [day.weight for day in daily]
    period_weight = sum(weights)
    if period_weight > 0 and plan_utilization > 0:
        budget = period_weight / (plan_utilization / 100)
    else:
        budget = max(period_weight, 1)

    budgets = rolling_budgets(weights, [day.day for day in daily], budget, today)

    def heading(day: date) -> Text:
        """The row's weekday: marked where it is today, dim where it is still ahead."""
        name = day_name(day)
        if day == today:
            return Text.assemble(
                (f"  {name}", "bold bright_white"), (" ←  ", "bold bright_cyan")
            )
        if day > today:
            return Text(f"  {name}    ", style="dim")
        return Text(f"  {name}    ")

    def cell(column: int, fill_pos: int, pace_pos: int, fill: str, color: str) -> Text:
        """One column of a day's bar: its pace marker, its fill, or the room left."""
        if column == pace_pos:
            return Text("▎", style="bright_black")
        if column < fill_pos and column <= pace_pos:
            return Text(fill, style=color)
        if column < fill_pos:
            return Text("▒", style=color)
        if column < pace_pos:
            return Text("░", style="bright_black")
        return Text("░", style="black")

    for index, day in enumerate(daily):
        out.append(heading(day.day))

        if day.day > today:
            out.append("·" * bar_width, style="bright_black")
            out.append("\n")
            continue

        day_budget = budgets[index]
        fill_frac = day.weight / budget if budget > 0 else 0
        pace_frac = day_budget / budget if budget > 0 else 0
        fill_pos = min(int(fill_frac * bar_width), bar_width)
        pace_pos = min(max(int(pace_frac * bar_width), 0), bar_width - 1)
        ratio = (
            day.weight / day_budget
            if day_budget > 0
            else (2.0 if day.weight > 0 else 0)
        )
        color = pace_color(ratio)
        inferred = estimate is not None and index == estimate.index
        fill_char = "▓" if inferred else "█"

        for column in range(bar_width):
            out.append(cell(column, fill_pos, pace_pos, fill_char, color))

        tok_str = fmt_tokens(day.total_tokens)
        if inferred:
            out.append(f" ~{tok_str:>4}", style="bold dim")
        else:
            out.append(f" {tok_str:>5}", style="bold")
        if day.message_count > 0:
            out.append(f"  {day.message_count:,}m", style="dim")
        out.append("\n")

    out.append("\n")
    render_legend(out, report)


def render_legend(out: Text, report: UsageReport) -> None:
    """Render each model family's share of the period, where one is reported."""
    total = sum(share.tokens for share in report.legend)
    if total <= 0:
        return
    out.append("  models", style="bold bright_white")
    out.append("  ")
    for share in sorted(report.legend, key=lambda entry: entry.tokens, reverse=True):
        out.append(f"● {share.label} ", style=share.style)
        out.append(f"{share.tokens / total * 100:.0f}%  ", style="dim")
    out.append("\n")


# ── display assembly ───────────────────────────────────────


class BreakdownWindow(BaseModel):
    """The breakdown window's end and its budget-scaling utilization."""

    end: datetime
    utilization: float


def breakdown_window(report: UsageReport) -> BreakdownWindow:
    """Anchor the breakdown window and the utilization that scales its budget.

    The longest reported window gives the reset time and utilization when
    there is one; otherwise the window ends now and utilization is 0, so the
    breakdown still renders from the daily figures alone.
    """
    longest = max(report.windows, key=lambda window: window.window_hours, default=None)
    if longest is None:
        return BreakdownWindow(end=datetime.now(), utilization=0.0)
    return BreakdownWindow(end=longest.resets_at, utilization=longest.utilization_pct)


def panel(body: Text, runtime_name: str, border: str) -> Panel:
    """Frame one rendering under the name of the runtime that reported it."""
    return Panel(
        body,
        title=f"[bold bright_white]{runtime_name} Usage[/bold bright_white]",
        border_style=border,
        padding=(1, 1),
    )


def build_display(report: UsageReport, bar_width: int) -> Panel:
    # Common bar width so all bars are visually aligned. Daily bars need room
    # for the prefix ("  Sa    " = 8) and the suffix (" 400k" = 6).
    bar_w = bar_width - 14
    out = Text()

    for window in report.windows:
        render_window(out, window, bar_w)
        out.append("\n")

    if report.spend is not None:
        render_spend(out, report.spend, bar_w)

    if report.daily:
        window = breakdown_window(report)
        render_daily_breakdown(out, report, window.end, window.utilization, bar_w)

    return panel(out, report.runtime_name, "bright_cyan")


def window_snapshot(window: PacingWindow) -> WindowSnapshot:
    """One window as an agent reads it: percentages, pace, and a countdown."""
    remaining = window.resets_at - datetime.now(window.resets_at.tzinfo)
    return WindowSnapshot(
        name=window.label,
        utilization_pct=window.utilization_pct,
        pace=pace_label(window_pace(window).ratio).word,
        resets_at=window.resets_at.isoformat(),
        resets_in_seconds=max(int(remaining.total_seconds()), 0),
    )


def build_snapshot(report: UsageReport) -> UsageSnapshot:
    """Assemble the machine-readable usage state for ``--json`` consumers.

    Mirrors the display: the same windows, spend, and daily breakdown, but as
    plain counts and limits an agent can read instead of pacing bars.
    """
    tally: Counter[str] = Counter()
    for day in report.daily:
        for entry in day.by_model:
            tally[entry.model] += entry.tokens

    return UsageSnapshot(
        runtime=report.runtime_name,
        windows=[window_snapshot(window) for window in report.windows],
        spend=(
            None
            if report.spend is None
            else SpendSnapshot(
                name=report.spend.label,
                used=report.spend.used,
                limit=report.spend.limit,
                utilization_pct=report.spend.utilization_pct,
            )
        ),
        daily=[
            DaySnapshot(
                date=day.day.isoformat(),
                total_tokens=day.total_tokens,
                by_model=day.by_model,
                message_count=day.message_count,
            )
            for day in report.daily
        ],
        by_model=[
            ModelTokens(model=model, tokens=tokens) for model, tokens in tally.items()
        ],
        fresh_through=(
            None if report.fresh_through is None else report.fresh_through.isoformat()
        ),
    )


def build_error_panel(runtime_name: str, message: str) -> Panel:
    """Keep the frame while an attempt fails, so a watch loop stays readable."""
    out = Text()
    out.append(f"  {message}", style="red")
    out.append("\n  retrying...", style="dim")
    return panel(out, runtime_name, "red")
