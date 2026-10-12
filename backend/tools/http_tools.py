"""Generic HTTP tools for platform-agnostic coordinator workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

MAX_HTTP_OUTPUT = 12_000
ALLOWED_SCHEMES = {"http", "https"}

# Extra origins the coordinator is allowed to reach (e.g. VPN health check).
EXTRA_ALLOWED_HOSTS: set[str] = {"10.0.100.58"}


@dataclass(slots=True)
class HttpRequestResult:
    status_code: int
    reason: str
    headers: dict[str, str]
    body: str
    url: str

    def to_text(self) -> str:
        header_lines = [f"{k}: {v}" for k, v in self.headers.items()]
        header_text = "\n".join(header_lines)
        body = self.body
        if len(body) > MAX_HTTP_OUTPUT:
            body = body[:MAX_HTTP_OUTPUT] + f"\n... [truncated, total {len(self.body)} chars]"
        return (
            f"HTTP {self.status_code} {self.reason}\n"
            f"URL: {self.url}\n"
            f"Headers:\n{header_text or '(none)'}\n"
            f"Body:\n{body or '(empty)'}"
        )


def _normalize_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    if not headers:
        return {}
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if value is None:
            continue
        normalized[str(key)] = str(value)
    return normalized


def _is_origin_allowed(base_url: str, target_url: str) -> bool:
    """Check if target_url's origin matches base_url's origin or is in the allowlist."""
    base_parsed = urlparse(base_url)
    target_parsed = urlparse(target_url)

    if target_parsed.scheme not in ALLOWED_SCHEMES:
        return False

    # Allow same-origin requests
    if target_parsed.netloc == base_parsed.netloc:
        return True

    # Allow VPN health check and other explicitly whitelisted hosts
    target_host = target_parsed.hostname or ""
    return target_host in EXTRA_ALLOWED_HOSTS


async def do_http_request(
    base_url: str,
    path_or_url: str,
    *,
    method: str = "GET",
    headers: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    json_body: Any = None,
    body: str | bytes | None = None,
    timeout: float = 30.0,
) -> str:
    """Perform a constrained HTTP request.

    Only allows requests to the configured base_url origin, or explicit absolute
    URLs whose origin matches base_url. Also allows the VPN health-check host.
    """
    base_parsed = urlparse(base_url)
    if base_parsed.scheme not in ALLOWED_SCHEMES or not base_parsed.netloc:
        return f"HTTP request error: invalid base_url '{base_url}'"

    target_url = path_or_url
    if not urlparse(path_or_url).scheme:
        target_url = urljoin(base_url.rstrip("/") + "/", path_or_url.lstrip("/"))

    if not _is_origin_allowed(base_url, target_url):
        base_origin = f"{base_parsed.scheme}://{base_parsed.netloc}"
        return (
            "HTTP request error: cross-origin requests are blocked. "
            f"Allowed origin: {base_origin} (plus: {', '.join(EXTRA_ALLOWED_HOSTS)})"
        )

    req_headers = _normalize_headers(headers)
    req_headers.setdefault("User-Agent", "Mozilla/5.0")

    try:
        async with httpx.AsyncClient(verify=False, timeout=timeout, follow_redirects=True, trust_env=False) as client:
            resp = await client.request(
                method.upper(),
                target_url,
                headers=req_headers,
                params=query or None,
                json=json_body,
                content=body,
            )
            result = HttpRequestResult(
                status_code=resp.status_code,
                reason=resp.reason_phrase,
                headers={k: v for k, v in resp.headers.items() if k.lower() not in {"set-cookie"}},
                body=resp.text,
                url=str(resp.url),
            )
            return result.to_text()
    except Exception as e:
        return f"HTTP request error: {e}"
