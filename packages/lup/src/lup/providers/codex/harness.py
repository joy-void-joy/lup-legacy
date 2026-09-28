"""Codex-native prompt, plugin, skill, agent, and guidance renderers."""

import json
import shlex
from collections.abc import Collection
from importlib import resources
from pathlib import Path

import tomlkit
from lup.providers.codex.login import CODEX_LOGIN
from lup.providers.codex.model_choice import codex_model_id
from lup.providers.codex.subagents import CodexModelTiers
from lup.providers.drift_prompt import drift_hook
from lup.providers.peer_delivery import delivery_artifacts, delivery_command
from lup.providers.roster_prompt import (
    departure_hook,
    folded,
    prompt_hook,
    store_artifacts,
    wake_hook,
)
from lup.providers.subagent_cleanup import cleanup_hooks
from lup.types import ModelTier
from lup.harness.codescan.antipatterns import DOCUMENT_IN_HAND, rule_set_for
from lup.formats.markdown import MarkdownDocument, Prose
from lup.formats.toml import TomlDocument, TomlEntry, TomlScalar
from lup.formats.yaml import YamlDocument, YamlMap, scalars
from lup.formats.banner import (
    COMMENT_FREE,
    PROMPT_TEXT,
    REGENERATE_COMMAND,
    VERBATIM_COPY,
    GeneratedBanner,
)
from lup.harness.contracts import (
    ArtifactRenderer,
    Atom,
    Instruction,
    NativeSpellings,
    PromptRenderer,
    Spelled,
    Spelling,
    Unsupported,
)
from lup.harness.generation import argument_text
from lup.harness.prompts import (
    SPAWNED_SESSION_LOSES_SHELL,
    guidance_banner,
    sentences,
)
from lup.harness.models import (
    GUIDANCE_BUDGET,
    Agent,
    GuidanceBudget,
    Artifact,
    ArtifactTree,
    Harness,
    HookSet,
    Plugin,
    PluginLocation,
    QualifiedAgentName,
    Skill,
    SkillInvocation,
    TreeLocation,
)
from lup.policy.bundle import (
    POLICY_DATA_BANNER,
    policy_kernel_modules,
    render_policy_data,
    verification_row,
    runtime_url_scope,
)
from lup.policy.dispatcher import (
    DispatcherDeclaration,
    compile_dispatcher,
    edit_evaluator_artifact,
    dispatcher_banner,
    guarded_hook_command,
    hook_guard_artifact,
)
from lup.policy.kernel.commands import no_write_facts, unresolved_evidence
from lup.policy.kernel.decision import SandboxPlacement
from lup.policy.kernel.effects import declared_verdict
from lup.policy.kernel.rows import PathRoleRow
from lup.policy.kernel.shell import excluded_prefix, sandbox_excluded
from lup.policy.refused_tools import routed_for
from lup.policy.kernel.words import (
    INTERPRETERS,
    PASS_THROUGH_WORDS,
)
from lup.policy.shell_rules import (
    RunnerTargetRule,
    ShellCommandRule,
    erase_shell_rules,
)


