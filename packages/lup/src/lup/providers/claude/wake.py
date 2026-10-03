"""Claude Code's wake: one frame written into a session's own wake socket.

The frame shape is the runtime's own, spelled in its startup output, and it
names the session it is for, which a wake socket checks before delivering it.
"""

import json
import socket
from pathlib import Path

from lup.coordination.wake import Woken


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
