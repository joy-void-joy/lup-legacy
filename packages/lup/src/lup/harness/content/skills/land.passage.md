# Land Every Branch

Drive every local branch to its terminal state. Each one is classified by whether its commits have reached the integration branch — the ones that have not get landed, the ones that have get cleared — so no branch sits in a silent bucket waiting to go stale.

## Arguments

- **branch-name ...** (optional): one or more branches to dispose of. If any are provided, runs in targeted mode over all of them. If omitted, sweeps every branch.

Raw arguments: `{{ arguments }}`

Parse the raw arguments into a list of **branch names**: split on whitespace and commas, dropping bare connectors (`and`, `&`, `+`). Every remaining token names a branch. An empty list means full sweep mode; otherwise targeted mode runs over the whole list.

## Process

### 1. Settle the working tree

Read a dirty tree before writing it, because only one of the three cases it can be is a commit:

- **On the integration branch.** Committing code here is forbidden. Survey first, then check the dirty paths against every branch that could already carry them — `git diff <branch> -- <paths>` reports nothing where one does. Work a branch already holds is a stale duplicate, to discard or stash rather than commit; work no branch holds belongs on a branch of its own.
- **On a feature branch, where the work is that branch's own.** Invoke `{{ commit_skill }}`.
- **Anything else.** Ask.

Report which case it is and what the comparison showed, and let the user settle it. A sweep is about to act on every branch in the repository; opening it with a commit nobody asked for is the one move no later disposition can undo.

## Targeted Mode (branch names provided)

1. Run `uv run lup-devtools git survey --json`.
2. Read `integration_remote` and each row's `on_remote` as step 2 of the full sweep says, then resolve every named branch against the survey. Report each name that matches nothing; stop only when none of them resolve.
3. Show every resolved branch's `disposition` and `reason` in one table, then {{ approval }}
4. Carry out each disposition's action from the table below, committing every `COMMIT` branch and taking it and every `LAND` branch through step 6, and every branch an open PR is driving through step 7.

## Full Sweep Mode (no argument)

### 2. Collect data

```bash
uv run lup-devtools git survey --json
```

Every branch arrives with a `disposition` and a `reason` already computed. **Do not re-derive them** — the classifier is shared with `git survey`, so a judgement made here would drift from the one made there.

**Read `integration_remote` before any row.** Every disposition is judged against the local integration branch, so the sweep is only as right as that copy is current:

- `behind` counts the commits origin's copy holds and the local one lacks — usually pull requests merged on the forge. While it is non-zero, work that already landed there reads as `LAND` here. Report it first, bring the integration checkout level (`git pull --ff-only` there, or a merge where the two have diverged), and survey again before acting on any row.
- `ahead` counts the commits the local copy holds and no remote carries. Report it with the sweep: every branch landed on top of them builds on history nobody else has, and pushing it is the user's call.
- An empty `remote` means origin carries no integration branch at all; say so. `null` means no remote was read, which `remotes_fetched` already says.

**Read `on_remote` beside every row holding unlanded work.** `false` is work this clone alone holds — the disposition does not change, but the table says so, because it is the work a lost disk takes with it. `null` means no remote was read.

### 3. Reconcile against the runs holding branches

Read `runs` before you read `branches`. Each entry is a resolver run holding branches out of the sweep, and `alive` says whether anything is still answerable for them.

A run with `alive: false` holds work that no verb here reaches: it is not landing on its own, and nothing will retire its leases. Report it before the table — the run id, how many branches, and what it was doing when it stopped, which the resolver module's `status --run-id <id>` command answers — and ask what should happen to the run before offering any per-branch action. Landing its branches by hand bypasses the join machinery the run never ran, which is the whole reason the lease holds them.

Do not present a dead run's branches as a to-do list. One decision about the run is the honest question; twenty-six decisions about its branches is the same question asked in a form that hides what it is.

### 4. Present the sweep

One table covering every branch, ordered `LAND` and `COMMIT` first (that is the work at risk), then the `KEEP` rows an open PR is driving (work already asked for, waiting on nothing but an order to merge in), then `DELETE`/`STALE`, then the rest of `KEEP`/`CURRENT`:

| Branch | Disposition | Unique | Behind | Rewr | Diff | Dirt | Remote | PR | Proposed action |

`Dirt` is the survey's `changes` — what that branch's worktree holds uncommitted. On a reserved workspace it decides the disposition, which is what `COMMIT` says: reserving a workspace claims nobody has started, and an uncommitted change contradicts it. Everywhere else it changes only what carrying a disposition out costs — a dirty worktree makes a delete refuse until forced, and forcing discards those files, so a dirty row is one to read before proposing anything.

