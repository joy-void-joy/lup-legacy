"""Which ways through a verdict offers: its own, then the routes past it."""

from lup.policy.kernel.decision import (
    ESCALATE,
    ESCALATE_HINT,
    RELAY,
    RELAY_HINT,
    RESHAPE,
    RESHAPE_HINT,
    KernelDecision,
    offered,
)
from lup.policy.kernel.diagnostic import step

OWN = step("add the package through uv", ["uv", "add", "<package>"])


def test_a_verdict_with_no_way_of_its_own_offers_every_generic_one() -> None:
    assert offered(ESCALATE_HINT) == (RESHAPE, ESCALATE)
    assert offered(RELAY_HINT) == (RESHAPE, RELAY)
    assert offered(RESHAPE_HINT) == (RESHAPE,)
    assert offered(()) == ()


def test_a_way_of_its_own_leaves_out_change_the_command_but_not_the_route() -> None:
    assert offered((OWN, *ESCALATE_HINT)) == (OWN, ESCALATE)
    assert offered((OWN, *RELAY_HINT)) == (OWN, RELAY)
    assert offered((OWN, *RESHAPE_HINT)) == (OWN,)


def test_the_hook_and_the_agent_read_the_same_ways() -> None:
    verdict = KernelDecision(
        "deny", "changes packages outside this project's lockfile", subject="pip"
    ).advising((OWN, *ESCALATE_HINT))

    assert verdict.recovery == (OWN, *ESCALATE_HINT)
    assert verdict.diagnostic()["steps"] == [OWN, ESCALATE]
    assert "change the command" not in verdict.addressed()
    assert "change the command" not in verdict.beside()
