# Coordination

Orchestration is one process deciding what several agents do. Coordination is
several agents that already exist finding each other: a roster somebody joins
rather than is spawned into, mail that outlives the process that sent it, a
person who is addressed the way an agent is. Everything here folds shared
state off disk instead of remembering it, which is what lets a peer answer to
a process that did not create it.

## Two populations, one mechanism

A **cohort** is what one process assembled — a resolver run's workers, the
agents a session spawned as it went. Its directory is wherever that process
put it, and it ends when the work does.

A **repository roster** is the population nobody assembled: every session
working in this clone, in whatever worktree, started by whoever. It lives at
`<repo>.git/lup/coordination/`, which every worktree resolves to the same path
and none of them owns, so a branch cannot change what a peer reads and
removing a worktree does not take the roster with it.

Both are the same roster, mail and journal over different directories. That is
the whole of the reuse and it is the point: a message to a peer and a message
to a spawned worker travel one stream, fold through one set of records, and
wait in one kind of mailbox — so there is one delivery path to get right
rather than two that agree until they do not.

Different repositories are structurally disjoint. There is no global registry
to collide in, no daemon to elect, and no way for a session in one project to
appear on another project's roster.

## What a directory says about itself

A roster, a message stream and a journal in one directory were a cohort by
convention, which every reader had to know and a stranger could not read at
all. `cohort.json` states it — which run, since when, and what for — and
`cohorts_under` is what a peer that created nothing walks to find one. Nothing
else moved into it: who is present is still the roster's fold and what is
queued is still the mail's, because those move and this does not.

## The person is a member

The user joins as `user`, with a mailbox and no session, addressed by the verbs
that address an agent. A report is a message to `user`; a question is a
message to `user` carrying a slot id, and the reply settles the slot. Messages
park nobody; questions park, because the slot parks.

That is what makes the human channel one mechanism rather than two. A person
resolved ahead of the roster by a branch of their own leaves every other
reader half-right: a listing does not show them, a console cannot find them,
and whether an address reaches anybody depends on which of two paths the
caller is on. `live()` is the one place they are left out, because nobody
started them and a listing of spawns should not claim otherwise.

## Identity

Two facts, deliberately separate. The **id** is minted once and never moves:
mail is addressed to it, and a restart reattaches by it. The **name** is what
a person types, and a session renames itself whenever a name would tell its
peers more than its checkout does. Renames are journaled rather than
overwritten, so a name somebody wrote down an hour ago still reaches the
session it named until something else claims it — there is no error a sender
could be shown, because the name they used was correct when they read it.

**A name reaches one live session.** A session is called after its worktree,
and two sessions in one worktree want the same name, so the second is
numbered — `dev`, then `dev-2` — against whatever the live sessions are called
when it joins. A name chosen deliberately, by rename or by a launch, is
refused rather than numbered where a live session already answers to it,
because the caller meant it. Resolution runs the other way round: a name
reaches the live session that claimed it most recently, and only where none is
live does it reach the last claimant of all, so that a message to a session
that has stopped is refused with when it left and what it concluded rather
than queued for nobody. A message to one's own address is refused too.

**An id always reaches its row.** A name is chosen and may spell anything,
another member's id included, so an address is resolved as an id first and as
a name only where no member carries it; `coordination roster` prints the id
beside every name. That is what settles two rows a reader cannot tell apart by
name, and every verb taking an id — a console's `--id`, a lock, a handoff —
reaches a subagent's row by it as readily as a session's.

**A native subagent is a row of its own**, beneath its session's. Nothing a
tool server starts with tells a session's conversations apart — on Claude Code
they share one, and on Codex each subagent's calls reach a copy started under
the session's environment — so which of them made a coordination call is
carried by the call: a hook both runtimes fire before a coordination
tool runs writes the calling subagent's id into it, read off the runtime's own
payload, and the verbs act on that subagent's row — its description, its
name, its locks, its mailbox — and leave the session's alone. The row is keyed
by the runtime's subagent id under the session's, named what the spawn called
it — which Claude Code records beside the session's transcript and Codex atop
the subagent's own rollout — numbered like any default name, and live
while its session is: it ends when the subagent stops, forwarding whatever it
never read to its session, and with its session in any case. A subagent
reaches the session that dispatched it at that session's address. What a
subagent's calls change is held on its row, so a sibling writing there is
asked, and the session writing under a subagent it has running is asked too;
a subagent is not asked about its own session's claims, since the session
dispatched it into that work. `dev policy` run from a subagent's shell carries
only the session's id; it reads as that subagent while the subagent's command
is the one the session's family has running — the dispatcher opens a window
keyed by the calling conversation around each command — and as the session
where no single one is.

