"""The dashboard moves onto its checkout's newer code, in place, and says so until it has.

It keeps the code it imported, while the checkout it imported it from moves
every time something lands. So it watches those files: once they have
changed and settled, and the new code starts, it restarts itself once no
write is in flight; until then it tells the page, the status line and every
refused write what is happening, rather than refusing an answer as though a
review had been altered. A terminal's own dashboard (`dashboard serve`) is
served and restarted the same way, and either one ends every open stream as
it stops rather than waiting out its grace.
"""

import asyncio
import signal
import socket
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import sh
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from lup.devtools.dashboard.companion import (
    DashboardHealth,
    DashboardRegistry,
    DashboardToken,
    KnownRepository,
    dashboard_revision,
)
from lup.devtools.dashboard.pulse import DashboardPulse, RunningCode
from lup.devtools.dashboard.refresh import ImportedSource, Refresh, WriteGate
from lup.devtools.dashboard.reviews import ReviewStore
from lup.devtools.dashboard.service import ServiceArguments
from lup.devtools.dashboard.stream import (
    LiveFeed,
    ServiceEvent,
    SnapshotEvent,
    StreamFrame,
)

URL = "http://127.0.0.1:8766"


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A package this process imported one module from."""
    root = tmp_path / "lup"
    root.mkdir()
    source = root / "answers.py"
    source.write_text("FINGERPRINT = 'first'\n")
    module = ModuleType("lup_refresh_fixture_answers")
    module.__file__ = str(source)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return root


class Probe:
    """The newer code's start, answered as the test says, and counted."""

    def __init__(self, failing: str = "") -> None:
        self.failing = failing
        self.asked = 0

    def __call__(self) -> str:
        self.asked += 1
        return self.failing


def test_the_running_code_is_the_package_files_it_imported(package: Path) -> None:
    source = ImportedSource(package)
    source.taken()
    before = source.digest()
    (package / "answers.py").touch()
    touched = source.moved()

    (package / "answers.py").write_text("FINGERPRINT = 'second'\n")

    assert list(source.files) == [package / "answers.py"]
    assert touched == {}
    assert list(source.moved()) == [package / "answers.py"]
    assert source.digest() == before


def test_a_checkout_that_moved_and_settled_is_restarted_onto(package: Path) -> None:
    probe = Probe()
    refresh = Refresh(ImportedSource(package), probe=probe)
    refresh.look()
    current = refresh.code()

    (package / "answers.py").write_text("FINGERPRINT = 'second'\n")
    refresh.look()
    settling, due_while_settling, probed = refresh.code(), refresh.due, probe.asked
    refresh.look()

    assert not current.older and current.said() == ""
    assert settling.older and not due_while_settling and probed == 0
    assert settling.said() == "dashboard runs older code; restarting"
    assert refresh.due and probe.asked == 1
    assert refresh.refusal().startswith("The dashboard runs older code")


def test_newer_code_that_does_not_start_is_said_and_not_restarted_onto(
    package: Path,
) -> None:
    refresh = Refresh(
        ImportedSource(package), probe=Probe("SyntaxError: invalid syntax")
    )
    refresh.look()
    (package / "answers.py").write_text("FINGERPRINT = \n")
    refresh.look()
    refresh.look()

    code = refresh.code()
    assert code.older and not refresh.due
    assert code.failing == "SyntaxError: invalid syntax"
    assert code.said() == "dashboard runs older code; its newer code does not start"
    assert refresh.refusal() == ""


def test_the_operator_asks_for_a_restart_with_nothing_moved(package: Path) -> None:
    probe = Probe()
    refresh = Refresh(ImportedSource(package), probe=probe)
    refresh.look()

    refresh.asked = True
    refresh.look()

    assert refresh.due and probe.asked == 1
    assert not refresh.code().older


