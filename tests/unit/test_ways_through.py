"""A refusal offers its own ways through, and "change the command" only where it has none.

"Change the command" is the way past every refusal, so beside a rule's own
way through it says nothing the agent did not know. The escalation stays
either way: no rule's own step names it. Each runtime's generated hook is
run on the payload its harness sends, and beside it `dev policy`'s reading,
which has to offer the same ways the hook does.
"""

import json
import os
import sys
from pathlib import Path
from typing import Literal

import pytest
import sh

from lup.devtools.dev.policy_explain import verdict_for
from lup.policy.kernel.diagnostic import way
from lup.types import JsonObject
from lup_template.harness.catalog import declared_hook_set
from tests.unit.native import codex_denial
from tests.unit.repos import initialized_repo

type Runtime = Literal["claude", "codex"]

DISPATCHERS: dict[Runtime, Path] = {
    "claude": Path(".claude/plugins/lup/hooks/scripts/policy.py"),
    "codex": Path(".codex/plugins/lup/hooks/scripts/policy.py"),
}

RESHAPE = "→ change the command to one the policy allows"
ESCALATE = (
    "→ or resubmit it with a first line `# lup: escalate[decision]: <why>`,"
    " which puts it to a reviewer"
)
# A rule with ways of its own: pip names uv.
OWN_WAYS = "pip install requests"
# A refusal naming none: no global option reads as the command uv will run.
NO_WAYS = "uv --frobnicate run ls"


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> Runtime:
    return request.param


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Where every call here is made from."""
    initialized_repo(tmp_path / "checkout", tmp_path / "no-hooks")
    return tmp_path / "checkout"


def refused(runtime: Runtime, command: str, checkout: Path) -> list[str]:
    """The ways through a runtime's hook sends with its refusal, one a line."""
    payload: JsonObject = {
        "session_id": "ways-probe",
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
        reason = codex_denial(result)
    else:
        specific = json.loads(str(result))["hookSpecificOutput"]
        assert specific["permissionDecision"] == "deny"
        reason = str(specific["permissionDecisionReason"])
    return [line for line in reason.splitlines() if line.startswith("→")]


def previewed(command: str, checkout: Path) -> list[list[str]]:
    """The ways through `dev policy` offers under each placement it reads."""
    verdict = verdict_for(command, "shell", False, checkout, declared_hook_set())
    return [
        [way(through) for through in reading.said["steps"]]
        for reading in verdict.readings
        if reading.effect == "deny"
    ]


def test_a_refusal_with_ways_of_its_own_leaves_out_change_the_command(
    runtime: Runtime, checkout: Path
) -> None:
    ways = refused(runtime, OWN_WAYS, checkout)

    assert ways[0].startswith("→ add the package through uv")
    assert RESHAPE not in ways
    assert ways[-1] == ESCALATE


def test_a_refusal_with_no_way_of_its_own_keeps_both_generic_ones(
    runtime: Runtime, checkout: Path
) -> None:
    assert refused(runtime, NO_WAYS, checkout) == [RESHAPE, ESCALATE]


@pytest.mark.parametrize("command", [OWN_WAYS, NO_WAYS])
def test_dev_policy_offers_the_ways_the_hook_sends(
    runtime: Runtime, checkout: Path, command: str
) -> None:
    sent = refused(runtime, command, checkout)
    readings = previewed(command, checkout)

    assert readings
    assert all(ways == sent for ways in readings)
