"""Tests for the usage display, and for both readers that fill it.

The daily breakdown is fed by whatever daily figures a reader supplies, so it
must render even when the live windows expose no weekly bucket, in the direct
(non-watch) path as much as under watch. The ``--json`` snapshot must
surface the same windows and trailing-week tokens as machine-readable counts.

Codex is read the same way through its own account calls, which is the point:
one display, two readers, and no second rendering to keep in step.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from rich.console import Console

from lup.providers.codex.app_server import AppServerError, CodexAppServer, RpcError
from lup.providers.codex.usage.api import (
    METHOD_NOT_FOUND,
    RATE_LIMITS_METHOD,
    TOKEN_USAGE_METHOD,
    AccountTokenUsage,
    CodexAccountClient,
)
from lup.harness.codescan.boundaries import NATIVE_SPELLINGS
from lup.types import JsonObject, JsonValue
from lup.providers.claude.usage.api import StatsCache, UsageResponse
from lup.providers.claude.usage.reader import (
    days_from,
    legend_from,
    spend_from,
    windows_from,
)
from lup.providers.codex.usage.api import AccountUsage
from lup.providers.codex.usage.reader import days_from as codex_days_from
from lup.providers.codex.usage.reader import windows_from as codex_windows_from
from lup.observability.usage.models import UsageReport
from lup.observability.usage.render import (
    breakdown_window,
    build_display,
    build_snapshot,
)


def recent_stats() -> StatsCache:
    """A stats cache (camelCase, as written on disk) inside the trailing week."""
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=offset)).isoformat() for offset in (2, 1)]
    return StatsCache.model_validate(
        {
            "lastComputedDate": today.isoformat(),
            "dailyModelTokens": [
                {"date": days[0], "tokensByModel": {"claude-opus-4-8": 900_000}},
                {"date": days[1], "tokensByModel": {"claude-sonnet-4-6": 50_000}},
            ],
            "dailyActivity": [
                {"date": days[0], "messageCount": 120},
                {"date": days[1], "messageCount": 30},
            ],
        }
    )


def usage_without_weekly() -> UsageResponse:
    """An account that exposes only a 5-hour window — no weekly bucket."""
    resets = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    return UsageResponse.model_validate(
        {"five_hour": {"utilization": 12.0, "resets_at": resets}}
    )


def claude_report(stats: StatsCache | None) -> UsageReport:
    """Assemble what the Claude reader would return for that cache."""
    usage = usage_without_weekly()
    report = UsageReport(
        runtime_name="Claude Code",
        windows=windows_from(usage),
        spend=spend_from(usage),
    )
    if stats is None:
        return report
    daily = days_from(stats, breakdown_window(report).end)
    return report.model_copy(
        update={
            "daily": daily,
            "legend": legend_from(daily),
            "fresh_through": datetime.now(timezone.utc).date(),
        }
    )


def codex_account() -> AccountUsage:
    """Both windows and a week of daily buckets, in app-server spelling."""
    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=offset)).date().isoformat() for offset in (2, 1)]
    return AccountUsage.model_validate(
        {
            "limits": {
                "rateLimits": {
                    "planType": "pro",
                    "primary": {
                        "usedPercent": 12,
                        "resetsAt": int((now + timedelta(hours=3)).timestamp()),
                        "windowDurationMins": 300,
                    },
                    "secondary": {
                        "usedPercent": 44,
                        "resetsAt": int((now + timedelta(days=4)).timestamp()),
                        "windowDurationMins": 7 * 24 * 60,
                    },
                }
            },
            "tokens": {
                "dailyUsageBuckets": [
                    {"startDate": days[0], "tokens": 900_000},
                    {"startDate": days[1], "tokens": 50_000},
                ]
            },
        }
    )


def codex_report() -> UsageReport:
    """Assemble what the Codex reader would return for that account."""
    usage = codex_account()
    windows = codex_windows_from(usage)
    report = UsageReport(runtime_name="Codex", windows=windows)
    return report.model_copy(
        update={"daily": codex_days_from(usage, windows[0].resets_at)}
    )


def render(report: UsageReport) -> str:
    console = Console(force_terminal=False, width=90, record=True)
    console.print(build_display(report, 58))
    return console.export_text()


class TestDailyBreakdownWithoutWeeklyBucket:
    def test_breakdown_renders_from_daily_figures_alone(self) -> None:
        # The weekly window once gated the breakdown; the daily figures drive it.
        out = render(claude_report(recent_stats()))
        assert "per day" in out
        assert "models" in out

    def test_no_daily_figures_means_no_breakdown(self) -> None:
        out = render(claude_report(None))
        assert "per day" not in out


class TestSnapshot:
    def test_windows_and_tokens_match_the_daily_figures(self) -> None:
        snap = build_snapshot(claude_report(recent_stats()))
        assert [window.name for window in snap.windows] == ["5-hour"]
        tokens = {entry.model: entry.tokens for entry in snap.by_model}
        assert tokens["claude-opus-4-8"] == 900_000
        assert tokens["claude-sonnet-4-6"] == 50_000
        assert len(snap.daily) == 7
        assert any(day.message_count == 120 for day in snap.daily)

    def test_no_daily_figures_yields_empty_daily(self) -> None:
        snap = build_snapshot(claude_report(None))
        assert snap.daily == []
        assert snap.by_model == []
        assert snap.fresh_through is None


def test_json_dumps_snapshot_is_valid_json() -> None:
    snap = build_snapshot(claude_report(recent_stats()))
    parsed = json.loads(snap.model_dump_json())
    assert parsed["windows"][0]["name"] == "5-hour"
    assert parsed["fresh_through"]


class TestCodexReadsTheSameDisplay:
    """The parity claim, checked as behavior rather than as a second module."""

    def test_windows_are_labelled_by_the_length_the_plan_reports(self) -> None:
        assert [window.label for window in codex_report().windows] == [
            "weekly",
            "5-hour",
        ]

    def test_the_same_display_renders_a_codex_report(self) -> None:
        out = render(codex_report())
        assert "Codex Usage" in out
        assert "weekly" in out
        assert "per day" in out
        # No per-model split is published, so there is no legend to draw.
        assert "models" not in out

    def test_the_snapshot_carries_the_runtime_that_reported_it(self) -> None:
        snap = build_snapshot(codex_report())
        assert snap.runtime == "Codex"
        assert [window.name for window in snap.windows] == ["weekly", "5-hour"]
        assert len(snap.daily) == 7


class TestAPayloadThatDriftsCostsOnlyWhatItNames:
    """Neither account is versioned, so a renamed field must not take a panel.

    The windows are what the display is for. A day it cannot place, or a
    window that stops saying when it clears, is worth less than the whole
    reading — so each is dropped where it stands rather than raised.
    """

    def test_a_day_bucket_that_names_no_readable_date_is_skipped(self) -> None:
        usage = AccountUsage.model_validate(
            {
                "limits": codex_account().limits.model_dump(by_alias=True),
                "tokens": {
                    "dailyUsageBuckets": [
                        {"tokens": 900_000},
                        {"startDate": "the day before yesterday", "tokens": 1},
                    ]
                },
            }
        )
        days = codex_days_from(usage, datetime.now(timezone.utc))

        assert len(days) == 7
        assert all(day.total_tokens == 0 for day in days)

    def test_a_cache_that_stops_dating_itself_still_reports_its_days(self) -> None:
        stats = recent_stats().model_copy(
            update={"last_computed_date": "2026-08-11T00:00:00Z"}
        )

        assert stats.fresh_through() is None
        assert len(days_from(stats, datetime.now(timezone.utc))) == 7

    def test_a_window_that_stops_saying_when_it_clears_is_dropped(self) -> None:
        usage = UsageResponse.model_validate(
            {
                "five_hour": {"utilization": 12.0, "resets_at": "soon"},
                "seven_day": {"utilization": 5.0},
            }
        )

        assert windows_from(usage) == []


class RefusingServer(CodexAppServer):
    """An app-server that answers the daily read with one chosen error."""

    def __init__(self, code: int) -> None:
        super().__init__(Path("codex"))
        self.refusal = RpcError(code=code, message="nope")

    async def request(self, method: str, params: JsonObject) -> JsonValue:
        if method == TOKEN_USAGE_METHOD:
            raise AppServerError(self.refusal)
        return {}


def daily_read(code: int) -> AccountTokenUsage:
    """Ask for the daily buckets from a server that refuses with that code."""
    client = CodexAccountClient(Path("codex"), {})
    return asyncio.run(client.token_usage(RefusingServer(code)))


class TestOnlyAnUnknownMethodDegradesQuietly:
    """A silent empty daily section is indistinguishable from a quiet account.

    So the one error that may render as "no history" is the runtime saying it
    has no such method. Anything else — an expired login, a backend that is
    down — has to reach the caller, or a permanent fault and a passing one
    look identical forever.
    """

    def test_an_unknown_method_leaves_the_daily_section_empty(self) -> None:
        assert daily_read(METHOD_NOT_FOUND).daily_usage_buckets is None

    def test_any_other_error_reaches_the_caller(self) -> None:
        with pytest.raises(AppServerError):
            daily_read(-32000)


def test_the_daily_read_names_the_method_the_runtime_actually_has() -> None:
    """The wire spellings, pinned where a rename has to reach both places.

    ``account/usage/read`` reads wrongly — the response type beside it is
    ``GetAccountTokenUsageResponse`` — so the plausible spelling is the one
    worth guarding against, and ``codescan`` has to sanction whatever this
    sends or the boundary rule stops covering it.
    """
    assert TOKEN_USAGE_METHOD == "account/usage/read"
    assert RATE_LIMITS_METHOD == "account/rateLimits/read"
    for method in (TOKEN_USAGE_METHOD, RATE_LIMITS_METHOD):
        assert method in NATIVE_SPELLINGS, method
