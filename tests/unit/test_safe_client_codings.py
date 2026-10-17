"""Content-Encoding handling (agent SAMEAS: peaceagreements.org sends "Content-Encoding: none").

The safe client is in the digest-pinned autonomy template closure, so it keeps refusing ``none``; the atlas live
client (``reva.atlas.ingest._live_client``) reads a lone ``none`` as ``identity`` through an httpx response hook
and records the label the provider sent (SWEEP2).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from reva.adapters.connectors import eurostat_template as et
from reva.adapters.http.rate_limit import HostPolicy, RateLimiter
from reva.adapters.http.safe_client import SafeHttpClient, UnsafeRequest, content_codings
from reva.atlas import ingest as ai

HOST = "www.peaceagreements.org"
REPO = Path(__file__).resolve().parents[2]


def test_safe_client_codings_unchanged() -> None:
    assert content_codings(httpx.Headers({})) == "identity"
    assert content_codings(httpx.Headers({"content-encoding": "gzip"})) == "gzip"
    for bad in ("none", "br", "gzip, none"):
        with pytest.raises(UnsafeRequest):
            content_codings(httpx.Headers({"content-encoding": bad}))


def test_template_closure_still_matches_the_pinned_digest() -> None:
    pol = yaml.safe_load((REPO / "registry/policies/promotion.yaml").read_text(encoding="utf-8"))
    assert et.template_digest() == pol["approved_templates"][et.TEMPLATE_ID]["template_sha256"]


def _client(handler: httpx.MockTransport) -> SafeHttpClient:
    c = SafeHttpClient(
        allowed_hosts=[HOST],
        transport=handler,
        resolver=lambda _h: ["93.184.216.34"],
        rate_limiter=RateLimiter(default=HostPolicy(min_interval_seconds=0)),
    )
    c._client.event_hooks = {"request": [], "response": [ai._identity_coding_alias]}
    return c


async def test_atlas_hook_reads_none_as_identity() -> None:
    body = b"id,name\n1,x\n"
    t = httpx.MockTransport(lambda r: httpx.Response(200, content=body, headers={"content-encoding": "none"}))
    async with _client(t) as c:
        res = await c.get(f"https://{HOST}/data.csv")
    assert res.body == body
    assert res.headers["content-encoding"] == "identity"
    assert res.headers[ai.ORIGINAL_CODING_HEADER] == "none"


@pytest.mark.parametrize("coding", ["br", "gzip, none", "none, none"])
async def test_atlas_hook_leaves_other_codings_to_the_safe_client(coding: str) -> None:
    t = httpx.MockTransport(lambda r: httpx.Response(200, content=b"x", headers={"content-encoding": coding}))
    async with _client(t) as c:
        with pytest.raises(UnsafeRequest):
            await c.get(f"https://{HOST}/data.csv")


async def test_live_client_installs_the_hook() -> None:
    spec = SimpleNamespace(min_interval_seconds=1.0, allowed_hosts=(HOST,), max_response_bytes=1024)
    settings = SimpleNamespace(
        reva_http_timeout_seconds=30, reva_http_contact_email=None, reva_http_proxy_mode="direct"
    )
    c = ai._live_client(spec, settings)  # type: ignore[arg-type]
    try:
        assert c._client.event_hooks["response"] == [ai._identity_coding_alias]
    finally:
        await c.aclose()
