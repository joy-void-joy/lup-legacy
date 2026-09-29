<!-- Generated from lup.harness.content.docs.contributing by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Contributing

This page is for a contributor arriving cold. It covers getting a working
checkout, deciding where a change belongs, and what has to be green before it
lands. The component guides — [library.md](library.md),
[template.md](template.md), [harness.md](harness.md) — cover *how* to make a
change once you know where it goes.

## Getting set up

```bash
uv sync                                    # both workspace packages
uv run lup-devtools setup                  # interactive: keys, integrations
uv run lup-devtools dev check --changed     # ruff + pyright on what you changed
uv run lup-devtools dev test <paths>       # while iterating: only these files
uv run lup-devtools dev check              # the bar for landing
```

Three commands, because they answer different questions, and which one you owe
depends on what you are about to do. `dev check` puts both test suites, pyright
and ruff on the machine at once and costs whichever of them finishes last — a
couple of minutes — and it is **the bar for landing**: run it on the integrated
result before work reaches the integration branch, which is what `/lup:land` does — the
only moment the whole answer means anything. The gate reports what
each of its checks cost, so a run that felt slow can be read rather than
guessed at.

A commit on a feature branch owes the scoped pair instead — `dev check
--changed` over what you touched, and `dev test` over the files your change
reaches. The full gate is not forbidden there and is often worth running; what
it is not is owed on every commit, and treating it as owed is how several
agents sharing one working tree each start the whole suite at once. Over a tree
still being edited that answer is about a state that never existed, and a
failure in it cannot be attributed to whoever caused it — so a delegated agent
runs the scoped pair over what its change reaches and leaves the full gate to
whoever lands it. Whether it commits follows the tree it works in: in a
worktree of its own it commits its work, and in a checkout it shares it leaves
the commit to whoever dispatched it, since a commit there would carry what the
others left half-edited beside its change.

The other two are the loop while a change is still moving. `dev check
--changed` runs ruff and pyright over the Python files changed since the merge
base with your branch's recorded base (or with `--since <ref>`), in seconds —
those are the two checks a scope narrows *exactly*, because each answers about
the files it is handed and Pyright resolves their imports itself. The merge
base rather than the base's tip, so a branch answers for its own commits and
not for what the base took since the cut. It also runs the declared-migrations
row from that base, since a public name removed without a migration is a
one-file mistake. It runs **no tests** and no other whole-tree sweep, and every
run names them, beside each changed file it did not read. `dev test` runs the
test files you name, in the suite that installs each.

Which tests reach a change is deliberately left to you. It is a question about
the import graph, and modules reached through `importlib` are invisible to any
static reading of it — so a suite narrowed automatically could report green
while skipping the one test the change breaks. A gate that is trusted and
wrong costs more than one that is slow.

Running several at once is held to the machine by the commands themselves,
because each suite alone would spread itself across every core there is.
`dev check` and `dev test` each hold one of four slots the clone keeps under
its shared git directory, across every worktree, and spread their suites over
the share of the cores that the runs already under way leave — four runs of
four workers rather than four of sixteen. A fifth waits for a slot and says so
as its wait begins, so a run that seems to hang with that line above it is
queued rather than stuck; after half an hour it goes ahead at full width, in
case a holder died without releasing its slot. `dev check --changed` and
`dev check --no-test` hold none: neither runs a suite, and Pyright checks on one
core, so a slot either held would divide nothing of its own and narrow every
run opening beside it for the whole of that run. What the slots do not divide
is the repository — a suite reading one whose branches another command is
moving fails on that rather than on the code.

`uv` is the package manager: use `uv add <package>`, never edit
`pyproject.toml` by hand. Secrets go in `.env.local`, which is gitignored;
`.env` holds template defaults. `uv run lup-devtools --help` is the full
command tree.

To launch the repository as a native agent plugin:

```bash
uv run lup-devtools harness claude          # generate, then launch
uv run lup-devtools harness codex
```

## Where does my change go?

