---
name: install
description: Install lup plugin and scaffolding into a target repo
---

# Install Lup into Target Repo

Install the lup plugin, hooks, and useful scaffolding into an existing repository. Unlike `$lup:init` (which customizes the template for a new domain), this command ports lup capabilities into a repo that already has its own structure and conventions.

## Your Task

**Arguments provided**: the arguments supplied with this skill invocation

### Parse Arguments

- **target-repo**: Path to the target repository (default: `..`). Resolve relative paths from the current working directory.
- **--interactive**: If present, put each porting decision to the user as a choice. If absent, be conservative — modify as few files as possible.

### The source repository is read-only

You are running *inside* the source. Every write this command makes belongs to
the target, and nothing it does may change the checkout it runs from — not a
file, not a git ref, and not a piece of local state.

The failure is quiet rather than loud, which is why it is stated here: a
`lup-devtools` command run without a working directory acts on the current one,
so it lands in the source and looks like it worked. Two habits prevent it:

- Give every command the target explicitly — `git -C <target> …`, and
  `uv run --directory <target> lup-devtools …` for anything that reads or
  writes its state. `--directory` is the flag that changes the working
  directory; `--project` only discovers a manifest and leaves you in the source.
- Write files by absolute path under the target, never by a path relative to
  where you are standing.

Before reporting success, run `git -C <source> status --short` and confirm it
is clean. If the source changed, say so in the report rather than reverting
silently — something wrote where it should not have, and which command did it
is the useful part.

That rule is about *accidental* writes — a command that acted on the wrong
working directory. It does not forbid a deliberate contribution back. When the
target's fork carries code that passes the library placement test — would
another project built on lup want this? — folding it into the source is the
correct outcome rather than a violation. Raise it as a decision; once the user
approves, the source is writable for exactly that change, and the report names
the commits you made to it alongside the ones you made to the target.

### The source branch is part of what you install

Every phase below reads the source checkout as it stands, so the branch you are
standing on *is* the release you are about to install — a feature branch ports
unmerged work, and nothing downstream announces that. Resolve both before
Phase 1:

- `git -C <source> rev-parse --abbrev-ref HEAD` — the branch the library would come from
- `git -C <source> ls-remote --symref origin HEAD` — what the remote treats as stable, asked of the remote itself, since a bare clone keeps no `refs/remotes/origin/HEAD` to read it from

When they differ, Ask the user directly, offering concrete options, and wait for the answer: whether to proceed from the checkout's current branch, which carries work the stable branch has not reviewed, or from the stable branch instead

Record the branch and the commit the answer settles on. Everything below is
about that commit — the acquisition mode pins its branch and the upstream
checkpoint is taken at it — so the checkout supplying the library has to be
standing there before you go on.

Every later phase reads this checkout, and step 9 baselines the target's sync
checkpoint at its HEAD. Nothing here may move it, so if the answer was the
stable branch it is standing on the wrong one: stop and say so, and let the
work be re-run from a checkout of that branch rather than installing one branch
while recording another.

If `the arguments supplied with this skill invocation` is empty, use defaults: target=`..`, non-interactive.

## Phase 1: Analyze Source Repo (Lup Template)

Inventory what the lup plugin offers. Read these key files in the **current** repo (`.`):

### Plugin Structure

One declaration set renders into every harness tree the repo commits, so each
capability below exists once per tree:

