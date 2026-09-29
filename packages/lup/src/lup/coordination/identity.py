# lup: ignore[constant-declaration]
# The environment names are a handshake between a launcher, a hook and a tool
# server in three different processes, so a caller free to choose them is a
# caller free to open a session nobody can address.
"""Who a session is on its repository's roster, and what it is called.

Two facts, deliberately separated, because they change on different schedules
and answer different questions. The **id** is minted once and never moves: it
is what mail is addressed to, what a touch is attributed to, and what a
restart reattaches by. The **name** is what a person types and reads, and a
session may rename itself whenever what it is doing changes.

A rename appends rather than overwrites: a member's own file keeps every name
it has answered to, newest last, so a name somebody wrote down an hour ago
still reaches the session it named. A store holding only the current one would
silently misroute every reference taken before the rename, and there is no
error the sender could be shown, because the name they used was correct when
they read it. On the member rather than in a record beside it, because the two
would only ever be read together.

What is left here is the vocabulary — how an id is minted, proven and
defaulted, and how a name is chosen against the ones already taken. Reading a
member and writing one is :mod:`lup.coordination.bare.store`'s.

**A launcher declares the id; a session without one derives it.** The
environment variable is set by the process that minted the id and can prove it,
which mirrors ``LUP_AGENT_IDENTITY`` and works for the same reason: a hook
script is spawned by the runtime CLI with the CLI's own environment, so an
agent exporting this inside a shell call cannot reach the hook that reads it.
A session nobody launched that way — a bare ``claude`` in a worktree — has the
plugin and no launcher, so it falls back to the identity its own runtime
already gave it. It is a full peer that cannot prove who started it, which is
the honest description and costs it nothing on the roster.

Nothing here reads a runtime's environment for that fallback. Which variable
carries a session's own id is one runtime's spelling, so it arrives as an
argument from the adapter that knows it, and this module stays the vocabulary
both halves share.
"""

from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


from lup.coordination.bare.store import MEMBER_KIND
from lup.coordination.refs import ActorRef
from lup.types import EnvVars

MEMBER_ENV = "LUP_COORDINATION_MEMBER"
"""Environment variable naming the durable id a launcher minted for a session."""

NAME_ENV = "LUP_COORDINATION_NAME"
"""Environment variable naming what a launcher called the session it minted an id for.

Exported beside the id for the same reason: the runtime's own chrome shows a
name the launcher chose, and the roster has to answer to the same one, so the
choice is made once where both can read it rather than derived twice.
"""


class MemberEnv(BaseSettings):
    """The launcher's half of the identity contract, read from the environment.

    The consumer side of what :meth:`LaunchedMember.environment` writes,
    spelled once so the process that mints an id and the process that answers
    to it cannot drift — the arrangement
    :class:`~lup.workspace.context.SessionEnv` uses, for the same reason.
    """

    member_id: str = Field(default="", validation_alias=MEMBER_ENV)
    cli_name: str = Field(default="", validation_alias=NAME_ENV)


def session_cli_name() -> str:
    """What the launcher called this session, or blank where nothing launched it.

    Blank is a real answer: a session with the plugin and no launcher is named
    after its worktree when it joins, and nothing here has to invent one.
    """
    return MemberEnv().cli_name


def session_member_id(fallback: str = "") -> str:
    """The id this session is on the roster under, preferring the proven one.

    A launcher that minted an id and exported it is the strongest claim
    available: it set the variable in the environment the runtime CLI spawns
    hooks and tool servers with, so nothing the agent does inside a shell call
    can reach it. Where there is none, the caller's fallback stands — the
    identity that session's own runtime already gave it, which is honest about
    being unproven and is still stable for as long as the session lives.

    Empty means neither: a process that is not a session at all. Nothing joins
    a roster under an empty id, so the caller is left to decide, rather than
    being handed a mint that would put a new member on the roster every call.
    """
    return MemberEnv().member_id or fallback


def mint_member_id() -> str:
    """A durable id for one session, unique without asking anybody.

    Minted rather than derived from the worktree, because two sessions in one
    worktree are two peers and a derived id would make them one — which is the
    case a repository-wide roster exists to hold. The worktree still names the
    session; it just does not identify it.
    """
    return uuid4().hex[:12]


class LaunchedMember(BaseModel, frozen=True):
    """The identity a launcher mints for one session: its id and what it is called.

    Minted together because they are exported together and read together: the
    tool server joins under the id and answers to the name, and the runtime's
    chrome shows the name, so a launcher holding them as two values would be
    the one place they could disagree.
    """

    member_id: str
    cli_name: str

    def environment(self) -> EnvVars:
        """Declare this session's coordination identity for the processes it starts.

        Set rather than omitted for the reason the agent identity is: runtimes
        merge a session's environment over the launching process's, so an
        operator with these exported would otherwise hand their own address
        to every session they start, and two peers would answer to one id.
        """
        return {MEMBER_ENV: self.member_id, NAME_ENV: self.cli_name}


class NameTakenError(ValueError):
    """Raised when a session asks for a name a live session already answers to.

    A refusal rather than a silent suffix, because the name was chosen: a
    session renaming itself to what another live session is called would make
    the roster print one address for two peers, which is the collision the
    numbered default exists to rule out.
    """

    def __init__(self, cli_name: str, holder_id: str) -> None:
        super().__init__(
            f"{cli_name!r} is what session {holder_id} is called; a live "
            "session's name reaches it, so choose another"
        )
        self.cli_name = cli_name
        self.holder_id = holder_id


def member_ref(member_id: str) -> ActorRef:
    """The roster address one session is reached at."""
    return ActorRef(kind=MEMBER_KIND, id=member_id)


def derived_cli_name(worktree: Path) -> str:
    """What to call a session that was not given a name, from where it is working.

    The worktree, because that is what a person watching several sessions is
    actually distinguishing between — which branch is this one on — and it is
    the one fact a session has before it has done anything. Two sessions in one
    worktree want the same one, and the second is numbered by
    :func:`~lup.coordination.bare.store.unique_cli_name` when it joins, so the address a listing prints for
    either reaches that one and not the other.
    """
    return worktree.name
