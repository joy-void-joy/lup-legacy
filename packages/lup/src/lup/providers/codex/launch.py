"""Codex's spelling of a launch: the words and overrides an interactive CLI starts with."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from lup.coordination.repository import launched_member
from lup.harness.generate import ProjectContent, codex_generation_recipe, generate
from lup.harness.models import (
    ArtifactTree,
    CapabilityEvidence,
    Harness,
    HookSet,
    Resumption,
)
from lup.harness.notice import Notice
from lup.harness.requirements import Finding
from lup.harness.toolchain import codex_envelope_requirement
from lup.launch.boundary import apply_sandbox_environment
from lup.launch.companions import CompanionLaunch, Joined, held_companions
from lup.launch.compilation import allowance_environment, inherited_environment
from lup.launch.declaration import (
    InnerSandbox,
    LaunchCommand,
    LaunchSandbox,
    LaunchStep,
    Member,
    Recording,
    Sandbox,
    declared_image,
    declared_requirements,
    launched_sandbox,
    loopback_relayed,
    resumption,
)
from lup.launch.environments import revisions_home
from lup.launch.guidance import held_guidance
from lup.launch.foreground import between_steps, run_in_foreground
from lup.launch.preflight import LaunchSentinels, release_ledger
from lup.launch.refusal import LaunchRefused
from lup.launch.secrets import withheld_secrets
from lup.launch.session import (
    LaunchOpening,
    cleared_on_the_way_in,
    personal_config,
    runtime_preflight,
    session_argv,
    start_harness_transcript,
)
from lup.observability.audit import TraceJournal
from lup.providers.codex.confinement import CODEX_CONFINEMENT
from lup.providers.codex.harness import CodexGuidanceRenderer, CodexSpellings
from lup.providers.harness import codex_prompt_renderer, reject_oversized_guidance
from lup.providers.codex.harness_runtime import (
    CodexCliEvidence,
    codex_capability_probes,
)
from lup.providers.codex.home import (
    CodexHomeSelection,
    CodexWorktreeHomeStore,
    select_codex_home,
)
from lup.providers.codex.login import CODEX_HOME, CODEX_LOGIN
from lup.providers.codex.marketplace import MARKETPLACE_MANIFEST, CodexMarketplace
from lup.providers.codex.model_choice import (
    codex_default_effort,
    codex_effort_arguments,
)
from lup.providers.codex.profile import CodexAccountSettings
from lup.providers.codex.session import (
    carry_codex_home,
    codex_login_preflight,
    held_revision,
    prepare_codex_plugin,
    settled_codex_seed,
)
from lup.providers.codex.transcripts import CodexTranscripts
from lup.providers.profile_tree import profile_directory, profile_environment
from lup.providers.profiles import DefaultHomeProfile
from lup.providers.user_config import UserConfigFile
from lup.sandbox.rail import AccessibleRoot, working_trees
from lup.sessions.layers import SessionLayers
from lup.types import EnvVars, JsonObject
from lup.workspace.paths import worktrees_directory

if TYPE_CHECKING:
    from lup.providers.codex import Codex, CodexSandbox, CodexTools

# lup: ignore[library-default, constant-declaration] — each entry is literally a Codex CLI flag, its wire spelling
CODEX_SANDBOX_OVERRIDES = (
    "-s",
    "--sandbox",
    "--approve-for-me",
    "--not-so-yolo",
    "--yolo",
    "--dangerously-bypass-approvals-and-sandbox",
)
"""Every Codex flag by which a caller picks the sandbox itself, aliases included.

