---
name: commit
description: Review all diffs and create atomic commits
---

# Create Commits

Review all uncommitted changes and create well-structured atomic commits.

## Your Task

Create commits for all staged and unstaged changes, following conventional commit format.

## Phase 1: Assess Changes

Run these commands in parallel:

1. `uv run lup-devtools dev pending` - See all real changed files
2. `git diff` - See unstaged changes
3. `git diff --cached` - See staged changes
4. `git log --oneline -10` - See recent commit style

`dev pending` is authoritative because a sandbox may mask sensitive dotfiles
with device nodes that `git status` misreports as untracked repository content.

## Phase 2: Group Changes

Analyze the changes and group them into logical commits:

- **One logical change per commit**: If changes serve different purposes, split them
- **Related files together**: Changes to a module and its tests go together
- **Order matters**: Earlier commits should not depend on later ones

## Phase 3: Create Commits

For each group:

1. Stage the relevant files: `git add <files>`
2. Create the commit with conventional format. A subject and a body are two
   `-m` arguments, which the shell passes through untouched:

```bash
git commit -m "type(scope): description" -m "Optional body with more details."
```

A body long enough to want headings, code spans, or blank lines goes in a file
instead, for the reason `rebase` writes a pull-request body to one: every
apostrophe and backtick in an inline argument is yours to escape, and a missed
one truncates the message into a shell parse error naming an offset.

```bash
git commit -F <path>
```

Never wrap the message in a command substitution — `git commit -m "$(cat …)"`
and its heredoc form are denied by the permission policy, because a
substitution's result could expand into a guarded flag. The two spellings above
are the whole vocabulary; neither needs an escape hatch.

### Commit Types

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


### Examples

```
feat(agent): add retry logic for API calls
fix(tools): handle empty response from search
refactor(config): extract settings validation
meta(harness): add new workflow command
```

## Phase 4: Verify

After creating commits:

1. Run `git log --oneline -5` to show what was created
2. Run `uv run lup-devtools dev pending` to confirm the working directory is clean

## Guidelines

- **Never amend** unless explicitly requested
- **Never force push** to dev/main/master
- **Don't skip hooks** unless explicitly requested
- **Don't commit secrets** (.env.local, credentials, API keys)
- **Don't commit large binaries** unless necessary
- **Session data** (notes/traces/) is not committed here: the feedback loop's own page says how one session becomes one `data(sessions):` commit

## If Pre-commit Hooks Fail

1. Fix the issue (formatting, linting, etc.)
2. Re-stage the fixed files
3. Create a **new** commit (don't amend - the previous commit didn't happen)
