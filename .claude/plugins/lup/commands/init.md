---
description: Make this checkout a project for one domain — choose its modules, settle its seams, rename and scaffold it
allowed-tools: Bash(git:*, uv run lup-devtools:*, uv sync:*, uv run pyright:*, uv run ruff:*, uv run pytest:*), Read, Edit, Write, AskUserQuestion
---

# Initialize a Project

This command makes this checkout a project for one agent domain: it settles the project's identity, chooses the modules the project takes, puts each seam the library ships at a default to the user, renames the source package, and generates the scaffolding for the domain.

**This project builds on an agent SDK, not raw model API calls.** The SDK is the default and expected framework. If the user wants bare API calls instead, ask them to explain why -- the SDK provides structured outputs, tool use, subagents, and hooks out of the box.

## Your Task

Interview the user about their domain, rename the source package, and generate the appropriate scaffolding.

### The commit you start from is the library you get

`packages/lup/` and the copied half — `src/` and `tests/` — are lup at one
commit, the *base*, and everything Phase 2 settles about lup is taken at it:
the library pin names a branch holding it, and the upstream checkpoint and the
scaffold branch `dev update` merges against are both rooted there. Where the
base is written down depends on how this repository was made. A clone of lup
carries lup's history, and the base is the commit it stands on. A repository
made with GitHub's "Use this template" carries none of it: GitHub copies the
template's files into one fresh root commit, with no parent and no remote
naming lup, so this checkout's branch and its `origin` are the project's own
and say nothing about lup. Settle the base before Phase 0, in two commands.

First point the `lup` registration at the repository this project came from.
`sync.json` ships an entry naming lup's repository, required and mounted
read-write, which is what gives every session of this project a clone of lup
to fix upstream defects in, with nothing set up on any machine. A project
generated from a fork of lup builds on the fork, and GitHub records the
template a repository was generated from, so ask it once:

```bash
uv run lup-devtools dev init upstream --dry-run
uv run lup-devtools dev init upstream
```

It points the entry at the template where that is another repository, and
says why it left the entry as shipped otherwise. Where the project already
pins lup to a repository, the entry follows that pin, so the command prints
the `dev library git --url` invocation that moves the pin rather than writing
the entry. Where it cannot ask the forge, it says why: ask the user which
repository the project was generated from.

The mount takes effect at the next launch. This session opened while the
checkout was still the scaffold, which owes itself no copy of lup, so it holds
none; once initialization is done, relaunch and `refs/lup` is lup's working
tree.

Then read the base out of that repository's history:

```bash
uv run lup-devtools dev init base
```

It fetches the registration — cloning it under `~/.cache/lup/sync/` the first
time — and finds the base in lup's own history: in a clone, the newest commit
this checkout shares with lup; in a generated repository, the commit whose
tree the root commit holds exactly. It prints the base in full, how it was
found, and each branch of lup holding it with how far that branch has moved
past it. Where no commit holds the root tree exactly — the root commit was
amended, or the template's history rewritten since — it names the nearest
commit instead, says so, and exits nonzero: tell the user the base is an
estimate, and confirm it with them before anything below is taken at it.

A base the default branch does not hold carries work the stable branch has
not reviewed, and nothing downstream announces that. Where the report says
so, Ask the user with the AskUserQuestion tool, offering concrete options plus a free-text choice: whether to go on from the base, which carries work the default branch has not reviewed, or from the default branch instead. In a clone the stable branch is a `git switch` away: switch now,
before Phase 0 reads anything, and run `dev init base` again. A generated
repository holds only what GitHub copied, so there the answer is whether to go
on from this base at all.

Record the base, and the branch holding it that the answer settles on. The
base is *the recorded commit* every step below names.

## Phase 0: Check for DESIGN.md

Before starting the interview, check if `DESIGN.md` exists in the project root. If it does:

