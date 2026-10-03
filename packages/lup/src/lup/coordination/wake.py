"""Making a peer look, when leaving mail for it is not enough.

Mail is the durable record and it is always written; a wake sits on top of it,
never instead of it. That ordering is the whole safety property here — a wake
that fails costs a peer some latency, and never a message.

Native transports have different execution boundaries. Codex's ``queue`` uses
the daemon selected by its configuration home, so a sender must share the
target's proven execution scope and select that home. Claude gives every
session a private Unix socket -- its wake socket -- and accepts a
newline-delimited JSON frame written to it, which arrives in that session as a
message and starts a turn — measured against a background session confirmed
idle, which answered a token it could only have read there, and measured again
through a socket moved by ``--messaging-socket-path``. The frame shape is the
runtime's own, spelled in its own startup output.

A frame also names the session it is for, and a wake socket drops one whose
name disagrees with its own — measured, by sending two frames at one live
session and watching only the one carrying its id arrive. That matters because
the runtime's own socket path is not unique the way a session is: every
contained session's default is ``/tmp/cc-socks/<pid>.sock``, and two
containers whose runtime is pid 1 or pid 7 in their own namespaces name the
same file. So the id travels with the path, and a nudge that reached the wrong
session is refused by it rather than delivered to it.
:mod:`lup.harness.wake_sockets` is where lup puts the sockets, keyed by member id,
so that case is rare rather than ordinary; this is what makes it harmless when
it happens anyway.

An unavailable native route leaves durable mail pending and reports why no
wake was attempted. An owned stdio coordination server can relay its own mail
through the target's local queue. Queue acceptance does not prove an idle turn
started.
"""

import json
import socket
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from lup.execution.shell import LazyCommand
from lup.coordination.bare.scope import execution_scope

# lup: ignore[constant-declaration] — env overlays the target home while retaining native process discovery settings
QUEUE_COMMAND = LazyCommand("env")

type WakeRuntime = Literal["", "claude", "codex"]
"""Which runtimes a member can declare a wake path for, named once.

Named rather than spelled at the field, because a fold reading a member back
off disk has to narrow a string to exactly this set, and a second spelling of
the set is a second place a runtime would have to be added.
"""


class WakePath(BaseModel, frozen=True):
    """How one member can be made to look, and by what.

    A runtime and a handle rather than a command line, because what the handle
    means is the runtime's own: one addresses a conversation the provider's CLI
    knows by name, the other names a socket on this filesystem. A caller handed
    a command line would have no way to tell those apart, and no way to report
    which of them failed.
    """

    runtime: WakeRuntime = ""
    """Which runtime's path this is, empty where the member declared none.

    Empty is the honest default. A session that never said how to reach it
    still has a durable mailbox, and a sender is told that nothing will wake it
    rather than being told a wake was attempted.
    """

    handle: str = ""
    """What that runtime reaches this member by, in its own spelling.

    A thread id or session name for Codex, which ``codex queue --thread`` takes
    verbatim. For Claude it is the filesystem path of the session's own wake
    socket, which the launcher places, keyed by the member's id, and the
    session binds.
    """

    session: str = ""
    """Which session the handle belongs to, where the runtime checks it.

    Claude's own id for the session, which it compares against a frame's
    ``session_id`` and drops the frame on a mismatch. Carried beside the
    handle rather than folded into it because it answers a different question:
    the handle says where to write, and this says who has to be there for the
    write to count. Empty asks for no check. Codex's native hook records its
    session id too; the queue targets the handle directly.
    """

    home: str = ""
    """Native configuration home recorded inside the target session's boundary."""

    scope: str = ""
    """Execution identity proving this process can address that home and daemon."""

    @property
    def receiver_local(self) -> bool:
        """Whether this transport can relay durable mail from its own boundary."""
        return self.runtime == "codex"


def declared_wake(
    runtime: str, handle: str, session: str = "", home: str = "", scope: str = ""
) -> WakePath:
    """One member's wake path as a fold read it off the record.

    Narrowing rather than validating, because this is the read path a listing
    goes through: a runtime nobody here can reach reads as none declared, which
    costs that member a nudge and never the listing it appears in.
    """
    match runtime:
        case "claude" | "codex":
            return WakePath(
                runtime=runtime, handle=handle, session=session, home=home, scope=scope
            )
        case _:
            return WakePath(handle=handle, session=session)


