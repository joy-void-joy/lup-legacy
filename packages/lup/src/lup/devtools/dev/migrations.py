"""What an adopter has to do about a break, where two trees cannot say it.

Most of what a range asks of a project built on this one is derivable, and is
derived: a module that moved and a name that was renamed are fully determined
by the surface at each end, which git holds for every revision, so
:mod:`lup.devtools.dev.preservation` reads both and the map costs nothing to
keep. What is left over is the residue — a signature that gained required
parameters, a refusal that split in two, a default whose meaning changed —
where nothing in either tree says what a caller should now pass. That is what
is declared here, and only that.

A migration is keyed to the commit it applies after, because commits are what
this library ships at: a project pinned to a branch is between releases by
definition, and a record that only existed at release time would be silent
exactly when such a project is updating. A release cadence on top changes
nothing here — :func:`rendered` folds whatever is pending into the release's
changelog section, and the declarations leave the tree, so what stands here is
always the unreleased window and never a growing pile.

The gate is in this repository rather than downstream. A capability that
disappears between the merge base and the working tree, with no migration
naming it, fails the gate that would have shipped it — so a break cannot land
without its instruction, and an adopter reads a complete record rather than a
diligent one.
"""

from pathlib import Path

import sh
from pydantic import BaseModel

from lup.devtools.dev.branches import detect_base_branch
from lup.devtools.dev.release import RELEASE_SUBJECT_PREFIX
from lup.devtools.dev.preservation import (
    Capability,
    compare,
    surface_at,
    surface_now,
)
from lup.devtools.project import DevProject
from lup.execution.shell import git


class MigrationStep(BaseModel, frozen=True):
    """One thing to do about a break, in the form it can be done in."""

    instruction: str
    """What to do, as a sentence somebody can act on."""

    command: list[str] = []
    """The command that does it, where a command does.

    Empty for the ordinary case, which is the whole reason this is declared
    rather than derived: what a caller should pass to a signature that grew is
    a decision about their code, and a command that guessed at it would be
    worse than a sentence that asks.
    """

    def spelled(self) -> str:
        """This step as an update reports it, with its command where it has one."""
        return self.instruction + (
            f"\n    {' '.join(self.command)}" if self.command else ""
        )


class Migration(BaseModel, frozen=True):
    """One break, the commit that made it, and what a caller does about it."""

    subjects: list[str]
    """Every capability this break took, spelled as the surface walk spells it.

    Plural because one decision takes several names at once — a mode retired
    takes its enum member, its reader and its two commands — and splitting
    that into an entry per name would ask a reader to reassemble one change
    out of four records saying the same sentence.
    """

    reason: str
    """Why the break was taken. This is what reaches the changelog at release."""

    steps: list[MigrationStep]

    commit: str = ""
    """The commit the break landed in, once it has one.

    Empty while the declaration is younger than the commit carrying it — the
    commit being written cannot name itself — and read as *newer than every
    revision*, which is what it is: a project taking this library takes the
    break with it, whatever commit it stood at before. Filling it in later
    buys one thing, which is telling a project that already has it so.
    """

    def covers(self, capability: Capability) -> bool:
        """Whether this migration is the one that speaks for that capability.

        Matched on the name rather than on the module it was declared in: a
        capability that disappeared has no module any more, and the name is
        what a caller's code holds.
        """
        return capability.identity in self.subjects

    def applied_at(self, revision: str, root: Path = Path()) -> bool:
        """Whether a project standing at ``revision`` already has this break.

        Ancestry rather than ordering: a project pinned to a branch that never
        carried the commit has not applied it, whatever the dates say. A
        declaration naming no commit is pending for every revision, which is
        the honest answer while the break is newer than any commit that could
        be named.

        Asked of whichever checkout holds both commits. In this repository
        that is this one; in a project built on it, it is the clone of upstream
        the update already fetched, since neither commit is in the project's
        own history at all.
        """
        if not self.commit:
            return False
        try:
            git("-C", str(root), "merge-base", "--is-ancestor", self.commit, revision)
        except sh.ErrorReturnCode:
            return False
        return True

    def spelled(self) -> list[str]:
        """This migration as an update reports it: the reason, then the steps."""
        return [
            f"{', '.join(self.subjects)} — {self.reason}",
            *(f"  {step.spelled()}" for step in self.steps),
        ]


class RenderedMigrations(BaseModel, frozen=True):
    """Installed-library output consumed by a process started before its update."""

    count: int
    lines: list[str]


