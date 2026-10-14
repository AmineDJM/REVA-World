"""SafeHttpClient: the only HTTP path for connectors.

* HTTPS only; exact host allowlist; no userinfo; standard port only.
* Every resolved address must be globally routable; checked before the first
  request and on every redirect (private, loopback, link-local, metadata,
  multicast, reserved and CGNAT ranges are rejected).
* **DNS pinning** (direct egress): the TCP connection goes to an address that
  was vetted. ``check_url`` resolves the host once through ``resolver``, vets
  *every* answer and pins that answer set for the host; the connection pool's
  network backend (``PinnedNetworkBackend``) connects only to pinned (or, when
  no pin exists, freshly resolved *and vetted*) addresses. TLS SNI and
  certificate verification still use the hostname, and the ``Host`` header is
  the hostname, so no trust is placed in the IP. httpx/httpcore never perform
  their own name resolution on the direct path.
* **Egress proxy** (``REVA_HTTP_PROXY_MODE=env``): when an HTTPS proxy is
  configured in the environment (``HTTPS_PROXY``/``https_proxy``/``ALL_PROXY``),
  requests to hosts not excluded by ``NO_PROXY`` are tunnelled (CONNECT) through
  it and *the proxy resolves the name*. The local resolution/vetting then is
  only a pre-check: a rebinding answer or a split-horizon entry seen by the
  proxy is not covered. In that mode the egress proxy is the SSRF enforcement
  point and must itself apply the host allowlist and deny private, loopback,
  link-local, metadata and CGNAT destinations. Proxy use is explicit:
  ``direct`` is the default in production, ``env`` is the default for
  local/test (sandboxes that only have proxied egress) and must be set
  explicitly in production.
* Bounded redirects, timeouts, response size and compression ratio.
  Content decoding is done here, not by httpx: only a single ``identity``,
  ``gzip`` or ``deflate`` coding is accepted (stacked codings such as
  ``gzip, gzip`` and unknown codings are rejected), and raw bytes are inflated
  incrementally with ``zlib.decompressobj(...).decompress(data, max_length)`` so
  decoded output never exceeds ``limit + 1`` bytes at any time.
* 429/503 honour Retry-After within a cap; 5xx/transport errors use jittered
  exponential backoff; bounded attempts.
* Bodies are untrusted data. Secrets never appear in request fingerprints,
  response metadata or httpx/httpcore INFO logs.
"""

from __future__ import annotations

import asyncio
import email.utils
import hashlib
import ipaddress
import logging
import os
import socket
import time
import typing
import urllib.request
import zlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpcore
import httpx

from reva.adapters.http.rate_limit import RateLimiter, backoff_seconds
from reva.kernel.errors import ConfigurationError, SourceResponseError, UnsafeRequest

Resolver = Callable[[str], Sequence[str]]
ProxyMode = Literal["direct", "env"]
METADATA_ADDRESSES = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}
MAX_COMPRESSION_RATIO = 200
ALLOWED_CODINGS = frozenset({"identity", "gzip", "x-gzip", "deflate"})
#: Query parameters treated as secrets in fingerprints and persisted metadata.
SECRET_QUERY_PARAMS = frozenset(
    {
        "subscription-key",
        "api_key",
        "api-key",
        "apikey",
        "key",
        "token",
        "access_token",
        "secret",
        "password",
        "signature",
        "sig",
    }
)

# httpx logs every request URL at INFO (including query-string secrets such as
# subscription-key); keep both transports at WARNING regardless of REVA_LOG_LEVEL.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


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


def redact_url(url: str, secret_params: Iterable[str] = SECRET_QUERY_PARAMS) -> str:
    """URL with the values of secret query parameters replaced by ``***``."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    secret = {s.lower() for s in secret_params}
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k.lower() in secret for k, _ in pairs):
        return url  # unchanged (no re-encoding) when nothing is secret
    pairs = [(k, "***" if k.lower() in secret else v) for k, v in pairs]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs, safe="*"), parts.fragment))


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
            "final_url": redact_url(self.final_url),
            "redirects": [redact_url(u) for u in self.redirects],
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


# --------------------------------------------------------------------------- DNS pinning
class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that connects only to vetted addresses.

    ``connect_tcp(host, port)`` is called by httpcore with the *hostname* of the
    origin. Instead of letting the OS resolve it again, the backend takes the
    address set pinned by ``SafeHttpClient.check_url`` (or resolves and vets a
    fresh one when nothing is pinned) and opens the TCP connection to one of
    those IPs. httpcore then performs TLS with ``server_hostname=<hostname>``,
    so certificate verification is unchanged.
    """

    def __init__(self, addresses_for: Callable[[str], Sequence[str]], inner: httpcore.AsyncNetworkBackend | None = None):
        self._addresses_for = addresses_for
        self._inner = inner or httpcore.AnyIOBackend()  # asyncio or trio via anyio

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addrs = list(self._addresses_for(host))  # every address vetted; raises UnsafeRequest otherwise
        if not addrs:
            raise UnsafeRequest(f"{host} has no vetted address")
        last: Exception | None = None
        for addr in addrs:
            try:
                return await self._inner.connect_tcp(
                    addr, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as e:
                last = e
        assert last is not None
        raise last

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: typing.Iterable[Any] | None = None) -> httpcore.AsyncNetworkStream:
        raise UnsafeRequest("unix sockets are not permitted for outbound HTTP")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _pinned_transport(backend: httpcore.AsyncNetworkBackend, ssl_context: Any) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(verify=ssl_context, trust_env=False)
    if not hasattr(transport, "_pool"):  # pragma: no cover - fail closed on an httpx internals change
        raise ConfigurationError("httpx transport internals changed; DNS pinning unavailable")
    transport._pool = httpcore.AsyncConnectionPool(  # noqa: SLF001 - documented single integration point
        ssl_context=ssl_context,
        max_connections=100,
        max_keepalive_connections=20,
        keepalive_expiry=5.0,
        network_backend=backend,
    )
    return transport


