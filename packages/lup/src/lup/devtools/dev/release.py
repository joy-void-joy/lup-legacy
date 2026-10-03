"""Cutting a release: closing the changelog, moving the version, tagging it.

A release here is one transaction over the files it touches, and the argument
for compiling it rather than writing it down is what prose costs. Carried in a
skill — fold the pending migrations into the changelog, close the section,
move the version, move the migrations into the release — the steps make a list
that runs only as well as whoever is reading it that day, and a step skipped
leaves nothing behind to say it was.

What stays a judgement stays outside: which level the release is, and what the
entries under ``## Unreleased`` should say. Both are decided by somebody
reading the range, and neither is derivable. Everything downstream of them is
arithmetic on files, which is what this is.

The pending migrations move into the release's own directory because they
have shipped. `rendered` folds their prose into the section being closed, so
the instruction an adopter reads is in the release that carries the break,
and the files stay as that release's record, stamped with the commit each
break landed in, so an update crossing several releases reads every one of
them as data; :mod:`lup.devtools.dev.migrations` opens by saying so.

A release can go out first as candidates — ``vX.Y.ZrcN``, pre-releases in
PEP 440's own spelling, which an installer passes over unless asked for one or
unless nothing else is published — and then be promoted. The level is settled
once, by the first candidate, and each later one counts on from the tags
already spent on its version, since an index accepts a version once.

A candidate's commit carries its own version, so what a tag publishes is the
tree it names, built as it stands, and a project pinned to a candidate is
told it holds one. Promotion is then one commit on a branch cut from the
last candidate's tag, changing nothing but the version, the changelog
section it closes and the record of the breaks it carried — checked against
the candidate's tag before anything is committed, file by file, so what
ships is what was tested. It lands on the release branch the way any
release does, and is merged back into the integration branch, which goes on
moving the whole time a candidate soaks: nothing it landed since is in the
release, and nothing it landed is lost. A promotion is taken only while the
release branch holds exactly what the candidate shipped; where it moved,
whether to cut another candidate or to release what the integration branch
holds now is somebody's decision, and the command names both rather than
taking one.

Which files a release touches is declared, not assumed. This repository
publishes one distribution out of ``packages/lup`` and keeps two other version
numbers that a release must not touch — the scaffold's own, and the agent
version that names a trace directory — so a command that went looking for "the
version" would have found three and been wrong about two.
"""

import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeGuard, get_args

import tomlkit
import tomlkit.items
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel
from tomlkit import TOMLDocument

from lup.execution.git import Repository
from lup.devtools.changelog import Candidate, Changelog
from lup.devtools.utils import short_sha
from lup.execution.shell import git
from lup.formats.toml import edited_manifest
from lup.workspace.history import parse_semver
from lup.workspace.paths import manifest_table

if TYPE_CHECKING:
    # Only for the annotation: the record's module reads the release commit
    # this one writes, so it imports this one, and a runtime import back would
    # close the loop.
    from lup.devtools.dev.migrations import MigrationRecord

type ReleaseLevel = Literal["patch", "minor", "major"]
"""Which part of the version a release moves.

Spelled as the three it can be, so a caller holding one has been checked
rather than trusted; what arrives from a command line is a string until
:func:`is_level` says otherwise.
"""


def is_level(word: str) -> TypeGuard[ReleaseLevel]:
    """Whether that word is a level, narrowing it where it is."""
    return word in get_args(ReleaseLevel.__value__)


type ReleaseKind = Literal["release", "candidate", "promotion"]
"""What one run of a release does.

A release is cut and tagged; a candidate is cut and tagged as a pre-release
of the version its series is heading for; a promotion cuts nothing, and tags
the newest candidate's own commit as that version.
"""


