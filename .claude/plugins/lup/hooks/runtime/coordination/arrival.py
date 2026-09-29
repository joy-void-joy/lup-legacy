"""Bind a launcher-owned roster member to its root native session.

The hook supplies the authoritative native session id. A runtime that
inherited the launcher's environment from the session's shell binds the
member it is (:func:`.store.own_member`), so it cannot take its parent's wake
address. A missing member stays missing; prompt-time retries handle a tool
server joining after the native startup event. The runtime argument belongs to
the adapter.
"""

import json
import sys
from pathlib import Path
from typing import TypedDict

from .runtime import stdin_runtime
from .store import (
    MEMBER_KIND,
    Member,
    Wake,
    own_member,
    revised,
    session_actor,
    text,
)
from .scope import execution_scope


class Arrival(TypedDict, total=False):
    """Native hook fields that establish session and process scope."""

    session_id: str
    hook_event_name: str
    cwd: str
    agent_id: str
    agent_type: str


def bind(
    root: Path,
    member: str,
    runtime: str,
    arrival: Arrival,
    events: tuple[str, ...],
    home: str = "",
) -> bool:
    """Bind an existing member only when the native root scope matches it."""
    session = text(arrival.get("session_id"))
    event = text(arrival.get("hook_event_name"))
    if not event or event not in events:
        return False
    cwd = text(arrival.get("cwd"))
    if not session or not cwd or not runtime or not member:
        return False
    if text(arrival.get("agent_id")) or text(arrival.get("agent_type")):
        return False
    if not all(character.isalnum() or character in "-_" for character in member):
        return False
    bound = False

    def update(found: Member) -> Member:
        nonlocal bound
        if found.get("kind") != MEMBER_KIND or found.get("id") != member:
            return found
        worktree = text(found.get("worktree"))
        if not worktree or Path(worktree).resolve() != Path(cwd).resolve():
            return found
        wake = found.get("wake", Wake())
        if wake.get("runtime") not in {None, "", runtime}:
            return found
        found["wake"] = Wake(
            runtime=runtime,
            handle=session,
            session=session,
            home=str(Path(home).resolve()) if home else "",
            scope=execution_scope(),
        )
        bound = True
        return found

    return revised(root, session_actor(member), update) is not None and bound


def main() -> None:
    """Best-effort binding; unavailable roster metadata never blocks a prompt."""
    try:
        root, launched, runtime = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
        member = own_member(root, launched, stdin_runtime())
        arrival: Arrival = json.load(sys.stdin)
        bind(root, member, runtime, arrival, tuple(sys.argv[5:]), sys.argv[4])
    except Exception as error:
        print(
            f"Native session wake binding failed ({type(error).__name__}); "
            "check the hook input and coordination store permissions.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
