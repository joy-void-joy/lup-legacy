"""What an account may spend and how fast, and which agents wait because of it.

A subscription meters each account in windows — a share of the plan spent,
and a moment it clears — and an operator running many agents on one account
wants what a torrent client gives a link: a speed limit, a reserve kept back
for their own use, a cap on how many run at once, priorities, and a slower
set of limits for when they say so. This module says which of those an
account is under at a moment, and which agents should wait at their next
tool call, why, and until when.

It decides and does nothing else. Reading the windows, counting what each
agent spent and holding the agent are each another module's: the judgement
is a pure function of what they read, so the dashboard holding launched
sessions and an in-process pipeline waiting at its own door reach the same
verdict for the same facts.

Limits come from the person's ``[budget]`` table (:class:`BudgetConfig`), in
layers: the table's own values, an account's override, every schedule entry
that applies now, then the turtle's when it is on — each layer replacing only
what it names.
"""

from collections.abc import Sequence
from datetime import datetime, time, timedelta
from typing import Literal, get_args

from pydantic import BaseModel, Field

from lup.observability.usage.models import PacingWindow

type Priority = Literal["high", "normal", "low"]
"""Who holds first under pressure: low before normal, normal before high."""

type Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
"""A day a schedule entry applies on, Monday first as :meth:`datetime.weekday` counts."""

type Cause = Literal["window", "reserve", "cap", "rate", "pace", "slot"]
"""Why the budget holds an agent: a window used up, the operator's reserve
reached, the agent's spend cap reached, its own rate over its cap, the
account ahead of its speed limit, or no free slot to work in."""


def rank(priority: Priority) -> int:
    """Where a priority stands in the order agents are let go in: high first."""
    return get_args(Priority.__value__).index(priority)


class Ceiling(BaseModel, frozen=True, extra="forbid"):
    """A speed limit on one window, by the label its runtime gives it: so many percent of it an hour."""

    window: str
    per_hour: float = Field(gt=0)


class Limits(BaseModel, frozen=True, extra="forbid"):
    """One set of limits on an account; every limit left unset is no limit.

    ``pace`` is the speed limit: ``"even"`` keeps every window at or under
    even pace — no more of it spent than has gone by — and each of
    ``ceilings`` caps one window at so many percent of it an hour. Past
    either, low-priority agents hold at once, normal ones once the account is
    ``tolerance`` points over, high ones at twice that. ``reserve`` keeps the
    last so many percent of every window for the operator: once a window
    reaches it, every agent but the operator's own sessions holds until it
    clears. ``max_active`` is how many agents may work at once.
    """

    pace: Literal["even"] | None = None
    ceilings: list[Ceiling] | None = None
    tolerance: float | None = Field(default=None, ge=0)
    reserve: float | None = Field(default=None, ge=0, le=100)
    max_active: int | None = Field(default=None, ge=0)

    def then(self, over: "Limits") -> "Limits":
        """These limits with each one *over* names put in place of this one's."""
        mine = {name: getattr(self, name) for name in Limits.model_fields}
        named = {
            name: getattr(over, name)
            for name in Limits.model_fields
            if getattr(over, name) is not None
        }
        return Limits.model_validate({**mine, **named})

    def leeway(self, default: float = 5.0) -> float:
        """How far past a speed limit, in points, normal agents hold too."""
        return self.tolerance if self.tolerance is not None else default

    def ceiling(self, label: str) -> float | None:
        """The speed limit on the window called *label*, where one is set."""
        return next(
            (each.per_hour for each in self.ceilings or [] if each.window == label),
            None,
        )

    def said(self) -> list[str]:
        """Each limit set, in the words the meter and ``dashboard budget`` show it in."""
        return [
            *(["even pace"] if self.pace == "even" else []),
            *(f"{each.window} ≤{each.per_hour:g}%/h" for each in self.ceilings or []),
            *([f"keep {self.reserve:g}%"] if self.reserve else []),
            *([f"≤{self.max_active} at once"] if self.max_active is not None else []),
        ]


