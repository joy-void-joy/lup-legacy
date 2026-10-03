---
description: Review and modify the generated harness trees, brainstorm improvements interactively
allowed-tools: Bash(ls:*, uv run lup-devtools:*), Read, Edit, Write, Agent, AskUserQuestion
arguments:
  - name: arguments
    description: Optional arguments supplied with the skill invocation
    required: false
---

# Meta: Harness Structure Review & Improvement

You are reviewing the generated harness trees and brainstorming improvements with the user.

## User's Direction

$ARGUMENTS

## Your Task

Based on the user's input above, explore the relevant sources and brainstorm solutions. Almost every file in a harness tree is a generated artifact — read it for the rendered result, but make every change at its source:

| Artifact | Source |
| --- | --- |
| .claude/CLAUDE.md under Claude Code, AGENTS.md under Codex | `harness/content/guidance.py` |
| .claude/plugins/lup/commands/ under Claude Code, .codex/plugins/lup/skills/ under Codex | `harness/content/skills/*.py` |
| .claude/plugins/lup/agents/ under Claude Code, .codex/agents/ under Codex | `harness/content/agents/*.py` |
| .claude/plugins/lup/hooks/ under Claude Code, .codex/plugins/lup/hooks/ under Codex | the canonical policy in `lup.policy` |
| .claude/settings.json under Claude Code, .codex/config.toml under Codex | `harness/content/settings.py` and the adapter rendering each tree — the two are not parity, so read both before assuming a setting exists on either side |
| .claude/plugins/lup/TEMPLATE_CLAUDE.md under Claude Code, .codex/plugins/lup/TEMPLATE_AGENTS.md under Codex | `harness/content/template_sections.py` plus each flavor module |

Content paths above are relative to `src/lup_template/`. Every tree carries its own .claude/.lup-ownership.json recording which artifacts generation owns — consult the one for the tree you are changing whenever a path's source is not obvious.

Read the relevant sources based on what the user is asking about, then propose specific changes or additions and Request explicit user approval before editing any source the table names. Reason: one edit re-renders into every tree at once. Regenerate with `uv run lup-devtools harness generate all` after any accepted change.

## Rendered layout

Each tree lays the same declarations out its own way, and `docs/platform-differentiation.md` maps every intended difference between them. Read that rather than re-deriving a tree from memory: the table above is the mapping you need to make a change, and the layout only tells you where the result landed.

**Note:** Python CLI tooling (API inspection, trace analysis, feedback collection, worktree management, etc.) lives in `src/lup_template/devtools/` and is exposed as the `lup-devtools` CLI entry point. See the lup-devtools section in the guidance.

### When to Add to the Plugin

- **Skills**: Reusable workflows invoked by their qualified name, e.g. `/lup:command-name`
- **Hooks**: Permission rules in the canonical policy — auto-allow, deny, or quality gates
- **Agents**: Subagent definitions for specialized tasks
- **Devtools**: Python CLI tools go in `src/lup_template/devtools/` (exposed as `lup-devtools`), not in the plugin

## Walking the Decisions

### Before the first question

- **Read everything the user named, whole**, and the worked examples behind
  it — the repository it happened in, the notes it left, the register of what
  went wrong. Design from a failure inventory is grounded; design from first
  principles is a guess.
- **Prior notes are claims.** A `tmp/` design, an earlier session's
  conclusion, a document headed "settled": each records what an agent argued,
  approved by nobody until the user says so here. Say you are treating them
  that way, and walk each decision they contain as if it were new.
- **Verify every claim about the tree before repeating it** — a budget
  figure, "this appears nowhere in policy", "no such type exists". Open with
  where the tree disagrees with the notes; a discrepancy there reshapes the
  questions more than any answer will.

### The questions

- **Number them, in plaintext, all at once.** The user answers a batch by
  number in their own words and skips what is yours to decide; the structured
  facility takes one fork at a time and is kept for a single fork or a
  confirmation.
- **Each stands alone**: the failure it answers, drawn from the worked
  example with where it is recorded; the options; your recommendation marked
  as yours; one question. Someone who read nothing else can answer it.
- **A decision about an interface is shown as a code sketch** — the few
  lines the user would write under each option — not described in prose.
- **Keep each one short**: the problem, what was checked, the options, the
  recommendation — nothing about how you got there.
- **Open every later reply with what is decided so far, read back in your
  words.** A misreading surfaces there — "I'm unsure that should be on by
  default" — and costs one line to fix instead of a build. When a check
  reversed something you told the user, the readback says so plainly: what
  you said, what the check found, what now holds.

### When the user says "walk me through" or "from scratch"

