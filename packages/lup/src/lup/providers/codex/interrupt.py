"""Interrupting a Codex thread's running turn, through the app-server its home runs.

Codex's queue takes a message for a thread's next turn and nothing sooner. Its
app-server answers `turn/interrupt {threadId, turnId}` for the turn running
now: measured on Codex 0.159.2 against an inert local model endpoint, a turn
running a thirty-second command ended `interrupted` within ten milliseconds of
the request — the command's own process was left to finish on its own.

The app-server a session's thread lives in is the one its configuration home
runs, listening on that home's control socket with WebSocket framing, so this
reaches it the way `codex queue` does: by the home a session's row recorded,
from the execution scope it recorded, which the caller checks.
"""

import asyncio
from pathlib import Path

from pydantic import BaseModel

from lup.providers.codex.app_server import RpcMessage, RpcNotification, RpcRequest
from lup.providers.interrupts import Interrupted
from lup.types import JsonObject, JsonValue

CONTROL_SOCKET = Path("app-server-control") / "app-server-control.sock"
"""Where beneath a configuration home its app-server listens for clients."""


class CodexTurn(BaseModel, frozen=True, extra="ignore"):
    """One turn of a thread, as `thread/turns/list` lists it."""

    id: str
    status: str


class CodexTurns(BaseModel, frozen=True, extra="ignore"):
    data: list[CodexTurn] = []


async def codex_interrupted_turn(
    home: Path,
    thread: str,
    timeout: float = 10.0,
    client: str = "lup",
    control: Path = CONTROL_SOCKET,
) -> Interrupted:
    """Stop *thread*'s running turn in the app-server *home* runs, where one runs.

    *control* is where beneath the home that app-server listens. Never raises
    on a failed interrupt: a home whose app-server is not listening, a thread
    with no turn running, a refusal — each is said, and the caller's message
    is queued for the next turn either way.
    """
    socket = home / control
    if not socket.is_socket():
        return Interrupted(
            interrupted=False, reason=f"no Codex app-server listens at {socket}"
        )
    try:
        from websockets.asyncio.client import unix_connect
    except ImportError:
        return Interrupted(
            interrupted=False,
            reason=(
                "the websockets package this needs to reach Codex's app-server "
                "is not installed"
            ),
        )
    try:
        async with asyncio.timeout(timeout), unix_connect(str(socket)) as connection:

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
                    if message.id != identifier:
                        continue
                    if message.error is not None:
                        raise LookupError(message.error.message)
                    return message.result
                raise ConnectionError("the app-server closed the connection")

            await called(
                1, "initialize", {"clientInfo": {"name": client, "version": "1"}}
            )
            await connection.send(
                RpcNotification(method="initialized").model_dump_json()
            )
            listed = CodexTurns.model_validate(
                await called(
                    2,
                    "thread/turns/list",
                    {"threadId": thread, "sortDirection": "desc", "limit": 1},
                )
            )
            running = next(
                (turn for turn in listed.data if turn.status == "inProgress"), None
            )
            if running is None:
                return Interrupted(
                    interrupted=False, reason="no turn of it was running"
                )
            await called(
                3, "turn/interrupt", {"threadId": thread, "turnId": running.id}
            )
            return Interrupted(interrupted=True, turn=running.id)
    except (TimeoutError, OSError, LookupError, ConnectionError) as failed:
        return Interrupted(
            interrupted=False,
            reason=f"Codex's app-server did not stop its turn: {failed}",
        )
