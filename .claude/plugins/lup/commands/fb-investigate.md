---
description: Deep trace reading and error classification for selected sessions
allowed-tools: Bash(uv run lup-devtools:*), Read, Agent, AskUserQuestion
argument-hint: <session_id1> [session_id2 ...]
---

# Investigate: Trace Deep-Dive

Build first-hand understanding of what happened in each target session.

## Breadth vs. Depth

For the deep pass (the 5-10 selected target sessions), read the traces directly — first-hand reading is the point of this phase. When the session list is larger, or you need cross-cutting patterns over many sessions, delegate the bulk reading to the `trace-explorer` agent instead of reading every trace in this conversation: it reads traces in its own context window and returns a compact pattern report.

Delegate with Agent(subagent_type="lup:trace-explorer", name="trace_patterns", prompt="Analyze traces for sessions <ids>; report tool failures, capability gaps, reasoning quality")

Use its report to pick which sessions deserve the direct deep read below.

## Per-Session Investigation

One delegation per session, launched in parallel when investigating several:

Delegate with Agent(subagent_type="lup:trace-explorer", name="session_<session_id>", prompt="Investigate session <session_id> following the per-session steps below. Report: tool call inventory, errors with quoted output, workflow assessment, outcome classification, counterfactuals.")

Before presenting findings, spot-check each report against the trace itself (`uv run lup-devtools trace show <session_id>`) — quoted errors must appear verbatim in the trace, not paraphrased from a truncated read.

The per-session steps (for the subagent, or for investigating a single session directly):

### 1. Read the trace

```bash
uv run lup-devtools trace show <session_id>
```

Read every tool call, error, and reasoning step. Don't skim.

For a focused view of tool interactions:

```bash
uv run lup-devtools trace show <session_id> --tool-calls
```

### 2. Tool use audit

- **Tool call inventory**: List every tool call — what the agent tried to learn, whether it succeeded, whether the result was useful.
- **Tool errors**: For each failure — what happened (quote the error), why it failed (read the tool source in `src/lup_template/agent/tools/`), was recovery reasonable.
- **Subtle bugs**: Cases where a tool *succeeded* but returned misleading or incomplete data.
- **Missing tool calls**: Tools the agent *should* have called but didn't. Check available tools in `src/lup_template/agent/tools/`.

### 3. Workflow assessment

- **Information gathering**: Enough evidence? Diverse sources? Or jumped to conclusions?
- **Structured reasoning**: Decomposed the problem? Weighed uncertainties?
- **Self-correction**: Updated views on new evidence? Flagged its own uncertainty?
- **Efficiency**: Wasted tool calls? Proportional effort?

### 4. Pipeline health

System-level problems separate from agent reasoning:
- MCP connection issues (tools timing out, empty results)
- Token/context pressure (reasoning truncated, limits hit)
- Prompt issues (agent confused by instructions)
- Hook behavior (permission hooks blocking valid operations)

Read relevant source code when you spot a pipeline issue.

### 5. Classify the outcome

| Type | Description |
|------|-------------|
| Good outcome | Correct approach, reasonable result |
| Missing capability | Agent lacked a tool or data source it needed |
| Tool failure | Available tool broke or returned bad data |
| Reasoning error | Agent had the data but drew wrong conclusions |
| Scope misunderstanding | Misinterpreted the task requirements |
| Efficiency issue | Got there but wasted significant effort |

For each issue, build a **counterfactual**: what specific tool, data source, or reasoning step would have changed the outcome?

## Gate

Show:
1. Per-session summary table: Session ID | Task | Outcome Type | Key Finding
2. Cross-session patterns (if multiple sessions)
3. Top 2-3 counterfactuals

Then Ask the user with the AskUserQuestion tool, offering concrete options plus a free-text choice: whether to proceed to analysis, dig deeper on particular sessions, or skip ahead to implementation