1. Read it thoroughly
2. Use it as context for the entire init process -- it contains design decisions from a `/lup:brainstorm` session
3. Still run the full interview, but reference design decisions when asking questions (e.g., "DESIGN.md mentions you want a persistent agent with sleep/wake -- does that still hold?")
4. Skip questions whose answers are unambiguously covered in the design doc
5. If no DESIGN.md exists, proceed normally

## Phase 1: Project Identity

Determine the project name by asking:

### 1. Project Name

- What should the project be called? This becomes the Python package name.
- Must be a valid Python identifier (lowercase, underscores, no hyphens or spaces).
- Examples: `aib`, `forecast_bot`, `coach`, `game_agent`

### 2. Agent Purpose

- What does the agent do? (forecasting, coaching, game playing, task completion, etc.)
- What is a "session" or "run"? (one forecast, one conversation, one game, one task)

### 3. Ground Truth & Success Metrics

- How do you know if the agent did well?
  - **External ground truth**: Outcomes that resolve later (predictions, game wins, task success)
  - **Human feedback**: Ratings, corrections, preferences
  - **Proxy metrics**: Engagement time, task completion, coherence scores
  - **Self-assessment**: Agent's own meta-reflection quality
  - **No clear ground truth**: Focus on process quality and trace analysis

### 4. What to Track

- What outputs should be saved per session?
- What metrics matter? (accuracy, cost, time, tool usage, user satisfaction)
- What trace data is valuable? (reasoning, tool calls, intermediate states)

### 5. Feedback Sources

- Where does feedback come from?
  - Resolution/outcome data
  - User ratings or corrections
  - Comparison against baselines
  - Expert review
  - Automated quality checks

### 6. Task Format

- How are tasks provided to the agent? (free text, IDs, files, API calls)
- Should the `loop` CLI command batch-process them?
- What does auto-commit look like for this domain?

## Interviewing Style

Ask extensively -- don't make assumptions about the domain. Ask open-ended questions first, then drill into specifics. Example questions (adapt based on context):

- "What should this project be called? (valid Python package name, e.g., 'aib', 'forecast_bot')"
- "What does your agent do and what does a single session look like?"
- "How do you know if the agent did well? Is there ground truth that resolves later?"
- "What metrics matter most to you?"
- "How are tasks provided -- free text, IDs, files, API calls?"
- "Should results auto-commit after each session?"
- "What tools or APIs will the agent need?"

Let the conversation flow naturally. The goal is to understand the domain well enough to customize the template files below.

Open-ended exploration is ordinary conversation, but every answer that *forks
the scaffolding* — the package name, what a run is, whether ground truth
resolves later — decides which files exist at the end. Ask the user with the AskUserQuestion tool, offering concrete options plus a free-text choice: each identity answer that decides what gets generated, with the reading you would pick offered first rather than leaving it in prose the user has to notice and correct.

## Phase 1.5: Choose the Modules

First, the part that is not a decision. The template ships demonstrations of
*itself* — `examples/` composing lup's own runtime against lup's own README,
and the test modules driving them. A domain that adopted the template is a
consumer of that library rather than a demonstrator of it, so what it inherits
there is a directory it will never run and a suite it has to keep green. Run:

```bash
uv run lup-devtools dev init drop-examples --dry-run
uv run lup-devtools dev init drop-examples
```

It reports the handful of lines still naming what went — the `"examples/"`
roots in the catalog, which are dead once the directory is, and whatever else
of this project's still names it, such as a README link or a test case running
an example module. Fix those; the README is human-owned, so propose that edit
rather than making it. It also names the removed test modules the scaffold
declaration does not decline yet: add them to `declined` in
`declared_scaffold()`, so the next `dev update` leaves them out rather than
offering each back as a conflict.

Now the decisions, and there is one kind of them. Everything lup ships belongs
to a **module** — a subject as one value, carrying its skills, its agents, its
page under `docs/`, its paragraph in the always-loaded document, its command
tree and its tool group. A module is taken or declined whole, so there is no
keeping a subject's skills and deleting its page, and there are no files to
hunt down: declining is a name in a list.

