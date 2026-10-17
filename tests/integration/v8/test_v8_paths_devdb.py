"""V8 Liens against the dev database `reva` (read-only; skipped when it is not reachable).

A partial index (ChEMBL / HGNC / UniProt / MONDO properties + every causal claim and effect estimate) is built from
the live rows inside a READ ONLY transaction, then the HTTP routes are exercised with it (auth overridden: the
routes, not the auth layer, are under test). Checks real facts only as the sources state them: the CFTR gene, the
cystic fibrosis claim (MONDO), the ivacaftor indication and target (ChEMBL).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db

SUBSET = ("chembl:", "hgnc:", "uniprot:", "mondo:")


@pytest.fixture(scope="module")
def ctx() -> Iterator[dict[str, Any]]:
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings
    from reva.v8.paths import index as ix

    s = get_settings()
    try:
        eng = make_engine("api", s)
        with eng.connect() as c:
            n = c.execute(text("SELECT count(*) FROM kg.statement WHERE subject_qid = 'hgnc:1884'")).scalar_one()
    except Exception:
        pytest.skip("dev database not reachable")
    if not n:
        pytest.skip("CFTR (hgnc:1884) not loaded in this database")
    reg = ix.load_registries()
    reg.edge_family = {p: f for p, f in reg.edge_family.items() if p.startswith(SUBSET)}
    with eng.connect() as c, c.begin():
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        c.exec_driver_sql("SET LOCAL statement_timeout = '300s'")
        idx = ix.build_index(
            ix.read_statements(c, sorted(reg.edge_family)),
            ix.read_claims(c),
            ix.read_estimates(c),
            reg,
            meta={"data_key": "test"},
        )
    yield {"engine": eng, "idx": idx, "settings": s}
    eng.dispose()


def test_cftr_to_ivacaftor_prefers_the_graded_claim_chain(ctx: dict[str, Any]) -> None:
    from reva.v8.paths import index as ix
    from reva.v8.paths import render as rd
    from reva.v8.paths import search as se

    idx = ctx["idx"]
    a, b = idx.idx("hgnc:1884"), idx.idx("chembl:CHEMBL2010601")
    assert a is not None and b is not None
    flt = se.EdgeFilter(include_causal=True)
    paths, st = se.find_paths(idx, a, b, max_hops=4, k=5, flt=flt)
    assert paths and not st.truncated
    kinds = [ix.EDGE_KINDS[idx.e_kind[h.edge]] for h in paths[0].hops]
    assert kinds[0] == "causal_claim"  # CFTR -> cystic fibrosis, a claim of its source
    without, _ = se.find_paths(idx, a, b, max_hops=4, k=5)
    assert without and all(ix.EDGE_KINDS[idx.e_kind[h.edge]] == "statement" for p in without for h in p.hops)
    with ctx["engine"].connect() as c, c.begin():
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        nodes = rd.node_refs(c, idx, {idx.node_ids[n] for p in paths for n in p.nodes})
        det = rd.HopDetails(c, idx, [h.edge for p in paths for h in p.hops])
        out = rd.render_path(idx, paths[0], 1, nodes, det, flt)
    claim = out["hops"][0]
    assert claim["strength"]["grade"] in ("A", "B", "C", "D", "E")
    assert claim["strength"]["tier"] in ("T1", "T2", "T3", "T4")
    assert claim["source"]["key"]
    assert "→" in claim["sentence"]["fr"]
    st_hops = [h for h in out["hops"] if h["kind"] == "statement"]
    assert st_hops and all(h["source"] and h["family"] for h in st_hops)
    assert all(h["licence"] is None or h["licence"]["licence"] for h in out["hops"])  # null = unknown, never invented


@pytest.fixture(scope="module")
def client(ctx: dict[str, Any]) -> Iterator[Any]:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from reva.api.errors import install_error_handlers
    from reva.api.routes import v8_paths
    from reva.v8.paths.service import HOLDER

    app = FastAPI()
    install_error_handlers(app)
    app.state.engine = lambda: ctx["engine"]
    app.state.settings = ctx["settings"]
    app.include_router(v8_paths.router)
    app.dependency_overrides[v8_paths.router.dependencies[0].dependency] = lambda: None
    HOLDER.set(ctx["idx"])
    try:
        yield TestClient(app)
    finally:
        HOLDER.reset()


def test_paths_route(client: Any) -> None:
    r = client.get("/v1/v8/paths", params={"from": "hgnc:1884", "to": "chembl:CHEMBL2010601", "include_causal": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    d = body["data"]
    assert d["found"] and d["paths"][0]["hops"][0]["kind"] == "causal_claim"
    assert d["from"]["label"] == "CFTR"
    assert {w["code"] for w in body["warnings"]} >= {"path_not_causal", "claims_not_causation"}
    assert "pas une preuve causale" in d["note"]["fr"]
    r2 = client.get(
        "/v1/v8/paths", params={"from": "hgnc:1884", "to": "chembl:CHEMBL2010601", "families": "located_in"}
    )
    assert r2.status_code == 200 and not r2.json()["data"]["found"] and r2.json()["data"]["not_found_reason"]
    assert client.get("/v1/v8/paths", params={"from": "hgnc:1884", "to": "hgnc:1884"}).status_code == 422
    assert client.get("/v1/v8/paths", params={"from": "hgnc:1884", "to": "syn:nothing"}).status_code == 404
    assert (
        client.get(
            "/v1/v8/paths", params={"from": "hgnc:1884", "to": "chembl:CHEMBL2010601", "families": "causes"}
        ).status_code
        == 422
    )


def test_neighbourhood_and_nodes_routes(client: Any) -> None:
    r = client.get("/v1/v8/neighbourhood", params={"id": "hgnc:1884", "limit": 8})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["center"]["id"] == "hgnc:1884" and 0 < len(d["edges"]) <= 8 and d["groups"]
    assert all(e["hop"]["from"] == "hgnc:1884" for e in d["edges"])
    r = client.get("/v1/v8/paths/nodes", params={"q": "CFTR"})
    assert r.status_code == 200
    hits = r.json()["data"]["hits"]
    assert any(h["node"]["id"] == "hgnc:1884" and h["indexed"] for h in hits)
    s = client.get("/v1/v8/paths/status").json()["data"]
    assert s["ready"] and s["index"]["statements"] > 0