class CodexSpellings(NativeSpellings):
    """Spell everything portable prose names the way Codex spells it.

    Custom agents are the one location Codex keeps outside the plugin, so the
    plugin name is deliberately unused there. The model tier is declined
    outright: recorded evidence for Codex custom agents covers TOML parsing
    only, so there is no proven alias to spell a tier in.

    Reading a document whole is declined outright rather than approximated,
    saying why: nothing in this roster does it, and an approximation would
    read as an instruction the agent can follow and cost it a turn to find
    out otherwise.
    """

    @property
    def runtime_name(self) -> Atom:
        return Atom("Codex")

    @property
    def native_identifiers(self) -> list[Atom]:
        return [
            Atom("developers.openai.com"),
            Atom("learn.chatgpt.com"),
            Atom("exec_command"),
            Atom("write_stdin"),
        ]

    def render(self, invocation: SkillInvocation) -> str:
        mention = f"${invocation.plugin}:{invocation.skill}"
        arguments = " ".join(
            f"{argument.name}={shlex.quote(argument_text(argument.value))}"
            for argument in invocation.arguments
        )
        return f"{mention} {arguments}" if arguments else mention

    def invocation_pattern(self, plugin: str, placeholder: str) -> Atom:
        return Atom(f"${plugin}:{placeholder}")

    def ask_user(self, question: str) -> Instruction:
        return Instruction(
            "Ask the user directly, offering concrete options, and wait "
            f"for the answer: {question}"
        )

    def delegate(
        self, subagent_type: QualifiedAgentName, prompt: str, name: str = ""
    ) -> Instruction:
        named = f", naming the task {name!r}," if name else ""
        return Instruction(
            f"Delegate to the {subagent_type} custom agent{named} with this task: {prompt}"
        )

    def request_approval(self, action: str, reason: str) -> Instruction:
        return Instruction(
            f"Request explicit user approval before {action}. Reason: {reason}."
        )

    def relocate_session(self, path: str) -> Instruction:
        """Spell the two routes this runtime has, with the second's condition.

        No third route to name: this runtime cannot move a running session, so
        the tool the other adapter refuses does not exist here. The second
        carries its condition for the same reason it does there -- a worker's
        lease mounts every pre-existing sibling read-only, so from a worker,
        addressing one by absolute path reaches a filesystem refusing every
        write, while a worktree it cut itself is outside the lease and
        writable, and an operator's session holds every checkout writable.
        Four words here and a paragraph in `docs/contributing.md`,
        because what this renders into is budgeted: see the note on the
        other adapter's method.
        """
        return Instruction(
            f"start a session rooted at <{path}> and continue there — "
            "this runtime cannot move a running session, so work "
            "carried on here would land in the checkout it started from. "
            "Already running, address files there by absolute path, where "
            "that tree is writable, which reaches the same branch."
        )

    def escape_sandbox(self, reason: str) -> Spelling:
        """Spell the per-call escape Codex puts in the model's own hands.

        Read out of the source at tag ``rust-v0.145.0`` — the release
        ``docs/native-capabilities.md`` pins — rather than out of the prose
        pages, which describe the session-level flags and leave this one
        unmentioned. ``codex-rs/core/src/tools/handlers/shell_spec.rs`` puts
        ``sandbox_permissions`` on the shell tool the model calls, describing
        it to the model as a per-command sandbox override whose
        ``require_escalated`` value means unsandboxed execution, beside the
        ``justification`` the approval policy records; the value is pushed into
        that enum unconditionally, so it is
        offered on every session.
        ``codex-rs/core/src/tools/sandboxing.rs`` is where it lands, mapping
        ``requires_escalated_permissions()`` to a first attempt that bypasses
        the sandbox. Both paths are re-derivable at that tag, which is the
        point of naming them: the finding is checkable rather than inherited.

        This is the agent's route and not a hook's — a Codex policy verdict
        still places nothing, which is why
        :data:`~lup.providers.codex.hooks.CODEX_SEMANTICS` splits the two.
        """
        return Spelled(
            words=Instruction(
                "Issue it on the first attempt with `sandbox_permissions` set to "
                "`require_escalated`, and a `justification` saying why. A "
                "semantically allowed command is auto-approved; do not add a "
                f"shell `# lup: escalate` marker. {reason}"
            )
        )

    def watch_output(self, command: str) -> Instruction:
        """Spell the live session Codex gives the model, which is read rather
        than pushed.

        `exec_command` opens a long-lived PTY for streaming output, REPLs, and
        interactive sessions, and `write_stdin` feeds it keystrokes or, given
        none, polls what it has emitted — the prompting guide describes that
        second use as polling in so many words. The push form exists but not
        here: `command/exec` with `streamStdoutStderr` delivers
        `command/exec/outputDelta` notifications to an app-server client,
        behind `capabilities.experimentalApi`, and is not among the tools the
        model itself may call.

        So the advice inverts against Claude's. Reading the session is the
        mechanism rather than the mistake, and what is worth saying instead is
        to keep one session rather than re-running the command, which is what
        loses the output already emitted.
        """
        return Instruction(
            f"Open `{command}` with `exec_command`, which holds a live PTY, "
            "and read what it has emitted with `write_stdin` carrying no "
            "keystrokes. Keep the one session for as long as the command "
            "runs: re-running it starts over and loses everything it already "
            "reported"
        )

    def nested_run(self, prompt: str) -> Instruction:
        """Spell the non-interactive launch, `codex exec`.

        Read from codex-cli 0.155.1's own help rather than from a run, since a
        contained session reaches no Codex login and a nested `codex exec`
        answers 401, the credential sitting outside what a session is granted.
        `exec` takes
        the prompt as its argument, `--dangerously-bypass-approvals-and-sandbox`
        answers every approval a hook probe would otherwise stall on, and
        `--dangerously-bypass-hook-trust` admits a hooks file this machine has
        not trusted, which a throwaway kit's never is.
        """
        return Instruction(
            "Run `codex exec --dangerously-bypass-approvals-and-sandbox"
            f" --dangerously-bypass-hook-trust {json.dumps(prompt)}` from the"
            " kit's directory. A non-interactive run loads the hooks in that"
            " directory's settings at launch and exits when the prompt is"
            " answered"
        )

    def read_document(self, path: str) -> Spelling:
        return Unsupported(
            reason=(
                "Codex's tool roster reads a document only by running a shell "
                "command over it, and `view_image` — the one tool that takes a "
                "file whole — accepts images alone, so nothing here can be "
                "handed a PDF or an office document"
            )
        )

    def resolver_entry(self) -> Instruction:
        return Instruction(
            sentences(
                "Run `uv run lup-devtools resolve --adapter codex --detach`. "
                "`uv run lup-devtools resolve intake` first prints what a "
                "run started now would plan from — every actionable note at its "
                "file and line, the deferred ones it would carry, and the ones it "
                "would leave to their generator — creating no run and leasing no "
                "worktree, so the inventory can be read before committing to it. "
                "Where this project has a run that never finished, it refuses "
                "rather than starting a second one beside it, and lists them: "
                "relay that choice — resume with `--run-id <id>`, keeping every "
                "answer already collected, or start fresh with `--new`, which "
                "re-derives the inventory and discards them. Never take it "
                "yourself. Assembling the review branch is gated on the "
                "reserved `integration-assembly` question, so approving it is "
                "`--answer integration-assembly=approve` like any other answer. "
                "It waits zero seconds by "
                "default and parks on material questions, printing each one "
                "beside the `# lup:` notes it was raised from, the concern's "
                "spec, and its acceptance criteria; rerun with the repeatable "
                "`--answer <question-id>=<value>` flag to answer them. "
                "`--admit <text>` carries work in the human's own words: it seeds "
                "a run that does not exist yet, beside whatever notes the tree "
                "holds, and widens a parked one before its review branch is "
                "assembled. It takes the run's lock, so a run still moving "
                "refuses it — under `--detach` in the child, after the banner "
                "reports the run started, which loses the admission in "
                "silence. Admit at a park. `--admit-note <file>:<line>` "
                "names a note written in the tree and `--admit-issue <number>` an "
                "open issue; all three are repeatable. "
                "Never pass `--wait` or `--supervise`; both hold a run open "
                "for a human instead of parking — `--wait` at the mailbox, "
                "`--supervise` at the page it opens.",
                self.escape_sandbox(SPAWNED_SESSION_LOSES_SHELL).in_prose(),
            )
        )

    def arguments_ref(self) -> Atom:
        return Atom("the arguments supplied with this skill invocation")

    def runtime_docs(self) -> Instruction:
        return Instruction(
            "the Codex documentation at "
            "https://developers.openai.com/codex/ and "
            "https://learn.chatgpt.com/"
        )

    def runtime_key(self) -> str:
        return "codex"

    def project_root(self) -> str:
        # Codex substitutes nothing into a server command, but it reads this
        # config only for the project the config sits in, so the launch
        # directory is that project by construction. Naming it explicitly is
        # what makes a server started anywhere else fail instead of resolving
        # up the tree into a neighbouring checkout.
        return "."

    def model_alias(self, tier: ModelTier) -> str | None:
        return codex_model_id(tier, CodexModelTiers())

    def tree(self, location: TreeLocation) -> Atom:
        match location:
            case "tree_root":
                return Atom(".codex/")
            case "guidance_file":
                return Atom("AGENTS.md")
            case "ownership_manifest":
                return Atom(".codex/.lup-ownership.json")
            case "project_settings":
                return Atom(".codex/config.toml")
            case "personal_settings":
                return Atom(".codex/config.local.toml")
            case "marketplace":
                return Atom(".agents/plugins/marketplace.json")

    def plugin(self, plugin: str, location: PluginLocation, member: str | None) -> Atom:
        root = f".codex/plugins/{plugin}"
        match location:
            case "root":
                return Atom(f"{root}/")
            case "manifest":
                return Atom(f"{root}/.codex-plugin/plugin.json")
            case "skills":
                leaf = f"skills/{member}/SKILL.md" if member else "skills/"
                return Atom(f"{root}/{leaf}")
            case "agents":
                leaf = f"agents/{member}.toml" if member else "agents/"
                return Atom(f".codex/{leaf}")
            case "hooks":
                return Atom(f"{root}/hooks/")
            case "guidance_template":
                return Atom(f"{root}/TEMPLATE_AGENTS.md")


