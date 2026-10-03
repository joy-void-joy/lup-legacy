"""How this project obtains the ``lup`` library.

A project built on this template reaches ``lup`` one of three ways, and the
mode is a property of ``pyproject.toml`` that can be changed at any time:

``published``
    The release from PyPI, published as ``lup-agents``. Upgrading is
    ``uv lock --upgrade-package lup-agents`` plus a harness regeneration,
    rather than a merge against a vendored fork.
``git``
    The repository itself, resolved at a branch, tag, or commit. The default
    for a new project while no release is published: it gives an adopter the
    same package a release would, from the only place the library exists yet,
    so nothing has to be vendored to get it.
``local``
    A copy under ``packages/lup``, wired as a uv workspace member. What the
    template ships, and what a project that genuinely needs to modify library
    source keeps.

Every mode moves under a command, and the commit or version it moved to is
written into ``uv.lock`` — which is what lets ``dev update`` hold the library,
the generated trees and the copied half at one upstream commit. Improving lup
from a project built on it is a worktree in ``refs/lup`` and a pin at the
branch carrying the change.

Leaving ``local`` also strips the workspace wiring that stops resolving once
``packages/lup`` is gone: the uv workspace, the pytest source path, and the
pyright includes and execution environments rooted in the package.

Reading is :func:`~lup.workspace.paths.manifest_table` matched structurally;
writing goes through :func:`~lup.formats.toml.edited_manifest` so comments
and layout survive the edit.

Examples::

    $ uv run lup-devtools dev library status
    $ uv run lup-devtools dev library use published --version 0.3.0
    $ uv run lup-devtools dev library git --branch dev
"""

from enum import StrEnum
from pathlib import Path
from typing import Literal, TypedDict, get_args
from urllib.parse import urlsplit

import httpx
import sh
import tomlkit
import tomlkit.items
import typer
from pydantic import BaseModel, Field, ValidationError
from importlib.metadata import version as installed_version
from packaging.requirements import Requirement
from packaging.version import InvalidVersion, Version

from lup.execution.git import Repository
from lup.workspace.paths import manifest_table, project_root
from lup.execution.shell import git
from lup.formats.toml import edited_manifest
from lup.devtools.sync import load_projects
from lup.harness.codescan.common import LIBRARY_PACKAGE_ROOT
from lup.harness.credential import parse_remote, resolved_host
from lup.devtools.project import Tracker
from lup.types import JsonObject, JsonValue
from lup.devtools.utils import decode_stderr, slug_from_remote
from lup.providers.routing import Provider

# The three below spell where the vendored copy sits, which is a fact about
# lup's own layout and this repository's, not a choice either end makes.
VENDORED_ROOT = "packages/lup"  # lup: ignore[constant-declaration] — fixed layout
VENDORED_SRC = "packages/lup/src"  # lup: ignore[constant-declaration] — fixed layout
# lup: ignore[library-default] — fixed layout
VENDORED_SIBLINGS = {"src": VENDORED_SRC, "tests": f"{VENDORED_ROOT}/tests"}
"""Each plain search root and the vendored one that shadows it. A search path
naming the plain root wants its vendored twin exactly while the package is
there, and wants it gone the moment the package is not."""
DISTRIBUTION = "lup-agents"
"""The name the library is required and published under, which is not the
name it is imported by: a requirement, a ``[tool.uv.sources]`` key, a lock
entry and an index lookup spell this, and ``import lup`` does not."""
REGISTRATION = "lup"
"""The ``sync.json`` registration naming lup's repository, which ``refs/lup``,
``sync setup lup`` and a scaffold's ``project`` all spell: the repository the
distribution is built from, named apart from the distribution itself."""
# lup: ignore[constant-declaration] — the glob this repository's own uv workspace
# is laid out as, which the manifest below already states
WORKSPACE_MEMBERS = ["packages/*"]

PACKAGE_SUBDIRECTORY = VENDORED_ROOT
"""Where the distribution sits inside that repository — a fixed fact about
lup's own layout, not a choice an adopter makes."""


