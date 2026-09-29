<!-- Generated from lup.harness.content.docs.library by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# The lup library

`packages/lup` is the reusable half of this repository: a standalone published
package that knows how to run an agent turn, compile a harness, decide a
permission, and resolve reviewed feedback — without knowing which vendor is
behind any of it. It is the larger of the two code components and the one a
downstream project depends on.

The rule that shapes every module: **shared code never names a provider.**
Native config, hook payloads, command spellings, manifests, and wire schemas
live inside `lup/providers/`. Everything above the providers speaks contracts.
A third backend implements those contracts without editing a shared registry,
because there is no registry to edit.

## The front door

`packages/lup/src/lup/__init__.py` re-exports a deliberately small runtime
surface — the one place in the library where a barrel is allowed, because it
declares a public API:

```python
from lup import (
    Claude,           # a Claude Code agent, declared whole; it opens its sessions
    Codex,            # a Codex agent, declared whole; it opens its threads
    Agent,            # what either declaration answers, for code naming neither
    Conversation,     # an open session of either
    Turn,             # one turn of either: awaitable, iterable over its blocks
    TurnResult,       # a validated, typed result
    TurnInput,        # portable user input
    TurnMessage,      # one message of a conversation's history
    SessionId,        # the provider's identity for a conversation; resumes it
    SessionSummary,   # one conversation the provider has on record
    TurnId,           # the provider's identity for one turn; a fork is cut at it
    CustomModel,      # a model id outside the runtime's catalog, on purpose
    OuterContainer,   # the wall a launch opens behind: the verified container,
    InnerSandbox,     # the runtime's own sandbox on the host,
    NoSandbox,        # or the semantic policy alone
    Mount,            # a folder outside the working tree the session reaches
    Member,           # the session's name on the coordination roster
    Recording,        # what is kept of the session beside the runtime's record
    Latest,           # reopen the newest session in the workspace,
    Pick,             # the one the runtime's picker offers, at a terminal,
    Reopen,           # or the one named by its id
)
```

The shortest useful program imports and declares everything it uses:

```python
import asyncio

from pydantic import BaseModel

from lup import Claude


class Summary(BaseModel):
    summary: str


async def main() -> None:
    agent = Claude(model="opus", system_prompt="Be concise.")
    result = await agent.ask("Summarize why typed boundaries help.", Summary)
    print(result.output.summary)


asyncio.run(main())
```

`Claude` and `Codex` are why the root is worth importing. Each is a frozen
Pydantic model declaring one agent whole — model, prompt, tools, permissions,
workspace, and the layers its sessions are wrapped in — and each is also what
opens those sessions: there is no client to build from a declaration.
The launch vocabulary is the fields a launch adds to that declaration, each a
typed value from `lup.launch.declaration` (**Launching** below). Everything
else here is vocabulary, a name to annotate against, and vocabulary alone
builds nothing: the typed result is a Pydantic model the program declares
itself.

The agents and the launch vocabulary resolve on first access, because each
stands on several hundred modules — an agent on its provider's tools, a
launch field on the harness, policy and sandbox machinery a launch composes.
So `import lup` loads neither an adapter nor that machinery, and naming either
still loads no provider SDK: opening a session does.

### Asking

`ask` is the only verb. On an agent it is a one-shot: `agent.ask(prompt,
Model)` opens a session, takes one turn, closes the session however the turn
ended, and returns `TurnResult[Model]`; asked without a model it returns
`TurnResult[None]`. For more than one turn, open a session and ask it:

```python
from pydantic import BaseModel

from lup import Claude


class Plan(BaseModel):
    steps: list[str]


async def plan(agent: Claude) -> Plan:
    async with agent.open() as session:
        await session.ask("Draft a plan for the migration.")
        turn = session.ask("Now give that plan as steps.", Plan)
        async for block in turn:
            if (text := block.text_payload) is not None:
                print(text)
        result = await turn
        return result.output
```

A turn starts the first time anything asks for it — an `await`, an iteration,
`events()`, `live()`, `interrupt()`, or on Codex `steer()` — and starts once
however many ask; awaiting it after iterating returns the same result. Its
output model is bound before the provider accepts the prompt — Claude's
submission tool, Codex's `outputSchema` — so no turn runs ahead of the schema
it has to answer in.

| Ask the turn | For |
| --- | --- |
| `await turn` | The `TurnResult[T]`: `output`, `blocks`, `messages`, `usage`, `duration`, `identifiers` |
| `async for block in turn` | Each completed block, in the order they finished |
| `turn.events()` | Every durable event: turn and block starts, block and message completions, the turn's end |
| `turn.live()` | The durable events and the deltas between them |
| `await turn.interrupt()` | Stop the turn, returning once it has stopped |
| `await turn.steer(prompt)` | Add input to the running turn without starting another — `CodexTurn` only |

`live()` on a Claude agent declared with `delta_streaming=False` raises
`DeltaStreamingDisabled` rather than yielding a turn that only looks quiet.

