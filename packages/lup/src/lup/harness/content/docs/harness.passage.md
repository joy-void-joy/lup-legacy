# The harness

The committed `.claude/`, `.codex/`, `.agents/`, and root `AGENTS.md` trees are
build products. Skills, agents, guidance, permission policy, and this
documentation are authored as typed Python; one command renders every native
tree deterministically; the source and generated diffs are reviewed together.

They are committed rather than built on demand so that a checkout is directly
launchable as a native plugin with no build step, and so generated output is
reviewed like any other change. Hook execution in particular must not depend on
this checkout or its virtual environment — a generated plugin carries its own
policy runtime.

## The one loop

```bash
uv run lup-devtools harness generate all   # render both trees from source
uv run lup-devtools harness check all      # read-only drift check; what CI runs
```

`harness claude` and `harness codex` regenerate the declared targets, check the host, settle the
base, and launch the selected runtime; each wait is named as it starts, so a long silence is a
stopped launch rather than a slow one. `--generate-only` stops before launching. `git hooks install` installs
the drift check as a git pre-commit hook, so omitted generated output is
refused before the commit exists rather than minutes later in CI.
[quality-pipeline.md](quality-pipeline.md) maps all three layers.

`--sandbox` chooses the wall a session opens behind: `outer`, the verified
container; `inner`, the runtime's own sandbox on the host; or `none`, the
semantic policy alone. A launch naming none opens under `outer`, and where no
Docker or Podman client is found it falls back to `inner` with one warning,
which names what to install for the container and says that `--sandbox inner`
chooses the host without the warning. Only the default falls back: an
explicit `--sandbox outer` with no client is refused.

## Startup checks and container images

Both launchers build a container image when no image matches the rendered
Dockerfile. They reuse a matching image. Package declarations feed that
Dockerfile, so changing the declared packages triggers a build on the next
launch. Building an image installs packages inside it, not on the host.
`uv run lup-devtools harness image` prints the Dockerfile; it does not build it.

Requirements declare where they are needed:

| Location | Checked where |
| --- | --- |
| `host` | On the machine running the launcher |
| `image` | Inside the session container |
| `both` | In both environments |
| `session` | Inside the container for a normal launch; on the host with `--sandbox inner` or `--sandbox none`, and for a launch naming no `--sandbox` on a host with no Docker or Podman client |

The allowed shell commands are a `session` requirement. Missing `tree` or
`yq` on the host does not warn during a container launch when the image
provides them. The container is checked after its image is built or reused.
If a command is missing there, check that the image's declared packages
actually provide it; repeating an unchanged build is not a general repair.

Run `uv run lup-devtools harness requirements` to check host dependencies,
including tools for sessions running on the host. Add `--inside` to check
the container, or `--inside --launch-only` to run just its startup checks.
Full container checks include a test model turn.

Host setup checks include `harness sandbox-check`: a disposable, network-disabled
Python sandbox evaluates `1 + 1` through the persistent REPL and is removed
afterward. This verifies container creation and code execution beyond the
daemon-info check. It installs no packages; unavailable images, denied container
creation, broken Python, or failed cleanup produce a failed requirement with
the original diagnostic. Use `harness sandbox-check --image <image>` to exercise
a project's alternate sandbox image. Init and install run these declared
checks and report repairs that require host administration or a fresh session.

