"""Persistent coordinator history and compact solver resume checkpoints."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

logger = logging.getLogger(__name__)

CHECKPOINT_VERSION = 1
FLAG_RE = re.compile(r"(?i)(?:flag|CTF)\{[^{}\r\n]{1,256}\}")


def safe_component(value: str) -> str:
    """Return a Windows-safe, stable state-directory component."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned.strip(" ._") or "unknown"


def solver_checkpoint_path(state_dir: str, challenge_name: str, model_spec: str) -> Path:
    return (
        Path(state_dir)
        / "checkpoints"
        / safe_component(challenge_name)
        / f"{safe_component(model_spec)}.json"
    )


def solver_workspace_path(state_dir: str, challenge_name: str, model_spec: str) -> Path:
    return (
        Path(state_dir)
        / "workspaces"
        / safe_component(challenge_name)
        / safe_component(model_spec)
    )


def coordinator_history_path(
    state_dir: str,
    platform_mode: str,
    model_spec: str,
    task_salt: str = "",
) -> Path:
    """Coordinator history file; task_salt separates memory across benchmark tasks."""
    base = f"{safe_component(platform_mode)}-{safe_component(model_spec)}"
    if task_salt:
        base += f"-{safe_component(task_salt)}"
    return Path(state_dir) / "coordinator" / f"{base}.json"


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temp_path.replace(path)


def load_coordinator_turns(path: Path) -> list[list[ModelMessage]]:
    """Load complete coordinator turns; corrupt state is ignored safely."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        turns = raw.get("turns", []) if isinstance(raw, dict) else []
        return [
            ModelMessagesTypeAdapter.validate_python(turn)
            for turn in turns
            if isinstance(turn, list)
        ]
    except Exception as exc:
        logger.warning("Could not load coordinator history %s: %s", path, exc)
        return []


def save_coordinator_turns(path: Path, turns: list[list[ModelMessage]]) -> None:
    """Persist complete turns so tool-call/result pairs are never split."""
    try:
        payload = {
            "version": CHECKPOINT_VERSION,
            "updated_at": time.time(),
            "turns": [
                ModelMessagesTypeAdapter.dump_python(turn, mode="json")
                for turn in turns
            ],
        }
        _write_json_atomic(path, payload)
    except Exception as exc:
        logger.warning("Could not save coordinator history %s: %s", path, exc)


def load_solver_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Could not load solver checkpoint %s: %s", path, exc)
        return {}


def _trace_stem(value: str) -> str:
    """Match SolverTracer's historical filename normalization."""
    return value.replace("/", "_").replace(" ", "_")


