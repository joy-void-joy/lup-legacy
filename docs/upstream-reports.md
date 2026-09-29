<!-- Generated from lup.harness.content.docs.upstream_reports by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Upstream reports

Defects this project measured in components it does not own, each with the evidence that was actually run and the command that files it.

Nothing here files anything. Publishing under an account belongs to whoever owns the account, so a report stays *not filed* until a human runs the command and records the URL in the declaration at `packages/lup/src/lup/harness/content/docs/upstream_reports.py`.

## Worktree isolation refuses fifteen shell words in any argv position, including in read-only commands containing no git

Measured against **Claude Code 2.1.237**. Goes to `anthropics/claude-code`; currently **filed as https://github.com/anthropics/claude-code/issues/95611**.

```bash
uv run lup-devtools sync upstream worktree-token-wall | gh issue create --repo anthropics/claude-code --title 'Worktree isolation refuses fifteen shell words in any argv position, including in read-only commands containing no git' --body-file -
```

**What happens.** In a session isolated by `EnterWorktree`, a command is
refused whenever any of fifteen shell words appears as an argv element —
not only as `argv[0]`. The check is not gated on the command being a git
command, although every diagnostic in the family is phrased about git.

**Reproducer** (read-only, no git anywhere in it):

```
$ grep -c hash some_file.py
This session is isolated in the worktree …, but this command runs a string
through hash, which can't be verified to stay inside the worktree; run the
command directly instead. Refusing to run it — a worktree-isolated session's
git operations must target its own worktree.
```

`grep -c eval`, `rg complete src/` and `grep -rn enable .` fail identically.

Note the word must be its own argv element: `git log -S"ssh alias"` does
**not** reproduce, because quoting keeps `alias` from becoming one.

**The full set**, read out of the 2.1.237 binary rather than inferred:

```js
dNr = new Set(["eval","source",".","exec","nocorrect","fc","coproc","trap",
               "enable","mapfile","readarray","hash","bind","complete",
               "compgen","alias","let"])
qAv = new Set(["exec","nocorrect"])
A1p = new Set([...dNr].filter((e) => !qAv.has(e)))
```

So fifteen words match, of which several are ordinary English that appears in
argument position constantly: `hash`, `let`, `complete`, `enable`, `bind`,
`trap`, `source`. Searching a codebase for any of them is refused.

**The fix you already wrote, twice.**

First, `.` is in the same set and is index-gated to `argv[0]`:

```js
let n = e.find((o, i) => { let s = Hae.basename(o).toLowerCase();
                           return s === "." ? i === 0 : A1p.has(s) });
if (n !== void 0) {
  if (e.filter((i) => i !== n).length > 0)
    return `runs a string through ${Hae.basename(n)}, which can't be verified `
         + `to stay inside the worktree; run the command directly instead`
}
```

`.` was special-cased precisely because it appears in argument position
constantly. So do the other fourteen.

Second, and more tellingly: the two sibling checks in the very same function
**are** gated on git, and this one is not.

```js
ZLa = /^git(?:\.exe|\.real|-[a-z][\w-]*)?$/i
let t = e.some((o) => ZLa.test(Hae.basename(o)));

