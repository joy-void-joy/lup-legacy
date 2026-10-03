"""The budget governor: each account's windows, what each agent spent, and who waits for what.

The dashboard every launch holds runs it on its herald's look, whether or not
a page is open, so a limit holds with nobody watching. Each look it charges
the ledger what the agents spent since — a Claude session's requests as its
telemetry reported them, a Codex session's as its rollout counted them —
marks who is working, judges every agent against the person's ``[budget]``
(:func:`lup.sessions.limits.judged`) over the windows each account's reader
last read, and places or lifts the budget's holds, so an agent over a limit
waits at its next tool call and goes on once the limit allows.

The windows are read on a thread of their own every ``poll_seconds``, since
a read is a request to the provider; a Codex session's rollout carries its
account's windows with every token count, which keeps them current between
reads. What the page shows — each account's meter, each agent's rate, spend
and settings, the turtle — is :class:`BudgetView`, rebuilt each look and
handed to the stream.

An agent's priority and caps are the operator's, set live from the page or
the command line and kept in the ledger beside its spend. The turtle is a
key in the person's config, so a toggle outlives the dashboard.
"""

import json
import logging
import threading
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, ValidationError
from pydantic.alias_generators import to_camel

from lup.coordination import holds
from lup.coordination.bare import holds as bare_holds
from lup.coordination.bare import store
from lup.coordination.holds import Hold, HoldOwner, HoldReason, HoldScope
from lup.coordination.repository import RepositoryPeers
from lup.devtools.coordination.pausing import continued_with
from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.dashboard.live import Feature, RunningAgent
from lup.devtools.harness.launch import SwitchOutcome, switch_repository_login
from lup.launch.config_volume import LaunchedAccounts
from lup.launch.container import (
    VolumeLoginCopy,
    VolumeUnreachable,
    offered_login,
    state_volume_name,
)
from lup.execution.git import GitError
from lup.harness.image import Image
from lup.providers.harness import AdapterName
from lup.devtools.dashboard.telemetry import RequestSpend, TelemetryJoin
from lup.observability.usage.models import PacingWindow, UsageReader, UsageUnavailable
from lup.providers.accounts import (
    AccountHome,
    account_homes,
    account_reader,
    runtime_logins,
    transcript_spend,
)
from lup.providers.login import ProviderLogin
from lup.providers.login_sync import LoginPlace, bytes_at, identity_of
from lup.providers.user_config import UserConfigFile
from lup.sessions.budget import AgentLedger, Charge, LedgerState, SpendLedger
from lup.sessions.limits import (
    Account,
    AccountStanding,
    AgentCaps,
    AgentStanding,
    BudgetConfig,
    Cause,
    Limits,
    MeteredWindow,
    Priority,
    Spend,
    Verdict,
    clock,
    judged,
)
from lup.types import JsonObject

logger = logging.getLogger(__name__)


class AccountMeter(BaseModel, frozen=True):
    """One account as the page's meter shows it."""

    account: Account
    key: str
    home: str = ""
    signed_in: bool = True
    windows: list[MeteredWindow] = []
    read_at: datetime | None = None
    error: str = ""
    """Why the last reading failed, where it did; the windows are the last good ones."""

    limits: Limits = Limits()
    """The limits holding for it now, every layer applied."""

    said: list[str] = []
    """Those limits in words, each one set."""

    agents: int = 0
    """Running agents drawing on it."""

    held: int = 0
    """Of those, how many the budget holds."""

    exhausted: str = ""
    """What says a window of it is used up, and until when; empty while none is."""


class AgentMeter(BaseModel, frozen=True):
    """One running agent's spend and the operator's limits on it, by its stream key."""

    session: str
    account: str
    hour: Spend = Spend()
    """What it spent in the last hour: its rate."""

    total: Spend = Spend()
    priority: Priority = "normal"
    caps: AgentCaps = AgentCaps()
    exempt: bool = False
    """The operator's own session, which the budget never holds."""

    held: Verdict | None = None


class BudgetView(BaseModel, frozen=True):
    """Everything the page shows of the budget."""

    accounts: list[AccountMeter] = []
    agents: list[AgentMeter] = []
    turtle: bool = False
    telemetry: bool = False
    """Whether this dashboard receives sessions' telemetry, without which a
    Claude agent's spend reads as none."""

    refused: str = ""
    """Why the person's ``[budget]`` could not be read. Its limits are off
    meanwhile; a window used up still holds, since its provider refuses
    every request until it clears."""

    holds: bool = False
    """Whether this dashboard places the holds it judges; without a hold
    store to place them in, what it judges is shown and nothing waits."""


class HeldAgent(BaseModel, frozen=True):
    """One agent the budget holds in one repository's coordination store."""

    member: str
    cause: Cause
    said: str
    until: datetime | None = None


class WaitingCall(BaseModel, frozen=True):
    """One call a hook is holding: whose, and since when."""

    member: str
    since: datetime | None = None


class HoldDoor(ABC):
    """Where the budget's holds are placed, for every agent's hook to read."""

    @abstractmethod
    def holding(self, root: Path) -> list[HeldAgent]:
        """The budget's holds standing in the store at *root*."""

    @abstractmethod
    def place(self, root: Path, held: HeldAgent) -> None:
        """Hold one agent, replacing the budget's hold of it for the same cause."""

    @abstractmethod
    def lift(self, root: Path, held: HeldAgent) -> None:
        """Let one agent go from one of the budget's holds."""


