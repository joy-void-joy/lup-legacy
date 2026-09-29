"""Codex: the declaration a program writes, and every part it is written with.

A program names Codex parts from here and nowhere deeper: the agent it
declares, the session and turn that agent opens, the program it starts, the
tool servers it launches, the endpoint it may talk to instead. Which module
inside the adapter defines what is the adapter's business, so this module
holds each public part itself rather than pointing at another.

What stays behind in the modules beside this one is the adapter proper. The
runtime side: ``app_server`` is the typed JSON-RPC stdio transport,
``runtime`` opens app-server sessions behind the :mod:`lup.sessions`
contracts, and ``config`` holds profile and compatible-endpoint transforms.
The harness side: ``harness`` renders canonical declarations into the
``.codex`` plugin tree (including the generated policy dispatcher),
``harness_runtime`` probes the CLI, verifies the separately installed plugin
cache, and installs it explicitly, and ``native`` decodes hook payloads into
:mod:`lup.policy` events and renders decisions back to the wire.

Every behavior class in the adapter fills a neutral library contract:
artifact, prompt, invocation, and probe capabilities from
:mod:`lup.harness.contracts`; session, turn, and binding capabilities from
:mod:`lup.sessions.capabilities`; config transforms and profile resolution from
:mod:`lup.providers.config`; and native event decoding and decision rendering
from :mod:`lup.policy.native`. Frozen Pydantic models are the adapter-owned
configuration and evidence data those implementations consume.

Deliberately Codex-only, with no neutral contract:

- :class:`~lup.providers.codex.app_server.CodexAppServer` is the JSON-RPC
  stdio transport to ``codex app-server``. The Claude counterpart is the
  external ``claude_agent_sdk`` package, so no second in-repository
  implementation exists to justify a transport contract.
- :class:`~lup.providers.codex.harness_runtime.CodexPluginInstaller` and the
  plugin cache-evidence helpers install and digest-verify the separately
  cached plugin copy the Codex CLI executes. The Claude launcher runs the
  verified in-repository plugin directory in place, so an installer contract
  would have exactly one possible implementation.
"""

from collections.abc import AsyncIterator, Generator, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Literal, Self, overload

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, model_validator

from lup.harness.models import Harness, HookSet
from lup.harness.requirements import Finding, Manifest
from lup.launch.companions import HostCompanion, named_apart
from lup.launch.declaration import (
    LaunchCommand,
    LaunchStep,
    Member,
    Recording,
    Resume,
    NoSandbox,
    Reopen,
    SessionSandbox,
    declared_policy,
)
from lup.policy.hooks import LupHooksConfig
from lup.providers.codex.model_choice import (
    CodexModelChoice,
    codex_default_effort,
    codex_model_id,
    refuse_unsupported_effort,
)
from lup.providers.codex.models import CodexEffort
from lup.providers.codex.builtins import CodexBuiltins
from lup.mcp import ServeLaunch, ToolServer, uniquely_named
from lup.tools.builtin import BuiltinPreset
from lup.providers.codex.subagents import CodexModelTiers, CodexSubagentTools
from lup.sessions.capabilities import ConversationRecord, ForkSession, SessionEngine
from lup.sessions.events import (
    AnyTurnBlock,
    LiveTurnEvent,
    SessionId,
    SessionSummary,
    SubmissionGateResolver,
    TurnEvent,
    TurnId,
    TurnInput,
    TurnMessage,
    TurnRequest,
    TurnResult,
)
from lup.sessions.layers import SessionLayers
from lup.sessions.middleware import CorrectionConfig
from lup.sessions.turns import LazyTurn, turn_input
from lup.types import EnvVars, JsonObject

CODEX_PROGRAM = Path("codex")
"""The program a Codex session is started as when nothing names another.

Named once because two places read it: the field default below, and the
caller that falls back to it when a request asked for no container to enter.
Spelled twice, the fallback would be a second opinion about what this
runtime is called.
"""

