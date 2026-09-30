"""Where a dashboard reads what parks, every question parks, told to the agent as such.

A parked call is refused while it waits, and a refusal normally means "you
are wrong, change course": an agent reading one the usual way reshapes the
call and spends the review. So the refusal says, in the agent's terms, that
the call is queued rather than refused, not to change it, to carry on, and
how the conversation that asked hears the answer -- in the words of the
runtime's own tools. A session's own conversation holds no waiter, since the
operator's answer wakes it; a subagent, which nothing else wakes, holds one.
The operator is told, beside it, where the review waits.

The review is kept at the top of the session's checkout, however deep in it
or far from it the call runs, and every review command the refusal names is
spelled to run there, with that checkout's code.
"""

import json
import os
from pathlib import Path

import pytest
import sh

from lup.policy.identity import DASHBOARD_URL_ENV, POLICY_ROOT_ENV
from lup.policy.relay import QuestionRelay
from lup.types import EnvVars, JsonObject
from tests.unit.native import codex_denial

DASHBOARD = "http://127.0.0.1:8766"
HELD: EnvVars = {DASHBOARD_URL_ENV: DASHBOARD}
INTERACTIVE: EnvVars = {**HELD, "CLAUDE_CODE_ENTRYPOINT": "cli"}
QUALITY = "echo carried > marker.txt"
"""A whole-file write: a quality question a supervisor may answer, not a person's alone."""

SUBAGENT = "a9a4f9ef2f130fd7b"