class HeldCalls(ABC):
    """Which calls the hooks of one repository are holding now, whoever placed the hold."""

    @abstractmethod
    def waiting(self, root: Path) -> list[WaitingCall]:
        """Every call a hook in the store at *root* is holding now."""


def reason_of(cause: Cause) -> HoldReason:
    """The hold store's reason for a budget's cause, which its hook and row read."""
    match cause:
        case "window":
            return HoldReason.WINDOW
        case "reserve":
            return HoldReason.RESERVE
        case "cap":
            return HoldReason.CAP
        case "rate":
            return HoldReason.RATE
        case "slot":
            return HoldReason.SLOT


def cause_of(reason: HoldReason) -> Cause | None:
    """The budget's cause a hold's reason stands for; none for the operator's pause."""
    match reason:
        case HoldReason.WINDOW:
            return "window"
        case HoldReason.RESERVE:
            return "reserve"
        case HoldReason.CAP:
            return "cap"
        case HoldReason.RATE:
            return "rate"
        case HoldReason.SLOT:
            return "slot"
        case HoldReason.PAUSED:
            return None


class StoredHolds(HoldDoor):
    """The budget's holds in each repository's coordination store: one per agent, owned by the budget.

    Each covers the one conversation it names, since every subagent is judged
    on its own, and lapses at its ``until`` without anybody lifting it. One
    placed again keeps the moment it was first placed, so its row's "held
    since" holds still while its words change.
    """

    def holding(self, root: Path) -> list[HeldAgent]:
        return [
            HeldAgent(member=hold.member, cause=cause, said=hold.said, until=hold.until)
            for hold in holds.standing(root)
            if hold.owner is HoldOwner.BUDGET
            and hold.scope is HoldScope.SELF
            and (cause := cause_of(hold.reason)) is not None
        ]

    def place(self, root: Path, held: HeldAgent) -> None:
        reason = reason_of(held.cause)
        hold = Hold(
            scope=HoldScope.SELF,
            member=held.member,
            reason=reason,
            owner=HoldOwner.BUDGET,
            said=held.said,
            until=held.until,
        )
        first = next(
            (
                each.placed
                for each in holds.placed(root)
                if (each.owner, each.reason, each.scope, each.member)
                == (HoldOwner.BUDGET, reason, HoldScope.SELF, held.member)
            ),
            None,
        )
        holds.place(
            root, hold if first is None else hold.model_copy(update={"placed": first})
        )

    def lift(self, root: Path, held: HeldAgent) -> None:
        holds.lift(
            root, HoldOwner.BUDGET, reason_of(held.cause), HoldScope.SELF, held.member
        )


class StoredCalls(HeldCalls):
    """The calls each repository's hooks are holding, as the hold store's waiting markers say."""

    def waiting(self, root: Path) -> list[WaitingCall]:
        return [
            WaitingCall(member=call.member, since=call.since)
            for call in holds.held_calls(root)
        ]


class QuotaLimits(
    BaseModel,
    frozen=True,
    extra="ignore",
    alias_generator=to_camel,
    populate_by_name=True,
):
    """What a usage-limit refusal says of the limit it met."""

    resets_at: int | None = None
    rate_limit_type: str = ""


class TurnRecord(
    BaseModel,
    frozen=True,
    extra="ignore",
    alias_generator=to_camel,
    populate_by_name=True,
):
    """One record of a Claude Code transcript, as far as whether a usage limit ended its turn.

    Measured on Claude Code 2.1.283 and 2.1.285: a request refused at the
    limit records a synthetic assistant message with ``isApiErrorMessage``,
    ``error: "rate_limit"`` and ``quotaLimits`` naming the window and when it
    resets, and the turn ends there.
    """

    type: str = ""
    is_api_error_message: bool = False
    error: str = ""
    quota_limits: QuotaLimits | None = None


def limit_reset(transcript: Path, tail: int = 65536) -> datetime | None:
    """When the usage limit that ended a session's last turn resets, where one did; read off its transcript's end."""
    try:
        with transcript.open("rb") as handle:
            handle.seek(max(transcript.stat().st_size - tail, 0))
            lines = handle.read().decode("utf-8", "replace").splitlines()[1:]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = TurnRecord.model_validate_json(line)
        except ValidationError:
            continue
        if record.type not in ("user", "assistant"):
            continue
        quota = record.quota_limits
        if not (record.is_api_error_message and record.error == "rate_limit"):
            return None
        if quota is None or quota.resets_at is None:
            return None
        return datetime.fromtimestamp(quota.resets_at, UTC)
    return None


class Resumption(ABC):
    """Which sessions a limit stopped, and how one is told to go on."""

    @abstractmethod
    def stopped(self, each: "Placed") -> datetime | None:
        """When *each* may go on, where a limit ended its turn: nothing where none did, or something still holds it."""

    @abstractmethod
    def resume(self, each: "Placed") -> bool:
        """Wake *each* with a bare "continue", recorded as the prompt it is; whether it reached."""


class StoredResumption(Resumption):
    """A session stops at a limit two ways: its hook refused a call held past the hold's limit, or it ran into the provider's own.

    Either ended its turn, and it waits to be told to go on; one still held
    by anything -- the operator's pause among them -- is left to what holds it.
    """

    def stopped(self, each: "Placed") -> datetime | None:
        if holds.holding(each.store, each.agent.id, each.agent.parent):
            return None
        refused = {
            store.text(call.get("member")) for call in bare_holds.refused(each.store)
        }
        if each.agent.id in refused:
            return datetime.min.replace(tzinfo=UTC)
        transcript = each.agent.transcript
        return limit_reset(Path(transcript)) if transcript else None

    def resume(self, each: "Placed") -> bool:
        peers = RepositoryPeers(each.checkout)
        row = peers.row(each.agent.id)
        return row is not None and continued_with(peers, row, "continue")


