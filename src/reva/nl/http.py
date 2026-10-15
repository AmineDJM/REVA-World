"""HTTPS POST client for LLM providers, built on SafeHttpClient's egress rules.

``SafeHttpClient`` (the connectors' only HTTP path) supports GET only. LLM
providers need one JSON POST per draft, so this subclass adds ``post_json`` and
keeps every SafeHttpClient control: HTTPS only, exact host allowlist (the two
LLM API hosts and nothing else), no userinfo, standard port, literal-IP refusal,
DNS vetting/pinning on the direct path (or the environment egress proxy, which
then enforces SSRF policy and injects provider credentials), bounded response
size and decoding.

Differences from GET, all on the conservative side:

* **No redirects are followed** (a 3xx is an error): a POST body must never be
  replayed to another location.
* **No retries.** Each attempt is one budgeted request; retrying is the
  caller's explicit decision (it is not done in V2).
* The client never adds credentials. Provider authentication is injected by the
  deployment's egress proxy; REVA neither reads nor stores an LLM key.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from reva.adapters.http.rate_limit import HostPolicy, RateLimiter
from reva.adapters.http.safe_client import HttpResult, SafeHttpClient, _BoundedDecoder, content_codings
from reva.kernel.errors import SourceResponseError, UnsafeRequest

OPENAI_HOST = "api.openai.com"
GEMINI_HOST = "generativelanguage.googleapis.com"
LLM_HOSTS: tuple[str, ...] = (OPENAI_HOST, GEMINI_HOST)
#: Largest provider response accepted (a draft is a few KB of JSON).
LLM_MAX_RESPONSE_BYTES = 262_144
#: Largest request body sent (system prompt + catalogue + 4 KB of user text).
LLM_MAX_REQUEST_BYTES = 131_072


class LlmHttpClient(SafeHttpClient):
    """SafeHttpClient restricted to the LLM API hosts, with a single-shot JSON POST."""

    def __init__(
        self,
        *,
        allowed_hosts: tuple[str, ...] = LLM_HOSTS,
        timeout_seconds: float = 60.0,
        max_bytes: int = LLM_MAX_RESPONSE_BYTES,
        **kw: Any,
    ) -> None:
        unknown = {h.lower() for h in allowed_hosts} - set(LLM_HOSTS)
        if unknown:
            raise UnsafeRequest("LLM client hosts are fixed", hosts=sorted(unknown))
        limiter = kw.pop("rate_limiter", None) or RateLimiter(
            default=HostPolicy(min_interval_seconds=0.0, max_concurrency=4, max_retry_after_seconds=0.0)
        )
        super().__init__(
            allowed_hosts=allowed_hosts,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            max_redirects=0,
            max_attempts=1,
            rate_limiter=limiter,
            **kw,
        )

    async def post_json(
        self, url: str, payload: Mapping[str, Any], *, headers: Mapping[str, str] | None = None
    ) -> HttpResult:
        """One POST of ``payload`` as JSON; returns the (bounded) response whatever its status."""
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        if len(body) > LLM_MAX_REQUEST_BYTES:
            raise UnsafeRequest(f"LLM request body larger than {LLM_MAX_REQUEST_BYTES} bytes")
        host = self.check_url(url)
        limit = self.max_bytes
        send = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
        start = time.monotonic()
        await self.rate_limiter.acquire(host)
        try:
            async with self._client.stream("POST", url, content=body, headers=send) as resp:
                if resp.is_redirect:
                    raise UnsafeRequest("LLM provider answered with a redirect; POST bodies are never re-sent")
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
                return HttpResult(
                    url=url,
                    final_url=url,
                    status=resp.status_code,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    body=b"".join(chunks),
                    retrieved_at=datetime.now(UTC),
                    elapsed_seconds=time.monotonic() - start,
                    redirects=(),
                    attempts=1,
                )
        except (TimeoutError, httpx.TransportError) as e:
            raise SourceResponseError(
                f"LLM transport failure: {type(e).__name__}", retryable=True, provider_host=host
            ) from e
        finally:
            self.rate_limiter.release(host)
