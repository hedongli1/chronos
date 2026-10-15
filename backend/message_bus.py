"""Per-challenge message bus for inter-agent communication."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class Finding:
    model: str
    content: str
    timestamp: float = field(default_factory=time.time)


MAX_FINDINGS = 200


@dataclass
class ChallengeMessageBus:
    """Append-only shared findings list with per-model cursors."""

    findings: list[Finding] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def post(self, model: str, content: str) -> None:
        """Post a finding from a solver."""
        async with self._lock:
            self.findings.append(Finding(model=model, content=content))
            if len(self.findings) > MAX_FINDINGS:
                trim = len(self.findings) - MAX_FINDINGS
                self.findings = self.findings[trim:]
                self.cursors = {k: max(0, v - trim) for k, v in self.cursors.items()}

    async def check(self, model: str) -> list[Finding]:
        """Get unread findings from other models. Advances the cursor."""
        async with self._lock:
            cursor = self.cursors.get(model, 0)
            unread = [f for f in self.findings[cursor:] if f.model != model]
            self.cursors[model] = len(self.findings)
            return unread

    async def search(self, query: str, exclude_model: str, limit: int = 8) -> list[Finding]:
        """Keyword-search ALL sibling findings, most-recent first.

        Unlike `check` (push/poll-once: returns only unread items and advances
        the cursor), `search` is **repeatable and on-demand** — it never touches
        any cursor, so a solver can pull a specific endpoint/credential/error a
        sibling mentioned earlier even after it has already "read" the backlog.
        This is the process-level knowledge exchange ARTEX calls
        `search_all_worker_traces`, adapted to chronos's homogeneous-solver bus
        (no role decomposition, no LLM coordinator — baseline-safe).

        Matching: every whitespace-separated query term must appear in the
        finding's content (case-insensitive substring). Returns at most `limit`
        of the most recent matches, never the caller's own findings.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        terms = q.split()
        async with self._lock:
            matches: list[Finding] = []
            for f in reversed(self.findings):  # most recent first
                if f.model == exclude_model:
                    continue
                content = f.content.lower()
                if all(t in content for t in terms):
                    matches.append(f)
                    if len(matches) >= limit:
                        break
            return matches

    async def broadcast(self, content: str, source: str = "coordinator") -> None:
        """Coordinator broadcasts a message to all solvers."""
        await self.post(source, content)

    def format_unread(self, findings: list[Finding]) -> str:
        """Format findings for injection into a solver prompt."""
        if not findings:
            return ""
        parts = [f"[{f.model}] {f.content}" for f in findings]
        return (
            "**Findings from other agents** (unverified self-reports, not confirmed "
            "facts — verify any claimed endpoint, file, credential, or string with one "
            "cheap check before relying on it; files on the shared disk may belong to "
            "a different challenge):\n\n" + "\n\n".join(parts)
        )

    def format_search(self, findings: list[Finding], query: str) -> str:
        """Format on-demand search hits (same unverified-self-report caveat)."""
        if not findings:
            return ""
        parts = [f"[{f.model}] {f.content}" for f in findings]
        return (
            f"**{len(findings)} sibling finding(s) matching {query!r}** (most recent "
            "first; unverified self-reports, not confirmed facts — verify any claimed "
            "endpoint, file, credential, or string with one cheap check before relying "
            "on it):\n\n" + "\n\n".join(parts)
        )