class ReleaseSpec(BaseModel, frozen=True):
    """Which files a release moves in this repository.

    Every field is a fact about a layout rather than a value with a right
    answer, which is why each is a default a project replaces rather than a
    constant it would have to fork the command to change.
    """

    version_file: str = "pyproject.toml"
    """The manifest whose ``[project] version`` a release moves.

    One file, named, because a repository may hold several manifests and only
    one of them is what it publishes. A default of the repository root suits a
    project that publishes itself; one that keeps its distribution in a
    subdirectory names that manifest instead.
    """

    changelog: str = "CHANGELOG.md"
    """The document whose open section a release closes."""

    tag_prefix: str = "v"
    """What the tag puts before the version.

    A prefix rather than a whole format, because the version is the tag's
    content and `git describe --contains` is read by people who expect to find
    it there.
    """

    branch: str = "main"
    """The branch a release lands on, and a candidate is promoted from.

    Read from the remote's copy where one is fetched, because that is where a
    release lands and where the tag that publishes it goes; the local branch
    answers only where there is no remote copy to read.
    """

    promotion_prefix: str = "release-"
    """What the branch a promotion is committed on puts before its version.

    A branch of its own, cut from the candidate's tag, because the
    integration branch has moved on since and the release is the candidate,
    not it; the branch is what lands on :attr:`branch` through a pull request.
    """

    def promotion_branch(self, version: str) -> str:
        """The branch the promotion to ``version`` is committed on."""
        return f"{self.promotion_prefix}{version}"


# lup: ignore[constant-declaration] — `release` is this repository's own commit
# type, one row of the table `docs/contributing.md` publishes, and `dev release`
# writes it. A project choosing another subject would be choosing a type that
# table does not have.
RELEASE_SUBJECT_PREFIX = "release: "
"""How a release commit names itself, written once and read by two.

`dev release` writes the subject and the migrations gate finds it, which is
one fact with two readers rather than two literals that agree until somebody
edits one. The gate looks for the commit rather than the tag because the two
travel differently: a tag is pushed last, deliberately, so the branch reaches
a reviewer carrying the release and not yet its tag — and a gate reading the
tag calls every break that release shipped undeclared for exactly that long.
"""


def release_subject(previous: str, version: str) -> str:
    """The subject line a release commit carries — a candidate's and a promotion's too."""
    return f"{RELEASE_SUBJECT_PREFIX}{previous} → {version}"


# lup: ignore[constant-declaration] — PEP 440's own spelling of a release
# candidate, which an index and an installer read, not a choice made here
CANDIDATE_SEGMENT = "rc"
"""The pre-release segment a candidate's version carries.

``rc`` rather than ``a`` or ``b``, because what is cut is the release itself
rather than a preview of it; written in PEP 440's normal form, so the tag,
the version the index holds and the one an installer resolves are one string.
"""


def candidate_version(target: str, number: int) -> str:
    """The version the ``number``-th candidate for ``target`` is published as."""
    return f"{target}{CANDIDATE_SEGMENT}{number}"


def next_version(current: str, level: ReleaseLevel) -> str:
    """The version a release at that level moves to.

    Pre-1.0 is not special-cased. A project below 1.0 puts a break in the
    minor by convention rather than by arithmetic, and encoding the convention
    here would decide for every adopter what their zero means.
    """
    semver = parse_semver(current)
    if semver is None:
        raise ValueError(f"{current} is not a version this can move")
    match level:
        case "patch":
            return f"{semver.major}.{semver.minor}.{semver.patch + 1}"
        case "minor":
            return f"{semver.major}.{semver.minor + 1}.0"
        case "major":
            return f"{semver.major + 1}.0.0"
        case _:
            raise ValueError(f"{level} is not patch, minor or major")


def level_of(version: str) -> ReleaseLevel:
    """The level of the release that moves to ``version``, read off the version.

    The part a release moves is the last part it leaves non-zero, so a series
    of candidates records its own level in the version it is heading for, and
    nothing else has to remember it.
    """
    semver = parse_semver(version)
    if semver is None:
        raise ValueError(f"{version} is not a version a release moves to")
    if semver.patch:
        return "patch"
    if semver.minor:
        return "minor"
    return "major"


class ReleaseRefused(ValueError):
    """A request the tags and the release branch cannot honour, said in full."""


def relevelled(target: str, level: ReleaseLevel, last: str | None) -> str:
    """The version a series heading for ``target`` moves to at another level.

    Counted from the last release where one is tagged. Where none is — the
    series is the first release this project tags — a higher level is
    counted from the target instead, since raising a level lands on the same
    version from either; a lower one has nothing to count from, and says so.
    """
    if last is not None:
        return next_version(last, level)
    levels = get_args(ReleaseLevel.__value__)
    if levels.index(level) > levels.index(level_of(target)):
        return next_version(target, level)
    raise ReleaseRefused(
        f"{target} would be the first release this project tags, so no release "
        f"beneath it says where a {level} counts from — cut it at its own level "
        f"({level_of(target)}) or a higher one"
    )


