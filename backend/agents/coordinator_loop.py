"""Mechanical scheduler event loop.

No LLM anywhere in this file: it polls the platform, fills the concurrency
window with the highest-value unsolved challenges, enforces per-challenge and
whole-run time budgets, reaps finished swarms, and handles operator REPL
commands. Strategy is deliberately deterministic — the model's reasoning lives
inside each solver, not in an orchestrator that reasons about scheduling.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.deps import CoordinatorDeps
from backend.models import DEFAULT_MODELS
from backend.platforms import CompetitionPlatform
from backend.poller import PlatformPoller
from backend.prompts import ChallengeMeta

logger = logging.getLogger(__name__)


def remaining_global_seconds(deps: CoordinatorDeps) -> float | None:
    """Seconds left in the whole benchmark run, or None if untracked.

    None (the default, total_benchmark_budget_seconds=0) preserves the old
    behavior exactly: every per-challenge decision is made independently with
    no notion of a run-wide deadline. When a budget IS configured, callers
    use this to avoid starting work that cannot realistically finish before
    the platform's own cutoff forces it closed anyway.
    """
    import time as _time

    budget = int(getattr(deps.settings, "total_benchmark_budget_seconds", 0) or 0)
    if budget <= 0:
        return None
    elapsed = _time.monotonic() - deps.run_started_monotonic
    return max(0.0, budget - elapsed)


def _challenge_priority_key(name: str, stubs: dict[str, dict]) -> tuple[float, float, str]:
    """Sort key for the auto-spawn candidate queue.

    Highest score first, then easiest (lowest `level`) first, falling back to
    the name for determinism when both are equal or unknown. This replaces a
    plain alphabetical sort, which ignored point value entirely — across a
    title with dozens of unevenly-scored tasks sharing one wall-clock budget,
    working through them in name order has no relationship to maximizing
    total score, and the coordinator's own system prompt already claims
    "prioritizing by solve count (easy first)" that the code never actually
    implemented.
    """
    stub = stubs.get(name) or {}

    def _as_float(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    value = _as_float(stub.get("value"))
    level = _as_float(stub.get("level"))
    return (-value, level, name)

async def _stdin_reader(operator_inbox: asyncio.Queue) -> None:
    """Simple stdin REPL reader used in interactive mode."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, input, "> ")
        except (EOFError, OSError):
            # stdin can close during Ctrl+C/shutdown on Windows. Treat that as
            # a clean quit instead of leaving an unobserved task exception.
            operator_inbox.put_nowait("__quit__")
            return
        cmd = line.strip()
        if not cmd:
            continue
        if cmd == "/quit":
            operator_inbox.put_nowait("__quit__")
        elif cmd == "/status":
            operator_inbox.put_nowait("__status__")
        elif cmd.startswith("/pause "):
            operator_inbox.put_nowait(f"__pause__{cmd.removeprefix('/pause ').strip()}")
        elif cmd.startswith("/resume "):
            operator_inbox.put_nowait(f"__resume__{cmd.removeprefix('/resume ').strip()}")
        elif cmd.startswith("/kill "):
            operator_inbox.put_nowait(f"__kill__{cmd.removeprefix('/kill ').strip()}")
        else:
            # Mechanical scheduler: there is no LLM to receive free-form text.
            operator_inbox.put_nowait("__unknown__")


