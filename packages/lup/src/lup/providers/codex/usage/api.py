"""What the Codex app-server reports about an account's metered usage.

Two requests answer the whole display: one for the rate-limit windows the
plan meters, one for the daily token buckets behind them. Both are read over
the app-server rather than from the endpoint underneath it, so the rotating
credential stays with the runtime that owns and refreshes it, and a login
this process never reads cannot be a login it leaks.
"""

import logging
from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from lup.providers.codex.app_server import AppServerError, CodexAppServer
from lup.types import EnvVars, JsonValue

logger = logging.getLogger(__name__)

RATE_LIMITS_METHOD = "account/rateLimits/read"  # lup: ignore[constant-declaration]
# lup: ignore[constant-declaration] — both are app-server method names, and the
# docstring below is about how little freedom there is in spelling this one
TOKEN_USAGE_METHOD = "account/usage/read"
"""What the runtime calls the daily-token read, which is not what it returns.

The response type is ``GetAccountTokenUsageResponse`` and the notification
beside it is ``thread/tokenUsage/updated``, so the method reads as though it
should be ``account/tokenUsage/read``. It is not, and the runtime answers a
method it does not know with an error rather than a hint.
"""

# lup: ignore[constant-declaration] — JSON-RPC's own code, fixed by the protocol
METHOD_NOT_FOUND = -32601
"""JSON-RPC's own code for a method this runtime does not have."""


class RateLimitWindow(BaseModel, frozen=True, extra="ignore", populate_by_name=True):
    """One metered window: how much is spent, how long it runs, when it clears."""

    used_percent: float = Field(default=0, alias="usedPercent")
    resets_at: int | None = Field(default=None, alias="resetsAt")
    window_duration_mins: int | None = Field(default=None, alias="windowDurationMins")

    def clears_at(self) -> datetime | None:
        """When this window clears, as a moment rather than a Unix second."""
        if self.resets_at is None:
            return None
        return datetime.fromtimestamp(self.resets_at, tz=timezone.utc)


class RateLimitSnapshot(BaseModel, frozen=True, extra="ignore", populate_by_name=True):
    """Every window one plan meters, as one reading of them.

    The credit balance beside these is deliberately not modelled. Nothing
    here renders it, and a field parsed only to be discarded still decides
    the whole reading: one wrong guess at its type turns a payload this
    display never reads into a validation error that costs the panel.
    """

    plan_type: str | None = Field(default=None, alias="planType")
    primary: RateLimitWindow | None = None
    secondary: RateLimitWindow | None = None


class AccountRateLimits(BaseModel, frozen=True, extra="ignore", populate_by_name=True):
    """The rate-limit reading, whose single-bucket view is the one rendered."""

    rate_limits: RateLimitSnapshot = Field(
        default_factory=RateLimitSnapshot, alias="rateLimits"
    )


class TokenUsageDay(BaseModel, frozen=True, extra="ignore", populate_by_name=True):
    """One day's tokens, under the date the account's own billing day starts."""

    start_date: str = Field(default="", alias="startDate")
    tokens: int = 0

    def starts_on(self) -> date | None:
        """Which day this counts, or none where it does not name one.

        A bucket whose date is absent or in a shape this cannot read is worth
        less than the whole reading it would otherwise take down: the windows
        are what the display is for, and a day it cannot place is one day
        missing from a breakdown rather than a panel that fails to draw.
        """
        try:
            return date.fromisoformat(self.start_date)
        except ValueError:
            return None


class AccountTokenUsage(BaseModel, frozen=True, extra="ignore", populate_by_name=True):
    """The daily buckets behind the windows, where the account reports any."""

    daily_usage_buckets: list[TokenUsageDay] | None = Field(
        default=None, alias="dailyUsageBuckets"
    )


class AccountUsage(BaseModel, frozen=True):
    """One complete reading of an account: its windows, and its daily buckets."""

    limits: AccountRateLimits
    tokens: AccountTokenUsage


class RolloutWindow(BaseModel, frozen=True, extra="ignore"):
    """One metered window as a rollout's token count records it, in snake case.

    Read off codex-rs/protocol/src/protocol.rs at rust-v0.159.2
    (``RateLimitWindow``): the same three facts the app-server reports, under
    the core protocol's own names.
    """

    used_percent: float = 0
    window_minutes: int | None = None
    resets_at: int | None = None

    def read(self) -> RateLimitWindow:
        """The window as the app-server spells it, which the display reads."""
        return RateLimitWindow.model_validate(
            {
                "usedPercent": self.used_percent,
                "resetsAt": self.resets_at,
                "windowDurationMins": self.window_minutes,
            }
        )


class RolloutLimits(BaseModel, frozen=True, extra="ignore"):
    primary: RolloutWindow | None = None
    secondary: RolloutWindow | None = None


class RolloutTokens(BaseModel, frozen=True, extra="ignore"):
    total_tokens: int = 0


class RolloutUsage(BaseModel, frozen=True, extra="ignore"):
    last_token_usage: RolloutTokens = RolloutTokens()


class TokenCountPayload(BaseModel, frozen=True, extra="ignore"):
    """A rollout event's payload, as far as a token count needs it.

    Codex persists every ``token_count`` event to the rollout whatever the
    thread's history mode (codex-rs/rollout/src/policy.rs at rust-v0.159.2):
    what the last request moved, and the account's windows as it moved it.
    """

    type: str = ""
    info: RolloutUsage | None = None
    rate_limits: RolloutLimits | None = None


class RolloutEvent(BaseModel, frozen=True, extra="ignore"):
    type: str = ""
    payload: TokenCountPayload | None = None


class CodexAccountClient:
    """One short-lived app-server connection, opened to ask about an account.

    A display is not a session: it starts the same binary a session would,
    asks its two questions, and stops. Holding the connection open would keep
    a runtime process alive for a panel that refreshes every ten minutes.
    """

    def __init__(self, executable: Path, environment: EnvVars) -> None:
        self.executable = executable
        self.environment = environment

    async def read(self) -> AccountUsage:
        """Ask for the windows and the daily buckets over one connection."""
        server = CodexAppServer(self.executable, environment=dict(self.environment))
        await server.start()
        try:
            limits = await server.request(RATE_LIMITS_METHOD, {})
            tokens = await self.token_usage(server)
        finally:
            await server.close()
        return AccountUsage(
            limits=AccountRateLimits.model_validate(limits), tokens=tokens
        )

    async def token_usage(self, server: CodexAppServer) -> AccountTokenUsage:
        """Read the daily buckets, and none where this build has no such call.

        Only a runtime that does not know the method degrades quietly, and it
        says so in the log on the way past. Every other error — an expired
        login, a backend that is down — is raised, because a reading that
        silently renders as "no history" is indistinguishable from an account
        that has none, and the difference is the whole value of the section.
        """
        try:
            payload: JsonValue = await server.request(TOKEN_USAGE_METHOD, {})
        except AppServerError as error:
            if error.error.code != METHOD_NOT_FOUND:
                raise
            logger.warning(
                "Codex app-server does not know %s, so this account's daily "
                "usage is unavailable: %s",
                TOKEN_USAGE_METHOD,
                error.error.message,
            )
            return AccountTokenUsage()
        return AccountTokenUsage.model_validate(payload)
