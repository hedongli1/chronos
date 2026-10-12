"""ChallengeSwarm — Parallel solvers racing on one challenge."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from backend.agents.solver import Solver
from backend.checkpoints import (
    build_resume_context,
    refresh_checkpoint_from_latest_trace,
    save_solver_checkpoint,
    solver_checkpoint_path,
    solver_workspace_path,
)
from backend.cost_tracker import CostTracker
from backend.message_bus import ChallengeMessageBus
from backend.models import DEFAULT_MODELS, model_id_from_spec, provider_from_spec
from backend.platforms import CompetitionPlatform
from backend.prompts import ChallengeMeta
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    QUOTA_ERROR,
    SolverProtocol,
    SolverResult,
)

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)


# Quota fallback: map subscription-backed providers to API-backed equivalents
QUOTA_FALLBACK: dict[str, str] = {
    "claude-sdk/claude-opus-4-6": "bedrock/us.anthropic.claude-opus-4-6-v1",
    "codex/gpt-5.4": "azure/gpt-5.4",
    "codex/gpt-5.4-mini": "azure/gpt-5.4-mini",
    "codex/gpt-5.3-codex-spark": "zen/gpt-5.3-codex-spark",
}


def _quota_fallback_spec(model_spec: str) -> str | None:
    return QUOTA_FALLBACK.get(model_spec)


class CancelEvent(asyncio.Event):
    """asyncio.Event that also records WHY cancellation was requested.

    First write wins: the originating reason (circuit_breaker, wall_clock, ...)
    is more useful than later cascading set() calls. Solvers read `reason` for
    the cancel_reason field of their finish events.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reason = ""

    def cancel_with_reason(self, reason: str) -> None:
        if not self.is_set():
            self.reason = reason
        self.set()


SANDBOX_EXEMPT_PROVIDERS = {"claude-sdk", "codex"}  # solvers hard-wired to the Docker sandbox


def _resolve_executor_mode(settings) -> str:
    """docker | headless. 'auto': benchmark platforms default to headless."""
    mode = str(getattr(settings, "executor_mode", "auto") or "auto").strip().lower()
    if mode in ("docker", "headless"):
        return mode
    platform_mode = str(getattr(settings, "platform_mode", "") or "").lower()
    if platform_mode in ("tsec", "tsecbench", "benchmark"):
        return "headless"
    return "docker"


def _time_limit_for_challenge(settings, meta) -> int:
    """Per-challenge wall-clock budget; killchain tasks get a longer one."""
    limit = int(getattr(settings, "challenge_time_limit_seconds", 1800))
    if meta is not None:
        from backend.prompts import _guess_scenario

        if _guess_scenario(meta) == "killchain":
            limit = max(limit, int(getattr(settings, "killchain_time_limit_seconds", 2700)))
    return limit


