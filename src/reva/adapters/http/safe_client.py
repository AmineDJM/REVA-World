"""SafeHttpClient: the only HTTP path for connectors.

* HTTPS only; exact host allowlist; no userinfo; standard port only.
* Every resolved address must be globally routable; checked before the first
  request and on every redirect (private, loopback, link-local, metadata,
  multicast, reserved and CGNAT ranges are rejected).
* Bounded redirects, timeouts, response size (decoded bytes, which also bounds
  decompression) and compression ratio.
* 429/503 honour Retry-After within a cap; 5xx/transport errors use jittered
  exponential backoff; bounded attempts.
* Bodies are untrusted data. Secrets never appear in request fingerprints.
"""

from __future__ import annotations

import asyncio
import email.utils
import hashlib
import ipaddress
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from reva.adapters.http.rate_limit import RateLimiter, backoff_seconds
from reva.kernel.errors import SourceResponseError, UnsafeRequest

Resolver = Callable[[str], Sequence[str]]
METADATA_ADDRESSES = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}
MAX_COMPRESSION_RATIO = 200


def system_resolver(host: str) -> Sequence[str]:
    try:
        return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    except socket.gaierror as e:
        raise UnsafeRequest(f"DNS resolution failed for {host}") from e


def address_is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if ip in METADATA_ADDRESSES:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not ip.is_multicast and not ip.is_reserved


@dataclass(frozen=True)
class HttpResult:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    retrieved_at: datetime
    elapsed_seconds: float
    redirects: tuple[str, ...]
    attempts: int

    @property
    def media_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    def response_metadata(self) -> dict[str, Any]:
        keep = ("content-type", "etag", "last-modified", "date", "content-length", "x-rate-limit-limit",
                "x-rate-limit-interval", "x-concurrency-limit")
        return {
            "status": self.status,
            "final_url": self.final_url,
            "redirects": list(self.redirects),
            "headers": {k: v for k, v in self.headers.items() if k in keep},
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "attempts": self.attempts,
            "byte_size": len(self.body),
        }


def request_fingerprint(method: str, url: str, secret_params: Sequence[str] = ()) -> str:
    """Hash of method + URL with secret query parameters removed."""
    parts = urlsplit(url)
    q = "&".join(
        p for p in sorted(parts.query.split("&")) if p and p.split("=", 1)[0] not in set(secret_params)
    )
    clean = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, q, ""))
    return hashlib.sha256(f"{method.upper()} {clean}".encode()).hexdigest()


