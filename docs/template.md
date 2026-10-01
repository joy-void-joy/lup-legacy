<!-- Generated from lup_template.harness.content.docs.template by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# The application template

`src/lup_template` is the half you fork. It is a complete working agent — an
`lup` application, a development CLI, and the harness declarations that
generate the native plugin trees — arranged so that adapting it to a new
domain is a series of small, obvious edits rather than a rewrite.

Three packages, split by what changes and how often.

| Package | Owns | Changes |
| --- | --- | --- |
| `agent/` | Reasoning. Prompts, output schema, tools, subagents, tool policy. No I/O. | Every time the domain does |
| `environment/` | I/O. The CLI that starts a run, the session lifecycle, what happens to a result. | When the surface changes |
| `devtools/` | Development. What this project adds to the inherited `lup-devtools` CLI, and the typed harness declarations. | When the workflow changes |

Keeping the agent free of I/O is what makes it improvable: the self-improvement
loop reads traces and changes prompts, tools, and models, and it never has to
reason about where a session came from.

```
src/lup_template/
├── agent/                   # Agent module for the self-improving loop.
│   ├── config.py            # Configuration management using pydantic-settings.
│   ├── core.py              # Application composition roots over Lup's provider-neutral runtime.
│   ├── models.py            # Output models for the agent.
│   ├── prompts.py           # The standing prose the agent is told: its task, its guidelines, and how to deliver a result.
│   ├── subagents.py         # Subagent definitions.
│   ├── tool_policy.py       # Decide which tools the agent is allowed to use this session.
│   ├── tools/               # Tools package - MCP tools and domain-specific utilities.
│   │   ├── example.py       # Example MCP tools showing the tool pattern and Data Augmentation.
│   │   ├── nested.py        # Nested Agent pattern (template).
│   │   ├── realtime.py      # Real-time MCP tools for persistent agents.
│   │   └── reflect.py       # Reflection tool — forced self-assessment before output finalization.
│   └── toolsets.py          # Which tool groups this project's sessions carry.
├── corpus.py                # This repository's corpus: claims, what backs them, and what retired them.
├── devtools/                # Development and analysis CLI tools for lup.
│   ├── agent/               # Agent introspection and interactive debugging tools.
│   │   ├── inspect_agent.py # Agent configuration inspection: tools, schemas, prompt, subagents.
│   │   ├── repl.py          # Interactive REPL with the agent via the SDK (continuous session).
│   │   └── serve.py         # This project's tool groups, built for inspection rather than served.
│   ├── dev/                 # Dev operations: pre-flight checks, review markers, and reading the repository.
│   │   ├── app.py           # What only a template adds to the `dev` tree the library already builds.
│   │   └── init.py          # Package renaming for downstream project initialization.
│   ├── main.py              # Root CLI app composing all devtools sub-apps.
│   ├── setup.py             # This project's setup integrations, over the reusable wizard framework.
│   └── subapps.py           # This application's sub-app delta: what it declines, and what only it has.
├── environment/             # Environment harness — how the outside world reaches the agent.
│   └── cli/                 # CLI package for the environment client.
│       └── __main__.py      # Environment CLI for running agent sessions.
├── harness/                 # What this repository declares about the harness its own sessions run under.
│   ├── catalog.py           # Root of the project-owned harness declaration graph.
│   ├── composition.py       # What this project publishes through each native target, and what writes it.
│   └── content/             # Declaration leaves of the harness graph.
│       ├── assets/          # Typed harness content declarations.
│       ├── catalog.py       # Which modules this repository takes, and what it changed about each.
│       ├── docs/            # Typed source for every document under ``docs/``.
│       │   ├── catalog.py   # Every document this repository publishes under ``docs/``.
│       │   ├── corpus.py    # This repository's corpus: a body of claims, what backs each, and what retired it.
│       │   ├── decisions.py # Architectural decisions behind the development tooling.
│       │   ├── index.py     # The documentation index: what this repository is, and where each part is.
│       │   └── template.py  # Guide to ``src/lup_template``, the application built on the library.
│       ├── guidance.py      # Canonical repository guidance.
│       ├── image.py         # The container this repository's agent sessions run in.
│       ├── modules/
│       │   ├── catalog.py   # The modules only this repository has, each spec beside its builder.
│       │   ├── examples.py  # The scaffold demonstrating itself, which no adopter runs.
│       │   ├── project.py   # What this repository is, and what it expects of a session working in it.
│       │   ├── specs.py     # What the modules only this repository has are called, and what they are for.
│       │   ├── template_init.py # Standing a lup project up, and keeping it configured once it is standing.
│       │   └── upstream.py  # Keeping a project in step with what it was built from.
│       ├── provenance.py    # What a project settles about where its lup came from.
│       ├── requirements.py  # The external programs this repository needs, and what going without costs.
│       ├── settings.py      # What this repository grants, refuses, and enables for itself.
│       ├── shell_vocabulary.py # Where this project's shell vocabulary differs from the one lup offers.
│       ├── skills/          # Typed harness content declarations.
│       │   ├── distill.py   # Canonical declaration for the distill skill.
│       │   ├── import_skill.py # Canonical declaration for the import skill.
│       │   ├── init.py      # Canonical declaration for the init skill.
│       │   ├── install.py   # Canonical declaration for the install skill.
│       │   ├── meta.py      # Canonical declaration for the meta skill.
│       │   ├── review.py    # The review skill as this repository reviews a session: against its agent.
│       │   ├── update.py    # Canonical declaration for the update skill.
│       │   └── upstream_skill.py # Canonical declaration for the upstream skill.
│       ├── template_claude.py # Canonical downstream template guidance in its Claude flavor.
│       ├── template_codex.py # Canonical downstream template guidance in its Codex AGENTS.md flavor.
│       └── template_sections.py # Portable downstream-template sections shared by every guidance flavor.
├── kinds.py                 # What this repository records in its ledger, declared once.
└── writeups.py              # This repository's writeups: documents generated from its own ledger.
```

