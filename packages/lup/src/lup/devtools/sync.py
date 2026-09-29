"""Track repos to sync with and review their commits since the last sync.

These commands are the read/fetch half of ``/lup:update`` — the workflow that
pulls improvements *from* the lup template (and other tracked upstreams) *into*
this repo. ``/lup:update`` reviews every upstream commit since the last sync,
generalizes the domain-specific ones back into reusable scaffold, and applies
them; this module is what gives it the diffs to review and remembers how far it
got. ``status`` shows which upstreams have new commits, ``log``/``diff`` feed
the commit-by-commit review, and ``mark-synced`` advances the per-project
checkpoint once a review is done so the next run starts where this one stopped.

Reviewing a diff means reading the upstream's *current* code, so commits are
fetched into a local clone before comparison (see ``ensure_local``):
``status`` stays read-only and never touches the network, while ``log``/``diff``
clone-or-fetch on demand. Each materialized upstream also gets a stable
``refs/<name>`` symlink so other commands (e.g. ``/lup:import``) can browse it
by name without re-resolving cache paths.

Two registry files declare what to track:

- sync.json (committed): what the project declares about the repositories it
  tracks — the name each one is known by, which repository it is ("url"),
  whether the project cannot work without it ("required") and what a session
  may reach of it ("mount"). Every machine reads the same claims. It is
  template scaffold: agents must never modify it, and the edit policy asks
  before any change to it.
- sync.json.local (gitignored): what this machine answers — where a checkout
  actually is ("path"), the transport that reaches it from here ("remote"),
  branch overrides, and local-only projects. Set "ignore": true to skip a
  project's review. Most machines need no entry at all: the clone's location
  is derived from the name, and a registration naming the repository this
  checkout already is needs no opt-out either (see :func:`exemption`).

Which repository an entry means is its "url", completed in two places the
files do not have to restate (see :func:`completed`): the library's own
registration follows the git pin in pyproject.toml wherever the project
resolves lup from a repository, and a registration nothing places falls back
to the source repository its installed distribution declares.

The split is by *who the fact belongs to*, not by which key it is. A path can
genuinely differ between machines and a transport almost always does; which
repository is meant, and whether a workflow here needs it, are the same
answer everywhere and would be a decision re-made per machine if they lived
in the gitignored half.

The registry is direction-neutral: "sync" names the mechanism, not a
direction. Seen from a project built on the template, the shipped lup entry
is an upstream to pull improvements from; seen from the lup repo itself,
sync.json.local registers the downstream fleet whose commits /lup:update
reviews. Same tooling, opposite seats.

The script merges both: .local entries override sync.json entries by name.
Review checkpoints live under the common Git directory and are shared by
sibling worktrees. A legacy `last_synced_commit` remains a seed until a
shared checkpoint is explicitly recorded with `mark-synced`.
A project with a URL and no local path is materialized under
``~/.cache/lup/sync/`` in the layout a registration naming a local path
already points at -- a bare repository with a worktree attached to it -- so a
session can branch, commit and push in either, and a launch mounts a clone it
materialized whole (see :func:`mounted_root`). Nothing a review does moves a
local branch: the upstream's commits are read from a remote-tracking ref, and
refreshing one of these clones is a fetch and nothing else.

An entry carrying a "mount" is also a declaration about *access*: a session
can open that project, at the mode it names, wherever the project lives on
this machine. Absent, nothing is mounted — which is the whole difference
between a registry and a boundary, and the reason the key has to be written
rather than defaulted. A tracked mount still opens nothing on its own: until
this machine says where the project is, or a URL lets it be cloned, there is
nothing to bind.

An entry carrying "required": true says the project cannot work without that
repository, which is a claim about *this* project rather than about the
tracked one. It is what makes a fresh checkout materialize a repository
instead of failing at the point of use, and what makes an absence reported
rather than silent: `sync fetch` clones it, a launch clones what it also
mounts, `sync log` clones on first use, and `sync status` names every
requirement this machine has not answered and exits nonzero.

Examples::

    $ uv run lup-devtools sync status
    $ uv run lup-devtools sync log my-project
    $ uv run lup-devtools sync log my-project --no-stat
    $ uv run lup-devtools sync diff my-project abc1234
    $ uv run lup-devtools sync mark-synced my-project
    $ uv run lup-devtools sync mark-synced my-project --at 4c6293a6
    $ uv run lup-devtools sync setup my-project /path/to/repo --synced
    $ uv run lup-devtools sync setup my-project /path/to/repo --branch main
    $ uv run lup-devtools sync remote my-project git@github.com:owner/repo.git
    $ uv run lup-devtools sync grant nvidia.com/gpu=all
    $ uv run lup-devtools sync revoke nvidia.com/gpu=all
"""

import json
import logging
import os
import string
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Literal,
    NotRequired,
    Required,
    TypedDict,
    get_args,
)

import sh
import typer
from packaging.metadata import parse_email
from pydantic import (
    BaseModel,
    ConfigDict,
    TypeAdapter,
    ValidationError,
    with_config,
)

from lup.workspace.paths import is_template_scaffold, project_root
from lup.devtools import sync_state, sync_usage
from lup.devtools.dev.records import log_ref_updates
from lup.harness.credential import remote_url, same_repository
import lup.harness.content.docs.upstream_reports as upstream_reports
from lup.launch.preflight import reopened
from lup.devtools.subapps import subapp
from lup.devtools.utils import decode_stderr, format_table, short_sha
from lup.execution.shell import git
from lup.harness.devices import Device, registered_devices
from lup.harness.requirements import Finding, Manifest
from lup.harness.toolchain import (
    container_client,
    for_host,
    granted_device_requirement,
)
from lup.policy.assets.host import launched, measured_boundary
from lup.launch.pointer_trust import judged_roots
from lup.sandbox.rail import AccessibleRoot

if TYPE_CHECKING:
    from lup.devtools.dev.library import GitSource

app = typer.Typer(no_args_is_help=True)
SUBAPP = subapp(
    "sync", "Stay in step with upstream: tracked repos, and what they owe", app
)
logger = logging.getLogger(__name__)


