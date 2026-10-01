"""A Codex thread's running turn stops when its home's app-server is asked to.

What the dashboard's interrupt rests on for Codex, asked of the installed CLI:
an app-server over a configuration home, the way a session's own runs, an
inert local model endpoint whose first answer is a call running a long
command, and lup's own client asking `turn/interrupt` once that command runs.
No account is used and no model is reached.
"""

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import tomlkit
from pydantic import BaseModel
from websockets.asyncio.client import unix_connect

from lup.providers.codex.app_server import (
    RpcMessage,
    RpcNotification,
    RpcRequest,
    native_environment,
)
from lup.providers.codex.interrupt import CONTROL_SOCKET, codex_interrupted_turn
from lup.types import JsonObject, JsonValue

pytestmark = pytest.mark.integration

MODEL = "gpt-5-codex"
"""A model Codex knows the tools of, so the inert endpoint is offered `exec_command`."""


class Started(BaseModel, extra="ignore"):
    class Identified(BaseModel, extra="ignore"):
        id: str

    thread: Identified | None = None
    turn: Identified | None = None


@pytest.fixture
def native_home() -> Iterator[Path]:
    """A configuration home short enough for its control socket's path limit."""
    with TemporaryDirectory(prefix="lup-int-") as directory:
        yield Path(directory)


async def test_a_running_turn_is_interrupted_and_an_idle_thread_is_said_to_be(
    native_home: Path, tmp_path: Path
) -> None:
    requests: list[JsonObject] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            item: JsonObject = (
                {
                    "id": "call_item_1",
                    "type": "function_call",
                    "status": "completed",
                    "name": "exec_command",
                    "call_id": "call_1",
                    "arguments": json.dumps({"cmd": "sleep 30"}),
                }
                if len(requests) == 1
                else {
                    "id": f"message_{len(requests)}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "Done.", "annotations": []}
                    ],
                }
            )
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
            data = "".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                for event in events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            """The captured request bodies are the transport's evidence."""

    endpoint = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=endpoint.serve_forever, daemon=True).start()
    (native_home / "config.toml").write_text(
        tomlkit.dumps(
            {
                "model": MODEL,
                "model_provider": "lup_inert_interrupt",
                "model_providers": {
                    "lup_inert_interrupt": {
                        "name": "Inert local interrupt fixture",
                        "base_url": f"http://127.0.0.1:{endpoint.server_port}/v1",
                        "wire_api": "responses",
                        "requires_openai_auth": False,
                        "supports_websockets": False,
                    }
                },
                "features": {"enable_request_compression": False, "code_mode": False},
                "approval_policy": "never",
                "sandbox_mode": "danger-full-access",
                "notify": [],
            }
        )
    )
    native = await asyncio.create_subprocess_exec(
        "codex",
        "app-server",
        "--listen",
        "unix://",
        env=native_environment({"CODEX_HOME": str(native_home)}),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(60):
            socket = native_home / CONTROL_SOCKET
            while not socket.is_socket():
                await asyncio.sleep(0.05)
            async with unix_connect(str(socket)) as connection:
                seen: list[RpcMessage] = []

                async def called(
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
                            assert message.error is None, message.error
                            return message.result
                        seen.append(message)
                    raise AssertionError("the app-server closed before answering")

                async def notified(method: str, mentioning: str = "") -> RpcMessage:
                    def wanted(message: RpcMessage) -> bool:
                        return message.method == method and mentioning in json.dumps(
                            message.params
                        )

                    found = next((each for each in seen if wanted(each)), None)
                    if found is not None:
                        return found
                    async for payload in connection:
                        message = RpcMessage.model_validate_json(payload)
                        seen.append(message)
                        if wanted(message):
                            return message
                    raise AssertionError(f"the app-server closed before {method}")

                await called(
                    1,
                    "initialize",
                    {"clientInfo": {"name": "lup-test", "version": "1"}},
                )
                await connection.send(
                    RpcNotification(method="initialized").model_dump_json()
                )
                thread = Started.model_validate(
                    await called(
                        2,
                        "thread/start",
                        {
                            "model": MODEL,
                            "modelProvider": "lup_inert_interrupt",
                            "cwd": str(tmp_path),
                            "approvalPolicy": "never",
                            "sandbox": "danger-full-access",
                        },
                    )
                ).thread
                assert thread is not None
                await called(
                    3,
                    "turn/start",
                    {
                        "threadId": thread.id,
                        "input": [{"type": "text", "text": "Run the command."}],
                    },
                )
                await notified("item/started", "sleep 30")

                stopped = await codex_interrupted_turn(native_home, thread.id)
                completed = await notified("turn/completed")
                idle = await codex_interrupted_turn(native_home, thread.id)
    finally:
        native.terminate()
        await native.wait()
        endpoint.shutdown()
        endpoint.server_close()

    assert stopped.interrupted, stopped.reason
    assert '"status": "interrupted"' in json.dumps(completed.params)
    assert not idle.interrupted and "no turn" in idle.reason
    assert len(requests) == 1
