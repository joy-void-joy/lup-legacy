"""A sandbox grant declared from a home is read in the home reading it.

The declared grants join a launch's writable roots spelled as they were
written -- `~/.cache/uv` -- because they answer for whichever home reads the
ledger. Resolved without expanding `~`, that spelling names a directory called
`~` under the working directory, so every write into the uv cache the grant
exists for would be refused as outside the boundary, while `touch` on the same
path, which no reader resolves, goes through.

Driven the way a session meets it: each runtime's generated dispatcher, whose
host half is compiled from the same module the composed policy reads.
"""

import json
import os
import sys
from pathlib import Path
from typing import Literal

import pytest
import sh

from lup.types import JsonObject
from tests.unit.native import codex_effect
from tests.unit.repos import initialized_repo

type Runtime = Literal["claude", "codex"]

DISPATCHERS: dict[Runtime, Path] = {
    "claude": Path(".claude/plugins/lup/hooks/scripts/policy.py"),
    "codex": Path(".codex/plugins/lup/hooks/scripts/policy.py"),
}


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> Runtime:
    return request.param


@pytest.fixture
def launched(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout whose launch leased itself and granted the home's uv cache."""
    checkout = tmp_path / "checkout"
    home = tmp_path / "home"
    initialized_repo(checkout, tmp_path / "no-hooks")
    (home / ".cache" / "uv").mkdir(parents=True)
    ledger = checkout / ".lup" / "preflight" / "launch.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps({"writable_roots": [str(checkout), "~/.cache/uv"]}),
        encoding="utf-8",
    )
    return checkout, home


def met(runtime: Runtime, command: str, checkout: Path, home: Path) -> str:
    """The effect a shell call meets, with the launch's ledger named."""
    payload: JsonObject = {
        "session_id": "grant-probe",
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
        _env={
            **os.environ,
            "HOME": str(home),
            "LUP_BOUNDARY_NONCE": "launch",
            "LUP_BOUNDARY_ROOT": str(checkout),
            "PLUGIN_DATA": str(checkout.parent / "plugin-data"),
        },
    )
    assert isinstance(result, sh.RunningCommand)
    if runtime == "codex":
        return (
            "ask" if result.exit_code == 0 and result.stdout else codex_effect(result)
        )
    if result.exit_code == 2:
        return "deny"
    specific = json.loads(str(result))["hookSpecificOutput"]
    return str(specific["permissionDecision"])


def test_a_write_into_the_granted_cache_is_inside_the_boundary(
    runtime: Runtime, launched: tuple[Path, Path]
) -> None:
    checkout, home = launched
    cached = home / ".cache" / "uv" / "probe.txt"

    assert met(runtime, f"echo x > {cached}", checkout, home) != "deny"


def test_a_write_beside_the_granted_cache_is_still_outside_it(
    runtime: Runtime, launched: tuple[Path, Path]
) -> None:
    checkout, home = launched

    assert met(runtime, f"echo x > {home / '.bashrc'}", checkout, home) == "deny"