Nothing above is written down. The structure is walked from the checkout when
this page is generated, and each caption is the module's own docstring — a
package's from its `__init__.py`. So a module that is renamed, moved, or
re-described changes this page by being edited, and one that is deleted leaves
it by being deleted. A module with no docstring simply has no caption, which
is the only nudge this page gives about writing one.

## `agent/` — what you change first

| Module | What it holds | Adapt it by |
| --- | --- | --- |
| `models.py` | `AgentOutput` and `Factor`: the structured result a turn must submit. | Replacing the fields with your domain's result. The prompt's output section is generated from this schema, so it cannot drift. |
| `prompts.py` | The system prompt composed from named sections. | Editing `PURPOSE` and `GUIDELINES`. Leave `output_format()` alone — it reads the schema. |
| `toolsets.py` | Which MCP tool groups a session carries, as one declaration: lup's own named, this domain's own built. | Writing a group's builder and naming it in `declared_tool_groups()` — server registration, the names a subprocess backend serves and the servers a runtime starts are all read off that list. |
| `tools/` | The tool implementations. `example.py` is placeholder search/fetch/read/glob; `reflect.py`, `realtime.py`, and `nested.py` are working patterns. | Replacing `example.py` with your domain's tools. |
| `subagents.py` | Portable `SubagentSpec` declarations: capabilities, exact tool grants, model tiers. | Adding specs to `ALL_SPECS`. |
| `tool_policy.py` | Which tools are available given the configuration — a missing API key bans its tools rather than failing at call time. | Adding an exclusion for each new conditional dependency. |
| `config.py` | Pydantic settings from `.env` and `.env.local`: model, budget, turn cap, sandbox, paths. | Adding settings, never reading the environment directly elsewhere. |
| `core.py` | `provider_factory()` — the **one** place a concrete adapter is named. | Rarely. Everything downstream takes the portable `Agent` it returns. |

