# AGENTS.md Template

This file exports portable sections from the upstream AGENTS.md as a scaffold for downstream projects. It contains conventions, workflow patterns, and coding standards that apply to any project using lup.

**How it's used:** `{{ init_skill }}` and `{{ install_skill }}` perform a **section-level merge** — they use the `<!-- section: ... -->` markers below to identify independent merge units, compare them against the target's existing AGENTS.md, add sections that are missing, and leave existing sections untouched. Placeholders like `<project>` are replaced with the actual project name.

---

<!-- section: AGENTS.md -->
# AGENTS.md

This file provides guidance to Codex — and any agent that reads `AGENTS.md` — when working with code in this repository.

**Note:** Modifying `AGENTS.md` means modifying the repository-root `AGENTS.md` (this file).


<!-- passage: agent-vocabulary -->
- **Codex** = the meta-agent (the Codex CLI) that modifies the codebase, runs commands, and manages the development workflow

<!-- passage: naming -->
"Lup" is the framework's name for the inner agent, not a project-specific term. Use "Codex" when referring to the outer development agent and "Lup" when referring to the inner SDK agent, regardless of the project's package name.
<!-- passage: regeneration-pointer -->
`lup-devtools harness codex` regenerates the artifacts and installs the digest-verified plugin copy
<!-- passage: editing-style -->
## Editing Style

**Prefer small, atomic edits.** The PreToolUse hook decodes `apply_patch`'s complete command into before/after documents and applies the canonical edit policy. Safe changes with up to three real added lines are automatically allowed; protected paths, anti-patterns, marker changes, and full-file writes keep their guardrails.

- Split large changes into multiple small patches, one logical change each
- Separate concerns -- move imports in one patch, change logic in another
- Rename identifiers exhaustively and run `uv run pyright` to verify nothing dangles


<!-- passage: pyright-diagnostics -->
## Pyright Diagnostics

Nothing type-checks an edit for you here. Codex names the files it changed inside the patch envelope, and decoding one validates its context against the document on disk -- which is already the rewritten version by the time an edit could be observed, so there is no reading of what changed to check. Run `uv run pyright` after every substantive change and act on what it reports.

The `codeintel` tools do answer definitions, usages, and types, resolving imports and aliases as the checker does. Prefer them over word-boundary searches, and confirm a guess against them rather than acting on it. A relative path resolves against the checkout being edited, which the hook publishes on every patch; pass an absolute path when you mean a file somewhere else.


<!-- passage: launching -->
`lup-devtools harness codex` regenerates and verifies the Codex artifacts,
installs an immutable content-addressed copy of the plugin after a digest check, and
launches the Codex CLI in a persistent per-worktree home derived, at every
launch, from the selected account's authentication and settings — the account
home of the profile `~/.config/lup/config.toml` selects, else `~/.codex` — and
returns what a session changed in them to that account when it closes. The
home is kept in lup's state, `~/.local/state/lup/homes/` (or under
`$XDG_STATE_HOME`), never in the checkout, since it holds a copy of the login.
`lup-devtools dev usage codex` reports this backend's usage and
`lup-devtools dev usage claude` the other's; profiles, one name per account on
both runtimes, are managed with `lup-devtools harness profile`.
`--codex-home` or an inherited `CODEX_HOME` selects an explicit home instead.

Each repo names its plugin **marketplace** after the project — the plugin entry stays `lup`, so `{{ skill_pattern }}` is identical everywhere. Codex resolves the marketplace from the repository's `.agents/plugins/marketplace.json` and installs the plugin into its own cache, verifying the digest before every launch; `lup-devtools dev plugin name` (run by `{{ init_skill }}` and `{{ install_skill }}`) wires the per-project name.


<!-- passage: settings -->
## Settings & Configuration

Project Codex configuration is the generated `.codex/config.toml`, loaded only for a trusted project. Personal sandbox and approval defaults belong in `~/.codex/config.toml`; `sandbox_mode = "workspace-write"` with `approval_policy = "on-request"` is the low-friction guarded default. Never edit the generated project file.

Prefix-safe outside shell allows from the canonical policy are generated as project-local rules in `.codex/rules/lup.rules`. A matching native `allow` approves the command without choosing its sandbox placement. When the semantic policy places a command outside, set `sandbox_permissions = "require_escalated"` on the first call; the native rule removes the prompt and the PreToolUse hook still blocks unsafe variants. Commands whose safety depends on flags, paths, shell structure, or runtime content stay under the sandbox and approval flow.

---

<!-- section: Process & Communication -->
# Process & Communication

## Asking Questions

**Ask questions as explicit, numbered options** rather than burying them in prose. This applies to:

- Clarifying requirements or ambiguous instructions
- Offering choices between implementation approaches
- Confirming before destructive or irreversible actions
- Proposing changes or improvements
- Any situation where you need user input before proceeding

Even for open-ended questions, present concrete options plus an explicit free-form alternative, so the user can answer with a single short choice.

**When proposing changes:**

- **Propose, don't assume**: Ask before making changes
- **Show context**: Show relevant current state before proposing
- **Explain rationale**: Every suggestion should include why it would help
- **Offer alternatives**: Present options when multiple valid approaches exist

**When in doubt, ask.** Err on the side of asking questions rather than making assumptions.

## Skills

**After every skill invocation**, reflect on how it was actually used vs. documented:

1. **Compare intent vs usage**: Did the skill serve its documented purpose, or was it adapted?
2. **Notice patterns**: When the user corrects your approach or redirects focus, that's a signal the skill should evolve.
3. **Proactively propose updates**: Suggest skill improvements as explicit options.

**Evolution signals:**

- User provides external docs -> Add doc-fetching or reference to the skill
- User corrects your approach -> Update the skill to prevent future errors
- User asks for something the skill should cover -> Expand scope
- User ignores sections -> Consider simplifying

## External Resources

When questions involve the Claude Agent SDK or the Claude API used by the inner agent, fetch the docs directly:

- `https://docs.claude.com/en/agent-sdk/<topic>`
- `https://docs.claude.com/en/api/<topic>`

When the user provides documentation links, incorporate that knowledge into AGENTS.md or relevant skills.

