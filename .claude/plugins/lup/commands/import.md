---
description: Import a feature or pattern from a tracked project or local Git source
allowed-tools: Bash(git:*, uv run lup-devtools:*), Read, Edit, Write, AskUserQuestion, Skill(lup:commit)
argument-hint: <project|path|ref> [BASE..SOURCE] <scope description>
---

# Import from Another Git Line

Import a feature, pattern, or scoped set of changes from a tracked project,
local repository, sibling worktree, or branch into this project. Unlike `/lup:update` which reviews a configured downstream as a whole, this
command works from one frozen revision range and accounts for every commit
and portable slice inside the scope the user named.

## Your Task

**Arguments provided**: $ARGUMENTS

### Parse Arguments

The first word is the **source selector**. It may be a tracked project name,
an existing repository or worktree path, or a branch/ref in this repository.
If the next word has the form `BASE..SOURCE`, it is the explicit revision
range. Everything after that is the **scope description** — a natural-language
description of what belongs in the import.

**Examples:**

- `/lup:import forecaster version reviewer pattern` — tracked project
`forecaster`, with the exact base and source revisions resolved before review
- `/lup:import ../feat-boundary dev..HEAD all non-sandbox features` —
the sibling worktree at `../feat-boundary`, explicit range `dev..HEAD`, and a
scope that excludes sandbox-only slices
- `/lup:import feature/ref clipboard workflow` — a ref in this
repository, with the base resolved and frozen before inventory

**With no arguments this is a sweep** rather than a question: every tracked
project is read for news, and what that finds becomes the scope. It is the
harvest direction — this repository reading other people's commits — and it
runs before anything below:

```bash
uv run lup-devtools sync fetch
uv run lup-devtools sync status
```

`status` reads what is already cached, so the fetch comes first or a quiet
answer may only mean nothing new was downloaded. Where no project has new
commits, report that and stop. Where one or more do, read each project's
commits with `sync log <project>` and `sync diff <project> <sha>` — the
**complete** diff, never skimmed — build the inventory before classifying
anything, then work through the steps below once per project with its range as
the frozen one. Advance the checkpoint with `sync mark-synced <project> --at <SOURCE>` after
a full pass, whether or not anything was applied.

Moving *this* project onto a newer version of what it was built from is the
other direction and another skill: `/lup:update` moves every carrier at once and reads no commits.

If only a source selector is provided (single word, no description), Ask the user with the AskUserQuestion tool, offering concrete options plus a free-text choice: what feature or scope to import from that source

## Steps

### 1. Commit pending changes

Invoke `/lup:commit` to commit any uncommitted work before importing.

### 2. Resolve the source and freeze the range

```bash
uv run lup-devtools sync status
```

Resolve the selector in this order:

1. An existing path is a local repository or worktree. Resolve its root with
   `git -C <path> rev-parse --show-toplevel`.
2. A configured project name is made available by the sync tooling and read
   through `refs/<project>`.
3. A Git ref that resolves in this repository uses this repository as its
   source root.

A tracked project may need its local copy materialized:

```bash
uv run lup-devtools sync log <project>
```

Resolve **both ends** to immutable commit ids before reading history. An
explicit `BASE..SOURCE` wins. Otherwise SOURCE is the selected ref, the review
ref printed by `sync status` for a registered project, or `HEAD` for an
unregistered path. Derive BASE with `git merge-base` against this
checkout only when both repositories actually share that history; if they do
not, ask the user to name BASE. Never silently substitute the root commit or
an arbitrary branch.

Print the source root, exact BASE, exact SOURCE, and commit count. Every later
history command uses those exact ids. Do not fetch, pull, switch, or otherwise
mutate the source.

### 3. Inventory the complete source range

Start from the range, not from keyword search:

```bash
git -C <source-root> log --reverse --oneline <BASE>..<SOURCE>
git -C <source-root> diff --name-status <BASE>..<SOURCE>
git -C <source-root> show --stat --oneline <commit>
git -C <source-root> show --find-renames --format=fuller <commit>
```

Inspect every commit in `BASE..SOURCE`, including follow-up fixes and commits
whose subject appears unrelated. A mixed commit is not out of scope merely
because most of it is: enumerate each portable hunk, symbol, declaration, test,
or documentation change separately.

Keyword and path search are supplemental. Use them after the range inventory to
locate definitions and callers, never to decide which commits exist.

```bash
git -C <source-root> log --oneline <BASE>..<SOURCE> --grep="<keyword>"
```

Never use `git log --all` for this workflow. Recovery namespaces such as
`refs/lup/undo` retain old commits deliberately; including every ref makes
reverted or superseded work look current, which hides the actual source
boundary.

