"""Claude Code's spelling of a launch: the words and settings an interactive CLI starts with."""

import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING, Literal, TypedDict

from mcp.server import Server

from lup.coordination.identity import LaunchedMember
from lup.coordination.repository import launched_member
from lup.harness.generate import (
    ProjectContent,
    RuntimeReadiness,
    claude_generation_recipe,
    generate,
)
from lup.harness.models import CapabilityEvidence, Harness, Resumption
from lup.harness.requirements import Finding
from lup.harness.toolchain import bubblewrap_requirement, socat_requirement
from lup.launch.boundary import apply_sandbox_environment
from lup.launch.companions import CompanionLaunch, Joined, held_companions
from lup.launch.compilation import allowance_environment, inherited_environment
from lup.launch.config_volume import HomeSeedPlaces
from lup.launch.declaration import (
    LaunchCommand,
    LaunchStep,
    Member,
    Recording,
    declared_image,
    declared_requirements,
    launched_sandbox,
    resumption,
)
from lup.launch.foreground import between_steps, run_in_foreground
from lup.launch.preflight import LaunchSentinels, release_ledger
from lup.launch.refusal import LaunchRefused
from lup.launch.session import (
    LaunchOpening,
    cleared_on_the_way_in,
    personal_config,
    placed_wake_socket,
    runtime_preflight,
    session_argv,
    start_harness_transcript,
)
from lup.providers.claude.config_home import (
    ClaudeConfigUnreadable,
    selected_config_home,
)
from lup.providers.claude.confinement import CLAUDE_SANDBOX_OFF
from lup.providers.claude.harness import CLAUDE_OVERLAY
from lup.providers.claude.harness_runtime import (
    ClaudeCliEvidence,
    claude_capability_probes,
)
from lup.providers.claude.home_seed import ClaudeHomeSeed
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.providers.claude.model_choice import (
    claude_default_effort,
    claude_effort,
    claude_model_id,
)
from lup.providers.claude.session import carry_claude_home
from lup.providers.claude.theme import settle_claude_theme
from lup.providers.claude.transcripts import ClaudeTranscripts
from lup.providers.profile_tree import profile_environment
from lup.providers.user_config import UserConfigFile
from lup.observability.audit import TraceJournal
from lup.sandbox.rail import AccessibleRoot, host_run, in_repository
from lup.sessions.layers import SessionLayers
from lup.mcp import ServeLaunch
from lup.tools.mcp import (
    LupMcpServerConfig,
    McpServerEntry,
    RawHttpServerConfig,
    RawSseServerConfig,
    RawStdioServerConfig,
)
from lup.types import EnvVars, JsonObject, JsonValue
from lup.workspace.paths import worktrees_directory

if TYPE_CHECKING:
    from lup.providers.claude import Claude, ClaudeTools


def claude_resume_arguments(resume: Resumption) -> list[str]:
    """Claude Code's spelling: continuing and resuming are two flags.

    ``--continue`` takes the most recent conversation in the working
    directory and ``--resume`` opens the picker or takes a session id, so a
    request reaches the runtime as words rather than as a mode.
    """
    if resume.session is not None:
        return ["--resume", resume.session]
    if resume.pick:
        return ["--resume"]
    return ["--continue"] if resume.latest else []


def claude_filesystem(
    writable_paths: list[str], accessible: list[AccessibleRoot], tree: Path | None
) -> JsonObject:
    """The inner sandbox's write widening: declared paths, sibling worktrees, mounts.

    Each writable root that is a repository also denies what the host runs
    from it, its shared `config` and `hooks/`, which a deny inside a wider
    allow holds.
    """
    allowed: list[JsonValue] = [
        *writable_paths,
        *([str(tree)] if tree is not None else []),
        *[str(item.path) for item in accessible if item.writable],
    ]
    held: list[JsonValue] = [
        str(path)
        for item in accessible
        if item.writable and in_repository(item.path)
        for path in host_run(item.path)
    ]
    return {"allowWrite": allowed, **({"denyWrite": held} if held else {})}