| If you are changing… | It belongs in | And you should read |
| --- | --- | --- |
| Anything another project built on lup would want | `packages/lup/` | [library.md](library.md) |
| Anything only this application needs | `src/lup_template/` | [template.md](template.md) |
| A skill, agent, guidance, permission policy, or a page under `docs/` | `packages/lup/src/lup/harness/content/` where the library owns the subject, `src/lup_template/harness/content/` where only this application does | [harness.md](harness.md) |
| Repeated shell incantations | a new `lup-devtools` command | [template.md](template.md) |
| A one-off computation | a new `lup-devtools` command | below |

The placement question between the first two rows is the one that matters, and
it has a single test: *would another project built on lup want this?* If yes,
it goes in the library even if only this application uses it today. The
library never imports the application, so a utility placed wrongly in
`src/lup_template/` is unreachable from the library and will have to move
later. Deciding a module belongs on the other side is one line of judgement
and a hundred of consequence, which is where the judgement gets abandoned —
so the consequence is a command: `dev relocate old.module=new.module`
repoints every import and reports the mentions it left.

`tmp/` is scratch: gitignored, so nothing written there reaches a diff, a
reviewer, or a human. One-off work takes the first of these that fits:

1. To read code rather than run it: `py info`, `py source`, `py search`, `py text`,
   `py imports`, and the codeintel tools. Resolve names with `py search` or
   codeintel, and rename with `rename_symbol` rather than `replace_all`
   ([conventions.md](conventions.md) says what each tool answers); find literal
   text in explicitly scoped Python paths with `py text`, and characters in
   non-Python files with grep.
2. To compute something once: a script under `tmp/`, run directly. It imports
   this checkout the way any other module does, and the session it runs in is
   itself contained, so the objection — an unreviewable thing executing
   outside any boundary — holds for its first half alone. A one-off
   nobody will read again costs a reviewer nothing.
3. For anything you will want twice: a new `lup-devtools` command, which
   lands in the diff and can be run again by name rather than rewritten. The
   policy counts how often each script runs and says so when one has passed
   what a one-off is for — advice riding along with a verdict that already
   allowed the command, not a gate. It arrives at the fifth run and then
   every tenth, because a session that was mid-thought at the first one has
   to hear it again, and one that hears it every run stops reading it.
4. As a last resort, an inline heredoc behind an escalation marker
   ([permissions.md](permissions.md)).

No rung between the first two evaluates an expression inside its own
container. The agent session is contained, so a second boundary within it
buys no isolation — and actively gets in the way, since a tool inside one
cannot import the very checkout it is asked about.

The argument is reviewability, not power: an agent may already edit
`devtools/` and run it.

A result too large to return does not come back at all: the runtime persists
it to a file and returns a short preview naming that file. Hand the file to a
reader that takes a file whole. `cat`-ing it is another result too large to
return, persisted to another file, and `cat`-ing that one repeats it — a
regress whose every step looks like the command having worked.

Never create a tracking file, and never write to the harness's persistent
memory — a file per profile, unversioned, unreviewed. A `TODO.md`, backlog,
roadmap, or memory file parks a decision where no workflow surfaces it again:
delegation to nobody. Deferred work lives as a
`# lup: defer: <text>` note at the site it concerns — where `dev comments`
lists it in its own parked section and `dev check` keeps it visible until
somebody wakes it. That bare spelling is the
default, and a bracketed `defer[<gate>]: <text>` states a real,
externally-checkable gate — never a restatement that this code might change
again.

Some gates this checkout can resolve, and those it does. `dev check` asks them
every run, reports them among the other deferrals while the answer is no, and
fails the run the answer turns yes. `defer[gone:<path>]` wakes once that path
stops existing.

`defer[branch:<name>]` wakes for whoever is standing on that branch, and again
if the branch lands with nobody having acted. The first is the point. A note
about a branch is written by somebody standing somewhere else, and the person
it concerns is on the branch it names, in a checkout that carries no copy of
it — so the check reads the integration branch as well as the working tree,
for the notes naming the branch in hand. Write one where you are, aimed at the
branch that has to act, and it reaches them without waiting for a merge.

