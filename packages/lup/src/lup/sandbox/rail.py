"""The mounts one session gets over the repository it was opened on.

Worktrees of one repository share an object store and a branch set, so from
any of them `git -C ../other commit` writes to another's branch and
`cp x ../other/src/` overwrites another's file. Nothing separates them, and
this table does not try to: it hands a session the repository it works in,
every checkout of it writable.

**Why it does not separate them.** Holding siblings read-only was meant to
keep one session out of another's uncommitted work, which is the one thing
the reflog cannot restore. It did not protect that set. The table is
computed once, when a container starts, so it covers exactly the checkouts
that existed at that instant: one cut a minute later is outside it and
writable by everything in the session. Every worktree a resolver run leases
is cut after its operator started, so none of the checkouts with several
sessions touching them were ever covered -- while the branches an operator
is landing all predate it, and were. The protection reached the sessions
working alone and missed the ones working at once, which is the reverse of
what it was for, and a boundary that holds by an accident of timing teaches
a reader a rule that is not there.

So confining two workers from each other is a lease per worker, taken when
that worker starts against the tree it was given. What is left here is the
other question -- what one session may reach across its own repository --
and answering both from one table is what tied a worker's confinement to
whether its checkout predated somebody else's container.

**Where a boundary is wanted it stays a mount fact, not a judgement.** The
policy would have to decide, from a command's text, where it will act: that
is undecidable the moment a Makefile, an `xargs`, or a script that shells
out is involved, and `cd ../other && git commit` already walks past it. An OS
boundary does not predict. It observes the write and refuses it, whatever
route reached the syscall. Absolute paths stay identical on both sides --
forced rather than chosen, because a linked worktree's `.git` is a file
holding an absolute `gitdir:` pointer -- and only the modes vary.

**Siblings are still mounted, and that is load-bearing.** The obvious move
is to leave them out. It is wrong, and quietly so. `git gc` runs `git
worktree prune`, which deletes the admin directory of any worktree whose
`gitdir` target has gone missing. A session whose container could not see
its siblings would look around, find every one of their directories absent,
and delete their administrative state from the shared repository -- as
ordinary housekeeping, with no error anywhere. Two guards, where holding
each entry read-only would be a third: siblings are mounted so they exist,
and `gc.worktreePruneExpire` is set to never. The third is not missed,
because it only answers for a directory that is present anyway.

**`config` and `hooks/` are held read-only, by a directory mount.** They
are the two places under the shared directory whose *contents* run on the
host. `config` names a program through `core.hooksPath`, `alias.*`,
`credential.helper` and the `merge.*.driver` family, each handing git a
command line to execute at the operator's next git command in any worktree of
this repository; `hooks/` holds the script itself, the same reach with no key
in between -- a `pre-commit` written there runs at the next commit in any
worktree, and no config key is involved. Both are writes that land in no diff
and reach no review.

Binding `config` read-only as a *file* over a writable share does not hold.
Git rewrites `config` by renaming `config.lock` over it, and the kernel
detaches a mount whose dentry is renamed over from another namespace: after
the operator's next `git push -u`, `branch -d` or `remote` change on the host,
every running container found `config` writable and replaceable. Pointing
`config` at a read-only directory through a symlink does not hold either --
the symlink sits in the writable share, and replacing it takes one `rm`.
Both were measured in a user and mount namespace standing in for the
container. What holds is the shared directory itself bound read-only, with
each directory under it but `hooks/` and `modules/` bound writable back over
it: `config` is then a file inside a directory mount, and a host rewrite
changes the file without touching the mount. The files git replaces by
rename at the top -- `packed-refs` -- move into a writable directory behind a
symlink, which :func:`prepared_shared_directory` does once per clone, and a
plain checkout, whose per-worktree state lives at the top, keeps the file
binds. The engine applies the nesting because `Sandbox.declared_mounts` emits
parent before child.

Leaving them writable rests on the claim that a worker unable to write these
cannot cut a worktree. That was measured, and it is false: `git worktree add`
never opens `config`, and a guard armed in `hooks/` is inherited rather than
rewritten -- `git rev-parse --git-path hooks` in a linked worktree names
`<common>/hooks`, so a hook armed once covers every worktree of the clone,
whenever it is cut. Nothing else in the mandated workflow writes either --
committing, `pr push` with and without `--force`, base detection, reservation
reads, branch parsing and `worktree remove` were each run against a read-only
bind of both and none of them wrote one. Recording a base branch would have
written `config`, and does not: those facts live beside the branch instead.

Two writes are left, one per path, and both are host-side once-per-clone
acts. A clone registers the `merge.lup-ownership` driver once, because git
resolves a driver name from config alone and no repository can ship it; and a
clone arms its declared guards once, with `git hooks install`. Each has a
pre-flight in front of it that fires only where that write is outstanding, so
a clone that has made it cuts worktrees with both paths held, and one that
has not meets a refusal naming the act and the command before its first
worktree is half-made, rather than an errno about a busy device. The semantic
policy is what holds an approval question against those config keys by
name; the mount table is not the reason it has to.

**What this deliberately does not rail.** Commits landing on another branch.
The object store and refs have to be writable to commit at all, so branch
isolation would need a separate clone per worker -- and git already ships an
undo layer for refs in the reflog, where a mistaken commit is recoverable
completely. A sibling's *uncommitted* work is what the reflog cannot restore,
and it is the per-worker lease that covers it, not this table.

Boundary attribution is a prerequisite rather than a follow-on, and
:mod:`lup.sandbox.attribution` is it, reading the running mount table through
:mod:`lup.sandbox.observed` where the refusal is met from inside. A read-only
mount turns a stray write into `Read-only file system: .../src/foo.py`, and an
agent reading that debugs the filesystem instead of learning it holds a lease.
A rail without attribution is worse than no rail.
"""

