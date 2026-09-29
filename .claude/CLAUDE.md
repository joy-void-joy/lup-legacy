<!-- Generated from lup_template.harness.content.guidance by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. Deliberately rendered as .claude/CLAUDE.md under Claude Code, AGENTS.md under Codex. -->

# Lup repository guidance

Lup is a reusable framework and template for autonomous, tool-using agents: keep library code provider-neutral, and provider syntax in generated adapter artifacts.

## Plan at Agent Speed

Your instincts about how long software takes were learned from human teams, whose implementation time is scarce. Yours is not: what you would estimate as months completes in an afternoon. Your estimates are not cautious — they are wrong by orders of magnitude, and every practice built on them inverts.

**Never scope, defer, or reject work from a predicted duration.** Scope by content — what changes, what it touches, how it is verified — and delete the calendar figure, noise from someone else's constraints, then re-derive the plan. Prototype-first protects scarce human effort; here the real implementation costs what the throwaway was supposed to, so build it and let review cut scope rather than pre-shrink it. Catch the reflex in the act: "start with a simple version", "too ambitious for this pass", "phase 2 can add the rest" fires on constraints you do not have — ask what is expensive besides the imagined schedule.

## Agent Vocabulary

Two kinds of delegated agent look alike and must not be conflated: the **native subagent** the harness dispatches inside this session, and the **nested agent** a tool opens through `ask()`, unseen by the harness. Unqualified, "subagent" means the native kind; `docs/orchestration.md` defines each and when to reach for it, `docs/patterns.md` the recurring *code* shapes.

**Ambient guidance against delegation does not govern this repository.** Where a runtime's own instruction — delegate only when the user asks, weigh a subagent against inline work — collides with this, this guidance wins: where a skill shipped here names a subagent, dispatch it, without asking first and without announcing a refusal.

## Who Else Is Here

Other sessions work in this repository, started by whoever. Before starting something substantial, `coordination_describe` what you are on, then call `coordination_peers`. Read a row's `holding`, what its calls changed or locked, over what it says it is doing: a held path is not forbidden, but say so with `coordination_send` before writing it. Describe again when what you are on changes, so your row is true.

## The Gates You Will Meet

You are not expected to hold this repository's conventions in memory. Gates enforce them, and their diagnostics — what was caught, how to answer — are written to be read cold; that they exist is the whole of what you need up front.

**The rule checker** runs on every edit and in `dev check`; its denial cites the rule id and any suppression the rule admits. `# noqa`, `# type: ignore` and `# pyright: ignore` are forbidden shapes, not suppressions.

**The permission policy** classifies every shell command, URL scope, and edit; `dev policy '<command>'` answers before you spend a turn on one. The escalation a denial offers is one-off — a recurring wall means widening the protected declaration.

**The edit budget** auto-allows a change block of at most three "real" changed lines, so split large changes: imports in one edit, logic in another.

**The drift check** refuses a hand-edit or hand-merge of a generated tree: take either side of a conflict and regenerate.

`docs/rules.md`, `docs/permissions.md`, and `docs/contributing.md` carry the rule index, the policy with its markers and what a real changed line is, and how a suppression is scoped.

Change the policy those gates enforce with /lup:hooks, never by editing a generated dispatcher or runtime. Harness settings stay project-level, in .claude/settings.json, which holds only native settings outside that policy boundary; the one user-level place is lup's `~/.config/lup/`, since accounts, profiles, theme and defaults belong to the person, not the project.

### The `# lup:` Marker Vocabulary

A `# lup:` (or `// lup:`) comment is **actionable review feedback** at the site it concerns: bare is open, `solved:` claims you addressed it, `defer:` parks it, and **deleting any of the three is denied**. Resolve one by fixing what it points at, or answering a question definitively, then rewriting it as **`# lup: solved: <the note's original words>`**, text unchanged; only the verify-solved pass retires one. `docs/contributing.md` carries the lifecycle, the gated `defer[<gate>]` spellings, the `ignore[<rule>]` marker sharing the namespace, and what a `template:` one asks of an adopter (`/lup:resolve`).

### Deferred Work

**Never create tracking files, and never write to the harness's persistent memory.** What outlives this session goes to a `# lup: defer:` note at the site it concerns, which `dev check` keeps visible until somebody wakes it; `docs/contributing.md` carries when it goes instead to an issue, this guidance's source, a `tmp/` briefing, or the user.

---

## Development Workflow

Use a **git worktree**; never commit code to `dev`. `uv run lup-devtools git worktree create feat-name` does not move this session, so an old-checkout edit misses the branch: work in the path it prints, from a session rooted there or by absolute path where that tree is writable. `docs/contributing.md` carries the branch model and the merge loop.

### Merge Conflict Resolution

