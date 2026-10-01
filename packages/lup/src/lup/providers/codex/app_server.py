"""Typed JSON-RPC transport for the Codex app-server stdio boundary."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from queue import Queue

import sh
from pydantic import BaseModel

from lup.execution.threads import run_sync
from lup.harness.environment import inherited
from lup.types import EnvVars, JsonObject, JsonValue


class RpcError(BaseModel, frozen=True):
    code: int
    message: str
    data: JsonValue = None


class RpcMessage(BaseModel, frozen=True):
    """Validated envelope for responses, notifications, and server requests."""

    id: int | str | None = None
    method: str | None = None
    params: JsonObject = {}
    result: JsonValue = None
    error: RpcError | None = None


class RpcRequest(BaseModel, frozen=True):
    method: str
    id: int
    params: JsonObject


class RpcNotification(BaseModel, frozen=True):
    method: str
    params: JsonObject = {}


class RpcSuccess(BaseModel, frozen=True):
    id: int | str
    result: JsonValue


class RpcFailure(BaseModel, frozen=True):
    id: int | str
    error: RpcError


class AppServerError(RuntimeError):
    """A typed JSON-RPC error returned by app-server."""

    def __init__(self, error: RpcError) -> None:
        super().__init__(f"app-server error {error.code}: {error.message}")
        self.error = error


type ServerRequestHandler = Callable[[RpcMessage], Awaitable[JsonValue]]
type NotificationHandler = Callable[[RpcNotification], None]
type DisconnectHandler = Callable[[Exception], None]
type OutgoingRpcMessage = RpcRequest | RpcNotification | RpcSuccess | RpcFailure


def native_command(executable: Path, environment: EnvVars) -> sh.Command:
    """Resolve the executable in the same PATH its native process receives."""
    return sh.Command(str(executable), search_paths=os.get_exec_path(environment))


class CodexAppServer:
    """One initialized app-server process and routed JSON-RPC connection."""

    def __init__(
        self,
        executable: Path,
        *,
        arguments: list[str] | None = None,
        environment: EnvVars | None = None,
    ) -> None:
        self.executable = executable
        self.arguments = list(arguments or [])
        self.environment = dict(environment or {})
        self.input: Queue[str | None] = Queue()
        self.output: asyncio.Queue[str | None] = asyncio.Queue()
        self.stderr: list[str] = []
        self.pending: dict[int, asyncio.Future[JsonValue]] = {}
        self.next_id = 1
        self.process: sh.RunningCommand | None = None
        self.reader: asyncio.Task[None] | None = None
        self.watcher: asyncio.Task[None] | None = None
        # lup: ignore[set-shape] — task identities have no additional record
        self.handlers: set[asyncio.Task[None]] = set()
        self.closing = False
        self.exit_error: Exception | None = None
        self.connection_error: Exception | None = None
        self.server_request_handler: ServerRequestHandler | None = None
        self.notification_handler: NotificationHandler | None = None
        self.disconnect_handler: DisconnectHandler | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()

        def receive_output(line: str) -> None:
            loop.call_soon_threadsafe(self.output.put_nowait, line)

        def receive_error(line: str) -> None:
            self.stderr.append(line)

        environment = inherited(self.environment)
        command = native_command(self.executable, environment)
        running = command(
            *self.arguments,
            "app-server",
            _in=self.input,
            _out=receive_output,
            _err=receive_error,
            _bg=True,
            _bg_exc=False,
            _encoding="utf-8",
            _env=environment,
        )
        if not isinstance(running, sh.RunningCommand):
            raise RuntimeError("Codex app-server did not start as a background process")
        self.process = running
        self.reader = asyncio.create_task(self.read_messages())
        self.watcher = asyncio.create_task(self.watch_process(running))
        try:
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "lup",
                        "title": "Lup",
                        "version": "0.2.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
        except (Exception, asyncio.CancelledError):
            await self.close()
            raise
        self.notify("initialized", {})

    async def close(self) -> None:
        close_error: Exception | None = None
        self.closing = True
        self.input.put(None)
        reader = self.reader
        self.reader = None
        if reader is not None:
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                reader = None
            except Exception as error:
                close_error = error
        for handler in self.handlers:
            handler.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)
        process = self.process
        self.process = None
        if process is not None:
            try:
                process.terminate()
            except ProcessLookupError:
                # A child that already exited is shut down, not a failed close.
                pass
            except Exception as error:
                close_error = close_error or error
        watcher = self.watcher
        self.watcher = None
        if watcher is not None:
            try:
                await watcher
            except asyncio.CancelledError:
                watcher = None
            except Exception as error:
                close_error = close_error or error
        if self.connection_error is None:
            self.connection_error = RuntimeError("app-server connection closed")
        for future in self.pending.values():
            if not future.done():
                future.set_exception(RuntimeError("app-server connection closed"))
        self.pending.clear()
        if close_error is not None:
            raise close_error

    async def watch_process(self, process: sh.RunningCommand) -> None:
        """Wake the JSON-RPC reader when the native process exits or reaches EOF."""
        try:
            await run_sync(process.wait)
        except sh.ErrorReturnCode as error:
            if not self.closing:
                stderr = "".join(self.stderr).strip()
                detail = f": {stderr}" if stderr else ""
                self.exit_error = RuntimeError(
                    f"Codex app-server exited with status {error.exit_code}{detail}"
                )
        except Exception as error:
            if not self.closing:
                self.exit_error = error
        finally:
            self.output.put_nowait(None)

    def send(self, message: OutgoingRpcMessage) -> None:
        encoded = message.model_dump_json(by_alias=True, exclude_none=True)
        self.input.put(encoded + "\n")

    async def request(self, method: str, params: JsonObject) -> JsonValue:
        if self.connection_error is not None:
            raise RuntimeError(
                f"Codex app-server connection failed: {self.connection_error}"
            ) from self.connection_error
        request_id = self.next_id
        self.next_id += 1
        future: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.send(RpcRequest(method=method, id=request_id, params=params))
        try:
            return await future
        finally:
            if self.pending.get(request_id) is future:
                self.pending.pop(request_id)

    def notify(self, method: str, params: JsonObject) -> None:
        self.send(RpcNotification(method=method, params=params))

    async def read_messages(self) -> None:
        try:
            while True:
                line = await self.output.get()
                if line is None:
                    if self.closing:
                        return
                    raise self.exit_error or RuntimeError(
                        "Codex app-server exited before completing the connection"
                    )
                message = RpcMessage.model_validate_json(line)
                match message.id, message.method:
                    case None, None:
                        pass
                    case _, None:
                        await self.resolve_response(message)
                    case None, str() as method:
                        if self.notification_handler is not None:
                            self.notification_handler(
                                RpcNotification(method=method, params=message.params)
                            )
                    case _:
                        self.spawn_handler(self.resolve_server_request(message))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.fail_connection(error)
            raise

    def fail_connection(self, error: Exception) -> None:
        """Fail every pending consumer when connection-owned work fails."""
        self.connection_error = error
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        self.pending.clear()
        if self.disconnect_handler is not None:
            self.disconnect_handler(error)

    def spawn_handler(self, awaitable: Awaitable[None]) -> asyncio.Task[None]:
        """Own asynchronous request and notification work until connection close."""

        async def handle() -> None:
            try:
                await awaitable
            except Exception as error:
                self.fail_connection(error)

        task = asyncio.create_task(handle())
        self.handlers.add(task)
        task.add_done_callback(self.handlers.discard)
        return task

    async def resolve_response(self, message: RpcMessage) -> None:
        if not isinstance(message.id, int):
            return
        future = self.pending.pop(message.id, None)
        if future is None or future.done():
            return
        if message.error is not None:
            future.set_exception(AppServerError(message.error))
        else:
            future.set_result(message.result)

    async def resolve_server_request(self, message: RpcMessage) -> None:
        identifier = message.id
        if identifier is None:
            return
        if self.server_request_handler is None:
            self.send(
                RpcFailure(
                    id=identifier,
                    error=RpcError(code=-32601, message="no client handler installed"),
                )
            )
            return
        try:
            result = await self.server_request_handler(message)
            self.send(RpcSuccess(id=identifier, result=result))
        except Exception as error:
            self.send(
                RpcFailure(
                    id=identifier,
                    error=RpcError(code=-32000, message=str(error)),
                )
            )