``--yolo`` and ``--not-so-yolo`` are the CLI's hidden aliases of the two long
flags after them. ``--approve-for-me`` names workspace-write on its own, and
Codex refuses it beside ``--sandbox``, so the launcher cannot add one."""


def codex_resume_arguments(resume: Resumption) -> list[str]:
    """Codex's spelling: reopening is a subcommand, and it leads the vector.

    The same three requests, in the shape this runtime has for them —
    ``resume`` alone is the picker, ``--last`` is the most recent, and a
    session id is positional. It comes first because a subcommand does, which
    is the whole of why the two cannot share one word list.
    """
    if resume.session is not None:
        return ["resume", resume.session]
    if resume.pick:
        return ["resume"]
    return ["resume", "--last"] if resume.latest else []


def codex_envelope(
    hooks: HookSet | None,
    environment: EnvVars,
    extra_args: list[str],
    sandbox: LaunchSandbox = LaunchSandbox.INNER,
    accessible: list[AccessibleRoot] = [],
    tree: Path | None = None,
    mode: "CodexSandbox | None" = None,
) -> list[str]:
    """Compose the interactive Codex envelope that LUP_SANDBOX_ACTIVE vouches for.

    Establishing the inner sandbox, the launcher builds the boundary it
    announces: an explicit workspace-write sandbox on the Codex command line,
    mirroring how the Claude settings compile the same declaration into an
    OS wall. Path-level write and credential denials have no Codex
    equivalent, and neither does taking one command out of the envelope, so
    the envelope is the declaration's strict subset (network stays off). The
    dispatcher still reads the exclusions, judging those commands as though
    nothing confined them — which is the strict direction here too, since an
    envelope with no network is not a boundary they would have survived
    either. When the caller supplies its own sandbox flag the launcher vouches
    for nothing: the flag stays unset and the deny lattice keeps the
    escalation recipe.

    Contained, that same envelope is wrong in a way that has nothing to do
    with strictness, and what stands in its place is spelled by
    :data:`~lup.providers.codex.confinement.CODEX_CONFINEMENT` rather than
    here -- which carries why, and is where the image-side probe reads the
    same words rather than inventing its own. This is the counterpart of
    Claude's off switch: one concept, each runtime's own word for it.
    Choosing no sandbox at all spells the same off switch on the host, and
    the notice says which wall holds instead: none, so the deny lattice
    stays standing and every unjudged command keeps its escalation recipe.
    LUP_SANDBOX_ACTIVE stays unset in both, because neither session relies on
    it -- the kernel reads the containment out of what the launch measured.

    ``mode`` is a mode the declaration names beside its wall: it narrows the
    inner envelope and replaces the container's and the host's off switch,
    as :func:`codex_sandbox_mode` reconciles the two.
    """
    overrides = [
        word
        for word in extra_args
        if word in CODEX_SANDBOX_OVERRIDES or word.startswith("--sandbox=")
    ]
    if overrides:
        Notice(
            text=(
                f"codex sandbox: caller envelope ({' '.join(overrides)}) — "
                "deny lattice stays active"
            ),
            urgency="warning",
        ).say()
        return []
    match sandbox:
        case LaunchSandbox.OUTER:
            Notice(
                text=(
                    "codex sandbox: off inside the container — "
                    "the container is the boundary, and its proxy is the way out"
                ),
                urgency="boundary",
            ).say()
            return list(CODEX_CONFINEMENT.off) if mode is None else ["--sandbox", mode]
        case LaunchSandbox.NONE:
            Notice(
                text=(
                    "codex sandbox: off on the host — "
                    "the semantic policy alone judges, and its deny lattice stands"
                ),
                urgency="boundary",
            ).say()
            return list(CODEX_CONFINEMENT.off) if mode is None else ["--sandbox", mode]
        case LaunchSandbox.INNER:
            pass
    # Exercised before it is vouched for, the way the Claude path exercises
    # its confinement tools. Asserting the flag outright was the asymmetry:
    # `codex sandbox` runs a command under this exact envelope and no model
    # turn, so there was never a reason not to ask.
    vouched = apply_sandbox_environment(
        hooks,
        environment,
        "codex",
        [codex_envelope_requirement()],
        sandbox=sandbox,
        announce=False,
    )
    if vouched:
        Notice(
            text=(
                "codex sandbox: workspace-write envelope — "
                "unjudged shell defers to the OS boundary"
            ),
            urgency="boundary",
        ).say()
    # The envelope goes on either way. What a failed probe withdraws is the
    # claim, not the confinement: leaving the sandbox off because it could not
    # be verified would answer a boundary nobody could measure by removing it.
    inner = codex_sandbox_mode(InnerSandbox(), mode)
    return [
        "--sandbox",
        inner or "workspace-write",
        *writable_root_arguments(accessible, tree),
    ]


def writable_root_arguments(
    accessible: list[AccessibleRoot] = [], tree: Path | None = None
) -> list[str]:
    """Widen the workspace-write root to the tree/ holding sibling worktrees.

    Codex roots writes at the launch directory, so a feature worktree this
    project's own workflow prescribes creating lands outside the boundary
    and cannot be edited from the session that created it. ``tree`` is that
    directory, and ``None`` where the checkout keeps no sibling worktrees,
    which leaves the declared roots to widen it alone.

    The declared roots widen it alongside, which is what makes this the same
    change as the Claude settings merge rather than a second policy: one
    registration, and both runtimes' uncontained sandboxes admit it. The
    spelling is each runtime's own -- a settings document there, a dotted
    TOML override here, whose value the CLI parses as TOML and falls back to
    treating as a literal string.

    A root nothing declared writable is left out rather than admitted
    read-only: this key grants writes, and there is no Codex spelling for
    "reachable and not writable" to be faithful to. Reads are not what it
    governs.

    A bare repository is widened to its worktrees rather than to itself. Its
    `config` and `hooks/` name what the host runs, which the container binds
    read-only and Claude's widening denies; this key cannot hold a read-only
    region inside a root, and Codex keeps only a root's `.git` read-only,
    which a bare repository does not have. So the session writes the
    worktrees the clone holds at launch and not its git directory -- which
    also leaves a worktree cut later, and a commit that writes the object
    store, to a later launch or to Claude.
    """
    roots = [
        *([str(tree)] if tree is not None else []),
        *[
            str(checkout)
            for item in accessible
            if item.writable
            for checkout in working_trees(item.path)
        ],
    ]
    if not roots:
        return []
    # lup: defer: a Codex permission profile can hold `config` and `hooks/`
    # read-only inside a writable root (`[permissions.<name>.filesystem]`,
    # deepest entry wins), which would give a mounted bare clone the whole-
    # clone reach Claude has; it replaces `--sandbox workspace-write`, which
    # overrides a profile, so it is the envelope's redesign and not this key's
    return ["-c", f"sandbox_workspace_write.writable_roots={json.dumps(roots)}"]


# lup: ignore[constant-declaration] — Codex's own sandbox names, narrowest first
CODEX_SANDBOX_WIDTH: list["CodexSandbox"] = [
    "read-only",
    "workspace-write",
    "danger-full-access",
]
"""Codex's sandbox modes, ordered by how much they let a session reach."""


