<!-- Generated from lup_template.harness.content.template_codex by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# AGENTS.md Template

This file exports portable sections from the upstream AGENTS.md as a scaffold for downstream projects. It contains conventions, workflow patterns, and coding standards that apply to any project using lup.

**How it's used:** `$lup:init` and `$lup:install` perform a **section-level merge** — they use the `<!-- section: ... -->` markers below to identify independent merge units, compare them against the target's existing AGENTS.md, add sections that are missing, and leave existing sections untouched. Placeholders like `<project>` are replaced with the actual project name.

---

<!-- section: AGENTS.md -->
# AGENTS.md

This file provides guidance to Codex — and any agent that reads `AGENTS.md` — when working with code in this repository.

**Note:** Modifying `AGENTS.md` means modifying the repository-root `AGENTS.md` (this file).

<!-- section: First Setup -->
## First Setup

**[IMPORTANT: Run `uv run lup-devtools sync mark-synced lup` to initialize upstream sync tracking, then delete this section.]**

<!-- section: Project Overview -->
## Project Overview

**[Describe your agent and what it does]**

Built with Python 3.14+ on the Claude Agent SDK, with the inner agent also runnable on the OpenAI Codex SDK (`AGENT_SDK=codex`) or any OpenAI-compatible endpoint (`AGENT_SDK=openai`) through the same adapter interface. Uses `uv` as the package manager.

The security envelope is capability-specific: Claude uses normalized SDK hooks
plus its sandbox and permission mode; Codex uses generated command hooks where
the installed CLI supports them plus its workspace sandbox. Unsupported
approval effects fail closed and are recorded as explicit capability gaps.

Every supported runtime must provide equivalent user-visible behavior, validation, diagnostics, tests, and documentation for each capability. Runtime-specific implementations are valid only when their semantic differences are explicit and evidence-backed.

### Naming Convention

- **Codex** = the meta-agent (the Codex CLI) that modifies the codebase, runs commands, and manages the development workflow
- **Lup** = the SDK agent inside the code being built and improved — the agent that runs via the CLI and produces outputs

"Lup" is the framework's name for the inner agent, not a project-specific term. Use "Codex" when referring to the outer development agent and "Lup" when referring to the inner SDK agent, regardless of the project's package name. Only the application package directory (`src/<project>/`) carries the project name; all framework vocabulary (`lup_tool`, `LupMcpTool`, `lup-devtools`, the `lup` library, the `lup` CLI entry point) stays as `lup`.

### Important Context

**[Add domain-specific context here. Examples:]**

- What outcomes matter and how they're measured
- What data sources are available
- What constraints or limitations exist

### The Bitter Lesson

The single most important principle for improving this agent: **give it more tools and capabilities, not more rules.**

| Do This                                               | Not This                                           |
| ----------------------------------------------------- | -------------------------------------------------- |
| Add tools that provide data                           | Add prompt rules that constrain behavior           |
| Apply general principles                              | Apply specific pattern patches                     |
| Communicate principles and the _why_                  | Prescribe rigid mechanical procedures              |
| Provide state/context via tools                       | Use f-string prompt engineering                    |
| Ask for the `strongest` tier and a high `effort`      | Compensate for weak reasoning with complex prompts |
| See what went wrong from first principles             | Make small edits to patch one mistake              |
| Create subagents for specialized work                 | Build complex pipelines in main agent              |

**Tools are the primary scaffold.** When the agent struggles, the answer is almost always a missing tool — not a missing prompt paragraph. A tool that returns the right data at the right time is worth more than any amount of prompt engineering.

**The test:** Does this change add a capability, or just a rule? Would it still help if the domain changed completely? If not, it's over-fitted.

### Tool Design Philosophy

Tools are the interface between the agent and its environment. They outlast any particular prompt revision, and they compose — each new tool multiplies the agent's options rather than constraining them.

**Prompts rot; tools don't.** Tool names and sets change as the agent evolves. If the prompt lists them, every addition or rename means updating two places that can drift apart. Letting the agent discover tools through their descriptions keeps the prompt focused on _what to do_ and _how to reason_ — things that stay stable.

**The tool description is the contract.** It's the only documentation the agent sees for a tool. When the agent misuses a tool or ignores one it should use, the description is usually the problem. A good description answers:

1. **What** — What does this tool do? (concrete behavior, not vague summary)
2. **When** — When should the agent reach for this tool? (triggers, conditions)
3. **Why** — Why does this tool exist? (what problem it solves, what gap it fills)

Compare: `"Search the web for information"` vs. `"Search the web using keyword queries. Use this when the agent needs current information not available in local data, or when verifying claims against external sources. Exists because the agent has no built-in knowledge of events after its training cutoff. Returns a list of {title, url, snippet} results ordered by relevance."`

The first leaves the agent guessing about when and why. The second makes the tool self-selecting — the agent can match its situation to the description without prompt-level instructions.

### Persistent Agent Pattern

For agents that exist over time — maintaining conversations, monitoring systems, playing games, running autonomous workflows — the architecture inverts: the agent is a **persistent presence** that controls its own attention, not a processor steered by an event queue.

