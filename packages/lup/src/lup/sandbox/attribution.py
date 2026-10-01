"""Telling a boundary refusal apart from a bug, and refusing to guess.

A confined command that fails fails in the vocabulary of whatever it was
doing. A write the mount table refused arrives as ``Read-only file system``,
a host the proxy refused arrives as a timeout or a 403, and neither says
"you are confined". An agent reading those debugs the filesystem or the
network library, which is the wrong thing, for as long as it takes somebody
to notice.

So the boundary is asked to speak for itself. Two attributions are exact
where it matters and both work from a source that already exists: a failing
path is compared against the mount topology the container was built from, and
a refused host is read out of the proxy's own log rather than out of the
client's guess about why its connection died.

The discipline that makes this worth having is the refusal to guess. A marker
in the text never suffices on its own -- ``Read-only file system`` appears for
a genuinely read-only disk too -- so a claim is made only when the topology or
the log agrees, and everything else is reported as *unattributed*. That is not
modesty. A wrong boundary claim is worse than none: it teaches an agent to
reach for the host when the bug is in its own code, and that lesson outlives
the one command it was wrong about.
"""

from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Discriminator, Field

from lup.sandbox.models import Mount
from lup.sandbox.translation import MountTopology

WRITE_REFUSAL_MARKERS: tuple[str, ...] = (
    "Read-only file system",
    "Permission denied",
    "Operation not permitted",
    "Device or resource busy",
    "could not lock config file",
)
"""How a kernel says a write was refused, in the words a caller will see.

A default rather than a constant: these are what Linux and the tools above it
say, and a project on another platform -- or one whose toolchain wraps them --
has different words for the same event. The fourth is what a bind-mounted
*file* answers: a mount point cannot be unlinked, so a tool that replaces a
file by renaming over it -- git, every editor that writes atomically --
reports the mount as busy rather than read-only. The last is git wrapping
the same refusal and dropping the errno: `git branch -D` removing a branch's
section from a read-only `config` says only that it could not lock the
file, then deletes the branch and exits 0. Never sufficient alone; the
topology has to agree before anything is claimed.
"""

PROXY_DENIAL_MARKER = "TCP_DENIED"
"""What the proxy writes on the line where it refused a request."""

PATH_PUNCTUATION = "'\"`:,;()[]<>"
"""What a diagnostic wraps a path in when it mentions one."""


class FilesystemRefusal(BaseModel, frozen=True):
    """The boundary refused a write, and the mount table says which mount did."""

    kind: Literal["filesystem"] = "filesystem"
    path: str
    mount: Mount = Field(
        description=(
            "The read-only mount covering the path. Required, and that is the "
            "whole guarantee: this type cannot be constructed for a refusal "
            "no declared mount explains"
        ),
    )

    def explains(self) -> bool:
        """Whether this verdict accounts for the failure it was asked about."""
        return True

    def sentence(self) -> str:
        """What to tell the agent, naming the boundary rather than the errno."""
        return (
            f"The boundary refused {self.path}: it is under {self.mount.source} "
            f"mounted read-only at {self.mount.container_path}. This is "
            "confinement, not a broken filesystem -- that mount is read-only "
            "on purpose, and the remedy is to write within your own tree "
            "rather than to retry or change permissions."
        )


class EgressRefusal(BaseModel, frozen=True):
    """The proxy refused a host, and its own log is where that was read."""

    kind: Literal["egress"] = "egress"
    host: str

    def explains(self) -> bool:
        return True

    def sentence(self) -> str:
        return (
            f"The boundary refused the connection to {self.host}: the egress "
            "proxy denied it, which is why this reads as a timeout or a "
            "connection error rather than as a refusal. Nothing is wrong with "
            "the network -- add the host to the egress declaration if it "
            "belongs there."
        )


class Unattributed(BaseModel, frozen=True):
    """Nothing here ties this failure to the boundary, said out loud.

    The common case, and the one worth being careful about. Reported rather
    than passed over in silence so a caller can say "this failed, and the
    boundary is not why" -- which is a real answer, and the one that stops an
    agent going looking for a wall that was not involved.
    """

    kind: Literal["unattributed"] = "unattributed"
    detail: str = ""

    def explains(self) -> bool:
        """No. Which is the answer every caller has to be able to get cheaply.

        The one method the union exists to be asked. A caller deciding
        whether to say anything at all asks here rather than testing what
        type it holds, so a fourth kind of attribution added later is
        answered by writing this method rather than by finding every place
        that enumerated the first three.
        """
        return False

    def sentence(self) -> str:
        return (
            "This failure is not attributable to the boundary: no refused "
            "path matched the mount table and no host matched a proxy denial. "
            "Debug it as ordinary behaviour of the command."
        )


type Attribution = Annotated[
    FilesystemRefusal | EgressRefusal | Unattributed, Discriminator("kind")
]


