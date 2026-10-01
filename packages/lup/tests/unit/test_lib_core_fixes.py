# lup: ignore[bare-object, own-model-dispatch]
# Test fixtures and assertions construct these shapes deliberately.
# The no-loss checks assert which relay-event kind survived a raising handler,
# a non-destructive peek, and a reset — the variant is what is being observed
# from outside the union, not a walk over it that RelayEvent could answer.
"""Behavior tests for core-library correctness fixes.

Each test pins an invariant that a specific bug used to violate:

- Throttle releases its semaphore permit when cancelled mid-interval, so a
  long-lived process can't bleed concurrency down to a deadlock.
- Metrics flush is atomic: a reader never sees a half-written snapshot and
  no temp file is left behind.
- The realtime relay never drops an agent event when a handler raises or a
  watcher is cancelled mid-batch, and a re-run of a session id does not
  replay the previous run's events.
- The reflection gate's file-backed reset survives the file vanishing under
  it (TOCTOU).
- A session's read-only grant excludes the feedback-loop logs directory.

Docker-backed sandbox paths are not exercised here (no daemon in CI); the
pure timeout/deadline behavior of ``ReplSession.execute`` is, since it is
computable without a container.
"""

import asyncio
import gc
import json
import os
import time
from pathlib import Path

import pytest

from lup.workspace import paths
from lup.observability.metrics import (
    MetricsCollector,
    metrics_path,
    read_metrics_summary,
)
from lup.workspace.notes import setup_notes
from lup.workspace.paths import path_is_under, sessions_dir, trace_logs_dir
from lup.orchestration.realtime.models import SleepInput
from lup.orchestration.realtime.relay import RealtimeMailbox, ReplyEvent, RemindEvent
from lup.orchestration.reflection import ReflectionGate
from lup.sandbox.container import Sandbox
from lup.coordination.bare.runtime import process_scope, runtime_of
from lup.execution.resilience.throttle import LoopState, Throttle

HAVE_PROC = Path("/proc/self/stat").exists()


class TestThrottleCancellation:
    async def test_permit_released_when_cancelled_mid_interval(self) -> None:
        """A cancel during the interval wait must not leak the permit.

        With max_concurrent=1, a leaked permit makes every later acquire
        block forever. The throttle has a long min_interval so the first
        entrant is parked in the post-acquire sleep when we cancel it.
        """
        throttle = Throttle(max_concurrent=1, min_interval=100.0)

        # Prime last_request_time so the next entrant must wait the interval.
        async with throttle.slot():
            pass

        entered = asyncio.Event()

        async def holder() -> None:
            entered.set()
            async with throttle.slot():
                pass

        semaphore = throttle.get_state().semaphore
        task = asyncio.create_task(holder())
        await entered.wait()
        await asyncio.sleep(0.02)  # let it reach the interval sleep
        # The holder has acquired the only permit and is parked in the
        # interval wait; the semaphore is held.
        assert semaphore.locked()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The permit must be free again — cancellation released it. A direct
        # acquire returns immediately rather than blocking forever.
        async with asyncio.timeout(1.0):
            await semaphore.acquire()
        semaphore.release()

    async def test_normal_exit_still_releases(self) -> None:
        """Sanity: the happy path frees the permit so reuse works."""
        throttle = Throttle(max_concurrent=1)
        async with throttle.slot():
            pass
        semaphore = throttle.get_state().semaphore
        async with asyncio.timeout(1.0):
            await semaphore.acquire()
        semaphore.release()


class TestThrottlePerLoopState:
    def test_each_live_loop_holds_its_own_state(self) -> None:
        """Two live loops hold two distinct states; a state goes with its loop.

        The throttle keys its state by the loop object, so a loop never
        inherits another loop's semaphore, and a closed loop's entry is
        evicted rather than left for whichever loop comes next. Both loops
        stay alive across the assertions: comparing ids of states from
        loops that had already been freed let the allocator hand the second
        the first's address.
        """
        throttle = Throttle(max_concurrent=2)
        first_loop = asyncio.new_event_loop()
        second_loop = asyncio.new_event_loop()

        async def grab_state() -> LoopState:
            return throttle.get_state()

        first = first_loop.run_until_complete(grab_state())
        second = second_loop.run_until_complete(grab_state())
        # Distinct state per live loop, stable across calls on the same loop.
        assert first is not second
        assert first_loop.run_until_complete(grab_state()) is first
        assert second_loop.run_until_complete(grab_state()) is second
        assert throttle.loop_states[first_loop] is first
        assert throttle.loop_states[second_loop] is second
        assert len(throttle.loop_states) == 2

        first_loop.close()
        del first_loop
        gc.collect()
        # The closed loop's entry went with it; the live loop kept its own.
        assert list(throttle.loop_states) == [second_loop]
        assert throttle.loop_states[second_loop] is second
        second_loop.close()