class CodexSkillRenderer(ArtifactRenderer[Skill]):
    """Render one portable declaration as a same-named Codex skill."""

    def __init__(self, prompts: PromptRenderer, plugin_name: str) -> None:
        self.prompts = prompts
        self.plugin_name = plugin_name

    def render(self, source: Skill) -> ArtifactTree:
        return ArtifactTree(
            artifacts=[
                Artifact.in_markdown(
                    path=Path(
                        f".codex/plugins/{self.plugin_name}/skills/{source.name}/SKILL.md"
                    ),
                    document=MarkdownDocument(
                        frontmatter=YamlDocument(
                            root=YamlMap(
                                entries=scalars(
                                    {
                                        "name": source.name,
                                        "description": source.description,
                                    }
                                )
                            )
                        ),
                        blocks=[Prose(text=self.prompts.render(source.prompt))],
                    ),
                    semantic_id=source.id,
                    banner=PROMPT_TEXT.compiled_from(source.prompt.declared_source()),
                )
            ]
        )


class CodexAgentRenderer(ArtifactRenderer[Agent]):
    """Render one portable agent as project-scoped custom-agent TOML.

    The model row appears only where the vocabulary can spell the declared
    tier, and this one declines: omitting it lets the agent inherit the session
    model, which is honest where naming another runtime's alias would not be.
    """

    def __init__(self, prompts: PromptRenderer, spellings: NativeSpellings) -> None:
        self.prompts = prompts
        self.spellings = spellings

    def render(self, source: Agent) -> ArtifactTree:
        alias = (
            None if source.model is None else self.spellings.model_alias(source.model)
        )
        declared = {
            "name": source.name,
            "description": source.description,
            "developer_instructions": self.prompts.render(source.prompt),
            **({} if alias is None else {"model": alias}),
        }
        return ArtifactTree(
            artifacts=[
                Artifact.in_toml(
                    path=Path(f".codex/agents/{source.name}.toml"),
                    document=TomlDocument(
                        entries=[
                            TomlEntry(key=key, value=TomlScalar(value=value))
                            for key, value in declared.items()
                        ]
                    ),
                    semantic_id=source.id,
                    banner=GeneratedBanner(
                        source=f"the portable agent declaration {source.id}",
                        command=REGENERATE_COMMAND,
                    ),
                )
            ]
        )


