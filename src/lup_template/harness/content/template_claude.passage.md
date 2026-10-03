# CLAUDE.md Template

This file exports portable sections from the upstream CLAUDE.md as a scaffold for downstream projects. It contains conventions, workflow patterns, and coding standards that apply to any project using lup.

**How it's used:** `{{ init_skill }}` and `{{ install_skill }}` perform a **section-level merge** — they use the `<!-- section: ... -->` markers below to identify independent merge units, compare them against the target's existing CLAUDE.md, add sections that are missing, and leave existing sections untouched. Placeholders like `<project>` are replaced with the actual project name.

---

<!-- section: CLAUDE.md -->
# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

**Note:** Modifying `CLAUDE.md` means modifying `.claude/CLAUDE.md` (this file).


<!-- passage: agent-vocabulary -->
- **Claude** = the meta-agent (Claude Code) that modifies the codebase, runs commands, and manages the development workflow

<!-- passage: naming -->
"Lup" is the framework's name for the inner agent, not a project-specific term. Use "Claude" when referring to the outer development agent and "Lup" when referring to the inner SDK agent, regardless of the project's package name.
<!-- passage: orchestration-pointer -->
 `docs/orchestration.md` carries the full catalog.
<!-- passage: regeneration-pointer -->
`lup-devtools harness claude` regenerates and launches the verified local plugin
<!-- passage: editing-style -->
## Editing Style

**Prefer small, atomic edits.** A PreToolUse hook counts "real" changed lines (ignoring imports, comments, whitespace, blank lines, docstrings, string literals, type annotations, and TypedDict/BaseModel bodies) and auto-allows edits with <=3 real changes per change block. Pure deletions and single-line `replace_all` renames are auto-allowed; multi-line `replace_all` falls through to the size gate. Anti-pattern detection runs before any auto-allow, and `Write` (full-file rewrites) never auto-allows.

- **Split large changes into multiple small edits** -- keep real (non-trivial) line changes to <=3 per Edit call
- **Separate concerns** -- move imports in one edit, change logic in another (import changes are trivial and don't count)
- **Use `rename-symbol`** for identifier renames instead of `Edit` with `replace_all`


<!-- passage: diagnostics -->
## Diagnostics after an edit

Every edit is type-checked, by the hook rather than by an editor's language server. The checker runs in the checkout that holds the file you edited, so its answer is about the copy you changed. Findings for that file arrive as a hook error naming the line; nothing arrives when it checks out clean.

That rooting is the point. A language server the runtime starts is rooted once, where the session opened, and keeps that root after work moves to a worktree -- so it resolves the same module names against the launch checkout and reports, with no sign anything is wrong, about a file nobody edited. Anything a session-wide language server tells you about a file in a worktree is worth confirming against this.

The `codeintel` tools answer navigation questions the same way, per question rather than per session. **Use them actively** -- they resolve imports and aliases, which grep cannot.


<!-- passage: relative-paths -->

A relative path resolves against the checkout being edited, which the same hook publishes. Pass an absolute path when you mean a file somewhere else. Use grep for literal text and non-Python files; use the edit tools for changes after resolving the relevant symbols.


<!-- passage: launching -->
`lup-devtools harness claude` regenerates, verifies, and runs Claude Code with
the local Lup plugin and the active profile's account (`CLAUDE_CONFIG_DIR`).
`lup-devtools dev usage claude` reports usage for the chosen profile, and
`lup-devtools dev usage codex` reports the other backend's. Accounts, the
theme and the default tier and effort are the person's, kept once in
`~/.config/lup/` (`$XDG_CONFIG_HOME/lup`) for every repository: profiles as
directories under `profiles/`, curated with `lup-devtools harness profile`,
and the rest in `config.toml`.

Each repo names its plugin **marketplace** after the project — the plugin entry stays `lup`, so `{{ skill_pattern }}` is identical everywhere. Marketplace names share one global namespace (`~/.claude/plugins/known_marketplaces.json`), so a shared name like `lup`/`local` collides across repos and an install from one shadows the others; `lup-devtools dev plugin name` (run by `{{ init_skill }}` and `{{ install_skill }}`) wires the per-project name.


<!-- passage: settings -->
## Settings & Configuration

All Claude Code settings modifications should be **project-level** (in `.claude/settings.json`), not user-level.

---

<!-- section: Process & Communication -->
# Process & Communication

## Asking Questions

**Always use the `AskUserQuestion` tool** instead of asking questions in plain text. This applies to:

- Clarifying requirements or ambiguous instructions
- Offering choices between implementation approaches
- Confirming before destructive or irreversible actions
- Proposing changes or improvements
- Any situation where you need user input before proceeding

Even for open-ended questions, use `AskUserQuestion` with options that include a custom input option. This allows structured notification parsing.

**When proposing changes:**

- **Propose, don't assume**: Use AskUserQuestion before making changes
- **Show context**: Show relevant current state before proposing
- **Explain rationale**: Every suggestion should include why it would help
- **Offer alternatives**: Present options when multiple valid approaches exist

**When in doubt, ask.** Err on the side of asking questions rather than making assumptions.

## Slash Commands & Skills

**After every command invocation**, reflect on how it was actually used vs. documented:

1. **Compare intent vs usage**: Did the command serve its documented purpose, or was it adapted?
2. **Notice patterns**: When the user corrects your approach or redirects focus, that's a signal the command should evolve.
3. **Proactively propose updates**: Use AskUserQuestion to suggest command improvements.

**Evolution signals:**

- User provides external docs -> Add doc-fetching or reference to command
- User corrects your approach -> Update command to prevent future errors
- User asks for something the command should cover -> Expand scope
- User ignores sections -> Consider simplifying

## External Resources

When questions involve Claude Code, Agent SDK, or Claude API:

1. **Use the claude-code-guide subagent**:

   ```
   Agent(subagent_type="claude-code-guide", prompt="<specific question>")
   ```

2. **Fetch docs directly** for specific pages:
   - `WebFetch(url="https://docs.claude.com/en/agent-sdk/<topic>")`
   - `WebFetch(url="https://docs.claude.com/en/claude-code/<topic>")`

When the user provides documentation links, incorporate that knowledge into CLAUDE.md or relevant commands.

