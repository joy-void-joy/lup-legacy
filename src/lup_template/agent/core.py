"""Application composition roots over Lup's provider-neutral runtime."""

import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import AnyHttpUrl, BaseModel, SecretStr

from lup.channels.models import utc_now
from lup.providers.claude import ClaudeCompatibleEndpoint
from lup.launch.declaration import InnerSandbox, NoSandbox
from lup.providers.claude.config import ClaudeCompatibilityTransform
from lup.providers.claude.model_choice import ClaudeModelChoice, claude_model_choice
from lup.providers.claude import (
    Claude,
    ClaudeBuiltinTool,
    ClaudeTools,
    SESSION_THINKING_TOKENS,
)
from lup.tools.builtin import BuiltinPreset
from lup.providers.claude.subagents import subagent_builtins as claude_subagent_builtins
from lup.providers.codex import CodexCompatibleEndpoint
from lup.providers.codex.config import CodexCompatibilityTransform
from lup.providers.codex.model_choice import CodexModelChoice, codex_model_choice
from lup.providers.codex import Codex, CodexTools
from lup.providers.codex.subagents import subagent_tools as codex_subagent_tools
from lup.sessions.layers import CleanupWrapper, SessionLayers
from lup.sessions.surface import Agent
from lup.sessions.composition import submission_gate_resolver
from lup.policy.hooks import LupHooksConfig
from lup.sessions.events import (
    SessionId,
    SubmissionDecision,
    SubmissionGate,
    SubmissionGateResolver,
    TurnResult,
)
from lup.observability.cost import per_mtok_usage_cost
from lup.sessions.middleware import (
    BudgetConfig,
    CorrectionConfig,
    DisplayConfig,
    DisplayRecord,
    PersistenceConfig,
    TimeoutConfig,
    TraceRecord,
    TracingConfig,
)
from lup.mcp import External, ServeLaunch, ToolServer, Toolset
from lup.tools.mcp import LupMcpServerConfig, McpServerEntry
from lup.observability.metrics import (
    MetricsSummary,
    log_metrics_summary,
    read_metrics_summary,
    reset_metrics,
)
from lup.observability.sessions import (
    CloseRecordingWrapper,
    SessionRecorder,
    session_recorder,
)
from lup.observability.trace import TraceLogger
from lup.types import (
    CustomModel,
    PayloadText,
    SubagentCapability,
    SubagentSpec,
    ToolGrant,
    Usage,
    UsageCost,
)
from lup.workspace.history import save_session
from lup.workspace.notes import NotesConfig, session_gate_flag, setup_notes
from lup.workspace.paths import agent_version, project_root
from lup_template.agent.config import (
    compat_api_key,
    compat_base_url,
    engine_for_settings,
    settings,
)
from lup_template.agent.models import AgentOutput, AgentSessionResult
from lup_template.agent.prompts import get_system_prompt

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from lup.orchestration.reflection import ReviewGate
    from lup.sandbox.container import Sandbox


class PersistentSessionResult(BaseModel):
    """Number of wake-driven turns completed by a persistent session."""

    turns: int


