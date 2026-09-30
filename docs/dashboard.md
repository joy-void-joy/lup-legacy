<!-- Generated from lup.harness.content.docs.dashboard by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Dashboard

The `dashboard` module gives the operator one page over every session, in every
repository they work in: what each session and its subagents are doing, what
they said to each other, a box to write to any of them, the reviews they
parked, and each repository's setup. Declining it removes the `dashboard`
commands and the service every launch holds; `review list`, `show`, `approve`,
`decline`, `cancel`, `wait`, `reply` and `propose` remain, and a parked call is
answered from the terminal.

## A parked call, and the session waiting on it

A session whose launch holds the dashboard parks every question its policy
asks, on Claude as on Codex, for the session, its subagents and its `-p` runs
alike: the call is written to the review queue and refused while it waits.
Where no dashboard is held, Codex parks all the same, answered from the
terminal, and Claude puts every question to its own permission prompt
(`docs/permissions.md`, "Where a native ask is put").

The refusal is written for the agent: the call is queued as review `<id>`,
not refused; it is not to be changed; the agent carries on and starts
`uv run lup-devtools review wait <id>` in the background, which wakes it with
the result. `review wait <id>...` waits for every review named, `--any` for
the first of them, and with none named for every review this session and its
subagents have waiting; it reports each as it settles. An approved one it
carries out itself, inside the session's own shell and sandbox: an edit is
written as the after-document the operator saw, only where the file still
stands as the review recorded it — otherwise it reports the conflict and
writes nothing — and a command runs in the directory recorded with it, its
output the waiter's own. So the session is woken with "ran", "applied",
"declined" or "stale", and never retries the call; beneath the verdict,
whatever it is, come the operator's note and line comments, each as
`path:line[-end]: note`. The command runs in a fresh shell: what the
session's shell did since, a `cd` or an exported variable, does not reach it,
since the policy judged it standing alone. A call a shell cannot carry out —
one placed outside the session's sandbox, or a tool that is not a write or a
command — is reported as approved, for one exact retry the hook allows once.

The operator can also write without deciding: a note and line comments sent
alone. That ends the waiter too, as "commented", with the words beneath it,
the review still pending, and the command that waits on it again. The agent
answers on the review with `uv run lup-devtools review reply <id> <text>`,
which the page shows in the review's thread — only the session that asked
may — or cancels it and asks again.

A review whose recorded files moved before anybody approved it can never be
carried out, since the waiter writes and runs only where every recorded file
stands as recorded. It is retired into `stale` the moment that is noticed —
by the waiter, which reads the files as the session does; by the dashboard,
on its next look, when the review is opened and when it is approved; and by
`review list` — and the session is told which file moved, to re-read it and
ask again.

The waiter carries out only a review this session asked, by the id its
runtime gave the session or the roster member its launch named, reads the
answer where only the operator writes it, and takes the claim a retry would,
so an approval is spent once whichever comes first. On Claude, a background
command's end re-invokes the session. On Codex a command the shell tool
started keeps running after the turn and its end starts none — measured on
0.158.0 — so the waiter queues its report into the session's thread with
`codex queue` when it settles, which starts a turn in an idle session.

A waiter waits as long as the operator takes: it has no limit of its own,
and every ten minutes it says which reviews it still waits on. `--timeout
<seconds>` ends it early, exit 3, carrying nothing out and naming the
`review wait` that waits again. A runtime can still stop it: Claude Code's
shell tool stops a command at its `timeout`, thirty minutes in the background
unless the call names more, two hours at most. So the refusal asks for the
longest the tool takes and names the waiter with `--timeout 7140`, which ends
it a minute sooner with that line; and a waiter sent SIGTERM or SIGHUP with a
review still pending says the same before it exits. On Codex the waiter
queues that line into the thread as it would a verdict. A review whose waiter
is gone is not lost either way: the operator's answer then goes to the
session's mailbox, naming the `review wait` that carries it out.

The waiter is the requester's one channel. Where a `review wait` holds the
review — or already put the news to its session — the dashboard mails nothing
beside it; only where none does is the answer, the remark or the staleness
mailed to the requester and its wake tried. Where a subagent asked, the
review records it (`agent`), its own row is the requester, and whenever the
operator said something — a note, line comments, a remark — the session it
runs in gets a copy, marked `[copy]`: the subagent handles the call, its
session anything past it. A bare approval pings nobody but the waiter.

## Proposing a batch of edits

