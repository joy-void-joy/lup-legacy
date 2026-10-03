"""A script `uv run` is handed beside an unread word is judged as the script it is.

`cd tmp && T=/a; uv run python s.py $T` was refused as a bare interpreter,
while the same line with `T` never assigned was allowed. The `;` releases `T`
to a value nobody can read -- `cd` may have failed and skipped the
assignment -- and a command referencing such a name is put to the refusal no
unread word could lift. That refusal handed the words `uv run` runs to the
reading of an interpreter run directly, which refuses Python over any file,
because Python is meant to run through `uv run`. Through `uv run`, only the
code the interpreter is handed can stand: inline code, or a program nobody
can read. A script file with an unread argument after it gets the floor
any unread argument gets, which is the floor `uv run bash` already had.

Driven the way a session drives it: each runtime's generated dispatcher on
the payload its harness sends, under no boundary, the runtime's own sandbox
and a measured container, and beside it `dev policy`'s reading of the same
placements, which has to agree.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.unit.repos import initialized_repo
from tests.unit.test_outer_posture_policy import Posture, Runtime, met, previewed

POSTURES: tuple[Posture, ...] = ("none", "inner", "outer")

HANDED = [
    pytest.param(
        "cd tmp && T=/a; uv run python s.py $T",
        "cd tmp && T=/a; uv run bash s.sh $T",
        id="released-chain",
    ),
    pytest.param(
        "read T <<< x; uv run python tmp/s.py $T",
        "read T <<< x; uv run bash tmp/s.sh $T",
        id="read",
    ),
    pytest.param(
        "T=$(date); uv run python tmp/s.py $T",
        "T=$(date); uv run bash tmp/s.sh $T",
        id="substituted-binding",
    ),
    pytest.param(
        "uv run python tmp/s.py $(date)",
        "uv run bash tmp/s.sh $(date)",
        id="substitution",
    ),
    pytest.param(
        "read T <<< x; uv run perl tmp/s.pl $T",
        "read T <<< x; uv run bash tmp/s.sh $T",
        id="perl",
    ),
    pytest.param(
        "read T <<< x; uv run uv run python tmp/s.py $T",
        "read T <<< x; uv run uv run bash tmp/s.sh $T",
        id="nested-uv",
    ),
]
"""A script handed through `uv run` beside an unread word, and the same through bash."""

STANDING = [
    pytest.param("read T <<< x; uv run python -c 'print(1)' $T", id="inline"),
    pytest.param("read T <<< x; uv run python $T", id="unread-program"),
    pytest.param("read T <<< x; uv run uv run python -c x $T", id="nested-inline"),
    pytest.param("read T <<< x; python tmp/s.py $T", id="direct-python"),
]
"""What stays refused whatever the unread word turns out to be."""


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> Runtime:
    return request.param


@pytest.fixture
def checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch_record_held: None
) -> Iterator[Path]:
    """A checkout in a container, so the `outer` posture answers as a contained launch."""
    work = tmp_path / "checkout"
    initialized_repo(work, tmp_path / "no-hooks")
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
    monkeypatch.delenv("LUP_BOUNDARY_ROOT", raising=False)
    monkeypatch.delenv("LUP_BOUNDARY_NONCE", raising=False)
    monkeypatch.delenv("LUP_SANDBOX_ACTIVE", raising=False)
    yield work


def answers(
    runtime: Runtime, command: str, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, str], dict[str, str]]:
    """What each posture's session meets, and what `dev policy` says it meets.

    The preview is read standing in the checkout, as a session would, and
    the test is put back where it stood: each dispatcher is found from there.
    """
    here = Path.cwd()
    session = {
        posture: met(runtime, posture, command, checkout) for posture in POSTURES
    }
    preview = previewed(command, checkout, monkeypatch)
    monkeypatch.chdir(here)
    monkeypatch.delenv("LUP_BOUNDARY_NONCE", raising=False)
    return session, preview


@pytest.mark.parametrize(("command", "through_bash"), HANDED)
def test_a_script_handed_through_uv_run_gets_the_floor_any_unread_argument_gets(
    runtime: Runtime,
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    through_bash: str,
) -> None:
    session, preview = answers(runtime, command, checkout, monkeypatch)

    assert (session, preview) == answers(runtime, through_bash, checkout, monkeypatch)
    assert session["inner"] == session["outer"] == "allow"
    assert preview == session


@pytest.mark.parametrize("command", STANDING)
def test_code_nobody_can_read_stays_refused_through_uv_run(
    runtime: Runtime, checkout: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    session, preview = answers(runtime, command, checkout, monkeypatch)

    assert session == dict.fromkeys(POSTURES, "deny")
    assert preview == session