class SessionBuild(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """The configured provider-neutral agent and its application workspace."""

    factory: Agent
    notes: NotesConfig
    trace_logger: TraceLogger
    recorder: SessionRecorder | None = None
    """Where this session's record and its outputs are indexed, or nothing."""


def reflection_submission_gate(gate: "ReviewGate") -> SubmissionGate[AgentOutput]:
    """Adapt the domain review flag to portable turn submission semantics."""

    async def decide(_output: AgentOutput) -> SubmissionDecision:
        if gate.reflected:
            return SubmissionDecision(accepted=True)
        return SubmissionDecision(
            accepted=False,
            message="Call the review tool and address its verdict before submitting.",
        )

    return decide


def build_usage_cost(model: str | None = None) -> UsageCost | None:
    """Build configured token pricing without coupling it to an adapter."""
    if engine_for_settings(model) in ("claude", "claude-compat"):
        return reported_usage_cost
    if (
        settings.codex_usd_per_mtok_input is None
        or settings.codex_usd_per_mtok_output is None
    ):
        return None
    return per_mtok_usage_cost(
        input_usd=settings.codex_usd_per_mtok_input,
        output_usd=settings.codex_usd_per_mtok_output,
        cached_input_usd=settings.codex_usd_per_mtok_cached_input,
    )


def reported_usage_cost(usage: Usage) -> float:
    """Use the complete cost reported by providers that supply one."""
    if usage.cost_usd is None:
        raise ValueError("the provider completed without reporting turn cost")
    return usage.cost_usd


def provider_factory(
    *,
    model: str | None,
    system_prompt: str,
    cwd: Path,
    builtin: BuiltinPreset = "none",
    mcp: list[ToolServer] | None = None,
    allowed_tools: list[str] | None = None,
    hooks: LupHooksConfig | None = None,
    add_dirs: list[Path] | None = None,
    session_defaults: bool = True,
    submission_gate: SubmissionGateResolver | None = None,
    writable_roots: list[Path] | None = None,
    subagents: list[SubagentSpec] | None = None,
    thinking_budget: int | None = None,
    max_turns: int | None = None,
    role: SubagentSpec | None = None,
) -> Agent:
    """The one application-owned provider selection boundary.

    Native identifiers are intentionally confined to this concrete composition
    root. Every caller above it receives only a configured ``Agent``.
    """
    engine = engine_for_settings(model)
    logger.info(
        "Engine %s runs model %s (AGENT_SDK %s)",
        engine,
        model,
        settings.agent_sdk or "unset — routed by model",
    )
    if engine in ("claude", "claude-compat"):
        # A configured id is checked against Claude Code's catalog, except on
        # a compatible endpoint, whose model ids are its own to name.
        claude_model: ClaudeModelChoice | None = (
            None
            if model is None
            else claude_model_choice(model)
            if compat_base_url() is None
            else CustomModel(id=model)
        )
        roster: BuiltinPreset | list[ClaudeBuiltinTool] = builtin
        if role is not None:
            roster = claude_subagent_builtins(role)
            allowed_tools = list(roster)
            if role.model != "inherit":
                if compat_base_url() is not None:
                    raise ValueError(
                        "compatible endpoints require inherited role models"
                    )
                claude_model = role.model
        if claude_model is None:
            if compat_base_url() is not None:
                raise ValueError("AGENT_MODEL is required for a compatible endpoint")
            claude_model = "strongest"
        config = Claude(
            model=claude_model,
            system_prompt=system_prompt,
            tools=ClaudeTools(builtin=roster, mcp=mcp or []),
            allowed_tools=allowed_tools or [],
            permission_mode=(
                settings.permission_mode
                if settings.permission_mode is not None
                else "bypassPermissions"
                if session_defaults
                else None
            ),
            max_turns=(
                max_turns
                if max_turns is not None
                else settings.max_turns
                if session_defaults
                else None
            ),
            max_thinking_tokens=(
                thinking_budget
                if thinking_budget is not None
                else settings.max_thinking_tokens
                if session_defaults and settings.max_thinking_tokens is not None
                else SESSION_THINKING_TOKENS
                if session_defaults
                else None
            ),
            effort=settings.reasoning_effort,
            cwd=cwd,
            add_dirs=add_dirs or list(settings.extra_dirs),
            environment=(
                {"ENABLE_TOOL_SEARCH": settings.tool_search}
                if settings.tool_search is not None
                else {}
            ),
            sandbox=InnerSandbox() if session_defaults else NoSandbox(),
            hooks=hooks,
            submission_gate_resolver=submission_gate,
            subagents=subagents or [],
            setting_sources=[] if role is not None else None,
        )
        endpoint = compat_base_url()
        if endpoint is not None:
            config = ClaudeCompatibilityTransform(
                ClaudeCompatibleEndpoint(
                    base_url=AnyHttpUrl(endpoint),
                    api_key=(
                        SecretStr(key)
                        if (key := compat_api_key()) is not None
                        else None
                    ),
                )
            ).apply(config)
        return config

    if engine in ("codex", "openai", "openai-compat"):
        unsupported = [
            name
            for name, value in [
                (
                    "AGENT_PERMISSION_MODE",
                    settings.permission_mode if session_defaults else None,
                ),
                ("AGENT_MAX_TURNS", settings.max_turns if session_defaults else None),
                (
                    "AGENT_MAX_THINKING_TOKENS",
                    settings.max_thinking_tokens if session_defaults else None,
                ),
                ("max_turns", max_turns),
                ("thinking_budget", thinking_budget),
            ]
            if value is not None
        ]
        if allowed_tools:
            unsupported.append("allowed_tools")
        if subagents:
            unsupported.append("native subagents (serve run_subagent instead)")
        if unsupported:
            raise ValueError(
                "Codex app-server cannot honor configured option(s): "
                + ", ".join(unsupported)
            )
        delegated_tools = codex_subagent_tools(role) if role is not None else None
        # A configured id is checked against Codex's catalog on the native
        # engine; an OpenAI-compatible provider names its own models.
        codex_model: CodexModelChoice | None = (
            None
            if model is None
            else codex_model_choice(model)
            if engine == "codex"
            else CustomModel(id=model)
        )
        if role is not None and role.model != "inherit":
            if engine != "codex":
                raise ValueError("compatible endpoints require inherited role models")
            codex_model = role.model
        if codex_model is None:
            if engine != "codex":
                raise ValueError("AGENT_MODEL is required for a compatible endpoint")
            codex_model = "strongest"
        config = Codex(
            model=codex_model,
            system_prompt=system_prompt,
            cwd=cwd,
            sandbox_mode=(
                "read-only"
                if delegated_tools is not None
                else normalize_codex_sandbox(settings.codex_sandbox)
                or ("workspace-write" if session_defaults else None)
            ),
            # Hooks are what the app-server puts its approval requests to, so
            # a session carrying them asks and a session without them cannot.
            approval_policy=(
                "never"
                if delegated_tools is not None
                else normalize_codex_approval(settings.codex_approval_policy)
                or ("on-request" if hooks is not None else "never")
            ),
            hooks=hooks,
            effort=settings.codex_effort or settings.reasoning_effort,
            submission_gate_resolver=submission_gate,
            tools=CodexTools(
                builtin="none" if delegated_tools is not None else builtin,
                mcp=mcp or [],
            ),
            writable_roots=writable_roots or [],
            delegated_tools=delegated_tools,
        )
        endpoint = compat_base_url()
        if engine in ("openai", "openai-compat"):
            if endpoint is None:
                raise ValueError(
                    "OPENAI_BASE_URL is required for an OpenAI-compatible route"
                )
            config = CodexCompatibilityTransform(
                CodexCompatibleEndpoint(
                    identifier=settings.openai_model_provider or "lup_openai_compat",
                    base_url=AnyHttpUrl(endpoint),
                    api_key=(
                        SecretStr(key)
                        if (key := compat_api_key()) is not None
                        else None
                    ),
                )
            ).apply(config)
        return config

    raise ValueError(f"unsupported engine {engine!r}")


def normalize_codex_sandbox(
    value: str | None,
) -> Literal["read-only", "workspace-write", "danger-full-access"] | None:
    """Translate the documented environment spelling once at composition."""
    aliases = {
        None: None,
        "read_only": "read-only",
        "workspace_write": "workspace-write",
        "danger_full_access": "danger-full-access",
        "read-only": "read-only",
        "workspace-write": "workspace-write",
        "danger-full-access": "danger-full-access",
        "readOnly": "read-only",
        "workspaceWrite": "workspace-write",
        "dangerFullAccess": "danger-full-access",
    }
    try:
        return aliases[value]
    except KeyError as error:
        raise ValueError(f"unsupported Codex sandbox {value!r}") from error


def normalize_codex_approval(
    value: str | None,
) -> Literal["untrusted", "on-request", "granular", "never"] | None:
    """Validate the Codex approval policy before model construction.

    The asking policies are answerable because the adapter replies to the
    app-server's approval requests from a session's declared hooks, so they
    are settings rather than refusals. The value is the app-server's own
    spelling, and any other is refused with the four it accepts.
    """
    if value is None:
        return None
    match value:
        case "untrusted" | "on-request" | "granular" | "never":
            return value
    raise ValueError(
        f"Codex approval policy {value!r} is not one the app-server accepts; "
        "use 'never', 'on-request', 'untrusted', or 'granular'"
    )


def decorate_factory(
    agent: Agent,
    *,
    notes: NotesConfig | None = None,
    trace_logger: TraceLogger | None = None,
    timeout_seconds: float | None = None,
    model: str | None = None,
) -> Agent:
    """Lay complete-logical-turn governance over the agent, in its explicit order."""
    usage_cost = build_usage_cost(model)
    budget = None
    if settings.max_budget_usd is not None:
        if usage_cost is None:
            raise ValueError(
                "a budget requires CODEX_USD_PER_MTOK_INPUT and "
                "CODEX_USD_PER_MTOK_OUTPUT"
            )
        budget = BudgetConfig(
            maximum_usd=settings.max_budget_usd,
            usage_cost=usage_cost,
        )
    seconds = (
        timeout_seconds
        if timeout_seconds is not None
        else settings.turn_timeout_seconds
    )
    timeout = TimeoutConfig(seconds=seconds) if seconds is not None else None
    persistence = None
    tracing = None
    display = None
    if notes is not None and trace_logger is not None:
        from lup.observability.display import ColorAssigner, print_block
        from lup.observability.trace import TraceEvent

        colors = ColorAssigner()

        async def display_result(record: DisplayRecord) -> None:
            for block in record.blocks:
                print_block(
                    block.telemetry_block,
                    trace=trace_logger,
                    colors=colors,
                )

        async def trace_result(record: TraceRecord) -> None:
            if not record.succeeded and record.failure is not None:
                for block in record.failure.blocks:
                    trace_logger.log_block(block.telemetry_block)
                trace_logger.log_text(record.failure.message, heading="Turn error")
                trace_logger.emit_event(
                    TraceEvent(
                        kind="error",
                        timestamp=utc_now().isoformat(),
                        brief=record.failure.message,
                    )
                )
            trace_logger.save()

        persistence = PersistenceConfig(directory=notes.trace_log.parent / "turns")
        tracing = TracingConfig(sink=trace_result)
        display = DisplayConfig(sink=display_result)
    return agent.layered(
        SessionLayers(
            timeout=timeout,
            budget=budget,
            correction=CorrectionConfig(cycles=2),
            persistence=persistence,
            tracing=tracing,
            display=display,
        )
    )


def build_session_factory(
    session_id: str,
    task_id: str | None = None,
    *,
    realtime: bool = False,
    model: str | None = None,
    toolless: bool = False,
    bare_prompt: bool = False,
) -> SessionBuild:
    """Assemble tools and return a fully configured neutral factory."""
    # lup: defer: this factory and `provider_factory` are the half of the
    # composition root still copied: the user settled that both become library
    # code taking a declaration of the adopter's tool groups and kinds, so an
    # update reaches them by the library pin rather than by a hand-port;
    # `lup.tools.toolsets.assembled` moved, these two did not
    from lup.policy.hooks import (
        create_permission_hooks,
        create_tool_allowlist_hook,
        merge_hooks,
    )
    from lup.orchestration.realtime.relay import REALTIME_DIRNAME
    from lup_template.agent.tool_policy import ToolPolicy
    from lup_template.agent.subagents import get_subagent_specs
    from lup.tools.toolsets import SessionNeeds, assembled, registered, served_names
    from lup_template.agent.toolsets import (
        declared_tool_groups,
        declared_tool_servers,
    )
    from lup_template.agent.toolsets import session_needs as project_needs
    from lup.coordination.bare.runtime import runtime_of
    from lup.coordination.identity import MemberEnv, member_ref
    from lup.coordination.repository import runtime_member
    from lup_template.kinds import NODE_KINDS, LAYOUT

    engine = engine_for_settings(model)
    # A run started from a launched session's shell inherits that session's
    # id, and is a member of its own spawned by it rather than that session.
    member = runtime_member(
        project_root(), MemberEnv().member_id, session_id, runtime_of(os.getpid())
    )
    # Sessions and their results are indexed in the ledger as pointers,
    # under the identity the session's own tool group records with, so the
    # opening and what the session later claims share one author.
    recorder = session_recorder(
        project_root(),
        member_ref(member.member_id),
        NODE_KINDS,
        LAYOUT,
    )
    notes = setup_notes(session_id, task_id or "0", recorder=recorder, runtime=engine)
    no_subagents: list[SubagentSpec] = []
    subagents = no_subagents if toolless else get_subagent_specs()
    system_prompt = "" if bare_prompt else get_system_prompt()
    mcp: list[ToolServer] = []
    writable_roots: list[Path] = []
    allowed_tools: list[str] = []
    submission_gate: SubmissionGate[AgentOutput] | None = None
    hooks = create_permission_hooks(notes.rw, notes.ro)
    builtin: BuiltinPreset = "none" if toolless else "stock"
    sandbox: Sandbox | None = None
    groups = declared_tool_groups()

    def session_needs(
        gate: "ReviewGate", sandbox: "Sandbox | None", realtime_dir: Path | None
    ) -> SessionNeeds:
        """What this session's groups are built from, as each path resolves it.

        The two paths differ in three things — an in-memory gate against a
        file-backed one, a container this process started against none, a
        mailbox or not — and agree on the rest, which is why the rest is
        written here instead of twice.
        """
        return SessionNeeds(
            session_dir=notes.session,
            root=project_root(),
            gate=gate,
            outputs_dir=notes.output.parent,
            sandbox=sandbox,
            realtime_dir=realtime_dir,
            member=member.member_id,
            spawned_by=member.spawned_by,
        )

    match toolless, engine:
        case False, "claude" | "claude-compat":
            from lup.orchestration.reflection import ReviewGate

            policy = ToolPolicy(settings)
            realtime_dir = notes.session / REALTIME_DIRNAME if realtime else None
            sandbox = build_session_sandbox(notes)
            # In memory, because this path assembles the tools in the process
            # that waits on their verdict: a flag file is what the two-process
            # path needs and this one would only be writing to itself through
            # a file.
            gate = ReviewGate()
            toolset = assembled(groups, session_needs(gate, sandbox, realtime_dir))
            tool_servers = dict(
                policy.get_mcp_servers(*registered(toolset, groups, policy))
            )
            from lup.providers.claude import SUBMISSION_TOOL

            allowed_tools = policy.get_allowed_tools(
                tool_servers,
                # lup: ignore[frozenset-shape] — immutable policy input
                builtin_tools=frozenset(
                    {"Read", "Glob", "Grep", "WebSearch", "WebFetch", "Bash"}
                ),
            )
            # The turn-bound submission tool is registered by the adapter, not
            # the template toolsets; without this the allowlist hook denies
            # the very tool that finalizes the turn.
            allowed_tools.append(SUBMISSION_TOOL)
            hooks = merge_hooks(hooks, create_tool_allowlist_hook(allowed_tools))

            def declared(name: str, entry: McpServerEntry) -> ToolServer:
                """One registered group as the session declares it, hosted here."""
                match entry:
                    case LupMcpServerConfig():
                        return Toolset(entry.tools, name=name)
                    case _:
                        return External(name=name, server=entry)

            mcp = [declared(name, entry) for name, entry in tool_servers.items()]
            submission_gate = reflection_submission_gate(gate)
        case False, _:
            from lup.orchestration.reflection import ReviewGate
            from lup.workspace.context import SessionContext

            policy = ToolPolicy(settings)
            realtime_dir = notes.session / REALTIME_DIRNAME if realtime else None
            # The flag lives outside the sandbox's writable roots (workspace,
            # /tmp) so only the host-side tool server can open the gate.
            gate_flag = session_gate_flag(notes.session.name)
            gate_flag.unlink(missing_ok=True)
            gate = ReviewGate(flag_path=gate_flag)
            submission_gate = reflection_submission_gate(gate)
            context = SessionContext(
                session_dir=notes.session,
                outputs_dir=notes.output.parent,
                gate_flag=gate.flag_path,
                session_id=notes.session.name,
                task_id=notes.output.parent.name,
                realtime_dir=realtime_dir,
            )
            environment = {
                **context.to_env(),
                "AGENT_SDK": engine,
                "AGENT_SANDBOX_ENABLED": str(settings.sandbox_enabled).lower(),
            }
            if (selected_model := model or settings.model) is not None:
                environment["AGENT_MODEL"] = selected_model
            if settings.aux_model is not None:
                environment["AGENT_AUX_MODEL"] = settings.aux_model
            # Which groups this session has, derived by building them rather
            # than listed beside the builder: a server started for a group
            # this session builds empty is a subprocess serving nothing.
            served = policy.filter_group_names(
                served_names(groups, session_needs(gate, None, realtime_dir))
            )
            launch = ServeLaunch(
                program=["uv", "run", "lup-devtools", "tools", "serve"],
                needs=project_needs,
                environment=environment,
            )
            mcp = [
                External(
                    name=server.name,
                    server=server.launched(launch),
                    always_load=server.always_load,
                )
                for server in declared_tool_servers()
                if server.name in served
            ]
            writable_roots = list(notes.rw)

    resolver = (
        submission_gate_resolver(AgentOutput, submission_gate)
        if submission_gate is not None
        else None
    )
    factory = provider_factory(
        model=model or settings.model,
        system_prompt=system_prompt,
        cwd=Path.cwd(),
        builtin=builtin,
        mcp=mcp,
        allowed_tools=allowed_tools,
        hooks=hooks,
        add_dirs=[*notes.all_dirs, *settings.extra_dirs],
        submission_gate=resolver,
        writable_roots=writable_roots,
        subagents=subagents if engine in ("claude", "claude-compat") else None,
    )
    match sandbox:
        case None:
            if (
                not toolless
                and settings.sandbox_enabled
                and engine in ("codex", "openai", "openai-compat")
            ):
                factory = factory.layered(
                    SessionLayers(
                        wrappers=[CleanupWrapper(codex_sandbox_cleanup(notes))]
                    )
                )
        case _:
            factory = factory.layered(
                SessionLayers(wrappers=[CleanupWrapper(sandbox.stop)])
            )
    trace_logger = TraceLogger(
        trace_path=notes.trace_log,
        title=f"Session {session_id}",
    )
    decorated = decorate_factory(
        factory,
        notes=notes,
        trace_logger=trace_logger,
        model=model,
    )
    # Outermost, so the session's record closes after every other wrapper has
    # run and the trace it pins is the one the tracing sink finished writing.
    if recorder is not None and notes.record is not None:
        decorated = decorated.layered(
            SessionLayers(wrappers=[CloseRecordingWrapper(recorder, notes.record)])
        )
    return SessionBuild(
        factory=decorated,
        notes=notes,
        trace_logger=trace_logger,
        recorder=recorder,
    )


def build_auxiliary_factory(
    *,
    model: str | None,
    system_prompt: str = "",
    tools: list[ToolGrant] | None = None,
    capabilities: list[SubagentCapability] | None = None,
    thinking_budget: int | None = None,
    max_turns: int | None = None,
    timeout_seconds: float | None = None,
) -> Agent:
    """Build a one-shot nested/reviewer agent through the same route.

    A nested agent's bounds are the caller's: it is one query with a job, so
    the turn cap and thinking budget belong to whoever declared that job
    rather than to the session settings a whole run shares.
    """
    return decorate_factory(
        provider_factory(
            model=model,
            system_prompt=system_prompt,
            cwd=Path.cwd(),
            role=SubagentSpec(
                name="auxiliary",
                description="One-shot auxiliary role",
                prompt=system_prompt,
                tools=tools or [],
                capabilities=capabilities or [],
                # The role runs on the model this factory is handed, which is
                # already the strongest tier where the caller named none.
                model="inherit",
            )
            if tools is not None or capabilities is not None
            else None,
            session_defaults=False,
            thinking_budget=thinking_budget,
            max_turns=max_turns,
        ),
        timeout_seconds=timeout_seconds,
        model=model,
    )


def build_subagent_factory(spec: SubagentSpec) -> Agent:
    """Compile a declared role through the same engine as its parent session."""
    return decorate_factory(
        provider_factory(
            model=settings.model,
            system_prompt=spec.prompt,
            cwd=Path.cwd(),
            role=spec,
            session_defaults=False,
            max_turns=spec.max_turns,
        )
    )


def resolve_resume_token(reference: str) -> SessionId:
    """Resolve a saved run name or accept an opaque provider session id."""
    from lup.workspace.history import latest_session_record

    record = latest_session_record(reference)
    if record is None:
        return SessionId(value=reference)
    if not record.sdk_session_id:
        raise ValueError(f"session {reference!r} has no provider resume identity")
    return SessionId(value=record.sdk_session_id)


def result_text[T: BaseModel | None](result: TurnResult[T]) -> str:
    """Concatenate completed portable text blocks."""
    return "\n\n".join(
        text for block in result.blocks if (text := block.text_payload) is not None
    )


class ConsultedSource(BaseModel, frozen=True):
    """What a retrieval tool call names as the source it consulted.

    A fetch names a `url` and a search names a `query`; one model reads both
    because which of them a call carries is decided by the tool that made it,
    and the arguments arrive as whatever the model emitted.
    """

    url: PayloadText = None
    query: PayloadText = None


def result_sources[T: BaseModel | None](result: TurnResult[T]) -> list[str]:
    """Extract source URLs and search queries from semantic tool calls."""
    sources: list[str] = []  # lup: ignore[empty-collection]
    for block in result.blocks:
        arguments = block.tool_arguments
        if arguments is None:
            continue
        consulted = ConsultedSource.model_validate(arguments)
        match block.tool_call_name:
            case "FetchUrl" | "WebFetch":
                value = consulted.url
            case "SearchWeb" | "WebSearch":
                value = consulted.query
            case _:
                continue
        if value:
            sources.append(value)
    return sources


def application_result(
    result: TurnResult[AgentOutput],
    *,
    session_id: str,
    task_id: str | None,
    tool_metrics: MetricsSummary,
) -> AgentSessionResult:
    """Project a strict typed turn result into the domain history model."""
    usage_cost = build_usage_cost()
    return AgentSessionResult(
        session_id=session_id,
        task_id=task_id,
        agent_version=agent_version(),
        agent_sdk=engine_for_settings(),
        sdk_session_id=result.identifiers.session.value,
        timestamp=utc_now().isoformat(),
        output=result.output,
        reasoning=result_text(result),
        sources_consulted=result_sources(result),
        duration_seconds=result.duration.total_seconds(),
        cost_usd=usage_cost(result.usage) if usage_cost is not None else None,
        token_usage=result.usage,
        tool_metrics=tool_metrics,
    )


async def run_agent(
    task: str,
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    resume: SessionId | None = None,
) -> AgentSessionResult:
    """Run one strict typed turn and persist its application projection."""
    identifier = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    reset_metrics()
    build = build_session_factory(identifier, task_id)
    async with build.factory.open(resume) as session:
        result = await session.ask(task, AgentOutput)
    log_metrics_summary()
    projected = application_result(
        result,
        session_id=identifier,
        task_id=task_id,
        tool_metrics=read_metrics_summary(build.notes.session),
    )
    save_session(
        projected,
        session_id=identifier,
        recorder=build.recorder,
        session=build.notes.record,
    )
    return projected


async def run_persistent_agent(
    task: str,
    *,
    session_id: str | None = None,
    on_reply: Callable[[str], Awaitable[None]] | None = None,
    missing_sleep_message: str | None = None,
) -> PersistentSessionResult:
    """Run the relay over the same ``Session`` contract as ordinary turns.

    ``missing_sleep_message`` is the nudge a turn ending without sleep gets;
    unset is this domain's own, from the realtime tools.
    """
    from lup.orchestration.realtime.relay import (
        REALTIME_DIRNAME,
        RealtimeMailbox,
        run_relay_session,
    )
    from lup.orchestration.realtime.scheduler import Scheduler
    from lup_template.agent.tools.realtime import MISSING_SLEEP_MESSAGE

    identifier = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    build = build_session_factory(identifier, realtime=True)

    async def echo_reply(message: str) -> None:
        print(f"[lup] {message}")

    scheduler = Scheduler(on_action=on_reply or echo_reply)
    mailbox = RealtimeMailbox(build.notes.session / REALTIME_DIRNAME)
    from lup.orchestration.reflection import ReflectionGate

    relay_gate = ReflectionGate(
        flag_path=build.notes.trace_log.with_suffix(".reflection")
    )
    async with build.factory.open() as session:
        turns = await run_relay_session(
            session,
            scheduler=scheduler,
            mailbox=mailbox,
            initial_prompt=task,
            missing_sleep_message=missing_sleep_message or MISSING_SLEEP_MESSAGE,
            gate=relay_gate,
            trace_logger=build.trace_logger,
        )
    return PersistentSessionResult(turns=turns)


def build_session_sandbox(notes: NotesConfig) -> "Sandbox | None":
    """Build the optional application code-execution sandbox lazily.

    The exchange directory is mounted at the path the host already calls it,
    so a file has one name on both sides. This agent holds file tools and a
    code-execution tool at once, and nothing in a path tells it which side a
    path belongs to — giving the two sides one spelling removes the choice
    rather than asking it to choose correctly.
    """
    if not settings.sandbox_enabled:
        return None
    try:
        from lup.sandbox.container import Sandbox
    except ImportError:
        logger.warning("docker extra not installed; code execution is unavailable")
        return None
    shared = (notes.session / "sandbox_shared").resolve()
    return Sandbox(
        session_id=notes.session.name,
        shared_dir=shared,
        shared_path=str(shared),
        timeout_seconds=settings.sandbox_timeout_seconds,
    )


def codex_sandbox_cleanup(notes: NotesConfig) -> Callable[[], None]:
    """Build parent-side teardown for a sandbox hosted by an MCP subprocess."""

    def cleanup() -> None:
        try:
            from lup.sandbox.container import sandbox_cleanup

            with sandbox_cleanup(
                session_id=notes.session.name,
                shared_dir=notes.session / "sandbox_shared",
            ):
                pass
        except Exception:
            logger.exception("Post-session Codex sandbox cleanup failed")

    return cleanup
