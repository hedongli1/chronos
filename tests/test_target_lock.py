"""Tests for the target-lock enforcement (high-severity safety fixes).

Covers: empty-host fail-open, //host URL smuggling, and the redirect
allowlist re-check in do_raw_http (off-scope redirects must NOT be followed).
"""

from __future__ import annotations

import pytest

from backend.agents.headless_solver import (
    _parse_allowed_hosts,
    _target_lock_block,
)
from backend.deps import HeadlessDeps
from backend.tools.local_exec import do_run_python
from backend.tools.net_tools import do_raw_http


def _deps(allowed: frozenset[str] | None = None) -> HeadlessDeps:
    return HeadlessDeps(
        platform=None,  # type: ignore[arg-type]
        challenge_name="test",
        meta=None,  # type: ignore[arg-type]
        allowed_hosts=allowed if allowed is not None else frozenset({"10.0.0.5"}),
    )


class TestTargetLockBlock:
    def test_allowed_host_passes(self) -> None:
        assert _target_lock_block(_deps(), "10.0.0.5") is None

    def test_off_scope_host_blocked(self) -> None:
        msg = _target_lock_block(_deps(), "10.0.9.9")
        assert msg and "BLOCKED (target lock)" in msg and "10.0.9.9" in msg

    def test_empty_hostname_never_fails_open(self) -> None:
        # Regression: an unparsable host ("" after urlparse) used to be
        # treated as allowed, letting '//host' URLs bypass the lock.
        msg = _target_lock_block(_deps(), "")
        assert msg and "BLOCKED (target lock)" in msg
        msg2 = _target_lock_block(_deps(), "   ")
        assert msg2 and "BLOCKED (target lock)" in msg2

    def test_no_target_known_degrades_to_block_all(self) -> None:
        msg = _target_lock_block(_deps(allowed=frozenset()), "10.0.0.5")
        assert msg and "NO TARGET KNOWN" in msg

    def test_host_comparison_is_case_and_space_insensitive(self) -> None:
        assert _target_lock_block(_deps(), " 10.0.0.5 ") is None

    def test_bracketed_ipv6_host(self) -> None:
        assert _parse_allowed_hosts("[::1]:443") == frozenset({"::1"})


class TestParseAllowedHosts:
    def test_basic_hosts(self) -> None:
        hosts = _parse_allowed_hosts("10.0.0.5:8080, 10.0.0.6")
        assert hosts == frozenset({"10.0.0.5", "10.0.0.6"})

    def test_scheme_stripped(self) -> None:
        assert _parse_allowed_hosts("http://10.0.0.5:80") == frozenset({"10.0.0.5"})

    def test_empty_connection_info(self) -> None:
        assert _parse_allowed_hosts("") == frozenset()

    def test_bracketed_host(self) -> None:
        assert _parse_allowed_hosts("[10.0.0.5]:443") == frozenset({"10.0.0.5"})

class TestDoRawHttpRedirectLock:
    @pytest.mark.asyncio
    async def test_off_scope_redirect_is_blocked_and_not_followed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 302 to an out-of-scope host must be refused, not fetched."""
        fetched: list[str] = []

        class FakeResponse:
            status_code = 302
            reason_phrase = "Found"
            is_redirect = True
            headers = {"location": "http://10.0.9.9/evil"}
            url = "http://10.0.0.5/start"
            text = ""

        class FakeClient:
            def __init__(self, *a, **kw) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a) -> None:
                pass

            async def request(self, method: str, url: str, **kw):
                fetched.append(url)
                return FakeResponse()

        monkeypatch.setattr("backend.tools.net_tools.httpx.AsyncClient", FakeClient)
        out = await do_raw_http(
            "http://10.0.0.5/start",
            allowed_hosts=frozenset({"10.0.0.5"}),
        )
        assert "outside the authorized scope" in out
        assert "10.0.9.9" in out
        # Only the initial hop was fetched; the redirect target never was.
        assert fetched == ["http://10.0.0.5/start"]

    @pytest.mark.asyncio
    async def test_in_scope_redirect_is_followed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeResponse:
            status_code = 200
            reason_phrase = "OK"
            is_redirect = False
            headers = {"content-type": "text/plain"}
            url = "http://10.0.0.5/final"
            text = "flag{ok}"

        class FakeClient:
            def __init__(self, *a, **kw) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a) -> None:
                pass

            async def request(self, method: str, url: str, **kw):
                assert url == "http://10.0.0.5/final" or url == "http://10.0.0.5/start"
                return FakeResponse()

        monkeypatch.setattr("backend.tools.net_tools.httpx.AsyncClient", FakeClient)
        out = await do_raw_http(
            "http://10.0.0.5/start",
            allowed_hosts=frozenset({"10.0.0.5"}),
        )
        assert "flag{ok}" in out

    @pytest.mark.asyncio
    async def test_no_allowlist_means_no_hop_checks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backward compat: without an allowlist, behavior is unrestricted."""
        called = []

        class FakeResponse:
            status_code = 200
            reason_phrase = "OK"
            is_redirect = False
            headers = {}
            url = "http://example.com/"
            text = "hello"

        class FakeClient:
            def __init__(self, *a, **kw) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a) -> None:
                pass

            async def request(self, method: str, url: str, **kw):
                called.append(url)
                return FakeResponse()

        monkeypatch.setattr("backend.tools.net_tools.httpx.AsyncClient", FakeClient)
        out = await do_raw_http("http://example.com/")
        assert "hello" in out
        assert called == ["http://example.com/"]