`Behind` is the survey's `behind` — how many commits the integration branch holds that this branch lacks. **It is a signal, never a verdict**, and no disposition reads it: every merge moves the integration branch under every other branch at once, so a verb deciding on distance would retire a workspace because unrelated work landed. What it is for is the question left over once the verb is settled. A `KEEP` reserved workspace sitting far behind is one nobody has opened — its base is no longer somewhere a session would want to start — so name it and ask whether to refresh or drop it. Never act on the figure alone; a branch is not stale for trailing, and the current branch trails nothing by construction.

`Rewr` is the survey's `rewritten` — how many of that branch's unique commits already name a subject in the integration branch. Containment is decided by patch-id, which a rewrite changes, so a commit that landed rebased, reworded, or squashed reads as unlanded ever after and a pre-rewrite snapshot presents its whole history as work at risk. **It is a signal, never a verdict**: a shared subject is not proof, so where it is set, go and check — `uv run lup-devtools git preview <branch>` merges it in memory and says whether landing it would change anything at all, and pairs each commit no patch-id matches with the integration commit carrying its subject, reading it as rewritten only where the two change the same lines. Report what the comparison showed; never subtract it from `Unique` and never let it retire work on its own.

**Group the `LAND` rows by the run holding them**, where `runs` gives one, and label the group with the run rather than repeating it per row. Branches from one run are one situation; listed flat they read as unrelated work that happens to share a prefix.

**Report each group's union, not the sum of its rows.** Branches from a run are stacked, so a branch's `unique_commits` counts commits its siblings also carry — summing them multiplies the same work by how many branches contain it. `git rev-list --count ^<integration> <branch>...` over the group is the real figure, and the two can differ by more than a factor of two.

**Offer only the branches no sibling contains.** A branch listed in another's `contained_in` lands when that one does, so proposing both asks for a decision that has already been made. Name the ones riding along under the branch that carries them, so nothing looks dropped.

### 4b. The branches that exist only on a remote

`remote_branches` is every branch a remote carries that no local branch corresponds to. Read it as part of the sweep, not as an appendix: a branch whose local copy went when its work landed leaves nothing in `branches` to classify, so without this list nothing mentions it again and the remote keeps it for good. That is the silent bucket this command exists to empty, one clone removed.

**Read `remotes_fetched` before `remote_branches`.** Where it is false the remotes were not read for this survey — `fetch_complaint` says why — and an empty list then means nothing at all: it is what a repository with nothing stranded produces and what a fetch that never answered produces. Say so and treat the step as unperformed rather than as clean. Do not carry out any remote verb on those rows; get the fetch to work and survey again, and report that the remote bucket went unswept if it cannot be made to.

Each row carries the same `disposition` the local classifier gave it, so it means what it means everywhere else. What differs is the verb — a push, not a local delete — and that a remote branch has no worktree, no lease, and no dirt to weigh:

| Disposition | What it means here | Action |
| --- | --- | --- |
| `DELETE` | Its PR merged, or the integration branch already contains it | Offer `git push <remote> --delete <branch>`. Nothing is lost: the commits are in the integration branch, and where a PR exists GitHub keeps its head at `refs/pull/<number>/head` after the branch, the local copy, and the remote copy are all gone |
| `LAND` | Holds commits the integration branch lacks, with nothing driving them | **Never delete it.** The work exists only on the remote, and no local branch names it. Report it and ask whether to fetch it into a local branch — `git fetch <remote> <branch>:<branch>` — and take it through step 6 from there |
| `KEEP` | Protected, the scaffold carrier, or an open PR is driving it | Leave it. An open PR is step 7's business, and it will be driving a local branch too where one exists |
| `UNRELATED` | Shares no history with the integration branch | Report it and ask, exactly as for a local one. A default branch still holding an initial import is the usual cause, and replacing it is a decision about the repository rather than a step in a sweep |

**A remote delete is a push, so it takes the same explicit approval as everything else in step 8** — per branch, naming the branch and the PR that merged it. Nothing here is carried out because the disposition says so.

## Acting on Dispositions (both modes)

### 5. Act on each disposition