class _EgressRouter(httpx.AsyncBaseTransport):
    """Direct (pinned) by default; the env proxy only for hosts it applies to."""

    def __init__(self, direct: httpx.AsyncBaseTransport, proxied: httpx.AsyncBaseTransport | None,
                 bypass: Callable[[str], bool]):
        self.direct = direct
        self.proxied = proxied
        self._bypass = bypass

    def uses_proxy(self, host: str) -> bool:
        return self.proxied is not None and not self._bypass(host)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        t = self.proxied if self.uses_proxy(request.url.host) else self.direct
        assert t is not None
        return await t.handle_async_request(request)

    async def aclose(self) -> None:
        await self.direct.aclose()
        if self.proxied is not None:
            await self.proxied.aclose()


def resolve_proxy_mode(mode: str | None = None, environ: Mapping[str, str] | None = None) -> ProxyMode:
    """Explicit proxy mode: argument > REVA_HTTP_PROXY_MODE > (direct in production, env elsewhere)."""
    env = os.environ if environ is None else environ
    value = (mode or env.get("REVA_HTTP_PROXY_MODE") or "").strip().lower()
    if not value:
        value = "direct" if env.get("REVA_ENV", "local") == "production" else "env"
    if value not in ("direct", "env"):
        raise ConfigurationError("REVA_HTTP_PROXY_MODE must be 'direct' or 'env'")
    return typing.cast(ProxyMode, value)


