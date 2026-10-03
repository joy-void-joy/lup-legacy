"""A word nobody can read is judged as the strictest command it could stand for.

A verb or a flag built by an expansion -- `$OP`, a `$(...)` result, `set$X`,
`--ret$X` -- names no row as it is spelled, and so would reach whatever the
words before it fall back to: a registry widening and an operator-only verb
allowed, and a guarded flag slipping past its guard. So each is judged as
every command it could stand for, and keeps the strictest of those verdicts,
with a reason saying which word could not be read and what it could be.

Driven the way a session drives it: each runtime's generated dispatcher run
on the payload its harness sends, and beside it `dev policy`'s own reading,
which answers for both and so has to say what each says.
"""

from pathlib import Path

import pytest

from tests.unit.repos import initialized_repo
from tests.unit.test_registry_command_policy import Runtime, met, previewed

UNREAD = [
    pytest.param(
        "sync $OP lup /srv/lup --mount rw",
        "ask",
        "`$OP` could not be read and could be `lup-devtools sync setup`",
        id="widening-verb",
    ),
    pytest.param(
        "sync set$X lup /srv/lup",
        "ask",
        "`set$X` could not be read and could be `lup-devtools sync setup`",
        id="verb-begun",
    ),
    pytest.param(
        "review $(echo approve) abc --as operator",
        "deny",
        "could not be read and could be `lup-devtools review approve`",
        id="operator-only-verb",
    ),
    pytest.param(
        "dev comments --ret$X a.py:1",
        "ask",
        "`--ret$X` could not be read and could be `--retire`",
        id="guarded-flag",
    ),
]
"""Each unread word, the verdict the strictest command it could be earns, and the
line naming both."""

AS_BEFORE = [
    # `st$X` can only be `status`, which reads.
    pytest.param("sync st$X", id="legible-part-rules-out"),
    # The `git` sub-app guards no verb, so an unread one earns what it did.
    pytest.param("git $X", id="nothing-guarded"),
    pytest.param("sync status", id="plain-read"),
    pytest.param("dev comments --restore a.py:1", id="plain-flag"),
]
"""Where no command the word could stand for is stricter, or nothing is unread."""


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> Runtime:
    return request.param


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Where every call here is made from."""
    initialized_repo(tmp_path / "checkout", tmp_path / "no-hooks")
    return tmp_path / "checkout"


@pytest.mark.parametrize(("command", "strictest", "named"), UNREAD)
def test_an_unread_word_takes_the_strictest_command_it_could_be(
    runtime: Runtime, checkout: Path, command: str, strictest: str, named: str
) -> None:
    effect, reason = met(runtime, command, checkout)
    preview = previewed(command, checkout)

    assert effect == strictest
    assert named in reason
    assert {placed.effect for placed in preview.readings} == {effect}
    assert all(named in placed.reason for placed in preview.readings)
    assert all("\n" not in placed.reason for placed in preview.readings)


@pytest.mark.parametrize("command", AS_BEFORE)
def test_a_word_that_could_be_nothing_stricter_is_answered_as_before(
    runtime: Runtime, checkout: Path, command: str
) -> None:
    effect, _reason = met(runtime, command, checkout)

    assert effect == "allow"
    assert {placed.effect for placed in previewed(command, checkout).readings} == {
        effect
    }
