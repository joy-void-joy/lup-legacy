"""Behavior tests for create_tool_gate, its four presets, and the completion guard.

The gate pattern: deny tool B (or Stop) with an agent-readable message
until condition A holds. Each preset must deny while locked and let the
call through once unlocked.
"""

from pathlib import Path

import pytest

from lup.policy.hooks import (
    LupHookEvent,
    LupHookInput,
    LupHookOutput,
    LupHooksConfig,
    create_completion_guard,
    create_tool_gate,
)
from lup.orchestration.realtime.scheduler import (
    Scheduler,
    create_meta_before_sleep_guard,
    create_pending_event_guard,
    create_stop_guard,
)
from lup.orchestration.reflection import (
    ReflectionGate,
    ReviewGate,
    ReviewResult,
    ReviewVerdict,
    create_reflection_gate,
)
from lup.types import JsonObject


def pre_tool_use(tool_name: str, tool_input: JsonObject | None = None) -> LupHookInput:
    return LupHookInput(
        event="PreToolUse",
        tool_name=tool_name,
        tool_input=tool_input or {},
    )


def post_tool_use(tool_name: str) -> LupHookInput:
    return LupHookInput(
        event="PostToolUse",
        tool_name=tool_name,
        tool_input={},
    )


def stop_input(stop_hook_active: bool) -> LupHookInput:
    return LupHookInput(
        event="Stop",
        stop_hook_active=stop_hook_active,
    )


async def run_hook(
    config: LupHooksConfig,
    event: LupHookEvent,
    input_data: LupHookInput,
    index: int = 0,
) -> LupHookOutput:
    """Invoke the hook callback registered for *event* directly."""
    matcher = config.for_event(event)[index]
    return await matcher.hook(input_data)


def permission_decision(output: LupHookOutput) -> str | None:
    return output.decision


def denial_reason(output: LupHookOutput) -> str:
    return output.reason


async def noop_action(_content: str) -> None:
    return None


# ---------------------------------------------------------------------------
# Primitive
# ---------------------------------------------------------------------------


async def test_gate_denies_before_unlock_and_passes_after() -> None:
    flag = {"open": False}
    config = create_tool_gate(
        gated_tool="Target",
        message="locked out",
        unlocked=lambda _input: flag["open"],
    )

    denied = await run_hook(config, "PreToolUse", pre_tool_use("Target"))
    assert permission_decision(denied) == "deny"
    assert denial_reason(denied) == "locked out"

    flag["open"] = True
    passed = await run_hook(config, "PreToolUse", pre_tool_use("Target"))
    assert passed == LupHookOutput()


async def test_gate_allow_when_unlocked_returns_explicit_allow() -> None:
    config = create_tool_gate(
        gated_tool="Target",
        message="m",
        unlocked=lambda _input: True,
        allow_when_unlocked=True,
    )
    out = await run_hook(config, "PreToolUse", pre_tool_use("Target"))
    assert permission_decision(out) == "allow"


async def test_gate_block_style_uses_decision_field() -> None:
    config = create_tool_gate(
        gated_tool="Target",
        message="halt",
        unlocked=lambda _input: False,
        style="block",
    )
    out = await run_hook(config, "PreToolUse", pre_tool_use("Target"))
    assert out.decision == "block"
    assert out.reason == "halt"


async def test_gate_dynamic_message_evaluated_at_denial_time() -> None:
    count = {"n": 1}
    config = create_tool_gate(
        gated_tool="T",
        message=lambda: f"{count['n']} pending",
        unlocked=lambda _input: False,
    )
    count["n"] = 7
    out = await run_hook(config, "PreToolUse", pre_tool_use("T"))
    assert denial_reason(out) == "7 pending"


async def test_on_unlock_tool_opens_the_gate() -> None:
    config = create_tool_gate(
        gated_tool="B", message="call A first", on_unlock_tool="A"
    )

    denied = await run_hook(config, "PreToolUse", pre_tool_use("B"))
    assert permission_decision(denied) == "deny"

    await run_hook(config, "PostToolUse", post_tool_use("A"))

    passed = await run_hook(config, "PreToolUse", pre_tool_use("B"))
    assert passed == LupHookOutput()


async def test_gate_matches_each_guarded_tool() -> None:
    config = create_tool_gate(
        gated_tool=["t1", "t2"], message="m", unlocked=lambda _input: False
    )
    assert [m.matcher for m in config.pre_tool_use] == ["t1", "t2"]


def test_gate_requires_a_condition_and_a_target() -> None:
    with pytest.raises(ValueError):
        create_tool_gate(gated_tool="X", message="m")
    with pytest.raises(ValueError):
        create_tool_gate(message="m", unlocked=lambda _input: True)


