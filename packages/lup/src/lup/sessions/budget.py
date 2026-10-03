"""A spending ceiling that outlives the process holding it, and the ledger every limit charges.

:class:`~lup.sessions.middleware.BudgetConfig` caps one logical turn and raises
when the turn would exceed it. This caps a *period* — a rolling allowance such
as "so many dollars a day" — and, like :mod:`lup.sessions.quota`, waits rather
than failing: the run is long-lived and the money comes back when the window
rolls over, so stopping it would abandon work that only needed to be slower.

Two properties follow from the ceiling being an account's rather than a run's.
It is durable, because a run that crashes and restarts must not get a fresh
allowance; and it is shared, because several agents drawing on one account are
spending the same money. Both come from one small locked file the period's
spend is read and charged through, so concurrent processes serialize on it
rather than each keeping a private count.

That file is the :class:`SpendLedger`, and it holds more than one period's
total: what each agent spent in all and in its last hour, the limits the
operator set on it, whether it is working now, and each account's windows as
last read. Every limit charges it — a launched session's requests through the
dashboard's telemetry, an in-process pipeline's turns here — so the agents of
both draw on one budget, and :func:`lup.sessions.limits.judged` reads them the
same way wherever it runs.
"""

import asyncio
import fcntl
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from lup.sessions.capabilities import SessionEngine, SessionWrapper, TurnEngine
from lup.sessions.events import (
    SessionId,
    StartedTurn,
    TurnRequest,
    TurnResult,
)
from lup.sessions.limits import (
    Account,
    AccountStanding,
    AgentCaps,
    AgentStanding,
    BudgetConfig,
    Cause,
    Priority,
    Spend,
    Verdict,
    judged,
)
from lup.types import UsageCost
from lup.workspace.user_directories import UserDirectories


class Charge(BaseModel, frozen=True):
    """One request's spend, charged to the agent that made it on the account it drew on."""

    agent: str
    account: Account
    at: float
    """When it was spent, in seconds since the epoch."""

    usd: float = Field(default=0.0, ge=0)
    tokens: int = Field(default=0, ge=0)
    request: str = ""
    """The runtime's own id for the request, where it has one: a charge naming
    one already charged is not charged again, so an export sent twice counts once."""


class MinuteSpend(BaseModel, frozen=True):
    """What one agent spent in one minute, the grain its recent rate is read at."""

    minute: int
    usd: float = 0.0
    tokens: int = 0


