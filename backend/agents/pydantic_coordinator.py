"""Pydantic AI coordinator — works with DeepSeek, Qwen, and compatible models.

Uses Pydantic AI's Agent framework to drive a coordinator that can use any
model supported by the provider abstraction (deepseek, azure, google, etc).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.toolsets import FunctionToolset

from backend.agents.coordinator_core import (
    BUMP_DISCIPLINE,
    do_broadcast,
    do_bump_agent,
    do_check_swarm_status,
    do_fetch_challenges,
    do_get_solve_status,
    do_kill_swarm,
    do_read_solver_trace,
    do_spawn_swarm,
    do_submit_flag,
)
from backend.agents.coordinator_loop import build_deps, run_event_loop
from backend.checkpoints import (
    coordinator_history_path,
    load_coordinator_turns,
    save_coordinator_turns,
)
from backend.config import Settings
from backend.deps import CoordinatorDeps
from backend.models import resolve_model, resolve_model_settings
from backend.tools.http_tools import do_http_request

logger = logging.getLogger(__name__)

# Qwen occasionally emits a valid tool name with incomplete or malformed
# arguments.  Give it enough validation round-trips to repair the call without
# aborting the whole coordinator turn.
COORDINATOR_TOOL_MAX_RETRIES = 8

COORDINATOR_PROMPT = """\
You are the coordinator of an autonomous offensive-security agent swarm running for the ENTIRE duration of a live benchmark.
Your job is to maximize the number of targets solved within a strict per-task time budget.

Strategy:
- Spawn swarms for unsolved challenges, prioritizing by solve count (easy first)
- Use read_solver_trace to monitor what each solver is doing and where it's stuck
- When agents are stuck, read their traces, then craft targeted bumps that cite trace
  evidence (see BUMP DISCIPLINE below) with specific technical guidance
- Use broadcast to share cross-solver insights (e.g. flag format discovery, shared vulnerabilities)
- If the platform provides a REST API (e.g. for starting containers, listing challenges, or
  submitting flags), use the http_request tool to call it directly. You will be given the
  API base URL and auth headers — follow the operator's instructions for the specific
  platform workflow (endpoints, parameters, error codes).
- In TSec Benchmark mode, run fully autonomously but keep a rolling window of at most
  three active swarms (one range container each). When one solves, the system
  auto-fills the next available challenge so the window stays at three.

CRITICAL RULES:
- Each swarm has a hard per-challenge wall-clock limit enforced by the scheduler
  (default 1800s). A task that cannot be solved within its budget is closed and
  deferred; do not let one hard task starve the rest.
- When a TIME LIMIT or DEFERRED event arrives, do not respawn that challenge. Move
  immediately to another unsolved, non-deferred challenge. Only the operator may
  re-enable it with /resume.
- When a solver seems stuck, bump it with very specific technical guidance based on
  its trace, but only while the challenge remains within its time budget. Follow
  BUMP DISCIPLINE: cite the trace evidence; never assert facts you did not observe.
- Time and API cost matter. Do not keep investing in one challenge at the expense of others.
- Never claim that a flag was submitted or confirmed unless submit_flag returned CORRECT/ALREADY
  SOLVED or the platform emitted a SOLVED event. A solver's prose, candidate, or intention to
  submit is not confirmation.
- If you decide an exact candidate should be submitted, call submit_flag in that same turn.
  Do not merely say that you will submit it later or ask a solver to submit it.
- Current event messages and tool results are authoritative. If persisted history conflicts
  with current platform/swarm state, trust the current state.
- Review the prior conversation before acting. Do not repeat the same bump, spawn, or
  submission decision unless new solver/platform evidence justifies it.

