"""The budget governor: what it charges, whom it holds and lets go, and what the page is shown."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lup.coordination import holds
from lup.coordination.holds import HoldOwner, HoldReason
from lup.coordination.identity import mint_member_id
from lup.coordination.repository import RepositoryPeers
from lup.devtools.dashboard import budget as budget_module
from lup.devtools.dashboard.budget import (
    AccountPoller,
    AccountWatch,
    BudgetGovernor,
    HeldAgent,
    HeldCalls,
    HoldDoor,
    Placed,
    RepositoryAgents,
    Resumption,
    StoredCalls,
    StoredHolds,
    WaitingCall,
    drawn_reader,
    held_homes,
    launched_on,
    limit_reset,
    profile_routes,
)
from lup.devtools.harness.launch import SwitchOutcome
from lup.launch.config_volume import LaunchedAccount, LaunchedAccounts, LoginOwner
from lup.providers.claude.usage import reader as claude_reader
from lup.providers.claude.usage.api import UsageResponse
from lup.providers.harness import AdapterName
from lup.providers.login import ProviderLogin
from lup.providers.login_sync import LoginCopy, LoginPlace
from lup.devtools.dashboard.companion import KnownRepository
from lup.devtools.dashboard.live import RunningAgent
from lup.devtools.dashboard.telemetry import (
    HandedWindows,
    RequestAgent,
    RequestSpend,
    TelemetryJoin,
)
from lup.observability.usage.models import (
    PacingWindow,
    UsageReader,
    UsageReport,
    UsageUnavailable,
)
from lup.providers.accounts import AccountHome
from lup.providers.user_config import UserConfigFile
from lup.sessions.budget import Charge, SpendLedger
from lup.sessions.limits import (
    Account,
    AccountStanding,
    AgentCaps,
    MeteredWindow,
    Spend,
    clock,
)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CLAUDE = Account(runtime="claude")


class Door(HoldDoor):
    """A hold store in memory: what the governor placed and lifted, per store."""

    def __init__(self) -> None:
        self.held: dict[Path, list[HeldAgent]] = {}
        self.lifted: list[HeldAgent] = []

    def holding(self, root: Path) -> list[HeldAgent]:
        return list(self.held[root]) if root in self.held else []

    def place(self, root: Path, held: HeldAgent) -> None:
        kept = [
            each
            for each in self.holding(root)
            if (each.member, each.cause) != (held.member, held.cause)
        ]
        self.held[root] = [*kept, held]

    def lift(self, root: Path, held: HeldAgent) -> None:
        self.held[root] = [each for each in self.holding(root) if each != held]
        self.lifted.append(held)


class Calls(HeldCalls):
    """The calls a hook holds, as the test says they are."""

    def __init__(self, waiting: list[WaitingCall]) -> None:
        self.calls = waiting

    def waiting(self, root: Path) -> list[WaitingCall]:
        del root
        return self.calls


def repository(tmp_path: Path, *agents: RunningAgent) -> RepositoryAgents:
    return RepositoryAgents(
        key="repo",
        known=KnownRepository(repository=tmp_path / ".git", checkout=tmp_path),
        store=tmp_path / "store",
        agents=list(agents),
    )


def session(
    member: str, calling: str = "", at: datetime | None = NOW, **fields: object
) -> RunningAgent:
    return RunningAgent.model_validate(
        {
            "id": member,
            "runtime": "claude",
            "answers": [f"conversation-{member}"],
            "calling": calling,
            "at": at,
            "spawned_by": "operator-session",
            **fields,
        }
    )


def config(tmp_path: Path, budget: str = "") -> UserConfigFile:
    (tmp_path / "config.toml").write_text(budget)
    return UserConfigFile(tmp_path)


def governor(
    tmp_path: Path,
    door: Door,
    budget: str = "",
    join: TelemetryJoin | None = None,
    calls: Calls | None = None,
) -> BudgetGovernor:
    return BudgetGovernor(
        SpendLedger(tmp_path / "ledger.json"),
        config(tmp_path, budget),
        door,
        join=join,
        calls=calls,
    )


def window(used: float, hours_left: float = 2.0) -> AccountStanding:
    return AccountStanding(
        account=CLAUDE,
        windows=[
            MeteredWindow(
                window=PacingWindow(
                    label="5-hour",
                    utilization_pct=used,
                    resets_at=NOW + timedelta(hours=hours_left),
                    window_hours=5,
                )
            )
        ],
    )


def test_a_used_up_window_holds_every_agent_but_the_operator_s_own_and_lets_go_when_it_clears(
    tmp_path: Path,
) -> None:
    door = Door()
    governing = governor(tmp_path, door)
    governing.ledger.published([window(100)])
    seen = repository(
        tmp_path,
        session("worker", calling="Bash"),
        session("mine", spawned_by=""),
    )
    view = governing.look([seen], NOW)
    held = door.holding(seen.store)
    assert [(each.member, each.cause) for each in held] == [("worker", "window")]
    assert "5-hour window used up until" in held[0].said
    assert held[0].until == NOW + timedelta(hours=2)
    assert {each.session: each.exempt for each in view.agents} == {
        "repo/worker": False,
        "repo/mine": True,
    }
    assert view.accounts[0].exhausted.startswith("5-hour window used up")
    governing.ledger.published([window(40)])
    governing.look([seen], NOW + timedelta(minutes=1))
    assert door.holding(seen.store) == []
    assert [each.member for each in door.lifted] == ["worker"]


def test_telemetry_is_charged_to_the_session_and_to_the_subagent_that_spent_it(
    tmp_path: Path,
) -> None:
    join = TelemetryJoin(grace=0)
    join.spent(
        [
            RequestSpend(
                session="conversation-s1",
                request="r1",
                at=NOW.timestamp(),
                usd=0.5,
                tokens=100,
            ),
            RequestSpend(
                session="conversation-s1",
                request="r2",
                at=NOW.timestamp(),
                usd=0.25,
                tokens=40,
            ),
            RequestSpend(
                session="elsewhere", request="r3", at=NOW.timestamp(), usd=9, tokens=9
            ),
        ]
    )
    join.made(
        [
            RequestAgent(request="r1"),
            RequestAgent(request="r2", agent="a9c6e3cf8a6129cf9"),
        ]
    )
    governing = governor(tmp_path, Door(), join=join)
    seen = repository(
        tmp_path,
        session("s1"),
        session("s1-a9c6e3cf8a6129cf9", parent="s1", answers=["a9c6e3cf8a6129cf9"]),
    )
    view = governing.look([seen], NOW)
    spent = {each.session: each.total for each in view.agents}
    assert spent == {
        "repo/s1": Spend(usd=0.5, tokens=100),
        "repo/s1-a9c6e3cf8a6129cf9": Spend(usd=0.25, tokens=40),
    }
    assert [each.request for each in governing.unplaced] == ["r3"]
    assert view.telemetry


def test_slots_go_first_come_and_pass_on_once_the_holder_stops(tmp_path: Path) -> None:
    door = Door()
    governing = governor(tmp_path, door, "[budget]\nmax_active = 1\n")
    first = session("first", calling="Bash", at=NOW - timedelta(minutes=5))
    second = session("second", calling="Edit", at=NOW - timedelta(minutes=1))
    seen = repository(tmp_path, first, second)
    governing.look([seen], NOW)
    assert [(each.member, each.cause) for each in door.holding(seen.store)] == [
        ("second", "slot")
    ]
    idle = first.model_copy(update={"calling": "", "at": NOW - timedelta(minutes=4)})
    governing.look([repository(tmp_path, idle, second)], NOW + timedelta(minutes=1))
    assert door.holding(seen.store) == []


def test_a_held_call_keeps_its_place_in_the_queue(tmp_path: Path) -> None:
    door = Door()
    held = Calls([WaitingCall(member="second", since=NOW - timedelta(hours=1))])
    governing = governor(tmp_path, door, "[budget]\nmax_active = 1\n", calls=held)
    first = session("first", calling="Bash", at=NOW - timedelta(minutes=5))
    second = session("second", calling="Edit", at=NOW)
    seen = repository(tmp_path, first, second)
    governing.look([seen], NOW)
    assert [each.member for each in door.holding(seen.store)] == ["first"]


def test_a_spend_cap_holds_its_agent_and_tells_the_operator_once(
    tmp_path: Path,
) -> None:
    told: list[str] = []

    def notify(summary: str, body: str) -> bool:
        told.append(body)
        return bool(summary)

    door = Door()
    governing = BudgetGovernor(
        SpendLedger(tmp_path / "ledger.json"), config(tmp_path), door, notify=notify
    )
    seen = repository(tmp_path, session("spender", calling="Bash"))
    governing.look([seen], NOW)
    governing.settle("repo/spender", "low", AgentCaps(total_tokens=50))
    governing.ledger.charge(
        [Charge(agent="repo/spender", account=CLAUDE, at=NOW.timestamp(), tokens=60)]
    )
    governing.look([seen], NOW)
    governing.look([seen], NOW + timedelta(seconds=2))
    assert [each.cause for each in door.holding(seen.store)] == ["cap"]
    assert len(told) == 1 and "60 tokens of its 50 cap" in told[0]
    meter = next(
        each for each in governing.current().agents if each.session == "repo/spender"
    )
    assert (meter.priority, meter.caps.total_tokens) == ("low", 50)


def test_the_turtle_is_a_line_in_the_person_s_config(tmp_path: Path) -> None:
    governing = governor(tmp_path, Door(), "# mine\n")
    assert governing.turtle(True)
    assert "# mine" in (tmp_path / "config.toml").read_text()
    assert governing.look([], NOW).turtle
    assert not governing.turtle(False)


def test_an_unreadable_budget_says_why_and_only_a_used_up_window_holds(
    tmp_path: Path,
) -> None:
    door = Door()
    governing = governor(tmp_path, door, "[budget]\nmax_active = -1\n")
    governing.ledger.published([window(100)])
    seen = repository(tmp_path, session("worker", calling="Bash"))
    view = governing.look([seen], NOW)
    assert view.refused
    assert [each.cause for each in door.holding(seen.store)] == ["window"]


def test_a_codex_rollout_is_charged_by_its_token_counts(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout-2026-10-05T12-00-00-thread.jsonl"
    count = {
        "timestamp": "2026-10-05T11:59:00Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"total_tokens": 1200}},
            "rate_limits": {
                "primary": {
                    "used_percent": 41.0,
                    "window_minutes": 300,
                    "resets_at": int((NOW + timedelta(hours=2)).timestamp()),
                },
                "secondary": None,
            },
        },
    }
    rollout.write_text(
        "\n".join(
            [
                json.dumps({"type": "session_meta", "payload": {}}),
                json.dumps(count),
                json.dumps(count),
            ]
        )
        + "\n"
    )
    governing = governor(tmp_path, Door())
    codex = session("thread", runtime="codex", transcript=str(rollout))
    view = governing.look([repository(tmp_path, codex)], NOW)
    assert view.agents[0].total == Spend(tokens=2400)
    governing.look([repository(tmp_path, codex)], NOW + timedelta(seconds=2))
    line = governing.ledger.read().line("repo/thread")
    assert line is not None and line.total == Spend(tokens=2400)


class Reader(UsageReader):
    def __init__(self, used: list[float]) -> None:
        self.used = used

    def read(self, detail: bool) -> UsageReport:
        del detail
        if not self.used:
            raise UsageUnavailable("the endpoint is down")
        return UsageReport(
            runtime_name="Claude Code",
            windows=[
                PacingWindow(
                    label="5-hour",
                    utilization_pct=self.used.pop(0),
                    resets_at=NOW + timedelta(hours=3),
                    window_hours=5,
                )
            ],
        )


def test_an_account_s_rate_is_read_off_its_last_hour_of_readings(
    tmp_path: Path,
) -> None:
    home = AccountHome(account=CLAUDE, home=tmp_path, signed_in=True)
    reader = Reader([10.0, 25.0])
    watch = AccountWatch(home, lambda each: reader)
    watch.read(NOW - timedelta(minutes=30))
    watch.read(NOW)
    standing = watch.standing(NOW)
    assert standing.windows[0].per_hour == 30.0
    watch.read(NOW + timedelta(minutes=1))
    assert watch.standing(NOW).error == "the endpoint is down"
    assert watch.standing(NOW).windows[0].window.utilization_pct == 25.0


def test_the_poller_publishes_every_account_where_judgements_read_them(
    tmp_path: Path,
) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json")
    home = AccountHome(account=CLAUDE, home=tmp_path, signed_in=True)
    reader = Reader([55.0])
    poller = AccountPoller(
        lambda: [tmp_path],
        ledger,
        config(tmp_path),
        homes=lambda roots: [home],
        reader=lambda each: reader,
    )
    poller.poll(NOW)
    published = ledger.read().accounts
    assert [
        (each.account.key, each.windows[0].window.utilization_pct) for each in published
    ] == [("claude:default", 55.0)]


def test_a_session_draws_on_the_account_its_launch_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    work_home = tmp_path / "profiles" / "work" / "claude-config"
    LaunchedAccounts().record(
        LaunchedAccount(
            member="lead",
            runtime="claude",
            owner=LoginOwner(home=work_home, profile="work"),
            checkout=tmp_path,
            contained=False,
            at=NOW,
        )
    )
    work = Account(runtime="claude", profile="work")
    poller = AccountPoller(
        lambda: [tmp_path],
        SpendLedger(tmp_path / "ledger.json"),
        config(tmp_path),
        homes=lambda roots: [AccountHome(account=work, home=work_home, signed_in=True)],
        reader=lambda each: Reader([30.0]),
    )
    poller.poll(NOW)
    drawn = launched_on(poller)
    known = KnownRepository(repository=tmp_path / ".git", checkout=tmp_path)
    assert drawn(known, session("lead")) == work
    assert drawn(known, session("never-recorded")) == CLAUDE


def test_the_switch_route_answers_what_each_session_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    known = KnownRepository(repository=tmp_path / ".git", checkout=tmp_path)
    asked: list[str] = []

    def switched(checkout: Path, runtime: AdapterName, profile: str) -> SwitchOutcome:
        asked.append(f"{checkout} {runtime} {profile}")
        if profile == "nobody":
            raise KeyError("unknown profile 'nobody'")
        return SwitchOutcome(
            checkout=checkout,
            runtime=runtime,
            profile=profile,
            volume="lup-claude-repo",
        )

    monkeypatch.setattr(budget_module, "switch_repository_login", switched)
    served: list[tuple[str, ...]] = []
    app = FastAPI()
    profile_routes(app, served.append, lambda: [known])
    client = TestClient(app)
    reply = client.post(
        f"/api/repositories/{known.key()}/profile", json={"profile": "work"}
    )
    assert reply.status_code == 200
    assert reply.json()["outcome"]["volume"] == "lup-claude-repo"
    assert reply.json()["said"][0].startswith("lup-claude-repo still holds its login")
    assert asked == [f"{tmp_path} claude work"]
    missing = client.post(
        f"/api/repositories/{known.key()}/profile", json={"profile": "nobody"}
    )
    assert missing.status_code == 404
    elsewhere = client.post(
        "/api/repositories/elsewhere/profile", json={"profile": "a"}
    )
    assert elsewhere.status_code == 404
    assert served == [("profiles",)]


def test_a_window_at_its_ceiling_holds_through_the_store_and_its_reset_lets_go(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    peers = RepositoryPeers(checkout)
    worker, helper = mint_member_id(), mint_member_id()
    peers.join(worker, checkout, cli_name="worker")
    peers.join(helper, checkout, cli_name="helper")
    seen = RepositoryAgents(
        key="repo",
        known=KnownRepository(repository=checkout / ".git", checkout=checkout),
        store=peers.root,
        agents=[
            session(worker, calling="Bash"),
            session(helper),
            session("mine", spawned_by=""),
        ],
    )
    governing = BudgetGovernor(
        SpendLedger(tmp_path / "ledger.json"),
        config(tmp_path),
        StoredHolds(),
        calls=StoredCalls(),
    )
    governing.ledger.published([window(96)])
    view = governing.look([seen], NOW)
    standing = holds.standing(peers.root, now=NOW)
    assert sorted(
        (hold.member, hold.reason, hold.owner) for hold in standing
    ) == sorted(
        [
            (worker, HoldReason.WINDOW, HoldOwner.BUDGET),
            (helper, HoldReason.WINDOW, HoldOwner.BUDGET),
        ]
    )
    assert all(hold.until == NOW + timedelta(hours=2) for hold in standing)
    assert holds.holding(peers.root, worker)[0].said.startswith("5-hour window used up")
    assert view.holds and view.accounts[0].exhausted.startswith("5-hour window used up")
    placed = {hold.member: hold.placed for hold in standing}
    governing.look([seen], NOW + timedelta(minutes=1))
    again = holds.standing(peers.root, now=NOW)
    assert {hold.member: hold.placed for hold in again} == placed
    governing.ledger.published([window(2, hours_left=5)])
    governing.look([seen], NOW + timedelta(hours=2, minutes=1))
    assert holds.standing(peers.root, now=NOW) == []


class Stopped(Resumption):
    """Sessions a limit stopped, each until a moment, and who was told to continue."""

    def __init__(self, until: dict[str, datetime]) -> None:
        self.until = until
        self.told: list[str] = []

    def stopped(self, each: Placed) -> datetime | None:
        return self.until[each.agent.id] if each.agent.id in self.until else None

    def resume(self, each: Placed) -> bool:
        self.told.append(each.agent.id)
        return True


def test_a_session_a_limit_stopped_is_told_to_continue_once_its_window_resets(
    tmp_path: Path,
) -> None:
    resets = NOW + timedelta(hours=1)
    stopped = Stopped({"worker": resets, "mine": resets, "scout": resets})
    governing = BudgetGovernor(
        SpendLedger(tmp_path / "ledger.json"),
        config(tmp_path),
        Door(),
        resumption=stopped,
    )
    seen = repository(
        tmp_path,
        session("worker"),
        session("mine", spawned_by=""),
        session("scout", parent="worker"),
        session("busy", calling="Bash"),
    )
    governing.look([seen], NOW)
    assert stopped.told == []
    governing.look([seen], resets + timedelta(minutes=1))
    assert stopped.told == ["worker"]
    governing.look([seen], resets + timedelta(minutes=2))
    assert stopped.told == ["worker"]


def test_a_transcript_says_when_the_limit_that_ended_its_turn_resets(
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "session.jsonl"
    said = {"type": "assistant", "message": {"role": "assistant", "content": []}}
    refused = {
        "type": "assistant",
        "isApiErrorMessage": True,
        "error": "rate_limit",
        "apiErrorStatus": 429,
        "quotaLimits": {
            "status": "rejected",
            "resetsAt": 1790836200,
            "rateLimitType": "five_hour",
        },
        "message": {"role": "assistant", "model": "<synthetic>", "content": []},
    }
    transcript.write_text(
        "\n".join(json.dumps(each) for each in [said, refused]) + "\n"
    )
    assert limit_reset(transcript) == datetime.fromtimestamp(1790836200, UTC)
    with transcript.open("a") as handle:
        handle.write(
            json.dumps({"type": "user", "message": {"content": "continue"}}) + "\n"
        )
    assert limit_reset(transcript) is None
    assert limit_reset(tmp_path / "absent.jsonl") is None


def test_a_window_used_up_for_days_tells_the_operator_once(tmp_path: Path) -> None:
    told: list[str] = []
    governing = BudgetGovernor(
        SpendLedger(tmp_path / "ledger.json"),
        config(tmp_path),
        Door(),
        notify=lambda summary, body: told.append(summary) or True,
    )
    seen = repository(tmp_path, session("worker"))
    governing.ledger.published([window(96)])
    governing.look([seen], NOW)
    assert told == []
    governing.ledger.published([window(97, hours_left=60)])
    governing.look([seen], NOW)
    governing.look([seen], NOW + timedelta(minutes=1))
    assert told == [
        f"claude:default: 5-hour window used up until {clock(NOW + timedelta(hours=60), NOW)}"
    ]


def test_the_poller_reads_more_often_near_a_ceiling(tmp_path: Path) -> None:
    home = AccountHome(account=CLAUDE, home=tmp_path, signed_in=True)
    reader = Reader([50.0, 88.0])
    poller = AccountPoller(
        lambda: [tmp_path],
        SpendLedger(tmp_path / "ledger.json"),
        config(tmp_path),
        homes=lambda roots: [home],
        reader=lambda each: reader,
    )
    poller.poll(NOW)
    assert poller.interval(NOW) == 300
    poller.poll(NOW + timedelta(minutes=1))
    assert reader.used == [88.0], "a reading still fresh is not asked for again"
    later = NOW + timedelta(minutes=6)
    poller.poll(later)
    assert poller.interval(later) == 90


def test_a_session_s_status_line_reading_stands_in_for_the_provider(
    tmp_path: Path,
) -> None:
    join = TelemetryJoin()
    door = Door()
    home = AccountHome(account=CLAUDE, home=tmp_path, signed_in=True)
    reader = Reader([10.0, 20.0])
    poller = AccountPoller(
        lambda: [tmp_path],
        SpendLedger(tmp_path / "ledger.json"),
        config(tmp_path),
        homes=lambda roots: [home],
        reader=lambda each: reader,
    )
    poller.poll(NOW)
    governing = BudgetGovernor(
        SpendLedger(tmp_path / "ledger.json"),
        config(tmp_path),
        door,
        poller=poller,
        join=join,
    )
    join.heard(
        HandedWindows(
            session="conversation-worker",
            at=(NOW + timedelta(minutes=2)).timestamp(),
            windows=[
                PacingWindow(
                    label="5-hour",
                    utilization_pct=96,
                    resets_at=NOW + timedelta(hours=2),
                    window_hours=5,
                )
            ],
        )
    )
    seen = repository(tmp_path, session("worker", calling="Bash"))
    view = governing.look([seen], NOW + timedelta(minutes=3))
    assert view.accounts[0].windows[0].window.utilization_pct == 96
    assert [each.cause for each in door.holding(seen.store)] == ["window"]
    poller.poll(NOW + timedelta(minutes=3))
    assert reader.used == [20.0], "a session's fresh reading spares the provider"


class VolumeKept(LoginPlace):
    """A repository volume as a test keeps it: the login inside, and whose it is."""

    def __init__(self, login: bytes, account: str) -> None:
        self.login = login
        self.account = json.dumps(
            {
                "oauthAccount": {
                    "accountUuid": f"uuid-{account}",
                    "emailAddress": f"{account}@example.com",
                }
            }
        ).encode()

    def named(self) -> str:
        return "volume lup-claude-lup"

    def read(self) -> LoginCopy:
        return LoginCopy(login=self.login, account=self.account)

    def write(self, content: bytes, expected: bytes | None) -> None:
        raise AssertionError("the poller never writes a volume")


def test_the_account_signed_in_to_a_volume_is_read_with_the_volumes_own_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        budget_module,
        "state_volume_name",
        lambda root, login: f"lup-{login.state_volume}-lup",
    )
    asked: list[str] = []

    def answered(token: str) -> UsageResponse:
        asked.append(token)
        return UsageResponse.model_validate(
            {
                "five_hour": {
                    "utilization": 61.0,
                    "resets_at": "2026-10-05T14:00:00+00:00",
                }
            }
        )

    monkeypatch.setattr(claude_reader, "fetch_usage_as", answered)
    kept = VolumeKept(
        json.dumps({"claudeAiOauth": {"accessToken": "volume-token"}}).encode(),
        "second",
    )

    def copy(checkout: Path, login: ProviderLogin) -> LoginPlace:
        del checkout, login
        return kept

    [volume] = [each for each in held_homes([tmp_path], copy) if each.volume]
    assert volume.account == Account(runtime="claude", profile="second@example.com")
    assert (volume.identity, volume.volume) == ("uuid-second", "lup-claude-lup")
    poller = AccountPoller(
        lambda: [tmp_path],
        SpendLedger(tmp_path / "ledger.json"),
        config(tmp_path),
        homes=lambda roots: held_homes(roots, copy),
        reader=lambda each: drawn_reader(each, copy),
    )
    poller.poll(NOW)
    known = KnownRepository(repository=tmp_path / ".git", checkout=tmp_path)

    assert asked == ["volume-token"]
    assert launched_on(poller)(known, session("lead")) == volume.account
    standing = next(
        watch.standing(NOW) for watch in poller.standings(NOW) if watch.home.volume
    )
    assert standing.windows[0].window.utilization_pct == 61.0
