"""Recognising a runtime's own refusal without claiming to predict it.

The distinction under test is not decorative. A gate belonging to the runtime
changes on somebody else's release schedule, so an answer that folded it into
the effect would refuse work in lup's name for a rail lup does not own — and
would start failing a policy sweep on the day upstream changed a token set.
Every case here pins one half of that: what is recognised, and that
recognising it moves nothing.
"""

from lup.policy.foreign import (
    REACHABLE_GATES,
    WORKTREE_ISOLATION,
    ForeignGate,
    foreign_warnings,
)


def test_a_token_in_argument_position_is_what_makes_this_worth_saying() -> None:
    """The verdict already shows a token used as the program; this one it does not.

    `grep -c eval <file>` is read-only, touches no git, and is refused — which
    is the measurement the whole report rests on.
    """
    said = foreign_warnings("grep -c eval packages/lup/src/lup/policy/vocabulary.py")

    assert len(said) == 1
    assert "`eval` in any argv position" in said[0]
    assert "worktree isolation" in said[0]


def test_a_leading_only_token_is_not_described_as_reaching_everywhere() -> None:
    """Position is what a reader reshapes against, so it has to be said exactly.

    `.` is index-gated by the gate's own author. Describing it as refused
    anywhere would send somebody hunting an occurrence that was never a
    problem.
    """
    said = foreign_warnings(". ./script.sh")

    assert "`.` as its first word" in said[0]
    assert "any argv position" not in said[0]


def test_the_same_token_in_argument_position_is_not_caught_when_gated() -> None:
    """The gating is the point of the report, so it has to be modelled."""
    assert foreign_warnings("grep -c . file.txt") == []


def test_a_bare_token_alone_passes_the_gate_it_names() -> None:
    """The refusal needs one element beside the token, so a bare one is fine.

    Rounding this off would warn about `eval` typed on its own, which is what
    somebody types when they are asking what the word does.
    """
    assert foreign_warnings("eval") == []


def test_a_quoted_token_never_becomes_its_own_argv_element() -> None:
    """A quoted token stays inside its word, measured rather than noted.

    `git log -S"ssh alias"` does not reproduce: the gate matches whole
    elements, and quoting keeps this one inside a longer word. Filing it as
    the example is how a report gets closed as not-reproducible.
    """
    assert foreign_warnings('git log -S"ssh alias"') == []


def test_an_ordinary_command_is_said_nothing_about() -> None:
    """A warning on every command is one nobody reads by the third."""
    assert foreign_warnings("ls -la") == []


def test_a_command_the_lexer_cannot_read_gets_no_second_sentence() -> None:
    """The policy has already refused it in its own words.

    A warning about somebody else's gate stacked on top of that is noise on
    top of a verdict, and about a parse nothing established.
    """
    assert foreign_warnings("ls 'unterminated") == []


def test_only_the_runtime_that_has_such_a_rail_contributes_one() -> None:
    """Codex brings nothing here, which is an answer rather than an omission.

    It never enters a worktree through a tool of its own, so there is no
    session-long armed state to over-match. Pinned as the roster holding
    exactly the gate that does exist, so a second entry appearing has to say
    whose it is.
    """
    assert REACHABLE_GATES == [WORKTREE_ISOLATION]


def test_a_project_meeting_a_gate_lup_never_heard_of_declares_its_own() -> None:
    """The roster is a default, so a runtime nobody here uses is expressible."""
    said = foreign_warnings(
        "somerun --flag frobnicate",
        [
            ForeignGate(
                runtime="Some Runtime",
                gate="its own rail",
                refuses=["frobnicate"],
                consequence="It stops.",
            )
        ],
    )

    assert "Some Runtime's its own rail" in said[0]
    assert "`frobnicate` in any argv position" in said[0]


def test_the_declared_gate_names_the_tokens_the_report_is_about() -> None:
    """The declaration is the artifact §14's report is written from."""
    assert set(WORKTREE_ISOLATION.refuses) == {"eval", "alias", "source"}
    assert WORKTREE_ISOLATION.leading_only == ["."]