class CodexPluginManifestRenderer(ArtifactRenderer[Plugin]):
    """Render the required Codex manifest and repository marketplace entry."""

    def render(self, source: Plugin) -> ArtifactTree:
        manifest = {
            "name": source.name,
            "version": source.version,
            "description": source.description,
            "skills": "./skills/",
        }
        if source.hooks is not None:
            manifest["hooks"] = "./hooks/hooks.json"
        marketplace = {
            "name": source.marketplace,
            "plugins": [
                {
                    "name": source.name,
                    "category": "development",
                    "interface": {"displayName": "Lup"},
                    "source": {
                        "source": "local",
                        "path": f"./.codex/plugins/{source.name}",
                    },
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                }
            ],
        }
        return ArtifactTree(
            artifacts=[
                Artifact(
                    path=Path(
                        f".codex/plugins/{source.name}/.codex-plugin/plugin.json"
                    ),
                    content=json.dumps(manifest, indent=2, sort_keys=True),
                    semantic_id=source.id,
                    banner=COMMENT_FREE.compiled_from(source.id),
                ),
                Artifact(
                    path=Path(".agents/plugins/marketplace.json"),
                    content=json.dumps(marketplace, indent=2, sort_keys=True),
                    semantic_id=source.id,
                    banner=COMMENT_FREE.compiled_from(source.id),
                ),
            ]
        )


def codex_project_config(
    source: Harness,
    spellings: NativeSpellings,
    budget: GuidanceBudget = GUIDANCE_BUDGET,
) -> str:
    """Render the project config: enabled features, then every tool server.

    Codex keeps a project's servers in the same file as the rest of its
    project configuration, so this is one document rather than the separate
    artifact the other runtime reads.

    The guidance ceiling generation already enforces is restated here as
    ``project_doc_max_bytes``: the runtime truncates project guidance at its
    own default, so a document that passed generation would still reach the
    model short if the two disagreed.

    ``env_vars`` is rendered here and nowhere else because only this runtime
    needs telling. Codex starts a stdio server under a fixed base environment
    and forwards nothing else it was not asked for, so a server whose session
    relay or credential arrives as an environment variable gets neither
    unless the config names it; the other runtime hands its servers the whole
    environment and the same declaration is already satisfied there.

    ``default_tools_approval_mode`` grants every declared server outright,
    which is the same decision the other runtime's settings artifact already
    compiles into its served-tool grants, derived from the same fact: a
    server named in a plugin here is this project's own code, wired in
    deliberately, so asking per call would make the declaration a suggestion.
    Undeclared, the two runtimes disagree on it — a session opened with no
    operator to ask holds every server it was given and can call none
    of them, refusing each with its approval policy rather than with anything
    naming the servers.

    ``startup_timeout_sec`` is where a declared deadline lands, in this
    runtime's own unit. It renders only when the declaration names one, so a
    server that says nothing keeps the runtime's default instead of being
    given this file's opinion of one.
    """
    document = tomlkit.document()
    features = tomlkit.table()
    features["hooks"] = True
    document["features"] = features
    document["project_doc_max_bytes"] = budget.ceiling
    servers = tomlkit.table(is_super_table=True)
    for plugin in source.plugins:
        for server in plugin.mcp_servers:
            entry = tomlkit.table()
            entry["command"] = server.command
            entry["args"] = server.command_line(spellings)
            if server.env_vars:
                entry["env_vars"] = server.env_vars
            if server.startup_timeout_seconds is not None:
                entry["startup_timeout_sec"] = server.startup_timeout_seconds
            entry["default_tools_approval_mode"] = "approve"
            servers[server.name] = entry
    if servers:
        document["mcp_servers"] = servers
    return tomlkit.dumps(document)