A host device — a GPU — is never in the manifest, because a manifest is
committed and which GPU a machine holds is that machine's fact. `sync grant
<name>` records it in the machine's `sync.json.local`, and the host checks
build one requirement per grant they find there: a throwaway container
started with the device, at setup rather than every launch since it costs a
container start. A launch reads the host's CDI registry itself and withholds,
with one line, any grant no spec there names.
[contributing.md](contributing.md) carries how a device is granted.

Every session container drops every capability and refuses new privileges
(`--cap-drop ALL`, `no-new-privileges`), and its entrypoint starts the agent
through `setpriv` with empty inheritable and ambient sets, so the agent holds
no capability and no setuid binary in the image can hand it one.
`OuterContainer(sudo=True)` is the one widening: the image gains passwordless
sudo, the container's root gets back what administering its own files takes
(the `Capability` members, nothing reaching past the container), and new
privileges are allowed. It is granted only on a rootless engine, where that
root is an unprivileged user on the host; a rootful engine refuses the launch.
What sudo installs vanishes with the container, so a package the session keeps
needing belongs in the image's `tooling`.

The target selector also chooses its login layout and configuration home:
`claude` honors `CLAUDE_CONFIG_DIR`, falling back to the personal `.claude`
directory; `codex` honors `CODEX_HOME`, falling back to the launcher's worktree
home. `all` checks each with its own selection. These checks use existing
configuration; they do not install plugins or perform an interactive login.

Reports name the environment and show the failed operation, its impact and
the next step separately. A check that could not run reports an unknown
result. A missing command can produce misleading shell results: exit code
127 takes the fallback in `command || fallback`, just like other failures.

## Generated output is never hand-edited

This is the rule the whole design rests on, and it has one reason: Lup cannot
safely infer an arbitrary Python source change from rendered Markdown, TOML,
JSON, or shell. So a native artifact edit is not imported and not overwritten
— it is **preserved and reported as a conflict**.

Every generated artifact whose format allows a comment opens with a banner
naming its canonical source and the command that regenerates it. The banner
text comes from one parameterized helper in
`packages/lup/src/lup/harness/generation.py`, so its wording and placement
cannot drift between artifact families. Two families cannot carry one, and this
section is their provenance record instead:

- **JSON artifacts** — manifests, hooks, settings, evidence — have no comment
  syntax.
- **Skill, command, and agent Markdown** is verbatim model-facing prompt text
  after its frontmatter; a banner would be injected into every prompt.

### Ownership manifests

`.claude/.lup-ownership.json` and `.codex/.lup-ownership.json` are the
generator's proof of what it owns, written by
`packages/lup/src/lup/harness/ownership.py` after every successful generation.
Each records the generator version, a digest of the canonical declarations,
and — per generated file — its path, sha256, semantic id, and executable bit.

Reconciliation (`packages/lup/src/lup/harness/reconciliation.py`) compares
current bytes against those digests to classify every managed path. Files that
still match may be replaced or deleted by regeneration. Hand-edited generated
files are preserved as backpropagation candidates. Local files the generator
never wrote — including sensitive ones like `.claude/settings.local.json` and
`.codex/config.local.toml` — are never touched.

The manifests are committed because a fresh clone and CI need the recorded
digests: without them the drift check cannot run and the generator cannot
prove which bytes it owns, so it would refuse to replace anything.

A merge is where a manifest goes stale on its own. The generated trees merge
under the `lup-ownership` driver, which keeps one side; git takes whichever
side changed an artifact only one branch regenerated, so the artifacts come
out right, but both branches rewrote the manifest, and the side kept lists
its own digests for every file the other side changed. The settle guards,
armed with the others by `uv run lup-devtools git hooks install`, answer it
where the merge commit is made — `post-merge` for a merge git completes,
`post-commit` for one concluded by hand — by running `uv run lup-devtools git
settle`, which regenerates and replaces the merge commit with one carrying
what that wrote: same parents, message and author, nothing else staged. It
leaves alone any commit with one parent, a merge another branch already
holds, and a rebase in flight.

### Every generated path and its source

[generated-paths.md](generated-paths.md) is that map, one row per artifact,
walked from the trees the recipes compile. A table here drifts one way only:
an artifact added to a recipe stays invisible until somebody remembers this
page, so the rows that go missing are always the
newest — and a map with a family missing reads exactly like a complete one.
Each row is the artifact's own attribution, the same one its banner prints
for a reader who opens the file, so nothing there can name a source the
artifact does not.

Canonical sources live in `lup.harness.content`
(the declarations lup ships), {{ harness_content_directory }}
(the ones only this repository has), {{ harness_catalog_py }}
(plugin, hook, and resolver composition), and the `lup` package itself
(adapter renderers and the policy bundle).

Three things that map states and the reason for each. The
{{ kernel_module_count }} modules under `hooks/runtime/kernel/` are a verbatim
copy of `lup/policy/kernel/`, kept byte-identical so it can be diffed against
the canonical package. The ownership manifests are written by
`lup.harness.ownership` from the generation result rather than compiled from a
declaration. And `docs/rules.md`, `docs/commands.md`, `generated-paths.md`
itself, and the CI workflow belong to no runtime tree at all: each is written
by a `RepositoryWriter` the project declares beside its targets, reconciled
against nothing but the declaration that renders it, and so absent from a map
walked out of the recipes.

`.claude/CLAUDE.md` and root `AGENTS.md` are the *same* document rendered
twice, because each runtime reads guidance from its own location. The
redundancy is deliberate; edit `content/guidance.py` and regenerate rather
than patching either copy. `docs/` is rendered once, by the Claude recipe,
because these pages are repository documentation at a neutral location rather
than anything either runtime reads from its own tree.

## The pipeline

`lup-devtools harness generate|check|claude|codex` walks one path from typed
Python to a launched native plugin.

1. **Typed declarations** — `harness/content/` holds the skill, agent,
   guidance, pattern, template, and documentation declarations, above the
   tooling layer because they are what a harness is made of rather than
   anything the CLI adds; `harness/catalog.py` composes them with the
   application-owned `HookSet` into one canonical `lup.harness.models.Harness`.
   Prompt prose is stored as ordered typed parts, never as a native string.
2. **Renderers** — `lup.providers.claude.harness` and
   `lup.providers.codex.harness` implement the `ArtifactRenderer` seams from
   `lup.harness.contracts`; the compilation roots in `lup.providers.harness`
   compose them into a complete `ArtifactTree`. A `SkillInvocationRenderer`
   owns the entire native invocation spelling; shared code never rewrites one
   prefix into another.
3. **Validation** — `lup.harness.validation` checks the whole rendered tree
   (path uniqueness, ordering, identifiers, normalized text) and generation
   refuses to continue on any issue.
4. **Reconciliation** — `lup.harness.ownership` records what the generator
   owns; `lup.harness.reconciliation` classifies the current tree under that
   proof and proposes writes, proven deletions, and explicit conflicts. Local
   edits worth carrying back to canonical source are persisted as reviewable
   patches by `lup.harness.proposals`, never applied.
5. **Materialization** — `lup.harness.materialization` re-verifies every
   preimage and applies a conflict-free proposal atomically, then saves the
   manifest. Stale proposals are rejected.
6. **Launch** — `lup.providers.*.harness_runtime` probes native CLI
   capabilities, `lup.launch.session` composes the session a launch opens,
   and `lup.devtools.harness.launch` runs the native CLI in the
   foreground of the launching terminal, over the non-interactive defaults
   from `lup.harness.environment`. `lup.harness.process` is the
   captured-output launcher seam the resolver and the base-freshness probe run
   `git` and verification commands through.

Generation orchestration takes a frozen `GenerationRecipe` holding the desired
tree, current-tree reader, ownership location, and target requirements. Only
the CLI composition root maps a user-facing target name to a concrete recipe:
adding a third target supplies another recipe rather than a branch in
reconciliation or materialization.

Each harness module owns one concern. The declarations lup ships live in
`packages/lup/src/lup/harness/content/`, and the root of the declaration
graph is {{ harness_catalog_py }}, assembling them and
{{ harness_content_directory }} into a `Harness` — this repository's rather
than lup's, because its whole job is to be this project's own harness. The CLI
half lives in `packages/lup/src/lup/devtools/harness/`:

- `app.py` — Typer wiring only; every command body lives elsewhere
- `composition.py` — the target roster a CLI selector names, and the
  accounts each runtime keeps
- `drift.py` — console drift reporting for `generate` and `check`
- `reconcile.py` — drift classification and the source-patch flow
- `doctor.py` — runtime evidence against the `lup.harness.evidence` ledger
- `resolve.py` — persisted-resolver glue: broker, snapshots, factories
- `launch.py` — this repository's gates before a session (generation, base
  freshness, worktree pointers), its registrations handed to the library as
  standing grants, and the native launchers mapping flags onto the session
- `settings.py` — rendering a runtime's project settings from what the
  harness declares
- `accretion.py` — what the boundary has been widened for, and which of it
  nobody uses
- `policy_refresh.py` — accepting changed destination policy bytes for a
  live, already granted launch
- `profile_app.py` — the command tree over the local and global registries
  holding the runtime accounts, and the optional move from local to global
- `sandbox.py` — exercising the Python sandbox through its container and
  persistent REPL
- `generated_paths.py` — which file each typed declaration compiles to

What generation and a launch do for any project declaring an agent is the
library's rather than the CLI's. `lup.harness.generate` holds the recipes,
drift inspection, and atomic materialization, with the `NativeComposer` seam
each runtime compiles a project's content through in its own adapter
(`lup.providers.<runtime>.composition`), and
`packages/lup/src/lup/launch/` the launch:

- `session.py` — composing a session: the runtime and host rosters, the
  boundary compiled, measured and refused where it fell short, the argv that
  opens the session inside the container or on the host, and its transcript;
  what each runtime adds around one (its login, its plugin, its home carried
  back) is `lup.providers.<runtime>.session`
- `preflight.py` — minting a launch's boundary, measuring it, and writing it
  down for the session
- `container.py` — opening a native session inside the container the project
  declares
- `config_volume.py`, `environments.py`, `superseded.py` — what a contained
  session keeps between launches: its configuration volume, its project
  environments, and what a split volume left to sweep
- `pointer_trust.py` — which roots host git may enter, and which
  repositories lup comes to trust
- `refusal.py` — `LaunchRefused`, the refusal a launch that cannot open
  raises, which a command line turns into its own usage error

## What the plugin ships

Both rosters are rendered from the typed declarations, and neither is a list.
Every skill and agent belongs to a **module** — one subject as one value,
carrying its content, its page under `docs/`, its paragraph in the
always-loaded document, its command tree and its tool group — declared under
`lup.harness.content.modules` for the subjects lup ships and under
{{ harness_content_modules }} for the ones only this
repository has. {{ harness_content_catalog_py }} composes
both and states which modules this project takes; everything below is derived
from that rather than declared beside it, so declining a subject removes all
five surfaces at once. `dev modules` prints the roster. Change the module that
owns the subject, then regenerate.

**Skills:**


<!-- passage: authoring -->

## Authoring

### Add a skill

Create two files beneath `content/skills/` — the library's half when the
skill automates work inside a project, this repository's when its subject is
standing one up. The declaration is typed Python; the prose is Markdown
beside it, named for the module and read as its passage:

```python
"""The project-triage skill."""

