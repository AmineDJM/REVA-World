"""V8SCORES API (/v1/v8/scores/*) on a minimal app: auth stubbed to a read principal, registry DB calls stubbed
(the live path against the dev DB is in tests/integration/v8/test_v8_scores_live_db.py)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from reva.api.auth import Principal
from reva.api.errors import ApiProblem
from reva.api.routes import v8_scores as V
from reva.v8.scores import registry as REG

LIVE: dict[str, Any] = {
    "computed_at": "2026-10-17T22:00:00+00:00", "compute_ms": 1.0, "origin": None, "registries": [],
    "totals": {"sealed_batches": 0, "questions": 0, "forecasts": 0, "pending": 0, "resolved": 0, "void": 0,
               "not_prospective": 0},
    "next_resolution": None, "overdue_groups": 0, "upcoming": [], "upcoming_total": 0, "ensemble": None,
    "refreshing": False,
}  # fmt: skip


class _Auth:
    def authenticate(self, request: Request) -> Principal:
        return Principal(username="t", actor="token:t", method="token", scopes=frozenset({"read"}))


class _Live:
    def get(self, engine: Any, store: Any, origin: Any, *, wait: bool = True) -> dict[str, Any] | None:
        return LIVE if wait else None


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    @contextmanager
    def fake_conn(request: Request) -> Iterator[None]:
        yield None

    monkeypatch.setattr(V, "read_conn", fake_conn)
    monkeypatch.setattr(V.L, "sealed_models", lambda conn, origin: {"forecast.bm_stack": 7})
    monkeypatch.setattr(V.L, "SERVICE", _Live())
    monkeypatch.setattr(V, "get_engine", lambda request: None)
    monkeypatch.setattr(V, "_store", lambda request: None)
    app = FastAPI()
    app.state.auth = _Auth()

    @app.exception_handler(ApiProblem)
    async def _problem(request: Request, exc: ApiProblem) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse({"code": exc.code}, status_code=exc.http_status)

    app.include_router(V.router)
    with TestClient(app) as c:
        yield c


def test_bundle(client: TestClient) -> None:
    r = client.get("/v1/v8/scores")
    assert r.status_code == 200, r.text
    body = r.json()
    d = body["data"]
    assert d["headline"] == REG.headline(REG.load()["rows"])
    assert body["context_mode"] == "evaluation"
    assert {w["code"] for w in body["warnings"]} >= {"judged_forecasts", "not_advice"}
    assert d["matrix"] and d["forecasters"] and d["calendar"]
    assert client.get("/v1/v8/scores/matrix").json()["data"]["headline"] == d["headline"]


def test_track_cell_live_calendar_doc(client: TestClient) -> None:
    t = client.get("/v1/v8/scores/tracks/beat_health")
    assert t.status_code == 200 and t.json()["data"]["rows"]
    assert client.get("/v1/v8/scores/tracks/nope").status_code == 404
    row = REG.load()["rows"][0]
    key = f"{row['target']}~{row.get('forecaster') or 'naive'}"
    c = client.get(f"/v1/v8/scores/cells/{key}")
    assert c.status_code == 200 and any(x["id"] == row["id"] for x in c.json()["data"]["rows"])
    assert client.get("/v1/v8/scores/live").json()["data"]["totals"]["questions"] == 0
    cal = client.get("/v1/v8/scores/calendar").json()["data"]
    assert cal["scoreboard"] and cal["registry"] == []
    assert client.get("/v1/v8/scores/doc", params={"path": "docs/dev/v7_beat_health.md"}).status_code == 200
    assert client.get("/v1/v8/scores/doc", params={"path": "pyproject.toml"}).status_code == 404
