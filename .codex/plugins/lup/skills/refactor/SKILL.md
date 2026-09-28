---
name: refactor
description: Rewrite a file or folder from scratch while respecting coding conventions
---

# Refactor from Scratch

Rewrite a file (or folder) from scratch, preserving intent but enforcing coding conventions.

## Your Task

**Arguments provided**: the arguments supplied with this skill invocation

### Parse Arguments

The argument is a **file or folder path**. If no path is provided, Ask the user directly, offering concrete options, and wait for the answer: which file or folder to refactor

Resolve relative paths against the current working directory.

## Steps

### 1. Validate the target

- Confirm the path exists (file or directory)
- If it's a directory, list all files that will be refactored using `find`
- Show the user what will be refactored and ask for confirmation

### 2. Create a backup

Commit the current state so it can be recovered with `git diff` or `git checkout`:

```bash
git add <path>
git commit -m "refactor: snapshot before rewrite of <path>"
```

This commit serves as the backup — `git diff HEAD~1` shows exactly what changed, and `git restore --source=HEAD~1 -- <path>` restores the original.

### 3. Understand the original

For each file being refactored:

- Read the entire file
- Identify its **purpose**: what does this code do?
- Identify its **public interface**: exports, function signatures, class APIs
- Identify its **dependencies**: imports, external calls
- Identify its **side effects**: file I/O, network calls, state mutations
- Note any tests that import from or test this file (use `find_references` to resolve them)

### 4. Read coding conventions

Read the project's guidance file (`AGENTS.md`) to understand:

- Type safety requirements
- Error handling philosophy
- Code style preferences
- Library choices and patterns
- DRY principles and where utilities belong

Also scan neighboring files in the same package to understand local patterns and conventions.

### 5. Rewrite from scratch

For each file, **write it fresh** using the Write tool — do not edit the original incrementally. The rewrite must:

- **Preserve the full public interface** (same exports, same function signatures, same behavior)
- **Include everything the original included** — no features dropped, no edge cases lost
- **Follow all coding conventions** from the guidance file (types, error handling, style, etc.)
- **Improve structure** where the original was unclear or poorly organized
- **Keep the same filename and location**

### Prompt Content Is Code

Prompt strings (system prompts, agent instructions, guidance templates) are code — they have intent, structure, and conventions. When refactoring files containing prompts, apply the same guidance principles:

- **DRY**: If the same advice appears in multiple sections, consolidate. Every token in a system prompt is paid on every invocation.
- **Code as Documentation**: No defensive language added after a single bad outcome. No references to historical performance. Instructions should be structural, not reactive.
- **Avoid over-engineering**: Every section must earn its token cost. Guidance for rare cases shouldn't bloat the universal prompt that all invocations pay for. Move case-specific strategy into case-specific guidance blocks.
- **No redundant emphasis**: Saying the same thing 3 times in different sections isn't reinforcement — it's noise. State advice once in the right place.
- **Structural over behavioral**: "Always do X" is better than "Don't do Y, we've seen this go wrong." Frame instructions as what TO do, not what mistakes to avoid.

Present prompt content changes as a review with findings — prompt changes affect agent behavior and may require a version bump.

### 6. Verify

After rewriting:

- Verify types, lint, and tests:

Open `uv run lup-devtools dev check` with `exec_command`, which holds a live PTY, and read what it has emitted with `write_stdin` carrying no keystrokes. Keep the one session for as long as the command runs: re-running it starts over and loses everything it already reported

- Report any issues found and fix them

### 7. Summary

Show the user:

- What was refactored
- How to compare: `git diff HEAD~1 -- <path>`
- How to revert: `git restore --source=HEAD~1 -- <path>`
- Key improvements made
- Any issues found during verification

## Important

- **Never drop functionality.** The rewrite must do everything the original did.
- **Ask before proceeding** if the target is large (>5 files or >500 lines total).
- **Preserve tests.** If tests reference the refactored code, ensure they still pass.
