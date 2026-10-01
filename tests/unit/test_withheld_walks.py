"""A recursive read whose root holds a key or a login is judged as reading it.

A withheld path is refused to every word that names it, and `grep -r password
~` names none: it spells a home, and reads every key inside. The host walks
what a recursive reader would, and a root it finds holding a withheld path is
refused as naming that path would be. A listing, a single file, and a walk
that skips dot names stay reads. Driven through each runtime's generated
dispatcher on every posture, with `dev policy`'s reading beside it.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Literal

import pytest
import sh

from lup.devtools.dev import policy_explain
from lup.devtools.dev.policy_explain import PLACEMENTS, verdict_for
from lup.harness.enforcement import semantic_policy_for
from lup.policy.models import ShellCommand
from lup.types import JsonObject
from lup_template.harness.catalog import declared_hook_set
from tests.unit.repos import initialized_repo

type Posture = Literal["none", "inner", "outer"]

WALKS_INTO = [
    pytest.param("grep -r password ~", id="grep-home"),
    pytest.param("tar czf tmp/out.tgz ~", id="tar-home"),
    pytest.param("grep -r password $HOME", id="grep-home-variable"),
    pytest.param("grep -r password {home}", id="grep-home-absolute"),
    pytest.param("cd {home}/.. && grep -r password home", id="grep-home-from-above"),
    pytest.param("cp -r ~ tmp/home", id="copy-home"),
    pytest.param("zip -r tmp/out.zip ~", id="zip-home"),
    pytest.param("grep -r token .", id="grep-checkout-login"),
    # What a listing yields, read by whatever it is handed to, is the walk.
    pytest.param("find ~ | xargs cat", id="find-into-xargs"),
    pytest.param("find ~ -print0 | xargs -0 cat", id="find-into-xargs-null"),
    pytest.param("find ~ -type f -exec cat {{}} +", id="find-exec"),
    pytest.param("find ~ -type f -exec cat {{}} \\;", id="find-exec-each"),
    pytest.param('find ~ | while read f; do cat "$f"; done', id="find-read-loop"),
    pytest.param("ls -R ~ | xargs cat", id="ls-into-xargs"),
]
"""Walks the host finds holding a key or a login beneath the root they spell."""

STAYS_OUT = [
    pytest.param("ls ~", id="list-home"),
    pytest.param("grep password ~/notes.txt", id="one-file"),
    pytest.param("rg password ~", id="rg-skips-dot-names"),
    pytest.param("grep -rn token src", id="grep-source"),
    pytest.param("rg token .", id="rg-checkout"),
    pytest.param("grep -r --exclude-dir=.lup token .", id="grep-excluding-attached"),
    pytest.param("grep -r --exclude-dir .lup token .", id="grep-excluding-apart"),
    pytest.param("find ~ -name notes.txt", id="find-lists-names"),
    pytest.param("find src | xargs cat", id="find-source-into-xargs"),
    pytest.param("find src -exec cat {{}} +", id="find-exec-source"),
    pytest.param("find ~ -name *.txt | xargs cat", id="find-yielding-text-into-xargs"),
    pytest.param(
        "find . -name *.py -exec grep -l token {{}} +", id="find-exec-yielding"
    ),
]
"""Reads that name no withheld path and walk into none."""


def test_a_refused_checkout_walk_names_the_spellings_that_read_it(
    checkout: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session searching its own checkout is told the two searches that work.

    Every checkout keeping a runtime's login under `.lup/` refuses `grep -r .`,
    so the refusal says what reads the same tree without it: `rg`, which
    skips hidden and ignored paths, and grep leaving that directory out.
    """
    monkeypatch.setenv("HOME", str(home))
    policy = semantic_policy_for(declared_hook_set())

    decision = policy.decide(ShellCommand(command="grep -r token .", cwd=checkout))

    assert decision.effect == "deny"
    assert "`rg token .`" in decision.addressed()
    assert "`grep -r --exclude-dir=.lup token .`" in decision.addressed()


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home holding a key beside an ordinary file."""
    root = tmp_path / "home"
    (root / ".ssh").mkdir(parents=True)
    (root / ".ssh" / "id_ed25519").write_text("key\n", encoding="utf-8")
    (root / "notes.txt").write_text("password: none\n", encoding="utf-8")
    return root


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout keeping a runtime's login under its gitignored state."""
    work = tmp_path / "checkout"
    initialized_repo(work, tmp_path / "no-hooks")
    (work / "src").mkdir()
    (work / "src" / "app.py").write_text("token = None\n", encoding="utf-8")
    login = work / ".lup" / "codex-home" / "auth.json"
    login.parent.mkdir(parents=True)
    login.write_text('{"token": "secret"}\n', encoding="utf-8")
    ledger = work / ".lup" / "preflight" / "launch.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "contained": ["yes"],
                "delivered": ["inside_placement", "question_relay"],
                "blocked": ["host_executor"],
                "writable_roots": [str(work)],
            }
        ),
        encoding="utf-8",
    )
    return work


