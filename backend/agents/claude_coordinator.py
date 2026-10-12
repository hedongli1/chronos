"""Claude Agent SDK coordinator — uses the shared event loop with a Claude SDK client."""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    create_sdk_mcp_server,
    tool,
)

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
from backend.config import Settings
from backend.deps import CoordinatorDeps
from backend.tools.http_tools import do_http_request

logger = logging.getLogger(__name__)

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

CRITICAL RULES:
- Each swarm has a hard 30-minute wall-clock limit enforced by the scheduler.
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

You will receive event messages and operator prompts. Respond with tool calls to manage the competition.
"""


def _text(s: str) -> dict:
    """Wrap a string in the Claude SDK MCP tool return format."""
    return {"content": [{"type": "text", "text": s}]}


def _build_coordinator_mcp(deps: CoordinatorDeps):
    """Build MCP server — thin wrappers around coordinator_core functions."""

    @tool("fetch_challenges", "List all challenges with category, points, solve count, and status.", {})
    async def fetch_challenges(args: dict) -> dict:
        return _text(await do_fetch_challenges(deps))

    @tool("get_solve_status", "Check which challenges are solved and which swarms are running.", {})
    async def get_solve_status(args: dict) -> dict:
        return _text(await do_get_solve_status(deps))

    @tool("spawn_swarm", "Launch all solver models on a challenge.", {"challenge_name": str})
    async def spawn_swarm(args: dict) -> dict:
        return _text(await do_spawn_swarm(deps, args["challenge_name"]))

    @tool("check_swarm_status", "Get per-agent progress for a swarm.", {"challenge_name": str})
    async def check_swarm_status(args: dict) -> dict:
        return _text(await do_check_swarm_status(deps, args["challenge_name"]))

    @tool("submit_flag", "Submit a flag to the competition platform.", {"challenge_name": str, "flag": str})
    async def submit_flag(args: dict) -> dict:
        return _text(await do_submit_flag(deps, args["challenge_name"], args["flag"]))

    @tool("kill_swarm", "Cancel all agents for a challenge.", {"challenge_name": str})
    async def kill_swarm(args: dict) -> dict:
        return _text(await do_kill_swarm(deps, args["challenge_name"]))

    @tool("bump_agent", "Send targeted insights to a stuck agent. insights must follow BUMP DISCIPLINE: cite trace/tool evidence actually observed; label anything else as an unverified hypothesis with a cheap verification step; never assert endpoints, services, files, credentials, or strings not seen in a tool result.", {"challenge_name": str, "model_spec": str, "insights": str})
    async def bump_agent(args: dict) -> dict:
        return _text(await do_bump_agent(deps, args["challenge_name"], args["model_spec"], args["insights"]))

    @tool("broadcast", "Broadcast a strategic hint to ALL solvers on a challenge. Same evidence discipline as bump_agent: cite observed evidence or label unverified hypotheses.", {"challenge_name": str, "message": str})
    async def broadcast(args: dict) -> dict:
        return _text(await do_broadcast(deps, args["challenge_name"], args["message"]))

    @tool("read_solver_trace", "Read recent trace events from a specific solver. Use this to understand what a solver is doing, what it tried, and where it's stuck.", {"challenge_name": str, "model_spec": str, "last_n": int})
    async def read_solver_trace(args: dict) -> dict:
        return _text(await do_read_solver_trace(deps, args["challenge_name"], args["model_spec"], args.get("last_n", 20)))

    @tool("http_request", "Send an HTTP request to the configured competition platform origin only. Use this for platform-specific APIs that are not wrapped by other tools.", {"path_or_url": str, "method": str, "headers": dict, "query": dict, "json_body": object, "body": str, "timeout": float})
    async def http_request(args: dict) -> dict:
        return _text(await do_http_request(
            deps.platform_base_url,
            args["path_or_url"],
            method=args.get("method", "GET"),
            headers=args.get("headers"),
            query=args.get("query"),
            json_body=args.get("json_body"),
            body=args.get("body"),
            timeout=float(args.get("timeout", 30.0)),
        ))

    return create_sdk_mcp_server(
        name="coordinator", version="1.0.0",
        tools=[fetch_challenges, get_solve_status, spawn_swarm, check_swarm_status,
               submit_flag, kill_swarm, bump_agent, broadcast, read_solver_trace, http_request],
    )


async def run_claude_coordinator(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    no_submit: bool = False,
    coordinator_model: str | None = None,
    msg_port: int = 0,
    interactive: bool = False,
) -> dict[str, Any]:
    """Run the Claude Agent SDK coordinator with the shared event loop."""
    platform, cost_tracker, deps = build_deps(
        settings, model_specs, challenges_root, no_submit,
    )
    deps.msg_port = msg_port

    mcp_server = _build_coordinator_mcp(deps)
    resolved_model = coordinator_model or "claude-opus-4-6"

    allowed = {
        "mcp__coordinator__fetch_challenges", "mcp__coordinator__get_solve_status",
        "mcp__coordinator__spawn_swarm", "mcp__coordinator__check_swarm_status",
        "mcp__coordinator__submit_flag", "mcp__coordinator__kill_swarm",
        "mcp__coordinator__bump_agent", "mcp__coordinator__broadcast",
        "mcp__coordinator__read_solver_trace",
        "mcp__coordinator__http_request",
        "ToolSearch",
        "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop",
    }

    async def enforce_allowlist(input_data, tool_use_id, context):
        if input_data.get("hook_event_name") != "PreToolUse":
            return {}
        tool = input_data.get("tool_name", "")
        if tool in allowed:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"{tool} not available to coordinator.",
            }
        }

    options = ClaudeAgentOptions(
        model=resolved_model,
        system_prompt=COORDINATOR_PROMPT + BUMP_DISCIPLINE,
        env={"CLAUDECODE": ""},
        mcp_servers={"coordinator": mcp_server},
        allowed_tools=list(allowed),
        permission_mode="bypassPermissions",
        hooks={
            "PreToolUse": [HookMatcher(hooks=[enforce_allowlist])],
        },
    )

    async with ClaudeSDKClient(options=options) as client:
        async def turn_fn(msg: str) -> None:
            logger.debug(f"Coordinator query: {msg[:200]}")
            await client.query(msg)
            msg_count = 0
            async for message in client.receive_response():
                msg_count += 1
                msg_type = type(message).__name__
                logger.debug(f"Coordinator received: {msg_type}")
                if isinstance(message, ResultMessage):
                    cost = getattr(message, "total_cost_usd", 0)
                    session = getattr(message, "session_id", None)
                    logger.info(f"Claude coordinator turn done (messages={msg_count}, cost=${cost:.4f}, session={session})")
                    result_text = getattr(message, "result", "")
                    if result_text and interactive:
                        print(f"\n[Coordinator] {result_text[:500]}")
            if msg_count == 0:
                logger.warning("Coordinator turn produced no messages!")

        return await run_event_loop(deps, platform, cost_tracker, turn_fn, interactive=interactive)
