"""Read one Codex account's usage into the portable report shape.

Everything Codex-specific about the display is here: that its windows arrive
as a primary and a secondary carrying their own lengths rather than a fixed
roster, that a reset is a Unix second, and that its daily buckets count
tokens without splitting them by model — so the display renders bars and days
and simply has no legend to draw.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sh
from pydantic import ValidationError

from lup.harness.environment import inherited
from lup.providers.codex.harness import CodexSpellings
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.profile_tree import profile_directory
from lup.providers.profiles import DefaultHomeProfile, ProfileDirectory, UnknownProfile
from lup.providers.codex.usage.api import (
    AccountUsage,
    CodexAccountClient,
    RateLimitWindow,
)
from lup.types import EnvVars
from lup.observability.usage.app import UsageEntry
from lup.observability.usage.models import (
    DayUsage,
    PacingWindow,
    UsageReader,
    UsageReport,
    UsageUnavailable,
)

FIVE_HOURS_IN_MINUTES = 5 * 60
"""The shorter window's usual length, used only to size an unlabelled one."""

TRAILING_DAYS = 7
"""How much of the longer window the per-day breakdown shows."""


def window_label(window: RateLimitWindow, fallback: str) -> str:
    """Name a window by how long it runs, since the plan decides that.

    Codex reports two windows and the length of each, rather than a fixed
    roster under fixed names, so a plan metering something other than five
    hours and a week still gets a label that says what it is.
    """
    minutes = window.window_duration_mins
    if minutes is None:
        return fallback
    if minutes % (24 * 60) == 0:
        days = minutes // (24 * 60)
        return "weekly" if days == 7 else f"{days}-day"
    return f"{minutes // 60}-hour" if minutes >= 60 else f"{minutes}-minute"


def pacing_window(window: RateLimitWindow | None, fallback: str) -> PacingWindow | None:
    """One reported window, dropped where it does not say when it clears."""
    if window is None:
        return None
    clears_at = window.clears_at()
    if clears_at is None:
        return None
    minutes = window.window_duration_mins or FIVE_HOURS_IN_MINUTES
    return PacingWindow(
        label=window_label(window, fallback),
        utilization_pct=window.used_percent,
        resets_at=clears_at,
        window_hours=minutes / 60,
    )


def windows_from(usage: AccountUsage) -> list[PacingWindow]:
    """Both metered windows, longest first so the display leads with the plan."""
    snapshot = usage.limits.rate_limits
    reported = [
        pacing_window(snapshot.secondary, "weekly"),
        pacing_window(snapshot.primary, "5-hour"),
    ]
    return [window for window in reported if window is not None]


def days_from(
    usage: AccountUsage, window_end: datetime, trailing_days: int = TRAILING_DAYS
) -> list[DayUsage]:
    """The trailing week of daily buckets, weighed by the tokens they count.

    Codex publishes no per-model prices, so a day weighs what it moved. A day
    the account reported nothing for still appears: an empty bar is the honest
    rendering of a quiet day inside a window that covers it.
    """
    counted = {
        day: bucket.tokens
        for bucket in usage.tokens.daily_usage_buckets or []
        if (day := bucket.starts_on()) is not None
    }
    last = window_end.date()
    shown = [last - timedelta(days=offset) for offset in reversed(range(trailing_days))]
    return [
        DayUsage(
            day=day,
            total_tokens=counted[day] if day in counted else 0,
            weight=float(counted[day] if day in counted else 0),
        )
        for day in shown
    ]


class CodexUsageReader(UsageReader):
    """Read the account the app-server is signed in to under one home."""

    def __init__(self, executable: Path, home: Path) -> None:
        self.executable = executable
        self.home = home

    def environment(self) -> EnvVars:
        """Point the app-server this starts at the home being read."""
        return CODEX_LOGIN.environment(self.home)

    def refusal(self) -> str | None:
        """Why this reading cannot happen at all, where it cannot.

        The message does not offer the configuration-home variable as a way out.
        This reads the home it was composed against and exports that same
        home to the process it starts, so naming another one in the
        environment changes nothing — and advice that does nothing is worse
        than none, because it reads as a remedy already tried.
        """
        credentials = CODEX_LOGIN.credentials_path(self.home)
        if not credentials.exists():
            return (
                f"No credentials at {credentials}. This reads the account the "
                f"runtime is signed in to under {self.home}; sign in there "
                "with the runtime's own login."
            )
        return None

    def read(self, detail: bool) -> UsageReport:
        refused = self.refusal()
        if refused is not None:
            raise UsageUnavailable(refused)
        client = CodexAccountClient(self.executable, self.environment())
        try:
            usage = asyncio.run(client.read())
        except (OSError, RuntimeError, ValidationError) as error:
            raise UsageUnavailable(str(error)) from error
        except sh.CommandNotFound as error:
            # Not an OSError: `sh` raises this off AttributeError, so a machine
            # without the runtime installed would otherwise reach the terminal
            # as a traceback rather than as the one thing gone wrong.
            raise UsageUnavailable(
                f"No {self.executable} on PATH to read an account with."
            ) from error

        windows = windows_from(usage)
        report = UsageReport(
            runtime_name=CodexSpellings().runtime_name, windows=windows
        )
        if not detail:
            return report
        anchor = windows[0].resets_at if windows else datetime.now(UTC)
        return report.model_copy(update={"daily": days_from(usage, anchor)})


def codex_usage_entry(
    executable: Path = Path("codex"), profiles: ProfileDirectory | None = None
) -> UsageEntry:
    """This runtime's place in the usage sub-app, for an application to name.

    ``profiles`` is where ``--profile`` finds its account, this checkout's then
    the global ones unless an application keeps others: one name is one
    account on every runtime, so the name that selects a Claude login selects
    its Codex login.
    Naming none reads the selection, else whichever home the environment
    selects, as a launch would.
    """
    directory = profiles or profile_directory(CODEX_LOGIN)

    def opened(profile: str | None) -> UsageReader:
        """The reader for the home that profile selects, refused in its own words."""
        try:
            home = directory.launch_home(profile)
        except (DefaultHomeProfile, UnknownProfile) as refusal:
            raise UsageUnavailable(str(refusal)) from refusal
        unnamed = directory.login.selected_home(inherited())
        return CodexUsageReader(executable, home if home is not None else unnamed)

    return UsageEntry(
        name="codex",
        runtime_name=CodexSpellings().runtime_name,
        help="Show live Codex usage with pacing bars (ChatGPT plan).",
        open=opened,
    )