Landing wakes it even where the branch was deleted in the same sweep, because
`git delete` judges containment off the ref it is about to remove and records
that verdict beside the branch. A checkout that never deleted it holds no such
record and stays quiet — which is what keeps a clone that merely never fetched
the branch, every CI job among them, from waking every gate in the repository.

A gate the checkout cannot see — "until the v2 API ships" — stays prose and
stays advisory, which is the whole of what a stated gate ever did before.
Prefer a resolvable spelling where one fits, because a deferral is dormant
exactly as long as nobody has reason to read it, and the moment it stops being
dormant is the moment nothing else announces.

A note is right when the subject is the code: a bug worth remarking on, an
idea for a feature, anything the site it concerns can hold. Work whose
subject is the tooling misbehaving — friction, a command that half-completes,
a classifier reporting a failed probe as fact — has no site to sit at, and
becomes a GitHub issue instead, since a narrated workaround teaches nobody; so
does a milestone or plan, when the repository is deciding what comes next. A
rule every future session should carry goes into the guidance's source, then
regenerated. What a fresh session should pick up goes into a `tmp/` briefing,
rewritten whole and never appended. When whether to defer at all is the open
question, it becomes a question to the user rather than any note.

## Git workflow

Development happens in **worktrees**, not branches switched in place, so
several changes can be in flight at once:

```bash
uv run lup-devtools git worktree create feat-name
```

The worktree is created as a sibling under `tree/`. Never nest one inside
another checkout. `git checkout -b` would make a branch and switch the
current directory in place; `git worktree add` gives the branch a directory
of its own, which is what keeps several live at once. `worktrees/` and
`refs/` are gitignored, the latter holding symlinks to the projects this
repository tracks.

Those symlinks resolve inside a contained session only for a project whose
registration carries a `"mount"` of `"rw"` or `"ro"`, in `sync.json` where
the project decides it for every machine or in `sync.json.local` where one
machine does —
`sync setup <name> <path> --mount rw` writes one, and `sync status`
shows which projects have it. A mounted project is leased whole: its
checkout at that mode, with its shared git directory and its sibling
worktrees, which is what lets a session commit in it. Without the
key the project is tracked for review and nothing more, and the symlink
dangles inside the container the way an unmounted path does. The key is why
both registry files are protected edit roots, and why every command writing
one asks the same question: writing one widens the boundary
([permissions.md](permissions.md) lists the writers). A tracked mount binds
nothing until this machine says where the
project is, and a tracked `"required": true` is what makes that absence a
report with the command that answers it rather than a workflow that cannot
start.
For a folder one session needs without a standing registration, the launchers
take `--mount <dir>` and `--mount-ro <dir>` (repeatable): the same lease, the
same widening in every posture, lasting exactly one launch.

A host device — a GPU — is granted on the same terms and from the same file.
`sync grant nvidia.com/gpu=all` writes the name into `sync.json.local` after
starting a throwaway container with it, so a grant nobody's engine can honour
is refused where it is made; `sync revoke` takes it back, `sync status` shows
each grant beside whether a spec still names it, and the launchers take
`--device <name>` (repeatable) for one launch. The name is the Container
Device Interface's, `vendor/class=device`; the nodes under `/dev` and the
driver libraries beside them are the registered spec's to inject, so nothing
here enumerates either, and nothing committed names one: which GPU a machine
holds is that machine's fact. Every launch reads `/etc/cdi` and
`/var/run/cdi` on the host, hands the engine what a spec there names, and
withholds the rest with one line, because `/var/run/cdi` empties at boot and
a driver update regenerates a spec. A spec is written by the vendor's
toolkit — `sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml` for
NVIDIA — and Docker reads the registry from 28.3 (an older daemon needs
`"features": {"cdi": true}`; podman always has). What was granted is written
to `.lup/boundary.json` beside the mount table, so a run's provenance says
which devices its container held, and `harness requirements` re-exercises
every grant it finds.