def companion_plugin_directories(root: Path, generated: str) -> list[Path]:
    """The plugin directories this checkout carries beside the generated one.

    A project may keep a hand-written plugin next to the one the harness
    compiles. Its only other way into a session is a marketplace, and a
    marketplace name is one global namespace shared by every checkout
    declaring it — so the plugin a session loaded is whichever tree
    registered that name last, the same hazard `lease_plugin_dir` documents.
    A directory carrying `.claude-plugin/plugin.json` is a plugin by its own
    declaration, which is why nothing here needs to be written down twice.
    The machine's own overlay is left out: a launch names it explicitly, after
    the generated plugin and ahead of these, since it is rendered on the way
    in and may not be there yet when the launch is declared.

    Sorted, so what a launch names does not depend on directory order.
    """
    plugins = root / ".claude" / "plugins"
    if not plugins.is_dir():
        return []
    return sorted(
        directory
        for directory in plugins.iterdir()
        if directory.name not in (generated, CLAUDE_OVERLAY.name)
        and (directory / ".claude-plugin" / "plugin.json").is_file()
    )


def claude_account_environment(agent: "Claude") -> EnvVars:
    """The account a session runs as: the home named outright, or its profile's.

    A home named outright is the account's own, found by whoever named it,
    so the profile beside it only names that account and is not looked up.
    """
    if agent.home is not None:
        return CLAUDE_LOGIN.environment(agent.home)
    return profile_environment(CLAUDE_LOGIN, agent.profile)


def claude_plugin_directory(agent: "Claude", root: Path) -> Path | None:
    """Where the declared plugin loads from: the tree its harness compiles to, or the path named."""
    match agent.plugin:
        case Harness(plugins=[first, *_]):
            return root / ".claude" / "plugins" / first.name
        case Path() as built:
            return built
        case Harness() | None:
            return None


def compiled_claude(agent: "Claude") -> "Claude":
    """The declaration as its sessions open with it, the one compilation both outputs start from.

    A compatible endpoint becomes the environment that routes the CLI to it
    here rather than at declaration, so an agent can be copied and changed
    before it is built.

    What the declaration leaves unset is the person's to answer, read from
    their lup config as each session opens: a model left unnamed runs on
    their tier, unless an endpoint serves models of its own, and an effort
    left unnamed starts from theirs. A named profile, or a home named
    outright, becomes the account home the session runs under, and a
    declared plugin becomes the first plugin directory it loads.
    """
    personal = personal_config(UserConfigFile())
    model = (
        agent.model
        if agent.model is not None or agent.endpoint is not None
        else personal.tier
    )
    plugin = claude_plugin_directory(agent, agent.cwd or Path.cwd())
    config = agent.model_copy(
        update={
            "model": model,
            "effort": agent.effort
            or claude_default_effort(model, personal.effort or "xhigh"),
            "environment": {**agent.environment, **claude_account_environment(agent)},
            "plugin_dirs": [
                *([plugin] if plugin is not None else []),
                *agent.plugin_dirs,
            ],
        }
    )
    if config.endpoint is None:
        return config
    from lup.providers.claude.config import ClaudeCompatibilityTransform

    return ClaudeCompatibilityTransform(config.endpoint).apply(config)


