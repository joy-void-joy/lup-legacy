---
description: Aggregate tool health, capability gaps, and reasoning patterns across sessions
allowed-tools: Bash(uv run lup-devtools:*), Read, Agent, AskUserQuestion
---

# Analyze: Tool Health & Capability Gaps

Aggregate findings across sessions to identify systemic patterns.

## Process

### 1. Structured analysis report

```bash
uv run lup-devtools feedback analyze
```

This produces a JSON report with three sections:
- **tool_health**: per-tool call counts, error counts, error rate
- **error_patterns**: sessions with high error rates, grouped by error type
- **capability_gaps**: agent capability requests extracted from traces

Identify: which tools fail most? Are failures transient or systematic? What does the agent say it needs?

Trust capability requests — the agent knows what it lacks. Quote the agent's exact words so you can act on them.

### 2. The aggregates that report does not carry

`feedback analyze` answers what failed. Three siblings answer what it cost and
what moved, and every question below is measured against them rather than
recalled:

```bash
uv run lup-devtools feedback tools
uv run lup-devtools feedback costs
uv run lup-devtools feedback trends
```

`tools` aggregates usage, `costs` rolls up tokens and spend per backend, and
`trends` follows a metric over time. The first two together are what "is this
tool worth what it costs" is answered with — a tool with a low error rate can
still be the most expensive thing in the run, and `analyze` alone cannot say
so. `trends` is what distinguishes a change that moved a metric from one that
coincided with it, so read it before crediting a version with an improvement.

All three read session results, which only a session opened in process
writes. A session launched through `lup-devtools harness` leaves none, and
its tools' health is in the snapshots its tool servers wrote instead:

```bash
uv run lup-devtools tools metrics
```

### 3. Reasoning patterns

From investigation findings:
- Are there systematic reasoning errors, or just individual misjudgments?
- Does the agent consistently struggle with certain task types?
- Are expensive tools providing proportional value?

### 4. Version comparison (if relevant)

If comparing across versions, get code-level diffs: Delegate with Agent(subagent_type="lup:version-explorer", name="version_comparison", prompt="Compare vX.Y.Z and vA.B.C")

### 5. Summarize

From tool-health and capability-gap data:
- Which tools consistently fail? What's the root cause?
- What does the agent need but doesn't have?
- Are there reasoning quality patterns across sessions?