class Woken(BaseModel, frozen=True):
    """What happened when somebody tried to make a member look.

    Native acceptance is distinct from a peer reading mail. Missing or
    unreachable routes leave the durable record waiting for the peer.
    """

    reached: bool
    """Whether the native socket or queue accepted the message.

    Queue acceptance alone does not prove that an idle session started a turn.
    """

    reason: str = ""
    """Why nothing happened, empty where something did."""

    error_type: str = ""
    """Safe failure category for diagnostics that must not echo native arguments."""


def wake(
    path: WakePath,
    message: str,
    cwd: Path | None = None,
    *,
    queue_timeout_seconds: float = 20.0,
) -> Woken:
    """Make one member look at what is waiting.

    Never raises on a failed wake. The mail is already written by the time
    anything calls this, so a runtime that is missing, a session that has since
    exited, or a handle that no longer resolves all leave the record intact and
    the peer merely un-nudged — which is the state a member with no wake path
    is in permanently and which the system is built to tolerate.
    """
    match path.runtime:
        case "codex" if path.handle:
            return queued(path, message, cwd, queue_timeout_seconds)
        case "claude" if path.handle:
            return injected(Path(path.handle), message, path.session)
        case _:
            return Woken(
                reached=False,
                reason=(
                    "this member declared no wake path, so the mail waits until"
                    " it next looks"
                ),
            )


def injected(
    address: Path, message: str, session: str = "", patience: float = 3.0
) -> Woken:
    """Write one message into a Claude session's own wake socket at *address*.

    One frame, carrying the message as the session's own user turn, because
    that is what makes an idle session take a turn rather than merely record
    something. The authentication frame the runtime's help describes is left
    off: the socket accepts a message without one, and the token that frame
    would carry belongs to the receiving session and is published only for
    some of them, so requiring it here would make the wake work for a subset
    of peers and fail silently for the rest.

    *session* is the id the receiving socket checks the frame against, and
    omitting it asks for no check. It is the difference between reaching a
    path and reaching a member: the paths a runtime chooses collide across pid
    namespaces and members do not, so a frame that names its session is
    dropped by whoever else has bound that path rather than delivered by them.

    *patience* bounds the write rather than leaving it to the kernel, because
    a socket file can outlive the process that bound it, and the worst this
    call is allowed to cost is a peer that stays un-nudged.

    A process that may not open a Unix socket at all is told so apart from a
    peer that is not listening. Measured in a Claude Code Bash sandbox, where
    the socket itself is refused with EPERM before any address is tried, and
    reporting that as nobody listening sent the reader looking for a dead
    peer rather than at the boundary the call ran inside.
    """
    frame = {
        "type": "user",
        "message": {"role": "user", "content": message},
        **({"session_id": session} if session else {}),
    }
    try:
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as refused:
        return Woken(
            reached=False,
            reason=(
                f"this process may not open a Unix socket ({refused}), so no "
                "wake socket is reachable from here; the mail waits for the peer"
            ),
            error_type="UnixSocketRefused",
        )
    try:
        with peer:
            peer.settimeout(patience)
            peer.connect(str(address))
            peer.sendall(json.dumps(frame).encode() + b"\n")
    except OSError as failure:
        return Woken(
            reached=False,
            reason=f"nothing is listening at {str(address)!r}: {failure}",
        )
    return Woken(reached=True)


def queued(
    path: WakePath, message: str, cwd: Path | None = None, timeout: float = 20.0
) -> Woken:
    """Hand a message to Codex's queue; reached means the queue accepted it.

    Acceptance does not establish that an idle session started a turn.
    """
    if not path.home or not Path(path.home).is_absolute() or not path.scope:
        return Woken(
            reached=False,
            reason="Codex wake has no verified target home and execution scope; durable mail remains pending.",
            error_type="UnboundNativeRoute",
        )
    if path.scope != execution_scope():
        # lup: Add an owned execution bridge before supporting Codex wake across container boundaries.
        return Woken(
            reached=False,
            reason="Direct Codex wake cannot cross this execution boundary; durable mail remains pending for the peer's owned mailbox relay or its next activity.",
            error_type="ForeignExecutionScope",
        )
    try:
        QUEUE_COMMAND(
            f"CODEX_HOME={path.home}",
            "codex",
            "queue",
            "--thread",
            path.handle,
            "--message",
            message,
            _cwd=str(cwd) if cwd else None,
            _timeout=timeout,
        )
    except Exception as failure:
        return Woken(
            reached=False,
            reason=f"codex queue did not reach {path.handle!r}: {failure}",
            error_type=type(failure).__name__,
        )
    return Woken(reached=True)
