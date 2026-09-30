"""A parked review is labelled with the checkout it changes, not the one that asked.

A session in one checkout editing a file in a sibling worktree parks its
review in its own queue, since that is where its hook runs; the reviewer
reads the label to learn what the call touches, so the label names the
target file's checkout.
"""

import json
import os
from pathlib import Path

import sh

from lup.policy.identity import DASHBOARD_URL_ENV
from lup.policy.relay import QuestionRelay
from lup.types import JsonObject

DISPATCHER = Path(".claude/plugins/lup/hooks/scripts/policy.py")


def checkout(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    (path / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    return path


def test_an_edit_of_another_checkout_is_labelled_with_that_checkout(
    tmp_path: Path,
) -> None:
    asking = checkout(tmp_path / "tree" / "dev")
    target = checkout(tmp_path / "tree" / "feature") / "README.md"
    target.write_text("# Before\n")
    payload: JsonObject = {
        "session_id": "labelling-session",
        "cwd": str(asking),
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "# After\n"},
    }

    sh.Command(str(DISPATCHER.resolve()))(
        _in=json.dumps(payload),
        _env={
            **os.environ,
            "CLAUDE_PLUGIN_DATA": str(tmp_path / "plugin-data"),
            DASHBOARD_URL_ENV: "http://127.0.0.1:8766",
        },
    )

    (question,) = QuestionRelay(asking / ".lup/questions.jsonl").pending()
    assert question.operation.cwd == asking
    assert question.operation.worktree == target.parent
    assert question.operation.summary() == f"Write in {target.parent}"