def claude_settings(agent: "Claude", tree: Path | None = None) -> JsonObject:
    """The one settings document both outputs carry: the declared wall, and the effort's switches.

    A session opened here reads it as the SDK's ``settings`` and a launched
    one as ``--settings`` — one document, because the CLI reads one flag and
    a second would be read in place of the first — so neither inherits a
    sandbox from wherever it happens to start. An inner sandbox is enabled
    with the policy's own exclusions and write paths; an escape is refused
    unless the sandbox is declared escapable, which is the CLI's own default
    and so is said only to refuse.

    Enabled, it is widened. Claude roots writes at the working directory, so
    a second checkout is read-only to every command a session runs — and
    running the toolchain over one is ordinary work, which is why the symptom
    arrives as pytest failing to write a cache and `ruff format` refusing to
    save, neither naming a sandbox. ``tree`` is the directory holding this
    checkout's sibling worktrees, resolved at launch rather than declared,
    because an artifact carrying an absolute path would be drift in every
    other checkout. The declared mounts widen it the same way and for the
    same reason they reach the container's mount table: a folder the session
    is meant to write is writable whichever wall it opens behind.

    The container and no sandbox at all both stand Claude Code's own sandbox
    down, spelled by :data:`~lup.providers.claude.confinement.CLAUDE_SANDBOX_OFF`
    so the image-side probe asking whether a session can open at all opens
    the same one. Measured: in an unprivileged container bubblewrap cannot
    mount a fresh ``/proc``, so the inner sandbox does not start there, and
    what refuses is the egress proxy.
    """
    chosen = agent.resolved_effort()
    carried = claude_effort(chosen).settings if chosen is not None else {}
    policy = agent.enforced_policy()
    inner = agent.sandbox.confinement()
    if inner is None:
        return {**CLAUDE_SANDBOX_OFF, **carried}
    declared = policy.sandbox if policy is not None else None
    excluded: list[JsonValue] = list(
        dict.fromkeys(
            [
                *(policy.excluded_commands() if policy is not None else []),
                *inner.excluded_commands,
            ]
        )
    )
    block: JsonObject = {
        "enabled": True,
        **({} if inner.escapable else {"allowUnsandboxedCommands": False}),
        **({"excludedCommands": excluded} if excluded else {}),
        "filesystem": claude_filesystem(
            list(declared.writable_paths) if declared is not None else [],
            inner.roots(),
            tree,
        ),
    }
    return {"sandbox": block, **carried}


class ClaudeServerLoading(TypedDict, total=False):
    """Whether Claude Code offers a server's tools from the first turn.

    Claude Code defers MCP tools behind its tool search and exempts a server
    whose config says ``alwaysLoad``, on every transport
    (https://code.claude.com/docs/en/mcp#exempt-a-server-from-deferral). The
    Agent SDK's config types do not declare the key and hand it to the CLI as
    given, so each shape below is the transport's own with this key beside it.
    """

    alwaysLoad: bool


class ClaudeStdioServer(RawStdioServerConfig, ClaudeServerLoading):
    """A server Claude Code starts as a subprocess."""


class ClaudeSseServer(RawSseServerConfig, ClaudeServerLoading):
    """A server Claude Code reaches over Server-Sent Events."""


class ClaudeHttpServer(RawHttpServerConfig, ClaudeServerLoading):
    """A server Claude Code reaches over streamable HTTP."""


class ClaudeSdkServer(ClaudeServerLoading):
    """A server this process hosts, as the Agent SDK hands it to Claude Code."""

    type: Literal["sdk"]
    name: str
    instance: Server


type ClaudeServer = (
    ClaudeSdkServer | ClaudeStdioServer | ClaudeSseServer | ClaudeHttpServer
)
"""One MCP server as Claude Code reads it."""


def claude_server(entry: McpServerEntry, always_load: bool) -> ClaudeServer:
    """One server in the config both outputs carry, exempt from tool search where declared.

    A session opened here hands the SDK its hosted servers as instances and
    every other as a transport; a launched one names transports only. Either
    way the loading key rides on the entry itself, which is where Claude Code
    reads it. The projection is this adapter's because the spelling is: asking
    the neutral entry to convert itself would carry Claude's keys into library
    code, beside Codex's projection of the same entry into its own tables.
    """
    loading = (
        ClaudeServerLoading(alwaysLoad=True) if always_load else ClaudeServerLoading()
    )
    match entry:
        case LupMcpServerConfig():
            return ClaudeSdkServer(
                type="sdk", name=entry.name, instance=entry.server, **loading
            )
        case {"type": "sse"}:
            return ClaudeSseServer(**entry, **loading)
        case {"type": "http"}:
            return ClaudeHttpServer(**entry, **loading)
        case _:
            return ClaudeStdioServer(**entry, **loading)


