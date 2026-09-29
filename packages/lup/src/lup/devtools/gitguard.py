# lup: ignore[dict-str-payload] — ref name to object id, keyed by whatever
# refs a repository happens to hold; there is no closed set to model
"""Catching a test suite that wrote into the repository it is running inside.

A test that forgets to bind git to its throwaway repository inherits the
process working directory instead, and nothing fails — git finds a repository,
commits succeed, and the suite passes green while the developer's branch has
moved. Found the slow way, by a `git pr sync-base` merging a `dev` whose tip
had become a fixture's commit deleting the application source. The suite cannot
be trusted to notice, because noticing is exactly what it failed at, so the
refs are read around it.

A test that builds a throwaway repository binds git to it — `git -C <tmp>` —
and one that forgets inherits the process working directory instead, which
during a test run is a real checkout. Nothing about that fails: git finds a
repository, commits succeed, and the suite passes green while the branch the
developer is standing on has moved. It was found here the slow way, by a
`git pr sync-base` merging a `dev` whose tip had become a fixture's `chore:
base` commit deleting the entire application source, an hour after the fixture
ran.

The suite cannot be trusted to notice, because noticing is exactly what it
failed to do. So the check sits outside every test: the refs of the enclosing
repository are read once before the session and once after, and a difference
fails the run naming the refs that moved — except where the ref belongs to
another worktree of the same repository, which is that worktree's to move and
so is reported rather than failed on. Detection rather than prevention — a
ceiling that stopped git discovering the enclosing repository would also stop
the tests that legitimately read it, and a suite that cannot run is a worse
trade than one that reports what it broke.

Read around every test, and per worker, rather than once around the session.
Under xdist each worker is a session of its own over one shared ref store, so
a difference closed once per session lands on whichever test that worker ran
last: a policy row about `gh pr create` was failed for a branch a sibling
session cut forty seconds into the run. A :class:`RepositoryWatch` settles
after each test against a baseline that moves with it, so a change is laid at
the door of the test whose window saw it, naming the worker that saw it; and
the sibling worktrees are re-read when something moved, so a worktree cut
mid-run answers for its own branch instead of the suite.

Nothing here needs the repository to exist: a suite running outside a checkout
gets an empty snapshot both times and never fails.
"""

from collections.abc import Iterator
from pathlib import Path

import sh
from pydantic import BaseModel

from lup.harness.toolchain import preflight_namespace
from lup.policy.assets.host import undo_namespace

REF_FORMAT = "%(refname) %(objectname)"
"""One ref per line, as `repository_refs` reads it."""

UPSTREAM_FORMAT = "%(refname) %(upstream)"
"""One ref per line against the remote-tracking ref it follows, where it follows one."""

WATCHED_SETTINGS = ("user.name", "user.email", "core.hooksPath")
"""The config a fixture overwrites and never puts back.

Watched beside the refs because this is the quieter half of the same accident
and the more expensive one. A moved ref is visible the moment anybody looks at
the branch; a committer identity written into the shared config is inherited by
every worktree cut from the repository and shows up only as authorship on work
done hours later, by someone who never ran the suite.

`core.hooksPath` is quieter still, and it is the half that takes the alarm out
with it. Pointed at a directory a fixture built, it disables every hook the
repository declares — the drift guard and the gate guard both stop running,
and a checkout that runs no hooks looks exactly like one whose hooks pass. An
identity leak at least signs the work it spoils; this one leaves no trace at
all until something it should have refused gets through.
"""

GIT_ENVIRONMENT = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
)
"""What names a repository to git ahead of any directory a command is given.

The third way into the same accident, and the one no fixture can defend
itself against: `-C <throwaway>` chooses where git works, while these choose
which repository it resolves, and the environment wins. A suite that inherits
them commits into whatever they name, however carefully each helper bound its
own git.

Git exports them to every hook, so a gate installed as one — see
:mod:`lup.devtools.dev.git_guards` — hands the suite the very checkout being
pushed. Scrubbed where the hook is written rather than watched for afterwards,
because unlike a misbound command this arrives through one door.
"""


