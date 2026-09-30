"""Review focus comes from exact original verdicts, never path conventions."""

import json
from pathlib import Path

import pytest

from lup.policy.assets.host import document_digest

from lup.devtools.review.app import ReviewDetail, ReviewFile
from lup.devtools.dev.edit_prepare import native_patch
from lup.policy.assets.host import review_hook_call
from lup.policy.kernel.decision import (
    DecisionEffect,
    KernelDecision,
    captured_edit_decision,
)
from lup.policy.operations import Operation
from lup.policy.models import EditBatch, EditChange
from lup.policy.relay import CapturedFileReview, PersistentQuestion, QuestionRelay
from lup.policy.review import ReviewedFile


def captured(
    change: ReviewedFile,
    effect: DecisionEffect = "ask",
    rule: str = "edit:protected-path",
) -> CapturedFileReview:
    decision = captured_edit_decision(
        KernelDecision(effect, "original policy reason", rule=rule),
        str(change.path),
        before_sha256=document_digest(change.before),
        after_sha256=document_digest(change.after),
        after=change.after,
    )
    return CapturedFileReview.model_validate(decision.file_reviews[0])


@pytest.mark.parametrize(
    "filename", ["tests/test_protected.py", "README.md", "src/app.py"]
)
def test_actual_verdict_controls_focus_regardless_of_path(
    tmp_path: Path, filename: str
) -> None:
    change = ReviewedFile(path=tmp_path / filename, before="old\n", after="new\n")
    assert ReviewFile.of(change, [captured(change, "ask")]).review_effect == "ask"
    assert ReviewFile.of(change, [captured(change, "allow")]).review_effect == "allow"
    assert ReviewFile.of(change, [captured(change, "deny")]).review_effect == "deny"
    assert ReviewFile.of(change, [captured(change, "defer")]).review_effect == "defer"
    assert ReviewFile.of(change).review_effect == "unknown"


@pytest.mark.parametrize("changed", ["before", "after", "path"])
@pytest.mark.parametrize("effect", ["allow", "defer"])
def test_only_both_exact_images_and_path_can_hide_a_file(
    tmp_path: Path, changed: str, effect: DecisionEffect
) -> None:
    original = ReviewedFile(path=tmp_path / "app.py", before="old\n", after="new\n")
    revised = original.model_copy(
        update={
            changed: tmp_path / "elsewhere.py"
            if changed == "path"
            else "racing content\n"
        }
    )
    shown = ReviewFile.of(revised, [captured(original, effect)])
    assert shown.review_effect == "unknown"


def test_duplicate_capture_is_unknown(tmp_path: Path) -> None:
    change = ReviewedFile(path=tmp_path / "app.py", before="old\n", after="new\n")
    assert (
        ReviewFile.of(
            change, [captured(change, "allow"), captured(change, "ask")]
        ).review_effect
        == "unknown"
    )


def test_added_rule_is_separated_from_existing_rule_and_unchanged_directives(
    tmp_path: Path,
) -> None:
    before = "# lup: ignore[existing] — established\na = []\n# lup: ignore[stable] — already present\nb = []\n"
    after = before.replace("ignore[existing]", "ignore[existing, fresh]")
    change = ReviewedFile(path=tmp_path / "app.py", before=before, after=after)
    shown = ReviewFile.of(change, [captured(change, "ask", "edit:anti-pattern")])
    changed, stable = shown.suppressions
    assert changed.rule_ids == ["existing", "fresh"]
    assert changed.review_rule_ids == ["fresh"]
    assert changed.review_effect == "ask"
    assert stable.review_effect == "allow"
    assert stable.review_rule_ids == []


def test_historical_capture_still_excludes_old_and_resited_exceptions(
    tmp_path: Path,
) -> None:
    before = "# lup: ignore[existing]\na = []\n"
    after = "# heading\n" + before + "# lup: ignore[fresh]\nb = []\n"
    shown = ReviewFile.of(
        ReviewedFile(path=tmp_path / "app.py", before=before, after=after)
    )
    existing, fresh = shown.suppressions
    assert existing.review_effect == "allow"
    assert fresh.review_effect == "unknown"
    assert fresh.review_rule_ids == ["fresh"]