@app.command("upstream")
def upstream_cmd(
    slug: Annotated[
        str,
        typer.Argument(help="Which report to print; omit to list what is declared"),
    ] = "",
) -> None:
    """Print a measured upstream defect, or list the ones declared.

    Beneath `sync` rather than `dev` because staying in step with a dependency
    and reporting what that dependency got wrong are one subject: both are what
    this project owes to, and is owed by, code it does not own.

    The body goes to stdout alone so it pipes: each report's own section in
    `docs/upstream-reports.md` carries the `gh issue create` line that consumes
    it. Filing is deliberately not done here — an account is the person's, not
    the tooling's.
    """
    roster = upstream_reports.ROSTER
    if not slug:
        for report in roster.reports:
            typer.echo(
                f"{report.slug}  ({report.component} {report.version}, "
                f"{report.status()})"
            )
            typer.echo(f"    {report.title}")
        return
    report = roster.named(slug)
    if report is None:
        typer.echo(
            f"No upstream report named {slug!r}; declared: {roster.handles()}",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(report.body)


@with_config(ConfigDict(extra="allow"))
class ProjectEntry(TypedDict, total=False):
    """One tracked project, merged from sync.json(.local).

    A ``TypedDict``, not a ``BaseModel``: per CLAUDE.md, ``TypedDict`` types
    the JSON-shaped config we read from and write back to disk verbatim, while
    ``BaseModel`` is for values we validate and construct. These entries are
    hand-edited config, so loads validate the declared keys (catching typos
    with a real error instead of silent misbehavior) and ``extra="allow"``
    keeps any hand-added keys so the document round-trips unchanged.
    """

    name: Required[str]

    path: str
    """Where this machine keeps the repository, when it is not the derived place.

    An override, not the registration. A project with a URL and no path is
    materialized at :func:`bare_path`, under the per-user cache, which is
    derived from the name alone -- so the machine that keeps it there needs
    no entry of its own, and the local half carries a path only where
    somebody's checkout genuinely sits somewhere else. `sync setup` writes
    one."""

    url: str
    """Which repository this is, in the spelling every machine shares.

    Identity rather than reach. A committed registration names the
    repository the project means; how one machine gets to that same
    repository is its own question, answered by ``remote``, and two
    spellings of one repository are compared as one by
    :func:`~lup.harness.credential.same_repository`. So an https
    registration and an ssh clone of it agree, and two different
    repositories under one name are still refused.

    Spelled as https where it is committed: a clone is made on the host
    before any credential is lent, and https reads a public repository with
    none, while every remote a session opens is rewritten onto whichever
    transport its credential reaches (see
    :func:`~lup.harness.credential.remote_rewrites`).

    The library's own registration reads its ``url`` off the git pin
    wherever the project resolves lup from a repository, rather than off
    either file -- see :func:`completed`."""

    remote: str
    """The URL this machine fetches from, where it is not the one ``url`` names.

    This machine's own fact, so it belongs in the gitignored half beside the
    device grants: which transport reaches a forge from here is a question
    about this machine's keys, proxy and ssh config, and the committed file
    is scaffold every adopter runs from. `sync remote` writes it, and a
    clone is made with it. Absent, ``url`` is both the identity and the
    reach, which is what a registration with nothing special about its
    transport looks like."""

    branch: str
    review_from: Literal["remote", "local"]
    last_synced_commit: str
    ignore: bool

    required: bool
    """Whether this project cannot work without that repository present.

    The one key here that is about *this* project rather than about the
    registered one, and the reason it belongs in the committed half. Fixing a
    defect in a dependency inside that dependency, deriving a relocation map
    over its own range of history, reviewing its commits: each needs the
    repository on disk, and a project whose workflows need it needs it on
    every machine, not on the one where somebody once ran `setup`. What stays
    per-machine is the answer -- a transport, a path -- and most machines owe
    none, because the location is derived from the name.

    Never a clone behind somebody's back. `sync fetch` materializes what is
    required, a launch materializes what it is also going to mount, and the
    first command that reads an upstream's commits clones on demand; each
    says so as it goes. Required and absent is reported by `sync status`,
    with the command that answers it, and exits nonzero -- an unmet
    requirement is a result rather than a silence to be discovered at the
    point of use."""

    mount: Literal["rw", "ro"]
    """Whether a session may open this project, and in which mode.

    Written or absent, never defaulted -- and written in either half. What a
    session may reach is the project's decision and not the machine's:
    read-write against read-only is part of the claim, and a project whose
    own workflow commits in a dependency's checkout says that once, in the
    file every machine reads, rather than being widened one machine at a
    time by whoever meets the absence first. A default is still refused,
    which is the difference: silence is the answer for every registration
    that does not carry the key.

    A tracked mount hands out nothing by itself. Nothing is mounted until
    this machine says where the project is -- a path, a URL, or a clone the
    derived location already holds -- so the committed claim and the
    per-machine answer stay separate, and both files are protected edit roots
    so neither is widened without the question being asked.

    A separate question from ``ignore``, which governs sync *review*: being
    the upstream of this repository is a reason not to read its commits back
    and no reason at all to be unable to open it. Spelled as a literal so a
    misspelling is the error this registry's validation exists to raise
    rather than a project quietly out of reach."""


@with_config(ConfigDict(extra="allow"))
class SyncConfig(TypedDict):
    """Top-level shape of sync.json and sync.json.local.

    Same rationale as :class:`ProjectEntry`: a ``TypedDict`` mirroring the
    on-disk JSON document, validated on read with extras preserved.
    """

    projects: list[ProjectEntry]
    devices: NotRequired[list[str]]
    """The host devices sessions on this machine are granted, by CDI name.

    Read from the local file alone and written by `sync grant`. Beside the
    mounts because it is the same kind of claim -- what a session opened here
    reaches beyond its checkout -- and in the gitignored half for the reason
    the ``mount`` key is written or absent: which GPU a machine holds is that
    machine's fact, and the committed file is scaffold every adopter and
    every contributor runs from."""

    difftool: NotRequired[list[str]]
    """The program this machine opens a before/after pair in, as argv.

    The two paths are appended, which is what every common difftool already
    takes last, so a registration is the command and its flags and nothing
    about placeholders. Empty or absent means this machine has no editor for
    the job and a review renders in the terminal.

    Gitignored for the same reason a device grant is: which editor is
    installed is a fact about one machine, and a committed answer would open
    a program on every adopter's that may not be there."""


SYNC_CONFIG_ADAPTER = TypeAdapter(SyncConfig)
PROJECT_ENTRY_ADAPTER = TypeAdapter(ProjectEntry)

MOUNT_MODES: tuple[str, ...] = get_args(ProjectEntry.__annotations__["mount"])
"""The modes a registration may ask for, read off the entry that declares them.

Derived rather than restated, so the command line and the document cannot come
to accept different words. A second spelling here is how the CLI would end up
taking a mode the registry then refuses to validate."""


def sync_file() -> Path:
    return project_root() / "sync.json"


def local_file() -> Path:
    return project_root() / "sync.json.local"


def cache_dir() -> Path:
    """Where a registration carrying only a URL is materialized on this machine.

    Beside the rest of lup's per-user cache rather than under the project
    root, and the placement is load-bearing rather than tidy. A clone inside
    the checkout is inside the session's own writable mount, so
    :func:`~lup.sandbox.rail.fleet_lease` drops it as already covered and a
    registration asking for ``ro`` silently gets ``rw`` -- the one mode the
    key exists to be able to say. It is also re-cloned once per worktree,
    where a full history is worth having once per machine, and the ``refs/``
    symlinks ``worktree create`` copies into a new checkout point back into
    the cache of the one it was cut from.
    """
    return Path.home() / ".cache" / "lup" / "sync"


def legacy_cache_dir() -> Path:
    """A second place clones sit, still read so nothing quietly abandons one.

    A clone under the project root is writable with the checkout, so a
    session can commit in one and some have. Resolving the cache alone leaves
    that work in a directory nothing looks at again, which is the failure
    this guards against -- so this location is resolved as well, and a
    project found there is used where it stands rather than re-cloned
    beside it.
    """
    return project_root() / ".cache" / "sync"


def refs_dir() -> Path:
    return project_root() / "refs"


def load_json(path: Path) -> SyncConfig:
    if not path.exists():
        return {"projects": []}
    try:
        return SYNC_CONFIG_ADAPTER.validate_python(json.loads(path.read_text()))
    except json.JSONDecodeError as error:
        raise typer.BadParameter(f"{path} is not valid JSON: {error}") from error


def save_local(data: SyncConfig) -> None:
    local_file().write_text(json.dumps(data, indent=2) + "\n")


def ensure_ref_symlink(name: str, target: str) -> None:
    """Point the stable ``refs/<name>`` symlink at a project's working copy.

    ``refs/`` (gitignored) is a directory of by-name shortcuts into the
    repos this project tracks for sync, wherever each one actually lives —
    a user-configured path, or a clone under the cache. It exists
    so commands and humans can reach a tracked repo as ``refs/<name>`` without
    knowing or re-deriving its real location (e.g. ``/lup:import`` does
    ``cd refs/<project> && git log``). Every time a project is materialized the
    link is re-pointed at its current path; a pre-existing non-symlink at that
    name is left untouched so we never clobber real files.

    Always a working tree where the repository has one: the worktree attached
    to a bare repository rather than its bare half, whether the registration
    named a path or a URL. So `uv run --directory refs/<name>` runs that
    project's own tooling in either case, and a worktree cut from there lands
    beside it under the same ``tree/``.
    """
    link = refs_dir() / name
    target_path = Path(target).resolve()
    # A contained session holds `refs/` read-only, so a session cannot repoint
    # what confines it -- and a link that has to move then cannot. The link is
    # a shortcut and nothing a fetch depends on, so the sync goes on with the
    # shortcut stale and says so, rather than aborting every command in the
    # family before it has fetched anything.
    try:
        refs_dir().mkdir(exist_ok=True)
        match (link.is_symlink(), link.exists()):
            case (True, _):
                if link.resolve() == target_path:
                    return
                link.unlink()
            case (False, True):
                logger.warning("refs/%s exists but is not a symlink, skipping", name)
                return
        link.symlink_to(target_path)
    except OSError as error:
        logger.warning(
            "refs/%s could not be pointed at %s (%s); the shortcut is stale and "
            "the sync continues without it",
            name,
            target_path,
            error.strerror,
        )
        return
    logger.debug("refs/%s -> %s", name, target_path)


SOURCE_LABELS = ("source", "repository", "sourcecode", "github")
"""The labels core metadata files a distribution's source repository under.

PEP 753's well-known ``source`` label and the aliases it folds into it, each
compared after the normalization the standard defines: punctuation and
whitespace removed, then lowercased."""


def distribution_repository(name: str, labels: tuple[str, ...] = SOURCE_LABELS) -> str:
    """The source repository an installed distribution of this name declares.

    Read from the core metadata it was installed with, which is where a
    published package says where it comes from -- and the one statement of
    lup's home a project carries without anybody writing it down, whether it
    resolves a release or keeps a vendored copy. ``Project-URL`` is read by
    ``packaging``, whose parser is the standard's. Nothing where no such
    distribution is installed or it declares no source.
    """

    def normalized(label: str) -> str:
        """A label as PEP 753 compares one."""
        return "".join(
            character
            for character in label
            if character not in string.punctuation and not character.isspace()
        ).lower()

    try:
        metadata = distribution(name).read_text("METADATA") or ""
    except PackageNotFoundError:
        return ""
    raw, _unparsed = parse_email(metadata)
    if "project_urls" not in raw:
        return ""
    return next(
        (
            url
            for label, url in raw["project_urls"].items()
            if normalized(label) in labels
        ),
        "",
    )


def pinned_source(name: str, root: Path | None = None) -> "GitSource | None":
    """The git pin a registration of this name follows, where the project has one.

    Only the library's own registration follows one: ``[tool.uv.sources]``
    pins the library's distribution, and that registration names the
    repository the project consumes it from. Imported where it is asked
    rather than at the top, because the library module reads this module's
    registrations to decide where to pin from, and so imports it first.
    """
    from lup.devtools.dev.library import REGISTRATION, read_git_source

    if name != REGISTRATION:
        return None
    return read_git_source(root if root is not None else project_root())


def installed_as(name: str) -> str:
    """The distribution a registration of this name is installed as.

    The same name for every registration but the library's own, whose
    repository is registered as ``lup`` and published under another name.
    """
    from lup.devtools.dev.library import DISTRIBUTION, REGISTRATION

    return DISTRIBUTION if name == REGISTRATION else name


def completed(entry: ProjectEntry, root: Path) -> ProjectEntry:
    """One merged registration, with the repository it means filled in.

    Two completions, both derived and neither written back. The library's own
    registration takes its ``url`` from the git pin wherever the project
    resolves lup from a repository: the pin is what the project consumes, and
    a registration naming another repository would mount, review and compile
    the copied half from a history the library does not come from -- so the
    pin decides, and nothing has to restate it. A registration nothing
    places -- no ``url``, no ``remote``, no ``path`` -- takes the source
    repository its installed distribution declares; one this machine placed
    keeps the answer it gave.
    """
    pinned = pinned_source(entry["name"], root)
    if pinned is not None:
        return PROJECT_ENTRY_ADAPTER.validate_python({**entry, "url": pinned.url})
    if "url" in entry or "remote" in entry or "path" in entry:
        return entry
    declared = distribution_repository(installed_as(entry["name"]))
    if not declared:
        return entry
    return PROJECT_ENTRY_ADAPTER.validate_python({**entry, "url": declared})


def load_projects(root: Path | None = None) -> list[ProjectEntry]:
    """Load and merge projects from sync.json + sync.json.local.

    Local entries override tracked ones by name, and each merged entry is
    then completed with the repository it means (see :func:`completed`).
    """
    home = root if root is not None else project_root()
    base = load_json(home / "sync.json")
    local = load_json(home / "sync.json.local")

    merged: dict[str, ProjectEntry] = {}  # lup: ignore[empty-collection] — merge fold
    for p in base.get("projects", []):
        merged[p["name"]] = p.copy()
    for p in local.get("projects", []):
        name = p["name"]
        if name in merged:
            base_entry = merged[name]
            merged[name] = PROJECT_ENTRY_ADAPTER.validate_python({**base_entry, **p})
        else:
            merged[name] = p.copy()

    return [completed(entry, home) for entry in merged.values()]


def find_project(name: str) -> ProjectEntry:
    """Find a project by name, raising Exit if not found."""
    projects = load_projects()
    proj = next((p for p in projects if p["name"] == name), None)
    if not proj:
        typer.echo(f"Project '{name}' not found.")
        typer.echo(f"Available: {', '.join(p['name'] for p in projects)}")
        raise typer.Exit(1)
    return proj


class Upstream(BaseModel, frozen=True):
    """A registration located on disk, and the ref its commits are read from.

    The checkout locates Git objects and a place to browse files. Review
    targets a fetched remote ref unless the registration explicitly asks for
    local work or has no origin. Fetching never moves a worker's branch.
    """

    checkout: Path
    """Where git runs: the attached worktree, or the bare half while there is none."""

    tip: str = "HEAD"
    """The ref whose commits are the upstream's, resolved in that checkout."""


def bare_path(name: str) -> Path:
    """Where this project's own bare repository sits inside the cache.

    Spelled ``<name>.git`` with a ``tree/`` of worktrees inside it, which is
    the layout ``git worktree`` already assumes and ``get_tree_dir`` already
    finds — so a session opening one of these clones cuts a worktree in it
    with the same command it uses at home.
    """
    return cache_dir() / f"{name}.git"


def cached_clone(name: str) -> Path | None:
    """This project's clone in the cache, wherever this machine put it."""
    return next(
        (
            candidate
            for candidate in (bare_path(name), legacy_cache_dir() / name)
            if candidate.is_dir()
        ),
        None,
    )


def bare_repository(path: Path) -> bool:
    """Whether git calls this directory a bare repository, which has no tree.

    A directory git does not answer for is not one: a corpus or a set of
    reference material is registered for access alone, the lease binds it
    plainly, and asking it for a worktree would be asking git about a place
    git knows nothing of.
    """
    return (
        git.out("-C", str(path), "rev-parse", "--is-bare-repository", _ok_code=[0, 128])
        == "true"
    )


def clone_branch(proj: ProjectEntry, repository: Path) -> str:
    """The declared/consumed branch, then remote HEAD, then the local branch.

    Empty where HEAD names no branch, which a detached checkout is and a
    review still has to answer for — the caller falls back to the remote's
    own default rather than failing over a state nobody asked about.
    """
    declared = review_branch(proj, repository)
    if declared:
        return declared
    remote_head = git.out(
        "-C",
        str(repository),
        "symbolic-ref",
        "--short",
        "refs/remotes/origin/HEAD",
        _ok_code=[0, 1, 128],
    )
    if remote_head and proj.get("review_from", "remote") == "remote":
        return remote_head.removeprefix("origin/")
    return git.out(
        "-C", str(repository), "symbolic-ref", "--short", "HEAD", _ok_code=[0, 1]
    )


def review_branch(proj: ProjectEntry, repository: Path) -> str:
    """Honor an explicit branch and expose disagreements with the library pin."""
    from lup.devtools.dev.library import read_git_source

    declared = proj.get("branch", "")
    source = read_git_source(project_root())
    if source is None or source.ref_kind != "branch":
        return declared
    # The library's own registration never disagrees with the pin here: it
    # takes its url from it (see `completed`). What is left is any other
    # registration of the same repository, which follows the consumed branch.
    registered_url = registered_repository(proj) or remote_url(repository, "origin")
    if not registered_url or not same_repository(registered_url, source.url):
        return declared
    if declared and declared != source.ref:
        logger.warning(
            "Sync reviews branch %s while the library consumes %s; "
            "set sync setup %s --branch %s to follow the consumed branch",
            declared,
            source.ref,
            proj["name"],
            source.ref,
        )
    return declared or source.ref


def clone_upstream(proj: ProjectEntry, repository: Path) -> Upstream:
    """One cached clone read as an upstream, whichever layout it is in.

    The bare layout keeps its working tree at ``tree/<branch>``; a clone in
    the non-bare layout *is* its own working tree. Both are
    reviewed against a remote-tracking ref, which is the whole reason neither
    has to be reset — and the bare half answers for itself until a worktree
    is attached, because ``rev-list`` and ``show`` read a repository rather
    than a checkout.
    """
    branch = clone_branch(proj, repository)
    attached = repository / "tree" / branch
    return Upstream(
        checkout=attached if branch and attached.is_dir() else repository,
        tip=f"refs/remotes/origin/{branch}" if branch else "refs/remotes/origin/HEAD",
    )


def registered_upstream(
    proj: ProjectEntry, path: Path, report: Callable[[str], None] = typer.echo
) -> Upstream:
    """Read fetched upstream refs without moving a registered working tree.

    ``review_from: local`` explicitly reviews unpublished local commits.
    A repository without an origin is itself the upstream and stays local.
    """
    if not (path / ".git").exists() and not bare_repository(path):
        return Upstream(checkout=path)
    require_registered_origin(proj, path, report)
    branch = clone_branch(proj, path)
    attached = path / "tree" / branch
    remote = proj.get("review_from", "remote") == "remote" and bool(
        remote_url(path, "origin")
    )
    prefix = "refs/remotes/origin" if remote else "refs/heads"
    return Upstream(
        checkout=attached
        if bare_repository(path) and branch and attached.is_dir()
        else path,
        tip=f"{prefix}/{branch}"
        if branch
        else ("refs/remotes/origin/HEAD" if remote else "HEAD"),
    )


def checkpoint_identity(found: Upstream) -> sync_state.ReviewSource:
    """A remote URL or local repository identity, paired with the reviewed ref."""
    remote = (
        remote_url(found.checkout, "origin")
        if found.tip.startswith("refs/remotes/")
        else ""
    )
    repository = remote or git_in(
        str(found.checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    ref = found.tip
    if ref == "HEAD":
        ref = (
            git.out("-C", str(found.checkout), "symbolic-ref", "HEAD", _ok_code=[0, 1])
            or f"detached:{git_in(str(found.checkout), 'rev-parse', 'HEAD')}"
        )
    return sync_state.ReviewSource(repository=repository, ref=ref)


def checkpoint(proj: ProjectEntry, found: Upstream) -> str:
    """The shared checkpoint wins over stale worktree-local declarations.

    It holds for the ref it was taken on, in the repository reviewed rather
    than the spelling that reached it: `checkpoint_identity` reads the origin
    as git resolves it, so a review recorded inside a contained session reads
    the ssh spelling its rewrites put there, and `sync remote` may repoint
    the clone itself -- one repository either way, and one review.
    """
    recorded = sync_state.read(project_root(), proj["name"])
    if recorded is None:
        return proj.get("last_synced_commit", "")
    source = checkpoint_identity(found)
    if recorded.source.ref != source.ref or not same_repository(
        recorded.source.repository, source.repository
    ):
        logger.warning(
            "%s checkpoint belongs to %s at %s; %s at %s has not been reviewed",
            proj["name"],
            recorded.source.repository,
            recorded.source.ref,
            source.repository,
            source.ref,
        )
        return ""
    return recorded.commit


def record_checkpoint(proj: ProjectEntry, found: Upstream, commit: str) -> None:
    sync_state.write(
        project_root(),
        proj["name"],
        sync_state.Checkpoint(source=checkpoint_identity(found), commit=commit),
    )


def kept_checkout(proj: ProjectEntry) -> Path | None:
    """The checkout this machine keeps for a registration, where it named one.

    A ``path`` that is on disk, which is the one answer that outranks the
    cache: a registration whose path has gone is looked for where a URL one
    is. Asked by every reader that has to agree on which of the two a
    registration is -- locating it, materializing it, and deciding how much
    of it a launch mounts -- so the three cannot come to disagree.
    """
    path = proj.get("path", "")
    return Path(path) if path and Path(path).exists() else None


def refuse_redirected_location(
    location: Path, report: Callable[[str], None] = typer.echo
) -> Path:
    """This registration's location, once host git is known to be safe there.

    Asked before any git runs in it, since every git that follows -- the
    `rev-parse` locating it, the `fetch` refreshing it, the `log` a review
    reads, which honours `gpg.program` -- reads the config its pointers lead
    to. A checkout mounted writable into a launch is one a contained session
    could have redirected, and this is the host reading it afterwards.

    The location is discovered from the repository that vouches for it, found
    by path or remembered from the host, rather than trusted for where it sits
    -- see :func:`lup.launch.pointer_trust.judged_roots`. A repository met for the
    first time is remembered or reported, and only a real mismatch refuses.
    """
    trust = judged_roots([location], operator=Path.cwd())
    for notice in trust.notices:
        report(notice)
    if trust.refusal:
        report(trust.refusal)
        raise typer.Exit(1)
    return location


def existing_upstream(proj: ProjectEntry) -> Upstream | None:
    """Where this registration already is, WITHOUT cloning or fetching.

    Read-only counterpart to :func:`ensure_local` — for status reporting that
    must never mutate the working tree or hit the network.
    """
    kept = kept_checkout(proj)
    if kept is not None:
        return registered_upstream(proj, refuse_redirected_location(kept))
    repository = cached_clone(proj["name"])
    if repository is None:
        return None
    refuse_redirected_location(repository)
    require_registered_origin(proj, repository, typer.echo)
    return clone_upstream(proj, repository)


class MissingCheckout(BaseModel, frozen=True):
    """A required registration this machine cannot locate, and what answers it.

    A typed reading rather than a sentence assembled where it is printed,
    because three readers ask for the same thing and must not describe it
    differently: `sync status` puts it under its table and exits on it, a
    launch reports the requirements it was going to mount, and a command that
    needs the checkout says why it has none.
    """

    name: str

    reason: str
    """What is missing, in the registry's words rather than git's."""

    remedy: list[str]
    """What answers it — a command, or the edit and the file to make it in."""

    required: bool = False
    """Whether the registration said this project cannot work without it.

    It changes what the message *is* rather than how it reads: an absence a
    registration declared a need for is an unmet requirement, and one nobody
    claimed to need is a project that cannot be located. Both want the same
    reason and the same remedy, and only one of them is a failure.
    """

    def spelled(self) -> str:
        """This absence as a reader meets it: what is missing, then the answer."""
        opening = (
            f"'{self.name}' is required by this project and {self.reason}"
            if self.required
            else f"'{self.name}' could not be located: {self.reason}"
        )
        return "\n".join([opening, *(f"  {step}" for step in self.remedy)])


def missing_checkout(proj: ProjectEntry) -> MissingCheckout:
    """Why a registration could not be located, and what would answer it.

    Asked once the caller has already failed to locate it, so nothing here
    clones, fetches or touches the network: this reads the registration and
    says what it was missing. The three answers are the three states one can
    be in — nobody said where it is, the path somebody named is gone, and a
    URL is known but nothing is cloned from it yet — and each names the
    command that ends it.
    """
    name = proj["name"]
    needed = bool(proj.get("required"))
    match (transport_url(proj), proj.get("path", "")):
        case ("", ""):
            return MissingCheckout(
                name=name,
                required=needed,
                reason=(
                    "nothing on this machine says where it is: no url in "
                    f"{sync_file().name}, and no remote or path in "
                    f"{local_file().name}"
                ),
                remedy=[
                    f"uv run lup-devtools sync remote {name} <url>"
                    "   (the URL this machine fetches it from)",
                    f"uv run lup-devtools sync setup {name} /path/to/repo"
                    "   (a checkout this machine already has)",
                ],
            )
        case ("", registered):
            return MissingCheckout(
                name=name,
                required=needed,
                reason=f"the path registered for it, {registered}, is not there",
                remedy=[
                    f"uv run lup-devtools sync setup {name} /path/to/repo"
                    "   (where the checkout is now)",
                    f"uv run lup-devtools sync remote {name} <url>"
                    "   (to clone it at the derived location instead)",
                ],
            )
        case (url, _):
            return MissingCheckout(
                name=name,
                required=needed,
                reason=f"nothing is cloned from {url} yet",
                remedy=[f"uv run lup-devtools sync fetch {name}"],
            )


def exemption(proj: ProjectEntry) -> str:
    """Why this checkout owes a registration no repository, empty where it owes one.

    Two registrations are not this checkout's to answer, and both are the
    same shape: the registration names something this repository already is.
    Neither is fetched, mounted, located or reported missing, and `sync
    status` says which one it is where the row would otherwise read as
    something to act on.

    One names it outright — a project registered at the URL this checkout's
    own origin points at, which is what a repository tracking itself looks
    like. Read off origin rather than off a key somebody has to remember to
    write, so a repository never has to declare that it is itself, and a fork
    tracking its parent is still owed one, because a fork is a different
    repository.

    The other is a committed requirement read inside the template scaffold,
    which ``[tool.lup] template = true`` marks. `sync.json` there is the file
    an adopting project receives, and the scaffold is the upstream of every
    registration it ships: the requirement is the adopter's to answer, and
    the tree that wrote it has nothing to clone -- whichever account it was
    forked into. A requirement in the machine's own local file is that
    machine's and is owed here like any other.
    """
    declared = registered_repository(proj)
    own = remote_url(project_root(), "origin")
    if declared and own and same_repository(declared, own):
        return "this checkout is that repository"
    tracked = load_json(sync_file()).get("projects", [])
    shipped = any(
        entry["name"] == proj["name"] and entry.get("required") for entry in tracked
    )
    if shipped and is_template_scaffold(project_root()):
        return "shipped by this scaffold for the projects built on it"
    return ""


def owed_here(proj: ProjectEntry) -> bool:
    """Whether this checkout owes a registration a repository at all."""
    return not exemption(proj)


class LocatedProject(BaseModel, frozen=True):
    """One registration and where this machine found it, or that it did not.

    Paired, because locating a registration is several git commands and two
    readings want that one answer: the table says where each project is, and
    the requirements under it say which of the absences somebody declared a
    need for.
    """

    entry: ProjectEntry
    found: Upstream | None = None


def unmet_requirements(located: list[LocatedProject]) -> list[MissingCheckout]:
    """Every requirement this machine has not answered, from one pass of locating.

    Reads what the caller already resolved rather than resolving again, and
    leaves out the requirements nobody here owes -- see :func:`owed_here`.
    """
    return [
        missing_checkout(one.entry)
        for one in located
        if one.entry.get("required") and one.found is None and owed_here(one.entry)
    ]


def openable(
    proj: ProjectEntry, found: Upstream, report: Callable[[str], None]
) -> Path:
    """The working tree a session opens for this registration.

    Separate from :func:`existing_upstream` so that locating a clone stays a
    question and opening one stays a claim: this is allowed to cut a worktree
    where a bare clone has none, and ``status`` must never do that.
    """
    if not bare_repository(found.checkout):
        return found.checkout
    branch = clone_branch(proj, found.checkout)
    if not branch:
        report(
            f"The clone at {found.checkout} has no branch checked out and "
            f"'{proj['name']}' names none, so there is no worktree to open"
        )
        raise typer.Exit(1)
    return attach_worktree(found.checkout, branch, report)


def mounted_root(proj: ProjectEntry, opened: Path) -> Path:
    """The directory a launch mounts for a registration opened at ``opened``.

    The whole clone where this machine materialized it in the cache: the
    bare half, with every worktree it holds under ``tree/`` then or later.
    A bare mount is what `harness policy-refresh` accepts a new worktree
    beneath, so a branch `/lup:upstream` cuts in the clone is judged by that
    project's own policy rather than this one's, with nothing added to the
    launch. What that costs is edit authority over every worktree the clone
    holds, including one another project on this machine cut there -- the
    cache is per user, and only the projects registering that name work in
    it.

    ``opened`` for anything else. A checkout a registration names by path is
    somebody's own, and its other worktrees may be theirs: mounting its
    working tree accepts that checkout's policy alone, and a launch that
    should accept a worktree beside it grants one with ``--mount`` -- that
    worktree, or the bare directory holding it. A clone the cache holds in
    the plain layout is its own working tree, so mounting that already
    mounts all of it.
    """
    clone = cached_clone(proj["name"]) if kept_checkout(proj) is None else None
    return clone if clone is not None and bare_repository(clone) else opened


def accessible_roots(
    report: Callable[[str], None] = typer.echo,
) -> list[AccessibleRoot]:
    """Every registered project that asked to be reachable, as mounts.

    Only the entries carrying a ``mount``. Registering a project says the
    tooling may read its commits; nothing about it says a session may open
    it, and the two live in the same file only because the file is where a
    project is named. Silence here is the answer for every registration that
    does not carry the key, and for every one this checkout does not owe a
    repository -- see :func:`owed_here`. The scaffold's own lup entry is the
    second kind: it is mounted read-write in the repositories that adopt it,
    and in the repository that ships it the checkout it names is this one.

    Read from the registry rather than from `refs/`, and that difference is
    the whole of why this exists. `refs/` is a directory of symlinks built
    from this registry; it is gitignored, and it sits inside the checkout, so
    it is writable from inside the boundary. A mount table read off it would
    be one the confined session could extend by writing a symlink, which is
    the confined thing choosing what confines it.

    A registration carrying only a URL is materialized on the way past, so
    naming a project on a forge is enough to be able to open it -- which is
    what the lup entry the scaffold ships relies on, so a project built on it
    opens lup read-write at its first launch with nothing set up. That
    happens on the host, before the boundary and on the only side that can
    reach the forge -- and only where nothing is on disk yet, because
    :func:`ensure_local` also fetches, and opening a session is not a review.
    `refs/<name>` is pointed at the working tree opened either way, so a
    clone another project on this machine materialized first is reachable
    here by the same name the workflows use.

    What is mounted depends on whose clone it is -- see :func:`mounted_root`.
    A clone this machine materialized in the cache is mounted whole, its bare
    half with every worktree under ``tree/``; a checkout a registration names
    by path is mounted at the working tree it resolves to. The lease is the
    same either way -- `lease_for` holds every worktree of the repository
    writable, its shared `config` and `hooks/` read-only -- and what differs
    is the edit authority the launch grants: a bare mount accepts every
    worktree's own policy, and lets `harness policy-refresh` accept one cut
    after the launch, where a worktree mount accepts that checkout's alone.
    Either way a clone found without a worktree has one attached here,
    because `refs/<name>` names a working tree; it costs no network and is
    the one write locating a project may do.

    A registration that asked for a mount and cannot be located is reported
    and skipped -- whether it named neither a path nor a URL, or its
    materialization failed. That is a note somebody did not finish, in a
    gitignored file, or a forge that did not answer; failing a launch over
    one would turn either into a session that will not open. A *required* one
    is reported as the requirement it is, with the command that answers it,
    because the session about to open is the one that will meet its absence.
    """

    def located(project: ProjectEntry) -> AccessibleRoot | None:
        """Where one registration is on disk, materializing it if it is not."""
        if "mount" not in project or not owed_here(project):
            return None
        try:
            found = existing_upstream(project)
            if found is None:
                found = ensure_local(project, report)
            opened = openable(project, found, report)
        except typer.Exit:
            report(
                f"'{project['name']}' could not be materialized, so it stays out of reach"
            )
            if project.get("required"):
                report(missing_checkout(project).spelled())
            return None
        ensure_ref_symlink(project["name"], str(opened))
        # lup: solved: a worktree /lup:upstream cuts beside this one is no granted
        # destination -- `harness policy-refresh` accepts one only beneath a
        # mounted bare half -- so its edits are referred until a launch mounts the
        # clone's bare half by hand; decide whether a clone this machine
        # materialized, which only its registrants work in, mounts its bare half
        return AccessibleRoot(
            path=mounted_root(project, opened).resolve(),
            writable=project["mount"] == "rw",
        )

    return [
        root for project in load_projects() if (root := located(project)) is not None
    ]


def registered_difftool() -> list[str]:
    """The argv this machine opens a before/after pair with, empty where none.

    From the local file alone, never the merged registry, for the reason a
    device grant is: `sync.json` is committed scaffold, and an editor named
    there would be launched on every adopter's machine by an author who has
    never seen one of them.
    """
    return load_json(local_file()).get("difftool", [])


def granted_devices(report: Callable[[str], None] = typer.echo) -> list[Device]:
    """Every host device this machine grants its sessions, as the launcher takes them.

    Read from the local file alone, never the merged registry: `sync.json` is
    committed scaffold, and a device named there would be handed to sessions
    on every adopter's machine by an author who has never seen one of them.

    A name the specification's grammar refuses is reported and left out
    rather than failing the launch, the way a registration that cannot be
    materialized is: this is a hand-editable file, and a launch is not where
    a typo in it gets fixed. A grant `sync grant` wrote was validated on the
    way in, so the report only ever names a hand edit.
    """

    def declared(name: str) -> Device | None:
        try:
            return Device(name=name)
        except ValidationError:
            report(
                f"sync.json.local grants {name!r}, which is not a CDI device name "
                "(vendor/class=device), so it stays ungranted"
            )
            return None

    return [
        device
        for name in load_json(local_file()).get("devices", [])
        if (device := declared(name)) is not None
    ]


def verified_grant(device: Device) -> Finding:
    """Exercise one grant the way `harness requirements` will, before writing it.

    The same requirement the host roster builds per grant, pointed at this
    machine's container client the same way, so what a grant proves at the
    moment it is made is what the roster re-proves at setup and nothing else:
    a throwaway container started with the device and nothing more.
    """
    environ = os.environ  # lup: ignore[os-environ]
    aimed = for_host(
        Manifest(requirements=[granted_device_requirement(device)]),
        container_client(),
        project_root(),
    )
    return aimed.check(dict(environ), setting_up=True)[0]


def clone_bare(url: str, repository: Path, report: Callable[[str], None]) -> None:
    """Clone a URL as the bare half of the layout, with its whole history.

    Whole rather than shallow, which is what makes a URL registration as good
    as a path one. A shallow clone is a review window that quietly ends — a
    checkpoint older than the window computes no range at all — and it is
    single-branch besides, so every branch but one is missing and none can be
    cut a worktree from. A session working in one cannot rebase past the
    graft or push a branch that reaches behind it. A depth buys a faster
    first launch, once, for a clone made once per machine rather than once
    per worktree.

    ``--bare`` writes no ``remote.origin.fetch``, so the standard refspec is
    configured immediately afterwards. Without it every later fetch writes
    ``FETCH_HEAD`` alone: ``refs/remotes`` stays empty, and the ref the
    review reads never appears — a silence shaped exactly like an upstream
    with nothing new.

    ``--bare`` also leaves the reflog off, so it is turned on too: a session
    in this clone cuts branches, and a branch's first reflog entry is where
    git logs what it was cut from.
    """
    report(f"Cloning {url} into {repository}...")
    repository.parent.mkdir(parents=True, exist_ok=True)
    try:
        git("clone", "--bare", url, str(repository))
        git(
            "-C",
            str(repository),
            "config",
            "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/*",
        )
        log_ref_updates(repository)
        git("-C", str(repository), "fetch", "--quiet", "origin")
    except sh.ErrorReturnCode as error:
        report(f"Clone failed: {decode_stderr(error)}")
        raise typer.Exit(1)


def refresh(name: str, repository: Path, report: Callable[[str], None]) -> None:
    """Bring this clone's remote-tracking refs level, moving nothing local.

    The whole refresh: a fetch, with no hard reset onto the upstream behind
    it. A reset is what a review reading ``HEAD`` would need, and it is what
    makes a cached clone impossible to work in: a session's uncommitted
    files go with it and say nothing. Reading the review off the
    remote-tracking ref removes the reason for one rather than guarding
    against it, so there is no case in which this destroys anything.

    The refspec is named for the same reason :func:`clone_bare` configures
    one, and naming it here as well covers a clone whose configuration is
    missing it.
    """
    report(f"Fetching latest for '{name}' from {repository}...")
    try:
        for remote in git.lines("-C", str(repository), "remote"):
            git(
                "-C",
                str(repository),
                "fetch",
                "--prune",
                "--quiet",
                remote,
                f"+refs/heads/*:refs/remotes/{remote}/*",
            )
    except sh.ErrorReturnCode as error:
        report(f"Warning: fetch failed: {decode_stderr(error)}")
        raise typer.Exit(1) from error


def attach_worktree(
    repository: Path, branch: str, report: Callable[[str], None]
) -> Path:
    """The worktree this clone is worked in, cut where there is not one yet.

    A bare repository has no working tree, and a review reads files as well
    as commits — so the clone is only half made until one is attached. Cut at
    ``tree/<branch>`` rather than anywhere else because that is where
    ``get_tree_dir`` looks, which is what lets a session inside the clone
    reach for ``git worktree create`` and have the next one land beside this.

    ``worktree prune`` first, because a directory somebody removed leaves its
    administrative entry behind and ``worktree add`` refuses the path while
    one stands.
    """
    checkout = repository / "tree" / branch
    if checkout.is_dir():
        return checkout
    report(f"Attaching a worktree for '{branch}' at {checkout}...")
    try:
        git("-C", str(repository), "worktree", "prune")
        git("-C", str(repository), "worktree", "add", str(checkout), branch)
    except sh.ErrorReturnCode as error:
        report(f"Could not attach a worktree for '{branch}': {decode_stderr(error)}")
        raise typer.Exit(1)
    return checkout


def transport_url(proj: ProjectEntry) -> str:
    """The URL this machine fetches from: its own transport, else the shared one.

    A clone is made with what reaches the forge from here, which is the
    machine's ``remote`` where it wrote one and the registered ``url``
    otherwise. Identity is a separate question and is never read from this:
    see :func:`registered_repository`.
    """
    return proj.get("remote", "") or proj.get("url", "")


def registered_repository(proj: ProjectEntry) -> str:
    """Which repository this registration means, in the spelling that says so.

    The shared ``url`` where the project named one, because that is the claim
    every machine reads. Only where none was named does this machine's own
    transport stand in as the identity, which is what a registration that
    exists on one machine alone looks like.
    """
    return proj.get("url", "") or proj.get("remote", "")


def registered_elsewhere(repository: Path, url: str) -> str:
    """The origin this clone reaches, when that is not the repository registered.

    The cache is per user and keyed by the registered name, so two projects
    on this machine registering different repositories under one name would
    otherwise share a clone — and the second would review, mount and commit
    into the first one's history under its own name. Empty where they agree,
    or where either side has nothing to compare.

    Agreement is about which repository, never about the spelling: a machine
    cloning over ssh from a registration that names an https URL has the
    right history, and reporting that as a conflict would refuse the ordinary
    arrangement of a forge with two transports.
    """
    if not url:
        return ""
    found = git.out(
        "-C", str(repository), "remote", "get-url", "origin", _ok_code=[0, 1]
    )
    return "" if not found or same_repository(found, url) else found


def declaring_file(name: str, key: str) -> Path:
    """Which of the two registry files carries one key of one registration.

    So a refusal can name the file to edit rather than the pair. The tracked
    file wins when it holds the key, because that is the half the merge takes
    it from; anything else is answered by the local one, including a key
    nobody has written yet, which is where it would go.

    The library's own ``url`` is the exception: wherever the project pins lup
    to a repository the registration follows that pin (see :func:`completed`),
    so the manifest holding it is the file that says it.
    """
    if key == "url" and pinned_source(name) is not None:
        return project_root() / "pyproject.toml"
    tracked = next(
        (p for p in load_json(sync_file()).get("projects", []) if p["name"] == name),
        None,
    )
    return sync_file() if tracked is not None and key in tracked else local_file()


def renaming(name: str, url: str) -> str:
    """The edit that makes a registration mean ``url``, where its answer is kept.

    The pin, for the library's own registration wherever the project resolves
    lup from a repository: the registration follows it, so an edit to either
    registry file would be read over. The file carrying the key otherwise.
    """
    pinned = pinned_source(name)
    if pinned is not None:
        return (
            "repoint the pin it follows: uv run lup-devtools dev library git "
            f"--url {url} --{pinned.ref_kind} {pinned.ref}"
        )
    return (
        f"set \"url\" on the '{name}' entry in {declaring_file(name, 'url').name} to it"
    )


def require_registered_origin(
    proj: ProjectEntry, repository: Path, report: Callable[[str], None]
) -> None:
    """Refuse a checkout that holds a different repository from the registered one.

    Which repository, not which spelling. This machine reaching a forge over
    ssh while the shared registration names its https URL is the ordinary
    case and is accepted, so a refusal here means what it says: two
    repositories are registered under one name, and a review, a mount or a
    commit would land in the wrong history.

    The refusal names the file each half of the answer lives in and the exact
    edit, because "correct the registration" is not actionable when the
    registration is two files and the reader cannot tell which of them is
    wrong.
    """
    declared = registered_repository(proj)
    pointing = registered_elsewhere(repository, declared)
    if not pointing:
        return
    name = proj["name"]
    registered_path = proj.get("path", "")
    at_registered_path = bool(registered_path) and (
        Path(registered_path).resolve() == repository.resolve()
    )
    theirs = (
        f'point that entry\'s "path" at a checkout of it: '
        f"uv run lup-devtools sync setup {name} /path/to/repo"
        if at_registered_path
        else f"remove {repository} so the next fetch clones it again"
    )
    report(
        f"The checkout at {repository} is a clone of {pointing}, while "
        f"'{name}' is registered as {declared} in "
        f"{declaring_file(name, 'url').name}. Two repositories under one "
        "name, so a review, a mount or a commit would land in the wrong "
        "history; nothing was read from it. Write whichever is true:\n"
        f"  - {pointing} is the repository meant: {renaming(name, pointing)}.\n"
        f"  - {declared} is: {theirs}. Where this machine reaches it at "
        f"another URL, say so with: uv run lup-devtools sync remote {name} "
        "<url>\n"
        "  - both are tracked here: register them under two names."
    )
    raise typer.Exit(1)


def ensure_local(
    proj: ProjectEntry,
    report: Callable[[str], None] = typer.echo,
) -> Upstream:
    """Materialize an upstream so its commits can be read, and locate it.

    Reviewing upstream commits (``log``/``diff``) means reading the upstream's
    actual git history, which only exists locally — so before any such command
    can run, the project must be present and current on disk. This is that
    guarantee: it clones a project that has only a URL and fetches cached or
    registered repositories while preserving their branches and working trees.
    In every case it (re)points
    ``refs/<name>`` at the result and hands back where the caller runs git.
    ``status`` deliberately does *not* call this (it uses
    :func:`existing_upstream` instead) so a status check never clones,
    fetches, or writes.

    What a clone gets is the layout a path registration already points at: a
    bare repository with a worktree attached, holding every branch and the
    whole history. The point is that the two stop differing — a session can
    open either one, cut a branch in it, commit, and push, and a later review
    of the same project neither notices nor disturbs any of that.

    Progress and error text goes through ``report`` so callers rendering
    tables can defer the messages instead of interleaving them mid-table.
    """
    kept = kept_checkout(proj)
    name = proj["name"]
    if kept is not None:
        found = registered_upstream(
            proj, refuse_redirected_location(kept, report), report
        )
        if proj.get("review_from", "remote") == "remote" and remote_url(kept, "origin"):
            refresh(name, kept, report)
        ensure_ref_symlink(name, str(found.checkout))
        return found

    url = transport_url(proj)
    repository = cached_clone(name)
    if repository is None:
        if not url:
            report(missing_checkout(proj).spelled())
            raise typer.Exit(1)
        repository = bare_path(name)
        clone_bare(url, repository, report)
    else:
        refuse_redirected_location(repository, report)
        require_registered_origin(proj, repository, report)
        refresh(name, repository, report)

    branch = clone_branch(proj, repository)
    if branch and bare_repository(repository):
        attach_worktree(repository, branch, report)
    found = clone_upstream(proj, repository)
    ensure_ref_symlink(name, str(found.checkout))
    return found


def git_in(path: str, *args: str) -> str:
    """Run git command in a specific directory."""
    return git.out("-C", path, *args)


def commit_count(path: str, since: str, tip: str = "HEAD") -> int:
    """Count the commits on ``tip`` a checkpoint has not reached."""
    return int(
        git_in(path, "rev-list", "--count", tip if not since else f"{since}..{tip}")
    )


def resolved_checkpoint(path: str, ref: str, tip: str = "HEAD") -> str:
    """The commit a checkpoint should record, from a ref or from the tip.

    Resolution happens in the upstream checkout rather than being taken on
    trust, so a tag or branch name works and a commit that checkout does not
    have is refused here — where the caller can still fix it — instead of
    landing in the record as a checkpoint nothing can compute a range from.

    ``tip`` is the ref the review was read from, which is the one a finished
    review means. Defaulting it to ``HEAD`` would record what a clone happens
    to be checked out on — somebody's own branch, in a clone this project's
    sessions work in — as the point every upstream commit up to it was
    considered.
    """
    if not ref:
        return git_in(path, "rev-parse", tip)
    try:
        return git_in(path, "rev-parse", "--verify", f"{ref}^{{commit}}")
    except sh.ErrorReturnCode as error:
        raise typer.BadParameter(f"{ref!r} does not name a commit in {path}") from error


@app.command("usage")
def usage_cmd(
    names: Annotated[
        list[str] | None,
        typer.Argument(help="Tracked projects to read (default: every one owed here)"),
    ] = None,
    package: Annotated[
        str, typer.Option("--package", help="The package whose imports are counted")
    ] = "lup",
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Show what each tracked project imports from a package, name by name.

    The reach a removal, rename or move has, read from each project's own
    tracked files where this machine keeps a checkout — without cloning or
    fetching, so it answers about the checkouts as they stand. A project
    with no checkout here is named as unread rather than counted as unused.
    """
    wanted = [
        proj
        for proj in load_projects()
        if (
            proj["name"] in names
            if names
            else owed_here(proj) and not proj.get("ignore")
        )
    ]
    located = [(proj["name"], existing_upstream(proj)) for proj in wanted]
    report = sync_usage.usage_report(
        [
            sync_usage.usage_in(name, found.checkout, package)
            for name, found in located
            if found is not None
        ],
        [name for name, found in located if found is None],
        package,
    )
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
        return
    for reach in report.reach:
        typer.echo(f"{reach.module}  ({', '.join(reach.projects)})")
        if reach.names:
            typer.echo(f"    {', '.join(reach.names)}")
    for name in report.unlocated:
        typer.echo(
            f"{name}: no checkout on this machine; `sync fetch {name}` clones it"
        )


@app.command("status")
def status_cmd() -> None:
    """Show tracked projects and their sync status (read-only).

    Reports cached/not-cloned/behind state without cloning, fetching, or
    resetting. Run ``sync fetch`` to materialize and refresh repos.
    """
    projects = load_projects()
    granted = granted_devices()
    if granted:
        # Said before the projects, and whether or not any project is
        # tracked: a grant is this machine's own claim, and a machine that
        # granted a GPU and tracks nothing has still made it.
        registry = registered_devices()
        typer.echo()
        typer.echo(
            format_table(
                ("Device", "Registry"),
                [
                    [
                        device.name,
                        "registered"
                        if device.name in registry.names
                        else "no spec names it",
                    ]
                    for device in granted
                ],
            )
        )

    if not projects:
        typer.echo("No projects tracked. Check sync.json(.local) or run 'setup'.")
        raise typer.Exit(1)

    def reach(p: ProjectEntry) -> str:
        """Whether a session can open this project, in the words it declared.

        Its own column rather than a note on the source, because it answers a
        different question from the rest of the table: everything else says
        how far behind an upstream is, and this says what the next session
        can write. A registration that never asked reads as `—`, which is the
        same absence the launch acts on.
        """
        return p["mount"] if "mount" in p else "—"

    # Asked once per registration and read twice: a registration this
    # checkout owes no repository is neither located nor tabled as missing,
    # and its row says why rather than offering a fetch nobody should run.
    exempt = {p["name"]: exemption(p) for p in projects}

    def project_row(p: ProjectEntry, resolved: Upstream | None) -> list[str]:
        synced = p.get("last_synced_commit", "")
        synced_short = short_sha(synced) if synced else "never"

        if exempt[p["name"]]:
            return [p["name"], "—", "—", reach(p), exempt[p["name"]]]
        if p.get("ignore"):
            where = f"{resolved.checkout}" if resolved is not None else "(skipped)"
            return [p["name"], "—", "ignored", reach(p), where]

        if resolved is None:
            required = "required, " if p.get("required") else ""
            note = (
                f"{required}not cloned (run: sync fetch)"
                if transport_url(p)
                else f"{required}nowhere to read it from"
            )
            return [p["name"], "—", synced_short, reach(p), note]

        synced = checkpoint(p, resolved)
        synced_short = short_sha(synced) if synced else "never"

        try:
            behind: int | str = commit_count(
                str(resolved.checkout), synced, resolved.tip
            )
        except (sh.ErrorReturnCode, ValueError):
            behind = "?"
        source = f"{resolved.checkout} ({resolved.tip})"
        return [p["name"], str(behind), synced_short, reach(p), source]

    def worth_locating(p: ProjectEntry) -> bool:
        """Whether status asks where this project is, which costs git calls.

        A registration ignored for review is not located, because nothing
        below reads the answer -- unless the project declared it needs the
        repository present or reachable, which `ignore` says nothing about:
        being the upstream of this repository is a reason not to read its
        commits back and no reason at all to be out of reach. One this
        checkout owes nothing is never located: nothing below reads it.
        """
        if exempt[p["name"]]:
            return False
        return not p.get("ignore") or bool(p.get("required")) or "mount" in p

    # Located once and read twice: the table says where each project is, and
    # the requirements beneath it say which of the absences somebody declared
    # a need for. Locating runs several git commands per registration, so the
    # two readings share one pass rather than each making their own.
    located = [
        LocatedProject(
            entry=p, found=existing_upstream(p) if worth_locating(p) else None
        )
        for p in projects
    ]
    rows = [project_row(one.entry, one.found) for one in located]

    typer.echo()
    typer.echo(
        format_table(("Project", "Behind", "Last Synced", "Mount", "Source"), rows)
    )
    typer.echo()

    unmet = unmet_requirements(located)
    for requirement in unmet:
        typer.echo(requirement.spelled())
    if unmet:
        # Nonzero, because a requirement this machine has not answered is a
        # result and not a remark: every command that needs the checkout is
        # going to fail at the point of use, and a status that reported it and
        # exited clean is what let a fresh checkout look finished.
        typer.echo()
        raise typer.Exit(1)


@app.command("fetch")
def fetch_cmd(
    project: Annotated[
        str | None,
        typer.Argument(help="Project to materialize/refresh (default: all)"),
    ] = None,
) -> None:
    """Clone missing repos and fetch cached ones (network + writes).

    A fetch and nothing else: no branch a session may be working on is moved,
    and no working tree is reset. What keeps the review current is that it
    reads the remote-tracking ref this refreshes.

    This is where a fresh checkout materializes what the project declared it
    requires, including a registration whose *review* is ignored: `ignore`
    says the commits are not read back, and a project that needs the
    repository present needs it whether or not anybody reviews it. What the
    sweep leaves out is a registration this checkout owes nothing -- the
    scaffold's own lup entry, read inside the scaffold -- which would
    otherwise clone a second copy of the tree the command runs in.
    """
    projects = load_projects()
    targets = (
        [find_project(project)]
        if project
        else [
            p
            for p in projects
            if (not p.get("ignore") or p.get("required")) and owed_here(p)
        ]
    )
    failed = False
    for p in targets:
        try:
            resolved = ensure_local(p)
            typer.echo(f"{p['name']}: ready at {resolved.checkout}")
        except (typer.Exit, sh.ErrorReturnCode):
            failed = True
            typer.echo(f"{p['name']}: could not materialize", err=True)
    if failed:
        raise typer.Exit(1)


@app.command("log")
def show_log(
    project: Annotated[str, typer.Argument(help="Project name")],
    stat: Annotated[
        bool, typer.Option("--stat/--no-stat", help="Show file stats")
    ] = True,
) -> None:
    """List commits to review: everything upstream added since the last sync.

    This is the inventory ``/lup:update`` walks — the commits whose diffs get
    read (via ``sync diff``) and considered for porting. Fetches the upstream
    first so the list reflects where it now stands.
    """
    proj = find_project(project)
    found = ensure_local(proj)

    synced = checkpoint(proj, found)
    range_spec = f"{synced}..{found.tip}" if synced else found.tip

    args = ["log", "--oneline"]
    if stat:
        args.append("--stat")
    args.append(range_spec)

    output = git_in(str(found.checkout), *args)
    if output:
        typer.echo(output)
    else:
        typer.echo(f"No new commits since {short_sha(synced)}.")


@app.command("diff")
def show_diff(
    project: Annotated[str, typer.Argument(help="Project name")],
    commit: Annotated[str, typer.Argument(help="Commit SHA to show")],
) -> None:
    """Show full diff for a specific commit."""
    proj = find_project(project)
    found = ensure_local(proj)
    output = git_in(str(found.checkout), "show", commit)
    typer.echo(output)


@app.command("mark-synced")
def mark_synced(
    project: Annotated[str, typer.Argument(help="Project name")],
    at: Annotated[
        str,
        typer.Option(
            "--at", help="Record this commit as the checkpoint instead of HEAD"
        ),
    ] = "",
) -> None:
    """Share the reviewed checkpoint across this repository's worktrees.

    Run once a review is finished: it records that every commit up to the
    selected fetched tip has been considered, so the next ``sync log`` / ``status``
    only surfaces commits that land afterward. Marking synced even when nothing
    was ported is correct — it means "reviewed, decided to port none."

    This command does not fetch an already materialized upstream; ``--at``
    names the immutable tip actually reviewed. It also records a commit the project already consumed rather than the
    one the upstream is on now. A project adopting a library mid-stream knows
    which commit it took and has, without this, no way to say so: marking
    synced would silently claim every commit that landed afterward as
    reviewed, which is the opposite of what the checkpoint is for. The ref is
    resolved in the upstream checkout, so a tag or a branch name works and a
    commit that is not there is refused rather than written.
    """
    proj = find_project(project)
    found = existing_upstream(proj) or ensure_local(proj)

    head = resolved_checkpoint(str(found.checkout), at, found.tip)

    record_checkpoint(proj, found, head)
    typer.echo(f"Marked '{project}' as synced at {short_sha(head)}.")


@app.command("setup")
def setup_project(
    name: Annotated[str, typer.Argument(help="Project name")],
    path: Annotated[str, typer.Argument(help="Local path to the repo")],
    synced: Annotated[
        bool, typer.Option("--synced", help="Mark as already synced at current HEAD")
    ] = False,
    branch: Annotated[
        str,
        typer.Option("--branch", "-b", help="Branch to track (default: remote HEAD)"),
    ] = "",
    mount: Annotated[
        str,
        typer.Option(
            "--mount",
            help=(
                f"Open this project from a session: {' or '.join(MOUNT_MODES)}. "
                "Omitted, it is tracked and not reachable"
            ),
        ),
    ] = "",
    review_from: Annotated[
        Literal["", "remote", "local"],
        typer.Option(
            "--review-from",
            help="Review fetched remote commits or local unpublished work",
        ),
    ] = "",
) -> None:
    """Set the local path for a project (writes to sync.json.local).

    ``--mount`` is what makes the project *reachable* rather than merely
    tracked: a session opens it at this same path, inside the container as
    well as outside, and `refs/<name>` resolves there instead of dangling.
    Separate from registering it because the two are separate claims, and the
    one that hands a session the keys is the one worth typing out.
    """
    if mount and mount not in MOUNT_MODES:
        raise typer.BadParameter(f"--mount takes {' or '.join(MOUNT_MODES)}")
    resolved = Path(path).resolve()
    if not resolved.exists():
        typer.echo(f"Path does not exist: {resolved}")
        raise typer.Exit(1)

    repository = (resolved / ".git").exists() or bare_repository(resolved)
    # A directory that is not a checkout can still be worth reaching -- a
    # corpus, a set of reference material -- and the lease mounts one as a
    # plain bind. What it cannot do is be *reviewed*, which is what the rest
    # of this registry is for, so it is admitted only where the mount is the
    # point and refused where somebody meant to track commits it has none of.
    if not repository and not mount:
        typer.echo(
            f"Not a git repository: {resolved}\n"
            "Pass --mount to register it for access alone; tracking a project "
            "means reading its commits, and there are none to read here."
        )
        raise typer.Exit(1)

    local_data = load_json(local_file())
    local_projects = local_data.get("projects", [])

    entry = next((p for p in local_projects if p["name"] == name), None)
    if entry:
        entry["path"] = str(resolved)
    else:
        entry = ProjectEntry(name=name, path=str(resolved))
        local_projects.append(entry)
        local_data["projects"] = local_projects

    if branch:
        entry["branch"] = branch
    if review_from:
        entry["review_from"] = review_from

    if mount:
        entry["mount"] = "rw" if mount == "rw" else "ro"

    inherited = next((p for p in load_projects() if p["name"] == name), {})
    effective = PROJECT_ENTRY_ADAPTER.validate_python({**inherited, **entry})
    found = registered_upstream(effective, resolved)
    head = resolved_checkpoint(str(found.checkout), "", found.tip) if synced else ""
    if synced:
        record_checkpoint(effective, found, head)

    save_local(local_data)
    ensure_ref_symlink(name, str(found.checkout))
    typer.echo(f"Set '{name}' local path to {resolved}")
    if branch:
        typer.echo(f"  Tracking branch: {branch}")
    if mount:
        typer.echo(
            f"  Reachable from a session {'read-write' if mount == 'rw' else 'read-only'}"
            " — takes effect at the next launch, which is when mounts are built"
        )
        # The launch that opened this session recorded its own invocation, so
        # the registration can say which launch to repeat rather than leaving
        # the reader to reconstruct profile, sandbox, and flags from memory.
        launch = launched(measured_boundary(project_root()))
        if launch:
            spelled = " ".join(["uv", "run", "lup-devtools", *reopened(launch)])
            typer.echo(f"  Reopen this conversation to mount it: {spelled}")
    if synced:
        typer.echo(f"  Marked as synced at {short_sha(head)}")


@app.command("remote")
def set_remote(
    name: Annotated[str, typer.Argument(help="Project name")],
    url: Annotated[
        str,
        typer.Argument(help="The URL this machine fetches that repository from"),
    ],
) -> None:
    """Record how this machine reaches a repository (writes to sync.json.local).

    The machine's half of a registration, and the reason the committed half
    can hold a canonical URL at all. A forge serves one repository over two
    transports: a shared declaration names the https URL, and a machine whose
    keys are ssh reaches the same history at `git@host:owner/repo.git`. That
    is not a disagreement, and it has to be sayable without overwriting what
    every other machine reads.

    Checked against the shared declaration as it is written, the way `sync
    grant` exercises a device before recording it: a URL that names a
    *different* repository is refused here, where somebody is making the
    claim and can still fix it, rather than at the next fetch — or, worse,
    silently, by a clone of the wrong history being reviewed under this name.

    Where nothing shared names the repository, this is the whole registration:
    a project declared in the committed half by name alone is materialized
    from what this writes.

    A clone this machine already made, under the cache, is repointed with
    it: the transport a clone pushes over is its origin's, and a clone the
    shipped https entry materialized would otherwise go on pushing over https
    from the host, where no launch rewrites a remote, however this machine
    reaches the forge. Only ever onto another spelling of the same
    repository, which the check above has already established.
    """
    proj = find_project(name)
    declared = proj.get("url", "")
    if declared and not same_repository(url, declared):
        typer.echo(
            f"{url} does not name the repository '{name}' is registered as, "
            f"{declared} in {declaring_file(name, 'url').name}. A transport "
            "this machine reaches it over is expected here; a different "
            "repository under the same name would be reviewed, mounted and "
            "committed into as this one.\n"
            f"  - to reach {declared} from here, pass the URL that does.\n"
            f"  - to track {url} as well, register it under its own name.\n"
            f"  - to change which repository '{name}' means, "
            f"{renaming(name, url)}."
        )
        raise typer.Exit(1)

    local_data = load_json(local_file())
    local_projects = local_data.get("projects", [])
    entry = next((p for p in local_projects if p["name"] == name), None)
    if entry:
        entry["remote"] = url
    else:
        local_projects.append(ProjectEntry(name=name, remote=url))
        local_data["projects"] = local_projects
    save_local(local_data)

    typer.echo(f"'{name}' is fetched from {url} on this machine")
    if not declared:
        typer.echo(
            f"  Nothing in {sync_file().name} names the repository, so this is "
            "what identifies it here"
        )
    cached = cached_clone(name) if "path" not in proj else None
    if cached is not None:
        retransported(refuse_redirected_location(cached), url)
    if proj.get("required") and cached_clone(name) is None:
        typer.echo(f"  Run: uv run lup-devtools sync fetch {name}")


def retransported(clone: Path, url: str) -> None:
    """Point a clone's origin at ``url``, another spelling of what it holds.

    The spelling configured rather than the one git resolves to, since an
    ``insteadOf`` in force -- a contained session's own rewrites -- would read
    as a transport the clone does not have. Inside a session the shared
    `config` is read-only, so the refusal is said with the host command that
    makes the same change rather than raised.
    """
    held = git.out(
        "-C", str(clone), "config", "--get", "remote.origin.url", _ok_code=[0, 1]
    )
    if not held or held == url or not same_repository(held, url):
        return
    try:
        git("-C", str(clone), "remote", "set-url", "origin", url)
    except sh.ErrorReturnCode as error:
        typer.echo(
            f"  The clone at {clone} still reaches it over {held}: "
            f"{decode_stderr(error)}. From a host terminal: "
            f"git -C {clone} remote set-url origin {url}"
        )
        return
    typer.echo(f"  The clone at {clone} now fetches and pushes over it")


@app.command("grant")
def grant_device(
    device: Annotated[
        str,
        typer.Argument(help="A CDI device name, such as nvidia.com/gpu=all"),
    ],
) -> None:
    """Grant sessions on this machine a host device (writes to sync.json.local).

    Verified as it is made: a throwaway container is started with the device
    and nothing else, which proves the engine honours the CDI registry and a
    spec on this machine resolves the name -- the two halves that pass alone
    and fail together. Refused in the engine's own words otherwise, with what
    registers a spec, because the fix is made once on this machine and this
    is the moment somebody is making a claim about it.

    Written to the local file alone, like a ``--mount`` registration and for
    the same reason: which GPU a machine holds is that machine's fact, and
    the committed file is scaffold every adopter runs from.
    """
    try:
        granted = Device(name=device)
    except ValidationError as error:
        raise typer.BadParameter(
            f"{device!r}: a device is named the way CDI names it, "
            "vendor/class=device, such as nvidia.com/gpu=all"
        ) from error
    finding = verified_grant(granted)
    for notice in finding.notices():
        notice.say()
    if not finding.working:
        typer.echo(f"{granted.name} was not granted; see the check above.")
        raise typer.Exit(1)

    local_data = load_json(local_file())
    names = local_data.get("devices", [])
    if granted.name not in names:
        local_data["devices"] = [*names, granted.name]
        save_local(local_data)
    typer.echo(
        f"Granted {granted.name} to sessions on this machine — takes effect at "
        "the next launch, which is when devices are handed over"
    )
    launch = launched(measured_boundary(project_root()))
    if launch:
        spelled = " ".join(["uv", "run", "lup-devtools", *reopened(launch)])
        typer.echo(f"  Reopen this conversation to hold it: {spelled}")


@app.command("revoke")
def revoke_device(
    device: Annotated[
        str,
        typer.Argument(help="A granted device's CDI name, as `sync status` lists it"),
    ],
) -> None:
    """Take a device back from sessions on this machine (writes to sync.json.local)."""
    local_data = load_json(local_file())
    names = local_data.get("devices", [])
    if device not in names:
        typer.echo(
            f"{device} is not granted on this machine; `sync status` lists what is."
        )
        raise typer.Exit(1)
    local_data["devices"] = [name for name in names if name != device]
    save_local(local_data)
    typer.echo(f"Revoked {device}; the next launch opens without it")
