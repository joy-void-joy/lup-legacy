---
name: release
description: 'Cut a release or a candidate of one: settle the level, close the changelog, tag it — or promote the candidate that held'
---

# Cut a Release

`dev release` carries out a release. This decides what it should be, because
two of the things a release needs cannot be derived from the tree: which part
of the version moves, and whether the entries under `## Unreleased` describe
what actually landed.

A release can go out first as a **candidate** — `vX.Y.ZrcN`, published as a
pre-release, which an installer passes over unless asked for it or unless
nothing else is published — so the projects willing to try it do, before
everyone takes it by default. A candidate's commit carries its own version,
so a project pinned to one is told it holds one. What a candidate turns up is
fixed on the integration branch as usual, and the next candidate carries it;
other work goes on landing there too. When one holds, a plain release
**promotes** it: one commit on a branch cut from the candidate's tag that
changes nothing but the version, the changelog and the record of the breaks
it carried — checked against the candidate's tag before it is made, so what
ships is what was tested — landed like any release and merged back into the
integration branch.

## Input

**Level and stage** (optional): the arguments supplied with this skill invocation

A level — `patch`, `minor` or `major` — and `--pre` to cut a candidate rather
than the release.

## Process

### 1. Read where the release stands

```bash
uv run lup-devtools dev release [<level>] [--pre] --dry-run --json
```

The plan's `kind` says what the run would do:

- `release` — cut `version` from what the integration branch holds, and close
  the changelog section.
- `candidate` — cut `version`, a pre-release of `target`. Its number counts on
  from the tags already spent on that version, since an index accepts a
  version once. The section stays open, headed by the target and listing
  every candidate, and the pending breaks stay pending: they belong to the
  release.
- `promotion` — commit the release on `branch`, cut from the newest
  candidate's `commit`: the version moved, its section closed, and the breaks
  that commit carried kept as the release's record. Every file the promotion
  changes is checked against the candidate's tag before anything is
  committed, and anything but the changelog, those breaks, or a line
  differing only in the version is refused and undone. The release is tagged
  there and merged back into the integration branch, whose `since` commits
  landed after the candidate stay out of it and are kept.

It also reports the date, whether anything is open, and what the pending
breaks ask of a caller. Each is one file under the library's
`migrations/pending/`; a release moves them into `migrations/<version>/`,
stamped with the commit each landed in, and renders their prose into the
section it writes. Read the plan beside the range itself:

```bash
git log --oneline <last-tag>..HEAD
uv run lup-devtools dev migrate pending <last-tag>
```

Where there is no tag yet, the range is from the release branch. With a
candidate open, a plain release reads the release branch as the remote holds
it, so fetch before reading the plan.

**Where the release branch moved** since the newest candidate, a plain
release refuses, and says how many commits the branch holds that the
candidate does not. Which way on is right depends on what moved, so show it —
`git log --oneline <candidate-tag>..origin/<release-branch>` — and
Ask the user directly, offering concrete options, and wait for the answer: whether to cut another candidate or release what the integration branch holds now

`--pre` cuts another candidate from what the integration branch holds;
`--direct` releases it as the version itself, with no candidate first.

### 2. Settle the level

**A break decides it, not a count of commits.** `dev migrate check` has
already refused anything undeclared, so the pending files are the record of
what breaks — read them rather than guessing from diff size.

Pre-1.0, a break goes in the minor by convention; the command does not encode
that, because what a leading zero means is the project's to say. Recommend a
level with the evidence for it, then Ask the user directly, offering concrete options, and wait for the answer: which level this release is

The level is settled once per release. The first candidate names it; later
candidates and the promotion keep it without being told. Naming a different
one re-levels the release — a new version, counted from the last release,
whose candidates start again at rc1 — which is the same judgement again, and
asked the same way.

**Never pick the level silently.** It is the one judgement a reader of the
changelog cannot check against the code.

### 3. Read the open section against what landed

The entries under `## Unreleased` were written by whoever landed each change,
one at a time, without knowing what the release would turn out to hold. Before
closing it, check that it says what the range says: an entry per change that
an adopter would notice, and none for a change nobody outside would see.

