"""A subagent hands back its report only after the background work it started has stopped.

A subagent that arms a watch or backgrounds a command and then reports leaves
the task running: the runtime keeps it, and each line it emits resumes the
finished subagent, which reports again, until somebody ends the task by id.
The runtime's own note on such a subagent reads "stopped with background work
of its own still running. It may resume on its own when that work completes
or reports."

That the work outlives the report and that its output resumes the reporter
are two facts, and they were measured apart: a runtime has been seen holding
the first without the second, its leftover session running on with nothing it
emitted waking anybody. The start-time sentence is owed wherever the work
outlives the report, since the leak is real either way; the stop-time refusal
is owed only where the resume is, because a report refused to prevent a loop
that runtime does not have buys nothing and costs a turn. ``Leftover`` is how
a host half says which it has, and the two moments below are written so
either can be registered without the other.

Waiting is not reporting. A stop that ends a turn before the report goes out
is a subagent waiting on its own work, which the runtime wakes it from when
that work completes, and refusing it only teaches the subagent to wait some
costlier way — a second watch over the first. So the refusal is owed at the
hand-back alone: the stop that delivers the report, or follows the call that
did. Which stop that is, the host half reads, since how a runtime delivers a
report is its own.

What is owed is shell work — a watch, a backgrounded command. A subagent the
stopping one started is never named: it ends on its own and reports through
its own hand-back, and the ending call is refused for one that was resumed,
so a refusal naming it asks for something that cannot be done.

The tasks the subagent started are the ones whose start its own run records.
The runtime hands the hook every background task of the session, marking
none as anybody's, so what the subagent's run armed is the whole of the
evidence — and a run is the subagent's own writing, not history it inherited.
While any of them is still listed at the hand-back the stop is refused once,
with a reason naming each task and the call that ends it. Once, because the
runtime flags a stop that already follows a refusal, and a second refusal
would hold a subagent that cannot comply forever, when the runtime's own
notification already tells the parent it stopped with work running.

The main agent is deliberately not gated. Its stop fires with background
subagents listed as running, and that wait is the point: the events it is
waiting for are what wake it.

What a runtime spells — the payload's keys, the transcript's shape, the names
of the tools that arm and end a task and deliver a report, the output
envelopes — is its host half's; this module holds the part every runtime
answers identically.
"""

from typing import Literal, TypedDict

type TaskKind = Literal["shell", "subagent"]
"""The runtime's own partition of what it keeps running: a shell task, which a
watch and a backgrounded command both are, or a subagent."""


class BackgroundTask(TypedDict, total=False):
    """One background task of the session, as the host half decodes it.

    ``id`` is what the ending call takes. ``command`` is what a shell task
    runs, which is how it is matched back to the call that started it,
    because the list carries nothing else that says whose it is.
    """

    id: str
    kind: TaskKind
    command: str


class Armed(TypedDict):
    """What a subagent's own run started in the background, by the command it was given."""

    commands: list[str]


def stranded(
    tasks: list[BackgroundTask], armed: Armed, handing_back: bool
) -> list[BackgroundTask]:
    """The shell work this subagent started that its hand-back would leave running.

    Nothing unless this stop hands the report back: before that, a stop is
    the subagent waiting on its own work. At the hand-back, every listed
    shell task whose command its run armed, since a subagent it started ends
    on its own.
    """
    if not handing_back:
        return []
    return [
        task
        for task in tasks
        if task.get("kind") == "shell" and task.get("command", "") in armed["commands"]
    ]


def refusal(tasks: list[BackgroundTask], ending_call: str) -> str:
    """Why the stop is refused: each task by id, and the call that ends it."""
    named = ", ".join(
        f"{task.get('id', '')} ({task.get('kind', '')}: {task.get('command', '')})"
        for task in tasks
    )
    return (
        "Background work you started is still running as you hand back your"
        f" report: {named}. Stop each with {ending_call}, then finish — left"
        " running, each resumes you after you have reported."
    )


class Leftover(TypedDict):
    """What a runtime does with work a subagent leaves running, as measured.

    ``resumes`` is whether a line from that work wakes the subagent that has
    already reported, which is the loop the stop-time refusal exists to
    break. ``refused`` is whether the refusal is registered there, which is
    the same answer wherever the loop occurs and a separate field because a
    runtime may have one without the other.

    Both are measurements rather than settings, and the notice says what is
    true where it is read: a sentence promising a refusal that never comes
    teaches a subagent to discount the next one it is handed.
    """

    resumes: bool
    refused: bool


def notice(
    watch_call: str, ending_call: str, leftover: Leftover, report_call: str = ""
) -> str:
    """What a subagent is told as it starts: what it arms is its own to stop.

    ``report_call`` is the call a runtime delivers a subagent's report
    through, where it has one: there a turn can end before the report goes
    out, so waiting on work by ending one is said to be fine. Where the last
    message is the report, nothing is said about waiting, since every turn's
    end hands it back.
    """
    consequence = (
        "A task left running resumes you after you have finished"
        if leftover["resumes"]
        else "Work left running outlives your report and holds what it opened"
        " until this session ends"
    )
    refused = (
        ", and finishing is refused once while any of it outlives your hand-back"
        if leftover["refused"]
        else ""
    )
    waiting = (
        f" Where your report goes through {report_call}, ending a turn to wait"
        " on your work before that call is fine: its completion wakes you."
        if report_call
        else ""
    )
    return (
        f"Background work you start — {watch_call} or a command run in the"
        f" background — is yours to stop with {ending_call} before you hand"
        " back your report; a subagent you start is not, since it ends on its"
        f" own and reports through its own hand-back. {consequence}{refused}."
        f"{waiting}"
    )