def codex_sandbox_mode(
    sandbox: Sandbox, declared: "CodexSandbox | None"
) -> "CodexSandbox | None":
    """The one field Codex says both how much and how far with.

    Claude holds a permission mode and a sandbox and decides them apart.
    Codex has neither word: it states what a session may do by stating what
    it may reach, so a declaration naming a mode and a wall has named one
    field twice.

    The narrower of the two wins. That is not a precedence rule to remember
    but the refusal of one: neither may widen what the other narrowed, so an
    unattended session behind the inner sandbox reaches ``workspace-write``,
    and a planning one stays ``read-only``. No wall leaves the mode to
    whatever was declared, which is what every session meant before the wall
    was a field.

    The container takes the field instead, as ``danger-full-access`` — the
    word :data:`~lup.providers.codex.confinement.CODEX_CONFINEMENT` sends a
    launched CLI, for the same reason: Codex confines with the kernel's own
    facilities, which an unprivileged container does not hand a nested
    caller, so narrowing this field would arm a second boundary inside it —
    the one that cannot start there, which is what standing it down was for.
    A mode the declaration names itself is still sent: that is its author's
    to answer for, and a mode inferred from an autonomy is not declared.
    """
    match sandbox.posture():
        case LaunchSandbox.OUTER:
            return "danger-full-access" if declared is None else declared
        case LaunchSandbox.INNER:
            asked: list[CodexSandbox] = [
                "workspace-write",
                *([declared] if declared is not None else []),
            ]
            return min(asked, key=CODEX_SANDBOX_WIDTH.index)
        case LaunchSandbox.NONE:
            return declared


