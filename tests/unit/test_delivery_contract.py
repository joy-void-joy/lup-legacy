"""Every semantic guarantee is answered by a mechanism, or stated as absent.

An incomplete map reads exactly like a complete one, which is the whole reason
this is a suite rather than a table: a guarantee added to the vocabulary and
not answered by an adapter fails here, at the moment somebody adds it, rather
than silently reading as delivered.
"""

import json
import os
from pathlib import Path
from typing import get_args

import pytest
import sh

from lup.policy.delivery import (
    DeliveryFact,
    Guarantee,
    delivery_gaps,
    unmeasured,
)
from lup.policy.identity import DASHBOARD_URL_ENV
from lup.policy.relay import QuestionRelay
from lup.providers.claude.delivery import CLAUDE_DELIVERY
from lup.providers.codex.delivery import CODEX_DELIVERY


def test_every_guarantee_is_answered_by_both_adapters() -> None:
    """A guarantee no adapter mentions is one nobody decided about.

    Stated as absent it is a design with a fallback; omitted it is a silence
    that reads as delivered.
    """
    named = set(get_args(Guarantee.__value__))

    for facts in (CLAUDE_DELIVERY, CODEX_DELIVERY):
        assert {fact.guarantee for fact in facts} == named


def test_a_guarantee_with_no_mechanism_still_states_what_happens() -> None:
    """Fail-closed and fail-open look identical until somebody writes it down.

    So the fallback is required prose rather than an optional note: a
    guarantee with no stated fallback is one whose absence nobody thought
    about.
    """
    for facts in (CLAUDE_DELIVERY, CODEX_DELIVERY):
        for fact in facts:
            assert fact.fallback, fact.guarantee


def test_adapters_record_explicit_rejections_without_inferring_native_answers() -> None:
    """Only an explicit queue answer supplies a rejection receipt."""
    for facts in (CLAUDE_DELIVERY, CODEX_DELIVERY):
        receipt = next(fact for fact in facts if fact.guarantee == "rejection_receipt")

        assert receipt.carried()
        assert "explicit rejection" in receipt.mechanism
        assert "execution supplies no answer" in receipt.fallback


def test_the_placement_gap_is_the_one_the_preflight_answers() -> None:
    """One runtime's verdicts place no call, which is a capability and not a bug.

    Stated here, a profile requiring containment on it fails its launch
    preflight rather than running unconfined under a placement nothing
    carried — which is the difference between a declared boundary and a
    hoped-for one.
    """
    gaps = {fact.guarantee for fact in delivery_gaps(CODEX_DELIVERY)}

    assert "inside_placement_enforced" in gaps
    assert "inside_placement_enforced" not in {
        fact.guarantee for fact in delivery_gaps(CLAUDE_DELIVERY)
    }


def test_what_rests_on_documentation_is_visible_rather_than_collapsed() -> None:
    """Documentation is evidence and not proof of delivered behaviour.

    A documented behaviour a build changed reads the same as one it did not,
    so the queue for whoever next has a pinned binary is kept as a list rather
    than folded into "the adapter handles it". This asserts the list is
    non-empty on purpose: claiming everything measured would be the exact
    overstatement the standing field exists to prevent.
    """
    pending = unmeasured(CLAUDE_DELIVERY) + unmeasured(CODEX_DELIVERY)

    assert pending
    assert all(fact.standing == "documented" for fact in pending)


def test_nothing_rests_on_a_belief_carried_forward_from_a_prior_session() -> None:
    """`assumed` exists to be nameable and to stay empty.

    A claim somebody carried forward is the one standing that is neither
    observation nor vendor statement, and the failure it produces is
    invisible: everything reads as settled.
    """
    for facts in (CLAUDE_DELIVERY, CODEX_DELIVERY):
        assert not [fact for fact in facts if fact.standing == "assumed"]


def test_a_reader_sees_the_standing_before_the_claim() -> None:
    """The standing qualifies the mechanism, so it is not printed after it."""
    fact = DeliveryFact(
        guarantee="defer_is_transparent",
        provider="claude",
        mechanism="the hook emits no decision",
        standing="documented",
        fallback="none needed",
    )

    assert fact.line().index("[documented]") < fact.line().index("emits no decision")


@pytest.mark.parametrize("held", [False, True], ids=["no-dashboard", "dashboard"])
def test_claude_s_auto_mode_claim_is_what_its_dispatcher_does(
    tmp_path: Path, held: bool
) -> None:
    """The row says where a person's question goes; the generated hook is asked.

    Measured on Claude Code 2.1.283: an interactive auto-mode session held a
    hook's ask as a prompt, and nothing ran unanswered. So with no dashboard
    the hook asks; with one it parks, and only a recorded answer releases the
    call. This runs the dispatcher under an auto-mode payload both ways.
    """
    fact = next(
        fact for fact in CLAUDE_DELIVERY if fact.guarantee == "ask_survives_auto_mode"
    )
    assert fact.standing == "measured"
    assert "2.1.283" in fact.mechanism
    assert "parks every ask" in fact.mechanism
    assert "2.1.263" in fact.fallback
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    payload = {
        "session_id": "auto-mode-session",
        "cwd": str(root),
        "hook_event_name": "PreToolUse",
        "permission_mode": "auto",
        "tool_name": "Bash",
        "tool_input": {"command": "git push --delete origin topic"},
    }
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()
    answered = json.loads(
        str(
            sh.Command(str(script))(
                _in=json.dumps(payload),
                _env={
                    **{
                        name: value
                        for name, value in os.environ.items()
                        if name != DASHBOARD_URL_ENV
                    },
                    "CLAUDE_PLUGIN_DATA": str(tmp_path / "plugin-data"),
                    **({DASHBOARD_URL_ENV: "http://127.0.0.1:8766"} if held else {}),
                },
            )
        )
    )

    parked = QuestionRelay(root / ".lup/questions.jsonl").pending()
    decided = answered["hookSpecificOutput"]["permissionDecision"]
    if not held:
        assert decided == "ask"
        assert parked == []
        return
    (question,) = parked
    assert decided == "deny"
    assert question.requirement == "human_only"
