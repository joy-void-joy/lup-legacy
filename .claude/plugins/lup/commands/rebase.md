---
description: Clean up commit history on the feature branch and open/update a PR
allowed-tools: Bash(uv run lup-devtools:*, git:*), Read, AskUserQuestion, Skill(lup:commit)
argument-hint: '[target-branch]'
---

# Rebase and PR

Clean up the commit history on the current feature branch, push it, and open (or update) a PR.

## Determine Where This Runs

Everything below reads the current worktree, so settle which one that is before deciding anything else.

```bash
git worktree list
git branch --show-current
```

The feature branch is the branch you are standing on -- never something passed in. On an integration branch (`main`, `dev`, `master`) there is nothing to rebase: name the feature worktree, `cd` there, and start again from here.

That is also the answer when you were about to put a branch name in the argument. The argument is a PR *target*, which is a rare thing to need; wanting to say *which branch to work on* means you are in the wrong directory, and moving is the fix rather than naming it.

## Determine Branches

### Base branch (`<base>`)

Run `uv run lup-devtools git pr sync-base --json` (step 1 below) -- it reports the base branch and a `base_source`. `recorded` (from worktree creation) and `explicit` are authoritative. `guessed` means topology alone picked it: the command merges nothing and exits non-zero. Ask the user with the AskUserQuestion tool, offering concrete options plus a free-text choice: which branch is the true base, then rerun as `sync-base --base <branch>`.

Confirm against the divergence, not the name that sounds right. `git log --oneline <candidate>..HEAD` on the true base shows this branch's own commits; on a wrong one it shows hundreds, which is the tell that the whole history between the two branches is about to be treated as yours.

### PR target (`<target>`)

Rarely needed: a stacked PR aimed at another feature branch. If a target branch was provided as an argument (`$ARGUMENTS`), use it. Otherwise, `<target>` defaults to `<base>`.

## Pre-rebase Validation

### 1. Commit pending changes

Invoke `/lup:commit` to commit any uncommitted work before starting the rebase.

### 2. Sync and merge base

```bash
uv run lup-devtools git pr sync-base --json
```

A `guessed` base exits non-zero having merged nothing -- settle the base as above and rerun with `--base <branch>`. If conflicts are reported, resolve with `/lup:merge` (no argument) first.

Merging the base commonly leaves a generated tree behind its source -- most often the ownership manifest, which the next `dev check` reports as `harness drift: FAIL` with a stale proof. That is the merge working, not a conflict, and the settle guards answer it: at `post-merge` and `post-commit` they regenerate over the merge commit and fold what that writes into it. Where `uv run lup-devtools git hooks status` shows them unarmed, `uv run lup-devtools git settle` does the same by hand.

### 2b. Confirm the base matches its remote

`sync-base` merges the *local* base. The PR is read against the remote one, so anything the local base holds that the remote does not becomes part of your diff.

```bash
git fetch origin
git rev-list --left-right --count origin/<base>...<base>
```

A base ahead of its remote carries those commits into the PR, and on a repository that commits session data to its base branch that is the difference between a five-commit PR and a hundred-commit one nobody can read. Behind is ordinary -- the merge in step 2 has already brought the remote's work in.

When it is ahead, this is the user's call, not yours, because one of the answers publishes their unpushed work. Ask the user with the AskUserQuestion tool, offering concrete options plus a free-text choice: push the base, or rebuild this branch onto the remote base. Rebuilding means cherry-picking this branch's own commits onto `origin/<base>`, which touches nothing of theirs; verify it as step 8 does before going on.

Whichever they choose, check that the base's own commits do not overlap your files -- `git diff --stat <base>...origin/<base> -- src/ tests/`. Where they do, your work has to be reconciled with theirs whatever the PR ends up looking like.

### 3. Fold local grants into the canonical policy

Check whether `.claude/settings.local.json` exists. Its permission entries are ad-hoc grants one person accepted; leaving them there means the next person re-approves the same prompts.

`.claude/settings.json` is a generated artifact (`harness.project-settings`) -- never hand-merge into it. The next `lup-devtools harness generate all` regenerates it from the policy and drops the edit. Shell, fetch, and edit permissions belong to the canonical semantic policy instead.

Classify each entry:

- **Already covered by the policy** -- drop it from the local file. Most read-only commands are.
- **A genuine gap** -- invoke `/lup:hooks` to add it to the canonical policy, regenerate both native plugins, and commit that as a separate commit.
- **User-specific** (plugin toggles, personal model choice) -- leave it in `.claude/settings.local.json`.