def build_deps(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    no_submit: bool = False,
    challenge_dirs: dict[str, str] | None = None,
    challenge_metas: dict[str, ChallengeMeta] | None = None,
) -> tuple[CompetitionPlatform, CostTracker, CoordinatorDeps]:
    """Create platform client, cost tracker, and coordinator deps."""
    platform = _create_platform(settings)
    cost_tracker = CostTracker()
    specs = model_specs or list(DEFAULT_MODELS)
    Path(challenges_root).mkdir(parents=True, exist_ok=True)

    deps = CoordinatorDeps(
        platform=platform,
        cost_tracker=cost_tracker,
        settings=settings,
        model_specs=specs,
        challenges_root=challenges_root,
        no_submit=no_submit,
        max_concurrent_challenges=getattr(settings, "max_concurrent_challenges", 10),
        challenge_time_limit_seconds=max(
            1,
            getattr(settings, "challenge_time_limit_seconds", 1800),
        ),
        challenge_dirs=challenge_dirs or {},
        challenge_metas=challenge_metas or {},
    )

    for d in Path(challenges_root).iterdir():
        meta_path = d / "metadata.yml"
        if meta_path.exists():
            try:
                meta = ChallengeMeta.from_yaml(meta_path)
            except Exception as e:
                # One malformed/truncated metadata.yml (e.g. after a crash
                # mid-write) must not brick the whole scheduler startup.
                logger.warning("Skipping unreadable metadata %s: %s", meta_path, e)
                continue
            if meta.name not in deps.challenge_dirs:
                deps.challenge_dirs[meta.name] = str(d)
                deps.challenge_metas[meta.name] = meta

    return platform, cost_tracker, deps


def _create_platform(settings: Settings) -> CompetitionPlatform:
    """Create the TSecBench platform adapter."""
    mode = getattr(settings, "platform_mode", "tsec")
    if mode in ("tsec", "tsecbench", "benchmark"):
        from backend.tsec_platform import TSecBenchmarkPlatform

        base = settings.platform_url or settings.benchmark_url
        token = settings.platform_token or settings.benchmark_token
        return TSecBenchmarkPlatform(
            base_url=base,
            token=token,
            verify_tls=bool(getattr(settings, "platform_tls_verify", True)),
        )
    raise ValueError(f"Unknown platform mode: {mode!r} (expected 'tsec')")


