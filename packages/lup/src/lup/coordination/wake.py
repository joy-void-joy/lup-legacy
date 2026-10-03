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

Each transport is its runtime's adapter's to spell
(:mod:`lup.providers.claude.wake`, :mod:`lup.providers.codex.wake`), and
:func:`lup.providers.wake.wake` picks one by the path's runtime; this module
holds what every one of them answers in. An unavailable native route leaves
durable mail pending and reports why no wake was attempted. An owned stdio coordination server can relay its own mail
through the target's local queue. Queue acceptance does not prove an idle turn
started.
"""

from typing import Literal

from pydantic import BaseModel

type WakePriority = Literal["next", "now"]
"""When a woken session takes what the wake carries: at its next chance, or at once.

`now` is Claude's own frame priority, and what it does was measured on Claude
Code 2.1.285 in an interactive session, with the runtime's debug log as
witness: while the model is generating, the turn ends within milliseconds and
the frame's message is taken as the next turn; while a tool call runs, the call
runs to its end and the message is taken right after it, as `next` would be
taken at that boundary. `next` lets a generating turn finish first.
"""

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