def unquoted(word: str, punctuation: str = PATH_PUNCTUATION) -> str:
    """One word of a diagnostic, with the punctuation it was quoted in removed.

    An error message is prose with a path in it, and prose has no parser --
    which is why this trims rather than parses. Kept as its own function so
    that reasoning sits beside the one line it excuses instead of hovering
    over the comprehension that calls it.
    """
    # lup: ignore[string-strip] — the quotes a diagnostic wraps a path in are
    # exactly what has to come off, and no parser reads free-form prose
    return word.strip(punctuation)


def candidate_paths(failure: str, directory: str = "") -> list[str]:
    """Every path the failure text mentions, in the order it did.

    Deliberately crude, and crude in the safe direction: these are only
    *candidates*, and the mount table decides which of them means anything. A
    token that merely looks like a path and is under no mount contributes
    nothing, so over-collecting costs a lookup and never a wrong claim.

    ``directory`` is where the command ran, and it is what reaches a relative
    one. A relative candidate is taken only where the diagnostic put it in
    quotes: the crudeness that is safe for an absolute path is not safe for a
    bare word, which under a read-only checkout would make every noun in the
    sentence a claim about the mount.
    """
    # lup: the compiled twin in lup.policy.assets.host still takes only absolute candidates, so a git diagnostic naming a path relative to the checkout -- "unable to unlink old 'README.md': Device or resource busy" -- reaches nothing inside the dispatcher, which is where the refusal is met
    quoted = [
        stripped
        for word in failure.split()
        for stripped in [unquoted(word)]
        if stripped and word != stripped and not stripped.startswith("/")
    ]
    return [
        *[
            stripped
            for word in failure.split()
            for stripped in [unquoted(word)]
            if stripped.startswith("/") and len(stripped) > 1
        ],
        *[str(PurePosixPath(directory) / name) for name in quoted if directory],
    ]


def read_only_mount(topology: MountTopology, path: str) -> Mount | None:
    """The read-only mount covering this container path, if one does.

    Longest match wins, the same way translation resolves a path: a writable
    tree nested inside a read-only parent is the more specific answer, and
    reporting the parent would name a refusal that is not the one that
    happened.
    """
    requested = PurePosixPath(path)
    covering = [
        mount
        for mount in topology.mounts
        if requested.is_relative_to(PurePosixPath(mount.container_path))
    ]
    if not covering:
        return None
    closest = max(covering, key=lambda m: len(PurePosixPath(m.container_path).parts))
    return closest if closest.mode == "ro" else None


def attribute_filesystem(
    failure: str,
    topology: MountTopology,
    markers: tuple[str, ...] = WRITE_REFUSAL_MARKERS,
    directory: str = "",
) -> Attribution:
    """Attribute a write refusal to the mount that caused it, or to nothing.

    Both halves have to agree. The marker says a write was refused, which is
    also true of a genuinely read-only disk; the topology says this path is
    one *this boundary* made unwritable. Only together do they justify telling
    an agent that confinement is the reason, and either alone is a claim the
    agent has no way to check.

    A refused path under *no* mount is deliberately not claimed, and a first
    draft that did claim it was caught by its own test attributing
    ``mount: /dev/sda1: Read-only file system`` to this container. The reason
    it was wrong twice over: a write to an unmounted path inside a container
    ordinarily succeeds, into the container's own filesystem, so a refusal
    there is not the mount table's doing at all -- and the branch matched
    every path the table never mentioned, which is most paths on the machine.

    ``directory`` is where the command ran, which is what resolves the paths
    git names relative to the checkout. A caller that does not know where it
    ran passes nothing and gets the absolute reading alone.
    """
    if not any(marker in failure for marker in markers):
        return Unattributed(detail=failure)
    for path in candidate_paths(failure, directory):
        mount = read_only_mount(topology, path)
        if mount is not None:
            return FilesystemRefusal(path=path, mount=mount)
    return Unattributed(detail=failure)


def requested_host(line: str) -> str:
    """The host a proxy log line was about, or nothing when it names none.

    Parsed rather than pattern-matched: a squid access line carries the target
    as a full URL for an ordinary request and as a bare ``host:port`` for a
    tunnel, and ``urlsplit`` reads both once the second is given the ``//``
    that makes it an authority.
    """
    for word in line.split():
        if word.startswith(("http://", "https://")):
            return urlsplit(word).hostname or ""
        try:
            authority = urlsplit(f"//{word}")
            if authority.port is not None:
                return authority.hostname or ""
        except ValueError:
            continue
    return ""


def attribute_egress(
    proxy_log: list[str], marker: str = PROXY_DENIAL_MARKER
) -> list[EgressRefusal]:
    """Every host the proxy refused, read from the lines it wrote itself.

    From the proxy's log rather than from the client's error, because the two
    know different things. The client knows its connection did not complete
    and guesses at why; the proxy knows it denied a named host, which is the
    fact worth reporting. A denial that never reached the proxy leaves no line
    here and is correctly attributed to nothing at all.
    """
    return [
        EgressRefusal(host=host)
        for line in proxy_log
        if marker in line
        for host in [requested_host(line)]
        if host
    ]
