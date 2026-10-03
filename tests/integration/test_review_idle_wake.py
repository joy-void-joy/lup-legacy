"""A browser answer wakes an idle Codex thread through its local mailbox relay."""

import asyncio
import json
import os
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

import pytest
import sh
import tomlkit
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from websockets.asyncio.client import unix_connect

import lup.coordination.bare.arrival as arrival
import lup.providers.codex.wake as routing
from lup.channels.wait import wait_until
from lup.coordination.bare import store
from lup.coordination.identity import MEMBER_ENV, NAME_ENV, member_ref
from lup.coordination.relay import MailboxRelay
from lup.coordination.repository import RepositoryPeers
from lup.coordination.wake import WakePath
from lup.devtools.dashboard import reviews as dashboard
from lup.devtools.dashboard.reviews import ReviewDecision, ReviewSnapshot
from lup.devtools.review import app as review
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion
from lup.providers.codex.app_server import (
    AppServerError,
    RpcMessage,
    RpcNotification,
    RpcRequest,
)
from lup.types import JsonObject, JsonValue
from lup.harness.environment import inherited, tool_server_env
from lup.mcp import Coordination
from tests.unit.native import bound

pytestmark = pytest.mark.integration
BASE_URL: Final = "http://127.0.0.1:8765"
TOKEN: Final = "isolated-idle-wake-operator"
NOTE: Final = "Operator review delivery nonce: 92a1-idle-wake."


class StartedThread(BaseModel):
    class Thread(BaseModel):
        id: str

    thread: Thread


@pytest.fixture
def native_home() -> Iterator[Path]:
    """The native Unix socket must fit its operating system's path limit."""
    with TemporaryDirectory(prefix="lup-review-") as directory:
        yield Path(directory)


