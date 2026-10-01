"""A subagent's report waits for the background work it started, as measured.

The fixtures under ``fixtures/subagent_cleanup/`` are recordings, not
inventions: every hook payload Claude Code 2.1.278 handed a probe hook in a
throwaway project — once interactively with permissions skipped, once in
print mode from inside a sandboxed session — and the subagent's own
transcript from the print-mode run, cut to its ``user`` and ``assistant``
lines. The interactive run's transcript sat under a home directory the
recording session could not read, so its stop is judged against the
print-mode transcript, whose armed command is the same.

What is asserted is the rendered guard run as the runtime runs it, over
those payloads: the first stop is refused naming the monitor and not the
subagent's own entry, the stop that follows the refusal goes through, a task
the subagent did not arm is not its to stop, and the start event carries the
one sentence. The registration is asserted per event, and a project that
declined carries nothing.

Three more are cut from Claude Code 2.1.283 sessions, keeping the keys the
fold reads and shortening the prose it does not. ``transcript-claude-fork``
is a fork's: its parent's history, then its own run, whose stop the old fold
refused naming three siblings it never started. One inherited line — the
parent's backgrounded `Bash` — is borrowed from that parent's later history
to stand for shell work a fork inherits. ``transcript-claude-handback`` is a
subagent reporting through `SubagentHandback`: it backgrounded the full gate
and ended its turn to wait, the old fold refused that, and it armed a
monitor to wait instead before handing back. ``payloads-claude-park`` and
``transcript-claude-park`` are a probe's: a subagent that started a
background subagent and ended its turn while that one ran. The stop
payloads for the first two were not recorded, so each test builds its own
from the ids and commands the recording names.

``payloads-codex.jsonl`` is the same kind of recording from Codex 0.155.1,
where the two halves of the leak came apart: the work outlives the report and
its output resumes nobody. So that tree is asserted to register the start
alone, and its sentence to promise no refusal — the negative is asserted
rather than left to whoever reads the rendered text, because a promise that
never comes true is what teaches a subagent to discount the next one.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import NotRequired, TypedDict

import pytest
import sh

from lup.harness.generate import NativeHarnessComposition
from lup.harness.models import Artifact
from lup.policy.bundle import policy_kernel_modules
from lup.providers.codex.harness import CODEX_SUBAGENT_START_EVENT
from lup.providers.claude.harness import (
    CLAUDE_SUBAGENT_START_EVENT,
    CLAUDE_SUBAGENT_STOP_EVENT,
)
from lup.providers.roster_prompt import store_modules
from lup.providers.subagent_cleanup import (
    GUARD_SCRIPT,
    HOST_MODULE,
    RUNTIME_ENTRY,
    cleanup_hooks,
)
from lup_template.harness.catalog import portable_harness
from lup_template.harness.composition import claude_target, codex_target


class ListedTask(TypedDict, total=False):
    """One entry of a task list, as far as the tests write it."""

    id: str
    type: str
    status: str
    command: str
    description: str
    agent_type: str


class Payload(TypedDict):
    """One hook payload, as far as the tests read or write it."""

    hook_event_name: str
    agent_id: NotRequired[str]
    agent_type: NotRequired[str]
    stop_hook_active: NotRequired[bool]
    agent_transcript_path: NotRequired[str]
    background_tasks: NotRequired[list[ListedTask]]


class Recorded(TypedDict):
    """One line of a recording: when it fired, and what the hook was handed."""

    at: float
    payload: Payload


RECORDINGS = pytest.mark.parametrize(
    ("recording", "task_id", "own_id"),
    [
        pytest.param(
            "payloads-claude-interactive.jsonl",
            "bextuezys",
            "a89db2100fd8133cd",
            id="interactive",
        ),
        pytest.param(
            "payloads-claude-print.jsonl", "bf0g39eun", "a320c99e18b0ff176", id="print"
        ),
    ],
)


CLAUDE_PLUGIN = Path(".claude/plugins/lup")
CODEX_PLUGIN = Path(".codex/plugins/lup")
"""Where each runtime's plugin sits, which is what picks the tree to read."""


def fixtures() -> Path:
    """Where the recordings sit, beside this test."""
    return Path(__file__).parent / "fixtures" / "subagent_cleanup"


def recorded(recording: str, event: str) -> list[Payload]:
    """Every payload of one event in one recording, in the order it fired."""
    lines: list[Recorded] = [
        json.loads(line)
        for line in (fixtures() / recording).read_text("utf-8").splitlines()
    ]
    return [
        entry["payload"]
        for entry in lines
        if entry["payload"]["hook_event_name"] == event
    ]


