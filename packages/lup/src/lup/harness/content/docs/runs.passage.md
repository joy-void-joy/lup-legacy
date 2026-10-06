# Runs

A job worth launching in the background is a job nobody can see. `lup.runs`
answers that with one directory and a protocol over it, so a run launched
detached, launched by somebody else, or launched before this shell existed is
fully readable — and reading it cannot perturb what it is reading.

## What a run writes

```
<run-dir>/
  manifest.json          what was scheduled, written before the first unit
  units/<step>/<item>.json   one result per landed unit, written atomically
  attempts/<step>/<item>.json  one claim per unit currently running
  artifacts/<step>/<item>/     whatever a unit produces besides its result
  artifacts/<step>/<item>/progress.json  how far into its own work a unit is
  run.log                a line each time something happens
  summary.json           how the run ended, written whatever the ending
```

Every write is an atomic rename, because a reader holds no lock: it sees a
complete record or none. The claim is dropped in the same breath the result
lands, so no instant exists in which a unit is neither running nor landed.

`<item>` above is the item's own text whenever that is already one safe path
component, which is every item a sweep ordinarily names. An item that is not —
empty, `.` or `..`, holding a `/` or a NUL, or too long for the longest file
the layout derives from it, a result's atomic `.<name>.json.tmp` under the
255-byte filename limit — is filed under a readable prefix of its text and a
digest of the whole of it. The item itself is never cut: the manifest, the
claim and the result each carry it whole, and every reader names a unit by
those rather than by its file. An item's text therefore never decides whether
a run survives.

Nothing about one unit ends the run either. A claim or a result that cannot be
written is that unit's failure, with its traceback, exactly as a step that
raised is; a unit whose result cannot be written at all is named in the
summary's `unlanded`, so the tally never quietly loses it.

The summary is what says a run is over. A unit count cannot: a pipeline whose
second stage failed never lands its fourth, so waiting for the total is
waiting forever. It is written from a `finally`, so it appears whether the run
succeeded, failed, was interrupted or crashed — and its absence beside a
directory nothing is touching is exactly the evidence that the runner was
killed. A crash records its exception in `crashed` and its whole traceback in
`crash_traceback`, with a line in `run.log` pointing there, because the
launching shell's stderr is the one place nobody reads.

A run started again in the same directory takes the earlier attempt's summary
down before it claims anything, writing what it said to the log: until this
attempt writes its own, the directory holds a run that is still going, and a
watcher keyed on the summary must not report the last attempt's ending.

## A claim is a lease

A killed runner — a power cut, an OOM, a `kill -9` — leaves its claims on disk
with no result beside them and no process to finish them. On disk that is
indistinguishable from a unit that is simply taking a long time, and age
cannot separate them: it grows for a healthy unit exactly as fast.

So a claim carries `renewed_at` as well as `started_at`, and the runner
re-stamps it from the same heartbeat that writes the log line. The interval
sits well inside the lease, so a runner that stopped writing lines has also
stopped renewing and the two readings cannot disagree. A claim nobody has
renewed within the lease is one nobody holds:

- `run monitor` reports it as `abandoned=` rather than counting it as running,
  and says so outright when every claim has lapsed and no summary was written.
- A resumed run frees the lapsed claims and re-runs those units, naming each in
  the log. Claims whose lease is live are left alone, so two runners sharing a
  directory do not free each other's work.

The process table is not consulted, here or anywhere in this package. Under a
sandbox `/proc` is PID-isolated, so a healthy run is indistinguishable there
from a dead one — a liveness answer that asks it is no answer at all on the
host a long job most often runs on. The `pid` on a claim is there for a person
diagnosing the machine they are standing on, and nothing decides on it.

## Resuming

A pipeline is resumable by default, and nothing has to be arranged for it. Each
unit's result lands in its own file as it completes, and reuse is decided by
fingerprint — a step's own declaration folded together with the fingerprints of
everything it depends on. Re-running the same pipeline over the same directory
reuses every unit whose fingerprint still stands and re-runs the rest, so an
interrupted run costs only what never landed. Editing a step changes its
fingerprint, which reruns it and everything downstream without anybody
maintaining the list of what that is.

