"""Embedded web console — live run visualization + operator control + solver watch/talk.

Additive observability/control layer: the mechanical scheduler and the solvers
are unchanged. State is read by polling the shared CoordinatorDeps (challenges,
swarms, cost); a watched solver's live reasoning is streamed by tailing its
JSONL trace file; operator actions (pause/resume/kill, and "talk to the agent"
via bump) go through the same operator_inbox the terminal REPL uses.

Launch with `ctf-solve --web`. No LLM runs here — "talking to the agent" means
injecting a one-off guidance message into a specific running solver.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from backend.agents.coordinator_loop import build_deps, remaining_global_seconds, run_event_loop
from backend.config import Settings

logger = logging.getLogger(__name__)

_INDEX_HTML = Path(__file__).parent / "webui" / "index.html"


# --------------------------------------------------------------------------- #
#  State snapshot
# --------------------------------------------------------------------------- #

def _challenge_status(name: str, deps, solved: set[str]) -> str:
    if name in solved:
        return "solved"
    task = deps.swarm_tasks.get(name)
    if task is not None and not task.done():
        return "active"
    if name in deps.pending_spawns:
        return "preparing"
    if name in deps.deferred_challenges:
        return "deferred"
    return "unsolved"


def build_snapshot(deps, cost_tracker) -> dict[str, Any]:
    """A JSON-serializable view of the whole run for the UI."""
    poller = deps.poller
    known = poller.known_challenges if poller else set()
    solved = poller.known_solved if poller else set()
    stubs = poller.known_stubs if poller else {}

    challenges = []
    for name in sorted(known):
        stub = stubs.get(name) or {}
        challenges.append({
            "name": name,
            "value": stub.get("value", 0),
            "level": stub.get("level", 0),
            "category": stub.get("category", ""),
            "flag_count": stub.get("flag_count", 1),
            "correct_flag_count": stub.get("correct_flag_count", 0),
            "status": _challenge_status(name, deps, solved),
        })

    swarms = {}
    for name, swarm in deps.swarms.items():
        try:
            swarms[name] = swarm.get_status()
        except Exception:
            continue

    active = sum(1 for t in deps.swarm_tasks.values() if not t.done())
    budget_left = remaining_global_seconds(deps)
    summary = {
        "solved": len(solved),
        "unsolved": len(known - solved),
        "total": len(known),
        "active": active,
        "preparing": len(deps.pending_spawns),
        "deferred": len(deps.deferred_challenges),
        "cost_usd": round(getattr(cost_tracker, "total_cost_usd", 0.0), 4),
        "total_tokens": getattr(cost_tracker, "total_tokens", 0),
        "budget_left_min": int(budget_left // 60) if budget_left is not None else None,
    }
    return {
        "summary": summary,
        "challenges": challenges,
        "swarms": swarms,
        "deferred": dict(deps.deferred_challenges),
    }


def _solver_trace_path(deps, challenge: str, model: str) -> str | None:
    swarm = deps.swarms.get(challenge)
    solver = swarm.solvers.get(model) if swarm else None
    tracer = getattr(solver, "tracer", None)
    return getattr(tracer, "path", None)


def _queue_put(out: asyncio.Queue, frame: dict) -> None:
    """Drop-oldest bounded enqueue for trace/state frames (see ws() _put)."""
    try:
        out.put_nowait(frame)
    except asyncio.QueueFull:
        try:
            out.get_nowait()
        except asyncio.QueueEmpty:
            pass
        out.put_nowait(frame)


async def _tail_trace(path: str, out: asyncio.Queue, challenge: str, model: str) -> None:
    """Stream a solver's JSONL trace to a queue: backlog first, then new lines."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            # Send recent backlog (last ~200 lines) so the panel isn't empty.
            lines = fh.readlines()
            for line in lines[-200:]:
                _emit_trace_line(out, line, challenge, model)
            # Then follow.
            while True:
                line = fh.readline()
                if line:
                    _emit_trace_line(out, line, challenge, model)
                else:
                    await asyncio.sleep(0.4)
    except FileNotFoundError:
        _queue_put(out, {"type": "trace_error", "message": f"trace not found: {path}"})
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        _queue_put(out, {"type": "trace_error", "message": str(e)})


def _emit_trace_line(out: asyncio.Queue, line: str, challenge: str, model: str) -> None:
    line = line.strip()
    if not line:
        return
    try:
        event = json.loads(line)
    except ValueError:
        return
    _queue_put(out, {"type": "trace", "challenge": challenge, "model": model, "event": event})