Several edits the operator has to see, parked one call at a time, are
answered one at a time — each against a file the next edit then moves. A
session writes them under scratch instead, each at its path in the checkout
(`tmp/cdx/<path>`), tests them there, and runs
`uv run lup-devtools review propose tmp/cdx --why "<what it is for>"`, which
parks one review holding every file. Each file meets the edit gates a direct
write of it would meet — protected paths, anti-patterns, markers, size — and
the review carries each verdict; a file the gates refuse outright refuses the
proposal, naming it, and nothing is parked. A file they would let through is
carried all the same, folded on the page where it needs no reading. The
directory's `.proposal.json` notes files and names deletions, which have no
document to write:

```json
{"about": {"packages/app/module.py": "the new entry point"},
 "delete": ["docs/retired.md"]}
```

The page shows the proposal as one multi-file diff with the `--why` beside
the policy's reason and each note on its file's header. An approval releases
every file or none: the waiter writes them only where each still stands as
recorded, into place together, creating new files and removing deleted ones;
a file moved since stales the whole proposal. Declined, the line comments
send the agent back to its scratch copy, and it proposes again. Codex's own
multi-file `apply_patch` parks as one operation the same way and reads
through the same view.

## One service for every session

Every `harness claude` and `harness codex` launch holds the dashboard as a
host companion (`lup.launch.companions`). The first launch starts it on the
host, on `127.0.0.1:8766` or the next free port; every later one — in any
repository, a session a session launched included — joins it and registers
its repository; it stops once the last session holding it ends, however that
session ended. `--generate-only` holds nothing. It runs as the operator's: the
identity and boundary of whichever session started it are left behind. A
session launched from inside a container starts none there, where it would
serve nobody: the host's already reads that checkout's reviews, and the child
is handed its address.

Its address survives a restart. The port it was given is kept while it stays
free, and the capability that opens the page is minted once and kept in
`$XDG_STATE_HOME/lup/companions/user/dashboard/`, readable by the operator
alone and never handed to a session, so a tab left open while the dashboard
restarts reconnects on its own. The page opens in a browser the first time
that capability is minted, for a launch the operator made; afterwards
`uv run lup-devtools dashboard open` opens it. Each launch prints the address
and hands it to its session as `LUP_DASHBOARD_URL`, credential-free.

A launch from a checkout whose dashboard differs from the one running replaces
it for everyone, the sessions already holding it keeping their hold, so the
page is the newest one launched. One running that does not answer its
launcher's health check within five seconds is replaced the same way. A start
has a minute to answer, since a checkout whose environment has never run it
compiles every module it imports first; one that does not is stopped and
refuses the launch, which the sessions already holding it survive (below).

The page and every script and stylesheet it names are read whole as the
dashboard starts, and served from memory: read again while a rebuild has
written the page but not yet its scripts, until the page names nothing the
bundle lacks. So an open tab is never handed a page naming scripts a rebuild
has since removed, and the dashboard keeps serving after the worktree that
started it is removed. A request for an asset the page it serves does not
name — a tab still holding a page from before a restart — is answered 404 in
words, and the page itself says "This page is out of date — reload it"
wherever its script fails to load, rather than staying blank.

While any launch holds it, the launches keep it running. Each launcher looks
every second, on a thread of its own, whether the process it holds still
runs; whichever finds it gone first records how it ended — when, its exit
status or the signal that ended it, where anything can still collect it, and
the last forty lines of its output, which its log beside its state keeps
whole — and starts it again from its own checkout's code, under the slot's
lock, so exactly one does. It comes back on the same port and with the same
capability, and every open tab reconnects on its own. An exit soon after the
start before it is one more in a row: the launches wait a second before the
first start again, then five, then thirty, then a minute between tries. That
covers every way it ends while held that nobody asked for — a crash, a signal
from outside lup, a replacing launch whose own start then failed — and a
lease a release judged gone while its launcher still runs, which that launcher
takes back, starting what the release stopped. For five minutes after, the
page's banner and every status line say "restarted after it stopped:" and
why. The operator's `dashboard stop` is the one stop they leave alone: it is
recorded in the dashboard's state as the operator's (`stopped.stays`), the
launches holding it stand down, and every session's status line and
`dashboard status` say "stopped by the operator; `dashboard restart` starts
it", until `dashboard restart` or the next launch starts it. Every stop lup
makes — the last lease let go, a launch replacing it, the operator — is
written first into its log
(`$XDG_STATE_HOME/lup/companions/user/dashboard/output.log`) and its state:
which path, on whose behalf, and how many live leases held it, so a log that
ends in a clean shutdown with no such line was stopped from outside lup.