import posixpath
from pathlib import Path, PurePosixPath

import sh
from pydantic import BaseModel, Field, field_validator

from lup.execution.shell import git


class Lease(BaseModel, frozen=True):
    """One worker's confinement, as the mounts that produce it.

    Two mappings rather than one table because that is the shape
    :class:`~lup.sandbox.container.Sandbox` takes them in, and keeping the
    split here means a caller never re-derives which is which from a mode.
    """

    writable: dict[Path, str] = Field(
        default={},
        description="Host paths this worker may write, keyed to the same path inside",
    )
    read_only: dict[Path, str] = Field(
        default={},
        description="Host paths this worker may read and must not write",
    )

    def answers_from(self, path: Path) -> Path | None:
        """The mount whose mode this path takes, or ``None`` where none does.

        The deepest one containing it, which is how the boundary itself reads
        the same table: ``execution_write_refusal`` in
        :mod:`lup.policy.assets.host` collects every declared scope the path
        sits under and takes the mode of the longest. A read-only hole inside
        a writable share is exactly that reading, so asking it here rather
        than testing membership means a mount left out for being redundant
        answers the same as one spelled out.
        """
        enclosing = [
            root
            for root in [*self.writable, *self.read_only]
            if path == root or root in path.parents
        ]
        return max(enclosing, key=lambda root: len(root.parts), default=None)

    def covers(self, path: Path) -> bool:
        """Whether this lease says anything at all about a path."""
        return self.answers_from(path) is not None

    def writable_at(self, path: Path) -> bool:
        """Whether this lease lets a path be written."""
        return self.answers_from(path) in self.writable


class NestedRepository(BaseModel, frozen=True, extra="forbid"):
    """A repository kept inside the checkout, whose git state the host still runs.

    The checkout's own ``config`` and ``hooks/`` are held because the host's
    next git command runs what they name. A repository inside the checkout
    -- a directory of generated work with its own history, say -- keeps the
    same two under the checkout's writable mount, and the operator runs git
    there too, so it is held the same way: the two bound read-only, as a
    plain checkout's are, and its pointers verified on the host at every
    launch, as every worktree's are -- a ``commondir`` planted there would
    have git read them from elsewhere.

    Declared rather than found by scanning, for three reasons. A scan reads
    a tree the session writes, so the session would choose what its next
    launch mounts: a thousand planted ``.git`` directories is a launch that
    cannot start, the argument that makes :class:`AccessibleRoot` a
    declaration. A scan holds a repository from the launch *after* it
    appears, so the session that made it wrote its configuration unheld; a
    declared one is created by the host first where ``create`` asks. And a
    scan walks every ignored directory at every launch for an answer the
    project already has.
    """

    path: PurePosixPath = Field(description="Where it sits, relative to the checkout")
    create: bool = Field(
        default=False,
        description=(
            "Initialize it on the host when a launch finds it absent, so no "
            "session is ever the one that wrote its configuration"
        ),
    )

    @field_validator("path")
    @classmethod
    def inside_the_checkout(cls, value: PurePosixPath) -> PurePosixPath:
        """Refuse a path naming the checkout itself or reaching outside it."""
        if value.is_absolute() or ".." in value.parts or value == PurePosixPath("."):
            raise ValueError(
                f"nested repository {value.as_posix()!r} must name a directory "
                "inside the checkout, relative to its root"
            )
        return value


