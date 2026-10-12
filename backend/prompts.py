"""System prompt builder + ChallengeMeta.

The prompt is a neutral task contract: objective, tool contract, submission
format, and hard constraints. It encodes NO attack methodology, no per-scenario
playbook, and no "first do X then Y" procedure — the model's own reasoning
decides how to search. (`_guess_scenario` survives only to pick a per-challenge
time budget and to gate the target lock, never to shape the prompt text.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ChallengeMeta:
    name: str = "Unknown"
    category: str = ""
    value: int = 0
    description: str = ""
    tags: list[str] = field(default_factory=list)
    connection_info: str = ""
    hints: list[dict[str, Any]] = field(default_factory=list)
    solves: int = 0
    unique_code: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    # Multi-flag / benchmark semantics (TSec)
    flag_count: int = 1
    correct_flag_count: int = 0
    level: int = 0
    container_status: str = "stopped"

    @classmethod
    def from_yaml(cls, path: str | Path) -> ChallengeMeta:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(
            name=data.get("name", "Unknown"),
            category=data.get("category", ""),
            value=data.get("value", 0),
            description=data.get("description", ""),
            tags=data.get("tags", []),
            connection_info=data.get("connection_info", ""),
            hints=data.get("hints", []),
            solves=data.get("solves", 0),
            unique_code=data.get("unique_code", ""),
            attachments=data.get("attachments", []),
            flag_count=int(data.get("flag_count", 1) or 1),
            correct_flag_count=int(data.get("correct_flag_count", 0) or 0),
            level=int(data.get("level", 0) or 0),
            container_status=data.get("container_status", "stopped"),
        )


def _guess_scenario(meta: ChallengeMeta) -> str:
    """Infer the offensive-security dimension from code/name/category signals.

    NOTE: this is a free-text keyword heuristic over name/description — it is
    appropriate for picking which playbook text to show (low stakes, wrong
    guess just shows a slightly less relevant playbook), but it must NOT be
    used on its own to decide whether the target lock is disabled. See
    `_killchain_lock_open` below for the safety-relevant check.
    """
    blob = f"{meta.unique_code} {meta.name} {meta.category} {meta.description}".lower()
    for key in ("killchain", "evasion", "exploit", "cloud", "binary", "web"):
        if key in blob:
            return key
    for word, key in (
        ("渗透", "killchain"), ("横向", "killchain"), ("内网", "killchain"), ("kill chain", "killchain"),
        ("穿透", "killchain"), ("多级", "killchain"), ("跳板", "killchain"),
        ("免杀", "evasion"), ("绕过", "evasion"), ("规避", "evasion"), ("waf", "evasion"), ("edr", "evasion"),
        ("rce", "exploit"), ("poc", "exploit"), ("利用", "exploit"), ("exp_", "exploit"),
        ("挖掘", "exploit"), ("审计", "exploit"), ("安全测试", "exploit"), ("cve", "exploit"),
        ("comfyui", "exploit"), ("gradio", "exploit"), ("ollama", "exploit"), ("dify", "exploit"),
        ("k8s", "cloud"), ("kubernetes", "cloud"), ("容器逃逸", "cloud"), ("iam", "cloud"), ("云", "cloud"),
        ("pwn", "binary"), ("reverse", "binary"), ("逆向", "binary"), ("二进制", "binary"),
        ("stack", "binary"), ("heap", "binary"), ("overflow", "binary"), ("bof", "binary"),
        ("注入", "web"), ("sql", "web"), ("xss", "web"),
    ):
        if word in blob:
            return key
    return "web"


def _killchain_lock_open(meta: ChallengeMeta, allowed_hosts: frozenset[str]) -> bool:
    """Decide whether the target lock may be disabled for cross-host pivoting.

    This is deliberately STRICTER than `_guess_scenario`: a single mis-tagged
    word in a task's name/description must never be able to turn off the lock
    on its own (that was the c-03 incident — a non-killchain task misdetected
    as killchain ran unlocked on the shared VPN). We only open the lock when
    BOTH hold:
      1. The free-text heuristic guesses "killchain", AND
      2. The task's OWN connection_info already structurally announces more
         than one host at start — i.e. there is something real to pivot
         between. A genuine multi-stage task legitimately starts this way;
         a single-host task has nothing to gain from an open lock, so if the
         keyword guess is wrong there, it is also harmless (nowhere to drift).
    """
    if _guess_scenario(meta) != "killchain":
        return False
    return len(allowed_hosts) >= 2


def build_headless_prompt(
    meta: ChallengeMeta,
    *,
    remaining_seconds: int | None = None,
    allow_exec: bool = True,
) -> str:
    """Build the solver's system prompt: a neutral task contract.

    Objective + tool contract + submission format + hard constraints. No attack
    methodology, no per-scenario playbook, no procedure — how to search is left
    entirely to the model.
    """
    addrs = [a.strip() for a in re.split(r"[,\s]+", meta.connection_info) if a.strip()]

    lines: list[str] = [
        "You are an autonomous security AI solving one task on an authorized benchmark.",
        "You have no local toolchain beyond the tools listed below; your strengths are",
        "reasoning, protocol comprehension, and precise payload construction.",
        "",
        "## Objective",
        "This task is a SEARCH. Explore the authorized target and capture every flag you",
        "find by calling submit_flag the moment you have it — do not hoard flags or fixate",
        "on a single one. Many tasks have multiple flags (partial credit applies); the task",
        "is complete only when all of them are accepted.",
        "",
        "## Task",
        f"**Name**       : {meta.name}",
    ]
    if meta.unique_code:
        lines.append(f"**Unique Code**: {meta.unique_code}")
    if meta.category:
        lines.append(f"**Difficulty** : {meta.category}")
    if meta.value:
        lines.append(f"**Score**      : {meta.value}")
    if meta.level:
        lines.append(f"**Level**      : {meta.level}")
    flags_total = max(1, meta.flag_count)
    if flags_total > 1 or meta.correct_flag_count:
        lines.append(
            f"**Flags**      : {meta.correct_flag_count}/{flags_total} already accepted."
        )
    if remaining_seconds is not None:
        lines.append(f"**Time budget**: ~{max(0, remaining_seconds) // 60} minutes left for this task.")
    lines += ["", "## Description", meta.description or "_No description provided._", ""]

    lines += ["## Target (authorized scope)"]
    if addrs:
        lines += [
            f"Assigned target address(es): {', '.join(addrs)}",
            "These are the ONLY hosts in scope. A target lock is enforced: http_request,",
            "tcp_send, and run_python socket connections to any other host are blocked.",
            "Other hosts on this VPN belong to other tasks — a flag found there is invalid",
            "here and submitting it wastes attempts. Services may speak HTTP or raw TCP.",
        ]
    else:
        lines += [
            "No target address was provided with this task. Derive the intended target from",
            "the description; if an address arrives later in a platform message, use that.",
        ]

    lines += [
        "",
        "## Tools",
        "- `http_request(url, method, headers, body)`: full-control HTTP to the target.",
        "- `tcp_send(host, port, payload, payload_hex)`: one raw TCP exchange (line or binary).",
        "- `submit_flag(flag)`: submit one captured flag. Submit each as soon as you have it.",
    ]
    if allow_exec:
        lines.append(
            "- `run_python(code)`: local CPython scratchpad (stdlib plus installed libraries). "
            "print() what you need."
        )
    lines += [
        "- `request_hint()`: fetch the platform hint for this task. It DEDUCTS score and is "
        "limited to once per task — use it only when your own ideas are exhausted.",
        "- `task_status()`: accepted/total flag count and remaining time.",
        "- `check_findings()` / `notify_coordinator(msg)`: read sibling findings / report upward.",
        "",
        "## Constraints",
        "- Submit flags verbatim as observed in THIS task's target output. Never guess, fabricate,",
        "  or derive a flag from the title/description.",
        "- The local disk is shared and untrusted: a flag found in a local file may belong to a",
        "  different task — it does not count. Only flags from this target's responses count.",
        "- Never resubmit a flag already rejected; repeated wrong submissions trip a task-wide lock.",
        "- Treat task_status as ground truth after each submit_flag: if the accepted count did not",
        "  rise, the flag was not accepted regardless of how the acknowledgement read.",
        "- Stay within the authorized scope above.",
    ]

    return "\n".join(lines)
