"""Language servers kept warm for a page asking about code: one per repository and language.

A page asking what a name is cannot pay for a server's start on every
question — basedpyright reads a workspace before its first answer — so a
server starts on the first question about its repository, and stops once it
has gone unasked for a while. Each checkout of the repository is a workspace
folder of that one server, added the first time it is asked about, so a
worktree's imports resolve in that worktree; the server pulls each folder's
settings from its declaration (:mod:`lup.tools.lsp.servers`).

A document asked about is handed to the server whole, the way an editor hands
it an unsaved buffer: the file as it stands, or a version nobody has written
— a proposal waiting for approval — under the file's own name, so it resolves
exactly as the saved copy would.

Every question is bounded. One the server does not answer in time is
withdrawn and answered as not served, with why, so a slow server costs the
page an empty float rather than a hung one; the server keeps whatever it
worked out, and the next question finds it warmer.
"""

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ValidationError

from lup.tools.lsp.client import (
    CLIENT_CAPABILITIES,
    Call,
    ConfigurationItem,
    LspSession,
    lsp_session,
)
from lup.tools.lsp.replies import (
    HOVER,
    LOCATIONS,
    SEMANTIC_PROVIDER,
    SEMANTIC_TOKENS,
    path_of,
)
from lup.tools.lsp.servers import LanguageServer, Launch, Unavailable, default_servers
from lup.types import JsonObject, JsonValue


class Document(BaseModel, frozen=True):
    """One document asked about: its file, the checkout and repository holding it, and its text."""

    path: Path
    checkout: Path
    repository: Path
    text: str


class Answered(BaseModel, frozen=True):
    """Whether a server answered, which one, and why not where it did not."""

    served: bool
    server: str = ""
    why: str = ""


class CodeHover(Answered, frozen=True):
    """What the server says of the symbol at a position: its type, signature and documentation, as Markdown."""

    markdown: str = ""


class CodeLocation(BaseModel, frozen=True):
    """One place in a file, one-based line and UTF-16 column, with the line it is on."""

    path: str
    line: int
    column: int
    preview: str = ""


class CodeLocations(Answered, frozen=True):
    """Where a symbol is defined, or every place it is used."""

    locations: list[CodeLocation] = []


class CodeTokens(Answered, frozen=True):
    """What the server says each name in a document is, in its own legend.

    Five numbers a token, as the protocol relays them: the line and the
    UTF-16 start, each relative to the token before, then the length, the
    type's index in *types*, and the modifiers as bits over *modifiers*.
    """

    types: list[str] = []
    modifiers: list[str] = []
    data: list[int] = []


class CodeText(Answered, frozen=True):
    """A file a server pointed at, whole, for a page to show."""

    path: str = ""
    text: str = ""


class NotServed(Exception):
    """Why a question went unanswered, in words for the page."""

    def __init__(self, server: str, why: str) -> None:
        super().__init__(why)
        self.server = server
        self.why = why


class Warm:
    """One running server for one repository: the session, its folders, and when it was last asked."""

    def __init__(
        self,
        server: LanguageServer,
        launch: Launch,
        first: Path,
        idle: float,
        start: float,
    ) -> None:
        self.server = server
        self.folders: dict[Path, JsonObject] = {first: server.settings(first)}
        self.named: dict[str, CodeLocation] = {}
        """Every file this server pointed at, which a page may then ask to read, and the place it named there."""
        self.lock = asyncio.Lock()
        self.used = time.monotonic()
        self.idle = idle
        self.ready: asyncio.Future[LspSession] = (
            asyncio.get_running_loop().create_future()
        )
        self.task = asyncio.create_task(self.keep(launch, first, start))

    def configure(self, item: ConfigurationItem) -> JsonValue:
        """The section a folder asks for, from the settings its checkout was handed."""
        scope = Path(path_of(item.scope)) if item.scope else None
        owners = sorted(
            (
                folder
                for folder in self.folders
                if scope is None or scope == folder or folder in scope.parents
            ),
            key=lambda folder: len(folder.parts),
        )
        settings = self.folders[owners[-1]] if owners else {}
        return settings[item.section] if item.section in settings else None

    async def keep(self, launch: Launch, first: Path, start: float) -> None:
        """Run the server until it has gone unasked for `idle` seconds, or fails."""
        capabilities: JsonObject = {
            **CLIENT_CAPABILITIES,
            "workspace": {"configuration": True, "workspaceFolders": True},
        }
        try:
            async with asyncio.timeout(start) as deadline:
                async with lsp_session(
                    launch.command,
                    first,
                    name=self.server.name,
                    arguments=launch.arguments,
                    language=next(iter(self.server.languages.values()), ""),
                    capabilities=capabilities,
                    configure=self.configure,
                ) as session:
                    deadline.reschedule(None)
                    self.ready.set_result(session)
                    while not session.failure and (left := self.left()) > 0:
                        await asyncio.sleep(left)
                    deadline.reschedule(asyncio.get_running_loop().time() + start)
        except (OSError, EOFError, ValueError, TimeoutError) as failed:
            if not self.ready.done():
                self.ready.set_exception(
                    OSError(f"{self.server.name} did not start: {failed}")
                )

    def left(self) -> float:
        """How long until this server has been idle long enough to stop."""
        return self.idle - (time.monotonic() - self.used)

    def running(self) -> bool:
        return not self.task.done()

    async def include(self, session: LspSession, checkout: Path) -> None:
        """Make a checkout one of the server's workspace folders, the first time it is asked about."""
        if checkout in self.folders:
            return
        self.folders[checkout] = self.server.settings(checkout)
        await session.notify(
            "workspace/didChangeWorkspaceFolders",
            {
                "event": {
                    "added": [{"uri": checkout.as_uri(), "name": checkout.name}],
                    "removed": [],
                }
            },
        )