DECLARED: list[Migration] = [
    Migration(
        subjects=["SpawnNames.misspelled"],
        reason=(
            "a spawn whose name falls outside the shape is normalized and sent "
            "out rather than refused, so no refusal wording remains to declare"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Drop the field from a SpawnNames override. The first "
                    "character of `punctuation` is what joins the words of a "
                    "normalized name; `reason` and `recovery` still word the one "
                    "refusal left, a spawn with nothing to read a name from."
                )
            ),
        ],
    ),
    Migration(
        subjects=["SubagentCleanup.gate", "SubagentCleanup.record"],
        reason=(
            "a delegated agent is told the scoped check and the test runner it "
            "owes, and leaves the full gate to whoever lands its work"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Spell your project's scoped check in `SubagentCleanup.scoped` "
                    "and its test runner in `SubagentCleanup.tests` where `gate` "
                    "and `record` were set."
                )
            ),
        ],
    ),
    Migration(
        subjects=["ToolInput.description", "ToolInput.subagent_type"],
        reason=(
            "the stop hook names only shell work a subagent's own run started, "
            "so it reads no spawn arguments"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Nothing reads these fields of the cleanup hook's tool input. "
                    "A hook that needs a spawn's description or type reads them "
                    "from the payload its runtime hands it before the call, as "
                    "lup.policy.kernel.spawns is handed them."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "ModuleRun.renamed",
            "ModuleEdit",
            "ModuleEdit.row",
            "ModuleEdit.start",
            "ModuleEdit.end",
            "ModuleEdit.text",
            "module_edits",
            "apply_edits",
        ],
        reason=(
            "`dev relocate` plans a file's rewrite as splices that may span rows "
            "and split an import statement, which a one-row edit of a dotted run "
            "could not express"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Call respellings(tokens, moves) from lup.devtools.dev.relocate "
                    "for the splices a file's tokens need, and apply_splices(text, "
                    "splices) to write them; destination(named, moves) answers where "
                    "a dotted path lands, which ModuleRun.renamed used to."
                )
            ),
        ],
    ),
    Migration(
        subjects=["last_release_tag"],
        reason=(
            "the migrations gate measures from the release commit rather than "
            "the release tag, because a tag is pushed last on purpose and the "
            "gate was blind for exactly the window a release is under review"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Call last_release_commit() from lup.devtools.dev.migrations, "
                    "which answers with the commit `dev release` wrote rather than "
                    "the tag it then created. A caller that wanted the tag itself "
                    "reads it with `git describe --tags --abbrev=0`."
                )
            ),
        ],
    ),
    Migration(
        subjects=["CLAUDE_CONFIG_FILE"],
        reason=(
            "Claude Code reads `.config.json` only as a legacy document, ahead "
            "of `.claude.json` wherever one exists, so the constant naming it as "
            "the configuration document sent every reader to a file most homes "
            "do not hold, and every derived home it seeded began empty"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Read the document a session uses from "
                    "selected_config_home(environment).document in "
                    "lup.providers.claude.config_home, which resolves it by Claude "
                    "Code's own rule, and name CLAUDE_LEGACY_DOCUMENT where only "
                    "the legacy file is meant."
                )
            ),
        ],
    ),
    Migration(
        subjects=["ClaudeProfileSelection", "ClaudeProfileRegistry"],
        reason=(
            "an unnamed Claude profile named ~/.claude as the configuration home, "
            "and a named ~/.claude makes Claude Code read ~/.claude/.claude.json "
            "rather than the ~/.claude.json it reads when nothing is named, so "
            "every session opened through the default started from a document "
            "the account never wrote; the default names no home, as Codex's does"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Read ClaudeProfileSelection.config_directory as optional: "
                    "None leaves whichever home the session's environment already "
                    "selects, and CLAUDE_LOGIN.selected_home(environment) answers "
                    "that home, honouring whichever one the environment names. A "
                    "registry that must pin a home passes "
                    "default=ClaudeProfileSelection(config_directory=...)."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "ClaudeProfileSelection",
            "ClaudeProfileRegistrar",
            "ProfileDirectory",
        ],
        reason=(
            "a named Claude profile whose home is ~/.claude pointed each session "
            "at that home by name, which makes Claude Code read "
            "~/.claude/.claude.json rather than the ~/.claude.json a plain "
            "`claude` reads, so its sessions opened as if the account were new; "
            "a profile naming the default home is refused wherever one is "
            "registered, selected or resolved, and a typed selection cannot hold one"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Leave the profile unset to use the default account: forget a "
                    "profile registered at ~/.claude with `harness profile remove "
                    "<name>`, and pass config_directory=None rather than the default "
                    "home to ClaudeProfileSelection. Each refusal raises "
                    "DefaultHomeProfile from lup.providers.profiles, a ValueError."
                )
            ),
        ],
    ),
    Migration(
        subjects=["review_queue_rules"],
        reason=(
            "the verbs of lup's own toolchain that a person answers now include "
            "every write that widens what a later launch reaches -- `sync setup`, "
            "`sync remote`, `sync grant`, `dev init upstream`, `dev library git "
            "--url` and a launcher's `--mount`, `--mount-ro` and `--device` ask "
            "as an edit of the registry does -- so the table is named for the "
            "toolchain rather than for the review queue"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Import devtools_rules from lup.policy.vocabulary where "
                    "review_queue_rules was imported: it returns the review-queue "
                    "verbs it did, with the widening writers beside them. A rule "
                    "refusing the bare `lup-devtools` spelling keeps only its "
                    "operator-only operations and states its route as each "
                    "sub-app's reason, as the template's lup_devtools_rule does."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "create_claude",
            "create_codex",
            "CONSTRUCTORS",
            "Client",
            "Client.__init__",
            "Client.open",
            "Client.query",
        ],
        reason=(
            "a provider's declaration is its agent: Claude and Codex open "
            "sessions and ask turns themselves, so a constructor function "
            "building a Client around a config, and a Client holding nothing "
            "but that opener, stood between a caller and the one object doing "
            "the work"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Declare the agent directly: create_claude(ClaudeSessionConfig"
                    "(model=..., ...)) is Claude(model=..., ...), and "
                    "create_codex(CodexSessionConfig(...)) is Codex(...), both "
                    "importable from lup."
                )
            ),
            MigrationStep(
                instruction=(
                    "Ask where you queried: await client.query(prompt, Output) is "
                    "await agent.ask(prompt, Output), and client.open(resume) is "
                    "agent.open(resume=...), yielding a ClaudeSession or "
                    "CodexSession. Code naming no provider annotates Agent from "
                    "lup, and a hand-built Client(opener) implements that "
                    "protocol's open, ask, sessions and layered instead."
                )
            ),
            MigrationStep(
                instruction=(
                    "Read lup.LAZY_EXPORTS where lup.CONSTRUCTORS was read: it maps "
                    "Claude and Codex, among every name the package root "
                    "resolves on first access, to the modules defining them."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "create_client",
            "ProviderRoute",
            "ProviderRoute.provider",
            "ProviderRoute.matcher",
            "PROVIDER_ROUTES",
            "provider_for",
        ],
        reason=(
            "routing a model id to its provider served only create_client, "
            "whose common arguments could never carry either provider's typed "
            "options; choosing Claude or Codex is that routing, and holds them all"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Name the provider by the agent you declare: "
                    "create_client(model, system_prompt=..., cwd=...) is "
                    "Claude(model=..., system_prompt=..., cwd=...) or Codex(...) "
                    "with the same arguments. Its base_url and api_key are "
                    "endpoint=ClaudeCompatibleEndpoint(base_url=..., api_key=...) "
                    "from lup.providers.claude, or CodexCompatibleEndpoint from "
                    "lup.providers.codex."
                )
            ),
            MigrationStep(
                instruction=(
                    "Ask catalog_provider(model) in lup.providers.routing where "
                    "code needs which runtime's catalog lists a name; an id "
                    "neither catalog lists is CustomModel(id=...) on whichever "
                    "agent serves it."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "ActorSession.started",
            "StateToRequest",
        ],
        reason=(
            "the actor and background loops take a Conversation and ask it, so "
            "the names built on starting a TurnRequest went with that verb"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Take an actor's turn with ActorSession.turn(prompt, Output), "
                    "which reopens on a fresh conversation where the recorded one "
                    "is lost, as started did; ActorSession.taken(conversation, "
                    "prompt, Output, seen) is one attempt on a given conversation."
                )
            ),
            MigrationStep(
                instruction=(
                    "Construct BackgroundAgent(agent, state_to_turn, ...) with a "
                    "StateToTurn from lup.orchestration.background: where a "
                    "StateToRequest returned turn_request(prompt_for(state), "
                    "Output), the StateToTurn is lambda session, state: "
                    "session.ask(prompt_for(state), Output)."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "SessionHandle",
            "SessionHandle.session",
            "SessionHandle.fork",
            "TurnHandle",
            "TurnHandle.turn",
            "TurnHandle.events",
            "TurnHandle.interrupt",
            "TurnHandle.steer",
            "turn_request",
            "Session.start",
            "Turn.result",
        ],
        reason=(
            "a session and a turn are each one object carrying every capability "
            "its provider has, rather than a handle of optional capabilities "
            "around an engine a caller started with a request built by hand"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Ask the session: handle.session.start(turn_request(prompt, "
                    "Output)) is session.ask(prompt, Output), where session is "
                    "what agent.open() yields. The Turn it returns starts when "
                    "first awaited or iterated."
                )
            ),
            MigrationStep(
                instruction=(
                    "Use the turn itself: await turn.turn.result() is await turn; "
                    "turn.events and turn.interrupt are the methods turn.events(), "
                    "turn.live() and await turn.interrupt(); turn.steer is await "
                    "turn.steer(input), on a CodexTurn only."
                )
            ),
            MigrationStep(
                instruction=(
                    "Fork from the session: handle.fork(at) is "
                    "session.fork(at=turn_id), an async context manager yielding "
                    "the branch as a session of the same provider."
                )
            ),
            MigrationStep(
                instruction=(
                    "Implement an engine against SessionEngine and TurnEngine in "
                    "lup.sessions.capabilities, whose start and result are "
                    "Session.start and Turn.result under the engine's names; the "
                    "public Turn and Conversation are the surface over them."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "decorated_session_factory",
            "journal_session_factory",
            "recorded_session_factory",
            "financial_budget_session_factory",
            "quota_waiting_session_factory",
        ],
        reason=(
            "what wraps a session is part of the agent's declaration, its "
            "layers, so a wrapper no longer rebuilds a client around the one it "
            "was handed"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Lay the turn decorators on the agent: "
                    "decorated_session_factory(client, timeout=..., budget=...) is "
                    "agent.layered(SessionLayers(timeout=..., budget=...)), or "
                    "layers=SessionLayers(...) where the agent is declared. "
                    "SessionLayers, in lup.sessions.layers, takes the same keyword "
                    "arguments, serialized included."
                )
            ),
            MigrationStep(
                instruction=(
                    "Add a whole-session wrapper to SessionLayers(wrappers=[...]), "
                    "innermost first: journal_session_factory(client, journal) is "
                    "JournalWrapper(journal) from lup.observability.audit; "
                    "recorded_session_factory(client, recorder, session) is "
                    "CloseRecordingWrapper(recorder, session) from "
                    "lup.observability.sessions; financial_budget_session_factory"
                    "(client, config, sink) is FinancialBudgetWrapper(config, sink) "
                    "from lup.sessions.budget; and quota_waiting_session_factory"
                    "(client, config, sink) is QuotaWaitWrapper(config, sink) from "
                    "lup.sessions.quota."
                )
            ),
        ],
    ),
    Migration(
        subjects=["claude_session", "codex_session", "ConfiguredFactory"],
        reason=(
            "a rendered request is already the agent that opens its sessions, "
            "so no factory step follows rendering, and a profile selector "
            "applies its profile to that agent rather than building one"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Render with claude_config(request) from "
                    "lup.providers.claude.selection or codex_config(request) from "
                    "lup.providers.codex.selection, which return the Claude or "
                    "Codex that claude_session and codex_session wrapped; "
                    "runtime.session_factory(request) still renders for a "
                    "selected Runtime."
                )
            ),
            MigrationStep(
                instruction=(
                    "Construct ProfileSelector(resolver) without a build "
                    "function: selector.session_factory(base, name) returns base, "
                    "the agent, with the selected profile applied."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "ClaudeFork.open_fork",
            "CodexFork.open_fork",
            "ClaudeSessionOpener.create_state",
            "ClaudeSessionOpener.build_options",
        ],
        reason=(
            "a fork is its provider's opener opening a branch, at any turn the "
            "session took, and a Claude conversation's state builds its own "
            "options, so the helpers that did either half went"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Fork through fork(at) on ClaudeFork or CodexFork, which "
                    "session.fork(at=...) reaches: it opens the branch as a "
                    "ClaudeSession or CodexSession."
                )
            ),
            MigrationStep(
                instruction=(
                    "Construct ClaudeConversationState(opener, config, resume) "
                    "from lup.providers.claude.runtime where create_state(resume) "
                    "was called, and read its options() where build_options(...) "
                    "was."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "ClaudeSessionConfig",
            "ClaudeSessionConfig.model",
            "ClaudeSessionConfig.system_prompt",
            "ClaudeSessionConfig.coding_harness_preset",
            "ClaudeSessionConfig.native_tools",
            "ClaudeSessionConfig.allowed_tools",
            "ClaudeSessionConfig.disallowed_tools",
            "ClaudeSessionConfig.tool_servers",
            "ClaudeSessionConfig.permission_mode",
            "ClaudeSessionConfig.max_turns",
            "ClaudeSessionConfig.delta_streaming",
            "ClaudeSessionConfig.max_thinking_tokens",
            "ClaudeSessionConfig.effort",
            "ClaudeSessionConfig.cwd",
            "ClaudeSessionConfig.add_dirs",
            "ClaudeSessionConfig.plugin_dirs",
            "ClaudeSessionConfig.environment",
            "ClaudeSessionConfig.sandbox",
            "ClaudeSessionConfig.hooks",
            "ClaudeSessionConfig.submission_gate_resolver",
            "ClaudeSessionConfig.subagents",
            "ClaudeSessionConfig.max_buffer_size",
            "ClaudeSessionConfig.stderr_tail_lines",
            "ClaudeSessionConfig.setting_sources",
            "ClaudeSessionConfig.cli_path",
            "ClaudeSessionConfig.extra_args",
            "ClaudeSessionConfig.the_model_takes_its_effort",
            "ClaudeSessionConfig.model_id",
            "ClaudeSessionConfig.enforce_native_authority",
        ],
        reason=(
            "the declaration and the agent it opened were two objects saying "
            "one thing; Claude is both, so every field and validator of the "
            "config lives on it"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Construct Claude(...), from lup or lup.providers.claude, "
                    "wherever ClaudeSessionConfig(...) was built: every field "
                    "keeps its name and type, and model_id(), "
                    "the_model_takes_its_effort and enforce_native_authority move "
                    "with them."
                )
            ),
            MigrationStep(
                instruction=(
                    "Read an unset effort as the model's default rather than the "
                    "CLI's: xhigh where the model's catalog row takes it, the "
                    "row's highest rung below xhigh otherwise, and none for a "
                    "model taking no effort; resolved_effort() answers which rung "
                    "a session starts at. Pass effort=... where a session should "
                    "think at another."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "CodexSessionConfig",
            "CodexSessionConfig.model",
            "CodexSessionConfig.model_tiers",
            "CodexSessionConfig.developer_instructions",
            "CodexSessionConfig.cwd",
            "CodexSessionConfig.policy_root",
            "CodexSessionConfig.executable",
            "CodexSessionConfig.containment",
            "CodexSessionConfig.named_profile",
            "CodexSessionConfig.model_provider",
            "CodexSessionConfig.provider_config",
            "CodexSessionConfig.sandbox",
            "CodexSessionConfig.approval_policy",
            "CodexSessionConfig.hooks",
            "CodexSessionConfig.effort",
            "CodexSessionConfig.paired_effort",
            "CodexSessionConfig.environment",
            "CodexSessionConfig.submission_gate_resolver",
            "CodexSessionConfig.correction",
            "CodexSessionConfig.continuation",
            "CodexSessionConfig.mcp_servers",
            "CodexSessionConfig.writable_roots",
            "CodexSessionConfig.delegated_tools",
            "CodexSessionConfig.native_tools",
            "CodexSessionConfig.application_tools",
            "CodexSessionConfig.companions",
            "CodexSessionConfig.reject_unanswerable_approvals",
            "CodexSessionConfig.validated_for_app_server",
            "CodexSessionConfig.model_selection",
            "CodexSessionConfig.model_id",
            "CodexSessionConfig.the_model_takes_its_effort",
            "CodexSessionConfig.native_capabilities",
        ],
        reason=(
            "the declaration and the agent it opened were two objects saying "
            "one thing; Codex is both, so every field and validator of the "
            "config lives on it, its standing instructions under the name "
            "Claude gives them"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Construct Codex(...), from lup or lup.providers.codex, "
                    "wherever CodexSessionConfig(...) was built: "
                    "developer_instructions=... is system_prompt=..., "
                    "paired_effort is gone, and every other field, validator and "
                    "method keeps its name and type."
                )
            ),
            MigrationStep(
                instruction=(
                    "Pass effort=... where paired_effort=... was passed: an unset "
                    "effort is the model's default, xhigh where its catalog row "
                    "takes it and the row's highest rung below xhigh otherwise, "
                    "sent beside a named model and alone over an inherited one, "
                    "so no session inherits its home's effort any more; "
                    "resolved_effort() answers which rung is sent."
                )
            ),
        ],
    ),
    Migration(
        subjects=["GitIdentity.configuration"],
        reason=(
            "a contained commit's author travels as GIT_AUTHOR_* and "
            "GIT_COMMITTER_* variables, which git ranks above every "
            "configuration source, rather than as user.* keys in "
            "GIT_CONFIG_COUNT that a -c or an include could override"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Call GitIdentity.environment() from lup.harness.credential "
                    "and merge the variables it returns into the container "
                    "environment, as GitAccess.environment does; nothing reads "
                    "user.name or user.email from the settings any more."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "Claude.coding_harness_preset",
            "Claude.native_tools",
            "Claude.tool_servers",
            "Claude.servers",
            "Claude.enforce_native_authority",
            "TOOLS_SERVER",
            "claude_native_tools",
            "Codex.native_tools",
            "Codex.mcp_servers",
            "Codex.application_tools",
            "Codex.companions",
            "Codex.applications",
            "Codex.native_capabilities",
            "SessionRequest.native_tools",
            "SessionRequest.tool_servers",
            "NativeToolGroup",
            "NativeToolGroup.READ",
            "NativeToolGroup.WEB",
            "NativeToolGroup.WRITE",
            "NativeToolGroup.SHELL",
            "NativeToolGroup.ALL",
            "NativeTools",
            "native_grants",
        ],
        reason=(
            "what a session may call was four fields per provider — a roster of "
            "built-ins, a list of hosted tools, a map of servers, and a preset "
            "flag for the prompt that goes with the built-ins — so one agent "
            "declared its tools in a different place for each provider; one "
            "typed tools field holds all of it, and its default is the web "
            "alone rather than every built-in"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Declare tools=ClaudeTools(builtin=..., mcp=[...]) on Claude "
                    "and tools=CodexTools(builtin=..., mcp=[...]) on Codex, both "
                    "from their provider module, or "
                    "tools=SessionTools(builtin=..., mcp=[...]) on a "
                    "SessionRequest. builtin is 'stock' (every built-in; on "
                    "Claude also Claude Code's coding system prompt, which is "
                    "what coding_harness_preset=True sent), 'web' (fetch and "
                    "search: the default), 'none', or an exact list of "
                    "ClaudeBuiltinTool / CodexBuiltinTool names."
                )
            ),
            MigrationStep(
                instruction=(
                    "Spell a NativeToolGroup as its names: on Claude READ is "
                    "Read, Glob, Grep, WebFetch, WebSearch; WEB is 'web'; WRITE "
                    "is Write, Edit, NotebookEdit; SHELL is Bash, TaskOutput, "
                    "TaskStop; ALL is 'stock'. On Codex SHELL is Bash, WEB is "
                    "WebSearch, WRITE is apply_patch, ALL is 'stock'. A session "
                    "that declared no grant had no built-in (on Claude with "
                    "Claude Code's system prompt) and now has fetch and search: "
                    "say builtin='none' to keep none."
                )
            ),
            MigrationStep(
                instruction=(
                    "Move servers into mcp=[...] as objects from lup.mcp: "
                    "tools=[...] of @lup_tool handlers is Toolset([...], "
                    "name=...), a tool_servers or mcp_servers entry is "
                    "External(name=..., server=<its stdio, sse or http "
                    "transport>), and lup's own groups are Coordination(), "
                    "Ledger(...), CodeIntel() and Sandbox(). application_tools "
                    "and companions are what a hosted server compiles to, and "
                    "Codex.builtins() answers what native_capabilities() did."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "serve_toolset",
            "SessionToolset.served",
            "SessionToolset.beside",
        ],
        reason=(
            "a server is served by its own declaration, one per process, so "
            "the loop that served a group or a default set of them by name "
            "has nothing left to choose"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Declare each group as a server from lup.mcp — a project's "
                    "own as Group(builder) — and start it with "
                    "ServeLaunch(...).command(server), which runs "
                    "python -m lup.mcp.serve (or a composed CLI's `tools "
                    "serve`) for that one server; what a project adds to the "
                    "session goes in a needs hook named by ServeLaunch(needs=...)."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "CodexNativeTools",
            "CodexNativeTools.shell",
            "CodexNativeTools.web",
            "CodexNativeTools.write",
            "CodexNativeTools.images",
            "CodexNativeTools.all_tools",
            "CodexNativeTools.compile",
            "CodexNativeTools.configuration",
            "CodexNativeTools.arguments",
            "CodexNativeTools.model_catalog",
        ],
        reason=(
            "the controls holding a Codex session to its built-in grant are "
            "named for what they control rather than for where the tools come "
            "from"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Import CodexBuiltins from lup.providers.codex.builtins where "
                    "CodexNativeTools came from lup.providers.codex.native_tools: "
                    "every field and method keeps its name, and compile takes a "
                    "builtin preset or a list of CodexBuiltinTool names."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "PROFILE_ROOT",
            "local_profile_directory",
            "ACTIVE_FILE",
            "REGISTRY_PATH",
            "Account",
            "Account.config_dir",
            "Registry.profiles",
            "Registry.active",
            "AccountFile",
            "AccountFile.__init__",
            "AccountFile.homes_root",
            "AccountFile.load_registry",
            "AccountFile.save_registry",
            "AccountFile.resolver_registry",
            "AccountFile.resolve_config_dir",
            "ClaudeProfileNames",
            "ClaudeProfileNames.__init__",
            "ClaudeProfileNames.names",
            "ClaudeProfileNames.config_dir_for",
            "ClaudeProfileNames.active_profile",
            "ClaudeProfileRegistrar.__init__",
            "ClaudeProfileRegistrar.add_profile",
            "ClaudeProfileRegistrar.set_active",
            "ClaudeProfileRegistrar.remove_profile",
        ],
        reason=(
            "an account lived either in one checkout's .lup/profiles or in the "
            "personal registry at ~/.lup/profiles.json, which held Claude homes "
            "only, so a new repository opened its accounts signed out; global "
            "profiles now live once per person, a directory per name beside the "
            "per-user config at $XDG_CONFIG_HOME/lup (~/.config/lup), a home per "
            "runtime inside each, selected by that config.toml's profile — and "
            "a checkout's own .lup/profiles still comes first, its .active "
            "overriding that selection"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Move the old registry's accounts, which nothing reads any "
                    "more: this moves the homes it made, links a home registered "
                    "elsewhere, and carries its selection where the config file "
                    "records none. A checkout's .lup/profiles keeps working where "
                    "it is, and the same command moves it too, only if its "
                    "accounts should be shared by every checkout."
                ),
                command=["uv", "run", "lup-devtools", "harness", "profile", "migrate"],
            ),
            MigrationStep(
                instruction=(
                    "Build profile directories with profile_directory(login) from "
                    "lup.providers.profile_tree wherever "
                    "local_profile_directory(root, login) or a ProfileDirectory "
                    "over ClaudeProfileNames and ClaudeProfileRegistrar was built: "
                    "it resolves a name through the checkout's .lup/profiles, "
                    "then the global profiles, and takes checkout= for a root "
                    "other than the project's. A ProfileDirectory is built over a "
                    "list of ProfileRegistry, narrowest scope first, and "
                    "ProfileFolders takes its root, the login's home_subdir and "
                    "a ProfileSelection: ActiveFile for a checkout, "
                    "ConfigSelection for the config file's profile."
                )
            ),
            MigrationStep(
                instruction=(
                    "Pass an application's own origin as claude_usage_entry(profiles) "
                    "or codex_usage_entry(executable, profiles); codex_usage_entry "
                    "takes no home any more, and `usage codex --profile NAME` reads "
                    "that account's Codex home rather than refusing the name."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "user_profile_directory",
            "ProfileFolders.active",
            "ProfileFolders.select",
            "legacy_notice",
            "legacy_sources",
            "announce_legacy_profiles",
        ],
        reason=(
            "a checkout's .lup/profiles went unread once profiles moved beside "
            "the per-user config, so a project keeping local profiles lost them; "
            "a name resolves through the checkout's profiles again, then the "
            "global ones, and a launch no longer says to move them"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Call profile_directory(login, config, checkout) from "
                    "lup.providers.profile_tree where user_profile_directory was "
                    "called; it answers local profiles first, and curates them "
                    "unless scope='global' is passed to add, use or remove."
                )
            ),
            MigrationStep(
                instruction=(
                    "Read and record a root's selection through "
                    "ProfileFolders.selection, whose recorded() and record(name) "
                    "replace ProfileFolders.active() and select(name)."
                )
            ),
            MigrationStep(
                instruction=(
                    "Drop calls to legacy_notice, legacy_sources and "
                    "announce_legacy_profiles: a checkout's profiles are read "
                    "where they are, so nothing is left to announce."
                )
            ),
        ],
    ),
    Migration(
        subjects=["default_config_home"],
        reason=(
            "three places computed Claude's default configuration home each on "
            "their own; the login declaration is the one that says it"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Read CLAUDE_LOGIN.ambient_home, from "
                    "lup.providers.claude.login, wherever default_config_home() "
                    "was called: the same directory, joined onto this process's "
                    "home rather than a HOME a session carries."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "Codex.named_profile",
            "Codex.validated_for_app_server",
            "CodexProfileSelection.named_profile",
        ],
        reason=(
            "a Codex profile named a configuration overlay inside one home, "
            "which the app-server cannot select, so the field only ever "
            "refused; profile= now names an account on Claude and Codex alike"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Pass profile=NAME to Claude(...) or Codex(...) to open its "
                    "sessions as that account: a name among the person's lup "
                    "profiles, resolved to that runtime's home inside it, and "
                    "refused listing the known names where there is none. Drop "
                    "named_profile=..., which a session could never open with, "
                    "and every call to validated_for_app_server(), which checked "
                    "nothing else."
                )
            ),
        ],
    ),
    Migration(
        subjects=["DEFAULT_ACCOUNT_HOME"],
        reason=(
            "a worktree's Codex home was seeded from ~/.codex once and kept "
            "that copy, so every setting changed since, and every one changed "
            "in another checkout's session, never reached it; it is derived "
            "from the selected account at each launch and a session's changes "
            "return to that account, whose default the login declaration names"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Read CODEX_LOGIN.ambient_home, from lup.providers.codex.login, "
                    "wherever DEFAULT_ACCOUNT_HOME was read, and construct "
                    "CodexWorktreeHomeStore(account_home=...) with a profile's "
                    "Codex home where a run is started as one. Call "
                    "return_settings(worktree) beside publish(worktree) once a "
                    "session over a derived home closes."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "NativeHomeScope",
            "NativeHomeScope.key",
            "NativeHomeScope.volume_name",
            "CodexProfileSettings.state_scope",
            "state_volume_name",
            "superseded_volume_name",
            "superseded_volume_notice",
            "existing_volumes",
            "ProviderLogin",
        ],
        reason=(
            "a contained session's config home was one volume both runtimes "
            "wrote into, plus one per Codex settings digest; it is one volume "
            "per repository and runtime, named by the runtime's login "
            "declaration, with settings seeded at every launch rather than "
            "partitioned by volume, and the old volumes split once"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Give every ProviderLogin a state_volume word (and, to have "
                    "an old shared volume split, the home_entries it keeps). "
                    "Call state_volume_name(root, login) in place of "
                    "state_volume_name(root, scope), drop state_scope= from "
                    "contained_argv and session_argv, and read existing_volumes "
                    "from lup.devtools.harness.config_volume. The superseded "
                    "per-worktree notice is gone: split_config_volumes copies "
                    "those volumes and records them as superseded, and "
                    "sweep_superseded removes them once the person's days have "
                    "passed."
                )
            ),
        ],
    ),
    Migration(
        subjects=["environment_directory"],
        reason=(
            "a contained session's project environments outlived their "
            "worktrees with nothing saying whose they were; each is claimed "
            "when made and swept once its checkout is gone, by a module of "
            "their own"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Import environment_directory from "
                    "lup.devtools.harness.environments rather than "
                    "lup.devtools.harness.contained."
                )
            ),
        ],
    ),
    Migration(
        subjects=["return_settings", "SettingChange", "changed_settings"],
        reason=(
            "a Codex session's every changed setting went back to the "
            "account, a hook or its model pick included; only preferences "
            "return now, a portable one to the person's lup config, and "
            "the decision lives in lup.providers.codex.preferences"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Call return_settings(worktree, UserConfigFile(), current=...) "
                    "and read what moved from the CodexSettingsReturn it answers "
                    "(.carried(), .withheld, .session) rather than a list of names; "
                    "import SettingChange and changed_settings from "
                    "lup.providers.codex.preferences."
                )
            ),
        ],
    ),
    Migration(
        subjects=["runtime_path_rule"],
        reason=(
            "a declared protected root compiles through one function both "
            "enforcement paths read, lup.policy.rules.protected_root_rule, "
            "rather than a second copy in the bundle a test held equal to the "
            "first; no protected root releases an autonomous identity any more"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Call path_rule_row(protected_root_rule(root)) from "
                    "lup.policy.rules where runtime_path_rule(root) was called; "
                    "runtime_path_rules keeps its signature."
                )
            ),
        ],
    ),
    Migration(
        subjects=["RepositoryPeers.naming_settled"],
        reason=(
            "a native subagent's row is named under the same lock a session's "
            "join takes, from the shipped store a hook writes it through, so "
            "the lock moved to the store both halves stand on"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Hold `with naming_settled(peers.root):` from "
                    "lup.coordination.bare.store where `with "
                    "peers.naming_settled():` was held."
                )
            ),
        ],
    ),
    Migration(
        subjects=["HookSandbox.credential_paths", "ExecutionBoundary.credential_paths"],
        reason=(
            "the file tools and the OS sandbox are refused every path "
            "HookSet.refused_paths withholds from a command, compiled from that "
            "one declaration, where a second list named two of those paths and "
            "left the rest readable to Read, Grep and Glob"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Delete credential_paths from the HookSandbox declaration and "
                    "declare any path it named that the library's "
                    "credential_files() does not already withhold in "
                    "HookSet.refused_paths instead, through credential_files(also="
                    "[...]) or a RefusedPaths of its own; the settings render the "
                    "Read deny rules from there."
                )
            ),
        ],
    ),
    Migration(
        subjects=["NativeCapabilityEvidence"],
        reason=(
            "each runtime's composer moved into its adapter "
            "(lup.providers.claude.composition, lup.providers.codex.composition) "
            "and types its own readiness evidence, so the union naming both "
            "runtimes' evidence had nothing left to name"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Type a readiness probe's evidence as the runtime's own "
                    "CapabilityEvidence[ClaudeCliEvidence] or "
                    "CapabilityEvidence[CodexCliEvidence], or as the neutral "
                    "CapabilityReport where either will do."
                )
            ),
        ],
    ),
    Migration(
        subjects=[
            "ClaudeSandboxConfig",
            "ClaudeSandboxConfig.enabled",
            "ClaudeSandboxConfig.auto_allow_bash_if_sandboxed",
            "ClaudeSandboxConfig.allow_unsandboxed_commands",
            "ClaudeSandboxConfig.excluded_commands",
            "ClaudeSandboxConfig.posture",
            "SessionContainment",
            "CLAUDE_CONTAINMENT",
            "CODEX_CONTAINMENT",
            "codex_sandbox",
            "Codex.containment",
            "SessionRequest.containment",
        ],
        reason=(
            "one declaration now says which wall a session opens behind for "
            "both of its compilations — a session opened in process and one "
            "launched at a terminal — so the wall is one typed field on Claude, "
            "Codex and SessionRequest alike, OuterContainer, InnerSandbox or "
            "NoSandbox, rather than a Claude-only settings object, a Codex "
            "containment word and a request's third spelling of the same thing"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Declare sandbox=InnerSandbox() where ClaudeSandboxConfig() "
                    "was, from lup.launch.declaration: allow_unsandboxed_commands "
                    "is InnerSandbox(escapable=...), excluded_commands keeps its "
                    "name, and enabled=False inside a container is "
                    "sandbox=OuterContainer(). ClaudeSandboxConfig(...).posture() "
                    "is the wall's own .enforcement()."
                )
            ),
            MigrationStep(
                instruction=(
                    "Replace containment='outer'|'inner'|'none' on Codex and "
                    "SessionRequest with sandbox=OuterContainer()|InnerSandbox()|"
                    "NoSandbox(); a session opened in process inside the container "
                    "still names the program entering it, as executable= or "
                    "contained_program=. Codex's own mode word moves to "
                    "sandbox_mode=, which the wall narrows; codex_sandbox(request) "
                    "is codex_sandbox_mode(config.sandbox, config.sandbox_mode) in "
                    "lup.providers.codex.launch, beside CODEX_SANDBOX_WIDTH."
                )
            ),
        ],
    ),
    Migration(
        subjects=["Codex.sandbox"],
        reason=(
            "Codex's sandbox field is the declaration's wall now, the same "
            "type Claude's is, so Codex's own mode word moved to the name its "
            "config.toml gives it"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Rename Codex(sandbox='read-only'|'workspace-write'|"
                    "'danger-full-access') to Codex(sandbox_mode=...)."
                )
            ),
        ],
    ),
    Migration(
        subjects=["AGENTS", "DEFERRED"],
        reason=(
            "the package root resolves the launch vocabulary on first access "
            "the way it resolves the agents, through one table naming the "
            "module each lazy export is defined in, so the table is named "
            "for what it holds"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Read lup.LAZY_EXPORTS where lup.AGENTS was read. It maps every "
                    "name the root resolves on first access to its module; a "
                    "caller that wanted the agents alone keeps the rows for "
                    "Claude and Codex."
                )
            ),
        ],
    ),
    Migration(
        subjects=["ModuleRow.guidance_used"],
        reason=(
            "`dev modules` weighs each module as this project's selection "
            "resolves it, so its one prose column split in two: what the "
            "project ships of the module, and what the module itself declares"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Read ModuleRow.guidance_resolved where guidance_used was "
                    "read, for the bytes this project's version of the module "
                    "holds; ModuleRow.guidance_declared is the module's own "
                    "sections, before any were retired or rewritten."
                )
            ),
        ],
    ),
    Migration(
        subjects=["DISTRIBUTION"],
        reason=(
            "PyPI refuses `lup` as too close to an existing project, so the "
            "library's distribution is `lup-agents`; `import lup`, "
            "`packages/lup/`, `lup-devtools` and the `lup` sync registration "
            "keep their names, and DISTRIBUTION, which also stood for that "
            "registration, spells only the distribution"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "In pyproject.toml, rename the requirement lup[...] to "
                    "lup-agents[...] in [project].dependencies and the `lup` key "
                    "under [tool.uv.sources] to `lup-agents`, whether it pins a "
                    "repository or the vendored copy under packages/lup. No "
                    "command does this: the lup-devtools a project runs is the "
                    "library being replaced, and uv refuses to move it -- a "
                    "repository pin with 'Package metadata name `lup-agents` "
                    "does not match given name `lup`', a vendored copy with "
                    "'`lup` references a workspace in `tool.uv.sources` (e.g., "
                    "`lup = { workspace = true }`), but is not a workspace "
                    "member'. Then install the renamed library:"
                ),
                command=["uv", "sync"],
            ),
            MigrationStep(
                instruction=(
                    "A project pinned at a repository then brings the generated "
                    "trees and the copied half to the commit it now resolves:"
                ),
                command=["uv", "run", "lup-devtools", "dev", "update"],
            ),
            MigrationStep(
                instruction=(
                    "A vendored copy's editable install leaves its old metadata "
                    "beside the new, and importlib.metadata goes on answering "
                    "for `lup` until it is deleted:"
                ),
                command=["rm", "-r", "packages/lup/src/lup.egg-info"],
            ),
            MigrationStep(
                instruction=(
                    "A caller that passed DISTRIBUTION from "
                    "lup.devtools.dev.library as the sync registration's name "
                    "passes REGISTRATION instead; DISTRIBUTION is the "
                    "requirement, source key, lock entry and index name."
                )
            ),
        ],
    ),
    Migration(
        subjects=["changed_python_files"],
        reason=(
            "`dev check --changed` names every changed file a scoped check does "
            "not read, where it used to drop them, so the scope it reads is one "
            "answer holding both halves"
        ),
        steps=[
            MigrationStep(
                instruction=(
                    "Call changed_scope(since).checked from lup.devtools.dev.check "
                    "where changed_python_files(since) was called; "
                    "changed_scope(since).unread holds the other changed files."
                )
            ),
        ],
    ),
]
"""Every break this library has taken since its last release, and what to do.

Empty is the state to keep it in: an entry is added by the commit that breaks
something, and every entry leaves at the next release, rendered into that
release's changelog section by `version bump`. A list that grows is a release
overdue rather than a record to reorganise.
"""


