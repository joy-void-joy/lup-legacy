"""A session or a subagent leaving, recorded by the hook its runtime fires as it ends.

The roster's row for a session ends with the record that session writes on
its way out, and nothing wrote it: a session that exited cleanly read as
running forever, the same as one that was killed. Both runtimes fire an event
when a session ends — a clean exit, a cleared conversation, a terminal closed,
a termination signal — and this is what runs under it: the one line that says
the member is finished, appended to the roster every reader folds.

A native subagent's row ends the same way under the event its runtime fires as
the subagent stops, which both runtimes hand the subagent's ``agent_id``.
Whatever it was sent and never read goes to the session that dispatched it,
because a sender was told it would reach the subagent at its next call and
there will be none: the session is who can act on it, or resume the subagent
with it.

Shipped into each plugin's ``hooks/runtime/coordination/``, so everything
here resolves on a bare interpreter: standard library, and the fold beside it
in the same package. A departure that cannot be written is not worth stopping
an exit for, so every failure is silence — the pulse retires the row within
its window either way, and this only makes the ending exact.

**The record is the fold's.** Both the test — is this member standing? — and
the line appended live in :mod:`.store`, beside every other reader of that
file, so the row that arrives and the row that leaves cannot be written to
two different understandings of the same record. What is here is the hook:
which arguments a runtime hands over, and the rule that nothing it does may
be allowed to fail.

The member is the launcher-proven id where one was minted, and otherwise the
id the runtime hands the hook — the same fallback the prompt-time fold takes,
so the row that arrives and the row that leaves are one row.
"""

import json
import sys
from pathlib import Path
from typing import TypedDict

from .mail import consume, new_message, post, waiting
from .runtime import stdin_runtime
from .store import (
    conversation_of,
    depart,
    own_member,
    session_actor,
    subagent_actor,
    subagent_id,
    text,
)


class Ending(TypedDict, total=False):
    """What the runtime hands the hook on stdin, as far as this reads it."""

    session_id: str
    agent_id: str


def subagent_left(root: Path, session: str, agent: str) -> bool:
    """End one subagent's row, handing whatever it never read to its session.

    Forwarded as ordinary messages, a redirect included: the conversation it
    was meant to stop has stopped, and the session reading it is being told
    about somebody else's work rather than turned from its own.
    """
    left = subagent_actor(session, agent)
    unread = waiting(root, conversation_of(left))
    for message in unread:
        post(
            root,
            session_actor(session),
            new_message(
                sender=text(message.get("sender")),
                to=text(message.get("to")),
                body=(
                    f"for {subagent_id(session, agent)}, which stopped before "
                    f"reading it: {text(message.get('text'))}"
                ),
                door=text(message.get("door")),
                in_reply_to=text(message.get("in_reply_to")),
            ),
        )
    consume(root, conversation_of(left), unread)
    return depart(root, left)


def main() -> None:
    """Write the departure, or say nothing and let the session end.

    The store root and this member's launcher-proven id (blank where nothing
    launched it) arrive as arguments; the ending payload on stdin supplies
    the session's own id as the fallback, and the subagent's id where a
    subagent is what stopped. The event name the guard passes beside them is
    not read here — an ending is an ending, and only the prompt fold has an
    envelope to name it in. A runtime that inherited the launcher's id from
    the session's shell ends its own row, never that session's.

    Every failure is silence, because an exit is not something a broken
    roster may hold up.
    """
    try:
        root, member = Path(sys.argv[1]), sys.argv[2]
        runtime = stdin_runtime()
        ending: Ending = json.load(sys.stdin)
        session = own_member(root, member, runtime) or text(ending.get("session_id"))
        agent = text(ending.get("agent_id"))
        if agent:
            subagent_left(root, session, agent)
            return
        depart(root, session_actor(session))
    except Exception:
        return


if __name__ == "__main__":
    main()