class WindowReading(BaseModel, frozen=True):
    at: datetime
    windows: list[PacingWindow]


class AccountWatch:
    """One account's windows as read, kept long enough to say how fast each fills.

    ``rate_span`` is how far back a window's rate is read, and ``rate_least``
    the shortest stretch it is read over at all.
    """

    def __init__(
        self,
        home: AccountHome,
        reader: Callable[[AccountHome], UsageReader] = account_reader,
        rate_span: timedelta = timedelta(hours=1),
        rate_least: timedelta = timedelta(minutes=10),
    ) -> None:
        self.home = home
        self.reader = reader
        self.rate_span = rate_span
        self.rate_least = rate_least
        self.readings: deque[WindowReading] = deque()
        self.error = ""
        self.read_at: datetime | None = None
        self.lock = threading.Lock()

    def read(self, now: datetime) -> None:
        """Read the windows from the provider; a failure keeps the last ones and says why."""
        if not self.home.signed_in:
            self.error = "not signed in"
            return
        try:
            report = self.reader(self.home).read(detail=False)
        except UsageUnavailable as unreadable:
            self.error = str(unreadable)
            return
        self.observed(report.windows, report.read_at or now)
        self.error = report.stale

    def fresh(self, now: datetime, within: timedelta) -> bool:
        """Whether a reading from anywhere -- a session's status line, a rollout, the provider -- is that recent."""
        return self.read_at is not None and now - self.read_at < within

    def observed(self, windows: list[PacingWindow], now: datetime) -> None:
        """Take a reading from anywhere that reports one: the provider, a rollout, a status line.

        One older than the latest is kept out, so a reading another reader
        took a while ago never stands over a fresher one.
        """
        if not windows or (self.read_at is not None and now <= self.read_at):
            return
        with self.lock:
            self.readings.append(WindowReading(at=now, windows=windows))
            while self.readings and now - self.readings[0].at > self.rate_span * 2:
                self.readings.popleft()
            self.error = ""
            self.read_at = now

    def per_hour(self, window: PacingWindow, now: datetime) -> float | None:
        """How many percent of *window* an hour it filled at, over the last span of readings of it."""
        same = [
            (reading.at, each.utilization_pct)
            for reading in self.readings
            for each in reading.windows
            if each.label == window.label
            and abs((each.resets_at - window.resets_at).total_seconds()) < 120
            and now - reading.at <= self.rate_span
        ]
        if not same:
            return None
        at, used = same[0]
        hours = (now - at).total_seconds() / 3600
        if now - at < self.rate_least or hours <= 0:
            return None
        return max(window.utilization_pct - used, 0.0) / hours

    def standing(self, now: datetime) -> AccountStanding:
        """The account as last read, each window with how fast it fills."""
        with self.lock:
            last = self.readings[-1].windows if self.readings else []
            return AccountStanding(
                account=self.home.account,
                windows=[
                    MeteredWindow(window=each, per_hour=self.per_hour(each, now))
                    for each in last
                ],
                read_at=self.read_at,
                error=self.error,
            )


type VolumeCopy = Callable[[Path, ProviderLogin], LoginPlace]
"""The copy of a runtime's login a checkout's repository volume keeps."""


def volume_copy(checkout: Path, login: ProviderLogin) -> LoginPlace:
    """The volume's copy, reached through a helper container from the image a session there runs."""
    volume = state_volume_name(checkout, login)
    return VolumeLoginCopy(checkout, login, Image().config_home, offered_login(volume))


def volume_accounts(
    checkouts: list[Path], copy: VolumeCopy = volume_copy
) -> list[AccountHome]:
    """Whose login each served repository's volume holds now, read from the volume itself.

    What every contained session of that repository and runtime draws on:
    whoever signed in last inside it, which lup's record of what it handed
    the volume stops saying once someone signs in to another account from a
    session. Named as the account names itself, its *home* the checkout. A
    volume no helper container reaches, or whose login says no account, is
    left out.
    """

    def held(checkout: Path, runtime: str, login: ProviderLogin) -> AccountHome | None:
        try:
            volume = state_volume_name(checkout, login)
            read = copy(checkout, login).read()
        except (GitError, OSError, VolumeUnreachable):
            return None
        who = identity_of(login, read.account)
        if read.login is None or who is None:
            return None
        return AccountHome(
            account=Account(runtime=runtime, profile=who.name or who.id),
            home=checkout,
            signed_in=True,
            identity=who.id,
            volume=volume,
        )

    return [
        found
        for checkout in checkouts
        for each in runtime_logins()
        if each.login.state_volume and each.login.account_id
        for found in [held(checkout, each.runtime, each.login)]
        if found is not None
    ]


def identified(home: AccountHome) -> AccountHome:
    """A home's account with whose login it keeps, as the account document beside it says.

    The home no profile selects is named as its account names itself, since
    "default" says which home and not who.
    """
    login = next(
        (
            each.login
            for each in runtime_logins()
            if each.runtime == home.account.runtime
        ),
        None,
    )
    if login is None or not login.account_id:
        return home
    who = identity_of(login, bytes_at(login.account_document(home.home)))
    if who is None:
        return home
    named = (
        home.account.model_copy(update={"profile": who.name or who.id})
        if home.account.profile == "default"
        else home.account
    )
    return home.model_copy(update={"identity": who.id, "account": named})


