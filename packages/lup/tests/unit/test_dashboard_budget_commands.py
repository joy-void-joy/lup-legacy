"""The budget from a terminal: what `dashboard budget` reads, and the operator's turtle, priority and caps."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from lup.coordination.identity import MEMBER_ENV, mint_member_id
from lup.coordination.repository import RepositoryPeers
from lup.devtools.dashboard.budget import budget_ledger
from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.dashboard.reviews import create_operator_dashboard_app
from lup.observability.usage.models import PacingWindow
from lup.providers.user_config import UserConfigFile
from lup.sessions.budget import Charge
from lup.sessions.limits import Account, AccountStanding, AgentCaps, MeteredWindow

WORK = Account(runtime="claude", profile="work")
runner = CliRunner()


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout whose budget, config and launch records are the test's own."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "config" / "lup").mkdir(parents=True)
    root = tmp_path / "checkout"
    root.mkdir()
    return root


def dashboard(root: Path) -> typer.Typer:
    cli = typer.Typer()
    cli.add_typer(create_operator_dashboard_app(root), name="dashboard")
    return cli


def joined(root: Path, name: str) -> str:
    """Put one agent called *name* on the checkout's roster; its ledger key."""
    member = mint_member_id()
    RepositoryPeers(root).join(member, root, cli_name=name)
    return f"{KnownRepository.of(root).key()}/{member}"


def test_the_budget_prints_each_account_and_what_this_repositorys_agents_spent(
    checkout: Path,
) -> None:
    UserConfigFile().path().write_text("[budget]\nreserve = 10\nmax_active = 2\n")
    lead = joined(checkout, "lead")
    now = datetime.now(UTC)
    ledger = budget_ledger()
    ledger.published(
        [
            AccountStanding(
                account=WORK,
                windows=[
                    MeteredWindow(
                        window=PacingWindow(
                            label="5-hour",
                            utilization_pct=40,
                            resets_at=now + timedelta(hours=2),
                            window_hours=5,
                        )
                    )
                ],
                read_at=now,
            )
        ]
    )
    ledger.charge(
        [
            Charge(
                agent=lead,
                account=WORK,
                at=now.timestamp(),
                usd=1.5,
                tokens=30_000,
                request="one",
            )
        ]
    )
    ledger.settle(lead, WORK, now.timestamp(), "low", AgentCaps(total_usd=20))

    result = runner.invoke(dashboard(checkout), ["dashboard", "budget"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "The turtle is off."
    assert lines[1].startswith("claude:work: 5-hour 40% (even pace 60%), clears ")
    assert lines[2] == "  limits: keep 10% · ≤2 at once"
    assert (
        "  lead: $1.50 in the last hour · $1.50 and 30,000 tokens in all · low"
        " · caps $20.00 in all"
    ) in lines


def test_an_agents_priority_and_caps_are_set_by_its_name(checkout: Path) -> None:
    lead = joined(checkout, "lead")
    cli = dashboard(checkout)

    raised = runner.invoke(cli, ["dashboard", "priority", "lead", "high"])
    capped = runner.invoke(
        cli,
        ["dashboard", "cap", "lead", "--rate-usd", "2", "--total-tokens", "2000000"],
    )

    assert raised.exit_code == 0, raised.output
    assert "lead is high priority from the next look." in raised.output
    assert capped.exit_code == 0, capped.output
    assert (
        "lead's caps from the next look: $2.00 an hour, 2,000,000 tokens in all."
        in capped.output
    )
    line = budget_ledger().read().line(lead)
    assert line is not None and line.priority == "high"
    assert line.caps == AgentCaps(rate_usd=2, total_tokens=2_000_000)

    cleared = runner.invoke(cli, ["dashboard", "cap", "lead"])
    assert "lead has no cap from the next look." in cleared.output
    line = budget_ledger().read().line(lead)
    assert line is not None and line.caps == AgentCaps() and line.priority == "high"


def test_what_names_no_agent_or_no_priority_is_refused(checkout: Path) -> None:
    joined(checkout, "lead")
    cli = dashboard(checkout)

    nobody = runner.invoke(cli, ["dashboard", "priority", "nobody", "low"])
    urgent = runner.invoke(cli, ["dashboard", "priority", "lead", "urgent"])
    nothing = runner.invoke(cli, ["dashboard", "cap", "lead", "--total-usd", "0"])

    assert nobody.exit_code == 2
    assert "No agent of this repository answers to 'nobody'" in nobody.output
    assert urgent.exit_code != 0 and "is no priority" in urgent.output
    assert nothing.exit_code != 0 and "more than nothing" in nothing.output
    assert budget_ledger().read().agents == []


def test_the_turtle_is_the_operators_to_turn(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = dashboard(checkout)

    turned = runner.invoke(cli, ["dashboard", "turtle", "on"])
    asked = runner.invoke(cli, ["dashboard", "turtle"])
    monkeypatch.setenv(MEMBER_ENV, "a-session")
    refused = runner.invoke(cli, ["dashboard", "turtle", "off"])

    assert turned.exit_code == 0, turned.output
    assert "every account is under its slower limits" in turned.output
    assert asked.output.strip() == "The turtle is on."
    assert refused.exit_code == 2 and "is the operator's" in refused.output
    assert UserConfigFile().load().budget.turtle.on