from lup.harness.models import Argument, ArgumentsRef, Passage, PromptDocument, Skill

SKILL = Skill(
    id="skill.triage",
    name="triage",
    description="Classify one reported problem and identify the next investigation",
    arguments=[
        Argument(
            name="report",
            description="Problem report or error text to classify",
            required=True,
        )
    ],
    prompt=PromptDocument(
        parts=[Passage(module=__name__, values={"report": ArgumentsRef()})]
    ),
)
```

Beside it, `triage.passage.md`:

```markdown
Read the report, inspect the relevant boundary, and return the
most likely failure class with one concrete next check.

Report:
{{ '{{' }} report }}
```

A passage names values and nothing else. A statement tag ({{ statement_tag }})
or a comment tag ({{ comment_tag }}) is refused where the file is read, so
prose varying by more than a value is two passages, or a declaration in
Python that says which one is read. Every value is a part — an
`ArgumentsRef`, a `SkillInvocation`, a path through `code()` or `plain()` —
so it enters escaped, and a description carrying a backtick cannot close the
span it lands in. A name the values never carried fails generation rather
than rendering as a blank.

Import it into the module whose subject it serves, under `content/modules/`,
and name it in that module's `ContentRoster`. That is the whole registration:
a skill reaches a project because its module does, and there is no second
roster to add it to as well. Explicit imports make a misspelled or missing
module a type-checking error; there is no dynamic registry and no barrel file.
A declaration file no module claims fails `dev check`'s module-coverage sweep
rather than shipping unnoticed — and where no existing module is about the
skill's subject, that is a new module rather than a stretched one.

Then run the authoring loop:

```bash
uv run lup-devtools harness generate all
uv run lup-devtools harness check all
uv run ruff check packages/lup/src/lup {{ project_directory }}
uv run pyright
uv run pytest tests/unit/test_harness_compilation.py -q
```

A declaration must not branch on a provider name. Argument declarations and
`ArgumentsRef` must occur together — model validation rejects either alone.
Use semantic prompt parts such as `ArgumentsRef` or `SkillInvocation` and let
each renderer choose its spelling;
[platform-differentiation.md](platform-differentiation.md) records what prose
may and may not name.

### Add a document

Documentation is generated the same way. Add a module under `content/docs/`
in the half whose subject it is, list it in that half's
`content/docs/catalog.py`, and regenerate. The banner is applied from the
roster, so a document module holds prose only. The index builds its rows from
the documents both halves declare, so a page appears there by being declared
— and a page it lists that stops being published fails generation rather than
leaving a link that resolves to nothing.

### Change the fetch allowlist

The application-owned `HookSet` is constructed by `portable_harness()` in
`{{ harness_catalog_py }}`. Add the narrowest origin and
path prefix that supports the workflow:

```python
allowed_fetch=[
    HookUrlScope.model_validate(
        {
            "origin": "https://docs.example.com",
            "path_prefix": "/agent-api/",
        }
    ),
]
```

Origins normalize into scheme, host, port, and path-prefix rows. Put an
explicit exclusion in `denied_fetch` when a permitted host has a sensitive
subtree; deny rows win over allow rows. Never add provider-specific fetch
logic to the kernel or a generated dispatcher.

Regenerate and run the policy fixtures:

```bash
uv run lup-devtools harness generate all
uv run pytest tests/unit/test_semantic_policy.py -q
uv run lup-devtools harness check all
```

Inspect `hooks/runtime/policy_data.py` in both generated trees. The rows should
change while `hooks/runtime/kernel/` stays identical: configuration is
generated data, policy control flow is one copied package.

### Change the shell classification

The shell auto-allow vocabulary is data too. The baseline lives in
`lup.policy.vocabulary` (`default_vocabulary()`) as a readable table, and every
rule in it states what the command *does* rather than what that earns: a
subcommand command falls off its own enumeration into
`unclassified_operation`, and each verb it lists declares its own effects. To
teach the fleet a downstream toolchain, append rules through the `HookSet` in
`catalog.py` — never edit the kernel:

```python
shell_rules=[
    ShellCommandRule(
        name="cargo",
        effects=[declare("unclassified_operation", scope="cargo")],
        subcommands=[
            ShellSubcommandRule(
                name="check", effects=[declare("reads_path", scope="project")]
            ),
            ShellSubcommandRule(
                name="build",
                effects=[declare("writes_path", scope="scratch", write="create")],
            ),
            ShellSubcommandRule(
                name="test", effects=[declare("runs_declared_target")]
            ),
        ],
    ),
]
```

The extension is concatenated onto the baseline and erased into the same
`SHELL_RULES` rows the kernel interprets. A universal command every repository
should trust belongs in a `lup.policy.vocabulary` group instead. Regenerate and run the
policy fixtures exactly as above.

`effects` is required, so a rule that forgot to say is a type error rather
than a grant nobody wrote down, and a verb that genuinely does nothing this
table guards says `changes_nothing`. Nothing here states a verdict: the two
ways to reach one are declaring an effect that asks —
`installs_dependency`, `external_mutation`, `mutates_environment` — or, where
the objection is to the *spelling* rather than to the operation, setting
`refuses` with the route to take instead. A destructive form under an
allowing verb is guarded by naming the flag in `ask_flags` and what it adds
in `flag_effects`, so the question names the operation somebody is actually
being asked about.

Every axis cascades, so each subcommand above states its own `effects` rather
than leaving them out — omitting a field means "inherit from the level above",
never "allow". The same cascade is what lets `sandbox="outside"` be declared
once on a command and reach every verb beneath it. Run
`uv run lup-devtools dev vocabulary --provenance` to see which level supplied
each half of every rule, and `dev vocabulary --json --output <path>` before and
after a reshaping to confirm no verdict moved that you did not move.

Then sweep what an ordinary session runs. `uv run lup-devtools dev hooks sweep`
classifies the everyday corpus this project declared in
`HookSet.everyday_commands` and exits non-zero on anything that is not a plain
allow — the one measurement that reads the direction a *tightening* shows up
in, since a provenance diff and a verdict census both go on agreeing when a
de-escalation quietly stops firing. Each command is swept once per posture a
session runs in, so a rule that only asks where nobody can answer is caught
too. `dev check` runs the same sweep, so a rule that stopped `git status`
fails there rather than in somebody's session.

### Put a program in the image

A package reaches the container through one of three doors, and picking the
wrong one is how a tool ends up absent with nothing having said so.

`Image.baseline` is the library's answer to what any shell session needs to be
usable at all — `git`, `curl`, `jq`, the registry managers. It is not a place
for a project's own tools, and overriding it means restating every name in it
to add one.

A `Requirement` in the manifest is what a declared *capability* asked for. It
takes a purpose, an exercise that proves a machine has the thing, and a policy
for going without — so it is the right door exactly when the absence deserves
a diagnostic. It is the wrong one otherwise: a manifest that invents
prerequisites refuses machines that were fine, which is why ripgrep was
declared here once and taken back out.

`Image.tooling` is the third, and the one for a program this project's work
simply needs present. Declared where the image is composed:

```python
return Image(
    egress=SessionEgress(mode="host"),
    tooling=[
        Package(name="poppler"),
        Package(name="prettier", manager="bun", version="3.4.2"),
    ],
)
```

Whole packages rather than bare names, so a registry package is pinned and
reached through the manager that obtains it. Nothing exercises these: a name
the manager cannot resolve fails the build and names itself, which beats a
probe. `dev seams` reports what this project declared and where.

## Resolving a conflict

`harness reconcile` compares the current files, the desired render, and the
ownership manifest. It mutates nothing. A conflict means one of these:

| Category | Meaning | Action |
|---|---|---|
| `backpropagation_candidate` | A previously generated file differs from its owned digest. | Reproduce the intended change in the typed content or policy source, then regenerate. |
| `unknown_conflict` | Lup has no ownership proof for the existing bytes. | Decide whether the file belongs in typed generation or should stay local-only. |
| `local_only` | The recipe deliberately leaves the path to the user. | Keep it outside generation. |
| `sensitive_local_only` | The path may hold credentials or trust state. | Never import or commit it through the harness. |

The ordinary path is short:

```bash
uv run lup-devtools harness reconcile all
# edit the corresponding module under harness/content/ or harness/catalog.py
uv run lup-devtools harness generate all
uv run lup-devtools harness check all
```

Do not resolve a conflict by deleting an unknown file. Classify its ownership,
or leave the conflict explicit.

### Applying a source-patch proposal

A source-aware tool may produce a Git-format patch against canonical Python
without applying it. Keep the source tree at the patch's preimage, then persist
the proposal:

```bash
uv run lup-devtools harness propose-reconciliation tmp/source.patch
```

The command prints a proposal id and writes immutable `source.patch` and
`metadata.json` under `.lup/reconcile/<id>/`. Review both files and the named
preimages, then apply only the reviewed proposal:

```bash
uv run lup-devtools harness apply-reconciliation <proposal-id>
```

Apply verifies the patch digest, proposal identity, and current preimage digest
before showing the patch and asking for confirmation. It then runs
`git apply --check`, applies the canonical-source patch, regenerates both
targets, and removes the consumed proposal. A changed preimage, malformed
path, digest mismatch, or non-applying patch stops before any mutation.

This is a patch transport, not a native-body importer. A rendered artifact is
never parsed heuristically back into Python.

## Launch and trust

Generated plugins carry their own policy runtime and dispatcher; hook execution
imports neither `lup-devtools`, this checkout, nor its virtual environment.
Both decoders convert native tool payloads into the same semantic
edit/shell/fetch/search vocabulary, shared policy evaluates it, and each
adapter renders the decision back — Codex `ask` being a documented fail-closed
exit-code-2 approximation.

Codex packages install through the native plugin CLI only when a separately
installed, content-addressed cache revision is absent. Installed revisions are
immutable and retained so concurrent sessions keep valid hook paths; the source
plugin is never mistaken for the cache. Personal trust state, credentials,
active run state, and cache contents are never generated or committed. Review
hook trust with the native hooks surface after generation.

A contained Codex session's home is a volume it writes, so the revision its
hooks run from is held for it: the preparation inside the container reports
the revision it installed (`lup-codex-plugin --report`), the launch writes that
revision again from the plugin source on the host, under
`~/.cache/lup/codex-revisions/`, refusing a name that does not match the
source's content, and mounts it read-only over the plugin's whole cache in the
home, after the volume it nests in. The session runs the hooks it was
launched with and sees no other revision; the rest of the home stays
writable.

A Claude Code session loads its plugin from the checkout's generated tree,
which a contained session can write, so it could rewrite the hooks judging it.
`OuterContainer(hold_generated=True)` — `--hold-generated` on the command
line, or `hold_generated = true` in a mode, a person's `[container]` or the
project's declared container — holds what the runtime runs from read-only:
Claude Code's plugin directory whole with its marketplace, the project
settings and `.claude/CLAUDE.md`; Codex's plugin source with its rules, the
generated agents, `.codex/config.toml`, the marketplace and `AGENTS.md`. Only
this checkout's, so a sibling worktree stays the session's to regenerate. The
cost is that regenerating this checkout's trees is the host's work: `harness
generate all` in such a session refuses before writing anything, "these trees
are read-only in this session; run `uv run lup-devtools harness generate all`
on the host", `dev check`'s drift line says the same, and so is any git command
rewriting them here — a merge, a switch or a reset that touches them. Off by
default, since a project whose sessions regenerate their own checkout would
lose that.

`lup-devtools harness claude|codex` launches a declaration. Each flag is a
field of the `Claude(...)` or `Codex(...)` it builds from this repository's
composition — the generated plugin and every plugin the checkout keeps beside
it, the harness's policy, requirements, image and wake socket, the tool servers
every session carries — and `launch()` does what a program's launch does:
readies the home, checks the host, measures the boundary, compiles the argv
and runs the CLI in the foreground. What stays with the command is this
repository's workflow around a session, handed to that launch as its
`steps=` — the checkpoint outermost, then the worktree pointers verified,
the base brought level, and every tree regenerated — and the mapping from
its flags and launch modes to the declaration. `command()` on the same
declaration prints the process the command runs; `docs/library.md`
describes the declaration.

| Flag | Field |
|---|---|
| `--model`, `--effort` | `model=` (a name the catalog does not list goes through as `CustomModel`), `effort=` |
| `--profile` (both), `--codex-home` | `profile=`, and `home=` — the profile's resolved home on Claude, the named home on Codex |
| `--sandbox outer\|inner\|none` | `OuterContainer(image=..., mounts=..., devices=...)`, `InnerSandbox(escapable=True)` on Claude and `InnerSandbox()` on Codex, `NoSandbox()`; unnamed, outer where Docker or Podman answers and inner with a warning where neither does |
| `--mount`, `--mount-ro`, `sync.json.local` | `Mount(path, writable=...)` on the sandbox, the command line's first |
| `--device`, `sync.json.local` grants | `devices=` on `OuterContainer`; said and not granted on the host |
| `--sudo`/`--no-sudo`, `--network`, `--memory`, `--hold-generated`/`--release-generated` | `sudo=`, `network=`, `memory=MemoryLimit(...)`, `hold_generated=` on `OuterContainer`; said and not granted on the host |
| `--continue` / `--resume` / `--session ID` | `resume=Latest()` / `Pick()` / `Reopen(session=...)` |
| `--max-recursive-agent` | `max_recursive_agent=`, a mode's default where it names none |
| `--transcribe-session`, a mode's record | `record=Recording(transcript=..., root=..., mode=..., ledger=...)` |
| `--generate-only` | every tree regenerated, then `prepare()` — the home a host session opens against |
| `--force-install` (Codex) | `prepare(force=True)`, and `launch(force=True)` |
| `--ignore-antipatterns` | the plugin compiled with every rule retired |
| a launch mode | its targets compile the plugin; its model, words, record root and allowance are fields; what it opens around the run is a host companion |
| passthrough words | `launch(*words)`, after everything the declaration compiles to |

### Opening a session the anti-pattern gate leaves alone

`--ignore-antipatterns`, on both launchers, for the sessions where the rules
are not the point: exploring, spiking, or working over code these conventions
were never written for.

It reaches the gate rather than the command line, which is the only thing that
would make it work. The anti-pattern table is projected into each plugin's
hermetic edit policy at generation time, and a launch regenerates every tree
before it opens — so the flag compiles the tree the session actually runs against.
What it sets is `RuleSelection` with every id retired, spelled as the ids
rather than as a flag meaning "all of them", because the selection is
subtractive and a rule added later should be one the selection has visibly not
answered for.

Three things it deliberately does not do, each announced at launch because each
bites later and none announces itself:

- **The sweep does not follow.** `dev check --antipatterns` reads the
  repository's own declaration, so a session that edited freely under the flag
  will fail it. That is the point rather than an oversight: a transient switch
  must not quietly become the repository's answer.
- **The committed tree is rewritten.** Regenerate before committing, or the
  commit carries a plugin nothing declares.
- **It is not how a project drops the rules.** `dev seams --retire-all` is,
  because that writes the decision where a review sees it and `--keep` takes it
  back.

### Reopening a session, and why a launcher owns it

Both launchers reopen an earlier session, from one declaration and in each
runtime's own words:

| request | flag | Claude | Codex |
|---|---|---|---|
| the most recent session here | `--continue` / `-c` | `--continue` | `resume --last` |
| choose from a picker | `--resume` | `--resume` | `resume` |
| one session by id | `--session <id>` | `--resume <id>` | `resume <id>` |

The shapes are genuinely different rather than differently named — a
subcommand has to lead the argument vector where a flag does not — which is
why `Resumption` carries the request and each adapter's function carries the
words. Naming two at once is refused rather than ranked, before anything is
generated.

This exists for more than convenience. The policy a session enforces is
compiled into the plugin tree its runtime loads **at startup**, so widening
that policy takes effect only in a new process. Without reopening, the price
of every widening is the conversation that established what it was for — which
is what pushes an agent toward a per-call escape that helps once and
evaporates. With it the durable path is also the cheap one:

1. The agent proposes the declaration edit. The policy source is a protected
   path, so the edit surfaces as an approval with the diff in it.
2. Approve it — what is approved is the rule, not one command.
3. `harness generate all`, or just relaunch — the same thing: a launcher
   regenerates every declared tree on the way in, not only the one it opens.
4. `harness claude --continue` / `harness codex --continue`. The reopened
   session is already running against the tree the approval produced.

### Your lup config, and where a profile comes from

What belongs to the person rather than the project lives once per person, at
`$XDG_CONFIG_HOME/lup` (`~/.config/lup` where that is unset), so a checkout
opened for the first time starts signed in, in their theme, on their defaults:

```toml
# ~/.config/lup/config.toml — every key optional, each defaulting to lup's own
profile = "work"            # the account a launch runs as when none is named
                            # and the checkout's .active selects none