Nothing here consults the process table. Under a sandbox `/proc` is
PID-isolated, so a healthy run is indistinguishable there from a dead one; a
liveness answer that asks the process table is no answer at all on the host a
long job most often runs on.

## Watching one

`run monitor <run-dir>` redraws a reading in place: the units landed against
the units scheduled, where each step stands, and what is being worked on. That
is for a person at a second terminal.

`run monitor <run-dir> --events` emits one line per landing, step change,
failure and stall, and exits when the run ends. That is for a watcher, which
sees lines rather than a screen — it is how an agent follows a job instead of
asking whether it is done. The first line is a baseline rather than a replay:
attaching to a run that has already landed four hundred units says so once.

`--once` takes a single reading, for a report.

Silence is the failure that matters, so a run whose heartbeat has stopped is
reported as stalled rather than left looking like one still working.
`--quiet-limit` is how long counts as silence; a pipeline of shell steps is
quiet for seconds, a solver sweep for hours.

### The estimate

A reading never repeats the runner's own estimate of the time left. A progress
bar smooths its rate over the last few landings, and units land in bursts —
one per worker as a batch of budgets expires — so that number says
twenty-nine seconds about thirty-six two-hour cells. The estimate here divides
everything landed so far by the whole elapsed time, which is the only estimate
bursty landings support. A runtime writing its own bar sets `smoothing=0` for
the same reason.

## What a unit says while it runs

A claim says a unit started and is still held. It cannot separate a unit forty
percent through from one spinning at step zero: from outside those read alike,
and age grows for both at the same rate. So a unit publishes the one thing only
it knows, into its own workspace:

```
artifacts/<step>/<item>/progress.json
```

`done`, an optional `total`, a `phase` — a word, never a number — and a
`detail` in the unit's own vocabulary. Never a rate and never an estimate:
those take two readings and the clock between them, and the reader is what
holds two readings.

The record sits in the workspace rather than beside the claim because it
outlives the claim: a unit that died at 2870 of 3000 in phase `fit` leaves that
reading next to its traceback, where whoever comes back to the failure is
already looking.

### Three doorways, one writer

- A callable step calls `context.report(done=…, total=…, phase=…, detail=…)`.
  The context knows the workspace, so the body names no path.
- A shell unit that can import this library calls
  `lup.runs.report.report_progress(workspace, done=…)`, taking the workspace
  from `$LUP_RUN_WORKSPACE` at the call site.
- A unit in any language at all spawns `uv run lup-devtools run report --done N
  [--total N] [--phase word] [--detail key=value ...]`, which reads
  `$LUP_RUN_WORKSPACE` itself. A `--detail` value is read as JSON where it is
  JSON — `supports=41`, `ok=true`, `shape={"k":1}` — and as text where it is
  not. A process per report, which is right at one report a second and wrong at
  a thousand.

All three write through `report_progress`, so the record cannot drift between
them.

Call it from the loop that does the work rather than at chosen milestones. A
report landing within a second of the one before it for the same workspace is
dropped, because progress is a sample rather than a log and the monitor reads
every two seconds — so the cheapest call site is the right one, and the unit's
result records how it ended whatever the last sample was.

### What the reader makes of it

`run monitor` draws one line per reporting unit, under the run's own three:

```
family-3:  41%|████      | 2460/6000, fit · supports=41 · 41 steps/min · eta 0:12:30
```

The rate and the time left are the monitor's, taken from two readings that
share a clock. That estimate is honest at this grain in a way the run-level one
is not: one unit's own work advances steadily, where a run's landings come in
bursts. A unit that declared no `total` gets a count rather than a bar.
`detail` renders as sent — the unit's keys, in its order, nothing filtered and
nothing cut. At most twelve units get a line and the rest are counted on the
activity line, so a screen never implies it is showing everything.

A unit that reports nothing falls back to the last line it printed, so a shell
step is readable without cooperating at all. That line is only as fresh as the
unit's own buffering: one printing without `flush=True` shows a stale line,
which is the unit's to fix rather than the reader's.

`--events` yields a line when a unit enters a new phase — `solve/family-3
entered fit at 1200/6000` — and never per sample, so an agent following a
thousand-unit sweep is woken by changes rather than flooded by counts.
`--once` prints every running unit's line, which is where a watcher goes for
the counts themselves.

