"""Worktree create, list, and remove operations."""

import shutil
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import sh
import typer
from pydantic import BaseModel, ValidationError

from lup.execution.git import GitError, Repository, Worktree
import lup.devtools.dev.records as records
from lup.coordination.identity import session_member_id
from lup.coordination.repository import RepositoryPeers
from lup.providers.harness import every_runtime
from lup.devtools.dev.git_guards import (
    DECLARED_GUARDS,
    GitGuard,
    arm,
    arming_is_refused,
    blocked_arming,
    hooks_directory,
    read_guards,
)
from lup.policy.assets.host import project_environment
from lup.web.build import dependencies_behind, restore_dependencies
from lup.devtools.layout import find_tree_dir, get_tree_dir
from lup.devtools.clipboard import copy_to_clipboard
from lup.launch.environments import remove_worktree_environment
from lup.launch.homes import remove_checkout_homes
from lup.execution.shell import git
from lup.launch.pointer_trust import judged_roots
from lup.sandbox.pointers import tree_checkouts
from lup.devtools.utils import (
    attributed_stderr,
    clear_stale_config_locks,
    config_lock_diagnosis,
    decode_stderr,
    format_table,
    refuse_blocked_config_writes,
    short_sha,
)
from lup.diagnostics import refuse
from lup.policy.kernel.diagnostic import devtools, step


class RelocationHint(BaseModel):
    """How the harness a command is running under follows a new worktree."""

    agent: str
    shell: str


type WorktreeLauncher = Callable[[Path], RelocationHint]


# Gitignored paths that `git worktree add` does not carry over but a working
# worktree still needs (local secrets/settings, logs, sync `refs/`
# symlinks); `create` copies them into the new worktree unless --no-copy-data.
GITIGNORED_EXTRAS = [
    ".env.local",
    "sync.json.local",
    *(runtime.tree("personal_settings") for runtime in every_runtime()),
    "logs",
    "refs",
]


def copy_gitignored_extras(
    source_root: Path,
    worktree_path: Path,
    extras: list[str] = GITIGNORED_EXTRAS,
) -> list[str]:
    """Copy each present gitignored extra into a worktree; report what moved."""
    copied: list[str] = []  # lup: ignore[empty-collection] — copy report fold
    for rel_path in extras:
        src = source_root / rel_path
        if not src.exists():
            continue
        dst = worktree_path / rel_path
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        copied.append(rel_path)
    return copied


def sync_dependencies(worktree_path: Path) -> None:
    """Sync one worktree's environment; warn instead of failing the caller."""
    try:
        sh.Command("env")(
            "-u", "VIRTUAL_ENV", "uv", "sync", "--all-extras", _cwd=str(worktree_path)
        )
    except sh.ErrorReturnCode as e:
        typer.echo(f"Warning: uv sync failed: {decode_stderr(e)}")


def branch_exists(branch: str) -> bool:
    """Check if a git branch exists (local only)."""
    return Repository(Path.cwd()).resolves(f"refs/heads/{branch}") is not None


def worktree_is_registered(path: Path) -> bool:
    """Check if a path is registered as a git worktree (even if dir is missing)."""
    resolved = path.resolve()
    return any(listed.path == resolved for listed in Repository(Path.cwd()).worktrees())


def adopt_records() -> None:
    """Empty lup's own keys out of the shared config, saying what moved.

    Reads answer from the record alone, so a base still held in ``config``
    counts for nothing until this moves it. The move also leaves a shared
    ``config`` holding nothing lup wrote — which is what lets that file,
    whose keys name programs git runs on the host, stop having to be
    writable by a worker.
    """
    refuse_blocked_config_writes()
    moved = list(records.adopt_config_records())
    for line in moved:
        typer.echo(line)
    typer.echo(f"Adopted {len(moved)} record(s) out of the shared config.")


# lup: ignore[constant-declaration] — the driver name `.gitattributes` and this
# registration must spell alike for git to find one from the other
OWNERSHIP_MERGE_DRIVER = "lup-ownership"


def register_merge_driver() -> None:
    """Teach this clone the merge driver ``.gitattributes`` names.

    A driver that exits without writing leaves git holding one side, which is
    the whole resolution a generated digest manifest can have — regeneration
    settles it afterwards. Git resolves driver names from config alone, so no
    repository can ship this and every clone registers it once; the config is
    shared with every worktree of the same repository.
    """
    git("config", f"merge.{OWNERSHIP_MERGE_DRIVER}.name", "keep one side, regenerate")
    git("config", f"merge.{OWNERSHIP_MERGE_DRIVER}.driver", "true")


def merge_driver_registered(root: Path | None = None) -> bool:
    """Whether this clone can already resolve the driver `.gitattributes` names."""
    return all(
        git.out(
            *records.at(root),
            "config",
            "--get",
            f"merge.{OWNERSHIP_MERGE_DRIVER}.{setting}",
            _ok_code=[0, 1],
        )
        for setting in ("name", "driver")
    )