A launcher mints both halves and exports them as `LUP_COORDINATION_MEMBER` and
`LUP_COORDINATION_NAME`, and can prove them, the way `LUP_AGENT_IDENTITY` is
proven: a hook is spawned by the runtime CLI with the CLI's own environment, so
an agent exporting these inside a shell call cannot reach the hook that reads
them. The name is minted by the launcher rather than at the join because the
runtime's own chrome shows it, and a chrome saying `dev` over a roster
answering to `dev-2` would be two names for one session. A bare session in a
worktree has the plugin and no launcher, so it falls back to the identity its
own runtime gave it and is named after its worktree when it joins, numbered
the same way. It is a full peer that cannot prove who started it.

**A runtime started from a session's shell is somebody else.** Every process
the launched runtime starts inherits those two variables — and so does a
`claude -p`, a `codex exec` or a pipeline run from the session's shell, which
then joined, described, departed and read mail as that session: its ending
ended the session's row, its tool calls were handed the session's mail, and
its prompts, from another transcript, read as the session's conversation
rewound and cleared what the session said it was doing. The id was minted
for one runtime, and the row names the runtime it answers for, so each hook
and tool server asks which runtime it serves — the process feeding its input
— and a runtime that is neither the one the row names nor the one that
started it is a member of its own: its id is the session's followed by `_`
and a digest of its process (pid, start time and namespace), which its
hooks and servers each arrive at without asking one another, and its row
names the session it was `spawned_by` and is called after it
(`<name>-spawned`). That holds after the session has stopped, so a runtime
outliving it never takes its ended row. The first runtime to reach a row
naming nobody takes it, and a session whose own runtime started the one that
got there first — possible only in a store nobody had joined — takes its row
back. A lup CLI verb run from a shell has no input to read a runtime from
and acts as the session the environment names.

**Every verb but describing is refused until the session has described
itself.** The roster is read by sessions deciding whether they can touch the
same code, and a row saying only where a session is answers them wrongly; a
session that has run all day without saying what it is on is the ordinary
case, not the exception. So a session says what it is on before it may list
its peers, reach one, or take a prefix, and says it again after a rewind or a
clear, which unsays it. One line, at the start and whenever the work changes.

## Delivery is a property of the member

What carries a message differs by what the recipient is, so the roster carries
it and a sender is told which mode it got:

| Mode | What it means |
| --- | --- |
| `hook` | Its own hook puts the message in front of its next tool call, so a working recipient cannot fail to read it |
| `waiting` | The message waits in its mailbox until the recipient next looks, and nothing wakes it |

The mailbox is the durable record either way; every other mode is a wake *on top
of* it rather than an alternative. A sender told only that the mail accepted a
message cannot tell a hook from a file nobody is watching, which is why
`spawn_say` reports the mode rather than asserting delivery.

## What each session is holding

Nobody declares what they are working on. Asking them to is asking for the one
thing an agent reliably forgets, and a declaration nobody keeps current is
worse than none — it reads as current and is not. So it is observed: a hook
watches what each session's calls actually change, and the record of that is
the claim.

A **touch** is an exact file some session changed. A **lock** is a prefix a
session took deliberately, for the case observation cannot reach — an agent
about to rewrite a package has changed none of it yet, and the moment worth
telling anybody about is before the first write rather than after it.

A claim is alive while its holder is on the roster and its path is on the
disk, and expires with either. There is no timeout to tune and no release to
forget, which is what makes an observed claim safe to act on: the failure mode
of the whole mechanism is a session that stopped, and a stopped session's
claims go with it.

**A claim is evidence, not an assertion.** It records the modification time of
the path it was taken over, so a reader asks the filesystem rather than the
record: the path is gone and the claim names nothing anybody could write;
somebody has written it since and what stands there is not what this session
left; otherwise it holds. Nothing has to be written to retire one, which a
worktree removed from under a live session would otherwise need — a claim
outliving the tree, endable only by another record. A prefix lock is
exempt from the second test, because a directory's time moves whenever
anything under it does, including by the holder: a lock ends when it is
released, when its holder leaves, or when the prefix is gone.