def shipped(
    target: Callable[[Path], NativeHarnessComposition],
) -> dict[Path, Artifact]:
    """Every artifact the plugin carries, by the path it carries it at."""
    return {
        artifact.path: artifact
        for artifact in target(Path.cwd()).recipe.desired.artifacts
    }


def laid_out(
    root: Path,
    target: Callable[[Path], NativeHarnessComposition] = claude_target,
    plugin: Path = CLAUDE_PLUGIN,
) -> Path:
    """The guard, the host half, and the packages it imports, as a plugin lays them out."""
    artifacts = shipped(target)
    carried = [
        (
            Path("hooks") / "scripts" / GUARD_SCRIPT,
            artifacts[plugin / "hooks" / "scripts" / GUARD_SCRIPT].content,
        ),
        (
            Path("hooks") / "runtime" / RUNTIME_ENTRY,
            artifacts[plugin / "hooks" / "runtime" / RUNTIME_ENTRY].content,
        ),
        (
            Path("hooks") / "runtime" / f"{HOST_MODULE}.py",
            artifacts[plugin / "hooks" / "runtime" / f"{HOST_MODULE}.py"].content,
        ),
        # The entry reads this project's own gate spellings out of it, the way
        # the compiled dispatcher beside it reads every other declared value.
        (
            Path("hooks") / "runtime" / "policy_data.py",
            artifacts[plugin / "hooks" / "runtime" / "policy_data.py"].content,
        ),
        *[
            (Path("hooks") / "runtime" / "kernel" / module.name, module.source)
            for module in policy_kernel_modules()
        ],
        *[
            (Path("hooks") / "runtime" / module.path, module.content)
            for module in store_modules()
        ],
    ]
    for relative, content in carried:
        landed = root / relative
        landed.parent.mkdir(parents=True, exist_ok=True)
        landed.write_text(content)
    return root / "hooks" / "scripts" / GUARD_SCRIPT


def judged(guard: Path, payload: Payload) -> str:
    """The guard's stdout for one payload, run as the runtime runs it."""
    return str(sh.sh(str(guard), _in=json.dumps(payload)))


def at_stop(recording: str, index: int) -> Payload:
    """One recorded stop, its transcript pointed at the fixture copy."""
    stop = recorded(recording, CLAUDE_SUBAGENT_STOP_EVENT)[index]
    stop["agent_transcript_path"] = str(fixtures() / "transcript-claude-print.jsonl")
    return stop


class Input(TypedDict, total=False):
    """The arguments of a call that starts background work."""

    command: str


class Block(TypedDict, total=False):
    """One block of a transcript line's content, as far as the tests read it."""

    type: str
    name: str
    input: Input


class Message(TypedDict, total=False):
    content: list[Block] | str


class Line(TypedDict, total=False):
    """One transcript line, as far as the tests read or rewrite it."""

    type: str
    agentId: str
    attributionAgent: str
    message: Message


FORK_ID = "a098b9876c89f7f93"
SIBLINGS = {
    "a71832f159fedc383": "Triage G4 worktree/pr/check issues",
    "aae1435e50f1a97bb": "Triage G5 policy and codex issues",
    "abd5e022b32dde6d0": "Check adlib report items against dev",
}
"""The fork's siblings, still running, that the old fold told it to stop — by
the ids and descriptions its recorded refusal names."""
HANDBACK_ID = "a142a8740cb9a255b"


def transcript(name: str) -> list[Line]:
    """Every line of one fixture transcript."""
    return [
        json.loads(line) for line in (fixtures() / name).read_text("utf-8").splitlines()
    ]


def started(lines: list[Line], tool: str) -> str:
    """The command the one `tool` call among these lines was given."""

    def blocks(line: Line) -> list[Block]:
        """A line's content blocks, where it has any rather than plain text."""
        content = line.get("message", Message()).get("content", [])
        return [] if isinstance(content, str) else content

    [command] = [
        block.get("input", Input()).get("command", "")
        for line in lines
        if line.get("type") == "assistant"
        for block in blocks(line)
        if block.get("type") == "tool_use" and block.get("name") == tool
    ]
    return command


def stop_over(
    root: Path,
    lines: list[Line],
    agent: tuple[str, str],
    tasks: list[ListedTask],
) -> Payload:
    """A first stop of the agent ``(id, type)``, its transcript written from these lines."""
    path = root / "transcript.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), "utf-8")
    own_id, agent_type = agent
    return Payload(
        hook_event_name=CLAUDE_SUBAGENT_STOP_EVENT,
        agent_id=own_id,
        agent_type=agent_type,
        stop_hook_active=False,
        agent_transcript_path=str(path),
        background_tasks=[
            *tasks,
            ListedTask(id=own_id, type="subagent", status="running"),
        ],
    )


