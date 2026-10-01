---
name: distill
description: Restart from an explored repo — distill its direction into a fresh design
---

# Distill: Restart a Project from Its Exploration

The user explored a domain in a previous lup-based repository until the
direction became clear — and the exploration sprawled in finding it. This
fresh template is the restart: it carries the direction without the sediment.
You are the pendant of `$lup:brainstorm` for that situation — the design conversation starts
from an exploration instead of an idea. Your deliverable is the
**distillation brief**: a `DESIGN.md` distilled from the user's narrative and
the old repository's evidence, handed to `$lup:init`.

**The core stance: nothing is copied.** The whole point of restarting is to
*not* carry the old architecture. The old repository is read-only evidence you
consult to answer design questions — never a template to walk, never a source
tree to transplant. Everything in this project is rewritten from the brief. A
verbatim carry — even one file, even a test — happens only when the user
explicitly asks for it, and then it routes through `$lup:import` so every carried piece gets a ledger row and a
completeness audit rather than a quiet paste.

## User's Starting Point

the arguments supplied with this skill invocation

### Parse Arguments

Each leading path-like word names an **old repository** (there may be more
than one); everything after is the **narrative** — the direction found, the
concepts that earned their place, where the new project should go. The
narrative is the primary input, and the repository is evidence for sharpening
it. If the arguments come through empty, Ask the user directly, offering concrete options, and wait for the answer: which repository this restart distills from, and their narrative of the direction it found.

## Phase 0: Reach the Old Repository

Confirm the old repository is readable (`ls` its root). A fresh template's
session usually cannot see it — nothing mounted it. Register it read-only
rather than sending the user off to relaunch with a mount flag:

```bash
uv run lup-devtools sync setup <name> <old-path> --mount ro
```

The registration is an approval-gated edit: what a future launch mounts is
decided in `sync.json.local`, so a human approves the widening. Mounts are
built at launch — once the registration lands, ask the user to reopen the
session (`--continue` reaches this same conversation) and resume this skill.

Read-only is the mechanism behind the no-copy stance, not a limitation. And
the registration outlives this run on purpose: it keeps the old repository
reachable by name, so a month from now one genuinely needed piece is `$lup:import <name> <scope>` away — nothing is lost by leaving
everything behind today.

## Phase 1: Interview First

Read nothing yet. The user watched this exploration sprawl and knows why they
are restarting; that knowledge is the input the old repository cannot give
you.

## Discover Before Designing

An architecture proposed before the usecase is concrete anchors the whole
conversation on a guess. Before proposing structure, sketching a tool, or
reading code, get the usecase into the open: Ask the user directly, offering concrete options, and wait for the answer: the usecase itself — who runs this and on what occasion, what one run is, one worked example (a real input and the output they wish it produced), and what makes a run a success — with concrete candidate answers to react to, not open
prose alone. A vision the user holds precisely deserves precise questions:
probe until you can restate their vision and have them answer "yes, exactly
that" — then restate it and ask. Only after that confirmation do the
architecture forks earn their turn.

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
  the kit's own `.codex/config.toml` registering it under the events in question. Run it from where you are first:
Run `codex exec --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust "<the kit's first prompt>"` from the kit's directory. A non-interactive run loads the hooks in that directory's settings at launch and exits when the prompt is answered
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

Then the questions only a restart has: Ask the user directly, offering concrete options, and wait for the answer: which of the old project's concepts survive into the restart and which die with it, with the reason for each — and whether this restart is the only successor or one of several splitting the exploration.

## Phase 2: Archaeology

Now read the old repository — to sharpen the interview and draft the brief,
never to inventory code for carrying:

- `git -C <old-path> log --oneline --reverse` — the story: what was tried, in
  what order, what got reverted or abandoned
- Its `DESIGN.md`, `README`, and guidance — what it thought it was
- `# lup: defer:` markers and open `# lup:` feedback — what it knew was
  unfinished
- `notes/`, `tmp/` briefings, and feedback-loop state — what its sessions
  learned

You are extracting three things: decisions and their reasons, dead ends and
why they died, and domain facts that were expensive to learn. When something
in the old code answers a design question, read it, state the answer in your
own words in the brief, and close the file. Findings worth reacting to go back
to the user as questions, not as an inventory.

## Phase 3: The Distillation Brief

Write `DESIGN.md` at the project root, in the same structure `$lup:brainstorm` uses — Purpose, Architecture, Tools, Delegation,
Output Model, Session Behavior, Reflection, Success & Feedback, Environment,
Open Questions, only the sections actually discussed — plus the two sections a
restart owes to its history:

```markdown
## Graveyard
What the exploration tried and this project deliberately leaves behind — one
line each, with the reason. This is what stops a future session from
re-exploring a dead end.

## Field Notes
Domain facts that were expensive to learn and are free to carry: API quirks,
constants, format discoveries, rate limits, auth shapes. Knowledge, never
code.
```

When one exploration splits into several successors, the Graveyard and Field
Notes are shared history — write them the same in each successor's brief — and
a `## Scope` section states what *this* successor takes and what its siblings
take, so neither drifts into the other's ground.

Show the user the brief and fold in their corrections before handing off.

## Phase 4: Hand Off

Summarize what was decided and what remains open, then point the user to `$lup:init`, which reads `DESIGN.md` and skips every interview
question the brief already answers.

## Principles

- **Rewrite, don't carry.** The brief is the only conduit from old to new. If
  a design question tempts you to copy, the brief is missing a sentence —
  write the sentence.
- **The graveyard is a deliverable.** What was deliberately left behind, with
  reasons, is worth as much as what survives: it is the record that keeps the
  restart from sprawling the same way.
- **Ask, then read.** The user's narrative outranks the repository's evidence;
  archaeology sharpens questions rather than replacing them.
- **Scope at agent speed.** The restart is not a chance to shrink the vision —
  implementation runs at agent pace, and the clean slate is for clarity, not
  for a cut-down POC.
