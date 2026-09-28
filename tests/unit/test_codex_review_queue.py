"""Real hook order and explicit review of complete document replacements."""

import io
import json
import os
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import sh

from lup.policy.relay import QuestionRelay
from lup.policy.assets.host import review_hook_call
from lup.types import JsonObject
from tests.unit.bundled import bundled
from tests.unit.native import codex_denial, codex_effect


def hook(
    root: Path,
    command: str,
    *,
    tool: str = "apply_patch",
    event: str = "PreToolUse",
    session: str = "requester",
    call_id: str = "call-one",
) -> sh.RunningCommand:
    payload: JsonObject = {
        "session_id": session,
        "turn_id": "turn-one",
        "cwd": str(root),
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": {"command": command},
    }
    if call_id:
        payload["tool_use_id"] = call_id
    script = Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()
    result = sh.Command(str(script))(
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={**os.environ, "PLUGIN_DATA": str(root / "plugin-data")},
    )
    assert isinstance(result, sh.RunningCommand)
    return result


def allowed(result: sh.RunningCommand) -> bool:
    return codex_effect(result) == "allow"


def denial(result: sh.RunningCommand) -> str:
    return codex_denial(result)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    (tmp_path / "DESIGN.md").write_text("# Previous design\n")
    return tmp_path


def replacement() -> str:
    return "*** Begin Patch\n*** Add File: DESIGN.md\n+# Agreed design\n+\n+All decisions.\n*** End Patch"


@pytest.mark.parametrize("shell", [False, True])
def test_document_replacement_waits_for_review_then_runs_once(
    root: Path, shell: bool
) -> None:
    command = replacement()
    if shell:
        command = f"# lup: escalate[decision]: replace the agreed design\napply_patch <<'PATCH'\n{command}\nPATCH"
    tool = "Bash" if shell else "apply_patch"
    stopped = hook(root, command, tool=tool)
    detail = denial(stopped)
    assert "not classified" not in detail
    store = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = store.pending()
    assert question.operation.requester == "requester"
    assert question.preconditions == {root / "DESIGN.md": "# Previous design\n"}
    assert question.id in detail
    assert denial(hook(root, command, tool=tool))
    assert len(store.pending()) == 1
    store.answer(question.id, "operator", True)
    assert hook(root, command, tool=tool).exit_code == 0
    dispatched = store.find(question.id)
    assert dispatched is not None and dispatched.state == "dispatched"
    assert denial(hook(root, command, tool=tool))


