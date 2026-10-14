"""Generic competition platform abstractions.

This module defines the platform-neutral interface the agent uses to discover
challenges, submit answers, and synchronize solved state. The TSecBench adapter
implements this protocol so the coordinator / solver stack stays
platform-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class SubmitResult:
    """Normalized answer-submission result returned by any platform adapter."""

    status: str
    message: str
    display: str
    # Multi-flag challenges (TSec): True only when every flag is now accepted.
    challenge_complete: bool = False
    # Human-readable flag progress, e.g. "2/3 flags".
    progress: str = ""

    @property
    def confirmed(self) -> bool:
        return self.status in {"correct", "already_solved"}


@runtime_checkable
class CompetitionPlatform(Protocol):
    """Platform-neutral interface used by the agent coordinator."""

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]: ...
    async def fetch_all_challenges(self) -> list[dict[str, Any]]: ...
    async def fetch_solved_names(self) -> set[str]: ...
    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult: ...
    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str: ...
    async def close(self) -> None: ...
