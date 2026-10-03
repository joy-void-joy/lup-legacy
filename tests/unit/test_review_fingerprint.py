"""A parked review stays bound to what its record holds, whatever a later model adds.

A review is bound to the fingerprint its hook computed over the record it
parked, and every reader checks the record against it before anybody may
answer. A reader hashes the record as it holds it -- each row with the fields
it carries, through the hook's own function -- so a field a later version
adds with a default, absent from a record parked before it, never enters the
recomputation: that record stays answerable, and is carried out. Anything
else that moves -- the call, a path, the document a verdict judged, a reason,
a rule, the policy that judged it, a field added with a value -- still
unbinds it, on the reader's side and the hook's alike.

Each case is put to every path that parks a review: each runtime's generated
dispatcher, and a question the relay records in process, as `review propose`
does; the coordinator, which binds its own questions, is checked beside them.
"""

import json
import os
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Literal

import pytest
import sh
from pydantic import create_model

from lup.coordination.identity import MEMBER_ENV
from lup.devtools.review import app
from lup.devtools.review.app import ReviewSummary, runs_this_code
from lup.devtools.review.wait import carried_out
from lup.policy.assets.host import (
    document_digest,
    park_relay_entry,
    recorded_fingerprint,
    resolved_entry,
)
from lup.policy.boundary import (
    BoundaryPreflight,
    CapabilityEvidence,
    CapabilityRequirement,
    ExecutionBoundary,
)
from lup.policy.checkpoints import RecoveryCoordinator
from lup.policy.coordinator import OperationCoordinator
from lup.policy.identity import DASHBOARD_URL_ENV
from lup.policy.kernel.decision import KernelDecision, captured_edit_decision
from lup.policy.models import ShellCommand
from lup.policy.operations import Operation
from lup.policy.relay import (
    CapturedFileReview,
    CommandSegment,
    PersistentQuestion,
    Principal,
    ProtectedMatch,
    QuestionRelay,
    SupervisorChain,
    UnpreviewedStep,
)
from lup.policy.rules import EditPolicy, PathRule, ShellPolicy
from lup.types import JsonObject
from tests.unit.native import bound
from tests.unit.repos import initialized_repo

type Parker = Literal["claude", "codex", "relay"]

PARKERS: list[Parker] = ["claude", "codex", "relay"]

ESCALATED = "# lup: escalate[decision]: the operator reviews this\n"
WRITE = f"{ESCALATED}echo carried > marker.txt"
FIXTURE = Path(__file__).parent / "fixtures/reviews/parked_before_protected.json"

LaterProtected = create_model("LaterProtected", __base__=ProtectedMatch, note=(str, ""))
LaterFileReview = create_model(
    "LaterFileReview",
    __base__=CapturedFileReview,
    protected=(LaterProtected | None, None),
    weight=(int, 0),
)
LaterSegment = create_model("LaterSegment", __base__=CommandSegment, subject=(str, ""))
LaterStep = create_model("LaterStep", __base__=UnpreviewedStep, shown=(bool, False))
LaterQuestion = create_model(
    "LaterQuestion",
    __base__=PersistentQuestion,
    file_reviews=(list[LaterFileReview] | None, None),
    unpreviewed=(list[LaterStep] | None, None),
    segments=(list[LaterSegment] | None, None),
)
"""The question model as a later version might declare it: a new field, with a default, on every row it binds."""


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository of its own, where the roster the hook reads is kept."""
    checkout = tmp_path / "checkout"
    initialized_repo(checkout, tmp_path / "hooks", branch="feature")
    monkeypatch.delenv(MEMBER_ENV, raising=False)
    return checkout


def relay_of(root: Path) -> QuestionRelay:
    return QuestionRelay(root / ".lup/questions.jsonl", root / "host/answers.jsonl")


def parked(root: Path, parker: Parker) -> str:
    """Park one escalated write by *parker*, returning the review's id.

    A runtime's generated dispatcher parks it from a hook payload; ``relay``
    records it in process, from the verdict the in-process policy reaches on
    the same command, as `review propose` records its own.
    """
    match parker:
        case "claude" | "codex":
            payload: JsonObject = {
                "session_id": "asking-session",
                "cwd": str(root),
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": WRITE},
                "tool_use_id": "call-1",
            }
            sh.Command(
                str(Path(f".{parker}/plugins/lup/hooks/scripts/policy.py").resolve())
            )(
                _in=json.dumps(payload),
                _ok_code=[0, 2],
                _env={
                    **os.environ,
                    "CLAUDE_PLUGIN_DATA": str(root / "plugin-data"),
                    "PLUGIN_DATA": str(root / "plugin-data"),
                    DASHBOARD_URL_ENV: "http://127.0.0.1:8766",
                },
            )
        case "relay":
            decision = ShellPolicy(
                rules=[],
                authored=EditPolicy(
                    protected=[
                        PathRule(kind="exact", value="marker.txt", reason="owned")
                    ]
                ),
            ).decide(ShellCommand(command=WRITE, cwd=root))
            operation = Operation(
                id="in-process",
                session="asking-session",
                requester="asking-session",
                tool="Bash",
                payload={"command": WRITE},
                cwd=root,
                worktree=root,
            )
            relay_of(root).record(
                bound(
                    PersistentQuestion(
                        id=operation.id,
                        operation=operation,
                        fingerprint="",
                        preconditions={root / "marker.txt": None},
                        file_reviews=[
                            CapturedFileReview.model_validate(row)
                            for row in decision.file_reviews
                        ],
                        segments=[
                            CommandSegment.model_validate(row)
                            for row in decision.segments
                        ],
                        resumption="native_retry",
                        reason=decision.reason,
                        rule=decision.rule,
                        chain_resolved=False,
                        resolved={root / "marker.txt": root / "marker.txt"},
                    )
                )
            )
    (question,) = relay_of(root).pending()
    return question.id


def held(relay: QuestionRelay, question: str) -> JsonObject:
    """One parked record as its log holds it, every document it names read back whole."""
    relay.refreshed()
    return resolved_entry(
        json.loads(json.dumps(relay.fold.entries[question])), relay.blobs
    )


def reparked(relay: QuestionRelay, entry: JsonObject) -> None:
    """Write *entry* over the record parked under its id, as a session's own append would."""
    park_relay_entry(relay.path, entry)