def test_pending_native_prompt_is_never_an_approval(root: Path) -> None:
    response = hook(root, replacement(), event="PermissionRequest")
    assert response.exit_code == 0
    decision = json.loads(response.stdout)["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny"
    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()
    assert question.id in decision["message"]
    assert question.id in denial(hook(root, replacement()))


@pytest.mark.parametrize("approved", [False, True])
def test_real_sandbox_escalation_requires_explicit_permission_review(
    root: Path, approved: bool
) -> None:
    command = "# lup: escalate[sandbox]: inspect the host\nls"
    store = QuestionRelay(root / ".lup/questions.jsonl")
    assert denial(hook(root, command, tool="Bash"))
    pending = hook(root, command, tool="Bash", event="PermissionRequest")
    decision = json.loads(pending.stdout)["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny"
    (question,) = store.pending()
    assert question.id in decision["message"]
    store.answer(question.id, "operator", approved)
    retry = hook(root, command, tool="Bash")
    assert allowed(retry) if approved else denial(retry)
    retried = hook(root, command, tool="Bash", event="PermissionRequest")
    assert json.loads(retried.stdout)["hookSpecificOutput"]["decision"]["behavior"] == (
        "allow" if approved else "deny"
    )
    replay = hook(root, command, tool="Bash", event="PermissionRequest")
    assert (
        json.loads(replay.stdout)["hookSpecificOutput"]["decision"]["behavior"]
        == "deny"
    )


@pytest.mark.parametrize("approved", [False, True])
def test_host_executor_deferral_reaches_explicit_permission_review(
    root: Path,
    approved: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dispatcher = bundled(
        "codex_permission_policy", Path(".codex/plugins/lup/hooks/scripts/policy.py")
    )
    monkeypatch.setenv("PLUGIN_DATA", str(root / "plugin-data"))
    monkeypatch.setattr(
        dispatcher,
        "bash_decision",
        lambda *_args, **_kwargs: dispatcher.KernelDecision(
            "ask", "Host execution needs review", capability="host_executor"
        ),
    )

    def invoke(event: str) -> str:
        payload = {
            "session_id": "requester",
            "tool_use_id": "host-call",
            "cwd": str(root),
            "hook_event_name": event,
            "tool_name": "Bash",
            "tool_input": {"command": "declared-host-operation"},
        }
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        dispatcher.main()
        return capsys.readouterr().out

    store = QuestionRelay(root / ".lup/questions.jsonl")
    assert invoke("PreToolUse") == ""
    assert store.questions() == []
    pending = json.loads(invoke("PermissionRequest"))["hookSpecificOutput"]
    assert pending["hookEventName"] == "PermissionRequest"
    assert pending["decision"]["behavior"] == "deny"
    (question,) = store.pending()
    assert question.id in pending["decision"]["message"]
    store.answer(question.id, "operator", approved)
    assert invoke("PreToolUse") == ""
    retried = json.loads(invoke("PermissionRequest"))
    assert retried["hookSpecificOutput"]["decision"]["behavior"] == (
        "allow" if approved else "deny"
    )
    replay = json.loads(invoke("PermissionRequest"))
    assert replay["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_one_review_covers_both_events_for_one_identified_invocation(
    root: Path,
) -> None:
    assert denial(hook(root, replacement()))
    store = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = store.pending()
    store.answer(question.id, "operator", True)
    assert allowed(hook(root, replacement()))
    response = hook(root, replacement(), event="PermissionRequest")
    assert response.exit_code == 0
    assert json.loads(response.stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    assert len(store.questions()) == 1
    replay = hook(root, replacement(), event="PermissionRequest")
    assert (
        json.loads(replay.stdout)["hookSpecificOutput"]["decision"]["behavior"]
        == "deny"
    )
    assert denial(hook(root, replacement()))


@pytest.mark.parametrize("preapproved", [False, True])
def test_concurrent_permission_events_cannot_spend_an_answer_twice(
    root: Path, preapproved: bool
) -> None:
    hook(root, replacement())
    store = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = store.pending()
    store.answer(question.id, "operator", True)
    if preapproved:
        assert allowed(hook(root, replacement()))
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(
            workers.map(
                lambda _: hook(root, replacement(), event="PermissionRequest"),
                range(2),
            )
        )
    assert sorted(
        json.loads(response.stdout)["hookSpecificOutput"]["decision"]["behavior"]
        for response in responses
    ) == ["allow", "deny"]


@pytest.mark.parametrize(
    "change", ["missing-id", "id", "preimage", "payload", "session"]
)
def test_event_handoff_requires_the_same_identified_exact_operation(
    root: Path, change: str
) -> None:
    hook(root, replacement())
    store = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = store.pending()
    store.answer(question.id, "operator", True)
    assert allowed(hook(root, replacement()))
    command = replacement()
    session = "requester"
    call_id = "call-one"
    if change == "missing-id":
        call_id = ""
    if change == "id":
        call_id = "call-two"
    if change == "preimage":
        (root / "DESIGN.md").write_text("# Another writer's design\n")
    if change == "payload":
        command = command.replace("All decisions.", "Different decisions.")
    if change == "session":
        session = "another-requester"
    response = hook(
        root, command, event="PermissionRequest", session=session, call_id=call_id
    )
    assert (
        json.loads(response.stdout)["hookSpecificOutput"]["decision"]["behavior"]
        == "deny"
    )


@pytest.mark.parametrize("damage", ["missing", "incomplete"])
def test_event_handoff_never_infers_a_missing_or_corrupt_primary_claim(
    root: Path, damage: str
) -> None:
    hook(root, replacement())
    store = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = store.pending()
    store.answer(question.id, "operator", True)
    assert allowed(hook(root, replacement()))
    claim = root / ".lup/review-claims" / question.id
    if damage == "missing":
        claim.unlink()
    else:
        claim.write_text('{"fingerprint":')
    response = hook(root, replacement(), event="PermissionRequest")
    decision = json.loads(response.stdout)["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny"
    assert "Malformed hook input" in decision["message"]


def test_rejection_does_not_create_another_question(root: Path) -> None:
    hook(root, replacement())
    store = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = store.pending()
    store.answer(question.id, "operator", False, "Keep the original design")
    refused = hook(root, replacement())
    assert "rejected" in denial(refused)
    assert "Keep the original design" in denial(refused)
    assert len(store.questions()) == 1


@pytest.mark.parametrize("change", ["preimage", "payload", "session"])
def test_approval_does_not_follow_a_changed_operation(root: Path, change: str) -> None:
    hook(root, replacement())
    store = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = store.pending()
    store.answer(question.id, "operator", True)
    command = replacement()
    session = "requester"
    if change == "preimage":
        (root / "DESIGN.md").write_text("# Another writer's design\n")
    if change == "payload":
        command = command.replace("All decisions.", "Different decisions.")
    if change == "session":
        session = "another-requester"
    assert denial(hook(root, command, session=session))
    approved = store.find(question.id)
    assert approved is not None and approved.state == "approved"


@pytest.mark.parametrize(
    "runner",
    [
        ["uv", "run"],
        ["uv", "--directory", "/example", "run"],
        ["uv", "--directory=/example", "run"],
        ["uv", "--quiet", "--color", "never", "--directory", "/example", "run"],
    ],
)
def test_requester_cannot_answer_its_own_question(
    root: Path, runner: list[str]
) -> None:
    hook(root, replacement())
    store = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = store.pending()
    with pytest.raises(ValueError, match="may not answer"):
        store.answer(question.id, "requester", True)
    for prefix in ("", "# lup: escalate[decision]: user agreed\n"):
        refused = hook(
            root,
            prefix
            + shlex.join(
                [
                    *runner,
                    "lup-devtools",
                    "dev",
                    "questions",
                    "answer",
                    question.id,
                    "--as",
                    "operator",
                ]
            ),
            tool="Bash",
        )
        assert refused.exit_code == 2
        assert b"cannot approve" in refused.stderr
        located = shlex.join(
            [
                "uv",
                "run",
                "--directory",
                str(root),
                "lup-devtools",
                "dev",
                "questions",
                "answer",
                question.id,
                "--as",
                "operator",
            ]
        )
        refused = hook(root, prefix + located, tool="Bash")
        assert refused.exit_code == 2
        assert b"cannot approve" in refused.stderr


def test_malformed_patch_and_marker_deletion_are_not_queued(root: Path) -> None:
    assert hook(root, "not a patch").exit_code == 2
    (root / "DESIGN.md").write_text("# lup: preserve this decision\n")
    assert hook(root, replacement()).exit_code == 2
    assert not QuestionRelay(root / ".lup/questions.jsonl").pending()


def test_relative_patch_uses_payload_cwd_not_hook_process_cwd(root: Path) -> None:
    command = "*** Begin Patch\n*** Update File: DESIGN.md\n@@\n-# Previous design\n+# Revised design\n*** End Patch"
    assert hook(root, command).exit_code == 0


@pytest.mark.parametrize("suffix", ["\necho extra", "\nrm -rf /", " &"])
def test_shell_patch_recognition_never_hides_other_commands(
    root: Path, suffix: str
) -> None:
    command = f"apply_patch <<'PATCH'\n{replacement()}\nPATCH{suffix}"
    assert denial(hook(root, command, tool="Bash"))


def test_unquoted_shell_patch_is_not_interpreted_as_literal(root: Path) -> None:
    command = f"apply_patch <<PATCH\n{replacement()}\nPATCH"
    assert denial(hook(root, command, tool="Bash"))


@pytest.mark.parametrize("changed", ["source", "destination"])
def test_copy_approval_binds_both_documents(root: Path, changed: str) -> None:
    source = root / "proposal.md"
    source.write_text("# Agreed design\n")
    command = "# lup: escalate[decision]: install the reviewed design\ncp proposal.md DESIGN.md"
    assert denial(hook(root, command, tool="Bash"))
    store = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = store.pending()
    assert question.preconditions == {
        source: "# Agreed design\n",
        root / "DESIGN.md": "# Previous design\n",
    }
    store.answer(question.id, "operator", True)
    target = source if changed == "source" else root / "DESIGN.md"
    target.write_text("# Another document\n")
    assert denial(hook(root, command, tool="Bash"))
    assert len(store.pending()) == 1
    approved = store.find(question.id)
    assert approved is not None and approved.state == "approved"


@pytest.mark.parametrize(
    "command",
    [
        "cat > DESIGN.md <<'DOC'\n# Replacement\nDOC",
        "echo replacement > DESIGN.md",
        "sed -i 's/old/new/' DESIGN.md",
    ],
)
def test_shell_write_approval_binds_existing_document(root: Path, command: str) -> None:
    target = root / "DESIGN.md"
    before = "# lup: old\n" if command.startswith("sed ") else "# Previous design\n"
    target.write_text(before)
    command = f"# lup: escalate[decision]: review this document change\n{command}"
    assert denial(hook(root, command, tool="Bash"))
    store = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = store.pending()
    assert question.preconditions == {target: before}
    store.answer(question.id, "operator", True)
    target.write_text(before + "# Another writer\n")
    assert denial(hook(root, command, tool="Bash"))
    assert len(store.pending()) == 1
    approved = store.find(question.id)
    assert approved is not None and approved.state == "approved"


def test_policy_identity_changes_require_another_review(root: Path) -> None:
    arguments = (
        root,
        "requester",
        "apply_patch",
        "{}",
        "{}",
        "review",
        "edit",
        "",
        "human_only",
    )
    first = review_hook_call(*arguments, policy_identity="original-policy")
    store = QuestionRelay(root / ".lup/questions.jsonl")
    store.answer(first["id"], "operator", True)
    changed = review_hook_call(*arguments, policy_identity="replacement-policy")
    assert changed["state"] == "pending"
    assert changed["id"] != first["id"]
    assert (
        review_hook_call(*arguments, policy_identity="original-policy")["state"]
        == "approved"
    )


def test_review_notice_quotes_the_checkout(root: Path) -> None:
    nested = root / "checkout with 'quotes' and $substitution"
    nested.mkdir()
    (nested / "DESIGN.md").write_text("# Previous design\n")
    notice = denial(hook(nested, replacement()))
    assert "uv run --directory '" in notice
    assert "questions show" in notice
    assert "questions answer" in notice
    assert "questions reject" in notice
