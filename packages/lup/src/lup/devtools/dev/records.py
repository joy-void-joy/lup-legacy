"""What lup knows about a branch, kept where only lup reads it.

A branch carries two facts nothing in git produces and nothing in git
consumes: what it was cut from, and where it stood before anybody worked on
it. Both were written into the repository's shared ``config`` as
``branch.<name>.lup-*`` keys — the one file in the shared git directory whose
contents name programs git runs on the host, through ``core.hooksPath``,
``alias.*``, ``credential.helper`` and ``merge.*.driver``. Keeping a private
record there is what makes that file have to be writable by whoever holds a
worktree, and the record gains nothing from sitting beside keys git reads.

So it sits in ``<common>/lup/`` instead, one JSON document per branch, beside
the edition record already there. Shared by every worktree of the repository
for the same reason the config was: a base recorded where one checkout was
cut has to answer from any of its siblings.

The keys that came before are still read, per field, wherever the document
does not carry one. A branch recorded only in ``config`` therefore answers
exactly as it did, from any worktree, with no write anywhere and no command
run first. :func:`adopt_legacy_records` is what finally empties ``config``,
and it is a once-per-clone move rather than something a read performs.
"""

import functools
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path, PurePosixPath

import sh
from pydantic import BaseModel

from lup.execution.git import Repository
from lup.channels.models import publish_atomic
from lup.execution.shell import git


class BranchRecord(BaseModel, frozen=True):
    """Everything lup remembers about one branch, all of it optional.

    Every field is blank by default because they are written at different
    moments by different commands — the base at creation, the reservation
    only when the branch was cut fresh, the upstream at the first push — and
    a reader asks for one without implying the others were ever settled.
    """

    base: str = ""
    """The branch this one was cut from, as it was named at creation."""

    base_commit: str = ""
    """Where this branch stood when nobody had worked on it yet."""

    upstream: str = ""
    """The remote-tracking ref this branch publishes to, once it has."""

    landed_in: str = ""
    """The branch this one's ref was contained in when it was deleted.

    The one fact about a branch that stops being observable at the instant it
    becomes interesting: containment is read off a ref, and the deletion takes
    the ref. Blank says two things a reader must not run together — nobody
    deleted this branch through lup, or lup deleted it while it held commits
    the integration branch lacked — and neither of them is a landing.
    """

    def merged(self, addition: "BranchRecord") -> "BranchRecord":
        """This record with every field the addition names, and the rest kept.

        Merging rather than replacing is what lets three commands own three
        facts about one branch without any of them reading the others back:
        recording an upstream at push time cannot lose the base recorded at
        creation, whichever order the two happened in.
        """
        return BranchRecord(
            base=addition.base or self.base,
            base_commit=addition.base_commit or self.base_commit,
            upstream=addition.upstream or self.upstream,
            landed_in=addition.landed_in or self.landed_in,
        )


class LegacyFact(StrEnum):
    """One fact a clone's ``config`` may carry, by the suffix it is written under.

    A closed set rather than two loose names, because the spelling is needed
    at both ends of one operation — the pattern that finds a key still in
    ``config``, and the strip that recovers the branch out of the key found —
    and a disagreement between the two adopts nothing while reporting that it
    finished.

    Each member also settles which field of a record its key carries, so that
    correspondence is stated once here instead of re-derived wherever a key is
    turned back into a record.
    """

    BASE = "lup-base"
    RESERVATION = "lup-base-commit"

    def key(self, branch: str) -> str:
        """The shared-config key this fact was written under, for one branch."""
        return f"branch.{branch}.{self}"

    def branch_of(self, key: str) -> str:
        """The branch a key belongs to, empty where it carries another fact.

        A branch name may hold dots, so the name is recovered by removing the
        two fixed ends rather than by cutting the key at one of them.
        """
        if not key.endswith(f".{self}"):
            return ""
        return key.removeprefix("branch.").removesuffix(f".{self}")

    def addition(self, value: str) -> BranchRecord:
        """The record this fact contributes, holding the value its key held."""
        match self:
            case LegacyFact.BASE:
                return BranchRecord(base=value)
            case LegacyFact.RESERVATION:
                return BranchRecord(base_commit=value)


