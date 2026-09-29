"""Code intelligence as tools, so every runtime asks the same questions.

An agent that greps for `def foo` finds characters; a language server finds
the definition, through the imports and aliases that make a name mean what it
means. Serving that as MCP tools rather than relying on whatever each harness
happens to ship is what makes one instruction — *use these instead of grep* —
true on every runtime, instead of true on the one whose vendor provides them.

The server executable and workspace root are supplied, so this names no
particular checker. A failure is reported to the agent as a tool error rather
than swallowed: an agent told "no references" by a server that never started
would conclude the symbol is unused.

Which workspace answers is taken from the file asked about rather than from
the directory the server started in. The two differ whenever work happens in
a second checkout of one repository, which is the normal case here — and a
server rooted on the wrong one does not fail, it resolves the same module
names against different source and answers confidently about a file nobody
asked about.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, Field, ValidationError

from lup.tools.lsp.client import LspSession, lsp_session
from lup.tools.lsp.replies import (
    DOCUMENT_SYMBOLS,
    HOVER,
    LOCATIONS,
    WORKSPACE_EDIT,
)
from lup.tools.mcp import LupMcpTool, ToolDeclaration, ToolError, lup_tool
from lup.types import JsonObject, JsonValue
from lup.workspace.edition import edition_path, read_edition

REQUEST_TIMEOUT_SECONDS = 120.0
"""Budget for one question. A server that stops answering fails the tool."""

WORKSPACE_MARKERS = ("pyproject.toml", ".git")
"""What makes a directory the workspace a file belongs to, nearest first.