Start by reading the roster, which is the only complete statement of what is on
offer:

```bash
uv run lup-devtools dev modules --verbose
```

Each row says what the module is, whether this project has it, what its prose
costs in the always-loaded document, and what it contributes. Walk it against
the interview answers. **Declining is the expected answer for several of them,
and it is not a loss** — a module a domain has no subject for spends guidance
budget and session context every time and earns nothing. Three ship off by
default and are worth naming here, because each is a real capability rather
than a leftover:

- **`reflection`** — the gate an agent meets on its own output, an independent reviewer between finishing the work and submitting it. Take it if the agent commits a consequential, judgment-bearing output where self-critique helps.
- **`realtime`** — persistent agents that control their own attention: the sleep/wake loop, and the relay that spells it for subprocess backends. Take it for an agent that lives over time (chat, monitoring, a game), never for a one-shot one.
- **`feedback-loop`** — turning an observed agent failure into a durable capability change. Take it only if ground truth or a feedback signal resolves over time; a domain whose output nobody grades has nothing to feed it.

Ask about every module the roster offers rather than only those three — this
list goes stale and `dev modules` does not. A row marked `essential`, or
needed by an essential one, is every project's and is not offered; a module
another one `requires` goes only together with it, and generation says which.

Ask the user with the AskUserQuestion tool, offering concrete options plus a free-text choice: which modules this domain takes and which it declines, one option per module the roster offers — for each, say which way you would go and why, from what the interview
established rather than from what sounds useful.

Write the answer as module ids in `DECLINED`, in `harness/content/catalog.py`.
Nothing else changes: a declined module's skills, page, prose, commands and
tools stop arriving together, and a module left unnamed keeps its own default —
including the ones lup grows after that line was last edited, which is why the
list is refusals rather than what is kept. Then regenerate with
`uv run lup-devtools harness generate all` and re-read `dev modules`.

One decision in this phase is *not* a module, because it is this template's own
wiring rather than a subject lup ships. **Commit loop** (auto-commit in `environment/cli/__main__.py`) — keep only if each run yields a data artifact worth versioning. Session data is gitignored by default (the `notes/*` lines in `.gitignore`), so traces and outputs stay local; keeping this pattern means removing the `notes/*` and `!notes/.gitkeep` pair so session data can be committed. The `notes/harness/` line under them is not part of that decision and stays either way — a launch transcript is one native CLI session in full, redacted for portability rather than for publication. When deleting the pattern, leave every ignore line in place.

The customization steps below apply only to what you kept.

## Phase 1.6: Settle the Seams

A seam is a place the library holds an opinion this domain is meant to overrule, and every one of them ships at a default. **A default nobody was shown is not a decision** — so put each of them to the user rather than letting the scaffold's answer become theirs by silence.

Run `uv run lup-devtools dev seams`. It prints each seam, what it currently holds, and where it is written. Take them one at a time:

- **Who owns which files.** A human-owned file surfaces every change as an approval and the agent does not write it — it proposes the edit instead. `README.md` ships owned, which is right for a scaffold whose README describes the scaffold and often wrong for a domain whose README is the one file it most wants written for it. Ask; `dev seams --disown README.md` or `--own <path>` writes the answer.
- **Which trees an edit needs approval into.** What ships answers for a framework that generates its own plugin trees and carries its own policy. A domain whose sensitive files are a data directory, a migration set or a deployment manifest says so instead.
- **What each tree is for.** A role is how every gate tells a fixture from production and a build product from work, so a data directory, a notebook tree or a generated client belongs here — once, where all of them read it.
- **Which scan rules this domain holds itself to.** A convention is a judgement, and a repository that settled one differently is not defective there. Offer three answers and mean all three: keep them, drop a named few (`dev seams --retire <rule-id>`), or **drop the family outright** (`dev seams --retire-all`). Dropping the family is a legitimate answer given once here, rather than thirty retirements discovered one denial at a time.