A registration naming only a URL is materialized into the same shape: a full
bare clone under `~/.cache/lup/sync/<name>.git` with a worktree attached at
`tree/<branch>`, which `refs/<name>` names. Commit in it, cut branches in it,
push from it — the remotes of every mounted checkout are rewritten onto the
transport this session can reach, not just the one it was launched from. A
review never moves a branch there: `sync` reads the upstream's commits from
its remote-tracking ref, so refreshing is a fetch and nothing in the clone is
reset over.

The launch mounts such a clone whole rather than at its one worktree. The
lease is the same either way — every worktree writable, the shared git
directory read-only with its data directories writable, so `config` and
`hooks/` cannot be written — and what the whole clone adds is edit authority:
each worktree in it is judged by its own policy, and `harness policy-refresh`
accepts one cut after the launch. A registration naming a path mounts the
working tree it resolves to and accepts that checkout's policy alone, so a
worktree beside it needs a launch that grants it.

The `lup` entry `sync.json` ships is one of these. It names lup's repository
over https, required and mounted read-write, so a project built on the
scaffold holds lup from its first launch with nothing set up on the machine:
the launch clones it where the cache has none, and `refs/lup` is its working
tree. Where the project resolves lup from a repository, the entry follows
that pin instead of its own URL, so the clone is always of the repository the
library comes from. The scaffold itself is exempt, and so is any checkout of
the repository an entry names: neither is cloned, mounted, fetched or
reported missing, and `sync status` says which one it is.

The base the branch is cut from is recorded against it, because topology
cannot recover a creation point once the parent has merged on. A fresh branch
takes the integration branch, where work lands — but the checkout you run in
is often a worktree on a branch of its own, and continuing *that* work is as
real an intent as starting new work beside it. Both arrive as the same
command, so where the checkout carries commits the integration branch lacks,
creation asks which you mean and names both spellings of `--base <branch>`,
before there is a branch to reset. Where it carries none, the two bases are
one line and the integration branch's tip is taken without a word.

A detached HEAD has nothing to read at all: rather than record nothing and
let a later reader guess, creation refuses and asks for `--base <branch>`, or
`--no-record` to say deliberately that this branch has no base worth keeping.

The command prints the path and does not move whoever ran it. **Launch a
session rooted at that path**; do not relocate a running one. The difference
is not style. A runtime that can move a running session arms its own
worktree isolation when it does — a check on command *shape*, separate from
this project's policy and owned by nobody here, which refuses a command
carrying any of fifteen shell words as an argv element in any position:

    eval  source  .  fc  coproc  trap  enable  mapfile  readarray
    hash  bind  complete  compgen  alias  let

Only `.` is gated to first position; the other fourteen match anywhere, and
none of them is gated on the command being a git command. So an isolated
session loses `grep -c hash` and `rg complete src/` — read-only commands with
no git in them — for as long as it lasts, and no approval marker reaches the
refusal.
Relocation is bounded as well as expensive: `git worktree create` cuts under
a sibling `tree/`, outside the `.claude/worktrees/` a relocating tool
switches within, so such a path is taken only as a session's first entry from
the directory it launched in. A session already sitting in one worktree is
refused a second switch by the tool itself, whatever this project decides, so
stacking a branch means a launch or absolute paths either way.
A session launched already rooted in the worktree is never isolated and
keeps all of them, which is why the workflow asks for a launch. Staying put
and editing through absolute paths works too, but only into a worktree that
is writable, and which those are is the lease's to say (`lup.sandbox.rail`):
an operator's contained session holds every checkout of its repository
writable, so that route reaches any sibling; a resolver worker's lease holds
every sibling that existed when the worker started read-only, so there the
route reaches a filesystem refusing every write, and only a worktree the
worker cut itself takes edits. The isolation is measured against Claude Code
2.1.237; `docs/native-capabilities.md` carries the evidence.

