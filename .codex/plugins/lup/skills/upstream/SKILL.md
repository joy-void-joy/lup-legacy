---
name: upstream
description: Fix a defect in lup itself, in a worktree of lup, and pin it until it lands
---

# Fix It Upstream

**What is wrong:** the arguments supplied with this skill invocation

A defect in lup met from a project built on it has two repairs, and only one of
them is cheap twice. Working around it here puts this project's copy further
from upstream — which is exactly what makes the next update expensive — and
leaves the defect for whoever meets it next. Fixing it in lup costs one
worktree and reaches every project at once.

This is that loop. Nothing in it is unusual except where the work happens: in
a worktree of *lup*, under lup's own gate, while the pin here follows the
branch until it lands.

## 1. Confirm the defect is lup's

Read the code that misbehaves before deciding whose it is. Two things that
look like an upstream defect are not:

- **Something this project copied.** `src/` and `tests/` are this project's
  own, merged from upstream rather than imported, so a fix there is a fix
  here — and if upstream's version has the same flaw, both halves want it.
- **Something this project declared.** A catalog, a tool-group list, a policy
  selection. The library behaves as declared; the declaration is what to
  change.

What is left — library code under `packages/lup/` upstream, or a generated
tree compiled from lup's own declarations — is lup's.

## 2. Open a worktree in the lup checkout

`refs/lup` is lup's working tree, and a session holds it read-write from its
first launch: the registration `sync.json` ships names lup's repository and
mounts it `rw`, so the launch clones it under `~/.cache/lup/sync/lup.git`
where nothing is there yet, attaches a worktree for its default branch, and
mounts the whole clone -- so a worktree cut in it later is as writable as
that one. The exception is an inner launch whose runtime's own sandbox cannot
hold the clone's `config` and `hooks/` read-only inside it: that launch
admits only the worktrees the clone held when it started, so a new one is
written from the next launch (`docs/platform-differentiation.md` says which
runtime). A machine that keeps its own checkout of lup has registered that
one instead, and `refs/lup` is its working tree. Where the project resolves
lup from a repository, the registration follows that pin, so the checkout is
always a clone of the repository the library comes from.

Cut the branch there, not here, from lup's integration branch:

```bash
git -C refs/lup fetch origin dev:dev
uv run --directory refs/lup lup-devtools git worktree create fix-<name> --base dev
```

The fetch brings the clone's `dev` level with lup's: a clone's own branches
stand where it was made, because the launch and `sync` refresh only its
remote-tracking refs. Git refuses it where `dev` is checked out in a worktree
of that clone -- somebody's own checkout, whose `dev` is theirs to move -- and
the branch is cut from theirs then. The second command prints the new
worktree's path; the steps below call it `<fix>`.

If `refs/lup` does not resolve, the launch could not place lup and said why
as the session opened; `uv run lup-devtools sync status` names it again with
the command that answers it. A machine that reaches lup over another
transport records it with `sync remote lup <url>`, and one that keeps its own
checkout with `sync setup lup /path/to/repo --mount rw`. Both write
`sync.json.local`, a protected edit, so each asks the user before it writes.
Mounts are built at launch, so either takes effect at the next one.

## 3. Make the change under lup's gate

Edit in that worktree, and run **lup's** gate there rather than this project's:

```bash
uv run --directory <fix> lup-devtools dev check
```

An explicitly granted destination worktree is judged by its own generated
policy, while this session retains its measured boundary and approval channel.
The launch records the accepted evaluator bytes; a writable parent directory
or a `refs/` symlink alone supplies no repository policy grant.

Generate both native trees in a newly created worktree before editing it.
Until its policy is accepted, an edit in `<fix>` is judged by this project's
policy as another repository's file, not by lup's. The launch mounted lup's
clone whole, so an operator accepts it without restarting this session; from
the adopter checkout, the operator runs:

```bash
uv run lup-devtools harness policy-refresh --nonce <launch-nonce> --repository <canonical-worktree-path>
```

This accepts only a worktree inside the original mount and belonging to that
same Git repository. It is also the recovery after accepted generated policy
changes: regenerate there, then have the operator refresh its snapshot. The
requesting agent cannot approve replacement policy itself. A worktree outside
the original mount needs a launch granting that path -- which is where a
checkout this machine keeps leaves `<fix>`, because a registration naming a
path mounts that working tree alone. Run the upstream gate even when its hook
allows an edit; its checks also cover the completed branch.

Two conventions of lup's that are easy to miss from outside it:

- A capability that goes needs a migration declaring what a caller does about
  it — one TOML file under `packages/lup/src/lup/migrations/pending/` — or
  lup's own gate refuses the branch.
- Generated trees are regenerated, never hand-edited:
  `harness generate all` before the gate.

Commit there with `$lup:commit`, then push the branch and open its pull
request against lup's `dev`:

```bash
uv run --directory <fix> lup-devtools git pr push
uv run --directory <fix> lup-devtools git pr create --base dev --title "<what it fixes>" --body-file <notes>
```

The two need different credentials, and the launch said which it lent. The
push travels on the transport credential: inside the container the clone's
https remote is rewritten onto the ssh agent or key the launch lent, or kept
for a token, so it goes out as whichever the operator pushes lup with. A host
posture (`--sandbox inner` or `none`) rewrites nothing, so there the clone's
own origin decides; a machine that pushes lup over ssh says so once with
`sync remote lup <ssh url>`, which repoints the clone it already has. The
pull request is the forge API's, which only a token answers -- the one in
`LUP_GIT_TOKEN`, or the forge client's own login where the project declared
that source. `git pr create` refuses without one and says so; then the branch
is pushed and nothing is lost, so hand the user the same request to open from
a terminal that holds the credential:
`gh pr create --repo <owner>/lup --base dev --head fix-<name>`.

## 4. Run this project on the fix

Pin the library at the branch carrying it, and take the whole update — the fix
is only proved by the project that met the defect:

```bash
uv run lup-devtools dev library git --branch fix-<name>
uv run lup-devtools dev update
```

Then re-run whatever failed. A fix that does not resolve it is a fix aimed at
the wrong thing, and the worktree is still open to correct it in.

## 5. Land it, and come back to the branch you follow

Once the fix is merged upstream:

```bash
uv run lup-devtools dev library git --branch <the branch this project follows>
uv run lup-devtools dev update
```

Leaving the pin on the fix branch is how a project ends up following a branch
nobody advances, which reads exactly like being up to date.

## Guidelines

- **The defect goes upstream even when the workaround is tempting.** A
  workaround is a decision this project makes on behalf of every project that
  meets the same defect, and it is the decision nobody else can see.
- **Report friction you cannot repair.** Where the fix needs a decision that
  is not yours, or reproduction is the work, `dev report-friction` reports it
  against lup with the command, the error and the recovery cost. Routed to
  lup's tracker from a project that is not lup, it prints the command naming
  that tracker with `--repo`, and running that asks.
- **One branch, one subject.** The fix and whatever this project does about it
  are two changes in two repositories, and a reviewer of either wants only
  their half.
