"""One answer domain per gate, published and read from the same place.

The defect this shape prevents: a gate offering its answers as suggestions
while its only reader tests for a literal lets a human's prose grant promote
cleanly and then mean refusal, with nothing anomalous to report anywhere.
What prevents it is not a rule about keeping two lists in step — it is that
there is one list.
"""

from lup.resolver.core import approval_question, assembly_question
from lup.resolver.models import (
    AcceptanceCriterion,
    AllowanceRuling,
    ClosedAnswer,
    Concern,
    ConcernApproval,
    ConcernOutcome,
    ResidualRuling,
    SupersessionRuling,
)

GATES: list[type[ClosedAnswer]] = [
    AllowanceRuling,
    ConcernApproval,
    ResidualRuling,
    SupersessionRuling,
]


def test_every_gate_offers_exactly_the_answers_it_defines() -> None:
    """The offer is derived, so a reader cannot test an unoffered token."""
    for gate in GATES:
        assert gate.choices() == [member.value for member in gate], gate
        assert len(gate.choices()) > 1, gate


def test_no_two_gates_are_the_same_gate_spelled_twice() -> None:
    """Four domains, four meanings — a shared word would be a shared reader."""
    assert len({tuple(gate.choices()) for gate in GATES}) == len(GATES)


def test_a_planned_concern_gate_publishes_its_own_domain() -> None:
    concern = Concern(
        id="c",
        title="a concern",
        spec="what to do",
        criteria=[AcceptanceCriterion(id="a", description="it is done")],
    )

    question = approval_question(concern)

    assert question.choices == ConcernApproval.choices()
    assert question.recommendation == ConcernApproval.APPROVE
    assert question.closed_choices is True


def test_the_assembly_gate_publishes_the_same_domain_as_the_concern_gate() -> None:
    """One decision asked twice at different scopes is one domain.

    Both ask whether work joins this run — of one concern, and of the set —
    so a word that means "yes" at one scope has to mean it at the other.
    """
    question = assembly_question(
        [ConcernOutcome(concern_id="c", branch="resolve/r/c", verified=True)],
        [],
        "0" * 40,
        False,
        "review",
    )

    assert question.choices == ConcernApproval.choices()
