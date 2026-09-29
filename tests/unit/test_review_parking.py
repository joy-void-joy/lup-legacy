"""Where a dashboard reads what parks, every question parks, told to the agent as such.

A parked call is refused while it waits, and a refusal normally means "you
are wrong, change course": an agent reading one the usual way reshapes the
call and spends the review. So the refusal says, in the agent's terms, that
the call is queued rather than refused, not to change it, to carry on, and
how to wait on it -- in the words of the runtime's own tool for waiting. The
operator is told, beside it, where the review waits.
"""

import json
import os
from pathlib import Path

import pytest
import sh

from lup.policy.identity import DASHBOARD_URL_ENV
from lup.policy.relay import QuestionRelay
from lup.types import EnvVars, JsonObject
from tests.unit.native import codex_denial

DASHBOARD = "http://127.0.0.1:8766"
HELD: EnvVars = {DASHBOARD_URL_ENV: DASHBOARD}
QUALITY = "echo carried > marker.txt"
"""A whole-file write: a quality question a supervisor may answer, not a person's alone."""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    return checkout


def hooked(
    root: Path,
    runtime: str,
    command: str,
    environment: EnvVars | None = None,
    agent: str = "",
) -> sh.RunningCommand:
    """One shell call put to a generated hook, from a subagent where *agent* names one."""
    payload: JsonObject = {
        "session_id": "parking-session",
        "cwd": str(root),
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
                if name != DASHBOARD_URL_ENV
            },
            "CLAUDE_PLUGIN_DATA": str(root / "plugin-data"),
            "PLUGIN_DATA": str(root / "plugin-data"),
            **(environment or {}),
        },
    )
    assert isinstance(result, sh.RunningCommand)
    return result


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
    spoken = json.loads(str(hooked(root, "claude", QUALITY, HELD)))

    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()
    reason = spoken["hookSpecificOutput"]["permissionDecisionReason"]
    assert spoken["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert f"Queued for the operator as review {question.id} — not refused." in reason
    assert "Don't change the command; carry on with other work." in reason
    assert f"lup-devtools review wait {question.id}` in the background" in reason
    assert "(run_in_background, with the longest timeout the tool takes, " in reason
    assert "7200000" in reason
    assert "if it is stopped before the operator answers, start it again" in reason
    assert DASHBOARD in reason
    assert question.id in spoken["systemMessage"]
    assert DASHBOARD in spoken["systemMessage"]
    assert question.requirement == "supervisor_allowed"


@pytest.mark.parametrize(
    ("entrypoint", "agent", "background_ends"),
    [("cli", "", False), ("sdk-cli", "", True), ("cli", "a9a4f9ef2f130fd7b", True)],
    ids=["interactive", "print-run", "subagent"],
)
def test_where_a_background_command_ends_with_the_run_the_wait_moves_forward(
    root: Path, entrypoint: str, agent: str, background_ends: bool
) -> None:
    """A `-p` run's background commands end with it, and a foreground subagent's with it."""
    environment = {**HELD, "CLAUDE_CODE_ENTRYPOINT": entrypoint}
    spoken = json.loads(str(hooked(root, "claude", QUALITY, environment, agent)))
    reason = spoken["hookSpecificOutput"]["permissionDecisionReason"]

    assert ("run it in the foreground instead" in reason) is background_ends


def test_codex_is_told_to_leave_its_waiter_running_under_its_shell_tool(
    root: Path,
) -> None:
    reason = codex_denial(hooked(root, "codex", QUALITY))

    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()
    assert f"Queued for the operator as review {question.id} — not refused." in reason
    assert f"lup-devtools review wait {question.id}` with your shell tool" in reason
    assert "run_in_background" not in reason
    assert "review approve" in reason


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_the_wait_a_parked_call_names_is_one_the_policy_lets_through(
    root: Path, runtime: str
) -> None:
    denied = hooked(root, runtime, QUALITY, HELD)
    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()
    reason = (
        codex_denial(denied)
        if runtime == "codex"
        else json.loads(str(denied))["hookSpecificOutput"]["permissionDecisionReason"]
    )
    start = reason.index("`uv run ")
    wait = reason[start + 1 : reason.index("`", start + 1)]
    assert wait.endswith(f"review wait {question.id}")

    waited = hooked(root, runtime, wait, HELD)

    assert waited.exit_code == 0
    assert (
        not str(waited)
        or json.loads(str(waited))["hookSpecificOutput"]["permissionDecision"]
        == "allow"
    )