class ScheduledLimits(Limits, frozen=True, extra="forbid", populate_by_name=True):
    """Limits that apply on some days between two times of day, local to this machine.

    An entry whose end is before its start runs overnight, into the next day.
    ``accounts`` narrows it to the accounts named; none names every account.
    """

    days: list[Weekday] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    start: time = Field(validation_alias="from")
    end: time = Field(validation_alias="to")
    accounts: list[str] = []

    def applies(self, account: "Account", moment: datetime) -> bool:
        """Whether this entry holds for *account* at *moment*."""
        if self.accounts and not any(account.answers(each) for each in self.accounts):
            return False
        local = moment.astimezone()
        days = get_args(Weekday.__value__)
        today = days[local.weekday()]
        yesterday = days[(local.weekday() - 1) % 7]
        now = local.time()
        if self.start <= self.end:
            return today in self.days and self.start <= now < self.end
        return (today in self.days and now >= self.start) or (
            yesterday in self.days and now < self.end
        )


class Turtle(Limits, frozen=True, extra="forbid"):
    """The slower limits the one-key toggle puts in place, and whether it is on.

    Even pace and one agent at a time unless the person names otherwise.
    """

    on: bool = False
    pace: Literal["even"] | None = "even"
    max_active: int | None = Field(default=1, ge=0)


class Account(BaseModel, frozen=True):
    """One runtime's login under one profile: what a window meters and an agent draws on."""

    runtime: str = ""
    """The runtime whose login it is; empty for one no runtime names, as a pipeline's may be."""

    profile: str = "default"
    """The profile whose home holds the login; ``default`` for the home none selects."""

    @property
    def key(self) -> str:
        return f"{self.runtime}:{self.profile}" if self.runtime else self.profile

    def answers(self, name: str) -> bool:
        """Whether a name in the person's config means this account: its key, or its profile's name."""
        return name in (self.key, self.profile)


class BudgetConfig(Limits, frozen=True, extra="forbid"):
    """``[budget]`` in the person's lup config: every account's limits, and when they change.

    ``accounts`` overrides them for one account, named by its profile — every
    runtime's login of that profile — or as ``<runtime>:<profile>`` for one
    runtime's, ``default`` naming the home no profile selects.
    ``poll_seconds`` is how often the dashboard reads each account's windows.
    """

    accounts: dict[str, Limits] = {}
    schedule: list[ScheduledLimits] = []
    turtle: Turtle = Turtle()
    poll_seconds: int = Field(default=120, ge=30)

    def limits(self, account: Account, moment: datetime) -> Limits:
        """The limits holding for *account* at *moment*, every layer applied."""
        layered = Limits().then(self)
        for name, override in self.accounts.items():
            if account.answers(name):
                layered = layered.then(override)
        for entry in self.schedule:
            if entry.applies(account, moment):
                layered = layered.then(entry)
        return layered.then(self.turtle) if self.turtle.on else layered


class MeteredWindow(BaseModel, frozen=True):
    """One window of an account as last read, and how fast it has been filling."""

    window: PacingWindow
    per_hour: float | None = None
    """Percent of it spent per hour over the last hour of readings, where
    enough of them span this window to say."""


class AccountStanding(BaseModel, frozen=True):
    """What an account's windows read, the last time they were read."""

    account: Account
    windows: list[MeteredWindow] = []
    read_at: datetime | None = None
    error: str = ""
    """Why the last reading failed, where it did; the windows are the last good ones."""


class Spend(BaseModel, frozen=True):
    """What an agent spent: dollars where its runtime prices work, tokens always."""

    usd: float = 0.0
    tokens: int = 0