| Do This                                                     | Not This                                    |
| ----------------------------------------------------------- | ------------------------------------------- |
| Agent sleeps when it chooses, wakes on events               | Event queue drives agent responses          |
| All timing is tools (sleep, debounce, remind, schedule)     | Hardcode delays or polling in orchestration |
| Stop hook prevents turn from ending — only sleep yields     | Request-response per event                  |
| Pull-based state reading (agent calls `context` when ready) | Push state changes as SDK user turns        |
| Agent parks thoughts (ideas, reminders) for later           | Drop context between interactions           |
| Expose environment state as tool-readable data              | Hide activity from the agent                |

**The core loop:** The agent never ends its turn. Instead it cycles: wake → read context → think → act → meta-assess → sleep. The only way to yield control is `sleep()`, so the agent decides when to engage, when to wait, and when to come back — it can debounce bursts, schedule actions, set reminders, and park thoughts, where a queue would force a reaction to every event.

### Reflection Pattern

Agents produce better output when forced to self-assess before committing. A reflection tool records confidence, uncertainties, and a tool audit, and runs an independent nested reviewer whose verdict opens or holds a gate; the gate rides inside submission, so a gated turn is rejected with a retriable message until the reviewer passes. The tool and its input model are the domain-specific half — add the fields your domain is actually judged on — while the gate is domain-neutral.

[docs/orchestration.md](docs/orchestration.md) carries both patterns in full: the scheduler and relay wirings, which backend inverts the loop and why, the gate's escape hatch after repeated failures, and where each piece lives.

---

<!-- section: Getting Started -->
# Getting Started

## Reference Files

Which file holds what is reference material — consulted once, when you already
know you need it — so it lives in the generated pages rather than here:
[docs/template.md](docs/template.md) for the application you customize, and
[docs/library.md](docs/library.md) for the `lup` package beneath it. Both are
generated from the same declarations this guidance is, so neither can fall
behind the tree it describes.

The three you will open first: `agent/prompts.py` for what the agent is told,
`agent/toolsets.py` for the tool-group registry — the one place a group is
added — and `agent/core.py` for how a session is composed.

**Versioning:**

- **pyproject.toml `[tool.lup] agent_version`**: The agent version — bump on behavior changes with `uv run lup-devtools version bump` (or `$lup:bump`)

**Environment:**

- **src/<project>/environment/cli/\_\_main\_\_.py**: Typer CLI — the `lup` entry point with `run` and `loop` (batch + auto-commit) commands

## Commands

```bash
uv sync                                  # install; `uv add <pkg>` to add, never edit pyproject.toml
uv run lup-devtools dev check            # the pre-flight bar: ruff, pyright, tests
uv run lup run "your task here"          # one session; --session-id names it
uv run lup loop "task1" "task2"          # several, auto-committing each
uv run lup-devtools setup                # keys, integrations, env vars (`dashboard` for the web UI)
```

`AGENT_SDK` and `AGENT_MODEL` pick the runtime and the model a session opens
against. `uv run lup --help` and `uv run lup-devtools --help` are the full
trees; [docs/contributing.md](docs/contributing.md) is the tour.

## Testing

`uv run pytest`, narrowed with `-k "<pattern>"` or a path when you want one
case. `tests/unit/` mocks external APIs; `tests/integration/` needs real keys
and is marked `@pytest.mark.integration`.

## Test Principles

**Test behavior, not construction.** Never test that a constructor sets attributes — that's testing the framework (Pydantic, dataclasses), not your code. If a class is a pure data container with no methods, computed properties, or custom validation, it doesn't need tests.

**Every test should answer: "what could go wrong?"** If nothing can go wrong (e.g., `assert artifact.name == "solution.py"` after setting `name="solution.py"`), the test is worthless. Good tests exercise:

- **State transitions** — does adding then removing leave the system clean?
- **Edge cases** — empty inputs, missing files, duplicate names, boundary values
- **Invariants** — properties that must hold across operations (e.g., cleanup stops all sandboxes)
- **Integration points** — does the code read from disk correctly? Does it compose with its dependencies?

**The test for a test:** Remove it. Does the remaining suite still catch real bugs? If yes, the test was dead weight.

| Write Tests For                           | Don't Write Tests For                        |
| ----------------------------------------- | -------------------------------------------- |
| Computed properties that read from disk   | Pydantic model construction                  |
| Registry CRUD with state verification     | Attribute access after `__init__`            |
| Error paths and graceful degradation      | Default field values                         |
| Multi-step workflows (add → use → remove) | Constants (`assert "Bash" in BUILTIN_TOOLS`) |
| Concurrency and timing behavior           | Sorted output of deterministic functions     |

## Debugging

**Do not hypothesize -- trace.** When debugging errors, find the actual logs and read the exact exception. Do not list "likely causes" or suggest the user check things. Open the log files yourself, grep for the error, read the traceback, and report what actually happened. If the logs don't contain enough information, say exactly what logging to add and where, so the error is captured next time.

Use `$lup:debug <error message>` to trace an error through the logs automatically.

## Feedback Loop Scripts

```bash
# Collect feedback from sessions
uv run lup-devtools feedback collect --all-time

# Status: version, data, analysis state, aggregate stats
uv run lup-devtools feedback status

# Analyze traces
uv run lup-devtools trace list
uv run lup-devtools trace show <session_id>
```

---

# Customization Guide

### Step 1: Run $lup:init