class ExecutionEnvironment(TypedDict):
    """One pyright execution environment lup declares: a root and its paths.

    Both keys, because an environment lup owns exists to put the generated
    runtime on the search path. An environment the *project* declares is not
    this shape — ``extraPaths`` is optional in pyright's schema, and the
    diagnostic overrides an environment may carry are not lup's to restate —
    so those move as the tables they were written as.
    """

    root: str
    extraPaths: list[str]


# Type-checking a vendored adapter's dispatcher asset needs the generated
# runtime beside it on the search path. Both halves live under the package, so
# the pair exists exactly when the library is vendored.
RUNTIMES: tuple[Provider, ...] = get_args(Provider.__value__)
"""Which adapters have a tree under the package, read off the type naming them.

Derived rather than listed: a search path exists for an adapter because lup
ships one, so the answer is :data:`~lup.providers.routing.Provider`'s and a
second spelling here is a list that would go on naming two after a third
arrived."""
VENDORED_EXECUTION_ENVIRONMENTS = {
    runtime: ExecutionEnvironment(
        root=f"{VENDORED_SRC}/lup/providers/{runtime}/assets",
        extraPaths=[
            f".{runtime}/plugins/lup/hooks/runtime",
            f"{VENDORED_SRC}/lup/policy/assets",
        ],
    )
    for runtime in RUNTIMES
}
"""The environment each runtime's vendored adapter needs, keyed by that
runtime — so where one belongs among the project's own is read off the key
rather than sniffed back out of its root."""


class LibraryMode(StrEnum):
    """Where the ``lup-agents`` distribution is resolved from."""

    PUBLISHED = "published"
    GIT = "git"
    LOCAL = "local"


type GitRefKind = Literal["branch", "tag", "rev"]
"""Which kind of ref a git source pins, spelled as uv spells it."""


class GitSource(BaseModel, frozen=True):
    """A repository and the single ref of it a project resolves ``lup`` at.

    The ref is one field pair rather than three optional ones, so a source
    naming both a branch and a tag cannot be constructed — uv accepts only
    one, and a model that can hold two only moves the error later.
    """

    url: str = Field(min_length=1)
    ref_kind: GitRefKind = "branch"
    ref: str = "main"

    def require_available_branch(self) -> None:
        """Distinguish a deleted branch from a transport failure before relocking."""
        if self.ref_kind != "branch":
            return
        try:
            git.out(
                "ls-remote",
                "--exit-code",
                "--heads",
                self.url,
                f"refs/heads/{self.ref}",
            )
        except sh.ErrorReturnCode as error:
            if error.exit_code == 2:
                raise typer.BadParameter(
                    f"Pinned branch {self.ref!r} is absent at {self.url}. "
                    "The existing lock remains usable. Inspect the remote branches "
                    "and compare the locked commit with the intended replacement; "
                    "then run `uv run --no-sync lup-devtools dev library git "
                    "--branch <replacement>` and `uv run --no-sync lup-devtools "
                    "dev update`, or `dev update --commit <reviewed-sha>`. "
                    "No replacement branch was selected automatically."
                ) from error
            raise typer.BadParameter(
                f"Could not verify pinned branch {self.ref!r} at {self.url}: "
                f"{decode_stderr(error) or f'git exited {error.exit_code}'}. "
                "Its absence is unconfirmed; restore remote access before updating."
            ) from error

    def entry(self) -> tomlkit.items.InlineTable:
        """Render the ``[tool.uv.sources]`` value this source declares."""
        entry = tomlkit.inline_table()
        entry.update(
            {
                "git": self.url,
                self.ref_kind: self.ref,
                "subdirectory": PACKAGE_SUBDIRECTORY,
            }
        )
        return entry


class RefFlag(BaseModel, frozen=True):
    """One ref-kind flag, and the ref a command line gave it — or nothing."""

    kind: GitRefKind
    ref: str | None = None


