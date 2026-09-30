"""A deferral is never a parked review, on either runtime.

A change too large for the small-change gate is the runtime's to answer, as
the operator set that runtime up to answer it: Claude Code's own mode, or
Codex's own approval policy. So the hook answers with no decision, and lup
parks nothing, whichever spelling carried the change -- a copy, a heredoc, an
in-place `sed`, a native edit -- and with a `# lup: escalate[decision]:` line
over it too.
"""

import json
import os
from pathlib import Path

import pytest
import sh

from lup.policy.identity import DASHBOARD_URL_ENV
from lup.policy.relay import QuestionRelay
from lup.types import EnvVars, JsonObject
from tests.unit.repos import initialized_repo
from tests.unit.test_copy_is_an_edit import MODULE, module_text, scratch

HELD: EnvVars = {DASHBOARD_URL_ENV: "http://127.0.0.1:8766"}
"""A launch holding a dashboard, where every ask parks."""


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout with one committed production module and a scratch tree."""
    root = tmp_path / "checkout"
    git = initialized_repo(root, tmp_path / "hooks")
    (root / "src/app").mkdir(parents=True)
    (root / MODULE).write_text(module_text(8), encoding="utf-8")
    (root / "tmp").mkdir()
    git("add", "-A")
    git("commit", "-m", "base")
    return root


def hooked(
    root: Path, runtime: str, payload: JsonObject, environment: EnvVars
) -> sh.RunningCommand:
    """One call put to a generated dispatcher of this checkout's plugin trees."""
    script = Path(f".{runtime}/plugins/lup/hooks/scripts/policy.py").resolve()
    result = sh.Command(str(script))(
        _in=json.dumps({"session_id": "deferral", "cwd": str(root), **payload}),
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
            **environment,
        },
    )
    assert isinstance(result, sh.RunningCommand)
    return result


def deferring_commands(checkout: Path) -> list[str]:
    """Every shell spelling of one change too large for the small-change gate."""
    large = scratch(
        checkout,
        "large.py",
        module_text(8) + "".join(f"G{index} = {index}\n" for index in range(12)),
    )
    appended = "".join(f"H{index} = {index}\n" for index in range(12))
    return [
        f"cp {large} {MODULE}",
        f"cat >> {MODULE} <<'EOF'\n{appended}EOF",
        f"sed -i 's/return \\([0-9]\\)$/return \\1 + 0/' {MODULE}",
        f"# lup: escalate[decision]: it is a large change\ncp {large} {MODULE}",
    ]


@pytest.mark.parametrize("held", [False, True], ids=["prompt", "dashboard"])
def test_a_deferral_reaches_claude_as_no_decision_and_parks_nothing(
    checkout: Path, held: bool
) -> None:
    """Claude Code's own mode answers: its classifier, or its prompt."""
    for command in deferring_commands(checkout):
        spoken = json.loads(
            str(
                hooked(
                    checkout,
                    "claude",
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    },
                    HELD if held else {},
                )
            )
            or "{}"
        )

        answer = spoken.get("hookSpecificOutput", {})
        assert "permissionDecision" not in answer, (command, spoken)
        assert "systemMessage" not in spoken, command
    assert QuestionRelay(checkout / ".lup/questions.jsonl").pending() == []


@pytest.mark.parametrize("event", ["PreToolUse", "PermissionRequest"])
def test_a_deferral_reaches_codex_as_its_own_approval_and_parks_nothing(
    checkout: Path, event: str
) -> None:
    """Codex's own approval policy answers, and a permission request is left to it."""
    for command in deferring_commands(checkout):
        answered = hooked(
            checkout,
            "codex",
            {
                "hook_event_name": event,
                "tool_name": "Bash",
                "tool_input": {"command": command},
            },
            HELD,
        )

        assert answered.exit_code == 0, (command, answered.stderr)
        assert answered.stdout.decode().strip() == "", (command, answered.stdout)
    assert QuestionRelay(checkout / ".lup/questions.jsonl").pending() == []


def test_a_large_edit_parks_on_neither_runtime(checkout: Path) -> None:
    """The same change as a native edit: Claude's `Edit`, Codex's `apply_patch`."""
    before = (checkout / MODULE).read_text(encoding="utf-8")
    added = "".join(f"E{index} = {index}\n" for index in range(12))
    claude = json.loads(
        str(
            hooked(
                checkout,
                "claude",
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Edit",
                    "tool_input": {
                        "file_path": str(checkout / MODULE),
                        "old_string": "    return 7\n",
                        "new_string": "    return 7\n" + added,
                    },
                },
                HELD,
            )
        )
        or "{}"
    )
    patch = (
        "*** Begin Patch\n"
        f"*** Update File: {MODULE}\n"
        "@@\n"
        " def f7() -> int:\n"
        "     return 7\n"
        + "".join(f"+{line}\n" for line in added.splitlines())
        + "*** End Patch\n"
    )
    codex = hooked(
        checkout,
        "codex",
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": patch},
        },
        HELD,
    )

    assert "permissionDecision" not in claude.get("hookSpecificOutput", {}), claude
    assert codex.exit_code == 0, codex.stderr
    assert codex.stdout.decode().strip() == ""
    assert (checkout / MODULE).read_text(encoding="utf-8") == before
    assert QuestionRelay(checkout / ".lup/questions.jsonl").pending() == []