@pytest.mark.parametrize(
    ("effect", "rule"), [("ask", "edit:size"), ("allow", "edit:anti-pattern")]
)
def test_edit_budget_or_allowance_does_not_claim_exception_approval(
    tmp_path: Path, effect: DecisionEffect, rule: str
) -> None:
    change = ReviewedFile(
        path=tmp_path / "app.py",
        before="a = 1\n",
        after="# lup: ignore[fresh]\na = []\n",
    )
    shown = ReviewFile.of(change, [captured(change, effect, rule)])
    assert shown.suppressions[0].review_effect == "allow"
    assert shown.review_effect == effect


def test_existing_marker_can_gate_new_code_without_being_highlighted_as_new(
    tmp_path: Path,
) -> None:
    change = ReviewedFile(
        path=tmp_path / "app.py",
        before="# lup: ignore[existing]\na = []\n",
        after="# lup: ignore[existing]\na = []\nb = []\n",
    )
    shown = ReviewFile.of(change, [captured(change, "ask", "edit:anti-pattern")])
    assert shown.review_effect == "ask"
    assert shown.suppressions[0].review_effect == "allow"


def test_mixed_batch_summary_references_only_original_asks(tmp_path: Path) -> None:
    asked = ReviewedFile(path=tmp_path / "app.py", before="old\n", after="new\n")
    allowed = ReviewedFile(
        path=tmp_path / "tests/test_app.py", before="old\n", after="new\n"
    )
    patch = "*** Begin Patch\n*** Update File: app.py\n@@\n-old\n+new\n*** Update File: tests/test_app.py\n@@\n-old\n+new\n*** End Patch\n"
    operation = Operation(
        id="op",
        session="s",
        requester="s",
        tool="apply_patch",
        payload={"command": patch},
        cwd=tmp_path,
        worktree=tmp_path,
    )
    question = PersistentQuestion(
        id="review",
        operation=operation,
        fingerprint="unchanged-binding",
        reason="original batch reason",
        preconditions={asked.path: asked.before, allowed.path: allowed.before},
        file_reviews=[captured(asked), captured(allowed, "allow")],
    )
    detail = ReviewDetail.of(tmp_path, question, "operator")
    assert detail.summary.title == "Update app.py"
    assert detail.summary.paths == ["app.py"]
    assert detail.summary.total_files == 2
    assert [file.review_effect for file in detail.files] == ["ask", "allow"]
    assert detail.question == question.shown()
    assert detail.question.operation.payload == {"command": patch}
    assert detail.summary.reason == "original batch reason"
    assert detail.question.fingerprint == "unchanged-binding"


@pytest.mark.parametrize("effect", ["allow", "defer"])
def test_shell_only_question_keeps_command_gate_when_no_file_asks(
    tmp_path: Path,
    effect: DecisionEffect,
) -> None:
    change = ReviewedFile(path=tmp_path / "app.txt", before="old\n", after="new\n")
    command = "sed -i s/old/new/ app.txt"
    operation = Operation(
        id="op",
        session="s",
        requester="s",
        tool="Bash",
        payload={"command": command},
        cwd=tmp_path,
        worktree=tmp_path,
    )
    question = PersistentQuestion(
        id="q",
        operation=operation,
        fingerprint="bound",
        reason="shell-level gate",
        preconditions={change.path: change.before},
        file_reviews=[captured(change, effect)],
    )
    detail = ReviewDetail.of(tmp_path, question, "operator")
    assert detail.command == command
    assert detail.summary.reason == "shell-level gate"
    assert detail.files[0].review_effect == effect
    assert detail.summary.paths == []
    assert detail.question.fingerprint == "bound"


def test_twenty_file_review_focuses_three_asks_and_keeps_the_exact_operation(
    tmp_path: Path,
) -> None:
    counts: list[tuple[DecisionEffect, int]] = [("ask", 3), ("allow", 8), ("defer", 9)]
    effects: list[DecisionEffect] = [
        effect for effect, count in counts for _ in range(count)
    ]
    changes = [
        ReviewedFile(path=tmp_path / f"file-{index}.txt", before="old\n", after="new\n")
        for index in range(len(effects))
    ]
    patch = native_patch(
        EditBatch(
            changes=[
                EditChange(path=change.path, before=change.before, after=change.after)
                for change in changes
            ],
            cwd=tmp_path,
        )
    )
    question = PersistentQuestion(
        id="review",
        operation=Operation(
            id="op",
            session="s",
            requester="s",
            tool="apply_patch",
            payload={"command": patch},
            cwd=tmp_path,
            worktree=tmp_path,
        ),
        fingerprint="unchanged-twenty-file-binding",
        reason="protected files require approval",
        preconditions={change.path: change.before for change in changes},
        file_reviews=[
            captured(change, effect)
            for change, effect in zip(changes, effects, strict=True)
        ],
    )

    detail = ReviewDetail.of(tmp_path, question, "operator")

    assert detail.summary.paths == ["file-0.txt", "file-1.txt", "file-2.txt"]
    assert detail.summary.title.startswith("Change 3 files")
    assert detail.summary.total_files == 20
    assert len(detail.files) == 20
    assert [file.review_effect for file in detail.files] == effects
    assert detail.question == question.shown()
    assert detail.question.operation.payload == {"command": patch}
    assert detail.question.fingerprint == "unchanged-twenty-file-binding"