def before_protected(entry: JsonObject) -> JsonObject:
    """*entry* as a hook that never knew ``protected`` parked it: no such key, and the fingerprint it hashed then."""
    rows = entry["file_reviews"]
    assert isinstance(rows, list)
    older: JsonObject = {
        **entry,
        "file_reviews": [
            {field: value for field, value in row.items() if field != "protected"}
            for row in rows
            if isinstance(row, dict)
        ],
    }
    return {**older, "fingerprint": recorded_fingerprint(older)}


def test_a_record_parked_before_protected_existed_is_answered_as_it_was_parked(
    tmp_path: Path,
) -> None:
    """The fixture is a record as a hook wrote it before file verdicts named their rule."""
    relay = relay_of(tmp_path)
    entry = json.loads(FIXTURE.read_text())
    reparked(relay, entry)

    (waiting,) = relay.pending()
    question = relay.resolve(waiting)

    rows = question.held()["file_reviews"]
    assert isinstance(rows, list)
    assert all(isinstance(row, dict) and "protected" not in row for row in rows)
    assert question.native_fingerprint() == entry["fingerprint"]
    assert question.unbound() == ""
    answered = relay.answer(question.id, "operator", True)
    assert answered.state == "approved"


@pytest.mark.parametrize("parker", PARKERS)
def test_a_record_parked_before_a_field_existed_is_answered_and_carried_out(
    root: Path, parker: Parker
) -> None:
    relay = relay_of(root)
    question = parked(root, parker)
    reparked(relay, before_protected(held(relay, question)))

    shown = relay.question(question)
    assert shown is not None
    assert shown.bound()
    assert ReviewSummary.of(root, shown, "operator").answerable
    relay.answer(question, "operator", True)
    approved = relay.find(question)
    assert approved is not None

    outcome = carried_out(root, relay, approved)

    assert (outcome.verdict, outcome.carried) == ("ran", True)
    assert (root / "marker.txt").read_text() == "carried\n"


@pytest.mark.parametrize("parker", PARKERS)
def test_a_field_a_later_model_adds_leaves_every_record_bound(
    root: Path, parker: Parker
) -> None:
    relay = relay_of(root)
    question = parked(root, parker)
    entry = held(relay, question)

    later = LaterQuestion.model_validate(entry)

    assert later.bound()
    assert later.native_fingerprint() == entry["fingerprint"]
    rows = later.held()["file_reviews"]
    assert isinstance(rows, list) and rows
    assert all(isinstance(row, dict) and "weight" not in row for row in rows)


def altered(change: Callable[[JsonObject], None]) -> Callable[[JsonObject], JsonObject]:
    def applied(entry: JsonObject) -> JsonObject:
        copied = json.loads(json.dumps(entry))
        change(copied)
        return copied

    return applied


def first_row(entry: JsonObject) -> JsonObject:
    rows = entry["file_reviews"]
    assert isinstance(rows, list) and isinstance(rows[0], dict)
    return rows[0]


def operation_of(entry: JsonObject) -> JsonObject:
    operation = entry["operation"]
    assert isinstance(operation, dict)
    return operation


