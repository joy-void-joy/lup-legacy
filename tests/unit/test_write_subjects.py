"""A write or a delete names the paths it touches, as the policy placed them.

What an approver weighs is the path: `rm` over a scratch file and over
somebody's is one rule and two questions. So the verdict's subject is the
command with the paths it writes or deletes, and a path a `cd` left unknown
reads `$PWD/...`, which is why the question is asked at all. Each runtime's
generated hook is run on the payload its harness sends, and `dev policy`
beside it.
"""

import json
import os
import sys
from pathlib import Path
from typing import Literal

import pytest
import sh

from lup.devtools.dev.policy_explain import verdict_for
from lup.policy.kernel.diagnostic import headline
from lup.types import JsonObject
from lup_template.harness.catalog import declared_hook_set
from tests.unit.native import codex_denial
from tests.unit.repos import initialized_repo

type Runtime = Literal["claude", "codex"]

DISPATCHERS: dict[Runtime, Path] = {
    "claude": Path(".claude/plugins/lup/hooks/scripts/policy.py"),
    "codex": Path(".codex/plugins/lup/hooks/scripts/policy.py"),
}

NAMED = [
    pytest.param(
        "rm /etc/hosts", "`rm /etc/hosts` — deleting files requires approval", id="rm"
    ),
    pytest.param(
        "cd $T && rm -rf tmp/x",
        "`rm $PWD/tmp/x` — deleting files requires approval",
        id="rm-after-an-unknown-cd",
    ),
    pytest.param(
        "cd $T && echo hi > out.txt",
        "`$PWD/out.txt` — the redirection target is a path that is only known"
        " when the command runs",
        id="redirection-after-an-unknown-cd",
    ),
]


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> Runtime:
    return request.param


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Where every call here is made from."""
    initialized_repo(tmp_path / "checkout", tmp_path / "no-hooks")
    return tmp_path / "checkout"


def said(runtime: Runtime, command: str, checkout: Path) -> str:
    """What a runtime's hook tells the session about one shell call."""
    payload: JsonObject = {
        "session_id": "subject-probe",
        "hook_event_name": "PreToolUse",
        "cwd": str(checkout),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    result = sh.Command(sys.executable)(
        "-I",
        "-S",
        str(DISPATCHERS[runtime].resolve()),
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={**os.environ, "PLUGIN_DATA": str(checkout.parent / "plugin-data")},
    )
    assert isinstance(result, sh.RunningCommand)
    if runtime == "codex":
        return codex_denial(result)
    specific = json.loads(str(result))["hookSpecificOutput"]
    return str(specific.get("permissionDecisionReason", ""))


@pytest.mark.parametrize(("command", "subject"), NAMED)
def test_the_hook_names_the_paths_a_write_touches(
    runtime: Runtime, checkout: Path, command: str, subject: str
) -> None:
    assert subject in said(runtime, command, checkout)


@pytest.mark.parametrize(("command", "subject"), NAMED)
def test_dev_policy_names_the_same_paths(
    checkout: Path, command: str, subject: str
) -> None:
    verdict = verdict_for(command, "shell", False, checkout, declared_hook_set())

    assert verdict.readings
    assert all(subject in headline(reading.said) for reading in verdict.readings)