OPENAI_COMPAT_API_KEY_ENV = "LUP_OPENAI_COMPAT_API_KEY"


type CodexBuiltinTool = Literal["Bash", "WebSearch", "apply_patch"]
"""A facility Codex ships: command execution, hosted web search, file patches."""


class CodexTools(BaseModel, frozen=True, extra="forbid", arbitrary_types_allowed=True):
    """What a Codex session may call: which built-ins, and which MCP servers."""

    builtin: BuiltinPreset | list[CodexBuiltinTool] = "web"
    """Codex's own facilities: a preset, or exactly the ones named."""

    mcp: list[ToolServer] = []
    """The MCP servers every session carries, each hosted or started as declared."""

    serve: ServeLaunch = ServeLaunch()
    """How a launched CLI starts the servers lup hosts, each in a process of its own."""

    @model_validator(mode="after")
    def servers_are_named_apart(self) -> Self:
        """Refuse two servers under one name, which would address one tool twice."""
        uniquely_named(self.mcp)
        return self


class CodexMcpServerConfig(BaseModel, frozen=True):
    """One project tool group served to Codex over an explicit subprocess."""

    command: str
    args: list[str] = []
    env: EnvVars = {}
    required: bool = True


class CodexCompatibleEndpoint(BaseModel, frozen=True):
    """All configuration owned by one OpenAI-compatible model provider."""

    identifier: str = "lup_openai_compat"
    name: str | None = None
    base_url: AnyHttpUrl
    api_key: SecretStr | None = None
    api_key_environment: str = OPENAI_COMPAT_API_KEY_ENV

    def native_config(self) -> JsonObject:
        provider: JsonObject = {
            "name": self.name or self.identifier,
            "base_url": str(self.base_url),
        }
        if self.api_key is not None:
            provider["env_key"] = self.api_key_environment
        return {
            "model_provider": self.identifier,
            "model_providers": {self.identifier: provider},
        }


class CodexTurn[T: BaseModel | None]:
    """One turn of a Codex session: await it for the result, iterate its blocks.

    Nothing starts until something asks — an await, an iteration,
    :meth:`events`, :meth:`live`, :meth:`interrupt` or :meth:`steer` — and
    then it starts once, however many ask. Awaiting it again, after iterating
    or after awaiting, returns the same result.
    """

    def __init__(self, turn: LazyTurn[T]) -> None:
        self.turn = turn

    def __await__(self) -> Generator[object, None, TurnResult[T]]:
        return self.turn.result().__await__()

    def __aiter__(self) -> AsyncIterator[AnyTurnBlock]:
        return self.turn.blocks()

    def events(self) -> AsyncIterator[TurnEvent]:
        """Every durable event of this turn, from its first, as they happen."""
        return self.turn.events()

    def live(self) -> AsyncIterator[LiveTurnEvent]:
        """The durable events and the deltas between them, as they happen."""
        return self.turn.live()

    async def interrupt(self) -> None:
        """Stop this turn, returning once it has stopped."""
        await self.turn.interrupt()

    async def steer(self, prompt: str | TurnInput) -> None:
        """Add ``prompt`` to this turn while it runs, without starting another."""
        await self.turn.steer(prompt)


