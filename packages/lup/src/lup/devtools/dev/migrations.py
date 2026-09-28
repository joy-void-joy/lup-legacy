"""What an adopter has to do about a break, where two trees cannot say it.

Most of what a range asks of a project built on this one is derivable, and is
derived: a module that moved and a name that was renamed are fully determined
by the surface at each end, which git holds for every revision, so
:mod:`lup.devtools.dev.preservation` reads both and the map costs nothing to
keep. What is left over is the residue — a signature that gained required
parameters, a refusal that split in two, a default whose meaning changed —
where nothing in either tree says what a caller should now pass. That is what
is declared, and only that.

Each break is one TOML file under the library's ``migrations/pending/``,
because a declaration is added by the commit that breaks something, and any
number of those are in flight at once on branches of their own: files beside
each other merge, where entries appended to one list meet at one position and
conflict every time two branches both broke something. A release moves the
pending files into ``migrations/<version>/`` and renders their prose into its
changelog section, and the files stay. An update crossing several releases
reads each one's declarations as data, and a gate judging a range that spans a
release finds what that release declared.

A migration is keyed to the commit it applies after, because commits are what
this library ships at: a project pinned to a branch is between releases by
definition, and a record that only existed at release time would be silent
exactly when such a project is updating. A pending file names no commit — the
commit adding it cannot name itself — and the release fills in the one that
did, read off the history.

The gate is in this repository rather than downstream. A capability that
disappears between the merge base and the working tree, with no migration
naming it, fails the gate that would have shipped it — so a break cannot land
without its instruction, and an adopter reads a complete record rather than a
diligent one.
"""

import tomllib
from pathlib import Path

import sh
import tomlkit
from pydantic import BaseModel, ValidationError
from semver import Version

from lup.devtools.dev.branches import detect_base_branch
from lup.devtools.dev.preservation import Capability, Span
from lup.devtools.dev.release import RELEASE_SUBJECT_PREFIX
from lup.devtools.project import DevProject
from lup.execution.shell import git
from lup.workspace.history import parse_semver

LIBRARY_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
"""Where this library keeps its own record, inside the package so it installs.

A project updating reads the record of the library it just installed, from
wherever that install put it, so the record ships as package data rather
than living beside the source in a directory no wheel carries.
"""


class UnreadableMigration(ValueError):
    """A declaration that does not parse or validate, named by its file."""


class MigrationStep(BaseModel, frozen=True, extra="forbid"):
    """One thing to do about a break, in the form it can be done in."""

    instruction: str
    """What to do, as a sentence somebody can act on."""

    command: list[str] = []
    """The command that does it, where a command does.

    Empty for the ordinary case, which is the whole reason this is declared
    rather than derived: what a caller should pass to a signature that grew is
    a decision about their code, and a command that guessed at it would be
    worse than a sentence that asks.
    """

    def spelled(self) -> str:
        """This step as an update reports it, with its command where it has one."""
        return self.instruction + (
            f"\n    {' '.join(self.command)}" if self.command else ""
        )


class Migration(BaseModel, frozen=True, extra="forbid"):
    """One break, the commit that made it, and what a caller does about it.

    Read from a file an author wrote by hand, so a key it does not know is
    refused rather than dropped: a misspelled ``command`` read as no command
    would ship a step with its one runnable part silently gone.
    """

    subjects: list[str]
    """Every capability this break took, spelled as the surface walk spells it.

    Plural because one decision takes several names at once — a mode retired
    takes its enum member, its reader and its two commands — and splitting
    that into an entry per name would ask a reader to reassemble one change
    out of four records saying the same sentence.
    """

    reason: str
    """Why the break was taken. This is what reaches the changelog at release."""

    steps: list[MigrationStep]

    commit: str = ""
    """The commit the break landed in, once it has one.

    Left out of a pending file, whose commit is the one adding it and cannot
    name itself, and read as *newer than every revision*, which is what it
    is: a project taking this library takes the break with it, whatever
    commit it stood at before. The release fills it in from the commit that
    added the file, which buys one thing — telling a project that already has
    it so.
    """

    @classmethod
    def read(cls, path: Path) -> "Migration":
        """The migration one file declares, refused with its path where it is not one."""
        try:
            return cls.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
        except (tomllib.TOMLDecodeError, ValidationError) as error:
            raise UnreadableMigration(f"{path}: {error}") from error

    def covers(self, capability: Capability) -> bool:
        """Whether this migration is the one that speaks for that capability.

        Matched on the name rather than on the module it was declared in: a
        capability that disappeared has no module any more, and the name is
        what a caller's code holds.
        """
        return capability.identity in self.subjects

    def applied_at(self, revision: str, root: Path = Path()) -> bool:
        """Whether a project standing at ``revision`` already has this break.

        Ancestry rather than ordering: a project pinned to a branch that never
        carried the commit has not applied it, whatever the dates say. A
        declaration naming no commit is pending for every revision, which is
        the honest answer while the break is newer than any commit that could
        be named.

        Asked of whichever checkout holds both commits. In this repository
        that is this one; in a project built on it, it is the clone of upstream
        the update already fetched, since neither commit is in the project's
        own history at all.
        """
        if not self.commit:
            return False
        try:
            git("-C", str(root), "merge-base", "--is-ancestor", self.commit, revision)
        except sh.ErrorReturnCode:
            return False
        return True

    def landed_between(self, base: str, head: str = "", root: Path = Path()) -> bool:
        """Whether this break landed after ``base`` and by ``head``.

        What lets it speak for a name that went over that range. A pending
        declaration names no commit and lands in every range it is read in,
        since it is newer than any commit. A stamped one lands only where its
        commit is not yet in ``base``'s history and is in ``head``'s — so one a
        base already carried speaks for nothing that base still had, and one
        from a history this checkout does not hold speaks for nothing here.

        An empty ``head`` is the working tree, whose history is every commit
        this checkout holds rather than HEAD's alone: while a merge runs, the
        tree carries the merged branch's record, stamped with commits only the
        merge head has.
        """
        if not self.commit:
            return True
        if self.applied_at(base, root):
            return False
        if head:
            return self.applied_at(head, root)
        try:
            git("-C", str(root), "cat-file", "-e", f"{self.commit}^{{commit}}")
        except sh.ErrorReturnCode:
            return False
        return True

    def spelled(self) -> list[str]:
        """This migration as an update reports it: the reason, then the steps."""
        return [
            f"{', '.join(self.subjects)} — {self.reason}",
            *(f"  {step.spelled()}" for step in self.steps),
        ]