What a session asks for itself fails early instead: a lock over a path that
does not exist is refused, and so is releasing a prefix the session does not
hold, naming who does.

Editing under somebody else's live claim is an approval question naming the
holder, never a refusal. Two sessions in one file is sometimes exactly right,
and a policy that decided otherwise would refuse ordinary parallel work. What
it must not be is silent — the failure this exists for is finding out at merge
time. Claims are keyed by absolute path, so two sessions in different worktrees
never collide over the same source; the merge is what reconciles those.

**A claim can have more than one holder, and that is derived rather than
guessed.** A call that names its file attributes exactly. A shell command names
nothing it will write, so what it changed is read by comparing the tree before
and after — and that comparison sees every change in its window regardless of
who made it. Each session writes down what it left, on its own file, and a
contest is two live members' claims meeting on one path: nothing records it,
and nobody names a rival at the moment of writing. That is the difference
between an honest pair and a guess — a before-and-after sees the change and
cannot see who made it, so the reader deciding whether it is safe to write
gets both names from evidence rather than one from a suspicion.

**Holdings ride on the roster row, beside the description rather than behind a
second call.** A session listing its peers is asking one question — is it safe
to start here — and the two halves of the answer are what a peer *said* it was
doing and what its calls actually claimed. Splitting them across two surfaces
would put the reliable half behind a call nobody makes at the moment it
matters, so `coordination_peers` carries `holding` on every row, and
`contested` beside it for the claims that already have more than one name on
them. `doing` is self-reported and only as fresh as the last time somebody
wrote it; `holding` is observed. Read the second before writing.

The console splits them the other way, and deliberately. `coordination roster`
carries a count per row, because a count is the decision — whether there is
anything here to ask about — and `coordination holdings` carries the paths,
because a person who wants those wants all of them at once rather than one row
at a time.

**A listing is who is here, and who left while the reader was.** A session is
shown the live rows and the rows that stopped since it joined — the departures
it was here for, each saying it stopped and what it concluded — and a console,
which has no arrival of its own, is shown the live rows and the departures
still inside the retention window. A session that stopped longer ago than that
is gone from the store entirely, because the store is the population rather
than its history: a listing that showed everyone who ever joined would be read
a month on by scrolling past them to find the two who still work here.

## A rate is owed by the repository, not by each session

A session spacing its own requests is polite on its own and three of them
are not: the repository this grew out of got a host to block it with three
sessions each keeping to the rate the operator asked for. So the budget a
host or an account is owed is counted where every session of one repository
meets — `SharedBudget` in `lup.execution.resilience.budget` keeps one file
per key under a directory the caller names, the coordination directory
being the obvious one, holding the moments of the requests still inside the
window. A reservation reads, prunes and appends under an exclusive lock and
returns either a slot or how long until the oldest request leaves the
window; `slot` sleeps that long outside the lock and asks again, because a
slot promised to a waiting process is not a slot held. A budget of zero is
never granted, which is the honest answer to a surface that must not be
requested at all. The per-loop `Throttle` stays what it is: spacing inside
one process, which the shared budget does not replace but sums over.

## Work that outlives the session that found it

A touch says what a live session is holding and expires with it. A **task**
outlives whoever wrote it and is meant to be picked up by somebody who was not
there — the same distinction the ledger exists for, so a task is a node in the
repository's log rather than a record of coordination's own. That also puts it
in reach of an edge from anywhere: a handoff transferring it, a claim it
verifies.

**A task needs only a title.** Delegating is reached for far more often than
anything else here, so it has to be one line. A gate asking for more would
make it expensive enough to skip, and a task nobody has scoped is still a real
thing to have written down.

**`needs` and `blocks` are different facts and must not be conflated.**
`needs` says what *class of input* a task waits on — judgement, identity,
account, payment, command, review — from a closed vocabulary, which is what
lets a rendering be ordered without anybody writing "most urgent first" at the
top, and what tells a reader whether a row is a decision to make or a command
to paste. A dependency between two tasks is the `blocks` edge, node to node.
Running them together makes both useless: the ordering stops meaning anything
and the dependency stops being checkable.