class AccessibleRoot(BaseModel, frozen=True):
    """One checkout outside this repository a session is meant to reach.

    Declared rather than discovered, which is the whole of why this type
    exists instead of a directory scan. A `refs/` symlink lives inside the
    checkout and is writable from inside the boundary, so a mount table read
    off one would let the confined thing choose what confines it -- the same
    argument that keeps remotes, identity and credentials resolved on the
    host rather than in the container they describe.
    """

    path: Path
    writable: bool = Field(
        default=True,
        description="Whether this root may be written, or only read",
    )


def reached_through(path: Path, inside: str, mounts: dict[Path, str]) -> Path | None:
    """The deepest other mount that already lands this path where this one would.

    A mount reaches a path when it encloses it on the host *and* carries it to
    the same place inside, so the offset from root to path is equal on both
    sides. Anything else is a different directory at a similar spelling, and
    reading it as a substitute would move where the path resolves.

    The deepest, because that is the one the boundary answers from:
    ``execution_write_refusal`` in :mod:`lup.policy.assets.host` matches a
    write against every declared scope containing it and takes the mode of
    the longest. A reading that consulted any other enclosing mount would be
    answering a question the boundary never asks.
    """
    reaching = [
        root
        for root, target in mounts.items()
        if root in path.parents
        and posixpath.join(target, path.relative_to(root).as_posix()) == inside
    ]
    return max(reaching, key=lambda root: len(root.parts), default=None)


def resolved(writable: dict[Path, str], read_only: dict[Path, str]) -> Lease:
    """One path, one mode, settled toward writable, and stated once.

    Two ways to arrive at the same collision, and neither is hypothetical.
    `git worktree list` reports the main worktree, and in a bare layout that
    directory *is* the shared one -- so the shared directory arrives as its
    own sibling and would be declared read-only and read-write at once. Once
    more than one repository is leased, two registered worktrees of a single
    repository each hold the other read-only while both were named writable
    deliberately.

    Settled toward writable because the paths that reach here writable are
    the ones somebody named, and settled here rather than in the engine,
    where two mounts at one target resolve by whichever order they happen to
    be applied in.

    Then the same collision one directory further out. A mount whose deepest
    enclosing mount already carries it in the same mode changes nothing about
    what may be read or written, and costs something a lease cannot pay back:
    a mount point cannot be removed from inside its own namespace, so a
    checkout bound individually under a shared directory that is itself bound
    outlives `git worktree remove` as an empty directory no session can
    clear. One accumulated per landed branch.

    Same mode is the whole of the relaxation, and it is the *deepest*
    enclosing mount's mode that decides. A read-only entry inside a writable
    share keeps its own mount, which is the nesting this whole arrangement
    rests on; so does a writable checkout inside a read-only directory inside
    a writable share, which stays writable because the mount between them is
    the one that would otherwise answer for it.
    """
    settled = {
        path: inside for path, inside in read_only.items() if path not in writable
    }

    def stated_once(mode: dict[Path, str]) -> dict[Path, str]:
        """This mode's mounts, less the ones an enclosing mount already makes."""
        return {
            path: inside
            for path, inside in mode.items()
            if reached_through(path, inside, {**writable, **settled}) not in mode
        }

    return Lease(writable=stated_once(writable), read_only=stated_once(settled))


