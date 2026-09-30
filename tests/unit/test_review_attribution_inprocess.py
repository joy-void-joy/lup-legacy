"""Original file decisions survive in-process policy and durable approval."""

import json
from hashlib import sha256
from pathlib import Path

import pytest

from lup.policy.assets.host import document_digest

from lup.policy import rules
from lup.policy.boundary import (
    BoundaryPreflight,
    CapabilityEvidence,
    CapabilityRequirement,
    ExecutionBoundary,
)
from lup.policy.checkpoints import RecoveryCoordinator
from lup.policy.coordinator import OperationCoordinator
from lup.policy.kernel.decision import KernelDecision, captured_edit_decision
from lup.policy.kernel.policy_protocol import decision_wire
from lup.policy.kernel.rows import PathRoleRow
from lup.policy.models import Decision, EditBatch, EditChange, ShellCommand
from lup.policy.operations import Operation
from lup.policy.relay import Principal, QuestionRelay, SupervisorChain
from lup.policy.rules import EditPolicy, PathRule, ShellPolicy


def test_pydantic_roundtrip_retains_exact_captured_file_verdict(tmp_path: Path) -> None:
    original = captured_edit_decision(
        KernelDecision("ask", "human review", rule="edit:protected"),
        str(tmp_path / "source.txt"),
        before_sha256=document_digest("before\n"),
        after_sha256=document_digest("after\n"),
        after="after\n",
    )
    validated = Decision.model_validate_json(Decision.of(original).model_dump_json())
    assert validated.as_kernel().file_reviews == original.file_reviews
    assert validated.placed(escapable=False).file_reviews == original.file_reviews


@pytest.mark.parametrize("protected_test", [False, True])
def test_mixed_batch_records_real_file_gates(
    tmp_path: Path, protected_test: bool
) -> None:
    (tmp_path / "tests").mkdir()
    test_file = tmp_path / "tests/check.txt"
    source_file = tmp_path / "source.txt"
    for path in (test_file, source_file):
        path.write_text("before\n")
    paths = ["source.txt", *(["tests/check.txt"] if protected_test else [])]
    policy = EditPolicy(
        protected=[
            PathRule(kind="exact", value=path, reason="human review") for path in paths
        ],
        path_roles=[PathRoleRow(root="tests", role="test")],
    )
    decision = policy.decide(
        EditBatch(
            cwd=tmp_path,
            changes=[
                EditChange(path=path, before="before\n", after="after\n")
                for path in (source_file, test_file)
            ],
        )
    )
    assert decision.effect == "ask"
    assert [(row["path"], row["effect"]) for row in decision.file_reviews] == [
        (str(source_file), "ask"),
        (str(test_file), "ask" if protected_test else "allow"),
    ]
    assert all(
        row["before_sha256"] == sha256(b"before\n").hexdigest()
        for row in decision.file_reviews
    )
    assert all(
        row["after_sha256"] == sha256(b"after\n").hexdigest()
        for row in decision.file_reviews
    )


def test_destination_verdict_is_captured_after_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = KernelDecision("ask", "destination owns this test", rule="edit:human-owned")
    reply = json.dumps({"protocol": 1, "decision": decision_wire(owner)})
    monkeypatch.setattr(rules, "routed_edit_response", lambda *args: reply)
    target = tmp_path / "tests/check.txt"
    decision = EditPolicy(
        protected=[], path_roles=[PathRoleRow(root="tests", role="test")]
    ).decide_change(
        EditChange(path=target, before="before\n", after="after\n"), tmp_path
    )
    assert decision.file_reviews[0]["effect"] == "ask"
    assert decision.file_reviews[0]["reason"] == owner.reason
    assert decision.file_reviews[0]["rule"] == owner.rule