def report_a_blocked_registration(root: Path | None = None) -> bool:
    """Name a merge-driver registration this clone cannot make, without stopping.

    Registering the driver is a config write left in making a worktree — the
    other is a bare clone's reflog, :func:`report_a_blocked_reflog`'s — and
    it is a once-per-clone one: git resolves a driver name from config
    alone, so no repository can ship it and the clone that first cut a
    worktree registered it for every worktree after. A clone that already
    resolves the driver writes nothing here and is not asked about it.

    A clone that cannot -- the shared `config` is held read-only in every
    contained session, and the registration is a host's act -- is told so and
    given the worktree anyway. Refusing it would protect nothing: the driver
    decides how a *merge* of the generated trees resolves, which a worktree
    cut without it meets no sooner than every worktree of this clone already
    does, and what a refusal costs is the work itself -- a branch falling back
    to plain `git worktree add`, or a resolver run unable to lease its first
    concern inside the sandbox.
    Where the write would simply happen, nothing is said.

    Answers whether the registration is blocked, which the setup step reads
    so the worktree is handed over usable rather than reported unfinished.
    """
    if merge_driver_registered(root):
        return False
    for cleared in clear_stale_config_locks(root):
        typer.echo(cleared)
    diagnosis = config_lock_diagnosis(root)
    if not diagnosis:
        return False
    typer.echo(diagnosis, err=True)
    typer.echo(
        f"The {OWNERSHIP_MERGE_DRIVER} merge driver stays unregistered for this "
        "clone, so a merge touching the generated trees resolves as a plain "
        "three-way merge until it is. Register it once from a host terminal: "
        "`uv run lup-devtools git merge-driver`.",
        err=True,
    )
    return True


def report_a_blocked_reflog(diagnosed: bool, root: Path | None = None) -> bool:
    """Name a reflog setting this bare clone cannot take, without stopping.

    The other once-per-clone write to the shared ``config``, and blocked where
    the merge driver's is: a contained session holds that file read-only, so
    turning the reflog on is a host's act. ``diagnosed`` is whether
    :func:`report_a_blocked_registration` already said why the config cannot
    be written, which is then not said twice. A clone whose reflog is on, or
    that is not bare, writes nothing here and is not asked about it.

    Answers whether the setting is blocked, so the step is left out rather
    than reported unfinished on every later run.
    """
    if not records.reflog_off(root):
        return False
    if not diagnosed:
        for cleared in clear_stale_config_locks(root):
            typer.echo(cleared)
        diagnosis = config_lock_diagnosis(root)
        if not diagnosis:
            return False
        typer.echo(diagnosis, err=True)
    shared = records.shared_directory(root)
    typer.echo(
        "Git's reflog stays off for this bare clone, so a branch cut by running "
        "git against its git directory names no base git logged. Turn it on "
        f"once from a host terminal: `git -C {shared} config "
        "core.logAllRefUpdates true`.",
        err=True,
    )
    return True


def report_a_blocked_arming(
    guards: list[GitGuard] = DECLARED_GUARDS, root: Path | None = None
) -> None:
    """Name the guards this clone has not armed, without stopping over them.

    The same pre-flight moment as :func:`refuse_a_blocked_registration`, over
    the other thing under the shared directory whose contents name a program
    the host runs, and answered differently. `<common>/hooks/` holds scripts git executes at the operator's
    next commit in any worktree of this repository, so it is held read-only —
    and holding it costs nothing, because arming is a once-per-clone act too:
    hooks resolve through that shared directory, so a guard armed on the host
    covers every worktree cut after it.

    Reported rather than refused, because refusing protects nothing it names.
    Hooks resolve through the shared directory, so the worktree about to be
    cut inherits exactly the arming every existing worktree of this clone
    already commits under: if the guard is stale, every checkout here has been
    running the stale one all along, and stopping this one creates no exposure
    it did not already have. What refusing does reach is the work — a clone
    whose `<common>/hooks/` is read-only cannot arm from inside the sandbox at
    all, so the gate would stand on every worktree forever, and the way past
    it is to stop using the command that records a base.

    So the outstanding moment is named and the run continues. The reader is
    told which host command settles it, which is the whole of what refusing
    communicated, and keeps the checkout that the refusal cost them.
    """
    diagnosis = blocked_arming(guards, root if root is not None else Path.cwd())
    if diagnosis:
        typer.echo(diagnosis, err=True)


class SetupStep(BaseModel, ABC, frozen=True):
    """One part of making a worktree usable, checkable on its own.

    A worktree is registered by one git call and made usable by several more,
    so an interruption between them leaves a directory that exists without
    being ready — and a later run that asks only whether the directory exists
    calls that ready. Each step reads its own effect off the worktree instead,
    rather than off a record of what an earlier run meant to do, so a re-run
    finishes exactly what was left and ``already active`` can mean ready
    rather than merely present.
    """

    @abstractmethod
    def label(self) -> str:
        """What to call this step where it has to be named as missing."""

    @abstractmethod
    def satisfied(self) -> bool:
        """Whether this step's effect is already there to be found."""

    @abstractmethod
    def run(self) -> None:
        """Carry the step out; whether it worked is re-read, never reported."""

    def required(self) -> bool:
        """Whether a worktree without this step's effect is unusable.

        A step that answers no is still run and still re-read, and is still
        named where it did not finish — what it does not do is fail the
        command. Anything reaching the network belongs here: a checkout made
        offline is a checkout to work in, and refusing to hand one over
        because a remote could not be told about it would cost more than the
        step itself is worth.
        """
        return True


