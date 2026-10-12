"""Shared coordinator event loop — used by both Claude SDK and Codex coordinators."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
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

# Callable type for a coordinator turn: (message) -> None
TurnFn = Callable[[str], Coroutine[Any, Any, None]]


class _DummyServer:
    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


async def _start_msg_server(operator_inbox: asyncio.Queue, port: int) -> _DummyServer:
    """Start the operator message HTTP endpoint.

    The full HTTP server is optional for local runs; when no explicit port is
    required, we return a dummy server so the coordinator loop remains functional.
    """
    _ = operator_inbox, port
    return _DummyServer()


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
            operator_inbox.put_nowait(cmd)


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
        platform_base_url=getattr(settings, "platform_url", ""),
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
            meta = ChallengeMeta.from_yaml(meta_path)
            if meta.name not in deps.challenge_dirs:
                deps.challenge_dirs[meta.name] = str(d)
                deps.challenge_metas[meta.name] = meta

    return platform, cost_tracker, deps


def _create_platform(settings: Settings) -> CompetitionPlatform:
    """Create the appropriate platform adapter based on settings."""
    mode = getattr(settings, "platform_mode", "auto")
    if mode == "local":
        from backend.local_platform import LocalPlatform
        return LocalPlatform(challenges_root="challenges")
    if mode in ("tsec", "tsecbench", "benchmark"):
        from backend.tsec_platform import TSecBenchmarkPlatform
        base = settings.platform_url or settings.benchmark_url
        token = settings.platform_token or settings.benchmark_token
        return TSecBenchmarkPlatform(base_url=base, token=token)
    raise ValueError(f"Unknown platform mode: {mode!r} (expected 'tsec' or 'local')")


async def run_event_loop(
    deps: CoordinatorDeps,
    platform: CompetitionPlatform,
    cost_tracker: CostTracker,
    turn_fn: TurnFn,
    status_interval: int = 60,
    interactive: bool = False,
) -> dict[str, Any]:
    """Run the shared coordinator event loop."""
    poll_min = float(getattr(platform, "poll_interval_min_s", 5.0))
    poll_max = max(
        poll_min,
        float(getattr(platform, "poll_interval_max_s", poll_min)),
    )
    poller = PlatformPoller(
        platform=platform,
        interval_s=(poll_min + poll_max) / 2,
        jitter_s=(poll_max - poll_min) / 2,
    )
    await poller.start()

    msg_server = await _start_msg_server(deps.operator_inbox, deps.msg_port)
    logger.info(
        "Coordinator starting: %d models, %d challenges, %d solved",
        len(deps.model_specs),
        len(poller.known_challenges),
        len(poller.known_solved),
    )

    repl_task: asyncio.Task | None = None
    if interactive:
        repl_task = asyncio.create_task(_stdin_reader(deps.operator_inbox), name="repl-stdin")
        console_msg = (
            "\n" + "=" * 60 + "\n"
            "Interactive REPL enabled. Type a message and press Enter.\n"
            "  /status    — show current swarm status\n"
            "  /pause X   — pause challenge X\n"
            "  /resume X  — resume or re-enable a deferred challenge ('/resume all' clears the whole list)\n"
            "  /kill X    — kill swarm for challenge X\n"
            "  /quit      — stop coordinator\n"
            "Anything else is sent to the coordinator as a prompt for the coordinator.\n"
            + "=" * 60 + "\n"
        )
        print(console_msg)

    unsolved = poller.known_challenges - poller.known_solved
    initial_msg = (
        f"Benchmark is LIVE. {len(poller.known_challenges)} targets, "
        f"{len(poller.known_solved)} solved.\n"
        f"Unsolved: {sorted(unsolved) if unsolved else 'NONE'}\n"
        f"Each challenge has a hard {deps.challenge_time_limit_seconds}s wall-clock budget. "
        "Fetch challenges and spawn swarms for unsolved, non-deferred challenges."
    )
    if hasattr(platform, "fetch_poll_state"):
        initial_msg += (
            "\nBenchmark notes: tasks may need MULTIPLE flags — a task only counts as "
            "solved when every flag is accepted (partial credit applies), so keep solvers "
            "running after a partial acceptance. Platform hints DEDUCT score: request one "
            "only for a task that is clearly stuck. Prefer high-score, low-difficulty "
            "tasks first to maximize total points within the time budget."
        )

    try:
        await turn_fn(initial_msg)
        await _auto_spawn_unsolved(deps, poller)

        last_status = asyncio.get_event_loop().time()
        deadlock_since: float | None = None

        while True:
            events = []
            evt = await poller.get_event(timeout=5.0)
            if evt:
                events.append(evt)
            events.extend(poller.drain_events())

            for evt in events:
                if evt.kind == "challenge_solved" and evt.challenge_name in deps.swarms:
                    swarm = deps.swarms[evt.challenge_name]
                    if not swarm.cancel_event.is_set():
                        swarm.kill("challenge_solved")
                        task = deps.swarm_tasks.get(evt.challenge_name)
                        if task and not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                        logger.info("Auto-killed swarm for: %s", evt.challenge_name)

            parts: list[str] = []
            for evt in events:
                if evt.kind == "new_challenge":
                    parts.append(f"NEW CHALLENGE: '{evt.challenge_name}' appeared. Spawn a swarm.")
                    await _auto_spawn_one(deps, evt.challenge_name)
                elif evt.kind == "challenge_solved":
                    parts.append(f"SOLVED: '{evt.challenge_name}' — swarm auto-killed.")

            slot_freed = False
            for name, task in list(deps.swarm_tasks.items()):
                if task.done():
                    if name in deps.deferred_challenges:
                        parts.append(
                            f"DEFERRED: '{name}' reached its time budget and was stopped. "
                            "Select another unsolved challenge."
                        )
                    else:
                        parts.append(f"SOLVER FINISHED: Swarm for '{name}' completed. Check results or retry.")
                    deps.swarm_tasks.pop(name, None)
                    deps.swarms.pop(name, None)
                    slot_freed = True

            if slot_freed:
                await _auto_spawn_unsolved(deps, poller)

            while True:
                try:
                    solver_msg = deps.coordinator_inbox.get_nowait()
                    parts.append(f"SOLVER MESSAGE: {solver_msg}")
                except asyncio.QueueEmpty:
                    break

            while True:
                try:
                    op_msg = deps.operator_inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break

                if op_msg == "__quit__":
                    logger.info("Operator requested quit — stopping coordinator")
                    raise KeyboardInterrupt
                elif op_msg == "__status__":
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
                        if swarm:
                            st = swarm.get_status()
                            winner = st.get("winner")
                            paused = st.get("paused")
                            agents = st.get("agents", {})
                            flag_info = f" flag={winner}" if winner else ""
                            pause_info = " [PAUSED]" if paused else ""
                            agent_info = ", ".join(f"{a}: {d['status']}" for a, d in agents.items()) if agents else "no agents"
                            print(f"    {n}:{flag_info}{pause_info} ({agent_info})")
                    for n, reason in sorted(deps.deferred_challenges.items()):
                        print(f"    {n}: [DEFERRED] {reason}")
                    continue
                elif op_msg.startswith("__pause__"):
                    challenge_name = op_msg[len("__pause__"):]
                    swarm = deps.swarms.get(challenge_name)
                    if swarm:
                        swarm.pause()
                        print(f"  Paused: {challenge_name}")
                    else:
                        print(f"  No swarm for: {challenge_name}")
                    continue
                elif op_msg.startswith("__resume__"):
                    challenge_name = op_msg[len("__resume__"):]
                    if challenge_name.lower() in ("all", "*"):
                        # Bulk un-defer: clear the whole deferred list and let
                        # the capacity gate refill the window automatically.
                        names = sorted(deps.deferred_challenges)
                        for n in names:
                            deps.deferred_challenges.pop(n, None)
                        print(f"  Re-enabled {len(names)} deferred challenges; refilling window...")
                        await _auto_spawn_unsolved(deps, poller)
                        continue
                    was_deferred = deps.deferred_challenges.pop(challenge_name, None)
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
                    continue
                elif op_msg.startswith("__kill__"):
                    challenge_name = op_msg[len("__kill__"):]
                    swarm = deps.swarms.get(challenge_name)
                    if swarm:
                        from backend.agents.coordinator_core import do_kill_swarm

                        await do_kill_swarm(deps, challenge_name)
                        deps.deferred_challenges[challenge_name] = "stopped by operator"
                        print(f"  Killed: {challenge_name}")
                    else:
                        # No live swarm (e.g. stuck in a spawn-retry loop):
                        # defer it anyway so auto-spawn stops picking it up.
                        deps.deferred_challenges[challenge_name] = "stopped by operator"
                        deps.pending_spawns.discard(challenge_name)
                        print(f"  Deferred (was not running): {challenge_name}")
                    continue

                parts.append(f"OPERATOR PROMPT: {op_msg}")
                logger.info("Operator prompt: %s", op_msg[:200])

            now = asyncio.get_event_loop().time()
            if now - last_status >= status_interval:
                last_status = now
                # Retry challenges that were not ready or whose solver stopped
                # after a transient network outage.
                await _auto_spawn_unsolved(deps, poller)
                active = [n for n, t in deps.swarm_tasks.items() if not t.done()]
                preparing = sorted(deps.pending_spawns)
                solved_set = poller.known_solved
                unsolved_set = poller.known_challenges - solved_set
                status_line = (
                    f"STATUS: {len(solved_set)} solved, {len(unsolved_set)} unsolved, "
                    f"{len(active)} active swarms, {len(preparing)} preparing, "
                    f"{len(deps.deferred_challenges)} deferred. "
                    f"Cost: ${cost_tracker.total_cost_usd:.2f}"
                )
                deadlock = (
                    not active
                    and not preparing
                    and bool(unsolved_set)
                    and len(deps.deferred_challenges) >= len(unsolved_set)
                )
                if deadlock:
                    if deadlock_since is None:
                        deadlock_since = now
                    idle_s = now - deadlock_since
                    auto_after = float(getattr(deps.settings, "auto_resume_idle_seconds", 240))
                    if idle_s >= auto_after:
                        cleared = len(deps.deferred_challenges)
                        deps.deferred_challenges.clear()
                        deadlock_since = None
                        logger.warning(
                            "Auto-resuming %d deferred challenges after %.0fs deadlock",
                            cleared, idle_s,
                        )
                        parts.append(
                            f"AUTO-RESUME: every unsolved challenge was deferred and nothing "
                            f"was running for {int(idle_s)}s, so the deferred list "
                            f"({cleared} challenges) was cleared automatically. Refill the "
                            "window and keep working."
                        )
                        await _auto_spawn_unsolved(deps, poller)
                        active = [n for n, t in deps.swarm_tasks.items() if not t.done()]
                        preparing = sorted(deps.pending_spawns)
                        status_line = (
                            f"STATUS: {len(solved_set)} solved, {len(unsolved_set)} unsolved, "
                            f"{len(active)} active swarms, {len(preparing)} preparing, "
                            f"0 deferred (auto-resumed). Cost: ${cost_tracker.total_cost_usd:.2f}"
                        )
                    else:
                        status_line += (
                            f" NOTE: every unsolved challenge is deferred; auto-resume in "
                            f"{int(auto_after - idle_s)}s (or operator: /resume all)."
                        )
                else:
                    deadlock_since = None
                if active or parts:
                    parts.append(status_line)
                else:
                    logger.info(f"Event -> coordinator: {status_line}")

            if parts:
                operator_prompts = [p.removeprefix("OPERATOR PROMPT: ") for p in parts if p.startswith("OPERATOR PROMPT:")]
                system_parts = [p for p in parts if not p.startswith("OPERATOR PROMPT:")]
                if system_parts:
                    msg = "\n\n".join(system_parts)
                    logger.info("Event -> coordinator: %s", msg[:200])
                    await turn_fn(msg)
                for prompt in operator_prompts:
                    logger.info("Operator prompt -> coordinator: %s", prompt[:200])
                    await turn_fn(f"OPERATOR REQUEST: {prompt}")

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Coordinator shutting down...")
    except Exception as e:
        logger.error("Coordinator fatal: %s", e, exc_info=True)
    finally:
        if repl_task:
            if not repl_task.done():
                repl_task.cancel()
            try:
                await repl_task
            except (asyncio.CancelledError, Exception):
                pass
        if msg_server:
            msg_server.close()
            await msg_server.wait_closed()
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
    try:
        from backend.agents.coordinator_core import do_spawn_swarm
        result = await do_spawn_swarm(deps, challenge_name)
        deps.spawn_failures.pop(challenge_name, None)
        logger.info(f"Auto-spawn {challenge_name}: {result[:100]}")
    except Exception as e:
        # Consecutive spawn failures (capacity walls, dead starts) should not
        # hammer the platform every tick — defer after 2 in a row; /resume or
        # the auto-resume cycle can re-enable it later.
        fails = deps.spawn_failures.get(challenge_name, 0) + 1
        deps.spawn_failures[challenge_name] = fails
        logger.warning(f"Auto-spawn failed for {challenge_name} ({fails}): {e}")
        if fails >= 2:
            deps.deferred_challenges[challenge_name] = f"spawn failed {fails}x: {str(e)[:120]}"
            logger.warning("[%s] deferred after repeated spawn failures", challenge_name)


async def _auto_spawn_unsolved(deps: CoordinatorDeps, poller) -> None:
    unsolved = sorted(poller.known_challenges - poller.known_solved)
    active = (
        sum(1 for t in deps.swarm_tasks.values() if not t.done())
        + len(deps.pending_spawns)
    )
    slots = max(0, deps.max_concurrent_challenges - active)
    if slots <= 0:
        return

    running = {name for name, task in deps.swarm_tasks.items() if not task.done()}
    candidates: list[str] = []
    for name in unsolved:
        if len(candidates) >= slots:
            break
        if name in running:
            continue
        if name in deps.pending_spawns:
            continue
        if name in deps.deferred_challenges:
            continue
        candidates.append(name)

    if candidates:
        await asyncio.gather(
            *(_auto_spawn_one(deps, name) for name in candidates),
        )