def find_latest_trace(
    challenge_name: str,
    model_id: str,
    *,
    log_dir: str = "logs",
    exclude: str = "",
) -> Path | None:
    root = Path(log_dir)
    if not root.exists():
        return None
    pattern = f"trace-{_trace_stem(challenge_name)}-{_trace_stem(model_id)}-*.jsonl"
    excluded = Path(exclude).resolve() if exclude else None
    candidates = [
        path
        for path in root.glob(pattern)
        if excluded is None or path.resolve() != excluded
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _compact(text: Any, limit: int = 500) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _append_unique(items: list[str], value: str, *, limit: int) -> None:
    if value and value not in items:
        items.append(value)
    if len(items) > limit:
        del items[:-limit]


def summarize_trace(trace_path: Path) -> dict[str, Any]:
    """Extract compact, deterministic progress from a solver JSONL trace."""
    attempts: list[str] = []
    key_results: list[str] = []
    model_notes: list[str] = []
    errors: list[str] = []
    candidates: dict[str, str] = {}
    pending_calls: dict[tuple[int, str], str] = {}
    max_step = 0

    try:
        lines = trace_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        logger.warning("Could not read solver trace %s: %s", trace_path, exc)
        return {}

    for line in lines:
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", ""))
        step = int(event.get("step", 0) or 0)
        max_step = max(max_step, step)

        if event_type == "tool_call":
            tool = str(event.get("tool", "?"))
            args = _parse_args(event.get("args"))
            if tool == "bash":
                detail = _compact(args.get("command"), 360)
            elif tool == "write_file":
                detail = f"path={args.get('path', '?')}"
            elif tool == "submit_flag":
                detail = f"flag={args.get('flag', '?')}"
            else:
                detail = _compact(args, 300)
            summary = f"step {step} {tool}: {detail}"
            pending_calls[(step, tool)] = summary
            _append_unique(attempts, summary, limit=80)
            for candidate in FLAG_RE.findall(str(event.get("args", ""))):
                candidates.setdefault(candidate, "candidate only")

        elif event_type == "tool_result":
            tool = str(event.get("tool", "?"))
            result = _compact(event.get("result"), 600)
            call = pending_calls.get((step, tool), f"step {step} {tool}")
            if result and result not in {"(no output)", "None"}:
                _append_unique(key_results, f"{call} => {result}", limit=60)
            if tool == "submit_flag":
                flag_match = FLAG_RE.search(call)
                if flag_match:
                    candidates[flag_match.group(0)] = result or "unknown result"

        elif event_type == "model_response":
            note = _compact(event.get("text"), 700)
            _append_unique(model_notes, note, limit=12)
            for candidate in FLAG_RE.findall(str(event.get("text", ""))):
                candidates.setdefault(candidate, "candidate only")

        elif event_type == "bump":
            _append_unique(model_notes, _compact(event.get("insights"), 700), limit=12)

        elif event_type in {"error", "turn_failed", "loop_break"}:
            detail = event.get("error") or event.get("tool") or event
            _append_unique(errors, _compact(detail, 500), limit=12)

    return {
        "source_trace": str(trace_path),
        "step_count": max_step,
        "attempts": attempts,
        "key_results": key_results,
        "model_notes": model_notes,
        "errors": errors,
        "candidate_flags": [
            {"flag": flag, "result": result}
            for flag, result in candidates.items()
        ],
    }


def _merge_unique(existing: list[Any], current: list[Any], *, limit: int) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for item in [*existing, *current]:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged[-limit:]


def _merge_candidate_flags(existing: list[Any], current: list[Any], *, limit: int) -> list[dict[str, str]]:
    """Keep one latest result per flag while preserving first-seen ordering."""
    merged: dict[str, dict[str, str]] = {}
    for item in [*existing, *current]:
        if not isinstance(item, dict):
            continue
        flag = str(item.get("flag", "")).strip()
        if not flag:
            continue
        if flag not in merged:
            merged[flag] = {"flag": flag, "result": "candidate only"}
        result = str(item.get("result", "")).strip()
        if result:
            merged[flag]["result"] = result
    return list(merged.values())[-limit:]


def save_solver_checkpoint(
    path: Path,
    *,
    challenge_name: str,
    model_spec: str,
    trace_path: str,
    step_count: int,
    findings_summary: str = "",
    flag: str | None = None,
    confirmed: bool = False,
    workspace_dir: str = "",
    next_step: str = "",
) -> dict[str, Any]:
    """Merge the current trace into a persistent, compact solver checkpoint."""
    existing = load_solver_checkpoint(path)
    trace_summary = summarize_trace(Path(trace_path)) if trace_path else {}

    candidate_flags = trace_summary.get("candidate_flags", [])
    if flag:
        candidate_flags = [
            *candidate_flags,
            {"flag": flag, "result": "confirmed" if confirmed else "unconfirmed candidate"},
        ]

    checkpoint = {
        "version": CHECKPOINT_VERSION,
        "challenge": challenge_name,
        "model_spec": model_spec,
        "updated_at": time.time(),
        "source_trace": trace_summary.get("source_trace", trace_path),
        "step_count": max(
            int(existing.get("step_count", 0) or 0),
            int(trace_summary.get("step_count", 0) or 0),
            step_count,
        ),
        "findings_summary": findings_summary or existing.get("findings_summary", ""),
        "attempts": _merge_unique(
            existing.get("attempts", []),
            trace_summary.get("attempts", []),
            limit=80,
        ),
        "key_results": _merge_unique(
            existing.get("key_results", []),
            trace_summary.get("key_results", []),
            limit=60,
        ),
        "model_notes": _merge_unique(
            existing.get("model_notes", []),
            trace_summary.get("model_notes", []),
            limit=12,
        ),
        "errors": _merge_unique(
            existing.get("errors", []),
            trace_summary.get("errors", []),
            limit=12,
        ),
        "candidate_flags": _merge_candidate_flags(
            existing.get("candidate_flags", []),
            candidate_flags,
            limit=30,
        ),
        "confirmed": bool(confirmed or existing.get("confirmed", False)),
        "workspace_dir": workspace_dir or existing.get("workspace_dir", ""),
        "next_step": _compact(next_step or existing.get("next_step", ""), 2000),
    }
    _write_json_atomic(path, checkpoint)
    return checkpoint


def refresh_checkpoint_from_latest_trace(
    path: Path,
    *,
    challenge_name: str,
    model_spec: str,
    model_id: str,
    current_trace: str,
    workspace_dir: str,
    log_dir: str = "logs",
) -> dict[str, Any]:
    """Recover progress even when a prior process died before graceful checkpointing."""
    existing = load_solver_checkpoint(path)
    latest = find_latest_trace(
        challenge_name,
        model_id,
        log_dir=log_dir,
        exclude=current_trace,
    )
    if latest is None:
        return existing
    existing_updated = float(existing.get("updated_at", 0) or 0)
    if existing and latest.stat().st_mtime <= existing_updated:
        return existing
    return save_solver_checkpoint(
        path,
        challenge_name=challenge_name,
        model_spec=model_spec,
        trace_path=str(latest),
        step_count=int(existing.get("step_count", 0) or 0),
        findings_summary=str(existing.get("findings_summary", "")),
        confirmed=bool(existing.get("confirmed", False)),
        workspace_dir=workspace_dir,
        next_step=str(existing.get("next_step", "")),
    )


def build_resume_context(checkpoint: dict[str, Any], *, max_chars: int = 12_000) -> str:
    if not checkpoint:
        return ""

    lines = [
        "## Persistent Resume Checkpoint",
        "A previous solver session exists. Continue from this state instead of restarting.",
        f"Previous steps recorded: {checkpoint.get('step_count', 0)}",
    ]
    if checkpoint.get("workspace_dir"):
        lines.append(
            "The previous /challenge/workspace has been restored. Inspect and reuse its files "
            "before extracting attachments or recreating scripts."
        )
    if checkpoint.get("findings_summary"):
        lines += ["", "### Previous findings", _compact(checkpoint["findings_summary"], 1600)]

    if checkpoint.get("next_step"):
        lines += ["", "### Next step", _compact(checkpoint["next_step"], 2000)]

    candidates = checkpoint.get("candidate_flags", [])[-15:]
    if candidates:
        lines += ["", "### Flag candidates already seen/submitted"]
        for item in candidates:
            lines.append(f"- {item.get('flag', '?')}: {_compact(item.get('result'), 350)}")

    attempts = checkpoint.get("attempts", [])[-20:]
    if attempts:
        lines += ["", "### Recent attempted approaches"]
        lines.extend(f"- {_compact(item, 500)}" for item in attempts)

    results = checkpoint.get("key_results", [])[-20:]
    if results:
        lines += ["", "### Key prior results"]
        lines.extend(f"- {_compact(item, 650)}" for item in results)

    notes = checkpoint.get("model_notes", [])[-6:]
    if notes:
        lines += ["", "### Prior reasoning / suggested next steps"]
        lines.extend(f"- {_compact(item, 700)}" for item in notes)

    errors = checkpoint.get("errors", [])[-6:]
    if errors:
        lines += ["", "### Prior errors to avoid"]
        lines.extend(f"- {_compact(item, 500)}" for item in errors)

    lines += [
        "",
        "Resume rules:",
        "- First inspect /challenge/workspace and the checkpoint above.",
        "- Do not repeat extraction, setup, failed commands, or rejected flag submissions unless necessary.",
        "- Continue with the most promising unfinished next step and preserve useful artifacts in /challenge/workspace.",
    ]
    context = "\n".join(lines)
    return context if len(context) <= max_chars else context[:max_chars] + "\n[checkpoint truncated]"
