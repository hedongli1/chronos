"""Headless solver — one model, one live target, NO sandbox.

Pure-LLM offensive-security worker: the model interacts with the live benchmark
target through http_request / tcp_send, computes with run_python, and submits
flags through the swarm's deduped channel. There is no Docker container and no
local security toolchain — the model's own reasoning is the primary capability.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.toolsets import FunctionToolset

from backend.cost_tracker import CostTracker
from backend.deps import HeadlessDeps
from backend.loop_detect import LoopDetector
from backend.models import (
    model_id_from_spec,
    provider_from_spec,
    resolve_model,
    resolve_model_settings,
    supports_vision,
)
from backend.platforms import CompetitionPlatform
from backend.prompts import (
    ChallengeMeta,
    _guess_scenario,
    _killchain_lock_open,
    build_headless_prompt,
)
from backend.solver_base import CANCELLED, ERROR, FLAG_FOUND, GAVE_UP, SolverResult
from backend.tools.local_exec import do_run_python
from backend.tools.net_tools import do_raw_http, do_tcp_send
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)

FLAG_TOKEN_RE = re.compile(r"(?i)(?:flag|CTF)\{[^{}\r\n]{1,256}\}")
TOOL_MAX_RETRIES = 8
TASK_TIME_BUDGET_SECONDS = 55 * 60  # platform hard limit is 60 min per task

# Hard gate for the score-deducting hint: past runs showed hints requested in
# the first minutes of a task are pure loss (the task stayed unsolved anyway),
# but runs already doomed (rejected submissions, stalled progress) benefit from
# an early unlock — see _HintGate. Values are defaults; Settings overrides them.
@dataclass(frozen=True)
class _HintGate:
    min_steps: int = 20
    min_seconds: int = 600
    stall_steps: int = 30
    stall_min_steps: int = 10
    stall_min_seconds: int = 300
    stall_wrong_submissions: int = 2


def _extract_flag_candidates(text: str) -> list[str]:
    """All distinct flag-shaped tokens in final model output, in order seen.

    Multi-flag tasks can legitimately have the model list several captured
    flags in its closing summary — the rescue path below must be able to
    submit all of them, not just bail out whenever more than one appears.
    """
    return list(dict.fromkeys(FLAG_TOKEN_RE.findall(text)))


def _norm_flag_text(value: str) -> str:
    """Case-insensitive, whitespace-normalized flag comparison key."""
    return re.sub(r"\s+", "", value).lower()


def _flag_inner(value: str) -> str | None:
    """Normalized content between flag{ and } — catches decoys re-wrapped with
    a different prefix or casing."""
    m = re.match(r"(?is)^\s*(?:flag|ctf)\{(.*)\}\s*$", value)
    return _norm_flag_text(m.group(1)) if m else None


def _is_rejected_variant(candidate: str, rejected: list[str]) -> bool:
    """True when candidate is a rejected flag modulo case/whitespace, or its
    inner content matches any rejected flag's inner content."""
    cand_norm = _norm_flag_text(candidate)
    cand_inner = _flag_inner(candidate)
    for r in rejected:
        if _norm_flag_text(r) == cand_norm:
            return True
        r_inner = _flag_inner(r)
        if cand_inner is not None and r_inner is not None and cand_inner == r_inner:
            return True
    return False


def _parse_allowed_hosts(connection_info: str) -> frozenset[str]:
    """Extract the assigned target hosts from connection_info ("ip:port, ip:port")."""
    hosts: set[str] = set()
    for part in re.split(r"[,\s]+", connection_info or ""):
        part = part.strip()
        if not part:
            continue
        part = re.sub(r"^https?://", "", part).strip("[]").split("/")[0]
        host = part.rsplit(":", 1)[0] if ":" in part else part
        if host:
            hosts.add(host.lower())
    return frozenset(hosts)