class SafeHttpClient:
    def __init__(
        self,
        *,
        allowed_hosts: Sequence[str],
        timeout_seconds: float = 30.0,
        max_bytes: int = 52_428_800,
        max_redirects: int = 3,
        max_attempts: int = 4,
        contact_email: str | None = None,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        rate_limiter: RateLimiter | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        self.timeout = timeout_seconds
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.max_attempts = max_attempts
        self.resolver = resolver
        self.rate_limiter = rate_limiter or RateLimiter()
        self._sleep = sleep
        ua = "REVA-World/0.1 (+private research MVP"
        ua += f"; mailto:{contact_email})" if contact_email else ")"
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
            trust_env=transport is None,
        )

    async def __aenter__(self) -> SafeHttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def check_url(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise UnsafeRequest("only https URLs are allowed", url=url)
        if parts.username or parts.password:
            raise UnsafeRequest("credentials in URL are forbidden")
        host = (parts.hostname or "").lower()
        if host not in self.allowed_hosts:
            raise UnsafeRequest(f"host {host!r} is not allowlisted")
        if parts.port not in (None, 443):
            raise UnsafeRequest("non-standard port is forbidden")
        try:
            ipaddress.ip_address(host)
            raise UnsafeRequest("literal IP hosts are forbidden")
        except ValueError:
            pass
        addrs = self.resolver(host)
        if not addrs:
            raise UnsafeRequest(f"{host} did not resolve")
        bad = [a for a in addrs if not address_is_public(a)]
        if bad:
            raise UnsafeRequest(f"{host} resolves to non-public address(es)", addresses=bad)
        return host

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        max_bytes: int | None = None,
        accept_status: Sequence[int] = (200,),
    ) -> HttpResult:
        limit = min(max_bytes or self.max_bytes, self.max_bytes)
        attempt = 0
        while True:
            attempt += 1
            try:
                result = await self._get_once(url, headers or {}, limit, attempt)
            except (httpx.TransportError, asyncio.TimeoutError) as e:
                if attempt >= self.max_attempts:
                    raise SourceResponseError(f"transport failure: {type(e).__name__}", retryable=True) from e
                await self._sleep(backoff_seconds(attempt))
                continue
            if result.status in accept_status:
                return result
            retryable = result.status in (429, 500, 502, 503, 504)
            if retryable and attempt < self.max_attempts:
                delay = self._retry_after(result) if result.status in (429, 503) else None
                await self._sleep(delay if delay is not None else backoff_seconds(attempt))
                continue
            raise SourceResponseError(
                f"HTTP {result.status} from {urlsplit(url).hostname}",
                retryable=retryable,
                status=result.status,
                attempts=attempt,
            )

    def _retry_after(self, result: HttpResult) -> float | None:
        value = result.headers.get("retry-after")
        if not value:
            return None
        cap = self.rate_limiter.policy(urlsplit(result.url).hostname or "").max_retry_after_seconds
        if value.isdigit():
            secs = float(value)
        else:
            try:
                dt = email.utils.parsedate_to_datetime(value)
                secs = (dt - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError):
                return None
        if secs > cap:
            raise SourceResponseError("provider Retry-After exceeds budget", retryable=True, retry_after=secs)
        return max(0.0, secs)

    async def _get_once(self, url: str, headers: Mapping[str, str], limit: int, attempt: int) -> HttpResult:
        redirects: list[str] = []
        current = url
        start = time.monotonic()
        while True:
            host = self.check_url(current)
            await self.rate_limiter.acquire(host)
            try:
                async with self._client.stream("GET", current, headers=dict(headers)) as resp:
                    if resp.is_redirect:
                        loc = resp.headers.get("location")
                        if not loc:
                            raise SourceResponseError("redirect without Location", retryable=False)
                        if len(redirects) >= self.max_redirects:
                            raise UnsafeRequest("too many redirects")
                        redirects.append(current)
                        current = urljoin(current, loc)
                        continue
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > limit:
                        raise UnsafeRequest(f"declared content-length {declared} exceeds limit {limit}")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > limit:
                            raise UnsafeRequest(f"response exceeds limit {limit} bytes (decoded)")
                        raw = max(1, resp.num_bytes_downloaded)
                        if total > 1_000_000 and total / raw > MAX_COMPRESSION_RATIO:
                            raise UnsafeRequest("suspicious compression ratio (decompression bomb)")
                        chunks.append(chunk)
                    hdrs = {k.lower(): v for k, v in resp.headers.items()}
                    self.rate_limiter.adapt_from_headers(host, hdrs)
                    return HttpResult(
                        url=url,
                        final_url=current,
                        status=resp.status_code,
                        headers=hdrs,
                        body=b"".join(chunks),
                        retrieved_at=datetime.now(UTC),
                        elapsed_seconds=time.monotonic() - start,
                        redirects=tuple(redirects),
                        attempts=attempt,
                    )
            finally:
                self.rate_limiter.release(host)


def require_json(result: HttpResult) -> Any:
    """Parse JSON, treating HTML error pages (even with HTTP 200) as failures."""
    import json

    head = result.body[:512].lstrip().lower()
    if result.media_type in ("text/html", "application/xhtml+xml") or head.startswith((b"<!doctype", b"<html")):
        raise SourceResponseError("HTML page received where JSON was expected", retryable=True, kind="html_body")
    try:
        return json.loads(result.body, parse_constant=_reject_constant)
    except ValueError as e:
        raise SourceResponseError("malformed JSON", retryable=False, kind="malformed_json") from e


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON constant {name} is forbidden")
