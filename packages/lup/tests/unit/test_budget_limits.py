"""Which agents the budget holds, why, and until when — from limits, windows and spend alone."""

from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from lup.observability.usage.models import PacingWindow
from lup.providers.user_config import UserConfigFile
from lup.sessions.limits import (
    Account,
    AccountStanding,
    AgentCaps,
    AgentStanding,
    BudgetConfig,
    Ceiling,
    Limits,
    MeteredWindow,
    ScheduledLimits,
    Spend,
    Turtle,
    clock,
    judged,
)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
WORK = Account(runtime="claude", profile="work")


def window(used: float, hours_left: float, length: float = 5.0) -> MeteredWindow:
    return MeteredWindow(
        window=PacingWindow(
            label="5-hour",
            utilization_pct=used,
            resets_at=NOW + timedelta(hours=hours_left),
            window_hours=length,
        )
    )


def account(*windows: MeteredWindow) -> AccountStanding:
    return AccountStanding(account=WORK, windows=list(windows), read_at=NOW)


def agent(key: str, **fields: object) -> AgentStanding:
    return AgentStanding.model_validate({"key": key, "account": WORK, **fields})


def causes(
    config: BudgetConfig, accounts: list[AccountStanding], *agents: AgentStanding
) -> dict[str, str]:
    return {
        verdict.key: verdict.cause for verdict in judged(accounts, agents, config, NOW)
    }


def test_nothing_holds_without_limits() -> None:
    assert (
        causes(BudgetConfig(), [account(window(90, 4))], agent("a", wanting=NOW)) == {}
    )


def test_a_used_up_window_holds_everyone_but_the_operator_until_it_clears() -> None:
    verdicts = judged(
        [account(window(100, 2))],
        [agent("a"), agent("mine", exempt=True)],
        BudgetConfig(),
        NOW,
    )
    assert [(each.key, each.cause) for each in verdicts] == [("a", "window")]
    assert verdicts[0].until == NOW + timedelta(hours=2)
    assert "5-hour window used up until" in verdicts[0].said


def test_a_window_at_its_ceiling_holds_every_agent_with_no_configuration() -> None:
    weekly = window(96, 50, length=168).model_copy(
        update={
            "window": window(96, 50, length=168).window.model_copy(
                update={"label": "weekly"}
            )
        }
    )
    for reading in (window(95, 2), weekly):
        verdicts = judged(
            [account(reading)],
            [agent("a"), agent("b", priority="high"), agent("mine", exempt=True)],
            BudgetConfig(),
            NOW,
        )
        assert [(each.key, each.cause) for each in verdicts] == [
            ("a", "window"),
            ("b", "window"),
        ]
        assert verdicts[0].until == reading.window.resets_at
        assert f"{reading.window.label} window used up until" in verdicts[0].said
    assert causes(BudgetConfig(), [account(window(94, 2))], agent("a")) == {}


def test_the_window_ceiling_is_the_persons_to_move() -> None:
    lower = BudgetConfig(window_ceiling=80)
    assert causes(lower, [account(window(81, 2))], agent("a")) == {"a": "window"}
    provider = BudgetConfig(window_ceiling=100)
    assert causes(provider, [account(window(99, 2))], agent("a")) == {}
    assert BudgetConfig().said()[0] == "hold at 95%"
    assert BudgetConfig(window_ceiling=100).said() == []


def test_the_reserve_holds_once_a_window_reaches_it() -> None:
    config = BudgetConfig(reserve=10)
    assert causes(config, [account(window(89, 1))], agent("a")) == {}
    verdicts = judged(
        [account(window(91, 1))], [agent("a", priority="high")], config, NOW
    )
    assert verdicts[0].cause == "reserve"
    assert verdicts[0].until == NOW + timedelta(hours=1)


def test_even_pace_holds_low_first_then_normal_then_high() -> None:
    # Two of five hours gone: even pace is 60% used; tolerance 5 points.
    config = BudgetConfig(pace="even", tolerance=5)
    agents = [
        agent("low", priority="low"),
        agent("normal"),
        agent("high", priority="high"),
    ]
    assert causes(config, [account(window(62, 2))], *agents) == {"low": "rate"}
    assert causes(config, [account(window(66, 2))], *agents) == {
        "low": "rate",
        "normal": "rate",
    }
    assert causes(config, [account(window(71, 2))], *agents) == {
        "low": "rate",
        "normal": "rate",
        "high": "rate",
    }


def test_a_pace_hold_lifts_when_even_pace_catches_up() -> None:
    verdict = judged(
        [account(window(70, 2))],
        [agent("a", priority="low")],
        BudgetConfig(pace="even"),
        NOW,
    )[0]
    # 70% of a five-hour window is reached at 3.5 h in, half an hour from now.
    assert verdict.until == NOW + timedelta(minutes=30)


def test_a_ceiling_holds_by_how_fast_the_window_fills() -> None:
    filling = MeteredWindow(window=window(40, 3).window, per_hour=30)
    config = BudgetConfig(
        ceilings=[Ceiling(window="5-hour", per_hour=25)], tolerance=10
    )
    assert causes(
        config, [account(filling)], agent("low", priority="low"), agent("normal")
    ) == {"low": "rate"}