if (t && e.some((o) => WAv.has(...)))       // xargs / parallel — git-gated
if (t && r("find") && e.some((o) => VAv.has(o)))  // find -execdir — git-gated
if (n !== void 0)                            // this one — not gated
```

Both neighbours require a git-looking argv element before they refuse.
Applying either existing pattern — the `t &&` gate, or the `.` index gate —
would close this.

**No escape hatch reaches it.** Our project's approval marker is a leading
comment line on the command; the refusal is byte-identical with it present.

**It arms on the tool, not on the directory.** A session *launched* already
rooted in a worktree is not isolated and runs all of these; only calling
`EnterWorktree` turns the check on. That asymmetry is the workaround we have
adopted, and it is also why the check is easy to miss in testing.

**Impact.** A project whose workflow directs all work into worktrees loses
every command containing one of these words for the whole session, including
read-only ones.

---

**Related, same family.** `bwrap` hard-fails when a path in its mount list has
vanished, rather than skipping it. Two instances:

```
bwrap: Can't get type of source /tmp/claude-1000/claude-settings-<hash>.json: No such file or directory
bwrap: Can't get type of source …/lup.git/worktrees/<name>/config.worktree: No such file or directory
```

The second is a stale git worktree's config file, so the mount list is derived
from git state that outlives the worktree it describes.

A third, on a host with `max_user_namespaces` unbounded,
`unprivileged_userns_clone=1`, no AppArmor restriction, and the shell already
inside a user namespace:

```
apply-seccomp: unshare(CLONE_NEWUSER): Invalid argument
```

All three share the property that makes them expensive: **the command does not
run, prints the failure on its own line, and returns what reads exactly like a
successful run with no output.** A `grep` that matched nothing and a `grep`
that never executed are indistinguishable to the caller.

## A subagent's unified-exec session outlives its thread, and rollout items are still recorded against the dead thread at session end

Measured against **Codex 0.158.0**. Goes to `openai/codex`; currently **not filed**.

```bash
uv run lup-devtools sync upstream subagent-session-outlives-thread | gh issue create --repo openai/codex --title "A subagent's unified-exec session outlives its thread, and rollout items are still recorded against the dead thread at session end" --body-file -
```

**What happens.** A PTY session a subagent opened with `exec_command`
keeps running after that subagent's turn has completed and its report has
been delivered, until the parent session ends. Then the session is torn down,
its completion is recorded against the subagent's thread, and `codex` prints:

```
ERROR codex_core::session: failed to record rollout items: thread <id> not found
```

The id is the subagent's `agent_id`, as `SubagentStart` and `SubagentStop`
spell it.

**Reproduced three times on 0.158.0**, model `gpt-6-luna` at `low` reasoning
effort, `permission_mode: bypassPermissions`, launched non-interactively with
`codex exec --dangerously-bypass-approvals-and-sandbox`. Every hook event was
registered to a recorder that appends the payload, the wall clock it arrived
at, and the live processes carrying the probe's command, read from `/proc` at
that moment. The same line appeared twice on 0.155.1 with `gpt-6-astra`.
Offsets below are seconds from the launch of `codex exec`; the probe's working
directory is shortened to `…`.

**Run A** — session `01a0e942-a71b-78d0-81c2-a81041053e57`, subagent
`01a0e942-ca47-7041-bc93-049f17af9c28`:

| offset | event | what |
| --- | --- | --- |
| +12.17s | `PreToolUse` (subagent) | `Bash` `sleep 517`, **no `&`**, `tool_use_id` `exec-faf422d7-9004-4974-84b6-225593e7e0e0` |
| +14.67s | `SubagentStop` | the subagent reports `ARMED`; `sleep 517` alive as pid 1777597 |
| +14.71s | `PostToolUse` (parent) | `collaborationwait_agent` → `{"message":"Wait completed.","timed_out":false}` |
| +41.12s | `PostToolUse` (parent) | `ps` → `1777597      28 sleep 517` |
| +47.35s | `Stop` | session ends; pid 1777597 still alive, 35.17s old |

The subagent's call, as its rollout records it, went out through the
code-mode host as `tools.exec_command({cmd:"sleep 517", yield_time_ms:1000,
tty:true, max_output_tokens:100})` and came back after one second holding a
session rather than an exit:

```
{"chunk_id":"429927","wall_time_seconds":1.003066467,"session_id":58407,"original_token_count":0,"output":""}
```

`etimes` is 28 and `41.12 − 12.17 = 28.95`, so this is one process alive
continuously, seen **26.5 seconds after the subagent reported** and still
alive 32.7 seconds after it, at `Stop`. The command carries no `&`, so it is
not an orphan reparented to init: the PTY holds it.

Immediately after `Stop`, on the terminal:

```
2026-09-28T18:25:01.844403Z ERROR codex_core::session: failed to record rollout items: thread 01a0e942-ca47-7041-bc93-049f17af9c28 not found
```

The subagent's rollout, whose last line had been its own `task_complete` at
`18:24:29.151Z`, gained one more line **2.4 ms before** that error: the
session's end, `status: failed`, `exit_code: -1`, under the `process_id` the
call had been handed as its `session_id`:

```
{"timestamp":"2026-09-28T18:25:01.842Z","ordinal":25,"type":"event_msg","payload":{"type":"item_completed","thread_id":"01a0e942-ca47-7041-bc93-049f17af9c28","turn_id":"01a0e942-ca66-7903-929a-b0daf5411116","item":{"type":"CommandExecution","id":"exec-faf422d7-9004-4974-84b6-225593e7e0e0","process_id":"58407","command":["/bin/bash","-lc","sleep 517"],"cwd":"file:///…","parsed_cmd":[{"type":"unknown","cmd":"sleep 517"}],"source":"unified_exec_startup","status":"failed","stdout":"","stderr":"","aggregated_output":"","exit_code":-1,"duration":{"secs":35,"nanos":28863081},"formatted_output":""},"started_at_ms":1790619866813,"completed_at_ms":1790619901842}}
```

**Run B** — session `01a0e945-8271-77f2-b02f-d4580ee98bcb`, subagent
`01a0e945-ac51-77a3-9b23-9a5fd3c80624`, written to force output onto the
session *after* the report:

| offset | event | what |
| --- | --- | --- |
| +15.05s | `PreToolUse` (subagent) | `tail -f …/wake.txt` on an empty file, no `&`; handed `session_id` 69981 |
| +17.83s | `SubagentStop` | the subagent reports `ARMED` |
| +33.93s | `PreToolUse` (parent) | `date +%s >> …/wake.txt` — output forced, 16.1s after the stop |
| +73.20s | `PostToolUse` (parent) | `ps` → `1899775      58 tail -f …/wake.txt` |
| +78.72s | `Stop` | session ends; pid 1899775 still alive, 63.64s old |

The session survived **60.9 seconds past the subagent's stop**, 44.8 of them
after output was forced onto it. That output was recorded nowhere as it
arrived: it surfaces only in the teardown line, 3.3 ms before the same error.

```
{"timestamp":"2026-09-28T18:28:40.242Z","ordinal":25,"type":"event_msg","payload":{"type":"item_completed","thread_id":"01a0e945-ac51-77a3-9b23-9a5fd3c80624","turn_id":"01a0e945-aca6-78f0-a778-be159f9eb698","item":{"type":"CommandExecution","id":"exec-3d4e68e7-565d-462d-9855-19374ec3a392","process_id":"69981","command":["/bin/bash","-lc","tail -f …/wake.txt"],"cwd":"file:///…","parsed_cmd":[{"type":"unknown","cmd":"tail -f …/wake.txt"}],"source":"unified_exec_startup","status":"failed","stdout":"1790620075\r\n","stderr":"","aggregated_output":"1790620075\r\n","exit_code":-1,"duration":{"secs":63,"nanos":505134667},"formatted_output":"1790620075\r\n"},"started_at_ms":1790620056737,"completed_at_ms":1790620120242}}
```

```
2026-09-28T18:28:40.245288Z ERROR codex_core::session: failed to record rollout items: thread 01a0e945-ac51-77a3-9b23-9a5fd3c80624 not found
```

**Run C** repeats A — session `01a0e947-cad0-7f73-a6c4-48f8957d7191`,
subagent `01a0e947-f0bb-7752-98fa-99a14793c18a`, `session_id` 35920, `ps` →
`1976138      29 sleep 517`, alive 33.21s old at `Stop`. Teardown line at
`18:30:38.016Z`, `exit_code: -1`, then:

```
2026-09-28T18:30:38.017592Z ERROR codex_core::session: failed to record rollout items: thread 01a0e947-f0bb-7752-98fa-99a14793c18a not found
```

In all three runs nothing carrying the command was left once `codex exec`
returned, so the leak is bounded by the parent session.

**Why the teardown looks like the producer.** In every run the error follows
the leaked session's own `item_completed` by 1.6 to 3.3 ms and names the
thread that item carries, and nothing else is written to that thread after
its `task_complete`. The line does reach the rollout file, so which write the
error reports failing — a second one, or one addressed through a registry the
subagent has already left — cannot be told from outside.

**Possibly two defects rather than one.**

1. A subagent's unified-exec sessions are not closed when its turn
   completes; they live until the parent session ends and are killed then.
   `exec_command` describes itself as "Runs a command in a PTY, returning
   output or a session ID for ongoing interaction" and `write_stdin` as
   "Writes characters to an existing unified exec session and returns recent
   output"; neither is scoped to a thread's lifetime, and no tool closes a
   session outright.
2. That teardown is still addressed to the ended thread, where recording
   fails with the error above rather than being dropped or re-homed.

**What it costs a caller.** Every session in which a subagent left a PTY open
ends with what reads as an internal failure in an otherwise successful run. It
does not resume the subagent: twelve hook records follow the single
`SubagentStop` in run B and none carries the subagent's `agent_id`, and the
output forced onto the session reaches nobody until the teardown line carries
it.

**What would help you chase it.** The three subagent rollouts, at the
`agent_transcript_path` each `SubagentStop` carried; the teardown line quoted
above is each one's last. They sit in the operator's home directory and are
attached by whoever files this.

## ListAgents gives two live sessions the same bracketed ref, in the line that tells each how it is addressed

Measured against **Claude Code 2.1.278**. Goes to `anthropics/claude-code`; currently **filed as https://github.com/anthropics/claude-code/issues/95610**.

```bash
uv run lup-devtools sync upstream list-agents-ref-collision | gh issue create --repo anthropics/claude-code --title 'ListAgents gives two live sessions the same bracketed ref, in the line that tells each how it is addressed' --body-file -
```

**What happens.** `ListAgents` labels each row with a short bracketed
ref — `name [36024e]` — presented as what disambiguates two rows sharing a
name. Two live sessions in different containers, working in the same project
directory, are labelled with the **same** ref, including in the self-describing
first line each is shown for itself.

**Measured on 2.1.278.**

- Two background sessions in *one* container, distinct names: `refprobe-a
  [ce3140]` and `refprobe-b [0039df]` — distinct, as intended.
- A session's self-line carries its own ref: `refprobe-a` running `ListAgents`
  names itself `[ce3140]`.
- Two sessions in *different* containers, same project directory, each named
  itself `[36024e]`.

**Addressing fails closed, which is the good news.** Four sends through
`SendMessage`:

| `to:` | result |
| --- | --- |
| `refprobe-b [ce3140]` (name B, ref A) | refused — `No agent named 'refprobe-b [ce3140]' is reachable. Did you mean: refprobe-b, refprobe-a?` |
| `ce3140` (bare ref) | refused — `No agent named 'ce3140' is reachable.` |
| `refprobe-a [ce3140]` (matched pair) | delivered |
| `refprobe-a` (bare name) | delivered |

A mismatched pair is rejected rather than silently resolved, and a bare ref
resolves to nothing at all. So no message can be misdelivered by this: it is a
listing defect, not a routing one.

**Inference, not measurement.** In both colliding containers `claude` runs as
pid 7 — `lup-entrypoint` at pid 1, `claude` at 7 — and the two sessions share
a project directory. If the ref derives from the pid, perhaps with the project
path, then every contained session that believes itself pid 7 in the same
project collides. We cannot see the derivation; this is the hypothesis that
fits what we can see, and the distinct refs within one container are the
control.

**Why it is worth fixing although sends fail closed.** The collision shows up
in the self-description line, which is exactly the line a reader acts on: it
states the name other sessions use to reach this one. Two such lines side by
side read as one session listed twice.

## A PreToolUse hook's ask reason is shown in the Bash permission prompt and dropped from the Write and Edit prompts

Measured against **Claude Code 2.1.283**. Goes to `anthropics/claude-code`; currently **not filed**.

```bash
uv run lup-devtools sync upstream hook-reason-edit-prompt | gh issue create --repo anthropics/claude-code --title "A PreToolUse hook's ask reason is shown in the Bash permission prompt and dropped from the Write and Edit prompts" --body-file -
```

**Measured on** 2.1.283 (`@anthropic-ai/claude-code-linux-x64`), Linux x64,
in an interactive session on a pseudo-terminal (140×60,
`TERM=xterm-256color`), default permission mode (`⏸ manual mode on`), with
`--model haiku`; the model is irrelevant to the defect. First observed on
2.1.237; no changelog entry between the two mentions it.

**What happens.** A `PreToolUse` hook that returns `permissionDecision: "ask"`
with a `permissionDecisionReason` gets its prompt in every case: for `Bash`,
`Write` and `Edit` alike. For **`Bash`** the prompt renders the reason. For
**`Write`** and **`Edit`** the prompt shows the path, the content or diff, and
the options, and the reason appears nowhere: not in the dialog, not in the
byte stream written to the terminal.

The hooks reference promises otherwise, without any exception by tool:

> `permissionDecisionReason` | For `"allow"` and `"ask"`, shown to the user but not Claude. For `"deny"`, shown to Claude. For `"defer"`, ignored
> — https://code.claude.com/docs/en/hooks, *PreToolUse decision control*

So a policy hook can stop a file write for a reason the person answering the
prompt is never shown, and they are asked to approve without knowing what was
found.

**Reproduction.** A project with this `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Write|Edit",
        "hooks": [
          { "type": "command", "command": "bash \"$CLAUDE_PROJECT_DIR/.claude/hooks/ask.sh\"" }
        ]
      }
    ]
  }
}
```

and this `.claude/hooks/ask.sh`, which answers every matched call with the
same four fields, each carrying a marker naming the tool:

```bash
#!/usr/bin/env bash
set -euo pipefail
tool=$(jq -r '.tool_name')
jq -cn --arg t "$tool" '{
  systemMessage: ("SYSMSG-" + $t + ": the probe hook system message"),
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "ask",
    permissionDecisionReason: ("REASON-" + $t + ": the probe hook asks because this reason must reach the person approving"),
    additionalContext: ("ADDCTX-" + $t + ": the probe hook additional context")
  }
}'
```

A `note.txt` containing `alpha`. Then, in an interactive `claude` session,
three prompts, answering **No** to each permission prompt:

1. `Run exactly this shell command with the Bash tool and nothing else: echo vendor-probe`
2. `Use the Write tool to create the file created.txt in the current directory, containing the single line: hello. Do nothing else.`
3. `Read note.txt, then use the Edit tool to replace the word alpha with beta in it. Do nothing else.`

**Expected:** each prompt carries its `REASON-<tool>` line, as the reference
says. **Actual:** only the `Bash` prompt does.

**What the prompts show.** Rendered screens, captured through a VT100
emulator at the moment each prompt was up, from the tool-call line down.

`Bash` — the reason is there:

```
  Running the requested echo command
  ⎿  $ echo vendor-probe