def codex_held_trees(root: Path, offered: CodexMarketplace | None) -> list[Path]:
    """The generated trees a Codex session in ``root`` runs from, as a container holds them.

    The plugin the session's home installs from, whole, and its rules file
    where the checkout offers one; then the generated agents, the project
    configuration Codex reads, the marketplace offering the plugin and the
    guidance, each alone. The machine's rendered skills and the checkout's
    local configuration stay writable, being nobody's generation. Only what
    is there, since a bind whose source is missing refuses the container.
    """
    checkout = root.resolve()
    offering = (
        [offered.source, checkout / ".codex" / "rules" / f"{offered.plugin}.rules"]
        if offered is not None and offered.source.is_relative_to(checkout)
        else []
    )
    alone = [
        checkout / ".codex" / "agents",
        checkout / ".codex" / "config.toml",
        checkout / MARKETPLACE_MANIFEST,
        checkout / "AGENTS.md",
    ]
    return [path for path in [*offering, *alone] if path.exists()]


def codex_guidance(root: Path, sandbox: Sandbox) -> dict[Path, str]:
    """The guidance a Codex session's container holds over ``AGENTS.md``.

    Rendered the way generation renders the project's and held to its
    budget, then written outside the checkout; nothing where the wall swaps
    no guidance in.
    """
    document = sandbox.held_guidance()
    if document is None:
        return {}
    try:
        rendered = CodexGuidanceRenderer(
            codex_prompt_renderer(), CodexSpellings()
        ).guidance(document)
        reject_oversized_guidance(ArtifactTree(artifacts=[rendered]))
    except ValueError as refused:
        raise LaunchRefused(f"this session's guidance: {refused}") from refused
    return held_guidance(root, rendered.path, rendered.content)


def codex_account_environment(agent: "Codex") -> EnvVars:
    """The account a session runs as: the home named outright, or its profile's.

    A home named outright is the account's own, found by whoever named it,
    so the profile beside it only names that account and is not looked up.
    """
    if agent.home is not None:
        return CODEX_LOGIN.environment(agent.home)
    return profile_environment(CODEX_LOGIN, agent.profile)


def compiled_codex(agent: "Codex") -> "Codex":
    """The declaration as its sessions open with it, the one compilation both outputs start from.

    A compatible endpoint becomes the provider definition and credential the
    thread is configured with, here rather than at declaration, so an agent
    can be copied and changed before it is built.

    What the declaration leaves unset is the person's to answer, read from
    their lup config as each session opens: a model left unnamed runs on
    their tier, unless a provider of its own serves the session, and an
    effort left unnamed starts from theirs. Which home the account runs in
    is each output's own: its home itself for a session opened here, and a
    home derived from it for the worktree a launch opens in.
    """
    personal = personal_config(UserConfigFile())
    served = (
        agent.endpoint is not None
        or agent.model_provider is not None
        or agent.provider_config is not None
    )
    model = agent.model if agent.model is not None or served else personal.tier
    config = agent.model_copy(
        update={
            "model": model,
            "effort": agent.effort
            or codex_default_effort(
                model, agent.model_tiers, personal.effort or "xhigh"
            ),
        }
    )
    if config.endpoint is None:
        return config
    from lup.providers.codex.config import CodexCompatibilityTransform

    return CodexCompatibilityTransform(config.endpoint).apply(config)


def codex_launched(agent: "Codex") -> "Codex":
    """The declaration with every field it left unset taking a launch's default.

    Read off what was set, so a field declared with the same value as its
    default still counts as said: built-in tools left unnamed are Codex's own
    stock, an unset sandbox is the verified container where an engine answers
    and the inner sandbox with a warning where none does, an unset identity is
    the worktree's name, and an unset record is the run's transcript.
    """
    launched = agent
    if "builtin" not in agent.tools.model_fields_set:
        tools = agent.tools.model_copy(update={"builtin": "stock"})
        launched = launched.model_copy(update={"tools": tools})
    if "sandbox" not in agent.model_fields_set:
        launched = launched.model_copy(update={"sandbox": launched_sandbox()})
    if agent.identity is None:
        launched = launched.model_copy(update={"identity": Member()})
    if agent.record is None:
        launched = launched.model_copy(update={"record": Recording(transcript=True)})
    return launched


