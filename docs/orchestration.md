<!-- Generated from lup.harness.content.docs.orchestration by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Agent Orchestration Patterns

How work is delegated across agents in this project — what runs where, and who sees it. The recurring *code* shapes live in [docs/patterns.md](patterns.md); daily development guidance is in the agent guidance document your runtime loads.

**Model selection:** every pattern below — subagents, reviewers, nested and background agents — defaults to the **strongest** tier. Reach up to `frontier` for the newest model a runtime ships, and drop to `balanced` or `fast` only with an explicit, justified reason (see § Model Selection in the agent guidance). A declaration states the tier and each runtime spells whichever model it can honor, so naming a model id here would pin one provider's lineup into a library that is provider-neutral, and pin it to a lineup that moves.

**Vocabulary:** two kinds of delegated agents look alike and must not be conflated:

- A **native subagent** ("subagent" for short) is dispatched by the harness itself: Claude Code's `Agent`/`Task` tool hands a focused task to a named role defined upfront, inside the main agent's session — shared trace, shared metrics. See [Subagent Pattern](#subagent-pattern).
- A **nested agent** (also called a *tool-subagent*) runs inside a tool call: the handler asks an explicitly declared agent of its own for one turn — `await agent.ask(prompt, Model)`, an independent session — and folds the result into the tool's response. The harness never sees it — to the calling agent it is just a tool. See [Nested Agent Pattern](#nested-agent-pattern).

Guidance that says "subagent" unqualified means the native kind; an agent living inside a tool handler is always a nested agent.

---

## Persistent Agent Pattern

For agents that exist over time — maintaining conversations, monitoring systems, playing games — the architecture inverts: the agent is a **persistent presence** that controls its own attention, not a processor steered by an event queue.

| Do This                                                     | Not This                                    |
| ----------------------------------------------------------- | ------------------------------------------- |
| Agent sleeps when it chooses, wakes on events               | Event queue drives agent responses          |
| All timing is tools (sleep, debounce, remind, schedule)     | Hardcode delays or polling in orchestration |
| Stop hook prevents turn from ending — only sleep yields     | Request-response per event                  |
| Pull-based state reading (agent calls `context` when ready) | Push state changes as SDK user turns        |
| Agent parks thoughts (ideas, reminders) for later           | Drop context between interactions           |
| Expose environment state as tool-readable data              | Hide activity from the agent                |

**The core loop:** The agent never ends its turn. A Stop hook blocks it. Instead it cycles: wake → read context → think → act → meta-assess → sleep. The only way to yield control is `sleep()`, which blocks on an asyncio Event until something wakes it.

**Why not an event queue?** The sleep/wake pattern lets the agent stay centered — it can debounce event bursts, schedule actions, set reminders, and park thoughts for later, all on its own terms.

**Two wirings, one pattern:** On Claude, tools run in-process — `sleep` blocks on the Scheduler directly and a Stop hook keeps the single turn open forever. On backends whose tools run in a subprocess (Codex, OpenAI-compatible), the loop inverts: **each wake is one SDK turn**. The served tools (`lup.orchestration.realtime.relay`) relay through files in `session_dir/realtime/`: `reply` and the timing tools append events a parent-side watcher applies mid-turn, `sleep` records a request and the agent ends its turn, and the parent loop (`run_relay_session`) consumes the request, sleeps on the Scheduler, and opens the next turn with a wake message. An agent that ends a turn without sleeping gets bounded corrective turns — the relay counterpart of the Stop hook. Enforcement is in-handler on both wirings (meta-before-sleep, unread-events guard), so no hooks are required.

**Library support:** `lup.orchestration.realtime.scheduler` provides the `Scheduler` class and hook factories (`create_stop_guard`, `create_pending_event_guard`); `lup.orchestration.realtime.relay` provides the subprocess wiring (`RealtimeMailbox`, `create_realtime_relay_tools`, `run_relay_session`), sharing the tool I/O models in `lup.orchestration.realtime.models`. Construction goes through the engine seam: `run_persistent_agent` in `agent/core.py` serves the `session` tool group into the subprocess engine, builds the parent-side session mailbox itself, and drives it via `run_relay_session` — the engine never touches the relay. See example tools in `src/lup_template/agent/tools/realtime.py`.

---

## Reflection Pattern

Agents produce better output when forced to self-assess before committing. Three components:

1. **Reflection tool** (`agent/tools/reflect.py`): Domain-customizable self-assessment — confidence, uncertainties, tool audit, process reflection. Runs a nested reviewer agent that returns a structured `ReviewResult` verdict. An explicit skip records approval; a startup error, timeout, or missing verdict returns a recoverable tool error without opening the gate. The template reviewer has read and web-research capabilities and a configurable wall-clock bound enforced across engines.
2. **Review gate** (`lup.orchestration.reflection`): `ReviewGate`, a verdict-aware `ReflectionGate` — in-memory, or file-backed (fail counter included) when tools run in a subprocess. Approve and warn open the gate; fail keeps it closed so the agent revises and re-reviews; after 3 consecutive fails it opens anyway (escape hatch). Enforced primarily *inside* the `submit_output` handler (`lup.sessions.output`), which rejects submission with a retriable error until the gate opens; `create_reflection_gate()` adds a PreToolUse hook as hardening where the backend supports it. The plain `ReflectionGate` base remains for act-of-reflecting gates (the realtime `sleep` meta-gate).
3. **Wiring**: The gate rides inside submission — `reflection_submission_gate` (`agent/core.py`) adapts the `ReviewGate` to the `SubmissionGate` carried by the turn's `TurnToolBinding`, so a gated submission is rejected with a retriable message until the reviewer passes (persistent agents gate `sleep` instead). Final output always flows through the turn-bound submission tool — registered by the adapter on every SDK backend — whose `submit_output` handler validates against the turn's output model and persists through the bound `SubmittedOutputStore` (in-memory, or file-backed when tools run in a subprocess). Completion is enforced by the logical turn itself: `ResilientTurn` sends bounded corrective cycles (`CorrectionConfig`, `lup.sessions.middleware`) when a turn ends without a submission, and a turn that still produces none raises `StructuredOutputError` for the orchestration layer — the one-shot counterpart of the relay's missing-sleep message. Those cycles advance on the turn's own task rather than on whichever caller awaits it, so a resilient turn's event stream — one logical stream over every cycle, closing when the result settles — may be consumed before, during, or after the turn is awaited, and a caller that watches a turn as it happens is not thereby waiting on itself. `create_completion_guard` (`lup.policy.hooks`) remains as optional Stop-hook hardening on backends that expose stop hooks.

**Customizing:** The gate in `lup.orchestration.reflection` is domain-neutral and parametric. The reflection tool and `ReflectInput` in `agent/tools/reflect.py` are domain-specific — add fields for your domain. The reviewer prompt should target your domain's common failure modes.

**Skip reviewer:** Set `skip_reviewer=True` for speed-sensitive or trivial tasks. The reviewer adds latency but catches calibration errors and reasoning gaps.

**Tool gates (generalization):** The reflection gate is one instance of a general primitive: `create_tool_gate()` in `lup.policy.hooks` denies a tool (or Stop) with an agent-readable message until a condition unlocks it — "the agent must do A before it may do B" as a structural constraint instead of a prompt rule. Presets built on it: `create_reflection_gate` (reflect before finalizing output), `create_stop_guard` (sleep instead of ending the turn), `create_pending_event_guard` (read events before timing tools), and `create_meta_before_sleep_guard` (meta-assess before sleep). Reach for the primitive directly when your domain needs a new ordering constraint.

---

## Subagent Pattern

SDK-native delegation: the main agent dispatches a focused task to a named role defined upfront, sharing the session's trace and metrics. A **native subagent** extends the main agent's thinking — a specialized lobe with its own prompt, tool subset, and model — where a nested agent (below) isolates work in a separate context.

**Library support:** portable harness agents come from the typed harness catalog.
Application-time delegation uses `create_run_subagent_tool()` only with an
explicit `SubagentSpec -> Agent` recipe; it never infers a provider or
reconstructs a native client.

`SubagentSpec.capabilities` declares runtime facilities (`workspace-read`,
`web-search`); `tools` retains exact canonical grants such as `Read` or a
specific MCP tool. These are separate contracts: a provider may implement
workspace reads with sandboxed commands, but cannot turn an exact `Read`
grant into general shell access. Unsupported exact grants or limits fail
explicitly. Model choices use `inherit`, `frontier`, `strongest`, `balanced`,
or `fast`, defaulting to `strongest`; concrete model identifiers belong to
provider configuration, typed against each runtime's generated catalog.

The template's served roles follow the engine that owns their MCP server.
Native server commands carry that selection explicitly, independent of the
application's default engine. Read capabilities do not replace filesystem
permission policy: the native runtime determines which files are visible,
and the delegated role receives no write facility. Hosted web search is
separate from network access for sandboxed commands.

**Name every spawn after its work.** The name a spawn carries is what the
runtime lists, what a message and a stop address, and what the roster row
the subagent joins under is called, so the caller chooses it: the task in two
or three words, a letter or digit first, then letters, digits and
underscores. Where the hook set declares `spawn_names` — on by default — a
spawn given no name still goes out named, read out of its description, and
its caller is told once in that conversation to choose its own next time;
the tool schema one runtime shows lists no such argument, and the runtime
takes it regardless. [platform-differentiation.md](platform-differentiation.md)
records what each runtime does with the name and what was measured.

---

## Nested Agent Pattern

Distinct from **native subagents** (defined upfront and delegated by the
harness). A **nested agent** — a *tool-subagent* — is a tool that receives or
builds an explicitly declared agent of its own, asks it for one typed turn,
and folds the result into its response.

| Aspect     | Native Subagent                   | Nested Agent                        |
| ---------- | --------------------------------- | ----------------------------------- |
| Definition | Upfront in `get_subagent_specs()` | On-demand inside a tool handler     |
| Runtime    | Main agent's session               | Independent agent via `ask()`       |
| Session    | Shared — same trace, same metrics | Isolated — no session persistence   |
| Return     | SDK `ResultMessage` (structured)  | Scalar result augmented by the tool |
| Use case   | Specialized long-running work     | Quick generation, review, parsing   |

**The augmentation pattern:** The tool handler post-processes the nested agent's output before returning it. The nested agent produces raw material; the tool shapes it into the MCP response:

```python
from pydantic import BaseModel, Field

from lup import Claude
from lup.tools.mcp import lup_tool


class ReviewInput(BaseModel):
    code: str = Field(description="The code to review, verbatim")


class ReviewResult(BaseModel):
    assessment: str
    issues: list[str]


class ReviewOutput(BaseModel):
    critique: str


reviewer = Claude(model="strongest", system_prompt="You review code for quality.")


@lup_tool("Review code quality and return structured assessment")
async def review(params: ReviewInput) -> ReviewOutput:
    result = await reviewer.ask(f"Review this code:\n\n{params.code}", ReviewResult)
    return ReviewOutput(critique=result.output.assessment)
```

**Library support:** `agent.ask(prompt, OutputModel)` opens one session of a
declared `Claude` or `Codex` agent, takes one turn, closes the session however
the turn ended, and returns a strict `TurnResult[T]`; the prompt may be a
string or a `TurnInput`. Provider selection, tools, limits, and compatible
endpoints are validated when the application declares the agent; unsupported
settings are never silently dropped.

**Example:** `src/lup_template/agent/tools/nested.py` is the dedicated copyable template — a minimal `critique` tool (input model → `ask()` → augmented output, exported as `NESTED_TOOLS`, unwired by default). For organic usages, see the reviewer inside `agent/tools/reflect.py` (`run_reviewer`, called from the `review` tool) — an independent one-shot `ask()` whose critique the tool folds into its structured output — and the `extract` path of `fetch_example` in `agent/tools/example.py` (`extract_answer`), the same shape applied to data augmentation.

**Routing whole tool families:** there is a standing tension between the bitter-lesson instinct — give the agent every tool and let the model decide — and context economy: every schema wired into the main agent occupies its context, and a large enough surface starts deferring schemas on Claude harnesses (see [Deferred Tool Schemas](#deferred-tool-schemas-tool-search)). The settled middle ground is **one delegating tool per family**: instead of serving every data tool to the main agent, expose a single `research` tool whose handler runs a nested agent holding the whole data-gathering family (search, fetch, markets, news). The main context carries one schema and receives structured findings; the specialist schemas — and the reasoning that used them — stay in the nested agent's context. Accept a batch of questions in one call so the handler can fan them out in parallel, and persist findings when later tasks will reuse them. The aib downstream repo (`refs/aib` when linked) carries a full-scale reference: its `research` tool moves ~35 data tools off the main agent, batches questions, resumes prior research sessions for follow-ups, and persists findings to a worldview store.

**When to use each:** The axis is **context separation**. **Native subagents** extend the main agent's thinking — same session, shared context, like a specialized lobe that makes reasoning more efficient. **Nested agents** are for truly separable work — the two contexts shouldn't pollute each other. The main agent doesn't need the nested agent's reasoning chain, just its conclusion. The tool handler acts as a context boundary.

---

## Background Agent Pattern

For persistent agents that need parallel processing, a **background agent**
runs alongside the main agent in one session of an injected agent. Immutable
Pydantic state is supplied on each wake and rapid wakes are debounced.

| Aspect        | Native Subagent              | Nested Agent                | Background Agent                |
| ------------- | ---------------------------- | --------------------------- | ------------------------------- |
| Lifetime      | Per-task (SDK dispatch)      | Per-tool-call               | Session-long                    |
| Runtime       | Main agent's session         | Independent via `ask()`     | Independent injected agent     |
| Initiation    | Agent dispatches via `Task()`| Tool handler creates on-demand| Wake events trigger turns       |
| Communication | SDK `ResultMessage`          | Tool return value           | Shared mutable state            |
| Use case      | Specialized long-running work| Quick generation, review    | Observation, research, execution|

**The shared-state pattern:** Background agents don't inject messages into the main agent's stream. Their tools write to shared objects (lists, dicts) that the main agent's tools read. The main agent pulls data when ready — no interruptions.

**Common use cases:**
- **Observer**: Summarizes conversation history so the main agent has context when earlier messages scroll out of the context window
- **Researcher**: Fetches and processes external data (with `tools=ClaudeTools(builtin=["Read", "Glob", "Grep", "WebFetch", "WebSearch"])`) while the main agent continues interacting
- **Executor**: Runs long-running tool calls without blocking the main agent's turns

**Lifecycle:** `start()` spawns an asyncio task. `wake(state)` copies the latest
typed state. After debounce, a `state_to_turn` callback asks the session for
the turn that state calls for; typed result/error handlers receive the
outcome. `stop()` cancels the task and session context cleanup aborts an
unfinished native turn.

**Library support:** `lup.orchestration.background.BackgroundAgent` composes only an
`Agent`, `state_to_turn`, typed result/error callbacks, and
`BackgroundConfig`. See the observer example in
`src/lup_template/agent/tools/realtime.py`.

**Customizing:** The `state_to_turn` callback is the main extension point.
It receives the open `Conversation` and the latest immutable state, and returns
`conversation.ask(prompt, Model)` — naming the output type there keeps the
result typed, without knowing which provider the injected agent is.

---

## Actor Cohort Pattern

The three patterns above all end the same way: the delegated agent runs, and
whatever anyone learns while it runs has nowhere to go. A subagent's task is
fixed at dispatch, a nested agent is unreachable inside its tool call, and a
background agent takes state on a wake rather than a sentence mid-turn. When
several agents work at once and the facts move under them, that is the whole
problem — an agent verifying a statement you have since disproved, or working
a branch you closed, keeps going because nothing can tell it.

An **actor cohort** (`lup.coordination.cohort.ActorCohort`) is a population of
agents that stay in contact while they work. Each holds one session across
every turn it takes; anything addressed to one lands in front of its next tool
call through a hook it never chooses to check; and the person watching is
itself an address, `user`, so an agent can say something back.

| Aspect | Actor Cohort |
| --- | --- |
| Lifetime | As long as the population is held; each member across many turns |
| Runtime | One held session per member, from a recipe the cohort configures |
| Initiation | `ask` (awaited), `start` (detached), or `work_all` (a whole wave) |
| Communication | Mail both ways, mid-turn; questions through a `QuestionMailbox` |
| Use case | Several agents at once, over work whose facts move under them |

**`ask` versus `start` is the load-bearing distinction.** A caller blocked
inside an awaited call cannot make another, so a cohort whose members are all
`ask`ed has steering tools that can never fire. `start` returns immediately
and the caller keeps its turn — which is what makes saying anything possible
at all.

**Fan out with `work_all`, not with a gather of your own.** How many agents
run at once, which of them are running, and what a close reaches are three
facts about the population; a caller that assembles its own wave from
`start_work` and `asyncio.gather` gets the cap right and the other two wrong.
`work_all` runs one piece of work per address and hands back each answer
positionally — a result or the exception it raised, faithfully, so a caller
that classifies failures can still tell a park from a host fault from a
cancellation.

**A raise does not always finish an agent.** A raise usually settles the agent
it came out of, but work can stop because it was suspended — parked on a
question, drained at a boundary, stopped by a failing host — and every one of
those expects the same agent to carry on. `settles` is how a consumer says
which of its own failures suspend; recorded finished instead, the resume opens
a fresh conversation rather than reattaching to the one holding the context,
and every door reads a waiting agent as a stopped one.

It belongs to the cohort, passed once at construction, rather than to each
wave. A suspension is raised in both places a raise can happen — a drain
checked between rounds comes out of the work, a host fault out of the turn
itself — so a judgement held by the wave answers for one and not the other,
and the turn's own failure path finishes the agent before the wave is ever
consulted. Which failures suspend is a fact about the consumer's vocabulary,
and a consumer has one.

**The cohort owns the wiring.** Delivery works only if the mailbox hook is in
the options the session opened with, so callers pass an `ActorRecipe`
(`(ActorRef, LupHooksConfig) -> Agent`) and the cohort hands it the
hooks. A recipe that had to fetch them could be written once without them,
producing an agent that looks spawned and reads nothing anyone sends it.

**Addresses are supplied or minted.** `cohort.actor(kind, id)` with an id
derived from durable state is stable across a restart, which is what lets a
resumed run reattach to conversations rather than open new ones;
`cohort.actor(kind)` mints one for a spawn nobody declared. That is the only
difference between the two cases.

**The population is a record, not a dict.** `live()`, `members()` and
`reaching()` fold `roster.jsonl`, so a console in another process resolves the
same address the cohort's own tools do, and a restart rebuilds the roster.
`cohort.json` beside it says the directory is a cohort at all, which is what a
peer that did not create one reads to find it.

**The person is on the roster.** They join as the member `user`, with a mailbox
and no session, so a report reaches them through the verb that steers an agent
rather than through a channel of its own — and a console attaching to a
directory some other process wrote resolves that address by the same fold. It
is the one member `live()` leaves out, because nobody started them.

**Library support:** `lup.coordination.tools.create_cohort_tools` serves the verbs an
agent needs — list what I spawned, read what one of them has found so far, say
something to one of them, say something to the person watching. Reading is
what makes steering more than a guess: a spawn's turn events reach the journal
as they happen, so `spawn_read` folds its own words, its calls and its
refusals out of that record while it is still working, and a redirect can be
aimed at what the agent is doing rather than at what it was asked.
`lup.coordination.mailbox.QuestionMailbox`
adds decisions that park a run, on the same storage; messages ride a stream
and never park anything, which is why "a message stalled the run" is not
expressible rather than merely avoided.

---

## Containing a Session

Every pattern above opens a session with a program behind it, and by default
that program runs wherever the application does — a nested agent inside a
tool call, a background agent on a wake, each member of a cohort. The wall
around one is the agent's own `sandbox` field, one of the launcher's three
walls, and it is `NoSandbox()` unless a declaration says otherwise.

| Wall | What it opens | What the declaration carries |
| --- | --- | --- |
| `NoSandbox()` | The runtime as the application's own configuration leaves it | Nothing; the default |
| `InnerSandbox()` | The runtime's own sandbox, established where the session runs | Nothing else |
| `OuterContainer()` | A container, with the runtime's own sandbox stood down inside it | The program that enters it, as Claude's `cli_path` or Codex's `executable` |

**`InnerSandbox()` is one field on each runtime and means the same thing on
both.** Claude receives the SDK's sandbox settings; Codex receives a sandbox
mode. Because Codex says how much a session may do by saying how far it may
reach, a declaration naming both a wall and a `sandbox_mode` lands twice on
one field there, and the narrower of the two wins — no mode widens the wall,
and the wall does not widen a session that was only meant to read.

**`OuterContainer()` needs a program, because a container is not something a
declaration can conjure.** The image, the mount table, the credential and the
login are the application's, so it builds the wrapper with
`lup.launch.container.contained_cli` and names the result:

```python
from pathlib import Path

from lup import Claude, OuterContainer
from lup.launch.container import contained_cli
from lup.harness.image import Image
from lup.harness.requirements import Manifest
from lup.providers.claude.login import CLAUDE_LOGIN


def contained_agent(
    run_dir: Path, image: Image, manifest: Manifest, workspace: Path
) -> Claude:
    program = contained_cli(
        run_dir / "enter.sh", image, manifest, workspace, "claude", CLAUDE_LOGIN
    )
    return Claude(cwd=workspace, sandbox=OuterContainer(), cli_path=program)
```

Both runtimes take that path where they would have found their own CLI —
Claude as `cli_path`, Codex as `executable` — so neither adapter learns
anything about containers, and the session inside one is confined by mounts
rather than by a judgement about what a command was going to do. The lease
defaults to the tree the session was given; `lease_for` widens it to every
checkout of the repository, and `demoted` takes the writes away.

The resolver's actor cohort is this arrangement with an actor's lease
(`worker_cli`), which is why a cohort's members can work the same repository
at once without reaching each other's worktrees.

---

## Deferred Tool Schemas (Tool Search)

Claude harnesses (the CLI and the Agent SDK alike) stop loading every tool schema upfront once the combined schemas exceed a threshold — by default 10% of the model's context window (roughly 20k tokens at 200k). Beyond it, tools are **deferred**: the agent sees only names and must load a tool through the `ToolSearch` tool before calling it. This applies to built-in, MCP, and custom SDK tools.

**The failure mode:** an agent assumes a tool it "should" have does not exist — the schema is not in context, and a search with the wrong terms comes back empty (each search returns roughly the top five matches) — so it concludes the capability is missing and gives up without ever calling the tool.

**Configuration:** the `ENABLE_TOOL_SEARCH` environment variable controls deferral per session (`ClaudeAgentOptions(env=...)` in the SDK, shell environment for the CLI). Unset leaves the harness default (deferral on); `true` forces tool search on; `auto` defers past the default threshold; `auto:N` defers past N% of the context window; `false` loads every schema upfront with deferral disabled. `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` disables tool search entirely and cannot be overridden by `ENABLE_TOOL_SEARCH`. This template plumbs `AGENT_TOOL_SEARCH` (default `false`) into every Claude session it opens — the served tool surface is small and curated, so no schema should ever be invisible — and the repo's own dev harness pins `ENABLE_TOOL_SEARCH=false` in `.claude/settings.json` for the same reason.

**Prompt mitigations** when a large surface makes deferral worth keeping:

- Name the available tool *categories* in the system prompt ("you have tools for Slack, GitHub, and Jira — search for them") so the agent searches instead of concluding absence.
- Give tool families semantic name prefixes (`github_*`, `slack_*`) and write descriptions with the words a caller would actually use — search matches names and descriptions.
- Instruct the agent to search again with different terms before concluding a capability is missing.

**Native subagents** inherit the parent session's tool-search configuration — there is no per-subagent deferral override — and re-discover deferred tools themselves (search results are not shared with the parent). `AgentDefinition.tools` restricts which tools a subagent may use; it does not preload them.

**The structural fix** is to keep every agent's tool surface small enough that nothing defers: route whole tool families behind one delegating tool whose nested agent holds the family (see [Nested Agent Pattern](#nested-agent-pattern)) — each family's schemas then load next to the work that uses them.

Sources: [tool search (API)](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool.md), [tool search (Agent SDK)](https://code.claude.com/docs/en/agent-sdk/tool-search.md), [managing tool context](https://platform.claude.com/docs/en/agents-and-tools/tool-use/manage-tool-context.md), [SDK MCP](https://code.claude.com/docs/en/agent-sdk/mcp.md), [SDK subagents](https://code.claude.com/docs/en/agent-sdk/subagents.md).

---

## Data Augmentation Pattern

Tools that fetch external data should **enrich it inside the tool** before returning to the agent. The agent receives structured, domain-aware results — not raw HTML, API responses, or search snippets it has to interpret.

| Do This                                                | Not This                                              |
| ------------------------------------------------------ | ----------------------------------------------------- |
| Tool recognizes URL domain, calls structured API       | Tool returns raw HTML for agent to parse               |
| Search results include API data for known domains      | Agent fetches each search result separately            |
| Null fields filled from fallback sources inside client | Agent retries with different queries to fill gaps      |
| Domain routing dispatches to specialized handlers      | Agent decides which tool to call per URL               |
| Enrichment runs in parallel inside the tool            | Agent sequentially processes each result               |

**The principle:** Every layer of the fetch pipeline automatically upgrades raw external data to structured, domain-appropriate content before it reaches the agent. The agent never parses HTML, never matches URL patterns, never decides which API to call for a given domain.

**Three forms of augmentation:**

1. **Domain dispatch** — URL patterns route to specialized API handlers (e.g., a wiki URL → structured article text via the wiki's API, instead of scraping HTML). Hints redirect the agent to a better tool when no direct handler exists.
2. **Null-filling** — Multi-source fallback pipelines that recover missing fields from alternative endpoints or sibling records (e.g., primary API withholds fields → fallback endpoint fills the gaps).
3. **Extraction** — Nested agent calls that distill large text blocks into focused answers (see [Nested Agent Pattern](#nested-agent-pattern)).

**Example:** `src/lup_template/agent/tools/example.py` is the template for all three forms — `fetch_example` routes known hosts to a specialized handler (`fetch_wiki_article`, domain dispatch) and distills fetched pages through a nested `ask()` call (`extract_answer`, extraction); `search_example` recovers missing snippet fields from a fallback source (`fill_missing_snippets`, null-filling).

**Customizing:** Domain dispatch routes belong in `agent/tools/`. Build them lazily to avoid circular imports. Null-filling logic lives in API wrappers. Extraction uses `agent.ask(prompt)` on an auxiliary agent (see [Nested Agent Pattern](#nested-agent-pattern)).