def rooted(lease: Lease) -> Lease:
    """This lease with every hold kept where it is, not only kept unwritable.

    A read-only mount refuses writes to what it covers. It does not stop the
    directory *holding* it from being renamed, and a directory with mounts
    beneath it can be: measured, ``mv .lup .lup2`` succeeded with
    ``.lup/preflight`` held inside, and nothing then stopped a new
    ``.lup/preflight`` being written where the host reads it. So every
    directory between a hold and the writable mount enclosing it is bound
    writable over itself -- a mount point cannot be renamed or removed from
    inside -- and each hold is then reachable only by the path the host
    reads it by.

    Stated after the lease is settled, and never settled again: :func:`resolved`
    drops a mount an enclosing one of the same mode already makes, which is
    exactly what a pin is on purpose. Nothing is pinned inside a read-only
    mount, where nothing can be renamed anyway, so this never makes a path
    writable that was not; a lease pinned already gains nothing more.
    """
    mounts = [*lease.writable, *lease.read_only]

    def between(held: Path) -> list[Path]:
        """The directories from the mount enclosing ``held`` down to its parent."""
        enclosing = max(
            (mount for mount in mounts if mount in held.parents),
            key=lambda mount: len(mount.parts),
            default=None,
        )
        if enclosing is None or enclosing not in lease.writable:
            return []
        return [
            directory for directory in held.parents if enclosing in directory.parents
        ]

    pins = same_path(
        [directory for held in lease.read_only for directory in between(held)]
    )
    return Lease(writable={**lease.writable, **pins}, read_only=lease.read_only)


def merged(leases: list[Lease]) -> Lease:
    """Every lease as one, with collisions settled across all of them at once.

    Settling each lease alone and concatenating is not the same thing, and
    the difference is silent: a path one lease holds read-only and another
    holds writable stays read-only, which mounts a worker's own checkout
    unwritable because some other repository called it a sibling.
    """
    return resolved(
        {path: inside for lease in leases for path, inside in lease.writable.items()},
        {path: inside for lease in leases for path, inside in lease.read_only.items()},
    )


class RepositoryLayout(BaseModel, frozen=True):
    """The two git directories a linked worktree lives between.

    ``common`` is shared by every worktree of the repository -- objects, refs,
    config, and the `worktrees/` directory holding each one's administrative
    state. ``private`` is this worktree's own entry inside it, holding the
    HEAD, index and logs that are its alone. The two are equal in a plain
    checkout, which is what makes a lease there degenerate rather than broken.
    """

    common: Path
    private: Path

    def linked(self) -> bool:
        """Whether this is a linked worktree rather than a plain checkout."""
        return self.common != self.private

    def name(self) -> str:
        """What to call the repository, identically from any of its worktrees.

        The shared directory is the one thing every worktree of a repository
        has in common, so its name is the one string they all agree on --
        which is what anything wanting to be per *repository* rather than per
        checkout has to key on. A worktree directory's own name is the branch
        somebody made it for.

        Two spellings collapse into it. A bare repository conventionally ends
        in `.git` and says nothing by it, and a plain checkout's shared
        directory *is* `.git`, whose name is the convention rather than the
        project.
        """
        return (
            self.common.parent.name
            if self.common.name == ".git"
            else self.common.name.removesuffix(".git")
        )


def same_path(roots: list[Path]) -> dict[Path, str]:
    """Mount each host path at the identical path inside the container.

    Not a convenience. A linked worktree's `.git` is a file whose contents are
    an absolute `gitdir:` pointer into the shared administrative directory, so
    a container mounting the tree anywhere else would hold a checkout pointing
    at a path that does not exist there. One spelling is the only spelling
    that works.

    Deduplicated by construction, since a dict is what comes back: the shared
    directory and a path beneath it can both be named without the second
    silently becoming a second mount of the first.

    Whether a host can actually do this is not assumed. It is a declared
    requirement, exercised by ``same_path_mount_requirement`` -- because a
    rail whose mounts silently do not happen is not a loosened rail, it is an
    absent one reporting success, and nothing else in this module would
    notice.

    How that probe has to be written was learned the hard way. Asking
    ``test -d`` about the mounted directory reported *false* on rootless
    podman for every worktree this rail leases, which reads exactly like an
    absent mount and is not one: reading a file through the same mount in the
    same container succeeded. The mount was there; `stat` on the mount point
    itself was not answerable under that user-namespace mapping. So the probe
    reads a file across the boundary rather than asking whether a directory
    is present, and the general lesson is the one this whole design keeps
    relearning -- a presence check answers a different question than the one
    being asked, and its wrong answer is shaped like a real finding.
    """
    return {root: root.as_posix() for root in roots}


