"""V8QA: responses ≥ 1 KB are gzip-compressed (when asked), keep their security headers; secrets never are."""

from __future__ import annotations

import gzip
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr

from reva.api.app import GZIP_MIN_BYTES, Compression, create_app
from reva.settings import Settings


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    for k in list(os.environ):
        if k.startswith("REVA_") or k == "DATABASE_URL":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    s = Settings(
        _env_file=None,
        reva_env="test",
        reva_allowed_hosts="testserver",  # type: ignore[call-arg]
        reva_session_secret=SecretStr("x" * 48),
    )
    return TestClient(create_app(s, static_dir=tmp_path))


def test_large_response_is_gzipped_with_security_headers(client: TestClient) -> None:
    r = client.get("/openapi.json", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in r.headers["vary"].lower()
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"]
    assert r.json()["openapi"]  # httpx decodes it transparently


def test_no_gzip_without_accept_encoding_or_for_small_bodies(client: TestClient) -> None:
    r = client.get("/openapi.json", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in r.headers
    small = client.get("/health/live", headers={"Accept-Encoding": "gzip"})
    assert len(small.content) < GZIP_MIN_BYTES and "content-encoding" not in small.headers
    assert small.headers["x-content-type-options"] == "nosniff"


def test_oversized_body_still_413_under_compression(client: TestClient) -> None:
    r = client.post("/v1/session", content=b"x" * (2 * 1024 * 1024), headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 413
    assert r.headers["x-content-type-options"] == "nosniff"


def test_session_path_is_never_compressed() -> None:
    inner = FastAPI()
    body = "csrf-token " * 500

    @inner.get("/v1/session")
    def session() -> PlainTextResponse:
        return PlainTextResponse(body)

    @inner.get("/v1/other")
    def other() -> PlainTextResponse:
        return PlainTextResponse(body)

    c = TestClient(Compression(inner))
    assert "content-encoding" not in c.get("/v1/session", headers={"Accept-Encoding": "gzip"}).headers
    r = c.get("/v1/other", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip" and r.text == body
    assert gzip.compress(body.encode())  # sanity: compressible payload