async def run_event_loop(
    deps: CoordinatorDeps,
    platform: CompetitionPlatform,
    cost_tracker: CostTracker,
    status_interval: int = 60,
    interactive: bool = False,
) -> dict[str, Any]:
    """Run the mechanical scheduler loop (no LLM).

    Poll → fill the window by priority → reap → refill, enforcing per-challenge
    and whole-run budgets, with a deadlock auto-resume safety net and an
    operator REPL. Exits cleanly when every challenge is solved, or on operator
    /quit or Ctrl+C.
    """
    poll_min = float(getattr(platform, "poll_interval_min_s", 5.0))
    poll_max = max(poll_min, float(getattr(platform, "poll_interval_max_s", poll_min)))
    poller = PlatformPoller(
        platform=platform,
        interval_s=(poll_min + poll_max) / 2,
        jitter_s=(poll_max - poll_min) / 2,
    )
    await poller.start()
    deps.poller = poller  # let an embedded web console read challenge/score state

    logger.info(
        "Scheduler starting: %d model(s), %d challenges, %d solved",
        len(deps.model_specs),
        len(poller.known_challenges),
        len(poller.known_solved),
    )

    repl_task: asyncio.Task | None = None
    if interactive:
        repl_task = asyncio.create_task(_stdin_reader(deps.operator_inbox), name="repl-stdin")
        print(
            "\n" + "=" * 60 + "\n"
            "Interactive REPL enabled (mechanical scheduler).\n"
            "  /status    — show current swarm status\n"
            "  /pause X   — pause challenge X\n"
            "  /resume X  — resume or re-enable a deferred challenge ('/resume all' clears all)\n"
            "  /kill X    — kill swarm for challenge X\n"
            "  /quit      — stop the scheduler\n"
            "Free-form text is ignored — there is no LLM coordinator to receive it.\n"
            + "=" * 60 + "\n"
        )

    try:
        await _auto_spawn_unsolved(deps, poller)

        last_status = asyncio.get_event_loop().time()
        deadlock_since: float | None = None

        while True:
            events = []
            # 1s (not 5s) so operator/web commands apply promptly.
            evt = await poller.get_event(timeout=1.0)
            if evt:
                events.append(evt)
            events.extend(poller.drain_events())

            for evt in events:
                if evt.kind == "challenge_solved":
                    logger.info("Solved: %s", evt.challenge_name)
                    swarm = deps.swarms.get(evt.challenge_name)
                    if swarm and not swarm.cancel_event.is_set():
                        swarm.kill("challenge_solved")
                        task = deps.swarm_tasks.get(evt.challenge_name)
                        if task and not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                        logger.info("Auto-killed solved swarm: %s", evt.challenge_name)
                elif evt.kind == "new_challenge":
                    logger.info("New challenge appeared: %s", evt.challenge_name)
                    await _auto_spawn_one(deps, evt.challenge_name)

            # Reap finished swarms and refill the freed slots.
            slot_freed = False
            for name, task in list(deps.swarm_tasks.items()):
                if task.done():
                    if name in deps.deferred_challenges:
                        deps.defer_time[name] = time.monotonic()  # start the retry cooldown
                        logger.info(
                            "Deferred: %s (%s)", name, deps.deferred_challenges[name]
                        )
                    else:
                        logger.info("Swarm finished: %s", name)
                    deps.swarm_tasks.pop(name, None)
                    deps.swarms.pop(name, None)
                    slot_freed = True
            if slot_freed:
                await _auto_spawn_unsolved(deps, poller)

            # Drain solver -> scheduler messages to the log (no LLM consumes them).
            while True:
                try:
                    solver_msg = deps.coordinator_inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                logger.info("Solver message: %s", str(solver_msg)[:300])

            # Operator REPL commands.
            while True:
                try:
                    op_msg = deps.operator_inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break

                if op_msg == "__quit__":
                    logger.info("Operator requested quit — stopping scheduler")
                    raise KeyboardInterrupt
                if op_msg == "__unknown__":
                    print("  (ignored — the scheduler is mechanical; use /status, /pause, /resume, /kill, /quit)")
                    continue
                if op_msg == "__status__":
                    _print_status(deps, poller)
                    continue
                if op_msg.startswith("__pause__"):
                    challenge_name = op_msg[len("__pause__"):]
                    swarm = deps.swarms.get(challenge_name)
                    if swarm:
                        swarm.pause()
                        print(f"  Paused: {challenge_name}")
                    else:
                        print(f"  No swarm for: {challenge_name}")
                    continue
                if op_msg.startswith("__resume__"):
                    await _handle_resume(deps, poller, op_msg[len("__resume__"):])
                    continue
                if op_msg.startswith("__kill__"):
                    await _handle_kill(deps, op_msg[len("__kill__"):])
                    continue
                if op_msg.startswith("__bump__"):
                    _handle_bump(deps, op_msg[len("__bump__"):])
                    continue

            # Periodic refill + status + deadlock auto-resume.
            now = asyncio.get_event_loop().time()
            if now - last_status >= status_interval:
                last_status = now
                await _auto_spawn_unsolved(deps, poller)
                deadlock_since = _log_status_and_handle_deadlock(
                    deps, poller, cost_tracker, deadlock_since, now
                )
                if deadlock_since is None:
                    await _auto_spawn_unsolved(deps, poller)

            # Clean termination: everything solved and nothing in flight.
            # Positive evidence required: only a SUCCESSFUL poll may declare
            # the run finished. A failed first poll (VPN down, transient 5xx)
            # used to leave known_challenges empty and exit instantly with an
            # empty result set — a false "all solved" success.
            unsolved_set = poller.known_challenges - poller.known_solved
            active_now = any(not t.done() for t in deps.swarm_tasks.values())
            if poller.last_poll_ok and not unsolved_set and not active_now and not deps.pending_spawns:
                logger.info("All challenges solved; scheduler exiting.")
                break
            if not poller.last_poll_ok and not active_now and not deps.pending_spawns:
                # Nothing is running and the platform has never answered even
                # one poll — exiting cleanly here would fake a completed run.
                logger.error(
                    "No successful platform poll yet (%d consecutive failures); "
                    "refusing to exit as if the benchmark were solved.",
                    poller.consecutive_failures,
                )
                raise RuntimeError(
                    "Platform never answered a poll — check VPN/BENCHMARK_TOKEN "
                    "and re-run. (This failure is deliberate: an empty challenge "
                    "list is not proof of completion.)"
                )

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Scheduler shutting down...")
    except RuntimeError:
        # Fatal platform/poller state — propagate so the CLI reports failure
        # with a non-zero exit instead of printing a fake "Final Results".
        raise
    except Exception as e:
        logger.error("Scheduler fatal: %s", e, exc_info=True)
    finally:
        if repl_task:
            if not repl_task.done():
                repl_task.cancel()
            try:
                await repl_task
            except (asyncio.CancelledError, Exception):
                pass
        await poller.stop()
        for swarm in deps.swarms.values():
            swarm.kill("shutdown")
        for task in deps.swarm_tasks.values():
            task.cancel()
        if deps.swarm_tasks:
            await asyncio.gather(*deps.swarm_tasks.values(), return_exceptions=True)
        cost_tracker.log_summary()
        try:
            await platform.close()
        except Exception:
            pass

    return {
        "results": deps.results,
        "total_cost_usd": cost_tracker.total_cost_usd,
        "total_tokens": cost_tracker.total_tokens,
    }