class CommitterIdentity(BaseModel):
    """Somebody for a suite's throwaway repositories to commit as."""

    name: str
    email: str

    def environment(self) -> dict[str, str]:
        """This identity as environment, which cannot persist anywhere.

        A suite building throwaway repositories has to give git somebody to
        commit as, and the obvious `git config` writes a file — the shared one
        when the command misbinds, which is the accident this module exists
        for. Passing the identity per invocation with `-c` closes that only for
        the commands the suite runs itself: code under test runs git of its own
        and inherits none of them, so it commits as nobody and fails wherever
        the developer's global config is not there to cover for it.

        The environment reaches both and outlives neither. There is no file for
        a misbound command to land in, so the identity half of the accident is
        impossible here rather than merely watched for.
        """
        return {
            "GIT_AUTHOR_NAME": self.name,
            "GIT_AUTHOR_EMAIL": self.email,
            "GIT_COMMITTER_NAME": self.name,
            "GIT_COMMITTER_EMAIL": self.email,
        }


TEST_IDENTITY = CommitterIdentity(name="Lup Test Suite", email="tests@lup.invalid")
"""The identity a suite arms its session with, overridable by a caller with cause."""


def repository_refs(root: Path, ref_format: str = REF_FORMAT) -> dict[str, str]:
    """Every ref in the repository enclosing `root`, or nothing if there is none.

    Read through git rather than by walking `.git`, because a worktree's refs
    live in the repository it was cut from and only git knows where that is.
    A failure to read is reported as no repository rather than raised: this
    runs before and after a suite whose result matters more than the guard's
    own footing, and a guard that can break the run it protects is worse than
    one that stays quiet.
    """
    try:
        listed = sh.Command("git")(
            "-C", str(root), "for-each-ref", f"--format={ref_format}", _tty_out=False
        )
    except (sh.ErrorReturnCode, sh.CommandNotFound):
        return {}
    pairs = [
        # lup: ignore[string-split] — git's own for-each-ref output, whose two
        # fields REF_FORMAT put either side of one space
        line.split(" ", 1)
        for line in str(listed).splitlines()
        if line
    ]
    return {pair[0]: pair[1] for pair in pairs if len(pair) == 2}


def watched_config(
    root: Path, settings: tuple[str, ...] = WATCHED_SETTINGS
) -> dict[str, str]:
    """What the repository enclosing `root` holds for each watched setting.

    Absent settings are simply absent, so a repository that leaves identity to
    the user's global config reads as empty here and a fixture writing one in
    shows up as a creation rather than as a change from nothing.

    One listing rather than one query per setting, because this is read after
    every test on every worker, and three processes a test is the difference
    between a guard the suite does not feel and one it does. Git lowercases
    the keys it lists, so each is mapped back to the spelling it was declared
    under, which is the spelling a reader knows it by.
    """
    try:
        listed = sh.Command("git")(
            "-C", str(root), "config", "--local", "--list", "-z", _tty_out=False
        )
    except (sh.ErrorReturnCode, sh.CommandNotFound):
        return {}
    declared = {setting.lower(): setting for setting in settings}
    # lup: ignore[string-split] — git's own `-z` listing, one `key\nvalue`
    # entry per NUL by that format's definition
    entries = [entry.partition("\n") for entry in str(listed).split("\0") if entry]
    return {
        f"config {declared[key]}": value for key, _, value in entries if key in declared
    }


def repository_state(root: Path, namespace: str = "") -> dict[str, str]:
    """Everything the guard watches: every ref, and the config a fixture can leak.

    Minus the two namespaces this repository's own tooling *writes by design*
    while a suite runs. The permission dispatcher takes an undo snapshot in
    front of every command an agent is allowed, so a suite an agent starts has
    refs appearing under that namespace throughout — measured, twenty-four in
    the ninety seconds around one `dev check`, and eight identical teardown
    failures, one per xdist worker, naming refs no fixture had touched. And
    `dev check` runs its harness rows beside the two test suites, one of which
    probes the checkpoint store by writing a ref under the preflight namespace
    and deleting it again, which the worker running a test just then reported
    as that test's doing.

    Excluded rather than reported, on the strength of what the namespaces are.
    A ref moving in either carries no evidence either way: it is written from
    outside the suite on a schedule the suite does not control, so it is never
    the suite's doing, and a notice saying so on every agent-run check is the
    line people learn to skip above the line that mattered. And it cannot be
    the accident this guard exists for — that accident is a branch moving or a
    shared identity being overwritten, and both are trees hung outside
    `refs/heads`, pointed at by nothing, that move no branch and write no
    config.

    Each namespace is imported from the half that writes it rather than
    spelled again here, for the reason that half gives for being importable
    at all: the writer and the reader must not end up looking in two places
    for one safety net. ``namespace`` is a parameter for the callers that pass
    their own undo namespace — the same override
    :func:`~lup.policy.assets.host.undo_snapshot` takes, so a suite testing
    snapshots against a namespace of its own is still watched in the real one.
    """
    written = unwatched_namespaces(namespace)
    return {
        **{
            name: value
            for name, value in repository_refs(root).items()
            if not any(name.startswith(f"{where}/") for where in written)
        },
        **watched_config(root),
    }


