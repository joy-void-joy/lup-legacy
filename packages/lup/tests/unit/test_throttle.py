"""Tests for the Throttle utility."""

import asyncio
import gc
import time

import pytest

from lup.execution.resilience.throttle import LoopState, Throttle


@pytest.mark.asyncio
async def test_concurrency_limit() -> None:
    """Verify max_concurrent limits parallel execution."""
    throttle = Throttle(max_concurrent=2)
    active = 0
    max_active = 0

    async def work() -> None:
        nonlocal active, max_active
        async with throttle.slot():
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(*[work() for _ in range(10)])
    assert max_active <= 2


@pytest.mark.asyncio
async def test_min_interval_enforced() -> None:
    """Verify min_interval enforces temporal spacing between requests."""
    throttle = Throttle(max_concurrent=10, min_interval=0.1)
    timestamps: list[float] = []

    async def work() -> None:
        async with throttle.slot():
            timestamps.append(time.monotonic())

    await asyncio.gather(*[work() for _ in range(5)])
    timestamps.sort()
    for i in range(1, len(timestamps)):
        gap = timestamps[i] - timestamps[i - 1]
        assert gap >= 0.09  # Allow small floating-point slack


@pytest.mark.asyncio
async def test_no_interval_is_fast() -> None:
    """With min_interval=0, only concurrency is limited -- no added delay."""
    throttle = Throttle(max_concurrent=5, min_interval=0.0)
    start = time.monotonic()

    async def work() -> None:
        async with throttle.slot():
            pass

    await asyncio.gather(*[work() for _ in range(5)])
    elapsed = time.monotonic() - start
    assert elapsed < 0.1


class TestThrottleCancellation:
    async def test_permit_released_when_cancelled_mid_interval(self) -> None:
        """A cancel during the interval wait must not leak the permit.

        With max_concurrent=1, a leaked permit makes every later acquire
        block forever. The throttle has a long min_interval so the first
        entrant is parked in the post-acquire sleep when it is cancelled.
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
        stay alive across the assertions, because ids of states from freed
        loops can coincide: the allocator may hand the second loop the
        first's address.
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
        # The closed loop's entry goes with it; the live loop keeps its own.
        assert list(throttle.loop_states) == [second_loop]
        assert throttle.loop_states[second_loop] is second
        second_loop.close()