It also follows the checkout it was started from. A process keeps the code it
imported, and that checkout moves every time something lands on it: once a
review's fingerprint is spelled differently there, sessions park reviews the
old code cannot answer. So every two seconds the dashboard compares the lup
files it imported — and every file of the page it serves — with the disk,
reading a file again only where its size or time moved. When they have moved, it waits for
them to settle (a look finding them as the last one did), starts the new
code once in a fresh interpreter (`python -m lup.devtools.dashboard.service
--probe`), and, once no write is in flight, stops serving as it would for a
stop and replaces itself with the same command in the same process: the same
pid, port and capability, the herald's record kept, and every open tab
reconnecting on its own. Until then it says so — "dashboard runs older code;
restarting" on the page, in `dashboard status` and in every session's status
line — and refuses a new write with that reason, rather than refusing an
answer as though the review had been altered. Where the new code does not
start, it stays on the code it runs, says "its newer code does not start" with
the error, keeps taking answers, and tries again once the files move again.
The pulse names the code it runs (`code.source`, a digest of those files, and
`code.since`).

`uv run lup-devtools dashboard restart` does the same now, onto its checkout's
code as it stands. Where none runs while sessions hold it — their launchers
predate keeping it running — it starts one for them from the checkout the
command runs in. A dashboard that predates restarting itself is replaced
instead: stopped, and started from that checkout for the sessions holding it.

`uv run lup-devtools dashboard status` says whether it serves, where, for how
many sessions, over which repositories, how many reviews wait, how many tabs
follow it, which code it runs, and how many times the sessions holding it
started it again; from the operator's terminal it also gives the last exit
(`exited`: when, how, and its last lines of output), the last stop lup made
(`stopped`: why, by which process, and the live leases it counted), and, while
none runs, when the sessions start it again. Inside a session it reads what
the dashboard publishes, its pulse (below), never the operator's private
state. `dashboard stop` stops it now, and it stays stopped, the sessions
holding it included, until `dashboard restart` or the next launch starts it.
`dashboard open`, `dashboard stop`, `dashboard restart`, `dashboard serve` and
`dashboard reopen` are the operator's, run from a terminal outside every agent
session.

`dashboard serve` serves one in this terminal instead, with a capability of its
own, over the current repository or each `--root <checkout>` named, until
Ctrl+C. `--host` picks a loopback address, `--port` its port, and `--no-open`
keeps the browser closed.

## When no page is open

The dashboard every launch holds looks at every queue it serves every two
seconds, whether or not a tab follows the page, and makes a review that parks
visible three ways.

**A desktop notice**, once per review, naming what waits and where: the
review's title — the file it changes or the command it runs — the session
that asked, and the checkout and repository, with the page's address. Where
more than three park at once, one notice names them all. It goes through
`notify-send`, the freedesktop notification service's own client, to
whichever notification daemon the desktop runs. Where `notify-send` is not
installed — a desktop without libnotify, a headless host, macOS — no notice
is shown, the dashboard's log says so once, and the other two carry on. What
it told is kept beside its state, so a restarted dashboard tells nothing
twice.

**The page itself**, reopened in the browser when a review parks and no tab
follows the page — a tab already open, even one in the background, means the
page is left alone. At most once per ten minutes, and only where a browser
the operator sees can open: on Linux only under X or Wayland. The person
turns it off with `[dashboard] reopen = false` in their lup config
(`~/.config/lup/config.toml`), or `uv run lup-devtools dashboard reopen
--off`, which writes that line; `--on` writes it back, and neither says which
it is. The notice is sent either way.

