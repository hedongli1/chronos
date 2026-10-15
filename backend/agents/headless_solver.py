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
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.toolsets import FunctionToolset

if TYPE_CHECKING:
    from backend.config import Settings

from backend.cost_tracker import CostTracker
from backend.deps import HeadlessDeps, SubmitFn
from backend.loop_detect import LoopDetector
from backend.models import (
    model_id_from_spec,
    provider_from_spec,
    resolve_model,
    resolve_model_settings,
)
from backend.platforms import CompetitionPlatform
from backend.prompts import (
    ChallengeMeta,
    build_headless_prompt,
)
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    SolverResult,
    terminal_reason_for,
)
from backend.tools.local_exec import do_run_python
from backend.tools.net_tools import do_raw_http, do_tcp_send
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)

FLAG_TOKEN_RE = re.compile(r"(?i)(?:flag|CTF)\{[^{}\r\n]{1,256}\}")
TOOL_MAX_RETRIES = 8
TASK_TIME_BUDGET_SECONDS = 55 * 60  # platform hard limit is 60 min per task
# Max auto-submissions from the final-output rescue path. Each wrong rescue
# submission feeds the swarm-wide circuit breaker, so a model that dumps many
# fake flag-shaped strings in its closing text must not be able to cancel
# every sibling solver.
RESCUE_MAX_SUBMITS = 3


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
    """Extract the assigned target hosts from connection_info ("ip:port, ip:port").

    Handles plain hosts ("10.0.0.5:80"), bracketed hosts ("[::1]:80") and
    scheme-prefixed entries ("http://10.0.0.5:80").
    """
    hosts: set[str] = set()
    for part in re.split(r"[,\s]+", connection_info or ""):
        part = part.strip()
        if not part:
            continue
        part = re.sub(r"^https?://", "", part)
        part = part.split("/")[0]
        if part.startswith("["):
            # Bracketed host (IPv6 literal): "[::1]:80" -> "::1".
            end = part.find("]")
            host = part[1:end] if end != -1 else part[1:]
        else:
            host = part.rsplit(":", 1)[0] if ":" in part else part
        if host:
            hosts.add(host.lower())
    return frozenset(hosts)


