"""Claude Code's half of the subagent cleanup fold, run as a bare script.

Shipped verbatim into the plugin's ``hooks/runtime/`` beside the kernel it
imports, and registered under ``SubagentStart`` and ``SubagentStop``. It holds
only what Claude Code spells for itself: the payload's keys, the transcript's
shape, the names of the tools that arm and end background work and deliver a
report, and the two output envelopes. The judgement — which listed tasks the
stopping subagent started, whether this stop hands its report back, and what
it is told — is :mod:`kernel.subagents`'s.

Measured on 2.1.278 rather than read from the docs, with the recordings kept
as fixtures under ``tests/unit/fixtures/subagent_cleanup/``. At
``SubagentStop`` the payload carries ``agent_id``, ``agent_type``,
``agent_transcript_path``, ``stop_hook_active`` and ``background_tasks``, the
last being the whole session's: each entry is typed ``shell`` — a Monitor and
a backgrounded command alike — or ``subagent``, and the subagent's own entry
may be among them. A transcript line of type ``assistant`` carries the tool
calls under ``message.content`` as blocks of type ``tool_use``. A block on
the first pass made the subagent stop its task within seconds and pass on the
second, which the runtime flags with ``stop_hook_active``.

Three more readings, on 2.1.283. A fork's transcript opens with its parent's
whole history, and each ``assistant`` line carries ``attributionAgent``, the
type that wrote it: the parent's, or none for the main session, on inherited
lines, and ``fork`` on the fork's own — so what the fork armed is read from
its own lines alone. Where the session runs in auto mode, a subagent's report
goes through ``SubagentHandback`` and the runtime places the reminder saying
so in its transcript, as a meta line it reads back itself to tell the
contract from its countermand; before that call a stop delivers nothing, and
the runtime tells the caller the subagent "has not reported yet: it is
waiting on its own background work". A fork gets no contract — it runs on its
parent's tools, and the runtime grants one only to a spawn given its own —
yet it inherits its parent's reminder uncountermanded, so its
``SubagentHandback`` answers "not active for this agent. Write your report as
plain text instead" and its plain text is what reaches the caller. That is a
runtime limit, kept to one wasted call; reading the reminder from the fork's
own lines is what keeps it from reading as the fork's contract here.

Every failure is silence. A fold that cannot read is a report let through,
which costs one leaked task, the same as having no fold — the opposite of a
permission hook, whose failure must refuse.
"""

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

# The hook is launched as a bare script, promised no cwd, PYTHONPATH, or
# interpreter environment, and this file sits in the `runtime/` directory
# that holds the kernel package and the coordination store's reader. Naming
# it as a search path is what lets the imports below resolve.
sys.path.insert(0, str(Path(__file__).parent))
from kernel.delegation import verification_notice
from policy_data import VERIFICATION
from kernel.subagents import (
    Armed,
    BackgroundTask,
    Leftover,
    notice,
    refusal,
    stranded,
)


class ListedTask(TypedDict, total=False):
    """One entry of ``background_tasks`` as Claude Code spells it."""

    id: str
    type: str
    status: str
    description: str
    command: str
    agent_type: str


class ToolInput(TypedDict, total=False):
    """The arguments of the calls that start background work."""

    command: str
    run_in_background: bool


class ToolUse(TypedDict, total=False):
    """One block of an assistant message, as far as a tool call is concerned."""

    type: str
    name: str
    input: ToolInput


class Message(TypedDict, total=False):
    content: list[ToolUse] | str


class Entry(TypedDict, total=False):
    """One line of a subagent's transcript, as far as the fold reads it."""

    type: str
    isMeta: bool
    attributionAgent: str
    message: Message


class Payload(TypedDict, total=False):
    """What the two events hand the hook, as far as it reads."""

    hook_event_name: str
    agent_id: str
    agent_type: str
    agent_transcript_path: str
    stop_hook_active: bool
    background_tasks: list[ListedTask]


class Pushed(TypedDict):
    hookEventName: str
    additionalContext: str


class Context(TypedDict):
    """The start-time answer: context the subagent reads, refusing nothing."""

    hookSpecificOutput: Pushed


class Refusal(TypedDict):
    """The stop-time answer: the subagent continues with the reason as its next instruction."""

    decision: str
    reason: str


def entries(transcript: Path) -> list[Entry]:
    """Every line of the transcript that is a JSON object.

    Claude Code's own format, read here because it is Claude Code's. A torn
    final line is the ordinary state of a transcript the runtime is still
    appending to, and one that will not parse must not stop the reader
    seeing the lines around it.
    """

    def parsed() -> Iterator[Entry]:
        try:
            written = transcript.read_text("utf-8")
        except OSError:
            return
        for line in written.splitlines():
            try:
                record: Entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record

    return list(parsed())


