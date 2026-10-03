"""Read one Claude Code account's usage into the portable report shape.

Everything Anthropic-specific about the display is here: which rate-limit
windows the OAuth endpoint publishes and how long each one runs, what a day's
tokens cost against the plan, and how a model family is named and coloured.
The display itself knows none of it.
"""

from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ValidationError

from lup.harness.environment import inherited
from lup.providers.claude.harness import ClaudeSpellings
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.profile_tree import profile_directory
from lup.providers.profiles import DefaultHomeProfile, ProfileDirectory, UnknownProfile
from lup.providers.claude.usage.api import (
    ModelUsageEntry,
    StatsCache,
    UsageBucket,
    UsageResponse,
    creds_path,
    fetch_usage,
    load_stats,
)
from lup.observability.usage.app import UsageEntry
from lup.observability.usage.models import (
    DayUsage,
    ModelShare,
    ModelTokens,
    PacingWindow,
    SpendWindow,
    UsageReader,
    UsageReport,
    UsageUnavailable,
)
from lup.observability.usage.render import breakdown_window

type BucketKey = Literal[
    "seven_day",
    "five_hour",
    "seven_day_opus",
    "seven_day_sonnet",
    "seven_day_cowork",
    "seven_day_oauth_apps",
]
"""Every window the OAuth usage endpoint publishes that the display renders."""


class BucketSpec(BaseModel, frozen=True):
    """One rate-limit window: the field it arrives in, its label, its length."""

    key: BucketKey
    label: str
    window_hours: float


# lup: ignore[constant-declaration] — one row per window the usage payload
# actually carries, so the vendor's key set decides which rows exist
BUCKET_SPECS: list[BucketSpec] = [
    BucketSpec(key="seven_day", label="weekly", window_hours=7 * 24),
    BucketSpec(key="five_hour", label="5-hour", window_hours=5),
    BucketSpec(key="seven_day_opus", label="opus 7d", window_hours=7 * 24),
    BucketSpec(key="seven_day_sonnet", label="sonnet 7d", window_hours=7 * 24),
    BucketSpec(key="seven_day_cowork", label="cowork 7d", window_hours=7 * 24),
    BucketSpec(key="seven_day_oauth_apps", label="oauth 7d", window_hours=7 * 24),
]


class ModelName(BaseModel, frozen=True):
    """What one model id is called where a human reads it."""

    model_id: str
    label: str


# lup: ignore[constant-declaration] — the vendor's own model ids, spelled the
# way it publishes them; an id this table lacks renders under its own name
MODEL_NAMES: list[ModelName] = [
    ModelName(model_id="claude-opus-5", label="Opus 5"),
    ModelName(model_id="claude-opus-4-8", label="Opus 4.8"),
    ModelName(model_id="claude-opus-4-6", label="Opus 4.6"),
    ModelName(model_id="claude-opus-4-5-20251101", label="Opus 4.5"),
    ModelName(model_id="claude-sonnet-4-6", label="Sonnet 4.6"),
    ModelName(model_id="claude-sonnet-4-5-20250929", label="Sonnet 4.5"),
    ModelName(model_id="claude-haiku-4-5-20251001", label="Haiku 4.5"),
]
"""An open list: an id newer than this table shows under its own name."""


class ModelFamily(BaseModel, frozen=True):
    """One family's word inside a model id, and the colour it renders in."""

    word: str
    style: str


MODEL_FAMILIES: list[ModelFamily] = [
    ModelFamily(word="opus", style="bright_magenta"),
    ModelFamily(word="sonnet", style="bright_blue"),
    ModelFamily(word="haiku", style="bright_cyan"),
]


class ModelRate(BaseModel, frozen=True):
    """What one token cost on one model, as the local cache has priced it."""

    model_id: str
    usd_per_token: float


TRAILING_DAYS = 7
"""How much of the weekly window the per-day breakdown shows."""


def bucket_for(usage: UsageResponse, key: BucketKey) -> UsageBucket | None:
    """Read one window off the payload, ignoring the ones this plan lacks."""
    match key:
        case "seven_day":
            return usage.seven_day
        case "five_hour":
            return usage.five_hour
        case "seven_day_opus":
            return usage.seven_day_opus
        case "seven_day_sonnet":
            return usage.seven_day_sonnet
        case "seven_day_cowork":
            return usage.seven_day_cowork
        case "seven_day_oauth_apps":
            return usage.seven_day_oauth_apps


def model_label(model_id: str) -> str:
    """What this model is called, or its own id where the table is behind."""
    return next(
        (entry.label for entry in MODEL_NAMES if entry.model_id == model_id), model_id
    )


def model_style(model_id: str, families: Sequence[ModelFamily] = MODEL_FAMILIES) -> str:
    """The family colour this id belongs to, and plain white outside them."""
    return next(
        (family.style for family in families if family.word in model_id), "white"
    )


def entry_tokens(entry: ModelUsageEntry) -> int:
    """Everything one model moved, cached reads and writes included."""
    return (
        entry.input_tokens
        + entry.output_tokens
        + entry.cache_read_input_tokens
        + entry.cache_creation_input_tokens
    )


def model_rates(stats: StatsCache) -> list[ModelRate]:
    """What a token cost on each model the cache priced enough of."""
    return [
        ModelRate(model_id=model_id, usd_per_token=entry.cost_usd / entry_tokens(entry))
        for model_id, entry in stats.model_usage.items()
        if entry_tokens(entry) > 0
    ]


def priced(rates: list[ModelRate], model_id: str, tokens: int) -> float:
    """What those tokens cost, and nothing where that model has no price."""
    rate = next(
        (entry.usd_per_token for entry in rates if entry.model_id == model_id), 0.0
    )
    return tokens * rate