class AgentLedger(BaseModel, frozen=True):
    """One agent's line in the ledger: what it spent, and what the operator set on it."""

    agent: str
    account: Account
    first: float = 0.0
    """When it was first charged or settled."""

    last: float = 0.0
    """When it was last charged or settled."""

    total: Spend = Spend()
    recent: list[MinuteSpend] = []
    """Its spend minute by minute, as far back as the ledger reads a rate."""

    priority: Priority = "normal"
    caps: AgentCaps = AgentCaps()
    working: float | None = None
    """Since when it has been working or waiting to, where somebody said so; none while idle."""

    def charged(self, charge: Charge, span: float) -> "AgentLedger":
        """This line with *charge* added, its minutes older than *span* dropped."""
        minute = int(charge.at // 60)
        here = next((each for each in self.recent if each.minute == minute), None)
        kept = [
            each
            for each in self.recent
            if each.minute != minute and (charge.at - each.minute * 60) < span
        ]
        added = MinuteSpend(
            minute=minute,
            usd=(here.usd if here else 0.0) + charge.usd,
            tokens=(here.tokens if here else 0) + charge.tokens,
        )
        return self.model_copy(
            update={
                "account": charge.account,
                "first": self.first or charge.at,
                "last": max(self.last, charge.at),
                "total": Spend(
                    usd=self.total.usd + charge.usd,
                    tokens=self.total.tokens + charge.tokens,
                ),
                "recent": sorted([*kept, added], key=lambda each: each.minute),
            }
        )

    def inside(self, epoch: float, span: float) -> list[MinuteSpend]:
        """The minutes still inside the last *span* seconds at *epoch*."""
        return [each for each in self.recent if epoch - each.minute * 60 < span]

    def hour(self, epoch: float, span: float) -> Spend:
        """What it spent in the last *span* seconds."""
        minutes = self.inside(epoch, span)
        return Spend(
            usd=sum(each.usd for each in minutes),
            tokens=sum(each.tokens for each in minutes),
        )

    def clears(self, epoch: float, span: float) -> datetime | None:
        """When the earliest minute still counted leaves the last *span* seconds."""
        minutes = self.inside(epoch, span)
        if not minutes:
            return None
        return datetime.fromtimestamp(minutes[0].minute * 60 + span, tz=UTC)

    def standing(
        self, epoch: float, span: float, exempt: bool = False
    ) -> AgentStanding:
        """This line as the judgement reads it."""
        return AgentStanding(
            key=self.agent,
            account=self.account,
            priority=self.priority,
            exempt=exempt,
            wanting=(
                datetime.fromtimestamp(self.working, tz=UTC)
                if self.working is not None
                else None
            ),
            hour=self.hour(epoch, span),
            hour_clears=self.clears(epoch, span),
            total=self.total,
            caps=self.caps,
        )


class PeriodSpend(BaseModel, frozen=True):
    """One account's spend in its current fixed allowance period."""

    account: str
    window_start_epoch: int
    spent_usd: float = Field(default=0.0, ge=0)


class LedgerState(BaseModel, frozen=True):
    """Everything the ledger holds, as one document."""

    accounts: list[AccountStanding] = []
    """Each account's windows as last read, published by whoever reads them."""

    agents: list[AgentLedger] = []
    periods: list[PeriodSpend] = []
    requests: list[str] = []
    """The latest request ids charged, oldest first."""

    def line(self, agent: str) -> AgentLedger | None:
        """The agent's line, where it has one."""
        return next((each for each in self.agents if each.agent == agent), None)

    def with_line(self, line: AgentLedger) -> "LedgerState":
        """This state with *line* in place of the agent's old one."""
        others = [each for each in self.agents if each.agent != line.agent]
        return self.model_copy(update={"agents": [*others, line]})


def utc_epoch() -> float:
    """Current UTC epoch seconds, the clock periods are cut against."""
    return datetime.now(UTC).timestamp()


class SpendLedger:
    """The one locked file every limit charges and every judgement reads.

    ``span`` is how far back an agent's rate is read — an hour unless a caller
    says otherwise — and so how many of its minutes the file keeps.
    ``remembered`` is how many request ids are kept to charge a resent export
    once; ``retained`` how long a line no charge or setting touched is kept.
    """

    def __init__(
        self,
        path: Path,
        span: float = 3600.0,
        remembered: int = 4096,
        retained: float = 7 * 86_400.0,
    ) -> None:
        self.path = path
        self.span = span
        self.remembered = remembered
        self.retained = retained

    def read(self) -> LedgerState:
        """The ledger as last written, or an empty one where none is."""
        try:
            content = self.path.read_text(encoding="utf-8")
        except OSError:
            return LedgerState()
        if not content.strip():
            return LedgerState()
        return LedgerState.model_validate_json(content)

    def transact(self, change: Callable[[LedgerState], LedgerState]) -> LedgerState:
        """Read, change and write the ledger in one locked pass.

        Read-then-write cannot be split: two agents that each read a spend of
        $9 against a $10 ceiling would both conclude they may proceed. Holding
        the lock across the whole transaction is what makes the ceiling mean
        the account rather than each process's view of it.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                content = handle.read()
                state = change(
                    LedgerState.model_validate_json(content)
                    if content.strip()
                    else LedgerState()
                )
                handle.seek(0)
                handle.truncate()
                handle.write(state.model_dump_json())
                handle.write("\n")
                handle.flush()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return state

    def charge(self, charges: Sequence[Charge]) -> LedgerState:
        """Charge each of *charges* to its agent, once per request id."""

        def charged(state: LedgerState) -> LedgerState:
            known = {request for request in state.requests}
            named = {
                each.request: each
                for each in charges
                if each.request and each.request not in known
            }
            fresh = [*named.values(), *(each for each in charges if not each.request)]
            for each in fresh:
                line = state.line(each.agent) or AgentLedger(
                    agent=each.agent, account=each.account
                )
                state = state.with_line(line.charged(each, self.span))
            remembered = [*state.requests, *named][-self.remembered :]
            return self.pruned(
                state.model_copy(update={"requests": remembered}),
                max((each.at for each in charges), default=0.0),
            )

        return self.transact(charged)

    def pruned(self, state: LedgerState, epoch: float) -> LedgerState:
        """*state* without the idle lines nothing touched for longer than the ledger retains."""
        if not epoch:
            return state
        kept = [
            each
            for each in state.agents
            if each.working is not None or epoch - each.last < self.retained
        ]
        return state.model_copy(update={"agents": kept})

    def settle(
        self,
        agent: str,
        account: Account,
        epoch: float,
        priority: Priority | None = None,
        caps: AgentCaps | None = None,
    ) -> AgentLedger:
        """Set an agent's priority or caps as the operator chose them; what is not named stays."""

        def settled(state: LedgerState) -> LedgerState:
            line = state.line(agent) or AgentLedger(
                agent=agent, account=account, first=epoch
            )
            return state.with_line(
                line.model_copy(
                    update={
                        "last": max(line.last, epoch),
                        "priority": priority if priority is not None else line.priority,
                        "caps": caps if caps is not None else line.caps,
                    }
                )
            )

        found = self.transact(settled).line(agent)
        if found is None:
            raise LookupError(f"the ledger lost {agent!r} as it was settled")
        return found

    def working(self, agents: Sequence[AgentLedger], epoch: float) -> LedgerState:
        """Say which of *agents* are working now: each named takes its ``working`` and account.

        An agent named for the first time gets a line; one already there keeps
        its spend and settings.
        """

        def marked(state: LedgerState) -> LedgerState:
            for each in agents:
                line = state.line(each.agent) or each.model_copy(
                    update={"first": epoch, "last": epoch}
                )
                state = state.with_line(
                    line.model_copy(
                        update={"working": each.working, "account": each.account}
                    )
                )
            return state

        return self.transact(marked)

    def published(self, accounts: Sequence[AccountStanding]) -> LedgerState:
        """Put each account's windows, as just read, where every judgement reads them."""

        def put(state: LedgerState) -> LedgerState:
            fresh = {each.account.key: each for each in accounts}
            kept = [each for each in state.accounts if each.account.key not in fresh]
            return state.model_copy(update={"accounts": [*kept, *fresh.values()]})

        return self.transact(put)


def budget_ledger(directories: UserDirectories | None = None) -> SpendLedger:
    """The ledger in the person's own state, which the dashboard and every in-process door share.

    An in-process pipeline that wants to draw on the same budget as the
    sessions the dashboard governs names this file as its
    :class:`~lup.sessions.budget.FinancialBudgetConfig` ``state_path``.
    """
    return SpendLedger(
        (directories or UserDirectories()).state() / "budget" / "ledger.json"
    )


class FinancialBudgetConfig(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One fixed UTC allowance period, where its spend is recorded, and whose it is.

    ``account`` and ``agent`` name what the ledger charges: the account the
    pipeline draws on and the agent it is there. ``limits``, where given, are
    the person's ``[budget]`` limits, which hold this agent at its door the
    way the dashboard holds a launched session at its next tool call;
    ``poll_seconds`` is how long a hold with no end of its own waits before
    it is judged again.
    """

    maximum_usd: float = Field(gt=0)
    period_seconds: int = Field(default=86_400, gt=0)
    state_path: Path
    usage_cost: UsageCost
    account: Account = Account()
    agent: str = "pipeline"
    limits: BudgetConfig | None = None
    poll_seconds: float = Field(default=60.0, gt=0)


class FinancialBudgetState(BaseModel, frozen=True):
    """The durable spend counter for one period."""

    window_start_epoch: int
    spent_usd: float = Field(ge=0)


class FinancialBudgetEvent(BaseModel, frozen=True):
    """Observable charge, sleep, or wake transition."""

    phase: Literal["charge", "sleep", "wake"]
    window_start_epoch: int
    spent_usd: float = Field(ge=0)
    maximum_usd: float = Field(gt=0)
    wait_seconds: float = Field(ge=0)
    charge_usd: float | None = Field(default=None, ge=0)
    cause: Cause | None = None
    """Which of the person's limits holds the turn, where one does rather
    than the period's ceiling."""

    said: str = ""
    """What that limit says of the hold."""


type BudgetSink = Callable[[FinancialBudgetEvent], Awaitable[None]]
type BudgetSleeper = Callable[[float], Awaitable[None]]
type EpochProvider = Callable[[], float]


class FinancialBudgetStore:
    """The period counter and the agent's line, read and charged through the ledger."""

    def __init__(self, config: FinancialBudgetConfig) -> None:
        self.config = config
        self.ledger = SpendLedger(config.state_path)

    def window_start(self, epoch: float) -> int:
        """The fixed period containing ``epoch``.

        Cut on absolute epoch boundaries rather than from first use, so every
        process sharing the account agrees on which window it is in without
        having to agree on when the run began.
        """
        period = self.config.period_seconds
        return int(epoch // period) * period

    def transact(
        self, epoch: float, charge_usd: float = 0, tokens: int = 0
    ) -> FinancialBudgetState:
        """Read, roll over, and charge the period and the agent in one locked pass."""
        start = self.window_start(epoch)
        key = self.config.account.key

        def charged(state: LedgerState) -> LedgerState:
            recorded = next(
                (each for each in state.periods if each.account == key), None
            )
            carried = (
                recorded.spent_usd
                if recorded is not None and recorded.window_start_epoch == start
                else 0.0
            )
            period = PeriodSpend(
                account=key, window_start_epoch=start, spent_usd=carried + charge_usd
            )
            others = [each for each in state.periods if each.account != key]
            state = state.model_copy(update={"periods": [*others, period]})
            if not charge_usd and not tokens:
                return state
            line = state.line(self.config.agent) or AgentLedger(
                agent=self.config.agent, account=self.config.account
            )
            spent = Charge(
                agent=self.config.agent,
                account=self.config.account,
                at=epoch,
                usd=charge_usd,
                tokens=tokens,
            )
            return state.with_line(line.charged(spent, self.ledger.span))

        state = self.ledger.transact(charged)
        period = next(each for each in state.periods if each.account == key)
        return FinancialBudgetState(
            window_start_epoch=period.window_start_epoch, spent_usd=period.spent_usd
        )

    def wait_seconds(self, state: FinancialBudgetState, epoch: float) -> float:
        """Seconds until the next period, with a boundary grace either side."""
        reset = state.window_start_epoch + self.config.period_seconds
        return max(1.0, reset - epoch + 1.0)

    def verdict(self, epoch: float) -> Verdict | None:
        """What the person's limits say of this agent starting a turn now, if they hold it.

        Judged over every agent the ledger says is working — the launched
        sessions the dashboard marks among them — so a pipeline waits for a
        slot the way they do.
        """
        limits = self.config.limits
        if limits is None:
            return None
        state = self.ledger.read()
        mine = state.line(self.config.agent) or AgentLedger(
            agent=self.config.agent, account=self.config.account
        )
        wanting = mine.model_copy(
            update={"working": mine.working if mine.working is not None else epoch}
        )
        others = [
            each.standing(epoch, self.ledger.span)
            for each in state.agents
            if each.agent != self.config.agent and each.working is not None
        ]
        verdicts = judged(
            state.accounts,
            [*others, wanting.standing(epoch, self.ledger.span)],
            limits,
            datetime.fromtimestamp(epoch, tz=UTC),
        )
        return next((each for each in verdicts if each.key == self.config.agent), None)

    def mark(self, epoch: float | None, now: float) -> None:
        """Say this agent is working since *epoch*, or that it stopped, where limits are judged."""
        if self.config.limits is None:
            return
        marked = AgentLedger(
            agent=self.config.agent, account=self.config.account, working=epoch
        )
        self.ledger.working([marked], now)


class FinancialBudgetTurn[T: BaseModel | None](TurnEngine[T]):
    """Charge a completed turn exactly once, and keep its result.

    A caller may await the same turn twice; the completed result is held so
    the second await returns it rather than charging the account again.
    """

    def __init__(
        self,
        inner: TurnEngine[T],
        store: FinancialBudgetStore,
        sink: BudgetSink,
        now: EpochProvider,
    ) -> None:
        self.inner = inner
        self.store = store
        self.sink = sink
        self.now = now
        self.completed: TurnResult[T] | None = None

    async def result(self) -> TurnResult[T]:
        if self.completed is not None:
            return self.completed
        try:
            result = await self.inner.result()
        finally:
            self.store.mark(None, self.now())
        charge = self.store.config.usage_cost(result.usage)
        tokens = result.usage.input_tokens + result.usage.output_tokens
        state = self.store.transact(self.now(), charge, tokens)
        await self.sink(
            FinancialBudgetEvent(
                phase="charge",
                window_start_epoch=state.window_start_epoch,
                spent_usd=state.spent_usd,
                maximum_usd=self.store.config.maximum_usd,
                wait_seconds=0,
                charge_usd=charge,
            )
        )
        self.completed = result
        return result


class FinancialBudgetSession(SessionEngine):
    """Hold new work at the door while the period is spent out or a limit holds it.

    Checked before starting rather than after charging, because a turn already
    accepted has to be allowed to finish — the overshoot from one final turn is
    charged and carried, and it is the *next* one that waits.
    """

    def __init__(
        self,
        inner: SessionEngine,
        store: FinancialBudgetStore,
        sink: BudgetSink,
        sleeper: BudgetSleeper,
        now: EpochProvider,
    ) -> None:
        self.inner = inner
        self.store = store
        self.sink = sink
        self.sleeper = sleeper
        self.now = now

    def delay(
        self, state: FinancialBudgetState, verdict: Verdict | None, epoch: float
    ) -> float:
        """How long to sleep before asking again: to the period's end, or the hold's."""
        if verdict is None:
            return self.store.wait_seconds(state, epoch)
        if verdict.until is None:
            return self.store.config.poll_seconds
        return max(1.0, verdict.until.timestamp() - epoch + 1.0)

    async def wait_for_allowance(self) -> None:
        """Sleep across exhausted periods and holding limits without changing model or provider."""
        while True:
            epoch = self.now()
            state = self.store.transact(epoch)
            spent = state.spent_usd >= self.store.config.maximum_usd
            verdict = None if spent else self.store.verdict(epoch)
            if not spent and verdict is None:
                self.store.mark(epoch, epoch)
                return
            delay = self.delay(state, verdict, epoch)
            event = FinancialBudgetEvent(
                phase="sleep",
                window_start_epoch=state.window_start_epoch,
                spent_usd=state.spent_usd,
                maximum_usd=self.store.config.maximum_usd,
                wait_seconds=delay,
                cause=verdict.cause if verdict is not None else None,
                said=verdict.said if verdict is not None else "",
            )
            await self.sink(event)
            await self.sleeper(delay)
            await self.sink(event.model_copy(update={"phase": "wake"}))

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> StartedTurn[T]:
        await self.wait_for_allowance()
        handle = await self.inner.start(request)
        return StartedTurn[T](
            turn=FinancialBudgetTurn(handle.turn, self.store, self.sink, self.now),
            events=handle.events,
            interrupt=handle.interrupt,
            steer=handle.steer,
        )


class FinancialBudgetWrapper(SessionWrapper):
    """Apply one durable period allowance across every session it wraps.

    One store for every session wrapped by the same wrapper, so the ceiling
    is the account's across them rather than each session's own.
    """

    def __init__(
        self,
        config: FinancialBudgetConfig,
        sink: BudgetSink,
        *,
        sleeper: BudgetSleeper = asyncio.sleep,
        now: EpochProvider = utc_epoch,
    ) -> None:
        self.store = FinancialBudgetStore(config)
        self.sink = sink
        self.sleeper = sleeper
        self.now = now

    def around(
        self,
        opened: AbstractAsyncContextManager[SessionEngine],
        resume: SessionId | None,
    ) -> AbstractAsyncContextManager[SessionEngine]:
        return self.budgeted(opened)

    @asynccontextmanager
    async def budgeted(
        self, opened: AbstractAsyncContextManager[SessionEngine]
    ) -> AsyncGenerator[SessionEngine]:
        async with opened as inner:
            yield FinancialBudgetSession(
                inner, self.store, self.sink, self.sleeper, self.now
            )