class CodexSession:
    """One open Codex thread, and every turn asked of it."""

    def __init__(
        self,
        engine: SessionEngine,
        record: ConversationRecord,
        forks: ForkSession["CodexSession"],
    ) -> None:
        self.engine = engine
        self.record = record
        self.forks = forks

    @property
    def id(self) -> SessionId:
        """Codex's own identity for this thread, which resumes it."""
        return self.record.identity()

    @overload
    def ask(self, prompt: str | TurnInput) -> CodexTurn[None]: ...

    @overload
    def ask[T: BaseModel](
        self, prompt: str | TurnInput, output: type[T]
    ) -> CodexTurn[T]: ...

    def ask[T: BaseModel](
        self, prompt: str | TurnInput, output: type[T] | None = None
    ) -> CodexTurn[T] | CodexTurn[None]:
        """A turn putting ``prompt`` to this thread, started when first used.

        With ``output`` the turn ends in a final answer validated as that
        model, which its result carries as ``output``; without, ``output`` is
        ``None``.
        """
        if output is None:
            request = TurnRequest[None](input=turn_input(prompt))
            return CodexTurn(LazyTurn(self.engine, request, deltas=True))
        typed = TurnRequest[T](input=turn_input(prompt), output_type=output)
        return CodexTurn(LazyTurn(self.engine, typed, deltas=True))

    async def history(self) -> list[TurnMessage]:
        """Every message of this thread, as the app-server reads it back."""
        return await self.record.messages()

    def fork(
        self, at: TurnId | None = None
    ) -> AbstractAsyncContextManager["CodexSession"]:
        """Open a new thread carrying this one's history, through turn ``at``.

        ``at`` names a turn of this thread by the identifier its result
        carries; unset, the fork carries everything so far.
        """
        return self.forks.fork(at)


type CodexSandbox = Literal["read-only", "workspace-write", "danger-full-access"]
"""Codex's own sandbox modes, which say both how much a session may do and how far."""


