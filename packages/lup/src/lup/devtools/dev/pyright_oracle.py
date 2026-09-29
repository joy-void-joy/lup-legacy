"""Pyright's language server, implementing the codescan type oracle.

`lup.harness.codescan.resolution` judges a site by what its subject is declared as,
and this is the only thing in the repository that can answer that. Pyright is
already the project's single type checker, but its CLI only reports
diagnostics — a declaration query needs the language-server protocol, so this
module speaks LSP to `pyright-langserver --stdio`.

**Which requests, and why.** Pyright 1.1 declares `definitionProvider`,
`typeDefinitionProvider`, `declarationProvider` and `hoverProvider`, and no
`typeHierarchyProvider` — so the one request that would answer "what does
this class descend from" as structured data does not exist here, and a
supertype chain has to be walked a declaration at a time.

`textDocument/definition` on the *member* is asked first, because it resolves
through the receiver's type to the class that actually declares the member —
which follows the inheritance chain for free. A `DeepBag(Bag(dict))` reaches
`dict.get` in one request, where asking about the receiver would name
`DeepBag` and leave the walk to us.

`textDocument/typeDefinition` on the *receiver* is the fallback, and it
answers the case the member cannot: a member no source declares. A
`TypedDict`'s `get` is synthesized, so the member request returns nothing at
all, while the receiver still resolves to the class the site is about.

`textDocument/hover` is not read. Its reply is `MarkupContent` — display
markup the protocol specifies as human-readable, whose `(module)`/`(method)`/
`(variable)` tags come from pyright's own hover provider and are not a
contract anything is owed. It would answer more sites than the two requests
above, and it would answer them in a string that a checker release is free to
reshape without telling anyone.

Nothing here decides anything about a rule: it returns what a subject is
declared as, and the rules measure that against their own families. Every way
this can fail — the server is not installed, it will not start, it stops
answering — resolves to an `UnknownDeclaration`, the same answer as a subject
the checker genuinely cannot type, so the audit keeps its unresolved verdict
rather than reporting a refutation it cannot support.
"""

import ast
import asyncio
from collections import deque
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import typer
from pydantic import BaseModel, ValidationError

from lup.devtools.launcher import project_python
from lup.tools.lsp.client import Call, LspSession, lsp_session
from lup.tools.lsp.replies import LOCATIONS
from lup.harness.codescan.oracle import (
    ClassDeclaration,
    Declaration,
    FunctionDeclaration,
    SourceBuffer,
    SourcePosition,
    SymbolQuery,
    TypeOracle,
    UnknownDeclaration,
)
from lup.policy.kernel.edit import name_position, nodes_of
from lup.types import JsonObject, JsonValue
from lup.workspace.paths import project_root

# lup: ignore[constant-declaration] — the binary pyright installs under
SERVER_NAME = "pyright-langserver"

RESOLVE_TIMEOUT_SECONDS = 600.0
"""Budget for a whole sweep. Exhausting it degrades to the broad rule."""

BASE_DEPTH_LIMIT = 8
"""How far a supertype walk follows a class's bases before it stops.

Every level costs a request per base, and a family is answered long before
this: `_Environ(MutableMapping[...])` is one, a project's own `dict`
subclass two or three. The bound is what keeps a cyclic or pathological
hierarchy from becoming an unbounded sweep, not a judgement about how deep a
real one goes.
"""

