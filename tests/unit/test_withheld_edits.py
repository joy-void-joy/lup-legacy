"""A path withheld from every command is withheld from the file tools too.

The key and login files are refused to any shell word that names them, and
Claude's `Read` deny rules keep its file tools from reading them. Writing one
was still an ordinary edit: `Write` could author a login file, and Codex's
`apply_patch` could replace a key, on every posture. Each runtime's generated
dispatcher is driven here the way its runtime drives it, and the in-process
edit policy beside it, which has to agree.
"""

import json
import os
import sys
from pathlib import Path

import pytest
import sh

from lup.harness.enforcement import semantic_policy_for
from lup.policy.models import EditBatch, EditChange
from lup.types import JsonObject
from lup_template.harness.catalog import declared_hook_set
from tests.unit.repos import initialized_repo

WITHHELD = [
    pytest.param("home/.aws/credentials", id="aws"),
    pytest.param("home/.ssh/id_ed25519", id="ssh-key"),
    pytest.param("checkout/.env.local", id="env-local"),
    pytest.param("home/.codex/auth.json", id="codex-login"),
]
"""Where each withheld pattern lands under a test's own directory.

A pattern spelled from a home names whatever path its trailing names end, so
a home under the test directory is a home as far as the policy can tell."""


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    initialized_repo(root, tmp_path / "no-hooks")
    return root


def met(runtime: str, target: Path, checkout: Path) -> str:
    """The effect authoring *target* meets through this runtime's file tool."""
    tool: JsonObject = (
        {"file_path": str(target), "content": "planted\n"}
        if runtime == "claude"
        else {
            "command": f"*** Begin Patch\n*** Add File: {target}\n+planted\n*** End Patch"
        }
    )
    payload: JsonObject = {
        "session_id": "withheld-probe",
        "hook_event_name": "PreToolUse",
        "cwd": str(checkout),
        "tool_name": "Write" if runtime == "claude" else "apply_patch",
        "tool_input": tool,
    }
    held = {
        name: value
        for name, value in os.environ.items()
        if name not in ("LUP_BOUNDARY_ROOT", "LUP_BOUNDARY_NONCE", "LUP_SANDBOX_ACTIVE")
    }
    result = sh.Command(sys.executable)(
        "-I",
        "-S",
        str(Path(f".{runtime}/plugins/lup/hooks/scripts/policy.py").resolve()),
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={**held, "PLUGIN_DATA": str(checkout.parent / "plugin-data")},
    )
    assert isinstance(result, sh.RunningCommand)
    if result.exit_code == 2:
        return "deny"
    if not str(result):
        return "allow"
    answer = json.loads(str(result))
    specific = answer["hookSpecificOutput"] if "hookSpecificOutput" in answer else {}
    return str(
        specific["permissionDecision"] if "permissionDecision" in specific else "ask"
    )


@pytest.mark.parametrize("spelled", WITHHELD)
def test_authoring_a_withheld_path_is_refused_by_every_runtime(
    runtime: str, checkout: Path, spelled: str
) -> None:
    target = checkout.parent / spelled

    assert met(runtime, target, checkout) == "deny"


@pytest.mark.parametrize("spelled", WITHHELD)
def test_the_composed_edit_policy_refuses_it_alike(
    checkout: Path, spelled: str
) -> None:
    policy = semantic_policy_for(declared_hook_set())
    target = checkout.parent / spelled

    decision = policy.decide(
        EditBatch(
            cwd=checkout,
            changes=[EditChange(path=target, before=None, after="planted\n")],
        )
    )

    assert decision.effect == "deny"
    assert "key or a login" in decision.reason