def met(
    runtime: str, posture: Posture, command: str, checkout: Path, home: Path
) -> str:
    """The effect a session of one posture meets before the call runs."""
    payload: JsonObject = {
        "session_id": "walk-probe",
        "hook_event_name": "PreToolUse",
        "cwd": str(checkout),
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    held = {
        name: value
        for name, value in os.environ.items()
        if name not in ("LUP_BOUNDARY_ROOT", "LUP_BOUNDARY_NONCE", "LUP_SANDBOX_ACTIVE")
    }
    placed = {
        "none": {},
        "inner": {"LUP_SANDBOX_ACTIVE": "1"},
        "outer": {"LUP_BOUNDARY_NONCE": "launch"},
    }[posture]
    result = sh.Command(sys.executable)(
        "-I",
        "-S",
        str(Path(f".{runtime}/plugins/lup/hooks/scripts/policy.py").resolve()),
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={
            **held,
            **placed,
            "HOME": str(home),
            "PLUGIN_DATA": str(checkout.parent / "plugin-data"),
        },
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


def previewed(
    command: str, checkout: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> set[str]:
    """What `dev policy` answers on every placement, from the same home."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(checkout)
    verdict = verdict_for(command, "shell", False, checkout, declared_hook_set())
    return {reading.effect for reading in verdict.readings}


@pytest.mark.parametrize("spelled", WALKS_INTO)
def test_a_walk_into_a_key_or_a_login_is_refused_on_every_posture(
    runtime: str,
    checkout: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    spelled: str,
) -> None:
    command = spelled.format(home=home)
    postures: tuple[Posture, ...] = ("none", "inner", "outer")

    assert {met(runtime, posture, command, checkout, home) for posture in postures} == {
        "deny"
    }
    assert previewed(command, checkout, home, monkeypatch) == {"deny"}


@pytest.mark.parametrize("spelled", STAYS_OUT)
def test_a_read_that_walks_into_none_stays_a_read(
    runtime: str,
    checkout: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    spelled: str,
) -> None:
    command = spelled.format(home=home)
    postures: tuple[Posture, ...] = ("none", "inner", "outer")

    assert {met(runtime, posture, command, checkout, home) for posture in postures} == {
        "allow"
    }
    assert previewed(command, checkout, home, monkeypatch) == {"allow"}


class Stalled:
    """A policy whose verdict never comes, as a walk of a whole disk would not."""

    def decide(self, event: ShellCommand) -> None:
        time.sleep(30)


def stalled(*_args: object, **_kwargs: object) -> Stalled:
    return Stalled()


def test_a_reading_in_process_is_bounded_as_a_hook_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`dev policy` meets the deadline each dispatcher opens, from one declaration.

    Unbounded, a reading waiting on something that never returns -- a walk
    of a whole disk -- hangs the command a session reaches for before it
    spends a turn, where the hook it previews has already refused.
    """
    monkeypatch.setattr(policy_explain, "semantic_policy_for", stalled)
    hooks = declared_hook_set().model_copy(update={"policy_timeout": 1})
    started = time.monotonic()

    verdict = verdict_for("ls", "shell", False, tmp_path, hooks, PLACEMENTS[:1])

    assert time.monotonic() - started < 10
    (reading,) = verdict.readings
    assert reading.effect == "deny"
    assert "deadline" in reading.reason