RESOLVE_WORKERS = min(8, max(2, (os.process_cpu_count() or 8) // 4))
"""Language servers a sweep drives at once, as a default a caller may raise.

Pyright answers one question at a time in one process, so a sweep pinned to
a single server is one core busy on a host that has many. Servers are what
parallelize it, and each one costs a process that builds its own picture of
the whole workspace — so the returns fall away once the sweep stops waiting
and the analyses start competing.

Derived from the host rather than pinned, because both ends of that trade
are the host's: a count that suits a large machine oversubscribes a laptop,
and one that suits a laptop leaves a large machine idle. Measured on a
32-core host, a cold whole-repository resolve took 20.5s under two servers,
17.8s under four and 16.0s under eight; across the whole gate, where the
servers compete with two test suites and a type checker, the same run took
41.1s, 36.2s and 33.5s, with six and eight indistinguishable and sixteen
back up at 36.1s. A quarter of the cores, capped, sits at that knee from
both directions.
"""


def langserver_path() -> Path | None:
    """The language server shipped beside this interpreter, or on PATH."""
    beside = Path(sys.executable).parent / SERVER_NAME
    if beside.is_file():
        return beside
    located = shutil.which(SERVER_NAME)
    return Path(located) if located is not None else None


def pyright_settings(root: Path) -> JsonObject:
    """Select this checkout's interpreter, leaving explicit Pyright config intact."""
    interpreter = project_python(root)
    return (
        {"python": {"pythonPath": str(interpreter)}} if interpreter is not None else {}
    )


class DeclarationSite(BaseModel, frozen=True):
    """One place a protocol reply pointed at, before it is read as anything."""

    path: Path
    line: int
    """1-based line the declaration starts on."""


def locations_of(result: JsonValue) -> list[DeclarationSite]:
    """Decode one location-answering reply into the places it names."""
    try:
        decoded = LOCATIONS.validate_python(result)
    except ValidationError:
        return []  # an unspecified result shape is no evidence, not an error
    if decoded is None:
        return []
    found = decoded if isinstance(decoded, list) else [decoded]
    return [
        DeclarationSite(
            path=Path(unquote(urlparse(location.uri).path)),
            line=location.range.start.line + 1,
        )
        for location in found
    ]


def base_name(node: ast.expr) -> str | None:
    """The declared name of one base class, unqualified and unsubscripted."""
    match node:
        case ast.Name(id=name):
            return name
        case ast.Attribute(attr=attribute):
            return attribute
        case ast.Subscript(value=value):
            return base_name(value)
    return None


def class_at(tree: ast.Module, line: int) -> ast.ClassDef | None:
    """The innermost class whose body spans `line`, or None outside any class.

    Read off the grouped node index rather than walked, because the files
    asked about most are the largest: every mapping in the repository
    resolves into typeshed's `builtins.pyi`, and walking it once per site is
    that file walked hundreds of times to answer the same question.
    """
    enclosing = [
        node
        for node in nodes_of(tree, ast.ClassDef)
        if node.lineno <= line <= (node.end_lineno or node.lineno)
    ]
    return max(enclosing, key=lambda node: node.lineno) if enclosing else None


def module_function_at(
    tree: ast.Module, line: int
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The module-level function whose body spans `line`, or None otherwise.

    Only the module's own body is walked, so what comes back is a declaration
    nothing encloses. That is the whole question: a `def` nested in a class is
    already answered for by :func:`class_at`, and anything that is not a
    function — an assignment, a stub the parse could not place — stays
    unanswered rather than being read as evidence it is not.
    """
    return next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.lineno <= line <= (node.end_lineno or node.lineno)
        ),
        None,
    )


def keys_for(path: Path) -> list[str]:
    """Every spelling one file answers to, so two namings meet at one entry.

    As written, made absolute, and with links followed: a caller says
    `packages/lup/src/…`, a checker replies `/home/…/packages/lup/src/…`, and
    a checkout reached through a symlink replies with the target. All three
    name one file and one buffer.
    """
    absolute = path.absolute()
    try:
        resolved = path.resolve()
    except OSError:
        resolved = absolute
    return list(
        dict.fromkeys([path.as_posix(), absolute.as_posix(), resolved.as_posix()])
    )


class Workspace:
    """The files one resolution reads, from what the caller holds or from disk.

    A buffer the caller supplied is what the checker was told about, so it is
    what a declaration reported inside that file has to be read out of. Going
    to disk instead answers about a file nobody audited — and the line the
    checker reported is a line in the buffer, so on disk it lands wherever
    the two copies have drifted to.
    """

    def __init__(self, buffers: list[SourceBuffer] | None = None) -> None:
        self.held = {
            key: buffer.text
            for buffer in buffers or []
            for key in keys_for(buffer.path)
        }
        self.trees: dict[str, ast.Module | None] = {}

    def text_of(self, path: Path) -> str | None:
        """One file's audited content, or None where it cannot be read.

        A caller names its files the way it holds them, usually relative to
        the repository; a checker reports a declaration by the absolute path
        it opened. Both spellings key the same buffer, because the two
        halves of one resolution asking about the same file and getting
        different text is the drift the buffer exists to prevent.
        """
        for key in keys_for(path):
            if key in self.held:
                return self.held[key]
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def tree_of(self, path: Path) -> ast.Module | None:
        """One file's parsed tree, remembered for the rest of the resolution."""
        key = path.as_posix()
        if key not in self.trees:
            text = self.text_of(path)
            try:
                self.trees[key] = None if text is None else ast.parse(text)
            except (SyntaxError, ValueError):
                self.trees[key] = None
        return self.trees[key]


class ClassRecord(BaseModel, frozen=True):
    """One class declaration as its own file states it, before anything is asked.

    ``base_names`` is what this class writes in its own bases, unqualified.
    ``base_positions`` is where each of those names sits, so a later round can
    ask what they lead to — a stub writes `MutableMapping` for a class
    declared somewhere else entirely.
    """

    key: str
    name: str
    path: Path
    line: int
    base_names: list[str]
    base_positions: list[SourcePosition]


def declaring_place(path: Path, line: int) -> str:
    """How one declaring place is named, so two answers about it meet."""
    return f"{path.as_posix()}:{line}"


def asked_place(position: SourcePosition) -> str:
    """How one queried position is named, column included.

    `class Holder(Mapping, Sized)` writes two bases on one line, so a key
    without the column would file both answers under the first.
    """
    return f"{position.path.as_posix()}:{position.line}:{position.column}"


def base_position(path: Path, node: ast.expr) -> SourcePosition | None:
    """Where the name of one base class sits, or None where it names none.

    A subscripted base is asked about at the name it subscripts:
    `dict[str, int]` ends at a bracket, and the class it is about is `dict`.
    """
    match node:
        case ast.Subscript(value=value):
            return base_position(path, value)
    written = name_position(node)
    return (
        None
        if written is None
        else SourcePosition(path=path, line=written["line"], column=written["column"])
    )


class Resolver:
    """One language-server session, answering what subjects are declared as."""

    def __init__(
        self,
        session: LspSession,
        workspace: Workspace,
        depth_limit: int = BASE_DEPTH_LIMIT,
    ) -> None:
        self.session = session
        self.workspace = workspace
        self.depth_limit = depth_limit
        self.classes: dict[str, ClassRecord] = {}
        """Every class declaration this session has read, by the place it sits.

        A whole sweep's mappings resolve to the same `dict` in the same stub,
        and every supertype walk from there re-asks the same questions about
        the same bases. What a place declares is a function of the place, so
        it is read once and the rest are lookups.
        """

        # lup: ignore[dict-str-payload] — the keys are positions in whatever
        # files a checker happened to answer about, which is as open as a
        # key set gets; both sides are places, and neither has other fields
        self.leads: dict[str, str] = {}
        """Which class each queried base name led to, by the position asked."""

    async def sites_at(
        self, method: str, positions: list[SourcePosition | None]
    ) -> list[list[DeclarationSite]]:
        """Ask one request of many positions at once, skipping the absent ones.

        Every document opens before the first question is asked, and the
        whole batch goes out before the first answer is read. Asking one
        position at a time leaves the server idle for a round trip per site,
        and a sweep is hundreds of sites: the questions are independent, so
        nothing is owed the ordering that costs bought.
        """
        asked = [position for position in positions if position is not None]
        if not asked:
            return [[] for _ in positions]
        calls = [
            Call(
                method=method,
                params=await self.session.position_in(
                    position.path,
                    position.line,
                    position.column,
                    self.workspace.text_of(position.path),
                ),
            )
            for position in asked
        ]
        answered = [
            locations_of(answer) for answer in await self.session.requests(calls)
        ]
        replies = iter(answered)
        return [[] if position is None else next(replies) for position in positions]

    def record_at(self, sites: list[DeclarationSite]) -> str | None:
        """Read the first of these places that is a class, and remember it.

        Nothing is asked here: a class's own bases are written in its own
        file, and where they lead is the next round's question rather than
        this one's. Keeping the two apart is what lets a whole level of the
        supertype walk go out in one batch.
        """
        for site in sites:
            tree = self.workspace.tree_of(site.path)
            if tree is None:
                continue
            node = class_at(tree, site.line)
            if node is None:
                continue
            key = declaring_place(site.path, node.lineno)
            if key not in self.classes:
                named = [
                    (name, position)
                    for base in node.bases
                    if (name := base_name(base)) is not None
                    and (position := base_position(site.path, base)) is not None
                ]
                self.classes[key] = ClassRecord(
                    key=key,
                    name=node.name,
                    path=site.path,
                    line=node.lineno,
                    base_names=[name for name, _ in named],
                    base_positions=[position for _, position in named],
                )
            return key
        return None

    def function_at(self, sites: list[DeclarationSite]) -> Declaration:
        """A module-level function at one of these places, or no answer at all.

        What a module-qualified call resolves to, and just as much an answer
        as a class is. Anything else — an assignment, a stub the parse could
        not place — stays unanswered rather than being read as evidence.
        """
        for site in sites:
            tree = self.workspace.tree_of(site.path)
            if tree is None:
                continue
            function = module_function_at(tree, site.line)
            if function is not None:
                return FunctionDeclaration(
                    name=function.name, path=site.path, line=function.lineno
                )
        return UnknownDeclaration()

    async def close_supertypes(self, frontier: list[str]) -> None:
        """Follow every class's bases outward, one whole level per batch.

        A supertype chain is a sequence of questions each depending on the
        answer before it, so walking one class at a time is a round trip per
        link — and a sweep walks hundreds of chains through the same few
        stubs. Level by level, every chain advances together: at most one
        batch per level of the deepest hierarchy, rather than one per link
        per chain.
        """
        seen = dict.fromkeys(frontier)
        for _ in range(self.depth_limit):
            positions = [
                position
                for key in frontier
                for position in self.classes[key].base_positions
                if asked_place(position) not in self.leads
            ]
            if not positions:
                return
            resolved = await self.sites_at("textDocument/definition", list(positions))
            for position, sites in zip(positions, resolved, strict=True):
                found = self.record_at(sites)
                if found is not None:
                    self.leads[asked_place(position)] = found
            reached = [
                self.leads[asked_place(position)]
                for position in positions
                if asked_place(position) in self.leads
            ]
            frontier = [key for key in dict.fromkeys(reached) if key not in seen]
            seen.update(dict.fromkeys(frontier))
            if not frontier:
                return

    def supertypes_of(self, key: str) -> list[str]:
        """Every class name one declaration descends from, however far up.

        Read off what the closure already recorded. A base whose own
        declaration was reached contributes its name and everything above it;
        one that resolved to nothing still contributes the name its subclass
        wrote, which is the whole answer wherever a checker declined to
        follow it.
        """
        reached = dict.fromkeys([key])
        pending = deque([key])
        while pending:
            record = self.classes[pending.popleft()]
            for position in record.base_positions:
                asked = asked_place(position)
                found = self.leads[asked] if asked in self.leads else None
                if found is not None and found not in reached:
                    reached[found] = None
                    pending.append(found)
        return list(
            dict.fromkeys(
                name for above in reached for name in self.classes[above].base_names
            )
        )

    def declaration_for(self, key: str) -> Declaration:
        """What one recorded place is, in the terms a family is read in."""
        record = self.classes[key]
        return ClassDeclaration(
            name=record.name,
            bases=self.supertypes_of(key),
            path=record.path,
            line=record.line,
        )

    async def resolve(self, queries: list[SymbolQuery]) -> list[Declaration]:
        """Answer every query, in as few batches as the questions allow.

        Three phases, each one batch rather than one per site: the members,
        the receivers of whatever the members left unanswered, and then the
        supertype closure over every class either phase reached.
        """
        members = await self.sites_at(
            "textDocument/definition", [query.member for query in queries]
        )
        found = [self.record_at(sites) for sites in members]
        pending = [
            query.receiver if key is None else None
            for query, key in zip(queries, found, strict=True)
        ]
        receivers = await self.sites_at("textDocument/typeDefinition", pending)
        settled = [
            key if key is not None else self.record_at(sites)
            for key, sites in zip(found, receivers, strict=True)
        ]
        await self.close_supertypes(
            [key for key in dict.fromkeys(settled) if key is not None]
        )
        return [
            self.function_at(sites) if key is None else self.declaration_for(key)
            for key, sites in zip(settled, members, strict=True)
        ]


class Shard(BaseModel, frozen=True):
    """One server's share of a sweep, and where in the sweep each answer goes."""

    offsets: list[int]
    queries: list[SymbolQuery]

    async def resolve(
        self, server: Path, root: Path, workspace: Workspace
    ) -> list[Declaration]:
        """Run one language-server session and answer this share in full."""
        async with lsp_session(
            server, root, name=SERVER_NAME, settings=pyright_settings(root)
        ) as session:
            return await Resolver(session, workspace).resolve(self.queries)


def sharded(queries: list[SymbolQuery], workers: int) -> list[Shard]:
    """Split a sweep across servers, keeping every file whole in one share.

    A server pays to open a document and to follow what it imports, so the
    same file asked about from two servers is that cost paid twice. Files are
    the unit the split is made of, dealt largest first so the heaviest one
    cannot land on a share that is already the fullest.
    """
    grouped = {
        path: [
            offset
            for offset, query in enumerate(queries)
            if query.member.path.as_posix() == path
        ]
        for path in {query.member.path.as_posix() for query in queries}
    }
    ranked = sorted(grouped.values(), key=len, reverse=True)
    shares = [
        [offset for group in ranked[worker::workers] for offset in group]
        for worker in range(min(workers, len(ranked)))
    ]
    return [
        Shard(offsets=share, queries=[queries[offset] for offset in share])
        for share in shares
    ]


async def resolve_sharded(
    server: Path,
    root: Path,
    shards: list[Shard],
    buffers: list[SourceBuffer] | None = None,
) -> list[Declaration]:
    """Run every shard's session at once and put the answers back in order."""
    workspace = Workspace(buffers)
    resolved = await asyncio.gather(
        *(shard.resolve(server, root, workspace) for shard in shards)
    )
    found = {
        offset: declaration
        for shard, answers in zip(shards, resolved, strict=True)
        for offset, declaration in zip(shard.offsets, answers, strict=True)
    }
    return [found[offset] for offset in range(len(found))]


class PyrightOracle(TypeOracle):
    """Answers declaration queries by driving `pyright-langserver` over stdio."""

    def __init__(
        self,
        server: Path,
        root: Path,
        timeout: float = RESOLVE_TIMEOUT_SECONDS,
        workers: int = RESOLVE_WORKERS,
    ) -> None:
        self.server = server
        self.root = root
        self.timeout = timeout
        self.workers = workers

    def declarations(
        self,
        queries: list[SymbolQuery],
        buffers: list[SourceBuffer] | None = None,
    ) -> list[Declaration]:
        """Resolve a whole sweep across server sessions, degrading on failure."""
        if not queries:
            return []
        try:
            return asyncio.run(
                asyncio.wait_for(
                    resolve_sharded(
                        self.server,
                        self.root,
                        sharded(queries, self.workers),
                        buffers,
                    ),
                    timeout=self.timeout,
                )
            )
        except (OSError, EOFError, TimeoutError, ValueError) as error:
            typer.echo(
                f"{SERVER_NAME}: {error} — anti-pattern findings keep their "
                "unresolved verdicts",
                err=True,
            )
            return [
                UnknownDeclaration(reason=f"{SERVER_NAME} answered nothing")
                for _ in queries
            ]


def default_oracle() -> TypeOracle | None:
    """The pyright-backed oracle, or None where the language server is absent."""
    server = langserver_path()
    return None if server is None else PyrightOracle(server, project_root())
