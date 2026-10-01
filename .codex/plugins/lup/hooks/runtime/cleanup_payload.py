"""Codex's half of the subagent cleanup fold, run by a bare script.

Shipped verbatim into the plugin's ``hooks/runtime/`` beside the kernel it
imports, where the fold's generated entry reads each event, asks
:func:`decided`, and prints its answer; registered under ``SubagentStart``
alone. It holds only what
Codex spells for itself: the event's name, the calls that open a live session
and end what runs in it, and the one output envelope.

Both halves of the leak were measured on 0.155.1 in operator-run sessions,
and they came apart. Work a subagent arms outlives its report: a ``sleep
494`` opened through the subagent's PTY with no ``&`` was alive 28.2 seconds
after ``SubagentStop``, its ``etimes`` matching the elapsed time exactly, so
the same process rather than a reparented orphan. Output from that work
resumes nothing: a ``tail -f`` on an empty file was armed the same way, the
parent appended a line 14.4 seconds after the stop to force output onto the
surviving session, and of the twelve records following the one stop, none
carried the subagent's ``agent_id``.

So the sentence is registered here and the refusal is not. A ``SubagentStop``
block would refuse a report to prevent a resume this runtime does not
perform; what it does have is a session running until the session that opened
it ends, which a subagent can close and is therefore told to.

Which call closes one is read from 0.155.1's own tool schemas: ``exec_command``
"Runs a command in a PTY, returning output or a session ID for ongoing
interaction", ``write_stdin`` "Writes characters to an existing unified exec
session and returns recent output", and nothing there closes a session
outright. So the sentence names the keystroke path to ending what runs in it,
which is what the schemas support.

The event is documented at https://learn.chatgpt.com/docs/hooks: at
``SubagentStart`` the hook is handed ``agent_id``, ``agent_type``, ``turn_id``
and ``permission_mode`` beside the common fields, cannot stop the subagent,
and on exit 0 its stdout's ``hookSpecificOutput.additionalContext`` is added
as context the subagent reads — the same envelope Claude Code spells, which
is why one kernel composes the sentence for both.

Every failure is silence. A fold that cannot read is a subagent that was not
told, which costs one leaked session — the same as having no fold — the
opposite of a permission hook, whose failure must refuse.
"""

from typing import TypedDict

from kernel.delegation import verification_notice
from kernel.subagents import Leftover, notice
from policy_data import VERIFICATION


class Payload(TypedDict, total=False):
    """What the event hands the hook, as far as it reads."""

    hook_event_name: str


class Pushed(TypedDict):
    hookEventName: str
    additionalContext: str


class Context(TypedDict):
    """The start-time answer: context the subagent reads, refusing nothing."""

    hookSpecificOutput: Pushed


def decided(payload: Payload) -> Context | None:
    """The answer to one event, or nothing where this runtime has none to give."""
    match payload.get("hook_event_name"):
        case "SubagentStart":
            return Context(
                hookSpecificOutput=Pushed(
                    hookEventName="SubagentStart",
                    additionalContext="\n\n".join(
                        [
                            notice(
                                "an `exec_command` session",
                                "`write_stdin` carrying the interrupt",
                                Leftover(resumes=False, refused=False),
                            ),
                            verification_notice(**VERIFICATION),
                        ]
                    ),
                )
            )
    return None