class TestMetricsAtomicFlush:
    def test_flush_leaves_valid_json_and_no_temp(self, tmp_path: Path) -> None:
        target = metrics_path(tmp_path)
        collector = MetricsCollector()
        collector.flush_path = target

        collector.record("search", 12.5)
        collector.record("search", 7.5, is_error=True)

        # Target is always complete, parseable JSON.
        summary = json.loads(target.read_text(encoding="utf-8"))
        assert summary["total_tool_calls"] == 2
        # The write-then-rename temp file is gone after a successful flush.
        assert not target.with_suffix(".tmp").exists()
        assert read_metrics_summary(tmp_path) is not None

    def test_target_only_changes_on_atomic_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The target file is mutated only by the rename, never in place.

        If the commit (``Path.replace``) fails, the target a reader may be
        parsing must still hold the previous complete snapshot — proof the
        new bytes were staged on a temp file, not written into the target.
        A direct write-into-place would truncate the target first.
        """
        target = metrics_path(tmp_path)
        collector = MetricsCollector()
        collector.flush_path = target

        collector.record("good", 1.0)
        good = target.read_text(encoding="utf-8")
        assert json.loads(good)["total_tool_calls"] == 1

        def failing_replace(src: object, dst: object) -> None:
            raise OSError("rename interrupted")

        monkeypatch.setattr(Path, "replace", failing_replace)
        collector.record("doomed", 1.0)  # flush() swallows the OSError

        # The target still parses as the last complete snapshot, untouched.
        assert target.read_text(encoding="utf-8") == good


async def apply_with_per_event_commit(
    mailbox: RealtimeMailbox,
    applied: list[str],
    *,
    fail_on: str | None = None,
) -> None:
    """Mirror the parent loop: apply each event, commit only after.

    ``fail_on`` makes the handler raise when it sees a reply with that
    message, standing in for a handler that errors mid-batch.
    """
    for pair in mailbox.peek_new_events():
        if isinstance(pair.event, ReplyEvent):
            if pair.event.message == fail_on:
                raise RuntimeError("handler boom")
            applied.append(pair.event.message)
        mailbox.read_offset = pair.commit_offset


class TestRelayNoLoss:
    async def test_raising_handler_leaves_unapplied_events(
        self, tmp_path: Path
    ) -> None:
        """A mid-batch failure must not advance past un-applied events."""
        writer = RealtimeMailbox(tmp_path)
        reader = RealtimeMailbox(tmp_path)
        writer.append_event(ReplyEvent(message="one"))
        writer.append_event(ReplyEvent(message="two"))
        writer.append_event(ReplyEvent(message="three"))

        applied: list[str] = []
        with pytest.raises(RuntimeError):
            await apply_with_per_event_commit(reader, applied, fail_on="two")

        # "one" applied; the failure at "two" left the offset there.
        assert applied == ["one"]

        # A retry redelivers "two" and "three" — nothing was dropped.
        await apply_with_per_event_commit(reader, applied)
        assert applied == ["one", "two", "three"]

    async def test_no_redelivery_once_fully_applied(self, tmp_path: Path) -> None:
        writer = RealtimeMailbox(tmp_path)
        reader = RealtimeMailbox(tmp_path)
        writer.append_event(ReplyEvent(message="only"))

        applied: list[str] = []
        await apply_with_per_event_commit(reader, applied)
        await apply_with_per_event_commit(reader, applied)
        assert applied == ["only"]

    def test_peek_does_not_advance_offset(self, tmp_path: Path) -> None:
        writer = RealtimeMailbox(tmp_path)
        reader = RealtimeMailbox(tmp_path)
        writer.append_event(ReplyEvent(message="x"))

        before = reader.read_offset
        pairs = reader.peek_new_events()
        assert [p.event.message for p in pairs if isinstance(p.event, ReplyEvent)] == [
            "x"
        ]
        assert reader.read_offset == before  # peek is non-destructive
        # Committing the last pair's offset consumes the event.
        reader.read_offset = pairs[-1].commit_offset
        assert reader.peek_new_events() == []


class TestRelayStaleReplay:
    def test_reset_clears_previous_run(self, tmp_path: Path) -> None:
        """A fresh run must not consume the previous run's leftovers."""
        mailbox = RealtimeMailbox(tmp_path)
        mailbox.append_event(ReplyEvent(message="ghost"))
        mailbox.append_event(RemindEvent(label="old", delay_seconds=5))
        mailbox.write_sleep_request(SleepInput(seconds=42))
        mailbox.meta_flag_path.parent.mkdir(parents=True, exist_ok=True)
        mailbox.meta_flag_path.touch()

        fresh = RealtimeMailbox(tmp_path)
        # Simulate a partial prior read so offset is non-zero too.
        fresh.read_offset = 10
        fresh.reset_for_new_run()

        assert fresh.read_offset == 0
        assert fresh.read_new_events() == []
        assert fresh.consume_sleep_request() is None
        assert not mailbox.meta_flag_path.exists()
        # The file is truncated, not deleted, so the within-run append path
        # keeps working.
        assert mailbox.actions_path.read_text(encoding="utf-8") == ""

    def test_within_run_protocol_survives_reset(self, tmp_path: Path) -> None:
        mailbox = RealtimeMailbox(tmp_path)
        mailbox.reset_for_new_run()
        mailbox.append_event(ReplyEvent(message="after reset"))

        events = mailbox.read_new_events()
        assert [e.message for e in events if isinstance(e, ReplyEvent)] == [
            "after reset"
        ]


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