ALTERATIONS: dict[str, Callable[[JsonObject], JsonObject]] = {
    "payload": altered(
        lambda entry: operation_of(entry).update(
            {"payload": {"command": f"{ESCALATED}echo other > marker.txt"}}
        )
    ),
    "path": altered(lambda entry: first_row(entry).update({"path": "/etc/hosts"})),
    "after document": altered(
        lambda entry: first_row(entry).update({"after": "something else\n"})
    ),
    "reason": altered(lambda entry: entry.update({"reason": "nothing to see"})),
    "rule": altered(lambda entry: entry.update({"rule": "edit:scratch"})),
    "policy identity": altered(
        lambda entry: entry.update({"policy_identity": json.dumps(["a", "b", "c"])})
    ),
    "protected with a value": altered(
        lambda entry: first_row(entry).update(
            {
                "protected": {
                    "kind": "exact",
                    "root": "marker.txt",
                    "description": "a file nobody minds",
                }
            }
        )
    ),
    "a row's verdict": altered(
        lambda entry: first_row(entry).update({"effect": "allow"})
    ),
    "preimage": altered(
        lambda entry: entry.update(
            {
                "preconditions": {
                    path: "stood here\n"
                    for path in (
                        entry["preconditions"]
                        if isinstance(entry["preconditions"], dict)
                        else {}
                    )
                }
            }
        )
    ),
}


@pytest.mark.parametrize("parker", PARKERS)
@pytest.mark.parametrize("alteration", list(ALTERATIONS))
def test_a_record_altered_after_it_was_parked_is_refused_everywhere(
    root: Path, parker: Parker, alteration: str
) -> None:
    """Only a field the record never carried is left out; every bound value still binds."""
    relay = relay_of(root)
    question = parked(root, parker)
    entry = held(relay, question)
    changed = ALTERATIONS[alteration](entry)
    reparked(relay, changed)

    shown = relay.question(question)

    assert shown is not None
    assert not shown.bound()
    assert recorded_fingerprint(held(relay, question)) != entry["fingerprint"]
    with pytest.raises(ValueError, match="does not hash to its fingerprint"):
        relay.answer(question, "operator", True)


@pytest.mark.parametrize("parker", PARKERS)
def test_a_default_written_into_a_record_that_never_carried_it_is_refused(
    root: Path, parker: Parker
) -> None:
    """Absent and defaulted is what was hashed; the same value written in later is an alteration."""
    relay = relay_of(root)
    question = parked(root, parker)
    older = before_protected(held(relay, question))
    reparked(relay, older)
    reparked(
        relay,
        altered(lambda entry: first_row(entry).update({"protected": None}))(older),
    )

    shown = relay.question(question)

    assert shown is not None and not shown.bound()


def coordinated(tmp_path: Path) -> OperationCoordinator:
    return OperationCoordinator(
        relay=QuestionRelay(tmp_path / "questions.jsonl", tmp_path / "answers.jsonl"),
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


def test_the_coordinator_binds_its_own_questions_as_they_are_held(
    tmp_path: Path,
) -> None:
    """A coordinator's question parked before a field existed resumes; one altered since does not."""
    running = coordinated(tmp_path)
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
    parked_question = running.preliminary(operation, verdict).question
    assert parked_question is not None
    relay = running.relay
    entry = held(relay, parked_question.id)
    rows = [
        {
            field: value
            for field, value in first_row(entry).items()
            if field != "protected"
        }
    ]
    older = {
        **entry,
        "file_reviews": rows,
        "fingerprint": sha256(
            json.dumps([operation.fingerprint(), rows, []], sort_keys=True).encode()
        ).hexdigest(),
    }
    reparked(relay, older)
    relay.answer(parked_question.id, "person", approved=True)

    assert running.resume(parked_question.id, operation).stage == "prepared"
    later: list[CapturedFileReview] = [
        LaterFileReview.model_validate(row) for row in rows
    ]
    assert (
        PersistentQuestion.review_fingerprint(operation, later, None, None)
        == older["fingerprint"]
    )
    reparked(
        relay,
        altered(
            lambda entry: first_row(entry).update(
                {
                    "protected": {
                        "kind": "exact",
                        "root": "source.txt",
                        "description": "x",
                    }
                }
            )
        )(older),
    )
    assert running.resume(parked_question.id, operation).stage == "refused"


def test_a_review_this_code_cannot_bind_names_the_hook_that_parked_it(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where the checkout's own commands run this code, offering them would only refuse it again."""
    relay = relay_of(root)
    question = parked(root, "claude")
    reparked(relay, ALTERATIONS["reason"](held(relay, question)))
    shown = relay.question(question)
    assert shown is not None
    script = shown.parked_by()
    assert len(script) == 64

    elsewhere = ReviewSummary.of(root, shown, "operator").unanswerable
    monkeypatch.setattr(app, "runs_this_code", lambda checkout: checkout == root)
    here = ReviewSummary.of(root, shown, "operator").unanswerable

    cancel = f"`uv run --directory {root} lup-devtools review cancel {question}`"
    for said in (elsewhere, here):
        assert said.startswith("Nothing here may answer it: its record does not hash")
        assert f"the hook whose compiled script hashes to {script}" in said
        assert cancel in said
    assert "review approve" in elsewhere
    assert "review approve" not in here
    assert "It has to be asked again" in here


def test_a_checkout_s_commands_run_this_code_only_where_this_code_lives(
    tmp_path: Path,
) -> None:
    assert runs_this_code(Path(__file__).resolve().parents[2])
    assert not runs_this_code(tmp_path)