class TestRunPythonNamespaceIsolation:
    """The run_python target-lock preamble must not leak its internals into the
    snippet's namespace. Regression for the confirmed sandbox-escape: the saved
    originals / guard / allowed-set used to be module globals reachable by model
    code (`_orig_connect(...)`, `guard = lambda…`), a trivial bypass.

    These run real local subprocesses (sys.executable -I). No network is touched:
    the guard raises PermissionError BEFORE any connect, and the introspection
    snippets only read globals().
    """

    _ALLOWED = frozenset({"10.0.0.5"})

    async def test_preamble_internals_absent_from_globals(self) -> None:
        out = await do_run_python(
            "import json; print(json.dumps(sorted(globals())))",
            allowed_hosts=self._ALLOWED,
        )
        # None of the guard internals may be visible to the snippet.
        for leaked in (
            "_orig_connect", "orig_connect", "_guard", "guard",
            "_ALLOWED_HOSTS", "allowed", "_spawn_error", "spawn_error",
            "_chronos_target_lock", "_orig_popen", "orig_popen",
        ):
            assert f'"{leaked}"' not in out, f"preamble internal {leaked!r} leaked into globals"

    async def test_guard_blocks_disallowed_host(self) -> None:
        out = await do_run_python(
            "import socket\n"
            "s = socket.socket()\n"
            "try:\n"
            "    s.connect(('10.0.9.9', 80))\n"
            "    print('CONNECTED')\n"
            "except PermissionError as e:\n"
            "    print('BLOCKED:', e)\n",
            allowed_hosts=self._ALLOWED,
        )
        assert "BLOCKED" in out and "CONNECTED" not in out

    async def test_reassigning_global_guard_does_not_disable_lock(self) -> None:
        """A snippet defining its own global `guard` must NOT neuter the real
        closure guard the patched socket methods use."""
        out = await do_run_python(
            "guard = lambda a: None\n"
            "allowed = frozenset({'10.0.9.9'})\n"
            "import socket\n"
            "s = socket.socket()\n"
            "try:\n"
            "    s.connect(('10.0.9.9', 80))\n"
            "    print('CONNECTED')\n"
            "except PermissionError as e:\n"
            "    print('BLOCKED:', e)\n",
            allowed_hosts=self._ALLOWED,
        )
        assert "BLOCKED" in out and "CONNECTED" not in out

    async def test_subprocess_still_blocked(self) -> None:
        out = await do_run_python(
            "import subprocess\n"
            "try:\n"
            "    subprocess.Popen(['echo', 'hi'])\n"
            "    print('SPAWNED')\n"
            "except PermissionError as e:\n"
            "    print('BLOCKED:', e)\n",
            allowed_hosts=self._ALLOWED,
        )
        assert "BLOCKED" in out and "SPAWNED" not in out

    async def test_snippet_cannot_reenable_subprocess_via_env(self) -> None:
        """The opt-out is captured at install time: a snippet flipping
        RUN_PYTHON_ALLOW_SUBPROCESS in os.environ must NOT unlock spawning."""
        out = await do_run_python(
            "import os, subprocess\n"
            "os.environ['RUN_PYTHON_ALLOW_SUBPROCESS'] = '1'\n"
            "try:\n"
            "    subprocess.Popen(['echo', 'hi'])\n"
            "    print('SPAWNED')\n"
            "except PermissionError as e:\n"
            "    print('BLOCKED:', e)\n",
            allowed_hosts=self._ALLOWED,
        )
        assert "BLOCKED" in out and "SPAWNED" not in out

    async def test_local_computation_still_works(self) -> None:
        """The isolation must not break ordinary in-process computation."""
        out = await do_run_python(
            "print(sum(range(10)))",
            allowed_hosts=self._ALLOWED,
        )
        assert "45" in out

    async def test_no_allowlist_runs_unpatched(self) -> None:
        """allowed_hosts=None means no preamble at all (backward compat)."""
        out = await do_run_python("print('plain')", allowed_hosts=None)
        assert "plain" in out