def pacing_window(spec: BucketSpec, bucket: UsageBucket | None) -> PacingWindow | None:
    """One published window, dropped where it does not say when it clears."""
    if bucket is None:
        return None
    clears_at = bucket.clears_at()
    if clears_at is None:
        return None
    return PacingWindow(
        label=spec.label,
        utilization_pct=bucket.utilization,
        resets_at=clears_at,
        window_hours=spec.window_hours,
    )


def windows_from(usage: UsageResponse) -> list[PacingWindow]:
    """Every published window that says when it clears, in display order."""
    reported = [
        pacing_window(spec, bucket_for(usage, spec.key)) for spec in BUCKET_SPECS
    ]
    return [window for window in reported if window is not None]


def spend_from(usage: UsageResponse) -> SpendWindow | None:
    """Overage as dollars, which the endpoint reports in cents."""
    extra = usage.extra_usage
    if extra is None or not extra.is_enabled:
        return None
    return SpendWindow(
        label="overage",
        used=extra.used_credits / 100,
        limit=extra.monthly_limit / 100,
        utilization_pct=extra.utilization,
    )


def days_from(
    stats: StatsCache, window_end: datetime, trailing_days: int = TRAILING_DAYS
) -> list[DayUsage]:
    """The trailing week of the window, priced by what each model costs.

    A cache with no prices in it weighs a day by its raw tokens: the bars
    still rank the days against each other, which is most of what they say.
    """
    breakdown = stats.daily_breakdown(
        window_end - timedelta(days=trailing_days), window_end
    )
    # A 168-hour window can span eight calendar dates; keep the most recent.
    recent = breakdown[-trailing_days:]
    rates = model_rates(stats)
    costs = [
        sum(
            priced(rates, model, tokens)
            for model, tokens in day.tokens_by_model.items()
        )
        for day in recent
    ]
    weights = costs if sum(costs) > 0 else [float(d.total_tokens) for d in recent]
    return [
        DayUsage(
            day=date.fromisoformat(day.date),
            total_tokens=day.total_tokens,
            weight=weight,
            by_model=[
                ModelTokens(model=model, tokens=tokens)
                for model, tokens in day.tokens_by_model.items()
            ],
            message_count=day.activity.message_count if day.activity else 0,
        )
        for day, weight in zip(recent, weights, strict=True)
    ]


def legend_from(daily: list[DayUsage]) -> list[ModelShare]:
    """Each model family's share of the shown week, named and coloured."""
    tally: Counter[str] = Counter()
    for day in daily:
        for entry in day.by_model:
            tally[entry.model] += entry.tokens
    return [
        ModelShare(
            label=model_label(model_id), style=model_style(model_id), tokens=tokens
        )
        for model_id, tokens in tally.items()
    ]


class ClaudeUsageReader(UsageReader):
    """Read the live OAuth usage endpoint, and the local cache for detail."""

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir

    def read(self, detail: bool) -> UsageReport:
        credentials = creds_path(self.config_dir)
        if not credentials.exists():
            raise UsageUnavailable(
                f"No credentials at {credentials}. This reads the OAuth usage "
                "endpoint for a signed-in profile; sign in, or name another "
                "profile with --profile."
            )
        try:
            usage = fetch_usage(self.config_dir)
        except (httpx.HTTPError, RuntimeError, ValidationError) as error:
            raise UsageUnavailable(str(error)) from error

        report = UsageReport(
            runtime_name=ClaudeSpellings().runtime_name,
            windows=windows_from(usage),
            spend=spend_from(usage),
        )
        stats = load_stats(self.config_dir) if detail else None
        if stats is None:
            return report

        daily = days_from(stats, breakdown_window(report).end)
        return report.model_copy(
            update={
                "daily": daily,
                "legend": legend_from(daily),
                "fresh_through": stats.fresh_through(),
            }
        )


def claude_usage_entry(profiles: ProfileDirectory | None = None) -> UsageEntry:
    """This runtime's place in the usage sub-app, for an application to name.

    ``profiles`` is what a launch resolves names against — this checkout's
    profiles, then the global ones, unless an application keeps others — so
    ``--profile`` reads the account a launch of that name opens.
    """
    directory = profiles or profile_directory(CLAUDE_LOGIN)

    def opened(profile: str | None) -> UsageReader:
        """The reader for the home a launch naming that profile runs under.

        The same resolution a launch makes — the name, else the selection,
        else whichever home the environment selects — so the two cannot read
        different accounts for one name. A name the directory does not hold,
        and one naming the default home, leave no account to read, which the
        entry's contract answers as :class:`UsageUnavailable` in the
        refusal's own words.
        """
        # lup: solved: This resolves `--profile` against the personal registry
        # at ~/.lup/profiles.json whatever origin the application keeps its
        # profiles in, so where a project keeps them as directories under
        # .lup/profiles, usage for a name reads a different account than a
        # launch of that name opens. Resolving through the application's own
        # ProfileDirectory needs that directory handed to this entry, which is
        # a change to how an application composes it.
        try:
            home = directory.launch_home(profile)
        except (DefaultHomeProfile, UnknownProfile) as refusal:
            raise UsageUnavailable(str(refusal)) from refusal
        unnamed = directory.login.selected_home(inherited())
        return ClaudeUsageReader(home if home is not None else unnamed)

    return UsageEntry(
        name="claude",
        runtime_name=ClaudeSpellings().runtime_name,
        help="Show live Claude Code usage with pacing bars (Anthropic OAuth).",
        open=opened,
    )