class MergeDriver(SetupStep, frozen=True):
    """The merge driver ``.gitattributes`` names, registered for this clone.

    ``blocked`` is what :func:`report_a_blocked_registration` found: a shared
    config this session cannot write. The step then neither tries the write
    nor fails the worktree over it -- the registration is the host's act, and
    the checkout is usable without it.
    """

    blocked: bool = False

    def label(self) -> str:
        return f"the {OWNERSHIP_MERGE_DRIVER} merge driver"

    def satisfied(self) -> bool:
        return merge_driver_registered()

    def run(self) -> None:
        if not self.blocked:
            register_merge_driver()

    def required(self) -> bool:
        return not self.blocked


class LoggedRefUpdates(SetupStep, frozen=True):
    """Git's own reflog, turned on for a bare clone left at git's default.

    What lets a branch cut by plain git against the git directory itself name
    its base through :func:`lup.devtools.dev.branches.created_from`, the same
    as one cut from any worktree. Once per clone, like the merge driver, and
    never required: the base this command records answers for its own branch
    either way.
    """

    def label(self) -> str:
        return "git's reflog for this bare clone (core.logAllRefUpdates)"

    def satisfied(self) -> bool:
        return not records.reflog_off()

    def run(self) -> None:
        records.log_ref_updates()

    def required(self) -> bool:
        return False


class ArmedGitGuards(SetupStep, frozen=True):
    """The refusals of stale output and a failing gate, installed here.

    A worktree is where a commit is made and where a branch is pushed from,
    so it is where the guards have to be armed: a check that only runs when
    somebody remembers to run it is what let two artifact-stale commits land.

    Armed from the project's own declaration rather than from lup's default
    pair. Reading the default instead made a repository that guards its
    moments differently unable to finish setup at all: the step arms hooks
    that repository never declared, finds its own hooks at those paths and
    reads them as somebody else's, and reports itself unsatisfied on every
    re-run — a worktree that is never ready over guards nobody asked for.
    """

    guards: list[GitGuard] = DECLARED_GUARDS
    worktree: Path

    def label(self) -> str:
        moments = dict.fromkeys(guard.hook for guard in self.guards)
        return f"the {' and '.join(moments)} guards"

    def satisfied(self) -> bool:
        return all(state.armed for state in read_guards(self.guards, self.worktree))

    def run(self) -> None:
        for line in arm(self.guards, self.worktree):
            typer.echo(line)

    def required(self) -> bool:
        """Whether a worktree this step could not finish is unusable.

        Arming writes into `<common>/hooks/`, which every worktree of the
        clone resolves to and which a confined session may be unable to write
        at all. Where it is held, no run from here can satisfy this step, and
        the worktree being cut inherits exactly the arming every other
        worktree here already commits under — so failing the command would
        withhold a checkout on every run, permanently, while arming nothing.

        Answered from the directory rather than declared, because the two
        cases are genuinely different: a clone that can arm and did not is a
        worktree whose commits would skip the drift gate, and that is worth
        refusing over. The report names the outstanding moment and the host
        command that settles it either way.
        """
        return not arming_is_refused(hooks_directory(self.worktree))


def commits_ahead(branch: str, other: str) -> int:
    """How many commits ``branch`` carries that ``other`` lacks.

    Zero says the two sit on one line, whichever of them is further along,
    and zero is also the answer for no branch at all: a detached HEAD names
    nothing to compare, and its caller is refused for that reason rather than
    told two bases differ.

    Unrelated histories count as the whole of ``branch``, since ``other..branch``
    is ``branch ^other`` and needs no merge base — two trees sharing nothing
    read as maximally apart, which is what they are.
    """
    if not branch:
        return 0
    try:
        return Repository(Path.cwd()).count(f"{other}..{branch}")
    except GitError:
        return 0


