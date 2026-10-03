"""One language-server session over stdio, answering arbitrary requests.

A language server resolves names the way the language does — through imports,
aliases, and re-exports — which is the difference between finding a symbol and
finding characters that look like one. The protocol is the same for every
question worth asking it, so the framing, the request/response pairing, and
the document bookkeeping are written once here and each caller supplies the
method it wants.

Nothing here decides which server to run: the command and the workspace root
are supplied, so a project that type-checks with something else answers
these questions with something else. Failures are the caller's to interpret —
a sweep that can degrade to a weaker verdict and a tool that must report the
truth to an agent want opposite things from a server that will not start.

One task reads everything the server sends, for as long as it runs. A reply
settles the question it answers; a request of the server's own — a
configuration it wants, a capability it registers — is answered at once,
since a server can hold everything else until it is; anything else is let
go. Reading in one place is what lets a question be asked while another is
outstanding, and be given up on without leaving the stream half read: a
caller that stops waiting withdraws it with `$/cancelRequest`, and a reply
that comes anyway finds nobody waiting and is dropped.
"""

import asyncio
import json
import os
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager
from email.parser import BytesParser
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from lup.types import JsonObject, JsonValue

CLIENT_CAPABILITIES: JsonObject = {
    "textDocument": {
        # Declared, not empty: a server answers `documentSymbol` with the flat
        # shape unless the client says it understands the nested one, and the
        # two are not interchangeable.
        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
        "hover": {"contentFormat": ["markdown", "plaintext"]},
        # The protocol's own legend. A server colours only with the types the
        # client names, and answers in the legend it declares back.
        "semanticTokens": {
            "requests": {"full": True},
            "formats": ["relative"],
            "tokenTypes": [
                "namespace", "type", "class", "enum", "interface", "struct",
                "typeParameter", "parameter", "variable", "property",
                "enumMember", "event", "function", "method", "macro",
                "keyword", "modifier", "comment", "string", "number",
                "regexp", "operator", "decorator",
            ],
            "tokenModifiers": [
                "declaration", "definition", "readonly", "static",
                "deprecated", "abstract", "async", "modification",
                "documentation", "defaultLibrary",
            ],
        },
    }
}  # fmt: skip
"""What this client understands of a server's answers, declared at `initialize`."""


def utf16_column(line: str, column: int) -> int:
    """Convert a UTF-8 byte offset into the UTF-16 offset LSP positions use."""
    prefix = line.encode("utf-8")[:column].decode("utf-8", errors="ignore")
    return len(prefix.encode("utf-16-le")) // 2


class Call(BaseModel, frozen=True):
    """One question in a batch: a method to ask, and the params to ask it with."""

    method: str
    params: JsonObject


class ConfigurationItem(BaseModel):
    """One section a server asks for in `workspace/configuration`, and the folder or file it is for."""

    scope: str = Field(default="", alias="scopeUri")
    section: str = ""


CONFIGURATION_ITEMS = TypeAdapter(list[ConfigurationItem])

type Configure = Callable[[ConfigurationItem], JsonValue]
"""What a session answers a server asking for one section of its configuration."""


class OpenDocument(BaseModel, frozen=True):
    """A document as the server was last told it stands, and its version there."""

    text: str
    version: int