`ledger delegate` records one and hands it over; a name nobody answers to
parks it rather than refusing it, because work is often scoped before there is
anybody to do it. `ledger mine` renders one holder's outstanding tasks grouped
by what they cost — the person's by default, since they read on a machine that
cannot query the log.

## Handing over a body of work

A task is one piece of work. A **handoff** is the larger thing: a session
stopping, or somebody better placed taking over, where what crosses is the
work *plus* what the sender learned that is not in the diff.

What a receiver needs is a **field, not a gate**. `open_questions` refuses to
be empty at construction — a handoff with nothing open is somebody finishing,
and closing the task is the verb for that. Every established result carries
the **source** it came from and a **grade** saying how well it is supported,
because a result the receiver can neither check nor weigh is one they have to
derive again, which is the cost the handoff exists to remove. The grade is a
string this library does not interpret: what grades exist is a project's
question, the same one the ledger refuses to answer about node types.

`not_again` is the cheapest field here and the one that pays most. A dead end
costs the receiver exactly what it cost the sender, and it is invisible in the
tasks, the locks and the diff.

`watch` names the nodes the work rests on, by id or slug. The brief opens with
which of them have a record newer than the handoff — a premise refuted, a
question answered, a task closed — read off the log's own timestamps rather
than any stored standing, so the receiver meets a moved premise before the
established results that rested on it. A research repository wrote this list
by hand in every direction file and nothing ever read it; here it is read.

**Locks move only where the sender held them.** Only a holder can release a
lock, which is an invariant rather than an accident. A scope naming a path
somebody else holds neither takes it from them nor refuses the handoff: the
contest is recorded with both names, the way an unattributable edit already
is, and the receiver is told. Refusing would make handing work over expensive
enough to skip; taking it would revoke a claim from a session still writing
under it.

**One record, three renderings.** A peer in this repository resolves ids and
keeps them, because an id stays true as the work moves where a copy goes
stale. An agent with no repository access gets everything a peer would look up
written out. A person gets a file. They differ in form and never in substance,
and the inlining one earns its keep by proving the record stands alone: if it
is not enough to work from, something was still living in the sender's head.

## Waking whoever it went to

Mail is written first, always. A wake that cannot be made costs latency and
never the work, which is what makes the asymmetry below tolerable rather than
a gap. A wake carries every message waiting whole, so one the member's
runtime accepted — a frame its wake socket took, a message its queue took —
hands over what it carried then, the way the member's own delivery hook
does: that hook, at the member's next tool call, hands over only what no wake
carried, and a message is put in front of the member once.

The asymmetry is the runtime's own. One of them serves a command that reaches
a session from any process, so waking finishes the job itself. The other has
no command that speaks to a running session at all — so waking returns an
*instruction* naming the caller's own messaging tool and the address to use,
and a skill running inside a session carries it. A third outcome, that nothing
can reach the peer, is reported rather than silently skipped.

The handle a peer is woken by is **declared when it joins**, and declared by
the adapter for the runtime that would use it — because what a handle even is
differs by runtime, and a neutral answer would be right for at most one of
them. On Claude it is the path of the session's own wake socket, which the
runtime names to the processes that session starts, so a tool server reports
where its session listens without being told; the wake is a frame written
there, carrying the member's session id so a socket that is not theirs drops
it, and the library makes it. The socket holds no mail — a message is the
store's, read whether or not anything woke its reader — which is why it is a
wake socket and not a mailbox. On Codex it is the thread `codex queue` takes,
which nothing hands a server Codex starts, so that adapter declares nothing —
an outcome reported rather than skipped.

Declaring it is not enough to reach anybody. A path is only good to a process
that can open it, and a contained session's filesystem is its own — so the
launcher places these sockets in one directory every session it starts can
reach, mounted under the path it has outside, because the path is what a
member publishes and another container reads back. Each is keyed by the
member's id, never its name: a name repeats and changes at a rename, and the
id is what addresses. A socket file is removed only where the roster says its
owner left and nothing still answers on it. Reachability of that path
is the whole of the credential: the frame carries no token, so the directory
those sockets live in is the boundary, and widening it widens who can put text
into a session. It is deliberately not a directory the runtime scans for
peers, which would make every session on the machine natively reachable by
every other through a channel the peer policy cannot see.

