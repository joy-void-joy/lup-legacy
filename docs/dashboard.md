<!-- Generated from lup.harness.content.docs.dashboard by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Dashboard

The `dashboard` module gives the operator one page over every session's parked
reviews, in every repository they work in, with each repository's setup beside
them. Declining it removes the `dashboard` commands and the service every
launch holds; `review list`, `show`, `approve`, `decline` and `cancel` remain,
and a parked call is answered from the terminal.

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
many sessions and over which repositories; inside a session it reports only the
address the launch handed it. `dashboard stop` stops it now, and the next launch
starts it again. `dashboard open`, `dashboard stop` and `dashboard serve` are
the operator's, run from a terminal outside every agent session.

`dashboard serve` serves one in this terminal instead, with a capability of its
own, over the current repository or each `--root <checkout>` named, until
Ctrl+C. `--host` picks a loopback address, `--port` its port, and `--no-open`
keeps the browser closed.

## What the page shows

Reviews are grouped by repository, then by the session that asked, named as the
roster names it. A review whose requester is gone is expired, recording why: at
once where the roster saw that session end, after an hour where the roster
never knew it, since a session it never recorded cannot be told from one that
has not joined yet. The dashboard sweeps every ten seconds, and
`review list` sweeps its checkout before it lists. A retry of an expired
review's call parks a fresh review.

One producer serves every open tab. A checkout's queue is read again only when
its relay changed on disk, and a settled review is projected once, so an idle
page costs a few file checks a second however long the history behind it.

Until the first snapshot arrives, the page shows loading with unknown counts.
Reconnecting or unreadable queues remain visibly incomplete; only a fresh,
complete snapshot can confirm that no requests are waiting.

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

The decision is recorded in the
same durable relay that the terminal commands use, so a browser and terminal
answering concurrently cannot replace each other's answer. Session notification
is best effort after the answer is saved. The browser can advance while the
server completes delivery; a missing route or failed delivery does not erase
the answer. Each browser answer retains a separate notification outcome
in `.lup/review-notifications/`, bound to its question, fingerprint and answer
timestamp. Answered requests show a compact status with expandable details:
mail queued, native queue accepted, failed or unconfirmed. Interrupted attempts
remain unconfirmed; diagnostics failures never undo the recorded approval.
Native retries notify only a unique registered requester whose bound native
session matches the request. Queue acceptance does not prove the agent read
the message. A native-hook approval still requires the agent to retry the exact
tool call. The dashboard never executes a reconstructed command.

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
