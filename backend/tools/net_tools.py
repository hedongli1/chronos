"""Sandbox-free network tools — direct interaction with live benchmark targets.

Unlike web_fetch (which blocks private ranges), these tools are *meant* to talk
to the challenge targets handed out by the platform (VPN-ranged container
addresses such as 10.0.x.x). Only outbound client sockets are opened.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

MAX_HTTP_OUTPUT = 24_000
MAX_TCP_OUTPUT = 24_000
DEFAULT_TIMEOUT = 30.0


def _truncate(text: str, limit: int = MAX_HTTP_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, total {len(text)} chars]"


def _normalize_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {str(k): str(v) for k, v in headers.items() if v is not None}


async def do_raw_http(
    url: str,
    method: str = "GET",
    headers: dict[str, Any] | None = None,
    body: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    allowed_hosts: frozenset[str] | None = None,
    max_redirects: int = 5,
) -> str:
    """Full-control HTTP request to a live target (challenge ranges included).

    Returns a verbatim transcript: status line, response headers, body.

    allowed_hosts: when given, every hop (initial URL and each redirect) is
    checked against this allowlist. Redirects to an off-scope host are NOT
    followed — the transcript reports the block instead. This keeps the
    target lock structural even when an in-scope service 302s elsewhere.
    """
    from urllib.parse import urlparse as _urlparse

    if not url.strip():
        return "http_request error: empty url"

    def _checked_hop(hop: str) -> str | None:
        """Return a block message when this hop's host is outside scope, else None."""
        if allowed_hosts is None:
            return None
        host = (_urlparse(hop).hostname or "").strip().lower()
        if host and host not in allowed_hosts:
            return (
                f"http_request error: redirect target '{host}' is outside the "
                f"authorized scope ({', '.join(sorted(allowed_hosts))}). "
                "The redirect was NOT followed."
            )
        return None

    current = url if url.startswith(("http://", "https://")) else "http://" + url.lstrip("/")
    req_headers = _normalize_headers(headers)
    content: str | bytes | None = body or None
    if req_headers.get("Content-Type", "").startswith("application/json") and isinstance(body, str) and body:
        try:
            content = json.dumps(json.loads(body))  # validate JSON before sending
        except Exception:
            pass  # send as-is; some tasks need malformed JSON on purpose

    transcript_parts: list[str] = []

    try:
        async with httpx.AsyncClient(
            verify=False,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for _hop in range(max_redirects + 1):
                block = _checked_hop(current)
                if block:
                    return block
                resp = await client.request(method.upper(), current, headers=req_headers, content=content)
                header_lines = "\n".join(f"{k}: {v}" for k, v in resp.headers.items())
                transcript_parts.append(
                    f"HTTP {resp.status_code} {resp.reason_phrase}\n"
                    f"URL: {resp.url}\n"
                    f"--- response headers ---\n{header_lines or '(none)'}\n"
                    f"--- response body ---\n{resp.text or '(empty)'}"
                )
                if resp.is_redirect and resp.headers.get("location"):
                    current = str(resp.headers["location"])
                    # relative Location resolves against the previous URL
                    if not current.startswith(("http://", "https://")):
                        current = str(httpx.URL(resp.url).join(current))
                    continue
                break
            else:
                return (
                    "http_request error: too many redirects "
                    f"(>{max_redirects}) — giving up."
                )
        return _truncate("\n\n=== redirect hop ===\n\n".join(transcript_parts))
    except Exception as e:
        return f"http_request error: {type(e).__name__}: {e}"


def _decode_payload(payload: str, payload_hex: str) -> bytes:
    if payload_hex:
        cleaned = "".join(payload_hex.split())
        return bytes.fromhex(cleaned)
    return payload.encode("utf-8", errors="surrogateescape")


async def do_tcp_send(
    host: str,
    port: int,
    payload: str = "",
    payload_hex: str = "",
    read_timeout: float = DEFAULT_TIMEOUT,
    keep_open_seconds: float = 3.0,
) -> str:
    """Send one payload over raw TCP and return everything the service sends back.

    Use payload_hex for binary protocols; payload for line-based ones.
    The connection closes after keep_open_seconds of silence.
    """
    if not host.strip():
        return "tcp_send error: empty host"
    try:
        data = _decode_payload(payload, payload_hex)
    except ValueError as e:
        return f"tcp_send error: invalid payload_hex ({e})"

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=read_timeout
        )
    except Exception as e:
        return f"tcp_send error: cannot connect to {host}:{port} — {type(e).__name__}: {e}"

    try:
        if data:
            writer.write(data)
            await writer.drain()

        chunks: list[bytes] = []
        total = 0
        try:
            while total < MAX_TCP_OUTPUT:
                chunk = await asyncio.wait_for(reader.read(8192), timeout=keep_open_seconds)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        except TimeoutError:
            pass  # silence window elapsed — that is the normal end of a reply

        raw = b"".join(chunks)
        text = raw.decode("utf-8", errors="backslashreplace")
        prefix = f"TCP {host}:{port} — sent {len(data)} bytes, received {len(raw)} bytes\n--- response ---\n"
        return _truncate(prefix + (text or "(no data received — service may expect different input or closed early)"))
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