def codex_mcp_arguments(tools: "CodexTools") -> list[str]:
    """The declared servers as a launched CLI starts them, one ``--config`` table each.

    Declared per session over whatever the home carries under the same
    names, so the declaration's roster is what the session serves.

    A server reading what the launcher exported names it in ``env_vars``,
    because Codex starts a stdio server under a fixed base environment and
    forwards nothing else: without it, a coordination server joins the roster
    under no id, and a nested agent its tools open spends no allowance. A
    deadline the launch declares lands as ``startup_timeout_sec``, per
    server, in this runtime's own unit.

    ``default_tools_approval_mode`` grants every declared server outright,
    the decision the other runtime's settings compile into their served-tool
    grants: a server declared here is the declarer's own code, wired in
    deliberately, so asking per call would make the declaration a
    suggestion — and a session with no operator to ask would hold every
    server it was given and call none of them.
    """
    serve = (
        tools.serve
        if tools.serve.runtime is not None
        else tools.serve.model_copy(update={"runtime": "codex"})
    )
    launched = [
        (server.name, key, value)
        for server in tools.mcp
        for key, value in dict(server.launched(serve)).items()
        if key != "type"
    ]
    forwarded = [
        (server.name, "env_vars", named)
        for server in tools.mcp
        if (named := server.launcher_variables())
    ]
    deadlines = [
        (server.name, "startup_timeout_sec", deadline)
        for server in tools.mcp
        if (deadline := server.startup_timeout(serve)) is not None
    ]
    approved = [
        (server.name, "default_tools_approval_mode", "approve") for server in tools.mcp
    ]
    return [
        argument
        for name, key, value in [*launched, *forwarded, *deadlines, *approved]
        for argument in ("--config", f"mcp_servers.{name}.{key}={json.dumps(value)}")
    ]


def refuse_codex_in_process_fields(agent: "Codex") -> None:
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
                agent.delegated_tools is not None,
                "delegated tools describe a role a program delegates to",
            ),
            (
                agent.layers != SessionLayers(),
                "layers wrap sessions opened in this process",
            ),
            (
                bool({"correction", "continuation"} & agent.model_fields_set),
                "corrections answer a Stop hook for a program driving the turn",
            ),
        )
        if asked
    ]
    if refused:
        raise LaunchRefused("cannot launch this declaration: " + "; ".join(refused))


def codex_arguments(
    config: "Codex", envelope: list[str], words: list[str]
) -> list[str]:
    """The words Codex starts with, from a declaration already launched and compiled.

    The reopening subcommand leads, because a word placed after a positional
    session id would be read as another one; the envelope follows it, then
    the model and its effort, then every override the declaration compiles
    to, and the caller's ``words`` last.
    """
    refuse_codex_in_process_fields(config)
    model = config.model_id()
    effort = config.resolved_effort()
    builtins = config.builtins()
    return [
        *codex_resume_arguments(resumption(config.resume)),
        *envelope,
        *(["--model", model] if model is not None else []),
        *(codex_effort_arguments(effort) if effort is not None else []),
        *codex_mcp_arguments(config.tools),
        *(
            []
            if config.tools.builtin == "stock"
            else builtins.configuration_arguments()
        ),
        *(
            ["--config", f"developer_instructions={json.dumps(config.system_prompt)}"]
            if config.system_prompt
            else []
        ),
        *(
            ["--config", f"approval_policy={json.dumps(config.approval_policy)}"]
            if config.approval_policy is not None
            else []
        ),
        *(
            [
                "--config",
                f"approvals_reviewer={json.dumps(config.approvals_reviewer)}",
            ]
            if config.approvals_reviewer is not None
            else []
        ),
        *(
            ["--config", f"model_provider={json.dumps(config.model_provider)}"]
            if config.model_provider is not None
            else []
        ),
        *[
            argument
            for key, value in (config.provider_config or {}).items()
            for argument in ("--config", f"{key}={json.dumps(value)}")
        ],
        *words,
    ]