def unwatched_namespaces(namespace: str = "") -> tuple[str, ...]:
    """The ref namespaces written by design while a suite runs, which the guard skips.

    One answer for both readings of the repository — the refs git lists and
    the files it keeps them in — so the cheap reading cannot vouch for a
    namespace the full one watches. :func:`repository_state` says why each
    is skipped.
    """
    return (namespace or undo_namespace(), preflight_namespace())


class FileStamp(BaseModel, frozen=True):
    """One file git reads refs or config from, as the filesystem describes it."""

    path: str
    inode: int
    size: int
    modified: int
    """``st_mtime_ns``: coarse on many kernels, so the inode is what decides."""


class RefStore(BaseModel, frozen=True):
    """Where git keeps what the guard watches, so a quiet test costs no process.

    Reading the state is two git processes, forked from a test worker whose
    whole heap the fork copies the page tables of — some twenty milliseconds
    after every test, and a tenth of what the suites cost. Git writes every
    file behind that state the same way: a lock file beside it, renamed over
    it. A ref, the packed list, the config — each write lands under a new
    inode, however coarse the clock, and a ref created or deleted adds or
    removes a file. So when no file here changed, nothing git would list
    changed either, and the processes are only asked when one did.
    """

    common: Path
    """The git directory every worktree of the repository shares."""

    own: Path
    """This checkout's own git directory, where its per-worktree refs live."""

    @classmethod
    def located(cls, root: Path) -> "RefStore | None":
        """The store behind the checkout enclosing ``root``, or ``None`` outside one."""
        try:
            listed = sh.Command("git")(
                "-C",
                str(root),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
                "--git-dir",
                _tty_out=False,
            )
        except (sh.ErrorReturnCode, sh.CommandNotFound):
            return None
        match str(listed).splitlines():
            case [common, own]:
                return cls(common=Path(common), own=Path(own))
            case _:
                return None

    def stamps(self, skipped: tuple[str, ...]) -> list[FileStamp] | None:
        """Every file the watched state is read from, or ``None`` mid-write.

        ``skipped`` are namespaces the state leaves out, whose directories are
        not walked either: a snapshot written in front of every command would
        otherwise send every test to the processes this exists to spare. A
        file vanishing between listing and reading is a write in progress,
        and answering nothing makes the caller read the state itself.
        """

        def listed(top: Path) -> Iterator[Path]:
            for directory, subdirectories, names in top.walk(follow_symlinks=True):
                subdirectories[:] = [
                    name
                    for name in subdirectories
                    if (directory / name).relative_to(top.parent).as_posix()
                    not in skipped
                ]
                yield from (directory / name for name in names)

        tops = dict.fromkeys(
            [self.common / "refs", self.common / "reftable", self.own / "refs"]
        )
        files = [
            *(self.common / name for name in ("packed-refs", "config")),
            *(path for top in tops for path in listed(top)),
        ]
        try:
            described = [(path, path.stat()) for path in files if path.exists()]
        except FileNotFoundError:
            return None
        return sorted(
            (
                FileStamp(
                    path=str(path),
                    inode=found.st_ino,
                    size=found.st_size,
                    modified=found.st_mtime_ns,
                )
                for path, found in described
            ),
            key=lambda stamp: stamp.path,
        )