class Codex(
    BaseModel,
    frozen=True,
    arbitrary_types_allowed=True,
    extra="forbid",
    revalidate_instances="always",
):
    """One Codex agent, declared whole: what opens its sessions."""

    model: CodexModelChoice | None = None
    """A slug from Codex's catalog, a portable tier, or a custom id."""

    model_tiers: CodexModelTiers = CodexModelTiers()
    """The slug each portable tier selects, for an account or endpoint whose
    lineup differs from the one lup ships as default."""

    system_prompt: str = ""
    """The standing instructions every turn of a session is read under.

    Codex calls these its developer instructions; the shared name is what
    lets a reader move between providers keeping one argument list.
    """

    cwd: Path | None = None
    """Where the session works and what it is sandboxed against.

    ``None`` is where the caller stands when a session opens, read then
    rather than here: read at import, a library loaded before a process
    changed directory would open every session somewhere else.
    """

    policy_root: Path | None = None
    """Application project declaring policy; direct callers default to cwd."""
    executable: Path = CODEX_PROGRAM
    """The program a session's app-server, or a launched CLI, is started as."""
    profile: str | None = None
    """The account every session opens as: a name among the person's lup
    profiles, resolved to that account's Codex home the way the same name
    selects its Claude home, and taking precedence over a home
    ``environment`` names. Unset, sessions stay on the account this process
    already runs under. An unknown name is refused when a session opens,
    listing the known ones."""

    model_provider: str | None = None
    provider_config: JsonObject | None = None
    endpoint: CodexCompatibleEndpoint | None = None
    """An OpenAI-compatible provider the sessions talk to instead of OpenAI."""

    home: Path | None = None
    """The Codex home every session runs in, named outright.

    Wins over ``profile`` the way an explicit directory outranks a name looked
    up; unset, the profile's home, the one this process already runs under,
    or a launch's home for its worktree."""

    sandbox: SessionSandbox = NoSandbox()
    """Which wall every session opens behind; ``NoSandbox()`` is none, the policy alone.

    Unset, ``launch()`` and ``command()`` open inside the verified container
    wherever a container engine answers, and under the inner sandbox with a
    warning where none does. A session opened here inside the container is
    started as the ``executable`` that enters it. The inner sandbox is
    Codex's workspace-write envelope, which has no way out for one command:
    an escapable one, or one excluding commands, is refused."""

    sandbox_mode: CodexSandbox | None = None
    """Codex's own sandbox mode, narrowed further by ``sandbox`` where it is
    narrower, and taken over outright by the container."""

    plugin: Harness | Path | None = None
    """The plugin every session runs with: a harness declaration :meth:`prepare`
    compiles into this project's tree, or the project whose Codex marketplace
    already offers one; either way it is installed into the session's home,
    as ``policy_root`` names it where that is set. Its MCP entries are
    overridden by ``tools.mcp``, the session's roster of servers in both
    compilations."""

    policy: HookSet | None = None
    """The semantic policy judging every call; unset, the plugin harness's.

    A different policy named beside a harness is refused, since the plugin
    already enforces its own. Compiled into hooks the app-server asks for a
    session opened here, and into the plugin's dispatcher for a launched one."""

    requirements: Manifest | None = None
    """What the host and the container are checked for before a launch opens.

    Unset, the roster the ``plugin`` harness declares, where it is one, and
    nothing beyond the runtime's own probes where it is not."""

    identity: Member | None = None
    """Who each session is on the coordination roster; unset, a session opened
    here joins none, and a launched one is named after its worktree."""

    record: Recording | None = None
    """What is kept of each session; unset, a launched session's transcript
    and nothing for a session opened here."""

    resume: Resume | None = None
    """The earlier thread the next session reopens, or a fresh one."""

    max_recursive_agent: int | None = Field(default=None, ge=-1)
    """How many more levels of lup-created agents a session may open, ``-1``
    for no limit; never more than this process has left to give. Unset, the
    allowance this process holds, one level spent."""

    companions: list[HostCompanion] = []
    """What is kept running on the host for as long as each session runs, each
    handing it the environment, folders and ports that reach it; one shared by
    several sessions is started by the first and stopped after the last."""

    # The app-server's own wire spellings, passed through by thread_parameters.
    approval_policy: Literal["untrusted", "on-request", "granular", "never"] | None = (
        None
    )
    """When Codex asks before it acts. A launched session asks the person at
    its terminal; one opened here asks this program, which answers only
    through declared ``hooks``, so opening one that asks without them is
    refused. Unset, Codex's own default."""

    approvals_reviewer: Literal["user", "auto_review"] | None = None
    """Who answers what Codex asks under ``on-request``: the person, or with
    ``auto_review`` a reviewer agent approving or refusing each request in
    their place — Codex's nearest to Claude Code's ``auto`` permission mode.
    It reaches only what the sandbox would stop, so where the container stands
    Codex's own sandbox down it sees side-effecting tool calls and permission
    requests rather than every command. Unset, Codex's own default, the user."""
    hooks: LupHooksConfig | None = None
    effort: CodexEffort | None = None
    """What this session asks the model to spend.

    Unset, the model's own default, which :meth:`resolved_effort` answers — so
    a named model always carries an effort, which :meth:`model_selection`
    explains the need for. Only the default adapts: a rung named here that the
    model lacks is refused.
    """

    environment: EnvVars = {}
    submission_gate_resolver: SubmissionGateResolver | None = None
    correction: CorrectionConfig = CorrectionConfig()
    continuation: CorrectionConfig = CorrectionConfig(
        instruction="Continue according to the Stop hook feedback."
    )
    layers: SessionLayers = SessionLayers()
    """What every session opened here is wrapped in, turn by turn and whole."""

    tools: CodexTools = CodexTools()
    """Which built-ins and MCP servers every session carries; the web alone unset.

    A server this library hosts is answered in this process through the
    thread's own dynamic tools, each named ``lup_app_``, its server and its
    own name; an external one is a subprocess the app-server starts.
    """

    writable_roots: list[Path] = []
    delegated_tools: CodexSubagentTools | None = None

    @model_validator(mode="after")
    def reject_unanswerable_approvals(self) -> Self:
        """Refuse what no Codex session this declares could honour.

        An envelope with a way out for one command, a policy that is not the
        plugin's, companions sharing a name, and delegated tools beside an
        authority they would widen. Whether an asking policy has something
        to answer it depends on how a session opens — a launched one asks
        its person — so that is refused where a session opens here.
        """
        inner = self.sandbox.confinement()
        if inner is not None and (inner.escapable or inner.excluded_commands):
            raise ValueError(
                "Codex's workspace-write envelope has no way out for one "
                "command; declare InnerSandbox() without escapable or "
                "excluded_commands, or judge those commands by the policy"
            )
        declared_policy(self.plugin, self.policy)
        named_apart(self.companions)
        for key in self.provider_config or {}:
            if key not in {"model_provider", "model_providers"}:
                raise ValueError(
                    f"provider_config {key!r} is outside provider endpoint declarations and explicit session authority; use tools"
                )
        if self.delegated_tools is not None and self.tools.builtin != "none":
            raise ValueError(
                "delegated_tools and tools.builtin are alternative authority "
                "declarations; declare tools=CodexTools(builtin='none') beside it"
            )
        if self.delegated_tools is not None and (
            self.sandbox_mode != "read-only" or self.approval_policy != "never"
        ):
            raise ValueError(
                "delegated tools require read-only sandbox and never approvals"
            )
        if self.delegated_tools is not None and (self.tools.mcp or self.writable_roots):
            raise ValueError(
                "delegated tool capabilities do not grant MCP servers or writable roots"
            )
        return self

    def model_selection(self) -> JsonObject:
        """The model and the effort that goes with it, never the model alone.

        A model and its reasoning effort are one choice, and the home this
        session opens against already holds an answer to both: it is seeded
        from the operator's own configuration, so it carries the model *they*
        chose and the effort they chose for it.

        Naming only the model therefore does not select a model. It selects
        half of somebody else's pair, and the API is the first thing to notice
        — ``'max' is not supported with the 'gpt-5.5' model``, a 400 before the
        turn does anything, naming neither the home nor the caller. Every
        Codex session Lup opened through a named model was one home edit away
        from it.

        So the two travel together. Naming a model sends an effort beside it,
        the caller's where they gave one and the model's default where they
        did not, and the default is drawn from the model's own catalog row, so
        the pair is one the catalog says the model takes. An inherited model
        still gets the default effort, sent alone over whichever model the
        home holds.
        """
        model = self.model_id()
        effort = self.resolved_effort()
        selection: JsonObject = {} if model is None else {"model": model}
        return selection if effort is None else {**selection, "effort": effort}

    def model_id(self) -> str | None:
        """The slug the app-server is asked for, or None to inherit the home's."""
        return codex_model_id(self.model, self.model_tiers)

    def resolved_effort(self) -> CodexEffort | None:
        """The effort the app-server is asked for: the one named, or the default.

        The default is :func:`~lup.providers.codex.model_choice.codex_default_effort`'s:
        ``xhigh`` where the model's catalog row takes it, and the row's highest
        rung below that otherwise.
        """
        if self.effort is not None:
            return self.effort
        return codex_default_effort(self.model, self.model_tiers)

    @model_validator(mode="after")
    def the_model_takes_its_effort(self) -> Self:
        """Refuse an effort the catalog says this session's model cannot take.

        Only a named effort can miss: the default is drawn from the model's
        row, and a rung the model lacks is what the API refuses with a 400
        before the first turn, naming neither the caller nor the model.
        """
        refuse_unsupported_effort(self.model, self.effort, self.model_tiers)
        return self

    def builtins(self) -> CodexBuiltins:
        """The facilities sessions start with, a delegated role's or the grant's."""
        if self.delegated_tools is not None:
            return CodexBuiltins(
                shell=self.delegated_tools.workspace_read,
                images=self.delegated_tools.workspace_read,
                web=self.delegated_tools.web_search,
            )
        return CodexBuiltins.compile(self.tools.builtin)

    def workspace(self) -> Path:
        """The directory a session works in: the declared one, or where the caller is."""
        return self.cwd if self.cwd is not None else Path.cwd()

    def open(
        self, resume: SessionId | None = None
    ) -> AbstractAsyncContextManager[CodexSession]:
        """Open a thread, or resume the one ``resume`` names.

        The app-server starts here and the thread with it, so the session
        answers ``id`` from the moment it opens.
        """
        from lup.providers.codex.runtime import CodexSessionOpener

        return CodexSessionOpener(self).open_session(
            Reopen(session=resume) if resume is not None else self.resume
        )

    @overload
    async def ask(self, prompt: str | TurnInput) -> TurnResult[None]: ...

    @overload
    async def ask[T: BaseModel](
        self, prompt: str | TurnInput, output: type[T]
    ) -> TurnResult[T]: ...

    async def ask[T: BaseModel](
        self, prompt: str | TurnInput, output: type[T] | None = None
    ) -> TurnResult[T] | TurnResult[None]:
        """Open a thread, take one turn, and close it however the turn ends."""
        async with self.open() as session:
            if output is None:
                return await session.ask(prompt)
            return await session.ask(prompt, output)

    async def sessions(self) -> list[SessionSummary]:
        """The threads Codex has on record for this agent's workspace, newest first.

        Asked of the app-server this agent's sessions run, under the same
        home, so what it lists is what ``open(resume=...)`` can reach.
        """
        from lup.providers.codex.runtime import codex_sessions

        return await codex_sessions(self)

    def enforced_policy(self) -> HookSet | None:
        """The policy judging every session: the one named, or the plugin harness's."""
        return declared_policy(self.plugin, self.policy)

    def launched(self) -> "Codex":
        """This agent as a launch opens it: every field left unset takes the launch's default.

        Built-in tools left unnamed are Codex's own stock, an unset sandbox is
        the verified container wherever an engine answers (the inner sandbox,
        with a warning, where none does), an unset identity is the worktree's
        name on the roster, and an unset record is the run's transcript.
        """
        from lup.providers.codex.launch import codex_launched

        return codex_launched(self)

    def command(self, *words: str) -> LaunchCommand:
        """The exact ``(argv, env, cwd)`` a launch of this agent runs, ``words`` passed through.

        Compiled from the same fields ``open()`` reads. What the argv depends
        on is settled first, as a launch settles it: the boundary is measured
        and recorded, and an outer container's image and egress are made
        ready, since the argv names them. Nothing is run in it.
        """
        from lup.providers.codex.launch import codex_command

        return codex_command(self, list(words))

    def prepare(self, force: bool = False) -> None:
        """Compile the plugin, install it into the home, and trust it, launching nothing.

        ``force`` reinstalls a plugin whose version has not moved.
        """
        from lup.providers.codex.launch import prepare_codex

        prepare_codex(self, force)

    def check(self) -> list[Finding]:
        """Probe the installed CLI, the declared requirements and the login.

        Answers every finding, and raises :class:`~lup.launch.refusal.LaunchRefused`
        where one a launch needs is missing.
        """
        from lup.providers.codex.launch import check_codex

        return check_codex(self)

    def launch(
        self, *words: str, steps: Sequence[LaunchStep] = (), force: bool = False
    ) -> int:
        """Prepare, check, and run Codex in the foreground, then clean up.

        The terminal is the session's until it ends; ``words`` reach the CLI
        after everything the declaration compiles to, and ``steps`` run around
        the whole of it — each ``before`` first, each ``after`` last, however
        the session ended. ``force`` reinstalls a plugin whose version has not
        moved into the home the session opens in, as :meth:`prepare` does.
        Answers the CLI's exit status.
        """
        from lup.providers.codex.launch import launch_codex_session

        return launch_codex_session(self, list(words), steps, force)

    def layered(self, layers: SessionLayers) -> Self:
        """This agent with ``layers`` laid over its own, the fields set there winning."""
        return self.model_copy(update={"layers": layers.over(self.layers)})