def held_homes(
    checkouts: list[Path], copy: VolumeCopy = volume_copy
) -> list[AccountHome]:
    """Every account the served repositories' sessions can draw on, each once, by who it is.

    Each volume's account first, read with the volume's own login, which its
    contained sessions keep renewed; then every profile and the home no
    profile selects, leaving out one whose account a volume already reads.
    """
    volumes = volume_accounts(checkouts, copy)
    seen = [each.identity for each in volumes]
    homes = [identified(each) for each in account_homes(checkouts)]
    return [
        *volumes,
        *(each for each in homes if not each.identity or each.identity not in seen),
    ]


def drawn_reader(account: AccountHome, copy: VolumeCopy = volume_copy) -> UsageReader:
    """The reader of one account's windows, asked with the login a volume keeps where it is kept in one.

    The volume's login is read through a helper container only when the
    provider is asked, which the reading every reader of the account shares
    keeps rare.
    """
    login = next(
        (
            each.login
            for each in runtime_logins()
            if each.runtime == account.account.runtime
        ),
        None,
    )
    if not account.volume or login is None:
        return account_reader(account)

    def stored() -> bytes | None:
        try:
            return copy(account.home, login).read().login
        except VolumeUnreachable as unreachable:
            raise UsageUnavailable(str(unreachable)) from unreachable

    return account_reader(account, stored)


class AccountPoller:
    """Read every account's windows on a thread of its own, every ``poll_seconds`` the person's config names."""

    def __init__(
        self,
        checkouts: Callable[[], list[Path]],
        ledger: SpendLedger,
        config: UserConfigFile,
        homes: Callable[[list[Path]], list[AccountHome]] = held_homes,
        reader: Callable[[AccountHome], UsageReader] = drawn_reader,
    ) -> None:
        self.checkouts = checkouts
        self.ledger = ledger
        self.config = config
        self.homes = homes
        self.reader = reader
        self.watches: dict[str, AccountWatch] = {}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(
            target=self.polling, name="lup-budget-accounts", daemon=True
        )
        self.failed = ""
        """Why the last pass over the accounts failed, where it did."""

    def interval(self, now: datetime | None = None) -> float:
        """How long between reads: ``close_seconds`` once a window is within ten points of its ceiling, else ``poll_seconds``."""
        try:
            config = self.config.load().budget
        except ValueError:
            config = BudgetConfig()
        moment = now or datetime.now(UTC)
        close = any(
            each.window.utilization_pct
            >= (config.limits(watch.home.account, moment).window_ceiling or 100.0) - 10
            for watch in self.standings(moment)
            for each in watch.standing(moment).windows
        )
        return float(config.close_seconds if close else config.poll_seconds)

    def poll(self, now: datetime | None = None) -> None:
        """Read every account once, and publish what was read where every judgement reads it."""
        moment = now or datetime.now(UTC)
        found = self.homes(self.checkouts())
        with self.lock:
            self.watches = {
                each.account.key: (
                    self.watches[each.account.key]
                    if each.account.key in self.watches
                    and self.watches[each.account.key].home == each
                    else AccountWatch(each, self.reader)
                )
                for each in found
            }
            watches = list(self.watches.values())
        within = timedelta(seconds=self.interval(moment))
        for watch in watches:
            if not watch.fresh(moment, within):
                watch.read(moment)
        self.ledger.published([watch.standing(moment) for watch in watches])

    def polling(self) -> None:
        while not self.stopping.is_set():
            try:
                self.poll()
                self.failed = ""
            except Exception as failed:
                logger.exception("the budget could not read every account's windows")
                self.failed = f"its last pass over the accounts failed: {failed}"
            self.stopping.wait(self.interval())

    def start(self) -> "AccountPoller":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()

    def observed(
        self, account: str, windows: list[PacingWindow], now: datetime
    ) -> None:
        """Take a reading of one account that a transcript reported."""
        with self.lock:
            watch = self.watches[account] if account in self.watches else None
        if watch is not None:
            watch.observed(windows, now)

    def standings(self, now: datetime) -> list[AccountWatch]:
        """Every account being read, as its watch keeps it."""
        del now
        with self.lock:
            return list(self.watches.values())

    def account_at(self, home: Path) -> Account | None:
        """The account whose login is kept in *home*, where one being read is."""
        with self.lock:
            return next(
                (
                    watch.home.account
                    for watch in self.watches.values()
                    if not watch.home.volume
                    and watch.home.home.resolve() == home.resolve()
                ),
                None,
            )

    def account_of_volume(self, volume: str) -> Account | None:
        """The account signed in to a repository volume, where one being read is."""
        with self.lock:
            return next(
                (
                    watch.home.account
                    for watch in self.watches.values()
                    if watch.home.volume == volume
                ),
                None,
            )


class RolloutLine(BaseModel, frozen=True):
    """One whole line of a rollout, and the byte it starts at."""

    at: int
    record: JsonObject


class RolloutTail:
    """One Codex rollout, read from where the last look stopped."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0

    def lines(self) -> list[RolloutLine]:
        """Every whole line appended since the last look."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return []

        def appended() -> Iterator[RolloutLine]:
            with self.path.open("rb") as rollout:
                rollout.seek(self.offset)
                for line in rollout:
                    if not line.endswith(b"\n"):
                        return
                    at = self.offset
                    self.offset += len(line)
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        yield RolloutLine(at=at, record=record)

        return list(appended())


type AccountOf = Callable[[KnownRepository, RunningAgent], Account]
"""Which account a running session draws on; each of its subagents draws on its."""