## The store is state, not records

Records are append-only logs folded whole by every reader on every call. They
answer *who is here* by replaying everyone who has ever been here, which grows
without bound — 597 KB of touches and sixteen rows for two live sessions — and
they cannot be asked anything they were not written to answer. A
claim over a path in a deleted worktree stands until another record retires it.
A change nothing can attribute is written down with a guess and a list of
suspects beside it. Presence needs a second stamp directory, because the only
record that ends a row is the one a session writes on its way out, and a
killed session writes nothing.

The store is one file per member instead, written by nobody but that member's
own processes, and every relation between members derived at the read:

| Question | What answers it |
| --- | --- |
| Is this member still here? | The runtime process its file names, where the reader can ask it; else the pulse its tool server holds; else its file's modification time |
| Does this claim still hold? | A stat of the path, against the time the claim recorded |
| Do two sessions contest a path? | Their two files both claiming it |
| What is this member called? | The names on its file, newest last |
| What is waiting for it? | The files in its mailbox |

Nothing is folded and nothing is replayed, so what the store holds is bounded
by the population rather than by its history: a member that stops takes its
file to `departed/`, and the sweep deletes that after the retention window.
Nothing here needs compaction.

A clone that ran 0.2.x still holds the records that store folded —
`touches.jsonl`, `roster.jsonl`, `messages.jsonl`, `names.jsonl`, and the
`delivery/`, `heartbeats/` and `resets/` directories. Nothing reads them, and
the first sweep that finds them deletes them; a sweep of a clone without them
touches nothing.

**Presence is the runtime's process, not a beat.** A session is the Claude
Code or Codex process somebody started, and its row names that process — its
id, its start time, and the pid namespace both belong to, since an id alone is
reused and means nothing in another container. A reader in that namespace asks
the process: present however long the file was quiet, which is what a machine
that slept leaves every file, and gone the moment it stops. A reader in
another container cannot see it, so the session's tool server holds a lock on
the row's pulse for as long as it answers for the session; the kernel lets it
go when the server ends however it ends, and nothing about it lapses in a
sleep. Only where neither speaks — a server that died without a word, behind
a namespace the reader cannot see into — does the file's modification time
decide, against the two-minute window.

A beat answers for the runtime rather than for the server beating it. The
server names its runtime as the process feeding its input, through any
`uv run` or shell between them, and beats only for a row naming that runtime,
and only while holding the pulse — which one server of a runtime holds at a
time, so the server Codex starts for each subagent, and keeps after the
subagent stops, answers for the session only where the session's own stopped
while the runtime runs on. Each tick asks the runtime first, and one that
stopped ends the row, saying so; a server stopping after its runtime went does
the same, which is what a runtime killed outright does to its servers' input.
A runtime started from a session's own shell inherits the session's id and
can outlive it: measured with real tool servers under stand-in runtimes, one
put a cleanly ended session back on the roster within a tick and beat for it
for as long as it lived, and one freeze past the window took a live session's
description off its row at the next sweep. Neither can happen to a row naming
another runtime, or answered for by a pulse somebody holds — and such a
runtime's server answers for a member of its own, spawned by the session.

**The one place this spends more is the stat**, because settling a claim means
asking the filesystem rather than reading a record. Measured on 2026-09-19
over a throwaway store, against the 8 ms a dispatcher-shaped fold took over
the real one: a claim check costs 0.6 ms against an empty roster, 2.3 ms with
one member holding a hundred paths, and 3.8 ms with ten members holding ten
each — the shape a busy clone reaches. It degrades with the *total* claims
rather than the population, reaching 32 ms at a thousand, which is a figure to
watch and not one a repository roster produces.

**Writing is the owner's.** A member file is revised under a lock of its own,
because three of that member's processes revise it — its tool server, its
prompt hook, and the permission dispatcher recording what a command just
changed. Per member rather than per store, so one session's writes never wait
on another's, and the region held is one read and one rename wide. The one
store-wide lock is taken for a join or a rename, which are the only decisions
made against every other member's file.

Both of those were raced in real processes on 2026-09-19 rather than reasoned
about: twelve sessions joining one worktree at once took twelve distinct
names, and twelve senders writing into one mailbox at once all landed and all
consumed. That is `flock` and `rename` on one Linux filesystem. Whether they
hold across a bind mount on Docker Desktop's virtiofs is the assumption this
store hands its adopters, and the reason it needs neither a daemon nor SQLite
to be wrong about.

