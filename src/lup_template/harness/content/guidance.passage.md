<!-- passage: header -->
# Lup repository guidance

Lup is a reusable framework and template for autonomous, tool-using agents: keep library code provider-neutral, and provider syntax in generated adapter artifacts.


<!-- passage: changing-the-policy -->
Change the policy those gates enforce with {{ hooks_skill }}, never by editing a generated dispatcher or runtime. Harness settings stay project-level, in {{ project_settings }}, which holds only native settings outside that policy boundary; the one user-level place is lup's `~/.config/lup/`, since accounts, profiles, theme and defaults belong to the person, not the project.


<!-- passage: marker-vocabulary -->
### The `# lup:` Marker Vocabulary

A `# lup:` (or `// lup:`) comment is **actionable review feedback** at the site it concerns: bare is open, `solved:` claims you addressed it, `defer:` parks it, and **deleting any of the three is denied**. Resolve one by fixing what it points at, or answering a question definitively, then rewriting it as **`# lup: solved: <the note's original words>`**, text unchanged; only the verify-solved pass retires one. `docs/contributing.md` carries the lifecycle, the gated `defer[<gate>]` spellings, the `ignore[<rule>]` marker sharing the namespace, and what a `template:` one asks of an adopter{{ resolve_pointer }}.


<!-- passage: deferred-work -->
### Deferred Work

**Never create tracking files, and never write to the harness's persistent memory.** What outlives this session goes to a `# lup: defer:` note at the site it concerns, which `dev check` keeps visible until somebody wakes it; `docs/contributing.md` carries when it goes instead to an issue, this guidance's source, a `tmp/` briefing, or the user.

---


<!-- passage: development-workflow -->
## Development Workflow

Use a **git worktree**; never commit code to `dev`. `uv run lup-devtools git worktree create feat-name` does not move this session, so an old-checkout edit misses the branch: work in the path it prints, from a session rooted there or by absolute path where that tree is writable. `docs/contributing.md` carries the branch model and the merge loop.


<!-- passage: commit-type-pointer -->
The type comes from `docs/contributing.md`'s table, which the commit skill renders when one is chosen.


<!-- passage: code-conventions -->
---

## Code Conventions

Build on `lup` and pydantic; prefer an existing PyPI library to raw HTTP or a rebuilt wheel. No module under {{ application }} imports a provider SDK, and `seam-boundary` holds adapter imports to the composition roots naming them. `docs/conventions.md` names each library, its typed forms, and a `@lup_tool` handler's contract.

**Model selection.** Default to the **strongest** tier everywhere — main agent, subagents, reviewers, background agents — on a subscription where the best model is the point. Reach for **balanced** only where latency or cost provably dominates quality, **fast** almost never; a role warranting less declares its tier with a reason, naming a tier rather than a model id.

**Error handling.** Raise for unrecoverable errors, wrap transient ones in `with_retry`, validate inputs early, never swallow one silently; a catch-all `except Exception` is fine at a boundary that logs, handles, or re-raises — a task loop, a subagent delegation.

**Placement, in this repository.** The library is `packages/lup/`, the application {{ application }}; logic already in `lup` is imported rather than copied, and `dev relocate old.module=new.module` moves a module across, repointing every import.


<!-- passage: tooling -->
---

## Tooling

`uv` is the package manager — `uv add <package>`, never edit pyproject.toml directly; `dev library status` says where `lup` itself resolves from, and whether its source is on disk to edit. Lint and format with ruff, type-check with pyright; `docs/contributing.md` carries the commands that have to be green.

An approved `escalate[sandbox]` never lifts a launch's read-only mounts — the shared git `config` or `hooks/`: a write there is the user's to run from a host terminal, with the exact command.

### lup-devtools

`lup-devtools` is the development CLI, composed from `packages/lup/` and this repository's {{ application }}. **Use it instead of ad-hoc commands**, and running the same one repeatedly means **add a command** to the half that would reuse it. Inline Python is denied: to **compute once**, run a script under gitignored `tmp/` with `uv run python <script.py>`. To **read** code, **prefer the `dev py` group or `codeintel` for anything about a name**, `rename_symbol` over `replace_all`; read the real tree with `dev pending`, as sandbox-masked dotfiles can look untracked to Git. `docs/contributing.md` carries that reviewability ladder and how a persisted result is read, `docs/commands.md` every command the CLI serves — read it to find one you did not know to look for.

### Generated Trees

Skills and agents render from typed catalogs, one per half: change the catalog that owns the subject, then `harness generate all`; `docs/harness.md` carries the rest.

