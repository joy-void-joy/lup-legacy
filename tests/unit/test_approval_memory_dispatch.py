"""Both compiled dispatchers require explicit receipts and never infer approval."""

import json
import os
from pathlib import Path

import pytest
import sh

from lup.policy.assets.host import approvals_log
from lup.policy.relay import QuestionRelay
from tests.unit.native import codex_effect
from tests.unit.repos import commit_file, initialized_repo

CLAUDE = Path(".claude/plugins/lup/hooks/scripts/policy.py")
CODEX = Path(".codex/plugins/lup/hooks/scripts/policy.py")
COMMAND = "git push --delete origin topic"
"""A command the offered vocabulary asks about wherever it runs."""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    git = initialized_repo(work, tmp_path / "no-hooks")
    commit_file(git, work, "file.txt", "base\n", "chore: base")
    return work


def payload(event: str, root: Path, command: str) -> str:
    return json.dumps(
        {
            "hook_event_name": event,
            "session_id": "requester",
            "turn_id": "turn-one",
            "cwd": str(root),
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
    )


# lup: ignore[dict-str-payload]
def claude(event: str, root: Path, command: str = COMMAND) -> dict[str, object]:
    """Run the Claude dispatcher over one event, returning what it answered."""
    answered = str(
        sh.Command("python3")(
            "-I", "-S", str(CLAUDE), _in=payload(event, root, command)
        )
    )
    return json.loads(answered)


def codex(event: str, root: Path, command: str = COMMAND) -> sh.RunningCommand:
    """Run the Codex dispatcher over one event, returning the finished command."""
    result = sh.Command(str(CODEX.resolve()))(
        _in=payload(event, root, command),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={**os.environ, "PLUGIN_DATA": str(root / "plugin-data")},
    )
    assert isinstance(result, sh.RunningCommand)
    return result


# lup: ignore[dict-str-payload]
def decision(answered: dict[str, object]) -> dict[str, object]:
    specific = answered["hookSpecificOutput"]
    assert isinstance(specific, dict)
    return specific


def refused(result: sh.RunningCommand) -> bool:
    return codex_effect(result) == "deny"


def test_claude_renders_the_question_rather_than_parking_it(repo: Path) -> None:
    """Where no dashboard is held, the verdict goes out as an ask, carrying its reason."""
    asked = decision(claude("PreToolUse", repo))
    assert asked["permissionDecision"] == "ask"
    assert "deleting a remote branch" in str(asked["permissionDecisionReason"])
    assert QuestionRelay(repo / ".lup/questions.jsonl").pending() == []


def test_a_call_that_ran_without_asking_leaves_no_memory(repo: Path) -> None:
    assert decision(claude("PreToolUse", repo, "git status"))["permissionDecision"] == (
        "allow"
    )
    claude("PostToolUse", repo, "git status")

    assert not approvals_log(repo).exists()


def test_a_call_changed_on_the_way_through_approves_nothing(repo: Path) -> None:
    """An unmatched execution cannot answer the pending original proposal."""
    assert refused(codex("PreToolUse", repo))
    relay = QuestionRelay(repo / ".lup/questions.jsonl")
    (question,) = relay.pending()
    codex("PostToolUse", repo, f"{COMMAND} --dry-run")

    assert refused(codex("PreToolUse", repo))
    assert relay.pending() == [question]


def test_codex_unexpected_execution_grants_no_authority(repo: Path) -> None:
    """Observed execution is diagnosed and leaves a retry needing an answer."""
    assert refused(codex("PreToolUse", repo))
    relay = QuestionRelay(repo / ".lup/questions.jsonl")
    (question,) = relay.pending()

    observed = codex("PostToolUse", repo)
    assert observed.exit_code == 2
    assert "without a consumed approval receipt" in observed.stderr.decode()
    uncertain = relay.find(question.id)
    assert uncertain is not None and uncertain.state == "in_doubt"
    assert refused(codex("PreToolUse", repo))


def test_codex_spends_an_explicit_answer_once(repo: Path) -> None:
    assert refused(codex("PreToolUse", repo))
    relay = QuestionRelay(repo / ".lup/questions.jsonl")
    (question,) = relay.pending()
    relay.answer(question.id, "operator", True)
    assert not refused(codex("PreToolUse", repo))
    assert codex("PostToolUse", repo).exit_code == 0
    assert refused(codex("PreToolUse", repo))
