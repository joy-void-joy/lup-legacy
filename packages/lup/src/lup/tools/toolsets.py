"""The tool groups one session serves, assembled from a declaration.

A *toolset* is a session's tools sorted into named groups rather than one flat
list. The grouping is what MCP needs — each group becomes one server, the
group's name becomes the server's name, and a tool ``foo`` in group ``notes``
is addressed as ``mcp__notes__foo`` on every backend — and it is what lets a
policy withhold a whole capability at once.

Assembling that is the same work in every project built on this library, so
it lives here rather than in a copy per project: a function naming lup's own
groups, constructing each from the session's directories, and skipping the
ones this session has nothing for. A copy goes stale the moment a group
gains a companion or a builder gains a parameter — the adopter keeps its
call while the pin lands the new library, the half-way arrival this exists
to remove. What a project genuinely decides is *which* groups its sessions
carry and what its own ones are made of, and that is a declaration.

So a group is a name and a builder over :class:`SessionNeeds`, and a builder
that has nothing to build for this session returns nothing — which is how a
sandbox nobody started, a language server nobody installed and a session with
no identity all reach the same answer without a conditional apiece. The names
a subprocess backend is told to serve are then read off the same declaration
rather than listed beside it, so the two cannot disagree.

The groups lup ships tools for come with builders of their own, below: an
adopter names them rather than rebuilding them, and a companion added to one
arrives with the pin. :mod:`lup.mcp` declares each as a server a session
carries, and :mod:`lup.mcp.serve` serves one to a runtime that launched it.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from lup.coordination.policy import COORDINATION_SERVER
from lup.coordination.wake import WakePath
from lup.ledger.models import LedgerEdge, LedgerNode
from lup.ledger.store import LedgerLayout
from lup.orchestration.reflection import ReviewGate
from lup.tools.mcp import (
    LupMcpServerConfig,
    LupMcpTool,
    ServerCompanion,
    create_mcp_server,
)
from lup.tools.policy import BaseToolPolicy


@runtime_checkable
class CodeSandbox(Protocol):
    """A container a session runs code in, as the groups serving it reach it.

    Named by what the groups call rather than by the class that implements it,
    so declaring a session's needs imports no container engine's client — that
    is the sandbox extra's, loaded only where a container is started.
    """

    def create_tools(self, usage_notes: str = "") -> list[LupMcpTool]: ...

    def stop(self) -> None: ...


class SessionNeeds(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """What one session gives its groups to be built out of.

    Everything a builder is allowed to close over, so a group is a function of
    the session rather than of whatever the caller happened to have in scope.
    A field that is None or empty is a capability this session does not have,
    and the builders below answer that by building nothing.
    """

    session_dir: Path
    """Where this session's own state goes: review artifacts, output, notes."""

    root: Path
    """The checkout the session works in, which is what puts it on a roster."""

    gate: ReviewGate
    """The review gate the session's reflection tools share.

    Passed in rather than made here and handed back, because two processes
    have to agree on it: a subprocess-served toolset and the parent that waits
    on the verdict need one flag file, and an in-memory gate is what a session
    assembled in one process wants.
    """

    outputs_dir: Path | None = None
    """Past outputs a reviewer reads for calibration, where there are any."""

    sandbox: CodeSandbox | None = None
    """The session's container, where one was started for it."""

    realtime_dir: Path | None = None
    """The relay mailbox, where this session is persistent."""

    subagent_tool: LupMcpTool | None = None
    """The delegation verb, where this session may open nested agents."""

    member: str = ""
    """What the roster knows this session by, empty where nothing minted one."""

    wake: WakePath = WakePath()
    """What would make this session look, as its own runtime's adapter answers.

    Resolved by the caller rather than here, because it is the one field whose
    value depends on which runtime opened the session, and this model is the
    neutral shape every builder reads. Empty is a session nothing can nudge,
    which is the honest answer for a runtime with no such path and for one
    nobody asked.
    """


