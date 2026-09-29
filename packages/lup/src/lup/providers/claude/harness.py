"""Claude-native prompt and artifact renderers."""

import json
from importlib import resources
import shlex
from collections.abc import Sequence
from pathlib import Path
from lup.providers.claude.login import CLAUDE_LOGIN
from lup.harness.codescan.antipatterns import DOCUMENT_IN_HAND, rule_set_for
from lup.providers.peer_delivery import delivery_artifacts, delivery_command
from lup.providers.drift_prompt import drift_hook
from lup.providers.subagent_cleanup import cleanup_hooks
from lup.providers.coordination_caller import caller_hooks
from lup.providers.roster_prompt import (
    PromptHook,
    departure_hook,
    folded,
    prompt_hook,
    store_artifacts,
)
from lup.formats.banner import COMMENT_FREE, PROMPT_TEXT, VERBATIM_COPY
from lup.formats.markdown import MarkdownDocument, Prose
from lup.formats.yaml import YamlDocument, YamlEntry, YamlList, YamlMap, scalars
from lup.harness.contracts import (
    ArtifactRenderer,
    Atom,
    Instruction,
    NativeSpellings,
    PromptRenderer,
    Spelled,
    Spelling,
)
from lup.harness.generation import argument_text
from lup.harness.prompts import (
    SPAWNED_SESSION_LOSES_SHELL,
    guidance_banner,
    sentences,
)
from lup.harness.models import (
    Agent,
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
from lup.policy.kernel.rows import PathRoleRow
from lup.policy.refused_tools import routed_for
from lup.providers.claude.subagents import model_alias
from lup.types import JsonValue, ModelTier


class ClaudeSpellings(NativeSpellings):
    """Spell everything portable prose names the way Claude Code spells it."""

    @property
    def runtime_name(self) -> Atom:
        return Atom("Claude Code")

    @property
    def native_identifiers(self) -> list[Atom]:
        return [
            Atom("AskUserQuestion"),
            Atom("subagent_type"),
            Atom("EnterWorktree"),
            Atom("ExitWorktree"),
            Atom("docs.claude.com"),
            Atom("code.claude.com"),
            Atom("Monitor"),
        ]

    def render(self, invocation: SkillInvocation) -> str:
        command = f"/{invocation.plugin}:{invocation.skill}"
        arguments = " ".join(
            f"{argument.name}={shlex.quote(argument_text(argument.value))}"
            for argument in invocation.arguments
        )
        return f"{command} {arguments}" if arguments else command

    def invocation_pattern(self, plugin: str, placeholder: str) -> Atom:
        return Atom(f"/{plugin}:{placeholder}")

    def ask_user(self, question: str) -> Instruction:
        return Instruction(
            "Ask the user with the AskUserQuestion tool, offering concrete "
            f"options plus a free-text choice: {question}"
        )

    def delegate(
        self, subagent_type: QualifiedAgentName, prompt: str, name: str = ""
    ) -> Instruction:
        named = f", name={json.dumps(name)}" if name else ""
        return Instruction(
            f"Delegate with Agent(subagent_type={json.dumps(subagent_type)}{named}"
            f", prompt={json.dumps(prompt)})"
        )

    def request_approval(self, action: str, reason: str) -> Instruction:
        return Instruction(
            f"Request explicit user approval before {action}. Reason: {reason}."
        )

    def relocate_session(self, path: str) -> Instruction:
        """Spell all three routes, cheapest first, with the cost of the last.

        This runtime can move a running session, and moving one arms its
        worktree isolation -- a separate check on command *shape* that refuses
        any command carrying one of fifteen shell words as an argv element,
        in any position, whether or not git appears anywhere in it. A session
        *launched* already rooted in the worktree is never isolated, so it
        does the same work and keeps `grep -c hash`.

        All three are spelled because the cheapest one is not always
        available. Launching belongs to whoever is starting work; an agent
        already mid-session cannot launch anything, and telling it to would
        be instructing it to do what it cannot -- so the fallback that
        reaches the same branch from where it stands is named second.

        That fallback carries its condition, because it is the one route a
        lease can take away. A resolver worker's lease mounts every sibling
        that existed when the worker started read-only
        (:func:`lup.sandbox.rail.worker_lease`), so from a worker, addressing
        a *pre-existing* sibling by absolute path reaches a filesystem
        refusing every write -- while one it cut itself is outside the lease
        and writable, which is why the same sentence is right where a
        worktree was just made and wrong where one was merely found. An
        operator's session holds every checkout writable
        (:func:`lup.sandbox.rail.lease_for`) and meets no such wall. Stated
        flatly it read as an equal alternative to launching, and an agent
        following it into a leased sibling spends its next hour discovering
        the mount.

        The condition is four words here and a paragraph in
        `docs/contributing.md`, which this instruction's paragraph already
        points at. Not a stylistic split: the scaffold ceiling this renders
        into leaves an adopting domain its own room, and the guidance sat
        within twenty-one bytes of that ceiling -- so the words that earn a
        place here are the ones that stop a wrong move, and the ones that
        explain it belong where there is room to explain.

        The third is named as refused rather than merely discouraged: the
        tool table denies it, carrying the cost as its reason, and a
        deliberate use escalates. Prose that only calls a route expensive is
        prose an agent reads once, so the gate is the table and the sentence
        is what keeps the reflex from reaching for it.

        What it reaches is stated beside the refusal, because escalating past
        one wall meets another. `git worktree create` cuts under a sibling
        `tree/`, and this runtime switches into a path outside its own
        `.claude/worktrees/` only as a session's first entry from the
        directory it launched in -- so a second switch is refused by the tool
        itself, whatever this project decides. An agent told the refusal
        alone would escalate it and meet an error the prose said could not
        happen; `docs/contributing.md` carries the measurement.
        """
        return Instruction(
            f"work in <{path}> by whichever of these you can reach: launch a "
            "session rooted there; or, already running, address its files by "
            "absolute path, where that tree is writable. "
            "`EnterWorktree` is refused, and takes a `tree/` path only as a "
            "session's first switch: entering one arms worktree "
            "isolation, whose refusals cover ordinary read-only commands for "
            "the rest of the session. Escalate it if you must, and "
            'leave with `ExitWorktree(action="keep")`'
        )

    def escape_sandbox(self, reason: str) -> Spelling:
        return Spelled(
            words=Instruction(
                f"Launch it with `dangerouslyDisableSandbox: true`. {reason}"
            )
        )

    def watch_output(self, command: str) -> Instruction:
        """Spell the push-based waiter, which is a tool rather than a call.

        `Monitor` treats each stdout line as an event and ends the watch when
        the command exits, so one invocation covers both "tell me when
        something moves" and "tell me when it is over" without the agent
        asking again. Running the same command through `Bash` with a long
        timeout returns once, at the end, and reading a background session
        repeatedly is the polling loop this exists to avoid. A watch left
        live past the report is the other failure: the runtime keeps it, and
        each line it emits resumes the finished reader, so the spelling says
        when to stop it as well as when not to.
        """
        return Instruction(
            f"Start a `Monitor` over `{command}`. Each line it emits arrives "
            "as an event, and the watch ends when the command does. Do not run "
            "it through `Bash`, whose long timeout returns once at the end, "
            "and do not read a backgrounded session on a loop — both are "
            "polling, however patient. A watch that outlives your report wakes "
            "you after you have finished, so stop it with `TaskStop` before "
            "reporting unless the command has exited"
        )

    def nested_run(self, prompt: str) -> Instruction:
        """Spell the print-mode launch, which nests inside a running session.

        Measured on 2.1.278: `claude -p` runs from a session's own shell with
        no variable unset, loads the hooks in the directory's settings at
        launch, and exits when the prompt is answered. A launch also masks the
        project's `.claude/agents`, `hooks`, `skills`, `commands` and
        `settings.local.json` and hands the plugin over with `--plugin-dir`,
        so a nested run that names no directory is judged by the checkout the
        outer session opened in and never by the worktree it runs from — the
        staleness :func:`lup.devtools.harness.resolve.lease_plugin_dir` names
        for a lease, met here by whoever probes a gate where it is written.
        """
        return Instruction(
            f"Run `claude -p {json.dumps(prompt)} --permission-mode"
            " bypassPermissions` from the kit's directory. A print-mode session"
            " loads the hooks in that directory's settings at launch, runs"
            " nested inside this one, and exits when the prompt is answered."
            " Its plugin is the one the outer session launched with, so a gate"
            " written in a worktree is probed only by naming that tree too:"
            " `--plugin-dir <the worktree>/.claude/plugins/<plugin>`"
        )

    def read_document(self, path: str) -> Spelling:
        return Spelled(
            words=Instruction(
                f"Hand {path} to the `Read` tool, which takes the document itself"
            )
        )

    def resolver_entry(self) -> Instruction:
        return Instruction(
            sentences(
                "Run `uv run lup-devtools resolve --adapter claude --detach`. "
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
        return Atom("$ARGUMENTS")

    def runtime_docs(self) -> Instruction:
        return Instruction(
            "the Claude Code and Agent SDK documentation at "
            "https://docs.claude.com/ and https://code.claude.com/"
        )

    def model_alias(self, tier: ModelTier) -> str | None:
        return model_alias(tier)

    def tree(self, location: TreeLocation) -> Atom:
        match location:
            case "tree_root":
                return Atom(".claude/")
            case "guidance_file":
                return Atom(".claude/CLAUDE.md")
            case "ownership_manifest":
                return Atom(".claude/.lup-ownership.json")
            case "project_settings":
                return Atom(".claude/settings.json")
            case "personal_settings":
                return Atom(".claude/settings.local.json")
            case "marketplace":
                return Atom(".claude/plugins/.claude-plugin/marketplace.json")

    def plugin(self, plugin: str, location: PluginLocation, member: str | None) -> Atom:
        root = f".claude/plugins/{plugin}"
        match location:
            case "root":
                return Atom(f"{root}/")
            case "manifest":
                return Atom(f"{root}/.claude-plugin/plugin.json")
            case "skills":
                leaf = f"commands/{member}.md" if member else "commands/"
                return Atom(f"{root}/{leaf}")
            case "agents":
                leaf = f"agents/{member}.md" if member else "agents/"
                return Atom(f"{root}/{leaf}")
            case "hooks":
                return Atom(f"{root}/hooks/")
            case "guidance_template":
                return Atom(f"{root}/TEMPLATE_CLAUDE.md")


# lup: ignore[constant-declaration] — which built-ins Claude Code ships is the
# runtime's fact to state, not a preference a caller holds
CLAUDE_ABSENT_TOOLS = ("Glob", "Grep")
"""Built-ins the portable vocabulary names that Claude Code does not ship.

The vocabulary is deliberately a superset — adapters translate their own
tool identities onto it — so a name in it is a request, not a promise. A
granted name the runtime has no tool for grants nothing while reading as a
capability the agent has, and an agent that believes it spends a turn per
attempt discovering otherwise. Filtered where the runtime is known, so a
declaration keeps naming the portable set and a runtime that ships these
again needs one edit here rather than one per declaration.
"""


# lup: ignore[constant-declaration] — where Claude Code's overlay plugin sits, a
# layout this adapter owns and every checkout ignores
CLAUDE_OVERLAY = Path(".claude/plugins/local")
"""The plugin one machine renders for itself, beside the committed one.

Inside ``.claude/plugins`` so a launch finds it where it finds every plugin a
checkout keeps, and ignored by git, because what it holds names this
machine's own profiles."""


def claude_granted_tools(tools: Sequence[str]) -> list[str]:
    """Keep only the grants this runtime can honor.

    A declaration names a tool server by the key it is registered under —
    ``mcp__notes`` — and a launch declares every server per session under
    that same key, so a grant is spelled as declared. What is left out is a
    built-in the runtime does not ship, which would read as a capability the
    agent has and grant nothing.
    """
    return [tool for tool in tools if tool not in CLAUDE_ABSENT_TOOLS]


class ClaudeSkillRenderer(ArtifactRenderer[Skill]):
    """Render one portable skill as a Claude command Markdown artifact."""

    def __init__(self, prompts: PromptRenderer, plugin: Plugin) -> None:
        self.prompts = prompts
        self.plugin = plugin
        self.plugin_name = plugin.name

    def render(self, source: Skill) -> ArtifactTree:
        granted = claude_granted_tools(source.tools)
        # A hint and a list of arguments answer the same question two ways,
        # so a skill declaring both is shown the hint it spelled itself.
        declared = [] if source.argument_hint is not None else source.arguments
        return ArtifactTree(
            artifacts=[
                Artifact.in_markdown(
                    path=Path(
                        f".claude/plugins/{self.plugin_name}/commands/{source.name}.md"
                    ),
                    document=MarkdownDocument(
                        frontmatter=YamlDocument(
                            root=YamlMap(
                                entries=[
                                    *scalars(
                                        {
                                            "description": source.description,
                                            "allowed-tools": ", ".join(granted),
                                            "argument-hint": source.argument_hint or "",
                                        }
                                    ),
                                    *(
                                        [
                                            YamlEntry(
                                                key="arguments",
                                                value=YamlList(
                                                    items=[
                                                        YamlMap(
                                                            entries=scalars(
                                                                {
                                                                    "name": argument.name,
                                                                    "description": argument.description,
                                                                    "required": argument.required,
                                                                }
                                                            )
                                                        )
                                                        for argument in declared
                                                    ]
                                                ),
                                            )
                                        ]
                                        if declared
                                        else []
                                    ),
                                ]
                            )
                        ),
                        blocks=[Prose(text=self.prompts.render(source.prompt))],
                    ),
                    semantic_id=source.id,
                    banner=PROMPT_TEXT.compiled_from(source.prompt.declared_source()),
                )
            ]
        )


class ClaudeAgentRenderer(ArtifactRenderer[Agent]):
    """Render one portable agent in Claude's Markdown format."""

    def __init__(
        self, prompts: PromptRenderer, plugin: Plugin, spellings: NativeSpellings
    ) -> None:
        self.prompts = prompts
        self.plugin = plugin
        self.plugin_name = plugin.name
        self.spellings = spellings

    def render(self, source: Agent) -> ArtifactTree:
        alias = (
            None if source.model is None else self.spellings.model_alias(source.model)
        )
        return ArtifactTree(
            artifacts=[
                Artifact.in_markdown(
                    path=Path(
                        f".claude/plugins/{self.plugin_name}/agents/{source.name}.md"
                    ),
                    document=MarkdownDocument(
                        frontmatter=YamlDocument(
                            root=YamlMap(
                                entries=scalars(
                                    {
                                        "name": source.name,
                                        "description": source.description,
                                        "tools": ", ".join(
                                            claude_granted_tools(source.tools)
                                        ),
                                        "model": alias or "",
                                        "color": source.color or "",
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


class ClaudePluginManifestRenderer(ArtifactRenderer[Plugin]):
    """Render Claude plugin metadata without importing shared native names."""

    def render(self, source: Plugin) -> ArtifactTree:
        payload = {
            "name": source.name,
            "version": source.version,
            "description": source.description,
        }
        marketplace = {
            "name": source.marketplace,
            "owner": {"name": "Lup"},
            "plugins": [
                {
                    "name": source.name,
                    "description": source.description,
                    "source": f"./{source.name}",
                }
            ],
        }
        return ArtifactTree(
            artifacts=[
                Artifact(
                    path=Path(
                        f".claude/plugins/{source.name}/.claude-plugin/plugin.json"
                    ),
                    content=json.dumps(payload, indent=2, sort_keys=True),
                    semantic_id=source.id,
                    banner=COMMENT_FREE.compiled_from(source.id),
                ),
                Artifact(
                    path=Path(".claude/plugins/.claude-plugin/marketplace.json"),
                    content=json.dumps(marketplace, indent=2, sort_keys=True),
                    semantic_id=source.id,
                    banner=COMMENT_FREE.compiled_from(source.id),
                ),
            ]
        )


class ClaudeGuidanceRenderer(ArtifactRenderer[Harness]):
    """Render project guidance at Claude's adapter-owned repository location."""

    def __init__(self, prompts: PromptRenderer) -> None:
        self.prompts = prompts

    def render(self, source: Harness) -> ArtifactTree:
        return ArtifactTree(
            artifacts=[
                Artifact.generated(
                    path=Path(".claude/CLAUDE.md"),
                    body=self.prompts.render(source.guidance),
                    semantic_id="harness.guidance",
                    banner=guidance_banner(self.prompts, source.guidance),
                )
            ]
        )


CLAUDE_DISPATCHER = DispatcherDeclaration(
    runtime_name="Claude Code",
    package="lup.providers.claude",
    managed_root_env=CLAUDE_LOGIN.config_home_env,
    routed_tools=[
        "Bash",
        "WebFetch",
        "Edit",
        "Write",
        "SendMessage",
        "ListAgents",
        "Agent",
    ],
    hook_events=["PreToolUse", "PostToolUse"],
    observation_event="PostToolUse",
    observed_tools=["Edit", "Write", "Bash"],
    failure="conservative_ask",
    runtime_modules=["caller_payload", "policy_data"],
)
"""Everything Claude Code spells differently from every other runtime.

The tools named here are both what the plugin registers the hook for and
what the compiler proves the dispatcher routes, so a tool cannot be handed
to the hook without a branch that decides it.
"""

# lup: ignore[constant-declaration] — the runtime's wire spelling of its own
# event, which no project could choose differently and still be heard
CLAUDE_PROMPT_EVENT = "UserPromptSubmit"
"""The event Claude Code fires when a prompt is submitted, before the model sees it.

Documented at https://code.claude.com/docs/en/hooks under "UserPromptSubmit":
the hook reads `session_id`, `cwd`, `prompt` and `hook_event_name` on stdin,
and on exit 0 its stdout's `hookSpecificOutput.additionalContext` is added as
context the model can see; exit 2 would block the prompt and erase it, which
nothing registered under it here does. The runtime's own spelling of the
moment, so not a value a project could choose.
"""

# lup: ignore[constant-declaration] — the runtime's wire spelling of its own
# event, which no project could choose differently and still be heard
CLAUDE_SUBAGENT_START_EVENT = "SubagentStart"
"""The event Claude Code fires as a subagent begins, before its first turn.

Documented at https://code.claude.com/docs/en/hooks under "SubagentStart" and
measured on 2.1.278: the hook reads `agent_id`, `agent_type`, `session_id`,
`cwd` and `hook_event_name` on stdin, cannot block the subagent, and on exit
0 its stdout's `hookSpecificOutput.additionalContext` is added as context the
subagent reads. The runtime's own spelling of the moment, so not a value a
project could choose.
"""

# lup: ignore[constant-declaration] — the runtime's wire spelling of its own
# event, which no project could choose differently and still be heard
CLAUDE_SUBAGENT_STOP_EVENT = "SubagentStop"
"""The event Claude Code fires as a subagent is about to hand back its report.

Documented at https://code.claude.com/docs/en/hooks under "SubagentStop" and
measured on 2.1.278: the hook reads `agent_id`, `agent_type`,
`agent_transcript_path`, `stop_hook_active`, `last_assistant_message`,
`background_tasks` and `session_crons` beside the common fields, and a
stdout of `{"decision": "block", "reason": ...}` on exit 0 keeps the subagent
running with the reason as its next instruction; the next stop then carries
`stop_hook_active` true. The runtime's own spelling of the moment, so not a
value a project could choose.
"""

CLAUDE_SUBAGENT_CLEANUP = (
    resources.files("lup.providers.claude")
    .joinpath("assets/subagent_cleanup.py")
    .read_text("utf-8")
)
"""The host half of the subagent cleanup fold, shipped verbatim beside the kernel."""

# lup: ignore[constant-declaration] — the runtime's wire spelling of its own
# event, which no project could choose differently and still be heard
CLAUDE_CALLER_EVENT = "PreToolUse"
"""The event Claude Code fires before a tool runs, where the caller is stamped.

Documented at https://code.claude.com/docs/en/hooks under "PreToolUse" and
measured on 2.1.283 against a probe tool server: an ``updatedInput`` returned
with no ``permissionDecision`` replaced an MCP call's arguments before the
server received them, and every event fired inside a subagent carried its
``agent_id``. The runtime's own spelling of the moment, so not a value a
project could choose.
"""

CLAUDE_CALLER_PAYLOAD = (
    resources.files("lup.providers.claude")
    .joinpath("assets/caller_payload.py")
    .read_text("utf-8")
)
"""The host half of the caller hook, shipped verbatim for its entry and the dispatcher."""

# lup: ignore[constant-declaration] — the runtime's wire spelling of its own
# event, which no project could choose differently and still be heard
CLAUDE_EXIT_EVENT = "SessionEnd"
"""The event Claude Code fires as a session ends, before its process exits.

Documented at https://code.claude.com/docs/en/hooks under "SessionEnd": fired
on a clean exit, `/clear`, a logout and a termination signal, with
`session_id`, `cwd`, `hook_event_name` and a `reason` on stdin, under a
budget the entry's own timeout raises. Nothing fires on a kill the process
cannot catch, which is what the pulse is for. The runtime's own spelling of
the moment, so not a value a project could choose.
"""


class ClaudeHookRenderer(ArtifactRenderer[HookSet]):
    """Render Claude hooks, canonical kernel, and application policy rows."""

    def __init__(
        self, plugin_name: str, worker_identity: str, spellings: NativeSpellings
    ) -> None:
        self.plugin_name = plugin_name
        self.worker_identity = worker_identity
        self.spellings = spellings

    def render(self, source: HookSet) -> ArtifactTree:
        command: list[JsonValue] = [
            {
                "type": "command",
                "command": guarded_hook_command("CLAUDE_PLUGIN_ROOT"),
                "timeout": source.policy_timeout,
            }
        ]
        decided: list[JsonValue] = [
            {
                "matcher": "|".join(
                    routed_for(CLAUDE_DISPATCHER.routed_tools, source.refused_tools)
                ),
                "hooks": command,
            }
        ]
        observed: list[JsonValue] = [
            {
                "matcher": "|".join(CLAUDE_DISPATCHER.observed_tools),
                "hooks": command,
            }
        ]
        # An empty matcher is every tool, which is what delivery needs and the
        # policy must not have: mail is worth carrying before a `Read` as much
        # as before an `Edit`, while a permission branch for `Read` is a
        # permission `Read` could be refused by. A second group rather than a
        # widened one keeps those apart — every matching hook runs, and the
        # most restrictive verdict wins, so a delivery that only ever allows
        # cannot loosen what the policy decided beside it.
        delivery: list[JsonValue] = [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": delivery_command("CLAUDE_PLUGIN_ROOT"),
                        "timeout": 10,
                    }
                ],
            }
        ]
        # Under its own event rather than beside the policy's: a prompt is not
        # a tool call, and what the roster has to say at that moment is
        # context rather than a verdict, so nothing here can refuse.
        # Two folds under the one event, kept side by side rather than merged:
        # who else is here, and whether what this project is built on still
        # stands at one commit. Both are context and neither can refuse, so
        # the runtime runs whichever of them the project declared.
        roster = folded(
            [
                prompt_hook(
                    Path(f".claude/plugins/{self.plugin_name}"),
                    "CLAUDE_PLUGIN_ROOT",
                    source,
                    CLAUDE_PROMPT_EVENT,
                ),
                drift_hook(
                    Path(f".claude/plugins/{self.plugin_name}"),
                    "CLAUDE_PLUGIN_ROOT",
                    source,
                    CLAUDE_PROMPT_EVENT,
                ),
            ]
        )
        # The row that arrived under the prompt event ends under the ending
        # one, written by the session itself, so a clean exit is exact and
        # the pulse only has to answer for the exits nothing announces.
        # A subagent's own row ends under the stop event its runtime fires for
        # it, beside the cleanup registered there, and is told apart from the
        # session's ending by the subagent's id in the payload.
        departure = departure_hook(
            Path(f".claude/plugins/{self.plugin_name}"),
            "CLAUDE_PLUGIN_ROOT",
            source,
            CLAUDE_EXIT_EVENT,
            CLAUDE_SUBAGENT_STOP_EVENT,
        )
        # A subagent is told at its start what it arms is its own to stop,
        # and refused once at the stop handing back its report while any of
        # it is still listed.
        cleanup = cleanup_hooks(
            Path(f".claude/plugins/{self.plugin_name}"),
            "CLAUDE_PLUGIN_ROOT",
            source,
            CLAUDE_SUBAGENT_CLEANUP,
            "lup.providers.claude.assets.subagent_cleanup",
            CLAUDE_SUBAGENT_START_EVENT,
            CLAUDE_SUBAGENT_STOP_EVENT,
        )
        # Every coordination call says which conversation made it, matched to
        # the coordination server's tools under the key a launch declares it.
        caller = caller_hooks(
            Path(f".claude/plugins/{self.plugin_name}"),
            "CLAUDE_PLUGIN_ROOT",
            source,
            CLAUDE_CALLER_PAYLOAD,
            "lup.providers.claude.assets.caller_payload",
            CLAUDE_CALLER_EVENT,
            lambda server: f"mcp__{server}__.*",
        )
        # Folded rather than merged, because two sources register under one
        # event — the policy and the caller hook before a tool, the cleanup
        # and the departure at a subagent's stop — and a merge would keep
        # whichever was written last.
        registered = folded(
            [
                PromptHook(
                    registered={
                        event: (
                            observed
                            if event == CLAUDE_DISPATCHER.observation_event
                            else [*decided, *delivery]
                        )
                        for event in CLAUDE_DISPATCHER.hook_events
                    },
                    artifacts=[],
                ),
                roster,
                departure,
                cleanup,
                caller,
            ]
        )
        hooks = {
            "description": (
                "Lup semantic permission policy, peer delivery, the calling "
                "subagent on each coordination call, the roster's changes at "
                "each prompt, a session's or subagent's departure as it ends, "
                "and a subagent's report waiting on its background work"
            ),
            "hooks": registered.registered,
        }
        evidence = {"schemaVersion": 1, "policyIds": source.policy_ids}
        return ArtifactTree(
            artifacts=[
                Artifact(
                    path=Path(f".claude/plugins/{self.plugin_name}/hooks/hooks.json"),
                    content=json.dumps(hooks, indent=2, sort_keys=True),
                    semantic_id=source.id,
                    banner=COMMENT_FREE.compiled_from(source.id),
                ),
                Artifact(
                    path=Path(
                        f".claude/plugins/{self.plugin_name}/hooks/scripts/policy.py"
                    ),
                    content=compile_dispatcher(CLAUDE_DISPATCHER),
                    semantic_id=source.id,
                    executable=True,
                    banner=dispatcher_banner(CLAUDE_DISPATCHER),
                ),
                hook_guard_artifact(
                    Path(f".claude/plugins/{self.plugin_name}"), source.id
                ),
                edit_evaluator_artifact(
                    Path(f".claude/plugins/{self.plugin_name}"),
                    CLAUDE_DISPATCHER,
                    source.id,
                ),
                *delivery_artifacts(
                    Path(f".claude/plugins/{self.plugin_name}"), source.id
                ),
                *roster.artifacts,
                *departure.artifacts,
                *cleanup.artifacts,
                *caller.artifacts,
                *store_artifacts(
                    Path(f".claude/plugins/{self.plugin_name}"), source.id
                ),
                *[
                    Artifact(
                        path=Path(
                            f".claude/plugins/{self.plugin_name}/hooks/runtime/"
                            f"kernel/{module.name}"
                        ),
                        content=module.source,
                        semantic_id=source.id,
                        banner=VERBATIM_COPY.compiled_from(module.origin()),
                    )
                    for module in policy_kernel_modules()
                ],
                Artifact.generated(
                    path=Path(
                        f".claude/plugins/{self.plugin_name}/hooks/runtime/"
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
                            [
                                self.worker_identity,
                                f"{self.plugin_name}:{self.worker_identity}",
                            ]
                            if self.worker_identity
                            else []
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
                        auto_escape_prefixes=[],
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
                        f".claude/plugins/{self.plugin_name}/hooks/runtime/evidence.json"
                    ),
                    content=json.dumps(evidence, indent=2, sort_keys=True),
                    semantic_id=source.id,
                    banner=COMMENT_FREE.compiled_from(source.id),
                ),
            ]
        )
