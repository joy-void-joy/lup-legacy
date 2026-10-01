"""Whether git can still take the lock its config writes need.

A confinement owning the path and a lock some git left behind when it died both
surface as `File exists`, and the remedies are opposite, so telling them apart
reads the mount state and the lock's age rather than the message. The same two-
consumers shape as `codeintel`: the resolver's orchestrator and
`devtools/utils` both diagnose it, and neither owns it.

Every config write goes through `config.lock`: git creates it exclusively,
writes the new config beside it, and renames over the old one. Two unrelated
failures stop that create, and git reports both as `File exists`:

- a confinement owns the path — a sandbox that bind-mounts `/dev/null` over
  the lock, or mounts the config read-only — where nothing can be deleted and
  the run has to move outside it;
- a lock a git left behind when it died mid-write, where deleting it is the
  entire fix.

The words are identical and the remedies are opposite, so what separates them
is the mount state and the lock's own age: observable here, and nowhere in
git's message. Reading only the first makes the second unsayable, which is
worse than saying nothing, because the first failure *manufactures* the
second — a sandboxed git that dies mid-write leaves a real lock on the host
filesystem for the next unconfined run to trip over.
"""

import os
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel


def on_read_only_mount(path: Path) -> bool:
    """Whether the mount holding a path is itself mounted read-only."""
    return path.exists() and bool(os.statvfs(path).f_flag & os.ST_RDONLY)


def refuses_a_new_file(directory: Path, probe_prefix: str = ".lup-lock-probe") -> bool:
    """Whether a directory refuses the lock file a config write has to create.

    Creating is the permission the lock protocol needs, and creating is the
    directory's permission rather than the config's: git writes `config.lock`
    beside `config` and renames over it, so a `config` nobody may write is
    still rewritten inside a directory they may. Asking the mode bits of the
    config instead would call a plain `chmod 444` a sandbox.

    Asked by attempting that create rather than by reading permission bits,
    because the two answers disagree exactly where it matters. `os.access`
    and the mount flags answer the kernel's own permission question, and a
    confinement that mediates the syscall above the kernel can leave both
    saying yes while still refusing `O_CREAT | O_EXCL` — a sandbox where a
    plain `touch` succeeds and git's lock create does not. Read that way the
    directory looks unconfined, so the lock beside it is diagnosed as debris
    somebody should wait out, and the reader is handed a remedy that cannot
    work. Attempting the create asks the only question with an answer: not
    who may write here, but whether this write happens.

    The attempt is the whole probe, since a read-only mount refuses it for
    the same reason under a different name.

    ``probe_prefix`` is never `config.lock`: a probe wearing the lock's name
    would be taking the lock it is trying to diagnose, and would race the git
    that may still hold it. The pid distinguishes two runs probing at once,
    and the name is the caller's to change where something else in an admin
    directory already claims it.
    """
    if not directory.exists():
        return False
    probe = directory / f"{probe_prefix}.{os.getpid()}"
    try:
        handle = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return True
    try:
        os.close(handle)
    finally:
        probe.unlink(missing_ok=True)
    return False


def render_age(age: timedelta) -> str:
    """An age in its largest whole unit, for a line a human reads once."""
    seconds = int(age.total_seconds())
    if seconds < 60:
        return f"{seconds} seconds"
    if seconds < 3600:
        return f"{seconds // 60} minutes"
    if seconds < 86400:
        return f"{seconds // 3600} hours"
    return f"{seconds // 86400} days"


class LockObstruction(BaseModel, ABC, frozen=True):
    """One reason a config write cannot take its lock, and what clears it.

    Whatever a caller needs to know about an obstruction is declared here and
    answered — or declined — by the obstruction itself, so a new confinement
    shape is one class rather than another arm in a message nobody re-reads.
    The declining answers are what make omission safe: a caller that clears
    what it can reaches every removable obstruction, including kinds written
    long after the caller was, and removes nothing else.

    Pydantic's metaclass is an ``ABCMeta``, so a kind that does not say what
    it saw and what to do about it cannot be built.
    """

    path: Path

    @abstractmethod
    def evidence(self) -> str:
        """What was observed here, in the terms git's message could not use."""

    @abstractmethod
    def remedy(self) -> str:
        """What the reader should do, stated so the other failure is excluded."""

    @property
    def confining(self) -> bool:
        """Whether this also stops anything beside it from being removed.

        A mount is not a caller's to undo, so a confinement's remedy wins
        over any advice to delete something: inside one, deleting is exactly
        what cannot work.
        """
        return False

    def clear(self) -> str | None:
        """Remove what holds the lock where that is the fix, saying what was done.

        Declined by default, which is what keeps an automatic clear honest:
        a confinement's mount and a lock a live git still holds both stay
        where they are, and only a kind that has established its own
        removability answers.
        """
        return None


class Confinement(LockObstruction, frozen=True):
    """An obstruction owned by the sandbox rather than by git.

    The three shapes differ only in what was observed — the remedy is one
    remedy, because no config write in this directory can succeed until the
    run is outside the confinement. Outside the confinement is not the same
    as outside the sandbox, and the remedy says both: a per-call escape
    re-judges a call without moving it to another filesystem, so against a
    mount the session placed it reports the identical refusal, and a remedy
    naming only the escape sends the reader around that loop again.
    """

    @property
    def confining(self) -> bool:
        return True

    def remedy(self) -> str:
        return (
            "git config writes are blocked by the sandbox: `File exists` names "
            "that mount, not a stale lock, and there is none here to delete. "
            "Rerun outside the sandbox — and if a per-call escape reports this "
            "same refusal, the confinement is above the sandbox rather than "
            "inside it: an escape changes which policy judges a call and never "
            "which filesystem it runs on, so what is left is a session whose "
            "mounts make this directory writable."
        )