def carrier_of(key: str) -> LegacyFact | None:
    """Which fact a legacy key carries, None where it carries neither.

    The reservation is tested first because the base's suffix is a prefix of
    it, so a key ending in the longer one ends in both.
    """
    return next((fact for fact in reversed(LegacyFact) if fact.branch_of(key)), None)


def at(cwd: Path | None) -> list[str]:
    """The words that point git at a checkout, none where the caller's own does.

    Every call here answers about one repository, and a caller either holds a
    path to it or is standing in it. Naming the difference once keeps the two
    spellings from being decided again at each call site.
    """
    return ["-C", str(cwd)] if cwd is not None else []


@functools.cache
def shared_directory_of(root: Path) -> Path:
    """The git directory every worktree of *root*'s repository shares.

    Asked of git rather than reconstructed from the checkout, because a
    linked worktree's ``.git`` is a file, a bare clone has none at all, and
    the caller may be standing anywhere beneath either.

    Cached on the path asked about, which is what keeps a survey from paying
    for a subprocess per branch: a checkout's common directory is fixed for
    as long as the checkout is, and the key is a resolved path rather than
    "wherever this process is", so a caller that moves is a different ask.
    """
    return Repository(root).common_dir()


def shared_directory(cwd: Path | None = None) -> Path:
    """The shared git directory of a named checkout, or of this process's own."""
    return shared_directory_of(cwd if cwd is not None else Path.cwd())


def reflog_off(cwd: Path | None = None) -> bool:
    """Whether git would log no ref updates against this repository's git directory.

    `core.logAllRefUpdates` defaults to false where there is no working tree,
    so a bare clone logs nothing for a branch cut by running git against the
    git directory itself — and the first reflog entry is where git records
    what a branch was cut from. Unset at every scope is the only case
    answered yes: an explicit value anywhere, off included, is somebody's
    choice and stays theirs.
    """
    shared = ["-C", str(shared_directory(cwd)), "config", "--get"]
    bare = git.out(*shared, "--bool", "core.bare", _ok_code=[0, 1]) == "true"
    return bare and not git.out(*shared, "core.logAllRefUpdates", _ok_code=[0, 1])


def log_ref_updates(cwd: Path | None = None) -> bool:
    """Turn git's reflog on for a bare clone left at the default, saying if it did.

    Written to the shared ``config`` because that is the only place git reads
    it for the git directory itself, and only where :func:`reflog_off` says
    so — so it is idempotent, and never overrides a value somebody set.
    """
    if not reflog_off(cwd):
        return False
    git("-C", str(shared_directory(cwd)), "config", "core.logAllRefUpdates", "true")
    return True


def record_location(branch: str) -> PurePosixPath:
    """Where one branch's record sits inside the shared git directory.

    The branch name is used as a relative path rather than flattened into a
    filename, so ``feat/x`` nests where git already nests it and no two
    branch names can collide on one document.

    Sayable without asking git anything, which is what lets a diagnostic name
    the file a reader should write without running a subprocess to do it.
    """
    return PurePosixPath("lup") / "branches" / f"{branch}.json"


def record_path(branch: str, cwd: Path | None = None) -> Path:
    """Where one branch's record lives, from any worktree of its repository."""
    return shared_directory(cwd) / record_location(branch)


def read_record(branch: str, cwd: Path | None = None) -> BranchRecord:
    """This branch's record, empty where nobody has written one.

    A document that cannot be read as a record is the same answer as no
    document. Every field has a fallback and a blank one is an ordinary
    state, so refusing to answer would cost a caller a working command over
    a fact it was already prepared not to have.
    """
    path = record_path(branch, cwd)
    if not path.is_file():
        return BranchRecord()
    try:
        return BranchRecord.model_validate_json(path.read_text("utf-8"))
    except ValueError:
        return BranchRecord()


def remember(branch: str, addition: BranchRecord, cwd: Path | None = None) -> None:
    """Fold what is known now into this branch's record, keeping the rest."""
    publish_atomic(record_path(branch, cwd), read_record(branch, cwd).merged(addition))