def _target_lock_block(deps: HeadlessDeps, host: str) -> str | None:
    """Return a block message when host is outside the assigned targets, else None.

    Purely structural: the solver's own egress is always restricted to the hosts
    parsed from this task's connection_info — no scenario/keyword guess can open
    it. This does not hinder multi-stage tasks: legitimate pivoting to internal
    hosts happens through a compromised host's own shell/RCE, not the solver's
    direct http_request/tcp_send/run_python sockets.

    Fail-loud: an empty allowed-hosts set must NOT silently disable the lock
    (the c-03 incident: unresolved connection_info let a run drift onto three
    other teams' instances). With no known target, ALL egress is refused and
    the model is told how to obtain/confirm the target.
    """
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
    if not h:
        # An unparsable/empty hostname is NOT in the allowed set — never fail
        # open (a caller can smuggle '//host' past urlparse). Block loudly.
        return (
            "BLOCKED (target lock): the request URL has no parseable host. "
            "Pass an explicit host (e.g. http://10.0.x.x:port/path), not a "
            "relative or scheme-only URL."
        )
    if h in deps.allowed_hosts:
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
    memory=None,
    findings=None,
) -> FunctionToolset[HeadlessDeps]:
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
        return await do_raw_http(
            url, method=method, headers=headers, body=body, timeout=timeout,
            allowed_hosts=ctx.deps.allowed_hosts,
        )

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
        # Always pass the allowed-hosts set (even when empty): do_run_python
        # treats an empty set as the degraded no-target-known lock (localhost
        # only). The solver's own sockets are always restricted to scope.
        return await do_run_python(
            code,
            timeout=timeout,
            allowed_hosts=ctx.deps.allowed_hosts,
            workdir=ctx.deps.workdir or None,
            allow_subprocess=ctx.deps.allow_subprocess,
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
        """Fetch the platform hint for this task.

        Note: the hint DEDUCTS score and can be requested at most once per task.
        Decide for yourself whether it is worth it — there is no gate."""
        if ctx.deps.hint_used:
            return "This task's one hint was already used."
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

        return await do_check_findings(ctx.deps.message_bus, ctx.deps.model_spec)

    @toolset.tool
    async def search_findings(ctx: RunContext[HeadlessDeps], query: str) -> str:
        """Keyword-search findings sibling solvers posted on THIS task.

        Unlike check_findings (which returns only the unread backlog once), this
        is repeatable and searches the whole shared log — use it to pull back a
        specific endpoint, credential, parameter, or error string a sibling
        mentioned earlier. All terms must appear (case-insensitive). Results are
        unverified self-reports: verify before relying on them."""
        from backend.tools.core import do_search_findings

        return await do_search_findings(ctx.deps.message_bus, ctx.deps.model_spec, query)

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

    if memory is not None:
        @toolset.tool
        async def remember(ctx: RunContext[HeadlessDeps], text: str) -> str:
            """Store a technique note in the cross-task memory (opt-in subsystem).

            Keep it general and self-contained: technique, protocol quirk, or
            reusable insight. Do NOT store flags, tasks passwords, or anything
            tied to one specific target's identity."""
            if len((text or "").strip()) > 2_000:
                return "remember: note too long (max 2000 chars)."
            if not (text or "").strip():
                return "remember: empty note."
            memory.remember(text)
            tracer.event("memory_remember", chars=len(text.strip()))
            return "Stored."

        @toolset.tool
        async def recall_memory(ctx: RunContext[HeadlessDeps], query: str) -> str:
            """Search the cross-task memory for notes matching every query term."""
            return memory.recall(query)

    if findings is not None:
        @toolset.tool
        async def record_finding(
            ctx: RunContext[HeadlessDeps],
            summary: str,
            evidence: str = "",
            severity: str = "high",
        ) -> str:
            """Record a vuln/credential/foothold finding (observability output).

            summary: one line. evidence: commands/output proving it. severity:
            high|medium|low|info. Not fed back into scoring — pure record."""
            if severity not in ("high", "medium", "low", "info"):
                severity = "info"
            findings.record(summary, evidence, severity)
            tracer.event("finding_recorded", summary=summary[:200], severity=severity)
            return f"Finding recorded ({severity})."

    return toolset


class HeadlessSolver:
    """A single sandbox-free solver: one model, one live target, one task."""

    def __init__(
        self,
        model_spec: str,
        meta: ChallengeMeta,
        platform: CompetitionPlatform,
        cost_tracker: CostTracker,
        settings: Settings,
        cancel_event: asyncio.Event | None = None,
        submit_fn: SubmitFn | None = None,
        message_bus=None,
        notify_coordinator: Callable[[str], Awaitable[None]] | None = None,
        allow_exec: bool = True,
        time_budget_seconds: int = TASK_TIME_BUDGET_SECONDS,
        workdir: str = "",
        no_submit: bool = False,
    ) -> None:
        self.model_spec = model_spec
        self.model_id = model_id_from_spec(model_spec)
        self.meta = meta
        self.platform = platform
        self.cost_tracker = cost_tracker
        self.settings = settings
        self.cancel_event = cancel_event or asyncio.Event()
        self.allow_exec = allow_exec
        self.time_budget_seconds = time_budget_seconds
        self.no_submit = no_submit

        # Opt-in subsystems — resolved once at construction so tool registration
        # is stable for the solver's lifetime.
        from backend.memory import CrossTaskMemory, FindingsRecorder

        self.memory = (
            CrossTaskMemory()
            if bool(getattr(settings, "cross_task_memory_enabled", False))
            else None
        )
        self.findings = (
            FindingsRecorder(meta.name)
            if bool(getattr(settings, "findings_output_enabled", False))
            else None
        )

        parsed_hosts = _parse_allowed_hosts(meta.connection_info)
        self.deps = HeadlessDeps(
            platform=platform,
            challenge_name=meta.name,
            meta=meta,
            cost_tracker=cost_tracker,
            submit_fn=submit_fn,
            message_bus=message_bus,
            model_spec=model_spec,
            notify_coordinator=notify_coordinator,
            allow_exec=allow_exec,
            allow_subprocess=bool(getattr(settings, "run_python_allow_subprocess", False)),
            workdir=workdir,
            no_submit=no_submit,
            flags_total=max(1, meta.flag_count),
            flags_accepted=meta.correct_flag_count,
            deadline_monotonic=time.monotonic() + time_budget_seconds,
            allowed_hosts=parsed_hosts,
        )
        self.loop_detector = LoopDetector()
        self.tracer = SolverTracer(meta.name, self.model_id)
        self.agent_name = f"{meta.name}/{self.model_id}"
        self._agent: Agent[HeadlessDeps, str] | None = None
        self._messages: list = []
        self._pending_bump: ModelRequest | None = None
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
    def step_count(self) -> int:
        """Current step count (tool calls made) for status reporting."""
        return self._step_count[0]

    async def start(self) -> None:
        remaining = max(0, int(self.deps.deadline_monotonic - time.monotonic()))
        extra_tools: list[str] = []
        if self.memory is not None:
            extra_tools += [
                "- `remember(text)`: store a reusable technique note in the cross-task memory "
                "(general insights only — never store flags or target-specific credentials).",
                "- `recall_memory(query)`: search the cross-task memory for notes matching every "
                "query term. Returned notes are UNVERIFIED self-reports from earlier tasks.",
            ]
        if self.findings is not None:
            extra_tools.append(
                "- `record_finding(summary, evidence, severity)`: record a vuln/credential/"
                "foothold for the post-run findings log (observability only, not scored)."
            )
        system_prompt = build_headless_prompt(
            self.meta,
            remaining_seconds=remaining,
            allow_exec=self.allow_exec,
            extra_tools=extra_tools or None,
        )
        if not self.deps.allowed_hosts:
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

        model = resolve_model(self.model_spec, self.settings)
        model_settings = resolve_model_settings(self.model_spec)
        raw_toolset = _build_toolset(
            self.deps, self.tracer, self._step_count,
            memory=self.memory, findings=self.findings,
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
        usage_acc = None  # filled below; kept for the exception paths
        usage_recorded = False  # True once this run's usage hit cost_tracker

        try:
            from pydantic_ai.usage import RunUsage, UsageLimits

            # Fresh per-call accumulator passed INTO the run: if the run is
            # cancelled or crashes, whatever accumulated so far is still
            # readable here (pydantic-ai mutates the passed object in place),
            # so interrupted runs keep their token accounting.
            usage_acc = RunUsage()
            history_bump = self._pending_bump  # snapshot: bump THIS run may consume
            result = await self._agent.run(
                "Solve this task." if not self._messages else "Continue solving.",
                deps=self.deps,
                message_history=self._message_history_for_run(),
                usage=usage_acc,
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
            usage_recorded = True
            self._messages = result.all_messages()
            # Only clear the bump this run consumed — a bump that landed while
            # the run was in flight must survive for the NEXT attempt.
            if self._pending_bump is history_bump:
                self._pending_bump = None

            output = result.output
            if isinstance(output, str) and output.strip():
                # Rescue every distinct flag-shaped token in the closing text,
                # not just the case where exactly one appears — a multi-flag
                # task's final summary can legitimately list several captured
                # flags, and the old "only if there's exactly one" rule
                # silently dropped all of them whenever that happened.
                for rescued, candidate in enumerate(_extract_flag_candidates(output)):
                    if self.deps.confirmed_flag:
                        break
                    if rescued >= RESCUE_MAX_SUBMITS:
                        self._findings += (
                            f"; further final-text flags dropped (rescue cap {RESCUE_MAX_SUBMITS})"
                        )
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
            if not usage_recorded:
                # The run itself was cancelled before completion — record the
                # partial usage. (If record() already ran and the cancellation
                # landed during a post-run await, the tokens are already in.)
                self._record_interrupted_usage(usage_acc, duration=time.monotonic() - t0)
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
            if not usage_recorded:
                self._record_interrupted_usage(usage_acc, duration=time.monotonic() - t0)
            self._findings = f"Error: {e}"
            self.tracer.event("error", error=str(e))
            # Mirror the cancellation guard: a flag confirmed right before an
            # unrelated crash is still a solve — never discard it.
            if self.deps.confirmed_flag and not self._confirmed:
                self._confirmed = True
                self._flag = self._flag or self.deps.confirmed_flag
                self.tracer.event("flag_confirmed_after_error", flag=self._flag)
                return self._result(FLAG_FOUND, cancel_reason=f"error: {e}")
            return self._result(ERROR)

    def _message_history_for_run(self) -> list | None:
        """History for the next run: previous messages plus any pending bump."""
        history: list = list(self._messages)
        if self._pending_bump is not None:
            history.append(self._pending_bump)
        return history or None

    def _record_interrupted_usage(self, usage_acc, duration: float) -> None:
        """Record tokens consumed before a cancelled/failed run.

        The passed-in RunUsage accumulator keeps whatever pydantic-ai added to
        it before the interruption — without this, cancelled runs (wall-clock
        kill, sibling win, operator stop) silently lost their whole token bill.
        """
        if usage_acc is not None and usage_acc.has_values():
            self.cost_tracker.record(
                self.agent_name,
                usage_acc,
                self.model_id,
                provider_spec=provider_from_spec(self.model_spec),
                duration_seconds=duration,
            )

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
            self._confirmed = True
            self.tracer.event("flag_confirmed", tool="submit_flag", flag=candidate, step=step)
        else:
            if display.startswith("INCORRECT") and candidate not in self.deps.rejected_flags:
                self.deps.rejected_flags.append(candidate)
            self._findings += f"; auto-submission result: {display[:300]}"

    def bump(self, insights: str) -> None:
        """Inject sibling insights and prepare to resume.

        Stored as a PENDING bump, consumed by the next run's message history.
        (Previously the bump was appended to self._messages directly, and a
        bump landing mid-run was silently discarded when the run loop
        reassigned self._messages = result.all_messages().)
        """
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
        self._pending_bump = bump_msg
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
        terminal_reason = terminal_reason_for(status, cancel_reason)
        finish["terminal_reason"] = terminal_reason
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
            terminal_reason=terminal_reason,
        )

    async def stop(self) -> None:
        self.tracer.event("stop", step_count=self._step_count[0])
        self.tracer.close()