Every one of these is also a `# lup: template:` marker in the catalog, so `dev todos` lists any left standing and Phase 4 meets them again. Answering here is what keeps that list from being the first time anyone sees the choice.

Each answer edits the declaration; **regenerate afterwards** with `uv run lup-devtools harness generate all`, because the compiled plugin trees are what the gates actually read.

## Phase 2: Rename Package

Run the devtool to rename the package. Preview first with `--dry-run`, then execute:

```bash
uv run lup-devtools dev init rename-package <project> --dry-run
uv run lup-devtools dev init rename-package <project>
```

This handles directory rename (`src/lup_template/` -> `src/<project>/`), import updates, pyproject.toml entry points, CLI app name, and the plugin marketplace name -- all in one shot. The marketplace registration in each tree (.claude/plugins/.claude-plugin/marketplace.json under Claude Code, .agents/plugins/marketplace.json under Codex) is named `<project>` so it doesn't collide in the global marketplace namespace, while the plugin entry stays `lup` (so `/lup:*` is identical everywhere). Framework vocabulary (`lup_tool`, `lup-devtools`, `.lup/`, etc.) is preserved automatically.

### After renaming:

#### 1. Root the scaffold branch at the base

`dev update` carries upstream's later changes to the copied half in as a git
merge, and a merge needs an ancestor both sides share: the `lup-scaffold`
branch, compiled from the copied half at the base and rooted once. Commit what
the rename and Phase 1.5 left first — the adoption is recorded as a merge, and
git refuses a merge over a staged change — then root it at the recorded
commit:

```bash
uv run lup-devtools dev scaffold adopt --base <commit>
```

Root it now, while the library is still vendored. Once the pin resolves to
the base itself, an adoption there reads as the mistake it usually is — a base
equal to the pin gives the first update nothing to carry — and is refused
unless its reading is restated, where here it is simply the truth. The command
measures the base against the copied half before writing anything, and
refuses one the measurement argues against with the reading that argues;
after an estimated base, that reading is the second opinion to show the user.

#### 2. Declare how the project obtains lup

The template ships the library vendored under `packages/lup/`, which makes the
project a fork of it. The rename is what allows leaving that mode: `dev library`
refuses to un-vendor while `src/lup_template/` is present, because an
uninitialized template and the lup repository are the same bytes and nothing
else separates them.

A project depends on lup as a package — the `lup-agents` distribution,
imported as `lup` — rather than keeping a copy of the library's source. Half
the answer is a fact to look up rather than a preference — whether a release
exists at all, and which:

```
uv run lup-devtools dev library release
```

It reports the released version, or that none is published yet, and prints the
command that declares what it found — so the release number is read from the
index rather than guessed at. A release candidate newer than the release is
named beside it and never offered in its place: a candidate is taken on
purpose, by naming it (`--version X.Y.ZrcN`, or `dev library git --tag
vX.Y.ZrcN`), and only when the user asks to try one.

The other half is a judgement about what this project is to lup, and the
look-up does not make it. Ask the user which of these describes them:

| Mode | The project it is for | Command |
| --- | --- | --- |
| published | A consumer of the library: it takes releases and upgrades on its own schedule | `uv run lup-devtools dev library use published --version <release>` |
| **git** | Either nothing is published yet, or the project works *on* lup as well as with it — running a branch to dogfood it and sending changes back | `uv run lup-devtools dev library git --branch <branch>` |

A project developing lup alongside its own work takes git mode as well, pinned
at the branch carrying its changes: the library then moves when a command moves
it, and `uv.lock` records the commit it moved to — which is what lets
`uv run lup-devtools dev update` hold the library, the generated trees and the copied half
at one upstream commit.

With nothing published, git is the only mode that resolves, so the look-up
settles it. Once a release exists, published is the quieter default and git
stays a live choice: a project that reads the library's own diffs, or that
expects to send work back, is better served by the branch it is improving than
by the last release cut from it. Both hand the project a real package, so
its `packages/lup/` stays absent and nothing has to be merged later. Vendoring
is not on this list — a vendored copy is a fork with all the reconciliation
that implies, and is only right for a project that genuinely intends to modify
library source.

