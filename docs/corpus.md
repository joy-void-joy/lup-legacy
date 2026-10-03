<!-- Generated from lup_template.harness.content.docs.corpus by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Corpus

A corpus is a body of claims somebody is prepared to be held to, together with
the evidence that backs each one and the corrections that have since retired
some. The scaffold's corpus types live in `src/lup_template/corpus.py`:
`Claim`, `Question`, `Evidence` as artifact or certificate, `Source`,
`Correction`, and the edges between them, and the ledger's generic commands
record, relate, list and check whatever a project declares.

**This repository declares none of them.** What it records in its ledger is
coordination — tasks, handoffs, and the sessions and outputs indexed as
pointers — and what it knows about itself it writes in `docs/`, where a
reader finds it without a log to query; `src/lup_template/kinds.py` is the
declaration and says so. An adopter that wants a corpus turns it on there:
list the corpus's kinds in `NODE_KINDS` and its edges in `EDGE_KINDS` beside
`Task`, and place them committed in `LAYOUT` so the corpus travels with the
code and is reviewed in a diff, or leave them local to each clone.

**They are the template's, not the library's, by decision.** Nothing in the
library consumes them; the ledger's charter is to declare no epistemics, and
the order in which a claim's standing is read is one; and every adopter edits
them — the grades, the priorities, what a question is. Copy this file and make
it yours. What the library keeps is mechanism: standing read on request, a
reader that follows a chain of premises, `prepared`, slugs, `moved_since`, and
the cite check.

## The failure it answers

One failure, in two shapes. A repository keeping a status per claim, moved by
a migration that copies added files and drops modifications, leaves claims
reading as evidenced while their artifacts are missing.
A correction register declaring itself authoritative over every other document
and applied to none of them leaves the canonical prose carrying stale figures
behind a list a reader has to consult first. **A claim keeps a label after the
thing supporting it goes away, and nothing reports the difference.**

The invariant that answers it is the ledger's: there is no stored status, so a
claim cannot outrun its artifact and cannot keep a label its evidence stopped
supporting. Standing is read, never stored, and may regress.

## Evidence rots against files

An `Artifact` or `Certificate` carries a `Validation`: a schema id naming which
checker's word this is, a digest of what was checked, and the digest of every
file it was checked *against*. That scope is the part that rots. The moment
one of those files changes, the evidence reads as `stale` and says which file,
and every claim it supported reads as `stale` on the next look — with nobody
having gone back to amend anything. A scoped path recorded with an empty digest
is pinned by the evidence itself as it is recorded, through `prepared`, so a
caller names files and never computes a digest.

A `Source` is external bytes under their own digest, so a claim read from a
page cites those bytes and not whatever the page serves next year. Read on a
machine with no working tree, evidence says `unchecked` rather than guessing.

A file is itself a node — the ledger's `File`, pinned by digest the same way
— and a claim or question `about` one names it through an ordinary edge. The
file reads `stale` the moment it changes and the explorer draws the line, so
what a claim is about is visible and rots on its own. The edge is
descriptive: a claim that must fall with a file rests on evidence scoped to
it instead.

## Sessions and outputs are indexed as pointers

`notes/` holds this repository's session data — per agent version, a
directory per session, its trace journal, the result documents it wrote, and
one `observable.jsonl` per `lup-devtools harness` launch — gitignored, per
checkout, hundreds of megabytes. The ledger indexes it without holding any of
it: `Session` and `Output`, from `lup.observability.sessions`, are among this
repository's kinds. A session is recorded when its directory opens, whether
`build_session_factory` opened it for the SDK or a harness launch opened its
transcript, and amended when it closes with how it ended and the journal's
digest pinned at that moment; each result document `save_session` writes is
recorded as an output, related to its session by `output_of`, the edge
`lup.observability.sessions` declares beside the two kinds and this
repository lists among its edges. A record carries the checkout it
was made in, because `notes/` is per worktree while the log is per clone, so
it reads `open`, then `fresh`, `stale` or `missing` from any worktree against
the tree that wrote it.

No trace, output or log byte enters the ledger or its blob store: both kinds
refuse an attachment, and a record is a path, a digest and metadata. What was
under `notes/` before recording started is not swept in.

## A claim refuses to hide contradiction

`Claim.standing()` reads, in order: `superseded` by a correction; `premise
regressed`, where something it rests on no longer stands; `contradicted`,
where a live counterexample and live support both stand; `refuted`; `stale`;
`verified` or `supported`; `unsupported`. Contradiction is reported as itself
rather than resolved, because a reader shown one side and not the other is
shown a lie by omission.