def own(lines: list[Entry], agent_type: str) -> list[Entry]:
    """The lines of the subagent's own run, without the history a fork inherits.

    The run begins after the last line another agent wrote before the
    subagent's first. A transcript attributing no line to the subagent's
    type is taken whole, which is what a runtime without the field hands
    over and what a subagent that inherited nothing is anyway.
    """
    authored = [
        index
        for index, entry in enumerate(lines)
        if entry.get("type") == "assistant"
        and entry.get("attributionAgent") == agent_type
    ]
    if not authored:
        return lines
    start = max(
        (
            index + 1
            for index, entry in enumerate(lines[: authored[0]])
            if entry.get("type") == "assistant"
        ),
        default=0,
    )
    return lines[start:]


def calls(lines: list[Entry]) -> Iterator[ToolUse]:
    """Every tool call these lines' assistant messages made."""
    for entry in lines:
        content = entry.get("message", Message()).get("content", [])
        if entry.get("type") == "assistant" and not isinstance(content, str):
            yield from (block for block in content if block.get("type") == "tool_use")


def armed(lines: list[Entry]) -> Armed:
    """Every background start these lines record, by the command it was given.

    A `Monitor` and a `Bash` call run in the background both become shell
    tasks, keyed by command.
    """

    def backgrounded(block: ToolUse) -> bool:
        arguments = block.get("input", ToolInput())
        match block.get("name"):
            case "Monitor":
                return True
            case "Bash":
                return arguments.get("run_in_background", False)
        return False

    return Armed(
        commands=[
            block.get("input", ToolInput()).get("command", "")
            for block in calls(lines)
            if backgrounded(block)
        ]
    )


def handing_back(lines: list[Entry]) -> bool:
    """Whether this stop is the one that hands the subagent's report back.

    Where the report goes through `SubagentHandback`, only a stop after that
    call: before it the runtime delivers nothing and wakes the subagent when
    its work completes. Elsewhere the last message is the report, so every
    stop hands it back. Which holds is read as the runtime reads it — the
    latest of its reminder and its countermand among the subagent's own
    lines.
    """

    def contract(entry: Entry) -> bool | None:
        """``True`` for the hand-back reminder, ``False`` for its countermand."""
        content = entry.get("message", Message()).get("content", [])
        if not entry.get("isMeta", False) or not isinstance(content, str):
            return None
        if content.startswith(
            "<system-reminder>\nYour final report is delivered through SubagentHandback"
        ):
            return True
        if content.startswith(
            "<system-reminder>\nSubagentHandback is not available in this run"
        ):
            return False
        return None

    reports_by_call = next(
        (
            said
            for said in (contract(entry) for entry in reversed(lines))
            if said is not None
        ),
        False,
    )
    return not reports_by_call or any(
        block.get("name") == "SubagentHandback" for block in calls(lines)
    )


def decoded(listed: ListedTask) -> BackgroundTask | None:
    """One listed shell task as the kernel reads it, or nothing for a kind it never names."""
    match listed.get("type"):
        case "shell":
            return BackgroundTask(
                id=listed.get("id", ""),
                kind="shell",
                command=listed.get("command", ""),
            )
    return None


def decided(payload: Payload) -> Context | Refusal | None:
    """The answer to one event, or nothing where the subagent goes through."""
    match payload.get("hook_event_name"):
        case "SubagentStart":
            return Context(
                hookSpecificOutput=Pushed(
                    hookEventName="SubagentStart",
                    additionalContext="\n\n".join(
                        [
                            notice(
                                "a Monitor",
                                "TaskStop",
                                # Both measured on 2.1.278 and kept as
                                # fixtures: the monitor survives the report,
                                # and each line it emits resumes the subagent
                                # that reported.
                                Leftover(resumes=True, refused=True),
                                "SubagentHandback",
                            ),
                            verification_notice(**VERIFICATION),
                        ]
                    ),
                )
            )
        case "SubagentStop" if not payload.get("stop_hook_active", False):
            tasks = [
                task
                for listed in payload.get("background_tasks", [])
                if (task := decoded(listed)) is not None
            ]
            run = own(
                entries(Path(payload.get("agent_transcript_path", ""))),
                payload.get("agent_type", ""),
            )
            left = stranded(tasks, armed(run), handing_back(run))
            if left:
                return Refusal(decision="block", reason=refusal(left, "TaskStop"))
    return None


def main() -> None:
    """Answer the event on stdin, or say nothing and let the subagent through."""
    try:
        payload: Payload = json.load(sys.stdin)
        answer = decided(payload)
    except Exception:
        return
    if answer is not None:
        print(json.dumps(answer))


if __name__ == "__main__":
    main()