def git_source(
    url: str,
    *,
    branch: str | None = None,
    tag: str | None = None,
    rev: str | None = None,
) -> GitSource:
    """Collapse the three ref flags into the one ref a git source may carry.

    A command line can spell all three; uv accepts one. Rejecting the excess
    here is what keeps :class:`GitSource` unable to represent the conflict.
    """
    named = [
        flag
        for flag in (
            RefFlag(kind="branch", ref=branch),
            RefFlag(kind="tag", ref=tag),
            RefFlag(kind="rev", ref=rev),
        )
        if flag.ref is not None
    ]
    match named:
        case []:
            return GitSource(url=url)
        case [only] if only.ref is not None:
            return GitSource(url=url, ref_kind=only.kind, ref=only.ref)
        case _:
            raise typer.BadParameter(
                "name one of --branch, --tag, or --rev, not "
                + " and ".join(f"--{flag.kind}" for flag in named)
            )


def declared_source(manifest: JsonObject | None) -> JsonValue:
    """What ``[tool.uv.sources]`` declares for :data:`DISTRIBUTION`, if anything."""
    match manifest:
        case {"tool": {"uv": {"sources": dict(sources)}}} if DISTRIBUTION in sources:
            return sources[DISTRIBUTION]
    return None


def read_mode(root: Path) -> LibraryMode:
    """Classify the acquisition mode ``pyproject.toml`` declares."""
    match declared_source(manifest_table(root / "pyproject.toml")):
        case {"workspace": True}:
            return LibraryMode.LOCAL
        case {"git": str()}:
            return LibraryMode.GIT
        case _:
            return LibraryMode.PUBLISHED


def read_git_source(root: Path) -> GitSource | None:
    """Return the repository and the ref it is pinned at, when git.

    Read through :func:`~lup.workspace.paths.manifest_table`, so a manifest
    that is missing, or that a merge stopped inside, pins nothing rather than
    raising: the sync registry derives the library's registration from this
    on every read, and a launch opened to repair a conflicted manifest is the
    one that must not fail over it.
    """
    match declared_source(manifest_table(root / "pyproject.toml")):
        case {"git": str(url), "branch": str(ref)}:
            return GitSource(url=url, ref_kind="branch", ref=ref)
        case {"git": str(url), "tag": str(ref)}:
            return GitSource(url=url, ref_kind="tag", ref=ref)
        case {"git": str(url), "rev": str(ref)}:
            return GitSource(url=url, ref_kind="rev", ref=ref)
        case {"git": str(url)}:
            return GitSource(url=url)
        case _:
            return None


def configured_repository(root: Path, project: str = REGISTRATION) -> str:
    """The dependency's repository, from its pin or named sync registration."""
    source = read_git_source(root)
    if source is not None:
        return source.url
    registered = next(
        (entry for entry in load_projects(root) if entry["name"] == project), None
    )
    if registered is None:
        return ""
    if url := registered.get("url"):
        return url
    # The machine's own transport, where nothing shared names the repository:
    # a registration that only ever existed here has no other identity, and
    # refusing over the absence of a key nobody wrote would leave the pin
    # unconfigurable on a machine that has said where the repository is.
    if reach := registered.get("remote"):
        return reach
    if path := registered.get("path"):
        checkout = (root / Path(path).expanduser()).resolve()
        return Repository(checkout).remote_url("origin") or ""
    return ""


def repository_url(
    root: Path, url: str | None = None, project: str = REGISTRATION
) -> str:
    """Require a declared dependency source, allowing an explicit override."""
    found = url if url is not None else configured_repository(root, project)
    if not found.strip():
        raise typer.BadParameter(
            f"No repository is configured for '{project}'. Pass --url <repository> "
            "to dev library git, set its url in sync.json, or say how this "
            f"machine reaches it: sync remote {project} <url>, or sync setup "
            f"{project} /path/to/repo."
        )
    return found


