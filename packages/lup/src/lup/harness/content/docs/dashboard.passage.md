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

The review is kept in the queue of the checkout the session's launch opened
(`LUP_BOUNDARY_ROOT`), at its top, wherever the call runs: from a
subdirectory, a sibling worktree or another repository, the call is recorded
there, labelled with the checkout it changes, and the directory it runs in
kept with it. That is the queue whose answers the launch lends the session,
and every review command the session is handed is spelled to run there, with
that checkout's code — `uv run --directory <session checkout> lup-devtools
review …` — so what a session parks is written and read by one version of
lup, the one the operator's dashboard follows. An unlaunched session keeps
its reviews at the top of the checkout it runs in.

The refusal is written for the agent: the call is queued as review `<id>`,
not refused; it is not to be changed; and how the conversation that asked
hears the answer. A session's own conversation carries on or ends its turn,
and starts no waiter: the operator's answer wakes it, and the
`review wait <id>` it names then carries the call out at once. A subagent,
which nothing but its own background work wakes, holds `review wait <id>`
in the background and carries on, starting it again quietly whenever it ends
with the review still waiting. `review wait <id>...` waits for every review
named, `--any` for the first of them, and with none named for every review
this session and its subagents have waiting; it reports each as it settles. An approved one it
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
answers on the review with `uv run --directory <session checkout>
lup-devtools review reply <id> <text>`, which the page shows in the review's
thread — only the session that asked may — or cancels it and asks again. A
`review reply`, `wait` or `cancel` run from another checkout than the
session's names the review's queue and the command that reaches it, and a
`review propose` run there is refused with the command that parks it in the
session's queue.

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
command's end re-invokes the subagent that started it. On Codex a command
the shell tool started keeps running after the turn and its end starts none
— measured on 0.158.0 — and a subagent's last message is its report, after
which nothing wakes it: a Codex subagent holds its waiter in its shell tool
and reads it before it reports. A waiter a session's own Codex thread left
running queues its report into the thread with `codex queue` when it
settles, which starts a turn in an idle session; one run once the answer
was in queues nothing, its report read in the call that ran it, and a
subagent's queues nothing into its session's thread.

A waiter waits as long as the operator takes: it has no limit of its own,
and every ten minutes it says which reviews it still waits on. `--timeout
<seconds>` ends it early, exit 3, carrying nothing out and naming the
`review wait` that waits again. A runtime can still stop it: Claude Code's
shell tool stops a command at its `timeout`, thirty minutes in the background
unless the call names more, two hours at most, and no monitor outlives half
an hour. That is why a session's own conversation holds no waiter: one
restarted every two hours woke the session for nothing, where the operator's
answer wakes it only when there is something to do. A subagent's refusal
asks for the longest the tool takes and names the waiter with `--timeout
7140`, which ends it a minute sooner saying the review still waits and to
start it again quietly — a waiter ending is news to nobody; and a waiter
sent SIGTERM or SIGHUP with a review still pending says the same before it
exits. A review no waiter holds is not lost: the operator's answer goes to
the asker's mailbox, naming the `review wait` that carries it out.

The waiter is the requester's one channel. Where a `review wait` holds the
review — or already put the news to its session — the dashboard mails nothing
beside it; only where none does is the answer, the remark or the staleness
mailed to the requester and its wake tried, whether the operator answered on
the page or with `review approve` or `decline` from a terminal, which says
how the session heard. Where a subagent asked, the
review records it (`agent`), its own row is the requester, and whenever the
operator said something — a note, line comments, a remark — the session it
runs in gets a copy, marked `[copy]`: the subagent handles the call, its
session anything past it. A bare approval pings nobody but the waiter.

## Proposing a batch of edits

Several edits the operator has to see, parked one call at a time, are
answered one at a time — each against a file the next edit then moves. A
session writes them under scratch instead, each at its path in the checkout
it changes (`<checkout>/tmp/cdx/<path>`), tests them there, and runs
`uv run --directory <session checkout> lup-devtools review propose
<checkout>/tmp/cdx --why "<what it changes and why>"`, which parks one review
holding every file, in the session's queue. The files land in the checkout
holding the directory, or the one `--checkout` names, which the review is
labelled with; the subagent that proposed it is recorded as the hooks record
one for a call (`agent`), read off which of the session's conversations has
a command running. Each file meets the edit gates a direct
write of it would meet — protected paths, anti-patterns, markers, size — and
the review carries each verdict; a file the gates refuse outright refuses the
proposal, naming it, and nothing is parked. A file they would let through is
carried all the same, folded on the page where it needs no reading. The
directory's `.proposal.json` notes files and names deletions, which have no
document to write:

```json
{"about": {"packages/app/host.py": "`patched_documents` now reads all the files first and only then writes them, replacing a 3-way if/elif. Same behaviour."},
 "delete": ["docs/retired.md"]}