def codex_plugin_root(agent: "Codex", root: Path) -> Path:
    """The project whose Codex marketplace offers the plugin a session installs.

    Codex installs a plugin from a marketplace rather than loading a
    directory, so a built plugin is named by the project offering it; a
    harness compiles into, and a declaration naming none installs from,
    the checkout it opens in.
    """
    return agent.plugin if isinstance(agent.plugin, Path) else root


def codex_root(agent: "Codex") -> Path:
    """The directory a launch opens in: the declared one, or where this process stands."""
    return agent.workspace().resolve()


def codex_checked(agent: "Codex", sentinels: LaunchSentinels) -> LaunchOpening:
    """Clear the gates before a session: the CLI's probes, then the declared requirements."""
    launched = codex_launched(agent)
    posture = launched.sandbox.posture()
    opening = LaunchOpening(sandbox=posture)

    def readiness() -> list[CapabilityEvidence[CodexCliEvidence]]:
        return [probe.probe() for probe in codex_capability_probes(launched.executable)]

    opening.findings = runtime_preflight(
        "codex",
        readiness,
        declared_requirements(launched.plugin, launched.requirements),
        codex_root(launched),
        sentinels,
        opening,
        posture.contained(),
    )
    return opening


class CodexLaunchHome(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """The home a launched Codex session runs in, and what it was derived from."""

    store: CodexWorktreeHomeStore
    selection: CodexHomeSelection
    settings: CodexAccountSettings | None = None
    """The person's settings, carried into a container's home at its start."""


def codex_launch_home(
    agent: "Codex", environment: EnvVars, root: Path
) -> CodexLaunchHome:
    """The home a launch runs in: the one named outright, or one derived for this worktree.

    Derived from the account's home, the profile's where one is declared,
    and seeded with the person's theme, editor and settings; a contained
    session's settings are captured to be carried into the container's own.
    """
    config = UserConfigFile()
    personal = personal_config(config)
    try:
        account_home = profile_directory(CODEX_LOGIN, config).launch_home(agent.profile)
    except (KeyError, DefaultHomeProfile) as error:
        raise LaunchRefused(str(error)) from error
    store = CodexWorktreeHomeStore(
        account_home=account_home or CODEX_LOGIN.ambient_home,
        theme=personal.theme.codex,
        editor=personal.editor,
        settings=personal.codex.settings,
    )
    selection = select_codex_home(agent.home, environment, root, store)
    contained = agent.sandbox.posture().contained()
    settings = CodexAccountSettings.capture(selection.path) if contained else None
    return CodexLaunchHome(store=store, selection=selection, settings=settings)


def check_codex(agent: "Codex") -> list[Finding]:
    """Every finding a launch of this declaration would clear, the login included.

    The login is asked of the home the session would run in on the host; a
    contained session is authenticated inside its container as it opens.
    """
    findings = codex_checked(agent, LaunchSentinels()).findings
    launched = codex_launched(agent)
    if launched.sandbox.posture().contained():
        return findings
    root = codex_root(launched)
    environment = {**inherited_environment(), **compiled_codex(launched).environment}
    home = codex_launch_home(launched, environment, root)
    codex_login_preflight(
        home.selection.path,
        {**environment, CODEX_HOME: str(home.selection.path)},
        [str(launched.executable)],
    )
    return findings


def prepare_codex(agent: "Codex", force: bool = False) -> None:
    """Compile a harness plugin into this project's tree, then install it into the home.

    Installed, with its hooks and the project trusted, into the home a host
    session would run in; a contained session's home is prepared inside its
    container as it opens, which the launch does. ``force`` reinstalls a
    plugin whose version has not moved.
    """
    launched = codex_launched(agent)
    root = codex_root(launched)
    if isinstance(launched.plugin, Harness):
        generate(codex_generation_recipe(root, ProjectContent(harness=launched.plugin)))
    if launched.sandbox.posture().contained():
        return
    environment = {**inherited_environment(), **compiled_codex(launched).environment}
    home = codex_launch_home(launched, environment, root)
    prepare_codex_plugin(
        [],
        home.selection.path,
        codex_plugin_root(launched, root),
        environment,
        force,
        settings=home.settings,
    )


class CodexLaunchState(BaseModel, arbitrary_types_allowed=True):
    """What preparing a launched Codex home settled, read again once it ends."""

    installed: list[Path] = []
    applied: list[JsonObject] = []


def codex_opening(
    agent: "Codex",
    words: list[str],
    sentinels: LaunchSentinels,
    opening: LaunchOpening,
    home: CodexLaunchHome,
    state: CodexLaunchState,
    force: bool = False,
    transcript: Path | None = None,
    joined: Joined = Joined(),
) -> LaunchCommand:
    """Compile a launched declaration into the process that opens its session.

    Settles what the argv depends on the way a launch does: the envelope
    exercised before it is vouched for, the home prepared and the login
    refreshed through the boundary the session runs behind, the boundary
    measured and recorded, and an outer container's image and egress made
    ready, since the argv names them. ``joined`` is what the host companions
    held around the session hand it: their variables join its environment
    and their folders its sandbox's.
    """
    launched = codex_launched(agent)
    compiled = compiled_codex(launched)
    config = compiled.model_copy(
        update={"sandbox": compiled.sandbox.widened(joined.mounts)}
    )
    root = codex_root(launched)
    posture = config.sandbox.posture()
    policy = config.enforced_policy()
    member = launched_member(root, config.identity.name if config.identity else None)
    environment = withheld_secrets(inherited_environment(), root)
    environment.update(config.environment)
    environment.update(joined.environment)
    environment.update(allowance_environment(config.max_recursive_agent, environment))
    opening.banner.add(joined.notices)
    accessible = [
        *config.sandbox.roots(),
        *[AccessibleRoot(path=path) for path in config.writable_roots],
    ]
    envelope = codex_envelope(
        policy,
        environment,
        words,
        sandbox=posture,
        accessible=accessible if posture is LaunchSandbox.INNER else [],
        tree=worktrees_directory(root),
        mode=config.sandbox_mode,
    )
    arguments = codex_arguments(config, envelope, words)
    environment[CODEX_HOME] = str(home.selection.path)
    image = declared_image(config.plugin, config.sandbox)
    offered = codex_plugin_root(config, root)

    def authenticate(command: list[str], native_home: Path, headless: bool) -> None:
        codex_login_preflight(native_home, environment, command, headless=headless)
        if home.selection.isolated and not posture.contained():
            home.store.publish(root)

    def prepare(prefix: list[str], native_home: Path) -> dict[Path, str]:
        declared = CodexMarketplace.declared(offered)
        if prefix and home.settings is not None:
            state.applied.append(
                settled_codex_seed(
                    image,
                    root,
                    home.settings.personal_settings(declared is not None),
                )
            )
        prepared = prepare_codex_plugin(
            prefix, native_home, offered, environment, force, settings=home.settings
        )
        state.installed.append(native_home)
        # A contained session's home is a volume it writes, so the revision
        # its hooks run from is held for it from a snapshot on the host.
        if not prefix or declared is None:
            return {}
        try:
            return held_revision(
                prepared, declared, image.config_home, revisions_home()
            )
        except (ValueError, FileNotFoundError) as refused:
            raise LaunchRefused(str(refused)) from refused

    argv = session_argv(
        str(config.executable),
        arguments,
        root,
        image,
        declared_requirements(config.plugin, config.requirements),
        policy,
        home.selection.path,
        CODEX_LOGIN,
        posture,
        environment,
        transcript,
        sentinels,
        opening,
        config.sandbox.roots(),
        config.sandbox.granted(),
        authenticate=authenticate,
        member=member,
        prepare=prepare,
        # Codex reads the clipboard through the X11 selection a container can
        # bridge, as its composition declares.
        clipboard="x11",
        forwarded=list(joined.environment),
        privileges=config.sandbox.privileges(),
        nested=config.sandbox.nested(),
        memory=config.sandbox.memory_limit(),
        trees=(
            codex_held_trees(root, CodexMarketplace.declared(offered))
            if config.sandbox.holds_generated()
            else []
        ),
        overlays=codex_guidance(root, config.sandbox),
    )
    return LaunchCommand(argv=argv, env=environment, cwd=root)


def codex_companions(
    agent: "Codex", root: Path, journal: TraceJournal | None
) -> CompanionLaunch:
    """The session its host companions are held for, as a launch of ``agent`` opens it."""
    return CompanionLaunch(
        root=root,
        runtime="codex",
        environment={
            **withheld_secrets(inherited_environment(), root),
            **compiled_codex(agent).environment,
        },
        journal=journal,
        relayed=loopback_relayed(agent.plugin, agent.sandbox),
    )


def codex_command(agent: "Codex", words: list[str]) -> LaunchCommand:
    """The process a launch of ``agent`` runs, compiled and settled but not run.

    Preparing the home and refreshing the login are part of what the argv
    depends on, so they happen here as they would for a launch; the boundary
    a launch records is released again once the command is known, and so are
    the host companions held to learn what they contribute.
    """
    launched = codex_launched(agent)
    root = codex_root(launched)
    sentinels = LaunchSentinels()
    opening = codex_checked(launched, sentinels)
    environment = {**inherited_environment(), **compiled_codex(launched).environment}
    home = codex_launch_home(launched, environment, root)
    try:
        with held_companions(
            launched.companions, codex_companions(launched, root, None)
        ) as joined:
            return codex_opening(
                launched,
                words,
                sentinels,
                opening,
                home,
                CodexLaunchState(),
                joined=joined,
            )
    finally:
        release_ledger(root, sentinels.nonce)


def launch_codex_session(
    agent: "Codex", words: list[str], steps: Sequence[LaunchStep], force: bool = False
) -> int:
    """Prepare, check, and run a launched Codex session in the foreground.

    The run's transcript is started before the session and closed after it;
    a worktree home returns its refreshed login and what the session changed
    of the person's settings to the account when it ends; the boundary it was
    measured behind is released however it ended.
    """
    launched = codex_launched(agent)
    # Compiled once before any step runs, so a declaration the person's lup
    # config cannot answer is refused before a step has done anything.
    compiled_codex(launched)

    def session() -> int:
        root = codex_root(launched)
        if isinstance(launched.plugin, Harness):
            generate(
                codex_generation_recipe(root, ProjectContent(harness=launched.plugin))
            )
        cleared_on_the_way_in(root)
        sentinels = LaunchSentinels()
        opening = codex_checked(launched, sentinels)
        config = compiled_codex(launched)
        environment = {**inherited_environment(), **config.environment}
        home = codex_launch_home(launched, environment, root)
        if home.selection.isolated:
            Notice(
                text=(
                    f"Using worktree-scoped Codex home: {home.selection.path}, "
                    f"derived from {home.store.account_home}"
                ),
                urgency="detail",
            ).say()
        record = launched.record or Recording()
        state = CodexLaunchState()
        transcript = start_harness_transcript(
            "codex",
            CodexTranscripts(home.selection.path),
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
        contained = launched.sandbox.posture().contained()
        try:
            with held_companions(
                launched.companions,
                codex_companions(launched, root, transcript.journal),
            ) as joined:
                command = codex_opening(
                    launched,
                    words,
                    sentinels,
                    opening,
                    home,
                    state,
                    force,
                    transcript.journal.path,
                    joined,
                )
                status = run_in_foreground(command)
            succeeded = status == 0
            return status
        except KeyboardInterrupt:
            interrupted = True
            raise
        finally:
            release_ledger(root, sentinels.nonce)
            transcript.close(succeeded=succeeded, interrupted=interrupted)
            if home.selection.isolated and home.store.publish(root):
                Notice(
                    text="Returned the refreshed Codex login to the account home",
                    urgency="detail",
                ).say()
            if home.selection.isolated and (state.installed or not contained):
                carry_codex_home(
                    home.store,
                    declared_image(launched.plugin, launched.sandbox)
                    if contained
                    else None,
                    root,
                    UserConfigFile(),
                    state.applied[0] if state.applied else None,
                )

    return between_steps(steps, session)