### 4. Build the import ledger

Read the full source files around every candidate and search this checkout for
existing equivalents. Group related commits and hunks into feature families,
then keep a ledger in the response (not a tracking file) with one row per
whole commit or mixed-commit slice:

- source commit and source file/hunk
- feature family and behavior
- classification: in scope, out of scope, already present, or superseded
- target file/symbol
- dependencies and follow-up fixes
- proposed disposition and evidence

Every commit in the frozen range must appear in at least one row, even when the
row says why none of it belongs. This is how a subject line about one domain
does not hide a reusable helper added in the same patch.

If multiple interpretations of the requested scope remain, present them and
ask which one the user means. If nothing matches, show the accounted range and
ask the user to clarify or point to specific files.

### 5. Analyze portability

Before porting, analyze what needs to change:

- **Domain-specific → generic**: Identify names, types, and logic tied to the downstream domain
- **Mixed commits**: Extract portable slices without restoring deleted or
  explicitly excluded subsystems around them
- **Dependencies**: Check if the feature requires packages not in the current project
- **Integration points**: Where does this pattern connect to the rest of the codebase?
- **Conflicts**: Does anything in the current project overlap with or contradict this pattern?
- **Follow-ups**: Include later fixes, tests, and documentation that make the
  source feature complete at SOURCE, rather than stopping at its first commit

Present your analysis and proposed adaptation plan, then Request explicit user approval before making any change to this repository. Reason: the pattern comes from another project and has to be adapted, not copied.

### 6. Port the pattern

For approved imports:

1. Read the full source files and their current target counterparts
2. Adapt the code:
   - Replace domain-specific identifiers with template-appropriate equivalents
   - Adjust import paths to match the current project structure
   - Follow the current project's coding conventions (see its guidance file)
   - Remove domain-specific logic, replace with generic scaffolding or placeholders
3. Prefer `git cherry-pick -x` for a wholly portable commit. For a mixed
   commit, apply only the ledgered slice and keep the source commit in the
   handoff/commit evidence.
4. Write or edit files in the current project. Preserve intentional deletions
   and audit every conflict against both sides; a source rename must not erase
   a target-only addition.
5. If a canonical harness input changed, regenerate **both** native trees:

```bash
uv run lup-devtools harness generate all
```

Never generate or hand-edit only one runtime.

6. Verify:

Start a `Monitor` over `uv run lup-devtools dev check`. Each line it emits arrives as an event, and the watch ends when the command does. Do not run it through `Bash`, whose long timeout returns once at the end, and do not read a backgrounded session on a loop — both are polling, however patient. A watch that outlives your report wakes you after you have finished, so stop it with `TaskStop` before reporting unless the command has exited

It runs ruff, pyright, and the test suite, and reports as it goes rather than
only at the end.

### 7. Audit completeness

Re-run the exact commit list and range diff from step 3. Reconcile every ledger
row against the result:

- **imported** — name the target file/symbol and the verification that exercises it
- **already present** — point to the equivalent target implementation
- **excluded** — state the scope reason, especially for domain- or
  sandbox-specific slices
- **superseded** — name the later source commit or target behavior that replaces it

There may be no unaccounted commit, mixed hunk, dependency, test, or follow-up
fix. Search the target for each imported public symbol and behavior, review the
final diff for accidental deletions, and run `dev check` again after anything
the audit corrects.

Report the immutable BASE and SOURCE ids with the final ledger, so another
reviewer can reproduce what “all” meant without consulting moving branches.

### 8. Optionally commit

Offer to commit the imported pattern, through `/lup:commit` as in step 1 — it groups the change and writes the message in a form the permission policy allows, which a hand-rolled `git commit` here would have to get right again.

## Guidelines

- **Generalize, don't copy** — The downstream code is domain-specific. Port the _pattern_, not the domain details.
- **Preserve intent** — Understand _why_ the downstream repo built this pattern, not just _what_ it does. The adaptation should serve the same purpose.
- **Check for existing work** — Before porting, search the current project for similar patterns that could be extended instead of duplicated.
- **Range first, search second** — `BASE..SOURCE` is the inventory.
  `--grep` helps explain it; `--all` is forbidden because recovery refs are
  not source history.
- **Account for mixed commits** — Classification happens at the smallest
  portable slice, not at the commit subject.
- **Prove completeness** — Finish with the reconciled ledger, not an intuition
  that the interesting commits were probably found.
- **Minimal dependencies** — If the pattern pulls in new packages, flag this to the user and ask whether to proceed.
- **Test after porting** — `dev check` in step 6 is that bar; it runs ruff, pyright and the suite together, so reaching for them one at a time only makes it easier to stop after the first.
