"""The one gate on assembling a review branch.

Integration is where every per-concern approval is cashed: twenty branches
merged onto one, and the least reversible thing a run does. Following the
last worker automatically, in the same invocation, leaves killing the process
in the seconds between as the only way to stop it. These pin what the gate
says and what each answer does.
"""

from lup.resolver.contracts import ResolverAssemblyDeferred
from lup.resolver.core import (
    ASSEMBLY_QUESTION_ID,
    assembly_question,
)
from lup.resolver.models import ConcernApproval, ConcernOutcome

BASE = "f6deb190abcd1234"


def verified(identifier: str) -> ConcernOutcome:
    return ConcernOutcome(
        concern_id=identifier,
        branch=f"resolve/run/{identifier}",
        commit=f"{identifier}-commit",
        verified=True,
    )


def failed(identifier: str, failure: str) -> ConcernOutcome:
    return ConcernOutcome(
        concern_id=identifier, branch=f"resolve/run/{identifier}", failure=failure
    )


def test_the_gate_names_what_it_is_about_to_merge() -> None:
    question = assembly_question([verified("alpha"), verified("beta")], [], BASE)

    assert question.id == ASSEMBLY_QUESTION_ID
    assert "merge alpha" in question.prompt
    assert "merge beta" in question.prompt
    assert "2 verified concern(s)" in question.prompt


def test_the_gate_names_the_base_the_branch_will_be_built_on() -> None:
    """Whether the base is still the right one is knowable here and not before."""
    question = assembly_question([verified("alpha")], [], BASE)

    assert BASE[:12] in question.prompt


def test_the_gate_says_when_the_base_has_been_superseded() -> None:
    """A run parks for hours and its branch moves underneath it.

    Assembling onto a base that has been superseded is exactly the moment a
    human wants to know, so the gate says so.
    """
    question = assembly_question([verified("alpha")], [], BASE, behind=7, branch="dev")

    assert "7 commit(s) behind dev" in question.prompt
    assert "resolve refresh --apply" in question.prompt


def test_a_current_base_is_not_remarked_on() -> None:
    """The ordinary case stays quiet, or the warning stops being one."""
    question = assembly_question([verified("alpha")], [], BASE, behind=0, branch="dev")

    assert "behind" not in question.prompt


def test_the_gate_names_what_is_excluded_and_why() -> None:
    """A concern that failed is dropped from the branch, which is a decision too."""
    question = assembly_question(
        [verified("alpha")],
        [failed("gamma", "revision limit exhausted")],
        BASE,
    )

    assert "exclude gamma: revision limit exhausted" in question.prompt


def test_an_excluded_concern_with_no_stated_failure_still_says_so() -> None:
    question = assembly_question([verified("alpha")], [failed("gamma", "")], BASE)

    assert "exclude gamma: not verified" in question.prompt


def test_the_answer_domain_is_closed_over_two_words() -> None:
    """A gate whose reader tests for a token cannot accept prose."""
    question = assembly_question([verified("alpha")], [], BASE)

    assert question.closed_choices
    assert question.choices == ConcernApproval.choices()
    assert question.recommendation == ConcernApproval.APPROVE


def test_deferring_reports_both_sides_of_what_was_not_assembled() -> None:
    deferred = ResolverAssemblyDeferred(["alpha", "beta"], ["gamma"])

    assert deferred.verified == ["alpha", "beta"]
    assert deferred.excluded == ["gamma"]
    assert "2 concern(s) ready to merge" in str(deferred)
