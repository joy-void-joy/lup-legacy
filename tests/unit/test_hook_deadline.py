"""A policy hook answers before its runtime stops listening, whatever it waits on.

Both runtimes let a call through once its policy hook overruns its timeout, so
a hook still waiting then has answered nothing. Each generated dispatcher is
driven here the way its runtime drives it, under a deadline the test sets a
few seconds out -- a deadline a parent set is never extended, and a start the
guard stamped is counted from -- so a wait that would outlast the runtime is
seen to end in a verdict instead.
"""

import ast
import fcntl
import inspect
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest
import sh
from claude_agent_sdk import types as claude_types

import lup.policy.assets.host as policy_host
from lup.policy.bundle import hook_answer_limit, hook_deadline
from lup.policy.contracts import DecisionPolicy
from lup.policy.enforcement import create_policy_hooks
from lup.policy.hooks import LupHookInput
from lup.policy.models import Decision, SemanticTool
from lup.providers.claude.harness import ClaudeHookRenderer, ClaudeSpellings
from lup.providers.claude.hooks import CLAUDE_SEMANTICS, lup_hooks_to_claude
from lup.providers.codex.harness import CodexHookRenderer, CodexSpellings
from lup.providers.codex.hooks import (
    CODEX_SEMANTICS,
    COMMAND_APPROVAL,
    CodexApprovalResponder,
)
from lup.types import JsonObject
from lup_template.harness.catalog import declared_hook_set
from tests.unit.repos import commit_file, git_in, initialized_repo

INHERITED = 3.0
"""Seconds from now the test's own deadline stands, which the hook inherits."""

RUNTIME_LIMIT = declared_hook_set().policy_timeout
"""Where each runtime stops listening and lets the call through."""

ANSWER_LIMIT = hook_answer_limit(RUNTIME_LIMIT)
"""Where the hook answers whatever it has, counted from when it started."""

UNREACHABLE_ALARM = (
    "\nimport signal as _blocked\n"
    "_blocked.pthread_sigmask(_blocked.SIG_BLOCK, {_blocked.SIGALRM})\n"
)
"""Loaded into a copied runtime, so no alarm the hook sets can be delivered.

Stands in for a wait no signal interrupts -- a read the kernel holds, native
code that never returns to the interpreter -- which only an answer from
outside the judgement can end."""


