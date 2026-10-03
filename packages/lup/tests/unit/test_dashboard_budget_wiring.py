"""How the budget reaches the dashboard's parts: the telemetry port, a session's variables, the stream, the status line."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from lup.devtools.dashboard.budget import BudgetView
from lup.devtools.dashboard.companion import Dashboard
from lup.devtools.dashboard.pulse import DashboardPulse, LineFacts, StatusInput
from lup.devtools.dashboard.reviews import ReviewStore
from lup.devtools.dashboard.service import ServiceArguments
from lup.devtools.dashboard.stream import BudgetEvent, LiveFeed
from lup.launch.companions import CompanionLaunch, CompanionPlace, Contribution
from lup.launch.declaration import Loopback
from lup.types import EnvVars

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
PORTS = {"page": 8767, "telemetry": 8776}


def test_the_shared_service_is_handed_its_telemetry_port_and_keeps_it_across_a_restart(
    tmp_path: Path,
) -> None:
    argv = (
        Dashboard().process(CompanionPlace(state=tmp_path, ports=PORTS), tmp_path).argv
    )
    words = argv[argv.index("lup.devtools.dashboard.service") + 1 :]
    arguments = ServiceArguments.parsed(words)
    assert (arguments.port, arguments.telemetry) == (8767, 8776)
    assert ServiceArguments.parsed(arguments.words()) == arguments
    older = ServiceArguments.parsed([str(tmp_path), "8767", "rev"])
    assert older.telemetry == 0 and older.words() == [str(tmp_path), "8767", "rev"]


@contextmanager
def held(
    tmp_path: Path, runtime: str, loopback: Loopback, environment: EnvVars | None = None
) -> Iterator[Contribution]:
    launch = CompanionLaunch(
        root=tmp_path, runtime=runtime, environment=environment or {}, loopback=loopback
    )
    with Dashboard().telemetered(launch, tmp_path, PORTS) as contribution:
        yield contribution


def test_a_claude_session_on_the_host_s_loopback_sends_its_telemetry_to_the_port(
    tmp_path: Path,
) -> None:
    with held(tmp_path, "claude", Loopback.HOST) as contribution:
        variables = contribution.environment
    assert variables["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:8776"
    token = (tmp_path / "telemetry-token").read_text().strip()
    assert variables["OTEL_EXPORTER_OTLP_HEADERS"] == f"Authorization=Bearer {token}"
    assert token != ""
    assert not (tmp_path / "token").exists()


def test_a_container_on_its_own_network_reaches_the_port_through_a_relay(
    tmp_path: Path,
) -> None:
    with held(tmp_path, "claude", Loopback.OWN) as contribution:
        relay = contribution.environment["LUP_HOST_SERVICE_DASHBOARD_TELEMETRY"]
        socket_path = Path(relay.split("@")[0])
        assert socket_path.exists()
        assert [mount.path for mount in contribution.mounts] == [socket_path.parent]
        assert relay.endswith("@8776")
    assert not socket_path.parent.exists()


def test_nothing_is_pointed_where_telemetry_cannot_reach_or_is_the_person_s_own(
    tmp_path: Path,
) -> None:
    with held(tmp_path, "claude", Loopback.SEALED) as sealed:
        assert sealed.environment == {}
        assert "no spend" in sealed.notices[0].text
    with held(tmp_path, "codex", Loopback.HOST) as codex:
        assert codex == Contribution()
    theirs = {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.example"}
    with held(tmp_path, "claude", Loopback.HOST, theirs) as kept:
        assert kept.environment == {}
        assert "already" in kept.notices[0].text


def test_a_session_launched_from_a_session_keeps_the_telemetry_it_inherited(
    tmp_path: Path,
) -> None:
    with held(tmp_path, "claude", Loopback.HOST) as first:
        inherited = first.environment
    with held(tmp_path, "claude", Loopback.HOST, inherited) as second:
        assert second.environment == inherited


def test_the_stream_carries_the_budget_when_it_moves(tmp_path: Path) -> None:
    shown = [BudgetView(turtle=False)]
    feed = LiveFeed(lambda: [], ReviewStore(roots=()), budget=lambda: shown[-1])
    feed.serves(("budgets",))
    feed.serves(("rename",))
    assert feed.state.observed(feed.observe()) == []
    shown.append(BudgetView(turtle=True))
    events = feed.state.observed(feed.observe())
    assert [type(each) for each in events] == [BudgetEvent]
    snapshot = feed.state.snapshot()
    assert snapshot.budget.turtle
    assert snapshot.served == ["budgets", "rename"]


def test_the_status_line_shows_the_turtle_while_it_is_on() -> None:
    pulse = DashboardPulse(url="http://127.0.0.1:8767", pid=1, beat=NOW, turtle=True)
    line = LineFacts.of(pulse, StatusInput(), NOW).fitted(0).plain()
    assert "🐢 turtle" in line
    calm = pulse.model_copy(update={"turtle": False})
    assert "🐢" not in LineFacts.of(calm, StatusInput(), NOW).fitted(0).plain()