def siblings() -> list[ListedTask]:
    """The recorded fork's siblings as its stop listed them."""
    return [
        ListedTask(
            id=task_id,
            type="subagent",
            status="running",
            description=description,
            agent_type="fork",
        )
        for task_id, description in SIBLINGS.items()
    ]


@RECORDINGS
def test_the_first_stop_refuses_the_report_naming_the_task_it_armed(
    recording: str, task_id: str, own_id: str, tmp_path: Path
) -> None:
    """The monitor is named, the subagent's own entry is not, and the ending call is."""
    guard = laid_out(tmp_path / "plugin")

    answer = json.loads(judged(guard, at_stop(recording, 0)))

    assert answer["decision"] == "block"
    assert task_id in answer["reason"]
    assert own_id not in answer["reason"]
    assert "TaskStop" in answer["reason"]


@RECORDINGS
def test_the_stop_that_follows_a_refusal_goes_through(
    recording: str, task_id: str, own_id: str, tmp_path: Path
) -> None:
    """Once: the runtime flags the second pass, and the task is gone from it anyway."""
    guard = laid_out(tmp_path / "plugin")

    assert judged(guard, at_stop(recording, 1)) == ""


def test_a_task_the_subagent_did_not_arm_is_not_its_to_stop(tmp_path: Path) -> None:
    """A watch the parent armed is in the same list and must not be named."""
    guard = laid_out(tmp_path / "plugin")
    stop = at_stop("payloads-claude-print.jsonl", 0)
    for task in stop.get("background_tasks", []):
        task["command"] = "tail -f somebody-elses.log"

    assert judged(guard, stop) == ""


def test_a_fork_is_not_credited_with_what_its_parent_started(tmp_path: Path) -> None:
    """Measured on 2.1.283: the fold told a fork to stop three siblings it never started.

    A fork's transcript opens with its parent's whole history, spawns and
    shell work included, and the runtime attributes every assistant line to
    the type that wrote it — the parent's, and `fork` on the fork's own. The
    refusal it met displaced its plain-text report as its last message, so
    its caller was handed a line about the siblings instead.
    """
    guard = laid_out(tmp_path / "plugin")
    lines = transcript("transcript-claude-fork.jsonl")
    inherited = ListedTask(
        id="bhf3oc721", type="shell", status="running", command=started(lines, "Bash")
    )

    stop = stop_over(tmp_path, lines, (FORK_ID, "fork"), [*siblings(), inherited])

    assert judged(guard, stop) == ""


def test_a_fork_reports_in_plain_text_whatever_its_parent_was_told(
    tmp_path: Path,
) -> None:
    """The parent's hand-back reminder is inherited history, not the fork's contract.

    Measured on 2.1.283: a fork's `SubagentHandback` answers "not active for
    this agent. Write your report as plain text instead", because the fork
    runs on its parent's tools without a hand-back of its own. So its last
    message is its report, every stop hands it back, and a monitor the fork
    armed itself is refused there as it is wherever a report goes out.
    """
    guard = laid_out(tmp_path / "plugin")
    fork = transcript("transcript-claude-fork.jsonl")
    # The probe's `Monitor` call, the third line of its recorded transcript.
    monitor = transcript("transcript-claude-print.jsonl")[2]
    own: Line = {**monitor, "agentId": FORK_ID, "attributionAgent": "fork"}
    # Through the fork's first prompt, its own monitor, then its plain report:
    # the failed hand-back is left out so that nothing but the inherited
    # reminder could make this stop look like one that is not handing back.
    lines = [*fork[:7], own, fork[9]]
    watch = ListedTask(
        id="bf0g39eun",
        type="shell",
        status="running",
        command=started(lines, "Monitor"),
    )

    answer = json.loads(
        judged(
            guard, stop_over(tmp_path, lines, (FORK_ID, "fork"), [*siblings(), watch])
        )
    )

    assert answer["decision"] == "block"
    assert "bf0g39eun" in answer["reason"]
    assert not any(sibling in answer["reason"] for sibling in SIBLINGS)


