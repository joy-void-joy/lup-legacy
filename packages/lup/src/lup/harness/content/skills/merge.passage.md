# Merge

Two modes depending on arguments:

- **`{{ merge_skill }} <target>`** — Merge `<target>` branch into the current branch, with intelligent conflict handling.
- **`{{ merge_skill }}`** (no argument) — Detect and resolve conflicts from an in-progress merge, rebase, or cherry-pick.

**Arguments provided:** {{ arguments }}

If an argument is provided, parse it as the target branch name and go to **Mode A**.
If no argument is provided, go to **Mode B**.

---

## Mode A: Merge a Target Branch

### 1. Commit pending changes

Invoke `{{ commit_skill }}` to commit any uncommitted work before starting the merge. This ensures a clean working tree and a restore point if the merge goes wrong.

### 2. Assess the situation

```bash
# Current branch and status
git branch --show-current

# Verify the source branch exists
git branch -a | grep <branch>

# Find the merge base
git merge-base HEAD <branch>
```

### 3. Understand both sides

**Source branch (what's being merged in):**

```bash
# Commits on the source branch since divergence
git log --oneline $(git merge-base HEAD <branch>)..<branch>

# Files changed on the source branch
git diff --stat $(git merge-base HEAD <branch>)..<branch>

# Full diff for understanding intent
git diff $(git merge-base HEAD <branch>)..<branch>
```

**Current branch (what we're merging into):**

```bash
# Commits on current branch since divergence
git log --oneline $(git merge-base HEAD <branch>)..HEAD

# Files changed on current branch
git diff --stat $(git merge-base HEAD <branch>)..HEAD
```

Summarize:
- **Source scope**: What the source branch does (features, fixes, refactors)
- **Current scope**: What the current branch does
- **Overlap**: Files changed on both sides

### 4. Predict conflict severity

```bash
uv run lup-devtools git preview <branch> --into HEAD
```

It merges in memory, touching neither the index nor this working tree, and names the files that would conflict — or, where nothing would, every file the merge changes and by how many lines. `nothing new` means the current branch already holds every line the source carries, however its commits were rewritten.

Classify the merge into one of:

- **Clean**: No conflicts. Proceed with `git merge`.
- **Light conflicts**: A few files with small, clearly-resolvable conflicts. Proceed with `git merge`, then resolve in-place.
- **Heavy conflicts**: Many files, structural reorganization on both sides, or changes that interleave in ways `git merge` handles poorly. Use **manual application** (step 4b).

Show the conflict prediction and recommend a strategy, then {{ ask }}

### 5a. Standard merge (clean or light conflicts)

Which spelling to use depends on whether this branch already carries the target. Where it does -- `sync-base` pulled the integration branch in before the rebase, which is the route `{{ land_skill }}` arrives by -- the join already happened in the branch's own worktree, so the target is an ancestor of the branch, and a merge commit here would record a second join of two lines that no longer diverge:

```bash
git merge --ff-only <branch>
```

Where the two have genuinely diverged -- Mode A's other use, merging one feature branch into another -- the merge commit is what records that they were joined, and `--no-ff` keeps it even where a fast-forward was available:

```bash
git merge --no-ff <branch>
```

A refusal from `--ff-only` is the useful signal rather than an obstacle: it says the target moved after the rebase, so the branch is rebased again in its own worktree instead of the divergence being buried under a merge commit here.

If conflicts arise, resolve them using the **Resolution Decision Tree** below, then stage resolved files and complete the merge.

### 5b. Manual application (heavy conflicts)

When the conflict surface is too large or structural, apply changes manually instead of fighting git's merge machinery:

1. **Read each changed file from the source branch:**

   ```bash
   # Get the source version of each changed file
   git show <branch>:<filepath>
   ```

2. **For each file, compare source changes against the current version:**
   - Read the current file
   - Read the source branch version
   - Read the merge-base version (the common ancestor)
   - Identify what the source branch *changed* relative to the base
   - Apply those changes to the current version, adapting to any refactoring or restructuring done on the current branch

3. **Apply changes semantically, not textually:**
   - If the source branch added a function, add it to the current file in the right location
   - If the source branch modified a function that was renamed on the current branch, apply the modification to the renamed version
   - If the source branch changed imports, merge the import lists
   - If both sides restructured, use the current branch's structure and port the source's features into it

4. **For ambiguous cases**, put the choice to the user:
   - Show both versions
   - Explain what each side intended
   - Recommend an approach

5. **After all files are applied**, create a merge commit that records the source branch as a parent:

   ```bash
   # Stage all changes
   git add <modified-files>

   # Create a merge commit that records lineage
   git commit -m "$(cat <<'EOF'
   merge: incorporate changes from <branch>

   Manual merge — applied changes semantically to avoid conflict churn.
   EOF
   )"
   ```

### 6. Validate

After the merge (either strategy):

```bash
# Check for leftover conflict markers
grep -rn "^<<<<<<< \|^=======$\|^>>>>>>> " <changed-files> || echo "No conflict markers found"

# Verify the real tree is clean, excluding sandbox-masked device paths
uv run lup-devtools dev pending

# Quick sanity check — does it parse/compile?
# (adapt to the project's tooling)
```

Read each modified file to verify the merge result makes sense — no duplicate functions, no broken imports, no dropped code.

### 7. Deletion audit

Compare the merge result against both parents:

```bash
# What existed in the source branch but not in the result?
git diff <branch> -- <changed-files> --stat

# What existed in HEAD (pre-merge) but not in the result?
git diff HEAD~1 -- <changed-files> --stat
```

If any functions, classes, or significant code blocks were dropped, verify the deletion was intentional.

### 8. Report

Summarize:
- Strategy used (standard merge vs manual application)
- Files merged and how conflicts were resolved
- Any items that need the user's attention

---

## Mode B: Resolve Existing Conflicts

When invoked without a target branch, detect and resolve conflicts from an in-progress merge, rebase, or cherry-pick.

These commands are spelled without `uv run` — and must stay that way — because `uv` parses `pyproject.toml` before it runs anything, so an integration merge that conflicts the manifest takes the whole conflict toolchain down with it. What replaces it is the console script in this project's own environment, which imports the package directly and starts whatever the manifest currently says.

Where that script is depends on where the environment is, so the commands below spell the `uv` default and you substitute if yours differs. An environment inside the checkout — `uv sync` writes `.venv` — is named by path, because a bare name would let a sibling worktree's environment answer for this one. An environment outside it — conda, pyenv, a system install, `UV_PROJECT_ENVIRONMENT` — is reached as plain `lup-devtools` once activated, and has no per-checkout copy for the lookup to confuse. `lup-devtools git conflict status --json` will tell you which you have: if it starts, use the bare name throughout.

### 1. Assess the situation

```bash
.venv/bin/lup-devtools git conflict status --json
```

This reports the operation type (merge/rebase/cherry-pick), conflicted files, and commits on both sides.

### 2. Understand what each branch does

From `ours_commits` and `theirs_commits` in the status output, derive the **branch scope** for each side — a summary of everything each branch is about.

### 3. Resolve each conflicted file

For each file in `conflicted_files`:

1. Read the full file to see all conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`)
2. Understand what each side changed by reading surrounding code
3. Resolve using the **Resolution Decision Tree** below
4. Validate: no remaining markers, syntactically correct, no duplicate imports
5. Stage: `git add <file>`

### 4. Deletion audit

After resolving all conflicts but **before completing the merge**:

```bash
.venv/bin/lup-devtools git conflict audit <conflicted-files> --json
```

Review the audit output. If any files have `warning: true`, check that the removals are intentional. Fix unjustified deletions before completing.

### 5. Complete

```bash
.venv/bin/lup-devtools git conflict complete
```

---

## Resolution Decision Tree

Used by both modes when resolving conflict hunks.

### Step A: Scope classification

Classify each conflict hunk against the branch scopes:

- **In-scope** — The conflict is in code this branch intentionally changed. **Take ours** (HEAD).
- **Out-of-scope** — The conflict is in code this branch didn't intentionally modify. **Take theirs**.
- **Mixed / ambiguous** — Both sides made intentional changes. Proceed to Step B.

**Direction awareness:** In initialization or sync merges, verify which side has more content — the richer side is the "authority" side.

### Step B: Resolve mixed/ambiguous conflicts

#### Auto-resolve (no user input needed)

- **Non-overlapping additions** — Both sides add different content. **Combine both.** Where a file's every hunk is both sides inserting at one place — two entries prepended to one declared list, the shape a changelog or a registry conflicts in — `.venv/bin/lup-devtools git conflict union <file>` rebuilds it from the three index stages with both insertions kept, ours first unless `--theirs-first`, and stages it. It refuses, leaving the file as git left it, where both sides changed the same lines.
- **Clear superset** — One side is a strict superset. Take the superset.
- **Whitespace / formatting only** — Take either side consistently.
- **Identical intent** — Same change, trivially different wording. Take either.
- **Refactoring vs features** — One side refactored, the other added features. **Keep both.**

#### Ask the user

- **Different approaches** — Both sides solve the same problem differently.
- **Conflicting deletions vs additions** — One side removes code the other modifies.
- **Structural reorganization** — Both sides restructured the same section differently.
- **Ambiguous priority** — Can't tell which version is better without domain knowledge.

**When asking:** Show the exact conflict as labeled code blocks before you ask. Explain what each side was trying to do. Offer "combine both" when feasible.

---

## Guidelines

- **Bias toward inclusion**: Never silently drop code. If both sides have value, keep both.
- **Understand intent before acting**: Read commits, not just diffs. A rename on one side must not swallow an addition on the other.
- **Manual application is the escape hatch**: When `git merge` would produce an unreadable mess, skip it entirely and apply changes by understanding what they do.
- **Always confirm with the user** before choosing a strategy, especially for manual application.
- **Preserve lineage**: Even in manual merges, the commit message should reference the source branch so history is traceable.
- **Watch for semantic conflicts**: Combined code must make sense (renamed variables, etc.)
- **Check adjacent code**: Nearby non-conflicting code may also need updating
