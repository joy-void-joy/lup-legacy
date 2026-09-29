# lup: ignore[constant-declaration]
# The envelope's names are this runtime's own wire spelling, a fact about
# the shared native hook contract rather than a taste. Everything the store is spelled with
# comes from the shipped package beside this, which is imported rather than
# restated.
"""Put a peer's mail in front of its next tool call, from inside the plugin.

Shipped into the plugin's ``hooks/runtime/``, so everything here has to
resolve on a bare interpreter: the standard library, and the coordination
package sitting beside it. That is the constraint the compiled dispatcher runs
under and it is what lets this reach a session nobody opened in-process — a
person's own native CLI, which has the plugin and no live object to close over.

**A reader, never the authority.** :mod:`lup.coordination.mail` owns what a
message means; this lists one member's mailbox, hands what is there to the
session, and deletes what it handed over. The mailbox *is* the position: there
is no offset to commit, nothing to re-read after a crash but what was never
handed over, and no token a reader has to recognize — a message in this
member's mailbox is this member's, and a sender that meant everyone resolved
that against the roster before writing.

**Mail only.** A standing notice is not delivered, so it is not here: it is
read at the head of a turn by the prompt fold, restated for as long as it
stays true, where this consumes what it hands over exactly once.

Mail delivery does not authorize a tool. Failures are diagnosed and mail stays
pending. A successful stdout flush is the native command hook's delivery
boundary; the hook contract exposes no acknowledgment of model consumption.
Redirects also carry their text as context, so a policy denial from a sibling
hook cannot obscure the message when the runtime chooses one denial reason.
"""

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

# The hook is launched as a bare script, promised no cwd, PYTHONPATH, or
# interpreter environment, and the coordination package is a plain sibling
# directory rather than an installed distribution. Naming this file's own
# directory as a search path is what lets the import below resolve, for the
# interpreter and for a type checker alike.
sys.path.insert(0, str(Path(__file__).parent))
from coordination.mail import Message, consume, spoken, waiting
from coordination.store import (
    MEMBER_KIND,
    Actor,
    conversation_of,
    subagent_actor,
    text,
)

EVENT_FIELD = "hookEventName"
EVENT_NAME = "PreToolUse"


class Delivered(TypedDict, total=False):
    """The runtime's own envelope fields, in the runtime's own spelling."""

    hookEventName: str
    additionalContext: str
    permissionDecision: str
    permissionDecisionReason: str


class HookOutput(TypedDict):
    """What this hook prints. `additionalContext` nested here is what is read.

    At the top level the runtime accepts it and ignores it, which is the one
    way to be wrong here that leaves no evidence.
    """

    hookSpecificOutput: Delivered


def envelope(messages: list[Message]) -> HookOutput:
    """The hook output carrying this mail, as telling or as stopping.

    A redirect denies the call and hands the text back as the reason, so an
    agent going the wrong way cannot take one more step down it. An ordinary
    message rides alongside and the call proceeds.
    """
    delivered = spoken(messages)
    if any(message.get("redirect") for message in messages):
        return HookOutput(
            hookSpecificOutput=Delivered(
                hookEventName=EVENT_NAME,
                additionalContext=delivered,
                permissionDecision="deny",
                permissionDecisionReason=(
                    f"{delivered}\n\nStop what this call was part of and act "
                    "on the above."
                ),
            )
        )
    return HookOutput(
        hookSpecificOutput=Delivered(
            hookEventName=EVENT_NAME, additionalContext=delivered
        )
    )


def publish(answer: HookOutput) -> None:
    """Hand a complete native envelope to the parent hook process."""
    print(json.dumps(answer), flush=True)


def deliver(
    root: Path,
    member_id: str,
    agent: str = "",
    send: Callable[[HookOutput], None] = publish,
) -> HookOutput | None:
    """Take one conversation's mail and say what it should be told.

    The mailbox is keyed by the conversation this call is on the roster as —
    this session's, or *agent*'s where the call is one of its subagents' —
    spelled by the shipped fold rather than assembled here: a sender writes to
    the same key through the typed half, and a directory only one of them
    could name is a message nobody receives.

    Consumed after publishing, by deleting exactly what was handed over, so a message that
    arrived between the listing and the deletion waits for the next call
    rather than leaving unseen.
    """
    reader = (
        subagent_actor(member_id, agent)
        if agent
        else Actor(kind=MEMBER_KIND, id=member_id)
    )
    mailbox = conversation_of(reader)
    messages = waiting(root, mailbox)
    if not messages:
        return None
    answer = envelope(messages)
    send(answer)
    consume(root, mailbox, messages)
    return answer


class HookInput(TypedDict, total=False):
    """Native fields that distinguish a root tool event from an inherited child."""

    hook_event_name: str
    agent_id: str
    agent_type: str
    session_id: str


def main() -> None:
    """Deliver each tool event its own conversation's mail; diagnose failures without blocking work.

    A subagent's event, which carries its ``agent_id``, is handed that
    subagent's own mailbox and never its session's: the session's mail waits for
    the session's own next call. An event naming an agent type and no id is
    not a subagent this session's roster holds a row for, and takes nothing.
    """
    try:
        event: HookInput = json.load(sys.stdin)
        member = sys.argv[2]
        if event.get("hook_event_name") != EVENT_NAME or not text(
            event.get("session_id")
        ):
            return
        agent = text(event.get("agent_id"))
        if not agent and text(event.get("agent_type")):
            return
        if not all(
            name and all(character.isalnum() or character in "-_" for character in name)
            for name in (member, agent or member)
        ):
            return
        deliver(Path(sys.argv[1]), member, agent)
    except Exception as error:
        print(
            f"Peer mail delivery failed ({type(error).__name__}); mail remains pending. Check hook input and store permissions.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
