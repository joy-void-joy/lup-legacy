"""Selecting which runtime answers a session, as one value.

An application that names a runtime in more than one place can be changed in
only one of them: the sessions it opens would come from Codex while the login
its profile system administers still belonged to Claude. A :class:`Runtime`
is the whole selection — what a session opens through, and where that runtime
keeps a login — so an application holds one and names a provider nowhere.

:class:`SessionRequest` is a declaration, not a configuration. It says what a
caller wants of a session in words every runtime shares; each runtime renders
it into its own shape and states, in its own docstring, what it has no words
for. A field added here is a request every runtime then has to answer.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from lup.policy.hooks import LupHooksConfig
from lup.tools.builtin import BuiltinPreset
from lup.mcp import ToolServer, uniquely_named
from lup.sessions.surface import Agent
from lup.providers.claude.models import ClaudeModel
from lup.providers.codex.models import CodexModel
from lup.providers.catalog import SessionEffort
from lup.providers.login import ProviderLogin
from lup.sessions.events import SubmissionGateResolver
from lup.types import CustomModel, EnvVars, ModelTier
from lup.launch.declaration import NoSandbox, SessionSandbox

type SessionAutonomy = Literal["ask", "accept_edits", "plan", "unattended"]
"""How much a session may do before it stops to ask.

Named for what a caller wants rather than for either runtime's own control:
one spells this as a permission mode over tools, the other as a sandbox its
approvals are decided against, and a caller wanting an unattended session
should not have to know which.
"""

type SessionModel = ClaudeModel | CodexModel | CustomModel | ModelTier
"""Every way a request may name its model.

Either runtime's catalog, an id outside both, or a tier each runtime spells in
its own lineup. A name only one catalog lists is refused by the other runtime
when the request is opened through it, rather than sent to a CLI that does
not know it.
"""


class SessionTools(
    BaseModel, frozen=True, extra="forbid", arbitrary_types_allowed=True
):
    """The tools a request asks for, in the vocabulary every runtime answers.

    Built-ins by preset alone: an exact tool name is one runtime's spelling,
    so a request naming one could only be opened by that runtime. A caller
    that needs exact names declares that runtime's own agent instead.
    """

    builtin: BuiltinPreset = "web"
    mcp: list[ToolServer] = []

    @model_validator(mode="after")
    def servers_are_named_apart(self) -> Self:
        """Refuse two servers under one name, which would address one tool twice."""
        uniquely_named(self.mcp)
        return self


class SessionRequest(
    BaseModel,
    frozen=True,
    arbitrary_types_allowed=True,
    extra="forbid",
    revalidate_instances="always",
):
    """What an application asks of a session, before a runtime renders it."""

    model: SessionModel | None = None
    instructions: str = ""
    """The standing instructions a session opens with, however it spells them."""

    cwd: Path | None = None
    autonomy: SessionAutonomy | None = None
    effort: SessionEffort | None = None
    """How hard the session thinks. Unset, it is the rendered model's own
    default: ``xhigh`` where that runtime's catalog row takes it, and the row's
    highest rung below ``xhigh`` otherwise."""

    sandbox: SessionSandbox = NoSandbox()
    """Which wall this session is opened behind; none unless it says, as it always was."""

    contained_program: Path | None = Field(
        default=None,
        description=(
            "The program an outer session's runtime is started as: the "
            "wrapper that execs the real CLI inside a container, which "
            "`lup.launch.container.contained_cli` writes. Named "
            "here rather than derived, because the image, the mount table "
            "and the login it is built from are the application's, and a "
            "request is a declaration that builds nothing"
        ),
    )

    tools: SessionTools = SessionTools()
    """The built-in tools and MCP servers a session is given; the web alone unset."""

    allowed_tools: list[str] = []
    disallowed_tools: list[str] = []
    """The tools this session may not call, whoever else would admit them.

    The third of three fields that read alike. ``tools`` is the roster a
    session is given, ``allowed_tools`` the part of it that runs without
    being asked about, and this one a refusal that outranks both — which is
    what lets a caller say "everything except this" without enumerating
    everything. A roster states a session's whole reach and has to be
    restated whenever that reach grows; a refusal keeps naming the same tool.
    """

    max_turns: int | None = None
    max_thinking_tokens: int | None = None
    environment: EnvVars = {}
    hooks: LupHooksConfig | None = None

    submission_gate: SubmissionGateResolver | None = None
    """Whether this session's validated output is accepted, asked per turn.

    Portable because it is stated against the turn's output type rather than
    against the tool that carries it — which is the one fact about submission
    the neutral vocabulary cannot hold, every backend spelling that tool its
    own way. A caller that had to reach the adapter's configuration to gate a
    session would name a provider to say something true of both, and would
    gate whichever provider it happened to name.
    """

    @model_validator(mode="after")
    def the_container_is_named_where_it_is_asked_for(self) -> Self:
        """An outer request carries its program, and no other request does.

        Both halves refuse rather than degrade, and they fail in opposite
        directions. A request asking for the container without naming the
        program that enters it would open on the host having asked for a
        boundary. A request naming one without asking for the container
        built a wrapper nothing starts, which reads as containment until
        somebody checks what the session actually ran in.
        """
        contained = self.sandbox.posture().contained()
        if contained and self.contained_program is None:
            raise ValueError(
                "an outer session is started as the program that enters "
                "its container; name it in contained_program"
            )
        if not contained and self.contained_program is not None:
            raise ValueError(
                "contained_program names a container this session does "
                "not open in; declare sandbox=OuterContainer() or drop it"
            )
        return self


type SessionOpener = Callable[[SessionRequest], Agent]
"""Render one request into the agent of one runtime that opens its sessions."""

type WorkspaceHome = Callable[[EnvVars, Path], EnvVars]
"""Give one workspace's sessions a configuration home of their own.

