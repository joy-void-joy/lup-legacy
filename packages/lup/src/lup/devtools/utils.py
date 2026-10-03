"""Pre-configured shell commands and output helpers for devtools scripts."""

import json
import logging
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, NoReturn
from urllib.parse import urlsplit

import sh
import typer
from pydantic import BaseModel

from lup.execution.git import GitError, Repository
from lup.execution.shell import LazyCommand
from lup.execution.writability import diagnose_git_admin, inspect_git_admin
from lup.policy.kernel.diagnostic import Diagnostic, Step, diagnostic, rendered
from lup.sandbox.attribution import attribute_filesystem
from lup.sandbox.observed import observed_topology
from lup.sandbox.translation import MountTopology

logger = logging.getLogger(__name__)


# Git abbreviates object names to this many hex characters by default
# (`core.abbrev`), the same width as `git log --oneline`. Short shas are for
# human-readable display only; never parse or compare against them.
SHORT_SHA_LENGTH = 7


gh = LazyCommand("gh", tty_out=False)
uv = LazyCommand("uv")


def repository_segments(value: str) -> list[str]:
    """Every path segment a forge reference or a remote URL carries.

    One reading for every spelling a repository is written in: the
    ``owner/name`` a ``--repo`` takes, the ``host/owner/name`` gh also
    accepts, an ``https://`` URL, and the scp-like ``git@host:owner/name``
    that is not a URL and has no parser in the standard library. A trailing
    ``.git`` is framing rather than name, so it comes off first, and the
    colon of the scp form separates an authority from a path rather than a
    host from a port.

    Returned whole rather than trimmed to the interesting end, because how
    many segments there were is itself the answer to a question: a reference
    a caller typed is well-formed at two or three, and a remote URL is
    whatever the forge's paths look like.
    """
    trimmed = value.removesuffix(".git")
    if "://" in trimmed:
        parsed = urlsplit(trimmed)
        return [
            parsed.netloc,
            *(part for part in PurePosixPath(parsed.path).parts if part != "/"),
        ]
    parts = list(PurePosixPath(trimmed).parts)
    if len(parts) == 3:
        try:
            authority = urlsplit(f"//{parts[0]}")
            port = authority.port
        except ValueError:
            port = None
        else:
            if port is not None and authority.username is None:
                return parts
    # The scp form's colon separates the authority from its repository path.
    # lup: ignore[string-split] — no parser reads the scp form
    located = trimmed.partition(":")
    return [
        part
        for half in (located[0], located[2])
        for part in PurePosixPath(half).parts
        if part != "/"
    ]


def repository_reference(value: str) -> str:
    """The ``[host/]owner/name`` a forge reference or a remote URL points at.

    The pair is the last two segments in every shape a repository is written
    in, and the host — where one is written at all — is the segment before
    them. Userinfo in front of the host is addressing rather than identity,
    so it comes off too.

    Empty where fewer than two segments are there to read, which every caller
    takes as "this names no repository I can weigh" rather than as a match.
    """
    named = repository_segments(value)
    if len(named) < 2:
        return ""
    pair = "/".join(named[-2:])
    if len(named) < 3:
        return pair
    # lup: ignore[string-split] — userinfo is separated from a host by `@` and
    # by nothing else, which no URL parser reaches for a bare authority word
    return f"{named[-3].rpartition('@')[2]}/{pair}"


def names_same_repository(value: str, other: str) -> bool:
    """Whether two forge references point at one repository.

    Both have to be *well-formed* references first: `[host/]owner/name` and
    nothing longer, which is exactly what gh's ``--repo`` takes. Anything
    with more segments in it is refused rather than read from the end —
    otherwise `github.com/decoy/acme/widget` wears `acme/widget`'s name in
    the middle of itself and passes for it. A remote URL with a deeper path
    is read by :func:`repository_segments` for its own reasons and is not
    something a caller types here.

    Case-blind on both sides. A forge resolves an owner and a repository name
    without regard to case and DNS resolves a hostname the same way, so
    folding turns away nobody but the caller who meant this repository all
    along.

    A host is compared when *both* references name one, and ignored when
    either does not. That is the lever rather than an omission: a project on
    two forges writes `host/owner/name` and gets the discrimination, and one
    on a single forge writes the pair and needs none. The asymmetric reading
    — a named host on one side refusing a bare pair on the other — is the
    defect this whole surface exists to remove: it refuses the
    caller who wrote the *more* precise form, which teaches everybody to
    write the vaguer one.
    """
    named = PurePosixPath(repository_reference(value).casefold()).parts
    theirs = PurePosixPath(repository_reference(other).casefold()).parts
    if not 2 <= len(repository_segments(value)) <= 3:
        return False
    if not 2 <= len(repository_segments(other)) <= 3:
        return False
    if named[-2:] != theirs[-2:]:
        return False
    return len(named) < 3 or len(theirs) < 3 or named[-3] == theirs[-3]