def requested_level(word: str | None) -> ReleaseLevel | None:
    """A level as a command line names it, or None where it names none."""
    if word is None or is_level(word):
        return word
    raise ReleaseRefused(f"{word} is not patch, minor or major")


class ReleaseRequest(BaseModel, frozen=True):
    """What somebody asked a release to be, before the tags say what it can be."""

    level: ReleaseLevel | None = None
    """Left out to keep the level an open series of candidates was cut at."""

    pre: bool = False
    """Cut a candidate of the release rather than the release."""

    direct: bool = False
    """Release what the branch holds, rather than promote the open candidate."""


class PendingBreaks(BaseModel, frozen=True):
    """The pending breaks as a release renders them into its section."""

    lines: list[str] = []
    """Rendered lines rather than breaks: one break contributes its reason and
    a line per step, so the length of this is not a count of anything a
    reader recognises."""

    count: int = 0
    """How many breaks those lines speak for."""


class Landing(BaseModel, frozen=True):
    """Where the release branch stands against the newest candidate."""

    commit: str
    """The commit the candidate's tag names."""

    branch: str = ""
    """The ref the release branch was read from; empty where there is none."""

    held: bool = False
    """Whether that branch holds exactly the tree the candidate shipped.

    The tree rather than the commit: a release lands through a merge or a
    squash, and either leaves the branch on a commit of its own holding the
    candidate's content — which is what promoting it needs to be true of.
    """

    moved: int = 0
    """Commits on that branch the candidate does not hold."""

    since: int = 0
    """Commits on this checkout the candidate does not hold.

    What a promotion leaves out: it is cut from the candidate, so the work
    landed here since reaches the release after this one.
    """

    carried: list[str] = []
    """The pending break declarations the candidate held, by file name."""


class Series(BaseModel, frozen=True):
    """The candidates cut toward one version that has not been released."""

    target: str
    numbers: list[int]

    def latest(self) -> str:
        """The newest candidate, which is the one a promotion releases."""
        return candidate_version(self.target, max(self.numbers))


def tagged_version(name: str) -> Version | None:
    """The version a tag names, where it is a release or a candidate of one.

    A tag naming no version, or a pre-release this command never cuts — a
    hand-made beta, a development build — is left out rather than read as
    the newest thing released.
    """
    try:
        version = Version(name)
    except InvalidVersion:
        return None
    plain = version.dev is None and version.post is None and version.local is None
    if plain and (version.pre is None or version.pre[0] == CANDIDATE_SEGMENT):
        return version
    return None


def spent(versions: list[Version], target: str) -> list[int]:
    """The candidate numbers tags have already spent on ``target``, in order."""
    return sorted(
        version.pre[1]
        for version in versions
        if version.pre is not None and Version(version.base_version) == Version(target)
    )


def open_series(versions: list[Version]) -> Series | None:
    """The series the newest tagged version is a candidate in, if it is one.

    By version rather than by date: a release cut on another line while a
    series is open — a fix to the version before it — is older than the
    series, and leaves it open.
    """
    newest = max(versions, default=None)
    if newest is None or newest.pre is None:
        return None
    return Series(
        target=newest.base_version, numbers=spent(versions, newest.base_version)
    )


