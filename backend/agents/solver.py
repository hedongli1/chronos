"""Per-model solver agent — one model, one container, one challenge."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from backend.cost_tracker import CostTracker
from backend.deps import SolverDeps
from backend.loop_detect import LOOP_WARNING_MESSAGE, LoopDetector
from backend.models import (
    model_id_from_spec,
    provider_from_spec,
    resolve_model,
    resolve_model_settings,
    supports_vision,
)
from backend.platforms import CompetitionPlatform
from backend.prompts import ChallengeMeta, build_prompt, list_distfiles
from backend.sandbox import DockerSandbox
from backend.solver_base import CANCELLED, ERROR, FLAG_FOUND, GAVE_UP, SolverResult
from backend.tools.flag import submit_flag
from backend.tools.sandbox import (
    bash,
    check_findings,
    list_files,
    notify_coordinator,
    read_file,
    web_fetch,
    webhook_create,
    webhook_get_requests,
    write_file,
)
from backend.tools.vision import view_image
from backend.tracing import SolverTracer

logger = logging.getLogger(__name__)

FLAG_TOKEN_RE = re.compile(r"(?i)(?:flag|CTF)\{[^{}\r\n]{1,256}\}")
SOLVER_TOOL_MAX_RETRIES = 8

# Escalating warnings when the solver goes many tool calls without ever
# submitting a flag. Run data shows tasks with 100+ unsubmitted steps are
# essentially never solved — better to force a decision than to keep grinding.
NO_PROGRESS_STEPS = (40, 80, 120)
NO_PROGRESS_WARNING = (
    "\n\n⚠️ NO PROGRESS WARNING: {gap} consecutive tool calls without a single "
    "flag submission. Run history shows tasks that pass 100 steps without a "
    "submission are never solved. Decide NOW: either submit your best "
    "evidence-backed candidate with submit_flag, or pivot to a completely "
    "different attack surface. Do not keep probing the same way."
)


def _extract_unique_flag_candidate(text: str) -> str | None:
    """Return one unambiguous, well-formed flag token from model output."""
    candidates = list(dict.fromkeys(FLAG_TOKEN_RE.findall(text)))
    return candidates[0] if len(candidates) == 1 else None


@dataclass
class TracingToolset(WrapperToolset[SolverDeps]):
    """Wraps a toolset to add per-call tracing and loop detection."""

    tracer: SolverTracer = field(repr=False)
    loop_detector: LoopDetector = field(repr=False)
    step_counter: list[int] = field(repr=False)
    checkpoint_callback: Callable[[], Awaitable[None]] | None = field(
        default=None,
        repr=False,
    )
    _last_submit_step: int = field(default=0, init=False, repr=False)

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[SolverDeps], tool: ToolsetTool[SolverDeps]
    ) -> Any:
        self.step_counter[0] += 1
        step = self.step_counter[0]

        self.tracer.tool_call(name, tool_args, step)

        # Loop detection
        loop_status = self.loop_detector.check(name, tool_args)
        if loop_status == "break":
            logger.warning(f"Loop break on {name} at step {step}")
            self.tracer.event("loop_break", tool=name, step=step)
            # Inject loop warning by returning it as the tool result
            return LOOP_WARNING_MESSAGE

        result = await self.wrapped.call_tool(name, tool_args, ctx, tool)

        result_str = str(result) if result is not None else ""
        self.tracer.tool_result(name, result_str, step)

        # No-progress escalation: long runs without a single submit_flag call
        # almost never solve the task; push the model to a decision point.
        if name == "submit_flag":
            self._last_submit_step = step
        elif isinstance(result, str) and step - self._last_submit_step in NO_PROGRESS_STEPS:
            gap = step - self._last_submit_step
            result += NO_PROGRESS_WARNING.format(gap=gap)
            self.tracer.event("no_progress_warning", step=step, gap=gap)

        # Inject loop warning alongside result on "warn" level
        if loop_status == "warn":
            result = f"{result}\n\n{LOOP_WARNING_MESSAGE}" if isinstance(result, str) else result

        # The display string may contain misleading substrings such as
        # "INCORRECT". Trust only the structured confirmation set by the flag
        # tool after the platform returns a correct/already_solved status.
        if name == "submit_flag" and ctx.deps.confirmed_flag:
            self.tracer.event(
                "flag_confirmed",
                tool=name,
                flag=ctx.deps.confirmed_flag,
                step=step,
            )

        # A model turn can last for many minutes. Snapshot every few tool calls
        # so Ctrl+C or a process crash loses as little workspace progress as
        # possible instead of waiting for the full turn to finish.
        if step % 5 == 0 and self.checkpoint_callback:
            try:
                await self.checkpoint_callback()
            except Exception as exc:
                logger.warning("Periodic solver checkpoint failed: %s", exc)

        if step % 5 == 0 and ctx.deps.message_bus and isinstance(result, str):
            from backend.tools.core import do_check_findings
            findings_text = await do_check_findings(ctx.deps.message_bus, ctx.deps.model_spec)
            if findings_text and "No new findings" not in findings_text:
                result = f"{result}\n\n---\n{findings_text}"
                self.tracer.event("findings_injected", step=step)

        return result


def _build_toolset(deps: SolverDeps) -> FunctionToolset[SolverDeps]:
    """Build the raw toolset for a solver agent."""
    tools = [bash, read_file, write_file, list_files, submit_flag, web_fetch,
             webhook_create, webhook_get_requests, check_findings, notify_coordinator]
    if deps.use_vision:
        tools.append(view_image)
    return FunctionToolset(tools=tools, max_retries=SOLVER_TOOL_MAX_RETRIES)


class Solver:
    """A single solver: one model, one container, one challenge."""

    def __init__(
        self,
        model_spec: str,
        challenge_dir: str,
        meta: ChallengeMeta,
        platform: CompetitionPlatform,
        cost_tracker: CostTracker,
        settings: object,
        cancel_event: asyncio.Event | None = None,
        sandbox: DockerSandbox | None = None,
        owns_sandbox: bool | None = None,
        persistent_workspace_dir: str = "",
        resume_context: str = "",
    ) -> None:
        self.model_spec = model_spec
        self.model_id = model_id_from_spec(model_spec)
        self.challenge_dir = challenge_dir
        self.meta = meta
        self.platform = platform
        self.cost_tracker = cost_tracker
        self.settings = settings
        self.cancel_event = cancel_event or asyncio.Event()
        self._owns_sandbox = owns_sandbox if owns_sandbox is not None else (sandbox is None)
        self.resume_context = resume_context

        self.sandbox = sandbox or DockerSandbox(
            image=getattr(settings, "sandbox_image", "ctf-sandbox"),
            challenge_dir=challenge_dir,
            memory_limit=getattr(settings, "container_memory_limit", "4g"),
            persistent_workspace_dir=persistent_workspace_dir,
        )
        self.use_vision = supports_vision(model_spec)
        self.deps = SolverDeps(
            sandbox=self.sandbox,
            platform=platform,
            challenge_dir=challenge_dir,
            challenge_name=meta.name,
            workspace_dir="",
            use_vision=self.use_vision,
            cost_tracker=cost_tracker,
        )
        self.loop_detector = LoopDetector()
        self.tracer = SolverTracer(meta.name, self.model_id)
        self.agent_name = f"{meta.name}/{self.model_id}"
        self._agent: Agent[SolverDeps, str] | None = None
        self._messages: list = []
        self._step_count = [0]  # mutable ref shared with TracingToolset
        self._flag: str | None = None
        self._confirmed: bool = False
        self._findings: str = ""
        self._target_focus: str | None = None
        self._target_signals: list[str] = []

    async def start(self) -> None:
        """Start the sandbox and build the agent."""
        if not self.sandbox._container:
            await self.sandbox.start()
        self.deps.workspace_dir = self.sandbox.workspace_dir

        arch_result = await self.sandbox.exec("uname -m", timeout_s=10)
        container_arch = arch_result.stdout.strip() or "unknown"

        distfile_names = list_distfiles(self.challenge_dir)
        system_prompt = build_prompt(
            self.meta,
            distfile_names,
            container_arch=container_arch,
        )
        if self.resume_context:
            system_prompt += "\n\n" + self.resume_context
        target_hint = self._extract_target_hint(system_prompt)
        if target_hint:
            self._target_focus = target_hint
            self._target_signals.append(target_hint)
            system_prompt = self._augment_prompt_with_target_lock(system_prompt, target_hint)

        model = resolve_model(self.model_spec, self.settings)
        model_settings = resolve_model_settings(self.model_spec)
        raw_toolset = _build_toolset(self.deps)
        toolset = TracingToolset(
            wrapped=raw_toolset,
            tracer=self.tracer,
            loop_detector=self.loop_detector,
            step_counter=self._step_count,
            checkpoint_callback=self._checkpoint_workspace,
        )

        self._agent = Agent(
            model,
            deps_type=SolverDeps,
            system_prompt=system_prompt,
            model_settings=model_settings,
            toolsets=[toolset],
            output_type=str,
        )

        self.tracer.event("start", challenge=self.meta.name, model=self.model_id)
        logger.info(f"[{self.agent_name}] Solver started")

    async def _checkpoint_workspace(self) -> None:
        persist_workspace = getattr(self.sandbox, "persist_workspace", None)
        if callable(persist_workspace):
            await persist_workspace()

    def _extract_target_hint(self, system_prompt: str) -> str | None:
        # Prefer explicit service connection info if present.
        m = re.search(r"## Service Connection\n```\n(.+?)\n```", system_prompt, re.S)
        if m:
            return m.group(1).strip()
        return None

    def _augment_prompt_with_target_lock(self, system_prompt: str, target_hint: str) -> str:
        return (
            system_prompt
            + "\n## Target Lock\n"
            + f"You are locked to this target for this solver session: `{target_hint}`.\n"
            + "Do not switch to other ports, hosts, or services unless the target itself confirms they are part of the same challenge.\n"
            + "If you have no evidence that a new endpoint belongs to this challenge, ignore it.\n"
        )

    async def run_until_done_or_gave_up(self) -> SolverResult:
        """Run the solver loop until flag found, gave up, or cancelled."""
        if not self._agent:
            await self.start()
        assert self._agent is not None

        t0 = time.monotonic()
        steps_before = self._step_count[0]

        try:
            from pydantic_ai.usage import UsageLimits
            result = await self._agent.run(
                "Solve this offensive-security task." if not self._messages else "Continue solving.",
                deps=self.deps,
                message_history=self._messages if self._messages else None,
                usage_limits=UsageLimits(request_limit=None),
            )

            duration = time.monotonic() - t0
            usage = result.usage

            self.cost_tracker.record(
                self.agent_name, usage, self.model_id,
                provider_spec=provider_from_spec(self.model_spec),
                duration_seconds=duration,
            )

            agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
            self.tracer.usage(
                usage.input_tokens, usage.output_tokens,
                usage.cache_read_tokens,
                agent_usage.cost_usd if agent_usage else 0.0,
            )

            self._messages = result.all_messages()

            # Trace model responses from new messages
            from pydantic_ai.messages import ModelResponse, TextPart
            for msg in result.new_messages():
                if isinstance(msg, ModelResponse):
                    text_parts = [p.content for p in msg.parts if isinstance(p, TextPart)]
                    text = " ".join(text_parts)
                    msg_usage = msg.usage
                    self.tracer.model_response(
                        text[:500], self._step_count[0],
                        input_tokens=msg_usage.input_tokens if msg_usage else 0,
                        output_tokens=msg_usage.output_tokens if msg_usage else 0,
                    )

            output = result.output
            if isinstance(output, str) and output.strip():
                candidate = _extract_unique_flag_candidate(output)
                if candidate:
                    self._flag = candidate
                    self._findings = f"Flag candidate from model output: {candidate}"
                    if not self.deps.confirmed_flag:
                        await self._submit_final_output_candidate(candidate)
            # Platform confirmation always counts (the primary path when not in dry-run)
            if self.deps.confirmed_flag:
                self._confirmed = True
                self._flag = self._flag or self.deps.confirmed_flag

            if self._confirmed and self._flag:
                return self._result(FLAG_FOUND)

            # If the model made zero tool calls and used zero tokens, the API
            # likely failed silently — treat as ERROR so the swarm loop handles
            # it differently (breaks early instead of bumping in a loop).
            steps_after = self._step_count[0]
            if steps_after == steps_before and usage.input_tokens == 0 and usage.output_tokens == 0:
                self._findings = "Model returned without using any tools."
                return self._result(ERROR)

            # For normal non-flag text, keep the solver alive so it can continue
            # with the next bump instead of being forced into output validation.
            if self._flag and not self._confirmed:
                return self._result(GAVE_UP)

            return self._result(GAVE_UP)

        except asyncio.CancelledError:
            return self._result(CANCELLED)
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

        self._step_count[0] += 1
        step = self._step_count[0]
        self.tracer.tool_call(
            "submit_flag",
            {"flag": candidate, "source": "final_output_rescue"},
            step,
        )

        if self.deps.submit_fn:
            display, confirmed = await self.deps.submit_fn(candidate)
        else:
            from backend.tools.core import do_submit_flag

            display, confirmed = await do_submit_flag(
                self.deps.platform,
                self.deps.challenge_name,
                candidate,
            )

        self.tracer.tool_result("submit_flag", display, step)
        if confirmed:
            self.deps.confirmed_flag = candidate
            self._confirmed = True
            self.tracer.event(
                "flag_confirmed",
                tool="submit_flag",
                flag=candidate,
                step=step,
            )
        else:
            self._findings += f"; auto-submission result: {display[:300]}"

    def bump(self, insights: str) -> None:
        """Inject insights from siblings and prepare to resume."""
        bump_msg = ModelRequest(
            parts=[
                UserPromptPart(
                    content=(
                        "Your previous attempt did not find the flag. Here are insights "
                        "from other agents working on the same challenge:\n\n"
                        f"{insights}\n\n"
                        "Treat these insights as unverified claims, not facts: verify any "
                        "claimed endpoint, service, file, credential, or string with one "
                        "cheap check of your own before relying on it (files on the shared "
                        "disk may belong to a different challenge). "
                        "Use these insights to try a different approach. "
                        "Do NOT repeat what has already been tried."
                    )
                )
            ]
        )
        self._messages.append(bump_msg)
        self.loop_detector.reset()
        self.tracer.event("bump", insights=insights[:500])
        logger.info(f"[{self.agent_name}] Bumped with sibling insights")

    def _result(self, status: str, run_steps: int | None = None, run_cost: float | None = None) -> SolverResult:
        agent_usage = self.cost_tracker.by_agent.get(self.agent_name)
        cost = agent_usage.cost_usd if agent_usage else 0.0
        self.tracer.event("finish", status=status, flag=self._flag, confirmed=self._confirmed, cost_usd=round(cost, 4))
        return SolverResult(
            flag=self._flag,
            status=status,
            findings_summary=self._findings[:2000],
            step_count=run_steps if run_steps is not None else self._step_count[0],
            cost_usd=run_cost if run_cost is not None else cost,
            log_path=self.tracer.path,
        )

    async def stop(self) -> None:
        self.tracer.event("stop", step_count=self._step_count[0])
        self.tracer.close()
        if self._owns_sandbox and self.sandbox:
            await self.sandbox.stop()