def test_shell_ask_keeps_automatically_allowed_authored_file_evidence(
    tmp_path: Path,
) -> None:
    (tmp_path / "tests").mkdir()
    policy = ShellPolicy(
        rules=[],
        authored=EditPolicy(
            protected=[], path_roles=[PathRoleRow(root="tests", role="test")]
        ),
    )
    command = "cat > tests/check.txt <<'EOF'\nvalue\nEOF"
    decision = policy.decide(ShellCommand(command=command, cwd=tmp_path))
    assert decision.effect == "ask"
    assert len(decision.file_reviews) == 1
    assert decision.file_reviews[0]["effect"] == "allow"
    assert decision.file_reviews[0]["path"] == str(tmp_path / "tests/check.txt")


def test_every_write_a_command_carries_keeps_its_own_evidence(tmp_path: Path) -> None:
    """The write that decides speaks for the command; every file keeps its row."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "source.txt").write_text("before\n")
    policy = ShellPolicy(
        rules=[],
        authored=EditPolicy(
            protected=[PathRule(kind="exact", value="source.txt", reason="owned")],
            path_roles=[PathRoleRow(root="tests", role="test")],
        ),
    )
    command = (
        "cat > tests/check.txt <<'EOF'\nvalue\nEOF\n"
        "cat > source.txt <<'EOF'\nafter\nEOF"
    )

    decision = policy.decide(ShellCommand(command=command, cwd=tmp_path))

    assert decision.effect == "ask"
    assert sorted((row["path"], row["effect"]) for row in decision.file_reviews) == [
        (str(tmp_path / "source.txt"), "ask"),
        (str(tmp_path / "tests/check.txt"), "allow"),
    ]


def test_parked_attribution_is_durable_and_cannot_reuse_a_different_receipt(
    tmp_path: Path,
) -> None:
    running = OperationCoordinator(
        relay=QuestionRelay(tmp_path / "questions.jsonl"),
        recovery=RecoveryCoordinator(tmp_path / "store"),
        preflight=BoundaryPreflight(
            boundary=ExecutionBoundary(
                name="test",
                contained=True,
                capabilities=[
                    CapabilityRequirement(capability="inside_placement"),
                    CapabilityRequirement(capability="question_relay"),
                ],
            ),
            evidence=[
                CapabilityEvidence(capability="inside_placement", delivered=True),
                CapabilityEvidence(capability="question_relay", delivered=True),
            ],
        ),
        chain=SupervisorChain(
            principals=[
                Principal(id="worker", kind="agent", supervisor="person"),
                Principal(id="person", kind="human"),
            ]
        ),
    )
    operation = Operation(
        id="edit-1",
        session="session-1",
        requester="worker",
        tool="apply_patch",
        payload={"input": "exact proposed patch"},
        cwd=tmp_path,
        worktree=tmp_path,
    )
    verdict = captured_edit_decision(
        KernelDecision("ask", "review", rule="edit:protected"),
        str(tmp_path / "source.txt"),
        before_sha256=document_digest("before\n"),
        after_sha256=document_digest("after\n"),
        after="after\n",
    )
    preliminary = running.preliminary(operation, verdict)
    assert preliminary.stage == "parked" and preliminary.question is not None
    question = preliminary.question
    recorded = running.relay.find(question.id)
    assert recorded is not None and recorded.file_reviews is not None
    assert recorded.operation == operation
    assert recorded.file_reviews[0].rule == "edit:protected"
    changed = captured_edit_decision(
        KernelDecision("ask", "review", rule="edit:budget"),
        str(tmp_path / "source.txt"),
        before_sha256=document_digest("before\n"),
        after_sha256=document_digest("after\n"),
        after="after\n",
    )
    assert running.park(operation, changed, None).id != question.id
    running.relay.answer(question.id, "person", approved=True)
    assert running.resume(question.id, operation).stage == "prepared"
    assert (
        running.resume(
            question.id,
            operation.model_copy(update={"payload": {"input": "different patch"}}),
        ).stage
        == "refused"
    )
    # The one record a session can still write is its question's: an answer
    # is the host's. Rewritten under the same id, the question keeps the
    # operator's answer and loses the attribution that answer was bound to.
    running.relay.record(
        recorded.model_copy(
            update={
                "file_reviews": [
                    recorded.file_reviews[0].model_copy(update={"effect": "allow"})
                ]
            }
        )
    )
    assert running.resume(question.id, operation).stage == "refused"