def unrecorded(known: KnownRepository, agent: RunningAgent) -> Account:
    """The home no profile selects, in the agent's runtime: the account where nothing records another."""
    del known
    return Account(runtime=agent.runtime)


def launched_on(
    poller: AccountPoller, accounts: LaunchedAccounts | None = None
) -> AccountOf:
    """Which account a session draws on: who its repository's volume holds, else what its launch recorded.

    A contained session -- and one whose launch recorded nothing, opened
    before launches recorded their account -- draws on whoever is signed in
    to its repository's volume of its runtime now, as the poller reads it,
    whatever lup first handed the volume. A host session draws on the home
    its launch recorded, read by that home; one with no record at all, on
    the home no profile selects.
    """

    def drawn(known: KnownRepository, agent: RunningAgent) -> Account:
        launched = (accounts or LaunchedAccounts()).launched(agent.id)
        login = next(
            (each.login for each in runtime_logins() if each.runtime == agent.runtime),
            None,
        )
        contained = launched is None or launched.contained
        if login is not None and login.state_volume and contained:
            try:
                volume = state_volume_name(known.checkout, login)
            except (GitError, OSError):
                volume = ""
            held = poller.account_of_volume(volume) if volume else None
            if held is not None:
                return held
        if launched is None:
            return unrecorded(known, agent)
        owner = launched.owner
        return poller.account_at(owner.home) or Account(
            runtime=agent.runtime, profile=owner.profile or "default"
        )

    return drawn


class RepositoryAgents(BaseModel, frozen=True):
    """One served repository's running agents, and the coordination store their holds go in."""

    key: str
    known: KnownRepository
    store: Path
    agents: list[RunningAgent] = []


class Placed(BaseModel, frozen=True):
    """One running agent this look found: where its row is, and the account it draws on."""

    key: str
    repository: str
    checkout: Path
    store: Path
    agent: RunningAgent
    account: Account
    exempt: bool
    wanting: datetime | None = None
    """Since when it has wanted to work, where it does."""


class Warned(BaseModel, frozen=True):
    """One used-up window the operator was told of: whose, which, and the reset it waits for."""

    account: str
    window: str
    resets_at: datetime


class Told(BaseModel, frozen=True):
    """One agent the operator was told its spend cap stopped, and when."""

    key: str
    at: datetime


class Remembered(BaseModel, frozen=True):
    """What the governor carries of one agent from one look to the next."""

    since: datetime | None = None
    """Since when it has wanted to work, where it does."""

    marked: float | None = None
    """The working mark last written to the ledger for it."""

    told: datetime | None = None
    """When the operator was told its spend cap stopped it, while it does."""

    account: Account | None = None
    resumed: datetime | None = None
    """When it was last told to continue after a limit stopped it."""


def epoch_of(moment: datetime | None) -> float | None:
    """A moment in seconds since the epoch, or nothing for no moment."""
    return moment.timestamp() if moment is not None else None


def notified_nowhere(summary: str, body: str) -> bool:
    del summary, body
    return False