class AgentCaps(BaseModel, frozen=True, extra="forbid"):
    """One agent's own limits: a rate it may not pass in an hour, and a total.

    A rate slows the agent: it holds until its last hour's spend falls back
    under the cap. A total stops it until the operator raises or clears it.
    """

    rate_usd: float | None = Field(default=None, gt=0)
    rate_tokens: int | None = Field(default=None, gt=0)
    total_usd: float | None = Field(default=None, gt=0)
    total_tokens: int | None = Field(default=None, gt=0)

    def over_rate(self, hour: Spend) -> str:
        """What the last hour spent past the rate cap, in words; nothing where it is under."""
        if self.rate_usd is not None and hour.usd >= self.rate_usd:
            return f"${hour.usd:.2f} in the last hour, its cap ${self.rate_usd:.2f}"
        if self.rate_tokens is not None and hour.tokens >= self.rate_tokens:
            return (
                f"{hour.tokens:,} tokens in the last hour, its cap {self.rate_tokens:,}"
            )
        return ""

    def over_total(self, total: Spend) -> str:
        """What it spent past its total cap, in words; nothing where it is under."""
        if self.total_usd is not None and total.usd >= self.total_usd:
            return f"spent ${total.usd:.2f} of its ${self.total_usd:.2f} cap"
        if self.total_tokens is not None and total.tokens >= self.total_tokens:
            return f"spent {total.tokens:,} tokens of its {self.total_tokens:,} cap"
        return ""

    def said(self) -> list[str]:
        """Each cap set, in words."""
        return [
            *([f"${self.rate_usd:.2f} an hour"] if self.rate_usd is not None else []),
            *(
                [f"{self.rate_tokens:,} tokens an hour"]
                if self.rate_tokens is not None
                else []
            ),
            *([f"${self.total_usd:.2f} in all"] if self.total_usd is not None else []),
            *(
                [f"{self.total_tokens:,} tokens in all"]
                if self.total_tokens is not None
                else []
            ),
        ]


class AgentStanding(BaseModel, frozen=True):
    """One agent as the judgement reads it."""

    key: str
    account: Account
    priority: Priority = "normal"
    exempt: bool = False
    """The operator's own interactive session, which nothing here ever holds."""

    wanting: datetime | None = None
    """Since when it has been working or waiting at a call to work; none while idle."""

    hour: Spend = Spend()
    """What it spent in the last hour."""

    hour_clears: datetime | None = None
    """When the earliest of that hour's spend leaves it."""

    total: Spend = Spend()
    caps: AgentCaps = AgentCaps()


class Verdict(BaseModel, frozen=True):
    """An agent the budget holds: why, in words its row and the agent read, and until when."""

    key: str
    cause: Cause
    said: str
    until: datetime | None = None
    """When it lifts on its own; none where it lifts only when what holds it changes."""


def clock(moment: datetime, now: datetime) -> str:
    """A moment as a reader expects it: its time today, else its day and time."""
    local, today = moment.astimezone(), now.astimezone()
    if local.date() == today.date():
        return local.strftime("%H:%M")
    return local.strftime("%a %H:%M")


def pressed(over: float, leeway: float, priority: Priority) -> bool:
    """Whether a speed limit passed by *over* points holds an agent of *priority*.

    Low holds as soon as the limit is passed, normal once it is passed by
    the leeway, high by twice that.
    """
    return over > 0 and over > leeway * (2 - rank(priority))


