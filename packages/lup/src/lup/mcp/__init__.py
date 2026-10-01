"""The MCP servers a session carries, each declared as a value.

A server is named in an agent's tools and read twice. A session this process
opens hosts it, built against what that session has — its checkout, its roster
identity, its container. A session a runtime's own CLI launches starts it
instead, as the transport that launch declares: a stdio subprocess serving it,
for anything lup hosts. Both answers come from one value, so the session a
library opens and the one a terminal launches carry the same servers, and
neither is a list kept beside the other.

:meth:`ToolServer.hosted` is the first reading and :meth:`ToolServer.launched`
the second, whose subprocess runs :mod:`lup.mcp.serve`. The servers lup ships are its tool groups, named: :class:`Coordination`,
:class:`Ledger`, :class:`CodeIntel` and :class:`Sandbox`. A project's own
tools are a :class:`Toolset` of its ``@lup_tool`` handlers, or a
:class:`Group` naming a builder that closes over the session. A server this
library does not host is an :class:`External` transport.

A hosted server reaches its subprocess as itself: the command carries the
server's class and fields, and the subprocess validates them back into the
same declaration. That is why a :class:`Toolset` names module-level tools and
a :class:`Group` a module-level builder — an import path is what crosses a
process boundary, and a closure built at runtime has none.
"""

import asyncio
import os
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from importlib import import_module
from pathlib import Path
from typing import Annotated, ClassVar, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ImportString,
    PlainSerializer,
    TypeAdapter,
    model_validator,
)

from lup.coordination.bare.runtime import runtime_of
from lup.coordination.identity import MEMBER_ENV, mint_member_id
from lup.coordination.policy import COORDINATION_SERVER
from lup.coordination.repository import runtime_member
from lup.harness.environment import tool_server_env
from lup.ledger.models import LedgerEdge, LedgerNode
from lup.ledger.store import LedgerLayout
from lup.orchestration.reflection import ReviewGate
from lup.tools.mcp import (
    LupMcpServerConfig,
    LupMcpTool,
    McpServerEntry,
    RawMcpServerConfig,
    RawStdioServerConfig,
    ServerCompanion,
    create_mcp_server,
)
from lup.tools.toolsets import (
    CodeSandbox,
    SessionNeeds,
    ToolGroup,
    codeintel_group,
    coordination_group,
    ledger_group,
    sandbox_group,
)
from lup.types import EnvVars, JsonObject


def tool_path(tool: LupMcpTool) -> str:
    """Where a tool is imported from, so a subprocess can serve the same one.

    Read off the handler the decorator wrapped: ``@lup_tool`` on a module-level
    function leaves the tool under that function's name in its module. A tool
    built inside a function has no such name, and serving it elsewhere would
    mean serving something else, so it is refused here rather than there.
    """
    handler = tool.call_handler
    module, name = handler.__module__, handler.__qualname__
    if getattr(import_module(module), name, None) is not tool:
        raise ValueError(
            f"tool {tool.name!r} is not a module-level @lup_tool, so a served "
            "Toolset cannot import it; declare it at module level, or serve the "
            "builder that makes it as a Group"
        )
    return f"{module}:{name}"


def imported_tool(value: LupMcpTool | str) -> LupMcpTool:
    """A tool given directly, or the one its import path names."""
    match value:
        case str():
            return TypeAdapter(
                ImportString[LupMcpTool],
                config=ConfigDict(arbitrary_types_allowed=True),
            ).validate_python(value)
        case _:
            return value


type ServedTool = Annotated[
    LupMcpTool,
    BeforeValidator(imported_tool),
    PlainSerializer(tool_path, return_type=str, when_used="json"),
]
"""A tool a server carries, crossing a process boundary as its import path."""

type NeedsHook = Callable[[SessionNeeds, str | None], SessionNeeds]
"""What a project adds to a served session's needs, given the runtime serving it."""