def repository_layout(worktree: Path) -> RepositoryLayout:
    """Where this checkout keeps its own admin directory and the shared one."""
    asked = ["rev-parse", "--path-format=absolute"]
    return RepositoryLayout(
        common=Path(git.out("-C", str(worktree), *asked, "--git-common-dir").strip()),
        private=Path(git.out("-C", str(worktree), *asked, "--git-dir").strip()),
    )


def host_run(
    worktree: Path, named: tuple[str, ...] = ("config", "hooks")
) -> list[Path]:
    """The paths under a repository's shared directory whose contents the host runs.

    What every posture withholds, whatever shape its withholding takes: a
    container holds them under a read-only directory mount, and an inner
    sandbox, which has no mounts of its own to nest, denies them by name.
    Denying the whole shared directory there instead would deny the objects
    and refs every commit writes.
    """
    common = repository_layout(worktree).common
    return [common / name for name in named]


def shared_entries(
    common: Path, held: tuple[str, ...] = ("hooks", "modules")
) -> list[Path]:
    """Every directory under a shared git directory a session writes through.

    Read off the disk rather than listed, because what lives there is each
    repository's own: git's `objects/`, `refs/` and `logs/`, a `tree/` a
    project keeps its worktrees in, whatever a tool keeps its state in. What
    is named is the exception. ``held`` stays under the read-only directory
    because its contents run on the host: `hooks/` holds the scripts git
    executes, and each `modules/<name>/` is a submodule's own git directory,
    `config` and `hooks/` included. A symlink is left where it is, since a
    bind would follow it to wherever it points.

    A directory that is not there yet is not mounted -- a bind whose source is
    missing is one the engine refuses the whole container for -- which is why
    :func:`prepared_shared_directory` makes the ones git creates on first use
    before a launch reads this.
    """
    return sorted(
        entry
        for entry in common.iterdir()
        if entry.is_dir() and not entry.is_symlink() and entry.name not in held
    )


def sibling_worktrees(worktree: Path) -> list[Path]:
    """Every other checkout of this repository, as absolute paths.

    Listed from git rather than by scanning the parent directory: where
    sibling checkouts live is a repository's own arrangement, and a scan
    would sweep in whatever else happens to sit beside them.
    """
    listed = git.lines("-C", str(worktree), "worktree", "list", "--porcelain")
    found = [
        Path(line.removeprefix("worktree "))
        for line in listed
        if line.startswith("worktree ")
    ]
    return [path for path in found if path != worktree and path.is_dir()]


def siblings_of(worktree: Path, layout: RepositoryLayout) -> list[Path]:
    """The sibling checkouts a lease mounts, less the shared directory itself.

    `git worktree list` reports a bare repository as the main worktree, so
    in a bare layout the shared directory comes back as its own sibling --
    and mounted as one, it is writable as a whole and `config` with it,
    because :func:`resolved` settles a path named both ways toward writable.
    The shared directory's mode is the lease's to decide, never a sibling's.
    """
    return [path for path in sibling_worktrees(worktree) if path != layout.common]


def working_trees(root: Path) -> list[Path]:
    """The checkouts a declared root is worked in: itself, or a bare one's worktrees.

    A bare repository has no tree of its own, so what a session syncs an
    environment in, or runs a project's tooling from, is each worktree
    attached to it -- which is what a launch mounting a clone whole hands
    over. Anything else answers for itself, a directory git knows nothing of
    included.
    """
    bare = git.out(
        "-C", str(root), "rev-parse", "--is-bare-repository", _ok_code=[0, 128]
    )
    if bare != "true":
        return [root]
    return [tree for tree in sibling_worktrees(root) if (tree / ".git").exists()]