def test_caps_hold_one_agent_alone() -> None:
    capped = agent(
        "a",
        total=Spend(usd=5.0, tokens=10),
        caps=AgentCaps(total_usd=5.0),
    )
    slow = agent(
        "b",
        hour=Spend(tokens=600_000),
        hour_clears=NOW + timedelta(minutes=12),
        caps=AgentCaps(rate_tokens=500_000),
    )
    verdicts = {
        each.key: each
        for each in judged([], [capped, slow, agent("c")], BudgetConfig(), NOW)
    }
    assert verdicts["a"].cause == "cap" and verdicts["a"].until is None
    assert verdicts["b"].cause == "rate" and verdicts["b"].until == NOW + timedelta(
        minutes=12
    )
    clears = clock(NOW + timedelta(minutes=12), NOW)
    assert verdicts["b"].said.endswith(f", until {clears}")
    assert "c" not in verdicts


def test_limits_say_each_limit_set_and_nothing_else() -> None:
    assert Limits().said() == []
    assert Limits(
        pace="even",
        ceilings=[Ceiling(window="5h", per_hour=12.5)],
        reserve=10,
        max_active=0,
    ).said() == ["even pace", "5h ≤12.5%/h", "keep 10%", "≤0 at once"]
    assert Limits(reserve=0).said() == []


def test_slots_go_by_priority_then_first_come() -> None:
    config = BudgetConfig(max_active=2)
    agents = [
        agent("late-low", priority="low", wanting=NOW - timedelta(minutes=50)),
        agent("early", wanting=NOW - timedelta(minutes=30)),
        agent("late", wanting=NOW - timedelta(minutes=5)),
        agent("urgent", priority="high", wanting=NOW),
        agent("idle"),
        agent("mine", exempt=True, wanting=NOW - timedelta(hours=1)),
    ]
    assert causes(config, [], *agents) == {"late-low": "slot", "late": "slot"}


def test_an_agent_held_for_another_reason_takes_no_slot() -> None:
    config = BudgetConfig(max_active=1)
    capped = agent(
        "capped",
        wanting=NOW - timedelta(hours=1),
        total=Spend(usd=9),
        caps=AgentCaps(total_usd=1),
    )
    assert causes(config, [], capped, agent("next", wanting=NOW)) == {"capped": "cap"}


def test_layers_apply_in_order_and_replace_only_what_they_name() -> None:
    config = BudgetConfig.model_validate(
        {
            "reserve": 10,
            "max_active": 4,
            "accounts": {"work": {"reserve": 20}},
            "schedule": [{"from": "11:00", "to": "13:00", "max_active": 2}],
            "turtle": {"on": True},
        }
    )
    limits = config.limits(WORK, datetime.combine(NOW.date(), time(12)).astimezone())
    assert (limits.reserve, limits.max_active, limits.pace) == (20, 1, "even")
    assert config.limits(Account(runtime="codex", profile="home"), NOW).reserve == 10


@pytest.mark.parametrize(
    ("start", "end", "at", "applies"),
    [
        (time(9), time(17), time(12), True),
        (time(9), time(17), time(18), False),
        (time(22), time(6), time(23), True),
        (time(22), time(6), time(5), True),
        (time(22), time(6), time(12), False),
    ],
)
def test_a_schedule_entry_runs_overnight_when_it_ends_before_it_starts(
    start: time, end: time, at: time, applies: bool
) -> None:
    entry = ScheduledLimits(start=start, end=end, max_active=1)
    moment = datetime.combine(NOW.date(), at).astimezone()
    assert entry.applies(WORK, moment) is applies


def test_a_schedule_entry_keeps_to_its_days_and_accounts() -> None:
    entry = ScheduledLimits(
        days=["sat", "sun"], start=time(0), end=time(23, 59), accounts=["home"]
    )
    monday = datetime(2026, 10, 5, 12).astimezone()
    saturday = datetime(2026, 10, 3, 12).astimezone()
    assert not entry.applies(Account(runtime="claude", profile="home"), monday)
    assert entry.applies(Account(runtime="claude", profile="home"), saturday)
    assert not entry.applies(WORK, saturday)


def test_turtle_defaults_to_even_pace_and_one_agent() -> None:
    limits = Limits().then(Turtle(on=True))
    assert (limits.pace, limits.max_active) == ("even", 1)


def test_the_person_writes_limits_in_their_lup_config(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        """
[budget]
reserve = 10
max_active = 3
pace = "even"

[[budget.ceilings]]
window = "5-hour"
per_hour = 25

[budget.accounts.work]
reserve = 25

[[budget.schedule]]
days = ["mon", "tue", "wed", "thu", "fri"]
from = "09:00"
to = "18:00"
max_active = 1

[budget.turtle]
on = true
"""
    )
    budget = UserConfigFile(tmp_path).load().budget
    assert (budget.reserve, budget.max_active, budget.pace) == (10, 3, "even")
    assert budget.ceiling("5-hour") == 25
    assert budget.accounts["work"].reserve == 25
    assert budget.schedule[0].start == time(9) and budget.schedule[0].days[-1] == "fri"
    assert budget.turtle.on and budget.limits(WORK, NOW).max_active == 1
