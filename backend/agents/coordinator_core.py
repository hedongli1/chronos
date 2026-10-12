"""Shared coordinator tool logic — called by both Claude SDK and Codex coordinators."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

from backend.deps import CoordinatorDeps
from backend.prompts import ChallengeMeta
from backend.solver_base import FLAG_FOUND

logger = logging.getLogger(__name__)

# A swarm that finishes below this many steps almost certainly hit a startup
# problem (dead container, transient API error) rather than a real solve attempt.
EARLY_EXIT_MIN_STEPS = 15

# Appended to every coordinator system prompt. Bumps that assert invented
# "facts" (endpoints, services, files, credentials, strings) with authority have
# repeatedly sent solvers chasing phantoms, so composition is restricted to
# cited evidence plus clearly-labeled hypotheses.
BUMP_DISCIPLINE = """\

BUMP DISCIPLINE (applies to bump_agent, broadcast, and ANY message you send to solvers):
- Restate ONLY evidence you actually saw in read_solver_trace output or another
  tool result this session. Quote or cite it (e.g. "step 42 RESULT nmap: ...").
- NEVER assert the existence of an endpoint, service, file, credential, or
  string you did not see in a tool result. No invented "AUTHORITATIVE FIX" claims.
- Hypotheses are allowed only when clearly labeled as unverified — write
  "HYPOTHESIS (unverified) / 假设（未验证）: ..." — and paired with ONE concrete,
  cheap verification step the solver can run first (e.g. "curl -s http://target/check").
- Local-disk artifacts are unreliable evidence: solvers share one runner disk,
  so files (web_source.txt, routes*.py, bundle.js, ...) may belong to a DIFFERENT
  challenge. Never tell a solver to trust a file unless its own trace shows the
  file was produced while working on THIS challenge.
- If an earlier bump of yours was wrong, say so explicitly and briefly first
  ("Correction: my earlier claim about X was wrong — disregard it"), then give
  the new guidance.
- Keep bumps short and actionable: cited evidence, then the hypothesis with its
  verification step, then the next action.
"""


def _tail_lines(path: str, count: int) -> list[str]:
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        return list(deque(stream, maxlen=max(1, count)))


async def do_fetch_challenges(deps: CoordinatorDeps) -> str:
    challenges = await deps.platform.fetch_all_challenges()
    solved = await deps.platform.fetch_solved_names()
    result = [
        {
            "name": ch.get("name", "?"),
            "category": ch.get("category", "?"),
            "value": ch.get("value", 0),
            "solves": ch.get("solves", 0),
            "status": "SOLVED" if ch.get("name") in solved else "unsolved",
            "description": (ch.get("description") or "")[:200],
        }
        for ch in challenges
    ]
    return json.dumps(result, indent=2)


async def do_get_solve_status(deps: CoordinatorDeps) -> str:
    solved = await deps.platform.fetch_solved_names()
    swarm_status = {name: swarm.get_status() for name, swarm in deps.swarms.items()}
    return json.dumps(
        {
            "solved": sorted(solved),
            "active_swarms": swarm_status,
            "pending_spawns": sorted(deps.pending_spawns),
            "deferred_challenges": deps.deferred_challenges,
        },
        indent=2,
    )


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
            # Early-exit retry: a swarm that dies after only a handful of steps
            # (container not ready, transient API error, ...) gets one automatic
            # re-pull + restart instead of being deferred for the whole run.
            steps = result.step_count if result else 0
            if steps < EARLY_EXIT_MIN_STEPS and challenge_name not in deps.early_retried:
                deps.early_retried.add(challenge_name)
                logger.warning(
                    "[%s] Early exit after %d steps — will retry once automatically",
                    challenge_name, steps,
                )
                deps.coordinator_inbox.put_nowait(
                    f"EARLY EXIT: '{challenge_name}' ended after only {steps} steps without "
                    "a result (likely a transient startup problem). It has been re-queued "
                    "automatically — the scheduler will retry it once with a fresh container."
                )
            else:
                deps.deferred_challenges[challenge_name] = "solver attempts exhausted"

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


async def do_check_swarm_status(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    return json.dumps(swarm.get_status(), indent=2)


async def do_submit_flag(deps: CoordinatorDeps, challenge_name: str, flag: str) -> str:
    if deps.no_submit:
        return f'DRY RUN — would submit "{flag.strip()}" for {challenge_name}'
    try:
        result = await deps.platform.submit_flag(challenge_name, flag)
        return result.display
    except Exception as e:
        return f"submit_flag error: {e}"


async def do_kill_swarm(deps: CoordinatorDeps, challenge_name: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    swarm.kill("operator")
    deps.deferred_challenges[challenge_name] = "stopped by coordinator"
    task = deps.swarm_tasks.get(challenge_name)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return f"Swarm for {challenge_name} cancelled"


async def do_bump_agent(deps: CoordinatorDeps, challenge_name: str, model_spec: str, insights: str) -> str:
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec} in {challenge_name}"
    solver.bump(insights)
    return f"Bumped {model_spec} on {challenge_name}"


async def do_read_solver_trace(deps: CoordinatorDeps, challenge_name: str, model_spec: str, last_n: int = 20) -> str:
    """Read the last N trace events from a solver's JSONL log."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm for {challenge_name}"
    solver = swarm.solvers.get(model_spec)
    if not solver:
        return f"No solver for {model_spec}"
    trace_path = getattr(solver, "tracer", None)
    if not trace_path:
        return "No tracer on solver"
    path = trace_path.path if hasattr(trace_path, "path") else str(trace_path)
    try:
        recent = await asyncio.to_thread(_tail_lines, path, last_n)
        summary = []
        for line in recent:
            try:
                d = json.loads(line)
                t = d.get("type", "?")
                if t == "tool_call":
                    args_str = str(d.get("args", ""))[:100]
                    summary.append(f"step {d.get('step','?')} CALL {d.get('tool','?')}: {args_str}")
                elif t == "tool_result":
                    result_str = str(d.get("result", ""))[:100]
                    summary.append(f"step {d.get('step','?')} RESULT {d.get('tool','?')}: {result_str}")
                elif t in ("finish", "error", "bump", "turn_failed"):
                    summary.append(f"** {t}: {json.dumps({k:v for k,v in d.items() if k != 'ts'})}")
                elif t == "usage":
                    summary.append(f"usage: in={d.get('input_tokens',0)} out={d.get('output_tokens',0)} cost=${d.get('cost_usd',0):.4f}")
                else:
                    summary.append(f"{t}: {str(d)[:80]}")
            except Exception:
                summary.append(line[:100])
        return "\n".join(summary)
    except FileNotFoundError:
        return f"Trace file not found: {path}"
    except Exception as e:
        return f"Error reading trace: {e}"


async def do_broadcast(deps: CoordinatorDeps, challenge_name: str, message: str) -> str:
    """Broadcast a message to all solvers working on a challenge."""
    swarm = deps.swarms.get(challenge_name)
    if not swarm:
        return f"No swarm running for {challenge_name}"
    await swarm.message_bus.broadcast(message)
    return f"Broadcast to all solvers on {challenge_name}"
