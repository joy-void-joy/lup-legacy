"""Review focus comes from exact original verdicts, never path conventions."""

import json
from pathlib import Path

import pytest

from lup.policy.assets.host import document_digest, sed_output

from lup.devtools.dev.questions import ReviewDetail, ReviewFile
from lup.policy.assets.host import review_hook_call
from lup.policy.kernel.decision import (
    DecisionEffect,
    KernelDecision,
    captured_edit_decision,
)
from lup.policy.operations import Operation
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
    assert ReviewFile.of(change).review_effect == "unknown"


@pytest.mark.parametrize("changed", ["before", "after", "path"])
def test_only_both_exact_images_and_path_can_hide_a_file(
    tmp_path: Path, changed: str
) -> None:
    original = ReviewedFile(path=tmp_path / "app.py", before="old\n", after="new\n")
    revised = original.model_copy(
        update={
            changed: tmp_path / "elsewhere.py"
            if changed == "path"
            else "racing content\n"
        }
    )
    shown = ReviewFile.of(revised, [captured(original, "allow")])
    assert shown.review_effect == "unknown"


def test_duplicate_or_deferred_capture_is_unknown(tmp_path: Path) -> None:
    change = ReviewedFile(path=tmp_path / "app.py", before="old\n", after="new\n")
    assert ReviewFile.of(change, [captured(change, "defer")]).review_effect == "unknown"
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
    assert detail.question is question
    assert detail.question.operation.payload == {"command": patch}
    assert detail.summary.reason == "original batch reason"
    assert detail.question.fingerprint == "unchanged-binding"


def test_shell_only_question_keeps_command_gate_when_all_files_allow(
    tmp_path: Path,
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
        file_reviews=[captured(change, "allow")],
    )
    detail = ReviewDetail.of(tmp_path, question, "operator")
    assert detail.command == command
    assert detail.summary.reason == "shell-level gate"
    assert detail.files[0].review_effect == "allow"
    assert detail.question.fingerprint == "bound"


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
    first = review_hook_call(*arguments, file_reviews=original)
    relay = QuestionRelay(tmp_path / ".lup/questions.jsonl")
    relay.answer(first["id"], "operator", True)
    changed = review_hook_call(
        *arguments,
        file_reviews=json.dumps([captured(change, "allow").model_dump(mode="json")]),
    )
    assert changed["id"] != first["id"]
    assert changed["state"] == "pending"
    assert review_hook_call(*arguments, file_reviews=original)["state"] == "approved"
    stored = relay.find(first["id"])
    assert stored is not None
    assert stored.file_reviews == [captured(change)]


def test_kernel_revision_preserves_original_capture(tmp_path: Path) -> None:
    change = ReviewedFile(path=tmp_path / "app.py", before=None, after="a = 1\n")
    decision = captured_edit_decision(
        KernelDecision("ask", rule="edit:protected-path"),
        str(change.path),
        before_sha256=document_digest(change.before),
        after_sha256=document_digest(change.after),
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
    (question,) = QuestionRelay(tmp_path / ".lup/questions.jsonl").pending()
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


def test_shell_summary_keeps_unknown_after_image_visible(tmp_path: Path) -> None:
    change = ReviewedFile(
        path=tmp_path / "app.txt", before="old\n", after="different\n"
    )
    operation = Operation(
        id="op",
        session="s",
        requester="s",
        tool="Bash",
        payload={"command": "sed -i s/old/new/ app.txt"},
        cwd=tmp_path,
        worktree=tmp_path,
    )
    question = PersistentQuestion(
        id="q",
        operation=operation,
        fingerprint="bound",
        reason="shell gate",
        preconditions={change.path: change.before},
        file_reviews=[captured(change, "allow")],
    )
    detail = ReviewDetail.of(tmp_path, question, "operator")
    assert detail.files[0].review_effect == "unknown"
    assert detail.summary.paths == ["app.txt"]
    assert detail.summary.total_files == 1
    historical = question.model_copy(update={"file_reviews": None})
    assert ReviewDetail.of(tmp_path, historical, "operator").summary.paths == [
        "app.txt"
    ]


def test_preview_cache_binds_complete_inputs_but_not_answer_state_or_live_staleness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lup.policy import review

    review.captured_sed_output.cache_clear()
    original = sed_output
    calls = []

    def counted(*args, **kwargs):
        calls.append(kwargs["before"])
        return original(*args, **kwargs)

    monkeypatch.setattr(review, "sed_output", counted)
    path = tmp_path / "app.txt"
    path.write_text("old\n")
    operation = Operation(
        id="op",
        session="s",
        requester="s",
        tool="Bash",
        payload={"command": "sed -i s/old/new/ app.txt"},
        cwd=tmp_path,
        worktree=tmp_path,
    )
    entry = PersistentQuestion(
        id="q",
        operation=operation,
        fingerprint="same",
        reason="shell gate",
        preconditions={path: "old\n"},
    )
    first = ReviewDetail.of(tmp_path, entry, "operator")
    assert calls == ["old\n"]
    assert first.stale_reason == ""
    path.write_text("changed\n")
    answered = entry.model_copy(update={"state": "approved"})
    second = ReviewDetail.of(tmp_path, answered, "operator")
    assert calls == ["old\n"]
    assert second.summary.state == "approved"
    assert second.stale_reason
    changed_input = entry.model_copy(update={"preconditions": {path: "old old\n"}})
    third = ReviewDetail.of(tmp_path, changed_input, "operator")
    assert calls == ["old\n", "old old\n"]
    assert third.files[0].after == "new old\n"


def test_cached_transformation_does_not_cache_live_symlink_binding(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("old\n")
    second.write_text("old\n")
    link = tmp_path / "target.txt"
    link.symlink_to(first)
    operation = Operation(
        id="op",
        session="s",
        requester="s",
        tool="Bash",
        payload={"command": "sed -i s/old/new/ target.txt"},
        cwd=tmp_path,
        worktree=tmp_path,
    )
    entry = PersistentQuestion(
        id="q",
        operation=operation,
        fingerprint="same",
        reason="shell gate",
        preconditions={first: "old\n"},
    )
    shown = ReviewDetail.of(tmp_path, entry, "operator")
    assert shown.files[0].path == str(first)
    link.unlink()
    link.symlink_to(second)
    changed = ReviewDetail.of(tmp_path, entry, "operator")
    assert changed.files == []
    assert "preimage" in changed.preview_unavailable