def test_a_stop_waiting_on_its_own_work_before_the_hand_back_goes_through(
    tmp_path: Path,
) -> None:
    """Measured on 2.1.283: the fold refused a subagent waiting on the gate it backgrounded.

    It reports through `SubagentHandback`, so ending its turn delivers
    nothing: the runtime tells its caller it "has not reported yet: it is
    waiting on its own background work" and wakes it when that work
    completes. The recorded subagent answered the refusal by arming a monitor
    to wait instead, the duplicate the refusal cost.
    """
    guard = laid_out(tmp_path / "plugin")
    lines = transcript("transcript-claude-handback.jsonl")[:5]
    gate = ListedTask(
        id="b3lur8ohj", type="shell", status="running", command=started(lines, "Bash")
    )

    stop = stop_over(tmp_path, lines, (HANDBACK_ID, "general-purpose"), [gate])

    assert judged(guard, stop) == ""


def test_the_stop_after_the_hand_back_refuses_what_it_leaves_running(
    tmp_path: Path,
) -> None:
    """The report is out, so a watch still listed would only wake a subagent with nothing to add."""
    guard = laid_out(tmp_path / "plugin")
    lines = transcript("transcript-claude-handback.jsonl")
    watch = ListedTask(
        id="btyob9szr",
        type="shell",
        status="running",
        command=started(lines, "Monitor"),
    )

    answer = json.loads(
        judged(
            guard,
            stop_over(tmp_path, lines, (HANDBACK_ID, "general-purpose"), [watch]),
        )
    )

    assert answer["decision"] == "block"
    assert "btyob9szr" in answer["reason"]
    assert "TaskStop" in answer["reason"]


def test_a_subagent_it_started_is_never_its_to_stop(tmp_path: Path) -> None:
    """Recorded on 2.1.283: a subagent ended its turn while the one it started ran.

    A subagent ends on its own and reports through its own hand-back, and
    `TaskStop` refuses one that was resumed — adlib met "owned by" itself —
    so the fold names none, whoever started it.
    """
    guard = laid_out(tmp_path / "plugin")
    [parent, _child] = recorded(
        "payloads-claude-park.jsonl", CLAUDE_SUBAGENT_STOP_EVENT
    )
    parent["agent_transcript_path"] = str(fixtures() / "transcript-claude-park.jsonl")

    assert [task.get("type") for task in parent.get("background_tasks", [])] == [
        "subagent"
    ]
    assert judged(guard, parent) == ""


def test_the_start_event_tells_the_subagent_what_is_its_to_stop(
    tmp_path: Path,
) -> None:
    """One sentence as context, naming the call that ends a task, refusing nothing."""
    guard = laid_out(tmp_path / "plugin")
    [start] = recorded("payloads-claude-print.jsonl", CLAUDE_SUBAGENT_START_EVENT)

    answer = json.loads(judged(guard, start))

    pushed = answer["hookSpecificOutput"]
    assert pushed["hookEventName"] == CLAUDE_SUBAGENT_START_EVENT
    assert "TaskStop" in pushed["additionalContext"]
    assert "decision" not in answer


def test_the_entry_reaches_the_kernel_under_an_isolated_interpreter(
    tmp_path: Path,
) -> None:
    """The host half names its own search path, so no interpreter flag takes it away."""
    laid_out(tmp_path / "plugin")

    isolated = str(
        sh.Command("python3")(
            "-I",
            "-S",
            str(tmp_path / "plugin" / "hooks" / "runtime" / RUNTIME_ENTRY),
            _in=json.dumps(at_stop("payloads-claude-print.jsonl", 0)),
        )
    )

    assert json.loads(isolated)["decision"] == "block"


@pytest.mark.parametrize(
    "event", [CLAUDE_SUBAGENT_START_EVENT, CLAUDE_SUBAGENT_STOP_EVENT]
)
def test_each_subagent_event_registers_the_fold_and_refuses_nothing(
    event: str,
) -> None:
    """Under its own event, with no matcher, and with no `exit 2` beside it."""
    artifacts = shipped(claude_target)
    plugin = CLAUDE_PLUGIN
    hooks = json.loads(artifacts[plugin / "hooks" / "hooks.json"].content)["hooks"]

    [group] = [
        group
        for group in hooks[event]
        if any(GUARD_SCRIPT in entry["command"] for entry in group["hooks"])
    ]
    [entry] = group["hooks"]

    assert "matcher" not in group
    assert "exit 2" not in entry["command"]
    assert artifacts[plugin / "hooks" / "scripts" / GUARD_SCRIPT].executable
    assert plugin / "hooks" / "runtime" / RUNTIME_ENTRY in artifacts


def test_the_other_runtime_tells_a_subagent_what_it_opened_is_its_to_close(
    tmp_path: Path,
) -> None:
    """Over its own recorded start, naming the call that ends what a session runs."""
    guard = laid_out(tmp_path / "plugin", codex_target, CODEX_PLUGIN)
    [start] = recorded("payloads-codex.jsonl", CODEX_SUBAGENT_START_EVENT)

    answer = json.loads(judged(guard, start))

    pushed = answer["hookSpecificOutput"]
    assert pushed["hookEventName"] == CODEX_SUBAGENT_START_EVENT
    assert "write_stdin" in pushed["additionalContext"]
    assert "decision" not in answer


