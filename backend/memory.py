"""Cross-task memory (opt-in) and findings output (opt-in).

Both subsystems are OFF by default and change nothing for a plain TSecBench run
(the baseline's principle 1: no cross-task memory, no report artifacts — see
docs/rewrite-plan.md "已拍板"). Operators enable them explicitly:

- CROSS_TASK_MEMORY_ENABLED=1: the solver gains `remember(text)` and
  `recall_memory(query)` tools backed by an append-only JSONL store under
  logs/cross-task-memory.jsonl. Recall is a plain subsequence/keyword scan —
  no RAG, no embedding, no LLM coordinator (Less-Is-More). Memory is
  self-reported and unverified by design; it is NEVER injected into the system
  prompt, only fetched on explicit recall.
- FINDINGS_OUTPUT_ENABLED=1: the solver gains `record_finding(evidence,
  summary, severity)` writing logs/findings-<challenge>.jsonl. This is a pure
  observability output ("发现即记录漏洞"), never fed back into scoring.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

MEMORY_PATH = "logs/cross-task-memory.jsonl"
FINDINGS_DIR = "logs"

# Hard cap on how many recent entries an unbounded recall may return — keeps a
# runaway recall from flooding the model's context.
RECALL_MAX_RESULTS = 8
RECALL_CONTEXT_CHARS = 220


class CrossTaskMemory:
    """Append-only cross-task memory store (JSONL), process-local, no locking
    beyond what the asyncio task interleaving already affords the solver."""

    def __init__(self, path: str = MEMORY_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def remember(self, text: str) -> None:
        text = (text or "").strip()
        if not text or len(text) > 2_000:
            if text:
                text = text[:2_000]
            else:
                return
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(
                    {"ts": time.time(), "text": text},
                    ensure_ascii=False,
                ) + "\n")
        except OSError:
            # Memory is best-effort: a full disk must never break a solver run.
            pass

    def recall(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "recall_memory: empty query."
        terms = [t for t in re.split(r"[^\w一-鿿]+", query.lower()) if t]
        if not terms:
            return "recall_memory: query has no searchable terms."
        hits: list[str] = []
        try:
            with self.path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    text = str(entry.get("text", ""))
                    low = text.lower()
                    # All query terms must appear in the entry (any order) —
                    # a plain, deterministic matcher.
                    if all(term in low for term in terms):
                        hits.append(text)
        except OSError:
            return "recall_memory: memory store unavailable."
        if not hits:
            return "recall_memory: no matching memories."
        # Newest first, capped.
        hits = list(reversed(hits))[:RECALL_MAX_RESULTS]
        out = [f"{i + 1}. {h[:RECALL_CONTEXT_CHARS]}" for i, h in enumerate(hits)]
        header = (
            "Recalled memories (self-reported notes from earlier tasks — "
            "UNVERIFIED, may reference other targets; treat as leads, never as facts):"
        )
        return header + "\n" + "\n".join(out)


class FindingsRecorder:
    """Append-only per-challenge findings log (JSONL) — pure observability."""

    def __init__(self, challenge_name: str, findings_dir: str = FINDINGS_DIR) -> None:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", challenge_name).strip("-") or "challenge"
        self.path = Path(findings_dir) / f"findings-{safe}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.challenge_name = challenge_name

    def record(self, summary: str, evidence: str, severity: str) -> None:
        summary, evidence = (summary or "").strip()[:1_000], (evidence or "").strip()[:4_000]
        if not summary:
            return
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": time.time(),
                    "challenge": self.challenge_name,
                    "summary": summary,
                    "evidence": evidence,
                    "severity": severity,
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass  # observability artifact; never fail the run