## A notice is not a message

Two things reach a member and only one of them is mail.

**A message is addressed and consumed.** It is one file in one member's mailbox,
written by the sender and deleted by that member once it has been handed over,
so "what is waiting for me" is a directory listing. There is no position for
anybody to keep: nothing to commit after a crash but what was never handed
over, and no way for a second reader to be behind a first.

A message is signed with the address a reply reaches — the sending member's
id, or `user` for the person — and its reader is handed it as
`[message from <sender> by <door>] …`; a door with no address of its own, a
run steering its workers, signs nothing and reads `[message by <door>] …`.
Every message posted also lands on the store's mail record, `mail.jsonl`, one
line naming the member it went to: the mailbox is its reader's position and
empties as it is read, so what was said to a member, and by whom, is read
from the record — the dashboard follows it from a cursor. The record holds
the roster's retention window, as the departed do: the sweep cuts every
message sent before it off the record's head, leaving a first line that
counts what was cut, so a line keeps its number and a reader following the
record is carried across the cut.

**A notice is neither.** "The base moved under all of you" is a fact about the
population rather than about its recipients: it stays true after it is said,
and a member arriving afterwards needs it as much as one already here. It sits
in one file per notice, is read at the head of a turn, and is retracted by
deleting it. Nothing consumes it, so nothing has to remember having read it —
which is why a replayed or resumed turn reads exactly what a first one did.

That separation is why there is no broadcast token. A "to everyone" spelled as
a `*` every reader matches against itself matches at *read*, and that forces a
delivery cursor: a message nobody addressed to you can still be
yours, so you have to remember how far you got. It is wrong for half its uses
besides — `redirect --to everyone` would stop every worker spawned *after* the
stop, with a reason that is never about it.

So the two acts are spelled apart. A **redirect** is about now: it denies a
tool call, and a member that does not exist cannot be stopped, so "everyone"
means everyone working and the sender resolves it against the roster. A
**statement** stays true, so it is not delivered at all — `notify` posts the
notice *and* sends a message to whoever is live, because a fact worth stating
is worth hearing before the turn they are in ends. The member that arrives
later never sees that copy and does not need to: the notice is still there at
its first turn.

A repository session reads its notices at the prompt fold, once each rather
than restated — a session here reads one prompt after another for hours, and a
line repeated at every one of them is read at none. The session that starts
tomorrow is told at its own first prompt, which is the whole of what a notice
being state rather than mail buys. `coordination notice`, `coordination
notices` and `coordination unnotice` are the console's; a run's are the same
three verbs under `resolve`.

## One fold, three readers

Three processes read this store and no two of them share an import. The typed
library runs inside a session's tool server and has all of `lup`. The hooks a
runtime fires — before a prompt, as a session ends — are bare scripts spawned
with no working directory, no `PYTHONPATH` and no virtual environment. The
compiled permission dispatcher is a third, under the same constraint.

A fold in each of them separately means every record the store gains has to be
taught to three readers, and a reader that misses one goes on answering
confidently about a store it does not understand. The fold is written once
instead, in `lup.coordination.bare`, under the strictest of the three
constraints: the standard library alone, no pydantic, no `lup`.
The library imports it as an ordinary module; each plugin carries the package
whole beneath `hooks/runtime/coordination/`, the way the policy kernel is
carried, so its relative imports resolve there exactly as they do here and
every file travels byte for byte.

It owns the layout too. Every file name, stamp directory and staleness window
the store is made of is declared in that one module and imported by the typed
writers beside it, so a rename moves every reader with it rather than leaving
a test to report the mismatch afterwards.

**The package travels whether or not a project declared a roster.** The
dispatcher imports it at its top level, and a plugin carrying the dispatcher
without the package would be a permission hook that raises before deciding
anything — which refuses every call in the session rather than one of them.
What is conditional is the two guards, which a project declining `peer_policy`
never registers.

Each guard hands over to a small entry beside the package rather than to a
module of it, and that entry names its own directory as the search path before
importing — the shape the compiled dispatcher already uses. A hook leaning on
the interpreter's own path would keep working until something passed `-I`,
`-P` or `PYTHONSAFEPATH`, and because these hooks fail open it would not break
loudly: the roster would simply stop answering.