def _target_lock_block(deps: HeadlessDeps, host: str) -> str | None:
    """Return a block message when host is outside the assigned targets, else None.

    Killchain (multi-stage) tasks legitimately pivot to newly discovered hosts,
    so the lock only applies to single-target scenarios — playbook discipline
    covers the rest. Whether a task qualifies for this bypass is decided once,
    structurally, at construction time (`deps.lock_open` — see
    `backend.prompts._killchain_lock_open`), NOT by re-guessing the scenario
    from free text here.

    Fail-loud: an empty allowed-hosts set must NOT silently disable the lock
    (the c-03 incident: unresolved connection_info let a run drift onto three
    other teams' instances). With no known target, ALL egress is refused and
    the model is told how to obtain/confirm the target.
    """
    if deps.lock_open:
        return None
    if not deps.allowed_hosts:
        return (
            "BLOCKED (target lock — NO TARGET KNOWN): this task's connection info "
            "was empty or unparsable, so ALL outbound network access is locked. "
            "The VPN carries OTHER teams' instances — probing them wastes budget "
            "and any flag found there is invalid for this task. Obtain/confirm "
            "THIS task's target first: re-read the task description, check "
            "task_status, or report the missing target via notify_coordinator. "
            "run_python still works for local computation (localhost only)."
        )
    h = (host or "").strip().lower()
    if not h or h in deps.allowed_hosts:
        return None
    targets = ", ".join(sorted(deps.allowed_hosts))
    return (
        f"BLOCKED (target lock): '{h}' is not one of your assigned targets ({targets}). "
        "Other IPs on this VPN belong to OTHER tasks — hitting them wastes your budget "
        "and any flag found there is invalid for this task. Return to your assigned "
        "target. Switch only if YOUR target's own output explicitly points there."
    )


