"""Native execution never turns an ask or a single-use answer into a grant."""

import json
import os
import shlex
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import pytest
import sh

from lup.policy.assets.host import approval_fingerprint, approvals_log
from lup.policy.relay import Answer, QuestionRelay, ReceiptKind
from lup.policy.identity import DASHBOARD_URL_ENV, POLICY_ROOT_ENV
from lup.types import JsonObject
from tests.unit.native import codex_denial, codex_effect


def native_response(
    root: Path,
    runtime: str,
    event: str = "PreToolUse",
    *,
    tool: str = "Bash",
    arguments: JsonObject | None = None,
    execution_id: str = "",
) -> sh.RunningCommand:
    payload: JsonObject = {
        "session_id": "requester",
        "tool_use_id": execution_id,
        "cwd": str(root),
        "hook_event_name": event,
        "tool_name": tool,
        "tool_input": arguments
        if arguments is not None
        else {
            "command": "git push origin --delete probe-compound probe-excluded-prefix 2>&1 | tail -5"
        },
    }
    script = Path(f".{runtime}/plugins/lup/hooks/scripts/policy.py").resolve()
    result = sh.Command(str(script))(
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        _env={
            **os.environ,
            "PLUGIN_DATA": str(root / "plugin-data"),
            "CLAUDE_PLUGIN_DATA": str(root / "plugin-data"),
        },
    )
    assert isinstance(result, sh.RunningCommand)
    return result


def native_call(
    root: Path,
    runtime: str,
    event: str = "PreToolUse",
    *,
    tool: str = "Bash",
    arguments: JsonObject | None = None,
    execution_id: str = "",
) -> str:
    result = native_response(
        root, runtime, event, tool=tool, arguments=arguments, execution_id=execution_id
    )
    if event == "PostToolUse":
        return "observed"
    if runtime == "codex":
        return codex_effect(result)
    return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/feature\n")
    return tmp_path


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_unexpected_execution_never_authorizes_retry(root: Path, runtime: str) -> None:
    before = native_call(root, runtime)
    assert before in ("ask", "deny")
    native_call(root, runtime, "PostToolUse")
    (question,) = QuestionRelay(root / ".lup/questions.jsonl").questions()
    assert question.state == "in_doubt"
    assert "without a consumed approval receipt" in question.outcome
    assert native_call(root, runtime) == before


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("state", ["approved", "observed"])
def test_unproven_legacy_record_never_authorizes(
    root: Path, runtime: str, state: Literal["approved", "observed"]
) -> None:
    """The approvals log is an audit, so the call is asked as if it were empty.

    Both runtimes park the question and refuse the call while it waits for a
    recorded answer.
    """
    command = (
        "git push origin --delete probe-compound probe-excluded-prefix 2>&1 | tail -5"
    )
    path = approvals_log(root)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "fingerprint": approval_fingerprint("shell", command, root),
                "state": state,
                "kind": "shell",
                "subject": command,
                "cwd": str(root),
                "at": "2026-09-20T00:00:00+00:00",
            }
        )
        + "\n"
    )
    assert native_call(root, runtime) == "deny"
    assert len(QuestionRelay(root / ".lup/questions.jsonl").pending()) == 1


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_exact_answer_cannot_be_reused_after_execution(
    root: Path, runtime: str
) -> None:
    assert native_call(root, runtime) == "deny"
    relay = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = relay.pending()
    relay.answer(question.id, "operator", True)
    assert native_call(root, runtime) == "allow"
    native_call(root, runtime, "PostToolUse")
    completed = relay.find(question.id)
    assert completed is not None and completed.state == "completed"
    assert native_call(root, runtime) == "deny"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("change", ["none", "payload", "tool"])