Both are markers a checkout carries at its own root, so the search stops at
the checkout holding the file rather than walking on to whatever encloses it.
A project that roots on something else passes its own.
"""

FIND_DEFINITION = ToolDeclaration(
    name="find_definition",
    description=(
        "Find where a symbol is defined. Use instead of grepping for `def name` "
        "or `class name`: this resolves imports and aliases, so it finds the "
        "real declaration rather than a line that looks like one."
    ),
)

FIND_REFERENCES = ToolDeclaration(
    name="find_references",
    description=(
        "Find every use of a symbol across the workspace. Use instead of "
        "grepping for a name: this excludes look-alikes in other scopes and "
        "includes uses reached through an alias or a re-export."
    ),
)

HOVER_TOOL = ToolDeclaration(
    name="hover",
    description=(
        "Read a symbol's inferred type and documentation. Use before assuming "
        "what a value is: the checker knows the type that was resolved."
    ),
)

LIST_SYMBOLS = ToolDeclaration(
    name="list_symbols",
    description=(
        "List every symbol a file declares, with its line. Use instead of "
        "grepping for `def ` or `class ` to learn a file's shape."
    ),
)

RENAME_SYMBOL = ToolDeclaration(
    name="rename_symbol",
    description=(
        "Plan a workspace-wide rename of the symbol at a position. Reports the "
        "files and edit counts without writing anything. Always prefer this "
        "over a find-and-replace, which cannot tell one scope from another."
    ),
)

CODEINTEL_TOOL_DECLARATIONS = (
    FIND_DEFINITION,
    FIND_REFERENCES,
    HOVER_TOOL,
    LIST_SYMBOLS,
    RENAME_SYMBOL,
)


def rendered_tool_declarations(
    declarations: tuple[ToolDeclaration, ...] = CODEINTEL_TOOL_DECLARATIONS,
) -> list[ToolDeclaration]:
    """What a guidance surface renders a code-intelligence roster from.

    The set is this library's answer rather than every adopter's: a project
    whose checker answers fewer questions, or which serves resolution tools of
    its own beside these, renders its own roster by passing them here instead
    of forking the module that writes ours down.
    """
    return list(declarations)


class SymbolSite(BaseModel):
    """One resolved source location, in the one-based terms an editor shows."""

    path: str
    line: int


class SiteList(BaseModel):
    """Where a symbol is defined, or every place it is used."""

    sites: list[SymbolSite]


class Documentation(BaseModel):
    """What the checker knows about the symbol at one position."""

    text: str


class FileSymbol(BaseModel):
    """One symbol a document declares."""

    name: str
    kind: int
    line: int


class SymbolList(BaseModel):
    """Every symbol one document declares."""

    symbols: list[FileSymbol]


class RenamedFile(BaseModel):
    """One file a rename would touch, and how many edits it would make."""

    path: str
    edits: int


class RenamePlan(BaseModel):
    """Every edit a rename implies, before anything is written.

    Reported rather than applied: a rename spanning files is exactly the
    change worth seeing first, and the edit tools already carry the approval
    path that writing from here would route around.
    """

    new_name: str
    files: list[RenamedFile]


class PositionInput(BaseModel, extra="forbid"):
    """A symbol named by where it sits."""

    path: str = Field(
        description=(
            "Path to the file. A relative one resolves against the checkout "
            "this server was started in; pass an absolute path when you are "
            "working in a different one, such as a worktree."
        )
    )
    line: int = Field(description="One-based line number the symbol is on.")
    column: int = Field(
        default=0, description="Zero-based byte column of the symbol on that line."
    )


class RenameInput(PositionInput):
    """A symbol to rename, and what to call it."""

    new_name: str = Field(description="The identifier to rename the symbol to.")


class DocumentInput(BaseModel, extra="forbid"):
    """A whole file to enumerate."""

    path: str = Field(
        description=(
            "Path to the file. A relative one resolves against the checkout "
            "this server was started in; pass an absolute path when you are "
            "working in a different one, such as a worktree."
        )
    )


def path_of(uri: str) -> str:
    """The filesystem path a `file:` URI names."""
    return Path(unquote(urlparse(uri).path)).as_posix()


def pointed(file: Path, params: PositionInput) -> None:
    """Refuse a position that lands on no symbol at all.

    A language server asked about whitespace answers nothing, and nothing
    arrives as an empty result — which reads as *this symbol has no
    references* rather than as *you did not name a symbol*. The two are
    opposite conclusions and the caller cannot tell them apart, so the one
    that is a mistake is refused here instead of being answered emptily.

    The column defaults to zero, the first character of the line: right for
    a name at the margin and whitespace for everything indented, so this
    catches the mistake the default invites.

    What it cannot catch is a column that lands inside the line on the wrong
    token — a bracket, a keyword, a neighbouring name — because which
    characters make a symbol is the language's answer and this module names
    no language. An empty result from a position inside the line is a real
    answer from the server, and stays one.
    """
    lines = file.read_text(encoding="utf-8").splitlines()
    if not 1 <= params.line <= len(lines):
        raise ToolError(
            f"{file} has {len(lines)} lines, so line {params.line} is not one "
            "of them. Line numbers are one-based, as list_symbols reports them."
        )
    line = lines[params.line - 1]
    if params.column < len(line) and not line[params.column].isspace():
        return
    sits = "is past the end of" if params.column >= len(line) else "is whitespace in"
    raise ToolError(
        f"column {params.column} {sits} line {params.line} of {file}, so it "
        f"names no symbol. The column is zero-based and points at the name "
        f"itself, not at the line holding it:\n{line}\n"
        "list_symbols reports the line a name is on; count its column from "
        "the start of that line, counting the indentation."
    )


def sites_of(result: JsonValue) -> list[SymbolSite]:
    """Decode a location-shaped reply into one-based source sites."""
    try:
        decoded = LOCATIONS.validate_python(result)
    except ValidationError:
        return []
    if decoded is None:
        return []
    found = decoded if isinstance(decoded, list) else [decoded]
    return [
        SymbolSite(path=path_of(location.uri), line=location.range.start.line + 1)
        for location in found
    ]


def workspace_for(
    file: Path, fallback: Path, markers: tuple[str, ...] = WORKSPACE_MARKERS
) -> Path:
    """The checkout that should answer about *file*, not the one launched in.

    A language server is started once, against the directory the session
    opened, and keeps that root for its lifetime. Ask it about a file in
    another checkout of the same repository — a worktree, which is where
    this project asks that every change be made — and it resolves the
    imports against the launch directory: same module names, different
    source, and an answer that is well-formed and about the wrong tree.

    The file is the one thing a question carries that knows where it lives,
    so it is what picks the workspace. Module scope rather than a closure
    because every consumer that drives a language server needs the same
    answer, and one of them getting it right by accident of process
    lifetime is how this goes unnoticed.
    """
    for directory in file.parents:
        if any((directory / marker).exists() for marker in markers):
            return directory
    return fallback


def create_codeintel_tools(
    server: Path,
    root: Path,
    request_timeout: float = REQUEST_TIMEOUT_SECONDS,
    markers: tuple[str, ...] = WORKSPACE_MARKERS,
    edition: Path | None = None,
    settings: Callable[[Path], JsonObject] | None = None,
) -> list[LupMcpTool]:
    """Build the code-intelligence tools driving *server* over *root*.

    Args:
        server: The language-server executable, run with ``--stdio``.
        root: Checkout a relative path resolves against, and the workspace
            for a file that sits in no discoverable one.
        markers: What makes a directory a workspace root, nearest first.
        edition: Where the permission hook publishes where editing is
            happening. Defaults to the location both halves derive from the
            repository, which is the only one either can name without being
            told; a caller supplies its own to keep a test out of the real
            repository's state.
        settings: Language-server configuration derived for the requested
            file's workspace, which may differ from the launch checkout.

    Returns:
        Tools answering definition, reference, hover, symbol, and rename
        questions. Each opens its own session, so no state survives a call
        and a server that dies costs one question rather than the group.
    """

    async def guarded[T](work: Awaitable[T], question: str) -> T:
        try:
            return await asyncio.wait_for(work, timeout=request_timeout)
        except (OSError, EOFError, TimeoutError, ValueError) as error:
            raise ToolError(
                f"{server.name} could not answer {question}: {error}"
            ) from error

    def located(path: str) -> Path:
        against = editing_root() or root
        resolved = (against / path).resolve()
        if not resolved.is_file():
            raise ToolError(f"no such file under {against}: {path}")
        return resolved

    def editing_root() -> Path | None:
        """The checkout being edited, for a path that names no checkout itself.

        An absolute path answers this for itself and never reaches here. A
        relative one cannot: it is relative to the working directory of
        whoever asked, and this server is a separate process whose own is
        fixed at launch — so joining it to the launch checkout is a guess
        that reads another tree's file whenever work has moved, silently,
        because both trees hold that path.

        The permission hook publishes where editing is happening on every
        edit, which is the fact the guess would stand in for.
        """
        published = read_edition(edition or edition_path(root))
        return published.workspace if published is not None else None

    @asynccontextmanager
    async def session_for(path: Path) -> AsyncIterator[LspSession]:
        workspace = workspace_for(path, root, markers)
        async with lsp_session(
            server,
            workspace,
            name=server.name,
            settings=None if settings is None else settings(workspace),
        ) as session:
            yield session

    async def at_position(method: str, params: PositionInput) -> JsonValue:
        resolved = located(params.path)
        pointed(resolved, params)

        async def run() -> JsonValue:
            async with session_for(resolved) as session:
                return await session.request(
                    method,
                    await session.position_in(resolved, params.line, params.column),
                )

        return await guarded(run(), method)

    @lup_tool(FIND_DEFINITION.description, name=FIND_DEFINITION.name)
    async def find_definition(params: PositionInput) -> SiteList:
        return SiteList(
            sites=sites_of(await at_position("textDocument/definition", params))
        )

    @lup_tool(FIND_REFERENCES.description, name=FIND_REFERENCES.name)
    async def find_references(params: PositionInput) -> SiteList:
        resolved = located(params.path)
        pointed(resolved, params)

        async def run() -> JsonValue:
            async with session_for(resolved) as session:
                request = await session.position_in(
                    resolved, params.line, params.column
                )
                request["context"] = {"includeDeclaration": False}
                return await session.request("textDocument/references", request)

        return SiteList(sites=sites_of(await guarded(run(), "references")))

    @lup_tool(HOVER_TOOL.description, name=HOVER_TOOL.name)
    async def hover(params: PositionInput) -> Documentation:
        result = await at_position("textDocument/hover", params)
        try:
            decoded = HOVER.validate_python(result)
        except ValidationError:
            return Documentation(text="")
        return Documentation(text="" if decoded is None else decoded.text())

    @lup_tool(LIST_SYMBOLS.description, name=LIST_SYMBOLS.name)
    async def list_symbols(params: DocumentInput) -> SymbolList:
        resolved = located(params.path)

        async def run() -> JsonValue:
            async with session_for(resolved) as session:
                await session.open(resolved)
                return await session.request(
                    "textDocument/documentSymbol",
                    {"textDocument": {"uri": resolved.as_uri()}},
                )

        result = await guarded(run(), "symbols")
        try:
            decoded = DOCUMENT_SYMBOLS.validate_python(result)
        except ValidationError:
            return SymbolList(symbols=[])
        return SymbolList(
            symbols=[
                FileSymbol(name=found.name, kind=found.kind, line=found.line)
                for symbol in decoded or []
                for found in symbol.declared()
            ]
        )

    @lup_tool(RENAME_SYMBOL.description, name=RENAME_SYMBOL.name)
    async def rename_symbol(params: RenameInput) -> RenamePlan:
        resolved = located(params.path)
        pointed(resolved, params)

        async def run() -> JsonValue:
            async with session_for(resolved) as session:
                request = await session.position_in(
                    resolved, params.line, params.column
                )
                request["newName"] = params.new_name
                return await session.request("textDocument/rename", request)

        result = await guarded(run(), "a rename")
        try:
            decoded = WORKSPACE_EDIT.validate_python(result)
        except ValidationError:
            decoded = None
        return RenamePlan(
            new_name=params.new_name,
            files=[
                RenamedFile(path=path_of(touched.uri), edits=touched.edits)
                for touched in (decoded.touched() if decoded else [])
            ],
        )

    return [find_definition, find_references, hover, list_symbols, rename_symbol]