def claude_launched_serve(tools: "ClaudeTools") -> ServeLaunch:
    """How a launched Claude Code starts the servers lup hosts, as this runtime's own."""
    return (
        tools.serve
        if tools.serve.runtime is not None
        else tools.serve.model_copy(update={"runtime": "claude"})
    )


def claude_server_environment(tools: "ClaudeTools") -> EnvVars:
    """What a launched Claude Code is told about the servers it starts.

    Claude Code reads one ``MCP_TIMEOUT``, in milliseconds, for every server
    it starts, so the widest deadline the servers declare is the one given:
    it loosens the limit for a server that declared none and never tightens
    one. Nothing where no server declares a deadline.
    """
    serve = claude_launched_serve(tools)
    deadlines = [
        deadline
        for server in tools.mcp
        if (deadline := server.startup_timeout(serve)) is not None
    ]
    if not deadlines:
        return {}
    return {"MCP_TIMEOUT": str(round(max(deadlines) * 1000))}


def claude_mcp_arguments(tools: "ClaudeTools") -> list[str]:
    """The declared servers as a launched CLI starts them, and no others.

    Strict, so a plugin's own MCP entries are not loaded beside them: the
    declaration's roster is the session's whole one, as it is for a session
    opened here.
    """
    serve = claude_launched_serve(tools)
    servers = {
        server.name: claude_server(server.launched(serve), server.always_load)
        for server in tools.mcp
    }
    return [
        "--mcp-config",
        json.dumps({"mcpServers": servers}),
        "--strict-mcp-config",
    ]


def refuse_in_process_fields(agent: "Claude") -> None:
    """Refuse what only a session opened in process can honour, naming what a launch takes instead."""
    refused = [
        reason
        for asked, reason in (
            (
                agent.hooks is not None,
                "hooks are callbacks in this process, which a launched CLI "
                "cannot call; declare the policy, which its plugin enforces",
            ),
            (
                agent.submission_gate_resolver is not None,
                "a submission gate answers turns a program drives; a launched "
                "session's turns are the person's",
            ),
            (
                agent.max_turns is not None,
                "max_turns bounds a session a program drives; a person ends "
                "the one they launched",
            ),
            (
                agent.layers != SessionLayers(),
                "layers wrap sessions opened in this process",
            ),
        )
        if asked
    ]
    if refused:
        raise LaunchRefused("cannot launch this declaration: " + "; ".join(refused))


