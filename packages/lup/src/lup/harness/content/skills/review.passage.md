# Review: Trace Workflow Analysis

**Don't speculate — analyze.** Read the actual trace, the actual guidance, and the actual tool descriptions. Ground every observation in evidence from the trace or source code.

## Input

**Trace input**: {{ arguments }}

## Process

### 1. Resolve the trace

Determine what was provided and get the full trace content:

**Session ID** (e.g., `repl_20260228_123036`, `20260228_123036`):

```bash
uv run lup-devtools trace show <session_id>
uv run lup-devtools trace show <session_id> --tool-calls
```

Also read the `SessionResult` JSON from `notes/traces/<version>/sessions/<session_id>/` for metadata (duration, cost, token usage, tool metrics, outcome).

**Launch run** (a run directory's name under `notes/harness/<runtime>/`, left by a session launched through `lup-devtools harness`):

```bash
uv run lup-devtools trace events <run_id>
uv run lup-devtools trace events <run_id> --kind tool_call --kind tool_result
```

Its first lines say whether the transcript's hash chain holds. Where it breaks, a line marks the record it breaks on: what follows is unverified text rather than evidence, so say so wherever a finding rests on it. `uv run lup-devtools trace verify` lists every launch with its chain, and `uv run lup-devtools tools metrics` gives the launched sessions' tool calls, errors and latency.

**File path** (contains `/` or ends in `.md`/`.json`):
- Read the file directly

**Raw pasted trace** (anything else):
- Work with what's pasted. Extract session metadata if present (timestamps, tool names, thinking blocks).

If you can't find the trace, use `uv run lup-devtools trace list` to show available sessions and ask the user which one to review.

### 2. Read the session's configuration

Before analyzing the trace, understand what the session had available. Its baseline is the harness it ran under:

- **Always-loaded guidance**: {{ harness_content_guidance_py }}, composed with the library modules this project takes and rendered into every tree — what the session was told before it read anything
- **Skills and agents**: `docs/harness.md` carries the composed roster and the tool servers the plugin declares; the declarations only this repository adds sit under {{ harness_content_skills_directory }}
- **Policy**: {{ harness_catalog_py }} declares what a shell command, fetch, or edit is allowed to do, and `docs/permissions.md` explains how a verdict is reached

A project whose sessions run an agent of its own declares this skill under its id with that agent's sources added here — its system prompt, its toolset registry, its tool policy — so read whatever this step names in the tree you are in.

This is the baseline for evaluating whether the session used its capabilities well.

### 3. Analyze the conversation flow

Walk through the trace chronologically and map the session's decision path:

**Task understanding:**
- Did the session correctly interpret the task?
- Did it plan before acting, or dive straight into tool calls?
- Were there thinking blocks that showed good or poor reasoning?

**Progress trajectory:**
- Did the conversation move toward the goal, or meander?
- Were there unnecessary loops (repeated tool calls with similar inputs)?
- Were there dead-end explorations that didn't contribute to the outcome?
- Did the session recover well when something failed or returned unexpected results?

**Decision quality:**
- At each major decision point, was the choice reasonable given available information?
- Did the session gather enough context before acting?
- Were there moments where it should have asked for clarification but didn't?

### 4. Audit tool usage

For each tool call in the trace:

**Selection**: Was this the right tool? Could a different available tool have been more effective?

**Inputs**: Were the arguments well-formed? Were searches specific enough? Were descriptions/specs complete?

**Results**: Did the session use the result effectively, or ignore useful information?

**Patterns to flag:**
- **Underused tools**: Tools that were available and would have helped but weren't called
- **Overused tools**: Repetitive calls that could have been batched or avoided
- **Poor tool descriptions**: If the session misused a tool, check whether the tool's description was unclear (read the actual `@lup_tool` decorator and the `Field(description=...)` on each input field in the source — that text is the session's only documentation for the field)
- **Missing tools**: Situations where the session worked around a gap that a new tool or command could fill
- **Policy friction**: Denials and approval questions the session met, and whether each named a recovery the session could take

### 5. Assess the reflection (if present)

If the session called `review` (the reflection tool):

- Was the self-assessment honest and accurate given the trace?
- Did the confidence score match the actual quality of work?
- Was the tool audit useful or perfunctory?
- Did the process reflection identify real friction?

### 6. Produce the report

Structure the report in four sections:

**Flow Summary**
A concise narrative of what happened in the session: task -> approach -> key decisions -> outcome. Include timestamps if available. This should read as a story, not a log dump.

**Tool Usage Audit**
A table or list of tool calls with assessment:
- Which tools were used well
- Which tools were misused or underused
- Specific tool calls that were wasteful or critical

**Workflow Assessment**
- Did the overall workflow function as designed?
- Where did friction occur?
- What went smoothly?

**Actionable Improvements**
Concrete, specific changes — not vague suggestions. Each improvement should reference:
- What evidence from the trace motivates it
- Which declaration to change — a guidance section, a skill, a tool group, the policy in {{ harness_catalog_py }} — or which library seam the change belongs upstream at
- What the change would be (new tool or command, description fix, guidance adjustment, workflow change)

Categorize improvements as:
- **Tool changes** — new tools or commands, better descriptions, schema fixes
- **Guidance changes** — prose that's missing, misleading, or unnecessary
- **Workflow changes** — process or architectural adjustments
- **Observability** — logging, metrics, or trace improvements needed

## Rules

- **Never guess.** Every observation must cite a specific trace entry, tool call, or source code location.
- **Read the source.** Don't evaluate tool usage without reading the actual tool descriptions and schemas.
- **Compare to intent.** The composed guidance defines the intended behavior — compare actual behavior against it.
- **Focus on the general.** Per the Bitter Lesson: prefer improvements that add capabilities over improvements that add rules. A missing tool is almost always a better diagnosis than a missing guidance paragraph.
- **Diagnose before prescribing.** For each proposed improvement, answer: what data was the session missing, and where in the workflow did the wrong decision enter? Don't propose "add rule X to the guidance" — propose the structural change that makes the failure impossible. Don't copy examples from this trace into the guidance — derive the general principle and write fresh examples.
- **Be honest about quality.** If the session went well, say so. Not every review needs to find problems.
- **Quote the trace.** When citing evidence, quote the actual trace text (tool names, thinking excerpts, result fragments) — don't paraphrase.