def library_trackers(root: Path, project: str = REGISTRATION) -> list[Tracker]:
    """Route library defects to its configured forge, preserving the host."""
    url = configured_repository(root, project)
    address = parse_remote(url)
    if address is None or not address.host:
        return []
    parsed = urlsplit(url) if "://" in url else None
    host = address.host
    if parsed is None or parsed.scheme == "ssh":
        host = resolved_host(host)
    if parsed is not None and parsed.scheme in {"http", "https"} and parsed.port:
        host = f"{host}:{parsed.port}"
    return [
        Tracker(
            repository=f"{host}/{slug_from_remote(url)}",
            what="the framework this project is built on",
            # A report names the module it is about, and modules are named
            # by the import root, whatever the distribution is called.
            components=[LIBRARY_PACKAGE_ROOT],
        )
    ]


def requirement_for(entry: str, version: str | None) -> str:
    """Restate one requirement with, or without, a lower version bound.

    The extras the project asked for survive: only the specifier changes,
    because a source override supplies the version in every mode but
    ``published``.
    """
    requirement = Requirement(entry)
    extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
    bound = f">={version}" if version is not None else ""
    return f"{requirement.name}{extras}{bound}"


def apply_dependency(document: tomlkit.TOMLDocument, version: str | None) -> list[str]:
    """Restate the ``lup-agents`` requirement in ``[project].dependencies``."""
    dependencies = document["project"]["dependencies"]
    for index, entry in enumerate(dependencies):
        if Requirement(str(entry)).name != DISTRIBUTION:
            continue
        restated = requirement_for(str(entry), version)
        if restated == str(entry):
            return []
        dependencies[index] = restated
        return [f"dependency: {entry} -> {restated}"]
    raise KeyError(f"no {DISTRIBUTION} requirement in [project].dependencies")


# GitSource's own share of this is `git.entry()`; what is left is the edit to
# `[tool.uv.sources]`, which the document owns and one mode's argument does not.
def apply_source(
    document: tomlkit.TOMLDocument,
    mode: LibraryMode,
    git: GitSource | None = None,
) -> list[str]:
    """Declare, or clear, the ``[tool.uv.sources]`` override for ``lup-agents``."""
    sources = document["tool"]["uv"]["sources"]
    match mode:
        case LibraryMode.PUBLISHED:
            if DISTRIBUTION not in sources:
                return []
            del sources[DISTRIBUTION]
            return ["source: resolved from the package index"]
        case LibraryMode.GIT:
            if git is None:
                raise ValueError("git mode needs a repository and ref")
            entry = git.entry()
        case LibraryMode.LOCAL:
            entry = tomlkit.inline_table()
            entry.update({"workspace": True})
    if DISTRIBUTION in sources and dict(sources[DISTRIBUTION]) == dict(entry):
        return []
    sources[DISTRIBUTION] = entry
    return [f"source: {tomlkit.dumps(entry).strip()}"]


def apply_workspace(document: tomlkit.TOMLDocument, vendored: bool) -> list[str]:
    """Declare the uv workspace exactly while the library is vendored."""
    uv = document["tool"]["uv"]
    if not vendored:
        if "workspace" not in uv:
            return []
        del uv["workspace"]
        return ["workspace: removed"]
    if "workspace" in uv:
        return []
    workspace = tomlkit.table()
    workspace["members"] = WORKSPACE_MEMBERS
    uv["workspace"] = workspace
    return [f"workspace: members = {WORKSPACE_MEMBERS}"]


def apply_search_path(
    document: tomlkit.TOMLDocument, table: list[str], key: str, vendored: bool
) -> list[str]:
    """Add or drop every root under the vendored package in one path list.

    Each vendored root is placed directly after the plain one it shadows, so
    a project that leaves the vendored mode and returns to it gets the list it
    started with rather than a reshuffled diff.
    """
    holder = document
    for step in table:
        if step not in holder:
            return []
        holder = holder[step]
    if key not in holder:
        return []
    paths = [str(entry) for entry in holder[key]]
    wanted = [path for path in paths if not path.startswith(f"{VENDORED_ROOT}/")]
    if vendored:
        for plain, shadowed in VENDORED_SIBLINGS.items():
            if plain in wanted:
                wanted.insert(wanted.index(plain) + 1, shadowed)
    if wanted == paths:
        return []
    holder[key] = wanted
    return [f"{'.'.join([*table, key])}: {paths} -> {wanted}"]