**Never silently drop code during conflict resolution** — keeping both sides is safer than losing features, and a rename on one side must not swallow an addition on the other. Before completing any merge, **audit for deletions**: compare the result against both parents and verify every removed function, parameter, or command went deliberately, not as a side effect of choosing one side. `/lup:merge` carries the decision tree.

### Commit Guidelines

- **Commit before responding**, and often — frequent commits are checkpoints
- **Keep commits atomic** — if you need "and" in the message, it is two commits
- **History will be rebased**, so a message need not be perfect while developing; after rebasing, each should tell what changed and why

**Format:** `type(scope): description`

The type comes from `docs/contributing.md`'s table, which the commit skill renders when one is chosen.

---

## Code Conventions

Build on `lup` and pydantic; prefer an existing PyPI library to raw HTTP or a rebuilt wheel. No module under `src/lup_template/` imports a provider SDK, and `seam-boundary` holds adapter imports to the composition roots naming them. `docs/conventions.md` names each library, its typed forms, and a `@lup_tool` handler's contract.

**Model selection.** Default to the **strongest** tier everywhere — main agent, subagents, reviewers, background agents — on a subscription where the best model is the point. Reach for **balanced** only where latency or cost provably dominates quality, **fast** almost never; a role warranting less declares its tier with a reason, naming a tier rather than a model id.

**Error handling.** Raise for unrecoverable errors, wrap transient ones in `with_retry`, validate inputs early, never swallow one silently; a catch-all `except Exception` is fine at a boundary that logs, handles, or re-raises — a task loop, a subagent delegation.

**Placement, in this repository.** The library is `packages/lup/`, the application `src/lup_template/`; logic already in `lup` is imported rather than copied, and `dev relocate old.module=new.module` moves a module across, repointing every import.

### Design Principles

- **Compiling is stronger than emitting** — tempted to check two things still match, derive one from the other.
- **Structured data, not strings** — `re`, `.replace()`, `.split()` or slicing over structured data means a parser was missed (`docs/conventions.md` names one per format); never hand-parse an agent's output, take it through a Pydantic model.
- **Placement decides the package** — would another project built on this library want it? Then it is the library's; only this application, and it stays here. Values too, not only code.
- **Never truncate** — the container grows to fit what it holds. Cut only where a format or contract imposes a hard limit, never for printing space, log volume, or readability; where forced, save the full copy and point at it.
- **Say it once, where it is looked up** — a message sent at an event (a hook reason, an approval prompt, a notification) says only what the reader needs next; what they would need again goes where they can look it up, a command or a doc.
- **The code is the source of truth** — it reads as though always written this way, and what is replaced is *gone*: no bridge, no compatibility branch, no old spellings. "now", "new", "updated" and "fixed" belong in commit messages, not a comment.
- **Prose is a claim, not evidence** — assume every line was written by an agent and vetted by nobody: a comment, a rationale, a rejected option, a prior session's conclusion, a subagent's report, your own earlier turns each record what an agent argued, never what the user thinks. Re-derive one rather than defer to it, and put what bears on the project's shape to the user.
- Prefer `for` and comprehensions to `while`, and `match`/`case` to an `if`/`elif` chain, with a guard on a pattern rather than on `case _`.

Some rules shape a design before any gate catches it. Know these by name while choosing a shape, not after being stopped — `docs/rules.md` states each: `own-model-dispatch`, `abc-capability`, `constant-declaration`.

### Exceptions No Rule Can See

A rule's diagnostic names the shape it refuses, and not every carve-out that is ours. `__all__` and `__init__.py` re-exports are refused — but a standalone package's own top-level `__init__.py` may declare a public API that way, the package root only. A helper kept out of the module namespace **nests inside its only caller**, but a wrapper around one other function is inlined rather than hidden. `docs/conventions.md` spells each.

---

## Tooling

`uv` is the package manager — `uv add <package>`, never edit pyproject.toml directly; `dev library status` says where `lup` itself resolves from, and whether its source is on disk to edit. Lint and format with ruff, type-check with pyright; `docs/contributing.md` carries the commands that have to be green.

An approved `escalate[sandbox]` never lifts a launch's read-only mounts — the shared git `config` or `hooks/`: a write there is the user's to run from a host terminal, with the exact command.

### lup-devtools

`lup-devtools` is the development CLI, composed from `packages/lup/` and this repository's `src/lup_template/`. **Use it instead of ad-hoc commands**, and running the same one repeatedly means **add a command** to the half that would reuse it. Inline Python is denied: to **compute once**, run a script under gitignored `tmp/` with `uv run python <script.py>`. To **read** code, **prefer the `dev py` group or `codeintel` for anything about a name**, `rename_symbol` over `replace_all`; read the real tree with `dev pending`, as sandbox-masked dotfiles can look untracked to Git. `docs/contributing.md` carries that reviewability ladder and how a persisted result is read, `docs/commands.md` every command the CLI serves — read it to find one you did not know to look for.