async def test_gate_passes_through_mismatched_events() -> None:
    config = create_tool_gate(
        gated_tool="X", message="m", unlocked=lambda _input: False
    )
    out = await run_hook(config, "PreToolUse", stop_input(False))
    assert out == LupHookOutput()


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


async def test_reflection_gate_preset() -> None:
    gate = ReflectionGate()
    config = create_reflection_gate(
        gate=gate, gated_tool="StructuredOutput", reflection_tool_name="review"
    )

    denied = await run_hook(config, "PreToolUse", pre_tool_use("StructuredOutput"))
    assert permission_decision(denied) == "deny"
    assert "review" in denial_reason(denied)

    gate.mark_reflected()
    allowed = await run_hook(config, "PreToolUse", pre_tool_use("StructuredOutput"))
    assert permission_decision(allowed) == "allow"

    gate.reset()
    denied_again = await run_hook(
        config, "PreToolUse", pre_tool_use("StructuredOutput")
    )
    assert permission_decision(denied_again) == "deny"


def review(verdict: ReviewVerdict) -> ReviewResult:
    return ReviewResult(verdict=verdict, assessment="a")


def test_review_gate_opens_on_approve_and_warn() -> None:
    approve_gate = ReviewGate()
    assert not approve_gate.reflected
    approve_gate.record(review(ReviewVerdict.approve))
    assert approve_gate.reflected

    warn_gate = ReviewGate()
    warn_gate.record(review(ReviewVerdict.warn))
    assert warn_gate.reflected


def test_review_gate_fail_keeps_closed_and_recloses() -> None:
    gate = ReviewGate()
    gate.record(review(ReviewVerdict.fail))
    assert not gate.reflected

    gate.record(review(ReviewVerdict.approve))
    assert gate.reflected

    # A later fail re-closes an open gate: the output has known errors now.
    gate.record(review(ReviewVerdict.fail))
    assert not gate.reflected


def test_review_gate_escape_hatch_after_consecutive_fails() -> None:
    gate = ReviewGate()
    gate.record(review(ReviewVerdict.fail))
    gate.record(review(ReviewVerdict.fail))
    assert not gate.reflected
    gate.record(review(ReviewVerdict.fail))
    assert gate.reflected


def test_review_gate_pass_resets_the_fail_streak() -> None:
    gate = ReviewGate()
    gate.record(review(ReviewVerdict.fail))
    gate.record(review(ReviewVerdict.fail))
    gate.record(review(ReviewVerdict.warn))
    assert gate.reflected
    # The streak restarted: two more fails do not trip the escape hatch.
    gate.record(review(ReviewVerdict.fail))
    gate.record(review(ReviewVerdict.fail))
    assert not gate.reflected


def test_review_gate_file_backed_state_crosses_instances(tmp_path: Path) -> None:
    flag = tmp_path / "gate_flag"
    first = ReviewGate(flag_path=flag)
    first.record(review(ReviewVerdict.fail))
    first.record(review(ReviewVerdict.fail))

    # A fresh instance over the same flag (subprocess restart) continues
    # the same streak and trips the escape hatch on the third fail.
    second = ReviewGate(flag_path=flag)
    assert not second.reflected
    second.record(review(ReviewVerdict.fail))
    assert second.reflected

    second.reset()
    assert second.consecutive_fails == 0
    assert not ReviewGate(flag_path=flag).reflected


async def test_reflection_gate_preset_accepts_a_review_gate() -> None:
    gate = ReviewGate()
    config = create_reflection_gate(gate=gate, gated_tool="mcp__notes__submit_output")

    gate.record(review(ReviewVerdict.fail))
    denied = await run_hook(
        config, "PreToolUse", pre_tool_use("mcp__notes__submit_output")
    )
    assert permission_decision(denied) == "deny"

    gate.record(review(ReviewVerdict.approve))
    allowed = await run_hook(
        config, "PreToolUse", pre_tool_use("mcp__notes__submit_output")
    )
    assert permission_decision(allowed) == "allow"


async def test_stop_guard_preset() -> None:
    config = create_stop_guard()

    blocked = await run_hook(config, "Stop", stop_input(False))
    assert blocked.decision == "block"
    assert "sleep" in blocked.reason

    passed = await run_hook(config, "Stop", stop_input(True))
    assert passed == LupHookOutput()