That last row is the load-bearing one. `seam-boundary` permits a concrete
adapter import in `agent/core.py` and a short list of other composition roots,
and rejects it everywhere else — so provider choice cannot spread by
accident.

## `environment/` — where a run begins

`environment/cli/__main__.py` is the `lup` entry point, with `run` and `loop`
commands. A run opens a session through the agent's factory, executes the
task, and disposes of the result. The template's disposal is a git commit of
the session directory, which suits a batch domain and is the first thing an
interactive domain deletes.

The boundary is deliberate: outside events arrive here, and only here.

## Configuration

`.env` holds the template's committed defaults; `.env.local` holds secrets
and personal overrides and is gitignored. `.env.local` wins where both
declare a value. `ANTHROPIC_API_KEY` is read straight from the environment by
the SDK; everything else is loaded through pydantic-settings in
`agent/config.py`, which is the only module that reads the environment.

```bash
# .env.local — secrets and overrides

# AGENT_MODEL=claude-opus-5
# AGENT_MAX_BUDGET_USD=5.00
# AGENT_MAX_TURNS=50
# AGENT_REASONING_EFFORT=high   # low through ultra, on both backends
# CODEX_EFFORT=xhigh            # overrides AGENT_REASONING_EFFORT on Codex/OpenAI
# AGENT_SANDBOX_ENABLED=false   # run without Docker (disables code execution tools)
# AGENT_NOTES_PATH=./notes      # relocate session data
# AGENT_LOGS_PATH=./logs        # relocate trace logs
```

With `AGENT_REASONING_EFFORT` and `CODEX_EFFORT` both unset, a session takes
the model's default effort: `xhigh` clamped to the model's catalog row — the
first of `xhigh`, `high`, `medium`, `low` the row takes, none for a model
whose row lists no effort, and `xhigh` for a model with no row. The agent's
`resolved_effort()`, beside `model_id()`, answers which one a session gets,
and an effort named that the model lacks is refused where the agent is
declared.

## `devtools/` — the development CLI

`lup-devtools` is the second entry point, and most of it is not here. The
workflow sub-apps live in `lup.devtools` and are *inherited*: an upgrade
brings their improvements without a merge, which is the point — they are
development tooling, not this domain, and a fork of them goes stale the day
it is taken. Which of them this project serves is not written down anywhere: a
sub-app is one surface of a subject, so the roster follows the modules this
project adopted and is derived beside them in `harness/content/catalog.py`.
`devtools/subapps.py` declares the one thing that cannot be derived — what a
sub-app of this project's own is called — and `devtools/main.py` is where each
name meets the app answering to it.

That is also where `usage` is decided, twice over: whether to serve it, and
which backends' accounts it reads. The display, the pacing bars, and the
snapshot live in `lup.observability.usage`; each adapter contributes a reader that turns
its own account call into the one report shape, and the roster composes the
display around the readers it names.

- `agent` — Agent introspection and debugging
- `conversation` — Retain authenticated AI conversations
- `coordination` — Reach the other sessions working in this repository
- `dashboard` — Serve the operator's page over the parked reviews
- `dev` — Read this repository, and hold it to what it settled
- `feedback` — Feedback state, metrics, and commits
- `git` — Branches, worktrees, pull requests, and conflicts
- `harness` — Generate and launch the native harnesses
- `ledger` — Read and preserve the notes this repository has recorded
- `resolve` — Drive a resolver run, and watch or answer it
- `review` — The parked reviews an operator answers, and what each waits on
- `run` — Follow work that outlives its tool call
- `setup` — Interactive setup wizard, and its page
- `sync` — Stay in step with upstream: tracked repos, and what they owe
- `tools` — Serve the MCP servers a launched session declares, and read what their tools did
- `trace` — What a session left behind: its trace and its records
- `version` — Agent version, changelog, and bump

Run `uv run lup-devtools --help` for the full command tree. The four you will
use daily:

- **`git`** — worktrees, branches, pull requests, and conflict resolution.
  This is the git workflow.
- **`dev`** — the pre-flight `dev check` and `dev test`, review markers
  (`dev comments`), and reading the repository (`dev py`, `dev pending`).
- **`harness`** — generate, verify, and launch the native trees. See
  [harness.md](harness.md).
- **`trace`** — read what a session actually did: `trace show`, `trace search`,
  `trace errors`.

If you run the same shell incantation twice, add a command here instead. The
CLI is written with [typer](https://typer.tiangolo.com/), and shells out with
[sh](https://sh.readthedocs.io/) rather than `subprocess`. One-off work goes
through the reviewable ladder in
[contributing.md](contributing.md) rather than a script in `tmp/`, which is
gitignored and so reaches no diff and no reviewer.

### `harness/` — the declaration graph

Declaration content sits above the tooling that compiles it, so the harness is
its own package rather than a corner of `devtools/`. `catalog.py` is the root:
it assembles the skills and agents this project composes with the
application-owned `HookSet` and the resolver spec into one `Harness`.
`content/` holds the leaves — one module per skill, per agent, per document —
and `content/modules/` groups them by subject: a module carries its content,
its page, its paragraph in the always-loaded document, its command tree and
its tool group, and `content/catalog.py` states which of them this project
takes. What the plugin ships, what `docs/` publishes, what the CLI serves and
what a session is offered are all derived from that one answer.
[harness.md](harness.md) is the guide; this is only where the files are.

### The setup page

The setup page is the web face of the same declarative `INTEGRATIONS`
registry that `uv run lup-devtools setup` walks in the terminal: a domain
customizes the registry once and gets both. `uv run lup-devtools setup serve`
serves it at `http://127.0.0.1:8765`, and the dashboard runs it to show it as this repository's setup pane.

A progress-oriented wizard covers first setup; an all-integrations view covers
later maintenance. Browser forms are generated only for declarative
environment fields, from an explicit per-integration allowlist, so the page
cannot write an arbitrary variable; anything needing OAuth or bespoke
validation routes to its existing CLI command. FastAPI serves the `wizard`
surface Vite built into `lup.web`'s package data — the wheel carries the
bundle, and bun is needed only to change it — and `--no-open` and `--port`
cover the cases where the defaults do not fit.

`--host` takes only a loopback address, and every request's `Host` header is
checked against one. The page writes credentials into `.env.local`, and a
local bind alone leaves that reachable by DNS rebinding from any page the
browser has open. Both halves are `lup.web.loopback`, shared with the
resolver's supervisor page; see [supervisor.md](supervisor.md).

An integration declared `host_only=True` is answered into the project's host
store instead, `$XDG_CONFIG_HOME/lup/secrets/<project>.env` (owner-only, one
file per `[project]` name, shared by every worktree): the place for a key a
host companion is started with and a session must never hold. No session
reads it — a contained launch refuses a mount reaching the person's lup
config, a launch takes every name the store holds out of what the session
inherits, and a `SharedProcess` is started with only the keys its `secrets`
names. `uv run lup-devtools setup secret <KEY>` sets one no integration
declares, `--unset` removes it. Inside a container each of these refuses
before anything is typed, naming the command to run on the host.

### The sync registry

`lup-devtools sync` tracks the other repositories this project exchanges
improvements with and reviews their commits since the last sync. The /lup:update and /lup:import skills are built on it. Two files declare
what to track.

The split between them is by whose fact each key is, not by which key it is.
What the *project* declares — which repository a name means, whether the
project can work without it, and what a session may reach of it — is the same
answer on every machine and belongs in the committed half. What *this machine*
answers — where the checkout is, and which transport gets there — belongs in
the gitignored one, and most machines answer only the second.

**`sync.json` (committed)** names the upstream, says which repository it
is, and says what this project needs of it:

```json
{
  "projects": [
    {
      "name": "lup",
      "url": "https://github.com/joy-void-joy/lup",
      "required": true,
      "mount": "rw"
    }
  ]
}
```

The URL is what makes the entry placeable on a machine that has never been
told anything: a project generated from the template inherits it, and its
first launch after initialization clones lup under
`~/.cache/lup/sync/lup.git` and mounts it read-write, with no `sync setup`
anywhere. It is spelled https because that clone is made on the host before
any credential is lent, and https reads a public repository with none; inside
a session every mounted checkout's remotes are rewritten onto the transport
the launch's credential reaches, so a push goes out on the operator's ssh key
or token alike. /lup:init points it at the repository the project was
actually generated from when that is a fork (`dev init upstream`).
Where the project resolves lup from a repository the entry
follows that pin rather than its own URL, so the two never name different
repositories.

`"required": true` says the project cannot work without that repository
present: the workflows that fix a defect upstream, derive a relocation map
over upstream's own history, or review its commits all open that checkout.
Nothing clones behind anybody's back — `sync fetch` materializes every
requirement, a launch materializes what it is also going to mount, and `sync
log` clones on first use, each saying so as it goes. A requirement this
machine has not answered is named by `sync status`, with the command that
answers it, and makes it exit nonzero; the absence is a result rather than
something the next command discovers.

Two registrations are owed by nobody: one naming the repository this checkout
already is, read off its own origin, and a committed one read inside the
template scaffold (`[tool.lup] template = true`), whose `sync.json` is the
file an adopter receives. Neither is cloned, mounted, fetched or reported
missing, and `sync status` says which one a row is, so the lup repository and
every fork of it need no opt-out for the entry they ship.

Most machines configure nothing. One that reaches lup differently says so,
once:

```bash
uv run lup-devtools sync remote lup git@github.com:joy-void-joy/lup.git
uv run lup-devtools sync setup lup /path/to/repo --mount rw
```

The first records the URL this machine fetches from — say, over ssh where it
pushes lup with its own key outside any container, where no launch rewrites a
remote — and repoints the clone already made to it; the second records a
checkout this machine already has, which is then what sessions open — the
working tree it resolves to, rather than the whole clone a URL registration
mounts (below). A registration with neither is materialized at
`~/.cache/lup/sync/<name>.git`, derived from the name, so a machine that
keeps its clone there needs no local entry at all.

Repository identity is configured independently from the adopting project's
own Git origin. `uv run lup-devtools dev library git --url <repository>` selects the dependency's
source explicitly. Without `--url`, it uses the existing Git dependency pin,
then the scaffold's named sync registration: its URL, or its checkout's origin.
An absent source is reported before any pin is changed. Library friction reports
use that same configured upstream; the consuming project's reports use its own
origin, and are the only ones filed without a question — one routed upstream
prints the command naming that tracker with `--repo`, which asks. A `lup`
entry that names no URL, remote or path — one committed before the entry
carried a URL, which no update rewrites, since root files are the
project's own — falls back to the source repository the installed library
declares in its `[project.urls]`.

It is scaffold, not personal state. **Agents must never modify the tracked
`sync.json`**, and neither should routine project work; the edit policy
enforces this by treating it as a protected path, which is also what keeps a
`"mount"` there from being widened without the question being asked. Every
per-machine registration belongs in the gitignored **`sync.json.local`**:
checkout paths, the `"remote"` this machine fetches through, branch
overrides, `"ignore": true` opt-outs, and additional projects. Entries there
override tracked entries by name or add local-only ones. `sync setup` and
`sync remote` write that registration. `sync mark-synced`
stores a checkpoint under the repository's common Git directory, so every
sibling worktree reads the same review progress. A project with no checkpoint
has never been reviewed. The checkpoint is bound to its reviewed ref and
source: the
origin URL for fetched reviews, or the local Git repository for unpublished
work. Two independent local clones keep separate unpublished checkpoints.

`sync fetch` refreshes remote-tracking refs without moving a local branch or
touching work in an attached checkout, and materializes a required
registration even where `"ignore": true` withholds its review: being the
upstream of this project is a reason not to read its commits back and no
reason for a workflow that opens the checkout to find nothing there. `sync
status` names the exact ref it reads. Registrations with an origin review
`refs/remotes/origin/<branch>`;
use `sync setup <name> <path> --review-from local` to review unpublished local
work. A repository without an origin is itself the upstream. An unbranched
`lup` registration follows the Git dependency's branch; an explicit branch
mismatch is reported. A failed fetch exits nonzero.

The registry has no direction in its name because direction depends on where
you sit. A project built on the template inherits the shipped `lup` entry and
pulls *from* it. The lup repository itself is exempt from that entry by its
template flag and registers its downstream fleet in `sync.json.local`, so /lup:update can generalize emerged patterns back into the template. Same
registry, opposite seats.

An entry may also carry a `"mount"` of `"rw"` or `"ro"`, which is a
declaration about *access* rather than about review: a session opens that
project at its own path, inside the container as well as outside it, and
`refs/<name>` resolves there rather than dangling. Written or absent, never
defaulted — tracking a project and handing a session the keys to it are
different claims. Either file may carry it, because which repositories a
project's own workflows write in is the project's decision and read-write
against read-only is part of it, rather than something each machine settles
for itself when it first meets the absence. A tracked mount binds nothing on
its own: until this machine says where the project is, there is nothing to
bind, so the committed claim and the per-machine answer stay separate.

`"url"` is which repository the entry means, and `"remote"` is the URL this
machine fetches it from. They are compared on what each one names — the host
and the path — so an ssh clone of an https registration is one repository and
not a disagreement, and the committed file keeps naming the repository every
machine shares. Two registrations that genuinely name different repositories
are still refused, in a message naming the file each half sits in and the
exact edit, because a clone of somebody else's history under this name would
be reviewed, mounted and committed into as this one.

The same file grants host devices, for the same reason: `sync grant
nvidia.com/gpu=all` writes the CDI name into a top-level `"devices"` list
after starting a throwaway container with it, and every session and resolver
worker opened on this machine is handed it from then on. Which GPU a machine
holds is that machine's fact, so the list lives only in the local half, never
in a committed declaration; `sync revoke` takes one back, `sync status` shows
each grant beside whether a spec on this machine still names it, and the
launchers take `--device <name>` for one launch.

A registration that names only a URL is materialized under
`~/.cache/lup/sync/<name>.git` in the layout one naming a local path already
points at: a full bare clone — every branch, whole history — with a worktree
attached at `tree/<branch>`, which `refs/<name>` names. What is mounted is
the whole clone, so `git worktree create` inside it lands its next checkout
beside the first and inside the same mount, where `harness policy-refresh`
accepts that worktree's own policy without another launch. A registration
naming a local path mounts only the working tree it resolves to, so a
worktree cut beside that one is judged by this project's policy until a
launch grants it.

The cache sits outside the project deliberately. A clone under the checkout
is inside the session's own writable mount, which makes a `"ro"`
registration silently `"rw"`, and it is re-cloned once per worktree where
the history is worth having once per machine.

Nothing a review does moves a branch in one of those clones. The upstream's
commits are read from its remote-tracking ref rather than from `HEAD`, so
refreshing is a fetch: `sync fetch`, `sync log` and `sync diff` leave a
branch cut in the clone, a commit made on it, and every uncommitted file
beside it exactly where they stand. `sync log` reports what the *upstream*
added, never what a session working in the clone did.

## How the two halves depend on each other

`lup_template` imports `lup`. `lup` never imports `lup_template` — it is
published standalone and could not. The placement test for any new utility is
the same question in both directions: *would another project built on lup want
this?* If yes it belongs in `packages/lup/`; if it only makes sense for this
application it belongs here.

`lup-agents[claude,codex,conversation,docker,web]` is declared as a workspace
dependency in the root `pyproject.toml`, so a checkout resolves the library
from source and an edit to either half is immediately live in the other.