class ServeLaunch(BaseModel, frozen=True):
    """How a launched runtime starts the servers lup hosts, and for which session.

    Where :meth:`ToolServer.hosted` is handed a session, a launched runtime
    starts each server as a subprocess before any session object exists, so
    what that subprocess serves is named on its command line: the session's
    name, the runtime answering for it, and the hook through which a project
    adds what only it can build.
    """

    program: list[str] = Field(
        default_factory=lambda: [sys.executable, "-m", "lup.mcp.serve"]
    )
    """The command serving one server, before its options.

    The interpreter running this process by default, which is the one that has
    lup installed. A project with a composed CLI names that instead, which is
    how its sub-app and this module stay one command.
    """

    session: str | None = None
    """The session the server opens where no launcher relayed one."""

    runtime: str | None = None
    """The runtime whose adapter says who this session is and what wakes it."""

    needs: ImportString[NeedsHook] | None = None
    """What a project adds to the session's needs, named by its import path.

    A module-level callable taking the resolved
    :class:`~lup.tools.toolsets.SessionNeeds` and the runtime and returning
    them refined — a container its settings start, a delegation verb its
    specs build — for the groups that read them.
    """

    environment: EnvVars = {}
    """The environment the server process starts with."""

    startup_timeout_seconds: float | None = None
    """How long each server it starts gets to come up before a runtime abandons it.

    Declared with the launch rather than the machine, because the answer
    belongs to how the servers start: a command that resolves its package
    before importing anything is slow on a cold checkout and instant on a
    warm one, while a runtime's own default is chosen for a server already
    installed. Missing it drops the server and keeps the session, so it
    arrives as a tool group simply absent. Unset leaves the runtime's default.
    """

    def options(self) -> list[str]:
        """The options naming this launch's session, runtime and needs hook."""
        hook = self.model_dump(mode="json", include={"needs"})["needs"]
        return [
            *(["--session", self.session] if self.session is not None else []),
            *(["--runtime", self.runtime] if self.runtime is not None else []),
            *(["--needs", hook] if isinstance(hook, str) else []),
        ]

    def command(self, server: "HostedServer") -> RawStdioServerConfig:
        """The stdio transport that starts ``server`` in a process of its own."""
        program, *arguments = self.program
        return RawStdioServerConfig(
            command=program,
            args=[*arguments, *self.options(), *server.served().arguments()],
            env=dict(self.environment),
        )


class ToolServer(
    BaseModel, ABC, frozen=True, extra="forbid", arbitrary_types_allowed=True
):
    """One MCP server a session carries, read by both of its compilations."""

    name: str
    """The server's name, which addresses its tools as ``mcp__<name>__<tool>``."""

    always_load: bool = False
    """Whether this server's tools are offered from the first turn, never deferred.

    A runtime that withholds tool definitions until a search asks for them
    spends a search call each time a deferred tool is wanted: a fair price for
    a server reached now and then, the wrong one for tools a session calls
    dozens of times. Claude spells it per server in both compilations; Codex
    documents no per-server loading control, so neither of its compilations
    says anything for it.
    """

    requires: ClassVar[str] = "nothing"
    """What a session must have for this server to build anything, for a refusal."""

    @abstractmethod
    def hosted(self, needs: SessionNeeds) -> McpServerEntry | None:
        """This server in a session opened here, or None where it has nothing."""

    @abstractmethod
    def launched(self, launch: ServeLaunch) -> RawMcpServerConfig:
        """This server as a runtime's own CLI starts it."""

    def tool_names(self) -> list[str] | None:
        """Every tool this server serves, where that is known before it is built.

        None where building decides it, which is every group whose tools
        depend on the session; a grant naming one of those is judged against
        the server's name alone until the session opens.
        """
        return None

    def launcher_variables(self) -> list[str]:
        """What this server reads from the environment its session's launcher made.

        Nothing by default: a server passed through as declared reads what its
        own declaration gives it, and a launched session's identity is not
        its to be handed.
        """
        return []

    def startup_timeout(self, launch: ServeLaunch) -> float | None:
        """How long a runtime waits for this server to come up, where a launch says.

        Nothing by default: a server passed through as declared starts however
        its own transport does, which ``launch`` says nothing about.
        """
        del launch
        return None