def slug_from_remote(url: str) -> str:
    """The ``owner/name`` a remote names, empty when it names none.

    The pair alone, taken from the fuller reading
    :func:`lup.devtools.utils.repository_reference` gives, because the pair is
    what `gh --repo` takes and what this project passes it.

    Read there rather than here so one parser answers for every shape a
    remote is written in — the scp-like ``git@host:owner/name`` among them,
    whose host ssh resolves from its own config and which is not a URL and
    has no parser in the standard library. That shape is why this is read at
    all rather than left to `gh` to infer: a remote written through an SSH
    alias names no host `gh` recognizes, and every query then fails with "no
    known GitHub host" as though the repository were unreachable. The policy
    that judges a ``--repo`` reads the same parser, so the guard and the
    tooling that has to satisfy it cannot come to read one remote two ways.
    """
    named = PurePosixPath(repository_reference(url)).parts
    return "/".join(named[-2:]) if len(named) >= 2 else ""


def repository_slug() -> str:
    """The ``owner/name`` this checkout answers to, empty when unreadable."""
    origin = Repository(Path.cwd()).remote_url("origin")
    if origin is None:
        logger.warning("no origin remote to read a slug from")
        return ""
    return slug_from_remote(origin)


def repository_arguments() -> list[str]:
    """The ``--repo`` a `gh` query needs, or nothing where none is readable.

    Every `gh` subcommand infers its repository from the origin remote unless
    told, and that inference is what an alias defeats. Naming it once here is
    what stops a query depending on the spelling a checkout happens to use.

    Empty where no slug is readable, which is a project with no forge rather
    than a forge that could not be reached. The two want opposite answers from
    a caller — absence is a fact in the first and unknown in the second — so
    they are not collapsed here.
    """
    slug = repository_slug()
    return ["--repo", slug] if slug else []


def decode_stderr(e: sh.ErrorReturnCode) -> str:
    """Decode a failed ``sh`` command's stderr to trimmed text.

    ``sh`` captures stderr as raw ``bytes`` and exposes no decoded accessor,
    so callers that want a readable message decode it here; the trailing
    newline the failing tool printed with is framing, not message.
    """
    raw = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
    return raw.strip()


def attributed_stderr(
    e: sh.ErrorReturnCode, topology: MountTopology | None = None
) -> str:
    """A failed command's words, plus the boundary's where the boundary is why.

    `Read-only file system` is what the kernel says when a lease refuses a
    write, and it is indistinguishable from what it says about a genuinely
    read-only disk. :mod:`lup.sandbox.rail` argues that leaving it at that is
    worse than having no rail at all, because the reader debugs a filesystem
    instead of learning they hold a lease -- so where the mount table agrees
    that this boundary made the path unwritable, the account is appended to
    the tool's own message rather than replacing it. Both halves are wanted:
    the command's words say what it was doing, and the attribution says why it
    was refused.

    Where nothing attributes, this is :func:`decode_stderr` exactly. That is
    the common case and the one worth keeping cheap: a caller can reach for
    this in place of the plain decode without deciding first whether a
    boundary is involved.

    ``topology`` is read from the running system when a caller does not pass
    one, which is what every caller here wants and what no test can rely on:
    the machine's own mounts are not a fixture. Overridable so the judgement
    reaches its caller rather than being sealed inside the call.
    """
    reported = decode_stderr(e)
    observed = topology if topology is not None else observed_topology()
    account = attribute_filesystem(reported, observed, directory=str(Path.cwd()))
    return f"{reported}\n{account.sentence()}" if account.explains() else reported


def config_lock_diagnosis(cwd: Path | None = None) -> str:
    """Why git config writes cannot run here, empty when they can.

    The admin directories are asked of git rather than reconstructed from
    the checkout, and the ask still answers when every write is refused.
    """
    try:
        admins = Repository(cwd if cwd is not None else Path.cwd()).admin_dirs()
    except GitError:
        # No repository to diagnose: whatever the caller's git failure was,
        # the lock protocol is not what it tripped on.
        return ""
    return diagnose_git_admin(admins)