def _print_status(deps: CoordinatorDeps, poller) -> None:
    active = [n for n, t in deps.swarm_tasks.items() if not t.done()]
    preparing = sorted(deps.pending_spawns)
    solved_set = poller.known_solved
    unsolved_set = poller.known_challenges - solved_set
    print(
        f"\n  Solved: {len(solved_set)}  Unsolved: {len(unsolved_set)}  "
        f"Active swarms: {len(active)}  Preparing: {len(preparing)}  "
        f"Deferred: {len(deps.deferred_challenges)}"
    )
    for n in preparing:
        print(f"    {n}: [PREPARING]")
    for n in active:
        swarm = deps.swarms.get(n)
        if not swarm:
            continue
        st = swarm.get_status()
        winner = st.get("winner")
        agents = st.get("agents", {})
        flag_info = f" flag={winner}" if winner else ""
        pause_info = " [PAUSED]" if st.get("paused") else ""
        agent_info = (
            ", ".join(f"{a}: {d['status']}" for a, d in agents.items())
            if agents else "no agents"
        )
        print(f"    {n}:{flag_info}{pause_info} ({agent_info})")
    for n, reason in sorted(deps.deferred_challenges.items()):
        print(f"    {n}: [DEFERRED] {reason}")


async def _handle_resume(deps: CoordinatorDeps, poller, challenge_name: str) -> None:
    if challenge_name.lower() in ("all", "*"):
        names = sorted(deps.deferred_challenges)
        for n in names:
            deps.deferred_challenges.pop(n, None)
            deps.challenge_attempts.pop(n, None)  # operator wants a clean retry
            deps.defer_time.pop(n, None)
        print(f"  Re-enabled {len(names)} deferred challenges; refilling window...")
        await _auto_spawn_unsolved(deps, poller)
        return
    deps.challenge_attempts.pop(challenge_name, None)
    deps.defer_time.pop(challenge_name, None)
    was_deferred = deps.deferred_challenges.pop(challenge_name, None)
    deps.spawn_failures.pop(challenge_name, None)  # re-enable resets the failure count
    swarm = deps.swarms.get(challenge_name)
    task = deps.swarm_tasks.get(challenge_name)
    if swarm and task and not task.done() and not swarm.cancel_event.is_set():
        swarm.resume()
        print(f"  Resumed: {challenge_name}")
    elif was_deferred is not None:
        deps.swarms.pop(challenge_name, None)
        old_task = deps.swarm_tasks.pop(challenge_name, None)
        if old_task and not old_task.done():
            old_task.cancel()
            await asyncio.gather(old_task, return_exceptions=True)
        await _auto_spawn_one(deps, challenge_name)
        print(f"  Re-enabled deferred challenge: {challenge_name}")
    else:
        print(f"  No swarm for: {challenge_name}")


async def _handle_kill(deps: CoordinatorDeps, challenge_name: str) -> None:
    swarm = deps.swarms.get(challenge_name)
    if swarm:
        from backend.agents.coordinator_core import do_kill_swarm

        await do_kill_swarm(deps, challenge_name)
        deps.deferred_challenges[challenge_name] = "stopped by operator"
        print(f"  Killed: {challenge_name}")
    else:
        deps.deferred_challenges[challenge_name] = "stopped by operator"
        deps.pending_spawns.discard(challenge_name)
        print(f"  Deferred (was not running): {challenge_name}")