class BudgetGovernor:
    """Charge, judge and hold, once a look; and keep what the page shows of it.

    ``stick`` is how long an agent that stopped calling keeps the slot it
    worked in, so one thinking between two calls is not overtaken by every
    agent at the door; ``unplaced`` how long spend from a session no roster
    lists yet is kept for one to. ``door`` is where holds are placed and
    ``calls`` says which calls the hooks hold now; without a door the
    governor judges and shows, and nothing waits. ``resumption`` finds the
    sessions a limit stopped and tells each to continue once nothing holds
    it, no more often than ``rewake``; ``far`` is how distant a used-up
    window's reset must be for the operator to be told of it.
    """

    def __init__(
        self,
        ledger: SpendLedger,
        config: UserConfigFile,
        door: HoldDoor | None = None,
        poller: AccountPoller | None = None,
        join: TelemetryJoin | None = None,
        calls: HeldCalls | None = None,
        account_of: AccountOf = unrecorded,
        notify: Callable[[str, str], bool] = notified_nowhere,
        stick: timedelta = timedelta(seconds=90),
        unplaced: timedelta = timedelta(minutes=10),
        resumption: Resumption | None = None,
        rewake: timedelta = timedelta(minutes=10),
        far: timedelta = timedelta(hours=6),
    ) -> None:
        self.ledger = ledger
        self.config = config
        self.door = door
        self.poller = poller
        self.join = join
        self.calls = calls
        self.account_of = account_of
        self.notify = notify
        self.stick = stick
        self.unplaced_for = unplaced
        self.resumption = resumption
        self.rewake = rewake
        self.far = far
        self.warned_of: list[Warned] = []
        self.remembered: dict[str, Remembered] = {}
        self.tails: dict[Path, RolloutTail] = {}
        self.unplaced: list[RequestSpend] = []
        self.view = BudgetView(telemetry=join is not None, holds=door is not None)
        self.lock = threading.Lock()

    def current(self) -> BudgetView:
        """What the page shows of the budget, as of the last look."""
        with self.lock:
            return self.view

    def placed(self, repositories: list[RepositoryAgents]) -> list[Placed]:
        """Every running agent of every repository, each on the account its session draws on."""

        def of(repository: RepositoryAgents) -> list[Placed]:
            rows = {agent.id: agent for agent in repository.agents}

            def account(agent: RunningAgent) -> Account:
                session = rows[agent.parent] if agent.parent in rows else agent
                return self.account_of(repository.known, session)

            return [
                Placed(
                    key=f"{repository.key}/{agent.id}",
                    repository=repository.key,
                    checkout=repository.known.checkout,
                    store=repository.store,
                    agent=agent,
                    account=account(agent),
                    exempt=not agent.parent and not agent.spawned_by,
                )
                for agent in repository.agents
            ]

        return [each for repository in repositories for each in of(repository)]

    def memory(self, key: str) -> Remembered:
        return self.remembered[key] if key in self.remembered else Remembered()

    def wanting(
        self, each: Placed, moment: datetime, waiting: list[WaitingCall]
    ) -> datetime | None:
        """Since when *each* has wanted to work, where it does: at a call, or between two still.

        A call its hook holds wants from when the hook began holding it, so a
        queue keeps its order however long it waits.
        """
        agent = each.agent
        kept = self.memory(each.key).since
        held = next(
            (call.since for call in waiting if call.member == agent.id and call.since),
            None,
        )
        if agent.calling:
            return held or kept or agent.at or moment
        if kept is not None and agent.at is not None and moment - agent.at < self.stick:
            return kept
        return None

    def telemetry_charges(self, agents: list[Placed], moment: datetime) -> list[Charge]:
        """What sessions' telemetry reported since, charged to the agent that spent it."""
        if self.join is None:
            return []
        sessions = {
            answer: each
            for each in agents
            if not each.agent.parent
            for answer in each.agent.answers
        }
        reported = [*self.unplaced, *self.join.settled(moment.timestamp())]
        self.unplaced = [
            spend
            for spend in reported
            if spend.session not in sessions
            and moment.timestamp() - spend.at < self.unplaced_for.total_seconds()
        ]
        return [
            Charge(
                agent=(
                    f"{session.repository}/{store.subagent_id(session.agent.id, spend.agent)}"
                    if spend.agent
                    else session.key
                ),
                account=session.account,
                at=spend.at,
                usd=spend.usd,
                tokens=spend.tokens,
                request=spend.request,
            )
            for spend in reported
            if spend.session in sessions
            for session in [sessions[spend.session]]
        ]

    def heard_windows(self, agents: list[Placed]) -> None:
        """Take the windows each session's status line handed as a reading of the account it draws on."""
        join, poller = self.join, self.poller
        if join is None or poller is None:
            return
        drawing = {
            answer: each.account
            for each in agents
            if not each.agent.parent
            for answer in each.agent.answers
        }
        for reading in join.windows():
            if reading.session in drawing:
                poller.observed(
                    drawing[reading.session].key,
                    reading.windows,
                    datetime.fromtimestamp(reading.at, UTC),
                )

    def rollout_charges(self, agents: list[Placed], moment: datetime) -> list[Charge]:
        """What Codex agents' rollouts counted since, charged; the windows they carry, observed."""
        followed = {
            Path(each.agent.transcript): each
            for each in agents
            if each.agent.runtime == "codex" and each.agent.transcript
        }
        self.tails = {
            path: self.tails[path] if path in self.tails else RolloutTail(path)
            for path in followed
        }

        def counted(path: Path, each: Placed) -> Iterator[Charge]:
            for line in self.tails[path].lines():
                spent = transcript_spend("codex", line.record)
                if spent is None:
                    continue
                stamp = line.record["timestamp"] if "timestamp" in line.record else None
                try:
                    at = datetime.fromisoformat(str(stamp)) if stamp else moment
                except ValueError:
                    at = moment
                if spent.windows and self.poller is not None:
                    self.poller.observed(each.account.key, spent.windows, at)
                if spent.tokens:
                    yield Charge(
                        agent=each.key,
                        account=each.account,
                        at=at.timestamp(),
                        tokens=spent.tokens,
                        request=f"{path}@{line.at}",
                    )

        return [
            charge for path, each in followed.items() for charge in counted(path, each)
        ]

    def marks(self, agents: list[Placed]) -> list[AgentLedger]:
        """The working marks that changed since the last look, an agent gone among them."""
        here = {each.key for each in agents}
        moved = [
            AgentLedger(
                agent=each.key, account=each.account, working=epoch_of(each.wanting)
            )
            for each in agents
            if epoch_of(each.wanting) != self.memory(each.key).marked
        ]
        gone = [
            AgentLedger(agent=key, account=memory.account, working=None)
            for key, memory in self.remembered.items()
            if key not in here
            and memory.marked is not None
            and memory.account is not None
        ]
        return [*moved, *gone]

    def look(
        self, repositories: list[RepositoryAgents], moment: datetime
    ) -> BudgetView:
        """Charge what was spent since, judge every agent, hold and let go; and say what the page shows."""
        try:
            config = self.config.load().budget
            refused = ""
        except ValueError as unreadable:
            config = BudgetConfig()
            refused = str(unreadable)
        calls = self.calls
        found = self.placed(repositories)
        waiting = [
            call
            for root in {each.store for each in found}
            for call in (calls.waiting(root) if calls is not None else [])
        ]
        agents = [
            each.model_copy(update={"wanting": self.wanting(each, moment, waiting)})
            for each in found
        ]
        epoch = moment.timestamp()
        self.heard_windows(agents)
        charges = [
            *self.telemetry_charges(agents, moment),
            *self.rollout_charges(agents, moment),
        ]
        if charges:
            self.ledger.charge(charges)
        changed = self.marks(agents)
        state = self.ledger.working(changed, epoch) if changed else self.ledger.read()
        accounts = (
            [watch.standing(moment) for watch in self.poller.standings(moment)]
            if self.poller is not None
            else state.accounts
        )
        verdicts = judged(
            accounts, self.standings(agents, state, epoch), config, moment
        )
        self.hold(agents, verdicts)
        told = self.tell(agents, verdicts, moment)
        woken = self.resumed(agents, verdicts, moment)
        self.warned(config, accounts, moment)
        self.remembered = {
            each.key: Remembered(
                since=each.wanting,
                marked=epoch_of(each.wanting),
                told=next(
                    (each_told.at for each_told in told if each_told.key == each.key),
                    None,
                ),
                account=each.account,
                resumed=moment if each.key in woken else self.memory(each.key).resumed,
            )
            for each in agents
        }
        view = self.viewed(config, refused, agents, accounts, verdicts, state, moment)
        with self.lock:
            self.view = view
        return view

    def standings(
        self,
        agents: list[Placed],
        state: LedgerState,
        epoch: float,
    ) -> list[AgentStanding]:
        """Every agent as the judgement reads it: the launched ones, and the in-process ones working now."""
        launched = [
            (state.line(each.key) or AgentLedger(agent=each.key, account=each.account))
            .model_copy(
                update={
                    "account": each.account,
                    "working": epoch_of(each.wanting),
                }
            )
            .standing(epoch, self.ledger.span, exempt=each.exempt)
            for each in agents
        ]
        keys = {each.key for each in agents}
        inside = [
            line.standing(epoch, self.ledger.span)
            for line in state.agents
            if line.agent not in keys and line.working is not None
        ]
        return [*launched, *inside]

    def hold(self, agents: list[Placed], verdicts: list[Verdict]) -> None:
        """Place each verdict's hold on its agent's row, and lift the budget's holds no verdict stands behind."""
        door = self.door
        if door is None:
            return
        here = {each.key: each for each in agents}
        for root in {each.store for each in agents}:
            meant = [
                HeldAgent(
                    member=here[verdict.key].agent.id,
                    cause=verdict.cause,
                    said=verdict.said,
                    until=verdict.until,
                )
                for verdict in verdicts
                if verdict.key in here and here[verdict.key].store == root
            ]
            standing = door.holding(root)
            for held in meant:
                if held not in standing:
                    door.place(root, held)
            for held in standing:
                if not any(
                    (each.member, each.cause) == (held.member, held.cause)
                    for each in meant
                ):
                    door.lift(root, held)

    def tell(
        self, agents: list[Placed], verdicts: list[Verdict], moment: datetime
    ) -> list[Told]:
        """Tell the operator once of each agent its spend cap stopped; say which it has told of."""
        here = {each.key for each in agents}
        capped = {
            verdict.key: verdict
            for verdict in verdicts
            if verdict.cause == "cap" and verdict.key in here
        }
        for key, verdict in capped.items():
            if self.memory(key).told is None:
                self.notify(f"An agent reached its spend cap: {key}", verdict.said)
        return [Told(key=key, at=self.memory(key).told or moment) for key in capped]

    def resumed(
        self, agents: list[Placed], verdicts: list[Verdict], moment: datetime
    ) -> list[str]:
        """Tell each session a limit stopped to continue, once nothing holds it; the keys told.

        A session's own, never a subagent's, which ends with its turn; and
        never the operator's own session, whose runtime waits for the reset.
        """
        resumption = self.resumption
        if resumption is None:
            return []
        held = {verdict.key for verdict in verdicts}

        def due(each: Placed) -> bool:
            last = self.memory(each.key).resumed
            if (
                each.exempt
                or each.agent.parent
                or each.agent.calling
                or each.key in held
            ):
                return False
            if last is not None and moment - last < self.rewake:
                return False
            clears = resumption.stopped(each)
            return clears is not None and clears <= moment

        return [each.key for each in agents if due(each) and resumption.resume(each)]

    def warned(
        self, config: BudgetConfig, accounts: list[AccountStanding], moment: datetime
    ) -> None:
        """Tell the operator once of each window used up for long: switching profile is theirs, never done for them."""
        for standing in accounts:
            limits = config.limits(standing.account, moment)
            for each in standing.windows:
                window = each.window
                told = Warned(
                    account=standing.account.key,
                    window=window.label,
                    resets_at=window.resets_at.replace(second=0, microsecond=0),
                )
                if (
                    told in self.warned_of
                    or not limits.used_up(window)
                    or window.resets_at - moment < self.far
                ):
                    continue
                self.warned_of.append(told)
                self.notify(
                    f"{standing.account.key}: {window.label} window used up until "
                    f"{clock(window.resets_at, moment)}",
                    "Its agents wait at their next tool call until it clears. Moving "
                    "them to another profile is yours: :switch <profile> on the "
                    "dashboard, or harness profile switch.",
                )

    def viewed(
        self,
        config: BudgetConfig,
        refused: str,
        agents: list[Placed],
        accounts: list[AccountStanding],
        verdicts: list[Verdict],
        state: LedgerState,
        moment: datetime,
    ) -> BudgetView:
        """What the page shows: every account's meter, every running agent's spend and settings."""
        held = {verdict.key: verdict for verdict in verdicts}
        homes = {
            watch.home.account.key: watch.home
            for watch in (self.poller.standings(moment) if self.poller else [])
        }
        epoch = moment.timestamp()

        def meter(standing: AccountStanding) -> AccountMeter:
            key = standing.account.key
            drawing = [each for each in agents if each.account.key == key]
            limits = config.limits(standing.account, moment)
            used = next(
                (
                    each.window
                    for each in standing.windows
                    if limits.used_up(each.window) and each.window.resets_at > moment
                ),
                None,
            )
            return AccountMeter(
                account=standing.account,
                key=key,
                home=str(homes[key].home) if key in homes else "",
                signed_in=homes[key].signed_in if key in homes else True,
                windows=standing.windows,
                read_at=standing.read_at,
                error=standing.error,
                limits=limits,
                said=limits.said(),
                agents=len(drawing),
                held=sum(1 for each in drawing if each.key in held),
                exhausted=(
                    f"{used.label} window used up until {clock(used.resets_at, moment)}"
                    if used is not None
                    else ""
                ),
            )

        def agent(each: Placed) -> AgentMeter:
            line = state.line(each.key) or AgentLedger(
                agent=each.key, account=each.account
            )
            return AgentMeter(
                session=each.key,
                account=each.account.key,
                hour=line.hour(epoch, self.ledger.span),
                total=line.total,
                priority=line.priority,
                caps=line.caps,
                exempt=each.exempt,
                held=held[each.key] if each.key in held else None,
            )

        return BudgetView(
            accounts=[meter(each) for each in accounts],
            agents=[agent(each) for each in agents],
            turtle=config.turtle.on,
            telemetry=self.join is not None,
            refused=refused,
            holds=self.door is not None,
        )

    def settle(
        self, key: str, priority: Priority | None, caps: AgentCaps | None
    ) -> AgentMeter:
        """Set one running agent's priority or caps, as the operator chose them."""
        account = self.memory(key).account
        if account is None:
            raise LookupError(f"no running agent has the key {key!r}")
        line = self.ledger.settle(
            key, account, datetime.now(UTC).timestamp(), priority, caps
        )
        shown = next(
            (each for each in self.current().agents if each.session == key), None
        )
        return AgentMeter(
            session=key,
            account=line.account.key,
            hour=shown.hour if shown is not None else Spend(),
            total=line.total,
            priority=line.priority,
            caps=line.caps,
            exempt=shown.exempt if shown is not None else False,
            held=shown.held if shown is not None else None,
        )

    def turtle(self, on: bool) -> bool:
        """Put the turtle's limits in place, or take them away, in the person's config."""
        self.config.record({("budget", "turtle", "on"): on})
        return self.config.load().budget.turtle.on