```

The `--why` and the notes are what the operator decides from, so they are
written plainly, the way you would tell a colleague at their desk: what
changes, in ordinary words, then why; the file, function or command by name;
and what behaves differently, or "no behaviour change". One or two short
sentences a file, a short paragraph for `--why`. `review propose --help`
carries the rule with a plain note and an obtuse one side by side. A
proposal of several files where one has no note parks all the same, with a
warning naming it; `review show` prints each note under its file, whole.

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
and hands it to its session as `LUP_DASHBOARD_URL`, credential-free; one the
operator made on a terminal also prints the launch address at each origin
(below).

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
`code.since`). Whenever it stops serving — to restart or for good — every open
stream ends at once, rather than being left for the server's two-second grace
and then cut off, and each tab reconnects on its own.

`uv run lup-devtools dashboard restart` does the same at once, onto its checkout's
code as it stands. Where none runs while sessions hold it — their launchers
run code that does not keep it running — it starts one for them from the
checkout the command runs in. A dashboard whose code has no restart is replaced
instead: stopped, and started from that checkout for the sessions holding it.

`uv run lup-devtools dashboard status` says whether it serves, where, for how
many sessions, over which repositories, how many reviews wait, how many
messages agents sent the operator wait unread, how many agents are quiet and
how many paths are held twice (as the status line counts them, below), how
many tabs follow it, which code it runs, and how many times the sessions holding it
started it again; from the operator's terminal it also gives the last exit
(`exited`: when, how, and its last lines of output), the last stop lup made
(`stopped`: why, by which process, and the live leases it counted), and, while
none runs, when the sessions start it again, and the origins declared for it
(`origins`), its launch address at each (`launch`) and what to heed of them
(`warnings`), as "Behind a reverse proxy" says. Inside a session it reads what
the dashboard publishes, its pulse (below), never the operator's private
state. `dashboard stop` stops it now, and it stays stopped, the sessions
holding it included, until `dashboard restart` or the next launch starts it.
`dashboard open`, `dashboard stop`, `dashboard restart`, `dashboard serve` and
`dashboard reopen` are the operator's, run from a terminal outside every agent
session.

`dashboard serve` serves one in this terminal instead, with a capability of its
own, over the current repository or each `--root <checkout>` named, until
Ctrl+C. `--host` picks a loopback address, `--port` its port, and `--no-open`
keeps the browser closed. It is the same service run by hand, and follows its
checkout the same way: when the lup files it imported or the page's bundle
move and the new code starts, it replaces itself in the same process, on the
same port and with the same capability, kept in a private directory of its
own that it removes when it stops; the page shows the same notices. What only
the shared dashboard does — the desktop notice, the reopened page, the pulse
every status line reads, and retiring the panes no session holds — it does
not.

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

**Every session's status line**, where its runtime draws one. It reads, left
to right:

- **which session this is**, dimmed: its repository's name as the page's
  tree names it, the roster's name for the session, and the worktree it was
  launched in, relative to the repository's directory — `lup · dev ·
  tree/fix-x`, the worktree left out where it is that directory, which the
  repository's name already says;
- **what waits on the operator**, in the warning colour and only while
  something does: the reviews waiting, and how many of them this session or
  its subagents parked, naming the one by the first eight characters of its
  id and counting several — `?2 reviews (1 here: 41cb73e1)` — and the
  messages agents sent the operator that still wait in its mailbox, `✉1`.
  Nothing takes a message out of that mailbox yet: the page shows each, and
  nothing marks one read, so `✉` counts every message an agent sent the
  operator in a repository the dashboard serves;
- **what other agents need**, only while one does: `⚠ 1 quiet`, an agent
  with a call outstanding and nothing new in its transcript for ten minutes,
  none of whose subagents runs (a session waiting on its subagent is waiting
  on that subagent, which answers for itself), and `⚠ held twice`, a path two
  sessions hold, a subagent holding with its session's hand;
- **the dashboard, as one glyph and its whole address**, always: the
  address the operator opens the page at — the first origin declared in
  `[dashboard] origins` ("Behind a reverse proxy"), else
  `http://127.0.0.1:<port>` — as a terminal hyperlink whose text is the
  address itself, so a terminal that links a bare address opens it too, and
  never with the capability. `● http://127.0.0.1:8767` while it serves
  current code; `◐ http://127.0.0.1:8767 restarting` while it moves onto
  newer code; `○ http://127.0.0.1:8767 down · dashboard restart` where
  nothing serves, and `○ dashboard down · dashboard restart` where it took
  its pulse down and no address is left to name; and the address followed
  by "dashboard stopped by the operator; `dashboard restart` starts it"
  after the operator's stop. For five minutes after the sessions started it
  again it adds "restarted after it stopped:" and why.

```
lup · dev · tree/dev │ ● http://127.0.0.1:8767
lup · dev · tree/fix-x │ ?2 reviews (1 here: 41cb73e1) · ✉1 │ ● http://127.0.0.1:8767
lup · dev · tree/dev │ ⚠ 1 quiet │ ◐ http://127.0.0.1:8767 restarting
lup · dev · tree/dev │ ○ http://127.0.0.1:8767 down · dashboard restart
```

The page keeps its capability in the browser's storage for its origin, so a
click on the line opens it signed in only where the operator has opened the
launch address at that same origin — the first declared origin where one is
declared, since that is the one they sign in at through their proxy. At an
origin whose launch address this browser never opened, the page says it is
not authorized and names `dashboard open`, which prints the launch address
at each origin (`dashboard status` gives them under `launch`). The line
itself never carries the capability.

Where the terminal is narrower than the line, whole pieces drop and no word
is cut, in this order: what other agents need, the worktree, the review's
id, the session's name, and last the repository's name. What waits on the
operator and the dashboard's glyph and address always stay; a line still too
wide is the runtime's to cut.

The session is the one the runtime names on the command's stdin. Claude
Code's `statusLine` input carries the conversation's id (`session_id`) and
the transcript it writes (`transcript_path`), and the running roster row whose
launch named that conversation, or whose last prompt came from that
transcript, is this session's — so a conversation the runtime opened after
the launch is found once it has taken a prompt. A session the dashboard does
not list is placed by the directory it was launched in
(`workspace.project_dir`), without a name, and named by the repository whose
directory holds that.

The dashboard publishes what the line reads as its pulse: the reviews
waiting, each running session with its repository's name and the reviews it
or its subagents parked, the messages to the operator, how many agents are
quiet and how many paths are held twice, the sessions holding it, its
repositories, open tabs, where it serves and where the operator opens it —
never its capability. It reads each roster, and the transcripts
the roster names, on a watch of its own at every look, so what it counts
holds whether or not a tab follows the page. The pulse is a small file in a
directory of its own that every launch lends its session read-only at the
path the host has it, named by `LUP_DASHBOARD_PULSE`. It is rewritten
whenever it changes and at least every ten seconds, and taken down when the
dashboard stops, so a pulse older than thirty seconds reads as a dashboard
that stopped. The operator's `dashboard stop` leaves a pulse in its place
saying so, which keeps the sessions and the operator's address the last one
listed, so each line still names its session and the page, and holds until a
start replaces it. A pulse whose dashboard runs code that lists no sessions
reads as one listing none: the line shows the counts it has,
places the session by its directory, and links the address it serves at.