After a candidate, `## Unreleased` gathers what landed since — a fix the
candidate turned up — above the section the candidates are cut toward. The
next candidate or the release folds it in; a promotion does not, because it
releases what the candidate shipped and those entries describe what came
after.

Where an entry is missing, write it — that is an edit to `CHANGELOG.md` and a
commit of its own, before the release. Where the section is empty and the
range is not, stop and say so: a release whose changelog says nothing is a
release nobody can read.

### 4. Verify every claimed-resolved note before any of it ships

A `# lup: solved:` marker is a claim somebody made about their own work, and
only the verify-solved pass retires one. A release carries every standing
claim into a version an adopter pins, where nothing will read it again — so
the pass runs here, on every release and every candidate, rather than
whenever somebody thinks of it. A promotion ships what the pass already ran
over when the candidate was cut — nothing but the version changes, and the
promotion checks that — so it needs no second one.

Run `/lup:verify-solved` and carry out its verdicts. What it decides is not a
formality: a claim the tree does not meet is restored to open feedback, and a
release with open notes is ordinary. A release with *unverified* ones is a
version whose record says a thing was handled because the agent that did it
said so.

Report what it retired and what it restored before going on. Where it
restores something that changes what the release should say — a break that
turns out not to be fixed, an entry describing work that did not land — go
back to step 3 and say so plainly rather than closing the section around it.

### 5. Confirm, then cut

Show the dry run's plan and Request explicit user approval before cutting.
Reason: the release closes or extends the changelog, moves the version and
the pending migrations, makes a commit and creates a tag, and the tag is what
a publish workflow acts on.

```bash
uv run lup-devtools dev release [<level>] [--pre | --direct]
```

It refuses a dirty tree and an undeclared break. Neither is a reason to force
anything: commit or discard what is loose, and declare what broke as a file
under `migrations/pending/`, in a commit of its own before the release. A
promotion is not held to the break gate: it ships what its candidate shipped.

### 6. Land it, then push the tag

A release or candidate commit is on the integration branch, and a promotion's
is on the plan's `branch`, cut from the candidate. Either has to reach the
release branch the way everything else does — through a pull request, with
its checks green. **Push the branch first and the tag second**, and never the
tag alone: a tag pointing at a commit no branch contains is a release nobody
can find their way back to.

```bash
git push origin <integration-branch-or-promotion-branch>
uv run lup-devtools git pr status --branch <that-branch> --json
```

Merge through whichever route the repository settled on, then:

```bash
git push origin <tag>
```

A promotion has already been merged back into the integration branch, which
now carries the closed section and the moved record beside the work that
landed while the candidate soaked; push it too. Where merging it back
conflicted in more than the changelog, the command said so and left the
merge to be done by hand — the release is tagged either way.

**Pushing the tag is what publishes.** The workflow builds the tree the tag
names as it stands — its manifest already says the version — and a
candidate's goes to the index and the forge marked as a pre-release. Where a
publishing workflow is armed, that push is the irreversible step — an index accepts a version once, and a release pushed
wrong is withdrawn rather than replaced. Request explicit user approval
before pushing the tag. Reason: it is the act that publishes.

### 7. Report

The version, the tag, what the section now says, what the breaks ask of a
caller, and where the publish run is. Name anything that did not happen —
a tag not yet pushed, a workflow that has not been armed — rather than
leaving it to be discovered.

For a candidate, say how a project opts in, since nothing takes one by
accident: `dev library git --tag <tag>` pins it from the repository, and
`dev library use published --version <version>` from the index, where a
requirement naming a pre-release is what lets the installer take one.

## Guidelines

- **The level is the user's**, always asked, never inferred from commit count
  — once per release, and again only to re-level it
- **Read the open section against the range** — its entries were written
  without knowing what the release would hold
- **No claimed-resolved note ships unverified** — the pass runs on every
  release and candidate, and a claim it cannot confirm is restored rather
  than carried
- **A promotion changes only the version** — cut from the candidate and
  checked against it before it is committed, so the integration branch never
  has to stand still; where the release branch moved, the way on is asked,
  not chosen
- **Branch before tag**, and the tag last of all
- **A declared break with no instruction stops the release** — that is the
  gate working, not an obstacle to route around