**Every session's status line**, where its runtime draws one: `N reviews
pending · <address>`, the address alone when nothing waits, nothing when no
dashboard answers. The dashboard publishes what it counts — reviews waiting,
sessions, repositories, open tabs and its address, never its capability — as
its pulse, a small file in a directory of its own that every launch lends its
session read-only at the path the host has it, named by
`LUP_DASHBOARD_PULSE`. It rewrites the pulse whenever it changes and at least
every ten seconds, and takes it down when it stops, so a pulse older than
thirty seconds reads as a dashboard that stopped. The operator's `dashboard
stop` leaves a pulse in its place saying so, which holds until a start
replaces it. The status line runs
`uv run lup-devtools dashboard line <pulse>`, which the CLI answers before
loading the project's application, in about a fifth of a second. Claude Code
shows it through `statusLine`, re-run every fifteen seconds so a review
another session parks shows while this one is idle, and only where nobody
else named one: a status line in the account's settings, the person's
`[claude.settings]` or the project's settings stays theirs. Codex has no
status line a command can fill (`docs/platform-differentiation.md`), so a
Codex session has the notice, the reopened page and `dashboard status`.

## One stream

Everything live reaches the page on one stream, `GET /api/stream`, as
server-sent events; nothing on the page polls. One producer serves every open
tab: while any tab follows, it looks at every repository the dashboard serves
twice a second — each roster, the transcript each live row names, the mail
record — and at the review queues once a second, and numbers every difference
it finds. A fresh tab is handed the whole state once, then each numbered
change. A tab that reconnects sends the cursor of the last frame it saw as
`Last-Event-ID` and is handed exactly what it missed; a cursor this dashboard
never handed out — one from before it restarted, or older than the 4096
changes it keeps — is answered with the whole state again, so a tab can fall
behind but never go wrong. Once what a tab missed is sent the stream says so,
and the page reads as current even when nothing had changed.

Each source is read again only where it moved. A roster is read when a member's
file changes, and at least every five seconds, since a runtime can stop without
writing; a transcript from the byte it was last read to, its last megabyte the
first time; the mail record from its cursor; a checkout's review queue only when
its relay changed on disk, and then only what was appended since, through the
relay the dashboard keeps open; and a settled review is projected once. An idle
dashboard costs a few file checks a look however many sessions it shows and
however long the history behind them.

The reviews on the stream are rows: every review waiting, and the fifty that
most recently left the queue, each with what the queue shows of it — its
title, the files it names, who asked, why the policy asks, whether it can be
answered here — and how many reviews History holds in all. A review's
documents, diffs and thread are read when it is opened
(`GET /api/reviews/<key>`), and the next one in the list is read as soon as
the open one has arrived, so moving on shows it at once; a review opened again
while its row stands is shown from what was read. What a review's documents
show is worked out once for its fingerprint, so opening it again, and the
answer that settles it, diff nothing anew. History past the fifty is read a
page at a time (`GET /api/reviews/history?offset=<n>&limit=<m>`, most
recently settled first) with **Load older requests**, and a link to a review
further back is looked up there by its id.

Until the first frame arrives, the page shows loading with unknown counts.
After it, a queue that is not current — the stream reconnecting, or a
checkout whose queue could not be read — keeps its last counts, marked as
refreshing, and the line beneath them says why: reconnecting, or which
checkout is unavailable and what reading it said. Only a current stream with
every queue read can confirm that no requests are waiting. A queue read that
fails is read again a moment later before it is reported, so a writer
appending to a relay never blanks the page.

## Sessions

The Sessions view lists every session of every repository the dashboard
serves, read from that repository's roster, with its native subagents nested
beneath it — each subagent is a roster row of its own, named from its spawn.
Each row says what the session says it is doing, the call it is waiting on or
the last thing it said, and how many messages wait in its mailbox. Sessions
that stopped within the roster's window stay listed, marked stopped.

Choose a session to read its id, worktree and task; what it says it is doing;
what it is doing now, folded from the runtime's own transcript the row names —
the last thing it said in its own words, and the tool call nothing has answered
yet, with its arguments (a subagent's from its own transcript beside its
session's); what its calls hold, marked where another session holds it too;
its subagents; and every message sent to it or by it, oldest first, each marked
as waiting in its mailbox or taken. Choose a repository's name to read every
message its sessions sent each other as one conversation. Messages come from
the repository's mail record, `mail.jsonl` in its coordination store, where
every message posted lands as well as in its reader's mailbox, so what was said
stays readable after its reader took it — for the roster's retention window,
past which the sweep cuts it from the record.

## Writing to a session

A running session has a box beneath its messages. What the operator writes goes
the way a session's own `coordination_send` to a peer goes: into the session's
mailbox, signed `user`, where its hook hands it over before its next tool call
as `[message from user by page] …`, then through its wake path so an idle
session takes a turn — its wake socket for Claude Code, `codex queue` for
Codex — carrying everything waiting for it. The page says which happened: the
runtime accepted the wake, the message waits for the next tool call (a
subagent's only route), or why no wake was attempted. The session answers the
operator by sending to `user`. The box writes through the page's capability and
origin check, addressed by the repository's key and the session's member id —
the id every verb accepts and no rename changes
(`POST /api/repositories/<key>/sessions/<member-id>/messages`). A session that
stopped has no box: a message to it would wait for nobody.

## Reviews

Reviews are grouped by repository, then by the session that asked, named as the
roster names it. A review whose requester is gone is expired, recording why: at
once where the roster saw that session end, after an hour where the roster
never knew it, since a session it never recorded cannot be told from one that
has not joined yet. The dashboard sweeps every ten seconds, and
`review list` sweeps its checkout before it lists. A retry of an expired
review's call parks a fresh review.

The same sweep archives what settled long enough ago. A review that came to
rest — carried out, declined, withdrawn, expired or stale — keeps its
documents for the retention window after it settled, seven days unless the
checkout's `pyproject.toml` says otherwise as `review-retention-days` under
`[tool.lup]`: long enough to reread a week's reviews with their diffs, short
enough that the relay every hook and every look reads holds a week of them.
Past it the review moves to the relay's archive, `.lup/reviews/archive.jsonl`,
keeping its record, how it read — its title, the files it named — and its
thread; its documents leave the store, unless a review still in the relay
names the same one. History shows archived reviews beside the rest, and one
opened says it was archived and shows what the archive kept; `review list
--all` and `review show <id>` read them too. At most two hundred move per
sweep, so a checkout with a long history catches up over several sweeps.

A waiting review a recorded file moved under leaves the pending queue at once
as `stale`, and shows only in History, labelled stale, with each file that
moved and how: changed, created, deleted, or a directory where none stood.
The dashboard watches every waiting review's recorded files by their size,
time and inode on each look, reading one again only where those moved. Only
the files a call writes are judged there, never a file a command reads from —
a copy's source, a patch — whose text the review already holds; and only
what the dashboard can see: a path inside a session's container, whose
directory is nowhere on the host, or a file the dashboard may not read, is
never stale. The requester's waiter, which runs inside the session, reads
every recorded file before it carries a command out, sources included: where
one moved, running the command would land something other than what the
operator approved, so it runs nothing and retires the review as stale,
naming the file.

## Setup

Each repository's setup — the integrations and steps its own CLI declares — is
a pane beside its reviews. The dashboard runs that repository's own
`lup-devtools setup serve` in a checkout of it the first time the pane opens,
and serves the page it draws under the dashboard's origin, behind a path
derived from the dashboard's capability. It stops with the dashboard, or once
no running session of that repository holds it any more. A repository that
declined the setup module serves no pane, and the pane says so.

## Reviewing requests

The editor is the centre of a review, at any width: it takes the height left
and scrolls inside itself, and the context around it is single lines. Above
it, the title — the change's paths relative to the checkout it changes, which
leads the line — with the full path on hover; one line naming the queue, the
directory the call runs in, the session that asked and when, with **Details**
opening the complete record; then the policy's reason, "Why approval is
needed · <rule>", in full. Below, the comment box
is pinned open and focused on every waiting review, with the decisions beside
it. What a review said and what it came to are its thread: the operator's
remarks, the requester's replies, and the answer, oldest first.

Beside the policy's reason stands the requester's own account of the call,
labelled as the claim it is — what it is for is the agent's to say, why it
asks is the policy's — and never cut; a long one folds behind **Show all**.
It is recorded when the call parks, from what the runtime's half reads there:
the note the tool call carries beside the command (Claude Code's
`description`, Codex's `justification` for running outside its sandbox), and
what the agent wrote since it last heard anything, read back off the
transcript of the conversation that made the call — a subagent's own. Where
it said nothing there, what its roster row says it is on stands in, labelled
as the session's; a proposal's is its `--why`, with each file's note on that
file. `review show` prints the same lines.

A command shows beneath, whole. Where its line runs several commands, each one
that asks or is refused is listed with the verdict it reached on its own, its
reason and rule, and the ones allowed on their own fold beneath, so a line
that asks twice is approved knowing both questions (`docs/permissions.md`).
Its file changes show the way an edit's do, one diff per file in the order it
writes them: the documents the policy worked out when it judged the command,
read off the review rather than re-derived where it is read. The steps whose
result exists only once they run are listed beside it, each with the files it
leaves so.

The dashboard titles requests from captured evidence: a file's action and path,
the number of files, or the command to run. The default view includes files
that require review and highlights newly introduced rule exceptions. Files the
policy allows automatically or explicitly leaves to the native provider, and
existing exceptions, remain available in the full-operation view. A captured
deferral means Lup requests no approval for that file; the native provider
still applies its own permissions. Approval still applies to the exact
complete submission. Where recorded evidence cannot establish a file's status,
it remains visible rather than being treated as automatically allowed. A
review of several files has a navigator with change counts and path search,
and `[` and `]` move between files; one of a single file gives the editor the
whole width. Each file shows as a coloured, numbered diff, its syntax
highlighted by the file's extension, with Before, After and Raw views beside
it. The lines a diff leaves out fold into one expander per gap, and
**Whole file** (`f`) shows the entire file with the changes marked in place.

Every `# lup:` marker is marked where it stands, in its own colour and
labelled with its kind: an open note, parked work (`defer:`, and
`defer[<gate>]:` with its gate), a resolution claim (`solved:`), a
customization point (`template:`), and a rule exception (`ignore[<rule>]`).
`m` and `Shift+M` jump to the next and previous marker across the review's
files, opening the whole file where a marker stands outside the diff's hunks.
Rule exceptions are also grouped by rule in the navigator, with `n` and `p`
to jump between them; existing exceptions appear only in the full-operation
view.