def judged(
    accounts: Sequence[AccountStanding],
    agents: Sequence[AgentStanding],
    config: BudgetConfig,
    now: datetime,
) -> list[Verdict]:
    """Every agent the budget holds now, with the first reason that holds it.

    The reasons are weighed in the order they bind: a window used up, the
    reserve, the agent's total and its rate, the account's speed limit, and
    last the slots, which go to the agents nothing else holds, by priority
    and then by who wanted one first.
    """
    standing = {each.account.key: each for each in accounts}

    def windows_of(agent: AgentStanding) -> list[MeteredWindow]:
        """The agent's account's windows that have not cleared since they were read."""
        key = agent.account.key
        read = standing[key].windows if key in standing else []
        return [each for each in read if each.window.resets_at > now]

    def paced(
        agent: AgentStanding, limits: Limits, windows: list[MeteredWindow]
    ) -> Verdict | None:
        """The window the account's speed limit holds *agent* for, if any."""
        leeway = limits.leeway()
        for each in windows:
            window = each.window
            even = window.even_pct(now)
            if limits.pace == "even" and pressed(
                window.utilization_pct - even, leeway, agent.priority
            ):
                caught = window.resets_at - timedelta(
                    hours=window.window_hours * (1 - window.utilization_pct / 100)
                )
                return Verdict(
                    key=agent.key,
                    cause="pace",
                    said=(
                        f"over its rate: {window.label} window "
                        f"{window.utilization_pct:.0f}% used with {even:.0f}% of it "
                        f"gone, ahead of even pace until {clock(caught, now)}"
                    ),
                    until=caught,
                )
            ceiling = limits.ceiling(window.label)
            if (
                ceiling is not None
                and each.per_hour is not None
                and pressed(each.per_hour - ceiling, leeway, agent.priority)
            ):
                return Verdict(
                    key=agent.key,
                    cause="pace",
                    said=(
                        f"over its rate: {window.label} window filling at "
                        f"{each.per_hour:.1f}%/h, its limit {ceiling:.1f}%/h"
                    ),
                )
        return None

    def held(agent: AgentStanding) -> Verdict | None:
        """The first reason holding *agent* other than a slot, or nothing."""
        limits = config.limits(agent.account, now)
        windows = windows_of(agent)
        used = next(
            (each.window for each in windows if each.window.utilization_pct >= 100),
            None,
        )
        if used is not None:
            return Verdict(
                key=agent.key,
                cause="window",
                said=f"{used.label} window used up until {clock(used.resets_at, now)}",
                until=used.resets_at,
            )
        reserve = limits.reserve or 0.0
        kept = next(
            (
                each.window
                for each in windows
                if reserve > 0 and each.window.utilization_pct >= 100 - reserve
            ),
            None,
        )
        if kept is not None:
            return Verdict(
                key=agent.key,
                cause="reserve",
                said=(
                    f"reserve reached: {kept.label} window at "
                    f"{kept.utilization_pct:.0f}%, the last {reserve:.0f}% kept for you "
                    f"until {clock(kept.resets_at, now)}"
                ),
                until=kept.resets_at,
            )
        total = agent.caps.over_total(agent.total)
        if total:
            return Verdict(
                key=agent.key,
                cause="cap",
                said=f"{total}; raise or clear its cap to let it go on",
            )
        rate = agent.caps.over_rate(agent.hour)
        if rate:
            clears = agent.hour_clears
            return Verdict(
                key=agent.key,
                cause="rate",
                said=f"over its rate: {rate}"
                + (f", until {clock(clears, now)}" if clears is not None else ""),
                until=clears,
            )
        return paced(agent, limits, windows)

    def slotted(waiting: list[AgentStanding]) -> list[Verdict]:
        """The agents left waiting once each account's slots go, in order, to the rest."""
        sharing = {agent.account.key: agent.account for agent in waiting}
        return [
            Verdict(
                key=agent.key,
                cause="slot",
                said=(
                    f"waiting for a slot: {allowed} agent"
                    f"{'' if allowed == 1 else 's'} may work at once on {key}"
                ),
            )
            for key, account in sharing.items()
            if (allowed := config.limits(account, now).max_active) is not None
            for agent in [each for each in waiting if each.account.key == key][allowed:]
        ]

    candidates = [agent for agent in agents if not agent.exempt]
    verdicts = {
        verdict.key: verdict
        for verdict in (held(agent) for agent in candidates)
        if verdict is not None
    }
    waiting = sorted(
        (
            agent
            for agent in candidates
            if agent.key not in verdicts and agent.wanting is not None
        ),
        key=lambda agent: (rank(agent.priority), agent.wanting or now),
    )
    verdicts.update({verdict.key: verdict for verdict in slotted(waiting)})
    return [verdicts[agent.key] for agent in agents if agent.key in verdicts]