def moved_refs(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Each ref the session created, deleted, or moved, as a readable line."""
    return [
        *(
            f"{name}: {before[name][:12]} -> {after[name][:12]}"
            for name in sorted(before.keys() & after.keys())
            if before[name] != after[name]
        ),
        *(f"{name}: created" for name in sorted(after.keys() - before.keys())),
        *(f"{name}: deleted" for name in sorted(before.keys() - after.keys())),
    ]


class Window(BaseModel, frozen=True):
    """Where a change was noticed: which worker, and the test it was running.

    Under xdist the suite is many sessions over one ref store, and a
    comparison closed once per session lands on whichever test tore down
    last on the worker that noticed — a policy row about `gh pr create` was
    failed for a branch a sibling session cut forty seconds into the run.
    Naming the window says the one thing the evidence supports: the change
    appeared while this test ran here.
    """

    worker: str
    """The xdist worker, or whatever the suite calls the only one."""

    test: str
    """The test whose window saw the change, as pytest names it."""

    def describe(self) -> str:
        """One line locating the change, for either report to carry."""
        return f"noticed on worker {self.worker}, during {self.test}"

    def located(self) -> list[str]:
        """The lines a failure opens with: where, and what that does not prove."""
        return [
            f"Change {self.describe()}.",
            "That is the test running here when the change appeared: the",
            "culprit when it came from this worker, and a bystander when",
            "another worker or another session wrote the shared ref store",
            "just then.",
            "",
        ]


def guard_report(
    before: dict[str, str], after: dict[str, str], window: Window | None = None
) -> str:
    """What to tell a developer whose checkout the suite just wrote into.

    The fixture reading leads, because it is the one the suite exists to
    catch and the one the reader cannot see for themselves. It is not the only
    one: where several sessions share a clone, a commit made in this worktree
    while the suite ran moves a ref exactly as a stray fixture does, and from
    the refs alone the two are the same event. So both are named, with the
    command that separates them — a reader handed one reading and meeting the
    other spends the length of a gate looking for a fixture that is not there.

    Empty when nothing moved, which is the caller's signal to say nothing.
    ``window`` locates the change where the caller settles per test; a caller
    comparing two bare states has none.
    """
    moved = moved_refs(before, after)
    if not moved:
        return ""
    return "\n".join(
        [
            "This test run modified the repository it is running inside.",
            "",
            *(window.located() if window else []),
            "A fixture bound git to the working directory rather than to its",
            "own throwaway repository, so this much of the real checkout",
            "changed:",
            "",
            *(f"  {line}" for line in moved),
            "",
            "A ref is recovered from `git reflog show <ref>`. A `config` line",
            "is worse than it looks: the shared config is inherited by every",
            "worktree cut from this repository, so it outlives this run in",
            "every session opened against it.",
            "",
            "A `user.*` line means commits made afterwards carry that author",
            "until it is unset — check `git log --format='%an <%ae>'` on",
            "recent work. A `core.hooksPath` line means this repository now",
            "runs no hooks at all: unset it, then re-arm the guards, and read",
            "what landed while they were off.",
            "",
            "Then find the fixture: it is one that runs git without",
            "`-C <tmp_path>` or without `monkeypatch.chdir` into the",
            "repository it built.",
            "",
            "If there is no such fixture, read the other cause: something",
            "committed in this worktree while the suite ran. `git reflog show",
            "<ref>` dates the move and names what made it, and an ordinary",
            "commit message there is a bystander rather than this run — from",
            "the refs alone the suite cannot tell the two apart. Re-run over",
            "a tree that has stopped moving.",
        ]
    )


class ForeignCheckouts(BaseModel, frozen=True):
    """Which refs a worktree other than the one under test answers for.

    Every worktree cut from a repository shares its ref store, so the guard
    reading `for-each-ref` in one of them sees every branch the repository
    holds — twenty-five of them here, of which one is the checkout the suite
    is running in. A commit landing in a sibling worktree while the suite runs
    moves a ref for real, and from the refs alone that is indistinguishable
    from a fixture escaping into the enclosing repository.

    Asking git who holds each branch is what tells them apart. It is a
    narrower question than "did anything move", and deliberately so: a ref
    another worktree has checked out is that worktree's to move, while a ref
    this one owns, or one that appeared from nowhere, is still the suite's to
    answer for.
    """

    holders: dict[str, str] = {}
    """Each ref another worktree answers for, against that worktree's path.

    The branch it has checked out, and the remote-tracking ref that branch
    follows -- a push moves the second as routinely as a commit moves the
    first, and neither is this run's doing.
    """

    def holder(self, key: str) -> str | None:
        """The other worktree that owns this key, when another one does."""
        return self.holders.get(key)

    def ours(self, state: dict[str, str]) -> dict[str, str]:
        """The watched state this checkout is answerable for."""
        return {key: value for key, value in state.items() if not self.holder(key)}

    def theirs(self, state: dict[str, str]) -> dict[str, str]:
        """The watched state another worktree owns."""
        return {key: value for key, value in state.items() if self.holder(key)}

    def joined(self, other: "ForeignCheckouts") -> "ForeignCheckouts":
        """This map and ``other`` together, either holder answering for a key.

        Read at both ends of a watch, because a worktree cut while the suite
        runs holds its branch at the end and not at the start: a map read
        only at the start reports that branch as appearing from nowhere,
        which is how a sibling session's `worktree create` failed a check it
        never touched. A worktree removed mid-run is the mirror case, and the
        map read at the start still holds what it held.
        """
        return ForeignCheckouts(holders=self.holders | other.holders)

    def verdict(
        self,
        before: dict[str, str],
        after: dict[str, str],
        window: Window | None = None,
    ) -> "GuardVerdict":
        """What moved, split into what this run answers for and what it does not.

        Config is never anybody else's: it is one shared file rather than a
        ref a worktree holds, so it stays on the failing side whatever moved.
        """
        return GuardVerdict(
            failure=guard_report(self.ours(before), self.ours(after), window),
            notice=foreign_notice(
                moved_refs(self.theirs(before), self.theirs(after)), window
            ),
        )

    @classmethod
    def beside(cls, root: Path) -> "ForeignCheckouts":
        """Every branch checked out in a worktree that is not ``root``.

        Read through `git worktree list`, which is the only thing that knows;
        a failure to read is reported as no siblings, so a guard that cannot
        answer the narrower question falls back to failing on everything
        rather than passing on everything.
        """
        try:
            listed = sh.Command("git")(
                "-C", str(root), "worktree", "list", "--porcelain", _tty_out=False
            )
        except (sh.ErrorReturnCode, sh.CommandNotFound):
            return cls()
        held = cls.declared(str(listed), root.resolve())
        return cls(holders=held | cls.tracked(root, held))

    @classmethod
    def declared(cls, listing: str, own: Path) -> dict[str, str]:
        """Each `branch` line of a porcelain listing, minus ``own``'s entry.

        The format is one stanza per worktree, `worktree <path>` opening each
        and `branch <ref>` naming what it holds; a detached or bare entry
        simply carries no branch line and so claims no ref.
        """
        held: dict[str, str] = {}  # lup: ignore[empty-collection] — stanza fold
        at = Path()
        for line in listing.splitlines():
            # lup: ignore[string-split] — git's own porcelain, whose key and
            # value sit either side of one space by that format's definition
            key, _, value = line.partition(" ")
            match key:
                case "worktree":
                    at = Path(value).resolve()
                case "branch" if at != own:
                    held[value] = str(at)
                case _:
                    continue
        return held

    @classmethod
    def tracked(
        cls, root: Path, held: dict[str, str], ref_format: str = UPSTREAM_FORMAT
    ) -> dict[str, str]:
        """Each held branch's remote-tracking ref, against the same worktree.

        A sibling that commits usually pushes, and the push moves
        `refs/remotes/<remote>/<branch>` as surely as the commit moved the
        branch. Only the branch half carries a worktree in `git worktree
        list` though, because a remote-tracking ref is checked out by nobody
        -- so without this it reads as a ref that appeared from nowhere, and
        the routine event fails the run exactly as before.

        Which remote ref belongs to which branch is asked of git as
        `upstream` rather than assembled from the two names: a branch may
        track a remote it is not named after, and neither name can be split
        back out of `refs/remotes/a/b/c` by guesswork.

        Two relations answer that, because neither covers the other. A branch
        already tracking one names it in `upstream`, which is exact even where
        the two are named differently. A branch whose remote lives in lup's
        own records names nothing here -- `git pr push` states its destination
        as a refspec and writes no tracking config, and a first push has not
        reached even that when this reads -- so the ref such a push creates is
        also claimed under each configured remote, which is the correspondence
        `git push` itself uses.

        Claiming a ref that never appears costs nothing: only refs the run
        actually saw move are ever looked up. A remote whose branch this
        checkout holds is never claimed, because that branch never reaches
        ``held``.
        """
        upstreams = repository_refs(root, ref_format)
        remotes = cls.remotes(root)
        found: dict[str, str] = {}  # lup: ignore[empty-collection] — two folds
        for branch, at in held.items():
            name = branch.removeprefix("refs/heads/")
            for remote in remotes:
                found[f"refs/remotes/{remote}/{name}"] = at
            if branch in upstreams and upstreams[branch]:
                found[upstreams[branch]] = at
        return found

    @classmethod
    def remotes(cls, root: Path) -> list[str]:
        """Every remote the repository configures, or none when git cannot say.

        None on failure keeps the module's rule that a guard which cannot
        answer falls back to failing on everything rather than passing on it.
        """
        try:
            listed = sh.Command("git")("-C", str(root), "remote", _tty_out=False)
        except (sh.ErrorReturnCode, sh.CommandNotFound):
            return []
        return [line for line in str(listed).splitlines() if line]


class GuardVerdict(BaseModel, frozen=True):
    """What the guard found, split by who is answerable for it."""

    failure: str = ""
    """The report to fail the run on, empty when nothing this run owns moved."""

    notice: str = ""
    """What moved in a sibling worktree: worth saying, not worth failing."""


def foreign_notice(moved: list[str], window: Window | None = None) -> str:
    """What to say about refs a sibling worktree moved under the suite's feet."""
    if not moved:
        return ""
    return "\n".join(
        [
            "Refs moved while this suite ran, in worktrees that own them:",
            "",
            *(f"  {line}" for line in moved),
            "",
            *([f"Change {window.describe()}.", ""] if window else []),
            "Another session committing in a sibling worktree is not this run",
            "writing into the checkout, so the suite is not failed for it. A",
            "ref this worktree owns, or one that appeared from nowhere, still",
            "is.",
        ]
    )


class RepositoryWatch(BaseModel):
    """One worker's watch over the repository enclosing its suite, test by test.

    A comparison per test rather than one per session, because a session
    under xdist is one worker's share of the suite and its single difference
    lands on whichever test that worker ran last. Settling after each test
    against a baseline that moves with it lays a change at the door of the
    test whose window saw it, on the worker that saw it, and leaves the tests
    after it answering for their own windows rather than for the same change
    again.
    """

    root: Path
    """The checkout the suite runs inside, which is what the watch reads."""

    worker: str
    """Who is watching, for the report: the xdist worker, or the only one."""

    foreign: ForeignCheckouts
    """Which refs sibling worktrees answer for, as last read."""

    baseline: dict[str, str]
    """The state every later reading is compared against, moving with each."""

    store: RefStore | None
    """Where that state is kept, or ``None`` outside a repository."""

    stamps: list[FileStamp] | None
    """The store's files as they stood before ``baseline`` was read.

    ``None`` vouches for nothing, so the next settlement reads the state."""

    @classmethod
    def armed(cls, root: Path, worker: str) -> "RepositoryWatch":
        """A watch reading the repository as it stands before the first test."""
        store = RefStore.located(root)
        # The files before the state, for the reason `after` gives.
        stamps = store.stamps(unwatched_namespaces()) if store else None
        baseline = repository_state(root)
        return cls(
            root=root,
            worker=worker,
            foreign=ForeignCheckouts.beside(root),
            store=store,
            stamps=stamps,
            baseline=baseline,
        )

    def after(self, test: str) -> GuardVerdict:
        """What moved since the last settlement, laid at ``test``'s door.

        The sibling map is re-read only when something moved: a worktree cut
        mid-run is found then, at the cost of one listing per change rather
        than per test. The state itself is read only when a file behind it
        changed (see :class:`RefStore`), so the quiet case — every test of
        every run — costs a walk of a few directories. The files are read
        before the state, never after: a write landing between the two is
        then in the state already or in the next reading of the files, and
        a change the state has not seen can never hide behind files that
        already show it.
        """
        stamps = self.store.stamps(unwatched_namespaces()) if self.store else None
        if stamps is not None and stamps == self.stamps:
            return GuardVerdict()
        current = repository_state(self.root)
        self.stamps = stamps
        if current == self.baseline:
            return GuardVerdict()
        self.foreign = self.foreign.joined(ForeignCheckouts.beside(self.root))
        verdict = self.foreign.verdict(
            self.baseline, current, Window(worker=self.worker, test=test)
        )
        self.baseline = current
        return verdict