| Disposition | Meaning | Action |
| --- | --- | --- |
| `LAND` | Holds commits the integration branch lacks, with no PR driving it | Land it — step 6 |
| `COMMIT` | A reserved workspace holding uncommitted work | The work sits in no commit, on no branch and on no remote, so nothing but this row records it. Read the diff against the integration branch before proposing anything — where it reports nothing, the work is a stale duplicate to discard rather than commit. Otherwise committing it in its own worktree — the move step 1 makes — turns it into a `LAND` branch; take it through step 6 from there. Never leave it on the grounds that the workspace was reserved: that is what it stopped being |
| `DELETE` | Reached the integration branch, or its PR merged | `uv run lup-devtools git delete <branch>`; where `Dirt` is set it refuses, so compare that worktree against the integration branch and report what forcing would discard before asking. A worktree a live session was launched in or created is refused whatever `--force` says, naming that session: its work is committed and it may still be writing there, so report whose it is and leave the branch until that session leaves. A contained session's mounts refuse too, in either of the two shapes step 6 spells — a worker's read-only lease, or a pre-existing directory that is a mount point — and either way the branch and origin's copy are left alone. Nothing is lost where the work already landed, but the directory stays until the host clears it, so say so rather than reporting the branch gone |
| `STALE` | Every commit already cherry-picked into the integration branch | Confirm, then delete |
| `KEEP` | Protected, the scaffold carrier, an open PR is already driving it, a resolver run holds its lease, or it is a clean reserved workspace | Read which of the five it is: protected leaves it alone, and so does the scaffold carrier — `dev update` merges the copied half from it, and its tip is the next update's merge base however landed it reads, so `git delete` refuses it; an open PR offers it — step 7; a resolver run is step 3, and one with `alive: false` holds it forever; a clean reserved workspace is somebody's next session, so leave it — the same workspace holding work is `COMMIT` and is not this row |
| `UNRELATED` | Shares no history with the integration branch | Never rebase or merge it — both would replay an unrelated tree. Report it and ask; an adopted subtree or a wrongly-pushed branch are the usual causes, and neither is this sweep's to settle |
| `CURRENT` | The branch checked out here | Never delete; warn if it would otherwise qualify |

### 6. Landing a LAND branch

Land one branch at a time, oldest divergence first — rebase, merge, and push before the next branch is touched. Every branch that lands moves the integration branch, so a rebase run ahead of a sibling's merge carries a base that no longer exists.

Ask the user, per branch, which route to take:

- **Open a PR** — get into that branch's worktree: {{ relocate }}. Create one first via `uv run lup-devtools git worktree create <branch>` when `worktree` is null. Then run `{{ rebase_skill }}`.
- **Merge directly** — take the same route into the worktree and through `{{ rebase_skill }}`, then merge from the integration checkout with `{{ merge_skill }} <branch>`. `sync-base` has already pulled the integration branch in, so the merge is a fast-forward, and pushing it closes the PR the rebase opened. Suits small, uncontroversial work that needs no review.
- **Merge it from here** — the route when that branch's worktree is held read-only, which is a resolver worker's lease: `worker_lease` in `lup.sandbox.rail` mounts every sibling worktree that existed when the worker started read-only, so `{{ rebase_skill }}` cannot run there at all: its first write is refused before it reaches a file, and neither a sandbox escalation nor a command exclusion lifts a mount. An operator's contained session is under the other lease, `lease_for`, which holds every checkout of the repository writable — the routes above are open to it, and what it meets instead is the mount below. Merge from the integration checkout — `git merge <branch>`, then `uv run lup-devtools harness generate all` until it reports `ownership=present`, committing what that writes, then `uv run lup-devtools dev check`. One branch at a time, so a failing check still names the branch that caused it. No history is rebuilt, so this suits commits that are already atomic; what it buys over the routes above is that the checks run on the integrated result rather than on each branch alone.

  **Where the branch's worktree existed when the session started, its directory is a mount point, and `git delete` leaves it standing.** A contained session bind-mounts every checkout that existed at launch at its own path, writable, and a mount point cannot be removed from within its namespace: `git worktree remove`, the first step of `uv run lup-devtools git delete <branch>`, empties the checkout and unregisters it, and the final removal of the directory fails with `Device or resource busy`. `git delete` reads that as what it is — the entry gone, the directory a mount point — and carries on to the branch and to origin's copy, reporting the worktree as unregistered and its empty directory as the host's to remove once the container is gone. Say so rather than reporting the directory gone. A worktree cut inside the session is no mount point and removes whole.
- **Stage them, then fast-forward** — the route when another session is landing too. The integration checkout is one worktree shared by every session in the repository, and two merges running in it at once share an index and a working tree; that is the one collision no later step undoes{{ peers_see_it }}. Cut a staging branch from the integration branch — `uv run lup-devtools git worktree create <name>` — and do the whole run there: merge each branch into it, regenerate, and `uv run lup-devtools dev check` on the integrated result. Then take the integration checkout for `git merge --ff-only <name>` and nothing else, which is seconds rather than the length of the run. **A refusal from `--ff-only` is the signal, not the obstacle**: somebody landed while you were building, so merge the integration branch into the staging branch in your own worktree, regenerate, let the drift check report that it settled, and fast-forward again. What lands is the same merge commits the in-place routes would have produced, and no peer waited on you for them.
- **Retire it** — the work is not worth landing. After explicit confirmation, `uv run lup-devtools git retire <branch> --reason "<why>"`, which pushes, opens a pull request, closes it without merging, and only then deletes.