def _build_toolset(
    deps: HeadlessDeps,
    tracer: SolverTracer,
    step_counter: list[int],
    started_monotonic: float,
    hint_gate: _HintGate | None = None,
) -> FunctionToolset[HeadlessDeps]:
    gate = hint_gate or _HintGate()
    toolset: FunctionToolset[HeadlessDeps] = FunctionToolset(max_retries=TOOL_MAX_RETRIES)

    @toolset.tool
    async def http_request(
        ctx: RunContext[HeadlessDeps],
        url: str,
        method: str = "GET",
        headers: dict[str, Any] | None = None,
        body: str = "",
        timeout: float = 30.0,
    ) -> str:
        """Send an HTTP request to the live task target.

        Full control: set headers (auth, content-type), body (raw or JSON), any method.
        Challenge addresses are VPN-ranged (e.g. 10.0.x.x) — that is expected.
        Returns status line, response headers, and body verbatim.
        """
        host = urlparse(url if "://" in url else f"http://{url}").hostname or ""
        block = _target_lock_block(ctx.deps, host)
        if block:
            tracer.event(
                "target_lock_block", tool="http_request", host=host,
                degraded=not ctx.deps.allowed_hosts,
            )
            return block
        return await do_raw_http(url, method=method, headers=headers, body=body, timeout=timeout)

    @toolset.tool
    async def tcp_send(
        ctx: RunContext[HeadlessDeps],
        host: str,
        port: int,
        payload: str = "",
        payload_hex: str = "",
        read_timeout: float = 30.0,
    ) -> str:
        """Send one payload over raw TCP and return the service's reply.

        Use payload for line protocols ("GET / HTTP/1.0\\r\\n\\r\\n"), payload_hex
        for binary protocols. Reads until 3s of silence.
        """
        block = _target_lock_block(ctx.deps, host)
        if block:
            tracer.event(
                "target_lock_block", tool="tcp_send", host=host,
                degraded=not ctx.deps.allowed_hosts,
            )
            return block
        return await do_tcp_send(host, port, payload=payload, payload_hex=payload_hex, read_timeout=read_timeout)

    @toolset.tool
    async def run_python(
        ctx: RunContext[HeadlessDeps],
        code: str,
        timeout: float = 60.0,
    ) -> str:
        """Execute Python code locally (CPython) and return its output.

        Full stdlib plus installed libraries: unicorn (CPU emulation), capstone
        (disassembly), requests/httpx, cryptography. Your scratchpad: compute,
        decode, emulate, craft payloads, verify crypto. print() what you need.
        """
        if not ctx.deps.allow_exec:
            return "run_python is disabled for this run (--no-exec)."
        # Pass the allowed-hosts set through even when empty: do_run_python
        # treats None as "no lock" and an empty set as the degraded
        # no-target-known lock (localhost only).
        allowed = None if ctx.deps.lock_open else ctx.deps.allowed_hosts
        return await do_run_python(
            code,
            timeout=timeout,
            allowed_hosts=allowed,
            workdir=ctx.deps.workdir or None,
        )

    @toolset.tool
    async def submit_flag(ctx: RunContext[HeadlessDeps], flag: str) -> str:
        """Submit a captured flag. On multi-flag tasks partial credit applies —
        submit each flag as soon as you have it, then keep working."""
        flag = flag.strip()
        if not flag:
            raise ModelRetry(
                "TOOL ARGUMENT ERROR: submit_flag requires a non-empty flag. "
                "Re-check the exact captured value and call submit_flag again."
            )
        if ctx.deps.no_submit:
            return f'DRY RUN — would submit "{flag}"'
        if ctx.deps.submit_fn:
            display, confirmed = await ctx.deps.submit_fn(flag)
        else:
            from backend.tools.core import do_submit_flag

            display, confirmed = await do_submit_flag(
                ctx.deps.platform, ctx.deps.challenge_name, flag
            )
        if confirmed or display.startswith(("CORRECT", "ALREADY SOLVED")):
            # Confirmed flag or accepted partial credit: real progress for the
            # hint stall gate.
            ctx.deps.last_progress_step = step_counter[0] if step_counter else 0
        if confirmed:
            ctx.deps.confirmed_flag = flag
        else:
            # Track platform-rejected candidates and keep them visible: blind
            # resubmission of known-wrong flags feeds the swarm's circuit breaker.
            if display.startswith("INCORRECT") and flag not in ctx.deps.rejected_flags:
                ctx.deps.rejected_flags.append(flag)
            if ctx.deps.rejected_flags:
                display += (
                    "\nREJECTED flags so far — NEVER resubmit any of these: "
                    + ", ".join(ctx.deps.rejected_flags)
                )
        return display

    @toolset.tool
    async def request_hint(ctx: RunContext[HeadlessDeps]) -> str:
        """Request the platform hint for this task. WARNING: deducts 10% of the score.

        Hard limits: at most ONE hint per task. The hint is gated — it unlocks
        after 20+ tool steps AND 10+ minutes of genuine work, OR earlier (once
        10+ steps AND 5+ minutes have passed) when the run shows doom signals:
        2 platform-rejected submissions, or no new confirmed flag and no new
        finding for 30 steps. Denials state the exact unlock criteria."""
        steps = step_counter[0] if step_counter else 0
        elapsed = time.monotonic() - started_monotonic
        rejected = len(ctx.deps.rejected_flags)
        stalled_for = steps - ctx.deps.last_progress_step
        normal_ok = steps >= gate.min_steps and elapsed >= gate.min_seconds
        floor_ok = steps >= gate.stall_min_steps and elapsed >= gate.stall_min_seconds
        wrong_trigger = gate.stall_wrong_submissions > 0 and rejected >= gate.stall_wrong_submissions
        stall_trigger = gate.stall_steps > 0 and stalled_for >= gate.stall_steps
        if not (normal_ok or (floor_ok and (wrong_trigger or stall_trigger))):
            return (
                "HINT DENIED — hints deduct 10% of this task's score, and hints "
                "used before real progress are pure loss. "
                f"Status: {steps} tool steps, {int(elapsed) // 60} min elapsed, "
                f"{rejected} platform-rejected submissions, "
                f"{stalled_for} steps since the last confirmed flag or new finding. "
                f"The hint unlocks at >={gate.min_steps} steps AND "
                f">={gate.min_seconds // 60} min — OR earlier, once >={gate.stall_min_steps} "
                f"steps AND >={gate.stall_min_seconds // 60} min have passed, when "
                f"{gate.stall_wrong_submissions} submissions are platform-rejected "
                f"OR progress stalls for {gate.stall_steps} steps. "
                "Do not retry blindly: keep attacking a different surface, or work "
                "until one of the doom signals above trips the early unlock."
            )
        if ctx.deps.hint_used:
            return "HINT DENIED — this task's one hint was already used. Work with what you have."
        reason = (
            "threshold" if normal_ok
            else "rejected_submissions" if wrong_trigger
            else "stalled_progress"
        )
        tracer.event(
            "hint_gate_passed", reason=reason, steps=steps,
            rejected_submissions=rejected, stalled_for_steps=stalled_for,
        )
        fetch = getattr(ctx.deps.platform, "fetch_hint", None)
        if not callable(fetch):
            return "No hint API on this platform. Reason from the target's behavior instead."
        try:
            hint = await fetch(ctx.deps.challenge_name)
        except Exception as e:
            return f"request_hint error: {e}"
        if not hint:
            return "Platform returned no hint for this task."
        ctx.deps.hint_used = True
        tracer.event("hint_requested", hint=str(hint)[:500])
        return f"HINT (score deducted): {hint}"

    @toolset.tool
    async def task_status(ctx: RunContext[HeadlessDeps]) -> str:
        """Check flag progress (accepted/total) and remaining time budget."""
        remaining = ""
        if ctx.deps.deadline_monotonic:
            left = ctx.deps.deadline_monotonic - time.monotonic()
            remaining = (
                f"~{max(0, int(left)) // 60} min remaining"
                if left > 0
                else "TIME BUDGET EXHAUSTED — wrap up and submit your best candidate now"
            )
        progress = f"{ctx.deps.flags_accepted}/{ctx.deps.flags_total} flags accepted"
        return f"{progress}. {remaining}".strip()

    @toolset.tool
    async def check_findings(ctx: RunContext[HeadlessDeps]) -> str:
        """Check for new findings from sibling agents working on the same task."""
        from backend.tools.core import do_check_findings

        result = await do_check_findings(ctx.deps.message_bus, ctx.deps.model_spec)
        if not result.startswith(("No message bus", "No new findings")):
            # New sibling findings count as progress for the hint stall gate.
            ctx.deps.last_progress_step = step_counter[0] if step_counter else 0
        return result

    @toolset.tool
    async def notify_coordinator(ctx: RunContext[HeadlessDeps], message: str) -> str:
        """Send a message to the coordinator (strategic discovery or help request)."""
        if ctx.deps.notify_coordinator:
            try:
                await ctx.deps.notify_coordinator(message)
                return "Message sent to coordinator."
            except Exception as e:
                return f"Notification failed: {e}"
        return "No coordinator connected."

    return toolset