async def test_pending_event_guard_preset() -> None:
    unread = {"n": 2}
    scheduler = Scheduler(on_action=noop_action)
    config = create_pending_event_guard(
        check_unread=lambda: unread["n"],
        scheduler=scheduler,
        guarded_tools=["mcp__s__sleep", "mcp__s__schedule_action"],
    )
    assert [m.matcher for m in config.pre_tool_use] == [
        "mcp__s__sleep",
        "mcp__s__schedule_action",
    ]

    blocked = await run_hook(config, "PreToolUse", pre_tool_use("mcp__s__sleep"))
    assert blocked.decision == "block"
    assert "2 unread" in blocked.reason

    forced = await run_hook(
        config, "PreToolUse", pre_tool_use("mcp__s__sleep", {"force": True})
    )
    assert forced == LupHookOutput()

    own_debounce = await run_hook(
        config, "PreToolUse", pre_tool_use("mcp__s__sleep", {"debounce_initial": 5})
    )
    assert own_debounce == LupHookOutput()

    scheduler.wake("event")
    wake_pending = await run_hook(config, "PreToolUse", pre_tool_use("mcp__s__sleep"))
    assert wake_pending == LupHookOutput()
    scheduler.consume_wake()

    unread["n"] = 0
    nothing_unread = await run_hook(config, "PreToolUse", pre_tool_use("mcp__s__sleep"))
    assert nothing_unread == LupHookOutput()


async def test_meta_before_sleep_guard_preset() -> None:
    scheduler = Scheduler(on_action=noop_action)
    config = create_meta_before_sleep_guard(
        scheduler=scheduler, sleep_tool_name="mcp__s__sleep"
    )

    denied = await run_hook(config, "PreToolUse", pre_tool_use("mcp__s__sleep"))
    assert permission_decision(denied) == "deny"
    assert "meta" in denial_reason(denied)

    scheduler.meta_gate.mark_reflected()
    allowed = await run_hook(config, "PreToolUse", pre_tool_use("mcp__s__sleep"))
    assert permission_decision(allowed) == "allow"

    scheduler.on_agent_action()
    denied_again = await run_hook(config, "PreToolUse", pre_tool_use("mcp__s__sleep"))
    assert permission_decision(denied_again) == "deny"


# ---------------------------------------------------------------------------
# Completion guard
# ---------------------------------------------------------------------------


async def test_completion_guard_blocks_until_output_exists() -> None:
    submitted = {"done": False}
    config = create_completion_guard(lambda: submitted["done"])

    blocked = await run_hook(config, "Stop", stop_input(False))
    assert blocked.decision == "block"
    assert "mcp__notes__submit_output" in blocked.reason

    submitted["done"] = True
    passed = await run_hook(config, "Stop", stop_input(False))
    assert passed == LupHookOutput()


async def test_completion_guard_gives_up_after_max_blocks() -> None:
    config = create_completion_guard(lambda: False, max_blocks=2)

    first = await run_hook(config, "Stop", stop_input(False))
    assert first.decision == "block"
    assert "attempt 1/2" in first.reason
    second = await run_hook(config, "Stop", stop_input(False))
    assert second.decision == "block"
    assert "attempt 2/2" in second.reason

    # A confused agent must not loop forever: the guard releases the stop
    # and the orchestration layer surfaces the missing output instead.
    released = await run_hook(config, "Stop", stop_input(False))
    assert released == LupHookOutput()


async def test_completion_guard_passes_through_non_stop_events() -> None:
    config = create_completion_guard(lambda: False)
    out = await run_hook(config, "Stop", pre_tool_use("Anything"))
    assert out == LupHookOutput()


# ---------------------------------------------------------------------------
# Scheduler sleep result
# ---------------------------------------------------------------------------


async def test_sleep_result_carries_reason_and_time() -> None:
    scheduler = Scheduler(on_action=noop_action)
    result = await scheduler.sleep(0)
    assert result.reason == "timer"
    assert result.time


class TestReflectionGateReset:
    def test_reset_is_idempotent_without_flag_file(self, tmp_path: Path) -> None:
        """reset() must not raise when the flag file is already gone."""
        gate = ReflectionGate(flag_path=tmp_path / "meta_flag")
        gate.reset()  # never created
        gate.mark_reflected()
        assert gate.reflected is True
        gate.reset()
        assert gate.reflected is False
        gate.reset()  # second reset on an absent file must be a no-op

    def test_reset_survives_external_unlink(self, tmp_path: Path) -> None:
        """A racing deletion between check and unlink must not crash reset."""
        flag = tmp_path / "meta_flag"
        gate = ReflectionGate(flag_path=flag)
        gate.mark_reflected()
        assert flag.exists()
        flag.unlink()  # vanishes out from under the gate
        gate.reset()  # must tolerate the missing file
        assert gate.reflected is False

    def test_externally_created_flag_unlocks_the_gate(self, tmp_path: Path) -> None:
        """A hook subprocess touching the flag file must unlock this gate."""
        flag = tmp_path / "meta_flag"
        gate = ReflectionGate(flag_path=flag)
        assert gate.reflected is False
        flag.touch()
        assert gate.reflected is True
