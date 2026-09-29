# Resolver supervisor

A resolver run drives nested agents through sessions it opens and asks
itself, so the harness never sees them and the operator has nothing to
watch. `ConsoleResolverObserver` narrates each durable transition, but a narrated run is still not a
supervisable one: concern-level state has no shape in a terminal, a dozen
open questions are not a form, and a parked run needs `jq` to read.

The supervisor is that missing surface, and it is an ordinary reader. It
holds no channel to a resolver process, so there are no modes: a run that is
moving, one parked overnight, and one that finished last week are all
reachable — and answerable — through the same page.

## Opening it

```text
uv run lup-devtools resolve supervise
uv run lup-devtools resolve supervise --run-id resolve-a45d2cd2c321
```

Without a run id the page opens on the runs rail alone, which reports an
unreadable run as its own row rather than hiding it. Selecting a run streams
its record, puts its open questions first — grouped by concern, groups still
waiting sorted ahead of settled ones, settled ones folded to one-line records
— and takes the accept/reject decision on its review branch.

`resolve --supervise` is sugar for a long `--wait` plus a spawned
`resolve supervise`, terminated when the run exits unless
`--supervise-linger`. It is a convenience, not a mode — the same page answers
the same run started any other way.

## The doors

Every surface writes an *offer* into the run's mailbox; nothing writes an
answer. A promoter inside the run turns offers into answers, which is what
makes "first answer wins" a decision rather than a race, and what keeps a
mistyped free-text value correctable until it counts.

| Door | How |
| --- | --- |
| the page | answer form, *Park run* |
| a rerun | `--answer <question-id>=<value>` |
| another shell | `resolve answer --run-id <id> q=value`, `resolve questions`, `resolve park` |
| a worker | its own `queue_questions` / `await_answers` tools |

Partial answers are legal. A question is answered by whoever knows that
decision, whenever they know it, so answering one of six open questions is
the normal case rather than a validation error.

**Not everything a worker has to say is a question**, and reading only the
row above is what makes it one. Three tools reach out of a worker and only
one of them blocks:

| What it is | Tool | Does it park the run? |
| --- | --- | --- |
| a decision you cannot continue without | `queue_questions` / `await_answers` | yes |
| a gate your concern was not approved for | `request_allowance` | yes, and resumes where you stopped |
| anything else worth saying | `send_message` | no |

Measured, in #202: two workers in one run blocked on material questions that
carried no decision — one needed to delete scratch files it had created
itself, the other needed to fix its own virtual environment. Both were
refusals of ordinary commands, and neither was a thing a human had an opinion
about. A worker that reaches for the blocking tool because it is the only one
it was told about spends the run's time on housekeeping.

Assembling the review branch is not special: it is the reserved
`integration-assembly` question with choices `approve` and `defer`, so every
door records that decision the way it records any other answer.

**Park is the clean abort.** *Park run* writes `park.request`, which ends
every open wait in that run and lands on exactly the headless park state,
printing the same flag-carrying rerun recipe. Ctrl-C is the dirty one:
`KeyboardInterrupt` is a `BaseException`, so the resolver's failure recording
does not catch it.

**Draining is the other verb, for a run that is working rather than waiting.**
A worker inside a model turn waits on nothing, so park never reached one and
killing was the only way to end a busy run — which discards the uncommitted
edits of each interrupted round along with its reviewer feedback and round
counter. `resolve drain` is observed at the top of a round, after the
previous one is committed, and at the boundary between dependency batches.
Nothing is failed and nothing is written off, so resuming costs only the turns
that had not finished. A satisfied drain is cleared on resume the way a stale
park is, or the run would stop again at the first boundary of the resume that
answered it.

## Resuming

Offers only become answers when a promoter takes them, and the promoter lives
inside a run. A parked run therefore needs something to start it again — the
page's *Resume run* button spawns `resolve --run-id <id>`, so the
surface that collected the decisions is also what spends them.

## Reading state

The page reads `state.json` through `ResolverStateRepository.load()`, which
takes no lock, and reads the mailbox directly.

**The mailbox is authoritative for anything pending.** `state.json`'s
`questions`/`answers` copies are a fold of it, written by the run as it
promotes — correct once the run finishes, and behind while it moves. So
pending questions and the parked status are derived from the mailbox, never
from the phase: workers ask mid-turn during `workers`, and the phase strip
legitimately reads `workers` while a human is being waited on.

Read only `state.json` among the state files. `save()` writes its six files in
sequence, so a concurrent reader can see a fresh `state.json` beside a stale
`questions.json`. Each file is atomic; the set is not. The five sidecars are
strict projections and carry nothing `state.json` lacks.

`live` means "this run is still moving", derived from phase plus recent
writes — never by probing `.run.lock`. A shared lock held even briefly can
make a concurrently starting run's `LOCK_EX | LOCK_NB` fail, so a viewer must
never touch it. Liveness only changes how a run is displayed; every run is
answerable regardless.

Transitions reach the page from the run's own journal: the record is the
stream. `/api/runs/{id}/events` follows `journal.jsonl` over SSE — a
reconnecting reader resumes exactly from the last sequence it saw, and a
fresh one is handed a bounded recent tail rather than the run from zero,
because its current state comes from the projection and replaying a long
record whole is what froze the reader the stream exists to serve. Record
older than that tail stays reachable through the paged journal route, one
bounded page at a time. There is no hub and no cross-thread hand-off,
because there is no publisher: the run writes files and the page reads them.

The trace draws every event either union can record — a roster test reads
the switch arms back out of the page, so a new event fails there rather than
in a record someone is trying to read — follows the newest entry until the
reader scrolls away, and narrows by concern, actor, text, or kind. An event
the page has never heard of is drawn raw rather than dropped.

## Security

The supervisor binds loopback and refuses anything else outright. Middleware
additionally rejects any request whose `Host` header is not `127.0.0.1:<port>`
or `localhost:<port>`. A loopback bind stops remote packets, but not DNS
rebinding — where the browser treats this origin as the attacker's own, so the
same-origin policy does not apply and CORS cannot help. The `Host` header is
what still differs.

Both halves live in `lup.web.loopback` and the setup page keeps the same
posture, because what a local surface is worth attacking is decided by what it
writes: this one answers a resolver's questions and decides its review branch,
and that one writes the user's credentials into `.env.local`. A surface that
took the bind without the header check was reachable by any page the user
happened to have open, and had no way of knowing it.

CSRF needs no separate defense: mutating routes take JSON bodies, a
cross-origin `fetch` with `application/json` is preflighted, and no CORS
headers are emitted. Not defended: other processes on this machine, which
only a token would address.

The page is the `supervisor` surface Vite built into `lup.web`'s package
data, typed against the projections the routes serve and the journal's own
entries: a field renamed on the Python side fails the build rather than a
reader. The wheel carries the bundle, and bun is needed only to change it.