def test_identified_execution_matches_the_approved_tool_and_input(
    root: Path, runtime: str, change: str
) -> None:
    arguments: JsonObject = {"command": "git push origin --delete reviewed-topic"}
    assert (
        native_call(root, runtime, arguments=arguments, execution_id="call-one")
        == "deny"
    )
    relay = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = relay.pending()
    relay.answer(question.id, "operator", True)
    assert (
        native_call(root, runtime, arguments=arguments, execution_id="call-one")
        == "allow"
    )
    dispatched = relay.find(question.id)
    assert dispatched is not None and dispatched.execution_payload is not None
    expected = dispatched.execution_payload
    tool = "Bash"
    executed: JsonObject
    match change:
        case "payload":
            executed = {
                **expected,
                "command": "git push origin --delete unreviewed-topic",
            }
        case "tool":
            tool = "WebFetch" if runtime == "claude" else "web_fetch"
            executed = {"url": "https://unreviewed.example.test/private"}
        case _:
            executed = expected
    observed = native_response(
        root,
        runtime,
        "PostToolUse",
        tool=tool,
        arguments=executed,
        execution_id="call-one",
    )
    recorded = relay.find(question.id)
    assert recorded is not None
    assert recorded.operation.payload == arguments
    assert recorded.execution_payload == expected
    if change == "none":
        assert recorded.state == "completed"
        assert observed.exit_code == 0
    else:
        assert recorded.state == "in_doubt"
        assert "tool or payload different from the reviewed call" in recorded.outcome
        if runtime == "codex":
            assert observed.exit_code == 2
            detail = observed.stderr.decode()
        else:
            result = json.loads(observed.stdout)
            assert result["decision"] == "block"
            detail = result["reason"]
        assert "tool or payload different from the reviewed call" in detail
        assert "grants no authority" in detail
    assert (
        native_call(root, runtime, arguments=arguments, execution_id="call-two")
        == "deny"
    )


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_simultaneous_native_retries_consume_only_one_answer(
    root: Path, runtime: str
) -> None:
    assert native_call(root, runtime) == "deny"
    relay = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = relay.pending()
    relay.answer(question.id, "operator", True)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: native_call(root, runtime), range(2)))
    assert sorted(results) == ["allow", "deny"]


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("receipt", ["observed", "inferred"])
def test_unrecorded_answer_does_not_release_a_native_retry(
    root: Path, runtime: str, receipt: ReceiptKind
) -> None:
    assert native_call(root, runtime) == "deny"
    relay = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = relay.pending()
    relay.answer(question.id, "operator", True, receipt=receipt)
    assert native_call(root, runtime) == "deny"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_external_workspace_preserves_application_human_owned_paths(
    root: Path, runtime: str
) -> None:
    """The gate holds from a workspace that is not the application's own.

    Each runtime meets it through the channel it has, so the assertion names
    both: a question the session renders, or the receipt that stands in for
    one. What neither may do is write the file.
    """
    path = Path("README.md").resolve()
    before = path.read_text()
    content = before + "\nReviewed addition.\n"
    arguments: JsonObject = (
        {"file_path": str(path), "content": content}
        if runtime == "claude"
        else {
            "command": "\n".join(
                [
                    "*** Begin Patch",
                    f"*** Add File: {path}",
                    *(f"+{line}" for line in content.splitlines()),
                    "*** End Patch",
                ]
            )
        }
    )
    tool = "Write" if runtime == "claude" else "apply_patch"
    answer = native_response(root, runtime, tool=tool, arguments=arguments)
    relay = QuestionRelay(root / ".lup/questions.jsonl")
    # The application is a registered destination, so its own policy judges the
    # write and names the gate it met; an unregistered repository meets the
    # foreign-repository ask, which test_destination_policy_routing pins.
    spoken = json.loads(answer.stdout)
    assert spoken["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert spoken["systemMessage"]
    (question,) = relay.pending()
    assert "human-authored" in question.reason
    assert question.preconditions == {path: before}
    assert path.read_text() == before


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_external_review_recovery_commands_select_the_application_environment(
    root: Path, runtime: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = root / "application with spaces"
    project.symlink_to(Path.cwd(), target_is_directory=True)
    monkeypatch.setenv(POLICY_ROOT_ENV, str(project))
    result = native_response(root, runtime)
    detail = (
        codex_denial(result)
        if runtime == "codex"
        else json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    )
    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()
    prefix = [
        "uv",
        "run",
        "--directory",
        str(root),
        "--project",
        str(project),
        "lup-devtools",
        "review",
    ]
    show = [*prefix, "show", question.id, "--json"]
    approve = [*prefix, "approve", question.id, "--as", "operator"]
    assert shlex.join([*prefix, "wait", question.id]) in detail
    assert shlex.join(approve) in detail
    assert (
        native_call(root, runtime, arguments={"command": shlex.join(approve)}) == "deny"
    )
    viewed = sh.Command(show[0])(*show[1:], _cwd=str(root))
    assert json.loads(str(viewed))["id"] == question.id
    sh.Command(approve[0])(*approve[1:], _cwd=str(root))
    assert native_call(root, runtime) == "allow"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("tail", [b'{"id":', b"\xff", b'[]\n{}\n{"id":'])
def test_damaged_review_log_retains_later_answers_and_observations(
    root: Path, runtime: str, tail: bytes
) -> None:
    assert native_call(root, runtime) == "deny"
    relay = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = relay.pending()
    with relay.path.open("ab") as stream:
        stream.write(tail)
    damaged = relay.path.read_bytes()
    assert native_call(root, runtime) == "deny"
    relay.answer(question.id, "operator", True)
    assert relay.path.read_bytes().startswith(damaged)
    assert native_call(root, runtime) == "allow"
    with relay.path.open("ab") as stream:
        stream.write(tail)
    native_call(root, runtime, "PostToolUse")
    completed = relay.find(question.id)
    assert completed is not None and completed.state == "completed"
    assert native_call(root, runtime) == "deny"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_unterminated_approval_never_becomes_authority_on_later_append(
    root: Path, runtime: str
) -> None:
    assert native_call(root, runtime) == "deny"
    relay = QuestionRelay(root / ".lup/questions.jsonl")
    (question,) = relay.pending()
    interrupted = question.model_copy(
        update={
            "state": "approved",
            "answer": Answer(approved=True, principal="operator"),
        }
    )
    with relay.path.open("ab") as stream:
        stream.write(interrupted.model_dump_json().encode())
    damaged = relay.path.read_bytes()
    assert native_call(root, runtime) == "deny"
    assert relay.pending() == [question]
    other: JsonObject = {"command": "git push origin --delete a-different-ref"}
    assert native_call(root, runtime, arguments=other) == "deny"
    assert relay.path.read_bytes().startswith(damaged)
    assert relay.find(question.id) == question
    assert native_call(root, runtime) == "deny"
    relay.answer(question.id, "operator", True)
    assert native_call(root, runtime) == "allow"


def test_a_protected_path_edit_is_parked_for_its_author(root: Path) -> None:
    """A protected path's edit waits for a recorded answer, never a prompt.

    Its question is a person's to answer, so it is parked with the document
    as it stands, and the file is untouched until somebody does.
    """
    target = Path("src/lup_template/harness/catalog.py").resolve()
    arguments: JsonObject = {
        "file_path": str(target),
        "old_string": "excluded_commands=served_exclusions(composed),",
        "new_string": "excluded_commands=served_exclusions(composed),  # reviewed",
    }
    assert native_call(root, "claude", tool="Edit", arguments=arguments) == "deny"
    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()
    assert question.requirement == "human_only"
    assert question.preconditions == {target: target.read_text()}
    assert "# reviewed" not in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["default", "auto", "bypassPermissions"])
def test_a_person_s_question_is_parked_whatever_the_mode(
    root: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `human_only` ask never reaches a prompt a mode could answer.

    Removing a remote ref is what the reviewer axis reserves for a person, and
    an auto-mode classifier answered exactly this prompt on Claude Code 2.1.263
    with nobody shown it. So it is parked and refused while it waits, whatever
    mode the session is in and whether or not a dashboard serves, and the
    refusal tells the operator where it waits: a mode can answer a prompt, and
    nothing answers a refusal but the recorded answer.
    """
    monkeypatch.delenv(DASHBOARD_URL_ENV, raising=False)
    arguments: JsonObject = {"command": "git push --delete origin topic"}
    payload: JsonObject = {
        "session_id": "requester",
        "cwd": str(root),
        "hook_event_name": "PreToolUse",
        "permission_mode": mode,
        "tool_name": "Bash",
        "tool_input": arguments,
    }
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()
    answer = json.loads(
        str(
            sh.Command(str(script))(
                _in=json.dumps(payload),
                _env={**os.environ, "CLAUDE_PLUGIN_DATA": str(root / "plugin-data")},
            )
        )
    )
    spoken = answer["hookSpecificOutput"]
    (question,) = QuestionRelay(root / ".lup/questions.jsonl").pending()
    assert spoken["permissionDecision"] == "deny"
    assert "remote branch" in spoken["permissionDecisionReason"]
    assert f"review {question.id}" in spoken["permissionDecisionReason"]
    assert "not refused" in spoken["permissionDecisionReason"]
    assert question.id in answer["systemMessage"]
    assert question.requirement == "human_only"