def checkout_at(path: Path) -> Path:
    """A directory Git would call a checkout: a `.git` holding a HEAD."""
    (path / ".git").mkdir(parents=True)
    (path / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return checkout_at(tmp_path / "checkout")


def hooked(
    root: Path,
    runtime: str,
    command: str,
    environment: EnvVars | None = None,
    agent: str = "",
    cwd: Path | None = None,
) -> sh.RunningCommand:
    """One shell call put to a generated hook, from a subagent where *agent* names one."""
    payload: JsonObject = {
        "session_id": "parking-session",
        "cwd": str(cwd or root),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        **({"agent_id": agent} if agent else {}),
    }
    script = Path(f".{runtime}/plugins/lup/hooks/scripts/policy.py").resolve()
    result = sh.Command(str(script))(
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={
            **{
                name: value
                for name, value in os.environ.items()
                if name not in (DASHBOARD_URL_ENV, POLICY_ROOT_ENV)
            },
            "CLAUDE_PLUGIN_DATA": str(root / "plugin-data"),
            "PLUGIN_DATA": str(root / "plugin-data"),
            **(environment or {}),
        },
    )
    assert isinstance(result, sh.RunningCommand)
    return result


def refusal(result: sh.RunningCommand, runtime: str) -> str:
    """What the agent is told of a parked call, on either runtime."""
    if runtime == "codex":
        return codex_denial(result)
    return json.loads(str(result))["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_supervisor_s_question_keeps_the_prompt_where_no_dashboard_reads(
    root: Path,
) -> None:
    spoken = json.loads(str(hooked(root, "claude", QUALITY)))

    assert spoken["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "systemMessage" not in spoken
    assert QuestionRelay(root / ".lup/questions.jsonl").pending() == []


def test_with_a_dashboard_held_the_same_question_is_parked_and_said_to_be(
    root: Path,
) -> None:
    spoken = json.loads(str(hooked(root, "claude", QUALITY, INTERACTIVE)))

    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()
    reason = spoken["hookSpecificOutput"]["permissionDecisionReason"]
    assert spoken["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert f"Queued for the operator as review {question.id} — not refused." in reason
    assert "Don't change the command." in reason
    assert DASHBOARD in reason
    assert question.id in spoken["systemMessage"]
    assert DASHBOARD in spoken["systemMessage"]
    assert question.requirement == "supervisor_allowed"


def test_a_session_s_own_conversation_is_woken_by_the_answer_and_holds_no_waiter(
    root: Path,
) -> None:
    """A waiter it held ended at the tool's two-hour limit and woke it for nothing."""
    reason = refusal(hooked(root, "claude", QUALITY, INTERACTIVE), "claude")
    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()

    assert (
        "Carry on with other work, or end your turn: the operator's answer wakes "
        "this session, and "
        f"`uv run --directory {root} lup-devtools review wait {question.id}` then "
        "carries the call out at once. Don't start a waiter."
    ) in reason
    assert "run_in_background" not in reason
    assert "--timeout" not in reason


def test_a_subagent_holds_a_background_waiter_and_restarts_it_quietly(
    root: Path,
) -> None:
    """Nothing but its own background work wakes a subagent."""
    reason = refusal(
        hooked(root, "claude", QUALITY, INTERACTIVE, agent=SUBAGENT), "claude"
    )
    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()

    assert (
        f"hold `uv run --directory {root} lup-devtools review wait {question.id} "
        "--timeout 7140` in the background (run_in_background, with the longest "
        "timeout the tool takes, 7200000 ms)"
    ) in reason
    assert "start it again quietly, reporting that to nobody" in reason
    assert "Don't start a waiter" not in reason
    assert question.agent == SUBAGENT


def test_a_print_run_waits_in_the_foreground_once_nothing_else_is_left(
    root: Path,
) -> None:
    """A `-p` run's background commands end with it, and nothing wakes a run that ended."""
    environment = {**HELD, "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}
    reason = refusal(hooked(root, "claude", QUALITY, environment), "claude")

    assert "--timeout 540` in the foreground" in reason
    assert "run_in_background" not in reason


def test_codex_s_own_thread_is_woken_by_the_queue_and_holds_no_waiter(
    root: Path,
) -> None:
    reason = codex_denial(hooked(root, "codex", QUALITY))

    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()
    assert f"Queued for the operator as review {question.id} — not refused." in reason
    assert "the operator's answer is queued into this thread, which starts a turn" in (
        reason
    )
    assert (
        f"`uv run --directory {root} lup-devtools review wait {question.id}` then "
        "carries the call out at once. Don't start a waiter."
    ) in reason
    assert "run_in_background" not in reason
    assert "review approve" in reason


def test_a_codex_subagent_holds_its_waiter_until_it_reports(root: Path) -> None:
    """A Codex subagent's last message is its report, and nothing wakes it after."""
    reason = codex_denial(hooked(root, "codex", QUALITY, agent=SUBAGENT))

    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()
    assert (
        f"hold `uv run --directory {root} lup-devtools review wait {question.id}` "
        "in your shell tool, reading its output before you report"
    ) in reason
    assert "start it again quietly, reporting that to nobody" in reason
    assert question.agent == SUBAGENT


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_a_call_from_a_subdirectory_parks_in_the_checkout_s_own_queue(
    root: Path, runtime: str
) -> None:
    """The dashboard reads the checkout's queue, never one a subdirectory would keep."""
    below = root / "docs" / "guide"
    below.mkdir(parents=True)

    reason = refusal(hooked(root, runtime, QUALITY, INTERACTIVE, cwd=below), runtime)

    assert not (below / ".lup").exists()
    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()
    assert question.operation.cwd == below
    assert question.operation.worktree == root
    assert f"uv run --directory {root} lup-devtools review wait {question.id}" in (
        reason
    )


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_a_call_into_another_checkout_parks_in_the_launch_checkout_s_queue(
    tmp_path: Path, runtime: str
) -> None:
    """Kept where the launch lends its answers, read with the session's own code.

    The review names the checkout the call changes, and the command waiting
    on it runs in the launch checkout.
    """
    launched = checkout_at(tmp_path / "dev")
    sibling = checkout_at(tmp_path / "feature")
    environment = {
        **INTERACTIVE,
        "LUP_BOUNDARY_ROOT": str(launched),
        POLICY_ROOT_ENV: str(launched),
    }

    reason = refusal(
        hooked(launched, runtime, QUALITY, environment, cwd=sibling), runtime
    )

    assert not (sibling / ".lup/questions.jsonl").exists()
    (question,) = QuestionRelay(launched / ".lup/questions.jsonl").pending()
    assert question.operation.cwd == sibling
    assert question.operation.worktree == sibling
    wait = f"uv run --directory {launched} lup-devtools review wait {question.id}"
    assert wait in reason
    assert "--project" not in reason


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_the_wait_a_parked_call_names_is_one_the_policy_lets_through(
    root: Path, runtime: str
) -> None:
    denied = hooked(root, runtime, QUALITY, INTERACTIVE)
    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()
    reason = refusal(denied, runtime)
    start = reason.index("`uv run ")
    wait = reason[start + 1 : reason.index("`", start + 1)]
    assert f"review wait {question.id}" in wait

    waited = hooked(root, runtime, wait, HELD)

    assert waited.exit_code == 0
    assert (
        not str(waited)
        or json.loads(str(waited))["hookSpecificOutput"]["permissionDecision"]
        == "allow"
    )
