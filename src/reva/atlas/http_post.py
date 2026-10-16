"""HTTP POST for atlas sources whose APIs are POST-only (PxWeb, IRENA, UNECE...).

``SafeHttpClient`` (inside the digest-pinned autonomy template closure) only issues GETs. This module
sends one POST **through an existing SafeHttpClient instance** with the same guarantees: https only,
allow-listed host resolved and pinned (``check_url``), the client's per-host rate limiter and
politeness, the bounded decoder (raw and decoded size capped, single permitted content coding), and
the same retry policy on transport errors / 429 / 5xx (a PxWeb query is a read: repeating it is safe).
Redirects are **not** followed (a POST body is never re-sent to another location): a 3xx is an error.

It never creates a client: the atlas run supplies the metered live client (``reva.atlas.ingest``),
whose budget accounting covers the POST exactly like a GET (``AtlasRun.post``).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from reva.adapters.http.rate_limit import backoff_seconds
from reva.adapters.http.safe_client import HttpResult, _BoundedDecoder, content_codings
from reva.kernel.errors import SourceResponseError, UnsafeRequest

MAX_POST_BODY = 1_048_576  # a query document, never an upload


async def safe_post(
    client: Any,
    url: str,
    *,
    content: bytes,
    headers: Mapping[str, str] | None = None,
    max_bytes: int | None = None,
    accept_status: Sequence[int] = (200,),
) -> HttpResult:
    """POST ``content`` to ``url`` through ``client`` (a SafeHttpClient); returns an ``HttpResult``."""
    if len(content) > MAX_POST_BODY:
        raise UnsafeRequest(f"POST body of {len(content)} bytes exceeds {MAX_POST_BODY}")
    limit = min(max_bytes or client.max_bytes, client.max_bytes)
    attempt = 0
    while True:
        attempt += 1
        try:
            result = await _post_once(client, url, content, dict(headers or {}), limit, attempt)
        except (TimeoutError, httpx.TransportError) as e:
            if attempt >= client.max_attempts:
                raise SourceResponseError(f"transport failure: {type(e).__name__}", retryable=True) from e
            await client._sleep(backoff_seconds(attempt))
            continue
        if result.status in accept_status:
            return result
        retryable = result.status in (429, 500, 502, 503, 504)
        if retryable and attempt < client.max_attempts:
            delay = client._retry_after(result) if result.status in (429, 503) else None
            await client._sleep(delay if delay is not None else backoff_seconds(attempt))
            continue
        raise SourceResponseError(
            f"HTTP {result.status} from {urlsplit(url).hostname} (POST)",
            retryable=retryable,
            status=result.status,
            attempts=attempt,
        )


async def _post_once(
    client: Any, url: str, content: bytes, headers: dict[str, str], limit: int, attempt: int
) -> HttpResult:
    host = client.check_url(url)  # https, allow-listed, no credentials/IP/port; resolves and pins
    await client.rate_limiter.acquire(host)
    start = time.monotonic()
    try:
        async with client._client.stream("POST", url, headers=headers, content=content) as resp:
            if resp.is_redirect:
                raise UnsafeRequest("redirect in reply to a POST is not followed")
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise UnsafeRequest(f"declared content-length {declared} exceeds limit {limit}")
            decoder = _BoundedDecoder(content_codings(resp.headers), limit)
            chunks: list[bytes] = []
            raw_iter: Any = resp.stream if resp.is_stream_consumed else resp.aiter_raw()
            async for raw in raw_iter:
                if decoder.raw + len(raw) > limit:
                    raise UnsafeRequest(f"response exceeds limit {limit} bytes (raw)")
                chunks.extend(decoder.feed(raw))
            chunks.extend(decoder.finish())
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            client.rate_limiter.adapt_from_headers(host, hdrs)
            return HttpResult(
                url=url,
                final_url=url,
                status=resp.status_code,
                headers=hdrs,
                body=b"".join(chunks),
                retrieved_at=datetime.now(UTC),
                elapsed_seconds=time.monotonic() - start,
                redirects=(),
                attempts=attempt,
            )
    finally:
        client.rate_limiter.release(host)