effort = "high"             # unset: xhigh, or the model's highest rung below it
tier = "strongest"          # the model when nothing names one; "inherit" leaves
                            # it to the runtime
editor = "vim"              # both runtimes' prompt: Claude Code's editorMode,
                            # Codex's tui.vim_mode_default; unset, the account's

[theme]                     # each runtime's own name; unset, the account's own
claude = "light"            # named, it wins over the account's
codex = "dracula"

[claude.settings]           # Claude Code settings, as written, over the account's
verbose = true

[codex.settings.tui]        # Codex configuration, leaf by leaf, over the account's
animations = false

[cleanup]
superseded_volumes_after_days = 14   # an old config volume's history, kept this long

[container]                 # what every contained session is granted: over the
network = "bridge"          # project's, under a mode and the command line
memory = "75%"              # an amount such as "12GiB", or a share of the engine's
sudo = true                 # rootless engines only
```

A value is chosen the same way everywhere: lup's default, then this file, then
what the project declares (a launch mode's model, an agent declaration's own
fields), then what the invocation names (`--model`, `--effort`, `--profile`,
`profile=`) — each overruling the one before. A named effort the model lacks is
refused; the file's effort, like lup's, is where the default starts before
stepping down to a rung the model takes. A file that does not parse refuses the
launch, naming itself.

The container is the one place the order turns: what a machine can grant is
its person's to say, so `[container]` overrules the project's declared
container (`DevtoolsDeclarations.launch_container`, over the harness's image
and the folders and devices `sync.json.local` registers), and a mode's
`container=` and the command line's `--sudo`, `--network`, `--memory`,
`--mount` and `--device` overrule it in turn. Each is an `OuterContainer`
stating only what it sets, laid one over the next by `OuterContainer.over`: a
setting stated higher wins even said as its default, and the folders and
devices every layer names are all granted. A person's config names no image
and no nested repositories, which are one repository's own.

The theme is the one exception, because it is the account's rather than the
launch's: a session's `/theme` is kept. A launch writes a theme into the
account only where this file names one, which wins, or where neither this file
nor the account names any, where it fills in lup's colorblind palette — Claude
Code's `dark-daltonized` and its Codex port `claude-daltonized`. It never
passes a theme as a launch override, which would outrank the session's own
choice. A Claude session on the host runs in the account's own home, so what
it chooses lands there.

Anywhere else — a Codex session's worktree home, and every contained session's
config volume — a launch seeds the home with the account's settings, this
file winning, and carries back what the session changed of the person's when
it closes, measured against the seed so a change the account took meanwhile
is not undone. A volume's seed is merged three ways against what the last
launch seeded, so a second launch leaves a setting alone that a session still
running there changed — it returns when that session closes — and where the
person's settings changed the same one, theirs win and the launch names it.
Only preferences come back: the theme and the editor mode to
this file, a key its `[claude.settings]` or `[codex.settings]` table holds
back into that table, and any other display or input preference to the
account's own settings (Claude Code's `settings.json`, its configuration
document for the few preferences only that holds, and `keybindings.json`;
Codex's `config.toml`, with a theme's file). What runs a program or widens
what may run — hooks, permissions, `env`, helpers, MCP servers, plugins,
sandbox — never leaves the home it was written in, and a `/model` or effort
picked in a session stays that session's. Every Claude Code settings key
carries that decision (`lup.providers.claude.preferences`, typed against the
keys `dev settings` reads out of the installed CLI, and `dev check` refuses a
key none decides); Codex ships no schema to read, so a Codex key nobody
listed stays where it was written. The launch says what it returned and what
it kept.

A contained session's config home is a volume per repository and runtime,
`lup-claude-<repo>` and `lup-codex-<repo>`: every worktree of one repository
shares its login, trust and transcripts, and neither runtime reads the
other's. The first launch that finds the older shared `lup-cfg-<repo>` splits
it by what each runtime declares it keeps — an entry neither declares goes to
both, said aloud — and copies Codex's per-settings-digest volumes into its
own; a volume an open session still holds postpones the split. The old
volumes, and the per-worktree `lup-cfg-<worktree>` ones that came before,
are kept rather than removed: each is recorded as superseded in
`$XDG_STATE_HOME/lup` (`~/.local/state/lup`), the launch says where its
history went and the day it goes, and any launch or `harness clean`
removes it once `[cleanup] superseded_volumes_after_days` (14 unless this
file says otherwise) have passed — never while a container holds it.
`harness clean` lists each with its size and that date, and
`harness clean --yes` removes them sooner.

What contained sessions leave on the machine is swept as it goes: a launch
that builds an image removes the ones no checkout points at, every launch
removes project environments whose worktree is gone and other projects'
stopped egress proxies, and removing a worktree removes its environment.
`harness clean` lists all of it with sizes and what points at each, and
`harness clean --yes` removes what nothing does.

A profile names one account and the configuration home it runs under. Each
profile is a directory, with each runtime's home in the subdirectory that
runtime's login names (`claude-config/` for Claude Code, `codex-home/` for
Codex), so one name is one account on both runtimes. Two places keep them:

| Where | Reach | Selected by |
| --- | --- | --- |
| `.lup/profiles/<name>/` in the checkout | local: this checkout only | `.lup/profiles/.active` |
| `profiles/<name>/` beside `config.toml` | global: every checkout | `config.toml`'s `profile` |

A name resolves through the local profiles first, then the global ones, so a
checkout's own profile of a name wins inside it. Naming none takes the
checkout's `.active` where it has one, else `config.toml`'s `profile`, each
resolved the same way; naming none with neither recorded leaves whatever home
the surrounding environment already selected, so a session launched from
inside another stays on the account it was started under. A project may still
supply an origin of its own through the harness, resolver and setup trees;
naming none takes these.

`harness profile` and `setup profile` curate them, acting on the checkout's
own profiles unless `--global` names the shared ones, as `git config` does:

| Command | Without `--global` | With `--global` |
| --- | --- | --- |
| `add NAME` | starts `.lup/profiles/NAME/` | starts `profiles/NAME/` beside `config.toml` |
| `use NAME` | writes `.lup/profiles/.active`; may name a global profile | writes `config.toml`'s `profile`; only a global profile |
| `remove NAME` | names the local directory to remove | names the global directory to remove |

`list` shows both, each marked `local` or `global`, with `*` on the profile a
launch naming none opens, and says where a global profile is shadowed by a
local one of the same name. The first profile added where nothing is selected
yet becomes the selection, in the place it was added; one added beside a
selection leaves it standing.

{{ machine_profile }}
`harness claude --profile` and `harness codex --profile` select one for a
single launch, and `profile=NAME` on a `Claude` or `Codex` declaration opens
every session as it, all resolved local first. A declaration naming no profile stays on its process's account
rather than taking the recorded one, for the reason above. A name nothing
answers to is refused with the roster that would have answered, at the
launcher, the command tree and the declaration alike, and `usage claude` and
`usage codex` resolve `--profile` the same way a launch does. A Codex launch
derives its worktree home from the selected account by the same order.

A profile's home is derived from its name, so `add --config-dir` pointing
elsewhere is refused, and `remove` says to remove the directory rather than
forgetting it: the directory is the profile and it holds the login. Where that
profile is the selected one, it names what records the selection as well — the
`.active` file or the `profile` line — since a selection left naming a removed
profile refuses every launch that names none. To point one at a home that
already exists, symlink that subdirectory at it.

`harness profile migrate` is optional. It moves a checkout's local profiles to
global, for accounts that should be shared by every checkout, and the accounts
of the old personal registry at `~/.lup/profiles.json`, which nothing reads any
more: each profile and each home the registry made, a runtime's home at a
time; a home registered somewhere of the person's own is linked rather than
moved; a name already present keeps what it holds and the source is left and
reported; the old selection is carried where `config.toml` records none.

No profile may name Claude Code's default home, `~/.claude`, however it is
spelled or reached — a symlinked directory profile included. A profile exports
its home as `CLAUDE_CONFIG_DIR`, and with `~/.claude` named there Claude Code
reads `~/.claude/.claude.json` rather than the `~/.claude.json` a plain
`claude` reads (see below), so every session would open without the account's
theme, trust records and projects. `add` refuses one before writing it; `use`,
a launch and a resolver run refuse one already registered, naming it, and
`remove` still forgets it. Leave the profile unset to use the default account.
Codex has no such trap: its `CODEX_HOME` set to `~/.codex` is the same home, and
the same state, as leaving it unset.

### Workspace trust, and the profile it is recorded against

Claude Code keeps workspace trust in its user-level configuration document,
and offers nowhere else to put it — so an untrusted workspace is not a
project-level fact a repository can declare for itself. An untrusted one does
not fail: the session drops every `permissions.allow` entry
`.claude/settings.json` declares, warns into its own stderr, and runs on under
a permission posture the repository never declared.

A headless run cannot accept a dialog, so it establishes trust itself. Each
workspace's sessions are pointed at a private configuration home derived under
the selected profile, and trust is recorded there — never in the operator's own
document — for the repository the run was invoked against and the checkouts the
run made of it, and nothing else a session happens to open in. Pointing a run at
a repository is the act of trust; a workspace outside that stops the run rather
than degrading it.

`CLAUDE_CONFIG_DIR` selects which profile all of this reads and writes. Where it
is set, the document is `.claude.json` inside the named directory; where it is
unset, it is `~/.claude.json` beside `~/.claude` rather than in it, so naming
`~/.claude` outright selects a different document than naming nothing.
`CLAUDE_CODE_CUSTOM_OAUTH_URL` renames it `.claude-custom-oauth.json` in the
same place. Ahead of all of these, a `.config.json` inside the home is read
wherever one exists — the document's first name, which Claude Code still
honours and never creates, and which wins however empty it is. A derived home
lives in the checkout, under `.lup/sessions/`, whichever profile it was derived
from: it keeps a document of its own under the current name, seeded from
whichever document the profile is read from, and never a `.config.json`.
Every one of these spellings matters for an interactive fix: accepting a trust
dialog in a shell that does not export the same variables writes to a different
document and appears to do nothing.
### Reviewing a session's edits in an editor

Reviewing a whole-file write in a terminal is reading a wall of text and
deciding. An editor renders the same approval as a side-by-side diff you can
edit before accepting it, and a contained session can reach one on the host.
Nothing is built for this — the pieces are already here — so the recipe
is the whole of it:

1. Install the editor's Claude Code extension.
2. Open the editor on the checkout the session runs in.
3. Set `diffTool` to `auto` in your own Claude Code settings. It is global
   configuration rather than anything this repository declares, and the entry
   appears in `/config` only while an editor is connected.
4. Launch the session as usual, then `/ide` inside it.

`--profile` is not a reason to avoid any of this. The bridge binds the
lockfile directory the *editor* uses — resolved from `CLAUDE_CONFIG_DIR` as the
editor's own process reads it — so which account the session runs under and
which editor it talks to are independent. Each launch says which directory it
bound, so a bridge that will not connect is visible at the top of the session
rather than as an editor that never appears.

`/ide` is spelled out because a container is neither case the vendor documents:
a CLI started from the editor's own terminal attaches on its own, and one
started from an external terminal attaches when `autoConnectIde` is set. A
contained session is documented as neither, so it asks.

Two checkers will otherwise disagree over the same files. This repository
already refuses Claude Code's own `pyright-lsp` plugin, because the per-edit
check in the policy's host half is the one wired to the gates. The editor's own
Python checker is a third opinion and is not this repository's to turn off:
set `python.analysis.typeCheckingMode` to `off` in your editor if its
diagnostics start contradicting the ones the gate produces.

**Codex has no equivalent, and this is not an omission.** Its extension drives
the app-server and spawns its own core, so there is no lockfile rendezvous to
bridge; `ProviderLogin.editor_lockfiles` records that as a declaration rather
than as prose. The only attachment point the vendor exposes is a setting
naming the executable to run, which its own description marks as for
development only, warns may break the extension, and scopes to the whole editor
install — so it cannot differ per worktree, and it is user-level configuration
this repository does not write. A Codex editor session would need the extension
host inside the image, or a host posture with a prepared home. Neither is
built, and neither is needed for the review problem: what a Codex session's
reviewer reads is `review show`, or the dashboard.


Commit generated artifacts together with the catalog changes that produced
them. [contributing.md](contributing.md) covers what review looks for.