@dataclass
class ChallengeSwarm:
    """Parallel solvers racing on one challenge."""

    challenge_dir: str
    meta: ChallengeMeta
    platform: CompetitionPlatform
    cost_tracker: CostTracker
    settings: Settings
    model_specs: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    no_submit: bool = False
    coordinator_inbox: asyncio.Queue | None = None

    cancel_event: CancelEvent = field(default_factory=CancelEvent)
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    solvers: dict[str, SolverProtocol] = field(default_factory=dict)
    findings: dict[str, str] = field(default_factory=dict)
    winner: SolverResult | None = None
    confirmed_flag: str | None = None
    _flag_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _confirmed_flags: set[str] = field(default_factory=set)  # platform-confirmed (normalized); challenge complete
    _accepted_flags: set[str] = field(default_factory=set)  # platform-accepted incl. multi-flag partials
    _submit_count: dict[str, int] = field(default_factory=dict)  # per-model wrong submission count
    _submitted_flags: set[str] = field(default_factory=set)  # dedup exact flags
    _last_submit_time: dict[str, float] = field(default_factory=dict)  # per-model last submit timestamp
    _wrong_total: int = 0  # wrong submissions across ALL models on this challenge
    _submit_locked: bool = False  # circuit breaker: too many wrong guesses
    _last_progress_monotonic: float = 0.0  # last accepted (partial or full) flag
    message_bus: ChallengeMessageBus = field(default_factory=ChallengeMessageBus)

    def _state_paths(self, model_spec: str):
        state_dir = getattr(self.settings, "state_dir", ".ctf-state")
        return (
            solver_checkpoint_path(state_dir, self.meta.name, model_spec),
            solver_workspace_path(state_dir, self.meta.name, model_spec).resolve(),
        )

    def _prepare_solver_state(self, model_spec: str) -> tuple[str, str]:
        """Recover an interrupted trace and build the model's compact resume context."""
        checkpoint_path, workspace_path = self._state_paths(model_spec)
        checkpoint = refresh_checkpoint_from_latest_trace(
            checkpoint_path,
            challenge_name=self.meta.name,
            model_spec=model_spec,
            model_id=model_id_from_spec(model_spec),
            current_trace="",
            workspace_dir=str(workspace_path),
        )
        return str(workspace_path), build_resume_context(checkpoint)

    @staticmethod
    def _next_step_for_result(result: SolverResult | None) -> str:
        if result is None:
            return "Inspect the restored workspace and checkpoint, then continue the most promising unfinished analysis."
        if result.status == FLAG_FOUND:
            return "Flag confirmed; no further solver work is required."
        if result.flag:
            return f"Verify the unconfirmed candidate {result.flag} against the challenge, then submit only if justified."
        if result.status == ERROR:
            return "Recover from the recorded error without repeating completed setup or failed commands."
        if result.status == QUOTA_ERROR:
            return "Resume the same analysis with the configured fallback model and reuse all workspace artifacts."
        if result.findings_summary:
            return f"Continue from the latest findings and try a materially different approach: {result.findings_summary[:1200]}"
        return "Review prior key results and try a materially different, evidence-driven approach."

    async def _persist_solver_state(
        self,
        solver: SolverProtocol,
        state_model_spec: str,
        result: SolverResult | None = None,
        *,
        next_step: str = "",
        sync_workspace: bool = True,
    ) -> None:
        """Snapshot artifacts and merge the latest trace into the durable checkpoint."""
        checkpoint_path, workspace_path = self._state_paths(state_model_spec)
        sandbox = getattr(solver, "sandbox", None)
        persist_workspace = getattr(sandbox, "persist_workspace", None)
        if sync_workspace and callable(persist_workspace):
            await persist_workspace()

        tracer = getattr(solver, "tracer", None)
        trace_path = str(getattr(tracer, "path", ""))
        step_counter = getattr(solver, "_step_count", 0)
        if isinstance(step_counter, list):
            step_counter = step_counter[0] if step_counter else 0
        step_count = result.step_count if result else int(step_counter or 0)
        findings = result.findings_summary if result else str(getattr(solver, "_findings", ""))
        flag = result.flag if result else getattr(solver, "_flag", None)
        confirmed = bool(getattr(solver, "_confirmed", False))

        try:
            save_solver_checkpoint(
                checkpoint_path,
                challenge_name=self.meta.name,
                model_spec=state_model_spec,
                trace_path=trace_path,
                step_count=step_count,
                findings_summary=findings,
                flag=flag,
                confirmed=confirmed,
                workspace_dir=str(workspace_path),
                next_step=next_step or self._next_step_for_result(result),
            )
        except Exception as exc:
            logger.warning(
                "[%s/%s] Could not persist solver checkpoint: %s",
                self.meta.name,
                state_model_spec,
                exc,
            )

    def _effective_model_specs(self) -> list[str]:
        """Drop solvers that cannot run under the active executor mode."""
        if _resolve_executor_mode(self.settings) != "headless":
            return list(self.model_specs)
        usable = [
            spec
            for spec in self.model_specs
            if provider_from_spec(spec) not in SANDBOX_EXEMPT_PROVIDERS
        ]
        skipped = [s for s in self.model_specs if s not in usable]
        if skipped:
            logger.warning(
                "[%s] Headless mode skips sandbox-bound solver(s): %s",
                self.meta.name,
                ", ".join(skipped),
            )
        if not usable:
            logger.error(
                "[%s] No runnable solver in headless mode — configure a Pydantic AI "
                "model (deepseek/qwen/azure/zen/google/bedrock) instead of %s",
                self.meta.name,
                ", ".join(self.model_specs),
            )
        return usable

    def _create_solver(self, model_spec: str):
        """Create the right solver type based on provider and executor mode.

        - headless executor → HeadlessSolver (no container; model-only + light tools)
        - claude-sdk/* → ClaudeSolver (Claude Agent SDK, subscription-first)
        - codex/* → CodexSolver (Codex App Server, subscription-first)
        - bedrock/*, azure/*, zen/*, google/*, qwen/*, deepseek/* → Pydantic AI Solver
        """
        provider = provider_from_spec(model_spec)
        executor_mode = _resolve_executor_mode(self.settings)
        workspace_dir, resume_context = self._prepare_solver_state(model_spec)

        def _submit_fn(flag): return self.try_submit_flag(flag, model_spec)
        _notify = self._make_notify_fn(model_spec)

        if executor_mode == "headless" and provider not in SANDBOX_EXEMPT_PROVIDERS:
            from backend.agents.headless_solver import HeadlessSolver

            time_budget = max(
                300,
                _time_limit_for_challenge(self.settings, self.meta) - 60,
            )
            return HeadlessSolver(
                model_spec=model_spec,
                meta=self.meta,
                platform=self.platform,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
                resume_context=resume_context,
                allow_exec=bool(getattr(self.settings, "allow_local_exec", True)),
                time_budget_seconds=time_budget,
                workdir=workspace_dir,
            )

        if provider == "claude-sdk":
            from backend.agents.claude_solver import ClaudeSolver
            return ClaudeSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                platform=self.platform,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
                persistent_workspace_dir=workspace_dir,
                resume_context=resume_context,
            )

        if provider == "codex":
            from backend.agents.codex_solver import CodexSolver
            return CodexSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                platform=self.platform,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
                persistent_workspace_dir=workspace_dir,
                resume_context=resume_context,
            )

        return self._create_pydantic_solver(
            model_spec,
            state_model_spec=model_spec,
            persistent_workspace_dir=workspace_dir,
            resume_context=resume_context,
        )

    def _make_notify_fn(self, model_spec: str):
        """Create a callback that pushes solver messages to the coordinator inbox."""
        async def _notify(message: str) -> None:
            if self.coordinator_inbox:
                self.coordinator_inbox.put_nowait(
                    f"[{self.meta.name}/{model_spec}] {message}"
                )
        return _notify

    def _create_pydantic_solver(
        self,
        model_spec: str,
        sandbox=None,
        owns_sandbox: bool | None = None,
        *,
        state_model_spec: str | None = None,
        persistent_workspace_dir: str = "",
        resume_context: str = "",
    ) -> Solver:
        """Create a Pydantic AI solver. Pass sandbox to reuse an existing container (quota fallback)."""
        state_model_spec = state_model_spec or model_spec
        if not persistent_workspace_dir:
            persistent_workspace_dir, loaded_context = self._prepare_solver_state(state_model_spec)
            resume_context = resume_context or loaded_context
        solver = Solver(
            model_spec=model_spec,
            challenge_dir=self.challenge_dir,
            meta=self.meta,
            platform=self.platform,
            cost_tracker=self.cost_tracker,
            settings=self.settings,
            cancel_event=self.cancel_event,
            sandbox=sandbox,
            owns_sandbox=owns_sandbox,
            persistent_workspace_dir=persistent_workspace_dir,
            resume_context=resume_context,
        )
        solver.deps.message_bus = self.message_bus
        solver.deps.model_spec = model_spec
        solver.deps.no_submit = self.no_submit
        solver.deps.submit_fn = lambda flag: self.try_submit_flag(flag, model_spec)
        solver.deps.notify_coordinator = self._make_notify_fn(model_spec)
        return solver

    def _gather_sibling_insights(self, exclude_model: str) -> str:
        parts: list[str] = []
        for model, finding in self.findings.items():
            if model != exclude_model and finding:
                parts.append(f"[{model}]: {finding}")
        if not parts:
            return "No sibling insights available yet."
        header = (
            "The following are sibling solvers' SELF-REPORTED findings — unverified "
            "claims, not confirmed facts. Before acting on any claimed endpoint, "
            "service, file, credential, or string, verify it yourself with one cheap "
            "tool call; if verification fails, discard the claim and move on. Note "
            "that solvers share one disk, so files mentioned below may belong to a "
            "different challenge."
        )
        return header + "\n\n" + "\n\n".join(parts)

    # Escalating cooldowns after incorrect submissions (per model)
    SUBMISSION_COOLDOWNS = [0, 30, 120, 300, 600]  # 0s, 30s, 2min, 5min, 10min
    # Circuit breaker: this many wrong submissions (all models combined) locks
    # the challenge — guessing flags burns the whole time budget.
    SUBMIT_LOCK_WRONG_LIMIT = 4

    async def try_submit_flag(self, flag: str, model_spec: str) -> tuple[str, bool]:
        """Cooldown-gated, deduplicated flag submission. Returns (display, is_confirmed)."""
        async with self._flag_lock:
            normalize_flag = getattr(self.platform, "normalize_flag", None)
            normalized = (
                normalize_flag(flag)
                if callable(normalize_flag)
                else flag.strip()
            )

            # Short-circuit ONLY a genuine duplicate of an already-confirmed
            # flag. Any NEW flag value is always forwarded to the platform —
            # on multi-flag tasks swallowing new flags after the first confirm
            # loses real points (the platform never receives them).
            if normalized in self._confirmed_flags:
                return f"ALREADY SOLVED — flag already confirmed: {normalized}", True

            if self._submit_locked:
                return (
                    f"SUBMIT LOCKED — this task already produced "
                    f"{self._wrong_total} incorrect submissions. Stop guessing. "
                    "Only submit again if you have hard evidence (flag string "
                    "observed verbatim in target output).",
                    False,
                )

            # Already accepted as partial credit on a multi-flag task: dedup
            # without mislabeling it as incorrect and without a confirmation.
            if normalized in self._accepted_flags:
                return (
                    "ALREADY ACCEPTED — the platform already credited this exact flag. "
                    "This task needs more flags; work a different attack surface "
                    "and do not resubmit this one.",
                    False,
                )

            # Dedup exact flags across all models
            if normalized in self._submitted_flags:
                return "INCORRECT — already tried this exact flag.", False

            # Escalating cooldown after incorrect submissions
            wrong_count = self._submit_count.get(model_spec, 0)
            cooldown_idx = min(wrong_count, len(self.SUBMISSION_COOLDOWNS) - 1)
            cooldown = self.SUBMISSION_COOLDOWNS[cooldown_idx]
            if cooldown > 0:
                last_time = self._last_submit_time.get(model_spec, 0)
                elapsed = time.monotonic() - last_time
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed)
                    return (
                        f"COOLDOWN — wait {remaining}s before submitting again. "
                        f"You have {wrong_count} incorrect submissions. "
                        "Use this time to do deeper analysis and verify your flag.",
                        False,
                    )

            self._submitted_flags.add(normalized)

            try:
                result = await self.platform.submit_flag(self.meta.name, flag)
            except Exception as e:
                return f"submit_flag error: {e}", False

            if result.status == "already_solved":
                self.confirmed_flag = normalized
                self._confirmed_flags.add(normalized)
                self._accepted_flags.add(normalized)
                self._last_progress_monotonic = time.monotonic()
                return result.display, True

            if result.status == "correct":
                self._last_progress_monotonic = time.monotonic()
                self._accepted_flags.add(normalized)
                multi_flag = max(1, self.meta.flag_count) > 1
                if result.challenge_complete or not multi_flag:
                    self.confirmed_flag = normalized
                    self._confirmed_flags.add(normalized)
                    return result.display, True
                # Partial credit on a multi-flag task: no cooldown, no winner —
                # the swarm keeps racing until every remaining flag is accepted.
                return (
                    result.display
                    + "\nPARTIAL CREDIT — this task needs more flags. Keep working "
                    "with a different attack surface; do not resubmit this flag.",
                    False,
                )

            self._submit_count[model_spec] = wrong_count + 1
            self._last_submit_time[model_spec] = time.monotonic()
            self._wrong_total += 1
            if self._wrong_total >= self.SUBMIT_LOCK_WRONG_LIMIT:
                # Circuit breaker: too many blind guesses — stand the swarm
                # down so the slot moves to a solvable challenge.
                self._submit_locked = True
                self.cancel_event.cancel_with_reason("circuit_breaker")
                logger.warning(
                    "[%s] Submit circuit breaker tripped after %d wrong submissions",
                    self.meta.name, self._wrong_total,
                )
                return (
                    result.display
                    + f"\nSUBMIT LOCKED — {self._wrong_total} incorrect submissions on "
                    "this task. The swarm is standing down; further work should "
                    "happen only with fresh evidence.",
                    False,
                )
            return result.display, False

    async def _run_solver(self, model_spec: str) -> SolverResult | None:
        solver = self._create_solver(model_spec)
        self.solvers[model_spec] = solver
        result: SolverResult | None = None

        try:
            result, final_solver = await self._run_solver_loop(solver, model_spec)
            solver = final_solver
            return result
        except Exception as e:
            logger.error(f"[{self.meta.name}/{model_spec}] Fatal: {e}", exc_info=True)
            return None
        finally:
            if result is None:
                await self._persist_solver_state(
                    solver,
                    model_spec,
                    next_step=self._next_step_for_result(result),
                    sync_workspace=False,
                )
            stop_task = asyncio.create_task(solver.stop())
            try:
                await asyncio.shield(stop_task)
            except asyncio.CancelledError:
                # Let Docker/SDK clients close fully before propagating shutdown.
                await stop_task
                raise

    async def _run_solver_loop(self, solver, model_spec: str) -> tuple[SolverResult, SolverProtocol]:
        """Inner loop: start → run → bump → run → ..."""
        bump_count = 0
        consecutive_errors = 0
        max_bumps = 5  # Don't loop forever on the same challenge
        result = SolverResult(
            flag=None, status=CANCELLED, findings_summary="",
            step_count=0, cost_usd=0.0, log_path="",
        )
        await solver.start()

        while not self.cancel_event.is_set():
            while self.pause_event.is_set() and not self.cancel_event.is_set():
                await asyncio.sleep(0.5)
            result = await solver.run_until_done_or_gave_up()
            await self._persist_solver_state(
                solver,
                model_spec,
                result,
                next_step=self._next_step_for_result(result),
            )

            # Only broadcast useful findings — skip errors and broken solvers
            if (result.status not in (ERROR, QUOTA_ERROR)
                    and not (result.step_count == 0 and result.cost_usd == 0)
                    and result.findings_summary
                    and not result.findings_summary.startswith(("Error:", "Turn failed:"))):
                self.findings[model_spec] = result.findings_summary
                await self.message_bus.post(model_spec, result.findings_summary[:500])

            if result.status == FLAG_FOUND:
                self.cancel_event.cancel_with_reason("sibling_win")
                self.winner = result
                logger.info(
                    f"[{self.meta.name}] Flag found by {model_spec}: {result.flag}"
                )
                return result, solver

            if result.status == CANCELLED:
                break

            # Quota exhaustion: fall back to API-backed Pydantic AI solver
            if result.status == QUOTA_ERROR:
                fallback_spec = _quota_fallback_spec(model_spec)
                if fallback_spec:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Quota exhausted — falling back to {fallback_spec}"
                    )
                    existing_sandbox = solver.sandbox
                    # Detach sandbox from old solver so stop() doesn't destroy it
                    cast(Any, solver).sandbox = None
                    await solver.stop()
                    _, resume_context = self._prepare_solver_state(model_spec)
                    solver = self._create_pydantic_solver(
                        fallback_spec,
                        sandbox=existing_sandbox,
                        owns_sandbox=True,
                        state_model_spec=model_spec,
                        resume_context=resume_context,
                    )
                    self.solvers[model_spec] = solver
                    await solver.start()
                    continue
                # No fallback available, treat as error
                break

            if result.status in (GAVE_UP, ERROR):
                if result.step_count == 0 and result.cost_usd == 0:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Broken (0 steps, $0) — not bumping"
                    )
                    break

                # Track consecutive errors — stop after 3 in a row
                if result.status == ERROR:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        logger.warning(
                            f"[{self.meta.name}/{model_spec}] {consecutive_errors} consecutive errors — giving up"
                        )
                        break
                else:
                    consecutive_errors = 0

                bump_count += 1
                if bump_count > max_bumps:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Max bumps ({max_bumps}) reached — giving up"
                    )
                    break
                # Cooldown between bumps — check cancellation during wait
                retry_delay = max(
                    0.0,
                    float(getattr(self.settings, "solver_retry_delay_seconds", 2.0)),
                )
                if retry_delay:
                    try:
                        await asyncio.wait_for(
                            self.cancel_event.wait(),
                            timeout=retry_delay,
                        )
                        break
                    except TimeoutError:
                        pass
                insights = self._gather_sibling_insights(model_spec)
                solver.bump(insights)
                await self._persist_solver_state(
                    solver,
                    model_spec,
                    result,
                    next_step=f"Apply this new guidance without repeating prior work: {insights[:1200]}",
                    sync_workspace=False,
                )
                logger.info(
                    f"[{self.meta.name}/{model_spec}] Bumped ({bump_count}), resuming"
                )
                continue

        return result, solver

    async def _partial_stall_watchdog(self, stall_seconds: float) -> None:
        """Multi-flag tasks: bail out with partial credit when progress stalls.

        After a partial accept, grinding the remaining flags for a long time
        without a new accept has poor marginal value — release the slot so the
        window moves to fresh challenges.
        """
        while not self.cancel_event.is_set():
            try:
                await asyncio.wait_for(self.cancel_event.wait(), timeout=30)
                return
            except TimeoutError:
                pass
            if (
                self._last_progress_monotonic
                and not self.confirmed_flag
                and time.monotonic() - self._last_progress_monotonic > stall_seconds
            ):
                logger.warning(
                    "[%s] Partial-credit stall: no new flag accepted in %.0fs — withdrawing",
                    self.meta.name, stall_seconds,
                )
                if self.coordinator_inbox:
                    self.coordinator_inbox.put_nowait(
                        f"PARTIAL STALL: '{self.meta.name}' kept its partial score but no new "
                        f"flag was accepted for {int(stall_seconds)}s. The swarm withdrew to "
                        "free the slot; revisit later only with a genuinely new approach."
                    )
                self.cancel_event.cancel_with_reason("stall_watchdog")
                return

    async def run(self) -> SolverResult | None:
        """Run all solvers in parallel. Returns the winner's result or None."""
        tasks = [
            asyncio.create_task(self._run_solver(spec), name=f"solver-{spec}")
            for spec in self._effective_model_specs()
        ]

        watchdog: asyncio.Task | None = None
        stall_s = float(getattr(self.settings, "partial_stall_seconds", 600))
        if max(1, self.meta.flag_count) > 1 and stall_s > 0:
            watchdog = asyncio.create_task(
                self._partial_stall_watchdog(stall_s),
                name=f"stall-watchdog-{self.meta.name}",
            )

        try:
            while tasks:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                for task in done:
                    try:
                        result = task.result()
                    except Exception:
                        continue
                    if result and result.status == FLAG_FOUND:
                        self.cancel_event.cancel_with_reason("sibling_win")
                        for p in pending:
                            p.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        return result

                tasks = list(pending)

            self.cancel_event.cancel_with_reason("swarm_complete")
            return self.winner
        except asyncio.CancelledError:
            self.cancel_event.cancel_with_reason("unknown")
            raise
        except Exception as e:
            logger.error(f"[{self.meta.name}] Swarm error: {e}", exc_info=True)
            self.cancel_event.cancel_with_reason("error")
            return None
        finally:
            if watchdog and not watchdog.done():
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def kill(self, reason: str = "operator") -> None:
        """Cancel all agents for this challenge."""
        self.cancel_event.cancel_with_reason(reason)

    def pause(self) -> None:
        """Pause solver progression for this challenge."""
        self.pause_event.set()

    def resume(self) -> None:
        """Resume solver progression for this challenge."""
        self.pause_event.clear()

    def get_status(self) -> dict:
        """Get per-agent progress and findings."""
        agents = {}
        for spec in self._effective_model_specs():
            solver = self.solvers.get(spec)
            agent_status = "finished"
            if self.cancel_event.is_set():
                agent_status = "cancelled"
            elif self.pause_event.is_set():
                agent_status = "paused"
            elif self.winner and self.winner.flag:
                agent_status = "won"
            elif solver:
                agent_status = "running"

            steps = getattr(getattr(solver, "_step_count", None), "__getitem__", lambda _: 0)(0) if solver else 0
            cost = 0.0
            duration = 0.0
            model_name = spec
            if solver and getattr(solver, "agent_name", None) and self.cost_tracker:
                usage = self.cost_tracker.by_agent.get(solver.agent_name)
                if usage:
                    cost = usage.cost_usd
                    duration = usage.duration_seconds
                    model_name = usage.model_name or model_name
            agents[spec] = {
                "spec": spec,
                "model_name": model_name,
                "findings": self.findings.get(spec, ""),
                "status": agent_status,
                "step_count": steps,
                "cost_usd": cost,
                "duration_seconds": duration,
                "progress": min(100, max(0, steps * 4)),
            }

        return {
            "challenge": self.meta.name,
            "cancelled": self.cancel_event.is_set(),
            "paused": self.pause_event.is_set(),
            "winner": self.winner.flag if self.winner else None,
            "agents": agents,
        }
