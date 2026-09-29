# lup: ignore[constant-declaration]
# Each variable's name is Claude Code's own spelling, which no project could
# choose differently and still read the value the runtime set; they are
# written here and nowhere outside this adapter.
"""The id Claude Code gives a session, as the tool servers it starts can read it.

A launcher that minted a member id exports it, and that is the strongest
identity a session has. A session nobody launched — a bare ``claude`` in a
worktree, with the plugin — has only what its runtime gives it, and Claude
Code gives the same session id to two of the processes that write this
session's roster row: its hooks read it on stdin as ``session_id``, and its
stdio MCP servers find it in their environment under the name below. Reading
it here is what lets the tool server and the hooks agree on one row without a
launcher.

Measured rather than documented. Claude Code 2.1.272 sets the variable in
the environment of the stdio MCP servers it starts, carrying the session's
id — read off a running coordination server on the machine this was written
on — while https://code.claude.com/docs/en/env-vars and
https://code.claude.com/docs/en/hooks document ``CLAUDE_PROJECT_DIR`` for
MCP servers and neither of these names. A release that stops setting it
costs an unlaunched session its identity and nothing else: the launched
case never reads it.
"""

from pydantic import Field
from pydantic_settings import BaseSettings

from lup.coordination.wake import WakePath

CLAUDE_SESSION_ENV = "CLAUDE_CODE_SESSION_ID"
CLAUDE_WAKE_SOCKET_ENV = "CLAUDE_CODE_MESSAGING_SOCKET"
CLAUDE_ENV_PREFIXES = ("CLAUDE_", "CLAUDECODE")
"""Every name Claude Code exports to the processes a session starts begins so."""


class ClaudeSessionEnv(BaseSettings):
    """The runtime's half of an unlaunched session's identity, read from the environment."""

    session_id: str = Field(default="", validation_alias=CLAUDE_SESSION_ENV)
    wake_socket: str = Field(default="", validation_alias=CLAUDE_WAKE_SOCKET_ENV)


def claude_session_id() -> str:
    """The session id Claude Code set for this process, or blank where it set none."""
    return ClaudeSessionEnv().session_id


def claude_wake(cli_name: str) -> WakePath:
    """The path of this session's own wake socket, which is what wakes it.

    Read from the environment rather than derived. The session binds the
    socket and the launcher only asks where; a path this adapter computed
    would be a second opinion about a file exactly one process created, and
    wrong for every session whose socket was placed somewhere else. The
    runtime sets this variable for the processes a session starts, which is
    what lets a tool server report its own session's wake socket without
    being told what it is.

    *cli_name* is accepted and unused, because what wakes a Claude session is
    a path on this filesystem rather than a name: the roster's name reaches
    the session through a tool another session holds, and the wake does not
    go that way.

    The session id travels beside the path, because the receiving socket
    checks a frame against its own id and drops one that disagrees. A path the
    runtime chose is not unique the way a session is -- every contained
    session's default socket is named after a pid its own namespace assigns --
    so the pair is what reaches a member, where the path alone reaches whoever
    bound it.

    Blank where the runtime set nothing, which is the honest answer for a
    session whose wake socket this process cannot name. The mail still waits
    in the durable record, and a sender is told nothing will nudge it.
    """
    reported = ClaudeSessionEnv()
    if not reported.wake_socket:
        return WakePath()
    return WakePath(
        runtime="claude", handle=reported.wake_socket, session=reported.session_id
    )