class ReleasePlan(BaseModel, frozen=True):
    """What a release would do, as the facts a reader checks before it runs."""

    kind: ReleaseKind
    previous: str
    """The version tagged last: the release before, or the newest candidate."""

    version: str
    """What this run publishes: the release, or the candidate of it."""

    target: str
    """The release this run is a step toward, which is ``version`` itself for
    a release and a promotion."""

    level: ReleaseLevel
    date: dt.date
    tag: str
    commit: str = ""
    """For a promotion, the candidate's commit the release is checked against.

    The release is one commit on top of it, and what that commit changes is
    held to the version, the changelog and the record before it is made.
    """

    branch: str = ""
    """For a promotion, the branch its commit is made on, cut from the candidate.

    What lands on the release branch through a pull request, like any
    release, and is merged back into the integration branch.
    """

    candidates: list[str] = []
    """The candidates already cut toward ``target``, oldest first."""

    relevelled: str = ""
    """The version an open series was heading for before this run moved it."""

    carried: list[str] = []
    """The pending break declarations a promotion keeps as the release's record.

    Those the candidate held, by file name: the only files a promotion may
    move, as the version and the changelog are the only ones it may change.
    """

    migrations: list[str] = []
    """The pending instructions this run renders into its section."""

    breaks: int = 0
    """How many declared breaks this run renders or keeps."""

    entries: bool = False
    """Whether the changelog had anything open for this run to fold in."""

    since: int = 0
    """For a promotion, the commits on this branch the release leaves out."""

    def spelled(self) -> list[str]:
        """This plan as the lines a reader is shown before approving it."""
        dated = f"dated {self.date.isoformat()}"
        match self.kind:
            case "candidate":
                listed = ", ".join([*self.candidates, self.version])
                moved = (
                    f" — re-levelled from {self.relevelled}" if self.relevelled else ""
                )
                folding = (
                    "folds in the open entries"
                    if self.entries
                    else "has no open entries to fold in"
                )
                return [
                    f"{self.previous} → {self.version}, tagged {self.tag} — a "
                    f"pre-release of {self.target} ({self.level})",
                    dated,
                    f"the {self.target} section lists {listed}{moved}, {folding}, "
                    f"and stays open until {self.target} is released",
                    f"rendering what {self.breaks} pending break(s) ask of a "
                    f"caller — they stay pending until {self.target} is released",
                ]
            case "promotion":
                return [
                    f"{self.previous} → {self.version}, tagged {self.tag} on "
                    f"{self.branch}: one commit on {self.previous} "
                    f"({short_sha(self.commit)}) that changes only the version, "
                    "the changelog and the record — checked before it is made",
                    dated,
                    f"closing the {self.target} section, which lists "
                    f"{', '.join(self.candidates)}",
                    f"keeping the {self.breaks} break(s) {self.previous} carried "
                    f"as {self.version}'s record",
                    f"merging {self.branch} back into this branch"
                    + (
                        f", whose {self.since} commit(s) since {self.previous} "
                        f"stay out of {self.version}"
                        if self.since
                        else ""
                    ),
                ]
            case "release":
                moved = (
                    f" — re-levelled from the {self.relevelled} candidates"
                    if self.relevelled
                    else ""
                )
                closing = (
                    f"closing the section {', '.join(self.candidates)} went out "
                    "from, with what landed since"
                    if self.candidates
                    else "closing the open changelog section"
                    if self.entries
                    else "the changelog has no open section — the release "
                    "records only itself"
                )
                return [
                    f"{self.previous} → {self.version}, tagged {self.tag}{moved}",
                    dated,
                    closing,
                    f"folding in what {self.breaks} pending break(s) ask of a "
                    f"caller, and keeping them as {self.version}'s record",
                ]

    def written(self, log: Changelog) -> Changelog:
        """The changelog as this run leaves it."""
        match self.kind:
            case "candidate":
                return log.with_candidate(
                    self.target,
                    Candidate(version=self.version, date=self.date),
                    self.migrations,
                )
            case "promotion":
                return log.promoted(self.version, self.date)
            case "release":
                return log.released_as(self.version, self.date, self.migrations)


