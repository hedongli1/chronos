"""Shared dependency types — avoids circular imports between agents and tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from backend.cost_tracker import CostTracker
from backend.platforms import CompetitionPlatform
from backend.prompts import ChallengeMeta
from backend.sandbox import DockerSandbox

if TYPE_CHECKING:
    from backend.message_bus import ChallengeMessageBus

# Type for the deduped submit callback: (flag) -> (display, is_confirmed)
SubmitFn = Callable[[str], Coroutine[Any, Any, tuple[str, bool]]]


@dataclass
class SolverDeps:
    sandbox: DockerSandbox
    platform: CompetitionPlatform
    challenge_dir: str
    challenge_name: str
    workspace_dir: str
    use_vision: bool
    cost_tracker: CostTracker | None = None
    confirmed_flag: str | None = None
    message_bus: ChallengeMessageBus | None = None
    model_spec: str = ""
    submit_fn: SubmitFn | None = None  # Deduped flag submission via swarm
    no_submit: bool = False
    notify_coordinator: Callable[[str], Coroutine[Any, Any, None]] | None = None


@dataclass
class HeadlessDeps:
    """Dependencies for the sandbox-free solver (field-compatible with TracingToolset)."""

    platform: CompetitionPlatform
    challenge_name: str
    meta: ChallengeMeta
    use_vision: bool
    cost_tracker: CostTracker | None = None
    confirmed_flag: str | None = None
    message_bus: ChallengeMessageBus | None = None
    model_spec: str = ""
    submit_fn: SubmitFn | None = None
    no_submit: bool = False
    notify_coordinator: Callable[[str], Coroutine[Any, Any, None]] | None = None
    allow_exec: bool = True
    # Per solver-run working directory for run_python (isolated, kept for
    # post-mortems). Empty = fall back to the shared system temp dir.
    workdir: str = ""
    deadline_monotonic: float = 0.0
    flags_total: int = 1
    flags_accepted: int = 0
    hint_used: bool = False
    # Flags the platform actively rejected this run — never resubmit these.
    rejected_flags: list[str] = field(default_factory=list)
    # Progress marker for the hint stall gate: step index of the last confirmed
    # flag, accepted partial credit, or newly received sibling finding.
    last_progress_step: int = 0
    # Target lock: hosts from the task's connection_info. Non-killchain tasks
    # hard-block other hosts; killchain tasks allow pivots but warn.
    allowed_hosts: frozenset[str] = frozenset()
    scenario: str = ""


@dataclass
class CoordinatorDeps:
    platform: CompetitionPlatform
    cost_tracker: CostTracker
    settings: Any
    platform_base_url: str = ""
    model_specs: list[str] = field(default_factory=list)
    challenges_root: str = "challenges"
    no_submit: bool = False
    max_concurrent_challenges: int = 10
    challenge_time_limit_seconds: int = 1800
    target_lock_timeout_seconds: int = 120

    msg_port: int = 0  # 0 = auto-pick free port
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 0  # 0 = auto-pick free port

    # Runtime state
    coordinator_inbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    operator_inbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    spawn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_spawns: set[str] = field(default_factory=set)
    open_containers: set[str] = field(default_factory=set)  # challenges with a live range container
    early_retried: set[str] = field(default_factory=set)  # early-exit challenges already retried once
    spawn_failures: dict[str, int] = field(default_factory=dict)  # consecutive spawn failures per challenge
    last_ghost_reap: float = 0.0  # last time stale platform containers were reaped
    swarms: dict[str, Any] = field(default_factory=dict)
    swarm_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    deferred_challenges: dict[str, str] = field(default_factory=dict)
    results: dict[str, dict] = field(default_factory=dict)
    challenge_dirs: dict[str, str] = field(default_factory=dict)
    challenge_metas: dict[str, ChallengeMeta] = field(default_factory=dict)
