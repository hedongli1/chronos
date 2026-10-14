"""TSec Benchmark platform adapter.

Talks directly to the TSec Benchmark REST API:
  GET    /openapi/v1/challenges
  POST   /openapi/v1/challenges/start?unique_code=<code>
  POST   /openapi/v1/challenges/submit
  GET    /openapi/v1/challenges/hint?unique_code=<code>
  POST   /openapi/v1/challenges/close?unique_code=<code>

All requests carry the header ``BENCHMARK_TOKEN: <token>``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from backend.platforms import SubmitResult

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


@dataclass
class TSecBenchmarkPlatform:
    """Adapter for the TSec Benchmark evaluation platform."""

    base_url: str = "https://tsecbench.zc.tencent.com"
    token: str = ""
    # TLS verification (default on; disabled only via explicit operator config).
    verify_tls: bool = True

    _client: httpx.AsyncClient | None = field(default=None, repr=False)
    _challenge_index: dict[str, dict[str, Any]] = field(default_factory=dict)
    _solved_cache: set[str] = field(default_factory=set)
    # unique_code -> container addrs for containers we started this process.
    # Prevents duplicate /start calls (each one spawns another range instance).
    _started_addrs: dict[str, list[str]] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Ensure base_url ends with "/" so relative paths append correctly.
            # e.g. base_url="https://x.com/benchmark" + "openapi/v1/challenges"
            #  → "https://x.com/benchmark/openapi/v1/challenges"
            url = self.base_url.rstrip("/")
            self._client = httpx.AsyncClient(
                base_url=url + "/",
                verify=self.verify_tls,
                timeout=30.0,
                # Direct connection: the platform is a domestic service and must
                # not depend on a local proxy that the VPN tunnel can break.
                trust_env=False,
                headers={
                    "User-Agent": USER_AGENT,
                    "BENCHMARK_TOKEN": self.token,
                },
            )
        return self._client

    async def _api_get(self, path: str, params: dict | None = None) -> Any:
        client = await self._ensure_client()
        resp = await client.get(path, params=params)
        return await self._parse_response(resp, "GET", path)

    async def _api_post(self, path: str, params: dict | None = None, json_body: dict | None = None) -> Any:
        client = await self._ensure_client()
        resp = await client.post(path, params=params, json=json_body)
        return await self._parse_response(resp, "POST", path)

    async def _parse_response(self, resp: httpx.Response, method: str, path: str) -> Any:
        body = resp.text
        if resp.status_code >= 400:
            logger.warning(
                "TSec API %s %s -> HTTP %d | body: %s",
                method, path, resp.status_code, body[:500],
            )
            resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            logger.error(
                "TSec API %s %s -> HTTP %d, non-JSON response (len=%d): %s",
                method, path, resp.status_code, len(body), body[:500],
            )
            raise RuntimeError(
                f"TSec API {method} {path} returned non-JSON response "
                f"(HTTP {resp.status_code}): {body[:200]}"
            ) from None

    def _slug(self, name: str) -> str:
        slug = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name.lower().strip())
        slug = re.sub(r"[\s_]+", "-", slug)
        return re.sub(r"-+", "-", slug).strip("-") or "challenge"

    # ------------------------------------------------------------------ #
    #  CompetitionPlatform implementation
    # ------------------------------------------------------------------ #

    async def fetch_challenge_stubs(self) -> list[dict[str, Any]]:
        return await self.fetch_all_challenges()

    async def fetch_all_challenges(self) -> list[dict[str, Any]]:
        try:
            data = await self._api_get("openapi/v1/challenges")
        except httpx.HTTPStatusError as e:
            # Fatal states (task finished / invalid token) must NOT be mistaken
            # for "zero challenges" — surface them instead of returning [].
            body = ""
            try:
                body = e.response.text[:300]
            except Exception:
                pass
            if "already finished" in body or "task_not_found" in body:
                raise RuntimeError(f"TSec task is not runnable: {body}") from e
            logger.warning("TSec fetch_all_challenges failed: %s", e)
            return []
        except Exception as e:
            logger.warning("TSec fetch_all_challenges failed: %s", e)
            return []
        if not isinstance(data, list):
            logger.warning("TSec API returned non-list: %s", str(data)[:200])
            return []
        challenges: list[dict[str, Any]] = []
        fresh_index: dict[str, dict[str, Any]] = {}

        for item in data:
            unique_code = item.get("unique_code", "")
            name = item.get("name", unique_code)
            is_completed = item.get("is_completed", False)
            correct = item.get("correct_flag_count", 0)
            total = item.get("flag_count", 1)

            normalized = {
                "name": name,
                "unique_code": unique_code,
                "category": item.get("difficulty", ""),
                "value": item.get("total_score", 0),
                "solves": 0,
                "description": item.get("description", ""),
                "is_completed": is_completed,
                "correct_flag_count": correct,
                "flag_count": total,
                "container_status": item.get("container_status", "stopped"),
                "container_addr": item.get("container_addr", []),
                "level": item.get("level", 0),
            }
            challenges.append(normalized)
            fresh_index[name] = normalized

            if is_completed:
                self._solved_cache.add(name)

        # Rebuild the index from the newest listing instead of merging into a
        # stale copy — container_addr / status / flag counts must reflect the
        # platform's current state (a cached recycled address would otherwise
        # be reused with no freshness check). Solved state is terminal on the
        # platform, so _solved_cache is only ever added to.
        self._challenge_index = fresh_index
        return challenges

    async def fetch_solved_names(self) -> set[str]:
        if not self._challenge_index:
            await self.fetch_all_challenges()
        return set(self._solved_cache)

    async def health_check(self) -> tuple[int, int]:
        """Strict read-only probe for smoke tests: raises on any API error.

        Unlike fetch_all_challenges (which degrades to [] for the poller),
        this surfaces auth/URL problems to the operator. Returns
        (total_challenges, completed_challenges).
        """
        data = await self._api_get("openapi/v1/challenges")
        if not isinstance(data, list):
            raise RuntimeError(f"unexpected challenges payload: {str(data)[:200]}")
        solved = sum(1 for item in data if item.get("is_completed"))
        return len(data), solved

    async def submit_flag(self, challenge_name: str, flag: str) -> SubmitResult:
        ch = self._challenge_index.get(challenge_name)
        if not ch:
            await self.fetch_all_challenges()
            ch = self._challenge_index.get(challenge_name)
        if not ch:
            return SubmitResult(
                "unknown",
                f"Challenge '{challenge_name}' not found",
                f"UNKNOWN — '{challenge_name}' not found on platform",
            )

        unique_code = ch["unique_code"]
        try:
            resp = await self._api_post(
                "openapi/v1/challenges/submit",
                json_body={"unique_code": unique_code, "flag": flag.strip()},
            )
        except httpx.HTTPStatusError as e:
            body = {}
            try:
                body = e.response.json()
            except Exception:
                pass
            code = body.get("code", "")
            message = body.get("message", str(e))

            if code == "duplicate":
                # A duplicate is an ack for an already-banked flag. On multi-flag
                # tasks it must NOT complete the challenge — remaining flags are
                # still open. Single-flag tasks: duplicate means fully solved.
                total_flags = int(ch.get("flag_count", 1) or 1)
                if total_flags > 1:
                    # A duplicate alone can't say whether the LAST missing
                    # flag was also banked by a sibling. Ask the platform for
                    # the current state instead of guessing "still open".
                    try:
                        await self.fetch_all_challenges()
                        fresh = self._challenge_index.get(challenge_name) or {}
                        if fresh.get("is_completed"):
                            return SubmitResult(
                                "already_solved",
                                message,
                                f'DUPLICATE — "{flag}" already banked, and the platform '
                                "now reports the challenge completed.",
                                challenge_complete=True,
                                progress="all flags banked",
                            )
                    except Exception:
                        pass  # fall through to the conservative below
                    return SubmitResult(
                        "correct",
                        message,
                        f'DUPLICATE — "{flag}" was already banked for {challenge_name}. '
                        "Other flags are still open — pivot to a different attack surface.",
                        challenge_complete=False,
                        progress=f"duplicate of a banked flag (/{total_flags} total)",
                    )
                return SubmitResult(
                    "already_solved",
                    message,
                    f'DUPLICATE — "{flag}" already submitted for {challenge_name}.',
                    challenge_complete=True,
                )
            if code == "invalid_state":
                return SubmitResult(
                    "unknown",
                    message,
                    f"INVALID_STATE — {message}",
                )
            return SubmitResult("unknown", message, f"SUBMIT ERROR ({code}) — {message}")

        correct = resp.get("correct", False)
        awarded = resp.get("awarded", 0)
        cumulative = resp.get("cumulative_score", 0)
        correct_count = resp.get("correct_flag_count", 0)
        total_count = resp.get("total_flag_count", 0)

        if correct:
            complete = total_count > 0 and correct_count >= total_count
            if complete:
                self._solved_cache.add(challenge_name)
            return SubmitResult(
                "correct",
                f"awarded={awarded}, cumulative={cumulative}, {correct_count}/{total_count}",
                f'CORRECT — "{flag}" accepted. +{awarded} pts. '
                f"Cumulative: {cumulative}. Progress: {correct_count}/{total_count}.",
                challenge_complete=complete,
                progress=f"{correct_count}/{total_count} flags",
            )
        return SubmitResult(
            "incorrect",
            f"cumulative={cumulative}, {correct_count}/{total_count}",
            f'INCORRECT — "{flag}" rejected. Progress: {correct_count}/{total_count}.',
            progress=f"{correct_count}/{total_count} flags",
        )

    async def fetch_poll_state(self) -> tuple[list[dict[str, Any]], set[str]]:
        """Single-request snapshot for the poller: (challenge dicts, completed names)."""
        challenges = await self.fetch_all_challenges()
        return challenges, set(self._solved_cache)

    async def fetch_hint(self, challenge_name: str) -> str | None:
        """Fetch the platform hint for a challenge (platform deducts score)."""
        ch = self._challenge_index.get(challenge_name)
        if not ch:
            await self.fetch_all_challenges()
            ch = self._challenge_index.get(challenge_name)
        unique_code = (ch or {}).get("unique_code", "")
        if not unique_code:
            return None
        return await self.get_hint(unique_code)

    async def close_stale_containers(self, keep: set[str]) -> list[str]:
        """Reap platform-side containers that no local swarm is using.

        After crashes or failed closes, the platform can hold ghost containers
        that occupy the active-instance slots. `keep` contains challenge names
        AND unique_codes that must not be touched (active + preparing spawns).
        """
        try:
            data = await self._api_get("openapi/v1/challenges")
        except Exception as e:
            logger.warning("Ghost reap: could not list challenges: %s", e)
            return []
        if not isinstance(data, list):
            return []
        closed: list[str] = []
        for item in data:
            if item.get("container_status") != "available":
                continue
            code = item.get("unique_code", "")
            name = item.get("name", code)
            if name in keep or code in keep:
                continue
            try:
                await self.close_challenge(code)
                closed.append(name)
                logger.info("Reaped stale container: %s (%s)", name, code)
            except Exception as e:
                logger.warning("Ghost reap: failed to close %s: %s", name, e)
        return closed

    async def release_challenge(self, challenge_name: str) -> bool:
        """Coordinator lifecycle hook: close the container to free the slot."""
        ch = self._challenge_index.get(challenge_name)
        if not ch:
            await self.fetch_all_challenges()
            ch = self._challenge_index.get(challenge_name)
        unique_code = (ch or {}).get("unique_code", "")
        if not unique_code:
            return False
        try:
            result = await self.close_challenge(unique_code)
            # A 200 whose payload says closed:false means the range is still
            # live — report failure so the coordinator keeps counting the slot.
            if result.get("closed") is False:
                logger.warning("TSec close %s: platform reports not closed", challenge_name)
                return False
            logger.info("TSec container closed for %s (%s)", challenge_name, unique_code)
            return True
        except Exception as e:
            logger.warning("TSec close_challenge failed for %s: %s", challenge_name, e)
            return False

    async def start_challenge(self, unique_code: str) -> dict[str, Any]:
        import asyncio as _aio

        resp = None
        # The platform frees instances asynchronously after /close: starting the
        # next challenge immediately can hit "max active". A rejected start
        # created no instance, so backing off and retrying is safe.
        for attempt in range(6):
            try:
                resp = await self._api_post(
                    "openapi/v1/challenges/start",
                    params={"unique_code": unique_code},
                )
                break
            except httpx.HTTPStatusError as e:
                body = ""
                try:
                    body = e.response.text
                except Exception:
                    pass
                if e.response.status_code == 409 and "max active" in body:
                    if attempt < 5:
                        delay = 4.0 + attempt * 2.0
                        logger.warning(
                            "TSec start %s: platform at capacity; retrying in %.0fs (%d/5)",
                            unique_code, delay, attempt + 1,
                        )
                        await _aio.sleep(delay)
                        continue
                    # final attempt exhausted
                    break
                raise
        if resp is None:
            raise RuntimeError(f"TSec start {unique_code}: platform stayed at capacity after retries")
        addrs = resp.get("container_addr", [])
        # Record the start attempt even when the payload carries no address:
        # a retry must know /start already fired, or it would POST again and
        # spawn a SECOND range instance for the same task.
        self._started_addrs[unique_code] = list(addrs)
        return {
            "unique_code": resp.get("unique_code", unique_code),
            "container_addr": addrs,
            "container_addrs_str": ", ".join(addrs) if addrs else "(none)",
        }

    async def close_challenge(self, unique_code: str) -> dict[str, Any]:
        import asyncio as _aio

        resp = None
        # Close goes through the platform's range controller, which can return
        # transient 5xx (gateway timeouts). A failed close leaves the container
        # occupying a slot, so retry before giving up.
        for attempt in range(4):
            try:
                resp = await self._api_post(
                    "openapi/v1/challenges/close",
                    params={"unique_code": unique_code},
                )
                break
            except httpx.HTTPStatusError as e:
                # 4xx is a terminal answer. "already closed" style states are
                # NOT failures — the container no longer occupies a slot, so
                # report success instead of permanently burning the local
                # capacity entry. Other 4xx (auth, unknown code) still raise.
                if e.response.status_code < 500:
                    body = ""
                    try:
                        body = e.response.text
                    except Exception:
                        pass
                    if "already" in body or "invalid_state" in body or "closed" in body:
                        logger.warning(
                            "TSec close %s: platform says already closed (%s) — treating as success",
                            unique_code, body[:120],
                        )
                        self._started_addrs.pop(unique_code, None)
                        return {"unique_code": unique_code, "closed": True}
                    raise
                if attempt < 3:
                    delay = (3.0, 8.0, 15.0)[attempt]
                    logger.warning(
                        "TSec close %s: HTTP %d, retrying in %.0fs (%d/3)",
                        unique_code, e.response.status_code, delay, attempt + 1,
                    )
                    await _aio.sleep(delay)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt < 3:
                    delay = (3.0, 8.0, 15.0)[attempt]
                    logger.warning(
                        "TSec close %s: %s, retrying in %.0fs (%d/3)",
                        unique_code, type(e).__name__, delay, attempt + 1,
                    )
                    await _aio.sleep(delay)
        if resp is None:
            raise RuntimeError(f"TSec close {unique_code}: failed after retries")
        self._started_addrs.pop(unique_code, None)
        return {
            "unique_code": resp.get("unique_code", unique_code),
            "closed": resp.get("closed", True),
        }

    async def get_hint(self, unique_code: str) -> str | None:
        resp = await self._api_get(
            "openapi/v1/challenges/hint",
            params={"unique_code": unique_code},
        )
        return resp.get("hint")

    async def pull_challenge(self, challenge: dict[str, Any], output_dir: str) -> str:
        name = challenge.get("name", challenge.get("unique_code", "challenge"))
        slug = self._slug(name)
        ch_dir = Path(output_dir) / slug
        ch_dir.mkdir(parents=True, exist_ok=True)

        # Try to start the challenge container to get connection info.
        # Reuse a container we already started (or one the platform reports as
        # available) instead of firing another /start — every extra start
        # spawns another range instance.
        unique_code = challenge.get("unique_code", "")
        addrs = challenge.get("container_addr", [])
        container_status = challenge.get("container_status", "stopped")
        if (
            not addrs and unique_code and unique_code in self._started_addrs
            # Only reuse the cached address when the platform's fresh listing
            # doesn't say the container is stopped — a recycled/closed range
            # would otherwise hand the solver a dead address.
            and container_status != "stopped"
        ):
            addrs = self._started_addrs[unique_code]
        if not addrs and unique_code:
            # Fire /start exactly once, then poll the challenge LIST for the
            # address. Re-POSTing /start while the container is still spinning
            # up would spawn a second range instance for the same task.
            import asyncio as _aio

            try:
                await self.start_challenge(unique_code)
            except Exception as e:
                logger.warning("TSec start_challenge failed for %s: %s", unique_code, e)
            addrs = self._started_addrs.get(unique_code, [])
            for _ in range(10):  # up to ~30s for the container to become available
                if addrs:
                    break
                await _aio.sleep(3)
                try:
                    data = await self._api_get("openapi/v1/challenges")
                except Exception as e:
                    logger.warning("TSec poll for container_addr failed (%s): %s", unique_code, e)
                    continue
                if not isinstance(data, list):
                    continue
                for item in data:
                    if (
                        item.get("unique_code") == unique_code
                        and item.get("container_status") == "available"
                        and item.get("container_addr")
                    ):
                        addrs = item["container_addr"]
                        self._started_addrs[unique_code] = addrs
                        break

        connection_info = ", ".join(addrs) if addrs else ""

        if not connection_info:
            # Fail the spawn instead of running a solver with no target: the
            # scheduler will retry this challenge at the next refill tick (the
            # usual cause is a max-active race right after another close).
            raise RuntimeError(
                f"TSec: container for {name} did not become available "
                "(no container_addr after start + polling) — spawn aborted, retry later"
            )

        meta = {
            "name": name,
            "category": challenge.get("category", ""),
            "description": challenge.get("description", ""),
            "value": challenge.get("value", 0),
            "connection_info": connection_info,
            "tags": [],
            "solves": 0,
            "unique_code": unique_code,
            "flag_count": challenge.get("flag_count", 1),
            "correct_flag_count": challenge.get("correct_flag_count", 0),
            "level": challenge.get("level", 0),
            "container_status": challenge.get("container_status", "stopped"),
        }
        (ch_dir / "metadata.yml").write_text(
            yaml.dump(meta, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        return str(ch_dir)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