async def test_browser_answer_starts_an_idle_codex_turn_through_the_relay(
    tmp_path: Path, native_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = native_home
    root = tmp_path / "checkout"
    sh.git("init", "--quiet", str(root))
    requests: list[JsonObject] = []
    peers = RepositoryPeers(root)
    peers.join("recipient", root, wake=WakePath(runtime="codex"))
    forwarded = Coordination().launcher_variables()
    assert forwarded == tool_server_env()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            item: JsonObject = {
                "id": f"message_{len(requests)}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "The inert turn is complete.",
                        "annotations": [],
                    }
                ],
            }
            response: JsonObject = {
                "id": f"response_{len(requests)}",
                "object": "response",
                "status": "completed",
                "output": [item],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }
            events: list[JsonObject] = [
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            body = "".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                for event in events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            """The captured request bodies are the transport's evidence."""

    endpoint = HTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=endpoint.serve_forever, daemon=True)
    serving.start()
    native = None
    try:
        (home / "config.toml").write_text(
            tomlkit.dumps(
                {
                    "model": "gpt-6-astra",
                    "model_provider": "lup_inert_review",
                    "model_providers": {
                        "lup_inert_review": {
                            "name": "Inert local review fixture",
                            "base_url": f"http://127.0.0.1:{endpoint.server_port}/v1",
                            "wire_api": "responses",
                            "requires_openai_auth": False,
                            "supports_websockets": False,
                        }
                    },
                    "features": {
                        "enable_request_compression": False,
                        "code_mode": False,
                        "code_mode_host": False,
                        "code_mode_only": False,
                    },
                    "approval_policy": "never",
                    "sandbox_mode": "read-only",
                    "notify": [],
                    "mcp_servers": {
                        "coordination": {
                            "command": sys.executable,
                            "args": [
                                str(Path(__file__).with_name("coordination_server.py"))
                            ],
                            "cwd": str(root),
                            "env_vars": forwarded,
                            "startup_timeout_sec": 10,
                            "required": True,
                        }
                    },
                }
            )
        )
        async with asyncio.timeout(35):
            native = await asyncio.create_subprocess_exec(
                "codex",
                "app-server",
                "--listen",
                "unix://",
                env=inherited(
                    {
                        "CODEX_HOME": str(home),
                        MEMBER_ENV: "recipient",
                        NAME_ENV: "idle-review-recipient",
                    }
                ),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            sockets: list[Path] = []
            for _ in range(100):
                sockets = [path for path in home.rglob("*") if path.is_socket()]
                if sockets:
                    break
                await asyncio.sleep(0.05)
            [socket] = sockets
            observed: list[RpcNotification] = []
            # Native Unix listeners use the documented WebSocket framing; the
            # protocol envelopes are the production client's typed models.
            async with unix_connect(str(socket)) as connection:

                def notification(message: RpcMessage) -> None:
                    assert message.id is None and message.method is not None
                    observed.append(
                        RpcNotification(method=message.method, params=message.params)
                    )

                async def request(
                    identifier: int, method: str, params: JsonObject
                ) -> JsonValue:
                    await connection.send(
                        RpcRequest(
                            id=identifier, method=method, params=params
                        ).model_dump_json()
                    )
                    async for payload in connection:
                        message = RpcMessage.model_validate_json(payload)
                        if message.id == identifier:
                            if message.error is not None:
                                raise AppServerError(message.error)
                            return message.result
                        notification(message)
                    raise AssertionError("Native connection closed before its reply")

                async def completed(count: int) -> None:
                    def finished() -> bool:
                        return (
                            sum(event.method == "turn/completed" for event in observed)
                            >= count
                        )

                    if finished():
                        return
                    async for payload in connection:
                        notification(RpcMessage.model_validate_json(payload))
                        if finished():
                            return
                    raise AssertionError("Native connection closed before completion")

                await request(
                    1,
                    "initialize",
                    {"clientInfo": {"name": "lup-idle-wake-test", "version": "1"}},
                )
                await connection.send(
                    RpcNotification(method="initialized").model_dump_json()
                )
                started = StartedThread.model_validate(
                    await request(
                        2,
                        "thread/start",
                        {
                            "model": "gpt-6-astra",
                            "modelProvider": "lup_inert_review",
                            "cwd": str(root),
                            "approvalPolicy": "never",
                            "sandbox": "read-only",
                        },
                    )
                )
                await request(
                    3,
                    "turn/start",
                    {
                        "threadId": started.thread.id,
                        "input": [{"type": "text", "text": "Complete an inert turn."}],
                    },
                )
                await completed(1)
                assert len(requests) == 1
                assert NOTE not in json.dumps(requests[0])
                assert arrival.bind(
                    peers.root,
                    "recipient",
                    "codex",
                    arrival.Arrival(
                        session_id=started.thread.id,
                        hook_event_name="SessionStart",
                        cwd=str(root),
                    ),
                    ("SessionStart",),
                    str(home),
                )
                member_path = store.member_path(
                    peers.root,
                    {"kind": member_ref("recipient").kind, "id": "recipient"},
                )
                old_stamp = member_path.stat().st_mtime - store.STALE_AFTER_SECONDS - 10
                os.utime(member_path, (old_stamp, old_stamp))
                for _ in range(100):
                    if (
                        member_path.is_file()
                        and member_path.stat().st_mtime > old_stamp
                    ):
                        break
                    await asyncio.sleep(0.02)
                assert member_path.stat().st_mtime > old_stamp
                [live] = [member for member in peers.present() if member.running]
                assert live.actor.id == "recipient"
                assert live.wake.session == started.thread.id
                target = root / "must-not-execute"
                operation = Operation(
                    id="idle-review-operation",
                    requester="recipient",
                    session=started.thread.id,
                    tool="Bash",
                    payload={"command": f"touch {target}"},
                    cwd=root,
                    worktree=root,
                )
                entry = review.relay(root).record(
                    bound(
                        PersistentQuestion(
                            id="idle-review-question",
                            operation=operation,
                            fingerprint="",
                            reason="The operator reviews the command before its retry.",
                            rule="shell:test",
                            eligible=["operator"],
                            resumption="native_retry",
                        )
                    )
                )
                with monkeypatch.context() as host:
                    host.setattr(routing, "execution_scope", lambda: "browser-host")
                    app = dashboard.dashboard_app(
                        BASE_URL, TOKEN, (root,), discover=False
                    )
                    async with AsyncClient(
                        transport=ASGITransport(app=app), base_url=BASE_URL
                    ) as http:
                        listed = await http.get(
                            "/api/reviews",
                            headers={"Authorization": f"Bearer {TOKEN}"},
                        )
                        assert listed.status_code == 200
                        [summary] = ReviewSnapshot.model_validate(listed.json()).reviews
                        response = await http.post(
                            f"/api/reviews/{summary.key}/answer",
                            headers={
                                "Authorization": f"Bearer {TOKEN}",
                                "Origin": BASE_URL,
                            },
                            json={
                                "approved": True,
                                "note": NOTE,
                                "fingerprint": entry.fingerprint,
                            },
                        )
                assert response.status_code == 200
                decision = ReviewDecision.model_validate(response.json())
                assert (
                    not decision.notification.queued and not decision.notification.woken
                )
                outcome = (
                    dashboard.ReviewStore(roots=(root,))
                    .detail(summary.key)
                    .notification
                )
                assert outcome is not None and outcome.queued and not outcome.woken
                await completed(2)
                relay = MailboxRelay(root=root, member_id="recipient")

                assert len(requests) == 2
                assert NOTE in json.dumps(requests[1])
                assert entry.id in json.dumps(requests[1])
                assert sum(event.method == "turn/started" for event in observed) == 2
                # Handed over once the served relay's queue call returns, which
                # can be after the turn it started has completed.
                assert await wait_until(
                    lambda: True if not peers.waiting("recipient").messages else None,
                    wait_seconds=20,
                )
                assert relay.tick() is None
                assert review.relay(root).find(entry.id) == decision.review.question
                assert not target.exists()
    finally:
        try:
            if native is not None and native.returncode is None:
                native.terminate()
                try:
                    async with asyncio.timeout(5):
                        await native.wait()
                except TimeoutError:
                    native.kill()
                    await native.wait()
        finally:
            endpoint.shutdown()
            endpoint.server_close()
            serving.join(timeout=5)
            assert not serving.is_alive()