def _handle_bump(deps: CoordinatorDeps, payload: str) -> None:
    """Inject an operator message into one running solver (the web console's
    'talk to the agent'). Payload is JSON: {challenge, model, message}."""
    import json

    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise TypeError("payload is not an object")
        challenge = data.get("challenge")
        model = data.get("model")
        message = str(data.get("message", "")).strip()
        if not isinstance(challenge, str) or not isinstance(model, str):
            raise TypeError("challenge/model must be strings")
    except (ValueError, KeyError, TypeError) as e:
        logger.warning("Bad __bump__ payload: %s (%s)", payload[:120], e)
        return
    if not message:
        return
    swarm = deps.swarms.get(challenge)
    solver = swarm.solvers.get(model) if swarm else None
    if not solver:
        logger.warning("Bump: no live solver %s / %s", challenge, model)
        return
    solver.bump(f"[Operator guidance] {message}")
    logger.info("Operator bumped %s / %s", challenge, model)


def _log_status_and_handle_deadlock(
    deps: CoordinatorDeps, poller, cost_tracker: CostTracker,
    deadlock_since: float | None, now: float,
) -> float | None:
    """Log a status line and run the deadlock auto-resume. Returns new deadlock_since."""
    active = [n for n, t in deps.swarm_tasks.items() if not t.done()]
    preparing = sorted(deps.pending_spawns)
    solved_set = poller.known_solved
    unsolved_set = poller.known_challenges - solved_set
    budget_left = remaining_global_seconds(deps)
    budget_note = f" Budget left: {int(budget_left) // 60}min." if budget_left is not None else ""
    status_line = (
        f"STATUS: {len(solved_set)} solved, {len(unsolved_set)} unsolved, "
        f"{len(active)} active, {len(preparing)} preparing, "
        f"{len(deps.deferred_challenges)} deferred. "
        f"Cost: ${cost_tracker.total_cost_usd:.2f}{budget_note}"
    )
    # Deadlock = every UNSOLVED challenge is deferred. Stale deferred entries
    # for challenges that are now solved must not inflate the count and
    # trigger false deadlocks.
    unsolved_deferred = unsolved_set & set(deps.deferred_challenges)
    deadlock = (
        not active and not preparing
        and bool(unsolved_set)
        and len(unsolved_deferred) >= len(unsolved_set)
    )
    if not deadlock:
        logger.info(status_line)
        return None

    if deadlock_since is None:
        deadlock_since = now
    idle_s = now - deadlock_since
    auto_after = float(getattr(deps.settings, "auto_resume_idle_seconds", 240))
    if idle_s >= auto_after:
        cleared = len(deps.deferred_challenges)
        deps.deferred_challenges.clear()
        logger.warning(
            "%s Auto-resuming %d deferred challenges after %.0fs deadlock",
            status_line, cleared, idle_s,
        )
        return None
    logger.info(
        "%s NOTE: all unsolved deferred; auto-resume in %ds (or /resume all).",
        status_line, int(auto_after - idle_s),
    )
    return deadlock_since


