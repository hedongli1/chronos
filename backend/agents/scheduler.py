"""Mechanical scheduler entrypoint.

Replaces the former LLM coordinators. There is no model driving strategy here:
`build_deps` wires up the platform client and run state, and `run_event_loop`
runs a deterministic poll → fill-by-priority → reap → refill loop. All model
reasoning lives inside the solvers.
"""

from __future__ import annotations

from typing import Any

from backend.agents.coordinator_loop import build_deps, run_event_loop
from backend.config import Settings


async def run_scheduler(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    no_submit: bool = False,
    interactive: bool = False,
) -> dict[str, Any]:
    """Build run state and drive the mechanical scheduler loop to completion."""
    platform, cost_tracker, deps = build_deps(
        settings, model_specs, challenges_root, no_submit,
    )

    if getattr(settings, "platform_mode", "auto") in ("tsec", "tsecbench", "benchmark"):
        # TSec caps live range containers at 3; keep the rolling window within it.
        deps.max_concurrent_challenges = min(
            getattr(settings, "max_concurrent_challenges", 3), 3
        )

    return await run_event_loop(
        deps, platform, cost_tracker, interactive=interactive,
    )