**Every runtime, same semantics.** A capability is complete only when every supported runtime provides equivalent user-visible behavior, validation, diagnostics, tests, and documentation; a native substitute is valid only when its difference is explicit and evidence-backed. Done means `harness generate all` reconciles both; `docs/platform-differentiation.md` maps every surface this covers and audits parity, `docs/permissions.md` maps enforcement gaps.


<!-- passage: configuration -->
---

## Configuration

Settings load through pydantic-settings in {{ config_py }}, the only module reading the environment; `docs/template.md` lists them and how gitignored `.env.local` overrides `.env`.

**A committed declaration is consumer-independent**: no fact about *this* machine — a path, a device, a client, a login — sits in one. Those go in `.env.local` for the application's settings, `sync.json.local` for what the launcher grants sessions here, a flag for one launch. Nor does a `# lup: template:` marker ask what two machines running one commit would answer differently.


<!-- passage: keeping-in-step -->
## What This Was Built From

`dev update` ({{ update_skill }}) moves all three carriers — the pin, the generated trees, the copied half — to one upstream commit. Nothing else moves them: a hand-port diverges silently, where a merge makes the next update cheap. **Fix upstream defects upstream** with {{ upstream_skill }}, pinned here until it lands; a copied-half workaround hides the repair.


<!-- passage: process-and-communication -->
---

## Process & Communication

**Wait on pushed tool output, not polls.** Keep a long-lived command's resumable call live and yield to the runtime's event-driven waiter; repeated shell-session reads are polling, even with long timeouts. A call refused as *queued for the operator* is not refused: leave it as it is, start the `review wait` it names in the background, and carry on — the waiter carries the approved call out and wakes you with what happened, the operator's note and line comments with it. Several edits the operator has to see go as one review: write them under `tmp/<name>/` at their paths in the checkout and `review propose` the directory. **What the operator reads to decide is plain speech** — a proposal's `--why` and file notes, a `review reply`, the note on a shell call that may park: say what changes, in ordinary words, then why, naming the file, function or command, as you would to a colleague at their desk.

**Surface every question through the harness's structured facility**, not narration — clarifications, choices, destructive confirmations — with concrete options plus free-form even when open-ended, because downstream notifications read structured answers. **Ask what form the project should take** rather than inferring it: the shape a fix takes, how work is cut into branches or issues, what a surface looks like are the user's to settle, and picking one silently spends their decision. **A design conversation is the exception:** its decisions go numbered in plaintext in one batch, each standing alone.

**Explain decisions from scratch:** the problem, relevant state, options, rationale, and your recommendation marked as yours — a verdict cannot be judged, so prefer complete context to brevity. **Check a checkable claim before asking about it** — a version, a hook, a payload — and answer "did you check?" with what you read against what you ran. **Say what is, not how it came to be:** an overview carries the thing as it stands and the reasoning holding it up, not what was tried or which turn found what, your path rather than the subject.

Verify claims against **what was actually asked** — the note or issue itself, not a title, commit, or prior summary; state surviving claims plainly and correct failures out loud, including yours. **After every command**, compare actual use with its docs and propose an update as a question: external docs, corrections, uncovered requests, or ignored sections all say it should evolve.


<!-- passage: reporting-friction -->
### Reporting Friction

**Fix tooling friction instead of working around it.** This repository usually owns the hook, command, or classifier that obstructed you; repair it on its own branch so the diff stays single-purpose. **Open an issue only when this session cannot repair it** — the owner is outside this repository, a design decision is missing, or reproduction is the work. **Read the tracker first:** `dev issues` lists the open reports, and closed ones are worth searching. File with `dev report-friction`, updating a match with `--issue NUMBER` rather than splitting it across duplicates; its fields want evidence, not conclusions.


<!-- passage: external-resources -->
### External Resources

When a question is about the harness you run under, its agent SDK, or its model API, read that runtime's own documentation rather than answering from memory — delegate to the documentation subagent your harness ships, or fetch the vendor's docs at {{ runtime_docs }}. When the user provides documentation links, fold what they teach into the guidance source or the relevant skill.


<!-- passage: self-improvement -->
---

## Self-Improvement Loop

`docs/self-improvement.md` carries the full loop — what to ask of a failure, and what to change in answer — and the feedback-loop, review, and meta skills each work from it. The durable fix is a capability, not a rule: trace the failure to the missing input or the workflow step where the wrong decision entered, and change that — a prompt rule coexists peacefully with the failure it warns about.