def restored_beside_their_runtime(
    kept: list[tomlkit.items.Table],
) -> list[tomlkit.items.Table]:
    """Put each vendored environment back ahead of its runtime's own tree.

    Order is reconstructed rather than remembered, so leaving the vendored
    mode and returning to it restores the list the project started with
    instead of handing every adopter a reshuffled diff. Reconstruction only
    ever inserts: the environments the project kept stay in the order it wrote
    them, including one whose root names no runtime at all and which therefore
    anchors nothing.
    """

    def declared_table(declared: ExecutionEnvironment) -> tomlkit.items.Table:
        """Render one environment lup owns as the table the file holds."""
        entry = tomlkit.table()
        entry.update(declared)
        return entry

    placed = list(kept)
    for runtime, declared in VENDORED_EXECUTION_ENVIRONMENTS.items():
        beside = next(
            (
                index
                for index, entry in enumerate(placed)
                if runtime in str(entry["root"])
            ),
            len(placed),
        )
        placed.insert(beside, declared_table(declared))
    return placed


def apply_execution_environments(
    document: tomlkit.TOMLDocument, vendored: bool
) -> list[str]:
    """Keep pyright environments rooted in the package only while it is there.

    A restored entry lands ahead of its own runtime's tree, which is the order
    the template ships and the only one reconstructible without remembering
    where it sat. Environments match on disjoint roots, so order carries no
    meaning to pyright — fixing it is what makes leaving and re-entering the
    vendored mode churn-free.

    Every other environment is moved rather than rewritten: the table the
    project wrote is the one that lands, so a missing ``extraPaths`` and a
    diagnostic override beside the root both survive a mode change lup has no
    reason to involve them in.
    """
    if "pyright" not in document["tool"]:
        return []
    pyright = document["tool"]["pyright"]
    key = "executionEnvironments"
    if key not in pyright:
        return []
    current = list(pyright[key])
    kept = [item for item in current if not str(item["root"]).startswith(VENDORED_ROOT)]
    desired = restored_beside_their_runtime(kept) if vendored else kept
    if desired == current:
        return []
    environments = tomlkit.aot()
    for entry in desired:
        environments.append(entry)
    pyright[key] = environments
    return [f"pyright environments: {len(current)} -> {len(desired)}"]


def guard_leaving_local(root: Path, force: bool) -> None:
    """Refuse to un-vendor a checkout that still is the template itself.

    An uninitialized template and the lup repository are the same bytes, so
    no probe separates them. The rename is the event that makes a checkout
    somebody's project, and it is what ``/lup:init`` runs first.
    """
    if force or not (root / "src" / "lup_template").is_dir():
        return
    raise typer.BadParameter(
        "src/lup_template/ is still present, so this checkout is the template "
        "itself rather than a project built on it. Run `dev init rename-package "
        "<project>` first, or pass --force to un-vendor anyway."
    )


