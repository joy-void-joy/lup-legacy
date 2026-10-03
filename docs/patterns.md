<!-- Generated from lup.harness.content.docs.patterns by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Design Patterns

The recurring *code* shapes in this repository: what each one is for, where to
read a worked instance, and the reasoning that makes it the default. How work
is delegated across *agents* is a different subject and lives in
[docs/orchestration.md](orchestration.md).

Each shape below exists to close a gap by construction rather than to remind
someone to keep two places in step. That is the thread running through all of
them, and it is stated outright in § Compiling Is Stronger Than Emitting.

---

## Declaration Plus Renderer

A declaration says *what is meant*; a renderer says *how one runtime spells
it*. Keeping them apart is what lets one document serve several runtimes.

`SkillInvocation` (`packages/lup/src/lup/harness/models.py`) names a plugin and
a skill and nothing else — no slash, no prefix, no punctuation belonging to any
runtime. `SkillInvocationRenderer` (`packages/lup/src/lup/harness/contracts.py`)
turns it into the words one runtime reads. Prose that reaches a native tree
therefore contains no platform vocabulary at any point where a human authored
it, and the `portable-content` rule enforces exactly that.

Reach for this whenever content has to exist in more than one dialect. The
alternative — writing the dialect inline and translating later — puts the
translation where nothing checks it.

## Closed By Construction

When adding a variant *must* force an update elsewhere, arrange for the
compiler to be the one that says so.

`NativeSpellings` (`packages/lup/src/lup/harness/contracts.py`) declares one
abstract method per native word a prompt can reach. A new kind of prompt part
adds an abstract method, and no runtime can be constructed without answering
it. The seam is closed by construction rather than by a reminder to edit two
renderers — which is the same reasoning as § Never Dispatch On Our Own Models
in the agent guidance, seen from the other side: there, a declining base answer
makes omission *safe*; here, an abstract member makes omission *impossible*.
Choose by whether a silent default is a correct answer.

## Typed Matcher Plus First-Match Router

To pick a behaviour from a value, route through typed matchers to an ABC and
call it — do not grow a chain of conditionals, and do not reach for a regex
where a named capability will do.

`ModelRoute` pairs a `ModelMatcher` with a factory recipe, and
`ModelRouter.resolve` takes the first route that matches
(`packages/lup/src/lup/providers/routing.py`). `ExactModelMatcher` and
`PrefixModelMatcher` are each a few lines, and each *says what it means* —
where a regex would have encoded the same intent in punctuation that no
reader, and no type checker, can check. A new matching strategy is a new
class, not an edit to the router.

## An ABC Is An Engine, Not A Surface

A capability ABC declares a seam that several implementations fill. It is not
the thing a caller holds. What a caller holds is a concrete plain class that
*composes* the seam and is parametrized by which implementation fills it.

The scope is capability seams, not every abstract base. A union whose subtypes
answer for themselves — `TurnBlock`, where a consumer holds the variant and
calls `text_payload` on it — is Closed By Construction above, and holding the
variant directly is the whole point there. This section is about the seam a
consumer would otherwise reach past a missing surface to reach.

`ModelRouter` (`packages/lup/src/lup/providers/routing.py`) is the shape, and it
is the previous section seen from the consumer's side: `ModelMatcher` is the
engine, `ExactModelMatcher` and `PrefixModelMatcher` fill it, and nobody
outside the router calls `matches`. Callers hold the router and ask
`resolve`. `ClaudeSession` (`packages/lup/src/lup/providers/claude/__init__.py`)
and `CodexSession` are the same arrangement one level up — plain classes
composed over a `SessionEngine`, which the adapter fills, the agent's declared
layers wrap, and a test's double replaces. A caller asks the session for a
turn and never holds the engine the turn starts itself through; the agent's
own `ask` homes the rest: open a conversation, take one turn, close it.

What goes wrong without the surface is that the shared behaviour has nowhere
to live. When the seam is all there is, every consumer writes the part the
seam does not cover — opening a session, running one turn on it, closing it
whatever happened — and writes it slightly differently. There is no one place
to fix a bug in it, because there is no one place at all. The concrete class
is where that behaviour goes, and parametrizing it is what keeps the
implementation swappable anyway.

`ProfileResolver` (`packages/lup/src/lup/providers/config.py`) is what that
looks like caught in the act. The seam resolves a profile name to a
`ConfigTransform`, but every consumer wants the configured agent, so both
`ClaudeProfileResolver` and `CodexProfileResolver` grew the same
resolve-then-apply method on the side — the same four lines twice, in
two adapters that must never learn about each other. `ProfileSelector` is
that behaviour given a home: one plain class parametrized by the resolver,
handing back the transformed declaration, which opens its own sessions.

Two kinds of seam are exempt, for two different reasons that meet at the same
question: is there shared behaviour with no home? A pure function over config
has none: `ConfigTransform` holds nothing a surface could gather, so
applications stack transforms directly.