The status line runs `uv run lup-devtools dashboard line <pulse>`, which the
CLI answers before loading the project's application, reading only the pulse
and stdin, in about a fifth of a second. Claude Code shows it through
`statusLine` (https://code.claude.com/docs/en/statusline), which paints its
ANSI colours and passes its OSC 8 hyperlink through where it detects the
terminal takes one (`FORCE_HYPERLINK=1` overrides that detection; a terminal
without them shows the address, which one that links bare addresses opens
as well), and sets `COLUMNS` to the terminal's
width for the command. It re-runs it every fifteen seconds so a review
another session parks shows while this one is idle, and only where nobody
else named one: a status line in the account's settings, the person's
`[claude.settings]` or the project's settings stays theirs. Codex has no
status line a command can fill (`docs/platform-differentiation.md`), so a
Codex session has the notice, the reopened page, and `dashboard status`,
which carries the same counts — `pending`, `unread`, `quiet` and
`contested`.

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
first time; the mail record from its end the first time, then from its cursor;
a checkout's review queue only when its relay changed on disk, and then only
what was appended since, through the relay the dashboard keeps open; and a
settled review is projected once. An idle
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

The person's own keys ride the same stream: the first frame carries the keys in
effect — every action whose keys differ from lup's, and what was refused and
why — and a `keys` frame follows whenever their lup config changes on disk, so
a saved `[dashboard.keys]` reaches every open tab with nothing to reload
(Personal keys, below).

Until the first frame arrives, the page shows loading with unknown counts.
After it, a queue that is not current — the stream reconnecting, or a
checkout whose queue could not be read — keeps its last counts, marked as
refreshing, and the line beneath them says why: reconnecting, or which
checkout is unavailable and what reading it said. Only a current stream with
every queue read can confirm that no requests are waiting. A queue read that
fails is read again a moment later before it is reported, so a writer
appending to a relay never blanks the page.

## The page

The page is one editor. Its middle window holds one buffer — a review, an
agent, the operator's own row, a repository's page, the inbox, or a
discussion — with the agents tree on its left and the context window on its
right, describing what the buffer holds beside the lines it describes. The
split under the buffer is the box: the note on a review, the message box
beside an agent, the post box under a discussion. The tabline above names the
views — **1 Supervise**, **2 History**, **3 Inbox**, **4 Threads** and
**5 Setup**, `{n}gt` going to view n — with what each counts; the statusline
below says the mode, where focus is, what is open and where the cursor
stands, what is unsent, what waits on the operator, whether the stream is
live, and which code the dashboard runs; the command line sits under it.

Focus decides where keys act, and exactly one place has it: the tree (History's
list in History, the discussions in Threads), the buffer, the box, or the
context. It wears the one outline on the page, and the statusline names it —
`in the tree`, `in the buffer`, `in the box`, `in the context`. In the tree
`j`/`k` walk its rows, opening each; in the buffer the cursor moves like an
editor's; in the context they walk its items, and `Enter` acts on one.
`Tab` and `Shift+Tab` move focus round the places that show, from inside the
box too; `Esc` from the box, the tree or the context goes to the buffer; a
click moves focus where it lands.

In the buffer the cursor has a line and a column, and the caret is drawn on
its character while the buffer has focus. `h`/`l` move a character and
`j`/`k` a line, keeping the column chosen; `w`, `b` and `e` move by words
across lines; `0`, `^` and `$` go to the line's start, its first non-blank and
its end; a count repeats any of them (`3w`, `5j`). `gg` and `G` go to the top
and the bottom, `{n}G` to line n of the file in a review and row n anywhere
else; `{` and `}` go to the previous and next change or step in a review, and
section elsewhere; `Ctrl+d`/`Ctrl+u` and `PgDn`/`PgUp` move half a page. `/`
searches the window with focus, incrementally and smartcase, landing on the
match's column, and `;` and `,` go to the next and previous match. A click
puts the cursor on the character clicked. The statusline says where it stands:
`L972:12` in a review, `row r:c` anywhere else. Help, the full context of a call, `:messages`, `:map` and the finder
open as floats over the page, drawn only while open, and every one closes
with `q`, `Esc`, its ✕, or the key that opened it. What the dashboard says —
an answer's outcome, a refusal, a restart — comes up as a notice at the top
right; an outcome closes on its own, and a refusal or the dashboard's own word
stands until dismissed (`Space u d`). `:messages` keeps everything the page said.

The windows give way by width rather than squeezing. From 1600 px the tree is
wide enough that names and calls wrap less; below 1280 px the tree and the
editor show, the context steps aside, and `Space o` brings it in the tree's
place and gives it back when it goes. Long lines wrap beside the gutter rather
than cutting anything; `:set nowrap` scrolls them sideways instead. Below
861 px the page is the touch layout (On a phone or a tablet, below). The colours follow
the system's light or dark setting and come from one palette — Claude Code's
colour-blind themes and VS Code's high-contrast ones — every text colour
reading at 7:1 or better on whatever it sits on; `:contrast` shows each colour,
where it came from, and how it reads. No state is told by colour alone: every
row carries a glyph or a sign as well.

## Supervising

The Supervise view is where the page lands. Its tree lists every repository
the dashboard serves, the operator's own row, and each session read from that
repository's roster, with its native subagents nested beneath it — each
subagent a roster row of its own, named from its spawn — and under each agent
the reviews it parked that wait on the operator. An agent's row reads its
standing — `●` working, `◌` idle, `◷` quiet (a call running ten minutes with
nothing new), `○` stopped — its name, and what needs the operator: `?n`
reviews waiting, `✎n` unread messages it sent the operator, `✉n` messages
waiting in its own mailbox, `⌂n` paths its calls hold and `!n` paths another
agent holds too, then how long since it was heard. Its second line is what it
is doing: the call nothing has answered yet in one line, or what it last said.
A repository's stopped agents fold into one row at the end of its tree, unless
something under one still runs, waits on the operator, or wrote to them
unread; `za` or a click on that row shows them, and `:set stopped` shows every
repository's. `Space t t` narrows the tree to the agents that need the
operator, `Space t r` to the reviews waiting, and `Space t a` shows every agent.

Choose an agent — a click, the tree's own `j`/`k` while it has focus, `(` and
`)` in the tree's order, `:agent <name>`, or `Space f a` — to read its buffer:
what it is doing now, folded from the runtime's own transcript its row names
(the call nothing has answered yet with its arguments, and the last thing it
said in its own words), then every message sent to it or by it, oldest first,
each marked as waiting in its mailbox or taken. Its context says its kind and
parent, what needs the operator, its id, worktree and task, when it arrived
and was last heard, what a message to it reaches, what its calls hold —
marked where another agent holds it too — its mailbox, the reviews it parked,
its subagents, and what can be done to it. `gs` goes from a review to the
agent that asked, or from an agent to its parent session; `gr` goes from an
agent to a review it parked. A repository's own row opens its page: its
members, every message between them as one conversation, and a live log of
what the stream moved, which the page writes from the frames it applies.

Every agent's row also says which runtime it runs in (a subagent its
session's), the session whose shell spawned it where one did, and its runtime
process — pid and start time, whether the dashboard shares its pid namespace,
and whether the dashboard could stop it or why not. Its latest twelve calls
ride with what it is doing now, each answered, failed or still waiting, summed
up by its own description, command or path. The person's own row in each
repository rides the stream too, as a `user` event: what they say they are on,
what they hold, the notices standing there, and how many messages to them wait.
A message carries its `post` and `thread`, which is how the page groups a
discussion. The whole state the stream hands a fresh tab says which pieces of
supervision this server serves (`served`), by the names the page gives them.

An agent's whole transcript is read a page at a time from its end
(`GET /api/repositories/<key>/sessions/<member-id>/transcript?before=<byte>&limit=<n>`):
each entry is something said, a call made with its arguments, or what a call
returned, whole, placed by the byte its line starts at. A tab showing one
follows it by asking for it with the byte its page ends at
(`POST /api/transcripts/follow`), renewed while it shows it: the stream then
carries `transcript` frames with what that transcript recorded since, for as
long as some tab renews within a minute.

Messages come from the repository's mail record, `mail.jsonl` in its
coordination store, where every message posted lands as well as in its
reader's mailbox, so what was said stays readable after its reader took it,
for as long as the clone stands: the record is never trimmed. So it is never
read whole either. The stream carries each repository's latest hundred
messages and whatever is posted after them, read from the record's end, and
`E`, or the **Load earlier messages** row, reads the hundred before the
earliest the page holds (`GET /api/repositories/<key>/messages?before=<byte>`),
back to the record's start.

Beside a running agent, the box under its buffer writes to it: `c` enters it,
and `Alt+Enter` sends. What the operator writes goes the way a session's own
`coordination_send` to a peer goes: into the agent's mailbox, signed `user`,
where its hook hands it over before its next tool call as
`[message from user by page · post <post>] …`, then through its wake path so an idle session
takes a turn — its wake socket for Claude Code, `codex queue` for Codex —
carrying everything waiting for it. The page says which happened in the
server's own words: the runtime accepted the wake, the message waits for the
next tool call (a subagent's only route), or why no wake was attempted. The
agent answers the operator by sending to `user`. The box writes through the
page's capability and origin check, addressed by the repository's key and the
member id — the id every verb accepts and no rename changes
(`POST /api/repositories/<key>/sessions/<member-id>/messages`). A stopped
agent has no box: a message to it would wait for nobody, and the box's place
names its parent session instead. `Space a p` asks a subagent's parent session
what it is doing, in words written for the operator, and a repository's page
broadcasts its box to every working member as one post, each woken.

Beside the box, **Interrupt** (`Space a n`) sends its words, or standard ones
where it is empty, with priority `now`, and **Redirect** sends them as a
redirect. `Space a r` makes the box answer the last message between the agent
and the operator, and `r` or `Enter` on a message the one under the cursor,
in its thread; the box names what it answers, and `✕` lets it go. `Space a w`
wakes the agent, `Space a R` (`:rename`) renames it, and `Space a x` stops its
runtime: the first press says which process it would signal, and only a
second within ten seconds, or `:stop!`, sends it. `T` reads the agent's whole
transcript in a float, from its latest page back, following it while it
shows. The context lists every one of these on the agent, with where it runs —
its runtime, who spawned it, and the process the dashboard could stop or why
not — and its latest calls. On a phone, **Act** under the agent opens the same
list.

The Inbox lists every message addressed to the operator, in every repository,
newest first; `Enter` on one opens its sender with the box ready to write
back. The operator's own row shows what was sent to them, what they sent
lately, and their working verbs.

Threads reads the same mail as discussions. A post is the copies one send
left in several mailboxes, sharing one post id, shown once with every
recipient and whether each took it. Posts sharing a thread, or replying to
each other through `in_reply_to`, transitively, are a thread, titled by its
first post's first line; the posts between the same members that reply to
nothing are their running conversation, titled by who is in it. A message the
record kept from before posts had ids is one post with the copies sharing its
sender, text and time, threaded by its `in_reply_to` alone. Its list holds
every discussion in every repository, newest first, `●` where something in it
is unread to the operator; its buffer shows one whole: each post with its
author's standing, every recipient marked taken `✓`, waiting `◷` or unread to
the operator, the post it answers, and its text. Its context names who is in
it, each one step from their agent. `r` or `Enter` on a post makes the box
answer that post, `c` goes to the box, `Alt+Enter` posts to everyone in the
discussion through its thread, `Space f t` finds a discussion, and `:threads`
opens the view.

The dashboard's server serves the rest of supervising — a reply in a
message's thread, a redirect, an interrupt, a wake without a message, a whole
transcript, renaming and stopping an agent, standing notices, the operator's
own description and holds, marking the inbox read, and a post into a
discussion to everyone in it — as the sections below say, and the whole state
the stream hands a tab names each (`served`). The page reads that: an action
whose piece a dashboard running older code does not serve is refused naming
the route it needs, the draft kept, and the help, the finder and the context
mark it not served here. The operator's own verbs are commands: `:describe`,
`:notice` and `:unnotice`, `:lock` and `:release` (a relative path is the
repository's checkout's), `:redirect`, `:read` and `:read all` (`x` and `X`
in the inbox), and `:wake`, `:nudge`, `:interrupt`, `:rename`, `:transcript`
and `:stop` for an agent. Their own row shows what they are on, what they
hold and the notices standing, each held path and notice a click from being
given back or withdrawn.

## A reply, a redirect, an interrupt

A message can answer one the operator was sent (`in_reply_to`, a post id),
which puts it in that post's thread. It can be a **redirect**, which refuses
the agent's next tool call with the operator's words: a wake shows it to the
agent and leaves it for the hook, which refuses that call. With priority `now`
it stops the agent's turn for it. A Claude session is asked through its wake
socket: a turn that is generating ends at once, and a tool call already
running finishes first — measured on Claude Code 2.1.285. A Codex session's
running turn is stopped through the app-server its configuration home runs —
at once, even mid-command, though the command's own process runs on — and its
queue takes the message as the next turn, measured on Codex 0.159.2; only a
dashboard in the execution scope its row recorded can reach that app-server. A subagent has no wake of its own, so its
message waits for its next call and its session is interrupted with a copy
naming it. `now` is refused, saying why, for an agent nothing can reach.

## Acting on an agent

Every write below holds to the page's capability, its own origin and JSON, a
`DELETE` as much as a `POST`, and answers what it could not do with why: 404
where nothing answers to what was named, 409 where something does and the verb
cannot be done to it.

- **Wake** (`POST …/sessions/<member-id>/wake`): makes a session look, carrying
  what waits for it, or, where nothing does, a line saying the person asked it
  to look. A subagent has no wake of its own.
- **Rename** (`POST …/sessions/<member-id>/name`, `{name}`): what the agent is
  called from now on; a name another live agent answers to is refused, and the
  old name reaches it until another takes it.
- **Stop** (`POST …/sessions/<member-id>/stop`): sends SIGTERM to the runtime
  process its row recorded, only where the dashboard shares that process's pid
  namespace and the process with that id still started when the row recorded
  it. A contained session's runtime is its launcher's to stop, and a subagent
  ends with its session; both are refused saying so.
- **Broadcast** (`POST /api/repositories/<key>/broadcast`, `{text}`): one post
  to every working agent of a repository, each woken as a message is.

## Discussions

A thread is posted into whole (`POST
/api/repositories/<key>/threads/<thread>/posts`, `{text, in_reply_to, to}`): one
copy to everyone who wrote in it or was written to, sharing one post id,
answering the thread's latest post unless `in_reply_to` names another, and
bringing in any member `to` names. Each copy is headed by the discussion —
`[discussion «<first line>» · with <everyone else> · thread <thread>]` — so its
reader can answer everyone with `coordination_send`'s `thread`, and each is
woken as a message is. The answer says what became of each copy.

## You as a peer

The person is a full peer in every repository the dashboard serves:

- **What you are on** (`POST /api/user/description`, `{text}`): said on your
  row in every repository served, which `coordination_peers` lists last.
- **Holds** (`POST` and `DELETE /api/repositories/<key>/claims`, `{path}`): you
  hold an absolute, existing path the way a session's `coordination_lock` does,
  and an agent about to write under it is asked first, told `held by user — the
  operator locked this path`. Only what you hold can be given back; giving back
  another's hold is refused, naming them.
- **Notices** (`POST /api/repositories/<key>/notices`, `{text}`; `DELETE
  …/notices/<id>`): a fact every session reads at the head of each prompt, until
  it is withdrawn; the working ones are told at once as well.
- **Your inbox** (`POST /api/repositories/<key>/inbox/read`, `{ids}`): takes
  exactly those messages out of your mailbox, as read.

The same mailbox reads from a shell, in the repository of the working
directory: `lup-devtools coordination mailbox --id user` prints each message
headed with who sent it and its post, and `--take` takes them as read.
`lup-devtools coordination send <text> --to <agent> --as user` answers, signed
so the reply comes back to you, and `--reply-to <post>` puts it in that post's
thread.

## Budgets

The dashboard every launch holds also keeps a budget over the accounts its
sessions draw on, the way a torrent client limits a link: a speed limit, a
reserve kept back for the person's own use, a cap on how many agents work at
once, priorities, per-agent caps, a schedule, and a slower set of limits one
key turns on. It judges at every look of its herald, whether or not a page is
open, so a limit holds with nobody watching. The person's own sessions —
every session no other session's shell opened — are never held, nor counted
against a slot; their subagents, and the sessions an agent opens, are.

### What it meters

An account is one runtime's login under one profile — `claude:work`,
`codex:default`, `default` naming the home no profile selects — and the
dashboard reads every one the served repositories can launch on, each
profile's included, every `poll_seconds`. Its windows are what the provider
meters it in: Claude's 5-hour and weekly windows from the OAuth usage
endpoint, Codex's two self-describing windows from the app-server, and between
reads a Codex session's rollout, which carries its account's windows with
every token count. Each window shows how much of it is used, where even pace
stands — as much of it used as has gone by — how fast it filled over the last
hour of readings, and when it clears.

What each agent spends comes from what its runtime emits. A Claude session
launched with the dashboard sends its telemetry to the dashboard's own port
(8776 where it is free), bearing a token of its own rather than the page's
capability: each request's `api_request` event carries its cost and its token
counts, and its `claude_code.llm_request` span names the subagent that made it,
the two joined by the request id. That is what Claude Code 2.1.285 was
measured to emit; its metrics name a subagent only by its type, so they are
not exported, and the span needs `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`, which
the launch sets. A Codex session's spend is the token counts its rollout
records, and Codex prices nothing, so a Codex agent's rate and caps are in
tokens.

Every charge lands, once per request, in the budget's ledger at
`$XDG_STATE_HOME/lup/budget/ledger.json` (`~/.local/state/lup/budget/` by
default), beside each agent's priority and caps. A pipeline running in
process charges and waits on the same ledger through
`FinancialBudgetConfig(state_path=…, limits=…)`, so it shares one budget with
every launched session.

A launch says when the budget cannot see its session's spend: a container
joining no network reaches no dashboard, and a person who already exports
Claude Code telemetry somewhere keeps theirs. That session's account is still
metered and its limits still hold; only its own spend reads as none, and the
meter says `no telemetry` where the dashboard receives none at all.

### Limits

`[budget]` in the person's lup config sets them, never a project's:

```toml
# ~/.config/lup/config.toml
[budget]
pace = "even"        # hold agents spending a window faster than it passes
tolerance = 5        # points past a speed limit normal agents still work
reserve = 10         # the last 10% of every window is kept for you
max_active = 3       # at most three agents work at once
poll_seconds = 120   # how often each account's windows are read

[[budget.ceilings]]  # a speed limit on one window, in percent of it an hour
window = "5-hour"
per_hour = 15

[budget.accounts.work]  # every runtime's login of the work profile
max_active = 6          # or name one: [budget.accounts."claude:work"]

[[budget.schedule]]  # working hours, local to this machine
days = ["mon", "tue", "wed", "thu", "fri"]
from = "09:00"
to = "18:00"
reserve = 30

[budget.turtle]      # the slower limits the turtle puts in place
max_active = 1
```

The limits on an account are layered: the table's own, then its account's
entry, then every schedule entry holding now — one whose `to` is before its
`from` runs overnight, and `accounts` narrows one to the accounts it names —
then the turtle's while it is on, each layer replacing only what it names.
The turtle is even pace and one agent at a time unless `[budget.turtle]` says
otherwise. A `[budget]` lup cannot read holds nothing but a window used up,
and the meter says why.

Each agent is weighed against its account's limits, and the first that
applies holds it, saying why on its row and to the agent:

1. **A window used up** — every agent drawing on it, until it clears: `5-hour
   window used up until 14:20`.
2. **The reserve** — once a window reaches it, until it clears: `reserve
   reached: …, the last 10% kept for you until 14:20`.
3. **Its total cap** — until the operator raises or clears it; the operator is
   told once, as a desktop notice.
4. **Its rate cap** — until its last hour's spend falls back under it.
5. **The speed limit** — even pace, or a window's ceiling: low-priority agents
   hold as soon as the account passes it, normal ones once it is `tolerance`
   points past, high ones at twice that.
6. **A slot** — `max_active` working agents per account; the rest wait, high
   priority first and then whoever wanted one first. An agent that stopped
   calling keeps its slot for ninety seconds, so one thinking between two
   calls is not overtaken.

The hold is the pause's: an agent the budget holds waits at its next tool
call and goes on, unprompted, once the limit allows. A dashboard with no hold
store to place its holds in judges and shows, and nothing waits: the stream's
`holds` says which, the meter says `not holding`, and each row says what
`would hold` its agent.

### On the page

The meter heads the page: each account's windows as bars with a mark where
even pace stands, how fast each fills and when it clears, the limits in force,
how many agents draw on it and how many are held, and the turtle. On a phone
it folds into the top bar, one line an account, its fullest window. `Space b
t` or `:turtle [on|off]` turns the turtle, kept in the person's config so it
outlives the dashboard, and the status line shows `🐢 turtle` while it is on.

An agent's tree row says what holds it and what it spends — `$1.84/h · $7.94`,
or tokens where nothing priced it — and its priority where it is not normal.
Its context says the same at length, with its priority and caps editable in
place: `Space b p` or `:priority [agent] high|normal|low`, and `Space b c` or
`:cap [agent] $2/h $10` — dollar amounts or token counts (`500k/h 2M`), `/h`
making one a rate, every cap at once, nothing clearing them. The routes behind
them are `POST /api/budget/turtle` (`{on}`) and `POST
/api/repositories/<key>/sessions/<member-id>/budget` (`{priority, caps}`).

From a terminal, `dashboard budget` prints the same accounts and what this
repository's agents spent, and `dashboard turtle on|off`, `dashboard priority
<agent> <level>` and `dashboard cap <agent> --rate-usd … --total-tokens …`
set what the page sets, by an agent's name or id in the repository of the
working directory. The three that change something are the operator's, from a
terminal outside any agent session, as the page is.

### Switching profile

When an account's window is used up, the page says so once, sticky until it
clears, and names the profiles of that runtime with room — `:switch home moves
a repository's contained sessions to it`. It never moves anything by itself:
the providers' terms rule out moving work to another account automatically
when one runs out, so the switch is always the operator's.

`:switch <profile> [claude|codex]` on an agent's repository, or `harness
profile switch <profile> [--runtime codex]` in a checkout of it, hands the
profile's login to the container volume every contained session of that
runtime in the repository shares (`POST /api/repositories/<key>/profile`).
Claude Code reads its login file at every request, measured on 2.1.285, so a
contained Claude session runs on the new account from its next request. A
Codex session keeps the login it started with until it is opened again, and a
host session runs in its own account's home, which no volume reaches; each is
answered with the command that opens it again on the profile, and the page
keeps that answer in a notice. A contained launch on a profile other than
the one the repository's volume holds would move every session running on it,
so it refuses, saying how many, unless `--move-sessions` says that is meant.

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

A review is one buffer. Its bar says what the call is in plain terms — a
proposal of eleven files, a write that replaces a file, a shell command of five
steps of which two need approval — who asked and how that agent stands, and
under it, in the warning colour, what the policy asks about; `gd` walks the
parts it names. The buffer opens on the first of them. Its context holds the
rest beside it: the queue, the directory the call runs in, when it was asked,
the files, exceptions and markers, the thread, and `I` for the complete record
— the tool input and the whole question, pretty-printed. What a review said
and what it came to are its thread: the operator's remarks, the requester's
replies, and the answer, oldest first.

The requester's own account of the call stands in the context as prose,
labelled with where it was found and as the claim it is — what it is for is
the agent's to say, why it asks is the policy's — and never cut.
It is recorded when the call parks, from what the runtime's half reads there:
the note the tool call carries beside the command (Claude Code's
`description`, Codex's `justification` for running outside its sandbox), and
what the agent wrote since it last heard anything, read back off the
transcript of the conversation that made the call — a subagent's own. Where
it said nothing there, what its roster row says it is on stands in, labelled
as the session's; a proposal's is its `--why`, with each file's note on that
file. `review show` prints the same lines.

A command's buffer starts with what asked. Where its line runs several
commands, each one that asks or is refused comes first, with the verdict it
reached on its own, its reason and rule; where the policy judged the line
whole, the line as a whole is what asked, with the question's own reason. Then
the command, whole, the asking steps marked inside it, so a line that asks
twice is approved knowing both questions (`docs/permissions.md`). The steps
allowed on their own fold beneath with the steps whose effect shows only once
they run — each saying so quietly, "effect shown only after it runs · allowed
on its own" where its own verdict allowed it — information, never the cause;
the bar's one line names only what asked. Its file changes show the way an
edit's do, one diff per file in the order it writes them: the documents the
policy worked out when it judged the command, read off the review rather than
re-derived where it is read.

The dashboard titles requests from captured evidence: a file's action and path,
the number of files, or the command to run. The paths are relative to the
checkout the files lie in, found from the files themselves — a session kept
in one checkout editing another is titled in the one it edits — and the
queue's row and the desktop notice name that checkout where it is not the
queue's own. The checkout recorded with the call stands in where the files
lie in several.

A waiting review the page cannot answer is marked "can't answer here", with
why and the way out beside it: the terminal commands that answer it,
`uv run --directory <queue's checkout> lup-devtools review approve <id> --as
<who>` or `decline`, run with the code of the checkout keeping the queue,
which is the code that parked it; and, where the dashboard is about to
restart onto its checkout's newer code, that it restarts shortly and may
answer it then. That covers a review only another principal may answer, one
parked by newer code than the dashboard runs, and one whose record or
documents this code cannot read back.

Each file's header says, inline and never folded away, why that file needs
approval, in a few words read off the gate that decided its verdict and the
file's own facts — `protected`, `new devtools module`, `written whole,
66 lines`, `adds a # lup: note`, `adds a rule suppression` — or `automatic`
where the policy let it through; the policy's own sentence is a `K` away. The
context counts a proposal's files by those reasons, `7 protected · 1 new
devtools module · 3 written whole`. A protected file names the root that
protects it where its verdict records the rule it matched.

The default view includes files that require review and highlights newly
introduced rule exceptions. Files the policy allows automatically or
explicitly leaves to the native provider fold to their header, and existing
exceptions show in the full operation (`F`). A captured deferral means Lup
requests no approval for that file; the native provider still applies its own
permissions. Approval still applies to the exact complete submission. Where
recorded evidence cannot establish a file's status, it remains visible rather
than being treated as automatically allowed. `[` and `]` move between files,
and `Space f f` finds one by its path. Each file shows as a coloured, numbered
diff, its syntax highlighted by the file's extension; `Space v` shows the file
before or after instead, or the unified diff as text, and `Space w v` splits
before | after side by side. The lines a diff leaves out fold into one row per
gap, which `Enter` or `za` opens, and `f` shows the whole file with the
changes marked in place.

Every `# lup:` marker is marked where it stands, in its own colour and
labelled with its kind: an open note, parked work (`defer:`, and
`defer[<gate>]:` with its gate), a resolution claim (`solved:`), a
customization point (`template:`), and a rule exception (`ignore[<rule>]`).
`m` and `Shift+M` jump to the next and previous marker across the review's
files, opening the whole file where a marker stands outside the diff's hunks.
Rule exceptions are listed by rule in the context, with `n` and `p` to jump
between them; existing exceptions appear only in the full operation.

`i`, `a`, `o` or `Enter` on a line comments on it; `V`, then `j`/`k`, then `gc`
comments on the range picked; a click on a line number comments on that line
and Shift+click on another on the range between. A comment is a box beneath
its last line, `Esc` leaves it, `x` on its line deletes it and `u` brings it
back. Comments are drafts until sent — with a decision, or alone — and stored
in the answer, or the remark, as structured anchors: the path, the first and
last line, the side the numbers belong to (`before` or `after`), and the note.
They show on answered reviews where they were made, and in the thread.

The triage loop: a review opens in its note box. While the box is empty, `j`
and `k` move to the next and previous review, landing in its box, and `↓`/`↑`
and `Ctrl+d`/`Ctrl+u` scroll the diff behind it; the first letter typed makes
the box the operator's, and from then on every key types. `Ctrl+Enter`
(`Cmd+Enter`) approves; `Alt+Delete` declines — the forward Delete key, never
Backspace, so `Alt+Backspace` still deletes a word; `Alt+Enter` sends the note
and line comments without deciding. All three work from anywhere, the box, a
line comment or the buffer, and `Alt+↑`/`Alt+↓` move to the previous and next
review from anywhere too, the draft staying with its review. `Esc` reads in
the buffer and `c` comes back to the box. Holding a key cannot answer another
request, and `:w` or `:wq` writes nothing: an answer is deliberate.

Answering is immediate: the page shows the review answered and opens the next
one at once, in its box, and reconciles with the server's reply in a notice
that says how the requester hears of it — its waiter holding the review, the
mailbox and whether the session was woken, or nobody running to hear it — and
whether a copy went to a subagent's session. A refusal puts the review back
where it was, the note and line comments with it, and says why in a notice
that stands until dismissed and opens the review again. `Space u a` turns off
advancing, to stay on the answered request. New arrivals do not move a
selection already under review.

The command line (`:`) runs commands with Tab completion — `:approve`,
`:decline`, `:send`, `:review <id>`, `:agent <name>`, `:threads`, `:set`, a
line number — and the finder (`Space Space` for reviews, `Space f` then a
letter for agents, the inbox, discussions, History, a review's files, the
buffer's lines, every message, the commands, the keys or the markers) filters
as you type and previews what `Enter` opens. `Space` shows what the leader
does after a moment, `?` lists every key with its action's name, and `K`
shows what is attached to the line, file, step, message or tree row under the
cursor.

`Space y` copies a link to share a request without sharing a credential. Links use
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
and checks the origin of every submission, answering loopback's names and the
origins the person declared alone. These controls protect the browser
surface; they are not isolation against arbitrary processes running as the
operator's user. The session's filesystem and process boundary remains part
of the authority boundary.

## Personal keys

Every key the page answers runs one action with a stable name — `answer.approve`,
`down`, `find.agent`, `word.next` — and the help, which-key and the key finder
all read the one catalog of them the library declares
(`lup.devtools.dashboard.keys`), compiled into the page, so none of them can
say a key does something it does not. A person rebinds actions by name in their
own lup config, never a project's:

```toml
# ~/.config/lup/config.toml
[dashboard.keys]
"agent.next" = ["<A-Right>", ")"]   # a list replaces the action's keys
"help" = "<F1>"                      # one key alone is a list of one
"tree.stopped" = []                  # no keys unbinds it
```

Keys are written in Vim's notation: `<C-d>` is Ctrl+d, `<A-Del>` Alt+Delete,
`<leader>fa` Space then f then a, `gd` g then d. Each entry is checked on its
own, and one that cannot apply is refused saying what was written, why, and
the way through, while the rest apply: a name no action has (with the nearest
one that does), a key that does not read, a key the browser keeps for itself,
a key that would type into a box for an action that works while one is typed
in, or a key another action already holds where both act. A conflict refuses
the override that made it, and lup's own key stands. A key that starts a longer
one where both act runs after a short wait for the rest, and the report says
which.

The dashboard reads the file on every look and hands the keys in effect to
every tab on the stream; a tab says once at load what the person's keys did,
and again when the file changes. `:map` lists what applied and what was
refused; `:map <action> <keys>` tries keys in this tab alone, checked by the
dashboard as the file is; `:unmap <keys>` takes a key off whatever holds it;
and `:mapwrite` writes the tab's lines into the file, keeping its comments.
`uv run lup-devtools dashboard keys` prints the same report offline.

## On a phone or a tablet

Below 861 px the page is one column, not a squeezed desktop: a top bar names
what is in view in two lines at most, and opens the tree and the context as
drawers; an action bar above the tabs holds what the keys hold — Approve,
Decline and Send on a review, Write beside an agent, Post to all in a
discussion — every button at least 44 px; the five views sit in a tab bar under
the thumb, each with what it counts. Nothing lands in the note box, whose
keyboard would cover the diff. An answer takes two taps in different places:
the first opens a sheet saying what goes with it, its confirming button at the
top and Cancel at the bottom, where the first tap was, so a double tap cannot
confirm.

One strip under a review's bar steps through it — `‹ change 2/36 ▾ › full
file` — by exceptions where the review shows any, else by changes; tapping the
kind offers every kind with where the cursor stands in each, and `full file`
shows the whole file at the cursor until it reads `back to diff`. The editor's
bar drops what the top bar already says and folds what the policy asks about
to two lines, and a note, what an agent said, a message or a post folds to
four lines with `more`. A sideways swipe over the buffer moves to the next or
previous item, a long press on a line starts a range that taps stretch, and a
tap on a line with something attached shows it. Notices sit under the top bar,
never over its buttons.

## Behind a reverse proxy

The dashboard binds loopback alone, so a browser elsewhere reaches it only
through a reverse proxy on this machine, at the proxy's name. The person
declares each origin they reach it at in their lup config, never in a
project's:

```toml
# ~/.config/lup/config.toml
[dashboard]
origins = ["https://their.proxy.name"]
```

Each is a whole origin, `scheme://host[:port]` over http or https, with no
path: the page names its routes from the root, so a proxy serving it under a
path is not supported. Each is read as a browser writes it, lowercase and
without its scheme's default port. The Host check then answers each origin's
host beside loopback's, with and without that default port, and a write is
taken where its `Origin` is exactly a declared origin or the dashboard's own
address. Nothing else changes: any other name is refused as it is with no
origin declared, which is
what a rebinding site sends; the API still wants the capability; and the
dashboard still binds loopback alone. The dashboard every launch holds and
`dashboard serve` read the list the same way, again on each request where
the file moved, so a change holds from the next request with nothing to
restart. A file lup cannot read declares nothing: the dashboard answers
loopback alone and says why in its log and in `dashboard status`, and a
launch refuses the file.

The proxy needs no header rewriting. Caddy passes the browser's `Host`
through and flushes the stream as it arrives, so this is the whole of it:

```caddy
their.proxy.name {
    reverse_proxy 127.0.0.1:8766
}
```

`8766` stands for the port `dashboard status` names, which the dashboard
keeps while it stays free. The page opens through the proxy at its launch
address there, `https://their.proxy.name/#token=…`: `dashboard status` gives
it under `launch`, `dashboard open` and `dashboard serve` print it, and so
does a launch the operator made on a terminal. A session's launch, and a
launch whose output is captured, print none, since the address carries the
capability. The page keeps the capability for the proxy's origin as it does
for loopback's, and every link it builds or copies names the origin it was
opened at. Every session's status line links the first declared origin, as
the address the operator signs in at, capability-free ("When no page is
open"). `dashboard status` warns of a declared origin that is neither
https nor loopback, whose launch address carries the capability across the
network in the clear.