The git mode resolves `subdirectory = "packages/lup"`, because the distribution sits inside the repository rather than at its root, and pins whichever ref you name. **The ref resolves against the remote, not against any checkout on disk**: uv fetches the branch as the remote has it, so work the remote has not seen is not in what you pinned. Before declaring a git source, read what the remote's branch actually resolves to — `uv run lup-devtools dev init base` names that tip — and if it is not the recorded commit, say so rather than pinning a dependency whose contents you have not accounted for.

The extras come from what the project runs: `claude` and/or `codex` for the
adapters it drives, `docker` for the code-execution sandbox, `web` for the
session API. Name them in the requirement (`lup-agents[claude,codex,docker]`).

The command prints the `uv sync` and the regeneration it wants next. Run both
before anything reads the project's types.

A branch that has moved past the base pins past it too: the lock resolves the
branch's tip, while the copied half is still the base's. Where `dev init base`
reported the branch ahead, Ask the user with the AskUserQuestion tool, offering concrete options plus a free-text choice: whether to pin the branch and carry the copied half across the commits it moved past the base with dev update now, or to pin the recorded commit as a revision and move later. Carrying it now is
`uv run lup-devtools dev update` after the sync, which merges upstream's
changes to the copied half between the base and the pin onto the branch rooted
above and regenerates under the library that landed. Starting exactly at the
base is `dev library git --rev <commit>` instead, and `dev update` whenever
the project is ready to move.

#### 3. Merge the guidance template into the guidance declaration

The merge lands in `src/<project>/harness/content/guidance.py`, never in a tree's guidance file (.claude/CLAUDE.md under Claude Code, AGENTS.md under Codex): those are generation's outputs, and an edit made directly to one is undone the next time the harness runs. Take the sections from that tree's template flavor (.claude/plugins/lup/TEMPLATE_CLAUDE.md under Claude Code, .codex/plugins/lup/TEMPLATE_AGENTS.md under Codex), covering every tree the project commits:

1. Read the template and replace `<project>` placeholders with the actual project name
2. Read the existing declaration
3. Use the `<!-- section: ... -->` markers in the template to identify independent merge units
4. Compare sections: for each marked section, check whether the declaration already composes it (by heading match)
5. Add missing sections to the declaration
6. Leave existing sections untouched -- don't overwrite content the project already has
7. Regenerate with `uv run lup-devtools harness generate all`, which is what carries the merged sections into every tree

The template is a menu, not a document to adopt whole. Guidance is loaded on
every turn and is held to a byte budget for a reason a reader never sees
otherwise: a runtime that caps how much project documentation it will load
stops adding at the cap, so an over-budget guidance file is not an error, it is
silent truncation. Generation enforces that ceiling and refuses the merged
declaration, naming the overage — so take the sections this domain will act on,
and leave the rest to the pages under `docs/` that already carry them.

Clearing the scaffold flag is also what hands this domain its room. While the
flag stood, the template was held to a *smaller* ceiling than the runtime's,
holding roughly 11.5 KiB back on purpose — so what you inherit is a deliberately
lean document with space to say what is true of this domain, not a full budget
already spent on somebody else's conventions. `dev guidance` reports what each
section costs, and after adoption only the runtime ceiling applies.

Which runtimes the project carries is not a choice made here: every tree
arrives with the clone, and generation writes each one it finds. Dropping a
runtime is a later removal somebody decides on its own terms.

#### 4. Initialize upstream sync

Baseline the upstream checkpoint at *the recorded commit*. The `lup` entry
`sync.json` ships already names lup's repository -- or, where the project
resolves lup from a repository, follows that pin -- so nothing has to be
registered first. Fetch it, which clones it under `~/.cache/lup/sync/lup.git`
the first time, and record the exact commit already consumed:

```
uv run lup-devtools sync fetch lup
uv run lup-devtools sync mark-synced lup --at <commit>
```

Review reads the fetched upstream ref, so work anybody does in that clone
stays out of it, and the checkpoint is shared by all worktrees of this
consuming repository. The ref is the pinned branch where there is one and
the repository's default branch otherwise. To review another, or to use a
checkout this machine already keeps instead of the clone, register it:
`uv run lup-devtools sync setup lup <lup-checkout> --branch <branch>`
-- `--synced` there is right only when that ref itself is exactly the commit
already consumed.

A project that already consumed the library, and knows which commit it took, names it rather than moving a checkout to stand on it:

```
uv run lup-devtools sync mark-synced lup --at <commit>
```

That is the case an adoption mid-stream is always in — the code is already here, and what is missing is only the record of how far it reached. Without the commit, marking synced claims every commit that landed afterward as reviewed, which is the one thing the checkpoint exists to prevent.
Never register this project's own checkout as lup's: its history is the
project's — a clone holds the recorded commit as well, a generated repository
only its own root — and the review would read it as upstream work. And take
the checkpoint at the recorded commit, never at the branch's tip: the branch
may have advanced since this project was generated, and a checkpoint at its
tip marks the commits in between as already reviewed when the project does
not carry them.

#### 5. Verify

```bash
uv sync
uv run pyright
uv run ruff check .
uv run pytest
<project> --help
```

## Phase 2.5: Settle the Seams

Everything above customizes what the project *is*. This phase settles what it
holds *itself* to, and it exists because a default nobody was shown is not a
decision. The library ships each of these as a starting point, and a domain
that would have answered differently never finds out it could until an edit is
denied by a rule it never agreed to.

Put each one to the user rather than letting the default stand by silence.

### 1. Which rules this domain holds itself to

`RuleSelection` names which of the rules the library ships this project keeps.
Its own docstring is the point: a rule is a convention written down and a
convention is a judgement, so a repository that settled one differently "is not
answering it wrongly — it is answering a question this library had no standing
to close." The selection is subtractive, so a project that disagrees with three
rules names those three rather than restating the thirty it keeps.

Show the families before asking — `uv run lup-devtools dev rules` writes the
generated index, and every rule in it carries the shape it matches and the
diagnostic it prints. Then Ask the user with the AskUserQuestion tool, offering concrete options plus a free-text choice: which rule families this domain keeps, with retiring the anti-pattern family altogether as one answer. Offer the whole-family answer explicitly: a domain that does not want the
anti-pattern rules should be able to say so once, here, rather than retire
thirty ids one at a time as it meets them.

### 2. Who owns which files

A human-owned file surfaces every agent edit as an approval, so the agent
proposes rather than writes. The template ships `README.md` that way, which is
right for a file whose words are the author's and wrong for a project that
wants its README kept current by the agent.

Ask the user with the AskUserQuestion tool, offering concrete options plus a free-text choice: which files the human author owns, starting from whether README.md stays human-owned — then apply the answer with `uv run lup-devtools dev seams --own <path>`
or `--disown <path>`, which rewrites the declaration for the regeneration below to
compile. Never hand-edit `human_owned_files` in the catalog.

### 3. What each path role means here

`HookPathRole` says which roots are scratch, which are source, and which are
tests. The template's roles describe the template's own tree, and a domain that
keeps its data somewhere else, or vendors a dependency, has roots the shipped
list does not mention. Read the declared roles, then Ask the user with the AskUserQuestion tool, offering concrete options plus a free-text choice: whether any root this domain adds needs a path role, and which.

### 4. Whether tests are held still

An **acceptance guard** asks an ordinary session before it edits a test and
refuses an autonomous one outright, because for an autonomous worker those
tests are the specification it implements against. It is worth having exactly
when this domain will run unattended sessions against a test suite it must not
rewrite, and worth skipping when it will not. Ask the user with the AskUserQuestion tool, offering concrete options plus a free-text choice: whether this domain declares an acceptance guard over its test roots