async def test_a_write_is_held_in_view_and_refused_while_the_dashboard_restarts() -> (
    None
):
    released = asyncio.Event()
    refusing = ""

    async def answer(request: Request) -> JSONResponse:
        del request
        await released.wait()
        return JSONResponse({"answered": True})

    async def page(request: Request) -> JSONResponse:
        del request
        return JSONResponse({"page": True})

    gate = WriteGate(
        Starlette(
            routes=[Route("/answer", answer, methods=["POST"]), Route("/", page)]
        ),
        lambda: refusing,
    )
    async with AsyncClient(transport=ASGITransport(app=gate), base_url=URL) as client:
        pending = asyncio.create_task(client.post("/answer"))
        while not gate.writing:
            await asyncio.sleep(0.01)
        in_flight = gate.writing
        released.set()
        answered = await pending
        refusing = "The dashboard runs older code; answer again in a moment."
        refused = await client.post("/answer")
        read = await client.get("/")

    assert in_flight == 1 and gate.writing == 0
    assert answered.json() == {"answered": True}
    assert refused.status_code == 503
    assert refused.json() == {"detail": refusing}
    assert read.status_code == 200


def test_the_status_line_says_the_dashboard_is_restarting() -> None:
    pulse = DashboardPulse(
        url=URL,
        pid=1,
        pending=2,
        beat=datetime.now(UTC),
        code=RunningCode(source="abc", older=True),
    )

    assert pulse.line() == (
        f"2 reviews pending · dashboard runs older code; restarting · {URL}"
    )
    assert pulse.model_copy(update={"pending": 0}).line() == (
        f"dashboard runs older code; restarting · {URL}"
    )


async def test_the_page_is_told_which_code_runs_and_when_it_is_older() -> None:
    running = RunningCode(source="abc")
    feed = LiveFeed(
        lambda: [], ReviewStore(roots=()), interval=0.02, code=lambda: running
    )
    received: list[SnapshotEvent | ServiceEvent] = []
    done = asyncio.Event()

    async def disconnected() -> bool:
        return done.is_set()

    async def follow() -> None:
        nonlocal running
        async for chunk in feed.follow("", disconnected):
            data = [
                line.removeprefix("data: ")
                for line in chunk.splitlines()
                if line.startswith("data: ")
            ]
            if not data:
                continue
            match StreamFrame.model_validate_json(data[0]).event:
                case SnapshotEvent() | ServiceEvent() as event:
                    received.append(event)
                case _:
                    continue
            running = RunningCode(source="abc", older=True)
            if len(received) == 2:
                done.set()
                return

    await asyncio.wait_for(follow(), timeout=10)

    snapshot, service = received
    assert isinstance(snapshot, SnapshotEvent) and snapshot.code.source == "abc"
    assert isinstance(service, ServiceEvent) and service.code.older


async def test_closing_the_feed_ends_every_stream_at_once() -> None:
    """As the dashboard stops serving: a tab waiting for the next change is not left waiting."""
    feed = LiveFeed(lambda: [], ReviewStore(roots=()), interval=5.0)
    current = asyncio.Event()

    async def disconnected() -> bool:
        return False

    async def follow() -> None:
        async for chunk in feed.follow("", disconnected):
            if chunk.startswith(": live"):
                current.set()

    following = asyncio.create_task(follow())
    await asyncio.wait_for(current.wait(), timeout=10)
    began = time.monotonic()
    feed.close()
    await asyncio.wait_for(following, timeout=2)
    ended = time.monotonic() - began
    if feed.producer is not None:
        feed.producer.cancel()

    assert ended < 0.5 and feed.followers == 0