The second is a seam that is genuinely engine-only: implemented and injected,
never held. `TurnToolBinder` and `SubmittedOutputStore`
(`packages/lup/src/lup/sessions/capabilities.py`) are filled by adapters and
handed to `ComposedSession`, which binds through them. `SessionEngine` is the
same: every provider's session and every declared layer is composed over one,
and nothing a program holds is one. A driver that runs a turn inside its own
concern — signal handling in `send_interruptible`, a mailbox in
`run_relay_session` — takes a `Conversation` rather than an engine, and asks
it for a turn like any other caller: the protocol carries only what both
providers answer, so neither driver demands a `fork` or `steer` it never
uses. Say that a seam is engine-only in the ABC's own docstring, where
the next reader is already looking. A marker or a
rule cannot carry it: composing an ABC and holding one are spelled
identically at the import site, so a check would flag every composing class or
catch nothing.

---

## Compiling Is Stronger Than Emitting

Constructing output from a typed declaration closes divergence **by
construction**. Transporting checked source and hoping a checker complains
only lets something *warn* about divergence after it exists.

The distinction decides where a guarantee lives. When one canonical
declaration is compiled into every artifact that depends on it, the artifacts
cannot disagree — there is no state in which they differ, because there is
only one statement of the fact. When each artifact is written separately and a
test compares them, they can and eventually do disagree; the test only reports
it, and only where someone remembered to look.

This is the same reasoning as the guidance that the durable fix is a
capability, not a rule. A rule coexists peacefully with the failure it warns
about. A capability removes the state the failure needs in order to exist.

Apply it when you catch yourself adding a check that two things still match:
ask instead whether one of them can be *derived* from the other. A generated
artifact, a compiled dispatcher, and a rendered document are all this move.
Where derivation genuinely is not possible, say so where the duplication is —
and then the check is the right fallback rather than the first idea.

## A Constant Should Probably Be An Overridable Default

The defect is never that a value is a constant, nor how many constants there
are. It is assuming a **non-canonical** choice with no way to state a
different one.

Sort a value by asking whether anyone could reasonably want another:

- **Canonical** — a native tool's actual name, a wire protocol's field, a
  vendor's documented event. There is one right answer and it is not ours.
  Hardcode it; a parameter here is noise that implies a choice nobody has.
- **Non-canonical** — an allowlist of shell builtins, a size ceiling, a
  retry count, a palette. These are *our* judgement, reasonable people differ,
  and a project building on this one will have its own. Make them a default
  that a caller can override, not a constant it must fork the module to change.

The tell is a downstream reader who agrees with the mechanism and disagrees
with the number. If that reader has to edit library source, the value was
declared at the wrong level. `GuidanceBudget`
(`packages/lup/src/lup/harness/models.py`) is a worked example: its
ceiling mirrors a real vendor default, so the number is not arbitrary — but
*which* number a given project wants is still its own call, so it is a field
default on a declaration callers take, rather than a constant.

It also shows what a declaration buys over two loose defaults sitting side by
side. The scaffold's reserve belongs with the ceiling it comes out of, because
the number every caller actually wants is the difference between them —
derived once on the declaration rather than subtracted by hand at each site,
where the site that gets it wrong is the one nobody reads twice.

## Read The Thing You Are Checking Moved

A check that catches movement compares two values. Derive them from two
different places and the check stops testing what it was written to test: it
fires when the *sources* disagree, which they eventually will for reasons
that have nothing to do with the movement being guarded.

Take a worker's turn validated against a commit carried in a local, seeded
once at loop entry from the note clearance and advanced only by that loop.
Within one process the two agree. A concern resumed in a second
process re-enters at the clearance while its lease already holds a round the
first process committed, so the guard reads the orchestrator's own commit as
the worker seizing commit authority and fails the concern for work the
orchestrator did itself.

The rule is one line: **read the current value from its own source at the
moment you compare, and never carry a copy across a boundary that can also
move it.** `execute_concern_inner`
(`packages/lup/src/lup/resolver/execution.py`) reads `worktrees.head(lease)`
at the top of each round, and `join_commits` in `joins.py` reads its commits
the same way.

Two things follow. A value that must be captured *before* an operation — a
pre-turn head, a pre-edit digest — is captured as close to that operation as
the code allows, not hoisted. And because this class only appears once a
process boundary is crossed, the covering test is a **resume** test: run to
a committed intermediate state, build a fresh instance over the same
persisted state, and continue. A single-process test passes either way.

## An Instruction Is A Claim About What Is Permitted

Telling an agent to do something asserts the system will let it. When the
assertion is false the agent cannot comply and cannot proceed: it reasons
correctly, finds two authorities in contradiction, and spends a round asking
which one wins — or worse, complies with the one that was wrong.

Four instances of the shape: a question offering an option that needs a
gate its concern was never granted; a correction loop re-prompting a
submission the runtime refused
and would refuse identically; an acceptance criterion asking a worker to
convert a review note the orchestrator already deleted, against a standing
instruction never to write one; and a worker told to use a
plugin its worktree does not contain.

The remedy is not to warn the agent about the conflict. It is to **check the
instruction against the authority that would refuse it, at the point the
instruction is declared** — so an impossible one cannot be handed out. A
question names the gates its options need and a concern that asks for
more than it requests fails to validate, which is the shape to copy. Where
the conflict is genuine and intended, the carve-out belongs in the standing
instruction rather than in the agent's judgement: say which of the two rules
governs, so there is nothing left to adjudicate.