def test_the_sentence_promises_no_refusal_where_none_is_registered(
    tmp_path: Path,
) -> None:
    """Measured: leftovers there resume nobody, so nothing refuses a report there.

    A sentence promising a refusal that never comes is what teaches a
    subagent to discount the next one, so the negative is asserted rather
    than left to the reader of the rendered text.
    """
    guard = laid_out(tmp_path / "plugin", codex_target, CODEX_PLUGIN)
    [start] = recorded("payloads-codex.jsonl", CODEX_SUBAGENT_START_EVENT)

    said = json.loads(judged(guard, start))["hookSpecificOutput"]["additionalContext"]

    assert "refused" not in said
    assert "resumes you" not in said


def test_the_other_runtime_registers_the_start_and_no_stop() -> None:
    """The whole difference between the two trees, read off the registration."""
    artifacts = shipped(codex_target)
    hooks = json.loads(artifacts[CODEX_PLUGIN / "hooks" / "hooks.json"].content)[
        "hooks"
    ]

    folded = {
        event: groups
        for event, groups in hooks.items()
        if any(
            GUARD_SCRIPT in entry["command"]
            for group in groups
            for entry in group["hooks"]
        )
    }

    assert list(folded) == [CODEX_SUBAGENT_START_EVENT]


def test_a_runtime_that_resumes_nobody_takes_the_sentence_alone() -> None:
    """No stop event is how a host half says its leftovers wake no one."""
    quiet = cleanup_hooks(
        Path("plugin"),
        "PLUGIN_ROOT",
        portable_harness().declared_hooks,
        "print()",
        "nowhere",
        CODEX_SUBAGENT_START_EVENT,
        None,
    )

    assert list(quiet.registered) == [CODEX_SUBAGENT_START_EVENT]


def test_a_project_that_declined_registers_nothing_and_carries_nothing() -> None:
    """The declaration is the hook set's own field, so None declines both events."""
    declined = portable_harness().declared_hooks.model_copy(
        update={"subagent_cleanup": None}
    )

    quiet = cleanup_hooks(
        Path("plugin"),
        "PLUGIN_ROOT",
        declined,
        "print()",
        "nowhere",
        CLAUDE_SUBAGENT_START_EVENT,
        CLAUDE_SUBAGENT_STOP_EVENT,
    )

    assert quiet.registered == {}
    assert quiet.artifacts == []


@pytest.mark.parametrize(
    ("target", "plugin", "recording", "event", "ending_call"),
    [
        pytest.param(
            claude_target,
            CLAUDE_PLUGIN,
            "payloads-claude-print.jsonl",
            CLAUDE_SUBAGENT_START_EVENT,
            "TaskStop",
            id="claude",
        ),
        pytest.param(
            codex_target,
            CODEX_PLUGIN,
            "payloads-codex.jsonl",
            CODEX_SUBAGENT_START_EVENT,
            "write_stdin",
            id="codex",
        ),
    ],
)
def test_the_start_event_tells_the_subagent_what_it_verifies_and_commits(
    target: Callable[[Path], NativeHarnessComposition],
    plugin: Path,
    recording: str,
    event: str,
    ending_call: str,
    tmp_path: Path,
) -> None:
    """The other half of what is true at that moment, and the costlier half.

    A delegated agent inherits the repository's guidance and reads, correctly,
    that the full gate is what has to be green — and nothing there says it is
    not the one to run it. So it is told the scoped pair is its to run and
    the gate is whoever lands it. Whether it commits depends on whose tree it
    is in: a builder dispatched into a worktree of its own was told to leave
    the commit to its caller while its brief said to commit, and followed the
    brief — so the sentence names both places rather than assuming one.
    """
    guard = laid_out(tmp_path / "plugin", target, plugin)
    [start] = recorded(recording, event)

    said = json.loads(judged(guard, start))["hookSpecificOutput"]["additionalContext"]

    assert "`uv run lup-devtools dev check --changed` and" in said
    assert "`uv run lup-devtools dev test` over what your change reaches" in said
    assert "leave the full gate to whoever lands it" in said
    assert "In a worktree of your own, commit your work" in said
    assert "in a checkout you share, leave the commit to your caller" in said
    assert "and the commit to your caller" not in said
    # Both halves arrive together or a subagent reads neither.
    assert ending_call in said
