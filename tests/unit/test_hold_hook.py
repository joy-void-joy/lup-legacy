"""The hold hook keeps every tool call of a paused agent waiting, whatever the tool.

Each generated hold hook is run here the way its runtime runs it: the guard
from the plugin, in a repository whose coordination store the test pauses and
resumes. Nothing placed, it answers at once without starting an interpreter's
worth of work; paused, it says nothing until the pause is lifted, and a call
still held at the hold's limit is refused in the hold's own words.
"""

import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest
import sh

from lup.coordination.bare.store import subagent_id
from lup.coordination.holds import (
    HoldOwner,
    HoldReason,
    HoldScope,
    held_calls,
    lift,
    operator_pause,
    place,
)
from lup.coordination.meeting import coordination_root
from lup.coordination.repository import RepositoryPeers
from lup.types import JsonObject
from tests.unit.repos import initialized_repo

SESSION = "a1b2c3d4e5f6"
"""The member a launch named the session whose calls are held."""


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def checkout(tmp_path: Path, runtime: str) -> Path:
    """A repository carrying this runtime's generated hooks, with one session on its roster."""
    root = tmp_path / "checkout"
    initialized_repo(root, tmp_path / "no-hooks")
    shutil.copytree(
        Path(f".{runtime}/plugins/lup/hooks"), root / f".{runtime}/plugins/lup/hooks"
    )
    RepositoryPeers(root).join(SESSION, root, cli_name="lead")
    return root


def held(
    checkout: Path, runtime: str, agent: str = "", member: str = SESSION
) -> tuple[int, str, float]:
    """Run the hold hook for one Bash call: its exit, what it printed, how long it took."""
    payload: JsonObject = {
        "session_id": "hold-probe",
        "hook_event_name": "PreToolUse",
        "cwd": str(checkout),
        "tool_name": "Read",
        "tool_input": {"file_path": "README.md"},
        "tool_use_id": "toolu_probe",
        **({"agent_id": agent} if agent else {}),
    }
    started = time.monotonic()
    result = sh.Command("sh")(
        str(checkout / f".{runtime}/plugins/lup/hooks/scripts/coordination_hold.sh"),
        _in=json.dumps(payload),
        _cwd=str(checkout),
        _ok_code=[0, 2],
        _return_cmd=True,
        _timeout=60,
        _env={
            "PATH": os.environ["PATH"],
            **({"LUP_COORDINATION_MEMBER": member} if member else {}),
        },
    )
    assert isinstance(result, sh.RunningCommand)
    return result.exit_code, str(result), time.monotonic() - started


class Resume(threading.Thread):
    """The operator's resume, given once the hook has marked the call it holds.

    Waiting for the mark rather than for a set time is what keeps this true
    on a loaded machine: the hook's interpreter may take seconds to start.
    """

    def __init__(self, store: Path, scope: HoldScope, member: str = "") -> None:
        super().__init__(daemon=True)
        self.store, self.scope, self.member = store, scope, member
        self.marked: list[str] = []
        self.at = 0.0
        self.start()

    def run(self) -> None:
        for _ in range(600):
            calls = held_calls(self.store)
            if calls:
                self.marked = [call.member for call in calls]
                break
            time.sleep(0.1)
        self.at = time.monotonic()
        lift(self.store, HoldOwner.OPERATOR, HoldReason.PAUSED, self.scope, self.member)


def test_a_call_nothing_holds_goes_on_at_once(checkout: Path, runtime: str) -> None:
    status, said, elapsed = held(checkout, runtime)
    assert (status, said) == (0, "")
    assert elapsed < 5


def test_a_session_nothing_launched_is_never_held(checkout: Path, runtime: str) -> None:
    place(coordination_root(checkout), operator_pause(HoldScope.REPOSITORY))
    status, said, elapsed = held(checkout, runtime, member="")
    assert (status, said) == (0, "")
    assert elapsed < 5


def test_a_paused_session_s_read_waits_silently_until_it_is_resumed(
    checkout: Path, runtime: str
) -> None:
    store = coordination_root(checkout)
    place(store, operator_pause(HoldScope.AGENT, SESSION))
    resume = Resume(store, HoldScope.AGENT, SESSION)
    status, said, _ = held(checkout, runtime)
    ended = time.monotonic()
    resume.join()
    assert (status, said) == (0, "")
    assert resume.marked == [SESSION]
    assert ended >= resume.at
    assert held_calls(store) == []


def test_a_subagent_is_held_by_its_session_s_pause(
    checkout: Path, runtime: str
) -> None:
    store = coordination_root(checkout)
    place(store, operator_pause(HoldScope.AGENT, SESSION))
    resume = Resume(store, HoldScope.AGENT, SESSION)
    status, _, _ = held(checkout, runtime, agent="a0177")
    ended = time.monotonic()
    resume.join()
    assert status == 0 and ended >= resume.at
    assert resume.marked == [subagent_id(SESSION, "a0177")]


def test_a_call_still_held_at_the_limit_is_refused_to_be_retried(
    checkout: Path, runtime: str
) -> None:
    place(coordination_root(checkout), operator_pause(HoldScope.REPOSITORY))
    data = checkout / f".{runtime}/plugins/lup/hooks/runtime/policy_data.py"
    data.write_text(
        data.read_text(encoding="utf-8") + "\nHOLD_SECONDS = 2\n", encoding="utf-8"
    )
    status, said, elapsed = held(checkout, runtime)
    answer = json.loads(said)["hookSpecificOutput"]
    assert status == 0
    assert answer["permissionDecision"] == "deny"
    assert answer["permissionDecisionReason"] == (
        "paused by the operator; this call didn't run; retry it"
    )
    assert 1.0 <= elapsed < 15
