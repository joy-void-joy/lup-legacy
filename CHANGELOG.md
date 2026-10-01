# Changelog

## Unreleased

### Files, locks and per-person state are each spelled once in the library

The same file chores were hand-rolled at dozens of sites, each a little
differently. Each now has one helper, and the hand-rolled copies are gone:

- **Atomic writes.** `write_atomic` sets a file's mode before its first byte
  (`mode=`), syncs the file and its directory (`durable=`), and replaces only
  what the caller read (`expected=`, raising `ChannelConflictError`).
  Companion state, replay journals, rejected-attempt history, the dashboard
  registry, guidance, ledger blobs, materialized artifacts, the host secret
  store, Codex configs and `harness policy-refresh` write through it, and so
  no staging name is shared between two writers any more. The bare store's
  writers share one helper of their own.
- **Locks.** `lup.execution.locks` gives `exclusive(path)` and
  `try_exclusive(path)`. Companions, the Codex home, the mailbox relay,
  resolver state, review waiters and the review queue take theirs through it.
- **Logs.** The trace's `.events.jsonl` sidecar (`trace_events(path)`), the
  hook corpus and the ledger's journals are read and appended as
  `lup.channels.stream.Stream`s, so a torn last line and a malformed line
  are treated alike everywhere.
- **Configuration homes.** `ProviderLogin.selected_home` is the one resolver:
  an exported-but-empty `CLAUDE_CONFIG_DIR` or `CODEX_HOME` names no home,
  the default sits in the environment's own `HOME`, and Codex's home is
  resolved as Codex resolves it.
- **Per-person directories.** `lup.workspace.user_directories.UserDirectories`
  reads `XDG_STATE_HOME`, `XDG_CONFIG_HOME` and `XDG_CACHE_HOME`; lup's
  caches (environments, plugin revisions, releases, sync clones, guidance,
  gate state) now follow `XDG_CACHE_HOME` instead of always living in
  `~/.cache/lup`.
- **Checkout state.** `lup.workspace.checkout_state.CheckoutState` names every
  path under a checkout's `.lup/`, and the policy's protected `.lup/` list is
  read from it rather than repeated.
- **Digests, clock, manifests, environment.** `lup.formats.digest` hashes
  text, files, trees and part lists the way each copy did, so stored digests
  stay valid. Every compared stamp is aware UTC (`utc_now`, with `aware()`
  reading an older naive stamp as local time). `edited_manifest` changes a
  `pyproject.toml` without disturbing its layout. `inherited(overlay)` is the
  one reader of the whole process environment.
- **Hooks.** The caller-hook and subagent-cleanup host halves expose only
  `decided()`, and a generated entry reads the event, asks and prints. The
  carrier-drift fold ships from `providers/assets/`.

Removed and renamed names, each with what to call instead, are in
`migrations/pending/`.

### A write reached through a variable, a substitution or a `cd` asks as the path it names would

