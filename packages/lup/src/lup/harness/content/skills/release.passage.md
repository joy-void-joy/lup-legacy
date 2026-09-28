# Cut a Release

`dev release` carries out a release. This decides what it should be, because
two of the things a release needs cannot be derived from the tree: which part
of the version moves, and whether the entries under `## Unreleased` describe
what actually landed.

## Input

**Level** (optional): {{ arguments }}

## Process

### 1. Read the range before proposing anything

```bash
uv run lup-devtools dev release <level> --dry-run --json
```

It reports the version it would move to, the date, whether a section is open,
and what the pending breaks ask of a caller. Each is one file under the
library's `migrations/pending/`; the release moves them into
`migrations/<version>/`, stamped with the commit each landed in, and renders
their prose into the section it closes. Read the plan beside the range itself:

```bash
git log --oneline <last-tag>..HEAD
uv run lup-devtools dev migrate pending <last-tag>
```

Where there is no tag yet, the range is from the release branch.

### 2. Settle the level

**A break decides it, not a count of commits.** `dev migrate check` has
already refused anything undeclared, so the pending files are the record of
what breaks — read them rather than guessing from diff size.

Pre-1.0, a break goes in the minor by convention; the command does not encode
that, because what a leading zero means is the project's to say. Recommend a
level with the evidence for it, then {{ ask }}

**Never pick the level silently.** It is the one judgement a reader of the
changelog cannot check against the code.

### 3. Read the open section against what landed

The entries under `## Unreleased` were written by whoever landed each change,
one at a time, without knowing what the release would turn out to hold. Before
closing it, check that it says what the range says: an entry per change that
an adopter would notice, and none for a change nobody outside would see.

Where an entry is missing, write it — that is an edit to `CHANGELOG.md` and a
commit of its own, before the release. Where the section is empty and the
range is not, stop and say so: a release whose changelog says nothing is a
release nobody can read.

### 4. Verify every claimed-resolved note before any of it ships

A `# lup: solved:` marker is a claim somebody made about their own work, and
only the verify-solved pass retires one. A release carries every standing
claim into a version an adopter pins, where nothing will read it again — so
the pass runs here, on every release, rather than whenever somebody thinks of
it.

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
Reason: the release closes the changelog, moves the version and the pending
migrations, makes a commit and creates a tag, and the tag is what a publish
workflow acts on.

```bash
uv run lup-devtools dev release <level>
```

It refuses a dirty tree and an undeclared break. Neither is a reason to force
anything: commit or discard what is loose, and declare what broke as a file
under `migrations/pending/`, in a commit of its own before the release.

### 6. Land it, then push the tag

The release commit is on the integration branch and has to reach the release
branch the way everything else does — through a pull request, with its checks
green. **Push the branch first and the tag second**, and never the tag alone:
a tag pointing at a commit no branch contains is a release nobody can find
their way back to.

```bash
git push origin <integration-branch>
uv run lup-devtools git pr status --branch <integration-branch> --json
```

Merge through whichever route the repository settled on, then:

```bash
git push origin <tag>
```

**Pushing the tag is what publishes.** Where a publishing workflow is armed,
that push is the irreversible step — an index accepts a version once, and a
release pushed wrong is withdrawn rather than replaced. Request explicit user
approval before pushing the tag. Reason: it is the act that publishes.

### 7. Report

The version, the tag, what the section now says, what the breaks ask of a
caller, and where the publish run is. Name anything that did not happen —
a tag not yet pushed, a workflow that has not been armed — rather than
leaving it to be discovered.

## Guidelines

- **The level is the user's**, always asked, never inferred from commit count
- **Read the open section against the range** — its entries were written
  without knowing what the release would hold
- **No claimed-resolved note ships unverified** — the pass runs on every
  release, and a claim it cannot confirm is restored rather than carried
- **Branch before tag**, and the tag last of all
- **A declared break with no instruction stops the release** — that is the
  gate working, not an obstacle to route around
