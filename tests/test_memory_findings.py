"""Tests for the opt-in cross-task memory and findings subsystems (R4)."""

from __future__ import annotations

import json

import pytest

from backend.memory import RECALL_MAX_RESULTS, CrossTaskMemory, FindingsRecorder


class TestCrossTaskMemory:
    @pytest.fixture
    def store(self, tmp_path) -> CrossTaskMemory:
        return CrossTaskMemory(path=str(tmp_path / "memory.jsonl"))

    def test_remember_and_recall(self, store: CrossTaskMemory) -> None:
        store.remember("django debug page leaks stack traces; check /__debug__")
        store.remember("nginx alias traversal: use ../ after the location prefix")
        out = store.recall("django debug")
        assert "__debug__" in out
        assert "nginx" not in out

    def test_recall_all_terms_required(self, store: CrossTaskMemory) -> None:
        store.remember("ssti in flask templates")
        assert "no matching" in store.recall("ssti django").lower()
        assert "flask" in store.recall("ssti")

    def test_recall_empty_query(self, store: CrossTaskMemory) -> None:
        assert "empty query" in store.recall("")

    def test_remember_empty_ignored(self, store: CrossTaskMemory) -> None:
        store.remember("   ")
        assert not store.path.exists() or store.path.read_text(encoding="utf-8") == ""

    def test_recall_caps_results(self, store: CrossTaskMemory) -> None:
        for i in range(RECALL_MAX_RESULTS + 5):
            store.remember(f"php loose comparison auth bypass variant {i}")
        out = store.recall("php loose comparison")
        # newest first, capped at RECALL_MAX_RESULTS
        assert out.count(". ") >= RECALL_MAX_RESULTS - 1
        assert f"variant {RECALL_MAX_RESULTS + 4}" in out  # newest entry present
        assert "variant 0" not in out  # oldest trimmed

    def test_remember_truncates_long_notes(self, store: CrossTaskMemory) -> None:
        store.remember("x" * 5_000)
        line = store.path.read_text(encoding="utf-8").strip()
        assert len(json.loads(line)["text"]) == 2_000


class TestFindingsRecorder:
    def test_record_writes_jsonl(self, tmp_path) -> None:
        rec = FindingsRecorder("chal a/b", findings_dir=str(tmp_path))
        rec.record("sql injection in login", "payload: ' or 1=1 --", "high")
        line = rec.path.read_text(encoding="utf-8").strip()
        entry = json.loads(line)
        assert entry["challenge"] == "chal a/b"
        assert entry["severity"] == "high"
        assert "sql injection" in entry["summary"]

    def test_record_empty_summary_ignored(self, tmp_path) -> None:
        rec = FindingsRecorder("c", findings_dir=str(tmp_path))
        rec.record("", "evidence", "low")
        assert not rec.path.exists() or rec.path.read_text(encoding="utf-8") == ""

    def test_filename_sanitized(self, tmp_path) -> None:
        rec = FindingsRecorder("c:/weird<>name", findings_dir=str(tmp_path))
        assert "<" not in rec.path.name and ":" not in rec.path.name


class TestOptInToolRegistration:
    def test_default_settings_leave_subsystems_off(self) -> None:
        from backend.agents.headless_solver import HeadlessSolver
        from backend.cost_tracker import CostTracker
        from backend.prompts import ChallengeMeta

        solver = HeadlessSolver(
            model_spec="deepseek/deepseek-chat",
            meta=ChallengeMeta(name="c", connection_info="10.0.0.1:80"),
            platform=object(),  # type: ignore[arg-type]
            cost_tracker=CostTracker(),
            settings=object(),
        )
        assert solver.memory is None
        assert solver.findings is None

    def test_enabled_settings_create_subsystems(self) -> None:
        from backend.agents.headless_solver import HeadlessSolver
        from backend.cost_tracker import CostTracker
        from backend.prompts import ChallengeMeta

        class Settings:
            cross_task_memory_enabled = True
            findings_output_enabled = True

        solver = HeadlessSolver(
            model_spec="deepseek/deepseek-chat",
            meta=ChallengeMeta(name="c", connection_info="10.0.0.1:80"),
            platform=object(),  # type: ignore[arg-type]
            cost_tracker=CostTracker(),
            settings=Settings(),
        )
        assert solver.memory is not None
        assert solver.findings is not None
        # No stray files left in the repo root: stores live under logs/
        from pathlib import Path

        Path("logs").mkdir(parents=True, exist_ok=True)  # memory store creates it on first write only