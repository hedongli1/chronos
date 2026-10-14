"""Mechanical scheduler helpers: spawn, run-with-budget, lifecycle, kill.

No LLM-facing tool wrappers live here anymore — the scheduler calls these
functions directly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from backend.deps import CoordinatorDeps
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND

logger = logging.getLogger(__name__)

# A swarm that finishes below this many steps almost certainly hit a startup
# problem (dead container, transient API error) rather than a real solve attempt.
EARLY_EXIT_MIN_STEPS = 15


async def do_spawn_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    """Reserve capacity atomically, then prepare the target without serializing peers."""
    async with deps.spawn_lock:
        finished = [
            name
            for name, task in deps.swarm_tasks.items()
            if task.done()
        ]
        for name in finished:
            deps.swarms.pop(name, None)
            deps.swarm_tasks.pop(name, None)

        if challenge_name in deps.deferred_challenges:
            reason = deps.deferred_challenges[challenge_name]
            return (
                f"Challenge '{challenge_name}' is deferred ({reason}). "
                f"Only the operator can re-enable it: /resume {challenge_name} "
                "(or /resume all to clear the whole deferred list)."
            )
        if challenge_name in deps.swarms:
            return f"Swarm still running for {challenge_name}"
        if challenge_name in deps.pending_spawns:
            return f"Swarm is already being prepared for {challenge_name}"

        # Capacity is gated on live range containers, not just running swarms:
        # a deferred (unsolved) challenge keeps its container and still occupies
        # a slot, so spawning on top of it would exceed the platform limit.
        active_count = len(deps.open_containers) + len(deps.pending_spawns)
        if active_count >= deps.max_concurrent_challenges:
            return (
                f"At capacity ({active_count}/{deps.max_concurrent_challenges} "
                "range containers active or preparing). Wait for one to be solved."
            )
        deps.pending_spawns.add(challenge_name)

    try:
        return await _start_reserved_swarm(deps, challenge_name)
    finally:
        async with deps.spawn_lock:
            deps.pending_spawns.discard(challenge_name)


async def _start_reserved_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    # (Re)pull unless this challenge's container is still open in a running
    # swarm: a deferred challenge's container was closed on timeout, so stale
    # metadata would point at a dead address — pull fresh and start a new one.
    if challenge_name not in deps.open_containers:
        try:
            challenges = await deps.platform.fetch_all_challenges()
            ch_data = next((c for c in challenges if c.get("name") == challenge_name), None)
            if not ch_data:
                return f"Challenge '{challenge_name}' not found on platform"
            output_dir = str(Path(deps.challenges_root))
            ch_dir = await deps.platform.pull_challenge(ch_data, output_dir)
            deps.challenge_dirs[challenge_name] = ch_dir
            deps.challenge_metas[challenge_name] = ChallengeMeta.from_yaml(Path(ch_dir) / "metadata.yml")
        except Exception as e:
            # Capacity/start failures often mean the platform holds ghost
            # containers (crashed runs, failed closes). Reap them, then let the
            # scheduler retry this spawn at the next tick.
            reaper = getattr(deps.platform, "close_stale_containers", None)
            if callable(reaper) and time.monotonic() - deps.last_ghost_reap > 120:
                deps.last_ghost_reap = time.monotonic()
                keep = (
                    {n for n, t in deps.swarm_tasks.items() if not t.done()}
                    | set(deps.pending_spawns)
                    | {challenge_name}
                )
                try:
                    reaped = await reaper(keep)
                    if reaped:
                        logger.warning("Reaped ghost containers: %s", reaped)
                        deps.coordinator_inbox.put_nowait(
                            f"GHOST REAP: closed stale platform containers {reaped} after "
                            f"spawn of '{challenge_name}' failed ({e}). It will be retried."
                        )
                except Exception as re:
                    logger.warning("Ghost reap failed: %s", re)
            raise

    if deps.challenge_metas[challenge_name].connection_info:
        deps.open_containers.add(challenge_name)

    from backend.agents.coordinator_loop import remaining_global_seconds
    from backend.agents.swarm import ChallengeSwarm

    swarm = ChallengeSwarm(
        challenge_dir=deps.challenge_dirs[challenge_name],
        meta=deps.challenge_metas[challenge_name],
        platform=deps.platform,
        cost_tracker=deps.cost_tracker,
        settings=deps.settings,
        model_specs=deps.model_specs,
        no_submit=deps.no_submit,
        coordinator_inbox=deps.coordinator_inbox,
        remaining_global_seconds=remaining_global_seconds(deps),
    )
    async def _run_and_cleanup() -> None:
        result = await _run_swarm_with_time_limit(deps, challenge_name, swarm)
        # Flag already submitted/confirmed by solver's submit_fn — just record the result
        if result and result.status == FLAG_FOUND:
            deps.results[challenge_name] = {
                "flag": result.flag,
                "submit": "DRY RUN" if deps.no_submit else "confirmed by solver",
            }
        elif challenge_name not in deps.deferred_challenges:
            # Not solved → defer it. The scheduler's rolling retry re-opens
            # deferred challenges only after a cooldown and only when the window
            # has spare capacity, so a just-failed challenge is NOT re-opened
            # immediately — that would burn a container start + a full model run
            # for nothing. A fast early-exit (likely a transient startup problem)
            # is labeled as such but follows the same cooldown-gated retry.
            steps = result.step_count if result else 0
            note = (
                f"early exit ({steps} steps)"
                if steps < EARLY_EXIT_MIN_STEPS
                else "solver attempts exhausted"
            )
            deps.deferred_challenges[challenge_name] = note
            logger.info(
                "[%s] %s — deferred; rolling retry re-opens it after the cooldown",
                challenge_name, note,
            )

    async with deps.spawn_lock:
        deps.swarms[challenge_name] = swarm
        task = asyncio.create_task(_run_and_cleanup(), name=f"swarm-{challenge_name}")
        deps.swarm_tasks[challenge_name] = task
    return f"Swarm spawned for {challenge_name} with {len(deps.model_specs)} models"


async def _run_swarm_with_time_limit(deps: CoordinatorDeps, challenge_name: str, swarm):
    """Run one swarm with the configured wall-clock budget."""
    from backend.agents.coordinator_loop import remaining_global_seconds
    from backend.agents.swarm import _time_limit_for_challenge

    limit = _time_limit_for_challenge(
        deps.settings,
        deps.challenge_metas.get(challenge_name),
        remaining_global_seconds(deps),
    )
    # asyncio.wait (unlike wait_for) never cancels the swarm task on timeout, so
    # kill() records the cancel reason BEFORE solvers write their finish events.
    run_task = asyncio.create_task(swarm.run(), name=f"swarm-run-{challenge_name}")
    try:
        done, _ = await asyncio.wait({run_task}, timeout=limit)
        if run_task in done:
            return run_task.result()
        swarm.kill("wall_clock")
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        reason = f"time limit reached ({limit}s)"
        deps.deferred_challenges[challenge_name] = reason
        logger.warning("[%s] %s; moving to another challenge", challenge_name, reason)
        deps.coordinator_inbox.put_nowait(
            f"TIME LIMIT: '{challenge_name}' ran for {limit}s without a confirmed flag. "
            "It has been stopped and deferred for this run; move to another unsolved challenge."
        )
        return None
    except asyncio.CancelledError:
        # External kill (operator / coordinator shutdown): propagate into the swarm.
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
        raise
    finally:
        # Solved or not, once the swarm ends the container is closed so the
        # concurrency slot is freed for the next challenge. Unsolved timeouts
        # are deferred and can be re-enabled later with /resume.
        await _recover_challenge_environment(deps, challenge_name)


async def _recover_challenge_environment(deps: CoordinatorDeps, challenge_name: str) -> None:
    """Release the platform container owned by a finished swarm (TSec close).

    The local slot is freed only when the platform confirms the close — a failed
    close means the container still occupies a platform slot, and pretending
    otherwise causes max-active rejections on later starts.
    """
    release = getattr(deps.platform, "release_challenge", None)
    if callable(release):
        try:
            released = await release(challenge_name)
        except Exception as e:
            logger.warning("Could not release challenge environment for %s: %s", challenge_name, e)
            released = False
        if released is False:
            logger.warning(
                "Container for %s may still occupy a platform slot; keeping it counted",
                challenge_name,
            )
            return
    deps.open_containers.discard(challenge_name)


async def do_kill_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    swarm.kill("operator")
    deps.deferred_challenges[challenge_name] = "stopped by operator"
    task = deps.swarm_tasks.get(challenge_name)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return f"Swarm for {challenge_name} cancelled"