class BranchBase(BaseModel, frozen=True):
    """What a worktree's branch is cut from, and what is recorded as its base.

    Two intents reach this command through identical arguments. One is new
    work, which belongs on the integration branch: cut from the feature
    checkout the command happened to run in, it carries commits its own pull
    request never asked for — the request opens conflicting, no CI runs on
    it, and the repair is a rebase and a force-push on work that was correct.
    The other is the continuation of the checkout's own work, which belongs
    on that branch: cut from the integration branch instead, it lacks the
    code it was written against, and the repair is a reset onto the branch it
    should have started at, plus the base record that went with it.

    Nothing in the arguments separates them, so neither default is right and
    the cost is in guessing rather than in which way one guesses. Where the
    two answers differ, :meth:`refuse_guessing` says so before anything is created
    and names both spellings; advice printed after the branch exists is
    advice nobody can act on without an undo.

    They differ only where the checkout's branch carries commits the
    integration branch lacks. Standing on the integration branch, on a
    workspace nobody has committed to, or on work that has already landed,
    ``ahead`` is zero, both answers lie on one line, and the integration
    branch is simply the later point on it — so that is taken, silently,
    which is the ordinary case for a session cutting a sibling of the
    worktree it just made.

    A branch that already exists is not cut at all: ``worktree add <path>
    <branch>`` takes it where it stands, and a base could only reach it by
    moving it. Nothing here moves one, so the record for a re-attached branch
    stays what a caller named or what the checkout says.
    """

    branch: str
    named: str | None
    current: str
    integration: str
    fresh: bool
    ahead: int = 0
    """Commits the invoking checkout's branch carries that the integration lacks."""

    def cut_from(self) -> str | None:
        """The ref the branch starts at; ``None`` starts it where HEAD is."""
        if self.named:
            return self.named
        if self.fresh and self.current:
            return self.integration
        return None

    def recorded(self) -> str:
        """The name written as this branch's base, empty where nobody can say."""
        return self.cut_from() or self.current

    def guessed(self) -> bool:
        """Whether the two bases give different trees and nobody named one."""
        return not self.named and self.fresh and self.ahead > 0

    def refuse_guessing(self) -> None:
        """Stop where the base would be a guess, naming both ways to settle it.

        Read before the worktree is registered, so whichever of the two the
        caller meant costs them one re-run rather than an undo.
        """
        if not self.guessed():
            return
        refuse(
            f"this ran in a checkout on {self.current}, which carries"
            f" {self.ahead} commit(s) {self.integration} does not, so the two"
            " bases give different trees and nothing says which one is meant",
            what=self.branch,
            steps=[
                step(
                    "continue that work, stacking on this checkout",
                    devtools(
                        "git", "worktree", "create", self.branch, "--base", self.current
                    ),
                ),
                step(
                    "or start fresh from where work lands",
                    devtools(
                        "git",
                        "worktree",
                        "create",
                        self.branch,
                        "--base",
                        self.integration,
                    ),
                ),
            ],
        )


class RecordedBase(SetupStep, frozen=True):
    """The branch a worktree was cut from, recorded where detection reads it.

    Recorded wherever it is missing rather than only on a branch this run
    created: an interrupted run leaves the branch made and the record unmade,
    and afterwards the two are indistinguishable. A branch that already
    carries a record keeps it.

    The commit is recorded beside the branch name because they answer
    different questions and only one of them keeps answering. A name says
    what this was cut from, which is what recovering a base needs; the commit
    says where the branch stood when nobody had worked on it yet, which is
    the only thing that still separates a workspace nobody opened from a
    branch whose work landed. Topology stops separating them the moment work
    lands by fast-forward — the tip is then the integration branch's own tip,
    exactly as an untouched workspace's is — so a reader with no record has
    no question left to ask.

    Which is why only a branch this run cut carries the commit, where the
    name is recorded wherever it is missing. The name answers what a base
    was, and a re-attached branch's base is the same fact whenever it is
    written down; the commit asserts that nobody has worked here, and against
    a branch that already existed the run has no standing to assert it. Taking
    the tip at re-attach time would record whatever had been committed as the
    place nothing was committed, and a branch reserved by that reading holds
    unlanded work behind a verb that means the opposite — the one direction
    this record must never fail in. Nor can the tip be checked against the
    base's instead: a branch whose work landed by fast-forward stands exactly
    where an untouched one does, which is the whole reason the record exists.
    So an interrupted run leaves a workspace no later run can vouch for, and
    it is offered for deletion — a question answered once, against a spent
    branch that would otherwise be hidden for good.
    """

    branch: str
    origin: str
    cut_fresh: bool = False

    def label(self) -> str:
        return f"the base recorded for {self.branch}"

    def already_recorded(self) -> bool:
        """Whether a base is written for this branch, whoever wrote it."""
        return bool(records.recorded_base(self.branch))

    def satisfied(self) -> bool:
        if self.origin == self.branch:
            return True
        return self.already_recorded()

    def run(self) -> None:
        reserved = git.out("rev-parse", self.branch).strip() if self.cut_fresh else ""
        records.remember(
            self.branch,
            records.BranchRecord(base=self.origin, base_commit=reserved),
        )


class CopiedExtras(SetupStep, frozen=True):
    """The gitignored files a checkout needs that ``worktree add`` leaves behind."""

    source: Path
    worktree: Path
    extras: list[str]

    def label(self) -> str:
        return "the gitignored extras"

    def satisfied(self) -> bool:
        return all(
            (self.worktree / rel_path).exists()
            for rel_path in self.extras
            if (self.source / rel_path).exists()
        )

    def run(self) -> None:
        for rel_path in copy_gitignored_extras(self.source, self.worktree, self.extras):
            typer.echo(f"Copied {rel_path}")