Click a line number to comment on that line, and Shift+click another to
comment on the range between: an inline box opens beneath it. Comments are
drafts until sent — with a decision, or alone — and stored in the answer, or
the remark, as structured anchors: the path, the first and last line, the
side the numbers belong to (`before` or `after`), and the note. They show on
answered reviews where they were made, and in the thread.

Ctrl+Enter (Cmd+Enter) approves; Alt+Delete declines — the forward Delete
key, never Backspace, so Alt+Backspace still deletes a word; Alt+Enter sends
the note and line comments without deciding. Alt+↑ and Alt+↓ move to the
previous and next review, from inside the box too. Esc leaves the box, so the
one-letter keys apply again: `j` / `k` for the next and previous request,
`c` back into the box, `[` / `]`, `n` / `p`, `m` / `Shift+M`, `f`, and `?`
for the help that lists them. Holding a key cannot answer another request.

Answering is immediate: the page shows the review answered and opens the next
one at once, and reconciles with the server's reply in a toast that says how
the requester hears of it — its waiter holding the review, the mailbox and
whether the session was woken, or nobody running to hear it — and whether a
copy went to a subagent's session. A refusal puts the review back where it
was, the note and line comments with it, and says why in the toast, on the
queue row and above the box. Auto-advance can be turned off to stay on the
answered request. New arrivals do not move a selection already under review.