class CodexGuidanceRenderer(ArtifactRenderer[Harness]):
    """Render root project guidance at Codex's documented repository location."""

    def __init__(
        self,
        prompts: PromptRenderer,
        spellings: NativeSpellings,
        budget: GuidanceBudget = GUIDANCE_BUDGET,
    ) -> None:
        self.prompts = prompts
        self.spellings = spellings
        self.budget = budget

    def render(self, source: Harness) -> ArtifactTree:
        return ArtifactTree(
            artifacts=[
                Artifact.generated(
                    path=Path("AGENTS.md"),
                    body=self.prompts.render(source.guidance),
                    semantic_id="harness.guidance",
                    banner=guidance_banner(self.prompts, source.guidance),
                ),
                Artifact.generated(
                    path=Path(".codex/config.toml"),
                    body=codex_project_config(source, self.spellings, self.budget),
                    semantic_id="harness.project-config",
                    banner=GeneratedBanner(
                        source=__name__,
                        command=REGENERATE_COMMAND,
                        notes=[
                            "Personal sandbox and approval defaults stay in "
                            "~/.codex/config.toml.",
                            "Native shell allows are generated under .codex/rules/.",
                            "Tool servers start from this project, so start "
                            "the runtime at its root.",
                        ],
                    ),
                ),
            ]
        )


CODEX_DISPATCHER = DispatcherDeclaration(
    runtime_name="Codex",
    package="lup.providers.codex",
    managed_root_env=CODEX_LOGIN.config_home_env,
    routed_tools=["Bash", "web_fetch", "apply_patch", "collaborationspawn_agent"],
    hook_events=["PermissionRequest", "PreToolUse", "PostToolUse"],
    observation_event="PostToolUse",
    observed_tools=["apply_patch", "Bash"],
    failure="stderr_exit",
    runtime_modules=["codex_patch", "policy_data"],
)
"""Everything Codex spells differently from every other runtime.

Both hook events reach the same dispatcher: the permission request carries
the approval channel an ask needs, and the pre-tool event covers the paths
that never raise one. The tools named here are both what the plugin
registers the hook for and what the compiler proves the dispatcher routes.
"""

# lup: ignore[constant-declaration] — the runtime's wire spelling of its own
# event, which no project could choose differently and still be heard
CODEX_PROMPT_EVENT = "UserPromptSubmit"
"""The event Codex fires when a prompt is submitted, before the model sees it.

Documented at https://learn.chatgpt.com/docs/hooks, where
https://developers.openai.com/codex/hooks redirects, in the same words as
Claude Code's: the hook reads `session_id`, `cwd`, `prompt` and
`hook_event_name` on stdin, `matcher` is not read for this event, and on exit
0 its stdout's `hookSpecificOutput.additionalContext` is added as context,
under a default limit of about 2,500 tokens per hook past which it spills to
disk. Documented and not yet measured: a contained session reaches no Codex
login — a nested `codex exec` answers 401, the credential sitting outside
what a session is granted — so the run that would measure this is the
operator's to start from a host terminal. The runtime's own spelling of the
moment, so not a value a project could choose.
"""

# lup: ignore[constant-declaration] — the runtime's wire spelling of its own
# event, which no project could choose differently and still be heard
CODEX_EXIT_EVENT = "SessionEnd"
"""The event Codex fires as a session ends.

Documented at https://learn.chatgpt.com/docs/hooks beside `SessionStart` and
the tool events, with `session_id` and `cwd` on stdin as for the prompt
event. Documented and not yet measured: a contained session reaches no Codex
login — a nested `codex exec` answers 401, the credential sitting outside
what a session is granted — so the run that would measure this is the
operator's to start from a host terminal. The runtime's own spelling of the
moment, so not a value a project could choose.
"""

# lup: ignore[constant-declaration] — the runtime's wire spelling of its own
# event, which no project could choose differently and still be heard
CODEX_SUBAGENT_START_EVENT = "SubagentStart"
"""The event Codex fires as a subagent begins, before its first turn.

Documented at https://learn.chatgpt.com/docs/hooks and measured on 0.155.1 in
a user-run session, whose payload is kept as a fixture: the hook reads
`agent_id`, `agent_type`, `turn_id` and `permission_mode` beside the common
fields, cannot stop the subagent — `continue: false` is parsed and ignored
here — and on exit 0 its stdout's `hookSpecificOutput.additionalContext` is
added as context the subagent reads. The runtime's own spelling of the
moment, so not a value a project could choose.
"""