def lease_for(worktree: Path) -> Lease:
    """The mounts one session needs over the repository it works in.

    Every checkout of this repository writable, and the shared administrative
    directory read-only with every directory under it but `hooks/` and
    `modules/` writable -- or, in a plain checkout, the shared directory
    writable with `config` and `hooks/` held read-only inside it. Either way
    `config` and `hooks/` are what cannot be written. Siblings are mounted rather than left
    out for the reason the module docstring gives -- absent, they are what
    `git worktree prune` deletes the administrative state of -- and writable
    for the reason it gives beside that.

    A path the project declared its author owns is not among the read-only
    mounts. Held read-only, `README.md` refuses the fast-forward
    that lands a branch touching it: git replaces a file by unlinking it,
    a mount point refuses that, and the merge is the user's from a host
    terminal every time. What such a mount protects is protected by the
    policy: an edit or a shell write to a human-owned path asks, and the
    approval is the author's answer, which a mount can neither ask for nor
    honour.
    """
    layout = repository_layout(worktree)
    writable = [worktree, *siblings_of(worktree, layout)]
    if layout.linked():
        # The shared directory read-only as a directory, and each directory
        # under it bound writable back over it but `hooks/` and `modules/`.
        # `config` and `hooks/` are the two places whose contents name a
        # program the host runs, and a directory mount is the one shape a
        # host-side rewrite cannot shed: git replaces `config` by renaming a
        # lockfile over it, and the kernel detaches a *file* mount whose
        # dentry is renamed over in another namespace -- while a rename inside
        # a mounted directory changes a file under the mount, not the mount.
        # Every sibling's administrative entry stays present and writable
        # inside the writable `worktrees/`, which is the guard `worktree
        # prune` needs and what lets `git worktree remove` unlink one.
        read_only = [layout.common]
        writable += [*shared_entries(layout.common), layout.private]
    else:
        # A plain checkout keeps its per-worktree state -- `HEAD`, the index,
        # `ORIG_HEAD` -- at the top of the shared directory, so the directory
        # stays writable and `config` and `hooks/` are bound read-only back
        # over it. The file bind is detached by the next host-side rewrite of
        # `config`, which the read-only-bind requirement reports.
        read_only = [layout.common / "config", layout.common / "hooks"]
        writable.append(layout.common)
    return resolved(same_path(writable), same_path(read_only))


def worker_lease(worktree: Path) -> Lease:
    """The mounts that confine one worker to the tree it was given.

    The arrangement :func:`lease_for` carries, at the level it is
    actually true at. Taken when a worker starts rather than when a session
    does, it covers the checkouts that exist by then -- which is every
    worktree a run leases, the population a launch-time table misses
    entirely. Nothing about the shape differs; only when it is computed, and
    for whom.

    Nested modes rather than two flat ones: this worktree writable, every
    sibling read-only, and the shared administrative directory read-only
    with its directories writable back over it as :func:`lease_for` binds
    them -- and inside the writable `worktrees/`, each sibling's own entry
    read-only again. `worktrees/` is writable so a new worktree's entry can
    be created beside the others -- without which no worker can cut a
    worktree -- and `config` and `hooks/` are not carried along with it
    because a worker needs the directory to admit a new child, never that
    file rewritten or that hook armed. A human-owned path is not held either, for the reason
    :func:`lease_for` gives: the policy asks about a write to one, and a
    mount cannot.
    """
    # lup: defer: unverified live -- no session with a container engine has run
    # a resolver with two concerns and seen each worker hold its own worktree
    # writable and its siblings read-only, nor a write into a sibling refused
    # naming this lease rather than an errno; needs a host terminal with podman
    layout = repository_layout(worktree)
    writable = [worktree]
    read_only = siblings_of(worktree, layout)
    if layout.linked():
        # The shared directory read-only with its directories bound writable
        # back over it, for the reason :func:`lease_for` gives -- and inside
        # the writable `worktrees/`, each sibling's administrative entry
        # punched read-only again, so every one stays present and unwritable,
        # which is what keeps `worktree prune` from removing it. Each entry
        # rather than `worktrees/` itself, because a read-only directory
        # refuses two different acts and only one of them was the subject:
        # rewriting an entry that is already there endangers a sibling, and
        # creating a new one beside them endangers nobody. That nesting is
        # the whole arrangement, and `Sandbox.declared_mounts` is what holds
        # it up: it emits mounts parent before child, so a read-only hole is
        # applied after the writable base it sits in instead of being
        # shadowed by it.
        read_only += [
            layout.common,
            *[
                entry
                for entry in sorted((layout.common / "worktrees").iterdir())
                if entry != layout.private
            ],
        ]
        writable += [*shared_entries(layout.common), layout.private]
    else:
        read_only += [layout.common / "config", layout.common / "hooks"]
        writable.append(layout.common)
    return resolved(same_path(writable), same_path(read_only))


def in_repository(path: Path) -> bool:
    """Whether git answers for this directory at all.

    Asked rather than assumed, because not everything worth reaching is a
    checkout: a directory of reference material registered for access is a
    plain bind, and putting it through the worktree arrangement would only
    ask git about a place git knows nothing of.
    """
    try:
        repository_layout(path)
    except sh.ErrorReturnCode:
        return False
    return True


