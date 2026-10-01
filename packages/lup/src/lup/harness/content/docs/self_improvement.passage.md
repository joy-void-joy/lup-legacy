# Self-Improvement Loop

How to diagnose agent failures and turn them into durable changes. For daily
development guidance, see [.claude/CLAUDE.md](../.claude/CLAUDE.md); for the
architectural pattern catalog, see [docs/orchestration.md](orchestration.md).

The two principles that govern every improvement below — **The Bitter Lesson**
(give the agent more tools and capabilities, not more rules) and **Tool Design
Philosophy** (the tool description is the contract) — are written out in
`.claude/plugins/lup/TEMPLATE_CLAUDE.md`.


<!-- passage: diagnosing-failures -->
## Diagnosing Failures

When the agent fails, the instinct is to patch the prompt. Resist it. Instead, trace the failure through the pipeline:

1. **What data did the agent have?** Read the trace. What tools did it call? What did they return? Was the information sufficient for a correct decision?
2. **Where in the workflow did the wrong decision enter?** Find the exact step — not the symptom, the entry point. A bad output is a symptom; a missing tool call or a misleading tool result is the cause.
3. **What structural change prevents it?** A new tool, a better tool description, a restructured pipeline step, richer data — these are durable fixes. A prompt rule is a patch that coexists with the failure.

| Do This | Not This |
|---|---|
| Trace the failure to a missing input or structural flaw | Add "NEVER do X" or "ALWAYS do Y" to the prompt |
| Formulate general principles with fresh examples | Copy examples from the specific trace that failed |
| Ask "what data was the agent missing?" and provide it | Add a numeric threshold ("if score > 15, then...") |
| Restructure the pipeline step where the error enters | Add a warning after the error-prone step |

**Examples that look the same but aren't:**

- Agent misclassifies commits → **Do:** Restructure the step to process files individually before grouping. **Don't:** Add "CRITICAL: Always check if a commit touches multiple concerns."
- Agent produces verbose output → **Do:** Constrain via output model or add a reviewer subagent. **Don't:** Add "Keep responses under 200 words."
- Agent ignores an available tool → **Do:** Improve the tool's description (what/when/why). **Don't:** Add "Remember to use X tool" to the prompt.

## Three Levels of Analysis

1. **Object Level** — The agent itself: tools, capabilities, behavior
2. **Meta Level** — The agent's self-tracking: what it monitors about itself
3. **Meta-Meta Level** — The feedback loop process: scripts, analysis methods

## Running the Feedback Loop

1. **Collect feedback**: `uv run lup-devtools feedback collect`
2. **Read traces deeply**: Read 5-10 sessions in detail — don't skip to aggregates
3. **Extract patterns**: Tool failures, capability requests, reasoning quality
4. **Implement changes**: Fix tools → Build requested capabilities → Simplify prompts
5. **Update documentation**: guidance should evolve with the agent
6. **Commit the session data**: `uv run lup-devtools feedback commit` turns each session under `notes/traces/` into one `data(sessions):` commit — the commit skill leaves that data alone

## What to Track Per Session

- **Sessions**: `notes/traces/<version>/sessions/<session_id>/`
- **Outputs**: `notes/traces/<version>/outputs/<task_id>/`
- **Traces**: `notes/traces/<version>/logs/<session_id>/`
- **Metrics**: Tool calls, timing, errors via metrics tracking — on a session result for a session opened in process, and for launched sessions in the snapshot each tool server writes under `notes/traces/<version>/sessions/harness/<name>/metrics/`, read with `uv run lup-devtools tools metrics`
- **Launch transcripts**: `notes/harness/<runtime>/<run>/observable.jsonl`, hash-chained; `uv run lup-devtools trace events <run>` reads one with its chain checked first, and `trace verify` checks every launch's

## Anti-Patterns

- Adding rules the agent can't act on (no access to required data)
- Adding "CRITICAL: Never do X" warnings instead of restructuring the workflow so X has no entry point
- Copying examples from a specific trace into the prompt instead of deriving general principles and writing fresh examples
- Adding numeric thresholds or absolute rules ("if more than N, do X") — these are brittle and don't survive domain shifts
- Patching for one observed symptom instead of tracing the failure through the pipeline to find the structural cause
- Listing tools by name in the system prompt (two sources of truth that drift apart)
- Skipping trace analysis to jump to aggregate statistics
- Over-engineering initial implementations
- Making changes in the application's `environment/` package, which carries inputs in and outputs out, when its `agent/` package, which decides, is the right place

**Validation questions for proposed changes:**

1. Does this add a capability or just a rule?
2. Would this help if the domain changed completely?
3. Are we changing the right level (object/meta/meta-meta)?
4. What data would we need to validate this change worked?