### Generated Trees

Skills and agents render from typed catalogs, one per half: change the catalog that owns the subject, then `harness generate all`; `docs/harness.md` carries the rest.

**Every runtime, same semantics.** A capability is complete only when every supported runtime provides equivalent user-visible behavior, validation, diagnostics, tests, and documentation; a native substitute is valid only when its difference is explicit and evidence-backed. Done means `harness generate all` reconciles both; `docs/platform-differentiation.md` maps every surface this covers and audits parity, `docs/permissions.md` maps enforcement gaps.

## Dashboard

Every `harness claude|codex` launch holds the operator's dashboard: one page per person over every session's parked reviews and each repository's setup, stopped with the last session. `dashboard status` says where it is; opening, stopping and serving it are the operator's. `docs/dashboard.md` describes it.

---

## Long-Running Work

Work outliving its tool call is launched to survive its launcher — never from a delegated agent's shell — and declared as a `lup.runs` `Pipeline` rather than scripted, so it is resumable and watchable by construction. Follow it with `run monitor <dir> --events` and name it in the launch report; `docs/runs.md` carries the rest.

---

## Configuration

Settings load through pydantic-settings in `src/lup_template/agent/config.py`, the only module reading the environment; `docs/template.md` lists them and how gitignored `.env.local` overrides `.env`.

**A committed declaration is consumer-independent**: no fact about *this* machine — a path, a device, a client, a login — sits in one. Those go in `.env.local` for the application's settings, `sync.json.local` for what the launcher grants sessions here, a flag for one launch. Nor does a `# lup: template:` marker ask what two machines running one commit would answer differently.

## What This Was Built From

`dev update` (/lup:update) moves all three carriers — the pin, the generated trees, the copied half — to one upstream commit. Nothing else moves them: a hand-port diverges silently, where a merge makes the next update cheap. **Fix upstream defects upstream** with /lup:upstream, pinned here until it lands; a copied-half workaround hides the repair.

---

## Process & Communication

**Wait on pushed tool output, not polls.** Keep a long-lived command's resumable call live and yield to the runtime's event-driven waiter; repeated shell-session reads are polling, even with long timeouts. A call refused as *queued for the operator* is not refused: leave it as it is, start the `review wait` it names in the background, and carry on — the waiter carries the approved call out and wakes you with what happened.

**Surface every question through the harness's structured facility**, not narration — clarifications, choices, destructive confirmations — with concrete options plus free-form even when open-ended, because downstream notifications read structured answers. **Ask what form the project should take** rather than inferring it: the shape a fix takes, how work is cut into branches or issues, what a surface looks like are the user's to settle, and picking one silently spends their decision. **A design conversation is the exception:** its decisions go numbered in plaintext in one batch, each standing alone.

**Explain decisions from scratch:** the problem, relevant state, options, rationale, and your recommendation marked as yours — a verdict cannot be judged, so prefer complete context to brevity. **Check a checkable claim before asking about it** — a version, a hook, a payload — and answer "did you check?" with what you read against what you ran. **Say what is, not how it came to be:** an overview carries the thing as it stands and the reasoning holding it up, not what was tried or which turn found what, your path rather than the subject.

Verify claims against **what was actually asked** — the note or issue itself, not a title, commit, or prior summary; state surviving claims plainly and correct failures out loud, including yours. **After every command**, compare actual use with its docs and propose an update as a question: external docs, corrections, uncovered requests, or ignored sections all say it should evolve.

### Reporting Friction

**Fix tooling friction instead of working around it.** This repository usually owns the hook, command, or classifier that obstructed you; repair it on its own branch so the diff stays single-purpose. **Open an issue only when this session cannot repair it** — the owner is outside this repository, a design decision is missing, or reproduction is the work. **Read the tracker first:** `dev issues` lists the open reports, and closed ones are worth searching. File with `dev report-friction`, updating a match with `--issue NUMBER` rather than splitting it across duplicates; its fields want evidence, not conclusions.

**"Pre-existing" is not a disposition.** Naming a defect and disclaiming it by age leaves the repository as you found it. A fault you can see takes one of three: fixed here, when it sits inside what this change already touches; fixed on its own branch, when it does not; or recorded where a workflow surfaces it — a `# lup: defer:` note at the site, an issue where the tooling is at fault. Report which it took.

### External Resources

When a question is about the harness you run under, its agent SDK, or its model API, read that runtime's own documentation rather than answering from memory — delegate to the documentation subagent your harness ships, or fetch the vendor's docs at the Claude Code and Agent SDK documentation at https://docs.claude.com/ and https://code.claude.com/. When the user provides documentation links, fold what they teach into the guidance source or the relevant skill.