Commit early, commit often, and keep commits atomic — if the message needs an
"and", it is two commits. The format is `type(scope): description`:

| Type | Use |
| --- | --- |
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `refactor` | Neither fixes a bug nor adds a feature |
| `docs` | Documentation only |
| `test` | Adding or updating tests |
| `chore` | Maintenance — dependencies, build config |
| `meta` | Harness content and the trees it generates: guidance, settings, skills, hooks |
| `data` | Generated data and outputs |
| `release` | What `dev release` commits for a release or a candidate of one — never written by hand |


A `data` commit of generated outputs may go straight to `dev`; code never
does. Session data under `notes/` is gitignored here, so such commits arise
only in a repository that opted into the commit-loop pattern at init.

Two branches: `dev` is the integration branch feature work merges into, and
`main` is stable and receives only reviewed pull requests from `dev`. Never
commit code directly to `dev`.

/lup:rebase pushes, opens the pull request, and rebuilds history
with `git reset --soft main` and a force-push; re-run it after each round of
review fixes. /lup:close merges the approved one and cleans up.
/lup:merge guides conflict resolution.

During a merge the bias is toward inclusion: audit the result
against both parents and confirm every removed function, parameter, or command
was removed deliberately rather than lost to a conflict side.

Generated artifacts are regenerated, never hand-merged. Every file in a
generated tree conflicts on parallel branches because every line is derived,
so `.gitattributes` declares those trees under a driver that keeps one side,
and `lup-devtools git merge-driver` registers that driver in a clone that has
not run `worktree create`. Reconciling such a file hunk by hunk produces an
artifact matching neither tree: take either side, run
`lup-devtools harness generate all`, and let `harness check all` confirm it
settled.

That declaration is a lockout guard as much as a convenience. One file in
those trees is executed rather than read — the compiled hook dispatcher — so
conflict markers in it leave a script that will not parse, and the permission
boundary answers every shell command and every edit by refusing, `git merge
--abort` included. Reaching that state, the refusal says which build product
broke and what rebuilds it, and the rebuild has to be run from outside the
session. The driver is per-clone git config, so it covers a merge performed in
a clone that registered it and nothing else: a merge run on the forge's own
server reads no config and lands the conflict anyway.

Before publishing a PR, fetch its target and run `uv run lup-devtools git pr
prepare --base origin/<target> --json` in the clean feature checkout. This
merges the exact target commit using the generated-tree driver, regenerates
all harnesses from the combined sources, and commits locally. It pushes
nothing. The resulting head contains the target as an ancestor, so a forge
needs no custom merge driver. Source conflicts remain open for `git conflict`
repair; regenerate before completing that merge. Re-run preparation if the
target advances or the feature history is rebuilt.

Cleanup checks the live coordination roster as well as Git worktree locks.
A clean checkout owned by a live session remains protected even with
`--force`; removal becomes available after the session departs or its pulse
expires. Cleanup rechecks ownership immediately before removing the tree.
A session owns a checkout it was launched in, and one it created: `git
worktree create` locks the new checkout with the creating session's roster id
as the reason, since that session usually writes there by absolute path from
another checkout. Only another live session is refused by that hold; the
creator removes its own, and once it leaves the roster the hold is dropped
by whichever removal comes next.

Undo snapshots publish and retire duplicate refs in one fsynced Git reference
transaction. `dev undo` also reports empty or null loose undo refs, which Git
omits from its ordinary listing but which can break fetch. Run `uv run
lup-devtools dev undo --repair` to quarantine those bytes under the shared Git
directory's `lup/undo-damaged/` directory. Valid snapshots and active ref locks
are preserved; the command prints every quarantine path.

## What has to be green

```bash
uv run ruff format . && uv run ruff check . && uv run pyright && uv run pytest
uv run lup-devtools dev check              # markers, anti-patterns, boundaries
uv run lup-devtools harness check all      # generated-tree drift
uv run lup-devtools dev rules --check      # the generated rule reference
```

