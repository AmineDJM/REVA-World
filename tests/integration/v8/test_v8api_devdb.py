"""V8API against the dev database ``reva`` and the built search index (skipped when either is missing).

Read-only on the data: search reads the index file, the change feed reads as ``reva_api``; the board CRUD test runs
the 0020 SECURITY DEFINER functions inside a transaction that is rolled back (nothing persists).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import text

from reva.api.auth import Principal

pytestmark = pytest.mark.db

QUERIES = ["France", "pétrole brut", "FRA", "imatinib", "inflation", "HGNC:1100", "Brent", "OPEP", "paludisme",
           "Ghawar", "Allemagne", "chômage", "malaria", "CHEMBL941", "Q142", "FR-IDF", "cote d'ivoire", "BRCA1",
           "petrol", "OPEC"]  # fmt: skip


@pytest.fixture(scope="module")
def engine() -> Any:
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings

    try:
        eng = make_engine("api", get_settings())
        with eng.connect() as c:
            c.execute(text("SELECT 1 FROM ops.v8_board LIMIT 1"))
    except Exception:
        pytest.skip("dev database (with migration 0020) not reachable")
    return eng


@pytest.fixture(scope="module")
def index() -> Any:
    from reva.v8.search.index import IndexMissing, get_index

    try:
        return get_index()
    except IndexMissing:
        pytest.skip("search index not built (uv run reva v8 index build)")


def test_typical_queries_find_the_expected_records(index: Any) -> None:
    def top(q: str, n: int = 3) -> list[str]:
        return [h["id"] for h in index.search(q, limit=n)["hits"]]

    assert top("France", 1) == ["FRA"]
    assert top("FRA", 1) == ["FRA"]
    assert top("HGNC:1100", 1) == ["hgnc:1100"]
    assert "chembl:CHEMBL941" in top("imatinib")
    assert top("Allemagne", 1) == ["DEU"]
    r = index.search("pétrole brut", limit=10)["hits"]
    assert r and all("petrole brut" in h["matched"].lower().replace("é", "e") for h in r)
    assert any(h["kind"] == "decision" for h in index.search("OPEP", limit=5)["hits"])


def test_search_latency_p95_under_150ms_warm(index: Any) -> None:
    for q in QUERIES:  # warm
        index.search(q)
    ms = []
    for _ in range(3):
        for q in QUERIES:
            t = time.perf_counter()
            index.search(q)
            ms.append((time.perf_counter() - t) * 1000)
    ms.sort()
    p95 = ms[int(0.95 * (len(ms) - 1))]
    assert p95 < 150, f"p95 {p95:.1f} ms"


class _Auth:
    def authenticate(self, request: Request) -> Principal:
        return Principal(username="t", actor="token:v8api-test", method="token", scopes=frozenset({"read"}))


def _app(engine: Any, *routers: Any) -> FastAPI:
    from reva.api.errors import install_error_handlers
    from reva.settings import get_settings

    app = FastAPI()
    app.state.auth = _Auth()
    app.state.settings = get_settings()
    app.state.engine = lambda: engine
    install_error_handlers(app)
    for r in routers:
        app.include_router(r)
    return app


def test_search_route(engine: Any, index: Any) -> None:
    from reva.api.routes import v8_search

    with TestClient(_app(engine, v8_search.router)) as c:
        r = c.get("/v1/v8/search", params={"q": "inflation", "limit": 5})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["hits"] and all(h["href"].startswith("/v8/") for h in d["hits"])
        assert {w["code"] for w in r.json()["warnings"]} >= {"lookup_not_identity"}
        assert c.get("/v1/v8/search", params={"q": "x", "kinds": "nope"}).status_code == 422
        assert c.get("/v1/v8/search/status").json()["data"]["built"] is True


def test_change_feed_reads_recorded_facts(engine: Any) -> None:
    from reva.api.routes import v8_changes

    with TestClient(_app(engine, v8_changes.router)) as c:
        since = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        r = c.get("/v1/v8/changes", params={"since": since, "limit": 20})
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        ats = [i["at"] for i in d["items"]]
        assert ats == sorted(ats, reverse=True)
        assert sum(d["counts"].values()) >= len(d["items"])
        for i in d["items"]:
            assert i["title"] and i["kind"] in d["kind_labels"]
            assert all(isinstance(v, int) for v in i["counts"].values())
        nxt = r.json()["next_cursor"]
        if nxt:
            r2 = c.get("/v1/v8/changes", params={"since": since, "limit": 20, "cursor": nxt})
            assert r2.status_code == 200
            assert not {i["id"] for i in r2.json()["data"]["items"]} & {i["id"] for i in d["items"]}
        r = c.get("/v1/v8/changes", params={"since": since, "kinds": "ingest", "limit": 5})
        assert {i["kind"] for i in r.json()["data"]["items"]} <= {"ingest"}


def test_board_crud_and_alerts_rolled_back(engine: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """CRUD through the routes on one connection whose outer transaction is rolled back at the end."""
    from reva.api.routes import v8_boards

    conn = engine.connect()
    outer = conn.begin()

    @contextmanager
    def same_conn(request: Request) -> Iterator[Any]:
        with conn.begin_nested():
            yield conn

    monkeypatch.setattr(v8_boards, "_write", same_conn)
    monkeypatch.setattr(v8_boards, "read_conn", same_conn)
    monkeypatch.setattr(v8_boards, "read_changes", lambda c, q, store=None: {"items": []})
    try:
        with TestClient(_app(engine, v8_boards.router)) as c:
            items = [{"kind": "series", "uid": "i1", "added_at": "t", "concept": "imf.inflation_cpi_avg",
                      "geo": "FRA", "label": "Inflation FR", "table": "imf.weo"}]  # fmt: skip
            r = c.post("/v1/v8/boards", json={"title": "Test V8API", "items": items, "id": "b_v8api_test"})
            assert r.status_code == 201, r.text
            assert r.json()["data"]["version"] == 1
            assert c.post("/v1/v8/boards", json={"title": "dup", "id": "b_v8api_test"}).status_code == 409
            r = c.post("/v1/v8/boards/b_v8api_test/rules", json={"kind": "threshold", "item_uid": "i1",
                                                                  "op": ">", "value": "2.5"})  # fmt: skip
            assert r.status_code == 201, r.text
            b = r.json()["data"]
            assert b["version"] == 2 and b["rules"][0]["op"] == ">"
            bad = c.post("/v1/v8/boards/b_v8api_test/rules", json={"kind": "resolution", "item_uid": "i1"})
            assert bad.status_code == 422
            stale = c.post("/v1/v8/boards/b_v8api_test", json={"title": "x", "items": items, "rules": [],
                                                               "version": 1})  # fmt: skip
            assert stale.status_code == 409
            ok = c.post("/v1/v8/boards/b_v8api_test", json={"title": "Renamed", "items": items,
                                                            "rules": b["rules"], "version": 2})  # fmt: skip
            assert ok.status_code == 200 and ok.json()["data"]["title"] == "Renamed"
            lst = c.get("/v1/v8/boards").json()["data"]["boards"]
            assert [x["id"] for x in lst if x["id"] == "b_v8api_test"]
            al = c.get("/v1/v8/boards/b_v8api_test/alerts")
            assert al.status_code == 200, al.text
            (a,) = al.json()["data"]["alerts"]
            assert a["kind"] == "threshold" and a["status"] in ("fired", "not_fired", "not_evaluable")
            assert c.delete("/v1/v8/boards/b_v8api_test").status_code == 204
            assert c.get("/v1/v8/boards/b_v8api_test").status_code == 404
    finally:
        outer.rollback()
        conn.close()
