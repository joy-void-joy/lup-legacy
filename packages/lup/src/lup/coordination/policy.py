"""What the permission policy is told about this repository's sessions.

The hook that judges a call runs as a bare script outside every import graph,
so it cannot ask this package anything. What it *can* do is import the fold
shipped beside it, :mod:`lup.coordination.bare.store`, which knows every file
the store is made of — so what has to travel as data is only what that fold
cannot know: where beneath the shared git directory this project put its
store, which environment variable carries a session's proven id, and the
prose a stopped caller reads.

The prose is a default rather than a fixture. What a stopped caller should
reach for is a judgement about the surfaces a project offers, and a project
that renamed its own is entitled to say so without editing the library.
"""

from pathlib import PurePosixPath

from lup.coordination.bare.store import COORDINATION_DIR, STORE_DIR, WINDOWS_DIR
from lup.coordination.identity import MEMBER_ENV
from lup.policy.kernel.diagnostic import Step, step
from lup.policy.peer_policy import PeerPolicy
from lup.policy.refused_paths import RefusedPaths

COORDINATION_SERVER = "coordination"
"""The tool server a session's coordination verbs are served from.

The one name the server's declaration and the hook stamping each call's
caller have to agree on: a runtime addresses a tool by the server carrying it,
so a hook matched against any other name stamps nothing and every subagent
acts as its session again.
"""

SEND_REDIRECT = (
    "a native send to a session on this repository's roster leaves no record"
    " any other worktree can read"
)
"""Why a native send to a roster member was stopped, in one line."""

SEND_RECOVERY = (
    step(
        "say it with `coordination_send` instead: it reaches the same peer,"
        " records it where every session in this clone can read it, and reports"
        " whether the peer's hook puts it in front of that peer's next tool call"
        " or it waits until they next look"
    ),
)
"""What the durable path buys, for a sender that would otherwise retry.

Names what the other surface buys rather than only refusing, because a sender
told no and nothing else sends the same message again through whatever it
tries next. The delivery report is the half a native send has no answer for at
all: it cannot say whether anybody will hear this.
"""

LISTING_NOTE = (
    "This repository's own roster, which is a different population from the"
    " listing above: these are the sessions working in this clone, in whatever"
    " worktree, and they include peers no account-scoped listing can see."
    " Reach any of them with `coordination_send`, which records what it"
    " carries. The person watching is always at `user`."
)
"""How the roster is framed beside a listing that speaks for something wider.

The two are different objects and the whole risk is a reader taking one for
the other, so the note says which is which before it says what to do — an
attachment that only offered a tool would read as a correction of the listing
it rides on.
"""

CLAIM_HELD = "another live session has changed or locked this path"
"""What the approver of a write into a path somebody else is in reads.

An approval question rather than a refusal, because the answer is genuinely
the operator's: two sessions editing one file is sometimes exactly right, and
a policy that decided otherwise would refuse ordinary work. What it must not
be is silent — the failure this exists for is finding out at merge time.
"""

CLAIM_RECOVERY = (
    step(
        "ask the holder with `coordination_send` first: writing under a held"
        " path is how two sessions overwrite each other between merges"
    ),
    step(
        "or go ahead if you already know what they are doing; a claim expires"
        " with the session holding it, so one still standing means that session"
        " has not stopped"
    ),
)
"""What the writing agent can do about the holder, beside the question."""


def peer_policy(
    send_reason: str = SEND_REDIRECT,
    listing_note: str = LISTING_NOTE,
    claim_reason: str = CLAIM_HELD,
    send_recovery: tuple[Step, ...] = SEND_RECOVERY,
    claim_recovery: tuple[Step, ...] = CLAIM_RECOVERY,
    server: str = COORDINATION_SERVER,
) -> PeerPolicy:
    """This repository's sessions, as the compiled permission hook reads them."""
    return PeerPolicy(
        server=server,
        store=[STORE_DIR, COORDINATION_DIR],
        windows_dir=WINDOWS_DIR,
        member_env=MEMBER_ENV,
        send_reason=send_reason,
        send_recovery=list(send_recovery),
        listing_note=listing_note,
        claim_reason=claim_reason,
        claim_recovery=list(claim_recovery),
    )


WAKE_SOCKET_REACHED = (
    "a session's wake socket is its wake handle, and a frame written to it"
    " starts that session's turn with text no roster records"
)
"""Why a command connecting to a peer's wake socket was stopped, in one line."""

WAKE_SOCKET_RECOVERY = (
    step(
        "reach the peer with `coordination_send` instead: it records the message"
        " where every session in this clone can read it, and wakes the peer"
        " through this socket itself"
    ),
)
"""What reaches the same peer and leaves the record a raw frame does not."""


def wake_socket_refusal(
    directory: str,
    reason: str = WAKE_SOCKET_REACHED,
    recovery: tuple[Step, ...] = WAKE_SOCKET_RECOVERY,
) -> list[RefusedPaths]:
    """The directory sessions bind their wake sockets in, withheld from every command.

    ``directory`` is the launch's own declaration of where wake sockets go,
    passed rather than spelled here, so a project that moves it moves the
    refusal with it. Every spelling of a connection the kernel reads names the
    path -- a socat address, `nc -U`, `curl --unix-socket`, a redirection -- so
    the path is refused rather than each program. Empty declares no wake
    socket directory, and refuses nothing.
    """
    if not directory:
        return []
    return [
        RefusedPaths(
            paths=[str(PurePosixPath(directory) / "**")],
            reason=reason,
            recovery=list(recovery),
        )
    ]