The type-checking gate, code intelligence, and rule resolver give Pyright
the interpreter selected for the requested checkout. `UV_PROJECT_ENVIRONMENT`
is resolved against that project; with the variable unset, `uv` selects
`.venv`. The scaffold leaves `venvPath` and `venv` unset:
Pyright gives an explicit pair priority over that interpreter. Deliberate
project environment declarations retain that priority for both code
intelligence and the type-checking gate. After adopting a
scaffold that supplied `venvPath = "."` and `venv = ".venv"`, run
`uv run lup-devtools dev migrate pyright-environment --dry-run`, then omit
`--dry-run` to retire exactly that unchanged pair. The migration preserves
custom or partial selectors, configurations using `extends`, and separate
`pyrightconfig.json` files. Restart an already-running code-intelligence
server after changing its environment configuration.

For tests spanning the application and library, use
`uv run lup-devtools dev test tests/unit/test_toolsets.py packages/lup/tests/unit/test_lup_tool.py`.
It starts a separate pytest process in each declared test root, preserving
each suite's configuration and imports. A raw pytest invocation naming both
roots can fail while importing `tests.conftest`, because both independently
installed suites use that package name. Recover with `dev test` over the same
paths; changing import mode does not separate those packages. The runner uses
parallel workers only when pytest-xdist is installed; otherwise it runs serially.

The generated trees include the frontend bundles under `lup.web`'s package
data, built from `packages/lup/web/` by Vite, so the gate needs `bun`. The
workspace's dependencies it restores itself, the way `uv run` syncs the
environment before running: where `packages/lup/web/node_modules` is missing
or older than `bun.lock`, the bundle build and the `bun test` row run
`bun install --frozen-lockfile` first, and `git worktree create` runs it
beside `uv sync` (both skipped by `--no-sync`), so a fresh worktree is ready.
A restore that fails is the row's verdict, carrying bun's own output. The
policy allows that frozen restore, and `uv sync --frozen` and `uv sync
--locked` on the same reasoning — a frozen lockfile pins every package by
integrity hash, which is what `uv run` already fetches unasked — while
`bun install` without the flag, `bun add`, `uv sync` without a freeze flag and
`uv add` ask, since each can rewrite the lockfile. The workspace's own tests
are a third suite beside the two pytest roots, run by `bun test` from the
workspace, so a green gate ran the frontend's tests too. They sit beside
their source as `*.test.ts` and `*.test.tsx`, and carry the `test` role a
file under `tests/` carries — a whole one is written without a question —
because the policy derives that role from the suites the gate declares
rather than from a second table naming the same files.

[quality-pipeline.md](quality-pipeline.md) explains which of the three
automated layers catches what. The short version: `git hooks install`
refuses a commit whose generated artifacts are behind their source and a
push whose branch fails the gate, the per-push CI workflow runs those same
commands and binds whether or not anyone armed the hooks, and the nightly
lane owns everything that needs a real native CLI.

Two conventions catch most first-time review comments:

- **Every function specifies input and output types**, and `Any`,
  `dict[str, Any]`, and `dict[str, object]` are not among them. Use a
  `TypedDict`, a Pydantic model, or `JsonValue`/`JsonObject` from `lup.types`
  for data whose schema lives elsewhere. `# type: ignore` is forbidden; the
  audited `# lup: ignore[rule-id]` escape hatch exists for genuine boundaries.
- **Errors are never silently swallowed.** No `except: pass`, no
  `contextlib.suppress`. Log with `logger.exception()`, handle it, or re-raise.

[rules.md](rules.md) indexes every executable rule with its matching shape and
the module that enforces it. A denial names its rule id, so you rarely need to
read it first.

### A break declares what to do about it

In a repository other projects build on — one declaring a `spread`, as lup
does — `dev check` also reads what the change took away. A module that moved
or a name that was renamed needs nothing written down: `dev migrate map`
derives the relocation from the two trees. A capability that is gone, or a
signature a caller can no longer satisfy, needs a sentence somebody wrote,
and the `declared migrations` row fails until one exists.