A capability a provider lacks is absent from its type, never present and
`None`: `ClaudeTurn` has no `steer`, because Claude takes no input into a
running turn, so the call fails in the type checker rather than at run time.
`ClaudeSession`, `ClaudeTurn`, `CodexSession`, and `CodexTurn` are named from
`lup.providers.claude` and `lup.providers.codex`, each of which holds every
part of its provider a program writes. Code that works over either provider
names the `Agent`, `Conversation`, and `Turn` protocols instead, and asks
only for what both answer.

### Conversations outlive the process

`session.id` is the provider's own identity for a conversation, and it
resumes it. `agent.sessions()` lists what the provider has on record for the
agent's workspace, newest first — conversations a terminal started as well as
the ones this library did:

```python
from lup import Codex


async def last_conversation(agent: Codex) -> None:
    past = await agent.sessions()
    async with agent.open(resume=past[0].id) as session:
        for message in await session.history():
            texts = [text for block in message.blocks if (text := block.text_payload)]
            print(message.role, texts)
```

`history()` reads the provider's own record — Claude Code's transcripts,
Codex's thread — normalized into the same `TurnMessage` and block types a turn
yields. `session.fork(at=result.identifiers.turn)` opens an independent
conversation carrying this one's history through that turn, or everything so
far with `at` unset; nothing asked of either reaches the other.

### Models and effort

`model` takes a name from the runtime's own catalog, a `Literal`, so a typo
fails in the type checker; a portable tier — `frontier`, `strongest`,
`balanced`, `fast` — that each adapter spells in its own lineup; or
`CustomModel(id=...)` for an id the catalog does not list, such as a
compatible endpoint's own model. `effort` climbs `low`, `medium`, `high`,
`xhigh`, `max`, `ultra`, and an effort the catalog says the model cannot take
is refused where the agent is declared rather than dropped by the CLI.

A caller holding nothing but a model id asks `catalog_provider(model)`
(`lup.providers.routing`) which runtime's catalog lists it, then declares that
agent: dispatch cannot carry typed provider options, so it answers the
question and leaves the declaration to the caller.

Both declarations resolve on first access, so `import lup` pulls neither
adapter nor either provider SDK. Naming `Claude` or `Codex` imports its
adapter — several hundred modules, the MCP tooling its tools are declared in —
and still no SDK: opening a session is what finally loads Claude's SDK or
starts Codex's app-server.

## Tools

What a session may call is one field, `tools`, typed per provider:
`ClaudeTools` from `lup.providers.claude` and `CodexTools` from
`lup.providers.codex`. Each holds `builtin`, the runtime's own tools, and
`mcp`, the MCP servers every session carries.

`builtin` is a preset or an exact list. `"web"` is the default: fetch and
search, and nothing that reads, writes or runs anything on the machine the
session runs on until something grants it. `"stock"` is everything the runtime
ships — on Claude the SDK's `claude_code` tool preset and the coding system
prompt that teaches it, with `system_prompt` appended. `"none"` leaves only the
declared servers. A list names exactly the tools granted, as
`ClaudeBuiltinTool` or `CodexBuiltinTool` literals, so a misspelt name is a
type error where it is written and a validation error where it is read:

```python
from lup import Claude, Codex
from lup.mcp import CodeIntel, Coordination, Toolset
from lup.providers.claude import ClaudeTools
from lup.providers.codex import CodexTools

from my_project.tools import lookup  # an @lup_tool declared at module level

reader = Claude(
    tools=ClaudeTools(
        builtin=["Read", "Glob", "Grep"],
        mcp=[Coordination(), CodeIntel(), Toolset([lookup], name="project")],
    )
)
executor = Codex(tools=CodexTools(builtin=["Bash"]))
coder = Claude(tools=ClaudeTools(builtin="stock"))
```

| `builtin` | Claude | Codex |
|---|---|---|
| `"stock"` | The `claude_code` preset, with Claude Code's system prompt | Every facility: shell, web search, patches, images, delegation |
| `"web"` | `WebFetch`, `WebSearch` | Hosted web search |
| `"none"` | No built-in | No facility |
| A list | Exactly those of its 25 tools | Any of `Bash`, `WebSearch`, `apply_patch` |

Codex has no tool that reads, writes or fetches without being one of those
three facilities, so `Read`, `Write` and `WebFetch` are not Codex names rather
than names it approximates. Permission patterns such as `Bash(*)` are not tool
names either.

