"""Every spawn goes out named, judged end to end through the installed dispatchers.

Each half is run as its runtime runs it, over the payload shape it was
measured to deliver: Claude Code's `Agent` call holds `description`, `prompt`
and `subagent_type`, and `name` when the caller passed one; Codex's
`collaborationspawn_agent` holds `task_name` and `message`. The kernel is
asked directly for the shape of a derived name and for what a project that
requires no name does, since no installed tree declines it.
"""

import json
import sys
from pathlib import Path

import sh

from lup.policy.kernel.spawns import decide_spawn, spawn_name
from lup.policy.relay import QuestionRelay
from lup.types import JsonObject
from lup_template.harness.catalog import portable_harness
from tests.unit.native import codex_denial

DISPATCHER = Path(".claude/plugins/lup/hooks/scripts/policy.py")
CODEX_DISPATCHER = Path(".codex/plugins/lup/hooks/scripts/policy.py")
DESCRIPTION = "Run monitor leak probe"
PROMPT = "Reply with the single word ok."


def decide(payload: JsonObject) -> JsonObject:
    """Run the generated Claude dispatcher over one hook payload."""
    return json.loads(
        str(sh.Command("python3")("-I", "-S", str(DISPATCHER), _in=json.dumps(payload)))
    )


def arguments(
    name: str | None, prompt: str = PROMPT, description: str = DESCRIPTION
) -> JsonObject:
    """What one native spawn carries, in the shape the runtime was measured to deliver."""
    named: JsonObject = {} if name is None else {"name": name}
    return {
        "description": description,
        "prompt": prompt,
        "subagent_type": "general-purpose",
        **named,
    }


def spawn(
    name: str | None, prompt: str = PROMPT, description: str = DESCRIPTION
) -> JsonObject:
    """One native spawn as the hook is handed it."""
    return {
        "tool_name": "Agent",
        "tool_input": arguments(name, prompt, description),
        "cwd": str(Path.cwd()),
    }


def rewritten(decision: JsonObject) -> JsonObject:
    """The arguments a decision sends the call out with, having decided nothing.

    Claude Code takes `updatedInput` without `permissionDecision` as the
    arguments alone and leaves the permission where it was, which is what a
    deferral is: read out of 2.1.283, and measured there — an unnamed spawn
    rewritten this way was recorded by the runtime under the name the hook
    gave it.
    """
    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert "permissionDecision" not in specific
    updated = specific["updatedInput"]
    assert isinstance(updated, dict)
    return updated


def refusal(decision: JsonObject) -> str:
    """The reason a refused spawn is handed back with."""
    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "deny"
    return str(specific["permissionDecisionReason"])


