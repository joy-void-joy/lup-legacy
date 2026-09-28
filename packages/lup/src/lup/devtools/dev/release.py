"""Cutting a release: closing the changelog, moving the version, tagging it.

A release here is one transaction over the files it touches, and the argument
for compiling it rather than writing it down is what prose costs. Carried in a
skill — fold the pending migrations into the changelog, close the section,
move the version, move the migrations into the release — the steps make a list
that runs only as well as whoever is reading it that day. Measured in this
repository, three of the four had never run at all.

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

Which files a release touches is declared, not assumed. This repository
publishes one distribution out of ``packages/lup`` and keeps two other version
numbers that a release must not touch — the scaffold's own, and the agent
version that names a trace directory — so a command that went looking for "the
version" would have found three and been wrong about two.
"""

import datetime as dt
from pathlib import Path
from typing import Literal, TypeGuard, get_args

import tomlkit
import tomlkit.items
from pydantic import BaseModel

from lup.devtools.changelog import Changelog
from lup.workspace.history import parse_semver

type ReleaseLevel = Literal["patch", "minor", "major"]
"""Which part of the version a release moves.

Spelled as the three it can be, so a caller holding one has been checked
rather than trusted; what arrives from a command line is a string until
:func:`is_level` says otherwise.
"""


def is_level(word: str) -> TypeGuard[ReleaseLevel]:
    """Whether that word is a level, narrowing it where it is."""
    return word in get_args(ReleaseLevel.__value__)


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
    """The subject line a release commit carries."""
    return f"{RELEASE_SUBJECT_PREFIX}{previous} → {version}"


class ReleasePlan(BaseModel, frozen=True):
    """What a release would do, as the facts a reader checks before it runs."""

    previous: str
    version: str
    date: dt.date
    tag: str
    migrations: list[str]
    """The pending instructions this release folds into its section.

    Rendered lines rather than migrations: one break contributes its reason
    and a line per step, so the length of this is not a count of anything a
    reader recognises. :attr:`breaks` is that count.
    """

    breaks: int
    """How many declared breaks those lines speak for."""

    entries: bool
    """Whether the open section had anything under it."""

    def spelled(self) -> list[str]:
        """This plan as the lines a reader is shown before approving it."""
        return [
            f"{self.previous} → {self.version}, tagged {self.tag}",
            f"dated {self.date.isoformat()}",
            (
                "closing the open changelog section"
                if self.entries
                else "the changelog has no open section — the release records only itself"
            ),
            f"folding in what {self.breaks} pending break(s) ask of a caller, "
            f"and keeping them as {self.version}'s record",
        ]


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


def published_version(manifest: Path) -> str:
    """The version the named manifest declares, read structurally."""
    match tomlkit.parse(manifest.read_text()).unwrap():
        case {"project": {"version": str(version)}}:
            return version
        case _:
            raise KeyError(f"{manifest} declares no [project] version")


def with_version(text: str, version: str) -> str:
    """That manifest's text with its ``[project] version`` moved.

    Through ``tomlkit`` so the comments and the layout an author wrote survive
    a change to one value, which a re-emit from a parsed table would not.
    """
    document = tomlkit.parse(text)
    project = document["project"]
    if not isinstance(project, tomlkit.items.Table):
        raise KeyError("no [project] table to move a version in")
    project["version"] = version
    return tomlkit.dumps(document)


def released(
    log: Changelog, version: str, date: dt.date, migrations: list[str]
) -> Changelog:
    """The changelog with its open section closed as this release.

    The pending instructions go in under a heading of their own rather than
    among the entries, because they are not what changed — they are what a
    reader has to do about it, and somebody scanning for that should not have
    to read the rest to find out whether there is any.
    """
    if not migrations:
        return log.released_as(version, date)
    folded = "".join(f"- {line}\n" for line in migrations)
    return log.released_as(
        version, date, additions=f"\n### What this release asks of a caller\n\n{folded}"
    )