# --------------------------------------------------------------------------- #
#  FastAPI app
# --------------------------------------------------------------------------- #

def _build_app(deps, cost_tracker):
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI(title="chronos console")

    @app.get("/")
    async def index() -> HTMLResponse:
        try:
            return HTMLResponse(_INDEX_HTML.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return HTMLResponse("<h1>chronos console</h1><p>index.html missing</p>", status_code=500)

    @app.get("/api/state")
    async def state() -> JSONResponse:
        return JSONResponse(build_snapshot(deps, cost_tracker))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        # Bounded queue: a slow/stuck client must not let the pusher grow
        # memory without limit. State snapshots are replaceable — drop the
        # oldest pending frame when full.
        out: asyncio.Queue = asyncio.Queue(maxsize=256)
        tail_task: asyncio.Task | None = None

        def _put(frame: dict) -> None:
            """Bounded enqueue shared by ALL producers: drop the oldest frame
            when full so a stuck client can never wedge or grow memory."""
            try:
                out.put_nowait(frame)
            except asyncio.QueueFull:
                try:
                    out.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                out.put_nowait(frame)

        async def pusher() -> None:
            while True:
                _put({"type": "state", **build_snapshot(deps, cost_tracker)})
                await asyncio.sleep(1.0)

        async def sender() -> None:
            while True:
                msg = await out.get()
                await websocket.send_text(json.dumps(msg, default=str))

        pusher_task = asyncio.create_task(pusher())
        sender_task = asyncio.create_task(sender())

        def _stop_tail() -> None:
            nonlocal tail_task
            if tail_task and not tail_task.done():
                tail_task.cancel()
            tail_task = None

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                kind = msg.get("type")

                if kind == "command":
                    _enqueue_command(deps, msg)

                elif kind == "watch":
                    challenge = msg.get("challenge", "")
                    model = msg.get("model", "")
                    _stop_tail()
                    path = _solver_trace_path(deps, challenge, model)
                    if path:
                        _put({"type": "watch_ok", "challenge": challenge, "model": model})
                        tail_task = asyncio.create_task(_tail_trace(path, out, challenge, model))
                    else:
                        _put({"type": "trace_error", "message": f"no live solver {challenge} / {model}"})

                elif kind == "unwatch":
                    _stop_tail()
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug("ws closed: %s", e)
        finally:
            _stop_tail()
            pusher_task.cancel()
            sender_task.cancel()
            for t in (pusher_task, sender_task):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t

    return app


def _enqueue_command(deps, msg: dict) -> None:
    """Translate a UI command into an operator_inbox token (same path as the REPL)."""
    action = msg.get("action")
    challenge = msg.get("challenge", "")
    if action == "pause" and challenge:
        deps.operator_inbox.put_nowait(f"__pause__{challenge}")
    elif action == "resume" and challenge:
        deps.operator_inbox.put_nowait(f"__resume__{challenge}")
    elif action == "kill" and challenge:
        deps.operator_inbox.put_nowait(f"__kill__{challenge}")
    elif action == "bump":
        payload = json.dumps({
            "challenge": challenge,
            "model": msg.get("model", ""),
            "message": msg.get("message", ""),
        })
        deps.operator_inbox.put_nowait(f"__bump__{payload}")


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #

async def run_web_scheduler(
    settings: Settings,
    model_specs: list[str] | None = None,
    challenges_root: str = "challenges",
    no_submit: bool = False,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> dict[str, Any]:
    """Run the mechanical scheduler in the background and serve the web console."""
    import uvicorn

    platform, cost_tracker, deps = build_deps(settings, model_specs, challenges_root, no_submit)
    if getattr(settings, "platform_mode", "tsec") in ("tsec", "tsecbench", "benchmark"):
        deps.max_concurrent_challenges = min(getattr(settings, "max_concurrent_challenges", 3), 3)

    app = _build_app(deps, cost_tracker)
    scheduler_task = asyncio.create_task(
        run_event_loop(deps, platform, cost_tracker), name="scheduler",
    )

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    logger.info("Web console on http://%s:%d", host, port)

    try:
        # Serve until Ctrl+C (uvicorn traps the signal); the scheduler runs behind it.
        await server.serve()
    finally:
        if not scheduler_task.done():
            scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await scheduler_task

    if scheduler_task.done() and not scheduler_task.cancelled():
        with contextlib.suppress(Exception):
            return scheduler_task.result()
    return {"results": deps.results, "total_cost_usd": cost_tracker.total_cost_usd}