def demoted(lease: Lease) -> Lease:
    """This lease with nothing writable, for a root declared read-only.

    Its shared git directory goes read-only along with the rest, which costs
    the commands that take a lockfile beside the file they write -- `git
    config`, an index refresh -- and that is what read-only means here rather
    than an oversight. A root nobody may write is one whose repository state
    nobody may move either, and the alternative is a mode that refuses the
    file while admitting the thing that rewrites it.
    """
    return Lease(read_only={**lease.writable, **lease.read_only})


def accessible_lease(root: AccessibleRoot) -> Lease:
    """The mounts one declared root needs, whatever kind of directory it is.

    A checkout gets the whole arrangement :func:`lease_for` builds, and the
    reason is the same one that forces same-path mounting: a linked
    worktree's `.git` is a file holding an absolute `gitdir:` pointer, so a
    bind of the working copy alone hands the session a checkout pointing at a
    path that is not there. That is a broken repository rather than a
    boundary, and it is debugged as one.

    The siblings matter here more than they do at home, not less. `git gc`
    runs `git worktree prune`, which deletes the administrative state of any
    worktree whose directory has gone missing -- so a session that could see
    one checkout of somebody else's repository and none of the others would
    remove their entries as ordinary housekeeping, in a repository nobody in
    this session owns.
    """
    lease = (
        lease_for(root.path)
        if in_repository(root.path)
        else Lease(writable=same_path([root.path]))
    )
    return lease if root.writable else demoted(lease)


def fleet_lease(
    worktree: Path,
    accessible: list[AccessibleRoot] | None = None,
) -> Lease:
    """This worktree's lease, plus every root the project declared reachable.

    A root already covered by this worktree's own lease is dropped rather
    than mounted twice: a registration naming a sibling of this repository,
    or a checkout kept inside it, is already leased, and saying so again
    hands the engine two mounts at one target for it to settle by order.

    A root that is not there is skipped rather than declared. A bind mount
    whose source does not exist is one the engine refuses the whole container
    for, so a registration somebody moved would take the session down instead
    of costing its own reachability -- which is the only thing it should
    cost.
    """
    own = lease_for(worktree)
    return merged(
        [
            own,
            *[
                accessible_lease(root)
                for root in accessible or []
                if root.path.exists() and not own.covers(root.path)
            ],
        ]
    )


def relocated(path: Path, target: Path) -> None:
    """Put a file git replaces by rename behind a symlink into a writable directory.

    Git writes `packed-refs` the way it writes `config`: a lockfile beside
    the file, renamed over it. Under a read-only shared directory that
    lockfile cannot be made, and every ref deletion takes it -- `branch -D`,
    `fetch --prune`, `pack-refs` -- so the file moves to ``target``, relative
    to its own directory, and a symlink takes its place. Git resolves the
    symlink before it takes the lock, so the lock and the rename both land
    beside the target, on the host and inside alike; that was measured with
    `pack-refs`, `branch -D` of a packed ref, `fetch --prune`, `gc`, `repack`
    and `maintenance`, none of which replaced the symlink.

    Taken under git's own lock, the one every writer of this file takes, so
    no writer is between reading and replacing it; one that holds it now is a
    `FileExistsError` and the move waits for the next launch. The file is
    hard-linked into place and the symlink renamed over the original, so a
    reader sees the old file or the symlink to the same bytes, never neither.
    A file that does not exist yet leaves a symlink to where it will be,
    which git reads as holding nothing and writes through on first use.
    """
    if path.is_symlink() and path.readlink() == target:
        return
    lock = path.with_name(f"{path.name}.lock")
    lock.touch(exist_ok=False)
    try:
        if path.is_symlink():
            raise FileExistsError(f"{path} points at {path.readlink()}, not {target}")
        placed = path.parent / target
        if path.exists():
            staged = placed.with_name(f"{placed.name}.staged")
            staged.unlink(missing_ok=True)
            staged.hardlink_to(path)
            staged.replace(placed)
        pointer = path.with_name(f"{path.name}.link")
        pointer.unlink(missing_ok=True)
        pointer.symlink_to(target)
        pointer.replace(path)
    finally:
        lock.unlink()


