"""Where lup puts its sessions' wake sockets, so one can reach another.

Claude Code gives every session a private Unix socket and takes a
newline-delimited JSON frame written to it, which arrives there as a message
and starts a turn. That socket is the session's **wake socket**: what
:mod:`lup.coordination.wake` writes to when a peer is to look, and the only
thing between a nudge and an idle peer is whether the waking process can open
the file. It holds no mail -- a message is the coordination store's, read
whether or not anything woke its reader -- which is why it is not called an
inbox.

A contained session cannot open it, left alone. Measured:
``/proc/self/mountinfo`` carries no mount for ``/tmp``, so each session's
socket directory is its own. Worse than invisible -- the runtime's own default
is ``/tmp/cc-socks/<pid>.sock``, and a session that is pid 7 in its own
namespace names a path that exists, live, and belongs to somebody else in every
sibling container.

So lup places the sockets itself: the launcher passes
``--messaging-socket-path`` naming a file under this directory, one bind mount
arrives there, and a member's declared wake socket is a path every peer can
open and exactly one session owns.

**The path is keyed by the member's id, which is what makes it one member's.**
The id is minted once and never moves; a display name repeats by design --
``dev``, ``dev-2`` -- and changes whenever its session renames itself. A path
keyed by the name met the stale socket of an earlier session called the same,
and moved out from under every peer holding it at a rename. The name is for a
person to read and stays off the path.

**A socket file is removed only where the roster says its owner is gone.** A
crashed session leaves its file behind, and an id is never minted twice, so a
left file blocks nobody: removing it is housekeeping, done at a launch for
the departed members of that launch's repository, and only where nothing
still answers on the file -- the roster reads a lapsed pulse as a departure,
and a process can outlive its pulse.

**The mount is path-preserving, which is the design rather than a
convenience.** The path is a datum that travels -- a member declares what it
bound and a peer in another container opens that same string -- so a source
and target that disagreed would leave every handle right inside the session
that wrote it and wrong everywhere it was read.

**This is deliberately not a directory the runtime scans.** Its own peer
discovery matches a socket directory against four anchored patterns --
``/tmp/cc-socks``, its ``/private/tmp`` and Termux spellings, and
``/run/user/<uid>/cc-socks`` -- and a directory lup names matches none of
them. Measured: a session launched with the flag bound here and left
``/tmp/cc-socks`` holding only the launcher's own socket. That is the whole
difference between this and mounting the runtime's own directory, which would
make every session on the machine natively reachable by every other through a
file channel :mod:`lup.policy.kernel.peers` cannot see, since that guards tool
calls rather than files.

What this does grant is worth naming rather than burying: any process that can
open these files can start a turn in any session that bound one. Two things
bound it. The record is always written before anything is nudged, so a wake
that goes astray costs latency and never a message; and a wake carries the
session id of the member it is for, which the receiving socket checks against
its own and drops on a mismatch -- so a frame that reached the wrong session is
refused by it rather than delivered.
"""

import errno
import hashlib
import logging
import socket
from pathlib import Path

from pydantic import BaseModel, Field

from lup.harness.notice import Notice

logger = logging.getLogger(__name__)


