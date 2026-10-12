"""Toolset wrapper that adds per-call tracing, loop detection (telemetry only),
and sibling-finding injection.

Deliberately does NOT steer the model: loop detection emits a trace event for
observability but injects no "you are looping" text, and there is no
no-progress nagging. The only thing it adds to a tool result is sibling
findings from the shared message bus — the same-task knowledge sharing we keep
on purpose.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from backend.loop_detect import LoopDetector

logger = logging.getLogger(__name__)


@dataclass
class TracingToolset(WrapperToolset[Any]):
    """Wraps a toolset to add per-call tracing, loop telemetry, and sibling-finding injection."""

    tracer: Any = field(repr=False)
    loop_detector: LoopDetector = field(repr=False)
    step_counter: list[int] = field(repr=False)

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        self.step_counter[0] += 1
        step = self.step_counter[0]

        self.tracer.tool_call(name, tool_args, step)

        # Loop detection is telemetry only — record it, never inject steering text.
        loop_status = self.loop_detector.check(name, tool_args)
        if loop_status in ("warn", "break"):
            self.tracer.event("loop_detected", level=loop_status, tool=name, step=step)

        result = await self.wrapped.call_tool(name, tool_args, ctx, tool)

        result_str = str(result) if result is not None else ""
        self.tracer.tool_result(name, result_str, step)

        # Trust only the structured confirmation set by the flag tool, not the
        # display string (which may contain substrings like "INCORRECT").
        if name == "submit_flag" and getattr(ctx.deps, "confirmed_flag", None):
            self.tracer.event("flag_confirmed", tool=name, flag=ctx.deps.confirmed_flag, step=step)

        # Same-task knowledge sharing: fold in any new sibling findings.
        message_bus = getattr(ctx.deps, "message_bus", None)
        if step % 5 == 0 and message_bus and isinstance(result, str):
            from backend.tools.core import do_check_findings

            findings_text = await do_check_findings(message_bus, getattr(ctx.deps, "model_spec", ""))
            if findings_text and "No new findings" not in findings_text:
                result = f"{result}\n\n---\n{findings_text}"
                self.tracer.event("findings_injected", step=step)

        return result
