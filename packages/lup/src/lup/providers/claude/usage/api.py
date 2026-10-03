"""Data layer for the usage display.

Loads Claude Code OAuth credentials, fetches the live usage API at
api.anthropic.com, and parses stats-cache.json into typed models.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from lup.providers.claude.login import CLAUDE_LOGIN

# ── constants ──────────────────────────────────────────────


def creds_path(config_dir: Path) -> Path:
    """OAuth credentials file inside a Claude config dir."""
    return CLAUDE_LOGIN.credentials_path(config_dir)


def stats_path(config_dir: Path) -> Path:
    """Stats cache file inside a Claude config dir."""
    return config_dir / "stats-cache.json"


# lup: ignore[constant-declaration] — the endpoint the vendor publishes
USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
# lup: ignore[constant-declaration] — the beta header value that endpoint requires
ANTHROPIC_BETA = "oauth-2025-04-20"


# ── API response types ─────────────────────────────────────


class UsageBucket(BaseModel, frozen=True, extra="ignore"):
    """One rate-limit window: how much is spent, and when it clears."""

    utilization: float = 0
    resets_at: str = ""

    def clears_at(self) -> datetime | None:
        """When this window clears, or none where it does not say.

        A window with no readable reset cannot be paced against — the bar
        needs the window's start to place even pace — so it is left out
        rather than drawn against a guess.
        """
        try:
            return datetime.fromisoformat(self.resets_at)
        except ValueError:
            return None


class ExtraUsage(BaseModel, frozen=True, extra="ignore"):
    """Metered spend past the plan, in cents."""

    is_enabled: bool = False
    monthly_limit: float | None = None
    used_credits: float | None = None
    utilization: float | None = None


class UsageResponse(BaseModel, frozen=True, extra="ignore"):
    """What the unversioned OAuth endpoint reports about this account.

    Every window is optional and unknown keys are ignored: the endpoint is
    unversioned, plans differ in which windows they meter, and a payload that
    grew a field is not a reason to stop reporting the ones it kept.
    """

    five_hour: UsageBucket | None = None
    seven_day: UsageBucket | None = None
    seven_day_opus: UsageBucket | None = None
    seven_day_sonnet: UsageBucket | None = None
    seven_day_oauth_apps: UsageBucket | None = None
    seven_day_cowork: UsageBucket | None = None
    extra_usage: ExtraUsage | None = None


# ── stats cache models ─────────────────────────────────────


class DailyActivity(BaseModel, populate_by_name=True):
    date: str
    message_count: int = Field(alias="messageCount", default=0)
    session_count: int = Field(alias="sessionCount", default=0)
    tool_call_count: int = Field(alias="toolCallCount", default=0)


class DailyModelTokens(BaseModel, populate_by_name=True):
    date: str
    tokens_by_model: dict[str, int] = Field(  # lup: ignore[dict-str-payload] — tally
        alias="tokensByModel", default={}
    )


class ModelUsageEntry(BaseModel, populate_by_name=True):
    input_tokens: int = Field(alias="inputTokens", default=0)
    output_tokens: int = Field(alias="outputTokens", default=0)
    cache_read_input_tokens: int = Field(alias="cacheReadInputTokens", default=0)
    cache_creation_input_tokens: int = Field(
        alias="cacheCreationInputTokens", default=0
    )
    web_search_requests: int = Field(alias="webSearchRequests", default=0)
    cost_usd: float = Field(alias="costUSD", default=0)
    context_window: int = Field(alias="contextWindow", default=0)
    max_output_tokens: int = Field(alias="maxOutputTokens", default=0)


class LongestSession(BaseModel, populate_by_name=True):
    session_id: str = Field(alias="sessionId")
    duration: int
    message_count: int = Field(alias="messageCount")
    timestamp: str


class StatsCache(BaseModel, populate_by_name=True):
    version: int = 0
    last_computed_date: str = Field(alias="lastComputedDate", default="")
    daily_activity: list[DailyActivity] = Field(alias="dailyActivity", default=[])
    daily_model_tokens: list[DailyModelTokens] = Field(
        alias="dailyModelTokens", default=[]
    )
    model_usage: dict[str, ModelUsageEntry] = Field(alias="modelUsage", default={})
    total_sessions: int = Field(alias="totalSessions", default=0)
    total_messages: int = Field(alias="totalMessages", default=0)
    longest_session: LongestSession | None = Field(alias="longestSession", default=None)
    first_session_date: str = Field(alias="firstSessionDate", default="")
    hour_counts: dict[str, int] = Field(  # lup: ignore[dict-str-payload] — tally
        alias="hourCounts", default={}
    )
    total_speculation_time_saved_ms: int = Field(
        alias="totalSpeculationTimeSavedMs", default=0
    )

    def fresh_through(self) -> date | None:
        """The last day this cache covers, or none where it does not say.

        The runtime writes this file on its own schedule and in its own
        shape, so a date it stops stating readably leaves the breakdown
        unable to mark what it does not cover — which costs the annotation,
        not the reading.
        """
        try:
            return date.fromisoformat(self.last_computed_date)
        except ValueError:
            return None

    def daily_breakdown(
        self, window_start: datetime, window_end: datetime
    ) -> list["DailyBreakdown"]:
        """Per-day token and activity breakdown for a time window."""
        tokens_by_date = {
            entry.date: entry.tokens_by_model for entry in self.daily_model_tokens
        }
        activity_by_date = {entry.date: entry for entry in self.daily_activity}

        def day_breakdown(ds: str) -> DailyBreakdown:
            by_model = tokens_by_date.get(ds, {})
            return DailyBreakdown(
                date=ds,
                total_tokens=sum(by_model.values()),
                tokens_by_model=by_model,
                activity=activity_by_date.get(ds),
            )

        span = (window_end.date() - window_start.date()).days
        return [
            day_breakdown((window_start.date() + timedelta(days=offset)).isoformat())
            for offset in range(span + 1)
        ]


# ── derived data ───────────────────────────────────────────


class DailyBreakdown(BaseModel):
    date: str
    total_tokens: int
    tokens_by_model: dict[str, int]  # lup: ignore[dict-str-payload] — open tally
    activity: DailyActivity | None


# ── API ────────────────────────────────────────────────────


def fetch_usage(config_dir: Path) -> UsageResponse:
    """Call the live usage API using the profile's OAuth credentials."""
    creds_file = creds_path(config_dir)
    try:
        creds = json.loads(creds_file.read_text())
        oauth = creds["claudeAiOauth"]
        token: str = oauth["accessToken"]
    except (json.JSONDecodeError, KeyError, OSError) as e:
        msg = f"Bad credentials file at {creds_file}: {e}"
        raise RuntimeError(msg) from e

    resp = httpx.get(
        USAGE_API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": ANTHROPIC_BETA,
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return UsageResponse.model_validate(resp.json())


# ── stats cache ────────────────────────────────────────────


def load_stats(config_dir: Path) -> StatsCache | None:
    path = stats_path(config_dir)
    if not path.exists():
        return None
    try:
        return StatsCache.model_validate_json(path.read_bytes())
    except (ValueError, OSError):
        return None
