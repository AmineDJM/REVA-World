"""LINKS against a throwaway database: the real-Wikidata fixture loaded into ``kg.*`` through KGCORE's
``load_batch`` (origin ``test_fixture``, source key ``wikidata.links_fixture``), then paths and country
links read back through ``reva.links.service`` (the ``/v1/links`` code path) and the HTTP API.

The session database is created by tests/conftest.py (``reva_test_<random>``) and dropped afterwards; the
dev database ``reva`` is never touched.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from reva.links.fixture import FIXTURE_FILE, load_fixture

pytestmark = pytest.mark.db
SRC = "wikidata.links_fixture"


@pytest.fixture(scope="module")
def kg(db):  # type: ignore[no-untyped-def]
    if not FIXTURE_FILE.exists():
        pytest.skip("links fixture not captured")
    from reva.adapters.connectors.wikidata import load_allowlist
    from reva.kg.harvest import kg_properties
    from reva.kg.model import KgEntity, KgStatement, KgTime, load_batch, register_properties

    register_properties(db.worker, kg_properties(load_allowlist()))
    body = FIXTURE_FILE.read_bytes()
    raw = db.store.put_bytes(body, "raw", "application/json").sha256
    assert raw == hashlib.sha256(body).hexdigest()
    fx = load_fixture()
    t = lambda d: None if d is None else KgTime(d["time"], d["precision"], d.get("calendar", "gregorian"))  # noqa: E731
    sts = [
        KgStatement(
            subject_qid=r["subject_qid"],
            property_pid=r["property_pid"],
            object_qid=r["object_qid"],
            value=r["value"],
            start=t(r["start"]),
            end=t(r["end"]),
            point_in_time=t(r["point_in_time"]),
            qualifiers=r["qualifiers"],
            rank=r["rank"],
            references=r["references"],
            source_key=SRC,
            source_revision=r["source_revision"],
            statement_id=r["statement_id"],
            raw_sha256=raw,
        )
        for r in fx.raw_statements
    ]
    ents = [
        KgEntity(q, "fixture_entity", e["labels"], e.get("descriptions") or {}, source_revision=str(e["lastrevid"]))
        for q, e in fx.entities.items()
    ]
    rep = load_batch(
        db.worker,
        db.store,
        entities=ents,
        statements=sts,
        source_key=SRC,
        raw_sha256s=[raw],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
    )
    assert rep.rejected_total == 0, rep.rejected[:5]
    with db.api.connect() as conn:
        yield conn


def test_path_columbus_to_portuguese_from_db(kg) -> None:  # type: ignore[no-untyped-def]
    from reva.links import service

    res = service.path_between(kg, "Q7322", "Q5146", origin="test_fixture", k=3, max_hops=6)
    assert res.found, res.reason
    hop_sources = {e.source for p in res.paths for h in p.hops for e in h.edge.evidence}
    assert hop_sources == {SRC}
    assert all(e.origin == "test_fixture" for p in res.paths for h in p.hops for e in h.edge.evidence)
    assert res.paths[0].nodes[0] == "Q7322" and res.paths[0].nodes[-1] == "Q5146"
    # the same chain as the offline test, forced through Tordesillas and Brazil, read from kg.statement_current
    chain = service.path_between(kg, "Q7322", "Q5146", via=["Q180897", "BRA"], origin="test_fixture", max_hops=3)
    (path,) = chain.paths
    assert "Q180897" in path.nodes and "Q155" in path.nodes
    assert path.text["fr"][1].startswith("Isabelle la Catholique — signataire de → traité de Tordesillas (1494-07-02)")
    assert all("[wikidata.links_fixture:" in t for t in path.text["en"])


def test_country_links_from_db(kg) -> None:  # type: ignore[no-untyped-def]
    from reva.links import service

    out = service.links_for_country(kg, "BRA", origin="test_fixture")
    assert out["qid"] == "Q155"
    types = {lk["link_type"] for lk in out["links"]}
    assert "shared_official_language" in types
    for lk in out["links"]:
        assert lk["evidence"], lk
    assert out["link_types"]["bilateral_trade"]["status"] == "awaiting_source"


def test_links_http_api(kg, db) -> None:  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient
    from pydantic import SecretStr
    from sqlalchemy import text

    from reva.api.app import create_app
    from reva.api.auth import new_api_token
    from reva.settings import Settings

    tok, sha = new_api_token()
    with db.owner.begin() as c:
        c.execute(
            text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'links-read')"),
            {"h": sha, "s": ["read"]},
        )
    settings = Settings.model_validate(
        {
            **db.settings.model_dump(),
            "reva_session_secret": SecretStr("s" * 48),
            "reva_allowed_hosts": "testserver,localhost",
            "reva_cors_origins": "",
        }
    )
    app = create_app(settings)
    auth = {"Authorization": f"Bearer {tok}"}
    try:
        with TestClient(app) as client:
            assert client.get("/v1/links/BRA").status_code == 401
            r = client.get("/v1/links/path", params={"from": "BRA", "to": "PRT", "max_hops": 3}, headers=auth)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["data"]["found"] and body["origin"] == "test_fixture"
            assert body["warnings"][0]["code"] == "path_not_causal"
            assert {w["code"] for w in body["warnings"]} >= {"community_edited", "fixture_data"}
            hop = body["data"]["paths"][0]["hops"][0]
            assert hop["evidence"] and hop["text"]["fr"] and hop["text"]["en"]
            r = client.get("/v1/links/path", params={"from": "Q155", "to": "Q45", "max_hops": 9}, headers=auth)
            assert r.status_code == 422
            r = client.get("/v1/links/ZZZ", headers=auth)
            assert r.status_code == 404
            r = client.get("/v1/links/BRA", params={"link_type": "shared_official_language"}, headers=auth)
            assert r.status_code == 200, r.text
            links = r.json()["data"]["links"]
            assert links and {lk["link_type"] for lk in links} == {"shared_official_language"}
    finally:
        app.state.engine.dispose()
