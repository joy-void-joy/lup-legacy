"""The reviewer's half of the relay: reading a queue and answering from it.

A durable record with no way to read it is a queue that silently grows, so
what is pinned here is that the surface exists, that it narrows to what a
reviewer may actually answer, and that the requester reading its own question
is not the requester answering it.
"""

from pathlib import Path

from typer.testing import CliRunner

from lup.devtools.review.app import create_review_app, relay
from lup.policy.operations import Operation
from lup.policy.relay import PersistentQuestion

RUNNER = CliRunner()


def parked(root: Path) -> None:
    """One question in one checkout's relay, as the coordinator would park it."""
    call = Operation(
        id="op-1",
        session="s",
        requester="worker",
        tool="Bash",
        payload={"command": "gh pr merge 12"},
        cwd=root,
        worktree=root,
    )
    relay(root).record(
        PersistentQuestion(
            id="q-1",
            operation=call,
            fingerprint=call.fingerprint(),
            reason="merging runs the change into the base branch",
            rule="shell:gh.pr.merge",
            purpose="external_consequence",
            requirement="human_only",
            eligible=["person"],
        )
    )


def test_a_reviewer_sees_only_what_they_may_answer(tmp_path: Path) -> None:
    """A supervisor shown a question it may not answer is one about to try.

    And the refusal it then receives teaches it nothing about which questions
    are its, so the listing narrows by eligibility rather than leaving the
    reviewer to discover it one refusal at a time.
    """
    parked(tmp_path)
    app = create_review_app(tmp_path)

    theirs = RUNNER.invoke(app, ["list", "--as", "person"])
    not_theirs = RUNNER.invoke(app, ["list", "--as", "lead"])

    assert "q-1" in theirs.stdout
    assert "nothing is waiting" in not_theirs.stdout


def test_the_requester_reads_its_own_question_and_cannot_answer_it(
    tmp_path: Path,
) -> None:
    """Status is not authority, and the split is enforced on the record.

    Leaving the command out of an agent's tool list would make the reviewer
    "whoever could reach the command", which changes the moment somebody adds
    a tool.
    """
    parked(tmp_path)
    app = create_review_app(tmp_path)

    read = RUNNER.invoke(app, ["show", "q-1"])
    answered = RUNNER.invoke(app, ["approve", "q-1", "--as", "worker"])

    assert read.exit_code == 0
    assert "shell:gh.pr.merge" in read.stdout
    assert answered.exit_code == 2
    assert "may not answer" in answered.stderr


def test_an_answer_carries_its_note_and_says_who_resumes_the_operation(
    tmp_path: Path,
) -> None:
    """The requester does not reissue what was approved.

    An agent asked to reconstruct an approved call is an agent that can
    reconstruct a different one, so the surface says who resumes it — and the
    note travels with the resumption rather than as a second message.
    """
    parked(tmp_path)
    app = create_review_app(tmp_path)

    result = RUNNER.invoke(
        app, ["approve", "q-1", "--as", "person", "--note", "squash it"]
    )
    shown = RUNNER.invoke(app, ["show", "q-1"])

    assert result.exit_code == 0
    assert "spent once" in result.stdout
    assert "squash it" in shown.stdout


def test_a_question_that_does_not_exist_is_a_refusal_and_not_a_silence(
    tmp_path: Path,
) -> None:
    app = create_review_app(tmp_path)

    result = RUNNER.invoke(app, ["show", "q-9"])

    assert result.exit_code == 2
    assert "no review" in result.stderr