The `$lup:init` command walks you through customizing the template for your domain. It asks about:

- What your agent does
- How outcomes/ground truth are measured
- What metrics matter

### Step 2: Customize Models

Edit `src/<project>/agent/models.py`:

- `AgentOutput`: Your agent's structured output format
- `Factor`: Reasoning factors that influence outputs
- `SessionResult`: Complete session data for feedback analysis

### Step 3: Define Subagents

Edit `src/<project>/agent/subagents.py`:

- Create specialized subagents for focused tasks
- Define which tools each subagent can use
- Choose a model tier per subagent (strongest by default — see Model Selection; cheaper only with an explicit reason)

### Step 4: Configure Tools

Edit `src/<project>/agent/toolsets.py` and `src/<project>/agent/tool_policy.py`:

- Register tool groups once in `toolsets.py` — the single source every backend builds its servers from
- Tag tools that need credentials (`lup_tool(..., tags=["requires:<service>"])`)
- Map missing settings to excluded tags in `ToolPolicy`; `filter_tools()` drops tagged tools before server registration
- Add MCP server configurations
- Availability is enforced at runtime by an allowlist PreToolUse hook (`create_tool_allowlist_hook`) — the SDK's `allowed_tools` option is ignored under `bypassPermissions`

### Step 5: Configure Reflection

Edit `src/<project>/agent/tools/reflect.py`:

- Customize `ReflectInput` fields for your domain (e.g., factor analysis for forecasting)
- Customize the reviewer system prompt for your domain's failure modes
- Decide whether the nested reviewer agent adds value (adds latency but catches errors)
- The gate in `core.py` is already wired — reflection is enforced by default

### Step 6: Set Agent Version

The agent version lives in `pyproject.toml` under `[tool.lup]`:

```toml
[tool.lup]
agent_version = "0.1.0"
```

- Set the initial version during init
- Bump on behavior changes (prompts, tools, subagents) with `uv run lup-devtools version bump <level>` or `$lup:bump`

### Step 7: Enable Persistent Agent Mode (Optional)

For agents that exist over time (conversations, monitoring, games), use the persistent agent pattern:

- Wire `Scheduler` from `lup.orchestration.realtime.scheduler` into your session
- Add Stop hook to prevent turn ending (`create_stop_guard`)
- Implement sleep/context/reply tools from `agent/tools/realtime.py`
- Replace the request-response `run_agent()` in `core.py` with a sleep/wake loop
- The reflection gate also works here — gate `sleep` instead of `submit_output`

### Step 8: Update Feedback Collection

Edit `src/<project>/devtools/feedback/`:

- Implement `load_outcomes()` for your domain (`state.py`)
- Customize `compute_metrics()` for your metrics (`metrics.py`)
- Add domain-specific summary output (`reports.py`)

## Scaffolding Is a Menu, Not a Mandate

Everything lup ships belongs to a **module** — one subject as one value, carrying its skills, its agents, its page under `docs/`, its paragraph in this document, its command tree and its tool group. A module is taken or declined whole, so declining is a name in `DECLINED` rather than files to hunt down, and there is no keeping a subject's skills while deleting its page. `dev modules` prints the roster with what each contributes and what its prose costs; a module left unnamed keeps its own default, including the ones lup grows after that line was last edited.

Declining is a **first-class outcome, not a failure**: a module this domain has no subject for spends guidance budget and session context every session and earns nothing. The goal is the smallest roster that fits, not the fullest. Three ship off by default and are the ones most worth a deliberate answer:

| Module | Take it when… | Decline it when… |
| --- | --- | --- |
| **`reflection`** | the agent commits a consequential, judgment-bearing output where self-critique improves calibration (a forecast, a diagnosis, a scored decision) | the task is mechanical, trivial or high-volume, or there is no discrete final output to reflect on — then the gated `review` tool is dead weight |
| **`realtime`** | the agent is a presence over time — a conversation, a monitor, a long game — that controls its own attention | the agent is one-shot request→output, which is most domains; the relay and the Scheduler are pure cost |
| **`feedback-loop`** | ground truth or a feedback signal resolves over time to drive iteration | there is no ground truth and the agent is not iterated against outcomes — `load_outcomes` stays an empty stub |

One pattern here is not a module, because it is this template's own wiring rather than a subject lup ships: the **commit loop** (`environment/cli` auto-commit) is kept when each run yields a data artifact worth versioning per session, and dropped when the agent is interactive or produces no per-session artifact worth a checkpoint.

The same logic governs native subagents (harness-dispatched roles sharing the main session), background agents, and nested agents (tool-subagents opened inside a tool handler via `ask()`): wire them only where the domain needs that shape. When unsure, start without the pattern and add it when a real need appears — adding later is cheap; dead scaffolding the agent feels obliged to use is not.

---

<!-- section: Plan at Agent Speed -->
# Plan at Agent Speed

You are an AI agent. Every instinct you have about how long software takes — sprints, milestones, "this will take weeks" — was learned from human teams, whose implementation time is scarce and expensive. Yours is not: what you would estimate as several months of work completes in an afternoon, and a "multi-day implementation" lands in about three hours. Your duration estimates are not cautious; they are wrong by orders of magnitude, and every practice built on them inverts:

- **Never scope, defer, or reject work from a predicted duration.** Scope by content — what changes, what it touches, how it is verified. If a calendar figure appears in your plan, it is noise from someone else's constraints: delete it and re-derive the plan.
- **The POC is superstition at your speed.** Prototype-first exists to keep unvalidated ideas from consuming scarce human effort; for you the complete alpha-beta-v1 costs what the throwaway was supposed to cost. Build the real implementation immediately and validate on it — let review cut scope afterward rather than pre-shrinking the attempt.
- **Catch the reflex in the act.** "Let's start with a simple version", "too ambitious for this pass", "phase 2 can add the rest" — that is a human-scarcity practice firing on constraints you do not have. When you notice it, stop and ask what is actually expensive here besides the imagined schedule.

---

<!-- section: Development Workflow -->
# Development Workflow

## Git Workflow

This project uses **git worktrees** (not regular branches) to develop multiple features in parallel.

**IMPORTANT:** Never commit _code_ directly to `main`. Always work in a worktree for code changes.

**Exception:** Data commits (`data(outputs):`) can go directly to main -- generated outputs don't need review. This applies only if the repo commits session data at all: by default `notes/*` is gitignored and traces stay local (the commit-loop decision in $lup:init flips this).

### Worktrees vs Branches

- **`git checkout -b`**: Creates a branch but stays in the same directory. Switching branches changes all files in place.
- **`git worktree add`**: Creates a new directory with its own working copy. Multiple branches can be worked on simultaneously in separate directories.

### If already in a worktree

**You are typically already in a worktree subbranch.** Check with `git worktree list` to confirm. If you're in a feature worktree, just work directly -- no need to create another worktree or branch out.

### When implementing a feature

