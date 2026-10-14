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
    allow_subprocess: bool = False,
) -> str:
    """Execute a Python snippet on the host and return stdout/stderr.

    allowed_hosts: when not None, outbound socket connections from the snippet
    are restricted to these hosts (target lock) plus localhost. An empty set
    means the lock is in its degraded no-target-known state: localhost only.
    The guard covers connect/connect_ex/sendto/sendmsg, create_connection and
    the asyncio connection paths. Subprocess escape hatches (subprocess,
    os.system, os.exec*/spawn*/popen) are refused — child processes would not
    inherit the in-process patches and would escape the allowlist; set
    allow_subprocess=True (RUN_PYTHON_ALLOW_SUBPROCESS=1) to opt out
    (operator decision, documented gap).

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
            # connect / connect_ex (the error-code variant must not bypass)
            "_orig_connect = _socket.socket.connect\n"
            "def _connect(self, address):\n"
            "    _guard(address)\n"
            "    return _orig_connect(self, address)\n"
            "_socket.socket.connect = _connect\n"
            "_orig_connect_ex = _socket.socket.connect_ex\n"
            "def _connect_ex(self, address):\n"
            "    _guard(address)\n"
            "    return _orig_connect_ex(self, address)\n"
            "_socket.socket.connect_ex = _connect_ex\n"
            # connectionless UDP egress (sendto / sendmsg) must not bypass
            "_orig_sendto = _socket.socket.sendto\n"
            "def _sendto(self, *a):\n"
            "    # 2-arg form: sendto(data, address); 3-arg: sendto(data, flags, address)\n"
            "    _guard(a[2] if len(a) >= 3 else a[1])\n"
            "    return _orig_sendto(self, *a)\n"
            "_socket.socket.sendto = _sendto\n"
            # sendmsg is Unix-only: guard the patch the way CPython's own
            # Lib/socket.py does, or every run_python call dies on Windows.
            "if hasattr(_socket.socket, 'sendmsg'):\n"
            "    _orig_sendmsg = _socket.socket.sendmsg\n"
            "    def _sendmsg(self, buffers, ancdata=(), flags=0, address=None):\n"
            "        if address is not None: _guard(address)\n"
            "        return _orig_sendmsg(self, buffers, ancdata, flags, address)\n"
            "    _socket.socket.sendmsg = _sendmsg\n"
            # create_connection and asyncio paths (Proactor ConnectEx bypasses
            # the patched connect — guard before delegating)
            "_orig_ce = _socket.create_connection\n"
            "def _create_connection(address, *a, **kw):\n"
            "    _guard(address)\n"
            "    return _orig_ce(address, *a, **kw)\n"
            "_socket.create_connection = _create_connection\n"
            "import asyncio as _asyncio\n"
            "_orig_occ = _asyncio.open_connection\n"
            "def _open_connection(host, port=None, *a, **kw):\n"
            "    # A pre-connected sock (sock=...) is already a patched socket;\n"
            "    # only guard the host/port form. host=None + sock=None would be\n"
            "    # invalid anyway.\n"
            "    if host is not None:\n"
            "        _guard((host, port))\n"
            "    return _orig_occ(host, port, *a, **kw)\n"
            "_asyncio.open_connection = _open_connection\n"
            "_orig_loop_cc = _asyncio.BaseEventLoop.create_connection\n"
            "def _loop_cc(self, protocol_factory, host=None, port=None, *a, **kw):\n"
            "    if host is not None:\n"
            "        _guard((host, port))\n"
            "    return _orig_loop_cc(self, protocol_factory, host, port, *a, **kw)\n"
            "_asyncio.BaseEventLoop.create_connection = _loop_cc\n"
            # Child processes do not inherit in-process patches and would
            # escape the lock entirely. The headless contract promises "no
            # local toolchain beyond the tools listed", so subprocess escape
            # hatches are refused (operator can opt out via env flag).
            "import os as _os, subprocess as _subprocess\n"
            "def _spawn_error(what):\n"
            "    if _os.environ.get('RUN_PYTHON_ALLOW_SUBPROCESS') == '1':\n"
            "        return False\n"
            "    raise PermissionError(\n"
            "        f\"target lock: {what} is blocked — subprocesses would escape \"\n"
            "        \"the network allowlist. Compute in-process (stdlib + installed \"\n"
            "        \"libraries) instead.\")\n"
            "_orig_popen = _subprocess.Popen\n"
            "def _popen(*a, **kw):\n"
            "    _spawn_error('subprocess')\n"
            "    return _orig_popen(*a, **kw)\n"
            "_subprocess.Popen = _popen\n"
            "_orig_system = _os.system\n"
            "def _system(cmd):\n"
            "    _spawn_error('os.system')\n"
            "    return _orig_system(cmd)\n"
            "_os.system = _system\n"
            "for _name in ('execl','execle','execlp','execlpe','execv','execve','execvp','execvpe',\n"
            "              'spawnl','spawnle','spawnlp','spawnlpe','spawnv','spawnve','spawnvp',\n"
            "              'spawnvpe','posix_spawn','popen'):\n"
            "    if hasattr(_os, _name):\n"
            "        _orig = getattr(_os, _name)\n"
            "        def _blocked(*a, _orig=_orig, _what=_name, **kw):\n"
            "            _spawn_error('os.' + _what)\n"
            "            return _orig(*a, **kw)\n"
            "        setattr(_os, _name, _blocked)\n"
            # _spawn_error reads _os.environ at call time: _os must survive.
            "del _socket, _asyncio, _subprocess\n"
        )
        code = preamble + code

    run_dir: Path | None = None
    if workdir:
        run_dir = Path(workdir)
        run_dir.mkdir(parents=True, exist_ok=True)
    # mkstemp hands back an open fd — close it immediately so Windows doesn't
    # hold a lock on the file and no fd leaks per call. The path is used below.
    fd, tmp_path = tempfile.mkstemp(suffix=".py", prefix="agent_", dir=run_dir)
    os.close(fd)
    tmp = Path(tmp_path)
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
        # The subprocess opt-out flag is threaded as an env var the snippet's
        # preamble reads. Set it explicitly so a stray variable in the parent
        # environment cannot override the caller's explicit choice.
        if env is None:
            env = dict(os.environ)
        env["RUN_PYTHON_ALLOW_SUBPROCESS"] = "1" if allow_subprocess else "0"

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
            # Kill the whole process tree, not just the direct child: on
            # Windows use taskkill /T (the subprocess patch above blocks
            # children anyway, but this stays correct if it's ever lifted).
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            if sys.platform == "win32":
                try:
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/F", "/T", "/PID", str(proc.pid),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(killer.communicate(), timeout=15)
                except Exception:
                    # Best effort: the direct kill above already landed.
                    pass
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
