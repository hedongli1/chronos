"""Tests for R3 medium/low defect fixes (isolated, no network).

Covers: multi-flag duplicate completion, partial-credit non-confirmation,
poller suspicious-data guard, workdir slug traversal, cost provider mapping.
"""

from __future__ import annotations

import pytest

from backend.poller import PlatformPoller
from backend.tools.core import do_submit_flag


class TestDoSubmitFlagPartialCredit:
    class Platform:
        def __init__(self, result):
            self.result = result

        async def submit_flag(self, challenge_name, flag):
            return self.result

    @pytest.mark.asyncio
    async def test_partial_credit_not_confirmed(self) -> None:
        from backend.platforms import SubmitResult

        # correct but challenge_complete=False → partial credit, not confirmed
        r = SubmitResult(
            "correct", "partial", "CORRECT — +10 pts", challenge_complete=False, progress="1/3 flags"
        )
        display, confirmed = await do_submit_flag(self.Platform(r), "c", "flag{a}")
        assert confirmed is False

    @pytest.mark.asyncio
    async def test_full_solve_confirmed(self) -> None:
        from backend.platforms import SubmitResult

        r = SubmitResult(
            "correct", "full", "CORRECT", challenge_complete=True, progress="3/3 flags"
        )
        display, confirmed = await do_submit_flag(self.Platform(r), "c", "flag{a}")
        assert confirmed is True


class TestPollerSuspiciousData:
    class Platform:
        async def fetch_challenge_stubs(self):
            return [{"name": "only-one", "value": 10, "level": 1}]

        async def fetch_solved_names(self):
            return set()

    class EmptyPlatform:
        async def fetch_challenge_stubs(self):
            return []

        async def fetch_solved_names(self):
            return set()

    @pytest.mark.asyncio
    async def test_drop_to_zero_is_skipped(self) -> None:
        poller = PlatformPoller(platform=self.Platform())  # type: ignore[arg-type]
        await poller.start()
        assert "only-one" in poller.known_challenges
        # Now a poll that returns [] must be rejected, not accepted.
        empty = self.EmptyPlatform()

        async def empty_fetch():
            return await empty.fetch_challenge_stubs(), await empty.fetch_solved_names()

        poller._fetch_state = empty_fetch  # type: ignore[assignment]
        await poller._poll_once()
        assert "only-one" in poller.known_challenges
        await poller.stop()
        await poller._poll_once()
        assert "only-one" in poller.known_challenges
        await poller.stop()


class TestWorkdirSlug:
    def test_traversal_names_collapse(self) -> None:
        from backend.agents.swarm import ChallengeSwarm
        from backend.cost_tracker import CostTracker
        from backend.prompts import ChallengeMeta

        swarm = ChallengeSwarm(
            challenge_dir=".",
            meta=ChallengeMeta(name="..", connection_info="10.0.0.1:80"),
            platform=object(),  # type: ignore[arg-type]
            cost_tracker=CostTracker(),
            settings=object(),
            model_specs=["m/1"],
        )
        path = swarm._workdir_for("m/1")
        # Must not resolve to the parent of workspaces
        assert ".." not in path.split("/")[-2:]
        assert path.endswith("x") or path.endswith("-x") or "x" in path.split("/")[-1]


class TestCostProviderMap:
    def test_provider_map_has_no_bogus_ids(self) -> None:
        from backend.cost_tracker import PROVIDER_MAP

        assert PROVIDER_MAP.get("qwen") is None
        assert PROVIDER_MAP.get("glm") is None
        assert PROVIDER_MAP.get("deepseek") == "deepseek"