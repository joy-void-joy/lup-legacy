# Quality pipeline

Three layers guard the repository. Each runs at a different moment and
catches a class of problem the others cannot.

## Commit time: the git guard

`uv run lup-devtools git hooks install` writes one hook per moment a
project declares, and names no check in it: each hook hands its moment to
`uv run lup-devtools git hooks run <hook>`, and the checkout git fired it in
runs the guards its own devtools declares there. The `pre-commit` moment
comes declared with two checks. The
first runs `uv run lup-devtools harness check all`, so a commit is refused
while any generated artifact differs from what its source renders. The
second runs `uv run lup-devtools dev check --conflict-markers --staged`, so a
commit is refused while a file it holds carries a conflict block a merge left
behind: a `<<<<<<< ` line, a `=======` line and a `>>>>>>> ` line, in order.
That second check runs for a merge's own commit too, which the drift check
stands down for, because that commit is where markers get committed. Each
costs seconds rather than the gate's minutes, which is what a check standing
between somebody and their next keystroke can afford.

The whole gate and `dev check --changed` run the same conflict-marker row,
over every tracked text file and over the changed ones. A fixture holding a
conflict on purpose is excused by a marker in the file, never by its path: a
line carrying `lup: ignore[conflict-marker]` excuses the blocks that open in
the paragraph it heads, up to the next blank line.

The whole gate is not among them. It belongs to the pipeline below, where a
runner spends the two minutes rather than the person who is still working: a
hook charging that to every push would buy only the interval between pushing
and the pipeline answering, and would charge it against the loop that has to
stay tight.

Which hooks a project arms is its own declaration, so one that guards a
second moment — or runs its gate under another name — says so instead of
forking the module that writes them.

The hook names no check because every worktree of a clone runs the one
shared hooks directory, each at a revision of its own. A hook spelling its
checks out was written by one revision and run by all of them: a check that
grew an option failed every commit in a worktree cut before it, and a hook
written from an older checkout ran none of the checks a newer one declares.
Written from any revision the hook is the same file, so a guard added,
dropped or changed reaches every checkout at the revision it holds, and
arming stays a once-per-clone act. Handing the moment over costs one start of
the checkout's devtools at each moment, the settle moments included.

One skew is left, and the hook tolerates it alone: a checkout older than `git
hooks run` itself, whose devtools runs but has no such verb, so declares no
guard the hook could reach. Its commit goes through with one line naming the
checkout. The case is told apart narrowly — typer's usage error exits 2, and
only where the verb's own help also exits 2 while the CLI's exits 0 is the
verb missing from devtools that run — so a broken environment, which `uv`
reports as 2 too, and a check that refused with 2 still refuse. Every other
failure refuses the commit as it came.

`git worktree create` arms them, re-running install refreshes a body left by
an older library and clears any this wrote at a moment nothing declares any
more, `git hooks status` says what a clone would run at each moment and lists
the checks behind each, and `uninstall` removes them. A hook written by
anything else is reported rather than replaced. Installing is a host step: a
contained session holds the shared hooks directory read-only, and `git hooks
install` there writes nothing where every moment is already current and
otherwise refuses with the exact command to run from a terminal on the host.

The commands the guards run are ones the pipeline runs too, reading the same
verdicts `dev check` reports, so the places that can refuse the same work
reach one computation instead of several that can disagree.

The check reads every generated artifact every time, with no path pattern
deciding when it applies. It costs well under a second, and the alternative
is a second belief about which commits could change generated output: the
sources compiled into the plugin trees are copied there verbatim, so
rewording a comment in one makes both trees stale without changing anything
either does.

Unique catch: a canonical harness edit committed without its regenerated
artifacts, or a hand-edit to an owned artifact, is refused before the commit
exists instead of minutes later in CI. Formatting, lint, type, and test
problems are deliberately not duplicated at commit time; the pipeline below
owns them. Two things a hook cannot refuse: a partial stage — canonical
source staged while its regenerated artifacts sit unstaged in an otherwise
current worktree — because it reads the worktree rather than the index, and a
`--no-verify` commit, which skips every hook by asking to. Both are the layer
below's, which is why both layers exist.

## Every pull request and push: quality and harness drift

`.github/workflows/quality.yml` runs on every pull request and on pushes to
`main` and `dev`. Its first step is `harness check all` — the same command
the commit hook installs, spelled from the same constant — and its second is
`dev check`: `ruff format --check`, `ruff check`, `pyright`, both `pytest`
suites, the review-note report, the rule sweep (line rules and project rules,
seam boundaries and library placement among them), hook reachability,
generated-tree drift, the
guidance budget, and — while `[tool.lup] template` still stands — the tighter
scaffold budget that holds guidance to the share a template may spend of it.

Unique catch: this is the authoritative gate. It binds whether or not a
contributor armed the git guards, and it is what the push guard runs
locally — so an armed checkout meets the same bar earlier rather than a
different one. It never regenerates or commits.

## Scheduled: native nightly

`.github/workflows/native-nightly.yml` runs on a daily cron and on manual
dispatch. The deterministic `evidence` job installs the real Claude and
Codex CLIs and runs `uv run lup-devtools harness doctor all
--strict-evidence` against the typed evidence ledger. The secrets-gated
`native` job runs the full `pytest -m integration` lane across the installed
CLIs.

Unique catch: breakage observable only through a real native CLI boundary —
installed-version drift against the evidence ledger, and live hook, plugin,
and session behavior — which is too slow and credential-bound for the
quality lane above. Release-evidence rules for this lane are in
[contributing.md](contributing.md).

## What a project built on lup runs in CI

One command:

```yaml
- run: uv run lup-devtools dev check
```

That is deliberately the whole of the gate. `dev check` is the same bar a
checkout runs locally — format, lint, types, tests, review notes,
the rule sweep with seam boundaries and library placement among its rules,
generated-tree drift, the guidance budget, and the scaffold budget where a
repository is still a template — so a green local run and a green pipeline
cannot mean different things. Putting `harness check all` in a step ahead of it, as this
repository does, buys a faster and more specific refusal of the one failure
a contributor can produce without running anything; it reads the same verdict
either way.

`uv run lup-devtools git hooks install` is the local half, and it is
the project's to arm rather than the framework's to impose: it writes into
`.git`, which is the contributor's, not the repository's.

Nothing is generated into an adopter's `.github/`. A workflow file is the
project's own, and a framework that wrote one would be claiming a schedule,
a runner, and a trigger policy that are not its to choose. This repository
generates its own `quality.yml` because it is *this* project's workflow; an
adopter writes the three lines above wherever its pipeline lives.

## Why the guard runs at commit time and in CI

ADR-013 in [dev-tooling-decisions.md](dev-tooling-decisions.md) records the
decision: a check that runs when somebody remembers to run it is a warning,
so the drift check sits on the path a commit must cross. The hook catches it
before history is written; the pipeline catches a hook nobody installed, and
the partial stage a worktree read cannot see.
