**What this run is about:** {{ arguments }}

Nothing there means the run plans from what the tree already carries. Anything there is evidence in its own right: pass each concern through as `--admit "<their words>"` on the entry command below, which seeds a fresh run with those statements beside the tree's own notes. Never invent a `# lup:` note site to hold a statement and never paraphrase one — the words are what the planner reads, and what the concern stays traceable to afterwards.

Read the intake preview before starting one: it is the answer to "is this worth running, or worth cleaning out first", and reconstructing it by hand from the scan and the ownership manifests is a dozen calls that still ends in a guess.

Deferred notes — `# lup: defer: <text>` — are parked work, not open feedback, and the resolver entry excludes them from its inventory, so an editor can never be assigned one. That bare spelling is the default, and `defer[<gate>]: <text>` is reserved for a real, externally-checkable gate ("until the v2 API ships") and never restates that this code might change again. Triage them before launching the resolver: read each note against the current state of the repository — its gate where it stated one, its own text where it did not — and when the work reads as due, propose waking it to the user. A gate `dev check` resolves for itself has already been triaged: `defer[branch:<name>]` and `defer[gone:<path>]` fail the run they come true, so one still parked is one whose condition has not, and the reading to do is on the prose gates and the bare ones. Note that a branch gate wakes on the branch it names — read off the integration branch, so it fires there without that branch having merged anything — which means a woken one may name a note that is not in this checkout at all; the report says which ref to read it on. Waking is an explicit edit that removes the `defer` head so the note re-enters open feedback on the next run; anything still parked carries forward untouched, never re-litigated.

{{ resolver }}

## Relaying a parked question

The run parks rather than guessing, so every material question is a decision that belongs to the human. Never answer one yourself — and never merely transcribe one either. A prompt and a choice list, handed over as printed, reads as a decision with no stakes and cannot be judged.

**Measure before you relay.** The planner writes from notes, not from the tree, so every question's framing is a hypothesis you can check: read the code it concerns and measure what it asserts. A type probe shows whether the "redundant" overload is load-bearing or whether removing it widens every call site. A grep bounds how many sites a proposed rule would actually catch, and how many of those are correct by design. A count of the thing a note named as the worst case can demote it to a false positive outright. Measurement changes the answer often enough that relaying from the planner's prose alone is guessing with extra steps.

**Relay what it takes to judge cold.** For each option: what it means concretely, what it costs, what it collides with elsewhere in this same run, and which of the concern's acceptance criteria it satisfies or fails. Explain the underlying problem from scratch when the question is unfamiliar rather than building on context the human does not have, and put it in the terms they reason in rather than the planner's vocabulary.

**Give your own recommendation, marked as yours.** It is allowed to differ from the planner's, and it should when your investigation says so. When the option set mis-carves the problem, say that and offer the corrected option instead of defending the list — a wrong framing costs a whole extra round even when every detail in it is accurate. The choices are suggestions, not a menu: say so, and pass an answer in the user's own words whenever they give one.

**Relay the whole batch at once.** A run parks with all of its open questions together; asking them one at a time makes the human re-establish the same context for each.

**End every report with `status --line`.** A run outlasts the attention of whoever started it, and a reader returning to a terminal sees how long your turn took, never when it ended — so two reports an hour apart and two a minute apart read identically. Print what the flag prints and nothing around it: it is the first line of `status`, it is composed to carry exactly what needs a person, and a reader who wants the rest runs `status` without the flag. Restating any of it here would be a second copy to keep true.

## Watching a run, and what silence means

A run is built to be left alone, so "is it still going, or did it stop?" is the question you will ask most. Ask it with `uv run lup-devtools resolve status --run-id <id>`, which answers from the run directory alone: the phase, the concerns per status, **how many questions are waiting on you**, and the last journal event with its age. Liveness comes from the run's own lock rather than the process table, because under a sandbox `/proc` is PID-isolated — `ps` and `pgrep` list nothing outside the current shell, so a healthy run and a dead one look identical there.

**Do not watch a run by tailing its log.** Two things a log cannot tell you, and both are missed that way. A worker that queues a question blocks on it while its siblings keep working, so the run does not park and prints nothing — a question can wait on you indefinitely with the log silent. And a tail started mid-run begins at the end of the file, so every event before it is skipped without a trace.

**Immediately after the detached entry prints its run id, start exactly one watch over the run and stay on it.** {{ watch }}. The watch emits on every change and ends when the run parks or finishes, so that one invocation covers both "tell me when something moves" and "tell me when it is over". Do not reach for anything that only reports on exit, because a queued question does not stop the run. A question that arrives is printed whole, with the notes it was raised from and the concern's criteria, so you can start measuring it without fetching anything; `--line` narrows the status half to one line and leaves the question intact.

Read the verdict rather than the quiet. A held lock is the fact and the last-event age is context on top of it: a run can legitimately record nothing for tens of minutes while a planner works, and judging by silence has produced a confident wrong "it crashed" about a run that was mid-turn. A growing age against a held lock is the shape of a wedged run; silence on its own is not evidence of anything.

`resolve supervise` serves the same projection as a live page for a human to sit in front of, and takes answers. It is server-sent events to a browser, so it is the human's surface and not the one to reach for when what you need is a signal you can act on.

## Work discovered during a run

Admit work into the run that found it: hand the run the new evidence and only that evidence is planned, so the run keeps its id, every answer already recorded, and every concern already completed. An existing run returns a durable queued receipt, even with `--detach`; inspect it with `resolve admissions --run-id <id> --json`. Queued means evidence accepted, not planned or approved. A live run applies it between worker waves; resume a parked run to apply it. The admitted concern passes the same approval and material-question gates as one from intake, and may depend on a concern this run has already finished.

The evidence is a `# lup:` note you write in the file it concerns, which keeps the concern traceable to code; an open issue by number, which keeps it traceable to what was filed; or the human's own words when neither carries them. The run records which, so a statement-grounded concern is distinguishable in review. Admission is accepted at any phase before integration — past that the review branch is assembled, and a fresh run is the honest answer.

So a concern discovered mid-run is never dropped and never a reason to restart. Restarting re-derives the inventory from scratch and discards every material answer already collected, which is the most expensive thing you can do at exactly the moment the run holds the most of them.

## Friction you hit during a run is an issue, not just an admission

A run exercises the resolver harder than anything else does, so it is where the harness's own defects surface. **File each one as an issue against this repository at the moment you meet it**, with the exact command, the exact error, the state it left behind, and what the recovery cost — observation rather than conclusion.

Use `uv run lup-devtools dev report-friction`; the command requires that evidence and files it on this checkout's own repository without a question. A component another declared tracker claims stops it instead, and it prints the same command naming that tracker with `--repo`, which the permission policy asks about.

File it *and then* decide whether to admit it. The two do different work: an admission dies with the run, while an issue survives it and is picked up by the next intake, which is what closes the loop — friction becomes an issue, an issue becomes evidence, evidence becomes a concern, and the concern becomes the fix. Friction only admitted is friction that has to be rediscovered.

Intake takes every open issue except those labelled `resolver-skip`, so a filed issue needs no further wiring to reach the next run. `uv run lup-devtools dev issues` prints exactly what a run would take, without starting one. When a concern derived from an issue lands, the run comments on the issue naming the review branch — and never closes it, because a reviewer passing is not a human having read the code.