Use **Copy link** to share a request without sharing a credential. Links use
`#review=<question-id>`; copied links also name the checkout to distinguish
identical IDs. They open the exact pending or historical request,
including in another tab of an already authorized browser. Back, forward, and
changed links select the corresponding request. A missing ID stays selected
while the dashboard watches for it; it never silently opens a different request.

The decision is recorded where the terminal commands record theirs: on the
host, in lup's own state (`$XDG_STATE_HOME/lup/reviews`), which every launch
lends its session read-only and no session writes, under one lock with the
terminal, so a browser and terminal answering concurrently cannot replace
each other's answer. An answer is given against the fingerprint the page
displayed, and a review whose record no longer hashes to its fingerprint is
refused an answer, the page saying so before Approve is reached for.
Notifying the requester follows the saved answer, and a missing route or
failed delivery does not erase it. Each browser answer retains a separate
notification outcome in `.lup/review-notifications/`, bound to its question,
fingerprint and answer timestamp; diagnostics failures never undo the recorded
approval. Native reviews notify only a unique registered requester whose
bound native session matches the request, by the one channel described above.
Queue acceptance does not prove the agent read the message. The dashboard
executes nothing: an approved call is carried out by the session's own
`review wait`, or one exact retry.

The capability travels in the launch URL's fragment, which HTTP requests do
not send to the server. The page removes the credential from the address and
keeps it in local storage for that exact origin: scheme, hostname, and port.
Tabs at that origin authenticate queue API requests with its bearer credential,
so shared request links carry only review identity. The credential is never a
cookie or read from stale session storage. A fresh launch link updates other
open tabs through storage events; opening it in the same tab keeps the selected
review and draft comment. If browser storage is blocked, the page explains that
access is limited to the tab that opened the launch link. The page and its
assets contain no credential. The dashboard every launch holds keeps its
capability across restarts; one `dashboard serve` mints is its own, and ends
with it. Treat the full address as an operator credential and keep it out of
agent messages. The server binds loopback, checks Host against DNS rebinding,
and checks the origin of every submission. These controls protect the browser
surface; they are not isolation against arbitrary processes running as the
operator's user. The session's filesystem and process boundary remains part
of the authority boundary.
