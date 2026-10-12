"""Local / offline platform adapter.

Reads challenges from a local directory; does not require any external
competition platform. Flag submissions are recorded locally only.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from backend.platforms import SubmitResult

logger = logging.getLogger(__name__)


class LocalPlatform:
    """Platform adapter that reads challenges from the local filesystem.

    - ``fetch_challenge_stubs`` / ``fetch_all_challenges`` scan
      ``challenges_root`` for sub-directories containing ``metadata.yml``.
    - ``fetch_solved_names`` returns challenges that have a ``solved``
      marker file (``.solved``) in their directory.
    - ``submit_flag`` checks the flag against ``metadata.yml``'s
      ``flag`` field if present, otherwise always reports "unknown".
    - ``pull_challenge`` is a no-op — challenges are already local.
    """

    def __init__(self, challenges_root: str = "challenges") -> None:
        self._root = Path(challenges_root)
        self._solved_cache: set[str] = set()

    def _read_metas(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if not self._root.exists():
            return results
        for d in sorted(self._root.iterdir()):
            meta_path = d / "metadata.yml"
            if not meta_path.exists():
                continue
            try:
                meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
                if meta and isinstance(meta, dict):
                    meta["_dir"] = str(d)
                    results.append(meta)
            except Exception as e:
                logger.warning("Failed to read %s: %s", meta_path, e)
        return results

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        return self._read_metas()

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        return self._read_metas()

    async def fetch_solved_names(self) -> set[str]:
        solved: set[str] = set()
        if not self._root.exists():
            return solved
        for d in self._root.iterdir():
            if (d / ".solved").exists():
                meta_path = d / "metadata.yml"
                if meta_path.exists():
                    try:
                        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
                        if meta and isinstance(meta, dict) and meta.get("name"):
                            solved.add(meta["name"])
                    except Exception:
                        pass
        self._solved_cache = solved
        return solved

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        flag = flag.strip()
        metas = self._read_metas()
        meta = next((m for m in metas if m.get("name") == challenge_name), None)
        if not meta:
            return SubmitResult(
                "unknown",
                f"Challenge '{challenge_name}' not found locally",
                f"UNKNOWN — '{challenge_name}' not in local challenges",
            )

        expected = meta.get("flag")
        if not expected:
            (Path(meta["_dir"]) / ".solved").touch()
            return SubmitResult(
                "correct",
                "No expected flag defined — auto-accepted",
                f'ACCEPTED — "{flag}" (no expected flag configured, auto-accepted)',
            )

        if flag == expected.strip():
            (Path(meta["_dir"]) / ".solved").touch()
            return SubmitResult(
                "correct",
                "Flag matches expected value",
                f'CORRECT — "{flag}" accepted.',
            )

        return SubmitResult(
            "incorrect",
            "Flag does not match expected value",
            f'INCORRECT — "{flag}" rejected.',
        )

    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str:
        name = challenge.get("name", "challenge")
        slug = name.lower().replace(" ", "-")
        ch_dir = Path(output_dir) / slug
        ch_dir.mkdir(parents=True, exist_ok=True)
        if "metadata.yml" not in [p.name for p in ch_dir.iterdir()]:
            (ch_dir / "metadata.yml").write_text(
                yaml.dump(challenge, allow_unicode=True, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )
        return str(ch_dir)

    async def close(self) -> None:
        pass
