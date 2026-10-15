"""Solver result type, status constants, and solver protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Status constants
FLAG_FOUND = "flag_found"
GAVE_UP = "gave_up"
CANCELLED = "cancelled"
ERROR = "error"

# Structured terminal reasons — a finer-grained "why did this solver stop" than
# the 4 coarse statuses above. Derived from status + the swarm's cancel reason
# (see headless_solver._terminal_reason). Pure observability: recorded in the
# trace `finish` event and on SolverResult; never fed back into scheduling or
# scoring. Inspired by ARTEX's terminalreason.go.
TERMINAL_REASONS = {
    # status-derived
    "flag_found": "flag_found",
    "gave_up": "gave_up",
    "error": "error",
    # cancel-reason-derived (CANCELLED status)
    "sibling_win": "sibling_won",
    "wall_clock": "wall_clock_timeout",
    "operator": "operator_kill",
    "stall_watchdog": "partial_stall_withdraw",
    "circuit_breaker": "submit_circuit_breaker",
    "swarm_complete": "swarm_complete_no_flag",
    "unknown": "cancelled_unknown",
}


def terminal_reason_for(status: str, cancel_reason: str = "") -> str:
    """Map (status, cancel_reason) → a structured terminal reason string.

    FLAG_FOUND/GAVE_UP/ERROR map directly; CANCELLED defers to the swarm's
    cancel_reason (wall_clock, operator, circuit_breaker, sibling_win, ...).
    An unmapped cancel_reason passes through verbatim so new swarm-side reasons
    stay visible instead of collapsing to a generic "cancelled".
    """
    if status in (FLAG_FOUND, GAVE_UP, ERROR):
        return TERMINAL_REASONS[status]
    if cancel_reason:
        return TERMINAL_REASONS.get(cancel_reason, cancel_reason)
    return "cancelled"


@dataclass
class SolverResult:
    flag: str | None
    status: str
    findings_summary: str
    step_count: int
    cost_usd: float
    log_path: str
    terminal_reason: str = ""


class SolverProtocol(Protocol):
    """Interface the swarm expects from a solver (only HeadlessSolver today)."""

    model_spec: str
    agent_name: str

    @property
    def step_count(self) -> int: ...

    async def start(self) -> None: ...
    async def run_until_done_or_gave_up(self) -> SolverResult: ...
    def bump(self, insights: str) -> None: ...
    async def stop(self) -> None: ...