def record_landing(branch: str, integration: str, cwd: Path | None = None) -> None:
    """Settle where this branch stood as its ref went, the empty answer included.

    The one write here that replaces rather than folds in. Every other field
    is a fact somebody established once, so a blank addition means "not mine
    to say"; this one is a verdict a deletion reaches in full, and the verdict
    "it held work the integration branch lacked" is spelled blank. Folding
    that in would leave an earlier branch of the same name still claiming a
    landing that belonged to the branch before it.
    """
    landing = read_record(branch, cwd).model_copy(update={"landed_in": integration})
    publish_atomic(record_path(branch, cwd), landing)


def legacy_value(branch: str, fact: LegacyFact, cwd: Path | None = None) -> str:
    """What ``config`` still holds for one fact, empty where it holds nothing.

    A read, never a write: this is what lets a clone whose records were never
    adopted behave exactly as it did, including one whose shared directory is
    mounted read-only.
    """
    try:
        return git.out(*at(cwd), "config", "--get", fact.key(branch), _ok_code=[0])
    except sh.ErrorReturnCode:
        return ""


def recorded_base(branch: str, cwd: Path | None = None) -> str:
    """The base recorded for this branch, from either place, or empty."""
    return read_record(branch, cwd).base or legacy_value(branch, LegacyFact.BASE, cwd)


def recorded_reservation(branch: str, cwd: Path | None = None) -> str:
    """The commit this branch was reserved at, from either place, or empty."""
    held = read_record(branch, cwd).base_commit
    return held or legacy_value(branch, LegacyFact.RESERVATION, cwd)


def recorded_upstream(branch: str, cwd: Path | None = None) -> str:
    """The remote-tracking ref this branch was last published to, or empty.

    No fallback, because no legacy key carries it: a branch with nothing
    recorded says nothing here, and its caller falls back to git's own
    tracking configuration.
    """
    return read_record(branch, cwd).upstream


def legacy_keys(cwd: Path | None = None) -> list[str]:
    """Every ``branch.*.lup-*`` key the shared config still carries.

    Names only, so nothing has to be parsed back out of a key-and-value line:
    each value is asked for separately, by the key git has just named.

    The shared file alone, because that is the one this exists to empty. A
    key somebody put in their own global configuration is theirs, still
    answers every read, and is nobody else's to unset.
    """
    spellings = "|".join(LegacyFact)
    asked = ["config", "--local", "--name-only", "--get-regexp"]
    try:
        found = git.lines(*at(cwd), *asked, rf"^branch\..*\.({spellings})$")
        return [line for line in found if line]
    except sh.ErrorReturnCode:
        return []


def branches_awaiting_adoption(cwd: Path | None = None) -> list[str]:
    """Every branch whose facts the shared config still carries, named once.

    The measurement behind saying the move is unfinished. A clone answers
    every read either way, so without asking this nothing would ever notice
    that half the bookkeeping still sits in the file it is meant to leave.

    Named rather than counted, because a caller wanting the number takes the
    length and one wanting the names cannot recover them from a number. One
    branch writes two keys, and one branch left behind is one thing to report
    rather than two, so the names are deduplicated.
    """

    def named() -> Iterator[str]:
        for key in legacy_keys(cwd):
            fact = carrier_of(key)
            if fact is not None:
                yield fact.branch_of(key)

    return list(dict.fromkeys(named()))


def adopt_legacy_records(cwd: Path | None = None) -> Iterator[str]:
    """Move every ``branch.*.lup-*`` key into ``<common>/lup/``, naming each.

    The half of the move a read cannot perform. Reads fall back to these keys
    indefinitely, so nothing depends on this having run — what it buys is a
    shared ``config`` holding nothing lup wrote, which is the only way that
    file stops needing to be writable by a worker.

    The document lands first and the key is dropped after it, so an
    interruption leaves a fact recorded twice rather than not at all, and a
    later run finds what is left and finishes it. A run with nothing to move
    yields nothing, which is what makes running it again both safe and cheap
    to describe.
    """
    for key in legacy_keys(cwd):
        fact = carrier_of(key)
        if fact is None:
            continue
        held = git.out(*at(cwd), "config", "--local", "--get", key)
        remember(fact.branch_of(key), fact.addition(held), cwd)
        git(*at(cwd), "config", "--local", "--unset", key)
        yield f"Adopted {key} into {record_path(fact.branch_of(key), cwd)}"