Declare it in the commit that makes the break, as one TOML file under the
library's `migrations/pending/` (`packages/lup/src/lup/migrations/pending/`
in lup's own checkout), named for the break:

```toml
subjects = ["Runtime.contained"]
reason = """
`contained` named the configuration home rather than a container, so the \
method takes the word for what it does"""

[[steps]]
instruction = "Call `Runtime.homed(request)` where you called `Runtime.contained(request)`."
command = ["uv", "run", "lup-devtools", "dev", "py", "text", "\\.contained\\("]
```

`subjects` names every capability the one decision took, spelled as the gate
spells them; `reason` is what the changelog carries; each step is a sentence
a caller acts on, with the `command` that does it where one does. Leave
`commit` out. A file per break is what lets parallel branches each declare
one without meeting in a merge.

`dev release` moves the pending files into `migrations/<version>/`, fills in
the commit each break landed in, and renders their prose into the section it
closes. A release candidate (`dev release --pre`) renders them and moves
nothing — they belong to the release — and promoting it moves only the files
its commit held, since a break declared after the candidate is not in it. The
files stay: `dev migrate pending <commit>` tells a project crossing several
releases what each one asks of it, and `dev migrate check --over
<base>..<head>` judges any range against every release's record as well as
the pending one.

### The `# lup: ignore` escape hatch

When `Any` or another anti-pattern is genuinely needed — an untyped library
boundary, MCP — an inline ignore requests user approval rather than silencing
the check on its own authority.

Prefer the typed, pyright-style `# lup: ignore[rule-id]`, comma-separating a
list (`# lup: ignore[dict-get, tuple-shape]`), so a site silences exactly the
rule it needs and still trips the others. The bare `# lup: ignore` stays
valid, but the auditor flags it as untyped to nudge migration. The marker sits
on the line that trips the rule, or stands alone directly above it — one
policy for every rule alike, and nowhere else reaches. Inline is the canonical
placement; the line above is where a reason too long for the column budget
goes, since a comment is the one thing the formatter cannot wrap.

A directive naming a rule that nothing it guards trips is refused rather than
approved — it silences nothing, so the approval would buy an exemption the
auditor already calls spurious. The refusal names the rule that does not fire,
and what the line trips instead where it trips something. Rules another
scanner owns are not judged this way: the edit gate carries the anti-pattern
table alone, and a verdict it cannot reach is not one it refuses over. Nor is
the bare form, which names no rule and so silences every rule there is — the
auditor still reports one that guards nothing, so a bare marker the gate
admits can still be a marker `dev check` refuses.

A spurious finding is the one kind with no decision in it, so
`dev check --antipatterns --fix` deletes those directives instead of listing
them, then sweeps again and reports what is left. The reason prose above a
standalone directive goes with it, being a sentence explaining a rule that
does not fire; prose written above *that* is not its reason and stays. The
other two kinds are untouched, because both are asking for a judgement:
"missing" is whether the rule is right or the line is, and "untyped" is a
reason nobody has written yet.

In a file's opening comment block the marker goes file-wide — a standalone
`# lup: ignore` disables anti-pattern checks for the whole file, and
`# lup: ignore[rule-id]` disables only that rule, the way `# pyright: ignore`
works for files.

### A customization marker reads two ways

`# lup: template: <decision>` is the one marker whose meaning depends on which
repository it sits in, and `[tool.lup] template` in `pyproject.toml` says
which. While that flag stands the repository is the scaffold itself and its
customization markers are inventory: `dev check` counts them and says no
more, because a permanent wall of text would sit in front of the notes
somebody is actually owed. `dev init` clears the flag in the same rewrite that
renames the package, and from then on every marker still standing lists in
`dev check` as a decision this domain has not made.

A marker asks a downstream repository's authors a question about their domain,
whose answer every user of that repository then shares; a question two
machines running one commit would answer differently is not one.