class ReleaseState(BaseModel, frozen=True):
    """What a release is decided from: the manifest, the tags, the release branch."""

    manifest: str
    """The version the published manifest declares."""

    tagged: list[str] = []
    """Every version a tag reachable from here names, its prefix taken off."""

    landing: Landing | None = None
    """Where the release branch stands against the newest candidate, if any."""

    def planned(
        self,
        request: ReleaseRequest,
        date: dt.date,
        log: Changelog,
        pending: PendingBreaks,
        spec: ReleaseSpec,
    ) -> ReleasePlan:
        """What ``request`` comes to, here, or the refusal saying why not."""
        if request.pre and request.direct:
            raise ReleaseRefused(
                "--pre cuts a candidate and --direct releases without one; "
                "ask for one of them"
            )
        versions = [
            version
            for version in map(tagged_version, self.tagged)
            if version is not None
        ]
        series = open_series(versions)
        last = max(
            (version for version in versions if version.pre is None), default=None
        )
        if series is None:
            if Version(self.manifest).is_prerelease:
                raise ReleaseRefused(
                    f"the manifest names {self.manifest}, a candidate, but no "
                    "tag of one is reachable from here — fetch the tags, since "
                    "the series it belongs to is read from them"
                )
            if request.level is None:
                raise ReleaseRefused(
                    "no candidate is open to continue or promote, so the level "
                    "is yours to name: patch, minor or major"
                )
            level = request.level
            target = next_version(self.manifest, level)
            previous = self.manifest
            moved = ""
        else:
            level = request.level or level_of(series.target)
            kept = level == level_of(series.target)
            target = (
                series.target
                if kept
                else relevelled(series.target, level, str(last) if last else None)
            )
            previous = series.latest()
            moved = "" if kept else series.target
        numbers = spent(versions, target)
        candidates = [candidate_version(target, number) for number in numbers]

        if request.pre:
            version = candidate_version(target, max(numbers, default=0) + 1)
            return ReleasePlan(
                kind="candidate",
                previous=previous,
                version=version,
                target=target,
                level=level,
                date=date,
                tag=f"{spec.tag_prefix}{version}",
                candidates=candidates,
                relevelled=moved,
                migrations=pending.lines,
                breaks=pending.count,
                entries=bool(log.unreleased),
            )
        if series is not None and not request.direct and not moved:
            landing = self.promotable(previous, spec)
            return ReleasePlan(
                kind="promotion",
                previous=previous,
                version=target,
                target=target,
                level=level,
                date=date,
                tag=f"{spec.tag_prefix}{target}",
                commit=landing.commit,
                branch=spec.promotion_branch(target),
                candidates=candidates,
                carried=landing.carried,
                breaks=len(landing.carried),
                entries=log.candidate is not None,
                since=landing.since,
            )
        return ReleasePlan(
            kind="release",
            previous=previous,
            version=target,
            target=target,
            level=level,
            date=date,
            tag=f"{spec.tag_prefix}{target}",
            candidates=candidates,
            relevelled=moved,
            migrations=pending.lines,
            breaks=pending.count,
            entries=bool(log.unreleased) or log.candidate is not None,
        )

    def promotable(self, candidate: str, spec: ReleaseSpec) -> Landing:
        """Where the release branch holds exactly ``candidate``, or why it does not.

        A branch that moved is the one case with two answers — another
        candidate, or releasing what the branch holds now — and which is right
        depends on what moved, so both are named and neither is taken.
        """
        landing = self.landing
        if landing is None or not landing.branch:
            raise ReleaseRefused(
                f"there is no {spec.branch} branch to read, so nothing says "
                f"whether it holds {candidate} — fetch it, or release what this "
                "branch holds with `dev release --direct`"
            )
        if landing.held:
            return landing
        if not landing.moved:
            raise ReleaseRefused(
                f"{landing.branch} does not hold {candidate} yet — land it, "
                "then promote it"
            )
        raise ReleaseRefused(
            f"{landing.branch} has moved since {candidate}: it holds "
            f"{landing.moved} commit(s) {candidate} does not. Cut another "
            "candidate with `dev release --pre`, or release what this branch "
            "holds now with `dev release --direct`"
        )


def held_declarations(commit: str, root: Path, pending: Path) -> list[str]:
    """The pending break declarations ``commit`` held, by file name.

    Read from the commit rather than the checkout, because those are the only
    ones a promotion of it may move. A record outside this repository holds
    nothing this release carried.
    """
    if not pending.is_relative_to(root):
        return []
    listed = git.lines(
        "ls-tree", "--name-only", commit, "--", f"{pending.relative_to(root)}/"
    )
    return [Path(name).name for name in listed if Path(name).suffix == ".toml"]


def landed(tag: str, branch: str, root: Path, pending: Path) -> Landing:
    """Where ``branch`` stands against the commit ``tag`` names.

    The remote's copy first: that is where a release lands, and a local
    branch nobody pulled into says nothing about it.
    """
    commit = git.out("rev-list", "-n", "1", tag)
    repository = Repository(Path.cwd())
    since = repository.count(f"{commit}..HEAD")
    carried = held_declarations(commit, root, pending)
    ref = next(
        (ref for ref in (f"origin/{branch}", branch) if repository.resolves(ref)),
        "",
    )
    if not ref:
        return Landing(commit=commit, since=since, carried=carried)
    return Landing(
        commit=commit,
        branch=ref,
        held=git.out("rev-parse", f"{ref}^{{tree}}")
        == git.out("rev-parse", f"{commit}^{{tree}}"),
        moved=repository.count(f"{commit}..{ref}"),
        since=since,
        carried=carried,
    )