async def _auto_spawn_one(deps: CoordinatorDeps, challenge_name: str) -> None:
    if challenge_name in deps.deferred_challenges:
        return
    if challenge_name in deps.swarms or challenge_name in deps.pending_spawns:
        return
    active = (
        sum(1 for t in deps.swarm_tasks.values() if not t.done())
        + len(deps.pending_spawns)
    )
    if active >= deps.max_concurrent_challenges:
        return
    remaining = remaining_global_seconds(deps)
    if remaining is not None:
        floor = float(getattr(deps.settings, "min_remaining_seconds_to_spawn", 300))
        if remaining < floor:
            # Run-wide budget is nearly gone: starting a new container here
            # has no realistic chance to finish before the platform cuts the
            # run off anyway, and it would just occupy a slot while already-
            # active work tries to land. Let what is active finish instead
            # of starting something new.
            logger.info(
                "Skipping spawn of %s: only %.0fs left of the total benchmark "
                "budget (floor %.0fs)",
                challenge_name, remaining, floor,
            )
            return
    try:
        from backend.agents.coordinator_core import do_spawn_swarm
        result = await do_spawn_swarm(deps, challenge_name)
        # Only a real spawn failure leaves the counter; a clean spawn (or a
        # capacity refusal that simply means "try later") clears it.
        deps.spawn_failures.pop(challenge_name, None)
        deps.challenge_attempts[challenge_name] = deps.challenge_attempts.get(challenge_name, 0) + 1
        logger.info(f"Auto-spawn {challenge_name} (attempt {deps.challenge_attempts[challenge_name]}): {result[:100]}")
    except Exception as e:
        # Consecutive spawn failures (capacity walls, dead starts) should not
        # hammer the platform every tick — defer after 2 in a row; the rolling
        # retry or /resume can re-enable it later.
        fails = deps.spawn_failures.get(challenge_name, 0) + 1
        deps.spawn_failures[challenge_name] = fails
        logger.warning(f"Auto-spawn failed for {challenge_name} ({fails}): {e}")
        if fails >= 2:
            deps.deferred_challenges[challenge_name] = f"spawn failed {fails}x: {str(e)[:120]}"
            deps.defer_time[challenge_name] = time.monotonic()
            logger.warning("[%s] deferred after repeated spawn failures", challenge_name)


def _retry_eligible(deps: CoordinatorDeps, name: str, now: float) -> bool:
    """A deferred challenge may be auto-retried when it was not held by an
    operator, still has attempts left, and its post-defer cooldown has elapsed."""
    # Operator-killed challenges stay down until an explicit /resume.
    if deps.deferred_challenges.get(name, "").startswith("stopped by operator"):
        return False
    max_attempts = int(getattr(deps.settings, "max_challenge_attempts", 3) or 0)
    if max_attempts and deps.challenge_attempts.get(name, 0) >= max_attempts:
        return False
    cooldown = float(getattr(deps.settings, "defer_retry_cooldown_seconds", 120))
    return now - deps.defer_time.get(name, 0.0) >= cooldown


async def _auto_spawn_unsolved(deps: CoordinatorDeps, poller) -> None:
    stubs = poller.known_stubs
    unsolved = sorted(
        poller.known_challenges - poller.known_solved,
        key=lambda name: _challenge_priority_key(name, stubs),
    )
    active = (
        sum(1 for t in deps.swarm_tasks.values() if not t.done())
        + len(deps.pending_spawns)
    )
    slots = max(0, deps.max_concurrent_challenges - active)
    if slots <= 0:
        return

    running = {name for name, task in deps.swarm_tasks.items() if not task.done()}

    def _busy(name: str) -> bool:
        return name in running or name in deps.pending_spawns

    # Fresh (never-deferred) challenges fill the window first, by priority.
    candidates = [n for n in unsolved if not _busy(n) and n not in deps.deferred_challenges]
    candidates = candidates[:slots]

    # Rolling retry: if slots remain and no fresh work is left, refill from
    # deferred challenges that still have attempts and whose cooldown elapsed —
    # fewest-attempts first, then priority. This un-defers them so they re-spawn
    # without needing an operator /resume.
    remaining = slots - len(candidates)
    if remaining > 0:
        now = time.monotonic()
        retryable = [
            n for n in unsolved
            if n in deps.deferred_challenges and not _busy(n) and _retry_eligible(deps, n, now)
        ]
        retryable.sort(key=lambda n: (deps.challenge_attempts.get(n, 0),
                                      _challenge_priority_key(n, stubs)))
        for n in retryable[:remaining]:
            reason = deps.deferred_challenges.pop(n, "")
            logger.info("Auto-retry deferred challenge: %s (was: %s)", n, reason)
            candidates.append(n)

    if candidates:
        await asyncio.gather(
            *(_auto_spawn_one(deps, name) for name in candidates),
        )
