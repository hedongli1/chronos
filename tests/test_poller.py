"""Tests for poller cancellation semantics and scheduler termination.

High-severity regressions fixed in R2:
1. `get_event` swallowed asyncio.CancelledError — a cancelled scheduler task
   kept looping, hanging web-console shutdown and ignoring Ctrl+C.
2. A failed first poll was interpreted as "all solved" and the scheduler
   exited with a fake empty success.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.poller import PlatformPoller


class DummyPlatform:
    async def fetch_challenge_stubs(self):
        return []

    async def fetch_solved_names(self):
        return set()


class FlakyPlatform(DummyPlatform):
    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def fetch_challenge_stubs(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("transient")
        return [{"name": "ch1", "value": 100, "level": 1}]


class TestPollerGetEvent:
    @pytest.mark.asyncio
    async def test_timeout_returns_none(self) -> None:
        poller = PlatformPoller(platform=DummyPlatform())  # type: ignore[arg-type]
        assert await poller.get_event(timeout=0.05) is None

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self) -> None:
        poller = PlatformPoller(platform=DummyPlatform())  # type: ignore[arg-type]
        started = asyncio.Event()
        done = asyncio.Event()

        async def waiter() -> None:
            started.set()
            await poller.get_event(timeout=10.0)
            done.set()

        task = asyncio.create_task(waiter())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not done.is_set()


class TestPollerSeedTracking:
    @pytest.mark.asyncio
    async def test_seed_failure_marks_poll_never_ok(self) -> None:
        poller = PlatformPoller(platform=FlakyPlatform(fail_times=1))  # type: ignore[arg-type]
        assert poller.last_poll_ok is False
        await poller.start()
        assert poller.last_poll_ok is False
        assert poller.consecutive_failures == 1
        await poller.stop()

    @pytest.mark.asyncio
    async def test_seed_success_marks_poll_ok(self) -> None:
        poller = PlatformPoller(platform=FlakyPlatform(fail_times=0))  # type: ignore[arg-type]
        await poller.start()
        assert poller.last_poll_ok is True
        await poller.stop()

    @pytest.mark.asyncio
    async def test_recovery_resets_failure_count(self) -> None:
        poller = PlatformPoller(platform=FlakyPlatform(fail_times=1), interval_s=0.05)
        await poller.start()
        assert poller.last_poll_ok is False
        # Wait for one successful background poll.
        for _ in range(200):
            if poller.last_poll_ok:
                break
            await asyncio.sleep(0.05)
        assert poller.last_poll_ok is True
        assert poller.consecutive_failures == 0
        await poller.stop()