CODEX_SUBAGENT_CLEANUP = (
    resources.files("lup.providers.codex")
    .joinpath("assets/subagent_cleanup.py")
    .read_text("utf-8")
)
"""The start-time sentence's host half, shipped beside the kernel it imports.

No stop event travels with it. Measured on 0.155.1: a subagent's session
outlives its report, and output forced onto that session afterwards resumes
nobody — so the sentence is owed and the refusal is not.
"""

CODEX_PATCH_RUNTIME = (
    resources.files("lup.providers.codex").joinpath("patch.py").read_text("utf-8")
)
"""Envelope decoder, shipped beside the kernel for the dispatcher to import."""


CODEX_DYNAMIC_COMMANDS = (
    *INTERPRETERS,
    *PASS_THROUGH_WORDS,
    "awk",
    "curl",
    "find",
    "gawk",
    "git",
    "mawk",
    "rm",
    "sed",
    "uv",
    "uvx",
    "wget",
    "xargs",
)
"""Executables whose semantic decision cannot be represented by one prefix."""


def codex_allow_prefixes(
    rules: list[ShellCommandRule],
    runner_targets: list[RunnerTargetRule],
    dynamic: Collection[str] = CODEX_DYNAMIC_COMMANDS,
    excluded_commands: Collection[str] = (),
) -> list[list[str]]:
    """Compile semantic allows that stay allowed for every suffix.

    Codex prefix rules approve commands but do not choose sandbox placement.
    Because the same rule also approves a caller-requested escape, only forms
    placed ``outside`` can be widened into a native allow; ambient forms stay
    inside, and one placed ``inside`` is never approved for an escape. The runtime
    hook continues to classify those forms, along with
    every command whose safety depends on parsed content.

    The erased rows are what this reads rather than the declarations they came
    from, because the rows are where both axes have been resolved down the
    nesting: a level that inherited its effect is native-allowed on exactly the
    terms of one that spelled it out, which is the same reading the dispatcher
    and the canonical policy take.

    The prefix is the approval half of an escape the caller requests with
    ``sandbox_permissions=require_escalated``. A command the boundary
    declaration excludes from isolation receives that approval, as does one
    a rule places on the launcher's host; an ambient command does not,
    because for it the escape would be the agent placing its own call
    somewhere no rule and no reviewer put it. The escape itself is chosen on
    the model's own call rather than compiled in advance —
    :meth:`CodexSpellings.escape_sandbox` carries it. The forms this cannot
    widen — a target behind a global flag, or one joined into a compound
    command — reach the hook, which judges the request there.
    """

    excluded = list(excluded_commands)

    def add(prefix: list[str], sandbox: SandboxPlacement) -> None:
        if sandbox != "outside" and not sandbox_excluded(shlex.join(prefix), excluded):
            return
        if prefix not in prefixes:
            prefixes.append(prefix)

    rows = erase_shell_rules(rules)
    gated = dict.fromkeys(row["command"] for row in rows if row["subcommand"])
    operational = dict.fromkeys(
        f"{row['command']} {row['subcommand']}" for row in rows if row["operation"]
    )
    # lup: ignore[empty-collection] — a fold over four sources with a
    # de-duplicating membership test between them, which no comprehension
    # expresses: each `add` reads the prefixes the ones before it produced
    prefixes: list[list[str]] = []
    for row in rows:
        # Derived, as everywhere a row's verdict is wanted. The unresolved
        # reading is the right one here for the reason it is right in the
        # classifier: a prefix is written down ahead of any command, so nothing
        # about a path is known when it is offered.
        earned = declared_verdict(
            row["effects"], row["refuses"], unresolved_evidence(no_write_facts())
        )
        if earned != "allow" or row["ask_flags"] or row["sandbox"] == "inside":
            continue
        match row["subcommand"], row["operation"]:
            case "", _:
                if row["command"] not in gated and row["command"] not in dynamic:
                    add([row["command"]], row["sandbox"])
            case subcommand, "":
                if f"{row['command']} {subcommand}" not in operational:
                    add([row["command"], subcommand], row["sandbox"])
            case subcommand, operation:
                add([row["command"], subcommand, operation], row["sandbox"])
    for target in runner_targets:
        # Read on the same terms as a command row above, which it was not
        # while a target stated its verdict outright: every declared target
        # took a native prefix allow, so one the project refused was approved
        # here and refused only by the hook beside it.
        earned = declared_verdict(
            list(target.effects), target.refuses, unresolved_evidence(no_write_facts())
        )
        if earned != "allow":
            continue
        add(["uv", "run", target.name], target.sandbox)
    # Seeded from the declaration rather than derived from a rule that happens
    # to overlap it. An exclusion may be narrower than any rule name — three
    # verbs of a toolchain the rules name whole — and a prefix taken from the
    # rule would approve the other thirty. Read down to its literal head, which
    # is the conservative half of a pattern's meaning: whatever a wildcard goes
    # on to match, the boundary drops at least everything the head covers.
    for pattern in excluded_commands:
        head = excluded_prefix(pattern)
        if head and head not in prefixes:
            prefixes.append(head)
    return sorted(prefixes)