def unapplied(
    declared: list[Migration], revision: str, root: Path = Path()
) -> list[Migration]:
    """The declared migrations a project standing at ``revision`` still owes."""
    return [
        migration for migration in declared if not migration.applied_at(revision, root)
    ]


def unnamed(
    disappeared: list[Capability], declared: list[Migration]
) -> list[Capability]:
    """Every capability that went with no migration speaking for it.

    The gate's whole question. A capability that moved is not here — the map
    is derived and needs nobody to write it down — and one that went on
    purpose is not here either, as long as the commit that took it said so.
    """
    return [
        capability
        for capability in disappeared
        if not any(migration.covers(capability) for migration in declared)
    ]


def last_release_commit() -> str:
    """The newest release commit this checkout can reach, or empty where none can.

    A release moves the declared breaks out of ``DECLARED`` and into the
    changelog, so what is owed afterwards is what the checkout has taken
    *since that commit*. Read from the release branch instead, the answer
    includes everything the release just shipped — against a list the release
    emptied — and every break comes back undeclared.

    The commit rather than the tag, though the tag is the more canonical mark
    of a release: the tag is pushed last on purpose, so a branch reaches CI
    and a reviewer carrying the release commit and no tag at all, and a gate
    reading the tag is blind for exactly the window a release is under review.
    The subject is :data:`RELEASE_SUBJECT_PREFIX`, which `dev release` writes
    and this reads.
    """
    return git.out(
        "log",
        f"--grep=^{RELEASE_SUBJECT_PREFIX}",
        "--extended-regexp",
        "--format=%H",
        "--max-count=1",
        "HEAD",
        _ok_code=[0, 1, 128],
    ).strip()