You will receive event messages and operator prompts. Respond with tool calls to manage the competition.
"""



def _build_toolset(deps: CoordinatorDeps) -> FunctionToolset[CoordinatorDeps]:
    """Build a Pydantic AI toolset with all coordinator tools."""
    toolset: FunctionToolset[CoordinatorDeps] = FunctionToolset(
        max_retries=COORDINATOR_TOOL_MAX_RETRIES,
    )

    @toolset.tool
    async def fetch_challenges(ctx: RunContext[CoordinatorDeps]) -> str:
        """List all challenges with category, points, solve count, and status."""
        return await do_fetch_challenges(ctx.deps)

    @toolset.tool
    async def get_solve_status(ctx: RunContext[CoordinatorDeps]) -> str:
        """Check which challenges are solved and which swarms are running."""
        return await do_get_solve_status(ctx.deps)

    @toolset.tool
    async def spawn_swarm(ctx: RunContext[CoordinatorDeps], challenge_name: str) -> str:
        """Launch all solver models on a challenge."""
        return await do_spawn_swarm(ctx.deps, challenge_name)

    @toolset.tool
    async def check_swarm_status(ctx: RunContext[CoordinatorDeps], challenge_name: str) -> str:
        """Get per-agent progress for a swarm."""
        return await do_check_swarm_status(ctx.deps, challenge_name)

    @toolset.tool
    async def submit_flag(
        ctx: RunContext[CoordinatorDeps],
        challenge_name: str,
        flag: str,
    ) -> str:
        """Submit a flag. Both challenge_name and flag are required; never call with empty arguments."""
        if not challenge_name.strip() or not flag.strip():
            raise ModelRetry(
                "TOOL ARGUMENT ERROR: submit_flag requires both challenge_name and flag. "
                "Inspect the active challenge and exact candidate, then call submit_flag again "
                "with both non-empty values."
            )
        return await do_submit_flag(ctx.deps, challenge_name, flag)

    @toolset.tool
    async def kill_swarm(ctx: RunContext[CoordinatorDeps], challenge_name: str) -> str:
        """Cancel all agents for a challenge."""
        return await do_kill_swarm(ctx.deps, challenge_name)

    @toolset.tool
    async def bump_agent(
        ctx: RunContext[CoordinatorDeps], challenge_name: str, model_spec: str, insights: str
    ) -> str:
        """Send targeted insights to a stuck agent.

        insights must follow BUMP DISCIPLINE: cite trace/tool evidence you actually
        observed; label anything else as an unverified hypothesis with a cheap
        verification step; never assert endpoints, services, files, credentials,
        or strings you did not see in a tool result.
        """
        return await do_bump_agent(ctx.deps, challenge_name, model_spec, insights)

    @toolset.tool
    async def broadcast(ctx: RunContext[CoordinatorDeps], challenge_name: str, message: str) -> str:
        """Broadcast a strategic hint to ALL solvers on a challenge.

        Same evidence discipline as bump_agent: cite observed evidence or label
        unverified hypotheses; never assert facts not seen in a tool result.
        """
        return await do_broadcast(ctx.deps, challenge_name, message)

    @toolset.tool
    async def read_solver_trace(
        ctx: RunContext[CoordinatorDeps], challenge_name: str, model_spec: str, last_n: int = 20
    ) -> str:
        """Read recent trace events from a specific solver."""
        return await do_read_solver_trace(ctx.deps, challenge_name, model_spec, last_n)

    @toolset.tool
    async def http_request(
        ctx: RunContext[CoordinatorDeps],
        path_or_url: str,
        method: str = "GET",
        headers: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        json_body: Any | None = None,
        body: str | None = None,
        timeout: float = 30.0,
    ) -> str:
        """Send an HTTP request to the configured competition platform origin only.

        Use this for platform-specific APIs (e.g. listing challenges, starting containers,
        submitting flags) that are not wrapped by other tools. Only requests to the
        same origin as the platform base URL are allowed.
        """
        return await do_http_request(
            ctx.deps.platform_base_url,
            path_or_url,
            method=method,
            headers=headers,
            query=query,
            json_body=json_body,
            body=body,
            timeout=timeout,
        )

    return toolset


async def run_pydantic_coordinator(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    no_submit: bool = False,
    coordinator_model: str | None = None,
    msg_port: int = 0,
    interactive: bool = False,
) -> dict[str, Any]:
    """Run a Pydantic AI coordinator (DeepSeek, Qwen, or compatible models)."""
    platform, cost_tracker, deps = build_deps(
        settings, model_specs, challenges_root, no_submit,
    )
    deps.msg_port = msg_port

    if getattr(settings, "platform_mode", "auto") in ("tsec", "tsecbench", "benchmark"):
        # TSec runs in fully autonomous mode, but keep a small rolling window
        # of at most 3 live range containers.
        deps.max_concurrent_challenges = min(getattr(settings, "max_concurrent_challenges", 3), 3)

    resolved_spec = coordinator_model or "deepseek/deepseek-chat"
    model = resolve_model(resolved_spec, settings)
    model_settings = resolve_model_settings(resolved_spec)

    toolset = _build_toolset(deps)

    # Coordinator memory is scoped per benchmark task: a new task gets a fresh
    # history instead of stale turns referencing a previous task's state.
    import hashlib

    token = getattr(settings, "benchmark_token", "") or getattr(settings, "platform_token", "")
    task_salt = hashlib.sha1(token.encode()).hexdigest()[:8] if token else ""
    history_path = coordinator_history_path(
        getattr(settings, "state_dir", ".ctf-state"),
        getattr(settings, "platform_mode", "auto"),
        resolved_spec,
        task_salt=task_salt,
    )
    # Keep a larger durable ledger than the slice sent back to the model.  This
    # survives process restarts while bounding context growth on each turn.
    max_persisted_turns = 100
    # Retain recent decisions without replaying a rapidly growing status/trace
    # transcript on every coordinator request.
    max_context_turns = 8
    coordinator_turns = load_coordinator_turns(history_path)[-max_persisted_turns:]
    if coordinator_turns:
        logger.info(
            "Loaded %d persisted coordinator turn(s) from %s",
            len(coordinator_turns),
            history_path,
        )

    agent: Agent[CoordinatorDeps, str] = Agent(
        model=model,
        system_prompt=COORDINATOR_PROMPT + BUMP_DISCIPLINE,
        toolsets=[toolset],
        model_settings=model_settings,
        deps_type=CoordinatorDeps,
        output_type=str,
    )

    async def turn_fn(msg: str) -> None:
        nonlocal coordinator_turns
        logger.debug("Coordinator query: %s", msg[:200])
        try:
            from pydantic_ai.usage import UsageLimits

            message_history = [
                message
                for turn in coordinator_turns[-max_context_turns:]
                for message in turn
            ]
            result = await agent.run(
                msg,
                deps=deps,
                message_history=message_history or None,
                usage_limits=UsageLimits(request_limit=10000),
            )
            new_turn = list(result.new_messages())
            if new_turn:
                coordinator_turns = [
                    *coordinator_turns,
                    new_turn,
                ][-max_persisted_turns:]
                save_coordinator_turns(history_path, coordinator_turns)
            output = result.output if hasattr(result, "output") else str(result.data)
            if output:
                print(f"\n[Coordinator] {output[:500]}")
        except Exception as e:
            logger.error("Coordinator turn failed: %s", e, exc_info=True)

    return await run_event_loop(deps, platform, cost_tracker, turn_fn, interactive=interactive)