def read_state(spec: ReleaseSpec, root: Path, pending: Path) -> ReleaseState:
    """What a release decides from, read off this checkout.

    ``pending`` is where break declarations wait for a release; a promotion
    reads it at the candidate's commit to know which of them it carried.
    """
    tagged = [
        name.removeprefix(spec.tag_prefix)
        for name in git.lines(
            "tag", "--list", f"{spec.tag_prefix}*", "--merged", "HEAD"
        )
        if name
    ]
    series = open_series(
        [version for version in map(tagged_version, tagged) if version is not None]
    )
    return ReleaseState(
        manifest=published_version(root / spec.version_file),
        tagged=tagged,
        landing=(
            landed(f"{spec.tag_prefix}{series.latest()}", spec.branch, root, pending)
            if series is not None
            else None
        ),
    )


def published_version(manifest: Path) -> str:
    """The version the named manifest declares, read structurally."""
    match manifest_table(manifest):
        case {"project": {"version": str(version)}}:
            return version
        case _:
            raise KeyError(f"{manifest} declares no [project] version")


def with_version(manifest: Path, version: str) -> None:
    """Move that manifest's ``[project] version``, and nothing else in it."""

    def moved(document: TOMLDocument) -> None:
        project = document["project"]
        if not isinstance(project, tomlkit.items.Table):
            raise KeyError(f"{manifest} has no [project] table to move a version in")
        project["version"] = version

    edited_manifest(manifest, moved)


def carry_out(
    plan: ReleasePlan,
    spec: ReleaseSpec,
    root: Path,
    log: Changelog,
    record: MigrationRecord,
    regenerate: Callable[[], None],
) -> None:
    """Write what the plan says, commit it, and tag it.

    A candidate or a release is committed here, on the branch this runs from.
    A promotion is committed from the candidate instead, by :func:`promote`,
    since this branch has moved on since the candidate and the release is not
    it; ``log`` is this branch's changelog, which a promotion does not write.
    """
    if plan.kind == "promotion":
        promote(plan, spec, root, record, regenerate)
        return
    prepare(plan, spec, root, log, record, regenerate)
    git.commit("-m", release_subject(plan.previous, plan.version))
    git.tag("-a", plan.tag, "-m", f"{spec.version_file} {plan.version}")


def prepare(
    plan: ReleasePlan,
    spec: ReleaseSpec,
    root: Path,
    log: Changelog,
    record: MigrationRecord,
    regenerate: Callable[[], None],
) -> None:
    """Write and stage what the plan's commit holds, whatever its kind.

    Every kind moves the manifest to the version it publishes, so the tree a
    tag names is the one the workflow builds and a project pinned to it
    reads. A candidate leaves every pending break where it is: the breaks
    belong to the release, and a later candidate renders them again. A
    release moves them all into its record, a promotion only those its
    candidate held.

    The record is the release's migration step, taken whole so a record kept
    differently is the one thing a project replaces. ``regenerate`` runs
    whatever the kind, because a version or a record the generated trees
    compile from leaves them behind, and the commit guard refuses exactly
    that.
    """
    (root / spec.changelog).write_text(plan.written(log).render())
    with_version(root / spec.version_file, plan.version)
    if plan.kind != "candidate":
        record.release(
            plan.version, root, plan.carried if plan.kind == "promotion" else None
        )
    regenerate()
    git.add("-A")


def promote(
    plan: ReleasePlan,
    spec: ReleaseSpec,
    root: Path,
    record: MigrationRecord,
    regenerate: Callable[[], None],
) -> None:
    """Commit the release on a branch cut from the candidate, tag it, merge it back.

    The checkout switches to the new branch for the commit, since
    regenerating needs a tree to run in, and returns to the branch it stood
    on whatever happens. What is staged there is checked against the
    candidate's commit before it is committed; where the check fails, or
    anything else does, what was written is undone and the branch is gone —
    the command refuses a checkout that was not clean, so undoing loses
    nothing of anybody's.
    """
    repository = Repository(Path.cwd())
    returning = repository.branch()
    if not returning:
        raise ReleaseRefused(
            "a promotion is merged back into the branch it is run from, and this "
            "checkout stands on none — check out the integration branch"
        )
    if repository.resolves(f"refs/heads/{plan.branch}"):
        raise ReleaseRefused(
            f"{plan.branch} already exists — finish the promotion it holds, or "
            "delete it before cutting another"
        )
    git.switch("-q", "-c", plan.branch, plan.commit)
    try:
        prepare(
            plan, spec, root, Changelog.read(root / spec.changelog), record, regenerate
        )
        beyond = unpromoted(plan, spec, root, record)
        if beyond:
            raise ReleaseRefused(
                f"the promotion would change {', '.join(beyond)} beyond "
                f"{plan.previous}'s version, so what it shipped would not be what "
                "was tested — nothing was committed or tagged"
            )
        git.commit("-m", release_subject(plan.previous, plan.version))
    except Exception:
        git.reset("-q", "--hard", "HEAD")
        git.switch("-q", returning)
        git.branch("-q", "-D", plan.branch)
        raise
    git.tag("-a", plan.tag, "-m", f"{spec.version_file} {plan.version}")
    git.switch("-q", returning)
    merged_back(plan, spec, root)