def claude_arguments(
    config: "Claude",
    member: LaunchedMember,
    wake_socket: str | None,
    words: list[str],
    tree: Path | None = None,
) -> list[str]:
    """The words Claude Code starts with, from a declaration already launched and compiled.

    In the order the CLI's own launcher has always spoken them, the caller's
    ``words`` last, so a flag a person passes still wins. Every list a flag
    takes is joined into one value, because the CLI's list flags are
    variadic and would otherwise swallow the words after them.
    """
    refuse_in_process_fields(config)
    from lup.providers.claude.subagents import model_alias, subagent_tools

    native = config.tools.roster()
    chosen = config.resolved_effort()
    effort = claude_effort(chosen) if chosen is not None else None
    model = config.model_id()
    agents: JsonObject = {
        spec.name: {
            "description": spec.description,
            "prompt": spec.prompt,
            "tools": list[JsonValue](subagent_tools(spec)),
            **(
                {"model": alias}
                if (alias := model_alias(spec.model)) is not None
                else {}
            ),
        }
        for spec in config.subagents
    }
    prompt = (
        ["--append-system-prompt", config.system_prompt]
        if native is None
        else ["--system-prompt", config.system_prompt]
    )
    return [
        *claude_resume_arguments(resumption(config.resume)),
        *(["--model", model] if model is not None else []),
        *(effort.arguments() if effort is not None else []),
        *[
            flag
            for directory in config.plugin_dirs
            for flag in ("--plugin-dir", str(directory))
        ],
        "--settings",
        json.dumps(claude_settings(config, tree)),
        *claude_mcp_arguments(config.tools),
        *(["--tools", ",".join(native)] if native is not None else []),
        *(
            ["--allowedTools", ",".join(config.allowed_tools)]
            if config.allowed_tools
            else []
        ),
        *(
            ["--disallowedTools", ",".join(config.disallowed_tools)]
            if config.disallowed_tools
            else []
        ),
        *(prompt if config.system_prompt else []),
        *[
            flag
            for directory in config.add_dirs
            for flag in ("--add-dir", str(directory))
        ],
        *(["--agents", json.dumps(agents)] if agents else []),
        *(
            ["--permission-mode", config.permission_mode]
            if config.permission_mode is not None
            else []
        ),
        *[
            word
            for argument, value in config.extra_args.items()
            for word in (f"--{argument}", *([value] if value is not None else []))
        ],
        # What this runtime shows in its own chrome, made to agree with the
        # name the roster answers to; ahead of ``words``, so a person who
        # named their own session still wins.
        "--name",
        member.cli_name,
        *(["--messaging-socket-path", wake_socket] if wake_socket is not None else []),
        *words,
    ]


def claude_launched(agent: "Claude") -> "Claude":
    """The declaration with every field it left unset taking a launch's default.

    Read off what was set, so a field declared with the same value as its
    default still counts as said: built-in tools left unnamed are Claude
    Code's own stock, the permission mode is the CLI's own, an unset sandbox
    is the verified container where an engine answers and the inner sandbox
    with a warning where none does, an unset identity is the worktree's name,
    and an unset record is the run's transcript.
    """
    launched = agent
    if "builtin" not in agent.tools.model_fields_set:
        tools = agent.tools.model_copy(update={"builtin": "stock"})
        launched = launched.model_copy(update={"tools": tools})
    if "permission_mode" not in agent.model_fields_set:
        launched = launched.model_copy(update={"permission_mode": None})
    if "sandbox" not in agent.model_fields_set:
        launched = launched.model_copy(update={"sandbox": launched_sandbox()})
    if agent.identity is None:
        launched = launched.model_copy(update={"identity": Member()})
    if agent.record is None:
        launched = launched.model_copy(update={"record": Recording(transcript=True)})
    return launched


def claude_root(agent: "Claude") -> Path:
    """The directory a launch opens in: the declared one, or where this process stands."""
    return (agent.cwd or Path.cwd()).resolve()


def claude_readiness(agent: "Claude", root: Path) -> RuntimeReadiness:
    """The probes asking whether the installed CLI can host this declaration's session."""
    program = agent.cli_path or Path("claude")
    plugin = claude_plugin_directory(agent, root)

    def readiness() -> list[CapabilityEvidence[ClaudeCliEvidence]]:
        probes = claude_capability_probes(plugin or root, program)
        return [
            probe.probe()
            for probe in probes
            if plugin is not None or probe.capability != "hooks"
        ]

    return readiness


def claude_checked(agent: "Claude", sentinels: LaunchSentinels) -> LaunchOpening:
    """Clear the gates before a session: the CLI's probes, then the declared requirements."""
    launched = claude_launched(agent)
    root = claude_root(launched)
    posture = launched.sandbox.posture()
    opening = LaunchOpening(sandbox=posture)
    opening.findings = runtime_preflight(
        "claude",
        claude_readiness(launched, root),
        declared_requirements(launched.plugin, launched.requirements),
        root,
        sentinels,
        opening,
        posture.contained(),
    )
    return opening