def test_captured_attribution_changes_require_fresh_native_review(
    tmp_path: Path,
) -> None:
    change = ReviewedFile(path=tmp_path / "app.py", before="old", after="new")
    arguments = (
        tmp_path,
        "requester",
        "apply_patch",
        "{}",
        "{}",
        "reason",
        "rule",
        "",
        "human_only",
    )
    original = json.dumps([captured(change).model_dump(mode="json")])
    relay = QuestionRelay(tmp_path / ".lup/questions.jsonl")
    answers = str(relay.answers)
    first = review_hook_call(*arguments, file_reviews=original, answers=answers)
    relay.answer(first["id"], "operator", True)
    changed = review_hook_call(
        *arguments,
        file_reviews=json.dumps([captured(change, "allow").model_dump(mode="json")]),
        answers=answers,
    )
    assert changed["id"] != first["id"]
    assert changed["state"] == "pending"
    assert (
        review_hook_call(*arguments, file_reviews=original, answers=answers)["state"]
        == "approved"
    )
    stored = relay.question(first["id"])
    assert stored is not None
    assert stored.file_reviews == [captured(change)]


def test_kernel_revision_preserves_original_capture(tmp_path: Path) -> None:
    change = ReviewedFile(path=tmp_path / "app.py", before=None, after="a = 1\n")
    decision = captured_edit_decision(
        KernelDecision("ask", rule="edit:protected-path"),
        str(change.path),
        before_sha256=document_digest(change.before),
        after_sha256=document_digest(change.after),
        after=change.after,
    )
    assert (
        decision.revised(reason="native caption").file_reviews == decision.file_reviews
    )
    assert decision.placed(True).file_reviews == decision.file_reviews


def test_native_queue_retains_the_asked_and_automatic_file_verdicts(
    tmp_path: Path,
) -> None:
    from tests.unit.test_codex_review_queue import denial, hook

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    (tmp_path / "DESIGN.md").write_text("# Previous design\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_example.py").write_text("value = 1\n")
    command = "*** Begin Patch\n*** Add File: DESIGN.md\n+# Reviewed design\n*** Update File: tests/test_example.py\n@@\n-value = 1\n+value = 2\n*** End Patch\n"
    assert "written whole" in denial(hook(tmp_path, command))
    store = QuestionRelay(tmp_path / ".lup/questions.jsonl")
    (waiting,) = store.pending()
    question = store.resolve(waiting)
    assert question.file_reviews is not None
    shown = ReviewDetail.of(tmp_path, question, "operator")
    assert {file.path: file.review_effect for file in shown.files} == {
        str(tmp_path / "DESIGN.md"): "ask",
        str(tmp_path / "tests/test_example.py"): "allow",
    }
    assert shown.summary.paths == ["DESIGN.md"]
    assert shown.summary.total_files == 2
    assert question.operation.payload["command"] == command


def test_owner_transport_cannot_substitute_callers_file_evidence(
    tmp_path: Path,
) -> None:
    from lup.policy.kernel.policy_protocol import decision_wire, read_decision

    fake = ReviewedFile(path=tmp_path / "unrelated.py", before="fake", after="fake")
    wrong = captured_edit_decision(
        KernelDecision("ask", "owner veto", rule="edit:protected-path"),
        str(fake.path),
        before_sha256=document_digest(fake.before),
        after_sha256=document_digest(fake.after),
        after=fake.after,
    )
    wire = decision_wire(wrong)
    assert "file_reviews" not in wire
    owner = read_decision(wire)
    assert owner.effect == "ask"
    assert owner.file_reviews == ()
    actual = ReviewedFile(path=tmp_path / "actual.py", before="before", after="after")
    bound = captured_edit_decision(
        wrong,
        str(actual.path),
        before_sha256=document_digest(actual.before),
        after_sha256=document_digest(actual.after),
        after=actual.after,
    )
    assert bound.effect == "ask"
    assert len(bound.file_reviews) == 1
    evidence = CapturedFileReview.model_validate(bound.file_reviews[0])
    assert evidence.path == actual.path
    assert ReviewFile.of(actual, [evidence]).review_effect == "ask"
    assert ReviewFile.of(fake, [evidence]).review_effect == "unknown"