────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 Bash command

   echo vendor-probe
   Run the requested echo command

 │ Hook PreToolUse:Bash requires confirmation for this command:
 │ REASON-Bash: the probe hook asks because this reason must reach the person approving
 settings.json to update hooks

 Do you want to proceed?
 ❯ 1. Yes
   2. No

 Esc to cancel · Tab to amend
```

`Write` — no reason, no hook attribution:

```
● Write(created.txt)

────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 Create file
 created.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
  1 hello
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to create created.txt?
 ❯ 1. Yes
   2. Yes, and switch to accept edits (auto-approve file edits and common file commands) for this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
```

`Edit` — the same:

```
● Update(note.txt)

────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 Edit file
 note.txt
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 1 -alpha
 1 +beta
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Do you want to make this edit to note.txt?
 ❯ 1. Yes
   2. Yes, and switch to accept edits (auto-approve file edits and common file commands) for this session (shift+tab)
   3. No

 Esc to cancel · Tab to amend
```

The Write and Edit dialogs were identical, line for line, in all six
sessions. The screen is not hiding the reason off the edge: in the raw byte
stream written to the terminal, with every control sequence stripped,
`REASON-Bash` occurs once per session and `REASON-Write` and `REASON-Edit`
occur zero times.

**Evidence that the reason was supplied.** The hook also logged each call with
what it returned. From the session that answered No to all three, projected to
four fields:

```
{"at":"2026-09-28T17:52:07+02:00","event":"PreToolUse","tool":"Bash","reason":"REASON-Bash: the probe hook asks because this reason must reach the person approving"}
{"at":"2026-09-28T17:52:18+02:00","event":"PreToolUse","tool":"Write","reason":"REASON-Write: the probe hook asks because this reason must reach the person approving"}
{"at":"2026-09-28T17:52:30+02:00","event":"PreToolUse","tool":"Edit","reason":"REASON-Edit: the probe hook asks because this reason must reach the person approving"}
```

The decision was honoured for all three: each prompt appeared because the hook
asked. Only the reason attached to that decision was lost, and only for two of
them.

**Where it is dropped**, read out of the 2.1.283 bundle; identifiers are that
build's minified names. The reason block is one component, `Tb`, which turns
a `decisionReason` into text. For a hook it produces exactly the two lines the
Bash prompt shows:

```js
case"hook":{let p=t.reason?`:
${c(t.reason)}`:".",a=t.hookSource?` ${pe.dim(`[${c(t.hookSource)}]`)}`:"";
return{reasonString:`Hook ${pe.bold(c(t.hookName))} requires confirmation for this ${u}${p}${a}`,
       configString:`${_r(t.hookSource)} to update hooks`}}