class MigrationRecord(BaseModel, frozen=True):
    """Every migration a library declares: the pending window, and each release's.

    A directory rather than a list in a module, and read rather than imported:
    a file per break is what lets two branches declare one each without
    meeting, and a directory per release is what keeps a shipped break
    readable as data after the release that carried it.
    """

    root: Path = LIBRARY_MIGRATIONS
    """The directory holding ``pending/`` and one directory per release."""

    def pending_directory(self) -> Path:
        """Where a break is declared, by the commit that makes it."""
        return self.root / "pending"

    def releases(self) -> list[str]:
        """Every release that carried migrations, oldest first.

        Ordered as versions rather than as names, so ``0.10.0`` follows
        ``0.9.0``. A directory that is neither ``pending/`` nor a version is
        refused rather than skipped: whatever was put there was meant to be
        read, and a reader that passed over it would lose it silently.
        """
        if not self.root.is_dir():
            return []

        def version_of(name: str) -> Version:
            version = parse_semver(name)
            if version is None:
                raise UnreadableMigration(
                    f"{self.root / name} is neither "
                    f"{self.pending_directory().name}/ nor a release's version"
                )
            return version

        return sorted(
            (
                entry.name
                for entry in self.root.iterdir()
                if entry.is_dir() and entry != self.pending_directory()
            ),
            key=version_of,
        )

    def files(self, directory: Path) -> list[Path]:
        """The declarations one directory holds, in the order they are read."""
        return sorted(directory.glob("*.toml")) if directory.is_dir() else []

    def pending(self) -> list[Migration]:
        """What the next release will carry."""
        return [Migration.read(path) for path in self.files(self.pending_directory())]

    def released(self, version: str) -> list[Migration]:
        """What one release carried."""
        return [Migration.read(path) for path in self.files(self.root / version)]

    def declared(self) -> list[Migration]:
        """Every migration in the record: each release's, oldest first, then pending.

        The order an update crossing all of them applies them in.
        """
        return [
            *(
                migration
                for version in self.releases()
                for migration in self.released(version)
            ),
            *self.pending(),
        ]

    def release(self, version: str, repository: Path) -> list[Migration]:
        """Move every pending declaration into ``version``, stamped with its commit.

        The commit is the one that added the file, read off ``repository``'s
        history — the commit the break landed in, since the gate refused the
        break until the declaration came with it. A declaration already
        naming one keeps it. The file is rewritten through ``tomlkit``, so
        what its author wrote reads the same after the release as before.
        """
        destination = self.root / version
        destination.mkdir(parents=True, exist_ok=True)
        for path in self.files(self.pending_directory()):
            document = tomlkit.parse(path.read_text(encoding="utf-8"))
            if not Migration.read(path).commit:
                document["commit"] = git.out(
                    "-C",
                    str(repository),
                    "log",
                    "--no-renames",
                    "--diff-filter=A",
                    "--format=%H",
                    "--max-count=1",
                    "--",
                    str(path.resolve()),
                ).strip()
            (destination / path.name).write_text(
                tomlkit.dumps(document), encoding="utf-8"
            )
            path.unlink()
        return self.released(version)

    def instruction(self, root: Path) -> str:
        """What a refusal tells somebody to do about a break nothing speaks for."""
        pending = self.pending_directory()
        shown = pending.relative_to(root) if pending.is_relative_to(root) else pending
        return (
            f"declare each in a file under `{shown}/`, with what a caller does "
            "about it (docs/contributing.md shows one)"
        )


class RenderedMigrations(BaseModel, frozen=True):
    """Installed-library output consumed by a process started before its update."""

    count: int
    lines: list[str]


