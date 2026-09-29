"""One language-server session over stdio, answering arbitrary requests.

A language server resolves names the way the language does — through imports,
aliases, and re-exports — which is the difference between finding a symbol and
finding characters that look like one. The protocol is the same for every
question worth asking it, so the framing, the request/response pairing, and
the document bookkeeping are written once here and each caller supplies the
method it wants.

Nothing here decides which server to run: the executable and the workspace
root are supplied, so a project that type-checks with something else answers
these questions with something else. Failures are the caller's to interpret —
a sweep that can degrade to a weaker verdict and a tool that must report the
truth to an agent want opposite things from a server that will not start.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from email.parser import BytesParser
from pathlib import Path

from pydantic import BaseModel

from lup.types import JsonObject, JsonValue


def utf16_column(line: str, column: int) -> int:
    """Convert a UTF-8 byte offset into the UTF-16 offset LSP positions use."""
    prefix = line.encode("utf-8")[:column].decode("utf-8", errors="ignore")
    return len(prefix.encode("utf-16-le")) // 2


class Call(BaseModel, frozen=True):
    """One question in a batch: a method to ask, and the params to ask it with."""

    method: str
    params: JsonObject


class Answer(BaseModel, frozen=True):
    """One reply, carrying the position of the call it answers.

    The position travels with the result because a batch is answered in
    whatever order the server finishes, and the caller asked in its own.
    """

    offset: int
    result: JsonValue


class LspSession:
    """An initialized language server, and the documents opened against it."""

    def __init__(
        self,
        stdin: asyncio.StreamWriter,
        stdout: asyncio.StreamReader,
        name: str,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.name = name
        self.asked = 0
        self.opened: dict[str, list[str]] = {}

    async def notify(self, method: str, params: JsonObject) -> None:
        """Send a notification, which the protocol never answers."""
        await self.frame({"jsonrpc": "2.0", "method": method, "params": params})

    async def frame(self, body: JsonObject) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.stdin.write(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
        await self.stdin.drain()

    async def receive(self) -> JsonObject:
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            chunk = await self.stdout.readline()
            if not chunk:
                raise OSError(f"{self.name} closed its output stream")
            header.extend(chunk)
        declared = BytesParser().parsebytes(bytes(header))["Content-Length"]
        if declared is None:
            raise OSError(f"{self.name} sent a frame with no Content-Length")
        message: JsonObject = json.loads(await self.stdout.readexactly(int(declared)))
        return message

    async def requests(self, calls: list[Call]) -> list[JsonValue]:
        """Ask many questions at once, and answer them in the order asked.

        Every frame goes out before the first reply is read, so the questions
        queue inside the server rather than in the round trip. Asked one at a
        time, a repository sweep pays a full round trip per site and leaves
        the server idle across each one; the protocol carries the pairing, so
        which answer arrives first is the server's business rather than the
        caller's.

        A server interleaves diagnostics, progress, and requests of its own
        with the replies it owes. A response carries an id and no method,
        which is what separates the answer to our third question from the
        server asking its own third — with a batch in flight, both ids exist.
        """
        first = self.asked + 1
        self.asked += len(calls)
        pending = {first + offset: offset for offset in range(len(calls))}
        for identifier, offset in pending.items():
            await self.frame(
                {
                    "jsonrpc": "2.0",
                    "method": calls[offset].method,
                    "params": calls[offset].params,
                    "id": identifier,
                }
            )

        async def answered() -> AsyncIterator[Answer]:
            outstanding = dict(pending)
            while outstanding:
                message = await self.receive()
                if "method" in message or "id" not in message:
                    continue
                identifier = message["id"]
                if not isinstance(identifier, int) or identifier not in outstanding:
                    continue
                yield Answer(
                    offset=outstanding.pop(identifier),
                    result=message["result"] if "result" in message else None,
                )

        replies = {answer.offset: answer.result async for answer in answered()}
        return [replies[offset] for offset in range(len(calls))]

    async def request(self, method: str, params: JsonObject) -> JsonValue:
        """Ask one question and return its answer, skipping unrelated traffic."""
        answers = await self.requests([Call(method=method, params=params)])
        return answers[0]

    async def open(self, path: Path, text: str | None = None) -> list[str]:
        """Open a document once per session and return its lines.

        The lines come back because an LSP position is a UTF-16 offset into
        one of them, so a caller holding a byte offset needs the text that
        offset is into.

        *text* is the document's content where the caller holds it and disk
        does not — an edit judged before it is written. The notification
        already carries the whole document, so serving one costs nothing but
        not reading the file, and the URI stays the file's own: the server
        resolves imports and the module's own name exactly as it would for
        the saved copy. This is what an editor sends for an unsaved buffer.
        """
        key = path.as_posix()
        if key in self.opened:
            return self.opened[key]
        if text is None:
            text = path.read_text(encoding="utf-8")
        self.opened[key] = text.splitlines()
        await self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": path.absolute().as_uri(),
                    "languageId": "python",
                    "version": 1,
                    "text": text,
                }
            },
        )
        return self.opened[key]

    async def position_in(
        self, path: Path, line: int, column: int, text: str | None = None
    ) -> JsonObject:
        """The `TextDocumentPositionParams` for a one-based line and byte column."""
        lines = await self.open(path, text)
        row = lines[line - 1] if line <= len(lines) else ""
        return {
            "textDocument": {"uri": path.absolute().as_uri()},
            "position": {"line": line - 1, "character": utf16_column(row, column)},
        }


@asynccontextmanager
async def lsp_session(
    server: Path,
    root: Path,
    *,
    name: str = "language server",
    settings: JsonObject | None = None,
) -> AsyncGenerator[LspSession]:
    """Start one language server, initialize it, and always stop it."""
    process = await asyncio.create_subprocess_exec(
        str(server),
        "--stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdin, stdout = process.stdin, process.stdout
    if stdin is None or stdout is None:
        process.kill()
        await process.wait()
        raise OSError(f"{name} started without usable pipes")
    session = LspSession(stdin, stdout, name)
    try:
        await session.request(
            "initialize",
            {
                "processId": None,
                "rootUri": root.as_uri(),
                # Declared, not empty: a server answers `documentSymbol` with
                # the flat shape unless the client says it understands the
                # nested one, and the two are not interchangeable.
                "capabilities": {
                    "textDocument": {
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True}
                    }
                },
                "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
            },
        )
        await session.notify("initialized", {})
        if settings is not None:
            await session.notify(
                "workspace/didChangeConfiguration", {"settings": settings}
            )
        yield session
        await session.request("shutdown", {})
        await session.notify("exit", {})
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