class SyncedEnvironment(SetupStep, frozen=True):
    """The environment ``uv sync`` builds inside the worktree.

    Its absence is the expensive failure, and the reason a worktree that only
    exists cannot be called ready: pyright resolves the project from wherever
    else it can and reports errors in code nobody touched, which reads as a
    bug in the change rather than as setup that never ran.
    """

    worktree: Path

    def label(self) -> str:
        return f"the synced environment ({project_environment(self.worktree).name})"

    def satisfied(self) -> bool:
        """Whether this worktree has an environment of its own.

        Its own, which ``is_dir()`` alone does not ask: that call follows the
        link, so an environment symlinked to a sibling worktree's answers yes
        and the sync is skipped. What the worktree then holds is a name
        pointing at somebody else's environment, and a ``uv sync`` reached
        through it repoints *that* worktree's editable install at this one's
        source — two checkouts import one tree, and the branch under test is
        whichever synced last. Nothing reports it, which is the expensive
        failure this class names, arrived at from the other side.

        A link is unsatisfied rather than an error, because the step's whole
        job is to build the environment: a run that builds a real one over the
        link leaves the worktree in the state the step promised.
        """
        environment = project_environment(self.worktree)
        return environment.is_dir() and not environment.is_symlink()

    def run(self) -> None:
        """Build this worktree's own environment, clearing a link left in its place.

        The link goes first because syncing through it is the failure rather
        than the repair: `uv` resolves the path, finds the sibling's
        environment at the end of it, and rewrites that one's editable install
        to point here. Only the link is removed — what it named belongs to
        another worktree and is left exactly as it is.
        """
        environment = project_environment(self.worktree)
        if environment.is_symlink():
            typer.echo(f"Removing {environment}, a link to {environment.readlink()}")
            environment.unlink()
        typer.echo("Running uv sync...")
        sync_dependencies(self.worktree)


class SyncedSubProject(SetupStep, frozen=True):
    """A declared sub-project's own environment, synced inside the worktree.

    Its execution environment names the packages that environment holds, so
    until the sync runs the per-edit check reports every third-party import
    in the sub-project unresolved — the same false alarm the root's own sync
    exists to prevent, from a project the root's sync never reaches.

    Not required: a worktree whose sub-project is not synced is one to work
    in, and the gate syncs it before reading it anyway. A sub-project the new
    tree does not hold — cut from a branch older than its declaration — is
    nothing to sync.
    """

    worktree: Path
    project: Path
    """Where the sub-project's `pyproject.toml` lives, relative to the worktree."""

    def label(self) -> str:
        return f"the synced sub-project environment ({self.project})"

    def satisfied(self) -> bool:
        held = self.worktree / self.project
        return (
            not (held / "pyproject.toml").is_file()
            or project_environment(held).is_dir()
        )

    def run(self) -> None:
        typer.echo(f"Running uv sync in {self.project}...")
        sync_dependencies(self.worktree / self.project)

    def required(self) -> bool:
        return False


class RestoredWorkspace(SetupStep, frozen=True):
    """A bun workspace's dependencies, restored from its lockfile in the worktree.

    The restore the gate runs before `bun test` and the bundle build, done
    once at creation so the first `dev check` is not the run that pays for
    it. Not required, and not only because it reaches the registry for what
    the cache lacks: the gate restores whatever it finds behind, so a worktree
    without this step is one to work in, where one without its environment
    is not.

    A workspace the new tree does not hold is nothing to restore rather than
    something behind. The list comes from the project the command was *run
    in*, and ``--base`` cuts from any branch, so a tree whose layout differs
    — one obtaining the library as a link where the other vendors it — is
    asked to restore a directory it has no copy of.
    """

    worktree: Path
    workspace: Path
    """Where `package.json` and `bun.lock` live, relative to the worktree."""

    def label(self) -> str:
        return f"the restored bun workspace ({self.workspace})"

    def satisfied(self) -> bool:
        held = self.worktree / self.workspace
        return not held.is_dir() or not dependencies_behind(held)

    def run(self) -> None:
        typer.echo(f"Running bun install --frozen-lockfile in {self.workspace}...")
        try:
            restore_dependencies(self.worktree / self.workspace)
        except RuntimeError as failed:
            typer.echo(f"Warning: {failed}")

    def required(self) -> bool:
        return False


class WorktreeHold(BaseModel, frozen=True):
    """The session a checkout was created by, written into git's lock on it.

    The session that runs `git worktree create` is rarely standing in what it
    creates: it was launched in another checkout and writes into the new one
    by absolute path, so the roster never names it as that checkout's user.
    Once its work is committed the checkout reads as clean and spent, and a
    lander would remove it while the session is still writing there.

    So creation locks the checkout with this as the reason. A lock is where
    every removal here already looks, and a plain `git worktree remove` meets
    it too. It names a session rather than deciding anything: whether that
    session is still live is the roster's to say, so the hold ends when the
    session leaves and nobody has to remember to release it.
    """

    session: str
    """The roster id of the session that created the checkout."""

    called: str = ""
    """What that session was called then, for a reader of `git worktree list`."""

    @classmethod
    def read(cls, reason: str) -> "WorktreeHold | None":
        """The hold a lock reason spells, or nothing where somebody else locked it."""
        try:
            return cls.model_validate_json(reason)
        except ValidationError:
            return None

    def live(self, peers: RepositoryPeers) -> bool:
        """Whether the session this names is still working, by the roster."""
        return self.session in peers.live_ids()


