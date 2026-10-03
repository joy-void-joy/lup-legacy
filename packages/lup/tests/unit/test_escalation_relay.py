"""An escalation on a host with no human to ask reaches one anyway.

`# lup: escalate[decision]: <why>` promotes a refusal into an approval
question so a human sees the intent at the moment of judgment. A resolver
worker has no human attached, so for a worker the question cannot be asked
where it stands — and the documented escape hatch must not become a
guaranteed refusal in exactly the context that most needs one. These pin
that the refusal stands, that the agent's stated reason survives it, and
that it arrives somewhere a person reads.
"""

from lup.policy.enforcement import RELAYED_NOTICE, policy_hook_output
from lup.policy.kernel.effects import declare
from lup.policy.kernel.shell import decide_shell
from lup.policy.models import Decision
from lup.policy.rules import pydantic_decision
from lup.policy.shell_rules import ShellCommandRule, erase_shell_rules

REFUSING = [
    ShellCommandRule(
        name="rm",
        effects=[declare("destroys_uncaptured", scope="targeted")],
        reason="removing a file is not reversible",
    )
]


def classified(command: str, interactive: bool) -> Decision:
    """One command through the shell kernel, as a host of that kind sees it."""
    return pydantic_decision(
        decide_shell(command, erase_shell_rules(REFUSING), interactive=interactive)
    )


def test_a_marker_still_asks_where_a_human_can_answer() -> None:
    """The interactive path is unchanged: the marker reaches the human."""
    decision = classified(
        "# lup: escalate[decision]: it is my own scratch\nrm junk.txt", True
    )

    assert decision.effect == "ask"
    assert decision.escalated == "it is my own scratch"


def test_a_marker_denied_without_a_human_keeps_the_reason_it_stated() -> None:
    """The stated intent outlives the refusal, which is what a relay carries.

    Denied with the reason dropped, the escalation would summon nobody and
    the agent would be the only party that knew it was stuck.
    """
    decision = classified(
        "# lup: escalate[decision]: it is my own scratch\nrm junk.txt", False
    )

    assert decision.effect == "deny"
    assert decision.escalated == "it is my own scratch"


def test_an_ordinary_refusal_carries_no_escalation() -> None:
    """Only what an agent deliberately escalated is worth anybody's attention."""
    decision = classified("rm junk.txt", False)

    assert decision.effect == "deny"
    assert decision.escalated == ""


def test_a_denied_escalation_is_relayed_and_says_so() -> None:
    """The refusal stands and the request arrives somewhere a person reads."""
    relayed: list[tuple[str, str]] = []

    output = policy_hook_output(
        classified(
            "# lup: escalate[decision]: it is my own scratch\nrm junk.txt", False
        ),
        relay=lambda why, refusal: relayed.append((why, refusal)),
    )

    assert output.decision == "deny"
    assert [why for why, _ in relayed] == ["it is my own scratch"]
    assert output.reason is not None and RELAYED_NOTICE in output.reason


def test_a_relay_never_turns_a_refusal_into_a_run() -> None:
    """A host with nobody to ask cannot approve, and must not appear to.

    The marker exists to summon a human. Resolving it to a run where there is
    no human would make the instrument for summoning one the instrument for
    bypassing the table.
    """
    output = policy_hook_output(
        classified("# lup: escalate[decision]: I want this\nrm -rf /", False),
        relay=lambda _why, _refusal: None,
    )

    assert output.decision == "deny"


def test_an_ordinary_refusal_relays_nothing() -> None:
    """A relay that fired on every denial would be noise nobody reads."""
    relayed: list[tuple[str, str]] = []

    policy_hook_output(
        classified("rm junk.txt", False),
        relay=lambda why, refusal: relayed.append((why, refusal)),
    )

    assert relayed == []
