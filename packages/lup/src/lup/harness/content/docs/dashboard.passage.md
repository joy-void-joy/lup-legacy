# Dashboard

The `dashboard` module gives the operator one page over every session, in every
repository they work in: what each session and its subagents are doing, what
they said to each other, a box to write to any of them, the reviews they
parked, and each repository's setup. Declining it removes the `dashboard`
commands and the service every launch holds; `review list`, `show`, `approve`,
`decline`, `cancel` and `wait` remain, and a parked call is answered from the
terminal.

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
output the waiter's own. So the session is woken with "ran", "applied" or
"declined" and the operator's note, and never retries the call. The command
runs in a fresh shell: what the session's shell did since, a `cd` or an
exported variable, does not reach it, since the policy judged it standing
alone. A call a shell cannot carry out — one placed outside the session's
sandbox, or a tool that is not a write or a command — is reported as
approved, for one exact retry the hook allows once.

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
<seconds>` ends it early, exit 3, carrying nothing out and saying to start
the same `review wait` again. A runtime can still stop it: Claude Code's
shell tool stops a command at its `timeout`, thirty minutes in the background
unless the call names more, so the refusal asks for the longest the tool
takes and to start the waiter again whenever it is stopped still waiting.

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
page is the newest one launched. The page is copied beside the dashboard's
state before it serves, so it keeps serving after the worktree that started it
is removed.

`uv run lup-devtools dashboard status` says whether it serves, where, for how
many sessions, over which repositories, how many reviews wait and how many tabs
follow it; inside a session it reads all of that from the dashboard's pulse
(below), never from the operator's private state. `dashboard stop` stops it
now, and the next launch starts it again. `dashboard open`, `dashboard stop`,
`dashboard serve` and `dashboard reopen` are the operator's, run from a
terminal outside every agent session.

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
thirty seconds reads as a dashboard that stopped. The status line runs
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
its relay changed on disk, and a settled review is projected once. An idle
dashboard costs a few file checks a look however many sessions it shows and
however long the history behind them.

Until the first frame arrives, the page shows loading with unknown counts.
Reconnecting or unreadable queues remain visibly incomplete; only a current
stream with every queue read can confirm that no requests are waiting.

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

## Setup

Each repository's setup — the integrations and steps its own CLI declares — is
a pane beside its reviews. The dashboard runs that repository's own
`lup-devtools setup serve` in a checkout of it the first time the pane opens,
and serves the page it draws under the dashboard's origin, behind a path
derived from the dashboard's capability. It stops with the dashboard, or once
no running session of that repository holds it any more. A repository that
declined the setup module serves no pane, and the pane says so.

## Reviewing requests

The dashboard titles requests from captured evidence: a file's action and path,
the number of files, or the command to run. The exact operation, requester,
rule, reason, command or captured file diff, and recorded answer remain visible.
The default view includes files that require review and highlights newly
introduced rule exceptions. Files the policy allows automatically or explicitly
leaves to the native provider, and existing exceptions, remain available in the
full-operation view. A captured deferral means Lup requests no approval for that
file; the native provider still applies its own permissions. Approval still applies
to the exact complete submission. Where recorded evidence cannot establish a
file's status, it remains visible rather than being treated as automatically allowed.
A shell command is shown the way an edit is, one diff per file it changes, in
the order it writes them: the documents the policy worked out when it judged
the command, read off the review rather than re-derived where it is read
(`docs/permissions.md`). The steps whose result exists only once they run are
listed beneath, each with the files it leaves so.
The file navigator shows change counts and supports searching paths. Select
one file to inspect its colored, numbered diff or complete Before, After and
Raw views; `[` and `]` move between files. A shared directory appears once,
with complete paths available for inspection. The queue, navigator and evidence
panels scroll independently; smaller screens offer panel switches.
Typed `lup: ignore[...]` comments are highlighted in the code and grouped by
rule; existing exceptions appear only in the full-operation view. Expand a
group for written reasons and occurrence links, or use `n` and `p` to jump
between exceptions.

Approve or decline one review with an optional note. Auto-advance opens the
next pending request after a successful decision; turn it off to stay on the
answered request. New arrivals do not move a selection already under review.
Use `j` / `k` for next / previous request, `Shift+A` to approve, `Shift+D` to
decline, `c` to open and focus the collapsed comment, and `?` for shortcut help.
Decision buttons stay visible beneath the selected evidence. Shortcuts pause
in text fields, and holding a decision key cannot answer another request.

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
refused an answer. Session notification is best effort after the answer is
saved. The browser can advance while the server completes delivery; a
missing route or failed delivery does not erase the answer. Each browser
answer retains a separate notification outcome in
`.lup/review-notifications/`, bound to its question, fingerprint and answer
timestamp. Answered requests show a compact status with expandable details:
mail queued, native queue accepted, failed or unconfirmed. Interrupted attempts
remain unconfirmed; diagnostics failures never undo the recorded approval.
Native reviews notify only a unique registered requester whose bound native
session matches the request, naming the `review wait` that carries the call
out; where a `review wait` already holds the review, the session is mailed and
not woken, since the waiter's end wakes it. Queue acceptance does not prove
the agent read the message. The dashboard executes nothing: an approved call
is carried out by the session's own `review wait`, or one exact retry.

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