class WakeSockets(BaseModel, frozen=True):
    """The one directory this machine's sessions bind their wake sockets in.

    One model because three places have to agree on the same string: the
    launcher makes the directory and names a file in it on the command line,
    the image mounts it, and a peer reads the path back off the roster. Split
    up, a launch mounts somewhere the session does not bind and the failure is
    a peer that never looks while everything reports success.
    """

    directory: str = Field(
        default="/tmp/lup-wake",
        description=(
            "Where every session this launcher starts binds its wake socket, "
            "on the host and under that same path inside a container. "
            "Constrained rather than free: the runtime refuses a socket "
            "directory that is a symlink, that it does not own, or that is not "
            "mode 0700, and refuses the address outright past about 104 bytes "
            "-- which is why this stays short and shallow rather than living "
            "beside the checkout it serves. Emptying it declares sessions that "
            "reach each other only where the runtime's own default path "
            "already does, which is the posture every launch had before this "
            "existed"
        ),
    )

    longest_address: int = Field(
        default=103,
        description=(
            "The longest socket path, in bytes, a placed wake socket may take. "
            "A Unix socket address holds 108 bytes on Linux and 104 on macOS, "
            "each counting the terminating zero, and the runtime refuses a "
            "path past about 104 outright -- so the default is the shorter "
            "platform's, and a repository name that would run past it is cut "
            "rather than bound somewhere else"
        ),
    )

    def socket(self, repository: Path, member_id: str) -> str:
        """The wake socket one member of *repository* binds, keyed by its id.

        By the id rather than by anything else a member has, because the id
        is minted once and names one member for as long as the file exists.
        A pid is the one thing that does not survive the container boundary
        this directory exists to cross, and a display name repeats by design
        and moves at every rename, so neither enters the path.

        After the repository too -- *repository* is the git directory its
        worktrees share -- as a readable name and a digest of its path,
        ``<repository>-<digest>--<member id>.sock``, because this directory
        is the machine's and a roster is one repository's: the name says
        which roster answers for the file's owner. A repository name that
        would run past :attr:`longest_address` is cut to fit, and the digest
        and the id never are, since a cut through the id would hand two
        members one path. A directory so long that not even they fit is
        refused rather than placed.
        """
        label = repository.name.removesuffix(".git") or repository.parent.name
        digest = hashlib.sha256(str(repository).encode()).hexdigest()[:8]
        keyed = f"{digest}--{member_id}.sock"
        room = self.longest_address - len(f"{self.directory}/-{keyed}".encode())
        if room < 0:
            raise ValueError(
                f"a wake socket in {self.directory} cannot carry member "
                f"{member_id}'s whole id within {self.longest_address} bytes; "
                "declare a shorter directory"
            )
        kept = label.encode()[:room].decode(errors="ignore")
        return str(Path(self.directory) / (f"{kept}-{keyed}" if kept else keyed))

    def serve(self) -> Path | None:
        """Make the directory, and answer with it.

        Made here rather than left to whoever mounts it, because a bind mount
        whose source does not exist is one the engine refuses the whole
        container for, and because the runtime checks the mode before it binds
        rather than fixing it.

        Nothing comes back when it cannot be made, which is a launch whose
        sessions do not nudge each other rather than a launch that fails: the
        mail is untouched and the notice says so.
        """
        if not self.directory:
            return None
        directory = Path(self.directory)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
        except OSError as error:
            # Logged rather than swallowed, for the reason the clipboard's is:
            # a session nobody can nudge is an ordinary outcome, and "why" is
            # the difference between a machine that cannot have one and a path
            # this got wrong.
            logger.warning("the wake socket directory did not open: %s", error)
            return None
        return directory

    def retire(
        self, repository: Path, member_id: str, handle: str, patience: float = 1.0
    ) -> bool:
        """Remove a departed member's wake socket, where it is provably theirs and dead.

        Asked only about a member the roster says is gone, which is the
        caller's to read: this module reads no roster. What is checked here
        is the rest. *handle* has to be the path this member's id keys,
        because any other path is not provably theirs -- a session nothing
        placed declares the runtime's pid-named default, which a session in
        another namespace may be listening on. And nothing may answer there,
        because the roster reads a lapsed pulse as a departure and a process
        can outlive its pulse: a machine back from sleep has every session
        minutes stale and every one still listening, and removing a socket its
        session listens on leaves that session unreachable for the rest of its
        life while it reports nothing wrong. A connection opened and closed
        with no frame written is no message, so the probe costs a live session
        nothing.

        Where this process may not open a Unix socket to ask -- a Claude Code
        Bash sandbox refuses ``socket(AF_UNIX)`` with EPERM -- the file is left
        and the reason logged: its path is never placed again, so leaving it
        costs an inode, where guessing could cost a live session its wake.

        Answers whether a file was removed.
        """
        if handle != self.socket(repository, member_id):
            return False
        address = Path(handle)
        if not address.is_socket():
            return False
        try:
            opened = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except OSError as refused:
            logger.info(
                "left the wake socket %s: this process may not open a Unix "
                "socket to ask whether its session still listens (%s)",
                address,
                refused,
            )
            return False
        with opened as probe:
            probe.settimeout(patience)
            answered = probe.connect_ex(handle)
        if answered != errno.ECONNREFUSED:
            return False
        address.unlink(missing_ok=True)
        return True

    def notice(self, serving: bool) -> list[Notice]:
        """What an operator is told about their sessions being able to nudge each other.

        Said either way, because both answers change what the reader does
        next. One who does not know it is there will route a nudge through the
        person; one who does not know it is absent reads a peer that never
        looks as the coordination store being broken, and goes looking in the
        wrong half.
        """
        if not serving:
            return [
                Notice(
                    text=(
                        "peer wake: this session's wake socket is out of its "
                        "peers' reach — mail still lands in the record, and "
                        "waits until the peer it is for next looks"
                    ),
                    urgency="boundary",
                )
            ]
        return [
            Notice(
                text=(
                    f"peer wake: sessions placed in {self.directory} can nudge "
                    "one another into reading their mail; what they say to "
                    "each other still goes through the recorded stream"
                ),
                urgency="boundary",
            )
        ]