class TestNotesReadOnlyExcludesLogs:
    def test_logs_dir_not_in_ro_grant(self, tmp_path: Path) -> None:
        """The agent's RO grant must not cover the feedback-loop logs dir."""
        saved_config = paths.state.config
        try:
            paths.configure(notes_dir=tmp_path / "notes", version="test")
            notes = setup_notes(session_id="s1", task_id="t1")

            logs_file = trace_logs_dir() / "s1" / "20200101_000000.md"
            assert not path_is_under(logs_file, notes.ro)
            assert not path_is_under(logs_file, notes.all_dirs)

            # Historical sessions and outputs remain readable.
            assert path_is_under(sessions_dir() / "other" / "x.json", notes.ro)
            assert path_is_under(trace_logs_dir(), [trace_logs_dir()])
        finally:
            paths.state.config = saved_config


def make_sandbox() -> Sandbox:
    """A Sandbox whose __init__ touches no Docker (no start() called)."""
    return Sandbox(session_id="liveness-test", shared_dir="/tmp/lup-test-shared")


NEVER_A_PID = 0x7FFFFFFF  # far above any real Linux PID; os.kill -> ProcessLookupError


class TestContainerOrphanDecision:
    def test_live_owner_kept_even_when_ancient(self) -> None:
        """The core fix: a long-lived owner survives past STALE_AGE_HOURS."""
        sandbox = make_sandbox()
        ancient = str(time.time() - sandbox.STALE_AGE_HOURS * 3600 * 10)
        owner = runtime_of(os.getpid())
        labels = {
            sandbox.OWNER_PID_LABEL: str(os.getpid()),
            sandbox.OWNER_START_LABEL: owner.get("started", ""),
            sandbox.OWNER_SCOPE_LABEL: owner.get("scope", ""),
            sandbox.CREATED_AT_LABEL: ancient,
        }
        assert sandbox.container_is_orphaned(labels) is False

    def test_dead_owner_is_orphaned(self) -> None:
        sandbox = make_sandbox()
        labels = {
            sandbox.OWNER_PID_LABEL: str(NEVER_A_PID),
            sandbox.OWNER_START_LABEL: "1",
            sandbox.OWNER_SCOPE_LABEL: process_scope(),
            sandbox.CREATED_AT_LABEL: str(time.time()),  # young, but owner gone
        }
        assert sandbox.container_is_orphaned(labels) is True

    def test_an_owner_recorded_elsewhere_is_judged_by_age(self) -> None:
        """A pid from another namespace names some other process here."""
        sandbox = make_sandbox()
        owner = runtime_of(os.getpid())
        elsewhere = {
            sandbox.OWNER_PID_LABEL: str(os.getpid()),
            sandbox.OWNER_START_LABEL: owner.get("started", ""),
            sandbox.OWNER_SCOPE_LABEL: "another-namespace",
        }
        young = {**elsewhere, sandbox.CREATED_AT_LABEL: str(time.time())}
        old = {
            **elsewhere,
            sandbox.CREATED_AT_LABEL: str(
                time.time() - sandbox.STALE_AGE_HOURS * 3600 - 60
            ),
        }
        assert sandbox.container_is_orphaned(young) is False
        assert sandbox.container_is_orphaned(old) is True

    def test_unparseable_owner_pid_is_orphaned(self) -> None:
        sandbox = make_sandbox()
        assert sandbox.container_is_orphaned({sandbox.OWNER_PID_LABEL: "nope"}) is True

    def test_age_fallback_when_no_owner_label(self) -> None:
        sandbox = make_sandbox()
        fresh = {sandbox.CREATED_AT_LABEL: str(time.time())}
        old = {
            sandbox.CREATED_AT_LABEL: str(
                time.time() - sandbox.STALE_AGE_HOURS * 3600 - 60
            )
        }
        assert sandbox.container_is_orphaned(fresh) is False
        assert sandbox.container_is_orphaned(old) is True
        # No labels at all -> created_at defaults to 0 -> treated as stale.
        assert sandbox.container_is_orphaned({}) is True