def test_unrelated_removed_directive_does_not_reduce_new_rule_list(
    tmp_path: Path,
) -> None:
    before = "# lup: ignore[old] — removed\na = []\nseparator = 1\nother = 2\n"
    after = (
        "a = []\nseparator = 1\nother = 2\n# lup: ignore[old, new] — fresh\nb = []\n"
    )
    change = ReviewedFile(path=tmp_path / "app.py", before=before, after=after)
    shown = ReviewFile.of(change, [captured(change, "ask", "edit:anti-pattern")])
    assert shown.suppressions[0].review_rule_ids == ["old", "new"]


def shell_question(
    root: Path, command: str, rows: list[CapturedFileReview] | None, before: str
) -> PersistentQuestion:
    """A parked command over ``app.txt``, holding ``before`` as its preimage."""
    operation = Operation(
        id="op",
        session="s",
        requester="s",
        tool="Bash",
        payload={"command": command},
        cwd=root,
        worktree=root,
    )
    return PersistentQuestion(
        id="q",
        operation=operation,
        fingerprint="bound",
        reason="shell gate",
        preconditions={root / "app.txt": before},
        file_reviews=rows,
    )


def test_a_command_shows_the_document_its_verdict_judged(tmp_path: Path) -> None:
    """The after shown is the one recorded, whatever the command would say now."""
    change = ReviewedFile(path=tmp_path / "app.txt", before="old\n", after="judged\n")
    question = shell_question(
        tmp_path, "sed -i s/old/new/ app.txt", [captured(change, "allow")], "old\n"
    )
    detail = ReviewDetail.of(tmp_path, question, "operator")
    assert [(file.before, file.after) for file in detail.files] == [
        ("old\n", "judged\n")
    ]
    assert detail.files[0].review_effect == "allow"
    assert detail.summary.paths == []
    assert detail.summary.total_files == 1


def test_a_record_that_kept_only_a_digest_says_so(tmp_path: Path) -> None:
    change = ReviewedFile(path=tmp_path / "app.txt", before="old\n", after="new\n")
    digest_only = captured(change).model_copy(update={"after": None})
    question = shell_question(
        tmp_path, "sed -i s/old/new/ app.txt", [digest_only], "old\n"
    )
    detail = ReviewDetail.of(tmp_path, question, "operator")
    assert detail.files == []
    assert "kept only a digest" in detail.preview_unavailable


def test_reading_a_command_review_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is run where the review is read: the documents are the record's."""
    import subprocess

    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"ran {args!r} while reading a review")

    change = ReviewedFile(path=tmp_path / "app.txt", before="old\n", after="new\n")
    question = shell_question(
        tmp_path, "sed -i s/old/new/ app.txt", [captured(change)], "old\n"
    )
    monkeypatch.setattr(subprocess, "run", refused)
    monkeypatch.setattr(subprocess, "Popen", refused)
    detail = ReviewDetail.of(tmp_path, question, "operator")
    assert detail.files[0].after == "new\n"


def test_an_unbound_preimage_is_read_only_where_its_digest_still_holds(
    tmp_path: Path,
) -> None:
    """A record with no preimage shows the file as it stands only if it is the one judged."""
    target = tmp_path / "app.txt"
    target.write_text("old\n", encoding="utf-8")
    change = ReviewedFile(path=target, before="old\n", after="new\n")
    question = shell_question(
        tmp_path, "sed -i s/old/new/ app.txt", [captured(change)], "old\n"
    ).model_copy(update={"preconditions": {}})
    shown = ReviewDetail.of(tmp_path, question, "operator")
    assert [(file.before, file.after) for file in shown.files] == [("old\n", "new\n")]
    target.write_text("moved on\n", encoding="utf-8")
    moved = ReviewDetail.of(tmp_path, question, "operator")
    assert moved.files == []
    assert "changed since the command was judged" in moved.preview_unavailable