def check_claude(agent: "Claude") -> list[Finding]:
    """Every finding a launch of this declaration would clear, or the refusal it stops on."""
    return claude_checked(agent, LaunchSentinels()).findings


def prepare_claude(agent: "Claude") -> None:
    """Compile a harness plugin into this project's tree, and settle the account's theme.

    A plugin named by its directory is loaded as built. The theme is the
    account's, filled in through its own files where it keeps none, so a
    session's own ``/theme`` still wins; a contained session's home is seeded
    at its start instead, by the launch.
    """
    launched = claude_launched(agent)
    root = claude_root(launched)
    if isinstance(launched.plugin, Harness):
        generate(
            claude_generation_recipe(root, ProjectContent(harness=launched.plugin))
        )
    if launched.sandbox.posture().contained():
        return
    environment = {**inherited_environment(), **compiled_claude(launched).environment}
    try:
        settle_claude_theme(
            selected_config_home(environment),
            personal_config(UserConfigFile()).theme.claude,
        )
    except ClaudeConfigUnreadable as error:
        raise LaunchRefused(str(error)) from error


def claude_opening(
    agent: "Claude",
    words: list[str],
    sentinels: LaunchSentinels,
    opening: LaunchOpening,
    transcript: Path | None = None,
    home_seed: HomeSeedPlaces | None = None,
    joined: Joined = Joined(),
) -> LaunchCommand:
    """Compile a launched declaration into the process that opens its session.

    Settles what the argv depends on the way a launch does: the wake socket
    is placed, the inner sandbox exercised before it is vouched for, the
    boundary measured and recorded, and an outer container's image and
    egress made ready, since the argv names them. ``joined`` is what the
    host companions held around the session hand it: their variables join
    its environment and their folders its sandbox's.
    """
    launched = claude_launched(agent)
    compiled = compiled_claude(launched)
    config = compiled.model_copy(
        update={"sandbox": compiled.sandbox.widened(joined.mounts)}
    )
    root = claude_root(launched)
    member = launched_member(root, config.identity.name if config.identity else None)
    sockets = config.identity.wake_sockets if config.identity is not None else None
    wake_socket = (
        placed_wake_socket(sockets, root, member) if sockets is not None else None
    )
    arguments = claude_arguments(
        config, member, wake_socket, words, worktrees_directory(root)
    )
    environment = {
        **claude_server_environment(config.tools),
        **inherited_environment(),
        **config.environment,
        **joined.environment,
    }
    environment.update(allowance_environment(config.max_recursive_agent, environment))
    opening.banner.add(joined.notices)
    posture = config.sandbox.posture()
    policy = config.enforced_policy()
    apply_sandbox_environment(
        policy,
        environment,
        "claude",
        [bubblewrap_requirement(), socat_requirement()],
        sandbox=posture,
    )
    argv = session_argv(
        str(config.cli_path or "claude"),
        arguments,
        root,
        declared_image(config.plugin, config.sandbox),
        declared_requirements(config.plugin, config.requirements),
        policy,
        CLAUDE_LOGIN.selected_home(environment),
        CLAUDE_LOGIN,
        posture,
        environment,
        transcript,
        sentinels,
        opening,
        config.sandbox.roots(),
        config.sandbox.granted(),
        member=member,
        home_seed=home_seed,
        # Claude Code reads the clipboard through commands a container can
        # carry, as its composition declares.
        clipboard="commands",
        forwarded=list(joined.environment),
        privileges=config.sandbox.privileges(),
        nested=config.sandbox.nested(),
    )
    return LaunchCommand(argv=argv, env=environment, cwd=root)


def claude_companions(
    agent: "Claude", root: Path, journal: TraceJournal | None
) -> CompanionLaunch:
    """The session its host companions are held for, as a launch of ``agent`` opens it."""
    return CompanionLaunch(
        root=root,
        runtime="claude",
        environment={**inherited_environment(), **compiled_claude(agent).environment},
        journal=journal,
    )


