"""The dashboard is one process per person that every launch holds.

The first launch starts it and every later one, in any repository, joins it
and registers its repository; it stops with the last. Its address — the port
it was given and the capability that opens it — survives a restart, it runs
as the operator's rather than as the session that happened to start it, and
a launch from code that differs replaces it. While any launch holds it, one
that stops is started again by them, and says why it stopped.
"""

import os
import signal
import socket
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import sh
from typer.testing import CliRunner

from lup.coordination.identity import MEMBER_ENV
from lup.devtools.dashboard.companion import (
    Dashboard,
    DashboardRegistry,
    DashboardToken,
    LaunchRecord,
    dashboard_status,
    launched_by_an_operator,
    private_url,
    written,
)
from lup.devtools.dashboard.pulse import (
    DASHBOARD_PULSE_ENV,
    DashboardPulse,
    PulseFile,
    status_line,
)
from lup.devtools.dashboard.reviews import create_operator_dashboard_app
from lup.launch.declaration import Mount
from lup.providers.user_config import UserConfigFile
from lup.devtools.harness.launch import held_services
from lup.devtools.review.answers import ReviewAnswers
from lup.devtools.review.app import relay
from lup.harness.models import Harness, PromptDocument
from lup.launch.companions import (
    CompanionLaunch,
    LiveProcess,
    held_companions,
    lent_directory,
)
from lup.launch.preflight import NONCE_VARIABLE
from lup.policy.identity import DASHBOARD_URL_ENV
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion
from tests.unit.reviews import bound


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """lup's own state, this test's alone, so no dashboard of the person's is touched."""
    home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(home))
    for name in (NONCE_VARIABLE, MEMBER_ENV, DASHBOARD_URL_ENV, DASHBOARD_PULSE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("webbrowser.open", lambda _url: True)
    return home


def repository(root: Path) -> Path:
    sh.Command("git")("init", "-q", "-b", "main", str(root))
    return root.resolve()


def launch_at(root: Path, **environment: str) -> CompanionLaunch:
    """A launch from ``root``, with the environment a terminal would hand it."""
    return CompanionLaunch(
        root=root,
        runtime="claude",
        environment={**os.environ, **environment},  # lup: ignore[os-environ]
    )


def parked(root: Path, question_id: str = "q-1") -> PersistentQuestion:
    operation = Operation(
        id=f"operation-{question_id}",
        session="native-session",
        requester="asking-session",
        tool="Bash",
        payload={"command": "touch must-not-run"},
        cwd=root,
        worktree=root,
    )
    return relay(root).record(
        bound(
            PersistentQuestion(
                id=question_id,
                operation=operation,
                fingerprint="",
                reason="The operator reviews this command.",
                eligible=["operator"],
                resumption="native_retry",
            )
        )
    )


@pytest.fixture
def dashboard(state: Path) -> Iterator[Dashboard]:
    """A dashboard on a port of its own, stopped whatever the test left running."""
    served = Dashboard(ports={"page": free_port()}, ready_within=30.0)
    yield served
    served.stopped(Path.cwd())


def test_the_capability_is_minted_once_and_kept_the_operators_own(
    tmp_path: Path,
) -> None:
    token = DashboardToken(directory=tmp_path / "dashboard")

    first = token.minted()
    again = token.minted()

    assert first.fresh and not again.fresh
    assert first.value == again.value
    assert token.path().stat().st_mode & 0o077 == 0
    token.path().chmod(0o644)
    with pytest.raises(PermissionError, match="readable by nobody else"):
        token.read()


def test_a_repository_stays_known_after_its_launch_ends(tmp_path: Path) -> None:
    root = repository(tmp_path / "project")
    registry = DashboardRegistry(directory=tmp_path / "dashboard")

    with registry.registered(root):
        [launch] = registry.launches()
        assert registry.live(launch.repository)

    assert registry.launches() == []
    [known] = registry.repositories()
    assert known.checkout == root
    assert known.name() == "project"
    assert not registry.live(known.repository)


def test_launches_share_one_dashboard_that_stops_with_the_last(
    dashboard: Dashboard, tmp_path: Path
) -> None:
    first_root = repository(tmp_path / "first")
    second_root = repository(tmp_path / "second")
    parked(second_root)

    with held_companions([dashboard], launch_at(first_root)) as first:
        url = first.environment[DASHBOARD_URL_ENV]
        serving = dashboard.standing(first_root).serving
        assert serving is not None
        with held_companions([dashboard], launch_at(second_root)) as second:
            assert second.environment[DASHBOARD_URL_ENV] == url
            assert dashboard.standing(second_root).serving == serving
            token = DashboardToken(
                directory=dashboard.standing(first_root).place.state
            ).read()
            snapshot = httpx.get(
                f"{url}/api/reviews",
                headers={"Authorization": f"Bearer {token}"},
                trust_env=False,
            ).json()
            assert [row["id"] for row in snapshot["reviews"]] == ["q-1"]
            assert {root["repository_name"] for root in snapshot["roots"]} == {
                "first",
                "second",
            }
        assert serving.running()

    assert not serving.running()
    assert dashboard.standing(first_root).serving is None


def test_a_restart_keeps_the_address_and_the_capability(
    dashboard: Dashboard, tmp_path: Path
) -> None:
    root = repository(tmp_path / "project")

    with held_companions([dashboard], launch_at(root)) as first:
        opened = private_url(dashboard, root)
        started = dashboard.standing(root).serving
    with held_companions([dashboard], launch_at(root)) as second:
        assert second.environment == first.environment
        assert private_url(dashboard, root) == opened
        assert dashboard.standing(root).serving != started


def test_the_dashboard_leaves_the_starting_sessions_identity_behind(
    dashboard: Dashboard, tmp_path: Path
) -> None:
    root = repository(tmp_path / "project")
    session = launch_at(
        root, **{MEMBER_ENV: "a-session", NONCE_VARIABLE: "its-boundary"}
    )

    assert not launched_by_an_operator(session.environment)
    with held_companions([dashboard], session):
        serving = dashboard.standing(root).serving
        assert serving is not None
        environ = Path(f"/proc/{serving.pid}/environ").read_bytes().split(b"\0")

    names = {entry.partition(b"=")[0].decode() for entry in environ if entry}
    assert MEMBER_ENV not in names and NONCE_VARIABLE not in names


def test_a_launch_from_other_code_replaces_what_runs(
    dashboard: Dashboard, tmp_path: Path
) -> None:
    root = repository(tmp_path / "project")
    newer = dashboard.model_copy(update={"revision": "another-revision"})

    with held_companions([dashboard], launch_at(root)):
        before = dashboard.standing(root).serving
        with held_companions([newer], launch_at(root)):
            after = newer.standing(root).serving
            assert before is not None and after is not None
            assert after != before
            assert not before.running()


def test_status_open_and_stop_are_read_from_outside_every_launch(
    dashboard: Dashboard, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path / "project")
    monkeypatch.setattr("lup.devtools.dashboard.reviews.Dashboard", lambda: dashboard)
    cli = create_operator_dashboard_app(root)
    runner = CliRunner()

    idle = dashboard_status(dashboard, root)
    assert not idle.serving and "Not running" in idle.detail
    with held_companions([dashboard], launch_at(root)):
        status = dashboard_status(dashboard, root)
        assert status.serving and status.sessions == 1
        assert status.repositories == [str(root / ".git")]
        opened = runner.invoke(cli, ["open"])
        assert opened.exit_code == 0, opened.output
        stopped = runner.invoke(cli, ["stop"])
        assert stopped.exit_code == 0
        assert "Dashboard stopped; it stays stopped until" in stopped.output
        assert dashboard.standing(root).serving is None


def test_inside_a_session_the_dashboard_answers_from_what_it_publishes(
    dashboard: Dashboard, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Truthfully, and without the private state or the capability a session never holds."""
    url = "http://127.0.0.1:8766"
    pulse = PulseFile.of(lent_directory(tmp_path / "dashboard"))
    beat = datetime.now(UTC)
    published = DashboardPulse(
        url=url,
        pid=4242,
        pending=2,
        sessions=3,
        repositories=["/work/project/.git"],
        tabs=1,
        beat=beat,
    )
    written(pulse.path, published.model_dump_json())
    monkeypatch.setenv(MEMBER_ENV, "a-session")
    monkeypatch.setenv(DASHBOARD_URL_ENV, url)
    monkeypatch.setenv(DASHBOARD_PULSE_ENV, str(pulse.path))

    status = dashboard_status(dashboard, tmp_path)
    runner = CliRunner()
    refused = runner.invoke(create_operator_dashboard_app(tmp_path), ["open"])
    line = runner.invoke(create_operator_dashboard_app(tmp_path), ["line"])
    stale = published.model_copy(update={"beat": beat - timedelta(minutes=5)})
    written(pulse.path, stale.model_dump_json())
    stopped = dashboard_status(dashboard, tmp_path)
    pulse.path.unlink()
    taken_down = dashboard_status(dashboard, tmp_path)
    monkeypatch.delenv(DASHBOARD_PULSE_ENV)
    unlent = dashboard_status(dashboard, tmp_path)

    assert status.serving and status.url == url and status.pid == 4242
    assert status.sessions == 3 and status.pending == 2 and status.tabs == 1
    assert status.repositories == ["/work/project/.git"]
    assert refused.exit_code == 2
    assert "outside the agent session" in refused.output
    assert line.exit_code == 0 and line.output == f"2 reviews pending · {url}\n"
    assert not stopped.serving and "stopped" in stopped.detail
    assert not taken_down.serving and "took its pulse down" in taken_down.detail
    assert unlent.serving and unlent.url == url and "lent no pulse" in unlent.detail


def test_the_service_tells_the_desktop_and_publishes_what_it_counts(
    dashboard: Dashboard, tmp_path: Path
) -> None:
    """End to end: a review parked under a held dashboard reaches the desktop and the pulse."""
    root = repository(tmp_path / "project")
    tools = tmp_path / "bin"
    tools.mkdir()
    told, opened = tmp_path / "told.txt", tmp_path / "opened.txt"
    for name, record in (("notify-send", told), ("browser", opened)):
        script = tools / name
        script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {record}\n")
        script.chmod(0o755)
    launch = launch_at(
        root,
        PATH=f"{tools}:{os.environ['PATH']}",  # lup: ignore[os-environ]
        BROWSER=str(tools / "browser"),
        DISPLAY=":0",
    )

    with held_companions([dashboard], launch) as joined:
        pulse = PulseFile(path=Path(joined.environment[DASHBOARD_PULSE_ENV]))
        parked(root)
        counted = None
        for _ in range(100):
            counted = pulse.read()
            waited = counted is not None and counted.pending == 1
            if told.exists() and opened.exists() and waited:
                break
            time.sleep(0.2)
        assert Mount(path=pulse.path.parent) in joined.mounts
        assert joined.status_line is not None
        assert joined.status_line.argv[-3:] == ["dashboard", "line", str(pulse.path)]
        assert counted is not None and counted.pending == 1 and counted.sessions == 1
        assert "Run: touch must-not-run" in told.read_text()
        assert "/#token=" in opened.read_text()

    assert pulse.read() is None


def running_since(pulse: PulseFile, after: datetime | None = None) -> datetime:
    """When the dashboard began running the code its pulse names, once it names one after ``after``."""
    for _ in range(150):
        published = pulse.read()
        since = published.code.since if published is not None else None
        if since is not None and (after is None or since > after):
            return since
        time.sleep(0.2)
    raise AssertionError(f"{pulse.path} named no code running after {after}")


def test_the_operator_restarts_the_dashboard_in_place(
    dashboard: Dashboard, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same process, port and capability; its pulse names the code it now runs."""
    root = repository(tmp_path / "project")
    monkeypatch.setattr("lup.devtools.dashboard.reviews.Dashboard", lambda: dashboard)
    cli = create_operator_dashboard_app(root)
    runner = CliRunner()

    with held_companions([dashboard], launch_at(root)) as joined:
        pulse = PulseFile(path=Path(joined.environment[DASHBOARD_PULSE_ENV]))
        serving = dashboard.standing(root).serving
        opened = private_url(dashboard, root)
        first = running_since(pulse)
        restarted = runner.invoke(cli, ["restart"])
        again = running_since(pulse, first)
        published = pulse.read()
        assert dashboard.standing(root).serving == serving
        assert private_url(dashboard, root) == opened
        monkeypatch.setenv(MEMBER_ENV, "a-session")
        refused = runner.invoke(cli, ["restart"])

    assert restarted.exit_code == 0, restarted.output
    assert "restarts onto its checkout's code" in restarted.output
    assert again > first
    assert published is not None and published.code.source and not published.code.older
    assert refused.exit_code == 2 and "outside the agent session" in refused.output


def serving_again(dashboard: Dashboard, root: Path, before: LiveProcess) -> LiveProcess:
    """The dashboard serving once a process other than ``before`` does."""
    for _ in range(300):
        serving = dashboard.standing(root).serving
        if serving is not None and serving != before:
            return serving
        time.sleep(0.2)
    raise AssertionError(f"nothing served in place of pid {before.pid}")


def restart_said(pulse: PulseFile) -> str:
    """Why the dashboard stopped, once its pulse says it was started again."""
    for _ in range(150):
        published = pulse.read()
        if published is not None and published.code.restarted:
            return published.code.restarted
        time.sleep(0.2)
    raise AssertionError(f"{pulse.path} never said the dashboard was restarted")


def test_a_dashboard_killed_while_held_comes_back_at_its_address(
    dashboard: Dashboard, tmp_path: Path
) -> None:
    """Same port and capability, so an open tab reconnects; and it says why it stopped."""
    root = repository(tmp_path / "project")
    watched = dashboard.model_copy(update={"backoff": (0.5,), "watched_every": 0.2})

    with held_companions([watched], launch_at(root)) as joined:
        pulse = PulseFile(path=Path(joined.environment[DASHBOARD_PULSE_ENV]))
        opened = private_url(watched, root)
        killed = watched.standing(root).serving
        assert killed is not None
        os.kill(killed.pid, signal.SIGKILL)
        serving_again(watched, root, killed)
        reopened = private_url(watched, root)
        said = restart_said(pulse)
        line = status_line(pulse.path)
        status = dashboard_status(watched, root)

    assert reopened == opened
    assert said == "it was ended by SIGKILL"
    assert line == f"restarted after it stopped: it was ended by SIGKILL · {status.url}"
    assert status.serving and status.restarts == 1
    assert status.exited is not None and status.exited.process == killed
    assert status.exited.status == -signal.SIGKILL
    assert status.exited.restarted is not None
    assert "Uvicorn running on" in "\n".join(status.exited.tail)


def test_the_dashboard_stops_at_once_with_a_tab_following_it(
    dashboard: Dashboard, tmp_path: Path
) -> None:
    """An open stream ends as serving stops, well inside the server's grace, with no error logged."""
    root = repository(tmp_path / "project")

    with held_companions([dashboard], launch_at(root)) as joined:
        url = joined.environment[DASHBOARD_URL_ENV]
        token = DashboardToken(directory=dashboard.slot(root).directory).read()
        with httpx.stream(
            "GET",
            f"{url}/api/stream",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            trust_env=False,
        ) as following:
            lines = following.iter_lines()
            next(line for line in lines if line.startswith("data: "))
            began = time.monotonic()
            assert dashboard.stopped(root)
            took = time.monotonic() - began
            for _ in lines:
                continue
    logged = dashboard.slot(root).log().read_text()

    assert took < 1.5, logged
    assert "ERROR" not in logged and "Traceback" not in logged, logged


def test_a_held_dashboard_the_operator_stops_stays_stopped_until_restart(
    dashboard: Dashboard, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sessions holding it leave it stopped; the status line and `dashboard
    status` say what starts it, and `dashboard restart` does, at the same address."""
    root = repository(tmp_path / "project")
    watched = dashboard.model_copy(update={"backoff": (0.2,), "watched_every": 0.1})
    monkeypatch.setattr("lup.devtools.dashboard.reviews.Dashboard", lambda: watched)
    cli = create_operator_dashboard_app(root)
    runner = CliRunner()

    with held_companions([watched], launch_at(root)) as joined:
        pulse = Path(joined.environment[DASHBOARD_PULSE_ENV])
        opened = private_url(watched, root)
        before = watched.standing(root).serving
        assert before is not None
        stopped = runner.invoke(cli, ["stop"])
        time.sleep(2.0)
        left = watched.standing(root)
        idle = dashboard_status(watched, root)
        line = status_line(pulse)
        restarted = runner.invoke(cli, ["restart"])
        again = watched.standing(root)
        reopened = private_url(watched, root)

    stop = left.stopped
    assert stopped.exit_code == 0
    assert (
        "Dashboard stopped; it stays stopped until "
        "`uv run lup-devtools dashboard restart` or the next launch."
    ) in stopped.output
    assert left.serving is None and left.stays_stopped and left.leases == 1
    assert stop is not None and stop.stays and stop.why == "the operator stopped it"
    assert stop.leases == 1 and stop.by == os.getpid()
    assert left.exited is None and left.restarts == 0
    assert idle.detail == (
        "Stopped by the operator; `uv run lup-devtools dashboard restart` starts it."
    )
    assert line == "dashboard stopped by the operator; `dashboard restart` starts it"
    assert restarted.exit_code == 0, restarted.output
    assert again.serving is not None and again.serving != before
    assert not again.stays_stopped and again.exited is None and again.restarts == 0
    assert reopened == opened


def test_restart_starts_a_dashboard_for_sessions_holding_none(
    dashboard: Dashboard, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sessions whose launchers do not look after it hold it while nothing runs:
    status says so, and the operator's restart starts one for them."""
    root = repository(tmp_path / "project")
    unwatched = dashboard.model_copy(update={"watched_every": 3600.0})
    monkeypatch.setattr("lup.devtools.dashboard.reviews.Dashboard", lambda: unwatched)
    cli = create_operator_dashboard_app(root)
    runner = CliRunner()

    with held_companions([unwatched], launch_at(root)):
        opened = private_url(unwatched, root)
        killed = unwatched.standing(root).serving
        assert killed is not None
        os.kill(killed.pid, signal.SIGKILL)
        for _ in range(100):
            if not killed.running():
                break
            time.sleep(0.05)
        idle = dashboard_status(unwatched, root)
        restarted = runner.invoke(cli, ["restart"])
        standing = unwatched.standing(root)
        reopened = private_url(unwatched, root)

    assert not idle.serving and idle.sessions == 1
    assert "`uv run lup-devtools dashboard restart` starts it for them" in idle.detail
    assert restarted.exit_code == 0, restarted.output
    assert "No dashboard was running while 1 session held it" in restarted.output
    assert standing.serving is not None and standing.serving != killed
    assert standing.leases == 1 and standing.restarts == 1
    assert standing.exited is not None
    assert standing.exited.status == -signal.SIGKILL
    assert reopened == opened


HOLDER = """
import os, sys, time
from pathlib import Path

from lup.devtools.dashboard.companion import Dashboard
from lup.launch.companions import CompanionLaunch, held_companions

port, root, done = int(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
launch = CompanionLaunch(root=root, runtime="claude", environment=dict(os.environ))
with held_companions([Dashboard(ports={"page": port}, ready_within=60.0)], launch):
    print("held", flush=True)
    for _ in range(12000):
        if done.exists():
            break
        time.sleep(0.05)
"""
"""A launch holding the dashboard in a process of its own, until a file says to let go."""


class Holder:
    """One launch's process holding the dashboard, as a terminal's launcher would."""

    def __init__(self, script: Path, port: int, root: Path, place: Path) -> None:
        place.mkdir()
        self.done = place / "done"
        self.said = place / "said"
        self.process = sh.Command(sys.executable)(
            str(script),
            str(port),
            str(root),
            str(self.done),
            _bg=True,
            _bg_exc=False,
            _out=str(self.said),
            _err_to_out=True,
        )
        for _ in range(600):
            if self.said.exists() and "held" in self.said.read_text():
                return
            time.sleep(0.1)
        raise AssertionError(self.said.read_text())

    def let_go(self) -> None:
        self.done.touch()
        self.process.wait()


def test_two_launches_hold_it_and_the_first_to_end_leaves_it_serving(
    dashboard: Dashboard, tmp_path: Path
) -> None:
    """Each launch's lease is its own process's: the one that started it ending takes nothing down."""
    root = repository(tmp_path / "project")
    script = tmp_path / "holder.py"
    script.write_text(HOLDER, encoding="utf-8")
    DashboardToken(directory=dashboard.slot(root).directory).minted()
    port = dashboard.ports["page"]

    first = Holder(script, port, root, tmp_path / "first")
    second = Holder(script, port, root, tmp_path / "second")
    both = dashboard.standing(root)
    first.let_go()
    one = dashboard.standing(root)
    second.let_go()
    none = dashboard.standing(root)

    assert both.serving is not None and both.leases == 2
    assert one.serving == both.serving and one.leases == 1
    assert none.serving is None and none.leases == 0
    assert none.stopped is not None and none.stopped.why.startswith("the last lease")


def test_a_dashboard_too_old_to_restart_itself_is_replaced(
    dashboard: Dashboard, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One that predates restarting in place is stopped and started again, still held."""
    root = repository(tmp_path / "project")
    monkeypatch.setattr("lup.devtools.dashboard.reviews.Dashboard", lambda: dashboard)
    monkeypatch.setattr(
        "lup.devtools.dashboard.companion.httpx.post",
        lambda *_words, **_named: httpx.Response(404),
    )
    cli = create_operator_dashboard_app(root)

    with held_companions([dashboard], launch_at(root)):
        before = dashboard.standing(root).serving
        replaced = CliRunner().invoke(cli, ["restart"])
        after = dashboard.standing(root)

    assert replaced.exit_code == 0, replaced.output
    assert "replaced" in replaced.output
    assert before is not None and after.serving is not None
    assert after.serving != before and not before.running()
    assert after.leases == 1


def test_reopening_is_turned_off_and_on_from_the_operators_terminal(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = create_operator_dashboard_app(tmp_path)
    runner = CliRunner()

    off = runner.invoke(cli, ["reopen", "--off"])
    said = runner.invoke(cli, ["reopen"])
    turned_off = UserConfigFile().load().dashboard.reopen
    on = runner.invoke(cli, ["reopen", "--on"])
    monkeypatch.setenv(MEMBER_ENV, "a-session")
    refused = runner.invoke(cli, ["reopen", "--off"])

    assert off.exit_code == 0 and not turned_off
    assert "off" in said.output
    assert on.exit_code == 0 and UserConfigFile().load().dashboard.reopen
    assert refused.exit_code == 2 and UserConfigFile().load().dashboard.reopen


def test_a_harness_taking_the_module_holds_the_dashboard_at_every_launch() -> None:
    """The answers are lent whether or not a dashboard serves: a parked call is answered either way."""
    declared = Harness(
        generator_version="0",
        plugins=[],
        guidance=PromptDocument(source=__name__, parts=[]),
        dashboard=True,
    )

    assert [type(each) for each in held_services(declared)] == [
        ReviewAnswers,
        Dashboard,
    ]
    assert [
        type(each)
        for each in held_services(declared.model_copy(update={"dashboard": False}))
    ] == [ReviewAnswers]


def test_a_launch_record_whose_launcher_died_is_swept(tmp_path: Path) -> None:
    registry = DashboardRegistry(directory=tmp_path / "dashboard")
    root = repository(tmp_path / "project")
    with registry.registered(root):
        [path] = registry.launches_directory().glob("*.json")
        record = LaunchRecord.model_validate_json(path.read_bytes())
        dead = LiveProcess(pid=2**22 + 7, started="0")
        path.write_text(record.model_copy(update={"holder": dead}).model_dump_json())
        assert registry.launches() == []
        assert not path.exists()


def test_a_launch_inside_a_container_starts_none_and_hands_on_the_hosts_address(
    dashboard: Dashboard, tmp_path: Path
) -> None:
    root = repository(tmp_path / "project")
    contained = launch_at(
        root, LUP_CONTAINED="1", **{DASHBOARD_URL_ENV: "http://127.0.0.1:8766"}
    )

    with held_companions([dashboard], contained) as handed:
        assert handed.environment == {DASHBOARD_URL_ENV: "http://127.0.0.1:8766"}
        assert dashboard.standing(root).serving is None
