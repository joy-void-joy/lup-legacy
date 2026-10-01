"""A command that widens what a later launch reaches asks, as an edit of the registry does.

`sync.json` and `sync.json.local` are protected edit roots: a registration
there decides what a session may mount, at which mode, and cloned from which
repository. A command writing those keys without a question would let
`sync setup … --mount rw` widen the boundary unasked while an edit of the
same line asks. So each writer that can add or move a mount, change the
repository a registration names, or grant a device asks, and so does a
launcher's flag lending the one session it opens a folder or a device. What
only reads, keeps books, narrows, or dry-runs stays allowed.

Every surface is driven the way a session drives it: each runtime's generated
dispatcher run on the payload its harness sends, and beside it `dev policy`'s
own reading, which answers for both and so has to say what each says.
"""

import json
import os
import sys
from pathlib import Path
from typing import Literal

import pytest
import sh

from lup.devtools.dev.policy_explain import PolicyVerdict, verdict_for
from lup.types import JsonObject
from lup_template.harness.catalog import declared_hook_set
from tests.unit.native import claude_effect, codex_denial
from tests.unit.repos import initialized_repo

type Runtime = Literal["claude", "codex"]

DISPATCHERS: dict[Runtime, Path] = {
    "claude": Path(".claude/plugins/lup/hooks/scripts/policy.py"),
    "codex": Path(".codex/plugins/lup/hooks/scripts/policy.py"),
}

WIDENING = [
    pytest.param(
        "sync setup lup /srv/lup --mount rw", "its mount opens", id="mount-rw"
    ),
    pytest.param(
        "sync setup notes /srv/notes --mount=ro", "its mount opens", id="mount-ro"
    ),
    # No flag, and still a widening: the lup entry every scaffold ships already
    # carries a read-write mount, so the path is what that mount opens.
    pytest.param("sync setup lup /srv/lup", "its mount opens", id="move-a-mount"),
    pytest.param(
        "sync remote lup git@github.com:owner/lup.git", "clones and mounts", id="remote"
    ),
    pytest.param("sync grant nvidia.com/gpu=all", "host device", id="grant"),
    pytest.param("dev init upstream", "committed lup registration", id="init-upstream"),
    pytest.param(
        "dev library git --url https://github.com/owner/fork --branch main",
        "follows this pin",
        id="repin",
    ),
    pytest.param(
        "dev library git --url=https://github.com/owner/fork",
        "follows this pin",
        id="repin-joined",
    ),
]
"""Every writer that widens what a launch reaches, and what its question names."""

LENDING = [
    pytest.param("harness claude --mount /srv/data", "folder or device", id="lend-rw"),
    pytest.param(
        "harness codex --mount-ro /srv/data", "folder or device", id="lend-ro"
    ),
    pytest.param(
        "harness claude --device nvidia.com/gpu=all", "folder or device", id="lend-gpu"
    ),
]
"""A launcher's flag lending the one session it opens a folder or a device.

Asked on the host, where the engine the launch reaches can bind any folder the
operator owns. From inside a container measured around the session there is
no engine socket to lend a host folder through, so `outer` settles it."""

BOOKKEEPING = [
    pytest.param("sync status", id="status"),
    pytest.param("sync fetch", id="fetch-all"),
    pytest.param("sync fetch lup", id="fetch"),
    pytest.param("sync log lup --no-stat", id="log"),
    pytest.param("sync diff lup 4c6293a6", id="diff"),
    pytest.param("sync mark-synced lup --at 4c6293a6", id="mark-synced"),
    pytest.param("sync upstream", id="upstream-reports"),
    pytest.param("sync revoke nvidia.com/gpu=all", id="revoke"),
    pytest.param("dev init upstream --dry-run", id="init-upstream-dry-run"),
    pytest.param("dev init upstream -n", id="init-upstream-n"),
    pytest.param("dev library git --branch main", id="move-the-ref"),
    pytest.param(
        "dev library git --url https://github.com/owner/fork --dry-run",
        id="repin-dry-run",
    ),
    pytest.param("dev library use published", id="leave-the-pin"),
    pytest.param("harness claude --continue", id="launch"),
    pytest.param("harness codex --generate-only --mount /srv/data", id="generate-only"),
]
"""What reads, keeps books, narrows, or writes nothing, and so is not asked about.

`sync revoke` only takes a grant back. `dev library git` without `--url` keeps
the repository the pin names and moves the ref, and `dev library use` returns
the registration to the URL the registry files already declare."""


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> Runtime:
    return request.param


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Where every call here is made from."""
    initialized_repo(tmp_path / "checkout", tmp_path / "no-hooks")
    return tmp_path / "checkout"


def met(runtime: Runtime, command: str, checkout: Path) -> tuple[str, str]:
    """The effect a session meets before the call runs, and the reason it reads.

    Codex has no ask at this boundary: it parks the question as a review and
    answers with a structured refusal naming who can release it, which is the
    same question put where Codex can carry one. Only exit 2 is a refusal.
    """
    payload: JsonObject = {
        "session_id": "registry-probe",
        "hook_event_name": "PreToolUse",
        "cwd": str(checkout),
        "tool_name": "Bash",
        "tool_input": {"command": f"uv run lup-devtools {command}"},
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
        if result.exit_code == 2:
            return "deny", result.stderr.decode()
        if not result.stdout:
            return "allow", ""
        return "ask", codex_denial(result)
    answered = json.loads(str(result))
    specific = answered["hookSpecificOutput"]
    return claude_effect(answered), str(
        specific["permissionDecisionReason"]
        if "permissionDecisionReason" in specific
        else ""
    )


def previewed(command: str, checkout: Path) -> PolicyVerdict:
    """What `dev policy` answers for the call, under every placement it reads."""
    return verdict_for(
        f"uv run lup-devtools {command}", "shell", False, checkout, declared_hook_set()
    )


@pytest.mark.parametrize(("command", "widens"), WIDENING)
def test_a_command_that_widens_a_later_launch_asks(
    runtime: Runtime, checkout: Path, command: str, widens: str
) -> None:
    effect, reason = met(runtime, command, checkout)
    preview = previewed(command, checkout)

    assert effect == "ask"
    assert widens in reason
    assert {placed.effect for placed in preview.readings} == {effect}
    assert all(widens in placed.reason for placed in preview.readings)


@pytest.mark.parametrize(("command", "widens"), LENDING)
def test_a_launch_lending_a_host_folder_asks_from_a_host_posture(
    runtime: Runtime, checkout: Path, command: str, widens: str
) -> None:
    effect, reason = met(runtime, command, checkout)
    readings = {
        placed.placement: placed for placed in previewed(command, checkout).readings
    }

    assert effect == "ask"
    assert widens in reason
    assert {readings[name].effect for name in ("none", "inner")} == {effect}
    assert all(widens in readings[name].reason for name in ("none", "inner"))
    assert readings["outer"].effect == "allow"


@pytest.mark.parametrize("command", BOOKKEEPING)
def test_a_command_that_only_reads_or_keeps_books_is_allowed(
    runtime: Runtime, checkout: Path, command: str
) -> None:
    effect, _reason = met(runtime, command, checkout)

    assert effect == "allow"
    assert {placed.effect for placed in previewed(command, checkout).readings} == {
        effect
    }