```

The command dialog renders it unconditionally:

```js
r(s,{flexDirection:"column",children:[e(Tb,{permissionResult:h.permissionResult,toolType:"command"}), …
```

The file dialog (the one titled `Create file` / `Edit file`, with the
`Save file to continue…` and symlink-target lines) renders it only when a
denial-limit fallback is present:

```js
r(xi,{title:h.showingDiffInIDE?…:h.title,subtitle:h.subtitle,…,children:[ho,
  h.permissionResult.denialLimitFallback!==void 0&&e(s,{paddingX:1,children:e(Tb,{permissionResult:h.permissionResult,toolType:"edit"})}),
  h.showingDiffInIDE?…
```

and `denialLimitFallback` is created in one place, where the classifier's
denial limit falls back to prompting; the bundle's three other occurrences copy
or update an existing one:

```js
t(`Classifier denial limit exceeded, falling back to prompting: ${Ee}`,{level:"warn"}) …
let Ie={type:"classifier",classifier:xe,reason:`${Ee}..Latest blocked action: ${n}`},Oe=KNe(g,Ie);
return{...g,...ge&&!M&&ye&&{denialLimitFallback:fBt(Ie,…)},decisionReason:Oe}
```

So a hook's `decisionReason` reaches the file dialog and is never rendered
there. Rendering `Tb` whenever `decisionReason` is a hook's, as the command
dialog does, would close this.

**What happens to `systemMessage` and `additionalContext`.** The same hook
output carries both. Where they went, per tool and answer (screen: the
rendered terminal and its raw byte stream; transcript: the session's
`.jsonl`):

| Tool | Answer | `systemMessage` on screen | `hook_*` attachments in the transcript |
| --- | --- | --- | --- |
| Bash | Yes | after the call, under the tool entry | `hook_success`, `hook_system_message`, `hook_additional_context` |
| Bash | No | never written to the terminal | none |
| Write | No | never written to the terminal | none |
| Edit | No | never written to the terminal | none |

The approved Bash call renders:

```
  Ran 1 shell command
  ⎿  PreToolUse:Bash says: SYSMSG-Bash: the probe hook system message

● The command executed successfully and output vendor-probe.
```

Two consequences:

- `systemMessage` is not a way around the missing reason. It renders only once
  the call has been approved, attached to the tool entry, and not at all when
  it is refused. On 2.1.237 an approved `Write` rendered it the same way, as
  `PreToolUse:Write says: …` above `Wrote 1 line to …`.
- On a refusal, `additionalContext` is discarded for every tool, although the
  refused call does get a tool result, the one Claude receives:
  `The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if it was a file edit, the new_string was NOT written to the file). STOP what you are doing and wait for the user to tell you how to proceed.`
  The reference describes `additionalContext` as a "String added to Claude's
  context alongside the tool result" and does not say a refusal drops it.
  Unlike the missing reason, this depends on the answer and not on the tool.

**Other channels, measured.**

- `PermissionRequest` fires for all three tools, from a project settings file
  and from a plugin loaded with `--plugin-dir` alike, in the same second as
  `PreToolUse`. Its output is a decision (allow or deny); it has no field that
  displays text.
- `Notification` on the `permission_prompt` matcher fires for all three, six
  seconds after the prompt appears (`"message":"Claude needs your permission"`),
  from both registrations. The reference says it discards `systemMessage`.
- `terminalSequence` returned by the same `PreToolUse` hook is written before
  the dialog: in the byte stream, the Write call's `ESC ]2;TITLE-Write: …`
  window title sits at offset 11210 and the dialog's `Create file` at 11751.
  Of everything measured here, that makes a window title or desktop
  notification the only way a hook's words reach the person before they answer
  a file prompt. It is outside the prompt, and it depends on the terminal.

**The documented source label is missing too.** The reference says an `ask`
prompt "includes a label identifying where the hook came from: `[settings]`
for a hook from any settings file…". The Bash prompt above has no `[settings]`
label for a hook registered in `.claude/settings.json`: `[settings]` occurs
zero times in all six sessions' byte streams. The builder above appends the
label only when `t.hookSource` is set, so its absence means `hookSource` was
empty when the dialog rendered, although the hook runner computes one for
every settings-file hook:

```js
Vt=Et?"pluginName"in _t?`plugin:${_t.pluginName}`:"plugin":bt?"skillName"in _t?`skill:${_t.skillName}`:"skill":"settings"
```

That is an inference from the symptom; the path between the runner and the
dialog was not traced. The hint line under the reason,
`settings.json to update hooks`, reads as a fragment; it is `_r`'s value for
any source that is neither a plugin nor a skill, empty included.

**Asks, in order of preference.**

1. **Render the hook's reason in the file-edit dialog**, as the command dialog
   already does and as the hooks reference already promises. The data arrives;
   one dialog gates it behind an auto-mode-only condition.
2. **Show the documented `[settings]` label** for a settings-file hook's `ask`.
3. **Say what a refusal does to a hook's `systemMessage` and
   `additionalContext`**, or deliver them: today both disappear when the
   person answers No, which the reference does not mention.

**Why this matters beyond one project.** The permission prompt is where a
person decides. A hook that can gate a file write but cannot explain itself at
that moment pushes every policy toward `deny`, whose reason at least reaches
Claude, where an `ask` would have let a person judge. That is the wrong
incentive for anyone writing a permission policy.

The six sessions' screen captures, raw terminal streams, hook logs and
transcript extracts are attached by whoever files this.