def prepared_shared_directory(
    worktree: Path,
    owned: tuple[str, ...] = (),
    made: tuple[str, ...] = ("logs", "info", "worktrees", "rr-cache"),
    home: str = "lup-refs",
    replaced: tuple[str, ...] = ("packed-refs",),
) -> str:
    """Ready a linked repository's shared directory to be bound read-only.

    Three host-side acts, each a no-op once made, which is what lets every
    launch run them. The directories git creates on first use are created
    now, because :func:`shared_entries` binds only what exists and a session
    finding `logs/` missing cannot make it under a read-only parent -- the
    first commit in a fresh bare clone failed exactly that way. ``owned``
    carries a caller's own additions beside git's. The files git replaces by
    rename move into ``home`` behind a symlink, through :func:`relocated`.
    And `fsck.badRefFiletype` is lowered to a warning, because `git fsck`
    and `git refs verify` report a symlinked `packed-refs` as an error --
    measured, exit 8 and 255 -- and nothing else git does objects to it.

    Nothing is written to `config` when that setting already stands: every
    rewrite of `config` detaches the file bind a running plain-checkout
    session holds over it.

    A plain checkout is left alone -- its lease keeps the directory writable.
    Returns why the directory could not be readied, or an empty string, and
    a launch goes ahead either way: a directory left unprepared costs its
    sessions ref deletion or a first-use directory, not the boundary.
    """
    layout = repository_layout(worktree)
    if not layout.linked():
        return ""
    common = layout.common
    try:
        for name in (*made, *owned, home):
            (common / name).mkdir(exist_ok=True)
        for name in replaced:
            relocated(common / name, Path(home) / name)
        asked = ["-C", str(worktree), "config", "fsck.badRefFiletype"]
        if git.out(*asked, _ok_code=[0, 1]).strip() != "warn":
            git(*asked, "warn")
    except (OSError, sh.ErrorReturnCode) as error:
        return f"{common}: {error}"
    return ""


def prepared_across(worktrees: list[Path], owned: tuple[str, ...] = ()) -> list[str]:
    """Ready every repository given for its read-only bind, and say which would not.

    Once per repository a launch leases writable, for the reason
    :func:`hold_pruning_across` gives: a lease spans repositories nobody in
    this session owns, and each binds its own shared directory. A path git
    does not answer for has no shared directory and is passed over.
    """
    said = [
        prepared_shared_directory(worktree, owned)
        for worktree in worktrees
        if in_repository(worktree)
    ]
    return [reason for reason in said if reason]


def hold_worktree_pruning(worktree: Path) -> bool:
    """Stop `git gc` deleting the administrative state of unseen worktrees.

    The third guard, and the one that does not depend on getting the mounts
    exactly right. `gc.worktreePruneExpire` decides how long a worktree whose
    directory has gone missing keeps its entry; set to never, a worker that
    somehow cannot see a sibling still cannot cause its removal.

    Written to the shared configuration, so it protects every worktree of the
    repository rather than only the one that set it. Reports whether it took:
    a repository nobody here may configure is a reason to say so, not a
    reason to fail a launch that is otherwise fine.

    Read before it is written, and written only where it does not already
    stand. Git rewrites `config` by renaming a lockfile over it, and that
    rename detaches every file bind held over `config` in a running
    container -- so a launch that rewrote it unconditionally unbound the
    read-only `config` of every plain-checkout session already open.
    """
    asked = ["-C", str(worktree), "config", "gc.worktreePruneExpire"]
    try:
        if git.out(*asked, _ok_code=[0, 1]).strip() != "never":
            git(*asked, "never")
        return True
    except sh.ErrorReturnCode:
        return False


def hold_pruning_across(worktrees: list[Path]) -> list[Path]:
    """Arm the prune guard in every repository given, and name the refusals.

    Once per repository rather than once per launch, because a lease
    spans repositories nobody in this session owns: a `git gc` inside the
    boundary reaches their administrative state through the same shared
    directory it reaches this one's, and the mount guards only hold while the
    mounts are exactly right. This one holds when they are not.

    A path git does not answer for is neither armed nor reported. Nothing
    there has administrative state to lose, and listing it would make a
    directory of reference material read as a repository this failed on.
    """
    return [
        worktree
        for worktree in worktrees
        if in_repository(worktree) and not hold_worktree_pruning(worktree)
    ]