class HostedServer(ToolServer, ABC, frozen=True):
    """A server lup builds out of one tool group, in process or in a subprocess."""

    @abstractmethod
    def group(self) -> ToolGroup:
        """The tool group this server serves."""

    def hosted(self, needs: SessionNeeds) -> LupMcpServerConfig | None:
        group = self.group()
        tools = group.tools(needs)
        if not tools:
            return None
        return create_mcp_server(
            self.name, tools=tools, companions=group.companions(needs)
        )

    def launched(self, launch: ServeLaunch) -> RawMcpServerConfig:
        return launch.command(self)

    def launcher_variables(self) -> list[str]:
        """The roster identity and recursion allowance of the session it serves.

        Whichever group it serves, it is one process of the launched session,
        so it answers to that session's roster identity and spends that
        session's recursion allowance.
        """
        return tool_server_env()

    def startup_timeout(self, launch: ServeLaunch) -> float | None:
        """The launch's deadline, since the launch is what starts this server."""
        return launch.startup_timeout_seconds

    def served(self) -> "ServedServer":
        """This server as a serve command names it: its class and its fields."""
        return ServedServer(
            server=type(self),
            fields=self.model_dump(mode="json", exclude_defaults=True),
        )


class ServedServer(BaseModel, frozen=True):
    """A hosted server as its subprocess receives it, and validates it back."""

    server: ImportString[type[HostedServer]]
    fields: JsonObject = {}

    def arguments(self) -> list[str]:
        """The serve command's arguments: the class, then its non-default fields."""
        path = f"{self.server.__module__}:{self.server.__qualname__}"
        fields = TypeAdapter(JsonObject).dump_json(self.fields).decode()
        return [path, fields] if self.fields else [path]

    def built(self) -> HostedServer:
        """The declaration this names, as the process that launched it held it."""
        return self.server.model_validate(self.fields)


class Coordination(HostedServer, frozen=True):
    """The repository's roster verbs, bound to this session's identity."""

    name: str = COORDINATION_SERVER
    requires: ClassVar[str] = "a roster identity"

    def group(self) -> ToolGroup:
        return coordination_group(self.name)


class Ledger(HostedServer, frozen=True):
    """What this session records, as the node and edge kinds a project declares."""

    name: str = "ledger"
    nodes: list[ImportString[type[LedgerNode]]] = []
    edges: list[ImportString[type[LedgerEdge]]] = []
    layout: LedgerLayout = LedgerLayout()
    requires: ClassVar[str] = "a roster identity to record as"

    def group(self) -> ToolGroup:
        return ledger_group(self.nodes, self.edges, self.layout, self.name)


class CodeIntel(HostedServer, frozen=True):
    """Names resolved through a language server, where one is installed."""

    name: str = "codeintel"
    requires: ClassVar[str] = "a language server installed on this machine"

    def group(self) -> ToolGroup:
        return codeintel_group(self.name)


class ContainerLifetime(ServerCompanion, frozen=True, arbitrary_types_allowed=True):
    """Stop a container this server started, when the server stops."""

    sandbox: CodeSandbox

    async def run(self) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.to_thread(self.sandbox.stop)


class Sandbox(HostedServer, frozen=True):
    """Code execution in a container: the session's own, or one started for it.

    A session given a container serves that one. Otherwise the server starts
    one of its own, named for the session, and stops it when it stops serving,
    so the container lives exactly as long as the tools that reach it.
    """

    name: str = "sandbox"

    def group(self) -> ToolGroup:
        return sandbox_group(self.name)

    def hosted(self, needs: SessionNeeds) -> LupMcpServerConfig | None:
        """The container's verbs, starting one where the session was given none.

        The container engine's client is imported here, where one is started,
        which is what keeps it the sandbox extra's rather than every agent's.
        """
        from lup.sandbox import container

        if needs.sandbox is not None:
            return super().hosted(needs)
        started = container.Sandbox(
            session_id=needs.session_dir.name,
            shared_dir=needs.session_dir / "sandbox_shared",
        )
        given = needs.model_copy(update={"sandbox": started})
        return create_mcp_server(
            self.name,
            tools=self.group().tools(given),
            companions=[ContainerLifetime(sandbox=started)],
        )


