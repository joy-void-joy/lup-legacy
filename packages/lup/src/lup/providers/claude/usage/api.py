"""Data layer for the usage display.

Loads Claude Code OAuth credentials, fetches the live usage API at
api.anthropic.com, and parses stats-cache.json into typed models.
"""

import json
import random
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from lup.channels.models import write_atomic
from lup.execution.locks import exclusive
from lup.formats import digest
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.workspace.user_directories import UserDirectories

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


# ── one reading per account ────────────────────────────────
#
# The endpoint is rate-limited per account. The dashboard's poller, `dev usage
# claude` and a sampler each asking it for the same account were refused with
# 429 Too Many Requests on 2026-10-03, at about one and a half calls a minute
# between them, and a refused poller read no window at all. So every reader
# asks through one record per account home, under lup's state: a reading
# younger than the caller allows answers from it, and a refusal sets when the
# endpoint may be asked again -- the Retry-After or RateLimit-Reset it names,
# else an exponential backoff with jitter -- until which the last good reading
# stands, with its age, rather than none. One reader asks at a time, under a
# lock beside the record, so readers arriving together make one request.


class UsageRecord(BaseModel, frozen=True):
    """What one account's endpoint last answered, and when it may be asked again."""

    usage: UsageResponse | None = None
    read_at: datetime | None = None
    refusals: int = 0
    """Refusals in a row since the last good answer, which the backoff doubles on."""

    retry_at: datetime | None = None
    error: str = ""


class CachedUsage(BaseModel, frozen=True):
    """A reading of one account, when it was taken, and why it is not fresher where it is not."""

    usage: UsageResponse
    read_at: datetime
    stale: str = ""


class UsageRefused(RuntimeError):
    """The endpoint answered nothing, and no earlier reading stands in."""


def usage_record(home: Path, directories: UserDirectories | None = None) -> Path:
    """Where the readings of the account whose login *home* keeps are shared."""
    named = digest.text(str(home.expanduser().resolve()))[:16]
    return (directories or UserDirectories()).state() / "usage" / f"claude-{named}.json"


def asked_again(response: httpx.Response, now: datetime) -> datetime | None:
    """When a refusal says the endpoint may be asked again, where its headers say."""
    headers = response.headers
    for name in ("retry-after", "ratelimit-reset", "x-ratelimit-reset"):
        if name not in headers:
            continue
        value = headers[name].strip()
        if value.isdigit():
            return now + timedelta(seconds=int(value))
        try:
            return parsedate_to_datetime(value).astimezone(UTC)
        except (TypeError, ValueError):
            continue
    return None


def backoff(
    refusals: int,
    base: timedelta,
    cap: timedelta,
    jitter: Callable[[], float] = random.random,
) -> timedelta:
    """How long to leave the endpoint after so many refusals in a row: doubling, capped, jittered."""
    doubled = min(base * (2 ** max(refusals - 1, 0)), cap)
    return doubled * (0.5 + jitter() / 2)


def stood(last: CachedUsage | None, held: UsageRecord, now: datetime) -> CachedUsage:
    """The last good reading, saying why it stands; or why there is none."""
    waiting = (
        f"; asking again at {held.retry_at.astimezone().strftime('%H:%M:%S')}"
        if held.retry_at is not None and held.retry_at > now
        else ""
    )
    if last is None:
        raise UsageRefused(f"{held.error}{waiting}")
    age = int((now - last.read_at).total_seconds() // 60)
    return last.model_copy(
        update={"stale": f"read {age} min ago: {held.error}{waiting}"}
    )


def cached_usage(
    home: Path,
    max_age: timedelta,
    now: datetime | None = None,
    record: Path | None = None,
    fetch: Callable[[Path], UsageResponse] = fetch_usage,
    base: timedelta = timedelta(minutes=1),
    cap: timedelta = timedelta(minutes=30),
    jitter: Callable[[], float] = random.random,
) -> CachedUsage:
    """The account's usage no older than *max_age*, asking the endpoint only when it must and may.

    Raises :class:`UsageRefused` only where no reading stands in for a refusal;
    any other failure raises as :func:`fetch_usage` raised it.
    """
    moment = now or datetime.now(UTC)
    path = record or usage_record(home)
    with exclusive(path.with_name(f".{path.name}.lock")):
        try:
            held = UsageRecord.model_validate_json(path.read_bytes())
        except (OSError, ValueError):
            held = UsageRecord()
        last = (
            CachedUsage(usage=held.usage, read_at=held.read_at)
            if held.usage is not None and held.read_at is not None
            else None
        )
        if last is not None and moment - last.read_at <= max_age:
            return last
        if held.retry_at is not None and moment < held.retry_at:
            return stood(last, held, moment)
        try:
            usage = fetch(home)
        except httpx.HTTPStatusError as refused:
            if refused.response.status_code != 429:
                raise
            refusals = held.refusals + 1
            retry_at = asked_again(refused.response, moment) or moment + backoff(
                refusals, base, cap, jitter
            )
            held = held.model_copy(
                update={
                    "refusals": refusals,
                    "retry_at": retry_at,
                    "error": str(refused),
                }
            )
            write_atomic(path, held.model_dump_json().encode("utf-8"), mode=0o600)
            return stood(last, held, moment)
        held = UsageRecord(usage=usage, read_at=moment)
        write_atomic(path, held.model_dump_json().encode("utf-8"), mode=0o600)
        return CachedUsage(usage=usage, read_at=moment)


# ── stats cache ────────────────────────────────────────────


def load_stats(config_dir: Path) -> StatsCache | None:
    path = stats_path(config_dir)
    if not path.exists():
        return None
    try:
        return StatsCache.model_validate_json(path.read_bytes())
    except (ValueError, OSError):
        return None