@pytest.fixture(params=["claude", "codex"])
def runtime(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def checkout(tmp_path: Path, runtime: str) -> Path:
    """A repository carrying this runtime's generated hooks, and nothing launched."""
    root = tmp_path / "checkout"
    initialized_repo(root, tmp_path / "no-hooks")
    shutil.copytree(
        Path(f".{runtime}/plugins/lup/hooks"), root / f".{runtime}/plugins/lup/hooks"
    )
    return root


def judged(
    checkout: Path,
    runtime: str,
    tool_name: str,
    tool_input: JsonObject,
    environment: dict[str, str] | None = None,
) -> tuple[str, str, float]:
    """The effect one PreToolUse call meets, what it said, and how long it took.

    ``environment`` is laid over the hook's, which otherwise inherits a
    deadline ``INHERITED`` seconds out.
    """
    payload: JsonObject = {
        "session_id": "deadline-probe",
        "hook_event_name": "PreToolUse",
        "cwd": str(checkout),
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
    held = {
        name: value
        for name, value in os.environ.items()
        if name
        not in (
            "LUP_BOUNDARY_ROOT",
            "LUP_BOUNDARY_NONCE",
            "LUP_SANDBOX_ACTIVE",
            "LUP_HOOK_DEADLINE",
            "LUP_HOOK_STARTED",
            "LUP_DASHBOARD_URL",
        )
    }
    started = time.monotonic()
    result = sh.Command(sys.executable)(
        "-I",
        "-S",
        str(checkout / f".{runtime}/plugins/lup/hooks/scripts/policy.py"),
        _in=json.dumps(payload),
        _ok_code=[0, 2],
        _return_cmd=True,
        # Past the runtime's own limit a hook has already failed open, so a
        # hook still running then fails the test instead of hanging it.
        _timeout=RUNTIME_LIMIT,
        _env={
            **held,
            "PLUGIN_DATA": str(checkout.parent / "plugin-data"),
            **(
                environment
                if environment is not None
                else {"LUP_HOOK_DEADLINE": repr(time.monotonic() + INHERITED)}
            ),
        },
    )
    elapsed = time.monotonic() - started
    assert isinstance(result, sh.RunningCommand)
    if result.exit_code == 2:
        return "deny", result.stderr.decode(), elapsed
    if not str(result):
        return "allow", "", elapsed
    answer = json.loads(str(result))
    specific = answer["hookSpecificOutput"] if "hookSpecificOutput" in answer else {}
    effect = (
        specific["permissionDecision"] if "permissionDecision" in specific else "allow"
    )
    return str(effect), json.dumps(answer), elapsed


def started_ago(seconds: float) -> dict[str, str]:
    """The stamp the guard leaves, as though it had started the hook ``seconds`` ago."""
    return {"LUP_HOOK_STARTED": str(int(time.time() - seconds))}


def test_a_language_server_that_never_answers_leaves_the_hook_refusing_in_time(
    checkout: Path, runtime: str
) -> None:
    """The resolver would hold the hook past the runtime's limit; the hook refuses in time.

    A rule whose verdict turns on a resolved receiver waits on the declared
    resolver, which here sleeps a minute. Cut short at the inherited deadline,
    it reads as no checker having looked -- and the questions the verdict still
    has to put to Git find no time left, which ends the judgement rather than
    being read as their answers' "no": the call is refused unjudged.
    """
    resolver = checkout / "stalled-resolver"
    resolver.write_text("#!/bin/sh\nexec sleep 60\n", encoding="utf-8")
    resolver.chmod(0o755)
    data = checkout / f".{runtime}/plugins/lup/hooks/runtime/policy_data.py"
    data.write_text(
        data.read_text(encoding="utf-8")
        + f"\nRESOLUTION_COMMAND = [{str(resolver)!r}]\n",
        encoding="utf-8",
    )
    target = checkout / "engine.py"
    target.write_text('def read(client):\n    return client.get("old")\n')
    old, new = '    return client.get("old")', '    return client.get("new")'
    edit: JsonObject = {"file_path": str(target), "old_string": old, "new_string": new}
    patch: JsonObject = {
        "command": f"*** Begin Patch\n*** Update File: {target}\n@@\n"
        f"-{old}\n+{new}\n*** End Patch"
    }

    effect, detail, elapsed = (
        judged(checkout, runtime, "Edit", edit)
        if runtime == "claude"
        else judged(checkout, runtime, "apply_patch", patch)
    )

    assert elapsed < INHERITED + 5
    assert effect == "deny"
    assert "could not judge this call in time" in detail


@pytest.mark.parametrize("runtime", ["codex"])
def test_a_wait_no_step_can_bound_is_refused_at_the_deadline(
    checkout: Path, runtime: str
) -> None:
    """A review queue another writer holds locked stops the hook at its alarm, refused.

    Codex parks every question in the queue, and reading it waits on the lock
    its writers share -- a wait no timeout can be handed to. Past the deadline
    the alarm raises where the hook is waiting, and the call is refused as one
    the hook could not judge, instead of outlasting the runtime into an allow.
    """
    relay = checkout / ".lup" / "questions.jsonl"
    relay.parent.mkdir(parents=True)
    relay.write_text("", encoding="utf-8")

    with relay.open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        effect, detail, elapsed = judged(
            checkout, runtime, "Bash", {"command": "sudo id"}
        )

    assert elapsed < INHERITED + 5
    assert effect == "deny"
    assert "could not judge this call in time" in detail
    assert "report-friction" in detail
    assert "Malformed hook input" not in detail


def test_a_judgement_no_alarm_reaches_is_refused_before_the_runtime_limit(
    checkout: Path, runtime: str
) -> None:
    """A judgement stuck where the alarm cannot reach it is refused, in time, from outside.

    The runtime the hook loads blocks the alarm, and the review queue the call
    parks in is held locked, so the judgement waits on a lock nothing inside it
    can interrupt. The hook stamped as started most of its answer limit ago
    has seconds left, and refuses within them instead of answering nothing.
    """
    data = checkout / f".{runtime}/plugins/lup/hooks/runtime/policy_data.py"
    data.write_text(
        data.read_text(encoding="utf-8") + UNREACHABLE_ALARM, encoding="utf-8"
    )
    relay = checkout / ".lup" / "questions.jsonl"
    relay.parent.mkdir(parents=True)
    relay.write_text("", encoding="utf-8")

    with relay.open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        effect, detail, elapsed = judged(
            checkout,
            runtime,
            "Bash",
            {"command": "sudo id"},
            {
                **started_ago(ANSWER_LIMIT - 3),
                # A Claude session parks its questions where a dashboard is held.
                "LUP_DASHBOARD_URL": "http://127.0.0.1:9",
            },
        )

    assert elapsed < 5
    assert effect == "deny"
    assert "could not judge this call in time" in detail
    assert "retry the same call once" in detail


def test_a_hook_started_past_its_answer_limit_refuses_at_once(
    checkout: Path, runtime: str
) -> None:
    """The deadline counts from the guard's stamp, not from the dispatcher's first line.

    A hook the runtime started longer ago than its answer limit -- an
    interpreter slow to start, a kernel slow to import -- has no time left to
    judge in, and says so rather than spending time it does not have.
    """
    effect, detail, elapsed = judged(
        checkout,
        runtime,
        "Bash",
        {"command": "git status"},
        started_ago(ANSWER_LIMIT + 1),
    )

    assert elapsed < 5
    assert effect == "deny"
    assert "could not judge this call in time" in detail


TRACKED_CHANGE = (
    "--- a/tracked.py\n+++ b/tracked.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
)
"""A patch over the tracked file, whose targets only Git reads."""

STARVED_CALLS: dict[str, JsonObject] = {
    "a redirect over a tracked file": {"command": "date > tracked.py"},
    "a heredoc into scratch, then a redirect over a tracked file": {
        "command": "cat > tmp/x.py <<'X'\ntext.replace('a', 'b')\nX\ndate > tracked.py"
    },
    "a patch whose targets Git reads": {"command": "git apply change.patch"},
    "a write into another repository": {"command": "date > nested/inner.py"},
}
"""Calls whose verdict turns on a question only Git answers: tracked, touched, whose."""


@pytest.mark.parametrize("call", sorted(STARVED_CALLS))
def test_a_question_the_deadline_starved_is_never_read_as_no(
    checkout: Path, runtime: str, call: str
) -> None:
    """With no time left, a question Git did not answer is unknown, not "no".

    Read as "no" -- not tracked, touches nothing, no repository -- each let
    its call through with the deadline starved, measured on both dispatchers,
    and over a tracked file that is a write the answer asks about in time. A
    starved question ends the judgement instead, so each call is refused
    unjudged rather than judged on an answer nobody gave.
    """
    hooks = checkout.parent / "no-hooks"
    commit_file(git_in(checkout, hooks), checkout, "tracked.py", "value = 1\n", "seed")
    (checkout / "tmp").mkdir()
    (checkout / "change.patch").write_text(TRACKED_CHANGE, encoding="utf-8")
    initialized_repo(checkout / "nested", hooks)

    effect, detail, _ = judged(
        checkout,
        runtime,
        "Bash",
        STARVED_CALLS[call],
        {"LUP_HOOK_DEADLINE": repr(time.monotonic() - 1.0)},
    )

    assert effect == "deny"
    assert "could not judge this call in time" in detail


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_one_declared_timeout_sets_the_hook_timeout_and_every_deadline(
    runtime: str,
) -> None:
    """The timeout a runtime is told and each deadline inside the hook move together.

    Rendered from a declaration whose ``policy_timeout`` is not the default,
    so a value restated anywhere instead of derived would be left behind.
    """
    timeout = RUNTIME_LIMIT + 17
    declared = declared_hook_set().model_copy(update={"policy_timeout": timeout})
    renderer = (
        ClaudeHookRenderer("lup", "", ClaudeSpellings())
        if runtime == "claude"
        else CodexHookRenderer("lup", "", CodexSpellings())
    )
    artifacts = {
        artifact.path.as_posix(): artifact.content
        for artifact in renderer.render(declared).artifacts
    }
    hooks = json.loads(artifacts[f".{runtime}/plugins/lup/hooks/hooks.json"])
    data = ast.parse(artifacts[f".{runtime}/plugins/lup/hooks/runtime/policy_data.py"])
    constants = {
        target.id: ast.literal_eval(node.value)
        for node in data.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    grace = inspect.signature(policy_host.opened_deadline).parameters["grace"].default

    assert {
        entry["timeout"]
        for groups in hooks["hooks"].values()
        for group in groups
        for entry in group["hooks"]
        if "policy.sh" in entry["command"]
    } == {timeout}
    assert constants["HOOK_DEADLINE_SECONDS"] == hook_deadline(timeout)
    assert constants["HOOK_ANSWER_SECONDS"] == hook_answer_limit(timeout)
    assert hook_deadline(timeout) + grace < hook_answer_limit(timeout) < timeout


def test_the_guard_stamps_the_start_every_deadline_counts_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ten seconds of interpreter start and imports are ten seconds the hook no longer has."""
    monkeypatch.setenv("LUP_HOOK_STARTED", str(time.time() - 10))

    started = policy_host.hook_started()

    assert time.monotonic() - started == pytest.approx(10, abs=0.5)
    guard = Path(".claude/plugins/lup/hooks/scripts/policy.sh").read_text()
    assert 'LUP_HOOK_STARTED=$(date +%s) python3 -s "$script"' in guard


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_a_call_nobody_could_read_is_refused_as_malformed(
    checkout: Path, runtime: str
) -> None:
    """A refusal names its cause: input that is not a hook payload at all."""
    result = sh.Command(sys.executable)(
        "-I",
        "-S",
        str(checkout / f".{runtime}/plugins/lup/hooks/scripts/policy.py"),
        _in="not json",
        _ok_code=[0, 2],
        _return_cmd=True,
        _timeout=RUNTIME_LIMIT,
        _env={**os.environ, "PLUGIN_DATA": str(checkout.parent / "plugin-data")},
    )
    assert isinstance(result, sh.RunningCommand)
    said = result.stderr.decode() if result.exit_code == 2 else str(result)

    assert runtime == "claude" or result.exit_code == 2
    assert '"permissionDecision": "deny"' in said or result.exit_code == 2
    assert "hook input is malformed" in said
    assert "deadline" not in said
    assert "in time" not in said


IN_PROCESS_TIMEOUT = 5.5
"""A runtime timeout leaving the verdict half a second, past the reserve a hook keeps."""


class StalledPolicy(DecisionPolicy[SemanticTool]):
    """A judgement that takes longer than the hook it sits behind is given."""

    def decide(self, event: SemanticTool) -> Decision:
        time.sleep(2)
        return Decision(effect="allow")


class FailingPolicy(DecisionPolicy[SemanticTool]):
    """A judgement that raises, as a defect in one does."""

    def decide(self, event: SemanticTool) -> Decision:
        raise KeyError("tool_input")


def sdk_call(command: str) -> claude_types.PreToolUseHookInput:
    return claude_types.PreToolUseHookInput(
        hook_event_name="PreToolUse",
        session_id="session",
        transcript_path="/transcript",
        cwd="/cwd",
        tool_name="Bash",
        tool_input={"command": command},
        tool_use_id="use-1",
    )


async def test_an_in_process_judgement_past_its_deadline_is_refused_in_time() -> None:
    """A session opened here meets the deadline its plugin's hook meets, from one timeout.

    The SDK is told the declared timeout, and the verdict is due by the
    deadline derived from it: a judgement still running then is refused in
    the dispatchers' words rather than left for the runtime's own timeout.
    """
    hooks = create_policy_hooks(
        StalledPolicy(), CLAUDE_SEMANTICS, timeout=IN_PROCESS_TIMEOUT
    )
    [native] = lup_hooks_to_claude(hooks)["PreToolUse"]

    started = time.monotonic()
    output = await native.hooks[0](
        sdk_call("git status"), "use-1", claude_types.HookContext(signal=None)
    )

    assert time.monotonic() - started < hook_deadline(IN_PROCESS_TIMEOUT) + 1
    assert native.timeout == IN_PROCESS_TIMEOUT
    assert "hookSpecificOutput" in output
    answer = output["hookSpecificOutput"]
    assert answer["hookEventName"] == "PreToolUse"
    assert "permissionDecision" in answer
    assert answer["permissionDecision"] == "deny"
    assert "permissionDecisionReason" in answer
    assert "could not judge this call in time" in answer["permissionDecisionReason"]


async def test_an_sdk_callback_whose_judgement_raises_refuses_the_call() -> None:
    """Claude Code runs a tool past a callback that raised, so a failed gate says deny."""
    hooks = create_policy_hooks(FailingPolicy(), CLAUDE_SEMANTICS)
    [native] = lup_hooks_to_claude(hooks)["PreToolUse"]

    output = await native.hooks[0](
        sdk_call("git status"), "use-1", claude_types.HookContext(signal=None)
    )

    assert "hookSpecificOutput" in output
    answer = output["hookSpecificOutput"]
    assert "permissionDecision" in answer
    assert answer["permissionDecision"] == "deny"
    assert "permissionDecisionReason" in answer
    assert answer["permissionDecisionReason"].startswith(
        "refused: the policy failed on this call, so it is refused unjudged (`KeyError"
    )
    assert "report-friction" in answer["permissionDecisionReason"]


@pytest.mark.parametrize("policy", [StalledPolicy(), FailingPolicy()])
async def test_a_codex_approval_nobody_judged_is_declined_with_the_refusal(
    policy: DecisionPolicy[SemanticTool],
) -> None:
    """The app-server waits on an approval without limit, and declines an error reply
    without a word to the agent; a judgement that stalls or raises is declined in
    time instead, with the refusal the turn is told."""
    told: list[str] = []

    async def receive(text: str) -> None:
        told.append(text)

    responder = CodexApprovalResponder(
        hooks=create_policy_hooks(policy, CODEX_SEMANTICS, timeout=IN_PROCESS_TIMEOUT),
        deliver_context=receive,
    )

    started = time.monotonic()
    answer = await responder.decide(COMMAND_APPROVAL, {"command": "git status"})

    assert time.monotonic() - started < hook_deadline(IN_PROCESS_TIMEOUT) + 1
    assert answer == "decline"
    assert (
        "could not judge this call in time"
        if isinstance(policy, StalledPolicy)
        else "the policy failed on this call, so it is refused unjudged (`KeyError"
    ) in told[0]


async def test_an_in_process_hook_leaves_other_events_alone() -> None:
    hooks = create_policy_hooks(FailingPolicy(), CLAUDE_SEMANTICS)

    output = await hooks.pre_tool_use[0].hook(
        LupHookInput(event="PostToolUse", tool_name="Bash", tool_input={})
    )

    assert output.decision is None


def test_the_alarm_interrupts_a_wait_nothing_else_bounds() -> None:
    started = time.monotonic()
    previous = policy_host.opened_deadline(0.0, grace=0.2)
    try:
        with pytest.raises(RuntimeError, match="deadline"):
            time.sleep(5)
    finally:
        policy_host.closed_deadline(previous)

    assert time.monotonic() - started < 2


def test_a_deadline_already_set_is_never_extended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process the hook starts inherits its deadline, and cannot push it back."""
    monkeypatch.setenv("LUP_HOOK_DEADLINE", repr(time.monotonic() + 2.0))

    previous = policy_host.opened_deadline(25.0)
    try:
        left = policy_host.hook_seconds_left(30.0)
    finally:
        policy_host.closed_deadline(previous)

    assert left <= 2.0
    assert policy_host.hook_seconds_left(30.0) <= 2.0
    monkeypatch.delenv("LUP_HOOK_DEADLINE")
    assert policy_host.hook_seconds_left(30.0) == 30.0
