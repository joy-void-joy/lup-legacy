"""What a runtime gives a session to be known by, asked of the adapter that knows.

A tool server started by a native runtime is the first process of its session
to run, and it is handed nothing about the session but the runtime's name.
The identity a session joins the roster under is the launcher's where one
minted it, and otherwise whatever the runtime itself gave the process — a
fact spelled differently by each runtime, or not at all, so each adapter
answers for its own and this asks the right one.

Blank is an answer. A runtime that hands its servers no session id leaves an
unlaunched session with nothing to join under, and the tool server serves no
coordination verbs rather than inventing a member on every call or answering
to a name every session in the worktree would share.
"""

from lup.coordination.wake import WakePath
from lup.providers.claude.identity import (
    CLAUDE_SESSION_ENV,
    CLAUDE_WAKE_SOCKET_ENV,
    claude_session_id,
    claude_wake,
)
from lup.providers.codex.identity import codex_session_id, codex_wake


# lup: ignore[constant-declaration] — each member is a runtime's own spelling,
# taken by reference from the adapter that owns it; no project could choose
# differently and still read the value that runtime set
RUNTIME_DECIDED_ENV: list[str] = [CLAUDE_SESSION_ENV, CLAUDE_WAKE_SOCKET_ENV]
"""What a runtime tells a session's own processes about that session.

Distinct from the launcher's own variables, which say where a process was
put: these are set by the runtime for the children it starts, and a process
reading one learns which session it belongs to. Declared at the seam because
only the adapters know their runtime's spellings, and taken by reference for
the reason the launcher's list is -- a second spelling is a second place a
variable has to be added, and the one that gets missed is how a suite comes
to measure the session running it.

Codex contributes none: it documents no variable carrying a session's
identity to a server it starts, which is the asymmetry
:mod:`lup.providers.codex.identity` states.
"""


def native_session_id(runtime: str) -> str:
    """The session id *runtime* set for this process, or blank where it set none."""
    match runtime:
        case "claude":
            return claude_session_id()
        case "codex":
            return codex_session_id()
        case _:
            return ""


def native_session_ids() -> list[str]:
    """Every id a runtime set for this process's session, whichever runtime started it.

    For a process that cannot say which runtime it runs under -- a command a
    session's shell started -- so each adapter is asked, and only the one
    whose runtime started it has set anything.
    """
    return [
        identity for identity in (claude_session_id(), codex_session_id()) if identity
    ]


def native_wake(runtime: str, cli_name: str) -> WakePath:
    """How *runtime* would have this session made to look, where anything can.

    Asked of the adapter for the same reason the session id is: what wakes a
    session is one runtime's own arrangement -- a name a tool inside another
    session resolves, or a thread a command takes -- and a caller that knew
    one of those spellings would answer wrongly for the other.

    *cli_name* is what the launcher called this session, which one adapter
    hands its runtime and the other has nowhere to put. Passing it to both
    keeps the asking here rather than making this module know which of them
    has a use for it.
    """
    match runtime:
        case "claude":
            return claude_wake(cli_name)
        case "codex":
            return codex_wake(cli_name)
        case _:
            return WakePath()