def unapplied(
    declared: list[Migration], revision: str, root: Path = Path()
) -> list[Migration]:
    """The declared migrations a project standing at ``revision`` still owes."""
    return [
        migration for migration in declared if not migration.applied_at(revision, root)
    ]


def unnamed(
    disappeared: list[Capability], declared: list[Migration]
) -> list[Capability]:
    """Every capability that went with no migration speaking for it.

    The gate's whole question. A capability that moved is not here — the map
    is derived and needs nobody to write it down — and one that went on
    purpose is not here either, as long as the commit that took it said so.
    """
    return [
        capability
        for capability in disappeared
        if not any(migration.covers(capability) for migration in declared)
    ]


def unnamed_between(
    disappeared: list[Capability],
    declared: list[Migration],
    base: str,
    head: str = "",
    root: Path = Path(),
) -> list[Capability]:
    """:func:`unnamed`, heard only from migrations that landed over the range.

    Released records are read with the rest, so a range spanning a release
    hears what it declared. Their names are common words, though, and a
    migration speaks only where it landed: one the base already carried
    spoke for a name gone before the range began, and one from another
    history — the library's record, read in a project built on it — spoke
    for a name of that history's. Either way a name gone here is a break of
    its own. ``head`` is empty for the working tree, as a span spells it.

    Ancestry is asked only of the migrations naming something that went,
    which is the few rather than the record.
    """
    return unnamed(
        disappeared,
        [
            migration
            for migration in declared
            if any(migration.covers(capability) for capability in disappeared)
            and migration.landed_between(base, head, root)
        ],
    )


def last_release_commit() -> str:
    """The newest release commit this checkout can reach, or empty where none can.

    What a release shipped was judged when it shipped, so what is owed
    afterwards is what the checkout has taken *since that commit*: the window
    a pending declaration speaks for.

    The commit rather than the tag, though the tag is the more canonical mark
    of a release: the tag is pushed last on purpose, so a branch reaches CI
    and a reviewer carrying the release commit and no tag at all, and a gate
    reading the tag is blind for exactly the window a release is under review.
    The subject is :data:`RELEASE_SUBJECT_PREFIX`, which `dev release` writes
    and this reads.
    """
    return git.out(
        "log",
        f"--grep=^{RELEASE_SUBJECT_PREFIX}",
        "--extended-regexp",
        "--format=%H",
        "--max-count=1",
        "HEAD",
        _ok_code=[0, 1, 128],
    ).strip()


def gate_base(integration: str, release: str = "main") -> str | None:
    """The commit this checkout's breaks are judged from, or ``None`` with none to read.

    A feature branch is judged from where it started, which creation recorded
    and topology can otherwise guess among the local branches.

    The integration branch is judged from its last release commit: what a
    release shipped was judged when it shipped, so the integration branch
    answers for what it has taken since. Read from the release branch
    instead, the range reaches back across every release that branch has not
    merged yet, judging again what was settled when it shipped.

    The branch answers where no release commit does: a repository before its
    first release, or one whose history a shallow clone truncated. Where no
    local branch stands beside the current one either -- a CI clone holds the
    branch it checks out and nothing else, and a pull request's checkout stands
    on no branch at all -- the remote's copy is read, which a full fetch
    carries.

    No base at all is a reading, not a refusal. The base detector exits the
    process where it finds no other local branch, and for every push after
    this gate was written that exit took the whole report with it: the log
    held one line and an exit code, and named no check.
    """
    current = git.out("branch", "--show-current").strip()
    siblings = [
        branch
        for branch in git.lines("branch", "--format=%(refname:short)")
        if branch != current
    ]
    if current and current != integration and siblings:
        return detect_base_branch(current).merge_base
    # Read off HEAD's history rather than off which branch is checked out,
    # because a pull request's checkout stands on no branch at all: gated on
    # standing *on* the integration branch, this answered for the push build
    # and not for the request build beside it, and one of the two reported
    # every break the release had shipped.
    if cut := last_release_commit():
        return cut
    named = release if current == integration else integration
    for ref in (named, f"origin/{named}"):
        found = git.out("merge-base", ref, "HEAD", _ok_code=[0, 1, 128]).strip()
        if found:
            return found
    return None


def undeclared_breaks(
    project: DevProject, base: str, record: MigrationRecord = MigrationRecord()
) -> list[Capability]:
    """Every capability this checkout took since ``base`` that nothing speaks for.

    ``base`` is what :func:`gate_base` answered: what this change took away is
    judged against where it started, and creation recorded that where
    topology can no longer recover it.
    """
    divergence = Span(base=base).divergence(project)
    return unnamed_between(divergence.disappeared, record.declared(), base)


def rendered(declared: list[Migration]) -> list[str]:
    """What a release's changelog section says about the breaks in it.

    The prose half only: a reason and the steps a reader has to act on. The
    derived half is left out on purpose — a relocation map is re-derivable
    from any two revisions, and a hundred pairs pasted into a changelog go
    stale the next time one module moves.
    """
    return [line for migration in declared for line in migration.spelled()]