class ServerKey(BaseModel, frozen=True):
    """Which running server answers: one per language server and repository."""

    server: str
    repository: Path


class Replied(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """What one running server said to one question."""

    warm: Warm
    result: JsonValue


class ServerPool:
    """The servers a page asks about code, kept warm per repository and language.

    *servers* are asked in order, the first reading a file's suffix answering
    for it. A server unasked for *idle* seconds stops; one not started within
    *start* seconds, or not answering a question within the *within* the
    question carries, answers as not served.
    """

    def __init__(
        self,
        servers: list[LanguageServer] | None = None,
        idle: float = 600.0,
        start: float = 60.0,
    ) -> None:
        self.servers = servers if servers is not None else default_servers()
        self.idle = idle
        self.start = start
        self.warm: dict[ServerKey, Warm] = {}

    def server(self, path: Path) -> LanguageServer | None:
        """The server that reads *path*, where one does."""
        return next((each for each in self.servers if each.language(path)), None)

    def running(self, document: Document) -> Warm:
        """The server warm for a document's repository, started where none runs."""
        server = self.server(document.path)
        if server is None:
            raise NotServed(
                "",
                f"no language server reads {document.path.suffix or document.path.name} files",
            )
        key = ServerKey(server=server.name, repository=document.repository)
        held = self.warm[key] if key in self.warm else None
        if held is not None and held.running():
            return held
        try:
            launch = server.launch()
        except Unavailable as missing:
            raise NotServed(server.name, str(missing)) from missing
        warm = Warm(server, launch, document.checkout, self.idle, self.start)
        self.warm[key] = warm
        return warm

    async def ask(
        self, document: Document, method: str, params: JsonObject, within: float
    ) -> Replied:
        """Hand the server a document as it stands here, ask it one question about it, and wait *within* seconds."""
        warm = self.running(document)
        name = warm.server.name
        try:
            session = await asyncio.wait_for(asyncio.shield(warm.ready), within)
        except TimeoutError as slow:
            raise NotServed(
                name,
                f"{name} is still starting for {document.repository.name}; ask again in a moment",
            ) from slow
        except OSError as failed:
            raise NotServed(name, str(failed)) from failed
        warm.used = time.monotonic()
        async with warm.lock:
            await warm.include(session, document.checkout)
            await session.open(
                document.path, document.text, warm.server.language(document.path)
            )
            asked = await session.ask(
                [
                    Call(
                        method=method,
                        params={
                            "textDocument": {"uri": document.path.as_uri()},
                            **params,
                        },
                    )
                ]
            )
        try:
            result = await asyncio.wait_for(asyncio.shield(asked[0].reply), within)
        except TimeoutError as slow:
            await session.withdraw(asked)
            raise NotServed(
                name, f"{name} did not answer within {within:g} seconds"
            ) from slow
        except OSError as failed:
            raise NotServed(name, str(failed)) from failed
        return Replied(warm=warm, result=result)

    async def hover(
        self, document: Document, line: int, column: int, within: float = 5.0
    ) -> CodeHover:
        """What the symbol at a one-based line and UTF-16 column is, as the server describes it."""
        try:
            replied = await self.ask(
                document, "textDocument/hover", position(line, column), within
            )
        except NotServed as unserved:
            return CodeHover(served=False, server=unserved.server, why=unserved.why)
        try:
            decoded = HOVER.validate_python(replied.result)
        except ValidationError:
            decoded = None
        if decoded is None or not decoded.text().strip():
            return CodeHover(
                served=True,
                server=replied.warm.server.name,
                why="nothing is known about what is here",
            )
        return CodeHover(
            served=True, server=replied.warm.server.name, markdown=decoded.text()
        )

    async def definition(
        self, document: Document, line: int, column: int, within: float = 5.0
    ) -> CodeLocations:
        """Where the symbol at a position is defined."""
        return await self.located(
            document, "textDocument/definition", position(line, column), within
        )

    async def references(
        self, document: Document, line: int, column: int, within: float = 10.0
    ) -> CodeLocations:
        """Every place the symbol at a position is used, its declaration left out."""
        params = {**position(line, column), "context": {"includeDeclaration": False}}
        return await self.located(document, "textDocument/references", params, within)

    async def located(
        self, document: Document, method: str, params: JsonObject, within: float
    ) -> CodeLocations:
        try:
            replied = await self.ask(document, method, params, within)
        except NotServed as unserved:
            return CodeLocations(served=False, server=unserved.server, why=unserved.why)
        try:
            decoded = LOCATIONS.validate_python(replied.result)
        except ValidationError:
            decoded = None
        found = (
            []
            if decoded is None
            else decoded
            if isinstance(decoded, list)
            else [decoded]
        )
        texts: dict[str, list[str]] = {
            document.path.as_posix(): document.text.splitlines()
        }

        def lines(path: str) -> list[str]:
            if path not in texts:
                try:
                    texts[path] = Path(path).read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeDecodeError):
                    texts[path] = []
            return texts[path]

        locations = [
            CodeLocation(
                path=path,
                line=location.range.start.line + 1,
                column=location.range.start.character,
                preview=at[location.range.start.line]
                if location.range.start.line < len(at)
                else "",
            )
            for location in found
            for path in [path_of(location.uri)]
            for at in [lines(path)]
        ]
        warm = replied.warm
        warm.named.update({each.path: each for each in locations})
        if not locations:
            return CodeLocations(
                served=True,
                server=warm.server.name,
                why="the server knows of no such place",
            )
        return CodeLocations(served=True, server=warm.server.name, locations=locations)

    async def tokens(self, document: Document, within: float = 20.0) -> CodeTokens:
        """What every name in a document is, as the server classifies it."""
        try:
            replied = await self.ask(
                document, "textDocument/semanticTokens/full", {}, within
            )
        except NotServed as unserved:
            return CodeTokens(served=False, server=unserved.server, why=unserved.why)
        warm = replied.warm
        session = warm.ready.result()
        declared = (
            session.served["semanticTokensProvider"]
            if "semanticTokensProvider" in session.served
            else None
        )
        try:
            provider = SEMANTIC_PROVIDER.validate_python(declared)
            decoded = SEMANTIC_TOKENS.validate_python(replied.result)
        except ValidationError:
            provider, decoded = None, None
        if provider is None:
            return CodeTokens(
                served=False,
                server=warm.server.name,
                why=f"{warm.server.name} serves no semantic tokens",
            )
        return CodeTokens(
            served=True,
            server=warm.server.name,
            types=provider.legend.types,
            modifiers=provider.legend.modifiers,
            data=[] if decoded is None else decoded.data,
        )

    def text(
        self, repository: Path, path: Path, readable: Callable[[Path], bool]
    ) -> CodeText:
        """A file whole, where a server warm for this repository pointed at it, or *readable* admits it."""
        named = any(
            path.as_posix() in warm.named
            for key, warm in self.warm.items()
            if key.repository == repository
        )
        if not named and not readable(path):
            return CodeText(
                served=False,
                path=path.as_posix(),
                why="no language server pointed at this file",
            )
        try:
            return CodeText(
                served=True, path=path.as_posix(), text=path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError) as unread:
            return CodeText(
                served=False,
                path=path.as_posix(),
                why=f"it could not be read: {unread}",
            )

    async def stop(self) -> None:
        """Stop every server this pool started."""
        tasks = [warm.task for warm in self.warm.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.warm.clear()


def position(line: int, column: int) -> JsonObject:
    """A one-based line and a UTF-16 column, as the protocol places them."""
    return {"position": {"line": max(0, line - 1), "character": max(0, column)}}