class HeadlessSolver:
    """A single sandbox-free solver: one model, one live target, one task."""

    def __init__(
        self,
        model_spec: str,
        meta: ChallengeMeta,
        platform: CompetitionPlatform,
        cost_tracker: CostTracker,
        settings: object,
        cancel_event: asyncio.Event | None = None,
        submit_fn: Callable[[str], Awaitable[tuple[str, bool]]] | None = None,
        message_bus=None,
        notify_coordinator: Callable[[str], Awaitable[None]] | None = None,
        resume_context: str = "",
        allow_exec: bool = True,
        time_budget_seconds: int = TASK_TIME_BUDGET_SECONDS,
        workdir: str = "",
    ) -> None:
        self.model_spec = model_spec
        self.model_id = model_id_from_spec(model_spec)
        self.meta = meta
        self.platform = platform
        self.cost_tracker = cost_tracker
        self.settings = settings
        self.cancel_event = cancel_event or asyncio.Event()
        self.resume_context = resume_context
        self.allow_exec = allow_exec
        self.time_budget_seconds = time_budget_seconds

        self.use_vision = supports_vision(model_spec)
        parsed_hosts = _parse_allowed_hosts(meta.connection_info)
        self.deps = HeadlessDeps(
            platform=platform,
            challenge_name=meta.name,
            meta=meta,
            use_vision=self.use_vision,
            cost_tracker=cost_tracker,
            submit_fn=submit_fn,
            message_bus=message_bus,
            model_spec=model_spec,
            notify_coordinator=notify_coordinator,
            allow_exec=allow_exec,
            workdir=workdir,
            flags_total=max(1, meta.flag_count),
            flags_accepted=meta.correct_flag_count,
            deadline_monotonic=time.monotonic() + time_budget_seconds,
            allowed_hosts=parsed_hosts,
            scenario=_guess_scenario(meta),
            # Structural + heuristic gate — see _killchain_lock_open docstring.
            # A single mis-tagged keyword can no longer open the lock on its
            # own; the task must also announce >=2 hosts up front.
            lock_open=_killchain_lock_open(meta, parsed_hosts),
        )
        self.loop_detector = LoopDetector()
        self.tracer = SolverTracer(meta.name, self.model_id)
        s = settings
        self._hint_gate = _HintGate(
            min_steps=int(getattr(s, "hint_min_steps", _HintGate.min_steps)),
            min_seconds=int(getattr(s, "hint_min_seconds", _HintGate.min_seconds)),
            stall_steps=int(getattr(s, "hint_stall_steps", _HintGate.stall_steps)),
            stall_min_steps=int(getattr(s, "hint_stall_min_steps", _HintGate.stall_min_steps)),
            stall_min_seconds=int(getattr(s, "hint_stall_min_seconds", _HintGate.stall_min_seconds)),
            stall_wrong_submissions=int(getattr(s, "hint_stall_wrong_submissions", _HintGate.stall_wrong_submissions)),
        )
        self.agent_name = f"{meta.name}/{self.model_id}"
        self._agent: Agent[HeadlessDeps, str] | None = None
        self._messages: list = []
        self._step_count = [0]
        self._flag: str | None = None
        self._confirmed: bool = False
        self._findings: str = ""
        self._usage_traced: bool = False
        self._traced_input_tokens: int = 0
        self._traced_output_tokens: int = 0
        self._traced_cache_tokens: int = 0
        self._traced_cost: float = 0.0

    @property
    def sandbox(self) -> None:
        """SolverProtocol compatibility — headless runs without a container."""
        return None

    async def start(self) -> None:
        remaining = max(0, int(self.deps.deadline_monotonic - time.monotonic()))
        system_prompt = build_headless_prompt(
            self.meta,
            remaining_seconds=remaining,
            allow_exec=self.allow_exec,
        )
        if not self.deps.lock_open and not self.deps.allowed_hosts:
            # Degraded target lock: no target hosts could be parsed from
            # connection_info, so the lock refuses ALL egress instead of
            # silently running unlocked. Make it loud in both trace and prompt.
            logger.warning(
                "[%s] Target lock DEGRADED: no hosts parsed from connection_info %r — "
                "all egress will be refused",
                self.agent_name, self.meta.connection_info,
            )
            self.tracer.event(
                "target_lock_degraded",
                challenge=self.meta.name,
                connection_info=self.meta.connection_info,
                detail="no target hosts parsed; all outbound network access is refused",
            )
            system_prompt += (
                "\n\n## TARGET LOCK — NO TARGET KNOWN\n"
                "This task's connection info was empty or unparsable, so the target "
                "lock is in its tightest state: http_request, tcp_send, and socket "
                "connections from run_python are ALL refused. This prevents you from "
                "drifting onto other teams' instances on the VPN. Do not probe VPN "
                "hosts. Re-derive the intended target from the task description, or "
                "report the missing target via notify_coordinator. run_python still "
                "works for local computation."
            )
        if self.resume_context:
            system_prompt += "\n\n" + self.resume_context

        model = resolve_model(self.model_spec, self.settings)
        model_settings = resolve_model_settings(self.model_spec)
        raw_toolset = _build_toolset(
            self.deps, self.tracer, self._step_count, time.monotonic(), self._hint_gate,
        )

        from backend.agents.tracing_toolset import TracingToolset

        toolset = TracingToolset(
            wrapped=raw_toolset,
            tracer=self.tracer,
            loop_detector=self.loop_detector,
            step_counter=self._step_count,
        )

        self._agent = Agent(
            model,
            deps_type=HeadlessDeps,
            system_prompt=system_prompt,
            model_settings=model_settings,
            toolsets=[toolset],
            output_type=str,
        )

        self.tracer.event("start", challenge=self.meta.name, model=self.model_id, mode="headless")
        logger.info(f"[{self.agent_name}] Headless solver started")

    async def run_until_done_or_gave_up(self) -> SolverResult:
        if not self._agent:
            await self.start()
        assert self._agent is not None

        t0 = time.monotonic()
        steps_before = self._step_count[0]

        try:
            from pydantic_ai.usage import UsageLimits

            result = await self._agent.run(
                "Solve this task." if not self._messages else "Continue solving.",
                deps=self.deps,
                message_history=self._messages if self._messages else None,
                usage_limits=UsageLimits(request_limit=None),
            )

            duration = time.monotonic() - t0
            usage = result.usage
            self.cost_tracker.record(
                self.agent_name,
                usage,
                self.model_id,
                provider_spec=provider_from_spec(self.model_spec),
                duration_seconds=duration,
            )
            self._messages = result.all_messages()

            output = result.output
            if isinstance(output, str) and output.strip():
                # Rescue every distinct flag-shaped token in the closing text,
                # not just the case where exactly one appears — a multi-flag
                # task's final summary can legitimately list several captured
                # flags, and the old "only if there's exactly one" rule
                # silently dropped all of them whenever that happened.
                for candidate in _extract_flag_candidates(output):
                    if self.deps.confirmed_flag:
                        break
                    await self._submit_final_output_candidate(candidate)
            if self.deps.confirmed_flag:
                self._confirmed = True
                self._flag = self._flag or self.deps.confirmed_flag
                return self._result(FLAG_FOUND)

            steps_after = self._step_count[0]
            if steps_after == steps_before and usage.input_tokens == 0 and usage.output_tokens == 0:
                self._findings = "Model returned without using any tools."
                return self._result(ERROR)

            return self._result(GAVE_UP)

        except asyncio.CancelledError:
            reason = getattr(self.cancel_event, "reason", "") or "unknown"
            # The run can be cancelled between the platform's flag confirmation
            # and this return (sibling win, wall-clock kill, ...). A confirmed
            # flag is a solve regardless of the cancellation.
            if self.deps.confirmed_flag and not self._confirmed:
                self._confirmed = True
                self._flag = self._flag or self.deps.confirmed_flag
                return self._result(FLAG_FOUND, cancel_reason=reason)
            return self._result(CANCELLED, cancel_reason=reason)
        except Exception as e:
            logger.error(f"[{self.agent_name}] Error: {e}", exc_info=True)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=str(e))
            return self._result(ERROR)

    async def _submit_final_output_candidate(self, candidate: str) -> None:
        """Rescue a flag token emitted as final text instead of a tool call."""
        if self.deps.no_submit:
            self._findings += " (not submitted because --no-submit is set)"
            return
        if _is_rejected_variant(candidate, self.deps.rejected_flags):
            self._findings += (
                f"; skipped auto-submission of {candidate}: matches an already-rejected flag"
            )
            return

        self._step_count[0] += 1
        step = self._step_count[0]
        self.tracer.tool_call("submit_flag", {"flag": candidate, "source": "final_output_rescue"}, step)

        if self.deps.submit_fn:
            display, confirmed = await self.deps.submit_fn(candidate)
        else:
            from backend.tools.core import do_submit_flag

            display, confirmed = await do_submit_flag(self.deps.platform, self.deps.challenge_name, candidate)

        self.tracer.tool_result("submit_flag", display, step)
        if confirmed:
            self.deps.confirmed_flag = candidate
            self.deps.last_progress_step = step
            self._confirmed = True
            self.tracer.event("flag_confirmed", tool="submit_flag", flag=candidate, step=step)
        else:
            if display.startswith("INCORRECT") and candidate not in self.deps.rejected_flags:
                self.deps.rejected_flags.append(candidate)
            self._findings += f"; auto-submission result: {display[:300]}"

    def bump(self, insights: str) -> None:
        """Inject sibling insights and prepare to resume."""
        bump_msg = ModelRequest(
            parts=[
                UserPromptPart(
                    content=(
                        "Your previous attempt did not finish the task. Insights from other agents:\n\n"
                        f"{insights}\n\n"
                        "Treat these insights as unverified claims, not facts: verify any claimed "
                        "endpoint, service, file, credential, or string with one cheap check before "
                        "building on it. Files on the local disk may belong to a different task.\n\n"
                        "Try a materially different approach. Do NOT repeat what already failed."
                    )
                )
            ]
        )
        self._messages.append(bump_msg)
        self.loop_detector.reset()
        self.tracer.event("bump", insights=insights[:500])
        logger.info(f"[{self.agent_name}] Bumped with sibling insights")

    def _result(self, status: str, cancel_reason: str = "") -> SolverResult:
        agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
        cost = agent_usage.cost_usd if agent_usage else 0.0
        if agent_usage:
            # Emit the not-yet-traced token delta here rather than only on the
            # success path, so a usage event lands on EVERY exit — cancel and
            # error paths would otherwise lose the run's accounting.
            u = agent_usage.usage
            d_in = u.input_tokens - self._traced_input_tokens
            d_out = u.output_tokens - self._traced_output_tokens
            d_cache = u.cache_read_tokens - self._traced_cache_tokens
            if d_in or d_out or d_cache or not self._usage_traced:
                self.tracer.usage(d_in, d_out, d_cache, cost - self._traced_cost)
                self._traced_input_tokens = u.input_tokens
                self._traced_output_tokens = u.output_tokens
                self._traced_cache_tokens = u.cache_read_tokens
                self._traced_cost = cost
                self._usage_traced = True
        finish: dict[str, Any] = {
            "status": status,
            "flag": self._flag,
            "confirmed": self._confirmed,
            "cost_usd": round(cost, 4),
        }
        if cancel_reason:
            finish["cancel_reason"] = cancel_reason
        self.tracer.event("finish", **finish)
        return SolverResult(
            flag=self._flag,
            status=status,
            findings_summary=self._findings[:2000],
            step_count=self._step_count[0],
            cost_usd=cost,
            log_path=self.tracer.path,
        )

    async def stop(self) -> None:
        self.tracer.event("stop", step_count=self._step_count[0])
        self.tracer.close()
