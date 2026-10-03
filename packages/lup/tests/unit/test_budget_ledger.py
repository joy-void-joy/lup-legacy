"""The ledger every limit charges: per-agent spend and rate, settings, working marks, and the door."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from lup.observability.usage.models import PacingWindow
from lup.sessions.budget import (
    AgentLedger,
    Charge,
    FinancialBudgetConfig,
    FinancialBudgetStore,
    SpendLedger,
)
from lup.sessions.limits import (
    Account,
    AccountStanding,
    AgentCaps,
    BudgetConfig,
    MeteredWindow,
    Spend,
)

WORK = Account(runtime="claude", profile="work")
START = 1_790_000_040.0


def charge(
    agent: str, at: float, usd: float = 1.0, tokens: int = 100, request: str = ""
) -> Charge:
    return Charge(
        agent=agent, account=WORK, at=at, usd=usd, tokens=tokens, request=request
    )


def test_charges_add_up_per_agent_and_a_resent_request_counts_once(
    tmp_path: Path,
) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json")
    ledger.charge([charge("a", START, request="req_1"), charge("b", START, usd=2)])
    state = ledger.charge(
        [charge("a", START + 5, request="req_1"), charge("a", START + 70)]
    )
    line = state.line("a")
    assert line is not None
    assert line.total == Spend(usd=2.0, tokens=200)
    assert [each.minute for each in line.recent] == [
        int(START // 60),
        int((START + 70) // 60),
    ]
    assert SpendLedger(tmp_path / "ledger.json").read().line("b") is not None


def test_the_last_hour_forgets_older_minutes_and_says_when_it_clears(
    tmp_path: Path,
) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json", span=3600)
    ledger.charge([charge("a", START), charge("a", START + 1800, usd=3)])
    line = ledger.read().line("a")
    assert line is not None
    assert line.hour(START + 1800, 3600) == Spend(usd=4.0, tokens=200)
    assert line.hour(START + 3700, 3600) == Spend(usd=3.0, tokens=100)
    cleared = line.clears(START + 1800, 3600)
    assert cleared == datetime.fromtimestamp(int(START // 60) * 60 + 3600, tz=UTC)


def test_settings_survive_charges_and_charges_survive_settings(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json")
    ledger.charge([charge("a", START)])
    ledger.settle("a", WORK, START + 1, priority="low", caps=AgentCaps(total_usd=5))
    state = ledger.charge([charge("a", START + 2)])
    line = state.line("a")
    assert line is not None
    assert (line.priority, line.caps.total_usd, line.total.usd) == ("low", 5, 2.0)


def test_idle_lines_go_after_the_retention_but_working_ones_stay(
    tmp_path: Path,
) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json", retained=60)
    ledger.charge([charge("old", START), charge("busy", START)])
    ledger.working([AgentLedger(agent="busy", account=WORK, working=START)], START)
    state = ledger.charge([charge("new", START + 120)])
    assert {each.agent for each in state.agents} == {"busy", "new"}


def test_the_period_and_the_agent_line_are_charged_together(tmp_path: Path) -> None:
    config = FinancialBudgetConfig(
        maximum_usd=10,
        period_seconds=3600,
        state_path=tmp_path / "ledger.json",
        usage_cost=lambda usage: 1.0,
        account=WORK,
        agent="pipeline:nightly",
    )
    store = FinancialBudgetStore(config)
    assert store.transact(START, charge_usd=4, tokens=10).spent_usd == 4
    line = store.ledger.read().line("pipeline:nightly")
    assert line is not None and line.total == Spend(usd=4.0, tokens=10)


def test_the_door_waits_for_a_slot_that_launched_sessions_hold(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = SpendLedger(path)
    ledger.working(
        [AgentLedger(agent="repo/session", account=WORK, working=START - 60)], START
    )
    store = FinancialBudgetStore(
        FinancialBudgetConfig(
            maximum_usd=10,
            state_path=path,
            usage_cost=lambda usage: 0.0,
            account=WORK,
            agent="pipeline:nightly",
            limits=BudgetConfig(max_active=1),
        )
    )
    verdict = store.verdict(START)
    assert verdict is not None and verdict.cause == "slot"
    ledger.working(
        [AgentLedger(agent="repo/session", account=WORK, working=None)], START
    )
    assert store.verdict(START) is None


def test_the_door_reads_the_windows_the_dashboard_published(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    now = datetime.fromtimestamp(START, tz=UTC)
    SpendLedger(path).published(
        [
            AccountStanding(
                account=WORK,
                windows=[
                    MeteredWindow(
                        window=PacingWindow(
                            label="weekly",
                            utilization_pct=100,
                            resets_at=now + timedelta(hours=3),
                            window_hours=168,
                        )
                    )
                ],
            )
        ]
    )
    store = FinancialBudgetStore(
        FinancialBudgetConfig(
            maximum_usd=10,
            state_path=path,
            usage_cost=lambda usage: 0.0,
            account=WORK,
            limits=BudgetConfig(),
        )
    )
    verdict = store.verdict(START)
    assert verdict is not None and verdict.cause == "window"
    assert store.verdict(START + 4 * 3600) is None
