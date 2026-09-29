"""A policy hook answers before its runtime stops listening, whatever it waits on.

Both runtimes let a call through once its policy hook overruns its timeout, so
a hook still waiting then has answered nothing. Each generated dispatcher is
driven here the way its runtime drives it, under a deadline the test sets a
few seconds out -- a deadline a parent set is never extended -- so a wait that
would outlast the runtime is seen to end in a verdict instead.
"""

import fcntl
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest
import sh

import lup.policy.assets.host as policy_host
from lup.types import JsonObject
from lup_template.harness.catalog import declared_hook_set
from tests.unit.repos import initialized_repo

INHERITED = 3.0
"""Seconds from now the test's own deadline stands, which the hook inherits."""

RUNTIME_LIMIT = declared_hook_set().policy_timeout
"""Where each runtime stops listening and lets the call through."""


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def checkout(tmp_path: Path, runtime: str) -> Path:
    """A repository carrying this runtime's generated hooks, and nothing launched."""
    root = tmp_path / "checkout"
    initialized_repo(root, tmp_path / "no-hooks")
    shutil.copytree(
        Path(f".{runtime}/plugins/lup/hooks"), root / f".{runtime}/plugins/lup/hooks"
    )
    return root


def judged(
    checkout: Path, runtime: str, tool_name: str, tool_input: JsonObject
) -> tuple[str, str, float]:
    """The effect one PreToolUse call meets, what it said, and how long it took."""
    payload: JsonObject = {
        "session_id": "deadline-probe",
        "hook_event_name": "PreToolUse",
        "cwd": str(checkout),
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
    held = {
        name: value
        for name, value in os.environ.items()
        if name
        not in (
            "LUP_BOUNDARY_ROOT",
            "LUP_BOUNDARY_NONCE",
            "LUP_SANDBOX_ACTIVE",
            "LUP_HOOK_DEADLINE",
        )
    }
    started = time.monotonic()
    result = sh.Command(sys.executable)(
        "-I",
        "-S",
        str(checkout / f".{runtime}/plugins/lup/hooks/scripts/policy.py"),
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        # Past the runtime's own limit a hook has already failed open, so a
        # hook still running then fails the test instead of hanging it.
        _timeout=RUNTIME_LIMIT,
        _env={
            **held,
            "PLUGIN_DATA": str(checkout.parent / "plugin-data"),
            "LUP_HOOK_DEADLINE": repr(time.monotonic() + INHERITED),
        },
    )
    elapsed = time.monotonic() - started
    assert isinstance(result, sh.RunningCommand)
    if result.exit_code == 2:
        return "deny", result.stderr.decode(), elapsed
    if not str(result):
        return "allow", "", elapsed
    answer = json.loads(str(result))
    specific = answer["hookSpecificOutput"] if "hookSpecificOutput" in answer else {}
    effect = (
        specific["permissionDecision"] if "permissionDecision" in specific else "allow"
    )
    return str(effect), json.dumps(answer), elapsed


def test_a_language_server_that_never_answers_is_answered_as_one_nobody_ran(
    checkout: Path, runtime: str
) -> None:
    """The resolver would hold the hook past the runtime's limit; the gate asks in time.

    A rule whose verdict turns on a resolved receiver waits on the declared
    resolver, which here sleeps a minute. Cut short at the inherited deadline,
    it reads as no checker having looked, which the gate answers by asking --
    and Codex, with no ask at this boundary, by the refusal a parked question is.
    """
    resolver = checkout / "stalled-resolver"
    resolver.write_text("#!/bin/sh\nexec sleep 60\n", encoding="utf-8")
    resolver.chmod(0o755)
    data = checkout / f".{runtime}/plugins/lup/hooks/runtime/policy_data.py"
    data.write_text(
        data.read_text(encoding="utf-8")
        + f"\nRESOLUTION_COMMAND = [{str(resolver)!r}]\n",
        encoding="utf-8",
    )
    target = checkout / "engine.py"
    target.write_text('def read(client):\n    return client.get("old")\n')
    old, new = '    return client.get("old")', '    return client.get("new")'
    edit: JsonObject = {"file_path": str(target), "old_string": old, "new_string": new}
    patch: JsonObject = {
        "command": f"*** Begin Patch\n*** Update File: {target}\n@@\n"
        f"-{old}\n+{new}\n*** End Patch"
    }

    effect, detail, elapsed = (
        judged(checkout, runtime, "Edit", edit)
        if runtime == "claude"
        else judged(checkout, runtime, "apply_patch", patch)
    )

    assert elapsed < INHERITED + 5
    assert effect == ("ask" if runtime == "claude" else "deny")
    assert "dict-get" in detail


@pytest.mark.parametrize("runtime", ["codex"])
def test_a_wait_no_step_can_bound_is_refused_at_the_deadline(
    checkout: Path, runtime: str
) -> None:
    """A review queue another writer holds locked stops the hook at its alarm, refused.

    Codex parks every question in the queue, and reading it waits on the lock
    its writers share -- a wait no timeout can be handed to. Past the deadline
    the alarm raises where the hook is waiting, and the call is refused as one
    the hook could not judge, instead of outlasting the runtime into an allow.
    """
    relay = checkout / ".lup" / "questions.jsonl"
    relay.parent.mkdir(parents=True)
    relay.write_text("", encoding="utf-8")

    with relay.open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        effect, detail, elapsed = judged(
            checkout, runtime, "Bash", {"command": "sudo id"}
        )

    assert elapsed < INHERITED + 5
    assert effect == "deny"
    assert "reached its deadline before a verdict" in detail
    assert "Malformed hook input" not in detail


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_a_call_nobody_could_read_is_refused_as_malformed(
    checkout: Path, runtime: str
) -> None:
    """A refusal names its cause: input that is not a hook payload at all."""
    result = sh.Command(sys.executable)(
        "-I",
        "-S",
        str(checkout / f".{runtime}/plugins/lup/hooks/scripts/policy.py"),
        _in="not json",
        _ok_code=[0, 2],
        _return_cmd=True,
        _timeout=RUNTIME_LIMIT,
        _env={**os.environ, "PLUGIN_DATA": str(checkout.parent / "plugin-data")},
    )
    assert isinstance(result, sh.RunningCommand)
    said = result.stderr.decode() if result.exit_code == 2 else str(result)

    assert runtime == "claude" or result.exit_code == 2
    assert '"permissionDecision": "deny"' in said or result.exit_code == 2
    assert "hook input is malformed" in said
    assert "deadline" not in said


def test_the_alarm_interrupts_a_wait_nothing_else_bounds() -> None:
    started = time.monotonic()
    previous = policy_host.opened_deadline(0.0, grace=0.2)
    try:
        with pytest.raises(RuntimeError, match="deadline"):
            time.sleep(5)
    finally:
        policy_host.closed_deadline(previous)

    assert time.monotonic() - started < 2


def test_a_deadline_already_set_is_never_extended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process the hook starts inherits its deadline, and cannot push it back."""
    monkeypatch.setenv("LUP_HOOK_DEADLINE", repr(time.monotonic() + 2.0))

    previous = policy_host.opened_deadline(25.0)
    try:
        left = policy_host.hook_seconds_left(30.0)
    finally:
        policy_host.closed_deadline(previous)

    assert left <= 2.0
    assert policy_host.hook_seconds_left(30.0) <= 2.0
    monkeypatch.delenv("LUP_HOOK_DEADLINE")
    assert policy_host.hook_seconds_left(30.0) == 30.0
