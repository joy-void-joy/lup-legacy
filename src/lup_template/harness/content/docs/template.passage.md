<!-- passage: setup-page -->
### The setup page

The setup page is the web face of the same declarative `INTEGRATIONS`
registry that `uv run lup-devtools setup` walks in the terminal: a domain
customizes the registry once and gets both. `uv run lup-devtools setup serve`
serves it at `http://127.0.0.1:8765`{{ setup_pane }}.

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
browser has open. Both halves are `lup.web.loopback`{{ shared_with_supervisor }}.

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


<!-- passage: sync-registry -->
### The sync registry

`lup-devtools sync` tracks the other repositories this project exchanges
improvements with and reviews their commits since the last sync. The {{ update_skill }} and {{ import_skill }} skills are built on it. Two files declare
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
or token alike. {{ init_points }}Where the project resolves lup from a repository the entry
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
sibling worktree reads the same review progress. Existing `last_synced_commit`
values are used until a shared checkpoint is recorded; the shared record wins
over stale local values and is bound to its reviewed ref and source: the
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
template flag and registers its downstream fleet in `sync.json.local`, so {{ update_skill_2 }} can generalize emerged patterns back into the template. Same
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


<!-- passage: template -->

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

{{ setup_page }}{{ sync_registry }}## How the two halves depend on each other

{{ application }} imports `lup`. `lup` never imports {{ application_2 }} — it is
published standalone and could not. The placement test for any new utility is
the same question in both directions: *would another project built on lup want
this?* If yes it belongs in `packages/lup/`; if it only makes sense for this
application it belongs here.

`lup-agents[claude,codex,conversation,docker,web]` is declared as a workspace
dependency in the root `pyproject.toml`, so a checkout resolves the library
from source and an edit to either half is immediately live in the other.