def gate_base(integration: str, release: str = "main") -> str | None:
    """The commit this checkout's breaks are judged from, or ``None`` with none to read.

    A feature branch is judged from where it started, which creation recorded
    and topology can otherwise guess among the local branches.

    The integration branch is judged from its last release commit: a release
    moves the declared breaks out of ``DECLARED`` and into the changelog, so
    what is owed afterwards is what the checkout has taken since then. Read
    from the release branch instead, the answer covers everything the release
    just shipped, against a list that release emptied — every break undeclared,
    for as long as the release takes to land.

    The branch answers where no release commit does: a repository before its
    first release, or one whose history a shallow clone truncated. Where no
    local branch stands beside the current one either -- a CI clone holds the
    branch it checks out and nothing else, and a pull request's checkout stands
    on no branch at all -- the remote's copy is read, which a full fetch
    carries.

    No base at all is a reading, not a refusal. The base detector exits the
    process where it finds no other local branch, and for every push after
    this gate was written that exit took the whole report with it: the log
    held one line and an exit code, and named no check.
    """
    current = git.out("branch", "--show-current").strip()
    siblings = [
        branch
        for branch in git.lines("branch", "--format=%(refname:short)")
        if branch != current
    ]
    if current and current != integration and siblings:
        return detect_base_branch(current).merge_base
    # Read off HEAD's history rather than off which branch is checked out,
    # because a pull request's checkout stands on no branch at all: gated on
    # standing *on* the integration branch, this answered for the push build
    # and not for the request build beside it, and one of the two reported
    # every break the release had shipped.
    if cut := last_release_commit():
        return cut
    named = release if current == integration else integration
    for ref in (named, f"origin/{named}"):
        found = git.out("merge-base", ref, "HEAD", _ok_code=[0, 1, 128]).strip()
        if found:
            return found
    return None


def undeclared_breaks(
    project: DevProject, base: str, declared: list[Migration] = DECLARED
) -> list[Capability]:
    """Every capability this checkout took since ``base`` that nothing speaks for.

    ``base`` is what :func:`gate_base` answered: what this change took away is
    judged against where it started, and creation recorded that where
    topology can no longer recover it.
    """
    divergence = compare(surface_at(base, project), surface_now(project))
    return unnamed(divergence.disappeared, declared)


def rendered(declared: list[Migration]) -> list[str]:
    """What a release's changelog section says about the breaks in it.

    The prose half only: a reason and the steps a reader has to act on. The
    derived half is left out on purpose — a relocation map is re-derivable
    from any two revisions, and a hundred pairs pasted into a changelog go
    stale the next time one module moves.
    """
    return [line for migration in declared for line in migration.spelled()]
