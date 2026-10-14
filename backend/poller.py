"""Background platform poller with configurable jittered intervals."""

import asyncio
import logging
import random
from dataclasses import dataclass, field

from backend.platforms import CompetitionPlatform

logger = logging.getLogger(__name__)


@dataclass
class PollEvent:
    kind: str  # "new_challenge" | "challenge_solved"
    challenge_name: str
    details: dict = field(default_factory=dict)


@dataclass
class PlatformPoller:
    """Polls the competition platform every interval_s seconds, emits events for new/solved challenges."""

    platform: CompetitionPlatform
    interval_s: float = 15.0
    jitter_s: float = 0.0

    _known_challenges: set[str] = field(default_factory=set)
    _known_solved: set[str] = field(default_factory=set)
    # Latest full stub dict per challenge name (value/level/category/etc) —
    # kept so the scheduler can prioritize by score/difficulty instead of
    # only knowing which names exist.
    _known_stubs: dict[str, dict] = field(default_factory=dict)
    # Whether at least one poll has succeeded since start. The scheduler uses
    # this to distinguish "genuinely zero unsolved challenges" from "the
    # platform was never reachable" before declaring the run finished.
    _last_poll_ok: bool = field(default=False)
    # Consecutive failed polls (reset on any success) — for the scheduler's
    # "never successfully polled" failure mode.
    _consecutive_failures: int = field(default=0)
    _event_queue: asyncio.Queue[PollEvent] = field(default_factory=asyncio.Queue)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self) -> None:
        """Do initial poll (silent — no events) and start the background loop.

        Raises RuntimeError for the adapter's deliberate fatal states (task
        finished / token invalid) so the CLI fails loudly instead of running
        a scheduler against a dead benchmark.
        """
        await self._seed()
        logger.info(
            "Poller initialized: %d challenges, %d solved",
            len(self._known_challenges),
            len(self._known_solved),
        )
        self._task = asyncio.create_task(self._loop(), name="platform-poller")

    async def _seed(self) -> None:
        """Initial fetch — just populate known state, no events."""
        try:
            stubs, solved = await self._fetch_state()
            self._known_challenges = {ch["name"] for ch in stubs}
            self._known_stubs = {ch["name"]: ch for ch in stubs}
            self._known_solved = solved
            self._last_poll_ok = True
            self._consecutive_failures = 0
        except RuntimeError:
            # Fatal platform state (task finished / invalid token): propagate.
            raise
        except Exception as e:
            self._consecutive_failures += 1
            logger.warning("Initial poll error: %s", e)

    async def _fetch_state(self) -> tuple[list[dict], set[str]]:
        """Use a platform snapshot when available to avoid duplicate list requests."""
        fetch_poll_state = getattr(self.platform, "fetch_poll_state", None)
        if callable(fetch_poll_state):
            return await fetch_poll_state()
        stubs = await self.platform.fetch_challenge_stubs()
        return stubs, await self.platform.fetch_solved_names()

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def get_event(self, timeout: float = 1.0) -> PollEvent | None:
        """Non-blocking get — returns None if no event within timeout.

        Only the timeout is swallowed: CancelledError must propagate so the
        scheduler task actually acknowledges an external cancel (Ctrl+C or
        web-console shutdown) instead of silently looping forever.
        """
        try:
            return await asyncio.wait_for(self._event_queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    def drain_events(self) -> list[PollEvent]:
        """Drain all pending events without blocking."""
        events: list[PollEvent] = []
        while not self._event_queue.empty():
            try:
                events.append(self._event_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

    @property
    def known_challenges(self) -> set[str]:
        return set(self._known_challenges)

    @property
    def known_solved(self) -> set[str]:
        return set(self._known_solved)

    @property
    def known_stubs(self) -> dict[str, dict]:
        """Latest stub dict per challenge name (value/level/category/...)."""
        return dict(self._known_stubs)

    @property
    def last_poll_ok(self) -> bool:
        """True when at least one poll has succeeded since start."""
        return self._last_poll_ok

    @property
    def consecutive_failures(self) -> int:
        """Consecutive failed polls (reset on any success)."""
        return self._consecutive_failures

    async def _poll_once(self) -> None:
        try:
            stubs, current_solved = await self._fetch_state()
            self._last_poll_ok = True
            self._consecutive_failures = 0
            current_names = {ch["name"] for ch in stubs}

            # Sanity check: if results look bogus compared to what we know, skip.
            if self._known_challenges and len(current_names) < len(self._known_challenges) // 2:
                logger.warning(f"Poll returned suspicious data ({len(current_names)} challenges vs {len(self._known_challenges)} known) — skipping")
                return
            # Single known challenge: any drop to zero is suspicious (a stale
            # [] answer would otherwise be accepted as "the run is over").
            if self._known_challenges and not current_names:
                logger.warning("Poll returned 0 challenges (had %d) — skipping", len(self._known_challenges))
                return
            # Don't let solved count regress (API might return empty on errors)
            if self._known_solved and not current_solved:
                logger.warning("Poll returned 0 solved (had %d) — skipping", len(self._known_solved))
                return

            # Detect new challenges
            new_challenges = current_names - self._known_challenges
            for name in new_challenges:
                logger.info("New challenge detected: %s", name)
                self._event_queue.put_nowait(
                    PollEvent("new_challenge", name)
                )

            # Detect newly solved
            new_solves = current_solved - self._known_solved
            for name in new_solves:
                logger.info("Challenge solved: %s", name)
                self._event_queue.put_nowait(
                    PollEvent("challenge_solved", name)
                )

            self._known_challenges = current_names
            self._known_stubs = {ch["name"]: ch for ch in stubs}
            self._known_solved = current_solved

        except Exception as e:
            self._consecutive_failures += 1
            logger.warning(f"Poll error: {e}")

    def _next_poll_delay(self) -> float:
        jitter = max(0.0, self.jitter_s)
        return random.uniform(
            max(0.0, self.interval_s - jitter),
            self.interval_s + jitter,
        )

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._next_poll_delay(),
                )
                break
            except TimeoutError:
                pass
            await self._poll_once()