def claude_command(agent: "Claude", words: list[str]) -> LaunchCommand:
    """The process a launch of ``agent`` runs, compiled and settled but not run.

    The boundary a launch records is released again once the command is
    known, since no session opens behind it here, and so are the host
    companions held to learn what they contribute.
    """
    launched = claude_launched(agent)
    root = claude_root(launched)
    sentinels = LaunchSentinels()
    opening = claude_checked(agent, sentinels)
    try:
        with held_companions(
            launched.companions, claude_companions(launched, root, None)
        ) as joined:
            return claude_opening(agent, words, sentinels, opening, joined=joined)
    finally:
        release_ledger(root, sentinels.nonce)


def launch_claude_session(
    agent: "Claude", words: list[str], steps: Sequence[LaunchStep]
) -> int:
    """Prepare, check, and run a launched Claude Code session in the foreground.

    The run's transcript is started before the session and closed after it;
    a contained session's home is seeded from the account at its start and
    what it changed of the person's settings is carried back when it ends;
    the boundary it was measured behind is released however it ended.
    """
    launched = claude_launched(agent)
    # Compiled once before any step runs, so a declaration the person's lup
    # config cannot answer is refused before a step has done anything.
    compiled_claude(launched)

    def session() -> int:
        root = claude_root(launched)
        prepare_claude(launched)
        cleared_on_the_way_in(root)
        sentinels = LaunchSentinels()
        opening = claude_checked(launched, sentinels)
        config = compiled_claude(launched)
        environment = {**inherited_environment(), **config.environment}
        account = selected_config_home(environment)
        user_config = UserConfigFile()
        personal = personal_config(user_config)
        contained = launched.sandbox.posture().contained()
        try:
            seed = (
                ClaudeHomeSeed.compose(
                    account,
                    personal,
                    model=claude_model_id(personal.tier),
                    effort=(
                        None
                        if personal.effort is None
                        else claude_effort(personal.effort).level
                    ),
                )
                if contained
                else None
            )
        except ClaudeConfigUnreadable as error:
            raise LaunchRefused(str(error)) from error
        seeded_at = Path(mkdtemp(prefix="lup-home-seed-")) if seed is not None else None
        places = (
            HomeSeedPlaces(
                seed=seed.write(seeded_at / "seed"), applied=seeded_at / "applied"
            )
            if seed is not None and seeded_at is not None
            else None
        )
        record = launched.record or Recording()
        transcript = start_harness_transcript(
            "claude",
            ClaudeTranscripts(CLAUDE_LOGIN.selected_home(environment)),
            root,
            model=config.model_id(),
            profile=config.profile,
            arguments=list(words),
            record_root=record.root,
            transcribe=record.transcript,
            mode=record.mode,
            recorder=record.ledger,
        )
        succeeded = False
        interrupted = False
        applied = False
        try:
            with held_companions(
                launched.companions,
                claude_companions(launched, root, transcript.journal),
            ) as joined:
                command = claude_opening(
                    launched,
                    words,
                    sentinels,
                    opening,
                    transcript.journal.path,
                    places,
                    joined,
                )
                applied = places is not None
                status = run_in_foreground(command)
            succeeded = status == 0
            return status
        except KeyboardInterrupt:
            interrupted = True
            raise
        finally:
            release_ledger(root, sentinels.nonce)
            transcript.close(succeeded=succeeded, interrupted=interrupted)
            if places is not None and applied:
                carry_claude_home(
                    declared_image(launched.plugin, launched.sandbox),
                    root,
                    CLAUDE_LOGIN,
                    ClaudeHomeSeed.applied(places.applied),
                    account,
                    user_config,
                    personal,
                )
            if seeded_at is not None:
                shutil.rmtree(seeded_at)

    return between_steps(steps, session)