A landed unit needs nothing new. `outcome` already tallies whatever word a unit
lands under, so a step setting `outcome="certified"` reads as `certified=6
failed=2` with nothing added — and a per-key breakdown of `detail` is the
project's own command over its own results, not the monitor's.

## Declaring the work

A `Pipeline` is a name and a list of steps. A step is a `CallableStep` around
a Python callable or a `ShellStep` around a command, it names what it depends
on, and it may fan out over items — `FixedItems` when the sweep is known when
the pipeline is written, `ComputedItems` when its width is read off what a
dependency landed.

A step body takes a `StepContext`: the run, which item it is, its dependencies'
results, and a `workspace` of its own to write into. A `ComputedItems` callable
takes the same thing without an item — a `FanContext` — because deciding a
width usually means reading what a dependency *wrote*, and `artifacts_of(result)`
is where one of those results put it. Neither reconstructs a path by hand.

```python
pipeline = Pipeline(
    name="sweep",
    workers=8,
    steps=[
        ShellStep(id="discover", command="ls corpus/*.json"),
        CallableStep(
            id="solve",
            dependencies=["discover"],
            over=ComputedItems(compute=each_line),
            body=solve_one,
            retries=1,
        ),
        CallableStep(id="report", dependencies=["solve"], body=summarize),
    ],
)

if __name__ == "__main__":
    pipeline.main()
```

`main()` serves the pipeline as a command line, so every pipeline gets the
same flags without writing any: `run`, with `--directory`, `--only`, `--from`,
`--force`, `--workers` and `--fresh`, and `plan`, which prints each step's
fingerprint. A shell step reaches its unit's coordinates through
`$LUP_RUN_ITEM`, `$LUP_RUN_STEP`, `$LUP_RUN_DIR` and `$LUP_RUN_WORKSPACE`
rather than through substitution into its text, which a command full of `$`
and `{}` would have to fight.

`examples/monitored_run.py` is a working one, and needs no credentials.

## Rerunning one part

A landed result records the fingerprint it was computed under. A step's
fingerprint folds its own declaration — its `params`, and its body's source
where that can be read — together with the fingerprints of everything it
depends on. So a step is reused when nothing it rests on has changed, and
recomputed when something has, along with everything downstream of it, without
anybody maintaining the list of what that is.

That is what makes the flags reach one part:

- `--only a b` runs exactly those steps.
- `--from encode` runs that step and everything downstream.
- `--force encode` reruns a step whose fingerprint has *not* changed, and
  everything downstream, which rests on a result just recomputed.
- `--fresh` discards every landed result first.

A run killed halfway needs no flag at all: the units that landed are reused
and the ones that never did are run, because that is what the fingerprints
already say. A step whose body cannot be read — one built at runtime, one
defined in a REPL — rests on its declared `params` alone, so editing it will
not invalidate what it landed; declare the parameters that matter.

A step whose dependency has landed nothing refuses rather than guessing, which
is what `--only` on a middle step gets when its input was never run.

## Failure

A unit that raises lands a result with its whole traceback, and its step
fails. Steps that read a failed step are skipped and named in the summary; the
rest of the run carries on, and everything that landed stays landed. `retries`
buys a step another attempt, and the tracebacks of the attempts it survived
are kept on the result that succeeded — a step that passes on its third try is
not the same as one that passed.

A `ShellStep` writes its output to files under `artifacts/` and points at them
from its result, because a step printing a hundred megabytes is an ordinary
step: a record that swallowed it would be unreadable, and one that kept a
prefix would look complete while being cut.

Shell steps fork from a process running a worker pool, which Python warns
about: a fork from a multi-threaded process can deadlock if the child touches
a lock another thread held. `sh` forks and execs promptly, and `sh` is what
this repository's rules mandate over `subprocess`, so the warning stands
rather than being silenced. A pipeline whose shell steps are long and few is
unaffected in practice; one that fans a shell step out over thousands of tiny
items is the shape to watch.

## Writing the layout without the runtime

A project with its own runner does not need `Pipeline`. Writing
`manifest.json`, a result per unit under `units/`, and a heartbeat line makes
that runner followable by the same `run monitor`, because the monitor reads
the layout rather than the runtime. `lup.runs.directory.RunDirectory` is where
every path is spelled, so both ends meet there instead of drifting.