Never choose a route on the user's behalf: a `LAND` branch by definition carries no PR expressing intent, so the intent has to come from them.

**Retiring is how a `LAND` branch ends, and `git delete` is not.** A branch the integration branch never absorbed, deleted with no copy on the remote, leaves its commits reachable from nothing and a collector free to take them — and `git delete` says so only at the moment it does it, which is too late to be a choice. Opening a request and closing it unmerged leaves a copy that outlives the branch: GitHub writes the head of every request to `refs/pull/<number>/head` and keeps it there after the request is closed and after both the branch and origin's copy are deleted. The work survives, and the reason it was dropped sits beside the commits rather than in a session nobody will read again.

**It is only for work the integration branch does not hold.** `git retire` refuses a branch holding nothing new, because there the commits are already in history, the branch is only a pointer, and `git delete` is the verb — which is every `DELETE` and `STALE` row. Where a request already exists it reuses it only if it is still open: one that merged or closed cannot be closed again, so anything else gets a fresh request over the same head.

### 7. Merging an open-PR branch

A `KEEP` branch an open PR is driving is not finished work — it is work whose intent is already on record. The question step 6 puts to the user is therefore already answered here: never *whether* to land it, only *when*, and that answer belongs to the sweep as a whole rather than to the branch alone.

**Offer these. Leaving one open is a decision the user makes, not one the sweep makes on their behalf.** Present them as their own group, each with its PR's review decision and check state — `uv run lup-devtools git pr status --branch <branch> --json` — and ask which to merge. A draft PR, a failing check, or a review still owed are all reasons to leave one standing, and each of them is the user's to weigh.

**`checks_state` has three answers, and only `passing` is one.** `running` says the checks have not finished — report it as its own state, never merged into the passing group and never presented as a difference between branches, because a probe that has not reported says nothing about the branch it is probing. Where the sweep turns on it, wait for the checks and read the status again rather than reading the unfinished answer.

**They share step 6's queue.** Every merge moves the integration branch, so open-PR branches and `LAND` branches form one ordered sequence rather than two independent passes. Take them one at a time, and re-derive the next one's base after each.

**A stacked PR is retargeted before anything lands, or it never reads as merged.** A PR whose base is its stack parent merges into that parent, and the forge marks a request merged only when a push to *its own base* carries its head — so landing the stack's work in the integration branch leaves every child PR open, and retargeting afterwards is refused with "no new commits" once the head is contained, closable forever but never merged. Read each open PR's base before its group's first merge, and where it names another feature branch, point it at the integration branch while the head still holds commits the integration branch lacks — `uv run lup-devtools git pr merge <number> --retarget` does both in one move, and `gh pr edit <number> --base <integration>` is the half by itself. Bottom-up over the stack, so each PR's diff collapses to its own commits as its parent lands.

**Order by what the branches touch, not by when they started.** Branches cut from the same tip have no divergence to sort by, so compare their file sets — `uv run lup-devtools git preview <branch>...` names the files each pair of them both touched, and which of them would conflict — and read the intersection:

- **Disjoint.** Any order serves. Take the smallest first, so the larger rebases onto a base that has stopped moving.
- **Overlapping source.** Merge the one the other builds on, and expect the second to want a real rebase rather than a fast-forward.
- **Overlapping generated tree.** A generated artifact's contents are derived, so merging two branches' versions as text yields something no generation run ever emitted — resolving cleanly while answering to nothing. Merge whichever regenerates least first, then reconcile the next where this repository's own merge drivers exist: rebase it inside its worktree, regenerate, and let the drift check report that it settled. A merge performed on the host has none of those drivers, because they are per-clone configuration no repository can ship.

Report the comparison and the order it implies, then ask before the first merge. The order is the decision; the merges only carry it out.

### 8. Confirm and execute

{{ approval_2 }} Then carry out the approved actions.

### 9. Report results

What landed, what merged, what was deleted locally, what was cleared from a remote, and what was deliberately left alone.

## Guidelines

- Never force-delete without explicit user approval for that specific branch
- **Never delete a `LAND` branch unless the user explicitly chose to drop it** — and then retire it rather than deleting it, so the choice ends the branch and not the work
- **Never discard a `COMMIT` branch's changes without explicit user approval**, and never delete the branch to be rid of them — uncommitted work has no copy anywhere, so `git restore` there ends it with nothing to recover it from
- Skip the current branch — warn the user instead
- Containment counts as landed only against the integration branch; riding inside a sibling that has not landed either is no reason to drop work
- For rebased branches, content may have reached the integration branch via a rebase PR even though `--is-ancestor` is false — the `DELETE` disposition already accounts for merged PRs