# Rewriting `pyproject.toml` is about the checkout, and `git` is the argument
# exactly one mode reads — beside `version`, which one other does.
def set_mode(
    root: Path,
    mode: LibraryMode,
    *,
    version: str | None = None,
    git: GitSource | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Rewrite ``pyproject.toml`` so ``lup`` resolves the way ``mode`` says."""
    vendored = mode is LibraryMode.LOCAL
    if vendored and not (root / VENDORED_ROOT).is_dir():
        raise typer.BadParameter(
            f"{VENDORED_ROOT}/ is not present, so there is no library to vendor. "
            "Resolve it from its repository instead — "
            "`dev library git --branch <branch>`."
        )

    def resolved(document: tomlkit.TOMLDocument) -> list[str]:
        return [
            *apply_dependency(
                document, version if mode is LibraryMode.PUBLISHED else None
            ),
            *apply_source(document, mode, git),
            *apply_workspace(document, vendored),
            *apply_search_path(
                document, ["tool", "pytest", "ini_options"], "pythonpath", vendored
            ),
            *apply_search_path(document, ["tool", "pyright"], "include", vendored),
            *apply_execution_environments(document, vendored),
        ]

    return edited_manifest(root / "pyproject.toml", resolved, write=not dry_run)


def drop_vendored(root: Path, dry_run: bool) -> list[str]:
    """Remove the vendored copy once nothing resolves through it."""
    if not (root / VENDORED_ROOT).is_dir():
        return []
    if not dry_run:
        git("rm", "-r", "--quiet", VENDORED_ROOT, _cwd=str(root))
    return [f"removed {VENDORED_ROOT}/"]


def report(changes: list[str], dry_run: bool, settled: str) -> None:
    """Print one mode change, or say the project already reads that way."""
    if not changes:
        typer.echo(settled)
        return
    typer.echo(f"Dry run — {len(changes)} change(s):" if dry_run else "Changed:")
    for change in changes:
        typer.echo(f"  {change}")
    if not dry_run:
        typer.echo("\nNext: uv sync && uv run lup-devtools harness generate all")


def use_library(
    mode: LibraryMode,
    version: str | None,
    keep_vendored: bool,
    force: bool,
    dry_run: bool,
) -> None:
    """CLI entry for ``lup-devtools dev library use`` (see module docstring)."""
    root = project_root()
    if mode is not LibraryMode.LOCAL:
        guard_leaving_local(root, force)
    changes = set_mode(root, mode, version=version, dry_run=dry_run)
    if mode is not LibraryMode.LOCAL and not keep_vendored:
        changes.extend(drop_vendored(root, dry_run))
    report(changes, dry_run, f"Already resolving {DISTRIBUTION} as {mode}.")


# The body of `dev library git`, beside the other three command entries: its
# subject is the command line, and GitSource is what the flags parsed into.
def git_library(
    source: GitSource, keep_vendored: bool, force: bool, dry_run: bool
) -> None:
    """CLI entry for ``lup-devtools dev library git`` (see module docstring)."""
    root = project_root()
    guard_leaving_local(root, force)
    changes = set_mode(root, LibraryMode.GIT, git=source, dry_run=dry_run)
    if not keep_vendored:
        changes.extend(drop_vendored(root, dry_run))
    report(
        changes,
        dry_run,
        f"Already resolving {DISTRIBUTION} from {source.url} "
        f"at {source.ref_kind} {source.ref}.",
    )


def library_status() -> None:
    """CLI entry for ``lup-devtools dev library status`` (see module docstring)."""
    root = project_root()
    mode = read_mode(root)
    typer.echo(f"mode: {mode}")
    match mode:
        case LibraryMode.LOCAL:
            typer.echo(f"vendored: {root / VENDORED_ROOT}")
        case LibraryMode.GIT:
            source = read_git_source(root)
            if source is not None:
                typer.echo(f"repository: {source.url}")
                typer.echo(f"{source.ref_kind}: {source.ref}")
        case LibraryMode.PUBLISHED:
            typer.echo(f"version: {installed_version(DISTRIBUTION)}")


RELEASE_INDEX_URL = f"https://pypi.org/pypi/{DISTRIBUTION}/json"
"""Where a release is looked up. Overridable: a project resolving lup through
a private index asks that index the same question in the same shape."""

RELEASE_PROBE_SECONDS = 10.0
"""How long the look-up waits. An unreachable index is an answer this command
knows how to give, and giving it beats blocking whoever is waiting on it."""


class ReleaseIndexInfo(BaseModel):
    """The one field of the index's document this reads."""

    version: str


class ReleaseIndexFile(BaseModel):
    """The one field of a published file this reads."""

    yanked: bool = False


class ReleaseIndexDocument(BaseModel):
    """A package index's answer about one distribution."""

    info: ReleaseIndexInfo
    releases: dict[str, list[ReleaseIndexFile]] = {}
    """Every version the index holds, with its files.

    Absent from an index that answers only with its latest, and read as that
    one version then.
    """

    def probed(self) -> "ReleaseProbe":
        """The newest release this holds, and any candidate newer than it.

        Told apart by the version itself, which PEP 440 marks a pre-release —
        not by which one the index calls latest, since an index holding only
        candidates calls one of them that. A version whose every file was
        yanked is withdrawn and is neither.
        """
        held = [
            Version(version)
            for version, files in self.releases.items()
            if any(not file.yanked for file in files)
        ] or [Version(self.info.version)]
        release = max((v for v in held if not v.is_prerelease), default=None)
        newer = [
            v for v in held if v.is_prerelease and (release is None or v > release)
        ]
        return ReleaseProbe(
            version=str(release) if release is not None else "",
            candidate=str(max(newer)) if newer else "",
        )


class ReleaseProbe(BaseModel, frozen=True):
    """What the package index holds for lup: a version, nothing, or no answer.

    The third outcome stays itself instead of collapsing into the second. An
    index that could not be reached has not said a release is absent, and
    reading it that way pins a project to a repository ref on the strength of
    a dropped connection.

    A release candidate is none of the three. It is published, and offered
    as the version to pin it would be taken by every project asking, which is
    what publishing it as a pre-release is for preventing — so it is named
    beside the answer, with the command that takes it on purpose.

    What this does not decide is the acquisition mode. Only one half of that
    is a fact — whether a release exists at all — and the other half is what
    the project is to the library: one that works on lup, dogfooding a branch
    and sending changes back, wants that branch whether or not a release was
    cut from it.
    """

    version: str = ""
    candidate: str = ""
    """The newest pre-release newer than ``version``, where the index holds one."""

    unreachable: str = ""

    def describe(self) -> list[str]:
        """What the index answered, and the command that takes it at its word."""
        if self.unreachable:
            return [
                f"index unreachable: {self.unreachable}",
                "A probe that did not land settles nothing. Retry, or declare "
                "the mode this project already knows it wants.",
            ]
        offered = (
            [
                f"candidate: {self.candidate} — a pre-release, taken only by "
                f"naming it: dev library use {LibraryMode.PUBLISHED} "
                f"--version {self.candidate}"
            ]
            if self.candidate
            else []
        )
        if not self.version:
            return [
                "no release published yet",
                f"dev library {LibraryMode.GIT} --branch <branch>",
                *offered,
            ]
        return [
            f"released: {self.version}",
            f"dev library use {LibraryMode.PUBLISHED} --version {self.version}",
            *offered,
            f"`{LibraryMode.GIT}` stays a live choice: a project that works on "
            "lup as well as with it runs the branch it is improving rather "
            "than the last release cut from it.",
        ]


def probe_release(
    url: str = RELEASE_INDEX_URL, timeout: float = RELEASE_PROBE_SECONDS
) -> ReleaseProbe:
    """Ask the package index whether a release of lup exists.

    A missing project is the index answering rather than failing: before the
    first release that is the true state of the world, and it is the answer
    that sends a new project to the repository rather than to nothing.
    """
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as error:
        return ReleaseProbe(unreachable=f"{type(error).__name__}: {error}")
    if response.status_code == httpx.codes.NOT_FOUND:
        return ReleaseProbe()
    if response.status_code != httpx.codes.OK:
        return ReleaseProbe(unreachable=f"{url} answered {response.status_code}")
    try:
        return ReleaseIndexDocument.model_validate_json(response.content).probed()
    except (ValidationError, InvalidVersion):
        return ReleaseProbe(unreachable=f"{url} answered a document it could not read")


def library_release() -> None:
    """CLI entry for ``lup-devtools dev library release`` (see module docstring)."""
    for line in probe_release().describe():
        typer.echo(line)
