"""What a policy composed in this process believes about its own boundary.

`dev policy` and `hooks classify` compose a policy here rather than running a
generated dispatcher, and the container was the one session fact neither
carried. Every rule annotated `container` therefore reported the question a
bare host would ask, which is not what a contained session is told -- and the
guidance sends a reader to `dev policy` *before* they spend a turn, so the
whole gap fell on the one reader the command exists for.
"""

import json
from pathlib import Path

import pytest

from lup.harness.enforcement import measured_containment, semantic_policy_for
from lup.harness.models import HookSet
from lup.policy.models import ShellCommand

CONTAINED = {
    "profile": ["contained"],
    "contained": ["yes"],
    "unjudged_ambient": ["ask"],
    "delivered": ["inside_placement", "question_relay"],
    "blocked": ["host_executor"],
}


def written(root: Path, nonce: str, ledger: dict[str, list[str]]) -> None:
    """One launch's measurement, where that launch would have put it."""
    path = root / ".lup" / "preflight" / f"{nonce}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger), encoding="utf-8")


def test_both_terms_come_from_the_launch_that_measured_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch_record_held: None
) -> None:
    """A session holding no nonce reads nothing, which is the fail-closed answer."""
    written(tmp_path, "this-launch", CONTAINED)

    monkeypatch.delenv("LUP_BOUNDARY_NONCE", raising=False)
    unmeasured = measured_containment(tmp_path)
    assert not unmeasured.contained
    assert not unmeasured.inside_placement

    monkeypatch.setenv("LUP_BOUNDARY_NONCE", "this-launch")
    held = measured_containment(tmp_path)
    assert held.contained
    assert held.inside_placement


def test_a_composed_policy_settles_what_its_boundary_holds(tmp_path: Path) -> None:
    """The verdict a live session gets, which this composition could not reach.

    `bounded()` reads both terms, so a caller passing containment without the
    placement it was measured with changes no answer at all -- which is why
    they are threaded together and asserted together.
    """
    hooks = HookSet(id="test", policy_ids=["shell"])
    unjudged = ShellCommand(command="frobnicate", cwd=tmp_path)

    assert semantic_policy_for(hooks).decide(unjudged).effect == "ask"
    unplaced = semantic_policy_for(hooks, contained=True)
    assert unplaced.decide(unjudged).effect == "ask"
    held = semantic_policy_for(hooks, contained=True, inside_placement=True)
    assert held.decide(unjudged).effect == "allow"
