---
name: modify-command
description: Modify an existing slash command based on a description or delta
---

# Modify Existing Slash Command

## Your Task

The user wants to MODIFY an existing slash command. Parse the arguments to determine which command to change and how.

**Arguments provided**: the arguments supplied with this skill invocation

### How to Parse Arguments

The first word is the **command name** to modify. Everything after is the **delta** (what to change) or a **new description** (replace the command's behavior entirely), with an optional `--args` flag.

**Basic:** `$lup:modify-command commit Add a step that runs ruff format before committing`

- Command name: `commit`
- Delta: "Add a step that runs ruff format before committing"

**With args:** `$lup:modify-command debug Add verbose flag --args [error] [--verbose]`

- Command name: `debug`
- Delta: "Add verbose flag"
- New argument hints: `[error] [--verbose]`

When `--args` is provided, set `argument_hint` on the declaration to the specified hints — the `argument-hint` frontmatter is what generation renders from it, never a place to edit — and ensure `the arguments supplied with this skill invocation` is handled in the command body.

### If No Arguments Provided

If `the arguments supplied with this skill invocation` is empty, Ask the user directly, offering concrete options, and wait for the answer: which command to modify, and what changes to make to it

### Steps

1. **Parse** the command name and delta from the arguments
2. **Find** the source -- search in these locations, in order:
   - `lup.harness.content.skills.<name>`, then `src/lup_template/harness/content/skills/<name>.py` (lup skills, including every `lup:name` variant -- the underscored module name; the library half holds the skills about agent work, this project's the ones about being a template). Where lup is not vendored here, read its modules with `uv run lup-devtools dev py source` rather than by path
   - a command the project or the person defined natively, outside any plugin

   Files under .claude/plugins/lup/commands/ under Claude Code, .codex/plugins/lup/skills/ under Codex are generated from the declarations -- read them to see the rendered result, never to edit.
3. **Read** the current declaration in full
4. **Analyze** the delta -- determine whether the user wants to:
   - **Add** new behavior (append steps, add sections)
   - **Change** existing behavior (modify instructions, update tools)
   - **Remove** behavior (simplify, strip sections)
   - **Replace** entirely (new description overrides old)
5. **Show the user** the proposed changes: summarize what will change, show
   before/after for key sections if helpful, then Request explicit user approval before writing the changed declaration. Reason: the change reaches every tree the declaration renders into.
6. **Apply** the changes -- edit the declaration's prompt parts
7. **Update the `tools` list** if needed (e.g., new grants for added functionality)
8. **Follow what the prose recites** — a skill interpolating a value from another declaration cannot be changed on its own. Change it where it is declared, and declare a migration (one file under the library's `migrations/pending/`) if a name left with it: a capability that goes has to leave somebody downstream something to read, and `dev check` fails until one does
9. **Regenerate** with `uv run lup-devtools harness generate all` when the source was a lup skill — not `harness claude` or `harness codex`, which regenerate one target and then launch it
10. **Confirm** the modification and show a summary

### Guidelines

- **Preserve the declaration's structure and style** -- match the raw-string and part-splitting patterns of the existing module
- **Don't over-modify** -- only change what the delta requires. If the user says "add X", don't also reorganize unrelated sections.
- **Update the `tools` list** if the delta introduces new tool requirements (e.g., adding a git step requires `Bash(git:*)`)
- **Keep the skill self-contained** -- it should work without requiring the user to remember the delta
- **Preserve working behavior** -- don't break existing functionality unless the user explicitly asks to replace it