- **Rebuild the concept from the problem it solves** — what went wrong, what
  any solution has to provide, then the shape — defining every term. Do not
  restate the option list in more words.
- **When the user offers their own shape, test whether it dissolves your
  objection** before defending the objection. It often does.
- **When the user gives a scenario, trace it literally** against the design
  as stated and say where it breaks. The break is the finding; a design that
  survives only the cases you chose is not settled.

### Verify, do not defer

- **A claim you can check in this session is checked now** — a CLI version,
  whether a hook fires, what a payload carries — with a script under `tmp/`
  when a command will not do. A stale document the check corrects becomes the
  first task of the build, on its own branch.
- **"Did you check?" gets a plain yes or no**, with what you read against
  what you ran. A ledger quoted is not a probe run.
- **A recommendation rests on claims already checked.** Marking one "I have
  not verified this" and recommending it anyway hands the user the check and
  dresses a guess as a caveat; the questions they push back on are the ones
  where that happened. Run it first. When the check refutes the reason, say
  so and give the recommendation the evidence now supports — a reversed
  recommendation with a measurement behind it is the turn worth taking, not
  an embarrassment to soften.
- **A harness behaviour is measured with a probe kit, not asked about.** A
  throwaway project under `tmp/`, with its own `git init`, because a runtime
  that takes the nearest repository as its project root would otherwise run
  the probe under this repository's instructions and project configuration.
  The init keeps this project's plugin out only where a plugin arrives per launch —
  where a runtime installs plugins into its own home instead, every session
  on the machine runs under them whatever directory it starts in, and the
  kit is governed by the policy it was built to sit outside. Check a command
  the kit will issue with `dev policy` before writing it down; a hook script
  that appends every payload it is
  handed to a JSONL file beside it and answers only the event under test; and
  the kit's own `.claude/settings.json` registering it under the events in question. Run it from where you are first:
Run `claude -p "<the kit's first prompt>" --permission-mode bypassPermissions` from the kit's directory. A print-mode session loads the hooks in that directory's settings at launch, runs nested inside this one, and exits when the prompt is answered. Its plugin is the one the outer session launched with, so a gate written in a worktree is probed only by naming that tree too: `--plugin-dir <the worktree>/.claude/plugins/<plugin>`
That run records the whole sequence, so no escalation marker is needed. Hand
the user the directory and the prompt only for what print mode cannot show — a
subagent left running in the background, a runtime whose credential a session
is not granted and whose nested run therefore answers 401. The recording is
evidence the build keeps as a fixture.

### A second case study

When the user names another repository that went through the same thing,
read it and derive the common denominator. What only one case needs leaves
the scope **by decision**, recorded as such, not by silence.

### Closing

- **The deliverable is a briefing rewritten whole** — `DESIGN.md` before a
  project exists, a `tmp/` briefing inside one: what is settled, what is out
  of scope and why, the build order, the empirical checks still owed, and what
  stays open, marked as the user's. Remove the notes it supersedes.
- **Do not start building.** How the work is cut into branches and when it
  starts are the user's; ask, with a recommendation.
- **Then ask what made the conversation work** and put it into this skill.

## First Principles Design

When considering changes, ask:

1. **Bitter Lesson Check**: Does this add a capability, or just a rule?
   - Prefer tools and capabilities over prompt constraints
   - Avoid pattern-matching patches

2. **Pipeline Diagnosis**: If fixing a failure, did you trace it?
   - What data did the agent have? What was missing?
   - Where in the workflow did the wrong decision enter?
   - Is the fix structural (new tool, better data, restructured step) or just a prompt patch?

3. **Generality Check**: Would this help if the domain changed?
   - General principles > specific patches
   - If it only works for one scenario, it's probably over-fitted

4. **Meta Level Check**: Are we changing the right layer?
   - Object level = the agent's behavior
   - Meta level = how the agent tracks itself
   - Meta-meta level = the feedback loop infrastructure

## Command Evolution

**After every command invocation**, reflect on how it was actually used:

1. **Compare intent vs usage**: Did the user use the command as documented, or did they adapt it?
2. **Notice patterns**: If the user provides documentation, links, or redirects the command's focus, that's a signal the command should evolve.
3. **Proactively propose updates**: When you notice the command being used differently than documented:
   - Propose updating the skill, as a question the user answers
   - Include the specific usage pattern you observed
   - Suggest concrete changes to its declaration

## Process

1. Read relevant files based on the user's direction
2. Analyze and identify potential improvements
3. Propose specific changes with rationale, and let the user choose among them
4. Implement approved changes immediately
5. **Reflect on this skill's execution** and propose updates to its own declaration if warranted
6. Continue brainstorming or summarize changes made