def codex_spawn(task_name: str | None) -> sh.RunningCommand:
    """Run the generated Codex dispatcher over one spawn of the recorded shape."""
    named: JsonObject = {} if task_name is None else {"task_name": task_name}
    result = sh.Command(sys.executable)(
        "-I",
        "-S",
        str(CODEX_DISPATCHER.resolve()),
        _in=json.dumps(
            {
                "session_id": "spawn-probe",
                "hook_event_name": "PreToolUse",
                "cwd": str(Path.cwd()),
                "tool_name": "collaborationspawn_agent",
                "tool_input": {"message": PROMPT, **named},
            }
        ),
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    return result


def test_a_spawn_without_a_name_goes_out_under_its_description() -> None:
    """The schema shown to the model lists no `name`, so the caller never passes one.

    Measured on Claude Code 2.1.280 and again on 2.1.283: the `Agent` schema
    the model reads has no `name` and `additionalProperties` false, while the
    runtime takes one. Every spawn there carries a description, so the name
    is read out of it rather than asked for.
    """
    decision = decide(spawn(None))

    assert rewritten(decision) == {
        **arguments(None),
        "name": "run_monitor_leak_probe",
    }


def test_a_blank_name_is_no_name() -> None:
    decision = decide(spawn("   "))

    assert rewritten(decision)["name"] == "run_monitor_leak_probe"


def test_a_named_spawn_is_left_to_the_runtime() -> None:
    """Deferred, not allowed, and not rewritten: the name it carries wins."""
    assert decide(spawn("leak_probe")) == {}
    assert decide(spawn("Leak_Probe_2")) == {}


def test_a_hyphen_is_normalized_rather_than_refused() -> None:
    """Measured on Codex 0.155.1: a hyphenated name produced no PostToolUse at all.

    The model retried with underscores unprompted, having learned the shape
    by guessing. Sending it out in that shape is the guess made for it.
    """
    decision = decide(spawn("Leak-Probe"))

    assert rewritten(decision) == {**arguments("Leak-Probe"), "name": "leak_probe"}


def test_a_name_may_not_open_with_its_punctuation() -> None:
    """Both runtimes want a letter or a digit first, so the name opens with one."""
    assert rewritten(decide(spawn("_leak_probe")))["name"] == "leak_probe"


def test_a_long_name_is_cut_at_a_word_within_the_limit() -> None:
    """The shorter of the two runtimes' limits, counted rather than trusted."""
    declared = portable_harness().declared_hooks.spawn_names
    assert declared is not None
    words = " ".join(f"step{index}" for index in range(40))

    named = spawn_name("", words, declared.erased())

    assert named == "_".join(f"step{index}" for index in range(10))
    assert len(named) <= declared.limit
    assert decide(spawn("a" * declared.limit)) == {}
    assert rewritten(decide(spawn("a" * (declared.limit + 1))))["name"] == (
        "a" * declared.limit
    )


def test_a_description_with_nothing_to_read_is_refused_with_the_shape_of_a_name() -> (
    None
):
    """The refusal says what a name is for, which argument it is, and its shape.

    The one spawn left without a name is one whose description holds no
    letter or digit a runtime here would accept, and the recovery spells the
    argument because the schema the model read does not.
    """
    declared = portable_harness().declared_hooks.spawn_names
    assert declared is not None

    reason = refusal(decide(spawn(None, description="探针")))

    assert declared.reason in reason
    assert "pass the name as `name`" in reason
    assert declared.recovery in reason


def test_the_recovery_names_the_key_each_runtime_reads() -> None:
    """One declaration, two spellings: the dispatcher that read the key passes it."""
    declared = portable_harness().declared_hooks.spawn_names
    assert declared is not None

    for field in ("name", "task_name"):
        refused = decide_spawn("", "", [], declared.erased(), field)
        assert refused.effect == "deny"
        assert f"pass the name as `{field}`" in str(refused.recovery)
        assert declared.recovery in str(refused.recovery)


def test_a_project_running_one_runtime_may_widen_what_a_name_carries() -> None:
    """The safe set is the declaration's, and its first mark joins a normalized name."""
    declared = portable_harness().declared_hooks.spawn_names
    assert declared is not None
    widened = declared.model_copy(update={"punctuation": "-_"}).erased()

    assert spawn_name("leak-probe", "", widened) == "leak-probe"
    assert spawn_name("leak.probe", "", widened) == "leak-probe"
    assert decide_spawn("leak.probe", "", [], widened, "name").effect == "defer"


def test_a_refusal_escalates_into_the_question_the_caller_asked_for(
    tmp_path: Path,
) -> None:
    """The marker rides in the prompt, the one input a caller writes prose into."""
    payload = spawn(
        None,
        prompt="# lup: escalate: measuring the hook\nReply ok.",
        description="探针",
    )
    payload.update(
        cwd=str(tmp_path), session_id="requester", hook_event_name="PreToolUse"
    )
    decision = decide(payload)

    specific = decision["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["permissionDecision"] == "ask"
    assert "measuring the hook" in str(specific["permissionDecisionReason"])
    assert QuestionRelay(tmp_path / ".lup/questions.jsonl").pending() == []


def test_a_project_requiring_no_name_leaves_every_spawn_alone() -> None:
    assert decide_spawn("", "", [], None, "name").effect == "defer"
    assert spawn_name("leak-probe", DESCRIPTION, None) == "leak-probe"
    assert spawn_name("", DESCRIPTION, None) == ""


def test_the_other_runtime_sends_a_hyphenated_name_out_normalized() -> None:
    """Codex takes a rewrite only beside an allow, which a spawn there needs no other way.

    Its hook documentation and the 0.158.0 binary agree: `updatedInput` is
    honoured for a function tool beside `permissionDecision: "allow"` and
    reported as an error anywhere else, and a spawn raises no approval of its
    own for the allow to have settled.
    """
    result = codex_spawn("pty-arming-probe")

    assert result.exit_code == 0
    specific = json.loads(result.stdout)["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "allow"
    assert specific["updatedInput"] == {
        "message": PROMPT,
        "task_name": "pty_arming_probe",
    }


def test_the_other_runtime_leaves_a_name_of_the_shape_alone() -> None:
    result = codex_spawn("pty_arming_probe")

    assert result.exit_code == 0
    assert result.stdout == b""


def test_the_other_runtime_has_no_description_to_read_a_name_from() -> None:
    """Its spawn carries a task name and a message, so a missing name is still refused."""
    declared = portable_harness().declared_hooks.spawn_names
    assert declared is not None

    reason = codex_denial(codex_spawn(None))

    assert declared.reason in reason
    assert "pass the name as `task_name`" in reason