class TurtleRequest(BaseModel, frozen=True, extra="forbid"):
    on: bool


class TurtleState(BaseModel, frozen=True):
    on: bool


class AgentBudgetRequest(BaseModel, frozen=True, extra="forbid"):
    """The operator's limits on one agent; a field left out stays as it was."""

    priority: Priority | None = None
    caps: AgentCaps | None = None
    """Every cap at once: a cap left out of it is cleared."""


class SwitchRequest(BaseModel, frozen=True, extra="forbid"):
    """Move a repository's sessions of one runtime onto a profile."""

    profile: str = Field(min_length=1)
    runtime: AdapterName = AdapterName.CLAUDE


class SwitchReply(BaseModel, frozen=True):
    """What moving a repository's sessions came to, and the lines that say it."""

    outcome: SwitchOutcome
    said: list[str]


def budget_models() -> list[type[BaseModel]]:
    """Every model the budget's routes and stream frames hand the page."""
    return [
        BudgetView,
        TurtleRequest,
        TurtleState,
        AgentBudgetRequest,
        AgentMeter,
        SwitchRequest,
        SwitchReply,
    ]


def profile_routes(
    app: FastAPI,
    serves: Callable[[tuple[Feature, ...]], None],
    served_repositories: Callable[[], list[KnownRepository]],
    served: tuple[Feature, ...] = ("profiles",),
) -> None:
    """Serve the one action that moves a repository's sessions onto another profile.

    Contained sessions of a runtime that rereads its login move at their next
    request; every other session is answered with the command that opens it
    again on that profile, and why.
    """
    serves(served)

    @app.post("/api/repositories/{repository}/profile")
    def switch(repository: str, asked: SwitchRequest) -> SwitchReply:
        """Hand the repository's volume the profile's login, saying what each session does."""
        known = next(
            (each for each in served_repositories() if each.key() == repository), None
        )
        if known is None:
            raise HTTPException(status_code=404, detail="No repository has that key")
        try:
            outcome = switch_repository_login(
                known.checkout, asked.runtime, asked.profile
            )
        except KeyError as unknown:
            raise HTTPException(status_code=404, detail=str(unknown)) from unknown
        except ValueError as refused:
            raise HTTPException(status_code=409, detail=str(refused)) from refused
        return SwitchReply(outcome=outcome, said=outcome.lines())


def budget_routes(
    app: FastAPI,
    serves: Callable[[tuple[Feature, ...]], None],
    governor: BudgetGovernor,
    served: tuple[Feature, ...] = ("budgets",),
) -> None:
    """Serve the budget's writes on *app*: the turtle, and one agent's limits.

    *served* is what these routes serve, which the stream tells the page.
    """
    serves(served)

    @app.post("/api/budget/turtle")
    def turtle(asked: TurtleRequest) -> TurtleState:
        """Turn the turtle's slower limits on or off, for every account."""
        try:
            return TurtleState(on=governor.turtle(asked.on))
        except ValueError as refused:
            raise HTTPException(status_code=409, detail=str(refused)) from refused

    @app.post("/api/repositories/{repository}/sessions/{member}/budget")
    def settle(repository: str, member: str, asked: AgentBudgetRequest) -> AgentMeter:
        """Set one running agent's priority, or its caps."""
        try:
            return governor.settle(f"{repository}/{member}", asked.priority, asked.caps)
        except LookupError as missing:
            raise HTTPException(status_code=404, detail=str(missing)) from missing
