"""The budget from a terminal: what it last read, the turtle, and an agent's priority and caps.

The settings the page's ``:turtle``, ``:priority`` and ``:cap`` change, written
where the dashboard reads them at its next look — the turtle in the person's
lup config, an agent's priority and caps in the budget's ledger beside its
spend — so they hold whether or not a dashboard runs. Changing one is the
operator's alone, as on the page: a session that could lift the turtle or
raise its own cap would spend what the operator kept back.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, get_args

import typer
from pydantic import BaseModel, TypeAdapter, ValidationError

from lup.coordination.bare import store
from lup.coordination.repository import RepositoryPeers
from lup.devtools.dashboard.address import AdvertisedDashboard
from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.dashboard.pulse import DashboardPulse, PulseFile
from lup.diagnostics import refuse
from lup.launch.config_volume import LaunchedAccount, LaunchedAccounts
from lup.policy.kernel.diagnostic import devtools, step
from lup.providers.user_config import UserConfigFile
from lup.sessions.budget import AgentLedger, SpendLedger, budget_ledger
from lup.sessions.limits import (
    Account,
    AccountStanding,
    AgentCaps,
    BudgetConfig,
    Priority,
    clock,
)


class Turned(StrEnum):
    """Which way the turtle goes."""

    ON = "on"
    OFF = "off"


class LedgerAgent(BaseModel, frozen=True):
    """One agent of a repository, under the key the budget's ledger keeps it by."""

    key: str
    member: str


def ledger_agent(root: Path, spelling: str) -> LedgerAgent:
    """The agent *spelling* reaches in the repository of *root*: a name or an id, as `coordination send` takes one."""
    reached = RepositoryPeers(root).address(spelling)
    if reached is None:
        refuse(
            f"no agent of this repository answers to {spelling!r}",
            what=spelling,
            steps=[step("list them", devtools("coordination", "roster"))],
            code=2,
        )
    return LedgerAgent(
        key=f"{KnownRepository.of(root).key()}/{reached.id}", member=reached.id
    )


def settled(
    agent: LedgerAgent,
    ledger: SpendLedger,
    priority: Priority | None = None,
    caps: AgentCaps | None = None,
    accounts: LaunchedAccounts | None = None,
) -> AgentLedger:
    """Set one agent's priority or caps in the ledger, the way the page sets them.

    An agent the ledger has no line for yet is put on the account its launch
    recorded, or none; the dashboard's next look puts it on the account it
    draws on either way.
    """

    def drawn(line: AgentLedger | None, launched: LaunchedAccount | None) -> Account:
        match line, launched:
            case AgentLedger(account=account), _:
                return account
            case None, LaunchedAccount(runtime=runtime, owner=owner):
                return Account(runtime=runtime, profile=owner.profile or "default")
            case _:
                return Account()

    account = drawn(
        ledger.read().line(agent.key),
        (accounts or LaunchedAccounts()).launched(agent.member),
    )
    return ledger.settle(
        agent.key, account, datetime.now(UTC).timestamp(), priority, caps
    )


def budget_report(
    root: Path,
    ledger: SpendLedger,
    config: BudgetConfig,
    now: datetime,
    pulse: DashboardPulse | None = None,
) -> list[str]:
    """What `dashboard budget` prints: every account as last read, and this repository's agents.

    The accounts are the running dashboard's, from the pulse it lends a
    session, where one is lent: a session's own state is not the
    dashboard's. Otherwise they are the ledger's, which the dashboard writes
    where it runs.
    """
    state = ledger.read()
    accounts = pulse.accounts if pulse is not None else state.accounts
    epoch = now.timestamp()

    def account_lines(standing: AccountStanding) -> list[str]:
        windows = [
            f"{each.window.label} {each.window.utilization_pct:.0f}% "
            f"(even pace {each.window.even_pct(now):.0f}%), "
            f"clears {clock(each.window.resets_at, now)}"
            for each in standing.windows
            if each.window.resets_at > now
        ]
        limits = config.limits(standing.account, now).said()
        read = (
            f"read {clock(standing.read_at, now)}"
            if standing.read_at is not None
            else "never read"
        )
        return [
            f"{standing.account.key}: {' · '.join(windows) or 'no window open'}",
            f"  limits: {' · '.join(limits) or 'none'}",
            f"  {read}"
            + (
                f"; the last reading failed: {standing.error}" if standing.error else ""
            ),
        ]

    peers = RepositoryPeers(root)
    repository = KnownRepository.of(root).key()
    names = {
        f"{repository}/{member}": name
        for member, name in store.called(peers.root).items()
    }

    def agent_line(line: AgentLedger) -> str:
        hour = line.hour(epoch, ledger.span)
        caps = line.caps.said()
        return " · ".join(
            [
                f"{names[line.agent]}: ${hour.usd:.2f} in the last hour",
                f"${line.total.usd:.2f} and {line.total.tokens:,} tokens in all",
                line.priority,
                *([f"caps {', '.join(caps)}"] if caps else []),
            ]
        )

    here = [agent_line(line) for line in state.agents if line.agent in names]
    return [
        f"The turtle is {'on' if config.turtle.on else 'off'}.",
        *(line for standing in accounts for line in account_lines(standing)),
        *(
            [
                "No account read yet: "
                + (
                    pulse.metering
                    if pulse is not None and pulse.metering
                    else "the dashboard reads them while it runs."
                )
            ]
            if not accounts
            else []
        ),
        *(
            ["This repository's agents:", *(f"  {each}" for each in here)]
            if here
            else []
        ),
    ]