The servers in `mcp` are objects from `lup.mcp`. `Coordination()`,
`Ledger(...)`, `CodeIntel()` and `Sandbox()` are lup's own groups;
`Toolset([...])` serves a project's `@lup_tool` handlers, `Group(builder)` a
group built over the session, and `External(name=..., server=...)` a transport
lup does not host. Each is read two ways. A session this process opens hosts
it, built when the session opens — Claude over the SDK's in-process MCP, Codex
through its dynamic tools. A runtime's own CLI starts it instead, as a stdio
command running `python -m lup.mcp.serve` (or a composed CLI's `tools serve`)
that carries the server's class and fields, which the subprocess validates back
into the same declaration. That is why a served `Toolset` names module-level
tools: an import path is what crosses the process boundary. Any of them takes
`always_load=True` for tools a session calls on most turns: Claude then offers
them from the first turn rather than behind its tool search, and Codex, which
names no such control, is unchanged.

Typed output remains available with no built-in. On `Claude`, `allowed_tools`
controls automatic approval within the declared tools and `disallowed_tools`
narrows it; neither adds an undeclared tool. A session opened here with a
plugin requires `"stock"`, since a plugin can introduce delegated authority.

Explicit session hooks remain attached when built-in tools are granted. Codex
enables the verified declared project policy plugin for an explicit built-in grant while keeping
unrelated inherited plugins disabled. Provider settings and extra arguments
that could widen the requested authority are rejected, including altered
copies of validated configurations.

Codex requires the selected model to appear in its native model catalog so
the adapter can bound the tool metadata attached to model requests. An unknown
explicit or inherited model is rejected before input.

A Codex thread's application tools are fixed at thread creation. Resume
requires the same set; native grants may narrow on resume. Start a fresh
session when application tools require another dynamic binding. The adapter
rejects an incompatible resume before sending user input. Typed output does
not ride that channel — each turn carries its own `outputSchema` — so the
model a turn is asked for may change from one turn to the next.

## Launching

A declaration is compiled twice. `open()` and `ask()` compile it into SDK
options for a session this process drives; `command()` compiles the same
fields into the `(argv, env, cwd)` an interactive CLI starts with, and
`launch()` runs that command in the foreground, the terminal handed over until
the session ends. The fields a launch adds are typed values the package root
exports, defined in `lup.launch.declaration`, and each means one thing to
both compilations:

```python
from pathlib import Path

from lup import Claude, Latest, Member, Mount, OuterContainer, Recording
from lup.mcp import CodeIntel, Coordination
from lup.providers.claude import ClaudeTools

agent = Claude(
    model="opus",
    tools=ClaudeTools(mcp=[Coordination(), CodeIntel()]),
    plugin=Path(".claude/plugins/lup"),  # or a Harness, which prepare() compiles
    sandbox=OuterContainer(mounts=[Mount(path=Path("../notes"), writable=True)]),
    identity=Member(name="reviewer"),
    record=Recording(transcript=True),
    resume=Latest(),
    max_recursive_agent=2,
    profile="work",
)


class Checkpoint:  # a LaunchStep: the repository's workflow, not the session's
    def before(self) -> None: ...

    def after(self, succeeded: bool) -> None: ...


command = agent.command("--verbose")  # printable: str(command) is its argv
agent.prepare()  # compile the plugin and settle the home; nothing launched
agent.check()  # the CLI's probes, the declared requirements, the login
status = agent.launch("--verbose", steps=[Checkpoint()])
```

| Field | A session opened here | A launched CLI |
|---|---|---|
| `sandbox` | One settings document: `InnerSandbox` enables Claude Code's sandbox with the policy's exclusions and the mounts' write widening, anything else stands it down; Codex's mode narrowed by the wall. `OuterContainer` needs the program entering the container in `cli_path` / `executable` | The same document as `--settings` (Codex: the `--sandbox` envelope and its writable roots); `OuterContainer` builds and verifies the container and starts the CLI inside it |
| `plugin` | Claude's first plugin directory, which takes `builtin="stock"`; Codex installs the plugin its project's marketplace offers | `--plugin-dir` (Claude); installed into the launch's home (Codex). A `Harness` is compiled into the project's tree by `prepare()` |
| `policy` | Hooks judging every call in process; unset, the plugin harness's own | The plugin's dispatcher, and the boundary the launch measures and records |
| `tools.mcp` | Hosted in process | `--mcp-config` with `--strict-mcp-config` (Claude), `--config mcp_servers.*` (Codex): the declared roster, never the plugin's |
| `identity` | Joins the coordination roster through the session's environment | The same, plus `--name` and the wake socket, keyed by the member's id, on Claude |
| `record` | The run's journal, transcript and ledger entry, kept while the session is open | The same, around the foreground CLI |
| `resume` | `Latest()` resumes the newest session on record; `Pick()` is refused | `--continue` / `--resume` (Claude), `resume --last` / `resume` (Codex); `Reopen(session=...)` names one |
| `max_recursive_agent` | `LUP_MAX_RECURSIVE_AGENT`, never more than this process has left | The same variable |
| `profile`, `home` | The account's configuration home | Claude: the same home; Codex: a home derived from the account's for the worktree |
| `companions` | Held while the session is open, their variables in its environment and their folders among its sandbox's mounts | Held around the foreground CLI, their variables carried into a container by name; `command()` holds them long enough to learn what they hand it |

What a launch does not say it assumes, and a session opened here does not:
an unset `sandbox` is the verified container wherever Docker or Podman answers
and the inner sandbox, with a warning, where neither does; built-in tools left
unnamed are the runtime's stock; the permission mode is the CLI's own rather
than a program's `bypassPermissions`; an unset identity is the worktree's name
on the roster; an unset record is the run's transcript. `launched()` answers
the declaration with those filled in. What only a program driving turns can
honour — in-process `hooks`, a submission gate, `layers`, `max_turns` — a
launch refuses in the field's own words rather than dropping.

`companions` are what a session wants running on the host beside it and
outside every wall it has: a service answering the operator, a preview
server, a watcher. Each is a `HostCompanion` from `lup.launch.companions`,
held around every session the declaration opens and handing it the
environment, mounts and ports that reach it. Most are one process shared by
many sessions — a `SharedProcess`, per checkout or per person: the first
session to hold it starts it on its preferred ports or the next free ones,
later sessions join it, a replacement is started where it stopped answering
or its declaration changed, and it is stopped once the last lease goes, a
lease whose launcher died counting as gone. Its state and output live under
lup's own state directory, never in a checkout. A service the operator
already runs on the host's loopback — a model server, a database — is a
`HostService`: the session is handed its address under the variable the
project names, and a container whose loopback is its own, on any network but
the host's, reaches it through a socket the launch relays for that one port,
which the image's entrypoint binds to the same address inside; nothing else on
the host's loopback is reachable that way. A container joined to no network
(`network="none"`) is refused a host service rather than relayed one, since the
relay would be the one way through that wall.

`steps` are the repository's own workflow around a launch — a checkpoint, a
base-freshness sync, a companion tree regenerated — each a `LaunchStep` with
`before()` and `after(succeeded)`. They nest as `with` blocks do: every
`before` in the order given, the session, then every `after` in reverse, run
however the session ended. `Codex.prepare(force=True)` and
`Codex.launch(force=True)` reinstall a plugin whose version has not moved;
Claude loads its plugin from the directory at each start, so it takes no such
flag. `lup-devtools harness claude|codex` is one such caller: it builds the
declaration from its flags and this repository's composition and launches
it, its own workflow the steps around the session — `docs/harness.md` maps
each flag to its field, and `examples/launch_*.py` show every field on its
own.

## Layering

Four tiers, and imports only ever point downward.

1. **Foundations** — entries that import nothing else in the library, so
   they sit at the top level rather than inside a subject. `lup.types` is the
   portable content and tool vocabulary every other package speaks
   (`JsonValue`/`JsonObject`, `ToolName`/`ToolGrant`, `LupContentBlock`,
   `LupMessage`, `Usage`, `SubagentSpec`); `lup.channels` is the file-backed
   primitive both durable state and inter-process rendezvous are built on;
   `lup.formats` is how a compiled artifact has to be spelled to survive being
   one, the do-not-edit banner and the escaping of a derived table's cells;
   `lup.seams` and `lup.execution` are the rest. Burying one of these inside a
   subject is what manufactures a cycle — folding `channels` in with
   `workspace` did exactly that and was undone. Gathering two of them under a
   question they share does not, and cannot: a package holding only leaves has
   no outgoing edge to close a loop with.
2. **`capabilities` and `events`** — each subject carries both.
   `sessions/events.py` owns the turn vocabulary; `sessions/capabilities.py`
   owns the narrow capability seams, and each other subject carries the same
   pair under its own names. Seams import the foundations only, so a fake
   implementation needs nothing else.
3. **Implementations** — composition, middleware, validation, reconciliation,
   rule evaluation. These import their own subject's seams and vocabulary, and
   nothing from `providers`.
4. **`lup.providers.claude` / `lup.providers.codex`** — the only packages that
   name a vendor. They implement the contracts above and are imported only by
   named composition roots.

`lup.harness.codescan.boundaries` enforces tier 4 mechanically with the
`seam-boundary` rule: a concrete adapter import outside `lup/providers/`,
the tests, the examples, or a named application composition root is a
build failure, not a review comment.

## Where a module belongs

Three questions place every module, and they point in different directions.

**Outward — would another project built on lup want this?** If yes it belongs
in `packages/lup/` even when only this application uses it today, because the
library never imports the application: a utility left in `src/lup_template/`
is unreachable from here and has to move later. The same test applies to
values. The library may declare one only when it could not have chosen
otherwise — a language's file suffixes, a provider's wire spelling, a closed
enum the library itself defines. Everything else is a judgement, and reaches
an adopter as an overridable default they replace rather than a constant they
fork. `library-default` in `lup.harness.codescan.boundaries` is the mechanical half of
that; canonicity it cannot judge, so a canonical table says so with
`# lup: ignore[library-default]` and a reason.

**Inward — is this the tooling layer, or what the tooling layer is built on?**
`lup/devtools/` is the development CLI an adopter inherits. Provider-neutral
code a program would want with no CLI in front of it sits above `devtools/`,
and `devtools/` imports it; the reverse never holds. A value follows the same
rule at module scale: a page's default port belongs to the module serving that
page, not to a module about checkout directories that happens to be imported
by both.

**Downward — is this a subject of its own, or part of one?** A top-level
package answers a question no sibling answers. One that exists to serve a
single subject nests under it — and library code follows its driver only as
far as the library edge, so a package driven from `lup/devtools/harness/`
nests under `lup/harness/` rather than moving into `devtools/`, which would
pull provider-neutral code into the tooling layer.

## The packages

### `sessions` — how one turn runs

The engine. `surface.py` declares what a program holds over either provider:
the `Agent`, `Conversation`, and `Turn` protocols. `capabilities.py` declares
the seams beneath them — start a turn, resolve it, stream its events,
interrupt, steer, fork, read the provider's record — as one-to-three-method
capabilities, and `turns.py` holds the turn that starts itself through them
the first time anything asks. `events.py` holds the shared turn vocabulary:
opaque `SessionId`/`TurnId`, the `TurnBlock` union (`TurnTextBlock`,
`TurnThinkingBlock`, `TurnToolCallBlock`, `TurnToolResultBlock`,
`TurnNativeActivityBlock`), `TurnMessage`, `SessionSummary`, and the generic
`TurnResult[T]`.

Everything optional is a layer or an absent capability, never a flag:
`middleware.py` holds the turn decorators — timeouts, budgets, retries,
correction, tracing, usage, and display — and `layers.py` the `SessionLayers`
an agent declares them in, beside the session wrappers that go around them;
`output.py` binds a fresh `submit_output` tool and store to each typed turn;
`budget.py` and `quota.py` are the two opposite kinds of "no more work" it
applies.

Everything about *which* runtime answers moved out to `providers`, and
everything about running work *over* a session moved out to
`orchestration` — a turn engine that also held routing, profile trees and a
background agent was three subjects sharing one name.

Unsupported behavior is *absent* from a provider's type rather than present
and raising: `CodexTurn` has `steer`, and `ClaudeTurn` has no such method.

### `harness` — declaration to disk

Compiles one provider-neutral declaration into native plugin trees, with a
proof of what it owns. `models.py` holds the declaration graph
(`Harness` → `Plugin` → `Skill`/`Agent`/`HookSet`) and the rendered
`Artifact`/`ArtifactTree`. Prompt bodies are ordered typed parts —
`TextPart` for prose, `SkillInvocation`/`NativePath`/`ArgumentsRef` and their
siblings for anything a runtime spells its own way.

The pipeline is `validation` → `ownership` → `reconciliation` →
`materialization`, plus `proposals` for the reviewed patch transport back to
canonical source and `process`/`environment` for launching a native CLI.
`generation.py` holds the small deterministic helpers the stages share. The
do-not-edit banner every commentable generated artifact opens with is
`lup.formats.banner`, a foundation rather than part of this subject, because the
policy bundle writes one too and a banner reached through the harness made
the two entries import each other.

`codescan/` nests here: the rule engine behind `lup-devtools dev check` and
both generated edit hooks, and it reads this package's declaration models to
judge a portable artifact. `common.py` provides comment-column tokenization,
docstring detection, and ignore-directive parsing; `markers.py` finds
`# lup:` review notes; `antipatterns.py`, `boundaries.py`, `capabilities.py`
and `portable.py` are the rule families; `registry.py` indexes them all into
[rules.md](rules.md). [harness.md](harness.md) walks the whole pipeline.

### `policy` — one decision, two homes

The permission core, split so the same verdict can be reached inside this
library and inside a generated plugin that cannot import it.

`policy/kernel/` is hermetic: stdlib-only, statically audited imports,
primitive rows in and a decision out. It is copied *verbatim* into every
generated tree, which is why a traceback from a hook still points at real
canonical line numbers. Above it, `rules.py` validates application inputs as
Pydantic surfaces and erases them into kernel rows, `chain.py` composes
policies deny-before-ask, and `bundle.py` assembles the kernel source plus
rendered data rows for generation. [permissions.md](permissions.md) is the
full lattice.

### `resolver` — reviewed feedback to an integration branch

A persisted state machine over concerns. `models.py` holds schema-versioned
records; `dag.py` validates and orders the concern graph; `state.py` persists
it atomically under a file lock; `run.py` names the one live state a run
holds, with the lock and the observer that guard it; `orchestrator.py` owns
every git side effect (leases, worktrees, commits, dependency bases);
`mailbox.py` binds `lup.coordination.mailbox`, which carries questions and
answers as files so any door can write while the run holds its lease, to the
resolver's own question type. Each phase is a collaborator over those rather
than a method on one class: `questions.py` publishes and promotes; the
population is `lup.coordination.cohort`, holding one durable session per
member through `lup.coordination.sessions`; `turns.py` puts the prompts
to them, `joins.py` brings branches together and settles what that breaks,
`verification.py` runs one tree through the verification set, and
`execution.py` drives one concern's revision loop. `core.py` composes them
and owns only the sequence. [resolver.md](resolver.md) covers the lifecycle.

### `providers` — the vendor edge

`providers/claude/` and `providers/codex/` each hold, in the package itself,
everything a program writes with that provider — the `Claude` or `Codex`
declaration, its session and turn classes, and the vocabulary they take — so
a program names nothing deeper. Behind that each implements the same four seams:
`runtime.py` (open sessions behind the runtime contracts), `harness.py`
(render the declaration into that runtime's tree), `harness_runtime.py`
(probe the installed CLI for evidence), and `native.py` (decode hook payloads
into policy events, render decisions back). `providers/harness.py` composes the
renderers into whole-tree compilers.

Each also carries what only it needs: Claude a personal account registry that
`providers/profile_tree.py` answers with the directories a project keeps instead,
and a reader of the transcripts Claude Code keeps, which `history()` and
`sessions()` answer from; Codex a typed JSON-RPC transport to
`codex app-server`, which answers both itself. Neither is mirrored for
symmetry's sake. [platform-differentiation.md](platform-differentiation.md)
is the map of every difference.

### The rest

Every remaining top-level entry, and what makes it one. `__init__` is the
front door, and six more are described at length above instead —
`types` in **Layering**; `sessions`, `harness`, `policy`, `resolver` and `providers` in **The packages** — so the rest each answer a question no sibling
answers.

Which entries this table has to cover is walked from the installed `lup`
package when the page is generated — `packages/lup/src/lup` in this repository,
and wherever a downstream project resolved the dependency to. Generation fails
naming any package that is neither described here nor tiered above, so a
package added to the library cannot be quietly missing from its own roster —
the way six of them once were.

| Package | Solves |
| --- | --- |
| `channels` | File-backed channels: a value that settles, and an ordered log. The widest dependency in the library: most of its top-level entries write through this one, which is what makes it a package rather than a helper inside any of them. Counted rather than listed, because the list is the thing that falls behind — the roster this paragraph came from named six consumers where the import graph held eleven, and nobody notices a sentence going stale. |
| `coordination` | Addressable agents: one held session each, reachable while they work. An agent a caller opens, drives for one turn and closes cannot be talked to, because there is nothing to talk to between the call and the result. This package is the other shape — an actor holds its session across turns, takes mail mid-turn through a hook it never chooses to check, and asks questions that settle without stalling whoever asked. |
| `devtools` | The development CLI a project built on lup inherits rather than forks. Worktrees and branches, trace and Python introspection, the resolver supervisor, the sync registry, version bookkeeping. Ships the whole roster — `roster.py` wires every sub-app over one `DevtoolsDeclarations`, and an application declares only what it retires and what only it has, so a sub-app added here reaches it on the next lock refresh instead of waiting to be noticed. Requires the `web` extra for the supervisor. |
| `execution` | What carrying work out runs into, and what to do about each of it. The retry and the throttle a flaky or rate-limited service is met with, the executor a blocking call is handed to so work in flight outlives any one loop&#x27;s teardown, and whether a path can be written at all — or whether a boundary owns it and something merely died holding a lock. |
| `formats` | What a generated artifact is written as, at the leaf where data enters it. Not what a document says, but what it has to be spelled like to survive being one. A file compiled from a declaration has to say so, in whatever comment syntax its own format admits; a value spliced into a compiled table has to survive the characters that would end a cell or a row early. Both are one question — the target format&#x27;s rules, applied where data crosses into it — and it is nobody else&#x27;s: prose a human wrote is Markdown all the way down and needs nothing here, which is why this sits below every package that compiles something rather than inside the one that compiles most. |
| `launch` | Launching a declared agent: the vocabulary both compilations of one declaration read. An agent declared as :class:`~lup.providers.claude.Claude` or :class:`~lup.providers.codex.Codex` is compiled twice from the same fields: into SDK options when a program opens a session in process, and into the ``(argv, env, cwd)`` an interactive CLI runs when a person launches one. What the two compilations share without being either provider&#x27;s lives here, one module per concern. |
| `ledger` | One DAG of typed nodes per repository, and nothing about what they mean. Work that outlives the session which did it has to live somewhere a later session finds. Prose rots because nothing checks it; a per-branch file forks because every worktree holds one; a stored status keeps its label after the support for it goes away. This is the mechanism that avoids all three, and it is deliberately only the mechanism. |
| `mcp` | The MCP servers a session carries, each declared as a value. A server is named in an agent&#x27;s tools and read twice. A session this process opens hosts it, built against what that session has — its checkout, its roster identity, its container. A session a runtime&#x27;s own CLI launches starts it instead, as the transport that launch declares: a stdio subprocess serving it, for anything lup hosts. Both answers come from one value, so the session a library opens and the one a terminal launches carry the same servers, and neither is a list kept beside the other. |
| `observability` | What happened, recorded so that a later reader can answer for it. One subject rather than four top-level entries answering the same reader question. The ordered record file every durable log appends to; the lossless hash-chained audit stream a session writes as it runs; the compact markdown trace and its sidecar a later reader skims to find a session worth opening; the console display; the per-tool metrics; the replay divergence check; the per-turn cost arithmetic; and the account-level metered usage. What separates them is what each is kept *for* — evidence, navigation, or a bill — and never the mechanism, which they share. |
| `orchestration` | Running more than one piece of work, and staying able to speak to it. A cohort of addressable held sessions with mail that lands in front of each one&#x27;s next tool call; a background agent that coalesces wakes into turns; a scheduler and relay for work that sleeps; durable out-of-process jobs; the review gates a turn passes through; and spec-driven delegation for runtimes whose own subagents will not do. |
| `runs` | Work that outlives the tool call which started it, and stays watchable. A job long enough to be worth launching in the background is a job nobody can see. The answer here is one directory and a protocol over it: the run declares what it scheduled before starting, lands one atomically written result per unit, claims a unit while it works on it, and writes a line each time something happens. Everything a follower knows it reads from those, so a run launched detached, from another session, or before this shell existed is observable without being touched — and following one cannot perturb it. |
| `sandbox` | Docker-based Python sandbox, split by concern. A Docker-isolated Python REPL — mount topology, container lifecycle, and the exec-multiplexed socket protocol. Requires the `docker` extra. |
| `seams` | A seam over a library table: taking it as offered, saying what differs. A seam is *a place this library holds an opinion a project is meant to overrule*, which is the definition `dev seams` reads and writes by. Most of them are declarations in the adopting project&#x27;s own catalog and need no mechanism here at all. The ones shaped like a table do, and this is theirs: three reach a project as a starting point rather than a fixture — the anti-patterns it holds its code to, the shell vocabulary it runs, the edit gates it judges its own changes by — and in all three the only way to disagree with one entry was to restate the table around it, where a restatement fallen behind the library looks exactly like a decision. A project names what it drops and adds what the library lacks, keyed on the same id a directive, a denial and the generated reference already use, so an override replaces its namesake in place rather than sitting beside it. |
| `tools` | What an agent is given to act with, and what decides which of it it gets. The tool decorator and server surface, the conditional availability policy, the URL routing that sends a fetch to the tool that may serve it, and the LSP-backed code intelligence the agent tools are built on. One subject: the instruments, not the work done with them. |
| `web` | Local web surfaces: the boundaries a page served on this machine keeps. What a page served on this machine does to stay local-only — the loopback bind refusal and the `Host` check that DNS rebinding would otherwise walk past — and the one sequence that stands such a page up, with the bundles and view schemas it serves. One subject: a local HTTP surface a browser reaches. The two user-facing pages, `devtools/dashboard` and `devtools/supervisor`, sit *on* this; it does not belong beside them. |
| `workspace` | Session workspace: where a run&#x27;s data lives and how it is addressed. Where a run&#x27;s data lives: version-aware paths, the `SessionContext` that crosses a process boundary, session history, and the note directories a session may touch. |

### What is left to place

The roster above is where the tree stands, and where the three questions put
it. Thirty-four top-level entries became these by asking, of each one, which
of the four kinds it is: a foundation that imports nothing here, a subject,
the one vendor boundary, or tooling.

`resolver` is the entry the downward question is hardest on, because
everything that drives it is tooling: 12 modules under
`devtools/` read its journal, its state repository, its question mailbox and
its lease table. What keeps it a sibling of the subjects rather than a package
inside one is what it imports. Of its import lines, 30
reach `coordination`, where its actors, their durable sessions and the shared
question mailbox live, and 16 reach `harness`, so neither
subject contains it, and what it answers — reviewed concerns driven over a DAG
of branches, each on its own branch in a leased worktree — is a question
neither of them answers.

Ten two-way edges between entries survive the sort. Nine are placement
questions still open; the tenth is the shape of a guarantee.

| pair | what closes the loop |
|---|---|
| `tools` ↔ `coordination` | `tools/toolsets.py`, the registry every tool group is assembled from, reaches coordination's wake path and its peer and relay tools, which coordination declares in `tools.mcp`'s vocabulary |
| `tools` ↔ `ledger` | the same registry reaches the ledger's models, store and tools, which the ledger declares in `tools.mcp` |
| `tools` ↔ `orchestration` | the same registry reaches the review gate and the realtime relay, which declare their tools in `tools.mcp` |
| `tools` ↔ `sandbox` | the same registry reaches the container, which declares its tools in `tools.mcp` |
| `tools` ↔ `devtools` | the same registry reaches the Pyright oracle's language-server lookup under the tooling half, lazily, inside the code-intelligence group; the oracle and the resolver's command glue read `tools.lsp`, `tools.mcp` and `tools.native` back |
| `devtools` ↔ `harness` | utilities the library needs live under the tooling half — the clipboard probes, a launcher's default environment, `gh`, the sub-app roster, and the report and upstream-report models two pages render |
| `coordination` ↔ `ledger` | the ledger names who acted by coordination's `ActorRef` and member identity and renders its tasks, while coordination's hand-offs, delegations and tasks are ledger records written through `LedgerStore` |
| `coordination` ↔ `observability` | the cohort and its durable sessions write `observability`'s journal, whose session record names its actor by coordination's `ActorRef` |
| `observability` ↔ `workspace` | the sweep walks `workspace`'s run history and parses its timestamps, and that history and the notes are built from `observability`'s session recorder and metrics |

The first five have one shape: a registry sitting in the package that
everything it registers already imports. Every tool group is declared in
`tools.mcp`'s vocabulary, and `tools/toolsets.py` assembles them all, so the
edge closes by moving the assembly above what it assembles. The last four have
the shape the do-not-edit banner had: vocabulary both sides speak — an actor
reference, a journal record, a history reader, a clipboard probe — sitting
inside one of them, which closes by moving it below both, as
`lup.formats.banner` already did for the banner the policy bundle and harness
both write. `tools` ↔ `devtools` also carries the question the rest of the
table assumes an answer to: its one import back is deferred inside a function,
and whether a deferred import counts as an edge at all is the question to
answer before an acyclicity check is written — answering it by choosing a
walker that does not look inside a function would be hiding it rather than
settling it.

`harness` ↔ `policy` is the one that stays, because breaking it would break
what `policy` is for. Of `codescan`'s modules, 8 read `policy.kernel.edit`
— the tokenizer, the AST walkers, and the match-site finders that the
compiled hook script carries — and `policy` reads `codescan`'s anti-pattern
table back. That is not an accident of where the utilities happened to be
written. This package exists to decide identically in two homes, the compiled
hook and `dev check`, and one shared reading of the source is how the two are
held to the same answer. Cutting the edge would mean two implementations of
that reading, drifting apart on exactly the cases nobody thought to test —
which is the failure the package was built to prevent, reintroduced for the
sake of a tidier graph.

Acting on one of these answers is a command rather than an afternoon.
`uv run lup-devtools dev relocate old.module=new.module` repoints every import
of what moved, locating each module path by Python's own grammar rather than
by pattern, and reports the mentions it deliberately did not touch — a log
line, a docstring naming the old home — for a human to read. That the
mechanical half is cheap is what keeps the placement question answerable
instead of perpetually deferred.

`usage/` and the `usage/` beside each adapter are worth naming next to it as
the placement rule worked all the way through. What an account publishes is
the only thing that differs between runtimes — which windows it meters,
whether it splits a day's tokens by model — so that is what stays at the
vendor edge, and the report shape, the pacing bars and the rendering are
decided once above it. Neither reader carries a command of its own: each
declares an entry, and an application composes the ones it wants, so no Typer
app sits under `providers/` and nothing above `devtools/` imports one.

The outward question also runs the other way, and `dev check` asks it on every
run: the `application placement` row names each module under the application's
`devtools/` that imports nothing from the application. It reports rather than
fails, because the template is copied and frozen the moment an adopter takes
it while `packages/lup` reaches them through an ordinary dependency bump — so
the row is a debt that shrinks, and this is where its verdicts are settled
rather than a list kept somewhere else. It names nothing today, which is the
shape this debt is meant to reach: how a project obtains lup is a question
every adopter has and no part of which is about any one application, so it
lives at `lup/devtools/dev/library.py` where `dev update` reads the pin it
writes. The row is read rather than trusted — a module that reaches the
application, as `devtools/setup.py` does for its own harness composition,
leaves it by doing so rather than by being argued about here.

## Building on it

The library is the dependency; your application is the composition root. That
inversion is the whole design, and it has three practical consequences.

**Name the provider exactly once.** Declare the agent — `Claude(...)` or
`Codex(...)` — in one function, and hand it everywhere else as an `Agent`, so
the code that asks it works unchanged when the declaration changes provider.
`seam-boundary` refuses an import of `lup.providers.claude` or
`lup.providers.codex` outside a composition root, which keeps a provider's
own parts where it is named.

**Declare layers rather than wrapping by hand.** Timeouts, budgets, retries,
correction, persistence, and tracing are whole-turn decorators, each taking
its own config; an agent lists them in its `layers`, and every session it
opens is wrapped in them in the library's one stated order:

```python
from lup import Claude
from lup.sessions.layers import SessionLayers
from lup.sessions.middleware import RecoveryConfig, TimeoutConfig

agent = Claude(
    model="strongest",
    layers=SessionLayers(
        timeout=TimeoutConfig(seconds=600), recovery=RecoveryConfig(retries=2)
    ),
)
```

Code holding an agent it did not declare lays more on with
`agent.layered(SessionLayers(...))`, the fields it sets winning, without
knowing which provider it holds.

**Let typed output be the only output.** Pass a Pydantic model to `ask` and
read `TurnResult.output`. A missing submission raises a typed error carrying
the blocks, usage, duration, and validation history — it cannot arrive as an
empty success.

`src/lup_template` is the worked example of all three; see
[template.md](template.md).