`uv run lup-devtools dev todos` walks them either way — an alias for
`dev comments --kind template` — and initialization goes through them one by
one. Answering one means writing this domain's own code where the scaffold's
example stood, which leaves no original ask for a `solved:` claim to be
checked against, so the marker is deleted rather than converted, exactly as
`ignore` is. Advisory in both readings: a domain that means to leave one
standing writes `# lup: defer:` and says why, which is the sentence it should
have to write rather than a red branch it learns to ignore.

## Tests

One standard decides whether a test earns its place: **would it catch a
realistic regression?** A test that asserts a fixture back at itself, or that
pins language behavior rather than library behavior, is deleted rather than
maintained.

Two lanes. `tests/unit/` is deterministic, runs on every push, and pins
adopter-visible behavior: security decisions, byte determinism, wire formats,
state persistence. `tests/integration/` carries the `integration` marker, is
deselected by default, and runs against real installed native CLIs and Docker
on the nightly lane — that is where anything requiring a live boundary
belongs, and nothing in the unit lane may infer it.

The strongest fixtures in the repository are the shared policy cases in
`test_semantic_policy.py`: every case runs against both the canonical policy
objects and the assembled hermetic runtime under `python3 -I -S`, so the
dependency-free generated kernel cannot drift from the library. When you touch
`lup.policy`, that suite is the one to run first.

Behavior that must not regress silently is pinned rather than described:
`test_harness_compilation.py` holds byte-deterministic regeneration and the
live tree drift-clean, `test_rule_reference.py` fails when
[rules.md](rules.md) goes stale, and `test_capability_matrix_docs.py` does the
same for the capability matrix.

## Reviewing a change

A change to canonical harness source arrives with its regenerated artifacts,
and both halves are reviewed together. What to look for:

- A prompt change should be understandable from its content module alone.
- A policy-data change should trace to the `HookSet` or a canonical rule
  object — and `hooks/runtime/kernel/` should be byte-identical to the
  canonical `lup.policy.kernel` package, with configuration confined to
  `policy_data.py`.
- Both native trees change when a portable declaration does; only the owning
  tree changes for an adapter-private renderer change.
- `.lup-ownership.json` is generated proof, not hand-authored metadata.
- A conflict is never resolved by deleting an unknown file. Classify its
  ownership or leave the conflict explicit.
- No credentials, plugin trust, installed cache contents, active sessions, or
  local profile configuration are ever committed.

Unresolved `# lup:` review notes stay visible in a full local `dev check`.
They are feedback to act on, not lint to clear: a note comes out when the code
or structure it points at has actually changed. Resolving one is a rewrite,
not a removal: fix what the note points at — or, for a question, answer it
definitively in code, docs, or a recorded user decision — then restate the
marker as `# lup: solved: <the note's original words>`, text unchanged, so
the claim sits beside what it claims to fix and can be checked against what
was asked. A `solved:` claim is retired only by the verify-solved review
pass, through `dev comments --retire`; the edit gate refuses a hand-deletion
or rewording for everyone, agent and human alike. `defer:` notes park work
at the site until deliberately resumed, and `ignore[<rule-id>]` hatches are
not feedback at all — they come out with the violation they cover. /lup:resolve runs that pass;
[resolver.md](resolver.md) describes what it does.

## Native evidence and the release gate

Deterministic fixtures run on every change. A scheduled workflow additionally
runs the full integration marker against installed Claude and Codex binaries,
covering the session-id, pager, dynamic-tool-schema, and blocked-edit
boundaries that only a real CLI can prove. `harness doctor` compares installed
versions against the typed evidence ledger; a newer component warns locally
and fails the nightly strict check, while the live job still runs so drift
cannot suppress the evidence needed to review it.

Beyond the ordinary pull-request checks, cutting a release requires two
consecutive scheduled nightly runs in which:

- the credentials-gated native job **completed successfully** — a skipped job
  is not a green run, and a completed failure stays release-blocking;
- the strict evidence job reported no drift between the installed native
  versions and [native-capabilities.md](native-capabilities.md).

Review the probe output together with the evidence ledger rather than updating
the ledger mechanically.
