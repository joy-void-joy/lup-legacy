"""The dashboard moves onto its checkout's newer code, in place, and says so until it has.

It keeps the code it imported, while the checkout it imported it from moves
every time something lands. So it watches those files: once they have
changed and settled, and the new code starts, it restarts itself once no
write is in flight; until then it tells the page, the status line and every
refused write what is happening, rather than refusing an answer as though a
review had been altered.
"""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from lup.devtools.dashboard.pulse import DashboardPulse, RunningCode
from lup.devtools.dashboard.refresh import ImportedSource, Refresh, WriteGate
from lup.devtools.dashboard.reviews import ReviewStore
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