## Watching the repository

Everything here is an append-only file, and nothing pushes: a session folds
the files again on its own next call, which serves a session and nobody else.
`coordination watch` is the fold run on a clock, saying only what is different
from the last look — who arrived and left, what a session now says it is on,
and what reached whose mailbox. Noticing consumes nothing: mail is read the way
a peek reads it, so a person watching a peer's mailbox is never the reason the
peer did not see a message. The first look is a baseline rather than a replay,
the same convention a run follower keeps when attaching to work already under
way.

The same watcher is a **run** for the case where nobody is at the terminal.
`coordination watch --as-run <dir>` declares it as a pipeline, so it survives
its launcher, is followed with `run monitor <dir> --events`, and reports a
stall as a stall. It nudges — a process nobody reads exists to act — by
whatever path each member declared, and lands when the roster is empty,
because a watcher with nobody to watch is finished. Started against an empty
roster it lands at once, which is the truth rather than a process idling for a
population that may never arrive.

## What reaches a session at prompt time

A session has no reason to ask who else is here at the moment a prompt
arrives, which is the moment it most needs to know. So the roster's *changes*
are pushed there: a hook the runtime fires when a prompt is submitted folds
the roster and says only what differs from this session's last prompt — a
path somebody now holds under this checkout, contested ones first, then who
arrived, who left, and who now says they are on something else. The first
prompt of a session is a baseline rather than a replay, and gets one line
saying how many others are here and where the listing is. A quiet roster costs
no context at all, and a broken one costs the prompt nothing, because the hook
fails open.

The same hook beats for the session, which a reader who can ask neither its
runtime nor its pulse reads it present by: a running row nobody vouches for
within the window reads as gone, and a sweep retires it. And it is where a
rewind is noticed. A runtime that
rewinds or clears a conversation keeps the process, the session id and the
tool server, and signals none of it — so the row would go on saying what the
discarded conversation was doing, under a pulse the same server keeps beating.
The fold reads the transcript the prompt names and counts its conversation
roots; a root that was not there at the last prompt, or a transcript that is
another file, means the conversation moved. It stamps a reset beside the
session's pulse and starts over from a baseline, and every reader — the
listing, this fold — treats a description older than that stamp as unsaid
until the session describes itself again. What the session holds is left
alone: a touch is what happened to the tree, and the tree is whatever the
rewind left it.

What does not reach a session this way: the roster itself, which
`coordination_peers` lists whenever asked; mail, which the delivery hook puts
in front of the next tool call; and holdings in other worktrees, which the
merge reconciles and the listing still shows. Each session's last look is kept
beside its delivery position under the coordination directory, so two sessions
in one checkout are each told what changed since *their* prompt.

What is measured differs by runtime, and
[platform-differentiation.md](platform-differentiation.md) carries the row:
on Claude Code the rendered hook's stdout is measured by a test that runs it
over a store the typed writers produced, and its arrival in a live session is
not yet; on Codex the event and its context channel rest on the vendor's
documentation at https://learn.chatgpt.com/docs/hooks alone, no Codex session
being signed in on the machine this was written on.

## The other address book

A runtime that can already address another session offers a second way to
reach one, and the two acts are not the same. A message on this stream is a
durable record every worktree folds and every later session can read; a native
send is a call whose text exists only inside whichever process received it. So
where both would reach the same member the native send is stopped and told
where the durable one is, and where it reaches somebody the roster has never
heard of nothing happens at all. A send between two conversations of one
session goes through untouched as well — a subagent reporting to the session
that dispatched it, or the session steering or continuing one of its own —
because it never leaves the process, so there is no record another worktree
could have read.

A native *listing* of who can be reached is the different case, and refusing it
would be wrong rather than merely strict. Measured against a live account, most
of what such a listing returns is sessions in other repositories: a population
this roster is structurally incapable of holding, since different repositories
are disjoint by construction. A redirect to the roster would answer a question
the reader did not ask and be escalated past every time. So the listing goes
ahead untouched and this repository's roster rides beside it, labelled — the
failure worth stopping is a reader taking the wider list for this repository's
address book, not the reader having the wider list.

That asymmetry is the whole rule. Refuse the act that would leave no record;
never refuse the answer to a question the record cannot give.