def environment_https_proxy(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        if env.get(key):
            return env[key]
    return None


def _no_proxy_bypass(environ: Mapping[str, str] | None = None) -> Callable[[str], bool]:
    env = os.environ if environ is None else environ
    no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""

    def bypass(host: str) -> bool:
        if not no_proxy:
            return False
        return bool(urllib.request.proxy_bypass_environment(host, {"no": no_proxy}))

    return bypass


# --------------------------------------------------------------------------- bounded decoding
class _BoundedDecoder:
    """Incremental content decoder whose output never exceeds ``limit + 1`` bytes."""

    def __init__(self, coding: str, limit: int):
        self.coding = coding
        self.limit = limit
        self.raw = 0
        self.total = 0
        self._first = True
        self._d: Any = None
        if coding in ("gzip", "x-gzip"):
            self._d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif coding == "deflate":
            self._d = zlib.decompressobj(zlib.MAX_WBITS)  # zlib-wrapped; raw deflate fallback below

    def _account(self, piece: bytes) -> None:
        self.total += len(piece)
        if self.total > self.limit:
            raise UnsafeRequest(f"response exceeds limit {self.limit} bytes (decoded)")
        if self.total > 1_000_000 and self.total / max(1, self.raw) > MAX_COMPRESSION_RATIO:
            raise UnsafeRequest("suspicious compression ratio (decompression bomb)")

    def feed(self, raw: bytes) -> list[bytes]:
        self.raw += len(raw)
        if self._d is None:
            self._account(raw)
            return [raw]
        out: list[bytes] = []
        data = raw
        while data:
            if self._d.eof:
                if not data.strip(b"\x00"):
                    break  # zero padding after the end of the stream is ignored
                if self.coding == "deflate":
                    raise SourceResponseError("trailing data after deflate stream", retryable=False,
                                              kind="decoding_error")
                self._d = zlib.decompressobj(16 + zlib.MAX_WBITS)  # next gzip member (RFC 1952 multi-member)
            budget = self.limit - self.total + 1
            try:
                piece = self._d.decompress(data, budget)
            except zlib.error as e:
                if self.coding == "deflate" and self._first:
                    self._d = zlib.decompressobj(-zlib.MAX_WBITS)  # raw deflate (non-conforming servers)
                    self._first = False
                    continue
                raise SourceResponseError("corrupt compressed response body", retryable=False,
                                          kind="decoding_error") from e
            self._first = False
            self._account(piece)
            if piece:
                out.append(piece)
            nxt = self._d.unused_data if self._d.eof else self._d.unconsumed_tail
            if not piece and nxt == data:
                break  # no progress possible (defensive)
            data = nxt
        return out

    def finish(self) -> list[bytes]:
        if self._d is None:
            return []
        out: list[bytes] = []
        while not self._d.eof:
            piece = self._d.decompress(self._d.unconsumed_tail, self.limit - self.total + 1)
            if not piece:
                break
            self._account(piece)
            out.append(piece)
        if not self._d.eof:
            raise SourceResponseError("truncated compressed response body", retryable=True, kind="decoding_error")
        return out


def content_codings(headers: httpx.Headers) -> str:
    """The single permitted content coding, or raise for stacked/unknown codings."""
    values = headers.get_list("content-encoding")
    codings = [c.strip().lower() for v in values for c in v.split(",") if c.strip()]
    if not codings:
        return "identity"
    if len(codings) > 1:
        raise UnsafeRequest(f"stacked content-encoding {codings} is not accepted")
    if codings[0] not in ALLOWED_CODINGS:
        raise UnsafeRequest(f"unsupported content-encoding {codings[0]!r}")
    return codings[0]


# --------------------------------------------------------------------------- client
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
        proxy_mode: ProxyMode | None = None,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        self.timeout = timeout_seconds
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.max_attempts = max_attempts
        self.resolver = resolver
        self.rate_limiter = rate_limiter or RateLimiter()
        self._sleep = sleep
        self._pins: dict[str, tuple[str, ...]] = {}
        self.proxy_mode: ProxyMode = resolve_proxy_mode(proxy_mode, environ)
        self.proxy_url: str | None = None
        ua = "REVA-World/0.1 (+private research MVP"
        ua += f"; mailto:{contact_email})" if contact_email else ")"
        if transport is None:
            ssl_context = httpx.create_ssl_context()  # certificate trust only (SSL_CERT_FILE/DIR); never proxies
            backend = PinnedNetworkBackend(self._vetted_addresses, network_backend)
            direct = _pinned_transport(backend, ssl_context)
            proxied: httpx.AsyncBaseTransport | None = None
            if self.proxy_mode == "env":
                self.proxy_url = environment_https_proxy(environ)
                if self.proxy_url:
                    proxied = httpx.AsyncHTTPTransport(proxy=self.proxy_url, verify=ssl_context, trust_env=False)
            transport = _EgressRouter(direct, proxied, _no_proxy_bypass(environ))
        self._transport = transport
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
            trust_env=False,  # proxy use is explicit (proxy_mode), never implicit
        )

    async def __aenter__(self) -> SafeHttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def egress_for(self, host: str) -> str:
        """'proxy' when requests to ``host`` go through the env proxy, else 'direct'."""
        t = self._transport
        return "proxy" if isinstance(t, _EgressRouter) and t.uses_proxy(host) else "direct"

    def _resolve_and_vet(self, host: str) -> tuple[str, ...]:
        addrs = tuple(self.resolver(host))
        if not addrs:
            raise UnsafeRequest(f"{host} did not resolve")
        bad = [a for a in addrs if not address_is_public(a)]
        if bad:
            raise UnsafeRequest(f"{host} resolves to non-public address(es)", addresses=bad)
        return addrs

    def _vetted_addresses(self, host: str) -> Sequence[str]:
        """Connect-time addresses: the pinned answer, else a fresh vetted resolution."""
        host = host.lower()
        if host not in self.allowed_hosts:
            raise UnsafeRequest(f"host {host!r} is not allowlisted")
        pinned = self._pins.get(host)
        if pinned:
            return pinned
        addrs = self._resolve_and_vet(host)
        self._pins[host] = addrs
        return addrs

    def check_url(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise UnsafeRequest("only https URLs are allowed", url=redact_url(url))
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
        # Resolve once, vet every answer, and pin it: the connection goes to one of these.
        self._pins[host] = self._resolve_and_vet(host)
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
                    decoder = _BoundedDecoder(content_codings(resp.headers), limit)
                    chunks: list[bytes] = []
                    # Raw (still encoded) bytes. A pre-read in-memory response (test transports
                    # built with content=) exposes its raw bytes through ``resp.stream``.
                    raw_iter: Any = resp.stream if resp.is_stream_consumed else resp.aiter_raw()
                    async for raw in raw_iter:
                        if decoder.raw + len(raw) > limit:
                            raise UnsafeRequest(f"response exceeds limit {limit} bytes (raw)")
                        chunks.extend(decoder.feed(raw))
                    chunks.extend(decoder.finish())
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
        return json.loads(result.body, parse_constant=_reject_constant, parse_float=_finite_float)
    except ValueError as e:
        raise SourceResponseError("malformed JSON", retryable=False, kind="malformed_json") from e


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON constant {name} is forbidden")


def _finite_float(literal: str) -> float:
    value = float(literal)
    if value in (float("inf"), float("-inf")) or value != value:
        raise ValueError("JSON number overflows to a non-finite float")
    return value