def clear_stale_config_locks(cwd: Path | None = None) -> Iterator[str]:
    """Remove every lock nothing is holding, naming each one removed.

    A confinement manufactures this debris — a sandboxed git dies mid-write
    and its lock outlives it on the host — so the run that can reach the
    filesystem is the one that has to clear it, and the next unconfined run
    is not sent hunting for a failure the previous one left. Nothing that
    declines removal is touched.
    """
    try:
        admins = Repository(cwd if cwd is not None else Path.cwd()).admin_dirs()
    except GitError:
        return
    for admin in admins:
        for obstruction in inspect_git_admin(admin):
            cleared = obstruction.clear()
            if cleared:
                yield cleared


def refuse_blocked_config_writes(cwd: Path | None = None) -> None:
    """Clear what is removable, and stop before a config write that still cannot run."""
    for cleared in clear_stale_config_locks(cwd):
        typer.echo(cleared)
    diagnosis = config_lock_diagnosis(cwd)
    if diagnosis:
        refuse(diagnosis)


class Refusal(typer.Exit):
    """A command's error exit, carrying the diagnostic it printed.

    An exit rather than an error the CLI renders itself, because Typer draws
    its own errors in a box, where a command meant to be copied picks up the
    border. Raised by :func:`refuse`, which prints first, so whoever catches
    one reads what was said as data in ``said``.
    """

    said: Diagnostic

    def __init__(self, said: Diagnostic, code: int = 1) -> None:
        super().__init__(code)
        self.said = said


def refuse(
    why: str,
    what: str = "",
    steps: Sequence[Step] = (),
    see: str = "",
    code: int = 1,
) -> NoReturn:
    """End the command: print what was caught, why and the ways through, then exit.

    The text is :func:`~lup.policy.kernel.diagnostic.rendered`, which a hook's
    refusal goes through too, so a refusal reads the same whether a hook or a
    command said it. ``what`` is the words that decided it, each step holds
    the command it names as the words that run it, and ``code`` is the exit.
    """
    said = diagnostic("error", why, what=what, steps=steps, see=see)
    typer.echo(rendered(said), err=True)
    raise Refusal(said, code)


def warn(why: str, what: str = "", steps: Sequence[Step] = (), see: str = "") -> None:
    """Say something the reader should know on stderr, in the same shape, and carry on."""
    typer.echo(
        rendered(diagnostic("warning", why, what=what, steps=steps, see=see)),
        err=True,
    )


def output_json(
    data: object,  # lup: ignore[bare-object] — pretty-printer: any serializable payload
) -> None:
    if isinstance(data, BaseModel):
        typer.echo(data.model_dump_json(indent=2))
    else:
        typer.echo(json.dumps(data, indent=2))


def short_sha(sha: str, length: int = SHORT_SHA_LENGTH) -> str:
    """Abbreviate a git object name for human-readable display.

    The single source of truth for how shas are shortened across devtools so
    every table and message uses one consistent width. Returns shorter input
    unchanged so already-abbreviated shas pass through.
    """
    return sha[:length]


def format_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[str]],
    aligns: Sequence[Literal["left", "right"]] | None = None,
) -> str:
    """Render rows as a column-aligned table sized to its own contents.

    Column widths come from the widest cell in each column, so no caller has
    to guess a fixed width that later clips real data. ``aligns`` picks left
    (default) or right justification per column; a trailing column gets no
    padding so variable-length tails (paths, messages) aren't padded out.
    """
    materialized = [list(row) for row in rows]
    widths = [len(h) for h in headers]
    for row in materialized:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def render(cells: Sequence[str]) -> str:
        last = len(cells) - 1

        def pad(i: int, cell: str) -> str:
            align = aligns[i] if aligns else "left"
            if align == "right":
                return f"{cell:>{widths[i]}}"
            return cell if i == last else f"{cell:<{widths[i]}}"

        return " ".join(pad(i, cell) for i, cell in enumerate(cells))

    header_line = render(headers)
    lines = [header_line, "-" * len(header_line)]
    lines.extend(render(row) for row in materialized)
    return "\n".join(lines)


VERSION_OPT = Annotated[
    str | None,
    typer.Option("--version", "-v", help="Agent version (default: current)"),
]
ALL_VERSIONS_OPT = Annotated[
    bool,
    typer.Option("--all-versions", help="Include all versions"),
]
JSON_OPT = Annotated[
    bool,
    typer.Option("--json", help="Output as JSON"),
]