class Asked(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One question on its way: the id it went out under, and where its reply lands."""

    identifier: int
    reply: asyncio.Future[JsonValue]


class LspSession:
    """An initialized language server, and the documents opened against it."""

    def __init__(
        self,
        stdin: asyncio.StreamWriter,
        stdout: asyncio.StreamReader,
        name: str,
        *,
        language: str = "python",
        configure: Configure | None = None,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.name = name
        self.language = language
        """The `languageId` a document is opened under where its caller names none."""
        self.configure = configure
        """How a section of the configuration is answered where the server asks for it; none where unset."""
        self.asked = 0
        self.opened: dict[str, OpenDocument] = {}
        self.waiting: dict[int, asyncio.Future[JsonValue]] = {}
        self.served: JsonObject = {}
        """The capabilities the server declared, once it is initialized."""
        self.failure = ""
        self.reader: asyncio.Task[None] | None = None

    def listen(self) -> None:
        """Start reading what the server sends, once, before the first question."""
        if self.reader is None:
            self.reader = asyncio.create_task(self.read())

    async def read(self) -> None:
        try:
            while True:
                await self.settle(await self.receive())
        except (OSError, EOFError, ValueError) as error:
            self.failure = f"{self.name} stopped answering: {error}"
            for reply in self.waiting.values():
                if not reply.done():
                    reply.set_exception(OSError(self.failure))
            self.waiting.clear()

    async def settle(self, message: JsonObject) -> None:
        """One message the server sent: a reply, a request of its own, or news nobody waits on.

        A request of the server's own is acknowledged and nothing more: a
        capability it registers, a progress token it creates. One asking for
        its configuration is answered section by section by the session's
        `configure`, and with none for each where it has no `configure`, so
        the server keeps what it was pushed with
        `workspace/didChangeConfiguration`, or its defaults.
        """
        match message:
            case {
                "method": "workspace/configuration",
                "id": int() | str() as identifier,
                "params": {"items": list(items)},
            }:
                await self.frame(
                    {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "result": self.configured(items),
                    }
                )
            case {"method": str(), "id": int() | str() as identifier}:
                await self.frame({"jsonrpc": "2.0", "id": identifier, "result": None})
            case {"id": int(identifier)} if identifier in self.waiting:
                reply = self.waiting.pop(identifier)
                if not reply.done():
                    reply.set_result(message["result"] if "result" in message else None)
            case _:
                return

    def configured(self, items: list[JsonValue]) -> list[JsonValue]:
        """Each section a `workspace/configuration` request asks for, as `configure` answers it."""
        try:
            asked = CONFIGURATION_ITEMS.validate_python(items)
        except ValidationError:
            return [None for _ in items]
        configure = self.configure
        return [None if configure is None else configure(item) for item in asked]

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

        A caller that stops waiting — cancelled, or out of time — withdraws
        every question still unanswered, so the server drops work nobody
        will read.
        """
        asked = await self.ask(calls)
        try:
            return list(await asyncio.gather(*(each.reply for each in asked)))
        finally:
            await self.withdraw(asked)

    async def ask(self, calls: list[Call]) -> list[Asked]:
        """Send each question, and hand back where each reply will land, without waiting for any."""
        if self.failure:
            raise OSError(self.failure)
        loop = asyncio.get_running_loop()
        asked = [
            Asked(identifier=self.asked + offset, reply=loop.create_future())
            for offset in range(1, len(calls) + 1)
        ]
        self.asked += len(calls)
        for each, call in zip(asked, calls, strict=True):
            self.waiting[each.identifier] = each.reply
            await self.frame(
                {
                    "jsonrpc": "2.0",
                    "method": call.method,
                    "params": call.params,
                    "id": each.identifier,
                }
            )
        return asked

    async def withdraw(self, asked: list[Asked]) -> None:
        """Give up on every question here still unanswered, telling the server so."""
        for each in asked:
            if each.reply.done() and not each.reply.cancelled():
                continue
            each.reply.cancel()
            if self.waiting.pop(each.identifier, None) is None or self.failure:
                continue
            try:
                await self.notify("$/cancelRequest", {"id": each.identifier})
            except OSError as gone:
                self.failure = f"{self.name} stopped answering: {gone}"

    async def request(self, method: str, params: JsonObject) -> JsonValue:
        """Ask one question and return its answer, skipping unrelated traffic."""
        answers = await self.requests([Call(method=method, params=params)])
        return answers[0]

    async def open(
        self, path: Path, text: str | None = None, language: str = ""
    ) -> list[str]:
        """Open a document, or bring the server's copy of it to *text*, and return its lines.

        The lines come back because an LSP position is a UTF-16 offset into
        one of them, so a caller holding a byte offset needs the text that
        offset is into.

        *text* is the document's content where the caller holds it and disk
        does not — an edit judged before it is written, a proposal not yet
        applied. The notification already carries the whole document, so
        serving one costs nothing but not reading the file, and the URI stays
        the file's own: the server resolves imports and the module's own name
        exactly as it would for the saved copy. This is what an editor sends
        for an unsaved buffer, and a later *text* for the same file is sent as
        the buffer changing, whole, under the next version. A document already
        open and given no text keeps the copy the server has.
        """
        key = path.as_posix()
        held = self.opened[key] if key in self.opened else None
        if text is None and held is not None:
            return held.text.splitlines()
        if text is None:
            text = path.read_text(encoding="utf-8")
        uri = path.absolute().as_uri()
        match held:
            case None:
                self.opened[key] = OpenDocument(text=text, version=1)
                opened: JsonObject = {
                    "uri": uri,
                    "languageId": language or self.language,
                    "version": 1,
                    "text": text,
                }
                await self.notify("textDocument/didOpen", {"textDocument": opened})
            case OpenDocument(text=same) if same == text:
                pass
            case OpenDocument(version=version):
                self.opened[key] = OpenDocument(text=text, version=version + 1)
                await self.notify(
                    "textDocument/didChange",
                    {
                        "textDocument": {"uri": uri, "version": version + 1},
                        "contentChanges": [{"text": text}],
                    },
                )
        return text.splitlines()

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
    arguments: Sequence[str] = ("--stdio",),
    language: str = "python",
    capabilities: JsonObject = CLIENT_CAPABILITIES,
    configure: Configure | None = None,
) -> AsyncGenerator[LspSession]:
    """Start one language server, initialize it, and always stop it.

    *arguments* follow the executable: ``--stdio`` for the servers that take
    it, ``--lsp --stdio`` for TypeScript's own compiler. *language* is the
    `languageId` a document opens under where its opener names none, and
    *configure* answers the server's `workspace/configuration`, where
    *capabilities* declare the client takes it. The server is told this
    process's id, so one this process left behind — killed before it could
    stop it — sees it gone and exits.
    """
    process = await asyncio.create_subprocess_exec(
        str(server),
        *arguments,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdin, stdout = process.stdin, process.stdout
    if stdin is None or stdout is None:
        process.kill()
        await process.wait()
        raise OSError(f"{name} started without usable pipes")
    session = LspSession(stdin, stdout, name, language=language, configure=configure)
    session.listen()
    try:
        initialized = await session.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": root.as_uri(),
                "capabilities": capabilities,
                "workspaceFolders": [{"uri": root.as_uri(), "name": root.name}],
            },
        )
        match initialized:
            case {"capabilities": dict(declared)}:
                session.served = declared
            case _:
                pass
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
        if session.reader is not None:
            session.reader.cancel()
            await asyncio.gather(session.reader, return_exceptions=True)