### 4. Run checks

Start a `Monitor` over `uv run lup-devtools dev check`. Each line it emits arrives as an event, and the watch ends when the command does. Do not run it through `Bash`, whose long timeout returns once at the end, and do not read a backgrounded session on a loop — both are polling, however patient. A watch that outlives your report wakes you after you have finished, so stop it with `TaskStop` before reporting unless the command has exited

It runs ruff, pyright, and the test suite, and reports as it goes rather than
only at the end.

Fix any failure this branch introduced. A failure the base already carries is not this branch's to fix: confirm it by running the same check on `<base>`, name it and its origin when reporting, and continue. Fixing it here buries an unrelated change in this PR; staying silent about it lets the next run inherit it as though it were yours.

## Process

### 5. Push and open PR

Prepare against the freshly fetched PR target before publishing:

```bash
git fetch origin
uv run lup-devtools git pr prepare --base origin/<target> --json
```

This local command requires a clean checkout, merges the exact target commit,
regenerates every harness from the combined sources, and commits the result.
It pushes nothing. GitHub cannot run the clone's generated-file merge driver;
including its target as an ancestor lets its ordinary merge preserve the
regenerated proofs. Resolve any source conflicts with `/lup:merge`,
regenerate, and complete the merge before continuing.

Open the PR **now, before the history is rebuilt** -- never after. The force-push in step 9 lands in the PR timeline as a force-push event, so the PR carries both the history as it was actually worked and the cleaned sequence that replaced it. Creating it after the rebuild saves one body update and throws that whole trace away.

```bash
uv run lup-devtools git pr push --json
```

**If no existing PR** (first run), draft a title and summary, then:

```bash
uv run lup-devtools git pr create --base "<target>" --title "<title>" --body-file "<path>"
```

Write the body to a file and pass `--body-file`. A body worth reading has headings, code spans and prose, and prose has apostrophes: as a `--body` argument every one of them is yours to escape, and a missed one truncates the document into a shell parse error naming an offset rather than the body. `--body` stays for a one-liner.

**If PR already exists**, skip -- we'll force-push the cleaned history later.

### 6. Understand all changes

- Review the full diff: `git diff <base>...HEAD`
- Read changed files to understand the complete set of modifications
- Think about logical units of work (features, refactors, fixes, tests, docs)
- **Ignore existing commit history** -- focus on what makes sense as a clean sequence

### 7. Reset and rebuild commits

Mark where the history was before touching it. The rebuild is the one step
that can lose a file, and a rebuild that dropped one looks exactly like a
rebuild that did not -- the tree is clean either way, and the checks pass
because what is missing is what would have failed them.

```bash
git branch -f rebase-backup HEAD
git reset --soft <base>
```

All changes are now staged. For each logical unit of work:
- Selectively unstage with `git reset HEAD <files>`, then stage and commit relevant pieces
- Or use `git commit` with specific files to build atomic commits
- Order logically: dependencies first, then features, then polish
- Use conventional format: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`

### 8. Prove the rebuild lost nothing

Rebuilt commits are a claim about the same tree, so check it against the mark
before the force-push makes it unrecoverable:

```bash
git diff rebase-backup HEAD --quiet && git branch -D rebase-backup
```

Silent means identical, and the backup is gone because it has served. Any
output is a file the rebuild dropped: find it in `git diff rebase-backup HEAD
--stat`, commit it where it belongs, and check again. Never force-push past
this -- the mark is the only copy of what the branch used to hold.

### 9. Force push and update PR

Fetch and run `git pr prepare --base origin/<target> --json` again after the
history rebuild. If the target advanced, review and check its resulting local
merge commit before publishing. This preserves server mergeability after the
rebuild and makes the target commit used explicit.

```bash
uv run lup-devtools git pr push --force --json
```

Update the PR body with a commit list:

```bash
uv run lup-devtools git pr update <PR_NUMBER> --body-file "<path>"
```

Return the PR URL to the user.

## Guidelines

- **Never rebase dev/main/master**
- **Confirm before force push**
- **`git pr push --force` pushes with a lease**: it replaces only what this checkout last pushed or built on, so a push somebody else made meanwhile is refused rather than overwritten -- fetch it, fold it in, and push again. It refuses an integration branch outright; spelled out by hand, a force needs `--force-with-lease` and a named feature branch, or it asks
- **Never force-push an unverified rebuild**: step 8 is what makes the cleaned history a claim you checked rather than one you made
- **Keep meaningful history**: Don't squash everything into one commit
- **Write good messages**: Future you will thank present you