`cd w && F=<protected path> && sed -i … $F` rewrote a protected file with no
question inside the runtime's own sandbox, while the same `sed` naming the
path asked (#529). The assignment after `&&` was left unread, and a command
referencing a name bound to something unread was answered by a floor the
sandbox settles — a sandbox that confines the call, not the checkout it
writes in. The same floor answered many other spellings of one write.

- A variable the line settles is judged as the literal it holds: one
  assigned in a `&&` chain holds for the rest of that chain, so the line
  above asks as `sed -i … <protected path>` does. Past an `||` or the end of
  the chain the name is unread.
- A write through a word nothing on the line can read — a `read`, a
  substitution's output, an assignment an `||` may skip, a loop over a glob,
  `while read` — asks as `sed -i 1d $F` does for a name the line never
  assigned, on every placement. A reference that could also become a flag
  still earns its floor where the command asks nothing, and no longer hides
  every command after it on the line: `read F; git status $F; git push
  --force origin main` left the push unjudged.
- A `cd` is followed the way the shell follows it: through `&&`, `||`, `!`
  and into the `if` branch its condition chose, so `cd a || rm x` and `if cd
  packages; then rm lup/…; fi` are judged where they run. Where the line
  may have skipped or undone a move, and after a `cd` nothing can read, a
  relative path written is one only the run can name, and asks instead of
  being carried by a boundary. `command cd` and `builtin cd` move the shell
  as `cd` does.
- `find -exec`'s `{}` is a path only the run names beneath each starting
  point: `find packages -exec rm {} +` asks where it was read as `rm ./x`.
- A `select`, an arithmetic command and a construct nested past the depth
  the walk opens no longer hide the commands inside and after them.

What changes for a session: a write through a glob loop, a `read`, a
substitution or `find -exec` asks even into scratch — spell the paths, or
run a script under `tmp/`. Where no boundary runs, those writes ask where
they used to be refused. Still open: a `cd` into a directory that is not
there, followed by `;`, is taken to have succeeded, so what runs after it is
judged in a directory the shell never entered (recorded in
`lup.policy.kernel.lex.placed_andor`); an unclassified command, or a write
after an array or a function definition, is still carried by the runtime's
own sandbox into a protected path (#531); and a write through a hard link,
or a link the same line makes, still lands on the file it links to unasked
(#532).

### What a session's tools did, and whether its transcript holds, have readers

- `uv run lup-devtools tools metrics` shows each tool's calls, errors, error
  rate and average, fastest and slowest call, per server, over the sessions
  launched in this checkout — or one session opened in process with
  `--session`, one launched session with `--member <roster id>`, the
  processes that recorded a call after a moment with `--since`, the report
  whole with `--json`.
- Every tool-server process writes those metrics to a snapshot of its own
  under its session directory's `metrics/`. The one `metrics.json` every
  process used to overwrite held whichever of a session's servers wrote
  last, and nothing read it; it is no longer written, and an old one can be
  deleted. The directory keeps 30 days and at most 1000 snapshots, pruned as
  each server starts.
- A session opened in process records the same tool metrics whichever
  backend ran it: where a backend runs its tools in subprocesses, the session
  result now folds in what they wrote, where it used to record none.
- `uv run lup-devtools trace verify` checks the hash chain of every launch's
  transcript (`notes/harness/<runtime>/<run>/observable.jsonl`), or the runs
  named, says where each breaks and how, and exits 1 on a break.
  `trace events <run>` reads one record by record, `--kind` to narrow it,
  with the chain's standing first and a line at the record it breaks on.
- A launched session's transcript is verified as it closes, Claude's and
  Codex's alike, and a chain that does not hold is said at the terminal.
- A transcript no longer restarts its chain mid-file after a record longer
  than 256 KiB: the writer looked only that far back for the last record,
  found none whole, and read the file as empty. Transcripts written before this that break
  that way read in `trace verify` as "a second chain starts at record N".
- `lup.observability.audit.chain_break` replaces `verify_event_chain`,
  answering where a chain first fails and why rather than whether.

### Containers starting at once on one config home no longer tear or drop what the others wrote

Several contained sessions of one repository start on its config volume at
once — a launch's probes, a run's workers, a second terminal — and each
amends the runtime's documents there before the runtime starts.

- Claude Code: the entrypoint's `jq … > .claude.json.lup && mv` is gone.
  `/opt/lup/trust-seed.py` records the checkout's and its repository's
  trust, seeding a missing or empty document, and `/opt/lup/home-seed.py`
  merges your settings in; both hold `.lup-trust.lock` in the home, stage
  through a file of their own and rename it over, and write nothing where
  the home already holds what they would. A start used to be able to publish
  a `.claude.json` that was empty or began with NUL bytes, which the next
  session refused as corrupt, or to drop another start's trust entry; and
  every start rewrote the document, so a session running in the volume lost
  whatever it saved in between.
- Codex: every write a launch's home preparation makes to `config.toml` —
  the settings, the plugin's registration, the checkout's and the hooks'
  trust — holds `.lup-plugin-install.lock` and is renamed into place
  (`replace_codex_config`), keeping the file's mode and a linked file linked.
  Trust and hook-trust records were rewritten in place with no lock, so two
  launches at once could each drop the other's checkout from the trusted
  projects, strip the plugin's registration, or let a starting Codex read an
  empty config. A worktree home's derivation and the settings a session
  returns to its account go the same way.
- `lup.channels.models.write_atomic` stages each write under a name of its
  own (`.<file>.<random>.tmp`) instead of the one `.<file>.tmp` every writer
  of that file shared, which tore a file two writers wrote at once.

A `.claude.json` that does not parse, whatever tore it, is left byte for byte
for Claude Code's own recovery by both programs, each saying so on stderr —
the settings seed used to replace it with its own keys alone — and the start
after that recovery seeds it afresh. A volume needs nothing done: the next
start takes the locks.

### A session is named for its work, and keeps the name it had

A session was called after its worktree — `dev`, `dev-2` — on the roster and
in Claude Code's own chrome. Wherever a roster is declared, the first prompt
that says what the work is now starts one model call, on the strongest tier
at low effort, in a process of its own, so no prompt waits on it. The answer
renames the roster, numbered past a live session's name, and the runtime
follows: Claude Code shows it from the next prompt, and Codex names the thread
through its app-server. `HookSet(session_naming=None)` declines.

A title somebody sets in the runtime wins. A `/rename`, or a reopened
conversation's own title, is taken up by the roster in a session name's shape
(`Naming Review` answers as `naming-review`) and left in the runtime exactly
as typed. `harness claude --continue`, `--resume` and `--session` no longer
pass `--name`, so the conversation keeps its title; a reopened Codex thread's
own name is read through `thread/read` and taken up the same way.

`SessionEffort` moves to `lup.types`, beside `ModelTier`; import it from there.

### The policy reads what these commands do, and a deferral never parks

A set of commands that stopped for the operator, or passed unread, are now
judged by what they do.

- `git config <key>` reads the key, whatever the key is; a value after it,
  or `--unset`/`--add`, still asks for a guarded key. `git config get` and
  `list` read, while `git config edit` and a section renamed or removed
  (`--rename-section x core` makes `core.hooksPath` out of `x.hooksPath`) ask.
- `install` is read by its own grammar: `-m 644`, `-o`, `-g` and `-t <dir>`
  no longer make the file it reads count as one it writes.
- `git merge-file -p` prints the merge and reads; the written form asks.
- A scratch copy under `tmp/` mirroring a devtools module is scratch.
- `cp` over a file that stands there is judged as the edit it makes: the gates
  an `Edit` meets read the real difference, so a small change allows, a larger
  one is the runtime's own to answer, a dropped `# lup:` note is refused and a
  protected path asks -- the same in this checkout and in a sibling worktree.
  A copy creating a file keeps the verb's own row.
- A deferral is never a parked review, on either runtime; a
  `# lup: escalate[decision]:` line over one leaves it the runtime's.
- `gh api` writes to the pull-request and issue routes answer as the typed
  verb does, so `gh_rule(allow_filing=False)` and `allow_authoring=False`
  reach `gh api repos/{owner}/{repo}/issues` too.
- A `curl` reaching a loopback port a host process holds asks however its
  output is handled -- `-w '%{http_code}'`, a pipe, an unclassified option;
  `-w` itself reads unless its format names a file.
- The `sed -i` preview runs under `C.UTF-8`, since no runtime hands a hook its
  shell's environment; a rewrite of a scratch file nothing could preview is
  allowed rather than asked.
- `dev policy` reads its input from where the session's commands start (the
  launch's checkout, or `--from`), says so when that is not where it runs,
  and names every path whose capture it assumed.
- `dev check --antipatterns --path <copy> --as <path>` judges a scratch copy
  as the file it will land as; `dev check --changed` runs the anti-pattern
  rules over the changed files.
- The pre-commit conflict guard reads the index git commits from under `git
  commit -a` and `git commit <path>`. The installed hook hands it on, so
  re-arm with `uv run lup-devtools git hooks install` from a host terminal.

### The status line names its repository and session, what waits on you, and the dashboard as one glyph

A Claude session's status line reads `lup · dev · tree/fix-x │ ?2 reviews
(1 here: 41cb73e1) · ✉1 │ ⚠ 1 quiet │ ● http://127.0.0.1:8767`: the
repository's name as the page's tree names it, the session's roster name and
its worktree, dimmed; the reviews waiting on you, with how many this session
or its subagents parked, and the messages agents sent you, in the warning
colour and only while something waits; an agent gone quiet (a call
outstanding ten minutes with nothing new in its transcript) or a path two
sessions hold, only while one does; and the dashboard as one glyph — `●`
serving, `◐` restarting, `○` down or stopped by the operator — beside its
whole address, always: the first origin `[dashboard] origins` declares, else
loopback's, as a terminal hyperlink whose text is the address and which
never carries the capability. The page keeps its capability per origin, so
the link opens it signed in where you signed in at that origin. The session
is found from the JSON Claude Code hands the command on stdin; a narrow
terminal drops the other agents' needs, the worktree, the review's id, the
session's name, and last the repository's name, never the address, and
never cuts a word. The dashboard's pulse now lists each running session with
its repository's name and the reviews it parked, names the address you open
the page at, and counts the unread messages, quiet agents and paths held
twice, which `dashboard status` carries too (`unread`, `quiet`,
`contested`); a session launched before this keeps working, its line showing
what an older pulse has. The line still answers on the CLI's fast path,
reading only the pulse and stdin.
`status_line` answers a `ShownLine` (`plain()`, `painted()`), and
`DashboardPulse.line` and `shown` are gone (see the migration).

### A session's reviews live in its own checkout, and only subagents hold waiters

A parked call's refusal no longer tells a session's own conversation to start
a `review wait`: Claude Code stops a background command after two hours at
most, so that waiter expired and woke the session every two hours for
nothing. The session now carries on or ends its turn, and the operator's
answer -- on the dashboard, or from a terminal with `review approve` or
`decline`, which now tells the session the same way -- wakes it through its
mailbox (the wake socket on Claude, `codex queue` on Codex); the
`review wait` it names then carries the call out at once. A subagent, which
nothing else wakes, still holds a waiter: on Claude in the background with
`--timeout 7140`, on Codex in its shell tool until it reports, and it
restarts it quietly when it ends. A `-p` run still waits in the foreground.
A Codex waiter queues its report into the session's thread only when it was
left running for a session's own thread.

Every review is kept at the top of the checkout the session's launch opened
(`LUP_BOUNDARY_ROOT`), whatever directory or checkout the call ran in, and
every review command a refusal, a notice or `--help` prints is spelled
`uv run --directory <session checkout> lup-devtools review …`, so the review
is written and read with the session's code. A call from a subdirectory no
longer leaves a `.lup` of its own there, the hook observations included, and
the review CLI run from a subdirectory reads the checkout's queue.
`review propose <checkout>/tmp/<name>` lands the files in the checkout
holding the directory (or `--checkout`), records the review in the session's
queue, records the subagent that proposed it (`agent`), and is refused, with
the command to run instead, from a checkout other than the session's;
`review reply`, `wait` and `cancel` name the session's queue when the review
is not found where they ran.

Titles read the paths relative to the checkout the files lie in, and the
queue row and desktop notice name that checkout where it is not the queue's.
A review the page cannot answer says why and gives the terminal commands that
can, run with the code of the checkout keeping it, and, while the dashboard
is about to restart onto newer code, that it will shortly.

### The dashboard answers behind a reverse proxy the person declares

`[dashboard] origins = ["https://their.proxy.name"]` in the person's lup
config (`~/.config/lup/config.toml`) declares each origin a reverse proxy
serves the dashboard at. Each must be a whole origin, `scheme://host[:port]`
over http or https with no path, and is read lowercase and without its
default port. The Host check that guards against DNS rebinding answers each
declared origin's host, with and without that port, beside loopback's, and a
write is taken from a declared `Origin` as from the dashboard's own address;
every other name is still answered 421, now saying where origins are
declared, and every other origin 403. The running dashboard, shared or
`dashboard serve`, reads the list again on the next request after the file
moves, so no restart is needed; a file it cannot read leaves loopback alone.
`dashboard status` gains `origins`, `launch` (the launch address, capability
included, at loopback and each origin; from the operator's terminal only)
and `warnings` (a declared origin that is neither https nor loopback);
`dashboard open` and `dashboard serve` print every launch address, and so
does a launch the operator makes on a terminal, never a session's or a
captured one. `lup.web` pages take `origins` and `refusal` for their Host
guard. docs/dashboard.md, "Behind a reverse proxy", has a Caddy example that
is `reverse_proxy 127.0.0.1:8766` and nothing more.

### Agents are told to write review notes in plain words

`review propose --help` now explains how to write `--why` and the per-file
notes in `.proposal.json`: say what changes in ordinary words, then why, name
the file, function or command, and say whether behaviour changes. It shows a
plain note next to an obtuse one. The `review reply` text argument, the
refusal that points agents at `review propose`, and the guidance (CLAUDE.md
and AGENTS.md) say the same in a line. `review propose` warns, and still
parks, when a proposal of several files leaves one without a note, naming
it; an empty `--why` is still refused, now with a pointer to the help.
`review show` prints each file's note under its name, whole, as the page
already did.

### The review relay keeps each question once, and the page reads rows

`.lup/questions.jsonl` opens with `{"relay": 2}` and keeps one record per
question as it was parked -- every document it binds (a preimage, a verdict's
after-document, each string of its call a kibibyte or longer) stored once in
`.lup/reviews/blobs/<sha256>` and named by digest, with a retry payload that
repeats the call recorded as null -- and a small transition per state it moves
to, never a full copy.
The first open under the relay's lock rewrites an older log into this shape;
fingerprints and the host's answers are unchanged. `QuestionRelay` folds to
`RecordedQuestion` and reads only what was appended since its last read;
`resolve()` reads a question back whole as `PersistentQuestion` where its
documents or fingerprint are needed. Settled reviews older than `[tool.lup]
review-retention-days` (seven days by default) move to
`.lup/reviews/archive.jsonl` with their summary and thread, and the documents
nothing else names are swept, on the dashboard's sweep and `review list`;
`review list --all`, `review show` and History still reach them. The
dashboard keeps one store for the stream, every route, the herald and the
sweep; the stream carries every waiting review and the fifty most recently
settled as rows, a review's detail is read when it is opened and the next one
ahead of time, older History is paged (`GET /api/reviews/history`), and an
answer reads only its own review. Measured on a copy of tree/dev's 41.6 MB
log (1201 reviews): the one-time rewrite takes 0.3 s and leaves a 2.4 MB log
beside 8.1 MB of documents, and 0.55 MB once the 1044 reviews past a week are
archived; a fold goes from 0.25 s to 0.06 s, then 0.013 s; the first snapshot
from 1.4 s and 957 KB to 0.1-0.4 s and 54 KB; an answer from 0.8-0.9 s to
5 ms once the review is open; a hook's park from 0.2 s to 0.01 s. A benchmark
in the unit suite bounds what a thousand reviews cost to keep, fold and hand
the page.

### `dashboard serve` restarts onto new code, and a stopping dashboard ends its streams

`dashboard serve` now runs through the dashboard service's own
`serve_dashboard` (`ServiceArguments(shared=False)`), over a private
directory holding its capability and the repositories it serves. It follows
its checkout's code and page bundle like the shared dashboard: once they move
and the new code imports, and no write is in flight, it restarts in place as
`python -m lup.devtools.dashboard.service --terminal <state> <host> <port>
<revision>`, keeping its port and capability. It removes that directory when
it stops. It sends no desktop notices and publishes no pulse. As any
dashboard stops serving, `LiveFeed.close()` ends every open stream at once,
instead of uvicorn waiting its two-second grace and then cancelling them.
The dashboard's request check (in `dashboard_app`) and `guard_loopback_host`
(`LoopbackHost`) are plain ASGI middleware now, so a stream ending as the
server stops is no longer logged as a cancelled task.
`DashboardRegistry.recorded` keeps a repository known without a launch.

### The launches holding a shared companion keep it running

`SharedProcess.held` watches what it holds on a thread of the launcher's,
every `watched_every` seconds: whichever holder finds the process gone first
records a `CompanionExit` in the slot — when, its exit status or signal where
it can still be collected (`LiveProcess.collected`), and its last
`exit_lines` of output — and starts it again from its own declaration, on the
ports it had, under the slot's lock, waiting along `backoff` (1, 5, 30, then
60 seconds) for exits in a row. A lease dropped while its launcher still
runs is taken back. Every stop lup makes writes a `CompanionStop` — why, by
which process, how many live leases — into the slot and a line into the
companion's log before the signal, and each lease let go says why its
launcher was judged gone. The operator's stop (`SharedProcess.stopped`,
`dashboard stop`) is recorded as one that stays (`CompanionStop.stays`,
`CompanionState.stays_stopped`): the holders leave it stopped until a launch
or `dashboard restart` starts it, and every status line and `dashboard
status` say "stopped by the operator; `dashboard restart` starts it"
(`DashboardPulse.halted`). `stopped(..., stays=False)` is lup's own stop,
which the holders undo. `CompanionStanding` and the dashboard's
`dashboard status` carry `exited`, `stopped`, `restarts` and `retry`; the
page's banner and every status line say "restarted after it stopped" and
why for five minutes (`RunningCode.restarted`, `DashboardPulse.restarts`).
`dashboard restart` with sessions holding a dashboard that does not run
starts one for them. A running companion of the same declaration gets five
seconds to answer before a launch replaces it, and the dashboard a minute to
answer its first start.

### A page and its assets come from one build

`bundle_app` reads a surface's page and every asset it names into memory as
it is built, reading again while a rebuild has written the page but not its
assets (`whole_bundle`), so a page it serves never names an asset that is
gone; an asset it does not hold answers a plain-text 404. The dashboard's
page says "This page is out of date — reload it" where its script fails to
load, and every file of the page counts as the dashboard's code, so a
rebuilt bundle restarts it onto the new one. The dashboard no longer copies
its page beside its state.

### A parked call keeps the asker's account, and each command of its line

`PersistentQuestion.account` holds what the asker said the call is for, each
`Account` with its source: Claude Code's `Bash` `description`, Codex's shell
`justification`, the text the agent wrote since it last heard anything --
read back off the transcript of the conversation making the call, a
subagent's own -- or, where it said nothing, its roster row's `doing`; a
proposal's is its `--why`. Recorded once, when the call first parks, and
bound into nothing. `KernelDecision.segments` keeps each command a line was
joined from with the verdict it reached alone (`SegmentRow`), carried to the
question as `PersistentQuestion.segments` (`CommandSegment`) and bound into
its fingerprint; the page lists every command that asks with its own reason
and folds the ones allowed alone, and `review show` prints both. A
conversation with two or more edits waiting is told in the refusal how to
`review propose` the next ones. A command's review is stale only where a
file it writes moved; the waiter also reads the files it copies from, and
retires the review rather than run a copy of something the operator did not
see. A parked record names the parts its fingerprint binds
(`PersistentQuestion.scheme`, `bound_parts`), so a reader on older code says
it cannot check the record (`PersistentQuestion.unverifiable`) instead of
calling it changed, and the page labels a review it cannot answer "can't
answer here" with the reason where Approve would be. A reader like `pyright`
is no step only running shows: its row reads, and `--createstub` or
`--writebaseline` makes it a writer again.

### The queue keeps its counts while it refreshes

While the stream reconnects or a checkout's queue cannot be read, the
header keeps the last counts, marked as refreshing, and says which queue is
unavailable and why; a queue read that fails is read again a moment later
before it is reported.

### A review a recorded file moved under leaves the queue as stale

A parked call binds its approval to the files it recorded, and the waiter
carries it out only where each still stands as recorded; a review whose
sources the session edited afterwards stayed in the queue with Approve
enabled, and approving it failed out of sight. Whoever notices first -- the
dashboard on each look (`PreimageWatch` reads a file again only where its
size, time or inode moved), the page opening it, an answer, the requester's
own waiter, `review list` -- retires it with `QuestionRelay.retire_stale`
into the `stale` state, naming each file that moved and how
(`lup.devtools.review.preimages.moved`: changed, created, deleted, or a
directory where absence was recorded), and the requester is told to re-read
the file and ask again. Only what the reader can see is judged: a path inside
a session's container, or a file it may not read, is never stale. A stale
review shows only in History; an answer to one is refused with the reason.
`PersistentQuestion.stale()` is `overdue()`, and `moved` records what moved.

### What the operator writes reaches the conversation that asked, once

A waiter holding a review reports its verdict with the operator's note and
line comments, an approval's included; the mailbox and the wake carry it only
where no waiter holds the review, and a bare approval pings nobody but the
waiter. A subagent's review records it (`PersistentQuestion.agent`), its own
row hears the answer, and the session it runs in gets a copy of anything the
operator said, marked `[copy]`. Claude Code's refusal names the waiter with
`--timeout 7140` under the tool's two-hour background limit, and `--timeout
540` in the foreground, so it ends on its own saying the review still waits
and how to wait again rather than being stopped mute; SIGTERM or SIGHUP
makes it say the same. The page's answer is delivered before the reply, so
its toast says how the requester heard.

### The review page is an editor

The editor takes the height left at any width, the title names the change
relative to its checkout, the policy's reason sits in a strip above it, and
the comment box is pinned open and focused beneath. Ctrl+Enter (Cmd+Enter)
approves, Alt+Delete declines, Alt+Enter sends the note and line comments
without deciding, Alt+↑/↓ move between reviews, and Esc leaves the box for
the one-letter keys; Shift+A and Shift+D are gone. A line number takes a
comment on its line, Shift+click on a range, stored on the answer or remark
as `LineComment` (path, lines, side, note) and printed to the requester as
`path:line[-end]: note`. What was said is the review's thread, the operator's
remarks and the requester's replies (`review reply <id> <text>`,
`lup.devtools.review.thread`) beside the answer. Diffs are highlighted by
extension, every `# lup:` marker kind is marked in its colour with `m` and
`Shift+M` to jump between them, gaps fold behind expanders, and `f` shows the
whole file. Answering is immediate and a refusal puts it back, with a toast.

### Several edits go to the operator as one review

`review propose <dir> --why <text>` parks every file under a scratch
directory, each at its path in the checkout, as one review: each file meets
the gates a direct write of it would, a refused one refuses the proposal,
and one they let through is folded. `.proposal.json` notes files and names
deletions. An approval releases every file or none, where each still stands
as recorded; a file moved since stales the whole proposal.

### A shell command's review shows each file it changes, as the policy judged it

The dashboard showed a diff for a lone `cp`, a patch envelope, and a lone
`sed -i` over ASCII input it re-ran where it was read; a chain, a `cd`, a
heredoc, `printf >`, `mv`, `rm` or a `git apply` showed the command alone.
`lup.policy.kernel.documents` now follows a command line file by file, each
write applied to what the line left -- sed sandboxed over the text in any
encoding, copies, moves, installs, removals, authored bytes and appends, and
`git apply` or `patch -pN` applied by Git to a copy -- and a question keeps
each changed file's verdict and judged document (`FileReviewRow.after`,
`CapturedFileReview.after`) beside the steps only running shows
(`KernelDecision.unpreviewed`, `PersistentQuestion.unpreviewed`,
`UnpreviewedStep`). The dashboard and `review show` read that record and run
nothing; files the policy allows on their own, such as scratch and tests,
leave the default view. The edit gates judge a file by what the whole line
leaves, so a second rewrite of one file is no longer read against the first
one's input. `sed_output(scripts, options, text)` is the one sed runner;
`rewritten_text`, `copied_paths`, `shell_preview` and the captured sed screen
are gone, and `captured_edit_decision` takes the judged `after`.

### An issue filed on this repository's own tracker is not a question

`gh issue create` joins the compensable verbs — closing restores what filing
opened — and allows, as does `gh api repos/{owner}/{repo}/issues`; with
`-R`/`--repo`, or a literal owner and name, it asks, as a filing on somebody
else's tracker. `gh_rule(allow_filing=False)` puts the question back, the way
`allow_authoring=False` does for pull requests. `dev report-friction` allows
unnamed and asks with `--repo`; a new report whose component a declared
tracker claims stops before filing and prints the same invocation naming that
tracker, which is the spelling the policy asks about. `--issue N` corrects a
report wherever routing sends it, and asks beside `--repo` as `gh issue edit
--repo` does. `ShellOperationRule.amending_flags` is gone with the one row
that declared it.

### A git hook installed from any revision runs in a worktree at any other

`git hooks install` writes hooks naming no guard: each hands its moment to
`uv run lup-devtools git hooks run <hook>`, and the checkout git fired it in
runs the guards its own devtools declares there. One clone's shared hooks
directory serves worktrees at every revision, so a hook written from `dev`
no longer fails every commit in a worktree whose devtools predates an option
a guard uses, and one written from an older checkout no longer skips the
guards a newer one declares. A checkout older than `git hooks run` itself
commits with one line naming it; every other failure refuses as before.
`GitGuard.standdown` takes a `Standdown` — `MergeInProgress`,
`NoMergeCommit` or `DeletionOnly` for the `MERGE_STANDDOWN`,
`SETTLE_STANDDOWN` and `DELETION_STANDDOWN` shell snippets — and a guard's
refusal is said when it refuses. `GitGuard.environment`, `GitGuard.check`,
`HookScript.replayed`, `capture`, `framed` and `REPLAY_CALL` are gone;
`fire` is what the verb runs, and `git hooks status` lists the guards behind
each moment. A clone armed before this runs `uv run lup-devtools git hooks
install` once, on the host.

### `git hooks install` names the host's command where it cannot write

A contained session holds the shared hooks directory read-only, where `git
hooks install` failed on an errno naming a path. It writes nothing where every
moment is already current, and otherwise refuses naming the outstanding
moments and the exact command to run from a host terminal, `cd <checkout> &&
uv run lup-devtools git hooks install`, which `git worktree create` names too;
`host_install(root)` in `lup.devtools.dev.git_guards` spells it.

### A git hook moment loads the project's devtools at most once

`harness generate all` compiles the declared git guards into `[tool.lup]
git-guards` in `pyproject.toml` (`write_git_guards`, held to the declaration
by the drift check, and by a `compiled git guards` row against the dev tree's
own), and `lup-devtools git hooks run` reads it before the project's
application loads (`compiled_guards`), so a moment where every guard stands
down — a plain commit's settle, a push that only deletes — ends in about a
third of a second instead of loading the application. A guard that is
nothing but a `lup-devtools` invocation runs in that same process through
`lup.devtools.entrypoint.in_process` rather than in one of its own; any other
line still runs by `sh -c`. Measured here, a commit's `pre-commit` takes 7.0 s
where one process per guard took 12.1 s, and its `post-commit` 0.29 s where it
took 2.8 s. `fire`, `HookScript.run` and `GitGuard.run` take the runner for
devtools commands; `GIT_ENVIRONMENT` lives in `lup.devtools.dev.git_guards`,
the default of `HookScript.environment`; a standdown compiles by its import
path, so a project's own compiles as lup's do.

### A runtime started from a session's shell is a member of its own

`LUP_COORDINATION_MEMBER` reaches every process a launched session starts,
so a `claude -p`, `codex exec` or pipeline run from its shell acted as that
session: its ending ended the session's row, its tool calls were handed the
session's mail, and its prompts cleared what the session said it was doing.
Every hook and tool server reads which runtime it serves, and one that is
neither the runtime the row names nor one that started it answers as
`<id>_<digest>` — the same for all of that runtime's processes — whose row
names the session it was `spawned_by` and is called `<name>-spawned`; one
outliving the session never takes its ended row. `RosterMember`,
`SessionNeeds`, `RosterPulse`, `create_peer_tools` and `RepositoryPeers.join`
carry `spawned_by`; `lup.coordination.repository.runtime_member` resolves a
process's member. `dev policy` run from a subagent's shell reads as that
subagent while its command is the one its session has running.

### A wake that reached hands over the mail it carried

A reply woken into a session, a watcher's nudge, a delegation, a handoff and
the Codex mailbox relay carried the mail whole, and the delivery hook handed
it over again at the session's next tool call. `lup.coordination.watch.roused`
wakes a member and, once its runtime accepts the wake, records what it
carried as delivered, so the hook hands over only what no wake carried.
`WakeReceipts` and `MailboxRelay.receipt_path` are gone; the relay keeps
`lock_path`.

### The mail record is kept whole, and read a page at a time

The sweep no longer cuts `mail.jsonl`: every message posted stays on it for
the life of the clone, and departed sessions still leave the roster after the
retention window. Nothing reads the record whole. `ActorMail.posted(cursor)`
reads the latest page (a hundred messages) back from the record's end, down
to the cursor where a reader follows one, and `ActorMail.earlier(before)`
reads the page before a byte; a `MailPage` says where its run starts, and a
`PostedMessage` carries `at`, the byte its line starts at, where it carried a
line number. The dashboard's stream hands a fresh tab each repository's
latest page and what was posted after it, and the Sessions view reads older
messages a page at a time with **Load earlier messages** (`GET
/api/repositories/<key>/messages?before=<byte>`). Measured on a generated
record: a page reads in 0.9 ms at 10,000 messages (4.8 MB) and at 100,000
(48 MB), where reading the record whole took 119 ms and 1.8 s. The `{"cut":
N}` header, `MailCursor.cut` and `.seq`, and the trimming functions are gone.
A record an earlier sweep cut still reads; the messages that sweep cut are
gone.

### The first sweep deletes what the 0.2.x store left

A clone that ran 0.2.x still held that store's records in its coordination
directory — `touches.jsonl`, `roster.jsonl`, `messages.jsonl`, `names.jsonl`
— and its `delivery/`, `heartbeats/` and `resets/` directories, which nothing
reads. The first sweep that finds them deletes them; a sweep of a clone
without them touches nothing. `RepositoryPeers` takes the names as
`superseded`.

### A nested uv project is declared once and checked by every gate

`SubProject(root, python, …)` in the catalog's `declared_sub_projects()`
compiles, through `harness generate all`, into Pyright execution environments
for the project and its `tmp/<root>` scratch on its own Python and packages,
its root in Pyright's `include`, Ruff's per-file target version, a test root
whose files carry the test role, and scratch roles for every environment
directory at any depth. A fresh worktree and the gate's Pyright sync its
environment first. A pytest root takes its test role from its own
`testpaths`, a suite spreads over workers only where its own environment
holds pytest-xdist (or its `TestRoot` declares `parallel`), a gate with no
suite declared says `tests: no suites declared`, and a module is named from
the segment its first `src/` introduces, whatever that package is called.
`parallel_arguments` and `scanned_roots` are gone.

### A mode is a named preset of the declaration, selected by `--mode`

`harness claude|codex --mode <name>` opens a kind of session the project
declares. A `LaunchMode` states a `Claude(...)` and its `Codex(...)` variant
naming only what it changes, laid over the declaration the launch builds by
`laid_over` — a nested declaration field by field — with the command line laid
over both, and an `OuterContainer` for what it grants its container. How much
the runtime asks is those declarations' own fields; a mode switching the asking
off, or carrying guidance of its own, is refused on the host. The per-mode
`--<name>` flags, `LaunchSession` and the mode's `model`, `arguments`,
`record_root` and `session` callables are gone (`migrations/pending/launch-modes.toml`).

### A container's settings are one `OuterContainer`, stated by each hand in turn

Network, memory, sudo, devices, folders and the held generated trees are
`OuterContainer` fields, and the command line (`--network`, `--memory`,
`--sudo/--no-sudo`, `--hold-generated/--release-generated`, `--mount`,
`--device`) lays over a mode's, over the person's `[container]` config, over
the project's `launch_container`. A memory share is resolved against what the
engine can hand out, and refused where nothing says.

### A contained session may be held from the trees that judge it

`OuterContainer(hold_generated=True)` binds the runtime's generated plugin,
settings and guidance read-only in the checkout's container, so a session
cannot change the hooks judging it; `harness generate all` in such a session
refuses before writing anything, naming the host command, and the drift line
says the same.

### A container may put guidance of its own over the committed one

`OuterContainer(guidance=PromptDocument(...))` is rendered as generation renders
the project's guidance, held to its budget, and mounted read-only over
`.claude/CLAUDE.md` or `AGENTS.md`; the host tree never changes.

### A service on the host's loopback, reached by its declared name

`HostService(name, port, variable)` hands a session the service's address;
a container whose loopback is its own reaches it through a socket the launch
relays for that one port, bound inside by the image's entrypoint.

### Host-only secrets

`$XDG_CONFIG_HOME/lup/secrets/<project>.env` keeps the keys a host companion
is started with: `Integration(host_only=True)` answers into it, `setup secret
<KEY> [--unset]` sets one no integration declares, a `SharedProcess` is handed
only the keys its `secrets` names, and a launched session inherits none of
them. Inside a container each write refuses, naming the host command.

### Codex says who answers what it asks

`Codex.approvals_reviewer` (`user` or `auto_review`) reaches both a launched
CLI and a thread opened here. An asking `approval_policy` is refused where a
session opens in this process without hooks to answer it, rather than where
it is declared, so a launched Codex may ask its person.

### `harness clean` sweeps held Codex revisions

A revision a contained Codex session ran its hooks from is listed with its
size, and removed by `--yes` once no running container binds it.

### The edit gates say everything at once, and each thing once

An anti-pattern refusal names every violation an edit adds, in file order,
each with its own way through, where it named the first and a whole file
came back once per rule it broke. A question about a receiver nothing
resolved is asked only once nothing is refused.

After a write, the hooks sweep the file with the whole-tree rule check scoped
to it, every project rule over every span, and report what it still refuses,
so a native spelling in a multi-line string is met at the write rather than
at the gate. The one-file sweep takes 7.5 seconds where it took 18. Its
repair defers to the policy the session loaded, putting a directive back
where that policy still needs it. What the hooks report after a call comes in
two parts: what a gate still refuses blocks, and what is worth knowing
arrives as context — a directive removed, a name the next edit supplies,
another repository's referral, said in full once per repository per session.
A shell command is reviewed for every file it changed, named or not.

`dev check --antipatterns` lists what blocks first and counts what a
receiver's type refuted (`--refutations` lists each). `--changed` no longer
calls the migration declarations it parses unread. A conflict block a merge
left in a tracked file fails the whole gate, `--changed` and a second
`pre-commit` guard; `lup: ignore[conflict-marker]` excuses a fixture holding
one on purpose. `dev test` names a path nothing answers, and `--integration`
runs the tests the suites deselect. `x or TABLE` reaches a table as a default
only behind what a caller supplies, and the rule reference shows an example
the path decides with its path. Both suites filter `sh`'s fork warning and
take every inherited `LUP_`, `CLAUDE` and `CODEX_` variable away.

### The dashboard shows every session live, and writes to any of them

Everything live reaches the page on one stream, `GET /api/stream`: a fresh tab
gets the whole state once and then each numbered change, and a reconnecting
tab resumes after the last cursor it saw. A Sessions view lists every
repository's sessions with their subagents nested beneath them — what each
says it is doing, the call it is waiting on and what it last said, read from
the transcript its roster row names, what it holds, and every message to and
from it — and a box writes to a session as `user`, through its mailbox and
then its wake socket or `codex queue`. Reviews ride the same stream;
`SnapshotFeed` and `/api/events` are gone.

### Mail names who sent it, and stays on a record

A message is signed with the address a reply reaches — the sending member's
id, or `user` — and read as `[message from <sender> by <door>] …`. Every
message posted also lands on the coordination store's `mail.jsonl`, which
`ActorMail.posted` follows from a cursor. `bare.mail.post` takes the
recipient rather than a mailbox's name, and a cohort no longer signs with its
run id.

### The dashboard restarts itself onto its checkout's code

A dashboard kept the code it was started with while its checkout moved on,
so once a review's fingerprint changed shape it refused every answer as
though the review had been altered. It now compares the lup files it
imported with the disk every two seconds; once they have moved and settled,
and the new code starts in a fresh interpreter, it re-executes itself in
place when no write is in flight — same pid, port, capability and herald
record, tabs reconnecting. Until then the page, `dashboard status` and the
status line say "dashboard runs older code; restarting", and a new write is
refused with that reason. `dashboard restart` asks for it by hand, and
replaces a dashboard too old to restart itself. The pulse, the status and the
stream carry `RunningCode`; `LiveFeed` and `Herald` take `code`.

### A parked review reaches the operator with no page open

The dashboard's service looks at every queue every two seconds whether or not
a tab follows: each review parked since gets one desktop notice through
`notify-send` naming what waits and where, and where no tab follows the page
it opens the page, at most once per ten minutes, unless `[dashboard] reopen =
false` in the person's lup config (`dashboard reopen --on/--off` writes it).
It publishes its pulse — reviews waiting, sessions, repositories, open tabs,
its address — to a file every launch lends its session read-only
(`LUP_DASHBOARD_PULSE`), which a Claude session's status line shows as `N
reviews pending · <address>` through `dashboard line`, answered before the
project's application loads; Codex has no status line a command fills.
`dashboard status` inside a session reads the pulse rather than reporting
zeros. `Contribution` and `Joined` carry a neutral `StatusLine`.

### `review wait` waits until the review settles

It has no limit of its own and says every ten minutes what it still waits on;
`--timeout` ends it early with exit 3. A Claude refusal asks for the shell
tool's longest timeout and to start the waiter again when it is stopped, since
the tool stops a background command at thirty minutes unless told more.

### A parked review is labelled with the checkout it changes

A session editing a sibling worktree parked reviews labelled with its own
checkout; the parked operation's `worktree` is now the checkout holding every
file the call records, and `Operation.summary` names it.

### Every launch holds one dashboard per person

`harness claude|codex` holds the dashboard as a host companion: the first
launch starts it, every later one — any repository, a child session included
— joins it, and the last one's end stops it. It keeps its port and its
capability across restarts, so an open tab reconnects rather than being told
its access expired, and the capability is withheld from every session. The
page groups reviews by repository and by the session that asked, one producer
serves every tab, and each repository's setup is a pane beside its reviews.
`dashboard open|status|stop` join `serve`; the group is the new `dashboard`
module's, taken by default. `setup dashboard` is the hidden `setup serve`.

### A review whose requester is gone expires

A review expires once the roster saw its requester end, or after an hour
where the roster never knew it, recording why; the dashboard sweeps every ten
seconds and `review list` before it lists.

### Pyright reads the environment the session selected

`dev check`, code intelligence and the rule resolver hand Pyright the
checkout's own interpreter, `UV_PROJECT_ENVIRONMENT` included, and the
scaffold no longer pins `venvPath`/`venv`; `dev migrate pyright-environment`
retires the unchanged pair.

### Browser login is `conversation setup`

The conversation module owns its login, so declining it leaves `setup`
without it; the login and the retention read one profile directory.

### Smaller fixes from the review stack

`dev migrate pending` reports while the application's code does not import.
Preflight hands the host sentinel to the probe observing it and leaves the
launcher's own environment as it found it. The dashboard keeps a
native-provider deferral under Full operation, tells loading from an empty
queue, and names the queue and target checkouts.

### Container sessions drop every capability; sudo only where declared

Every session container runs with `--cap-drop ALL` and `no-new-privileges`,
and its entrypoint starts the agent through `setpriv` with empty inheritable
and ambient sets, so the agent holds no capability and no setuid binary in
the image can hand it one. `OuterContainer(sudo=True)` builds that session's
image with passwordless sudo and gives the container's root back what
administering its own files takes; a rootful engine refuses it, and what sudo
installs vanishes with the container — the image's `tooling` keeps a package.

### A container holds its launch record, and the policy measures containment

The preflight ledger, accepted policy snapshots and mount table are mounted
read-only in the session's own container, with every directory between a
hold and its writable mount pinned, `.lup` and a plain checkout's `.git`
among them. A ledger's claim to a container is believed only where the
dispatcher reads it through that read-only mount, so a ledger a host session
wrote for itself is answered as uncontained. An uncontained launch empties
the mount table in place rather than removing it.

### A contained Codex session runs its hooks from a held revision

The installed plugin revision is written again on the host under
`~/.cache/lup/codex-revisions` and mounted read-only over the plugin's cache
in the session's home, so the session cannot rewrite the hooks judging it.
`lup-codex-plugin --report` prints the revision a preparation installed.

### No launch mounts what the launcher keeps

A mount at, above, or inside `$XDG_STATE_HOME/lup`, `$XDG_CONFIG_HOME/lup`
or the held Codex revisions refuses the launch; `store_exposure` is now
`launcher_state_exposure`.

### Repositories inside the checkout, held as its own

`OuterContainer(nested_repositories=[NestedRepository(path=...)])` holds a
nested repository's `config` and `hooks/` read-only and verifies its
pointers at every launch; `create=True` initializes it on the host first.

### `harness claude|codex` launches the declaration its flags make

The command no longer runs a launch of its own beside the library's: it
builds a `Claude(...)` or `Codex(...)` from this repository's composition and
its flags and calls `launch()`, so `command()` on the same declaration is
exactly the process it runs. Each flag is a field — `--sandbox inner` an
`InnerSandbox`, `--mount`, `--mount-ro` and `sync.json.local` registrations
`Mount`s, `--device` the container's devices, `--continue`/`--resume`/
`--session` `Latest()`/`Pick()`/`Reopen(...)`, `--generate-only` a
regeneration and `prepare()` — and the repository's checkpoint, worktree
pointers, base sync and regeneration are the launch's `steps=`.
`harness codex --profile` now names the account, as on Claude Code; a Codex
configuration overlay is gone, every setting one held being a `Codex(...)`
field. `docs/harness.md` maps every flag, and `examples/launch_*.py` show
every field on its own.

### Each launch declares the tool servers its session carries

Claude Code drops a plugin's own MCP servers under `--strict-mcp-config`, so
the generated plugin carries skills, agents and hooks, and every launch
declares its servers per session — `--mcp-config` on Claude Code,
`--config mcp_servers.*` on Codex — from the declaration's `tools.mcp`. A
server's tools are now named `mcp__<server>__<tool>` on Claude Code rather
than under the plugin's scope; the settings grants and the coordination
caller hook follow. The startup deadline moves to `ServeLaunch`.

### Host companions, held around every session a declaration opens

`companions=` on `Claude` and `Codex` takes services a session wants running
on the host beside it: each is held for as long as the session is open —
launched, printed as a command, or opened in process — and hands it
environment, mounts and ports. `SharedProcess` is one process shared per
checkout or per person, started by the first session, joined by the rest and
stopped with the last lease.

### `/lup:profile` names this machine's profiles

The command's hint lists the profiles the machine keeps, so it is in neither
committed tree: `harness generate` and every launch render it into a
gitignored overlay — `.claude/plugins/local/` and `.codex/skills/`.

### A member's messages wait in its mailbox

What peers say to a session waited in its "inbox". It is the session's
mailbox, and "inbox" names nothing in lup: the `coordination_inbox` tool is
`coordination_mailbox`, `lup-devtools coordination inbox` is
`coordination mailbox`, and the coordination store keeps each member's
mail under `mailbox/` rather than `inbox/`. `ActorInbox` is
`ActorMailbox`, `create_inbox_hooks` is `create_mailbox_hooks`,
`ActorCohort.inbox` is `ActorCohort.mailbox`, `InboxRelay` is
`MailboxRelay`, `INBOX_DIR` is `MAILBOX_DIR`, `inbox_path` is
`mailbox_path`, and the hook matcher that delivers mail is tagged
`mailbox`. End every session before regenerating, and move the mail still
waiting as the migration says.

The two ways mail reaches a member were called `inbox` and `mailbox`,
though mail waits in the member's mailbox either way. Each is named for
what hands the message over: `hook` (`Delivery.HOOK`), where the member's
own hook puts it in front of its next tool call, and `waiting`
(`Delivery.WAITING`), where it waits until the member next looks and
nothing wakes it. `coordination_send` and `spawn_say` report those
spellings, and the member files the store keeps are respelled by the
migration's step.

### A session's wake socket is keyed by its member id

The Unix socket a peer writes to so an idle Claude session takes a turn
was named after the session's display name, which repeats by design and
changes at a rename: a launch met the stale socket of an earlier session
called the same, and a rename left peers holding a path to nothing. It is
now `<dir>/<repository>-<digest>--<member id>.sock`, at most 103 bytes. A
file at a member's own path is replaced rather than refused, and a
departed member's socket is removed only where the roster says it left and
nothing answers on it.

It is also called what it is. This socket holds no mail, so it is the
session's wake socket, in `/tmp/lup-wake` rather than `/tmp/lup-inbox`.
`SessionInboxes` is `WakeSockets`,
`placed_inbox` is `placed_wake_socket`, `Image.inboxes` and
`Member.inboxes` are `wake_sockets`, and `inbox_refusal` is
`wake_socket_refusal`; `cleared`, `UnixSocketRefused` and
`RepositoryPeers.woken_through` are gone. Regenerate so the compiled
policy withholds the new directory.

### A native subagent is a roster row of its own

A session's native subagents inherited its coordination identity, so a
subagent's `coordination_describe` replaced its orchestrator's row, a lock
held a file for the whole session, and a subagent could not reach the
session that dispatched it (#505). Each subagent is now a row of its own
beneath its session's, keyed by the runtime's subagent id under the
session's, named from its spawn — on Claude Code from the spawn's meta
file, on Codex from the `agent_path` atop the subagent's own rollout — and
live while its session is.

A new `PreToolUse` hook on each runtime, matched to the coordination
server's tools, writes the calling conversation into the call's hidden
`lup_caller` argument, so `describe`, `rename`, `lock`, `release` and
`mailbox` act on the calling subagent's row and `coordination_peers` lists
subagents beneath their session. A subagent's edits are held on its row: a
sibling writing there is asked, its own session's claims are not. A native
send between conversations of one session is no longer redirected, and an
address resolves as an id before a name. `PeerPolicy` gains a required
`server`, the tool server the coordination verbs are served from, which
`lup.coordination.policy.peer_policy` fills with `COORDINATION_SERVER`.

### A session is present while its runtime runs

A stopped session read as running (#503). A runtime started from a
session's own shell inherits its coordination id, and its tool server joined
under it like any other: it put a cleanly ended session's row back within
one tick and beat for it for as long as that runtime lived. A session's row
now names its runtime process — the one feeding its tool server's input, by
pid, start time and pid namespace — and a server beats only for a row
naming its own runtime, never puts back a departure written under another,
and ends its row with `its runtime stopped` once that runtime has gone.

Readers ask the runtime itself where they share its namespace, so a killed
session reads as gone at once and a live one stays present, claims and
description whole, across a suspended machine. A reader in another container
tests the `<member>.pulse` lock the answering server holds, and falls back
to the two-minute window only where neither speaks. One server of a runtime
holds the pulse at a time, so the server Codex keeps for each subagent
answers for the session only where the session's own stopped while the
runtime runs on. `SessionNeeds.runtime` and `RosterPulse.runtime` carry the
process, and `create_peer_tools` takes it as `runtime=`.

### `dev check --changed` reads a branch from where it left its base

It diffed against the integration branch's tip, so a feature branch answered
for every commit that branch took after the cut — 108 files for an author
who had touched a handful — and it named only Python files, so a change of
Markdown and CSS reported nothing at all. It now reads from the merge base
with the base the branch records (or with `--since <ref>`), answers for
uncommitted work on the integration branch itself, lists every changed file
no scoped check read, and names the test suites and whole-tree sweeps it
left to `dev check`. It also runs the declared-migrations row from that same
base: two public names removed without a migration had reached the whole
gate because the narrowed run never asked.

### A parked review is answered from the dashboard or `review`

`lup-devtools dashboard serve` opens one loopback page over the parked reviews
of every worktree of the repositories it is given: numbered diffs per file,
the command a shell review would run, the rule and reason, and a history of
answers. Its capability is minted per launch and carried in the URL fragment;
an answer binds to the fingerprint the page showed and the preimages still on
disk, is recorded atomically in the relay a terminal answers too, and then
reaches the requester as mail and a wake. The page never runs the operation.
The terminal verbs leave `dev questions` for a `review` group — `list`,
`show`, `approve`, `decline`, `cancel` — and `review approve|decline` and
`dashboard serve` are operator-only. The policy now records each file's own
verdict beside a many-file edit's, so a review shows which files asked and
which passed; a suppression marker inside a string no longer counts as one,
and `sed -i` reviews preview what the rewrite would leave. `dev edit-prepare`
audits a batch of complete documents and writes one patch without touching
its targets.

### Claude parks a question where a dashboard reads it, and prompts elsewhere

0.4.0's note on native approval authority says a call parks in
`.lup/questions.jsonl` until an operator's single-use answer releases it.
Codex parks every ask, its pre-tool boundary having no ask effect. Claude
parks every ask where the session's launch holds a dashboard
(`LUP_DASHBOARD_URL`), for the session, its subagents and its `-p` runs
alike, refused while it waits and carried out once by `review wait` or one
exact retry. Where no dashboard is held, every Claude ask — a `human_only`
one included — is its native permission request, carrying its reason.

What a rendered ask rests on is the prompt holding until somebody answers.
On Claude Code 2.1.283 an interactive auto-mode session held a hook's ask
unanswered for a minute; on 2.1.263 an auto-mode classifier let one run with
nobody shown it (#436). The hook payload carries no field telling a mode's
answer from a person's, so a held dashboard parks. Observed execution grants
nothing on either runtime; `docs/permissions.md` ("Where a native ask is
put") states both channels.

### The library is published as `lup-agents`

PyPI refuses `lup` as too close to an existing project, so the library's
distribution is `lup-agents`. It is still imported as `lup`, and
`packages/lup/`, `lup-devtools` and the `lup` registration in `sync.json`
keep their names.

A project built on lup renames its requirement by hand, once. No command does
it: the `lup-devtools` a project runs is the library it is about to replace.
In `pyproject.toml`, rename `lup[...]` to `lup-agents[...]` in
`[project].dependencies`, and the `lup` key under `[tool.uv.sources]` to
`lup-agents` — whether it pins a repository or the vendored copy under
`packages/lup`. Until then uv refuses the library. A repository pin moved to a
renamed commit, the way `dev update` moves it, fails with:

```
  × Failed to download and build `lup @
  │ git+https://github.com/joy-void-joy/lup@<commit>#subdirectory=packages/lup`
  ╰─▶ Package metadata name `lup-agents` does not match given name `lup`
```

and a vendored copy renamed under a root that still requires `lup`, on
`uv lock`, `uv sync` or `uv run`, with:

```
  ├─▶ Failed to parse entry: `lup`
  ╰─▶ `lup` references a workspace in `tool.uv.sources` (e.g., `lup = {
      workspace = true }`), but is not a workspace member
```

After the rename, `uv sync` installs `lup-agents`, and a project pinned at a
repository runs `uv run lup-devtools dev update` to bring the generated trees
and the copied half to the commit it now resolves. A vendored copy's editable
install leaves `packages/lup/src/lup.egg-info` beside the new metadata; delete
it, or `importlib.metadata` goes on answering for `lup`.

`DISTRIBUTION` in `lup.devtools.dev.library` spells only the distribution; a
caller that passed it as the sync registration's name passes `REGISTRATION`.

### A migration is one file, and a release keeps its own

A break two trees cannot describe is declared as one TOML file under
`packages/lup/src/lup/migrations/pending/`, where it was an entry in
`lup.devtools.dev.migrations.DECLARED`: every branch appended to that list at
one position, so any two that each broke something conflicted there.
`dev release` moves the pending files into `migrations/<version>/`, stamped
with the commit each break landed in, and keeps them rather than emptying a
list; 0.3.0's and 0.4.0's migrations are recovered into records of their own.

`dev migrate pending` reads every release's record beside the pending one, so
a project updating across several releases hears what each asks of it, and
`dev migrate check` and the `declared migrations` gate read them too, so a
range spanning a release finds what that release declared. A released
migration speaks only for a range its commit landed in: a name an old release
retired, reused and dropped again, is a break of its own, and so is a
project's name that a library release happened to retire too.
`docs/contributing.md` shows a file. `MigrationRecord` reads the record where
`DECLARED` was read, and `undeclared_breaks` takes one as `record`.

### A release can go out as candidates first

`dev release <level> --pre` (`/lup:release --pre`) cuts a release candidate,
tagged `vX.Y.ZrcN`: a PEP 440 pre-release, published to the index and the
forge as one, whose commit's manifest says `X.Y.ZrcN` — so a project pinned
to it by git is told it holds the candidate. N counts on from the tags
already spent on that version, and a
later `--pre` keeps the series' level unless another one is named. The
changelog section stays open, headed by the version the series is heading
for and listing each candidate; work landing after a candidate gathers under
a fresh `## Unreleased` above it until the next candidate folds it in, and
the pending breaks stay pending until the release.

A plain `dev release` while the newest tag is a candidate promotes it, with a
release commit on a branch cut from the candidate's tag (`release-X.Y.Z`)
that changes only the version (`X.Y.ZrcN` to `X.Y.Z`), the changelog section
it closes, and the record of the breaks the candidate carried. Before
anything is committed, every file it changes is checked against the
candidate's tag; anything more is named, undone, and refused, so what ships
is what was tested. The branch is tagged `vX.Y.Z`, lands on the release
branch through a pull request like any release, and is merged back into the
integration branch — which goes on taking work while a candidate soaks, and
keeps it. Where the release branch has moved past the candidate, the command
says so and names the two ways on — another candidate with `--pre`, or the
release of what the integration branch holds with `--direct`.

Nothing takes a candidate by accident. A project opts in by naming it:
`dev library git --tag vX.Y.ZrcN`, or `dev library use published --version
X.Y.ZrcN`, whose requirement naming a pre-release is what lets the installer
take one. `dev library release` names a candidate newer than the release
beside it and never offers it as the version to pin.

The publishing workflow builds the tree a tag names as it stands, and a
second job records each release on GitHub, marked prerelease for a
candidate; it holds `contents: write` and not the index's identity.
`PublishSpec` takes `tag_prefix` in place of `tags`, and
`Changelog.released_as` takes the asks as rendered lines where `released()`
took them.

## 0.4.0 — 2026-09-22

### Native execution carries no reusable approval authority

A native runtime exposes its own prompt's answer to no hook, so the policy
read that answer off the two events a hook does see: the call was asked
about, and then it ran. A call that ran was therefore treated as a call
somebody approved — and Claude's auto mode has executed a native hook ask
with nobody at the keyboard (#436), which makes that inference a grant no
human gave.

Approval memory is gone. `remembered_approval` and `remembered_or_asked` are
removed from both policy adapters, and `note_asked`/`note_ran` now record an
execution as `observed` rather than `approved`. Authority comes only from an
explicit single-use review receipt: the call parks in `.lup/questions.jsonl`,
an operator answers it by its id, and that answer releases one exact retry.
Historical `approvals.jsonl` records carry no receipt and grant nothing;
`dev hooks approvals` and `dev hooks forget` still read and retire those
observations, without changing what is authorized.

A receipt binds what was reviewed, not just the command: captured file
preimages for every path the call would write, the resolved paths, the
originating dispatcher's own bytes and the accepted destination policy. A
changed file, payload or policy re-enters review rather than spending the
old answer, and an execution whose tool or input differs from the reviewed
one marks the receipt `in_doubt` instead of completing it.

Codex answers a blocked review on stdout, as a structured denial carrying
its reason, because the app-server drops `systemMessage` when a hook exits 2
and that reason names the operator who can release the call. Every other
refusal still takes exit 2, so exit status alone no longer separates a
permitted call from a parked one — read the structured answer.

### Native tool authority is explicit

`SessionRequest.tools` and `ClaudeSessionConfig.tools` are `native_tools`,
and the default grants nothing. `None` and `[]` both mean no built-in or
inherited tool authority on either provider; pass an explicit sequence of
`NativeToolGroup` values, or exact supported provider tool names, for what a
session may actually reach. `NativeToolGroup.ALL` is the broad opt-in, and
it is the only way to get what an ambient default used to hand over.

Audit `create_client`, `create_claude`, `create_codex` and any directly
constructed session config that relied on the old ambient grant. `@lup_tool`
handlers in the factory's `tools=[...]` and explicit MCP servers in
`tool_servers` need none of it — an application tool declaration is
independent of native authority. `allowed_tools` selects automatic approval
within declared authority and cannot grant a tool that authority withholds.

Codex compiles the grant independently of its sandbox and applies it before
the process starts rather than per thread, so a startup facility cannot
introduce a tool the session did not ask for. It refuses `READ` and the
exact `Read`, `Write` and `WebFetch` grants rather than approximating them:
`SHELL`/`Bash` grants command execution and `WEB`/`WebSearch` grants hosted
research, and neither silently substitutes for the others. Bounding model
tool metadata needs an explicit or inherited model present in Codex's own
catalog, which is read from the binary when a session opens.

### Codex typed output rides each turn

Typed output was a dynamic tool named `submit_output`, installed on
`thread/start`. `dynamicTools` exists only there and persists for the
thread's life, so a schema that changed mid-conversation could not be
rebound without losing conversation identity — the sequence `None -> A -> A
-> B -> None` was a declared release gap.

Every typed `turn/start` now carries `outputSchema`, so a schema may change
or disappear between turns and the native thread survives it. `dynamic_tool`
and `SUBMISSION_TOOL` are gone; pass the output model and submission gate
through `TurnRequest`, and configure `correction` on `CodexSessionConfig` to
bound validation retries. Handle `StructuredOutputError` for exhausted
validation and `UnsupportedCapability` for native controls Codex cannot
enforce. Native requests use strict mode: compatible object schemas are
closed, and anything else rides a strict `output_json` string carrier with
the original schema preserved in the turn prompt and the original Pydantic
validation applied to what comes back.

The dynamic-tool channel now carries declared application tools alone.
`CodexSessionConfig.application_tools` names `lup_app_*` tools whose handlers
run in the hosting process under `@lup_tool` validation, and the channel
stays silent for a session that declares none. Because it is still
thread-scoped, resuming with a different application tool set is refused —
`CodexSchemaRebindingError` now reports application-tool drift alone, and
output schemas need no fresh thread.

### An edit is judged by the policy of the repository it lands in

An authorized edit into another checkout was judged by the policy of the
session making it. It is now routed to the destination's own policy, bound
to a verified evaluator snapshot, while the caller's boundary and identity
are retained — and it fails closed when the destination policy or its Git
ownership has changed. An operator can accept a changed destination policy
without restarting, through `harness policy-refresh`. A repository with no
explicit grant still meets the foreign-repository ask exactly as before.

`shell_patch` is `lup.policy.kernel.review.literal_input(command,
'apply_patch')`, which reads any literal single-argument or quoted-heredoc
invocation rather than that one executable. `patch_review` takes the
captured preconditions and the shell flag; a parked review never re-reads
the working tree, because the bytes it was answered about are the bytes it
holds.

### Repository identity is the consumer's

The library carried a hosting account and an implicit upstream URL.
`REPOSITORY_URL` and the bare `GitSource` are gone: pass `url` when
constructing `GitSource`, or call `repository_url(root)`. For CLI use, pass
`dev library git --url <repository>`, keep an existing Git dependency pin,
or configure the scaffold's named project in `sync.json.local` with its url
or checkout path. Declare publication URLs in your own package metadata.
Dependency tracker routing follows the configured library source; project
issue routing continues to use its own origin.

### Concurrent resolver joins, and what a resume replays

`JoinDesk` takes the concern it belongs to: construct
`JoinDesk(run_dir, concern_id)`. With the run stopped, move any existing
`join/plan.json` and `join/progress.json` into `join/<concern_id>/` under
the identity recorded in the plan. Dependency checkpoints no longer populate
the integration-only `ResolveState.join_progress`.

Around it, a run survives more of what interrupts it. An unfinished join
parks on a durable question rather than dropping; admission into a running
resolver is queued; a blocker repeated after an answer is retained; an
interrupted integration and an incompatible actor binding are each
reconciled explicitly rather than guessed at; run ownership is kept through
host backoff; and a recheck verdict already settled is replayed on resume
instead of being asked again. `resolve cost` reports run cost derived from
the journal without mutating it, and reports incomplete turn timing as
uncertain rather than as zero.

### Review checkpoints are shared between sibling worktrees

`ProjectEntry.review_from` and `ProjectEntry.last_synced_commit` changed:
path registrations review fetched origin commits by default, and a review
checkpoint is bound to the repository and ref reviewed, recorded under the
common Git directory so every worktree of it reads the same one.

For unpublished local work, set `review_from` to `local` in
`sync.json.local`, or run `sync setup NAME /path/to/repo --review-from
local`; a repository with no origin stays local. Check `sync status` for the
selected review ref — a branchless registration matching the library Git
source follows its consumed branch, and a reported source/branch mismatch is
resolved before reviewing. After review, run `sync mark-synced NAME --at
REVIEWED_SHA` with the immutable commit actually reviewed. An older
`last_synced_commit` remains the seed until that shared checkpoint exists.

### Codex peer wakes name a verified home

Delivering to a Codex session needs the home and execution scope its arrival
hook verified, not a bare command. Call `wake(WakePath(...), message)` rather
than the raw `CODEX_COMMAND`, and retain the native arrival hook's home and
scope when persisting a wake path. `queued` takes that `WakePath` rather than
a thread string; a missing or foreign scope leaves durable mail pending
instead of delivering it somewhere the sender never verified.

### `Runtime.contained` is `Runtime.homed`

`contained` named the configuration home a workspace's sessions are pointed
at, while everywhere else in this library it names a container — and a
session can now ask for one. Call `Runtime.homed(request)` where you called
`Runtime.contained(request)`; nothing else about it moved, and a caller
reaching it through `Runtime.session_factory` was never naming it. To find
the call sites:

```bash
uv run lup-devtools dev py text '.contained(' .
```

### Migrating personal Claude profile callers

Two earlier public API changes have explicit migration records for adopters
updating from before those changes:

- `ClaudeProfileStore` split at `fcb61ade6`. Import `AccountFile`,
  `ClaudeProfileNames`, and `ClaudeProfileRegistrar` from
  `lup.providers.claude.profile_store`. Construct one
  `accounts = AccountFile(registry_path)` and share it between
  `ClaudeProfileNames(accounts)` and `ClaudeProfileRegistrar(accounts)`.
  Registry reads, writes, home resolution, and `resolver_registry()` belong to
  `accounts`; `names()`, `config_dir_for()`, and `active_profile()` belong to
  the names reader; `add_profile()`, `set_active()`, and `remove_profile()`
  belong to the registrar. A CLI composes these with
  `ProfileDirectory(names, registrar, CLAUDE_LOGIN)` from
  `lup.providers.profiles`. The existing registry and account homes remain valid.
- `CLAUDE_CONFIG_DIR` moved at `da46c6bb2`. Import it from
  `lup.providers.claude.login`, which also declares `CLAUDE_LOGIN`; use
  `CLAUDE_LOGIN.environment(config_home)` when constructing a launch environment.
  The former `lup.adapters` namespace is `lup.providers` in the current API.

`dev update` reports these migration steps when the previous pin predates the
corresponding change. Run `uv run lup-devtools dev check` after adapting callers
to catch remaining imports and capability mismatches.

### A session an application opens can be walled

`SessionRequest` could say how much a session may do and nothing about what
confined it, so an application composing `Client` reached neither boundary
the launcher already knew how to open: the sandbox settings each adapter
carried were reachable only by building that adapter's configuration by
hand, and the wrapper that runs a CLI inside a container was reachable only
as a resolver actor's.

`containment` is that axis, in the launcher's own three words. `inner`
establishes the runtime's own sandbox, `outer` starts the runtime as the
program named in `contained_program` and stands its own sandbox down inside
the container, and `none` is the default — what every request meant before
the field existed, so nothing an adopter holds changes until it asks.

The two runtimes render it into what each has. Claude keeps autonomy and
containment in two fields that decide nothing about each other. Codex has
one field for both, and takes the narrower of what the wall asks and what
the autonomy implies, so neither can widen what the other narrowed.
`contained_cli` writes the program an `outer` request names, for either
runtime, defaulting to the mounts that hold a session to the tree it was
given.

### The base an adoption is rooted at is measured

`dev scaffold adopt --base` decides every merge after it and nothing checked
it, so two wrong answers passed in silence. Rooting at the commit the library
pin already resolves to leaves the merge base and the merge target one
commit: the first `dev update` reports `0 fast-forwarded, 0 merged clean, 0
conflicted` and every change nobody hand-ported stays untaken. Rooting at an
initialization stamp a year back re-offers a year of already-applied changes
in a layout the project has left.

The compiled scaffold is a pure function of upstream and the checkout is
right there, so the base is now *measured* rather than trusted: each
candidate is compiled and its files compared byte for byte against the
project's own, and a copy stamped from one commit reads highest at that
commit. `dev scaffold fit` is that reading on its own — every commit that
changed the copied half is in range, read at descending resolution, so
sixteen hundred candidates answer in seventy-eight measurements. `adopt`
refuses a base equal to the pin, and one a candidate reads a tenth of the
copied half above; both refusals carry the reading, and restating it as
`--accept-fit <identical>` is what roots the branch there anyway.


### What this release asks of a caller

- command_words — lup.policy.rules.command_words passed its argument to the kernel's own command_words and returned the result, so one reading of a command line had two spellings and the rules module renamed the kernel's on import to make room for its copy
-   Import command_words from lup.policy.kernel.words, which is where every other caller already reads it and where the behaviour has always lived. Nothing about what it returns changes.
- remembered_approval, remembered_or_asked — Native execution is observation, not reusable approval authority; both native dispatchers require explicit single-use review receipts.
-   Remove approval-memory lookups from policy adapters. Keep explicit reusable grants in their declared policy scope; historical approvals.jsonl records have no such receipt and grant no authority. note_ran records observed execution only.
-   Regenerate both plugins. For unresolved native asks, inspect the named review with dev questions show and answer or reject it from an operator terminal. A recorded answer releases one exact retry; native auto-mode and execution cannot answer it. dev hooks approvals and forget inspect or retire observations without changing authorization.
- JoinDesk, JoinDesk.__init__ — Concurrent resolver joins require concern-owned checkpoint directories.
-   Construct JoinDesk(run_dir, concern_id). With the run stopped, move any join/plan.json and join/progress.json into join/<concern_id>/, using the identity recorded in the plan. Dependency checkpoints no longer populate the integration-only ResolveState.join_progress field.
- ProjectEntry.review_from, ProjectEntry.last_synced_commit — path registrations review fetched origin commits by default and review checkpoints are shared across sibling worktrees, bound to the repository and ref reviewed
-   For unpublished local work, set review_from to local in sync.json.local, or run sync setup NAME /path/to/repo --review-from local. A repository with no origin remains local. Remote review fetches without moving the checkout.
-   Check sync status for the selected review ref. A branchless registration matching the library Git source follows its consumed branch; resolve any reported source/branch mismatch before reviewing commits.
-   After review, run sync mark-synced NAME --at REVIEWED_SHA with the immutable commit actually reviewed. This records the checkpoint under the common Git directory for all worktrees, without fetching an existing upstream. An older last_synced_commit remains the seed until that shared checkpoint is recorded; changing the repository or ref requires reviewing and recording a checkpoint for that source.
- ClaudeProfileStore, ClaudeProfileStore.homes_root, ClaudeProfileStore.load_registry, ClaudeProfileStore.save_registry, ClaudeProfileStore.resolver_registry, ClaudeProfileStore.resolve_config_dir, ClaudeProfileStore.names, ClaudeProfileStore.config_dir_for, ClaudeProfileStore.active_profile, ClaudeProfileStore.add_profile, ClaudeProfileStore.set_active, ClaudeProfileStore.remove_profile — personal Claude profile storage, reading names, and curating accounts are separate collaborators; the registry file format is unchanged
-   Import AccountFile, ClaudeProfileNames, and ClaudeProfileRegistrar from lup.providers.claude.profile_store. Construct accounts = AccountFile(registry_path), then pass the same accounts to ClaudeProfileNames(accounts) and ClaudeProfileRegistrar(accounts). Keep homes_root, load_registry, save_registry, resolver_registry, and resolve_config_dir calls on accounts; call names, config_dir_for, and active_profile on the names reader; call add_profile, set_active, and remove_profile on the registrar.
-   For CLI profile selection, compose ProfileDirectory(names, registrar, CLAUDE_LOGIN) from lup.providers.profiles and lup.providers.claude.login. Preserve the existing registry path and account homes; no credential or data migration is needed.
- CLAUDE_CONFIG_DIR — the Claude configuration-home declaration belongs to its login adapter
-   Import CLAUDE_CONFIG_DIR from lup.providers.claude.login instead of the former lup.adapters.claude.config module. When building a launch environment, prefer CLAUDE_LOGIN.environment(config_home) from the same module.
- CODEX_COMMAND — Codex queue delivery must select the target's verified home and execution scope.
-   Call wake(WakePath(...), message) rather than the raw CODEX_COMMAND. Retain the native arrival hook's home and scope when persisting a wake path. queued now takes that WakePath, not a thread string; missing or foreign scope leaves durable mail pending.
- dynamic_tool, SUBMISSION_TOOL — Codex typed output uses a per-turn native schema and portable validation, so submission no longer installs a thread-lifetime dynamic tool. The dynamic-tool channel carries declared application tools only.
-   Pass the output model and submission gate through TurnRequest. Remove direct dynamic_tool use for submission; declare application tools in CodexSessionConfig.application_tools, which DynamicToolCall still carries. Untyped turns and changed output schemas need no fresh thread, so CodexSchemaRebindingError now reports application-tool drift alone.
-   Configure correction on CodexSessionConfig to bound validation retries. Handle StructuredOutputError for exhausted output validation, and UnsupportedCapability for native controls that cannot be enforced.
- runtime_of — Vendored execution environments are declared under the runtime they belong to, so nothing has to read a runtime back out of a path.
-   Read the runtime from the key it is declared under: VENDORED_EXECUTION_ENVIRONMENTS maps each runtime to its environment. A caller that sniffed one out of a root string was answering a question the declaration now states, and its first entry is no longer the fallback for a root naming no runtime.
- shell_patch, patch_review — Native hook reviews bind captured documents and policy bytes; Codex approvals remain single-use.
-   Regenerate both native plugins. Replace direct shell_patch callers with lup.policy.kernel.review.literal_input(command, 'apply_patch'). Pass captured preconditions and the shell flag to patch_review; never re-read the working tree for a parked review.
- SessionRequest.tools, ClaudeSessionConfig.tools, create_client, create_claude, create_codex, SessionRequest, ClaudeSessionConfig, CodexSessionConfig — Native tool authority is explicit. The native_tools default None grants no built-in or inherited tools on either provider; an application tool declaration remains independent of that authority.
-   Rename SessionRequest.tools and ClaudeSessionConfig.tools to native_tools. Audit create_client, create_claude, create_codex and direct session configs that relied on ambient tools: pass an explicit sequence of NativeToolGroup values or exact supported provider tool names. Use NativeToolGroup.ALL only where broad built-in authority is intended; None and [] both grant nothing.
-   Keep @lup_tool handlers in the factory tools=[...] argument and explicit MCP servers in tool_servers. Neither requires native_tools. allowed_tools selects automatic approval within declared authority and cannot grant a missing tool. Remove inherited setting sources and provider overrides that could widen authority. Codex rejects READ and exact Read, Write or WebFetch grants; use its supported facilities only when their broader semantics are intended.
-   Resume a Codex thread only with the same application tools and compatible native authority; native grants may narrow on resume. Start a fresh session when application tools change: the native resume protocol cannot replace dynamic tools. Output schemas ride each turn and need no fresh thread. Codex requires an explicit or inherited model present in its native catalog to bound model tool metadata.
- REPOSITORY_URL, GitSource.url — Repository identity is configured by each consumer; the library carries no hosting account or implicit upstream URL.
-   Pass url when constructing GitSource. Replace imports of REPOSITORY_URL with repository_url(root), or supply your own URL. For CLI use, pass dev library git --url <repository>, keep an existing Git dependency pin, or configure the scaffold's named project in sync.json.local with its url or checkout path.
-   Declare publication URLs in your package metadata when needed. Dependency tracker routing follows the configured library source; project issue routing continues to use its own origin.
- sync.json, ProjectEntry.url, ProjectEntry.mount — a tracked registration declares what the project needs -- which repository a name means, whether the project can work without it, and what a session may reach of it -- while the machine's file answers where it is and which transport gets there. The scaffold's `lup` entry is required and mounted, because the workflows that fix a defect upstream and derive a relocation map over its history cannot start without that checkout
-   Say where lup is on this machine, once: `sync remote lup <url>` for the URL this machine fetches it from, or `sync setup lup /path/to/repo` for a checkout it already has. `sync fetch` then materializes it, and a launch materializes what it also mounts. Until one of them is done, `sync status` names the requirement and exits nonzero rather than reporting `not cloned` in a column.
    uv run lup-devtools sync status
-   Move a machine-specific clone URL out of a local `url` override and into `remote`, which is what a transport is now called: `sync remote <name> <url>`. A tracked `url` is the repository's identity, compared on the host and path it names, so an ssh clone of an https registration is one repository and no longer a refusal; two repositories under one name still are, and the refusal names the file and the edit.
-   Declare a repository the project cannot work without on the tracked entry, with `"required": true`, and the mode a session may open it at beside it. Both keys stay written or absent, never defaulted; a tracked mount binds nothing until this machine says where the project is. A repository registered at this checkout's own origin, and a committed requirement read inside the template scaffold, are owed by nobody here.
- Runtime.contained — `contained` named the configuration home a workspace's sessions are pointed at, while everywhere else in this library it names a container — and a session can now ask for one. The method takes the word for what it does, `homed`, and the boundary keeps the other
-   Call `Runtime.homed(request)` where you called `Runtime.contained(request)`; nothing else about it moved. A caller reaching it through `Runtime.session_factory` was never naming it and has nothing to change.
    uv run lup-devtools dev py text '.contained(' .
- BranchBase.notice — `notice` narrated the base a worktree had already been cut from, which is advice nobody can act on without an undo. A base the command cannot guess is settled before the branch exists now, and `refusal` is what says so: a message the command exits on rather than one trailing a worktree that is already there
-   Read `BranchBase.refusal()` where you read `BranchBase.notice()`, and exit on it: it is empty wherever the base is settled, and where it is not it names both spellings of `--base` for the caller to re-run with.
    uv run lup-devtools dev py text '.notice(' .
-   Pass `branch` when you construct a `BranchBase`, which the refusal names the contested branch by, and `ahead` from `commits_ahead(current, integration)`, which is the measurement deciding whether the two bases differ at all.

## 0.3.0 — 2026-09-19

Breaking reorganisation of the library's top level. Thirty-four entries became
twenty by asking of each one which of four kinds it is: a foundation that
imports nothing else here, a subject, the one vendor boundary, or tooling.
Every import path an adopter holds is affected, and the migration is derived
rather than written. Both ends of the range are read out of git, so one command
prints the exact `dev relocate` invocation that repoints a checkout from
whichever commit it stands at:

```sh
uv run lup-devtools dev migrate map c564bc01a..
```

Derived rather than pasted here for the reason the reorganisation itself gives:
a hundred pairs written down go stale the next time one module moves, and a
list confidently wrong is worse than one that had to be looked up.

| Was | Is | Why |
| --- | --- | --- |
| `lup.adapters` | `lup.providers` | the boundary named for what sits behind it, not for the pattern |
| `lup.runtime` | `lup.sessions` | one of four modules called `runtime`; the engine is about a session's turns |
| `lup.runtime.contracts` / `.models` / `.wrappers` | `lup.sessions.capabilities` / `.events` / `.middleware` | each named for what it holds |
| `lup.runtime.profiles`, `.profile_tree`, `.login`, `.session_home`, `.selection`, `.routing`, `.config` | `lup.providers.*` | which runtime answers is the provider's question, not the turn engine's |
| `lup.mcp`, `lup.tool_policy`, `lup.tool_routes`, `lup.codeintel` | `lup.tools.mcp`, `.policy`, `.routing`, `.lsp` | one entry for what an agent acts with; `codeintel` shared eight characters with `codescan` on the opposite side of the system |
| `lup.journal`, `lup.telemetry.*`, `lup.replay.journal`, `lup.usage`, `lup.runtime.usage` | `lup.observability.journal`, `.audit`, `.trace`, `.display`, `.metrics`, `.blocks`, `.native`, `.replay`, `.usage`, `.cost` | four entries answered "what happened", and `journal` named four different things |
| `lup.actors`, `lup.jobs.runtime`, `lup.realtime`, `lup.subagents`, `lup.reflect`, `lup.runtime.background` | `lup.orchestration.*`, with `reflect` → `reflection` and `jobs.runtime` → `jobs` | five entries answered "run work concurrently" |
| `lup.resilience`, `lup.runtime.threads`, `lup.gitlocks` | `lup.execution.resilience`, `.threads`, `.writability` | what carrying work out runs into; `gitlocks` asks a filesystem question that git's `config.lock` is only the first caller of |
| `lup.hooks` | `lup.policy.hooks` | the hook seam is part of the permission subject |
| `lup.codescan` | `lup.harness.codescan` | the rule engine reads the harness declaration models it judges |
| `lup.selection` | `lup.tables` | it is about narrowing a library table, not selecting a runtime |
| `lup.gitguard` | `lup.devtools.gitguard` | catching a test suite writing outside its fixtures is development tooling |
| `lup.harness.banner` | `lup.banner` | a foundation both the harness and the policy bundle write |
| `lup.client` | `lup.sessions.client`, `lup.providers.routing`, `lup` | one module was the type every consumer holds *and* the router that reaches both vendors |
| `lup.gitlocks` → `lup.execution.writability`, and `lup.devtools.utils.git` | `lup.execution.shell` | a configured `git` the mount rail and the harness both reach, moved below both |

`lup.channels`, `lup.types`, `lup.markdown`, `lup.workspace`, `lup.web`,
`lup.sandbox`, `lup.policy`, `lup.harness`, `lup.resolver` and `lup.devtools`
keep their names. `channels` in particular stays a top-level foundation: it
imports nothing but `lup.types`, and folding it in with `workspace`
manufactured a cycle between storage and observability.

`from lup import Client, create_client, Provider` is unchanged — the package
root still exports the whole front door, and `create_client` is declared there
now rather than one module down. What moved is where a consumer that bypassed
the root has to look: `Client` is `lup.sessions.client`, and the model-id
routing is `lup.providers.routing`, which is the vendor edge that was already
keeping a matcher vocabulary of its own.

**One capability is gone rather than moved.** `PROVIDER_PREFIXES`, a
`dict[str, Provider]` matched longest-prefix-first, is replaced by
`PROVIDER_ROUTES`, a `list[ProviderRoute]` matched in declaration order and
carrying a `ModelMatcher` rather than a bare prefix — so an adopter can name a
model exactly, or write a matcher of its own, where a prefix says the wrong
thing. A caller passing `prefixes=` passes `routes=` instead, and writes the
narrower entry above the broader one rather than relying on a sort nothing on
the page mentioned. Every other export in the ledger resolved to its new home.

- Modelled the payloads a literal dictionary key was reading by hand: a Codex
  `turn/completed` notification, the two spellings a delegation names its role
  under, what a retrieval call names as its source, Claude's project section,
  and the REPL wire protocol, now declared once in the half that runs in the
  container. `PayloadText` in `lup.types` carries the fact three of them
  share.
- Named the shapes that replace an open string map in the `dict-str-payload`
  and `dict-str-object` diagnostics, so a denial points at a frozen id model,
  a declared route list, or `EnvVars`/`StringMap` rather than at a
  suppression.
- Derived the permissions page's settlement order from the kernel that reads
  it, and the supersession gate now reads its answer from the domain it
  publishes rather than from a constant pair declared beside it.
- Version directory names parse with `semver`, so an experiment arm's
  `+build` suffix orders with the release it came from, and `resolve_version`
  takes the counter and the word for what it counts as overridable defaults.

### The coordination store is state

Four append-only logs folded whole by every reader on every call became one
file per member, written by that member's own processes under its own lock,
with every relation between members derived at the read: presence is the
file's modification time, a claim carries the modification time of the path it
was taken over and is settled by a stat, and a contest is two live members'
files meeting on one path. Mail is one file per message in one inbox, consumed
by deletion, so no reader keeps a position.

Broadcast splits into the two acts it always was. A message has one recipient,
so a sender that means everyone resolves it against the roster; a standing
fact is state — read at the head of a turn for as long as it holds, retracted
by deleting it, and reaching a session that starts tomorrow.

Eight migrations are declared over the hundred and fifty-three names this
moves; `uv run lup-devtools dev migrate map` prints what to call instead.

### A module's prose is one file

Prose a content module composes is authored as Markdown beside it. A module
composing several passages marks them off inside that one file with
`<!-- passage: name -->` and names which it is placing, so a subject is one
file rather than eleven and a passage is named for what it holds rather than
for its position in a list.

`passage_path` takes the module alone — which passage is `Passage.name`, read
off the section markers rather than off a second filename. How many newlines a
rendered document ends on moved from `sectioned` to the renderer, which is the
one reader that sees a whole document whatever kinds of part composed it.

### A peer that is idle can be woken

`wake()` finishes the job itself on both runtimes rather than handing the
caller an instruction on one of them. Every Claude session runs a private
inbox socket, and lup passes `--messaging-socket-path` for every session it
launches, so a member declares the handle that wakes it when it joins and a
sender writes a frame there carrying that member's session id. A session lup
did not launch is still reached through the runtime's own default paths.

The handle is declared by the adapter for the runtime that would use it,
because what a handle *is* differs by runtime: on Codex it is the thread
`codex queue` takes, which nothing hands a server Codex starts, so that
adapter declares nothing and the outcome is reported rather than skipped.

A wake is always *on top of* the mail and never instead of it, so the record
is identical on both and only the latency differs. The agent never touches the
channel: `coordination_send` remains the one habit.

### Two rules read prose, where seventy-four read shapes

A comment could date the code beside it and a docstring could name a symbol
that resolves to nothing, and every gate stayed green. Both are claims a
machine can settle, so both are refused.

`historical-voice` refuses a phrase that can only be about a prior state —
`previously`, `formerly`, `renamed from`, `used to be`, a design named as the
one before this one, a migration something is *during*, an audience described
as needing compatibility — and a bare issue number, which stands in for a
reason the reader cannot follow. Bare `used to` and `no longer` are
deliberately absent: both are overwhelmingly present tense, and what
separates the real ones is the subject. `used to` after a pronoun can only be
past habitual, because the present reading needs an auxiliary the pronoun
form has nowhere to put — so *it used to filter* is refused and *a key used
to select a home* is not.

`stale-reference` resolves every fully-qualified symbol a docstring names and
refuses the ones reaching nothing. It is a project rule rather than an edit
rule because deciding it means importing, which the hermetic kernel cannot
do. Scoped to what resolves without context: a bare name is answered by the
module it sits in, and an attribute of a class is as often an annotation as a
binding, so reading only what is bound would report a declaration missing.

Prose is a third scan surface beside the comment and code ones, so a
docstring is read as sentences rather than blanked with every other string.
An adopter's own tree meets both rules at its next `dev check`; either is
suppressed at the site with `# lup: ignore[<rule>]` and a standing reason.

### The rest

- A contained session binds a checkout once. Where a repository keeps its
  worktrees beneath its own Git directory, that directory's mount already
  reaches every one of them at the same path and in the same mode, and the
  second bind cost what no session could undo: a mount point is not removable
  from inside its own namespace, so `git worktree remove` emptied the checkout
  and left the directory standing. One collected per landed branch. A mount
  whose deepest enclosing mount carries it the same way is dropped, which
  leaves a read-only `config` inside a writable share exactly where it was.
- `git delete` reads containment by patch-id, the way the sweep that asks for
  it does. Ancestry called a branch unmerged whenever its work landed rebased
  or squashed — which is most of what a sweep clears — so cleaning up after a
  landing demanded `--force`, the flag for discarding work. Where a branch
  does hold something, the refusal counts the commits sharing a subject with
  the integration branch and names `git cherry -v`, since that is the trace a
  rebase leaves and it is a signal rather than a verdict.
- A `# lup: ignore` inside a code span is prose about the convention and
  silences nothing. Documenting the escape declared one, of a rule named after
  whatever the example used as a placeholder, and put an approval in front of
  the sentence explaining it. The same reading the note scanner already
  applied to a marker now answers for a directive, in every language.
- `dev release` empties the declared-break list into a form the release after
  it can read. It wrote the annotated assignment and looked for the bare one,
  so a release crashed on the output of the release before it, with the
  changelog and version already written — a failure that could only arrive one
  release late. The round trip is pinned now, since a release's own output is
  the next release's input.
- `dev release` regenerates before it commits. The version is a source a
  generated artifact compiles from, so bumping it leaves those trees behind
  and the commit guard refuses — which made the release the one commit this
  repository could not make. What the bump regenerates lands in the same
  commit as the bump.
- `dev comments --retire` asks before deleting a claimed-resolved note. It is
  the one step of the verify-solved pass nothing undoes, and `/lup:release`
  runs that pass before cutting, so no claim reaches a version unverified.
- The migrations gate measures from the release, which is the tag rather than
  the release branch. The two part for as long as it takes a release to land,
  and read from the branch in that window every break a release had just
  shipped came back undeclared — against a list that is empty precisely then,
  because emptying it is what the release did.
- Each pytest root runs under a base temporary directory of its own. The gate
  runs both at once and neither named one, so each scanned `pytest-of-<user>`
  for the next free `pytest-N` and two runners starting together were handed
  one tree. What that looked like was a fixture meeting a path another suite's
  test had made, on a machine with too few cores to spread the runs apart.
- `ledger migrate` copies a kind's journal lines and blobs into the placement
  its mapping now declares, for a kind moved after records already exist. The
  source lines stay: the committed journal is merged by git's union driver, so
  a deletion there would not even be durable, and the fold already reads each
  record once.
- `ledger index-notes` records a closed session per directory under an
  existing `notes/` tree, and one output per result, through the same writers
  a live run uses — so a backfilled record is the same two records a live run
  leaves. Idempotent by `(checkout, path)`.
- Every parsed command carries the directory it runs in, and a segment's path
  words resolve by position rather than by value, so a relative operand after
  a `cd` is judged against the file it would reach.
- An in-place rewrite nothing produced says which reading stopped it: a path
  naming no file, a target that is not a regular file, a script `sed` would
  not run, and text that could not be read each carry their own recovery.
- A global in front of a subcommand is consumed before the subcommand is read.
- A contained session reads the clock in the operator's zone, filled from the
  file the machine keeps it in rather than from a `TZ` a Linux host exports
  for nobody.
- Every workflow job names the pinned runner image, held to it by a sweep over
  `.github/workflows` rather than by whoever remembers.

### What this release carries no migration for

A repository declares which of its subtrees it publishes nothing out of, in
`DevProject.internal_modules`, and the preservation gate walks what is left.
Two are declared here, and what went from them is not a break an adopter can
meet:

- **`lup_template`**, the scaffold. `dev init` copies this half into the
  adopting repository and renames it, so its names arrive there as that
  project's own source rather than as an import. Thirteen went, among them
  `build_session_toolset`, the five `*_GROUP` toolsets, and the `library_*`
  commands.
- **`lup.policy.kernel`**, compiled into the hermetic dispatcher each
  generated plugin runs and reached there as bare `kernel.*`. Forty-seven
  went, nearly all of them the hand-rolled shell tokenizer — `ShellToken`,
  `Lexeme`, `Scan`, `tokenize_shell`, `read_heredoc_bodies` — and the
  control-flow readers beside it, replaced in place.

A project that imported either subtree was reaching past what this repository
publishes. The declaration sits in the catalog with its reasoning, so the
judgement can be read and argued with rather than inferred from a gate that
quietly stopped firing.


### What this release asks of a caller

- agent_roster_text, topic_bullets, UpstreamReport.section — a roster and a report section are a list and a document rather than joined text: each is parts now, so a name or a description carrying a newline cannot end the bullet or the heading it was written into
-   Compose the parts instead of the string: `agent_roster_bullets(agents)` from `lup.harness.content.catalog` answers with a `BulletList`, and a report's section is `section(report)` from `lup.harness.content.docs.upstream_reports`, a `Passage`. Where the text itself was wanted, `part.text_payload` reads it back. `topic_bullets` has no replacement: its one caller builds a `BulletList` from `REPORT_TOPICS`.
- WorkflowSpec.body, WorkflowSpec.install_step, PublishSpec.body — a generated workflow is a declared document rather than a formatted string: the steps are `WorkflowStep` declarations and the file is a `YamlDocument`, which is emitted and parsed back before it is written, so a runner label or a command carrying a colon can no longer end the mapping it lands in
-   Read the YAML off `spec.document().text()` where `spec.body()` was read, or take the whole artifact from `spec.artifact()` as the generator does. A project that appended its own step by formatting text around `body()` declares a `WorkflowStep` instead and overrides `steps()`; `install_step` is `install_steps()`, which answers with a list rather than a block of YAML.
- MarkdownCell, MarkdownCell.text, MarkdownCell.render — the base every generated Markdown leaf answers through is an inline node rather than a table cell: the same escaping is what a heading compiled from a catalog and a value inside a sentence need, and a name saying `cell` said the table was the only container there could be
-   Import `InlineNode` from `lup.formats.markdown` where `MarkdownCell` was imported; `text` and `render` are unchanged, and every concrete kind — `PlainCell`, `CodeCell`, `HtmlCodeCell`, `LinkCell` — keeps its name and its behaviour.
- PYTHON_SUFFIXES, MARKDOWN_SUFFIXES, JS_SUFFIXES, JSON_SUFFIXES — the marker scanner routes a file by one dict from suffix to scan mode, where four parallel tuples each guarded an arm of a match that decided on nothing the subject carried
-   Read `SCAN_MODES` from `lup.harness.codescan.markers` where one of the suffix tuples was read: its keys are the suffixes and its values the `ScanMode` each routes to, so the suffixes of one mode are the keys whose value is that mode.
- AntiPatternSet, AntiPatternSet.python, AntiPatternSet.typescript, AntiPatternSet.for_suffix, AntiPatternSet.selected, antipattern_set_for — the set holds every rule a project is judged by — the line rules, the project rules the sweep runs, and the composition rule generation runs — so it is named for what it holds
-   Import `RuleSet` from `lup.harness.codescan.antipatterns` where `AntiPatternSet` was imported, and `rule_set_for` where `antipattern_set_for` was; the fields and methods keep their names, and `project` and `composition` stand beside `python` and `typescript`.
- AntiPattern.id, AntiPattern.examples, AntiPattern.message, AntiPattern.refinement, AntiPattern.strength, AntiPattern.examples_bound_the_rule, STRUCTURAL_RULES, anti_pattern_rules — every rule is one declaration: what every rule states moved to the `Rule` base that `AntiPattern`, `ProjectRule` and `CompositionRule` share, and the reference derives every card from the set instead of keeping the structural ones by hand
-   A caller constructing or reading an `AntiPattern` changes nothing: the fields are inherited. One that read `STRUCTURAL_RULES` or `anti_pattern_rules()` reads `all_rules()`, which derives every card. A project declaring a rule the sweep decides declares a `ProjectRule` from `lup.harness.codescan.project` beside its audit and adds it to its set's `project` list.
- member_environment — a launcher mints a session's name beside its id, numbered against the live sessions of the repository so two sessions in one worktree are reachable apart, and exports the two together
-   Where `member_environment(member_id)` was exported, call `launched_member(root)` from `lup.coordination.repository` and export its `.environment()`; a caller holding an id and a name of its own builds a `LaunchedMember` and exports that.
- NATIVE_SPELLING_RULE_ID, KERNEL_IMPORT_RULE_ID, LIBRARY_DEFAULT_RULE_ID, CONSTANT_DECLARATION_RULE_ID — the boundary scanner's rule ids are one enumeration: several audits share that module, and a constant apiece said the same thing as many times as there were suppression comments naming it, so `RuleId` says it once
-   Read the id off `RuleId` instead: `RuleId.NATIVE_SPELLING`, `RuleId.KERNEL_IMPORTS`, `RuleId.LIBRARY_DEFAULT` and `RuleId.CONSTANT_DECLARATION`, all from `lup.harness.codescan.boundaries`. Note the plural in the second, whose constant was singular. Each carries the same string it always did and the type is a `StrEnum`, so a comparison against a rule id read from anywhere else -- a directive, a deny message, the generated reference -- holds without being converted.
- changes_runtime_source, prompt_command, prompt_entry, prompt_artifacts — the roster's prompt hook became one guard serving the arriving and the departing event alike, so none of these names a thing that is only about the prompt any more, and each has to be told which script and which runtime module it is building for
-   Call `runtime_source(module)` for `changes_runtime_source()`, `guard_command(plugin_root_env, guard_script)` for `prompt_command(plugin_root_env)`, and `hook_entry(plugin_root_env, guard_script)` for `prompt_entry(plugin_root_env)`. The added argument is which guard the entry points at, and a caller that had one hook passes the script it was already generating.
-   Call `roster_artifacts` for `prompt_artifacts`. It keeps `plugin_root`, `semantic_id` and `event` and adds `guard_script`, `runtime_module`, `source_file` and `origin` -- what the one prompt guard used to hold as its own constants, passed now that two events render it.
- CallToolResultWithAlias, CallToolResultWithAlias.is_error — in-process MCP servers are built on the mcp 2.x server API, whose result already answers to `is_error`; the subclass existed only to alias `isError` onto the snake-case spelling an SDK query runner looked for, and it has nothing left to translate
-   Drop the subclass and let the server's own result stand. A caller that built one to get the alias reads `is_error` off the response envelope instead -- which is what a `@lup_tool` handler's failure already crosses as, and what `mcp_response(text, is_error=True)` sets.
- refuse_a_blocked_registration — a merge-driver registration a contained session cannot make no longer refuses the worktree: the registration is the host's once-per-clone act, and the checkout is usable without it, so the pre-flight reports the gap and answers whether it is blocked instead of stopping
-   Call `report_a_blocked_registration` where the refusal was called; it returns whether the registration is blocked, which a caller that made the worktree conditional on it reads instead of catching the exit.
- repository_worktrees, WriteFacts.worktrees, ShellContext.repository_worktrees, redirected_verb_only_reads, redirect_stays_in_this_repository — a git directory redirect is a value flag and nothing more: the verb behind `-C`, `--git-dir` and `--work-tree` is judged by its own row in the other tree, as `cd there && git <verb>` always was, so the guard that asked about the redirect and the worktree list it was measured against have nothing left to decide
-   Drop the `repository_worktrees` argument from any call into `decide_shell`, `classify_shell` or `shell_context`, and the `worktrees` key from a `WriteFacts` a project builds by hand; a project's own shell vocabulary needs no change, and a git rule that listed the directory flags under `ask_flags` keeps them under `value_flags` alone.
- approval_fingerprint, approval_receipt_root, record_approval, spend_approval, uncorrelated — a queue approval is bound to the exact call, session, checkout and file preimages and spent once on retry, so the receipts that stood in for that correlation — written per pending prompt, and matched by nothing narrower than a prompt — have nothing left to answer
-   Nothing outside the dispatcher called these. A project that carried its own copy of the Codex dispatcher asset takes the library's again by regenerating: the receipts directory it wrote under the session root is no longer read and can be removed.
    uv run lup-devtools harness generate all
- LibraryMode.LINKED, read_linked_path, link_library — an editable install of a lup checkout moved the library with no command run in the project and nothing written down, so the pin, the copied half and the generated trees had nothing to be held at one commit against
-   A project resolving lup from a path pins the branch carrying its changes instead, which moves under a command and records the commit it moved to.
    uv run lup-devtools dev library git --branch <branch>
- build_session_toolset, tool_group_names, ServerGroup — assembling a session's tool groups was the same work in every project built on lup and went stale in each of them separately; what a project decides is which groups it carries, so the assembly is the library's and the list is the project's
-   Replace the copied `build_session_toolset` with a list of `ToolGroup`s: name `coordination_group()`, `ledger_group(...)`, `sandbox_group()`, `codeintel_group()` and `realtime_group()` from `lup.tools.toolsets` rather than rebuilding them, and write a builder for each group of your own. A builder that has nothing to build for a session returns nothing, which replaces every `if` that asked whether the session had a sandbox or an identity.
-   `tool_group_names(realtime=...)` is derived now: `served_names(groups, needs)` for one session, `startup_names(groups)` for the servers a runtime starts when a session opens.
-   A `--server` option typed as the `ServerGroup` literal takes a plain string and is checked against the declared names, since a project's groups are its own.
- capture, operations, CAPTURE_FILE, CapabilityKind, CapabilityKind.COMMAND, CapabilityKind.EXPORT, Capability.kind, SurfaceCapture.commands, SurfaceCapture.read, SurfaceCapture.write — a surface stored in the tree ages against the walker that reads it — the checked-in one had already stopped seeing type aliases — while git holds every revision, so both ends of a range are read rather than remembered
-   `dev preserve capture` and its fixture are gone: nothing is stored. `dev migrate map <base>..<head>` derives the relocation, `dev migrate check` refuses a capability that went undeclared, and both read the surface out of git.
    uv run lup-devtools dev migrate map <base>..
-   A command is no longer walked as a capability of its own. It is declared by a function whose name is in the surface already, so a command that goes takes its declaration with it.
- standing, unsaid, gone, member_of, RepositoryPeers.pulsed, RepositoryPeers.rewound, RosterRecord.applied, ActorSpawned.applied, ActorJoined.applied, ActorDescribed.applied, ActorFinished.applied, TouchRecord.claim, TouchRecord.applied, PathTouched.claim, PathTouched.applied, PathContested.claim, PathContested.applied, PrefixLocked.claim, PrefixLocked.applied, PrefixReleased.claim, PrefixReleased.applied, PathVacated.claim, PathVacated.applied, stream_records, peer_members, peer_heard, peer_present, peer_name_claims, peer_addresses, peer_listing, claim_covers, peer_claims — the coordination store was folded by three hand-kept readers that shared no import — the typed library, the prompt-time hook and the compiled permission dispatcher — so every record the store gained had to be taught to each separately, and one that missed a record went on answering confidently about a store it no longer understood; there is now one fold, `lup.coordination.bare.store`, which every reader imports and each plugin ships
-   Fold the roster with `store.members(roster_path)`, and read it as the pulses and resets leave it with `store.present(root)`, which applies both. What a record makes of a member is `store.applied` and is no longer a method on the record: `ActorJoined` and its siblings are writers now, and the fold takes them off disk without importing them.
-   `RepositoryPeers.pulsed` and `.rewound` are `store.pulsed` and `store.rewound`; `RepositoryPeers.present()` applies both already and is what a caller wanted from either.
-   Claims fold with `store.claims(root)`, narrow to live holders with `store.held(root, live)`, and answer a path with `store.covering(root, path, live)`. `Claim.covers`, `.subject` and `.vacant` still answer for a typed caller, over that same fold.
-   The dispatcher's half is gone from `lup.policy.assets.host`: `peer_addresses` and `peer_listing` are `store.addresses` and `store.listing`, `claim_holders` is `store.claim_holders`, `record_claims` is `store.record_claims`, and each takes the coordination directory `peer_store` still resolves rather than a project root and a list of file names.
- PeerPolicy.roster_file, PeerPolicy.names_file, PeerPolicy.touches_file, PeerPolicy.member_kind, PeerPolicy.heartbeats_dir, PeerPolicy.stale_after_seconds — the compiled dispatcher imports the shipped fold, which owns every file name the store is made of, so a policy restating them was the second spelling that could drift
-   Drop those six from any `PeerPolicy(...)` you build. What is left is what the fold cannot know: `store`, the path parts beneath the shared git directory; `windows_dir`, the dispatcher's own snapshots; `member_env`; and the five lines a stopped caller reads. A project that moved its store still says so with `store`.
- roster_artifacts, DEPARTURE_ORIGIN, DEPARTURE_SOURCE — a plugin carries the coordination half as one package rather than a loose file per hook, because the two hooks and the dispatcher all stand on the same fold
-   `roster_artifacts` is gone. `store_artifacts(plugin_root, semantic_id)` places the package under `hooks/runtime/coordination/`, and is called unconditionally: it takes no hook set, because the dispatcher imports the package whatever a project declared about rosters. `hook_artifacts(...)` places one event's guard and the entry beside the package that it runs.
- HOOKS_MANIFEST, CodexHookEvent, CODEX_HOOK_EVENTS, declared_hook_records, untrusted_hooks — which hooks a Codex home would run is the runtime's verdict over its own records and the plugin cache those records name, so it is asked rather than reconstructed — the hand-kept event table knew three events while the generated manifest declares five, and the one call that decides whether a session carries the policy raised on the two it had never heard of
-   Ask the home instead of naming its records. `read_hooks(home, cwd)` in `lup.providers.codex.trust` returns a `CodexHookReport`, and each `CodexHook` in it carries the `key` the record is kept under, its `current_hash`, `trust_status` and `is_managed` — so nothing composes a record name from `HOOKS_MANIFEST` and `CODEX_HOOK_EVENTS`, and no table has to be kept level with the manifest.
-   Replace `untrusted_hooks(home, marketplace)` with `policy_hooks_skipped(home, project, marketplace)` from `lup.providers.codex.home`, which answers the same question for the working directory a session opens on. It returns the `CodexHook`s themselves rather than record names, and covers a hook whose recorded digest has gone stale — the `modified` verdict a table of names could not see, and the one a regenerated plugin meets constantly.
-   `CodexHookEvent` named the three events the table knew. Nothing narrows an event any more: `CodexHook.event_name` is whatever the runtime reports, so an event added to the manifest needs no change here.
- schema_digest_drift — one regeneration of the app-server schemas answers two questions — whether the shapes the typed models were read from have moved, and whether the reply hook trust is seeded from still carries the fields it is read by — and a second invocation for the second question could answer about a different version
-   Call `schema_reading(contracts)` from `lup.devtools.harness.doctor` where `schema_digest_drift()` was called. It takes the `WireContract`s the composition declares — `NativeHarnessComposition.wire_contracts`, empty for a runtime that depends on no reply by field name — and returns a `SchemaReading` carrying `digests` and `contracts`. The digests are what the old call returned; `findings()` renders both as the messages to print, and `drifted()` answers whether anything moved.
- Woken.instruction, Handover.instruction, Delegation.instruction — a wake is finished by the library on both runtimes now that a Claude session is reached by writing to its own inbox socket, so the outcome where the caller had to finish the job with a tool this library does not hold no longer happens
-   Drop the field from anything that read it. A wake now either reached the peer or did not: read `reached` for which, and `reason` for why not. Code that printed `instruction` beside `note` should print `note` alone, and code that branched on `instruction` being set has one branch fewer.
- Finished, Finished.actor, Finished.at, Finished.error, Finished.summary, Finished.type, HEARTBEATS_DIR, Held.digest, Look.conversation, NAMES_FILE, NameRecord, NameRecord.at, NameRecord.cli_name, NameRecord.id, Named.id, RESETS_DIR, ROSTER_FILE, RosterRecord, RosterRecord.actor, RosterRecord.at, RosterRecord.delivery, RosterRecord.description, RosterRecord.error, RosterRecord.liveness, RosterRecord.summary, RosterRecord.task, RosterRecord.type, RosterRecord.wake, RosterRecord.worktree, TOUCHES_FILE, TouchRecord, TouchRecord.actor, TouchRecord.at, TouchRecord.digest, TouchRecord.path, TouchRecord.prefix, TouchRecord.rivals, TouchRecord.type, Touched, Touched.actor, Touched.at, Touched.digest, Touched.path, Touched.rivals, Touched.type, appended, applied, arrival, beat_path, claims, heard, heard_at, name_holders, named, narrowed, reset, reset_at, reset_path, stamp, stamped_at, touch_prefix, touched_claim, vacant — the coordination store is one file per member and every relation between members is derived at the read, so the four append-only logs and the stamp directories beside them are gone: presence is the member file's modification time, a claim is settled against the path it names, a contest is two live members' files meeting, and a name is on the member that answers to it
-   Read the store through `lup.coordination.bare.store`: `present(root)` for who is here, `held(root, live)` for what they hold, `called(root)` and `naming(root)` for what each is called, `member_of(root, id)` for one. Nothing folds a record any more, so `appended`, `applied`, `arrival`, `heard`, `named`, `narrowed` and the record types they folded (`RosterRecord`, `NameRecord`, `TouchRecord`, `Finished`, `Touched`) have no replacement and need none.
-   Write through the typed verbs, never a record: `Roster.joined`/`describes`/`finished`/`beat` for a member, and `store.revised(root, id, revise)` for a bare writer that must read before it writes. A member file is revised under its own lock, so a caller appending to a shared log now revises one member's file instead.
-   Presence is the file's modification time. `beat(root, id)` touches it and creates nothing, so `heard_at`, `beat_path`, `stamp`, `stamped_at` and `HEARTBEATS_DIR` are gone; read `heard` off the member instead, which `present` fills in from the stat it already takes.
-   A rewind clears the member's own `description` and records the conversation it now belongs to, in one revision, so `reset`, `reset_at`, `reset_path` and `RESETS_DIR` are gone and so is `Look.conversation` — which conversation a row describes is on the row. The prompt fold does it; a caller doing it itself writes `conversation` and an empty `description` through `store.revised`.
-   A claim carries the modification time of the path it was taken over rather than a digest of it, so `Held.digest` and `claims(root)` are gone: `standing(claim)` asks the filesystem, and `held(root, live)` reports only what still holds. `vacant`, `touch_prefix` and `touched_claim` have no replacement — there is nothing to vacate and no record to build.
- MemberNamed, MemberNamed.at, MemberNamed.cli_name, MemberNamed.id, MemberNames, MemberNames.__init__, MemberNames.called, MemberNames.current, MemberNames.latest, MemberNames.named, MemberNames.rename, MemberNames.resolve, NAME_ADAPTER — a name is on the member that answers to it, with the names it answered to before beside it, because the two were only ever read together and a separate log made a rename an event about a member rather than the member changing
-   Call `RepositoryPeers.rename(member_id, cli_name)` for `MemberNames.rename`, `.called(member_id)` for `.current`, `.answering(cli_name)` for `.resolve`, and `.names_taken(except_id)` for `.called` — which now answers with the names live sessions hold, keyed by name. `store.naming(root)` is every claim any member ever made on a name, oldest first, for a caller that wants the history `MemberNames.named` gave.
-   A rename takes the store's `ROSTER_LOCK`, because a name is the one decision made against every other member's file. A caller writing one itself takes that lock around reading what is taken and writing what it chose.
- Claim.digest, Claim.vacant, PathContested, PathContested.digest, PathContested.rivals, PathContested.type, PathTouched, PathTouched.digest, PathTouched.type, PathVacated, PathVacated.prefix, PathVacated.type, PrefixLocked, PrefixLocked.type, PrefixReleased, PrefixReleased.type, TOUCH_ADAPTER, TouchEntry, TouchRecord, TouchRecord.actor, TouchRecord.at, TouchRecord.path, Touches, Touches.__init__, Touches.claims, Touches.contested, Touches.covering, Touches.held, Touches.locked, Touches.record, Touches.released, Touches.touched, Touches.vacated — a claim is evidence rather than an assertion: it records the modification time of the path it was taken over, so a reader settles it with a stat instead of waiting for a record to retire it, and a contest is derived from two members' files rather than guessed at by whichever of them wrote
-   Call `RepositoryPeers.touched(member_id, *paths)` for `Touches.touched`, `.lock`/`.release` for `Touches.locked`/`.released`, and `.held()`/`.holding()` for `Touches.held`/`.covering`. Each writes the claim onto that member's own file under its own lock; there is no `Touches` to open and no record to append.
-   Nothing records a contest: `Touches.contested` and `PathContested` are gone, and a reader derives it from two live members claiming one path. A caller that named rivals records its own claim and lets the reader meet the other.
-   Nothing ends a claim either: `Touches.vacated`, `PathVacated`, `Claim.vacant` and `Claim.digest` are gone, because a path that is no longer there — or that somebody has written since — answers for itself at the read.
- ActorCohort.say_all, ActorDelivery.through, ActorMessage.run_id, DELIVERY_DIR, EVERYONE, MESSAGE_FILE, QuestionMailbox.delivered, QuestionMailbox.send, QuestionMailbox.waiting — mail is one file per message in one member's inbox, consumed by deletion, and the token meaning everyone is resolved by the sender rather than matched by every reader — which is what forced a delivery position per member, and what made a redirect stop workers spawned after the stop
-   Call `ActorMail.send(to, text, door=..., sender=..., in_reply_to=..., redirect=...)` with the member rather than building an `ActorMessage`: resolving what an operator typed is the roster's, through `ActorCohort.post(address, ...)` or `RepositoryPeers.send(address, ...)`. `ActorMessage` keeps every field but `run_id`, which is `sender`.
-   Commit a delivery with the delivery: `ActorMail.delivered(actor, delivery)` deletes exactly the files it was handed, so `ActorDelivery.through` and every offset beside it are gone, and a message posted between the read and the commit is no longer consumed unseen.
-   Say what you mean by everyone. `ActorCohort.notify(text)` posts a standing notice — state, read at the head of every turn, reaching members spawned afterwards — and also sends it to whoever is live; `ActorCohort.redirect_all(text)` stops whoever is working and nobody else. `EVERYONE` is gone from the store, so nothing matches it at read.
-   Reach a member's inbox through the cohort rather than the question mailbox: `QuestionMailbox.send`, `.waiting` and `.delivered` are gone, because addressing needs a roster the mailbox does not hold. `run_cohort(mailbox, run_id)` from `lup.resolver.mailbox` builds the cohort over that run's own mail and journal.
- ActorDescribed, ActorDescribed.description, ActorDescribed.type, ActorFinished, ActorFinished.error, ActorFinished.summary, ActorFinished.type, ActorJoined, ActorJoined.delivery, ActorJoined.liveness, ActorJoined.task, ActorJoined.type, ActorJoined.wake, ActorJoined.worktree, ActorSpawned, ActorSpawned.task, ActorSpawned.type, RosterRecord, RosterRecord.actor, RosterRecord.at — a member is a file rather than a fold of arrival, description and departure records, so the records have nothing left to be: announcing twice finds the first file standing and leaves it, which is what the fold's idempotence was for
-   Construct `Roster(directory)` rather than `Roster(root / ROSTER_FILE)`: it holds the store's directory now, not one file inside it. `joined`, `spawned`, `describes`, `finished` and `beat` keep their signatures; `Roster.stream` and the record types it carried are gone.
- RepositoryPeers.heard, RepositoryPeers.standing, RepositoryPeers.touches, RepositoryPeers.vacant — what a repository's peers answer is read from the files rather than from a record beside them, so the methods that reconciled the two have nothing to reconcile
-   Read `RosterMember.heard` for `RepositoryPeers.heard`, which the fold fills from the member file's own stat. `standing()` is gone, because there is no reading of the record apart from the pulse; `present()` is the one answer, and `lapsed()` is what a sweep would retire.
-   Use `RepositoryPeers.touched`/`lock`/`release`/`held` for what `.touches` gave, and drop `.vacant()`: a claim over a path that has gone stops standing at the read, so nothing collects them to end.
- Arrived.consumed, Arrived.messages, DELIVERY_DIR, EVERYONE, MESSAGE_FILE, Message.to_actor, committed_offset, framed, reaches, reader_name — the peer delivery reader stands on the shipped store package instead of restating the format it reads, so it moved beside the compiled dispatcher — the other file type-checked against the generated tree it is shipped into — and the maildir left it with no position to keep and no address to match
-   Import `lup.providers.claude.assets.peer_delivery_runtime` for `lup.providers.claude.peer_delivery_runtime`, and run the emitted copy under `hooks/runtime/` rather than the module: it names its own directory as a search path and imports `coordination.mail` as a sibling, which resolves only there.
-   The inbox is the position, so `committed_offset`, `framed`, `Arrived` and `reader_name` are gone, and `reaches` with them — one directory per member means there is no address left to match. `deliver(root, member_id)` keeps its signature and its fail-open contract.
- stale_window — a claim window is this session's own before-and-after and nobody else's reader, because a change it cannot attribute is one each session records for itself and a reader derives the contest from
-   `close_claim_window(root, store, windows_dir, mine)` answers `{"paths": [...]}` and takes no `stale_after_seconds`: it no longer reads anybody else's window, so `stale_window` has nothing to judge. `store.record_claims(root, mine, paths)` takes the three arguments that are left.
- rewritten_files — one function had two names on the two sides of the boundary it answers across, and the row type beside it said `File` where the field it fills says `documents` — what a rewrite leaves behind is the document, the file being the path it lands at
-   Call `rewritten_documents` where `rewritten_files` was called; the arguments and the reading are unchanged. `RewrittenFileRow` is `RewrittenDocumentRow` and `UnreadFileRow` is `UnproducedDocumentRow`, whose field on `RewriteReading` is `unproduced` rather than `unread` — the word `unread` stays with the shell write nobody read the content of, which is a different question.
- SpawnedActor, SpawnedActor.actor, SpawnedActor.address, SpawnedActor.arrived, SpawnedActor.delivery, SpawnedActor.description, SpawnedActor.error, SpawnedActor.heard, SpawnedActor.kind, SpawnedActor.liveness, SpawnedActor.running, SpawnedActor.summary, SpawnedActor.task, SpawnedActor.wake, SpawnedActor.worktree — a repository peer is a session somebody started in a checkout, which nothing here spawned: the word belonged to a cohort's workers and read as false on the roster that carries most of these rows, where the fold's own source is a member file
-   Import `RosterMember` from `lup.coordination.roster` where `SpawnedActor` was imported. Every field keeps its name and its meaning; only the type is spelled for what it folds, which is `store.Member`.
- Claim, Claim.at, Claim.covers, Claim.held, Claim.holders, Claim.path, Claim.prefix, Claim.subject, folded_claim — `Claim` named two shapes one import apart: a member's own record of a path it holds, and the cross-member row derived from every member claiming one path. The first is `store.Holding` and the second is this, so each says which it is
-   Import `HeldPath` from `lup.coordination.touches` where `Claim` was imported, and `folded_held_path` where `folded_claim` was. The fields are unchanged: a path, whether it is a prefix, and the members holding it. A caller that meant one member's own record wants `lup.coordination.bare.store.Holding` instead.
## 0.2.0 — 2026-07-23

Breaking capability-composition and semantic-policy release. A clean break:
remove legacy imports rather than wrapping them, because no runtime
compatibility facade exists.

| Removed surface | Replacement |
|---|---|
| `Engine.client()` / `Client.session()` | adapter `create_*_session_factory(config)`, then `SessionFactory.open()` |
| `Client.query()` / broad `query(**options)` | `SessionFactory.query(prompt, OutputModel)`, or the free `query(factory, prompt, OutputModel)` alias |
| `Client.stream()` / `ReplayStream` | optional `TurnHandle.events`; completed `TurnResult.blocks` |
| old `Session.send(text)` | `handle = await Session.start(turn_request(text))`, then `await handle.turn.result()` |
| `Session.interrupt()` | optional `TurnHandle.interrupt.interrupt()` |
| `LupResponse.output(Model)` | strict `TurnResult[Model].output` |
| `output_schema` / `output_format` | `TurnRequest(output_type=Model)` and turn-bound `submit_output` |
| `Engine.profiles()` / `Profile.select()` | adapter `ProfileSelector.session_factory(base, name)`, or `transform(name)` plus immutable `ConfigTransform.apply()` |
| `Engine.background()` / `BackgroundDriver` | `runtime.background.BackgroundAgent(factory, state_to_request, …)` |
| `Engine.builtin_tools()` / provider tables | adapter `NativeEventDecoder` plus semantic events |
| `claude-compat` / `openai-compat` engines | `ClaudeCompatibilityTransform` / `CodexCompatibilityTransform` |
| `LupAgentOptions` | component-owned `ClaudeSessionConfig`, `CodexSessionConfig`, wrapper configs, `TurnRequest` |
| `ConsumeTracker`, `INTENT_KNOBS`, `refuse_unconsumed()` | Pydantic validation on the component owning each setting |
| global `ENGINES` / mutable `MODEL_ROUTES` | immutable `ModelRoute` values and explicit recipes |
| `adapters.tools.names` | semantic policy models; native names stay private to decoders and renderers |
| `lup-devtools claude` | `lup-devtools harness claude` |
| `lup-devtools claude usage` | `lup-devtools usage` |

- Replaced engine/client/options service locators with narrow `SessionFactory`,
  `Session`, `Turn`, event, interrupt, steer, fork, binding, render, launch, and
  policy capabilities.
- Added strict typed turn-bound `submit_output`, whole-logical-turn wrappers,
  debounced background scheduling, immutable routing, profile transforms, and
  Claude/Codex concrete factories.
- Added `SessionRequest.effort`, so reasoning effort is asked for in portable
  words and rendered by `CLAUDE_EFFORT`/`CODEX_EFFORT` the way autonomy already
  was. Both adapters already carried an effort field and passed it to their
  provider, but no request could reach either, so an application that set one
  silently ran at whatever the runtime's own configuration file said. The two
  ladders meet on `low`–`xhigh`; `minimal` opens at Claude's floor and `max` at
  Codex's ceiling, and Codex's `none` is withheld because Claude would render
  it as `low`.
- Moved Codex execution to typed live app-server JSON-RPC and records the
  current dynamic-tool rebinding limitation explicitly.
- Added a single Pydantic harness catalog, deterministic Claude/Codex artifact
  compilation, validation, safe reconciliation, ownership manifests, hermetic
  semantic policy dispatchers, Codex cache verification, and named launchers.
- Added the persisted DAG resolver with question brokering, isolated leases and
  worktrees, semantic multi-parent joins, bounded review, integration,
  verification, final review, and cleanup records. Native entries scan and
  organize inline notes through the shared Python core without modifying the
  user's checkout.
- Added the project-wide `abc-capability` AST rule and typed suppression audit.
- Added the semantic shell decision lattice: erased rule tables judge every
  command, subcommand, and flag tier; unjudged work denies with a
  `# lup: escalate:` recipe; loops, conditionals, case arms, subshells, brace
  groups, and `$(...)` substitutions classify recursively over frozen variable
  bindings; `find -exec` payloads, `timeout`/`nice` wrappers, read-only
  `sed`/`awk`/`curl` screens, and quoted heredocs are judged in place; segments
  join deny > ask > defer > allow.
- Added launcher-verified OS-sandbox awareness: a `HookSet` sandbox declaration
  compiles into settings, launch, and doctor; unjudged work defers to the
  active sandbox boundary, a `dangerouslyDisableSandbox` escape re-enters the
  deny lattice, and Codex launches establish the interactive sandbox envelope.
- Added the Codex guidance flavor: shared template sections render both
  `TEMPLATE_CLAUDE.md` and a native `TEMPLATE_AGENTS.md`, with intentional
  differences recorded in docs/platform-differentiation.md.
- Added `# lup: defer[<wake condition>]:` parked-work notes with wake-gated
  clearing; tracking files are retired and `dev check` stays red while any
  deferred note exists.
- Renamed the downstream registry to `sync.json` with a documented contract
  (docs/template.md) and a legacy fallback.
- Added human-owned file protection compiled from the hook catalog: README.md
  edits always ask and never auto-allow.
- Added generated-artifact provenance banners with ownership documentation
  (docs/harness.md), and extracted the resolver entry and hook
  dispatchers into real source assets.
- Retyped the harness catalog around annotated domain types, decomposed the
  harness CLI into composition, drift, reconcile, doctor, resolve, and launch
  modules, and gave anti-pattern rules token-masked syntactic contexts shared
  by the auditor and the hook kernel.
- Hardened the resolver entry's argument normalization and pinned
  worker-crash, revision-exhaustion, and join-conflict recovery legs.
- Added the pre-commit generation gate and the native-nightly workflow
  (deterministic evidence checks plus secrets-gated live smokes), and audited
  the test suite for load-bearing coverage.
- Removed all legacy engine, client, broad options, profile, background-driver,
  replay-stream, and provider-wide tool-registry modules. There is no legacy
  facade.
- Fixed `LocalProcessLauncher` to capture through pipes so git output stays
  plain regardless of the host pager configuration, and made resolver resume
  treat the persisted phase as a monotonic high-water mark so hard-killed runs
  recover from every mid-phase kill window.
- Fixed a Claude launch to name every plugin directory the checkout carries
  rather than only the compiled one, so a project's hand-written plugin loads
  from its own tree instead of through a marketplace name — one global
  namespace whose winner is whichever checkout registered it last.

This was a clean breaking release with no compatibility facade: every removed
surface above has a replacement in the current API, which
[docs/library.md](docs/library.md) describes directly.