def lent_pulse() -> DashboardPulse | None:
    """The running dashboard's pulse, where this process's launch lent it one."""
    advertised = AdvertisedDashboard()
    return PulseFile(path=Path(advertised.pulse)).read() if advertised.pulse else None


def budget_commands(
    app: typer.Typer,
    root: Path,
    refused: Callable[[str, Callable[[], None]], None],
    ledger: Callable[[], SpendLedger] = budget_ledger,
) -> None:
    """Add the budget's verbs to the `dashboard` group: `budget` reads, the rest are the operator's."""
    config = UserConfigFile()

    def person() -> BudgetConfig:
        try:
            return config.load().budget
        except ValueError as unread:
            refuse(str(unread), what="[budget]", code=2)

    @app.command("budget")
    def budget_cmd() -> None:
        """Print each account's windows as the budget last read them, its limits, and what this repository's agents spent."""
        for line in budget_report(
            root, ledger(), person(), datetime.now(UTC), lent_pulse()
        ):
            typer.echo(line)

    @app.command("turtle")
    def turtle_cmd(
        turned: Annotated[
            Turned | None,
            typer.Argument(help="on or off; left out, says which it is"),
        ] = None,
    ) -> None:
        """Put every account under the turtle's slower limits, or back under its usual ones."""
        if turned is None:
            typer.echo(f"The turtle is {'on' if person().turtle.on else 'off'}.")
            return

        def turn() -> None:
            config.record({("budget", "turtle", "on"): turned == Turned.ON})
            typer.echo(
                f"The turtle is {turned}: "
                + (
                    "every account is under its slower limits."
                    if turned == Turned.ON
                    else "every account is back under its usual limits."
                )
            )

        refused("turtle", turn)

    @app.command("priority")
    def priority_cmd(
        agent: Annotated[
            str, typer.Argument(help="The agent's name or id in this repository")
        ],
        priority: Annotated[
            str,
            typer.Argument(help=f"One of {', '.join(get_args(Priority.__value__))}"),
        ],
    ) -> None:
        """Set an agent's priority under its account's limits: low holds first, high last."""
        try:
            chosen: Priority = TypeAdapter(Priority).validate_python(priority)
        except ValidationError:
            refuse(
                f"{priority!r} is no priority",
                what=priority,
                steps=[step(f"name one of {', '.join(get_args(Priority.__value__))}")],
                code=2,
            )

        def settle() -> None:
            line = settled(ledger_agent(root, agent), ledger(), priority=chosen)
            typer.echo(f"{agent} is {line.priority} priority from the next look.")

        refused("priority", settle)

    @app.command("cap")
    def cap_cmd(
        agent: Annotated[
            str, typer.Argument(help="The agent's name or id in this repository")
        ],
        rate_usd: Annotated[
            float | None,
            typer.Option(help="Dollars it may spend in an hour; past it, it slows"),
        ] = None,
        rate_tokens: Annotated[
            int | None,
            typer.Option(help="Tokens it may spend in an hour; past it, it slows"),
        ] = None,
        total_usd: Annotated[
            float | None,
            typer.Option(help="Dollars it may spend in all; past it, it stops"),
        ] = None,
        total_tokens: Annotated[
            int | None,
            typer.Option(help="Tokens it may spend in all; past it, it stops"),
        ] = None,
    ) -> None:
        """Set an agent's caps, every one at once: a cap left out is cleared, and none clears them all."""
        try:
            caps = AgentCaps(
                rate_usd=rate_usd,
                rate_tokens=rate_tokens,
                total_usd=total_usd,
                total_tokens=total_tokens,
            )
        except ValidationError:
            refuse(
                "a cap is more than nothing",
                steps=[step("leave a cap out to clear it")],
                code=2,
            )

        def settle() -> None:
            line = settled(ledger_agent(root, agent), ledger(), caps=caps)
            said = line.caps.said()
            typer.echo(
                f"{agent}'s caps from the next look: {', '.join(said)}."
                if said
                else f"{agent} has no cap from the next look."
            )

        refused("cap", settle)