def lock_reason(path: Path) -> str | None:
    """Why this checkout is locked, empty where no reason was given, None if not."""
    from lup.devtools.dev.branches import locked_worktrees

    return next(
        (
            reason
            for locked, reason in locked_worktrees().items()
            if Path(locked).resolve() == path.resolve()
        ),
        None,
    )


def hold_on(path: Path) -> WorktreeHold | None:
    """The hold a session's creation left on this checkout, if one did."""
    reason = lock_reason(path)
    return WorktreeHold.read(reason) if reason is not None else None


class HeldForSession(SetupStep, frozen=True):
    """The creating session's hold on its checkout, taken at creation.

    Never taken from a live session holding it already, or from a lock that
    is not a session's hold: re-attaching a worktree somebody else is using
    does not make it the re-attacher's. A hold whose session has left is
    taken over, since it is nobody's any more.
    """

    worktree: Path
    session: str
    called: str = ""

    def label(self) -> str:
        return f"the hold for session {self.called or self.session}"

    def satisfied(self) -> bool:
        held = hold_on(self.worktree)
        return held is not None and held.session == self.session

    def run(self) -> None:
        reason = lock_reason(self.worktree)
        if reason is not None:
            held = WorktreeHold.read(reason)
            if held is None or held.live(RepositoryPeers(self.worktree)):
                return
            git("worktree", "unlock", str(self.worktree))
        hold = WorktreeHold(session=self.session, called=self.called)
        git("worktree", "lock", "--reason", hold.model_dump_json(), str(self.worktree))

    def required(self) -> bool:
        return False


def finish(steps: Sequence[SetupStep]) -> Iterator[SetupStep]:
    """Run each step given, yielding the ones still unfinished afterwards.

    Whether a step worked is re-read from the worktree rather than taken from
    whether it raised: a step whose tool exited badly and a step that quietly
    produced nothing leave the same worktree behind, and both are judged by
    what a later run will find rather than by their own account of themselves.
    """
    for setting in steps:
        try:
            setting.run()
        except sh.ErrorReturnCode as e:
            typer.echo(
                f"Warning: {setting.label()} failed: {decode_stderr(e)}", err=True
            )
        if not setting.satisfied():
            yield setting


def register_worktree(name: str, worktree_path: Path, base_branch: str | None) -> None:
    """Register the worktree itself, the one step nothing else can precede."""
    git("worktree", "prune")
    already_exists = branch_exists(name)

    # A named base that re-attaching cannot honour, refused rather than
    # dropped. `worktree add <path> <branch>` takes a branch where it already
    # is, so the flag reaches nothing -- and the caller then writes against a
    # tree they did not ask for, which is the expensive way to find out. The
    # branch is never moved to answer this: whatever sits on it would go.
    if (
        already_exists
        and base_branch
        and not Repository(Path.cwd()).is_ancestor(base_branch, name)
    ):
        refuse(
            f"already exists and does not descend from {base_branch}, so"
            f" --base {base_branch} would reach nothing: re-attaching takes a"
            " branch where it already stands",
            what=name,
            steps=[
                step(
                    "re-attach it where it stands",
                    devtools("git", "worktree", "create", name),
                ),
                step(
                    f"or cut a fresh branch from {base_branch} under a name that"
                    " does not exist yet",
                    devtools(
                        "git", "worktree", "create", "<new-name>", "--base", base_branch
                    ),
                ),
                step(
                    f"or move {name} yourself first, if discarding what is on it"
                    " is meant"
                ),
            ],
        )

    if already_exists:
        typer.echo(f"Re-attaching worktree: {worktree_path}")
        typer.echo(f"Existing branch: {name}")
    else:
        typer.echo(f"Creating worktree: {worktree_path}")
        typer.echo(f"New branch: {name}")

    try:
        match (already_exists, base_branch):
            case (True, _):
                git("worktree", "add", str(worktree_path), name)
            case (False, str() as base) if base:
                git("worktree", "add", str(worktree_path), "-b", name, base)
            case _:
                git("worktree", "add", str(worktree_path), "-b", name)
    except sh.ErrorReturnCode as e:
        refuse(f"git could not add the worktree: {decode_stderr(e)}", what=name)


def refuse_redirected_pointers() -> None:
    """Refuse before host git acts on a worktree whose pointer was moved.

    Judges the checkout this command runs in, and every directory standing
    where a worktree stands in ``tree/`` where there is one, each discovered
    from the repository that vouches for it rather than trusted for its place
    -- see :func:`lup.launch.pointer_trust.judged_roots`. Run from the host this
    is also where lup remembers the repository a worktree is cut from, before
    any container ran in it. Outside a repository nothing is judged, so this
    is safe to call before any host git command rather than only the worktree
    ones.
    """
    here = Path.cwd()
    tree = find_tree_dir()
    trust = judged_roots([here, *(tree_checkouts(tree) if tree else [])], operator=here)
    for notice in trust.notices:
        typer.echo(notice, err=True)
    if trust.refusal:
        refuse(trust.refusal)