Record each answer in the catalog, then regenerate and confirm the trees moved:

```bash
uv run lup-devtools harness generate all
```

An answer left at the default is fine — but it should be an answer, not a
silence. Where the user defers one, say which default now stands.

## Phase 3: Generate Scaffolding

**Start by gathering every customization point.** Each decision the template leaves to a domain carries a `# lup: template:` marker with a one-line description of the decision. Collect them all:

```bash
uv run lup-devtools dev todos --json
```

Walk the collected decision points one by one — each entry gives the file, line, decision text, and surrounding context. For every marker, either customize the code it points at and remove the marker, or delete it along with whatever Phase 1.5 declined. The numbered steps below give domain guidance for the major ones, but the gathered list is the source of truth: a marker you never reach is a decision silently defaulted.

Based on the answers from Phase 1, generate or modify:

### 1. `src/<project>/agent/models.py`

Customize AgentOutput for the domain:

- Add domain-specific fields (probability, move, response, etc.)

### 2. `src/<project>/agent/prompts.py`

Update the system prompt template for the domain. Focus on what the agent does and how to reason -- tools self-document via their descriptions, so listing them in the prompt creates a second source of truth that drifts as tools change.

### 3. `src/<project>/agent/subagents.py`

Create domain-appropriate subagents (researcher, analyzer, etc.)

### 4. `src/<project>/environment/cli/__main__.py`

Customize the CLI for the domain's task format:

- Update the `loop` command to accept domain-specific task inputs
- Customize `_commit_results()` message format (e.g., `data(forecasts):` instead of `data(sessions):`)
- Configure auto-commit behavior: enable/disable by default, target branch (main for data-only commits, or a dedicated branch) — requires the `notes/` ignore lines removed in Phase 1.5
- Add domain-specific CLI commands if needed

### 5. Agent Version

Set `agent_version` under `[tool.lup]` in `pyproject.toml` and explain bump rules for this domain.

### 6. Reflection (only if the `reflection` module was taken in Phase 1.5)

If this domain has no consequential, judgment-bearing output, `reflection` is in `DECLINED` and its tool group never reaches a session — skip this step. Otherwise customize `src/<project>/agent/tools/reflect.py`:

- Extend `ReflectInput` with domain-specific fields (factor analysis, move evaluation, etc.)
- Customize the reviewer prompt for the domain's common failure modes
- The reviewer runs on the strongest aux model available (see the guidance file's § Model Selection); pass `skip_reviewer=True` per call for speed-sensitive or trivial tasks

The reflection gate (`lup.orchestration.reflection`) is domain-neutral and doesn't need modification. Only the tool and its input model are domain-specific.

### 7. Update the guidance

Edit `src/<project>/harness/content/guidance.py`, then regenerate with `uv run lup-devtools harness generate all` -- .claude/CLAUDE.md under Claude Code, AGENTS.md under Codex are its outputs, and editing them directly is undone by the next generation.

The guidance should already carry the template sections from the Phase 2 merge. Now add domain-specific content based on the interview answers:

- Fill in the Project Overview placeholder with the domain description
- Add domain-specific commands and examples
- Add metrics and feedback collection instructions relevant to this domain
- Add any domain-specific context sections (Important Context, data sources, constraints)

### 8. Tool Description Standards

The agent discovers tools through their descriptions -- a terse description means the agent can't tell when or why to use it. Each description should answer:

1. **What** -- What does this tool do? (concrete behavior, not vague summary)
2. **When** -- When should the agent reach for this tool? (triggers, conditions)
3. **Why** -- Why does this tool exist? (what problem it solves, what gap it fills)

See `src/<project>/agent/tools/example.py` for the pattern.

### Setup Wizard (`src/<project>/devtools/setup.py`), where `setup` was taken

Customize the interactive setup wizard for the domain's integrations:

- Replace the template integrations (Slack, Google, Notion, Example API) with the domain's actual services
- Update the `INTEGRATIONS` list — each entry is an `Integration(name, env_keys, setup_func, status_func)`
- Add corresponding `@app.command()` subcommands for individual integration setup
- Update env var names in `config.py` to match what the setup wizard writes to `.env.local`
- Verify the setup page (`lup-devtools setup serve`, which the dashboard shows as this repository's setup pane) exposes the same registry: declarative fields become browser forms, while bespoke flows link back to their CLI command

The framework (env helpers, status table, mask, clipboard, browser open, wizard flow) is reusable — only the integration functions and registry need customization.

The registry is a list of services, and which ones this domain has is the
domain's answer rather than a guess from the code — so Ask the user with the AskUserQuestion tool, offering concrete options plus a free-text choice: which external services the agent uses, and for each whether it authenticates by OAuth flow, API key, or a credentials file before rewriting `INTEGRATIONS`.

### The feedback loop, where `feedback-loop` was taken in Phase 1.5

The feedback collection module is `devtools/feedback/state.py`, exposed via
`uv run lup-devtools feedback collect`. Customize `load_outcomes()` and
`compute_metrics()` for the domain's ground truth type.

Then customize the feedback loop command for the domain's ground truth type,
the metrics to analyze and the trace inspection approach, and verify that it
references the right scripts.

## Phase 4: Verify Setup

After generating files:

1. Run `uv run lup-devtools dev todos` -- any remaining `# lup: template:` marker is a decision not yet made; resolve or consciously defer each one. Resolving one means writing this domain's code where the placeholder stood and deleting the marker: it is not feedback, so it takes no `solved:` claim. Renaming the package cleared `[tool.lup] template`, so from here on `dev check` lists every marker still standing -- park one you mean to leave with `# lup: defer:` rather than letting it sit unexplained
2. Run the pre-flight bar, which is ruff, pyright and the suite in one pass and
   reports as it goes:

Start a `Monitor` over `uv run lup-devtools dev check`. Each line it emits arrives as an event, and the watch ends when the command does. Do not run it through `Bash`, whose long timeout returns once at the end, and do not read a backgrounded session on a loop — both are polling, however patient. A watch that outlives your report wakes you after you have finished, so stop it with `TaskStop` before reporting unless the command has exited

3. Run `uv run lup --help` to verify CLI
4. Declare the domain's external programs in `harness/content/requirements.py`
   using `Requirement`: name their purpose, execution location, smallest real
   operation, failure consequence, and recovery. Run
   `uv run lup-devtools harness requirements` to exercise host prerequisites,
   including the disposable Python sandbox. A daemon answering is not proof
   that an expression evaluates. For container sessions, also run
   `uv run lup-devtools harness requirements --inside --launch-only`; full
   `--inside` checks additionally exercise a native model turn and require its
   configured login. Report any unexercised checks explicitly.
5. Repair authorized local prerequisites and rerun their checks. Where the
   report names an endpoint override, missing service, group membership, or
   session restart, give that specific recovery; do not call setup complete
   merely because the executable exists. Host administration and a fresh
   login session remain actions for the operator when this session cannot
   perform them.
6. Regenerate both harnesses and check that the rendered guidance accurately describes the domain

## After Initialization

Once the scaffolding is generated, guide the user to:

1. Run a few sessions: `uv run lup loop "task1" "task2"`
2. Review traces in `notes/traces/`
3. Use `/lup:feedback-loop` to analyze and improve
4. Iterate on the feedback collection as patterns emerge

## Key Files to Customize

`docs/template.md` answers this from the checkout rather than from a list that
has to be maintained: it draws the package as it actually stands, captions each
module with its own docstring, and carries a table of what to adapt in each.

The order they usually get touched in: `agent/models.py` for the result the
domain produces, `agent/prompts.py` for what the agent is told, then
`agent/toolsets.py` and `agent/tools/` for what it can do.
