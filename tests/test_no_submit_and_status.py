"""Tests for --no-submit dry-run plumbing and submit-status accounting.

High-severity regressions fixed in R2:
1. `--no-submit` was dead: ChallengeSwarm.no_submit was never threaded into
   HeadlessSolver/HeadlessDeps, so dry runs submitted real flags.
2. Non-definitive statuses ('unknown') were counted as wrong guesses and
   could trip the swarm-wide circuit breaker.
"""

from __future__ import annotations

import pytest

from backend.agents.headless_solver import HeadlessSolver
from backend.agents.swarm import ChallengeSwarm
from backend.cost_tracker import CostTracker
from backend.platforms import SubmitResult
from backend.prompts import ChallengeMeta


def _meta() -> ChallengeMeta:
    return ChallengeMeta(name="chal-a", connection_info="10.0.0.5:80", flag_count=1)


class DummyPlatform:
    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.results: list[SubmitResult] = []

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        self.submitted.append(flag)
        result = self.results.pop(0) if self.results else SubmitResult("incorrect", "", "INCORRECT")
        return result


class TestNoSubmitPlumbing:
    def test_no_submit_reaches_headless_deps(self) -> None:
        solver = HeadlessSolver(
            model_spec="deepseek/deepseek-chat",
            meta=_meta(),
            platform=DummyPlatform(),  # type: ignore[arg-type]
            cost_tracker=CostTracker(),
            settings=object(),
            no_submit=True,
        )
        assert solver.no_submit is True
        assert solver.deps.no_submit is True

    def test_default_allows_submission(self) -> None:
        solver = HeadlessSolver(
            model_spec="deepseek/deepseek-chat",
            meta=_meta(),
            platform=DummyPlatform(),  # type: ignore[arg-type]
            cost_tracker=CostTracker(),
            settings=object(),
        )
        assert solver.no_submit is False
        assert solver.deps.no_submit is False


class TestTrySubmitFlagDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_never_touches_platform(self) -> None:
        platform = DummyPlatform()
        swarm = ChallengeSwarm(
            challenge_dir=".",
            meta=_meta(),
            platform=platform,  # type: ignore[arg-type]
            cost_tracker=CostTracker(),
            settings=object(),
            model_specs=["m/1"],
            no_submit=True,
        )
        display, confirmed = await swarm.try_submit_flag("flag{x}", "m/1")
        assert confirmed is False
        assert "DRY RUN" in display
        assert platform.submitted == []


class TestSubmitStatusAccounting:
    @pytest.mark.asyncio
    async def test_unknown_status_not_counted_as_wrong_guess(self) -> None:
        """'unknown' (infra error) must not feed the circuit breaker."""
        platform = DummyPlatform()
        swarm = ChallengeSwarm(
            challenge_dir=".",
            meta=_meta(),
            platform=platform,  # type: ignore[arg-type]
            cost_tracker=CostTracker(),
            settings=object(),
            model_specs=["m/1"],
        )
        platform.results = [
            SubmitResult("unknown", "infra down", "SUBMIT ERROR — infra down"),
        ] * 10
        for _ in range(10):
            display, confirmed = await swarm.try_submit_flag("flag{a}", "m/1")
            assert confirmed is False
            assert "WASN'T JUDGED" in display
        # Ten infra errors must not trip the breaker or burn the flag.
        assert swarm._wrong_total == 0
        assert swarm._submit_locked is False
        assert "flag{a}" not in swarm._submitted_flags

    @pytest.mark.asyncio
    async def test_incorrect_status_counts_and_trips_breaker(self) -> None:
        platform = DummyPlatform()
        swarm = ChallengeSwarm(
            challenge_dir=".",
            meta=_meta(),
            platform=platform,  # type: ignore[arg-type]
            cost_tracker=CostTracker(),
            settings=object(),
            model_specs=["m/1"],
        )
        platform.results = [
            SubmitResult("incorrect", "", 'INCORRECT — "flag{a}" rejected'),
        ] * 10
        # Disable escalating cooldowns so consecutive wrong submissions can
        # reach the breaker in test time (the breaker logic is under test,
        # not the cooldown clock).
        swarm.SUBMISSION_COOLDOWNS = [0, 0, 0, 0, 0]
        flags = ["flag{0}", "flag{1}", "flag{2}", "flag{3}"]
        for f in flags:
            await swarm.try_submit_flag(f, "m/1")
        assert swarm._wrong_total == 4
        assert swarm._submit_locked is True
        assert swarm.cancel_event.is_set()

    @pytest.mark.asyncio
    async def test_transient_submit_exception_keeps_flag_retryable(self) -> None:
        class FailingPlatform(DummyPlatform):
            async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
                raise ConnectionError("transient")

        swarm = ChallengeSwarm(
            challenge_dir=".",
            meta=_meta(),
            platform=FailingPlatform(),  # type: ignore[arg-type]
            cost_tracker=CostTracker(),
            settings=object(),
            model_specs=["m/1"],
        )
        display, confirmed = await swarm.try_submit_flag("flag{retry-me}", "m/1")
        assert confirmed is False
        assert "did not judge" in display
        assert "flag{retry-me}" not in swarm._submitted_flags
        assert swarm._wrong_total == 0
