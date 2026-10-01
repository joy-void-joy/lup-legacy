"""What a marker asks for, and what a malformed one is answered with.

Escalation is a request and never self-authority, so the two things worth
pinning are that a request says which axis it means and that a request saying
nothing is refused before anything reads it.
"""

from lup.policy.kernel.escalation import (
    MISSING_KIND,
    MISSING_REASON,
    ESCALATION_KINDS,
    read_escalation,
)


def test_a_marker_names_which_axis_it_asks_to_move() -> None:
    """Two different requests, each spelled by the axis it moves.

    "Put this to a person, because the rule that refused it does not know
    what I know" and "run this on the host, because inside cannot answer it"
    promote a verdict along different axes, and an operation may need both.
    """
    decision = read_escalation("# lup: escalate[decision]: the rule is wrong\nls")
    sandbox = read_escalation("# lup: escalate[sandbox]: the host has it\nls")

    assert decision.request is not None
    assert sandbox.request is not None
    assert decision.request.kinds == ("decision",)
    assert sandbox.request.kinds == ("sandbox",)


def test_a_marker_may_name_both_axes_at_once() -> None:
    """The one route past an overrideable refusal to the launcher's host."""
    reading = read_escalation("# lup: escalate[decision,sandbox]: both\nls")

    assert reading.request is not None
    assert reading.request.kinds == ("decision", "sandbox")


def test_a_marker_naming_no_kind_is_refused_with_the_spellings() -> None:
    """The kind is the request, so a marker without one asks for nothing.

    Read as a decision, it would leave an agent stuck inside the boundary
    never learning the sandbox half exists; the refusal names both.
    """
    reading = read_escalation("# lup: escalate: I need it\nls")

    assert reading.request is None
    assert reading.refusal == MISSING_KIND
    assert "escalate[decision]" in reading.refusal
    assert "escalate[sandbox]" in reading.refusal
    assert reading.remainder == "ls"


def test_a_marker_with_no_reason_is_refused_before_anything_reads_it() -> None:
    """The whole content of the request is what it says to whoever answers.

    A request that says nothing asks a person to approve a rule id, which is
    the agent authorising itself with extra steps.
    """
    reading = read_escalation("# lup: escalate[decision]:\nls")

    assert reading.request is None
    assert reading.refusal == MISSING_REASON


def test_a_kind_this_vocabulary_does_not_carry_is_named_rather_than_ignored() -> None:
    """A typo read as some other kind would grant the wrong axis in silence.

    The agent would ask for the host, receive decision escalation, watch the
    operation run inside anyway, and spend a turn finding out why.
    """
    reading = read_escalation("# lup: escalate[host]: typo\nls")

    assert reading.request is None
    assert "'host'" in reading.refusal


def test_the_marker_is_a_request_about_the_call_rather_than_part_of_it() -> None:
    """Left in, it makes an unclassifiable comment out of every escalation."""
    reading = read_escalation("# lup: escalate[decision]: why\nrm -rf build")

    assert reading.remainder == "rm -rf build"


def test_an_unmarked_call_asks_for_nothing_and_is_left_whole() -> None:
    reading = read_escalation("ls -la")

    assert (reading.request, reading.refusal, reading.remainder) == (
        None,
        "",
        "ls -la",
    )


def test_the_canonical_spelling_round_trips_what_was_asked_for() -> None:
    """The audit says which axes a marker asked for, in one spelling.

    Reconstructing that from the effect it produced is exactly the inference
    a record exists to make unnecessary, and two markers naming the same pair
    in another order are the same request.
    """
    reading = read_escalation("# lup: escalate[sandbox, decision]: why\nls")

    assert reading.request is not None
    assert reading.request.normalized == "# lup: escalate[decision,sandbox]: why"
    assert reading.request.raw.startswith("# lup: escalate[sandbox, decision]: why")


def test_every_kind_the_vocabulary_carries_parses_under_its_own_name() -> None:
    """A kind added to the list and nowhere else is one no marker can name."""
    for kind in ESCALATION_KINDS:
        reading = read_escalation(f"# lup: escalate[{kind}]: why\nls")

        assert reading.request is not None
        assert reading.request.asks(kind)