def test_a_restart_in_place_is_handed_back_what_it_was_started_with() -> None:
    shared = ServiceArguments(state=Path("/state"), port=8766, revision="abc")
    terminal = ServiceArguments(
        state=Path("/serve"), port=80, revision="abc", host="::1", shared=False
    )

    assert ServiceArguments.parsed(shared.words()) == shared
    assert ServiceArguments.parsed(terminal.words()) == terminal
    assert shared.words() == ["/state", "8766", "abc"]
    assert shared.url() == "http://127.0.0.1:8766"
    assert terminal.url() == "http://[::1]"
    with pytest.raises(ValueError, match="expected <state> <port> <revision>"):
        ServiceArguments.parsed(["only-one"])


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def health(url: str, token: str) -> DashboardHealth:
    """What the dashboard at ``url`` says of itself, once it answers."""
    for _ in range(600):
        try:
            answered = httpx.get(
                f"{url}/api/service",
                headers={"Authorization": f"Bearer {token}"},
                timeout=1,
                trust_env=False,
            )
        except httpx.HTTPError:
            time.sleep(0.1)
            continue
        if answered.status_code == 200:
            return DashboardHealth.model_validate_json(answered.content)
        time.sleep(0.1)
    raise AssertionError(f"nothing answered at {url}")


def running_since(url: str, token: str) -> datetime | None:
    """When the dashboard at ``url`` began running its code, as a fresh tab is first told."""
    with httpx.stream(
        "GET",
        f"{url}/api/stream",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
        trust_env=False,
    ) as following:
        for line in following.iter_lines():
            if not line.startswith("data: "):
                continue
            match StreamFrame.model_validate_json(line.removeprefix("data: ")).event:
                case SnapshotEvent() as snapshot:
                    return snapshot.code.since
                case _:
                    continue
    return None


def test_a_terminals_dashboard_restarts_in_place_and_ends_its_streams_as_it_stops(
    tmp_path: Path,
) -> None:
    """`dashboard serve` once it restarts itself: the same process, port and capability.

    Its open streams end as it stops, to restart or at Ctrl+C, well inside
    the grace a server gives them, and nothing is logged as an error.
    """
    repository = tmp_path / "project"
    sh.Command("git")("init", "-q", "-b", "main", str(repository))
    state = tmp_path / "state"
    token = DashboardToken(directory=state).minted().value
    DashboardRegistry(directory=state).recorded(
        KnownRepository(
            repository=(repository / ".git").resolve(), checkout=repository.resolve()
        )
    )
    arguments = ServiceArguments(
        state=state, port=free_port(), revision=dashboard_revision(), shared=False
    )
    url = arguments.url()
    said = tmp_path / "said"
    serving = sh.Command(sys.executable)(
        "-m",
        "lup.devtools.dashboard.service",
        *arguments.words(),
        _bg=True,
        _bg_exc=False,
        _new_session=True,
        _out=str(said),
        _err_to_out=True,
    )
    authorized = {"Authorization": f"Bearer {token}"}

    before = health(url, token)
    first = running_since(url, token)
    with httpx.stream(
        "GET", f"{url}/api/stream", headers=authorized, timeout=60, trust_env=False
    ) as following:
        lines = following.iter_lines()
        next(line for line in lines if line.startswith("data: "))
        asked = httpx.post(
            f"{url}/api/service/restart",
            headers={**authorized, "Origin": url},
            json={},
            trust_env=False,
        )
        for _ in lines:
            continue
    after = health(url, token)
    second = running_since(url, token)
    with httpx.stream(
        "GET", f"{url}/api/stream", headers=authorized, timeout=60, trust_env=False
    ) as following:
        lines = following.iter_lines()
        next(line for line in lines if line.startswith("data: "))
        began = time.monotonic()
        serving.signal(signal.SIGINT)
        for _ in lines:
            continue
        with pytest.raises(sh.ErrorReturnCode) as stopped:
            serving.wait()
        took = time.monotonic() - began
    output = said.read_text()

    assert asked.status_code == 202
    assert after.pid == before.pid == serving.pid
    assert first is not None and second is not None and second > first
    assert took < 1.5, output
    assert stopped.value.exit_code == 130
    assert "ERROR" not in output and "Traceback" not in output, output
    assert not state.exists()
