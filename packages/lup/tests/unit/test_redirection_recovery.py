"""A redirection's question names its loss, and a capture answers only its own.

A redirection is an unrecovered local mutation, and its verdict says so: the
purpose is the loss, and the checkpoint is read off where the target lands, so
the snapshot every session takes retires exactly the questions about files it
holds. That reading is also what keeps a capture from answering one it does
not hold. A target only the run resolves -- `cat > $B` -- names no path, and
nothing says it lands in the checkout the snapshot covers: it asks as the
local mutation it is, and no capture settles it. The rows that refuse a
generated tree, a protected path or a credential sit *above* this one and keep
their questions however complete the capture is.
"""

from lup.policy.kernel.decision import recovery_dischargeable
from lup.policy.kernel.rows import PathRuleRow, ShellRuleRow
from lup.policy.kernel.settlement import SettlementFacts, settle
from lup.policy.kernel.shell import decide_shell
from lup.policy.shell_rules import erase_shell_rules
from lup.policy.vocabulary import default_vocabulary

REDIRECTION = "cat > $B"
"""A write whose target only the run resolves."""


def rows() -> list[ShellRuleRow]:
    """The library's offered table, which is what the contract describes."""
    return erase_shell_rules(default_vocabulary())


def test_a_redirection_names_its_loss_and_where_it_lands() -> None:
    """The question states its own subject, and the subject is out of reach.

    A question carrying a `targeted` checkpoint would let a snapshot of the
    checkout settle a write to wherever `$B` points.
    """
    decision = decide_shell(REDIRECTION, rows())

    assert decision.effect == "ask"
    assert decision.purpose == "unrecovered_local_mutation"
    assert decision.checkpoint == "unrecoverable"
    assert not recovery_dischargeable(decision)


def test_a_proven_capture_does_not_settle_a_target_it_does_not_hold() -> None:
    """`absent` and `complete` are the two a session actually reaches here.

    Both ask: a capture that completed holds this checkout, and nothing says
    the write lands in it.
    """
    decision = decide_shell(REDIRECTION, rows())

    for checkpoint in ("absent", "complete"):
        settled = settle(SettlementFacts(decision, checkpoint=checkpoint))
        assert settled.effect == "ask", checkpoint


def test_a_redirection_into_a_protected_path_keeps_its_question() -> None:
    """The rows above the fallback are untouched, which is why this is safe.

    A protected path is not a local loss a capture discharges, and it is
    judged before the fallback ever runs -- so the relaxation cannot reach it
    however complete the capture is.
    """
    owned = [
        PathRuleRow(
            kind="exact",
            value="docs/owned.md",
            reason="human-owned",
            recovery="",
            allow_autonomous=False,
        )
    ]
    into_owned = decide_shell("cat > docs/owned.md", rows(), path_rules=owned)

    assert into_owned.effect == "ask"
    assert not recovery_dischargeable(into_owned)