class ReadOnlyConfig(Confinement, frozen=True):
    """A config on a mount that forbids the rename every write ends with."""

    def evidence(self) -> str:
        return f"`{self.path.name}` is on a read-only mount"


class UnwritableAdmin(Confinement, frozen=True):
    """An admin directory that refuses the lock file a config write creates."""

    def evidence(self) -> str:
        return f"`{self.path.name}/` refuses a new `config.lock`"


class ShadowedLock(Confinement, frozen=True):
    """A device node where git expects a regular file of its own."""

    def evidence(self) -> str:
        return f"`{self.path.name}` is a device node"


class AbandonedLock(LockObstruction, frozen=True):
    """A lock file a git left behind when it died before its rename.

    Age stands in for the holding process, which is not observable
    portably and which git records nowhere: a config write holds this file
    for the length of one rename, so a lock that has outlived any plausible
    write is one nobody is holding. The threshold is the caller's to set,
    and the age is reported rather than hidden behind the verdict it fed.
    """

    age: timedelta

    def evidence(self) -> str:
        return (
            f"`{self.path.name}` is a regular file {render_age(self.age)} old, "
            "and nothing here refuses a write"
        )

    def remedy(self) -> str:
        return (
            "Nothing here is confined: this is a lock an interrupted git left "
            "behind, and removing it is the whole fix."
        )

    def clear(self) -> str:
        self.path.unlink(missing_ok=True)
        return f"Removed `{self.path}`, a stale git lock {render_age(self.age)} old"


class ActiveLock(LockObstruction, frozen=True):
    """A lock young enough that a git may still be holding it."""

    age: timedelta

    def evidence(self) -> str:
        return f"`{self.path.name}` is a regular file only {render_age(self.age)} old"

    def remedy(self) -> str:
        return (
            "Another git may still be holding it: wait for that write to finish "
            "and retry, rather than removing the lock underneath it."
        )


def held_locks(
    git_dir: Path,
    lock_names: Sequence[str],
    stale_after: timedelta,
) -> Iterator[LockObstruction]:
    """Every lock file present where git's protocol expects none to remain."""
    for name in lock_names:
        path = git_dir / name
        if not path.is_file():
            continue
        age = datetime.now(UTC) - datetime.fromtimestamp(path.stat().st_mtime, UTC)
        if age >= stale_after:
            yield AbandonedLock(path=path, age=age)
        else:
            yield ActiveLock(path=path, age=age)


def inspect_git_admin(
    git_dir: Path,
    shadowed_names: Sequence[str] = ("config.lock", "config.worktree"),
    lock_names: Sequence[str] = ("config.lock",),
    stale_after: timedelta = timedelta(seconds=60),
) -> list[LockObstruction]:
    """Read one git admin directory's lock protocol off the filesystem.

    A confinement is answered alone: inside one, the lock file beside the
    config may well look abandoned — a sandboxed git that died is what left
    it — and saying so would prescribe a removal the mount refuses.

    `shadowed_names` are the paths a config write has to find as files of its
    own, and therefore the ones a confinement shadows to stop it; a
    confinement that shadows others names them. `lock_names` are the subset
    git creates and deletes around a single write, and therefore the only
    ones anything may remove: `config.worktree` is configuration a repository
    keeps, not debris.
    """

    def confinements() -> Iterator[LockObstruction]:
        if on_read_only_mount(git_dir / "config"):
            yield ReadOnlyConfig(path=git_dir / "config")
        if refuses_a_new_file(git_dir):
            yield UnwritableAdmin(path=git_dir)
        for name in shadowed_names:
            if (git_dir / name).is_char_device() or (git_dir / name).is_block_device():
                yield ShadowedLock(path=git_dir / name)

    confined = list(confinements())
    if confined:
        return confined
    return list(held_locks(git_dir, lock_names, stale_after))


def obstructions_across(git_dirs: Sequence[Path]) -> list[LockObstruction]:
    """Everything holding the lock across a checkout's admin directories.

    A linked worktree has two, and both have to be read: `config` and its
    lock live in the common directory every worktree shares, while
    `config.worktree` lands in the worktree's own once a repository turns
    per-worktree configuration on.
    """
    return [
        obstruction
        for git_dir in git_dirs
        for obstruction in inspect_git_admin(git_dir)
    ]


def diagnosis(obstructions: Sequence[LockObstruction]) -> str:
    """What is holding the lock and what to do, empty where nothing is.

    Every observation is named because each is a thing git could not have
    reported, while the remedy is one remedy — the confining one wherever a
    confinement is present, since a removal it forbids is advice that cannot
    be taken.
    """
    if not obstructions:
        return ""
    dominant = next((o for o in obstructions if o.confining), obstructions[0])
    evidence = ", ".join(obstruction.evidence() for obstruction in obstructions)
    return (
        f"git cannot take the lock its config writes need ({evidence}). "
        f"{dominant.remedy()}"
    )


def diagnose_git_admin(git_dirs: Sequence[Path]) -> str:
    """The diagnosis a checkout's admin directories give, empty where none do."""
    return diagnosis(obstructions_across(git_dirs))