class Toolset(HostedServer, frozen=True):
    """A project's own ``@lup_tool`` handlers, served under one name."""

    name: str = "tools"
    tools: list[ServedTool] = []
    requires: ClassVar[str] = "at least one tool"

    def __init__(
        self,
        tools: Sequence[LupMcpTool] = (),
        *,
        name: str = "tools",
        always_load: bool = False,
    ) -> None:
        BaseModel.__init__(self, tools=list(tools), name=name, always_load=always_load)

    @model_validator(mode="after")
    def tools_are_named_apart(self) -> Self:
        """Refuse two tools under one name, which one server cannot address."""
        names = [tool.name for tool in self.tools]
        if len(names) != len(dict.fromkeys(names)):
            raise ValueError(f"toolset {self.name!r} names a tool twice: {names}")
        return self

    def group(self) -> ToolGroup:
        carried = list(self.tools)
        return ToolGroup(name=self.name, tools=lambda _: carried)

    def tool_names(self) -> list[str] | None:
        return [tool.name for tool in self.tools]


class Group(HostedServer, frozen=True):
    """A tool group a project declares, built over whatever its session has.

    For tools that close over a session — its notes directory, its review
    gate, a verb its own specs build — which a module-level :class:`Toolset`
    cannot hold. Named by its builder, which the serve command imports.
    """

    builder: ImportString[Callable[[], ToolGroup]]
    requires: ClassVar[str] = "whatever its builder reads from the session"

    def __init__(
        self,
        builder: Callable[[], ToolGroup] | str,
        *,
        name: str | None = None,
        always_load: bool = False,
    ) -> None:
        """Declare ``builder``'s group, under its own name unless ``name`` is given.

        ``builder`` is the callable, or its import path as a serve command
        carries it.
        """
        resolved = TypeAdapter(ImportString[Callable[[], ToolGroup]]).validate_python(
            builder
        )
        BaseModel.__init__(
            self,
            builder=resolved,
            name=resolved().name if name is None else name,
            always_load=always_load,
        )

    def group(self) -> ToolGroup:
        return self.builder()


class External(ToolServer, frozen=True):
    """A server this library does not host: a transport, passed through as-is."""

    server: RawMcpServerConfig

    def hosted(self, needs: SessionNeeds) -> McpServerEntry | None:
        return self.server

    def launched(self, launch: ServeLaunch) -> RawMcpServerConfig:
        return self.server


def opened_needs(root: Path, session_dir: Path, environment: EnvVars) -> SessionNeeds:
    """What a session this process opens gives the servers it hosts.

    Its identity is the one its environment names, where a launcher minted
    one for it, and a new one otherwise: a session opened here is a session,
    and joins the roster as itself rather than as whoever opened it. A
    process that inherited the launcher's id from another session's shell —
    a pipeline run there — is not that session, and its sessions are a
    member of their own, spawned by it.
    """
    launched = environment[MEMBER_ENV] if MEMBER_ENV in environment else ""
    member = runtime_member(root, launched, mint_member_id(), runtime_of(os.getpid()))
    return SessionNeeds(
        session_dir=session_dir,
        root=root,
        gate=ReviewGate(),
        member=member.member_id,
        spawned_by=member.spawned_by,
    )


def hosted_servers(
    servers: Sequence[ToolServer], needs: SessionNeeds
) -> dict[str, McpServerEntry]:
    """Every declared server built for one session, refusing one with nothing.

    A server declared by name and built empty is a capability the caller asked
    for and did not get, so it is refused with what it needed rather than
    dropped from the session in silence.
    """

    def built(server: ToolServer) -> McpServerEntry:
        entry = server.hosted(needs)
        if entry is None:
            raise ValueError(
                f"tool server {server.name!r} has nothing to serve this session; "
                f"it needs {server.requires}"
            )
        return entry

    return {server.name: built(server) for server in servers}


def server_grants(name: str, servers: Sequence[ToolServer]) -> bool:
    """Whether ``name`` is a tool one of these servers serves, as declared.

    Exact where a server knows its tools before it is built, and by the
    server's prefix where building decides them.
    """
    return any(
        name in [f"mcp__{server.name}__{tool}" for tool in known]
        if (known := server.tool_names()) is not None
        else name.startswith(f"mcp__{server.name}__")
        for server in servers
    )