| Capability | Where it lands |
| --- | --- |
| Plugin identity | .claude/plugins/lup/.claude-plugin/plugin.json under Claude Code, .codex/plugins/lup/.codex-plugin/plugin.json under Codex |
| Hook definitions, dispatcher, and hermetic policy runtime | .claude/plugins/lup/hooks/ under Claude Code, .codex/plugins/lup/hooks/ under Codex |
| Skills | .claude/plugins/lup/commands/*.md under Claude Code, .codex/plugins/lup/skills/*/SKILL.md under Codex |
| Agents | .claude/plugins/lup/agents/*.md under Claude Code, .codex/agents/*.toml under Codex |
| Guidance template | .claude/plugins/lup/TEMPLATE_CLAUDE.md under Claude Code, .codex/plugins/lup/TEMPLATE_AGENTS.md under Codex |

### Reusable Library Code

- `packages/lup/src/lup/` — utilities (trace, hooks, metrics, mcp, retry, notes, history, paths)
- `lup.workspace.paths.agent_version()` — version tracking pattern (reads `[tool.lup] agent_version` from pyproject.toml)

### How the Target Obtains Lup

A project depends on lup as a package — the `lup-agents` distribution,
imported as `lup` — rather than keeping a copy of the library's source. Half
the answer is a fact to look up rather than a preference — whether a release
exists at all, and which:

```
uv run --directory <target> lup-devtools dev library release
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
| published | A consumer of the library: it takes releases and upgrades on its own schedule | `uv run --directory <target> lup-devtools dev library use published --version <release>` |
| **git** | Either nothing is published yet, or the project works *on* lup as well as with it — running a branch to dogfood it and sending changes back | `uv run --directory <target> lup-devtools dev library git --branch <branch>` |

A project developing lup alongside its own work takes git mode as well, pinned
at the branch carrying its changes: the library then moves when a command moves
it, and `uv.lock` records the commit it moved to — which is what lets
`uv run --directory <target> lup-devtools dev update` hold the library, the generated trees and the copied half
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

The git mode resolves `subdirectory = "packages/lup"`, because the distribution sits inside the repository rather than at its root, and pins whichever ref you name. **The ref resolves against the remote, not against any checkout on disk**: uv fetches the branch as the remote has it, so work the remote has not seen is not in what you pinned. Before declaring a git source, read what the remote's branch actually resolves to — `git -C <source> ls-remote origin <branch>` names that tip — and if it is not the recorded commit, say so rather than pinning a dependency whose contents you have not accounted for.

The extras come from what the project runs: `claude` and/or `codex` for the
adapters it drives, `docker` for the code-execution sandbox, `web` for the
session API. Name them in the requirement (`lup-agents[claude,codex,docker]`).

### DevTools CLI

The `lup-devtools` CLI (`src/lup_template/devtools/`) gives the meta-agent structured commands for development tasks that would otherwise require ad-hoc bash one-liners. Without it, an agent resorts to `python -c "..."` snippets or manual shell pipelines for trace analysis, feedback collection, and session management — which are fragile and unrepeatable. The devtools encode these workflows as proper CLI commands with argument parsing, output formatting, and error handling.

- `src/lup_template/devtools/main.py` — root typer app composing sub-apps (entry point: `lup-devtools`)
  - `conversation` — Retain authenticated AI conversations
  - `coordination` — Reach the other sessions working in this repository
  - `dashboard` — Serve the operator's page over the parked reviews
  - `ledger` — Read and preserve the notes this repository has recorded
  - `dev` — Read this repository, and hold it to what it settled
  - `feedback` — Feedback state, metrics, and commits
  - `git` — Branches, worktrees, pull requests, and conflicts
  - `harness` — Generate and launch the native harnesses
  - `resolve` — Drive a resolver run, and watch or answer it
  - `review` — The parked reviews an operator answers, and what each waits on
  - `run` — Follow work that outlives its tool call
  - `setup` — Interactive setup wizard, and its page
  - `sync` — Stay in step with upstream: tracked repos, and what they owe
  - `tools` — Serve the MCP servers a launched session declares
  - `trace` — What a session left behind: its trace and its records
  - `version` — Agent version, changelog, and bump

### Configuration Patterns

- .claude/settings.json under Claude Code, .codex/config.toml under Codex — each tree's project configuration
- `sync.json` — registry of repos tracked for sync

Build a mental inventory of **portable capabilities** organized by category:

1. **Plugin infrastructure**: hooks.json, hook scripts, plugin.json structure
2. **Permission hooks**: auto-allow patterns for shell commands, edits, and network fetches; pre-push quality gates; test protection
3. **Slash commands**: which ones are generic (commit, rebase, close, land, meta, merge, debug, refactor) vs lup-specific (init, feedback-loop, bump, update)
4. **Library utilities**: print_block, TraceLogger, version tracking, retry decorator, cache, hook composition
5. **Guidance patterns**: coding standards, git workflow, editing style, debugging philosophy
6. **DevTools patterns**: CLI structure, sync tracking

## Phase 2: Analyze Target Repo

### First: does the target already have lup?

Installing is one of two jobs this command does. The other is bringing a
target that already has lup up to date, and the two look nothing alike — so
decide which before reading anything else. Run
`uv run --directory <target> lup-devtools dev library status`; where that command
does not exist, look for a `lup-agents` dependency in the target's
`pyproject.toml`, for a vendored `packages/lup/`, and — the case both of those
miss — for a `lup-devtools` entry point or lup-shaped module names with no
`lup-agents` dependency standing behind them.

| What the target has | Do this instead of installing |
| --- | --- |
| Nothing | Continue with the phases below — this is a first install. |
| A `lup-agents` dependency resolved from an index, a repository, or a checkout | Nothing to port. Move the release it resolves forward, regenerate its harness, and report. The library arrives as a package; only the target's own declarations are its business. |
| A vendored `packages/lup/` copy | Do **not** overwrite it. Port the upstream commits through $lup:update, which reviews them one at a time against a tree that has diverged on purpose. Read the fork the other way too, before porting anything: run the library placement test over what it *added*, because a downstream that solved a framework problem is holding library code, and that folds back into lup rather than staying forked forever. Then offer to end the fork, saying plainly that it is one-way and worth reviewing: pick the acquisition mode by the § How the Target Obtains Lup table below rather than defaulting to a vendored copy, which is what the fork already is. |
| An **absorbed fork**: lup-shaped modules under the target's own package, no `packages/lup/`, no `lup-agents` dependency, and usually a plugin renamed to the target, so its skills answer to the target's own prefix rather than lup's | The fork boundary was erased by the rename, so there is nothing to update through and no diff to read. Reconstruct the boundary before touching anything: map each forked module to the lup module that now owns it, and report that mapping with line counts as the deletion set, because those copies drifted in place for however long the fork ran, which makes a swap a behavior change rather than a move. Never delete on the strength of a name match. That same rename is what makes coexistence safe — a skill under the target's own prefix cannot collide with one under $lup:* — so installing beside the fork and retiring it piecewise is available, and is the better first move whenever the target is something that has to keep working. Say plainly that two plugins means two hook sets classifying the same command, and settle which one decides before installing. |
| An old install with no sync baseline | Baseline it first (step 9 below, against the target), so the next review lists commits rather than the entire history. |

A target that already has lup is the common case after the first year, and
overwriting its tree is the one outcome worth ruling out: its declarations
have diverged on purpose, and they are what generation reads.

### Then: read its structure

1. **Top-level layout**: `ls` the root, look for `src/`, `lib/`, `tests/`, any harness tree, `package.json`, `pyproject.toml`, `Cargo.toml`, etc.
2. **Language and ecosystem**: Python/Node/Rust/Go/etc? Package manager? Build tools?
3. **Existing harness setup**: Does it have a guidance file? Project configuration? Any plugins already?
4. **Existing hooks**: Any permission hooks already wired? What patterns do they use?
5. **Existing commands**: Any slash commands already defined?
6. **Git workflow**: How does the repo handle branches, PRs, commits?
7. **Code conventions**: What patterns does the repo follow? Type checking? Linting?

**Key questions to answer:**

- What language/ecosystem is the target? (This determines which library utilities are portable)
- Does it already have a harness tree we'd be extending vs. creating from scratch?
- What existing conventions must be respected?

## Phase 3: Find the Overlap

Based on the analysis, classify each lup capability as:

### Always Portable (language-agnostic)

These work in any repo:

- **Plugin infrastructure**: The plugin directory itself — .claude/plugins/lup/ under Claude Code, .codex/plugins/lup/ under Codex
- **Permission policy**: configure URL scopes, protected roots, and the target's own shell vocabulary in the canonical `HookSet`; change semantic decisions in `lup.policy`, then regenerate
- **Pre-push quality gates**: Adapt to target's linter/type-checker/test runner
- **Generic commands**: commit, rebase, close, land, meta, debug, refactor, add-command, modify-command, merge, principle, review, create-investigator
- **Guidance patterns**: Git workflow, editing style, asking questions, debugging philosophy
- **Settings patterns**: the permission structure in each tree's project configuration

### Portable if Python

These port well to other Python projects:

- **Library utilities**: hook composition, version tracking, retry, cache
- **DevTools CLI**: The `lup-devtools` typer app structure — `main.py` composing sub-apps, `pyproject.toml` entry point. Even if the target doesn't need every subcommand, the skeleton (dev, py, sync, usage, version) gives the meta-agent reliable tooling instead of ad-hoc scripts.
- **Upstream sync**: sync.json + sync commands (`lup-devtools sync`)

### Portable if SDK agent

If the target repo builds (or will build) a tool-using SDK agent, the **self-improvement loop scaffolding** is the core value of lup — these are high-priority to port:

- **Agent scaffolding**: core.py pattern (orchestration), subagents.py, models.py (structured output), prompts.py, tool_policy.py, config.py (pydantic-settings)
- **Feedback loop**: feedback collection, trace analysis, metrics aggregation, scoring CSV
- **Session management**: CLI with `run` + `loop` commands, auto-commit, session storage
- **DevTools**: The full `lup-devtools` CLI (`conversation`, `coordination`, `dashboard`, `ledger`, `dev`, `feedback`, `git`, `harness`, `resolve`, `review`, `run`, `setup`, `sync`, `tools`, `trace`, `version`)
- **Version tracking**: `[tool.lup] agent_version` in pyproject.toml + `lup-devtools version bump` for tracking agent behavior changes
- **Commands**: `init`, `feedback-loop`, `bump`, `update` — the self-improvement workflow
- **Template guidance**: Section-level merge into each guidance file the target carries, from its matching template flavor (add missing sections, leave existing ones)

When the target has SDK agent code, adapt the scaffolding to wrap their existing agent — don't replace it. The lup patterns (trace logging, scoring, feedback collection) layer on top of whatever agent they already have.

These patterns are **opt-in, not a bundle**: `reflection`, `realtime` and `feedback-loop` are modules the target names in `DECLINED` or leaves to their defaults, and the commit loop is one file's wiring (see the guidance file's § Scaffolding Is a Menu, Not a Mandate). Don't install a pattern the target won't use — dead scaffolding is worse than a capability you can add later.

### Skip (never port)

- **`examples/`, and the test modules driving it.** These demonstrate lup's own runtime composition against lup's own README — a front door being opened, a wrapper stack, a policy denying the call it declared. The target is a consumer of that library rather than a demonstrator of it, so porting them installs a directory it will never run and a suite it has to keep green. When the target asks how a capability is composed, point at the source checkout's copy instead of copying it in.
- Domain-specific tool implementations (example.py contents)
- Domain-specific prompt content
- Domain-specific model fields (but port the pattern/structure)

## Phase 4: Decide Plugin Strategy

lup renders one declaration set into every harness tree, so first settle which
trees the target carries: Ask the user directly, offering concrete options, and wait for the answer: which harness trees the target should carry — one runtime's, or every runtime it uses In non-interactive mode, install the trees the target already shows evidence
of, and every tree when it shows none.

Then determine **where** the plugin goes. Two options:

### Option A: Install lup as its own plugin (preferred)

Create the plugin directory in each selected tree (.claude/plugins/lup/ under Claude Code, .codex/plugins/lup/ under Codex) as a standalone local plugin in the target repo. Per tree, this requires:

1. **Plugin manifest** (.claude/plugins/lup/.claude-plugin/plugin.json under Claude Code, .codex/plugins/lup/.codex-plugin/plugin.json under Codex) — the plugin entry's `name` stays `lup`, so every skill keeps its `lup:` qualifier.
2. **Marketplace registration** (.claude/plugins/.claude-plugin/marketplace.json under Claude Code, .agents/plugins/marketplace.json under Codex) with a **project-unique** `name` (use the target's package/repo name, e.g. `myproject`), listing the `lup` plugin:
   ```json
   { "name": "myproject", "plugins": [{ "name": "lup", "source": "./lup" }] }
   ```
3. **Register + enable** it in that tree's project configuration (.claude/settings.json under Claude Code, .codex/config.toml under Codex) under the same unique name. Each runtime spells this differently — read
   this repo's own configuration for the worked example rather than reciting one
   from memory. On a Python target where `lup-devtools` is installed,
   `lup-devtools dev plugin name` does steps 2–3 automatically (default name:
   the target's `[project].name`).

**Never name the marketplace `lup` or `local`.** Marketplace names share one global namespace per runtime, so a shared name collides across every repo that registers it — an install from one repo silently shadows the others. The plugin entry stays `lup`; only the *marketplace* is named per-project.

### Option B: Merge into an existing plugin

If the target already has a local plugin of its own, offer to merge lup's hooks and skills into it. This avoids a second plugin but mixes namespaces. Only do this in interactive mode when the user explicitly chooses it.

**In non-interactive mode, always use Option A.**

When the target already has other local plugins, lup installs alongside them under its own project-named marketplace — no conflict.

## Phase 5: Decide What to Install

Use your judgment based on what you found in Phases 1-3. The analysis should drive the decisions — don't follow a fixed checklist.

### Non-Interactive Mode (default)

Be conservative — only install what clearly adds value. Typical candidates (but decide based on the actual target):

- **Plugin infrastructure**: plugin.json, hooks.json, settings.json (project-named marketplace + plugin enablement)
- **Permission hooks** adapted to the target's ecosystem (its build tool, test runner, linter, doc URLs)
- **Generic skills** that work in any repo (git workflow, guidance maintenance, meta, refactor, etc.)
- **Guidance file**: Perform a **section-level merge** from each tree's template flavor (.claude/plugins/lup/TEMPLATE_CLAUDE.md under Claude Code, .codex/plugins/lup/TEMPLATE_AGENTS.md under Codex) into that tree's guidance file (.claude/CLAUDE.md under Claude Code, AGENTS.md under Codex). Read the template, use the `<!-- section: ... -->` markers to identify independent merge units, adapt for the target's project name and ecosystem, then compare marked sections against the target's existing guidance file. Add sections that are missing; leave existing sections untouched. If no guidance file exists, create one from the adapted template. The template is a menu rather than a document to adopt whole: guidance is loaded on every turn and held to a byte budget, because a runtime that caps how much project documentation it loads stops adding at the cap, so an over-budget file is silently truncated rather than reported. Take the sections the target will act on, and leave the rest to the `docs/` pages that carry them.
- **If SDK agent detected**: Also install the self-improvement scaffolding — this is lup's core value. The feedback loop commands, lib utilities (trace, scoring, metrics, hooks, version), devtools CLI pattern, session/trace directory structure, sync.json for upstream sync. Adapt to layer on top of the target's existing agent, not replace it.

**Constraints** in non-interactive mode:

- Don't rewrite the target's existing guidance content or change existing hooks and skills
- Don't install anything requiring new dependencies (suggest them in the report)
- Don't modify existing source code files (only create new files)

### Interactive Mode

Ask at decision points where the user's input matters — don't ask about things you can decide confidently from the analysis, and don't enumerate every file individually.

Group decisions at meaningful levels. Examples of the _kinds_ of things to surface:

- Plugin strategy (own plugin vs merge) when the target already has plugins
- Which capability categories to install when the target could use some but not all
- How to handle the target's guidance file when it already states different conventions
- Whether/how to port SDK agent scaffolding when the target has existing agent code
- Whether to restructure existing code to fit lup patterns or layer on top
- Offering rewrites or retemplating of existing files when they'd benefit from lup patterns

What questions to ask — and how many — depends entirely on what you found. A bare repo with no harness tree needs fewer questions than one with an established plugin ecosystem.

## Phase 6: Execute Installation

For each item being installed:

1. **Read the source file** from the current repo
2. **Adapt it** for the target:
   - Replace `uv run` with target's equivalent (npm run, cargo, make, etc.) in hooks and commands
   - Adjust file path patterns in hooks for target's directory structure
   - Keep command markdown structure but update tool references and examples
   - Walk the `# lup: template:` markers in ported scaffolding files — each marks a domain decision with a one-line description. Adapt the code a marker points at to the target (and remove the marker), or leave it in place as an open decision the target gathers later with `uv run lup-devtools dev todos`
3. **Write to the target repo** — create directories as needed
4. **Never overwrite** existing files without asking (even in non-interactive mode, warn and skip)

### Renaming Rules

**Only Python import paths change** to match the target's package name. All framework vocabulary stays as `lup`:

| Changes (adapt to target package) | Stays as `lup` (framework identity) |
|---|---|
| `from lup_template.*` → `from <target>.*` imports | `lup-devtools` CLI entry point name |
| `src/lup_template/` → `src/<target>/` paths | `@lup_tool(...)`, `LupMcpTool` |
| `pyproject.toml` package name | the plugin directory in each tree |
| marketplace `name` (marketplace.json) → `<target>` | plugin entry `name`: `lup` (so `$lup:*` is stable) |
| Main CLI entry point name | `.lup/` state directory |
| Logger module paths | `lup-tools`, `lup-sandbox-*`, `lup-mcp-*` |
| | Naming convention ("Lup" = inner agent) |

### Installation Order

Steps 1-4, 6 and 7 repeat per selected tree; step 5 is tree-independent.

1. Plugin manifest — .claude/plugins/lup/.claude-plugin/plugin.json under Claude Code, .codex/plugins/lup/.codex-plugin/plugin.json under Codex
2. Hook definitions and adapted hook scripts — .claude/plugins/lup/hooks/ under Claude Code, .codex/plugins/lup/hooks/ under Codex (only reference hooks being installed)
3. Marketplace registration — .claude/plugins/.claude-plugin/marketplace.json under Claude Code, .agents/plugins/marketplace.json under Codex
4. Selected skills — .claude/plugins/lup/commands/ under Claude Code, .codex/plugins/lup/skills/ under Codex
5. `src/<project>/devtools/` — devtools CLI skeleton (if Python target, adapt import paths but keep `lup-devtools` as the CLI entry point name), and the `lup-agents` requirement itself in the target's `pyproject.toml`, declared through the mode § How the Target Obtains Lup settled on
6. Project configuration — .claude/settings.json under Claude Code, .codex/config.toml under Codex — create or merge
7. Guidance file — .claude/CLAUDE.md under Claude Code, AGENTS.md under Codex — section-level merge from that tree's template flavor (read template → use `<!-- section: ... -->` markers to identify merge units → adapt for target → compare sections → add missing ones → leave existing untouched)
8. **Hand off to generation**: everything written in steps 1-4, 6 and 7 becomes a generated artifact once the target's harness runs. From here on, the target edits its declarations under `src/<project>/harness/content/` and regenerates with `uv run lup-devtools harness generate all`; the installed files are outputs, and a hand edit to one is reverted the next time generation runs. Say so explicitly in the Phase 7 report.
9. **Initialize upstream sync**, which comes last because it records what the previous eight steps installed:
Baseline the upstream checkpoint at *the recorded commit*. The `lup` entry
`sync.json` ships already names lup's repository -- or, where the project
resolves lup from a repository, follows that pin -- so nothing has to be
registered first. Fetch it, which clones it under `~/.cache/lup/sync/lup.git`
the first time, and record the exact commit already consumed:

```
uv run --directory <target> lup-devtools sync fetch lup
uv run --directory <target> lup-devtools sync mark-synced lup --at <commit>
```

Review reads the fetched upstream ref, so work anybody does in that clone
stays out of it, and the checkpoint is shared by all worktrees of this
consuming repository. The ref is the pinned branch where there is one and
the repository's default branch otherwise. To review another, or to use a
checkout this machine already keeps instead of the clone, register it:
`uv run --directory <target> lup-devtools sync setup lup <source> --branch <branch>`
-- `--synced` there is right only when that ref itself is exactly the commit
already consumed.

A project that already consumed the library, and knows which commit it took, names it rather than moving a checkout to stand on it:

```
uv run --directory <target> lup-devtools sync mark-synced lup --at <commit>
```

That is the case an adoption mid-stream is always in — the code is already here, and what is missing is only the record of how far it reached. Without the commit, marking synced claims every commit that landed afterward as reviewed, which is the one thing the checkpoint exists to prevent.
### Settle the seams the target inherits

Everything installed above ships at a default, and a handful of those defaults are places lup holds an opinion the target is meant to overrule. **A default nobody was shown is not a decision** — and it matters more here than in a fresh scaffold, because the target already has conventions of its own, which is exactly what § Guidelines means by respecting them.

Run `uv run --directory <target> lup-devtools dev seams`. It prints each seam, what it holds, and where it is written. Put each to the user:

- **Who owns which files.** A human-owned file surfaces every change as an approval and the agent proposes rather than writes it. Ask specifically about `README.md`: a target whose README is maintained by hand wants it owned, and one that wants the agent writing it says so with `dev seams --disown README.md`.
- **Which trees an edit needs approval into.** Ask what this target actually guards — a migration set, a deployment manifest, a data directory, a generated client — rather than carrying over paths that describe lup's own tree.
- **What each tree is for.** Its test roots, its build products, its scratch. Every gate reads this one answer, so a target whose layout differs and never says so is judged by lup's layout instead of its own.
- **Which scan rules it holds itself to.** The one most likely to be wrong by default: the rules encode conventions this project settled, and a target that settled one differently is not defective there. Offer keeping them, dropping named ones (`dev seams --retire <rule-id>`), or dropping the family outright (`dev seams --retire-all`), and mean all three. `docs/rules.md` in the target lists what each id refuses, so read it with them rather than asking about thirty ids blind.

Then regenerate: `uv run --directory <target> lup-devtools harness generate all`.

## Phase 7: Verify & Report

After installation:

Run `uv run --directory <target> lup-devtools harness requirements` against
the target's declared external programs. Adapt its requirement manifest to
the programs the target actually uses, including a real operation and
recovery for each. The setup-only Python sandbox check creates a disposable
container and evaluates through its REPL; daemon access alone is insufficient.
For container sessions, also run
`uv run --directory <target> lup-devtools harness requirements --inside --launch-only`.
Full `--inside` checks include a native model turn with the configured login;
name any checks that remain unexercised instead of reporting them as working.
Repair what the installation authorization permits, rerun the affected
checks, and report exact operator actions for host package installation,
service activation, endpoint corrections, or a fresh session after group
membership changes. A binary's presence is not a successful verification.

1. **List all files created/modified** in the target repo, marking which ones generation now owns
2. **Show a summary** of what was installed and why
3. **Note what was skipped** and why (especially in non-interactive mode)
4. **Suggest next steps**:
   - Review the installed hooks and adjust patterns
   - Try `$lup:meta` to review the generated harness trees
   - Run `$lup:commit` to test the commit workflow
   - Consider `$lup:update` later for ongoing sync

## Guidelines

- **Respect the target**: Don't impose lup conventions where the target has its own. Adapt to them.
- **Minimal footprint**: In non-interactive mode, prefer doing less. The user can always run with `--interactive` later to add more.
- **No new dependencies**: Don't install anything that requires `pip install` or `npm install` unless explicitly approved in interactive mode.
- **Adapt, don't copy**: Every file needs to be reviewed and adapted for the target's ecosystem.
- **Preserve existing work**: Never overwrite files a harness tree already holds. Merge or extend.
- **Explain decisions**: For each installed item, briefly explain what it does and why it helps.
