"""Codex app-server JSON-RPC routing, and the legs that hold a real child.

Every Codex turn — and every native approval request the policy layer
answers — rides this transport. These tests drive the wire queues
directly: responses must resolve exactly their pending request, error
envelopes must surface as :class:`AppServerError`, server-initiated
requests must always receive a reply (success, failure, or
no-handler-installed), notifications must reach the installed handler,
and a disconnect must fail every pending future rather than hang them.

Those run with nothing attached. The legs that exist only because a child
does — spawning it, the `initialize`/`initialized` exchange, waking the reader
when it dies, terminating it on close, unwinding a start that failed halfway —
run against the scripted child in :mod:`tests.unit.doubles`, which speaks the
same framing without an installed `codex` and can be told to fail on purpose.
"""

import asyncio
import json
import os
import threading

from pathlib import Path

import pytest
import sh

from pydantic import ValidationError

from lup.providers.codex.app_server import (
    AppServerError,
    CodexAppServer,
    RpcError,
    RpcMessage,
    RpcNotification,
)
from lup.types import JsonObject
from tests.unit.doubles import (
    AppServerRecord,
    AppServerTranscript,
    ChildEnvironment,
    ChildExit,
    FakeAppServer,
    ScriptedReply,
    alive,
)

SPAWN_TIMEOUT = 30.0
"""How long a scripted child gets before the suite calls the transport hung."""


def sent_lines(server: CodexAppServer) -> list[JsonObject]:
    """Decode every JSON-RPC line the server wrote to its stdin queue."""
    lines: list[JsonObject] = []  # lup: ignore[empty-collection] — queue drain
    while not server.input.empty():
        line = server.input.get_nowait()
        if line is not None:
            lines.append(json.loads(line))
    return lines


async def test_response_resolves_exactly_its_pending_request() -> None:
    server = CodexAppServer(Path("codex"))
    reader = asyncio.create_task(server.read_messages())
    first = asyncio.create_task(server.request("thread/start", {}))
    second = asyncio.create_task(server.request("thread/resume", {}))
    await asyncio.sleep(0)

    server.output.put_nowait(json.dumps({"id": 2, "result": {"thread": "resumed"}}))
    server.output.put_nowait(json.dumps({"id": 1, "result": {"thread": "started"}}))

    assert await first == {"thread": "started"}
    assert await second == {"thread": "resumed"}
    assert server.pending == {}
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader


async def test_error_envelope_surfaces_as_typed_app_server_error() -> None:
    server = CodexAppServer(Path("codex"))
    reader = asyncio.create_task(server.read_messages())
    request = asyncio.create_task(server.request("turn/start", {}))
    await asyncio.sleep(0)

    server.output.put_nowait(
        json.dumps({"id": 1, "error": {"code": -32600, "message": "bad turn"}})
    )

    with pytest.raises(AppServerError) as raised:
        await request
    assert raised.value.error.code == -32600
    assert "bad turn" in str(raised.value)
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader


async def test_server_request_round_trips_through_the_installed_handler() -> None:
    server = CodexAppServer(Path("codex"))
    seen: list[JsonObject] = []

    async def approve(message: RpcMessage) -> str:
        seen.append(message.params)
        return "approved"

    server.server_request_handler = approve
    reader = asyncio.create_task(server.read_messages())
    server.output.put_nowait(
        json.dumps(
            {"id": 9, "method": "execCommandApproval", "params": {"command": "ls"}}
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert seen == [{"command": "ls"}]
    assert sent_lines(server) == [{"id": 9, "result": "approved"}]
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader


async def test_server_request_without_handler_is_refused_not_dropped() -> None:
    server = CodexAppServer(Path("codex"))
    reader = asyncio.create_task(server.read_messages())
    server.output.put_nowait(
        json.dumps({"id": 4, "method": "applyPatchApproval", "params": {}})
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    replies = sent_lines(server)
    assert [reply["id"] for reply in replies] == [4]
    error = replies[0]["error"]
    assert isinstance(error, dict) and error["code"] == -32601
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader


async def test_raising_handler_returns_failure_so_the_native_side_never_hangs() -> None:
    server = CodexAppServer(Path("codex"))

    async def explode(message: RpcMessage) -> str:
        del message
        raise RuntimeError("policy backend offline")

    server.server_request_handler = explode
    reader = asyncio.create_task(server.read_messages())
    server.output.put_nowait(
        json.dumps({"id": 5, "method": "execCommandApproval", "params": {}})
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    replies = sent_lines(server)
    assert [reply["id"] for reply in replies] == [5]
    error = replies[0]["error"]
    assert isinstance(error, dict) and error["code"] == -32000
    assert error["message"] == "policy backend offline"
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader


async def test_notifications_reach_the_installed_handler_in_order() -> None:
    server = CodexAppServer(Path("codex"))
    received: list[RpcNotification] = []
    server.notification_handler = received.append
    reader = asyncio.create_task(server.read_messages())

    server.output.put_nowait(
        json.dumps({"method": "turn/started", "params": {"turn": "t1"}})
    )
    server.output.put_nowait(
        json.dumps({"method": "turn/completed", "params": {"turn": "t1"}})
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert [item.method for item in received] == ["turn/started", "turn/completed"]
    assert received[0].params == {"turn": "t1"}
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader


async def test_close_cancels_and_awaits_connection_owned_handlers() -> None:
    server = CodexAppServer(Path("codex"))
    entered = asyncio.Event()
    canceled = asyncio.Event()

    async def pending() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    server.spawn_handler(pending())
    await entered.wait()
    assert len(server.handlers) == 1
    await server.close()
    assert canceled.is_set()
    assert not server.handlers


async def test_owned_notification_failure_fails_the_active_connection() -> None:
    server = CodexAppServer(Path("codex"))
    failures: list[Exception] = []
    server.disconnect_handler = failures.append

    async def failing() -> None:
        raise RuntimeError("context delivery refused")

    server.spawn_handler(failing())
    await asyncio.gather(*server.handlers)
    assert [str(error) for error in failures] == ["context delivery refused"]
    with pytest.raises(RuntimeError, match="context delivery refused"):
        await server.request("turn/start", {})
    await server.close()


async def test_response_to_a_canceled_request_does_not_kill_the_connection() -> None:
    server = CodexAppServer(Path("codex"))
    pending = asyncio.get_running_loop().create_future()
    server.pending[3] = pending
    pending.cancel()
    await server.resolve_response(RpcMessage(id=3, result={}))
    assert server.pending == {}


async def test_disconnect_fails_pending_requests_and_notifies_the_handler() -> None:
    server = CodexAppServer(Path("codex"))
    disconnects: list[Exception] = []
    server.disconnect_handler = disconnects.append
    reader = asyncio.create_task(server.read_messages())
    request = asyncio.create_task(server.request("turn/start", {}))
    await asyncio.sleep(0)

    server.output.put_nowait(None)

    with pytest.raises(RuntimeError, match="exited before completing"):
        await request
    with pytest.raises(RuntimeError):
        await reader
    assert server.pending == {}
    assert len(disconnects) == 1


async def test_request_after_idle_disconnect_fails_fast_instead_of_hanging() -> None:
    """A death with no request in flight must still fail the next request.

    The resolver sits idle between turns; if the app-server dies there, a
    later ``request`` would otherwise enqueue into a dead stdin queue and
    await a future nobody resolves.
    """
    server = CodexAppServer(Path("codex"))
    server.exit_error = RuntimeError("Codex app-server exited with status 1: boom")
    reader = asyncio.create_task(server.read_messages())
    await asyncio.sleep(0)

    server.output.put_nowait(None)

    with pytest.raises(RuntimeError, match="boom"):
        await reader
    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.wait_for(server.request("thread/start", {}), timeout=1)


async def test_start_hands_the_child_its_launch_and_completes_the_handshake(
    fake_app_server: FakeAppServer,
) -> None:
    """Everything `start` composes is only observable from inside the child."""
    ready = asyncio.Event()
    server = fake_app_server.attach(
        AppServerTranscript(
            replies={
                "initialize": ScriptedReply(result={"userAgent": "fake"}),
                "initialized": ScriptedReply(
                    notifications=[RpcNotification(method="fake/ready")]
                ),
            }
        ),
        arguments=["--profile", "smoke"],
        environment={"LUP_FAKE_DECLARED": "overlaid"},
    )
    server.notification_handler = lambda _notification: ready.set()

    await asyncio.wait_for(server.start(), timeout=SPAWN_TIMEOUT)
    await asyncio.wait_for(ready.wait(), timeout=SPAWN_TIMEOUT)
    child = fake_app_server.observed()

    assert child.arguments[-3:] == ["--profile", "smoke", "app-server"]
    assert child.environment.lup_fake_declared == "overlaid"
    # The ambient half of the overlay: nothing here declares PATH.
    assert child.environment.path
    assert [message.method for message in child.received] == [
        "initialize",
        "initialized",
    ]
    client = child.received[0].params["clientInfo"]
    assert isinstance(client, dict) and client["name"] == "lup"
    assert child.received[0].params["capabilities"] == {"experimentalApi": True}
    assert server.reader is not None and not server.reader.done()
    assert server.watcher is not None and not server.watcher.done()
    await asyncio.wait_for(server.close(), timeout=SPAWN_TIMEOUT)


async def test_initialized_is_withheld_until_initialize_resolves(
    fake_app_server: FakeAppServer,
) -> None:
    """A server that accepts the connection and answers nothing gets no follow-up.

    Ordering is the claim, and a recorded pair proves only that stdin has an
    order. A child that has demonstrably heard `initialize` and has still not
    heard `initialized` is the same claim with the reply withheld.
    """
    heard = asyncio.Event()
    server = fake_app_server.attach(
        AppServerTranscript(
            replies={
                "initialize": ScriptedReply(
                    notifications=[RpcNotification(method="fake/heard")], silent=True
                )
            }
        )
    )
    server.notification_handler = lambda _notification: heard.set()
    starting = asyncio.create_task(server.start())

    await asyncio.wait_for(heard.wait(), timeout=SPAWN_TIMEOUT)

    assert not starting.done()
    child = fake_app_server.observed()
    assert [message.method for message in child.received] == ["initialize"]
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    await asyncio.wait_for(server.close(), timeout=SPAWN_TIMEOUT)


async def test_an_initialize_error_leaves_nothing_attached(
    fake_app_server: FakeAppServer,
) -> None:
    """A refused handshake unwinds rather than raising over a live child."""
    server = fake_app_server.attach(
        AppServerTranscript(
            replies={
                "initialize": ScriptedReply(
                    error=RpcError(code=-32001, message="no experimental api")
                )
            }
        )
    )

    with pytest.raises(AppServerError, match="no experimental api"):
        await asyncio.wait_for(server.start(), timeout=SPAWN_TIMEOUT)

    assert server.reader is None
    assert server.watcher is None
    assert server.process is None
    assert not alive(fake_app_server.observed().pid)


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
async def test_a_child_that_dies_during_initialize_leaves_nothing_attached(
    fake_app_server: FakeAppServer,
) -> None:
    """The same unwind when the child never answers because it is gone.

    The escalated thread warning pins the watcher as the child's only
    reaper: were sh's own background thread allowed to re-raise the exit
    it would surface here as an unhandled thread exception, recording the
    same death twice.
    """
    server = fake_app_server.attach(
        AppServerTranscript(
            replies={
                "initialize": ScriptedReply(
                    exits=ChildExit(status=3, stderr="fake app-server refused")
                )
            }
        )
    )

    with pytest.raises(RuntimeError, match="exited with status 3"):
        await asyncio.wait_for(server.start(), timeout=SPAWN_TIMEOUT)

    assert server.reader is None
    assert server.watcher is None
    assert server.process is None
    assert not alive(fake_app_server.observed().pid)


async def test_close_unwinds_the_reader_the_child_and_every_pending_request(
    fake_app_server: FakeAppServer,
) -> None:
    """Close is the whole unwind, and closing an already-closed server is safe.

    The child lingers past its stdin so that terminating it is a real signal
    exit rather than a clean one already banked — that exit is this close's
    own doing, and treating it as a failure would make every close raise.
    """
    server = fake_app_server.attach(
        AppServerTranscript(
            replies={
                "initialize": ScriptedReply(result={"userAgent": "fake"}),
                "thread/start": ScriptedReply(silent=True),
            },
            lingers=True,
        )
    )
    await asyncio.wait_for(server.start(), timeout=SPAWN_TIMEOUT)
    unanswered = asyncio.create_task(server.request("thread/start", {}))
    await asyncio.sleep(0)

    await asyncio.wait_for(server.close(), timeout=SPAWN_TIMEOUT)

    with pytest.raises(RuntimeError, match="connection closed"):
        await unanswered
    assert server.pending == {}
    assert server.connection_error is not None
    assert server.reader is None and server.watcher is None and server.process is None
    assert not alive(fake_app_server.observed().pid)
    # The signal exit is this close's own doing, so it is never reported as a
    # death, and the reader is still woken rather than left on a dead child.
    assert server.exit_error is None
    assert None in [server.output.get_nowait() for _ in range(server.output.qsize())]
    await asyncio.wait_for(server.close(), timeout=SPAWN_TIMEOUT)


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
async def test_a_child_that_dies_while_open_wakes_the_reader_with_its_stderr(
    fake_app_server: FakeAppServer,
) -> None:
    """An unexpected exit reaches whoever waited, carrying the child's own words.

    The watcher is what reaps the child, so the signal `close` sends afterwards
    lands on an id no process answers to. Awaiting the watcher states that
    ordering here rather than leaving it inferred from when the reader woke.
    Closing a server whose child already ended is the ordinary shape, and it
    must still report the death rather than the missing process.
    """
    server = fake_app_server.attach(
        AppServerTranscript(
            replies={
                "initialize": ScriptedReply(result={"userAgent": "fake"}),
                "turn/start": ScriptedReply(
                    exits=ChildExit(status=7, stderr="fake app-server crashed")
                ),
            }
        )
    )
    await asyncio.wait_for(server.start(), timeout=SPAWN_TIMEOUT)
    watcher = server.watcher
    assert watcher is not None

    with pytest.raises(RuntimeError, match="fake app-server crashed"):
        await asyncio.wait_for(server.request("turn/start", {}), timeout=SPAWN_TIMEOUT)

    assert server.exit_error is not None
    assert "exited with status 7" in str(server.exit_error)
    assert server.reader is not None and server.reader.done()
    await asyncio.wait_for(watcher, timeout=SPAWN_TIMEOUT)
    assert not alive(fake_app_server.observed().pid)
    with pytest.raises(RuntimeError, match="exited with status 7"):
        await asyncio.wait_for(server.close(), timeout=SPAWN_TIMEOUT)


async def test_the_attached_lifecycle_needs_no_codex_on_path(
    fake_app_server: FakeAppServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The offline claim, stated rather than inherited from whoever runs this.

    Every other test here would pass unchanged on a machine that happens to
    have `codex` installed, so none of them can tell the difference. Emptying
    `PATH` removes the possibility: what is left to find the child with is the
    interpreter's own absolute path, which is all the fixture ever used.
    """
    ready = asyncio.Event()
    monkeypatch.setenv("PATH", "")
    with pytest.raises(sh.CommandNotFound):
        sh.Command("codex")
    server = fake_app_server.attach(
        AppServerTranscript(
            replies={
                "initialize": ScriptedReply(result={"userAgent": "fake"}),
                "initialized": ScriptedReply(
                    notifications=[RpcNotification(method="fake/ready")]
                ),
            }
        )
    )
    server.notification_handler = lambda _notification: ready.set()

    await asyncio.wait_for(server.start(), timeout=SPAWN_TIMEOUT)
    await asyncio.wait_for(ready.wait(), timeout=SPAWN_TIMEOUT)

    assert [message.method for message in fake_app_server.observed().received] == [
        "initialize",
        "initialized",
    ]
    await asyncio.wait_for(server.close(), timeout=SPAWN_TIMEOUT)


def test_a_record_stays_readable_while_the_child_rewrites_it(tmp_path: Path) -> None:
    """A read racing a rewrite gets a whole record, never half of one.

    The child rewrites its record on every message it receives, and the
    assertions about a terminated child read that file the moment the child is
    killed — so reads land mid-rewrite by construction. A rewrite that
    truncated in place would hand those reads an empty file, surfacing as a
    JSON error naming this record rather than the leg the test was driving.
    Renaming onto the target instead removes the window, so every read here
    parses whichever rewrite it raced.
    """
    record = tmp_path / "record.json"
    start = AppServerRecord(
        pid=os.getpid(), arguments=["fake"], environment=ChildEnvironment()
    )
    start.report_to(record)
    rewrites = 400
    unreadable: list[str] = []  # lup: ignore[empty-collection] — reader's findings

    def rewrite_repeatedly() -> None:
        growing = start
        for turn in range(rewrites):
            growing = growing.model_copy(
                update={
                    "received": [
                        *growing.received,
                        RpcMessage(id=turn, method="turn/start"),
                    ]
                }
            )
            growing.report_to(record)

    writer = threading.Thread(target=rewrite_repeatedly)
    writer.start()
    # For as long as the rewrites run and no longer: a read after the last
    # one races nothing, and a fixed count spent most of its reads there.
    for _read in iter(writer.is_alive, False):
        try:
            AppServerRecord.model_validate_json(record.read_text(encoding="utf-8"))
        except ValidationError as torn:
            unreadable.append(str(torn))
    writer.join()

    assert unreadable == []