Every reading carries `sound`: the one bit every vocabulary shares, and what
the cite check reads. A stale, refuted, contradicted, superseded or
premise-regressed claim is not sound; an unsupported one is — it never claimed
more than it had.

## Premises propagate

A claim `rests_on` the claims it depends on, and a premise that no longer
stands takes the claim down with it, as deep as the chain goes, with the
reason naming which premise and why. That is the second half of the invariant
from the repository this came from — one landing invalidated ten keystone
claims at once — and it needs a reader that follows the chain rather than a
node's one-hop neighbourhood. The ledger supplies one: `Surroundings.standing_of`
remembers each answer and reports a cycle rather than following it.

## Questions are nodes

What is still to find out is a `Question`: open until a claim that `answers`
it stands, closed only by somebody saying why. Answered is read from the
answering claim's own standing, so a question answered by a claim whose premise
was later refuted goes back to open without anybody amending anything.

`priority` is a manual number on questions and tasks, higher first; map your
own words onto it. What the log itself can say about a node's weight is shown
beside it rather than folded in: `ledger show` counts the edges pointing at a
node by kind, so how much rests on a claim and how many claims address a
question are read fresh every time.

## Readable handles

Any node may carry a `slug`, unique across the log once taken, and every
surface that accepts an id accepts a slug: `ledger show ceiling`, a cite of
`lup:ceiling`, a handoff watching `ceiling`. Optional everywhere, because a
task is often one line and naming it is a cost; a claim somebody means to cite
will get one.

## Corrections supersede partially

A `Correction` records what was wrong and what to do differently, and
optionally where the mistake was noticed. What *survives* and what *changes*
live on the `supersedes` edge, because they are facts about the pair — and what
changes may not be empty, since a correction that changes nothing is a note.
The superseded node stays in the log, readable, with the correction pointing
at it. Evidence and sources are retired the same way: an artifact, a
certificate or a source a correction points at reads `superseded` before its
digests get a word, is not sound, and stops counting as support for the
claims it backed.

Nothing broadcasts. The one message a correction sends is to the author of
what it corrects, and only while that author is on the roster. Everyone else
meets it when the cite check, a listing, or a `--since` puts it in front of
them.

## The one rule about who may say what

A `verifies` edge refuses an author verifying their own claim. It lives on the
edge because only the edge sees both ends.

## Grades are ours

`Claim.grade` is validated against `GRADES` in `src/lup_template/corpus.py`,
six words with their meanings, taken from the mathematics corpus the design
came from. An adopting project replaces the table and nothing else.

## Citing a node from prose

A document names a node with an ordinary markdown link whose href carries the
`lup:` scheme — `[three of four](lup:ceiling)`. `dev check` reads every tracked
markdown file and **fails** a cite whose node is missing or not sound, naming
the file, the line and the reason; `ledger cite <doc>` runs the same reading
over one file. A document whose figures must never go stale is *generated*
from the corpus instead, which is what writeups are.

## Writeups

`src/lup_template/writeups.py` declares this repository's generated documents
the way guidance is declared: as parts. `docs/work-status.md` is the one it
declares — the tasks not yet finished, what among them waits on a person and
what each costs, the handoffs still open, and a stamp naming the newest
record it was generated from — over the coordination ledger, since that is
all this repository records. A project with a corpus declares a document
over it the same way: a numbered listing of the open questions, the claims
and where each stands, the corrections. Every kind such a document renders
is committed with the code, so it is a generated file like the rest:
`harness generate all` writes it and `dev check` refuses one that is behind;
`uv run lup-devtools ledger writeup` writes it too, and `--check` verifies
the file on disk. The prose is the
author's and every figure is the ledger's, cited, so a number in it cannot
outlive what supports it: a claim that stopped standing renders struck through
with the reason, and the cite check reports it besides.

## Recording it

```sh
uv run lup-devtools ledger types
uv run lup-devtools ledger record corpus:question "how deep do quotes nest?" \
    --slug quote-depth --json '{"priority": 2}'
uv run lup-devtools ledger record corpus:claim "quotes nest to depth two" \
    --slug quote-depth-two --text 2 --json '{"grade": "measured"}'
uv run lup-devtools ledger relate corpus:answers quote-depth-two quote-depth
uv run lup-devtools ledger record corpus:artifact run.log --attach run.log \
    --json '{"validation": {"schema_id": "pytest", "subject_digest": "…",
             "scope": [{"path": "src/parser.py", "digest": ""}]}}'
uv run lup-devtools ledger relate corpus:supports <artifact> quote-depth-two
uv run lup-devtools ledger relate corpus:rests_on <dependant> quote-depth-two
uv run lup-devtools ledger show quote-depth
```