def create(
    name: str,
    no_sync: bool,
    no_copy_data: bool,
    base_branch: str | None,
    launcher: WorktreeLauncher,
    force: bool = False,
    no_record: bool = False,
    clipboard: bool = False,
    extras: list[str] = GITIGNORED_EXTRAS,
    guards: list[GitGuard] = DECLARED_GUARDS,
    workspaces: Sequence[Path] = (),
    projects: Sequence[Path] = (),
) -> None:
    """Create a git worktree, re-attach one, or finish one left half-made.

    ``workspaces`` are the bun workspaces restored beside the environment,
    and ``projects`` the declared sub-projects whose own environments are
    synced beside it, both relative to the worktree; ``no_sync`` skips all
    three, since they are the same act for each environment.

    Nothing here reaches origin. A branch that has no commits of its own can
    only publish a ref holding what origin already had, and rebuilding the
    branch on a different base then meets that ref as a non-fast-forward —
    an obstacle to the work, on a remote where it read as noise. The branch
    is given its remote by the first push that carries something, which is
    `git pr push` and which the pre-push guard judges.
    """
    refuse_redirected_pointers()
    registration_blocked = report_a_blocked_registration()
    reflog_blocked = report_a_blocked_reflog(diagnosed=registration_blocked)
    report_a_blocked_arming(guards)
    current_dir = Path.cwd()

    tree_dir = get_tree_dir()
    worktree_path = tree_dir / name
    resuming = worktree_path.exists() and worktree_is_registered(worktree_path)

    from lup.devtools.dev.branches import get_integration_branch

    # What the branch comes from and what is recorded of it, settled before
    # anything is created — both refusals here, because a base named after
    # the branch exists is a correction and not an answer. Neither question
    # answers on a detached HEAD that is re-attaching a branch: there is no
    # current branch to read, the record is skipped, and nothing says so —
    # which is why `sync base` reports "Base guessed" long afterwards, on a
    # topology that has since moved. A base nobody can name is refused here
    # instead, where the answer is a flag rather than archaeology.
    current = Repository(Path.cwd()).branch()
    integration = get_integration_branch()
    base = BranchBase(
        branch=name,
        named=base_branch,
        current=current,
        integration=integration,
        fresh=not branch_exists(name),
        ahead=commits_ahead(current, integration),
    )
    base.refuse_guessing()
    recorded = RecordedBase(branch=name, origin=base.recorded(), cut_fresh=base.fresh)
    if not recorded.origin and not no_record and not recorded.already_recorded():
        refuse(
            f"{current_dir} is not on a branch, so nothing would be recorded as"
            " its base and every later reader would have to guess it from"
            " topology",
            what=name,
            steps=[
                step(
                    "name the base",
                    devtools("git", "worktree", "create", name, "--base", "<branch>"),
                ),
                step(
                    "or create it with no base recorded",
                    devtools("git", "worktree", "create", name, "--no-record"),
                ),
            ],
        )

    if worktree_path.exists() and not resuming:
        if not force:
            refuse(
                "the directory exists but is not a registered worktree",
                what=str(worktree_path),
                steps=[
                    step(
                        "delete it and create the worktree",
                        devtools("git", "worktree", "create", name, "--force"),
                    )
                ],
            )
        typer.echo(f"Removing stale worktree directory: {worktree_path}")
        shutil.rmtree(worktree_path)

    if not resuming:
        register_worktree(name, worktree_path, base.cut_from())
    session = session_member_id()

    def setup() -> Iterator[SetupStep]:
        """Everything that has to hold before this worktree can be used."""
        if session:
            called = RepositoryPeers(current_dir).called(session)
            yield HeldForSession(worktree=worktree_path, session=session, called=called)
        yield MergeDriver(blocked=registration_blocked)
        if not reflog_blocked:
            yield LoggedRefUpdates()
        yield ArmedGitGuards(guards=guards, worktree=worktree_path)
        if not no_record:
            yield recorded
        if not no_copy_data:
            yield CopiedExtras(
                source=current_dir, worktree=worktree_path, extras=extras
            )
        if not no_sync:
            yield SyncedEnvironment(worktree=worktree_path)
            for project in projects:
                yield SyncedSubProject(worktree=worktree_path, project=project)
            for workspace in workspaces:
                yield RestoredWorkspace(worktree=worktree_path, workspace=workspace)

    pending = [setting for setting in setup() if not setting.satisfied()]

    if resuming and not pending:
        typer.echo(f"Worktree already active: {worktree_path}")
        raise typer.Exit(0)
    if resuming:
        typer.echo(f"Worktree exists, but its setup never finished: {worktree_path}")

    incomplete = list(finish(pending))
    unusable = [setting for setting in incomplete if setting.required()]

    typer.echo()
    typer.echo(f"Worktree path: {worktree_path}")

    for setting in incomplete:
        if not setting.required():
            typer.echo(
                f"Without {setting.label()}, which this worktree is usable without."
            )

    if unusable:
        typer.echo("This worktree is not ready — these steps did not complete:")
        for setting in unusable:
            typer.echo(f"  - {setting.label()}")
        refuse(
            "its setup did not finish",
            what=name,
            steps=[step("run the same command again to finish it")],
        )

    typer.echo("Creating a worktree does not move whoever ran this. To follow it:")
    hint = launcher(worktree_path)
    if hint.agent:
        typer.echo(f"  agent:  {hint.agent}")

    # Opt-in, because most runs of this command are an agent's. The clipboard
    # is the human's own channel and a side effect nobody asked for is still a
    # side effect: an agent that copied here would silently replace whatever
    # its operator had put there, for a line the operator never sees.
    copied = clipboard and copy_to_clipboard(hint.shell)
    typer.echo(
        f"  shell:  {hint.shell}" + ("   [copied to clipboard]" if copied else "")
    )


