"""The question the dispatcher reaches is the question the queue holds.

The relay being "one authority" is a claim about the *live* path or it is not
a claim at all: a record only the in-process seam wrote would be a store the
compiled dispatcher does not use, and the compiled dispatcher is what a native
session actually runs.

So this drives the emitted script — not a renderer, not the canonical policy —
and reads the queue back through the surface a reviewer reads it through. The
script is Codex's, because a runtime with an ask effect renders the question
instead of parking it, and the queue is what the other one falls back to.
"""

import json
from pathlib import Path

import sh

from typer.testing import CliRunner

from lup.devtools.review.app import create_review_app, relay
from lup.types import JsonObject

RUNNER = CliRunner()

DISPATCHER = Path(".codex/plugins/lup/hooks/scripts/policy.py")


def judged(command: str, cwd: Path) -> JsonObject:
    """Put one command through the script a native session runs."""
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "requester",
        "tool_use_id": "call-one",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd),
    }
    result = sh.Command(str(DISPATCHER.resolve()))(
        _in=json.dumps(payload), _ok_code=[0, 2], _cwd=str(cwd), _return_cmd=True
    )
    assert isinstance(result, sh.RunningCommand)
    # A permitted call is answered by saying nothing, so there is no verdict
    # document to read back — only the absence of one.
    return json.loads(result.stdout) if result.stdout else {}


def test_a_question_the_dispatcher_reaches_is_parked_in_the_relay(
    tmp_path: Path,
) -> None:
    """Written from the boundary that reaches the verdict, so both runtimes do.

    The alternative is a queue populated only by whichever path happened to
    call the library — which is the shape that makes "every final ask is
    recorded" true of some asks.
    """
    response = judged("git push --delete origin feat", tmp_path)
    specific = response["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "deny"

    parked = relay(tmp_path).questions()

    assert [entry.state for entry in parked] == ["pending"]
    assert "deleting a remote branch" in parked[0].reason


def test_a_parked_question_carries_the_rule_that_asked_it(tmp_path: Path) -> None:
    """A queue of unattributable questions is one nobody can tune.

    The reviewer reading it needs to know which gate produced it — and the
    person answering the same question for the third time needs somewhere to
    go and change it.
    """
    judged("git push --delete origin feat", tmp_path)

    parked = relay(tmp_path).questions()[0]

    assert parked.rule == "shell:git.push"
    assert parked.requirement == "human_only"


def test_the_same_question_twice_folds_to_one_record(tmp_path: Path) -> None:
    """A session asks the same thing repeatedly and a queue is read by a person.

    Fifty identical rows is a queue nobody reads, which is the same failure the
    undo layer's dedup exists to prevent, in the same shape.
    """
    judged("git push --delete origin feat", tmp_path)
    judged("git push --delete origin feat", tmp_path)

    assert len(relay(tmp_path).questions()) == 1


def test_a_permitted_operation_parks_nothing(tmp_path: Path) -> None:
    """The queue holds questions, and an allow asked nobody anything."""
    judged("git status", tmp_path)

    assert relay(tmp_path).questions() == []


def test_a_reviewer_can_answer_what_the_dispatcher_parked(tmp_path: Path) -> None:
    """The whole point of a durable queue is that somebody can clear it.

    Parked by the hermetic dispatcher, which cannot resolve a supervisor chain,
    and answered through the surface a reviewer reads — the two ends of the one
    authority, exercised against each other rather than each against its own
    fixture.
    """
    judged("git push --delete origin feat", tmp_path)
    app = create_review_app(tmp_path)
    parked = relay(tmp_path).questions()[0]

    listed = RUNNER.invoke(app, ["list"])
    answered = RUNNER.invoke(
        app, ["approve", parked.id, "--as", "person", "--note", "force-with-lease"]
    )
    settled = relay(tmp_path).find(parked.id)

    assert parked.id in listed.stdout
    assert answered.exit_code == 0
    assert settled is not None
    assert settled.state == "approved"
    assert settled.answer is not None
    assert settled.answer.note == "force-with-lease"
    assert judged("git push --delete origin feat", tmp_path) == {}
    retried = judged("git push --delete origin feat", tmp_path)["hookSpecificOutput"]
    assert isinstance(retried, dict) and retried["permissionDecision"] == "deny"
    (pending,) = relay(tmp_path).pending()
    assert pending.id != parked.id


def test_rejection_stops_retry_and_preserves_the_rule(tmp_path: Path) -> None:
    command = "git push --delete origin feat"
    judged(command, tmp_path)
    (question,) = relay(tmp_path).pending()
    rejected = RUNNER.invoke(
        create_review_app(tmp_path), ["decline", question.id, "--as", "operator"]
    )
    assert rejected.exit_code == 0
    specific = judged(command, tmp_path)["hookSpecificOutput"]
    assert isinstance(specific, dict) and specific["permissionDecision"] == "deny"
    reason = str(specific["permissionDecisionReason"])
    assert "declined" in reason and "deleting a remote branch" in reason
    persisted = relay(tmp_path).find(question.id)
    assert persisted is not None and persisted.state == "rejected"
    assert persisted.rule == "shell:git.push"
    assert len(relay(tmp_path).questions()) == 1


def test_approved_question_cannot_release_a_changed_call(tmp_path: Path) -> None:
    original = "git push --delete origin feat"
    judged(original, tmp_path)
    (question,) = relay(tmp_path).pending()
    relay(tmp_path).answer(question.id, "operator", True)
    changed = judged("git push --delete origin different", tmp_path)[
        "hookSpecificOutput"
    ]
    assert isinstance(changed, dict) and changed["permissionDecision"] == "deny"
    assert judged(original, tmp_path) == {}


def test_an_answer_the_dispatcher_could_not_scope_says_so(tmp_path: Path) -> None:
    """A weaker receipt is recorded as weaker.

    The dispatcher reaches a verdict without the session's principals, so the
    approval it enables stands on no resolved chain — which the record says,
    rather than reporting an approval that looks checked.
    """
    judged("git push --delete origin feat", tmp_path)
    parked = relay(tmp_path).questions()[0]

    RUNNER.invoke(create_review_app(tmp_path), ["approve", parked.id, "--as", "person"])
    settled = relay(tmp_path).find(parked.id)

    assert settled is not None and settled.answer is not None
    assert settled.answer.unresolved_chain
