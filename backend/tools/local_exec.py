"""Minimal local Python execution — the model's scratchpad without a sandbox.

Runs plain CPython (stdlib + whatever the project venv has) directly on the
host with a hard timeout. This replaces the heavy Docker sandbox: no container,
no toolchain images — just the model's own ability to write code.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

MAX_OUTPUT = 24_000
DEFAULT_TIMEOUT = 60.0
MAX_CODE_BYTES = 200_000


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, total {len(text)} chars]"


async def do_run_python(
    code: str,
    timeout: float = DEFAULT_TIMEOUT,
    allowed_hosts: frozenset[str] | None = None,
    workdir: str | None = None,
) -> str:
    """Execute a Python snippet on the host and return stdout/stderr.

    allowed_hosts: when not None, outbound socket connections from the snippet
    are restricted to these hosts (target lock) plus localhost. An empty set
    means the lock is in its degraded no-target-known state: localhost only.

    workdir: per solver-run working directory. The snippet file is written
    there, the child process runs with it as cwd, and TMP/TEMP/TMPDIR point
    there too — artifacts never leak into a shared directory across challenges.
    The directory is kept (not cleaned) for post-mortem inspection.
    """
    if not code.strip():
        return "run_python error: empty code"
    if len(code.encode("utf-8", errors="replace")) > MAX_CODE_BYTES:
        return "run_python error: code too large (200 KB limit)"

    if allowed_hosts is not None:
        hosts = sorted(set(allowed_hosts) | {"127.0.0.1", "localhost", "::1"})
        preamble = (
            "import socket as _socket\n"
            f"_ALLOWED_HOSTS = frozenset({hosts!r})\n"
            "def _guard(address):\n"
            "    host = str(address[0] if isinstance(address, tuple) else address).strip('[]').lower()\n"
            "    if host and host not in _ALLOWED_HOSTS:\n"
            "        raise PermissionError(f\"target lock: '{host}' is not an assigned target; \"\n"
            "                              f\"allowed: {sorted(_ALLOWED_HOSTS)}\")\n"
            "_orig_connect = _socket.socket.connect\n"
            "def _connect(self, address):\n"
            "    _guard(address)\n"
            "    return _orig_connect(self, address)\n"
            "_socket.socket.connect = _connect\n"
            "_orig_ce = _socket.create_connection\n"
            "def _create_connection(address, *a, **kw):\n"
            "    _guard(address)\n"
            "    return _orig_ce(address, *a, **kw)\n"
            "_socket.create_connection = _create_connection\n"
            "del _socket\n"
        )
        code = preamble + code

    run_dir: Path | None = None
    if workdir:
        run_dir = Path(workdir)
        run_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkstemp(suffix=".py", prefix="agent_", dir=run_dir)[1])
    try:
        tmp.write_text(code, encoding="utf-8")

        env = None
        cwd = None
        if run_dir is not None:
            cwd = str(run_dir)
            env = dict(os.environ)
            # Redirect tempfile defaults so snippet artifacts (downloads,
            # decoded blobs, /tmp-style writes) stay inside this run's dir.
            env["TMP"] = env["TEMP"] = env["TMPDIR"] = str(run_dir)

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",  # isolated mode: no user site, no env vars influence
            str(tmp),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=cwd,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return (
                f"run_python: timed out after {timeout:.0f}s and the process was killed. "
                "Break the work into smaller steps or add prints to stream progress."
            )

        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        parts: list[str] = []
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr]\n{err}")
        if proc.returncode != 0:
            parts.append(f"[exit {proc.returncode}]")
        body = "\n".join(parts).strip() or "(no output — remember to print() results)"
        return _truncate(body)
    except Exception as e:
        return f"run_python error: {type(e).__name__}: {e}"
    finally:
        # Keep the snippet inside a per-run workdir for post-mortems; only
        # clean up the shared-temp fallback.
        if run_dir is None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