def merged_back(plan: ReleasePlan, spec: ReleaseSpec, root: Path) -> None:
    """Merge the promotion into the branch it was run from.

    A normal merge: the version, the moved record and the closed section come
    back, and whatever the branch landed since the candidate stays. The
    changelog is the one file both sides are sure to have touched — entries
    opened above the very heading the promotion rewrote — so it is written as
    this branch's own with the candidates' section closed as the promotion
    closed it, rather than left to a textual merge. Anything else that
    conflicts is left for a merge by hand, the release already tagged.

    Without renames: the promotion moves every break the candidate held out
    of the pending directory, which git reads as the directory itself moving,
    and would carry the breaks this branch declared since into the release's
    record with them.
    """
    ours = str(git("show", f"HEAD:{spec.changelog}"))
    git(
        "merge",
        "--no-ff",
        "--no-commit",
        "-X",
        "no-renames",
        plan.branch,
        _ok_code=[0, 1],
    )
    (root / spec.changelog).write_text(
        Changelog.parse(ours).promoted(plan.version, plan.date).render()
    )
    git.add(spec.changelog)
    conflicted = [str(path) for path in Repository(Path.cwd()).conflicted()]
    if conflicted:
        git.merge("--abort")
        raise ReleaseRefused(
            f"{plan.tag} is tagged on {plan.branch}, but merging it back conflicts "
            f"in {', '.join(conflicted)} — merge {plan.branch} into this branch "
            "by hand"
        )
    git.commit(
        "-m",
        f"merge: {plan.version} back from {plan.branch} — its closed section and record",
    )


def version_only(before: str, after: str, candidate: str, release: str) -> bool:
    """Whether ``after`` is ``before`` with only ``candidate`` respelled ``release``.

    Line by line, each line equal or equal once the version is respelled, in
    whatever format carries it — a manifest, a lock file, the proof a
    generator stamps its version into — which is why this reads lines rather
    than any one parser's document.
    """
    was, now = before.splitlines(), after.splitlines()
    return len(was) == len(now) and all(
        old == new
        # lup: ignore[string-replace] — the one respelling a promotion may
        # make, compared across formats no single parser reads
        or old.replace(candidate, release) == new
        for old, new in zip(was, now, strict=True)
    )


def unpromoted(
    plan: ReleasePlan, spec: ReleaseSpec, root: Path, record: MigrationRecord
) -> list[str]:
    """What the staged promotion changes beyond the version, the changelog and the record.

    Judged against the candidate's commit, file by file. The changelog, a
    break the candidate held moved from pending into the release's record,
    and a file whose every changed line differs only in the version are what
    a promotion is; anything else is named.
    """
    moved = {
        (directory / name).relative_to(root).as_posix()
        for directory in (record.pending_directory(), record.root / plan.version)
        if directory.is_relative_to(root)
        for name in plan.carried
    }

    def staged(*filters: str) -> list[str]:
        return [
            path
            for path in git.lines(
                "diff", "--cached", "--name-only", "--no-renames", *filters, plan.commit
            )
            if path and path != spec.changelog
        ]

    return [
        *(path for path in staged("--diff-filter=m") if path not in moved),
        *(
            path
            for path in staged("--diff-filter=M")
            if not version_only(
                git.out("show", f"{plan.commit}:{path}"),
                git.out("show", f":{path}"),
                plan.previous,
                plan.version,
            )
        ),
    ]
