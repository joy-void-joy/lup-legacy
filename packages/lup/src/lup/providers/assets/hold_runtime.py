# lup: ignore[constant-declaration]
# The event and the envelope's names are this runtime's own wire spelling, a
# fact about the shared native hook contract rather than a taste. Everything
# the store is spelled with comes from the shipped package beside this, which
# is imported rather than restated.
"""Keep a tool call waiting while the operator's pause or a budget holds its caller.

Shipped into the plugin's ``hooks/runtime/`` and started by its guard only
where some hold file is in the store, so everything here resolves on a bare
interpreter: the standard library, the coordination package beside it, and
the policy data the dispatcher reads its own hold limit from -- one limit,
whichever hook holds the call.

**Every tool.** The policy hook holds the calls it judges and judges them
once they are let go; this holds every call, the ones nothing judges among
them, so a paused agent reading files is held at its next read as much as
at its next command. The two wait on the same holds and let a call go
together.

**Silent while it holds.** Nothing is printed while a hold covers the call:
to the agent the call only takes long. A call still held at the limit is
refused in the hold's own words with a request to retry, short of the
timeout the plugin declares for this hook, past which the runtime would run
the call unheld.

**Refusing when it cannot tell.** The guard reaches here only while some
hold stands, so a failure here is a call that might be held: the guard's
entry turns any failure into a refusal rather than a call let through.
"""

import json
import sys
import time
from pathlib import Path
from typing import TypedDict

# The hook is launched as a bare script, promised no cwd, PYTHONPATH, or
# interpreter environment, and the coordination package is a plain sibling
# directory rather than an installed distribution. Naming this file's own
# directory as a search path is what lets the imports below resolve, for the
# interpreter and for a type checker alike.
sys.path.insert(0, str(Path(__file__).parent))
from coordination.holds import Waiting, held_call, refusal
from coordination.runtime import stdin_runtime
from coordination.store import own_member, subagent_id, text
from policy_data import HOLD_SECONDS

EVENT_NAME = "PreToolUse"


class HookInput(TypedDict, total=False):
    """The native fields this reads: which event, whose call, and which call."""

    hook_event_name: str
    agent_id: str
    tool_name: str
    tool_use_id: str


class Refusal(TypedDict):
    """The native refusal's own fields, in the runtime's own spelling."""

    hookEventName: str
    permissionDecision: str
    permissionDecisionReason: str


class HookOutput(TypedDict):
    """What this hook prints where it refuses: the refusal, nested where it is read."""

    hookSpecificOutput: Refusal


def started(stamp: str) -> float:
    """When the hook began, on the monotonic clock, from the guard's wall-clock stamp.

    The runtime counts this hook's timeout from when it started it, which is
    before this interpreter did; a stamp that will not read is now.
    """
    try:
        elapsed = time.time() - float(stamp)
    except ValueError:
        elapsed = 0.0
    return time.monotonic() - max(elapsed, 0.0)


def refused(reason: str) -> HookOutput:
    """The native refusal of one call, carrying the hold's own words."""
    return HookOutput(
        hookSpecificOutput=Refusal(
            hookEventName=EVENT_NAME,
            permissionDecision="deny",
            permissionDecisionReason=reason,
        )
    )


def main() -> None:
    """Hold this call while anything covers its caller; refuse it at the limit.

    A subagent's call, which carries its ``agent_id``, is held as that
    subagent -- under the session it runs in, so a pause of the session
    reaches a subagent the roster has not seen yet. A runtime that inherited
    the launcher's id from the session's shell is held as the member it is.
    """
    root = Path(sys.argv[1])
    began = started(sys.argv[3])
    member = own_member(root, sys.argv[2], stdin_runtime())
    event: HookInput = json.load(sys.stdin)
    if event.get("hook_event_name") != EVENT_NAME or not member:
        return
    agent = text(event.get("agent_id"))
    holds = held_call(
        root,
        subagent_id(member, agent) if agent else member,
        Waiting(tool=text(event.get("tool_name")), call=text(event.get("tool_use_id"))),
        began,
        HOLD_SECONDS,
        parent=member if agent else "",
    )
    if holds:
        print(json.dumps(refused(refusal(holds))), flush=True)


if __name__ == "__main__":
    main()