def nothing_beside(needs: SessionNeeds) -> list[ServerCompanion]:
    """No companion: what a group keeps running beside itself, for most of them."""
    return []


type Serving = Literal["startup", "session", "named"]
"""When a backend is told to serve one group, of the three moments there are.

``startup`` is every ordinary group: a runtime that starts its own tool
servers when a session opens starts one for each of these, and so does a
backend served over stdio. ``session`` is a group that exists only once the
session has something it needs — a relay mailbox — so a runtime starting
servers up front would start one to serve nothing; it is served when the
session builds it. ``named`` is a group nothing serves unless it is asked for
by name: placeholder tools with fabricated data, a diagnostic surface, what a
live agent should never be handed by default.

Three moments rather than a pair of booleans, because the question each
caller asks is *when*: the harness declaration asks what a runtime starts up
front, the subprocess backend asks what this session has, and the serve loop
asks what a default set leaves out.
"""


class ToolGroup(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One named group of tools, and what a session's own state makes of it."""

    name: str
    """The MCP server's name, which is how every backend addresses its tools."""

    tools: Callable[[SessionNeeds], list[LupMcpTool]]
    """Build this group for one session, or return nothing where it has none.

    Nothing is how a group declines: no sandbox was started, no language
    server is installed, no identity was minted. An absent group is never
    registered and never served, so a declaration carries every group a
    project could have and each session carries the ones it can.
    """

    companions: Callable[[SessionNeeds], list[ServerCompanion]] = nothing_beside
    """What this group's server keeps running for as long as it serves."""

    serving: Serving = "startup"
    """When a backend is told to serve this group, of the three moments there are."""


class SessionToolset(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Every group one session actually carries, and what runs beside each."""

    groups: dict[str, list[LupMcpTool]]
    companions: dict[str, list[ServerCompanion]]


def assembled(groups: list[ToolGroup], needs: SessionNeeds) -> SessionToolset:
    """Build every declared group this session has something to put in it."""
    built = [(group, group.tools(needs)) for group in groups]
    return SessionToolset(
        groups={group.name: tools for group, tools in built if tools},
        companions={
            group.name: companions
            for group, tools in built
            if tools and (companions := group.companions(needs))
        },
    )


def served_names(groups: list[ToolGroup], needs: SessionNeeds) -> list[str]:
    """Which groups a subprocess backend is told to serve for this session.

    Read off the same declaration the assembly reads, and by building the
    groups: a name served for a session that builds the group empty is a
    server the backend starts to find nothing in, and a list kept beside the
    builder is how that goes unnoticed.
    """
    return [
        group.name
        for group in groups
        if group.serving != "named" and group.tools(needs)
    ]


def startup_names(groups: list[ToolGroup]) -> list[str]:
    """Which servers a runtime starts when a session opens, from the declaration.

    Declared rather than built, because this answer is rendered into a native
    tree at generation time: a list that depended on what the generating
    machine had installed would make one checkout's plugin differ from
    another's, and the drift check would call that a stale tree.
    """
    return [group.name for group in groups if group.serving == "startup"]


def named_only(groups: list[ToolGroup]) -> list[str]:
    """Which groups a default set leaves out, being servable only by name."""
    return [group.name for group in groups if group.serving == "named"]


def registered(
    toolset: SessionToolset, groups: list[ToolGroup], policy: BaseToolPolicy
) -> list[LupMcpServerConfig]:
    """One server per group a session registers in the process running it.

    Two decisions: a group servable only by name is not registered, and what
    each server carries is what the policy admits. A runtime's own spelling of a
    server is its adapter's to make — this hands over the neutral
    configuration every adapter is built from.

    Registration alone starts no companion lifecycle, so this path provides
    neither a roster pulse nor receiver-local mailbox relay.
    """
    return [
        create_mcp_server(name, tools=policy.filter_tools(tools))
        for name, tools in toolset.groups.items()
        if name not in named_only(groups)
    ]


def coordination_group(name: str = COORDINATION_SERVER) -> ToolGroup:
    """The repository's own verbs, bound to this session's identity and checkout.

    Built only for a session the roster knows by name. A process with no
    identity would either join as a new member on every call or read somebody
    else's mailbox, and neither is better than having no verbs — so a session
    without one carries no coordination group rather than a broken one.

    The pulse serves beside it, because the server's lifetime is the session's:
    it is started when the session opens and stopped when it ends, however that
    ending comes, which is the one signal a session gives off for free.
    """

    def tools(needs: SessionNeeds) -> list[LupMcpTool]:
        from lup.coordination.peer_tools import create_peer_tools
        from lup.coordination.repository import RepositoryPeers

        if not needs.member:
            return []
        return create_peer_tools(
            RepositoryPeers(needs.root), needs.member, needs.root, wake=needs.wake
        )

    def companions(needs: SessionNeeds) -> list[ServerCompanion]:
        from lup.coordination.peer_tools import RosterPulse
        from lup.coordination.relay import MailboxRelay

        if not needs.member:
            return []
        return [
            RosterPulse(root=needs.root, member_id=needs.member, wake=needs.wake),
            MailboxRelay(root=needs.root, member_id=needs.member),
        ]

    return ToolGroup(name=name, tools=tools, companions=companions)


def ledger_group(
    classes: list[type[LedgerNode]],
    edges: list[type[LedgerEdge]],
    layout: LedgerLayout = LedgerLayout(),
    name: str = "ledger",
) -> ToolGroup:
    """What this session records, under the identity it records as.

    The kinds are the project's own — what it wants written down is its
    domain's question — and the verbs that write them are not. Waits on the
    same identity the coordination group does, and for the same reason: a
    record with no author is provenance nobody can read, which is worse than
    no record.
    """

    def tools(needs: SessionNeeds) -> list[LupMcpTool]:
        from lup.coordination.identity import member_ref
        from lup.ledger.tools import create_ledger_tools

        if not needs.member:
            return []
        return create_ledger_tools(
            needs.root, member_ref(needs.member), classes, edges, layout
        )

    return ToolGroup(name=name, tools=tools)


def codeintel_group(name: str = "codeintel") -> ToolGroup:
    """Resolving a name through a language server, where one is installed."""

    def tools(needs: SessionNeeds) -> list[LupMcpTool]:
        from lup.devtools.dev.pyright_oracle import langserver_path
        from lup.tools.lsp.tools import create_codeintel_tools

        server = langserver_path()
        return [] if server is None else create_codeintel_tools(server, needs.root)

    return ToolGroup(name=name, tools=tools)


def sandbox_group(name: str = "sandbox") -> ToolGroup:
    """The container's own verbs, where a session was given a container."""

    def tools(needs: SessionNeeds) -> list[LupMcpTool]:
        return [] if needs.sandbox is None else needs.sandbox.create_tools()

    return ToolGroup(name=name, tools=tools)


def realtime_group(name: str = "session") -> ToolGroup:
    """The relay a persistent session talks through, where it has a mailbox.

    Wired with a gate of its own, file-backed beside the mailbox: a project
    that asks for reflection before a session sleeps needs the flag where both
    sides of the relay can read it. Reflection stays opt-in — the library
    imposes none by default — and this group exists only for a session that
    was given a mailbox at all.
    """

    def tools(needs: SessionNeeds) -> list[LupMcpTool]:
        from lup.orchestration.reflection import ReflectionGate
        from lup.orchestration.realtime.relay import (
            RealtimeMailbox,
            create_realtime_relay_tools,
        )

        if needs.realtime_dir is None:
            return []
        flag = RealtimeMailbox(needs.realtime_dir).meta_flag_path
        return create_realtime_relay_tools(
            needs.realtime_dir, gate=ReflectionGate(flag_path=flag)
        )

    return ToolGroup(name=name, tools=tools, serving="session")