def worktree_status(path: str) -> str:
    """Check if a worktree has uncommitted changes."""
    try:
        dirty = git.lines("-C", path, "status", "--porcelain", _ok_code=[0])
        return "dirty" if dirty else "clean"
    except sh.ErrorReturnCode:
        return "?"


def list_worktrees() -> None:
    """List all git worktrees with branch and status info."""
    refuse_redirected_pointers()
    entries = Repository(Path.cwd()).worktrees()

    if not entries:
        typer.echo("No worktrees found")
        return

    cwd = Path.cwd().resolve()

    def row(entry: Worktree) -> list[str]:
        branch = entry.branch or ("(bare)" if entry.bare else "(detached)")
        marker = "* " if entry.path == cwd else "  "
        in_dir = not entry.bare and entry.path.is_dir()
        dirtiness = worktree_status(str(entry.path)) if in_dir else ""
        flag = " [prunable]" if entry.prunable else ""
        return [
            f"{marker}{branch}",
            short_sha(entry.head),
            dirtiness,
            f"{entry.path}{flag}",
        ]

    typer.echo(f"\n=== Worktrees ({len(entries)}) ===\n")
    typer.echo(
        format_table(("Branch", "HEAD", "Status", "Path"), [row(e) for e in entries])
    )


def live_worktree_owners(path: Path) -> list[str]:
    """Live sessions using a checkout, including one that is clean.

    Two ways to be one: launched in it, or holding it since creating it from
    somewhere else. The second is not asked of the session asking, whose own
    hold is on a checkout it is free to remove.
    """
    running = [member for member in RepositoryPeers(path).present() if member.running]
    held = hold_on(path)
    return list(
        dict.fromkeys(
            member.cli_name or member.actor.label()
            for member in running
            if (member.worktree and Path(member.worktree).resolve() == path.resolve())
            or (
                held is not None
                and member.actor.id == held.session
                and held.session != session_member_id()
            )
        )
    )


def drop_the_hold(path: Path) -> None:
    """Unlock a checkout whose lock is a session hold nobody else answers for.

    Called once :func:`refuse_live_worktree_removal` has passed, so a hold
    still standing here is spent or the remover's own, and git would
    otherwise refuse the removal over a lock nobody is answering for.
    """
    if hold_on(path) is not None:
        git("worktree", "unlock", str(path))


def refuse_live_worktree_removal(path: Path) -> None:
    """Recheck ownership immediately before the irreversible removal."""
    if owners := live_worktree_owners(path):
        refuse(
            f"live sessions use it: {', '.join(owners)}",
            what=str(path),
            steps=[
                step("wait for them to leave; --force does not override live ownership")
            ],
        )


def said_checkout_state_removed(path: Path) -> None:
    """Remove what lup kept outside a removed worktree for it, and say so.

    Its container environment, and the runtime homes lup's state kept for
    its host sessions. From a session inside a container the host's are not
    here to remove, so nothing is said; the next launch on the host sweeps
    every environment whose worktree is gone, and `harness clean` every home.
    """
    removed = remove_worktree_environment(path)
    if removed is not None:
        typer.echo(f"Removed its container environment: {removed}")
    homes = remove_checkout_homes(path.resolve())
    if homes is not None:
        typer.echo(f"Removed its runtime homes: {homes}")


def remove(name: str, force: bool) -> None:
    """Remove a git worktree.

    Nothing here writes git config: the removal rewrites the entry under the
    shared directory's ``worktrees/`` and touches ``config`` at no point. A
    config-lock diagnosis up front therefore refused a removal that would
    have worked wherever the shared config alone was held, which is the
    arrangement a session that cannot configure the host's git runs under.
    Where the shared directory as a whole is unwritable, git says so and the
    failure is attributed to the mount that caused it.
    """
    refuse_redirected_pointers()
    path = Path(name)

    if not path.is_absolute():
        tree_dir = get_tree_dir()
        path = tree_dir / name

    if not worktree_is_registered(path):
        refuse(
            "is not a registered worktree",
            what=str(path),
            steps=[step("see the ones there are", devtools("git", "worktree", "list"))],
        )

    try:
        refuse_live_worktree_removal(path)
        drop_the_hold(path)
        args = ["worktree", "remove", str(path)]
        if force:
            args.append("--force")
        git(*args)
        typer.echo(f"Removed worktree: {path}")
        said_checkout_state_removed(path)
    except sh.ErrorReturnCode as e:
        refuse(
            f"git could not remove it: {attributed_stderr(e)}",
            what=str(path),
            steps=(
                []
                if force
                else [
                    step(
                        "remove it even with changes in it",
                        devtools("git", "worktree", "remove", name, "--force"),
                    )
                ]
            ),
        )