def render_codex_rules(source: HookSet) -> str:
    """Render project-local native execution rules for prefix-safe allows."""
    rows = [
        f"prefix_rule(pattern = {json.dumps(prefix)}, decision = "
        '"allow", justification = "Allowed by Lup semantic shell policy")'
        for prefix in codex_allow_prefixes(
            source.resolved_shell_rules(),
            list(source.runner_targets),
            excluded_commands=source.sandbox.excluded_commands
            if source.sandbox
            else (),
        )
    ]
    return "\n".join([*rows, ""])


class CodexHookRenderer(ArtifactRenderer[HookSet]):
    """Render Codex hooks, canonical kernel, and application policy rows."""

    def __init__(
        self, plugin_name: str, worker_identity: str, spellings: NativeSpellings
    ) -> None:
        self.plugin_name = plugin_name
        self.worker_identity = worker_identity
        self.spellings = spellings

    def render(self, source: HookSet) -> ArtifactTree:
        policy_hook = {
            "type": "command",
            "command": guarded_hook_command("PLUGIN_ROOT"),
            "statusMessage": "Checking Lup policy",
            "timeout": source.policy_timeout,
        }
        decided = [
            {
                "matcher": "|".join(
                    routed_for(CODEX_DISPATCHER.routed_tools, source.refused_tools)
                ),
                "hooks": [policy_hook],
            }
        ]
        observed = [
            {
                "matcher": "|".join(CODEX_DISPATCHER.observed_tools),
                "hooks": [policy_hook],
            }
        ]
        delivery = [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": delivery_command("PLUGIN_ROOT"),
                        "timeout": 10,
                    }
                ],
            }
        ]
        # Two folds under the one event, kept side by side rather than merged:
        # who else is here, and whether what this project is built on still
        # stands at one commit. Both are context and neither can refuse, so
        # the runtime runs whichever of them the project declared.
        roster = folded(
            [
                wake_hook(
                    Path(f".codex/plugins/{self.plugin_name}"),
                    "PLUGIN_ROOT",
                    source,
                    "codex",
                    ("SessionStart", CODEX_PROMPT_EVENT),
                    CODEX_LOGIN.config_home_env,
                ),
                prompt_hook(
                    Path(f".codex/plugins/{self.plugin_name}"),
                    "PLUGIN_ROOT",
                    source,
                    CODEX_PROMPT_EVENT,
                ),
                drift_hook(
                    Path(f".codex/plugins/{self.plugin_name}"),
                    "PLUGIN_ROOT",
                    source,
                    CODEX_PROMPT_EVENT,
                ),
            ]
        )
        departure = departure_hook(
            Path(f".codex/plugins/{self.plugin_name}"),
            "PLUGIN_ROOT",
            source,
            CODEX_EXIT_EVENT,
        )
        # A subagent is told at its start what it opens is its own to close.
        # It is not refused at its stop: measured on 0.155.1, the session it
        # leaves running resumes nobody, so no stop event is named here.
        cleanup = cleanup_hooks(
            Path(f".codex/plugins/{self.plugin_name}"),
            "PLUGIN_ROOT",
            source,
            CODEX_SUBAGENT_CLEANUP,
            "lup.providers.codex.assets.subagent_cleanup",
            CODEX_SUBAGENT_START_EVENT,
            None,
        )
        hooks = {
            "hooks": {
                **{
                    event: (
                        observed
                        if event == CODEX_DISPATCHER.observation_event
                        else [*decided, *delivery]
                        if event == "PreToolUse"
                        else decided
                    )
                    for event in CODEX_DISPATCHER.hook_events
                },
                **roster.registered,
                **departure.registered,
                **cleanup.registered,
            }
        }
        evidence = {
            "schemaVersion": 1,
            "policyIds": source.policy_ids,
            "askApproximation": "asks require explicit exact review receipts at PreToolUse and PermissionRequest",
        }
        return ArtifactTree(
            artifacts=[
                Artifact(
                    path=Path(f".codex/plugins/{self.plugin_name}/hooks/hooks.json"),
                    content=json.dumps(hooks, indent=2, sort_keys=True),
                    semantic_id=source.id,
                    banner=COMMENT_FREE.compiled_from(source.id),
                ),
                Artifact.generated(
                    path=Path(f".codex/rules/{self.plugin_name}.rules"),
                    body=render_codex_rules(source),
                    semantic_id=source.id,
                    banner=GeneratedBanner(
                        source="lup.policy.shell_rules",
                        command=REGENERATE_COMMAND,
                    ),
                ),
                Artifact(
                    path=Path(
                        f".codex/plugins/{self.plugin_name}/hooks/scripts/policy.py"
                    ),
                    content=compile_dispatcher(CODEX_DISPATCHER),
                    semantic_id=source.id,
                    executable=True,
                    banner=dispatcher_banner(CODEX_DISPATCHER),
                ),
                hook_guard_artifact(
                    Path(f".codex/plugins/{self.plugin_name}"), source.id
                ),
                edit_evaluator_artifact(
                    Path(f".codex/plugins/{self.plugin_name}"),
                    CODEX_DISPATCHER,
                    source.id,
                ),
                *roster.artifacts,
                *delivery_artifacts(
                    Path(f".codex/plugins/{self.plugin_name}"), source.id
                ),
                *departure.artifacts,
                *cleanup.artifacts,
                *store_artifacts(Path(f".codex/plugins/{self.plugin_name}"), source.id),
                *[
                    Artifact(
                        path=Path(
                            f".codex/plugins/{self.plugin_name}/hooks/runtime/"
                            f"kernel/{module.name}"
                        ),
                        content=module.source,
                        semantic_id=source.id,
                        banner=VERBATIM_COPY.compiled_from(module.origin()),
                    )
                    for module in policy_kernel_modules()
                ],
                Artifact(
                    path=Path(
                        f".codex/plugins/{self.plugin_name}/hooks/runtime/"
                        "codex_patch.py"
                    ),
                    content=CODEX_PATCH_RUNTIME,
                    semantic_id=source.id,
                    banner=VERBATIM_COPY.compiled_from("lup.providers.codex.patch"),
                ),
                Artifact.generated(
                    path=Path(
                        f".codex/plugins/{self.plugin_name}/hooks/runtime/"
                        "policy_data.py"
                    ),
                    banner=POLICY_DATA_BANNER,
                    body=render_policy_data(
                        verification=verification_row(source.subagent_cleanup),
                        allowed_fetch_scopes=[
                            runtime_url_scope(
                                str(scope.origin),
                                scope.path_prefix,
                                reason=scope.reason,
                                include_subdomains=scope.include_subdomains,
                                any_port=scope.any_port,
                            )
                            for scope in source.allowed_fetch
                        ],
                        denied_fetch_scopes=[
                            runtime_url_scope(
                                str(scope.origin),
                                scope.path_prefix,
                                reason=scope.reason,
                                include_subdomains=scope.include_subdomains,
                                any_port=scope.any_port,
                            )
                            for scope in source.denied_fetch
                        ],
                        protected_roots=[
                            path.as_posix() for path in source.protected_edit_roots
                        ],
                        human_owned_files=[
                            path.as_posix() for path in source.human_owned_files
                        ],
                        autonomous_agent_identities=(
                            [self.worker_identity] if self.worker_identity else []
                        ),
                        path_roles=[
                            PathRoleRow(root=role.root.as_posix(), role=role.role)
                            for role in source.path_roles
                        ],
                        acceptance_guard=guard.erased()
                        if (guard := source.acceptance_guard)
                        else None,
                        spawn_names=names.erased()
                        if (names := source.spawn_names)
                        else None,
                        shell_rules=source.resolved_shell_rules(),
                        edit_rules=source.resolved_edit_rules(),
                        import_boundaries=source.resolved_import_boundaries(),
                        refused_tools=list(source.refused_tools),
                        peer_policy=source.peer_policy,
                        recoverable_target_limit=source.recoverable_target_limit,
                        unscoped_fetch=source.unscoped_fetch,
                        refused_paths=list(source.refused_paths),
                        secret_variables=list(source.secret_variables),
                        runner_targets=list(source.runner_targets),
                        sandbox_excluded_commands=source.excluded_commands(),
                        auto_escape_prefixes=codex_allow_prefixes(
                            source.resolved_shell_rules(),
                            list(source.runner_targets),
                            source.excluded_commands(),
                        ),
                        diagnostics_command=source.diagnostics_command,
                        resolution_command=source.resolution_command,
                        repair_command=source.repair_command,
                        hook_timeout=source.policy_timeout,
                        rules=rule_set_for(
                            self.spellings.read_document(DOCUMENT_IN_HAND),
                            source.rules,
                            source.anti_patterns,
                        ),
                    ),
                    semantic_id=source.id,
                ),
                Artifact(
                    path=Path(
                        f".codex/plugins/{self.plugin_name}/hooks/runtime/evidence.json"
                    ),
                    content=json.dumps(evidence, indent=2, sort_keys=True),
                    semantic_id=source.id,
                    banner=COMMENT_FREE.compiled_from(source.id),
                ),
            ]
        )