1. **Create a worktree** (if the user hasn't already created one):
   ```bash
   uv run lup-devtools git worktree create feat-name
   ```
   This creates the worktree as a sibling under `tree/` (e.g., `tree/feat-name` alongside `tree/main`) and syncs dependencies; `lup-devtools harness codex` regenerates the artifacts and installs the digest-verified plugin copy, so no per-worktree plugin install is needed. **Never** use `git worktree add ./worktrees/...` — worktrees must be siblings, not nested inside another checkout.
2. **Relocate this session into the worktree** -- start a session rooted at <the absolute path step 1 prints> and continue there — this runtime cannot move a running session, so work carried on here would land in the checkout it started from. Already running, address files there by absolute path, where that tree is writable, which reaches the same branch.. Creating a worktree does not move the session: skip this and the agent keeps editing the integration checkout while the branch it just made sits untouched, so the work stays invisible until it has already gone stale.
3. **Commit regularly and atomically** -- Each commit should represent a single logical change. Don't bundle unrelated changes together.
4. Push the branch when the feature is complete (or periodically for backup)
5. **`$lup:rebase`** -- Pushes the branch, opens a PR, then cleans up the commit history with `git reset --soft main` and force-pushes.
6. **Review the PR** -- If changes are needed, fix them on the feature branch and re-run `$lup:rebase` (it rebuilds the history and force-pushes, updating the PR).
7. **`$lup:close`** -- Once the PR is approved, merges it and cleans up the branch.

### Merge Conflict Resolution

**Never silently drop code during conflict resolution** — keeping both sides is safer than losing features, and a rename on one side must not swallow an addition on the other. Before completing any merge, **audit for deletions**: compare the result against both parents and verify every removed function, parameter, or command went deliberately, not as a side effect of choosing one side. `$lup:merge` carries the decision tree.

### Commit Guidelines

- **Commit before responding**, and often — frequent commits are checkpoints
- **Keep commits atomic** — if you need "and" in the message, it is two commits
- **History will be rebased**, so a message need not be perfect while developing; after rebasing, each should tell what changed and why

**Format:** `type(scope): description`

**Types:**

| Type | Use |
| --- | --- |
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `refactor` | Neither fixes a bug nor adds a feature |
| `docs` | Documentation only |
| `test` | Adding or updating tests |
| `chore` | Maintenance — dependencies, build config |
| `meta` | Harness content and the trees it generates: guidance, settings, skills, hooks |
| `data` | Generated data and outputs |
| `release` | What `dev release` commits for a release or a candidate of one — never written by hand |

**Examples:**

```
feat(agent): add retry logic for API calls
fix(tools): handle missing API key gracefully
refactor(config): extract settings validation
meta(claude): update commit message guidelines
data(outputs): add session batch results
```

## Editing Style

**Prefer small, atomic edits.** The PreToolUse hook decodes `apply_patch`'s complete command into before/after documents and applies the canonical edit policy. Safe changes with up to three real added lines are automatically allowed; protected paths, anti-patterns, marker changes, and full-file writes keep their guardrails.

- Split large changes into multiple small patches, one logical change each
- Separate concerns -- move imports in one patch, change logic in another
- Rename identifiers exhaustively and run `uv run pyright` to verify nothing dangles

## Directory Structure

Where each part of the application sits is in
[docs/template.md](docs/template.md), which walks the checkout when the page is
generated rather than describing it from memory, and carries the prose about
each part beside it.

The library beneath it is described in [docs/library.md](docs/library.md)
rather than drawn here, because in three of the four ways a project can obtain
`lup` its source is not on disk at all — a diagram of `packages/lup/` would be
describing a directory most projects never have.

---

<!-- section: Code Style & Patterns -->
# Code Style & Patterns

## Primary Libraries

- **lup**: The runtime this project composes against. `Claude` and `Codex` each declare an agent that opens its own sessions; `ask` is the only verb, putting the prompt and the type the answer must arrive as; a strict `TurnResult[T]` hands back `.output` already validated. `await agent.ask(prompt, Model)` is the whole of a one-shot.
- **pydantic**: For data validation and settings
- **pydantic-settings**: For configuration (not dotenv)

Which runtimes this project drives is an extra rather than a rewrite —
`lup-agents[claude]`, `lup-agents[codex]`, or both — and the same declarations
render into each. A provider's own SDK is that adapter's dependency, never
this application's.

## Model Selection

Default to the **strongest** tier for the main agent, every subagent, reviewer, and background agent. This runs on a subscription where the best model is the point: reach for **balanced** only when latency or cost provably dominates and quality is non-critical, and for **fast** almost never. A role that genuinely warrants a cheaper model declares that tier explicitly with a reason; otherwise it inherits the strongest default.

State the tier, not a model id. A declaration says what the role needs and each runtime spells whichever model in its own lineup honors it, so a role pinned to one provider's model name is a role that only works on one runtime and stops being right the next time that lineup moves. The one place a concrete model belongs is the runtime configuration a deployment sets (`AGENT_MODEL`), where naming a specific model is the whole point.

## Type Safety Requirements

- **Never silently swallow exceptions** -- no `except ...: pass`, no `contextlib.suppress`; log with `logger.exception()`, handle meaningfully, or re-raise. Catch-all `except Exception` is fine at boundaries (task loops, subagent delegation) that do so; bare `except:` and `except BaseException` are never fine
- **Every function must specify input and output types**
- **Never use `Any`, `dict[str, Any]`, or `dict[str, object]`** -- these erase type information and defeat static analysis. Which typed stand-in replaces one depends on where the dict came from, and [docs/conventions.md](docs/conventions.md) carries the row for each
- **Provider SDKs are the adapter's, not yours**: application code composes against `lup`'s runtime — `Claude`, `Codex`, `Agent`, `Conversation`, `Turn`, `TurnResult` — and never imports a provider SDK. Each SDK is one adapter's dependency behind an extra, so importing it here pins the application to one runtime and trips `seam-boundary` outside a composition root that names it.
- **Use Python 3.12+ generics syntax**: `class A[T]`, not `Generic[T]`
- Never manually parse an agent's output -- ask for it as a type. `ask(prompt, Model)` binds that turn's output to the schema, and `TurnResult[Model].output` arrives validated
- **Never use `# type: ignore`** -- Ask the user how to properly fix type errors
- **`# lup: ignore` escape hatch** -- When `Any` or another anti-pattern is genuinely needed (untyped library boundaries, MCP), add an inline ignore to request user approval. Prefer the typed, pyright-style `# lup: ignore[rule-id]` so a site silences exactly the rule it needs and still trips the others; the bare `# lup: ignore` stays valid but the auditor flags it as untyped. A standalone ignore in the first 10 lines applies file-wide. Each rule id is shown in its deny message; the generated `docs/rules.md` (`uv run lup-devtools dev rules`) indexes every rule family with the `lup.harness.codescan` module that defines it.
- **Use Pydantic BaseModel instead of dataclasses**
- **Use `match`/`case` instead of `if`/`elif` chains** for dispatching on values or ranges

## Tool Input Schemas

Define tool inputs as BaseModel classes with `Field(description=...)`: one declaration is where both the `@lup_tool` schema and the validation come from. The decorator infers each schema from the handler's annotations, validates the input before the handler runs, and serializes the returned model — so validating arguments or assembling a response envelope by hand is work already done for you, and a recoverable failure is a raised `ToolError` carrying what to do about it. [docs/conventions.md](docs/conventions.md) puts each typed form beside the raw dict it replaces.

## No String Manipulation on Structured Data

Reaching for `re`, `.replace()`, `.split()`, or string slicing to extract, transform, or filter structured data means the structured API was missed. Operate on the structure directly: [docs/conventions.md](docs/conventions.md) names a parser per format — web pages, XML, JSON, turn results, dates, URLs, filesystem paths.

String operations are for formatting output. If you are using them to understand or transform data, you are working at the wrong abstraction level, and `import re` in particular is a code smell -- if you find yourself writing a regex, stop and look for the structured API.

## Use Standard Libraries

When integrating with external services (APIs, data sources, etc.):

- **Use existing Python libraries first** -- Check PyPI for official or well-maintained client libraries before writing raw HTTP requests
- **Don't rebuild the wheel** -- If a library exists with good documentation and maintenance, use it

## Code as Documentation

The codebase should read as a **monolithic source of truth** -- understandable without any knowledge of its history.

**The test:** Before adding a comment, ask: "Would this comment exist if the code had always been written this way?" If no -- don't add it.

**Do not:**

- Add comments to explain modifications you made
- Reference what code used to do (e.g., "Previously this returned None")
- Add inline comments when changing a line
- Use phrases like "now", "new", "updated", "fixed", or "changed" in comments

**Do:**

- Write comments that would make sense to someone who never saw previous versions
- Use commit messages for change history, not code comments
- Only add comments that document genuinely non-obvious behavior

## Inline `# lup:` Notes

A `# lup:` (or `// lup:`) comment is **actionable review feedback** for the agent to address — distinct from the `# lup: ignore` anti-pattern escape hatch. The edits hook prompts whenever an edit changes a file's `# lup:` marker count, and `lup-devtools` scans for unresolved notes.

**Never delete a `# lup:` note until its concern is actually resolved** — fix the code it points at, or answer the question and reflect that answer in code, docs, or an explicit user decision. Making a file parse or tidying up does not count. A note in a comment-less format (e.g. JSON) still can't be silently dropped: resolve it, or relocate it to a file that can hold it. Use `$lup:resolve` to clear resolved notes.

## Error Handling Philosophy

**MCP tools should:**

- Return `{"content": [...], "is_error": True}` for recoverable errors
- Log exceptions with `logger.exception()` for debugging
- Include actionable error messages (what failed, why, what to try)

**Agent code should:**

- Raise exceptions for unrecoverable errors (missing config, invalid state)
- Use the `with_retry` decorator for transient failures (HTTP timeouts, rate limits)
- Validate inputs early with Pydantic models

**Never silently swallow errors** -- either handle them meaningfully or let them propagate.

## DRY: Don't Repeat Yourself

- **Never duplicate code** -- If logic exists in the `lup` library, import it. Don't copy-paste.
- **Utilities belong in `packages/lup/`** -- Functions like `print_block`, `TraceLogger`, formatters go in the lup package, not the application package.
- **The application imports from `lup`** -- The agent layer uses lup abstractions, never redefines them.
- **Check before writing** -- Before creating a utility, search the `lup` package for existing implementations.

## lup (library) vs application Boundary

Code in `packages/lup/` must be **complete-as-is and configurable through function arguments** — never by modifying the source. Domain-specific code belongs in `src/<project>/`. If a lup module requires subclassing or source modification to customize, it violates this principle.

- **Use function parameters** for customization (callbacks, config objects, path overrides)
- **Use `configure()`-style functions** for module-level state that needs overriding
- **No imports from the application package** in lup code — the dependency arrow points one way
- **Placement test:** Can this module be used as-is in a different project without modification? If yes → `packages/lup/`. Does it import from the application package? If yes → `src/<project>/`.

## Imports: No Barrel Files

**Never use `__init__.py` re-exports or `__all__` in internal packages.** Import directly from the module that defines the symbol.

- `from lup.tools.mcp import lup_tool` -- not `from lup import lup_tool`
- `__init__.py` files should contain only the module docstring (no imports, no `__all__`)
- Barrel files drift out of sync and hide real dependencies

**Exception:** Standalone library packages under `packages/` may use re-exports with `__all__` in their top-level `__init__.py` to declare a public API. Only the package root -- not subpackages.

## Naming: No Private Prefixes

**Never use `_` prefixes** on functions, methods, classes, or constants. Nothing is private.

- Module-level functions: just name them `build_options`, not `_build_options`
- Class methods: `remove_stale_container`, not `_remove_stale_container`
- Constants: `PACE_THRESHOLDS`, not `_PACE_THRESHOLDS`
- Classes: `PendingReminder`, not `_PendingReminder`

**If a helper truly shouldn't pollute the module namespace**, nest it inside its only caller:

```python
def build_display(usage, stats):
    def place_label(text, position, width):
        ...
    # use place_label here
```

**Avoid useless mini-wrappers.** If a function's only purpose is to call another function with no additional logic, inline it.

**Exceptions:** `_` prefix is fine for unused parameters (`_context`, `_exc_type`) -- that's a linting convention, not a privacy convention.

## Tools

- **uv**: Package manager. Use `uv add <package>` (never edit pyproject.toml directly)
- **ruff**: Formatting and linting
- **pyright**: Type checking

## Pyright Diagnostics

Nothing type-checks an edit for you here. Codex names the files it changed inside the patch envelope, and decoding one validates its context against the document on disk -- which is already the rewritten version by the time an edit could be observed, so there is no reading of what changed to check. Run `uv run pyright` after every substantive change and act on what it reports.

The `codeintel` tools do answer definitions, usages, and types, resolving imports and aliases as the checker does. Prefer them over word-boundary searches, and confirm a guess against them rather than acting on it. A relative path resolves against the checkout being edited, which the hook publishes on every patch; pass an absolute path when you mean a file somewhere else.

| Tool | Contract |
| --- | --- |
| `find_definition` | Find where a symbol is defined. Use instead of grepping for `def name` or `class name`: this resolves imports and aliases, so it finds the real declaration rather than a line that looks like one. |
| `find_references` | Find every use of a symbol across the workspace. Use instead of grepping for a name: this excludes look-alikes in other scopes and includes uses reached through an alias or a re-export. |
| `hover` | Read a symbol&#x27;s inferred type and documentation. Use before assuming what a value is: the checker knows the type that was resolved. |
| `list_symbols` | List every symbol a file declares, with its line. Use instead of grepping for `def ` or `class ` to learn a file&#x27;s shape. |
| `rename_symbol` | Plan a workspace-wide rename of the symbol at a position. Reports the files and edit counts without writing anything. Always prefer this over a find-and-replace, which cannot tell one scope from another. |

---

<!-- section: Tooling -->
# Tooling

## lup-devtools

All development tooling lives in `src/<project>/devtools/` and is exposed as the `lup-devtools` CLI entry point. **Always use `lup-devtools` instead of ad-hoc commands.** Never use `uv run python -c "..."` or bare `python`/`python3` -- these are denied by the Bash permission hook.

If you find yourself running the same command repeatedly, **add a command** to `src/<project>/devtools/`.

`tmp/` is scratch: gitignored, so nothing written there reaches a diff, a reviewer, or the human — which is why it does not execute. For one-off work, in order: run it in the sandbox where the work allows; add a `lup-devtools` command, which is reviewable because `devtools/` lands in the diff; or, as a last resort, `python3 <<<EOF` behind a `# lup: escalate[decision]: <why>` marker. The argument is reviewability, not power — an agent may already edit `devtools/` and run it.

**Write scripts in Python using [typer](https://typer.tiangolo.com/)** for CLI interfaces. Use **[sh](https://sh.readthedocs.io/)** for shell commands instead of `subprocess`.

Run `uv run lup-devtools --help` for the full command tree.

`lup-devtools harness codex` regenerates and verifies the Codex artifacts,
installs an immutable content-addressed copy of the plugin after a digest check, and
launches the Codex CLI in a persistent per-worktree home derived, at every
launch, from the selected account's authentication and settings — the account
home of the profile `~/.config/lup/config.toml` selects, else `~/.codex` — and
returns what a session changed in them to that account when it closes. The
home is kept in lup's state, `~/.local/state/lup/homes/` (or under
`$XDG_STATE_HOME`), never in the checkout, since it holds a copy of the login.
`lup-devtools dev usage codex` reports this backend's usage and
`lup-devtools dev usage claude` the other's; profiles, one name per account on
both runtimes, are managed with `lup-devtools harness profile`.
`--codex-home` or an inherited `CODEX_HOME` selects an explicit home instead.

Each repo names its plugin **marketplace** after the project — the plugin entry stays `lup`, so `$lup:*` is identical everywhere. Codex resolves the marketplace from the repository's `.agents/plugins/marketplace.json` and installs the plugin into its own cache, verifying the digest before every launch; `lup-devtools dev plugin name` (run by `$lup:init` and `$lup:install`) wires the per-project name.

## Permission Hooks

Permissions come from the canonical semantic policies in `lup.policy` and the
application-owned `HookSet` in `src/<project>/harness/catalog.py`. Harness generation
compiles one hermetic dispatcher and dependency-free runtime for each native
plugin. Do not edit generated policy files directly.

The policy classifies every shell command against the vocabulary `lup.policy.vocabulary`
declares and `src/<project>/harness/content/shell_vocabulary.py` adjusts, every URL scope, and every edit
in a batch. Segments join deny > ask > defer > allow, so a judged deny wins the
batch and malformed input fails conservatively. Ask is reserved for judged
risk: an unjudged command denies with a hint naming the
`# lup: escalate[decision]: <why>` marker, and that marker as a command's leading line
promotes the decision into an approval question carrying your stated reason.
Under a launcher-verified sandbox (`LUP_SANDBOX_ACTIVE`), unjudged work defers
to that boundary rather than denying. Native `apply_patch` commands are decoded into complete before/after
batches for the canonical edit policy, and malformed or unsupported patches
fail closed. Codex's own sandbox and approval policy remain the outer
filesystem and network boundary. Generation also compiles every prefix-safe
shell allow into `.codex/rules/lup.rules`, which Codex uses to run matching
commands outside the sandbox without prompting, while flag- and
content-sensitive forms stay under the hook.
[docs/permissions.md](docs/permissions.md) carries the full lattice. Use
`$lup:hooks` to update canonical inputs, regenerate both plugins, and run the
shared canonical/bundled fixture suite.

## Settings & Configuration

Project Codex configuration is the generated `.codex/config.toml`, loaded only for a trusted project. Personal sandbox and approval defaults belong in `~/.codex/config.toml`; `sandbox_mode = "workspace-write"` with `approval_policy = "on-request"` is the low-friction guarded default. Never edit the generated project file.

Prefix-safe outside shell allows from the canonical policy are generated as project-local rules in `.codex/rules/lup.rules`. A matching native `allow` approves the command without choosing its sandbox placement. When the semantic policy places a command outside, set `sandbox_permissions = "require_escalated"` on the first call; the native rule removes the prompt and the PreToolUse hook still blocks unsafe variants. Commands whose safety depends on flags, paths, shell structure, or runtime content stay under the sandbox and approval flow.

---

<!-- section: Process & Communication -->
# Process & Communication

## Asking Questions

**Ask questions as explicit, numbered options** rather than burying them in prose. This applies to:

- Clarifying requirements or ambiguous instructions
- Offering choices between implementation approaches
- Confirming before destructive or irreversible actions
- Proposing changes or improvements
- Any situation where you need user input before proceeding

Even for open-ended questions, present concrete options plus an explicit free-form alternative, so the user can answer with a single short choice.

**When proposing changes:**

- **Propose, don't assume**: Ask before making changes
- **Show context**: Show relevant current state before proposing
- **Explain rationale**: Every suggestion should include why it would help
- **Offer alternatives**: Present options when multiple valid approaches exist

**When in doubt, ask.** Err on the side of asking questions rather than making assumptions.

## Skills

**After every skill invocation**, reflect on how it was actually used vs. documented:

1. **Compare intent vs usage**: Did the skill serve its documented purpose, or was it adapted?
2. **Notice patterns**: When the user corrects your approach or redirects focus, that's a signal the skill should evolve.
3. **Proactively propose updates**: Suggest skill improvements as explicit options.

**Evolution signals:**

- User provides external docs -> Add doc-fetching or reference to the skill
- User corrects your approach -> Update the skill to prevent future errors
- User asks for something the skill should cover -> Expand scope
- User ignores sections -> Consider simplifying

## External Resources

When questions involve the Claude Agent SDK or the Claude API used by the inner agent, fetch the docs directly:

- `https://docs.claude.com/en/agent-sdk/<topic>`
- `https://docs.claude.com/en/api/<topic>`

When the user provides documentation links, incorporate that knowledge into AGENTS.md or relevant skills.

---

<!-- section: Self-Improvement Loop -->
# Self-Improvement Loop

See [The Bitter Lesson](#the-bitter-lesson) and [Tool Design Philosophy](#tool-design-philosophy) above — these are the governing principles for all agent improvements.

**When analyzing failures:** Ask "what general principle would have prevented this?" not "what specific rule would catch this case?" Instead of a prompt line about the decision that went wrong: does the agent have enough context? The right tools? A strong enough model?

When the principle points to a workflow failure, fix the workflow at the exact juncture where the failure enters — don't add a warning about it. A step named "Classify each commit" invites whole-commit thinking regardless of how many times the text says "decompose." Renaming the step to "Extract portable pieces" and separating reading from judging makes the failure structurally impossible. Warnings coexist peacefully with the workflows they warn against; structural changes don't.

### Diagnosing Failures

When the agent fails, trace the failure through the pipeline before changing anything:

1. **What data did the agent have?** Read the trace. What tools did it call? What did they return?
2. **Where in the workflow did the wrong decision enter?** Find the entry point, not the symptom.
3. **What structural change prevents it?** A new tool, a better tool description, a restructured step, richer data.

A prompt rule is a patch that coexists with the failure. A structural change makes the failure impossible.

### Three Levels of Analysis

1. **Object Level** -- The agent itself: tools, capabilities, behavior
2. **Meta Level** -- The agent's self-tracking: what it monitors about itself
3. **Meta-Meta Level** -- The feedback loop process: scripts, analysis methods

### Running the Feedback Loop

1. **Collect feedback**: `uv run lup-devtools feedback collect`
2. **Read traces deeply**: Don't skip to aggregates. Read 5-10 sessions in detail.
3. **Extract patterns**: Tool failures, capability requests, reasoning quality
4. **Implement changes**: Fix tools -> Build requested capabilities -> Simplify prompts
5. **Update documentation**: This file should evolve with the agent

### What to Track Per Session

- **Sessions**: Results saved to `notes/traces/<version>/sessions/<session_id>/`
- **Outputs**: Task outputs saved to `notes/traces/<version>/outputs/<task_id>/`
- **Traces**: Reasoning logs saved to `notes/traces/<version>/logs/<session_id>/`
- **Metrics**: Tool calls, timing, errors via metrics tracking

---

# Configuration

### Environment Variables

The `.env` file contains the template configuration. Create `.env.local` for your secrets (gitignored):

```bash
# .env.local - your secrets (ANTHROPIC_API_KEY is read directly by the SDK from env)

# Optional overrides
# AGENT_MODEL=claude-opus-5
# AGENT_MAX_BUDGET_USD=5.00
# AGENT_MAX_TURNS=50
# AGENT_SANDBOX_ENABLED=false   # run without Docker (disables code execution tools)
# AGENT_NOTES_PATH=./notes      # relocate session data
# AGENT_LOGS_PATH=./logs        # relocate trace logs
```

Settings in `.env.local` override `.env`.

### Settings

Configuration is loaded via pydantic-settings. See `src/<project>/agent/config.py` for all options.

---

<!-- section: Anti-Patterns to Avoid -->
# Anti-Patterns to Avoid

- Adding numeric patches ("subtract 10% from estimates") or absolute thresholds ("if X happens N times, do Y")
- Prompting the agent with rigid mechanical procedures instead of guidelines and rationale
- Copying examples from a specific trace into the prompt instead of deriving general principles and writing fresh examples
- Adding rules the agent can't act on (no access to required data)
- Patching for one observed symptom instead of tracing the failure through the pipeline to find the structural cause
- Adding "CRITICAL: Never do X" warnings instead of restructuring the workflow so X has no entry point
- Listing tools by name in the system prompt (creates two sources of truth that drift apart)
- Writing terse tool descriptions (the agent can't use a tool well if it doesn't know when or why)
- Skipping trace analysis to jump to aggregate statistics
- Over-engineering initial implementations

### Questions to Ask

When proposing changes:

1. Does this add a capability or just a rule?
2. Would this help if the domain changed completely?
3. Are we changing the right level (object/meta/meta-meta)?
4. What general principle would have prevented this failure?
5. What data would we need to validate this change worked?
