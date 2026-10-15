"""Tests for round-2 ARTEX-inspired features.

1. `ChallengeMessageBus.search` + `do_search_findings` — repeatable, on-demand
   keyword search across sibling findings (ARTEX's "过程级信息交换"), distinct
   from the poll-once `check`/`do_check_findings` path.
2. `terminal_reason_for` — structured "why did this solver stop" mapping
   (ARTEX terminalreason.go), pure observability.
"""

from __future__ import annotations

import pytest

from backend.message_bus import ChallengeMessageBus
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    SolverResult,
    terminal_reason_for,
)
from backend.tools.core import do_check_findings, do_search_findings


class TestMessageBusSearch:
    @pytest.fixture
    def bus(self) -> ChallengeMessageBus:
        return ChallengeMessageBus()

    async def test_search_finds_sibling_by_keyword(self, bus: ChallengeMessageBus) -> None:
        await bus.post("modelA", "found an admin endpoint at /api/v1/users?id=1")
        await bus.post("modelB", "login form is vulnerable to sqli")
        hits = await bus.search("admin endpoint", "modelB")
        assert len(hits) == 1
        assert hits[0].model == "modelA"

    async def test_search_excludes_own_findings(self, bus: ChallengeMessageBus) -> None:
        await bus.post("modelA", "my own note about a token")
        hits = await bus.search("token", "modelA")
        assert hits == []

    async def test_search_all_terms_required(self, bus: ChallengeMessageBus) -> None:
        await bus.post("modelA", "ssti in flask templates")
        # both terms must appear
        assert await bus.search("ssti django", "modelB") == []
        assert len(await bus.search("ssti flask", "modelB")) == 1

    async def test_search_case_insensitive(self, bus: ChallengeMessageBus) -> None:
        await bus.post("modelA", "Found an ADMIN Panel")
        assert len(await bus.search("admin panel", "modelB")) == 1

    async def test_search_most_recent_first(self, bus: ChallengeMessageBus) -> None:
        await bus.post("modelA", "credential old: password=first")
        await bus.post("modelB", "credential new: password=second")
        hits = await bus.search("credential", "modelZ")
        assert [h.model for h in hits] == ["modelB", "modelA"]

    async def test_search_respects_limit(self, bus: ChallengeMessageBus) -> None:
        for i in range(12):
            await bus.post(f"m{i}", f"shared keyword finding {i}")
        hits = await bus.search("keyword", "nobody", limit=5)
        assert len(hits) == 5

    async def test_search_empty_query_returns_nothing(self, bus: ChallengeMessageBus) -> None:
        await bus.post("modelA", "anything")
        assert await bus.search("   ", "modelB") == []

    async def test_search_does_not_advance_cursor(self, bus: ChallengeMessageBus) -> None:
        """search is repeatable and must NOT consume the unread backlog — a
        later check_findings still sees everything."""
        await bus.post("modelA", "a finding about a token")
        # search twice — both see it, cursor untouched
        assert len(await bus.search("token", "modelB")) == 1
        assert len(await bus.search("token", "modelB")) == 1
        # check still returns it as unread
        unread = await bus.check("modelB")
        assert len(unread) == 1

    async def test_search_survives_trim(self, bus: ChallengeMessageBus) -> None:
        """After MAX_FINDINGS trimming, search still works on the retained window."""
        from backend.message_bus import MAX_FINDINGS

        for i in range(MAX_FINDINGS + 10):
            await bus.post(f"m{i}", f"needle haystack entry {i}")
        hits = await bus.search("needle", "nobody")
        assert hits  # recent entries retained and searchable


class TestDoSearchFindings:
    async def test_no_bus(self) -> None:
        assert "No message bus" in await do_search_findings(None, "m", "x")

    async def test_empty_query(self) -> None:
        bus = ChallengeMessageBus()
        assert "empty query" in await do_search_findings(bus, "m", "  ")

    async def test_no_match(self) -> None:
        bus = ChallengeMessageBus()
        await bus.post("modelA", "something else")
        out = await do_search_findings(bus, "modelB", "zzz")
        assert "No sibling findings match" in out

    async def test_match_formatted(self) -> None:
        bus = ChallengeMessageBus()
        await bus.post("modelA", "admin panel at /admin")
        out = await do_search_findings(bus, "modelB", "admin")
        assert "admin panel at /admin" in out
        assert "modelA" in out
        # carries the unverified-self-report caveat
        assert "unverified" in out.lower()

    async def test_check_and_search_coexist(self) -> None:
        """check consumes the backlog; search still finds it afterward."""
        bus = ChallengeMessageBus()
        await bus.post("modelA", "shared secret token=abc")
        # check drains modelB's cursor
        assert "token=abc" in await do_check_findings(bus, "modelB")
        # a second check is empty (already read)
        assert "No new findings" in await do_check_findings(bus, "modelB")
        # but search still finds it (repeatable, cursor-independent)
        assert "token=abc" in await do_search_findings(bus, "modelB", "secret")


class TestTerminalReason:
    def test_flag_found(self) -> None:
        assert terminal_reason_for(FLAG_FOUND) == "flag_found"

    def test_gave_up(self) -> None:
        assert terminal_reason_for(GAVE_UP) == "gave_up"

    def test_error(self) -> None:
        assert terminal_reason_for(ERROR) == "error"

    def test_cancelled_wall_clock(self) -> None:
        assert terminal_reason_for(CANCELLED, "wall_clock") == "wall_clock_timeout"

    def test_cancelled_operator(self) -> None:
        assert terminal_reason_for(CANCELLED, "operator") == "operator_kill"

    def test_cancelled_circuit_breaker(self) -> None:
        assert terminal_reason_for(CANCELLED, "circuit_breaker") == "submit_circuit_breaker"

    def test_cancelled_sibling_win(self) -> None:
        assert terminal_reason_for(CANCELLED, "sibling_win") == "sibling_won"

    def test_cancelled_stall(self) -> None:
        assert terminal_reason_for(CANCELLED, "stall_watchdog") == "partial_stall_withdraw"

    def test_cancelled_swarm_complete(self) -> None:
        assert terminal_reason_for(CANCELLED, "swarm_complete") == "swarm_complete_no_flag"

    def test_cancelled_no_reason(self) -> None:
        assert terminal_reason_for(CANCELLED, "") == "cancelled"

    def test_status_wins_over_cancel_reason(self) -> None:
        """A confirmed flag is flag_found even if a cancel raced it."""
        assert terminal_reason_for(FLAG_FOUND, "sibling_win") == "flag_found"

    def test_unknown_cancel_reason_passes_through(self) -> None:
        """A new swarm-side reason stays visible instead of collapsing."""
        assert terminal_reason_for(CANCELLED, "brand_new_reason") == "brand_new_reason"

    def test_solver_result_default_terminal_reason(self) -> None:
        r = SolverResult(
            flag=None, status=CANCELLED, findings_summary="",
            step_count=0, cost_usd=0.0, log_path="",
        )
        assert r.terminal_reason == ""
