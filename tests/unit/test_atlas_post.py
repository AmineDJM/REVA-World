"""AtlasRun.post (ATLASOPS, GAPHUNT request): budget-metered POST for POST-only APIs (PxWeb, IRENA, UNECE).

Offline: an httpx MockTransport behind a real SafeHttpClient (allow-list, pinning, bounded decoding,
retries), the ledger top-up monkeypatched. Checks metering (requests + request/response bytes on the
run's MeteredHttp, refusal before the transport when the budget is spent), provenance (request body
archived in response_metadata, fingerprint covering the body), no redirects, fixture replay.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from reva.adapters.http.safe_client import SafeHttpClient
from reva.atlas.ingest import AtlasRun
from reva.atlas.sources import AtlasSourceSpec
from reva.kernel.errors import SourceResponseError, UnsafeRequest, ValidationProblem
from reva.operations.budgets import BudgetExhausted, IngestBudget, MeteredHttp, Reservation

URL = "https://px.example.org/api/v1/en/table.px"
SPEC = AtlasSourceSpec(
    name="atlas.posttest",
    version="1.0.0",
    source_key="s.src",
    atlas_source="posttest",
    provider="test",
    licence="CC0-1.0",
    allowed_hosts=("px.example.org",),
    datasets=("t",),
    cadence_seconds=86400,
    planned_requests=1,
    parser_version="t/1",
)


def _client(handler: Any) -> SafeHttpClient:
    async def nosleep(_s: float) -> None:
        return None

    return SafeHttpClient(
        allowed_hosts=["px.example.org"],
        transport=httpx.MockTransport(handler),
        resolver=lambda host: ("93.184.216.34",),
        sleep=nosleep,
    )


def _metered(inner: Any, monkeypatch: pytest.MonkeyPatch, grant: int = 10**9) -> MeteredHttp:
    import reva.operations.budgets as budgets

    monkeypatch.setattr(budgets, "top_up", lambda *a, **k: grant)
    r = Reservation("s.src", "op:requests", "op:bytes", 1, 0, 0, "t")
    return MeteredHttp(inner, None, r, budget=IngestBudget("t", "", 10, 10**9, {"s.src": 10}))  # type: ignore[arg-type]


def _run(http: Any) -> AtlasRun:
    run = AtlasRun.offline(SPEC, http, origin="live")
    run.engine = object()  # type: ignore[assignment]  # not offline: a live run must use the metered client
    return run


def test_post_is_metered_and_archives_its_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, bytes, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.content, req.headers["content-type"]))
        return httpx.Response(200, json={"data": [1, 2]}, headers={"content-type": "application/json"})

    m = _metered(_client(handler), monkeypatch)
    run = _run(m)
    q = {"query": [{"code": "Tid", "selection": {"filter": "all", "values": ["*"]}}], "response": {"format": "json"}}
    raw = asyncio.run(run.post(URL, key="px:t1", json_body=q))
    body = json.dumps(q, sort_keys=True, separators=(",", ":")).encode()
    assert seen == [("POST", body, "application/json")]
    assert m.requests == 1 and m.bytes == len(body) + len(raw.body)  # request + response bytes metered
    req = raw.response_metadata["request"]
    assert req["method"] == "POST" and req["body"] == body.decode()
    assert req["body_sha256"] == hashlib.sha256(body).hexdigest() == raw.item.params["request_body_sha256"]
    other = asyncio.run(run.post(URL, key="px:t2", json_body={**q, "x": 1}))
    assert other.request_fingerprint != raw.request_fingerprint  # a different query is a different acquisition


def test_post_refused_before_the_transport_when_budget_is_spent(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be reached
        called.append(1)
        return httpx.Response(200)

    m = _metered(_client(handler), monkeypatch, grant=0)
    m.requests = 1  # the reservation (1 request) is used up and the ledger grants nothing more
    with pytest.raises(BudgetExhausted):
        asyncio.run(_run(m).post(URL, key="k", json_body={}))
    assert called == [] and m.refused == 1


def test_post_safety_redirects_retries_and_unmetered_live_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    def redirect(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://px.example.org/other"})

    with pytest.raises(UnsafeRequest, match="redirect"):
        asyncio.run(_run(_metered(_client(redirect), monkeypatch)).post(URL, key="k", json_body={}))
    n = {"i": 0}

    def flaky(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        return httpx.Response(503 if n["i"] == 1 else 200, content=b"ok")

    raw = asyncio.run(
        _run(_metered(_client(flaky), monkeypatch)).post(URL, key="k", content=b"q", content_type="text/plain")
    )
    assert raw.body == b"ok" and n["i"] == 2
    with pytest.raises(UnsafeRequest):  # host not allow-listed
        asyncio.run(
            _run(_metered(_client(flaky), monkeypatch)).post("https://evil.example.com/x", key="k", json_body={})
        )
    with pytest.raises(ValidationProblem):
        asyncio.run(_run(_metered(_client(flaky), monkeypatch)).post(URL, key="k"))
    with pytest.raises(Exception, match="metered client"):  # a live run never POSTs unmetered
        asyncio.run(_run(_client(flaky)).post(URL, key="k", json_body={}))


def test_post_fixture_replay(tmp_path: Path) -> None:
    from reva.evidence.runner import FixtureHttp

    body = json.dumps({"q": 1}, sort_keys=True, separators=(",", ":")).encode()
    fp = hashlib.sha256(f"POST {URL} body:{hashlib.sha256(body).hexdigest()}".encode()).hexdigest()
    d = tmp_path / "atlas.posttest"
    d.mkdir()
    (d / "r.json").write_bytes(b'{"ok":true}')
    entry = {"request_fingerprint": fp, "url": URL, "file": "r.json", "sha256": hashlib.sha256(b'{"ok":true}').hexdigest(),
             "status": 200, "headers": {"content-type": "application/json"}}  # fmt: skip
    (d / "manifest.json").write_text(json.dumps([entry]))
    run = AtlasRun.offline(SPEC, FixtureHttp("atlas.posttest", root=tmp_path))
    raw = asyncio.run(run.post(URL, key="k", json_body={"q": 1}))
    assert raw.body == b'{"ok":true}' and raw.origin == "test_fixture" and raw.request_fingerprint == fp
    with pytest.raises(SourceResponseError, match="no captured fixture"):
        asyncio.run(run.post(URL, key="k", json_body={"q": 2}))