Reads the environment a session will run with — the launching process's own,
with whatever the request names layered over it — and answers the variables
routing this runtime's CLI at a home private to that workspace, so concurrent
sessions cannot read each other's half-written startup document. A profile
naming which account to run as decides the account wherever it was named: in
the request, or in the environment this program was launched with.

Which variable carries it is the runtime's own, which is why this is a field
rather than a shared helper: a session handed another runtime's is pointed at
a directory its CLI never reads, and loses the home its profile chose.

A request's ``HOME`` names no account. With no home named, each runtime's
default is the operator's, joined onto this process's home directory: a
request changes ``HOME`` for a tool its session runs, and the session must
still authenticate as the operator, though the CLI left to choose would read
the request's. Each runtime's default says so where it is chosen.
"""


class Runtime(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One runtime an application selects, end to end.

    A transparent carrier: it decides nothing and composes no seam, so an
    application stores one the way it stores any other declaration, and the
    single assignment naming it is the only place a provider is chosen.

    End to end is what makes the carrier worth having. What a session opens
    through, where the runtime keeps a login, and where a workspace's sessions
    write are three facts about one provider, and an application that reads
    them from three places can be switched in only some of them.
    """

    name: str
    login: ProviderLogin
    open: SessionOpener
    workspace_home: WorkspaceHome

    def session_factory(self, request: SessionRequest) -> Agent:
        """The agent this runtime renders a portable request into."""
        return self.open(self.homed(SessionRequest.model_validate(request)))

    def homed(self, request: SessionRequest) -> SessionRequest:
        """The same request, its sessions pointed at a home of the workspace's own.

        Derived when a session is opened rather than when a request is built.
        A request is a declaration and should cost nothing to state; a home
        is a directory that has to exist, seeded from the account the
        session's environment selects — the request's variables over this
        process's own, as :meth:`workspace_environment` reads them. Deriving
        it here is also what keeps the two from disagreeing: an application
        that built the home into a request would have chosen a runtime before
        naming one, and opening that request through the other would point it
        at a directory no CLI there reads.

        A request naming no working directory is returned untouched — there
        is no workspace to home it against.
        """
        if request.cwd is None:
            return request
        derived = self.workspace_environment(request.environment, request.cwd)
        return request.model_copy(
            update={"environment": {**request.environment, **derived}}
        )

    def workspace_environment(self, environment: EnvVars, workspace: Path) -> EnvVars:
        """Route this runtime's sessions at a home private to that workspace.

        ``environment`` is what a session is handed, and the home is selected
        from what it runs with. Every runtime here starts its CLI with this
        process's own environment beneath the one it is handed, so a home
        this program was launched under reaches the session unless the
        request names another. Selected from the handed variables alone,
        that home goes unseen: the session is routed at one derived from the
        runtime's default instead, and the routing overrides the home it
        would have inherited — so it opens as the default account, or as
        none where the default holds no login, in place of the account it
        was launched as and without a word.

        Only the routing is answered, never the environment it was read
        from, so a request homed here carries what it named and its home
        rather than a copy of this process's variables taken as it opened.
        """
        # lup: ignore[os-environ] — what a session inherits
        inherited = dict(os.environ)
        return self.workspace_home({**inherited, **environment}, workspace)