## Actor cohorts

The three delegation patterns in [orchestration.md](orchestration.md) all end
the same way: the delegated agent runs, and whatever anyone learns while it
runs has nowhere to go. A subagent's task is fixed at dispatch, a nested agent
is unreachable inside its tool call, and a background agent takes state on a
wake rather than a sentence mid-turn. When several agents work at once and the
facts move under them, that is the whole problem — an agent verifying a
statement you have since disproved keeps going because nothing can tell it.

An **actor cohort** (`lup.coordination.cohort.ActorCohort`) is a population of
agents that stay in contact while they work. Each holds one session across
every turn it takes; anything addressed to one lands in front of its next tool
call through a hook it never chooses to check; and the person watching is
itself an address, `user`, so an agent can say something back.

| Aspect | Actor Cohort |
| --- | --- |
| Lifetime | As long as the population is held; each member across many turns |
| Runtime | One held session per member, from a recipe the cohort configures |
| Initiation | `ask` (awaited), `start` (detached), or `work_all` (a whole wave) |
| Communication | Mail both ways, mid-turn; questions through a `QuestionMailbox` |
| Use case | Several agents at once, over work whose facts move under them |

**`ask` versus `start` is the load-bearing distinction.** A caller blocked
inside an awaited call cannot make another, so a cohort whose members are all
`ask`ed has steering tools that can never fire. `start` returns immediately
and the caller keeps its turn — which is what makes saying anything possible
at all.

**Fan out with `work_all`, not with a gather of your own.** How many agents
run at once, which of them are running, and what a close reaches are three
facts about the population; a caller that assembles its own wave from
`start_work` and `asyncio.gather` gets the cap right and the other two wrong.
`work_all` runs one piece of work per address and hands back each answer
positionally — a result or the exception it raised, faithfully, so a caller
that classifies failures can still tell a park from a host fault from a
cancellation.

**A raise does not always finish an agent.** A raise usually settles the agent
it came out of, but work can stop because it was suspended — parked on a
question, drained at a boundary, stopped by a failing host — and every one of
those expects the same agent to carry on. `settles` is how a consumer says
which of its own failures suspend; recorded finished instead, the resume opens
a fresh conversation rather than reattaching to the one holding the context,
and every door reads a waiting agent as a stopped one.

It belongs to the cohort, passed once at construction, rather than to each
wave. A suspension is raised in both places a raise can happen — a drain
checked between rounds comes out of the work, a host fault out of the turn
itself — so a judgement held by the wave answers for one and not the other,
and the turn's own failure path finishes the agent before the wave is ever
consulted. Which failures suspend is a fact about the consumer's vocabulary,
and a consumer has one.

**The cohort owns the wiring.** Delivery works only if the mailbox hook is in
the options the session opened with, so callers pass an `ActorRecipe`
(`(ActorRef, LupHooksConfig) -> Agent`) and the cohort hands it the hooks. A
recipe that had to fetch them could be written once without them, producing an
agent that looks spawned and reads nothing anyone sends it.

**Addresses are supplied or minted.** `cohort.actor(kind, id)` with an id
derived from durable state is stable across a restart, which is what lets a
resumed run reattach to conversations rather than open new ones;
`cohort.actor(kind)` mints one for a spawn nobody declared. That is the only
difference between the two cases.

**The population is a record, not a dict.** `live()`, `members()` and
`reaching()` fold `roster.jsonl`, so a console in another process resolves the
same address the cohort's own tools do, and a restart rebuilds the roster.

## What a session reaches this through

`lup.coordination.tools.create_cohort_tools` serves the verbs an agent needs —
list what I spawned, read what one of them has found so far, say something to
one of them, say something to the person watching. Reading is what makes
steering more than a guess: a spawn's turn events reach the journal as they
happen, so `spawn_read` folds its own words, its calls and its refusals out of
that record while it is still working, and a redirect can be aimed at what the
agent is doing rather than at what it was asked.
`lup.coordination.mailbox.QuestionMailbox` adds decisions that park a run, on
the same storage; messages ride a stream and never park anything, which is why
"a message stalled the run" is not expressible rather than merely avoided.

A person reaches the same roster through `coordination roster`, `send`,
`mailbox`, `describe` and `rename`. They drive the same files, so what a console
says is here is what a session addressing it will reach.
