"""GET /v1/kg/* (read scope): envelopes, provenance, honesty wording, bounds and problem details.

Synthetic graph (``syn:`` ids, source ``kgapitest``); nothing here is a real-world fact.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from reva.kg.model import KgEntity, KgProperty, KgStatement, KgTime, load_batch, register_properties
from reva.settings import Settings

pytestmark = pytest.mark.db
SRC = "kgapitest"


@pytest.fixture(scope="module")
def api(db) -> Iterator[tuple[TestClient, str]]:  # type: ignore[no-untyped-def]
    from reva.api.app import create_app
    from reva.api.auth import new_api_token

    register_properties(
        db.worker,
        [
            KgProperty(
                "syn:linked", "item", "linked_to", "linked to", "lié à", "linked_from", "linked from", "lié depuis"
            ),
            KgProperty("P828", "item", "has_cause", "has cause", "a pour cause", "cause_of", "cause of"),
        ],
    )
    raw = db.store.put_bytes(b"kg api test synthetic graph v1", "raw", "application/json").sha256
    ents = [KgEntity(f"syn:api{n}", "test_thing", {"en": f"Api node {n}", "fr": f"Nœud api {n}"}) for n in "pqrs"]
    sts = [
        KgStatement("syn:apip", "syn:linked", "syn:apiq", statement_id="pq", start=KgTime("1958", 9)),
        KgStatement("syn:apiq", "syn:linked", "syn:apir", statement_id="qr", end=KgTime("1962-07-05", 11)),
        KgStatement("syn:apir", "P828", "syn:apis", statement_id="rs"),
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
    assert rep.rejected_total == 0
    # live rows next to the fixture rows (KG-3 origin filter / warning) and ISO alpha-3 statements (iso3 lookup)
    register_properties(db.worker, [KgProperty("P298", "external_id", "iso_3166_1_alpha3", "ISO 3166-1 alpha-3 code")])
    raw2 = db.store.put_bytes(b"kg api test synthetic graph v1 live part", "raw", "application/json").sha256
    iso = {"type": "external_id", "value": "ZZX"}
    rep = load_batch(
        db.worker,
        db.store,
        entities=[KgEntity(f"syn:api{n}", "test_place", {"en": f"Live node {n}"}) for n in "lmn"],
        statements=[
            KgStatement("syn:apil", "syn:linked", "syn:apip", statement_id="lp"),
            KgStatement("syn:apil", "P298", value=iso, statement_id="l298"),
            KgStatement("syn:apim", "P298", value={**iso, "value": "ZZY"}, statement_id="m298"),
            KgStatement("syn:apin", "P298", value={**iso, "value": "ZZY"}, statement_id="n298", rank="preferred"),
        ],
        source_key=SRC + "live",
        raw_sha256s=[raw2],
        retrieved_at=datetime.now(UTC),
        origin="live",
    )
    assert rep.rejected_total == 0
    tok, sha = new_api_token()
    with db.owner.begin() as c:
        c.execute(
            text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'kg-read')"),
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
    try:
        with TestClient(app) as client:
            yield client, tok
    finally:
        app.state.engine.dispose()


def get(api: tuple[TestClient, str], path: str, **params: Any) -> Any:
    client, tok = api
    return client.get(path, params=params, headers={"Authorization": f"Bearer {tok}"})


def test_entity(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/kg/entities/syn:apip")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["data"]["labels"] == {"en": "Api node p", "fr": "Nœud api p"}
    assert b["data"]["provenance"]["source_key"] == SRC and b["data"]["statements_out"] == 1
    assert b["origin"] == "test_fixture" and b["context_mode"] == "evidence" and b["knowledge_cutoff"]


def test_unknown_and_invalid_entities_are_problems(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/kg/entities/syn:nope")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "not_found"
    r = get(api, "/v1/kg/entities/FRA")
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    r = get(api, "/v1/kg/entities/syn:apip/neighbours", valid_from="1970", valid_to="1960")
    assert r.status_code == 422 and r.headers["content-type"].startswith("application/problem+json")


def test_requires_authentication(api) -> None:  # type: ignore[no-untyped-def]
    client, _ = api
    r = client.get("/v1/kg/search", params={"q": "api"})
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"


def test_neighbours_and_pagination(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/kg/entities/syn:apiq/neighbours", limit=1)
    assert r.status_code == 200, r.text
    b = r.json()
    assert len(b["data"]["edges"]) == 1 and b["next_cursor"]
    r2 = get(api, "/v1/kg/entities/syn:apiq/neighbours", limit=1, cursor=b["next_cursor"])
    edges = b["data"]["edges"] + r2.json()["data"]["edges"]
    assert {e["direction"] for e in edges} == {"in", "out"} and r2.json()["next_cursor"] is None
    e = edges[0]["provenance"]
    assert e["raw_sha256"] and e["statement_key"].startswith(SRC + ":")
    r = get(api, "/v1/kg/entities/syn:apiq/neighbours", direction="out", valid_from="1963")
    assert r.json()["data"]["edges"] == []  # the q->r statement ended in 1962


def test_timeline(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/kg/entities/syn:apiq/timeline")
    assert r.status_code == 200, r.text
    ev = r.json()["data"]["events"]
    assert [e["provenance"]["statement_key"] for e in ev] == [f"{SRC}:pq", f"{SRC}:qr"]
    assert ev[0]["start"] == {"time": "1958", "precision": 9}


def test_search(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/kg/search", q="nœud api", lang=["fr"])
    assert r.status_code == 200, r.text
    assert {h["qid"] for h in r.json()["data"]["results"]} == {f"syn:api{n}" for n in "pqrs"}
    assert get(api, "/v1/kg/search", q="").status_code == 422


def test_path_says_connected_via_and_attributes_causal_claims(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/kg/path", **{"from": "syn:apip", "to": "syn:apis"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["warnings"][0]["code"] == "path_not_causal"
    d = b["data"]
    assert d["found"] and d["from"] == "syn:apip" and d["paths"][0]["length"] == 3
    hops = d["paths"][0]["hops"]
    assert [h["from"]["qid"] for h in hops] == ["syn:apip", "syn:apiq", "syn:apir"]
    assert "connected via 'linked to'" in hops[0]["wording"] and hops[0]["causal_assertion"] is None
    assert hops[2]["causal_assertion"]["asserted_by"] == SRC and f"asserted by {SRC}" in hops[2]["wording"]
    assert all(h["provenance"]["raw_sha256"] for h in hops)
    # time window: the second hop ended in 1962
    r = get(
        api, "/v1/kg/path", **{"from": "syn:apip", "to": "syn:apis", "valid_from": "1970", "include_undated": "false"}
    )
    assert r.status_code == 200 and not r.json()["data"]["found"]


@pytest.mark.parametrize(
    "params",
    [
        {"from": "syn:apip", "to": "syn:apis", "max_hops": 6},
        {"from": "syn:apip", "to": "syn:apip"},
        {"from": "syn:apip", "to": "not a qid"},
        {"from": "syn:apip", "to": "syn:apis", "property": "nope!"},
        {"from": "syn:apip", "to": "syn:apis", "valid_from": "19x0"},
    ],
)
def test_path_bounds_and_validation(api, params: dict) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/kg/path", **params)
    assert r.status_code == 422, r.text
    assert r.headers["content-type"].startswith("application/problem+json")


def test_path_unknown_endpoint_is_404(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/kg/path", **{"from": "syn:apip", "to": "syn:unknown"})
    assert r.status_code == 404


# ------------------------------------------------------------------------------- review V3 fixes (KG-3, KG-4, SEC-1)
def _codes(b: dict[str, Any]) -> list[str]:
    return [w["code"] for w in b["warnings"]]


def test_fixture_rows_are_warned_and_origin_filters(api) -> None:  # type: ignore[no-untyped-def]
    """KG-3: fixture rows served (alone or with live ones) carry the fixture_data warning; origin filters."""
    b = get(api, "/v1/kg/entities/syn:apip/neighbours").json()
    assert b["origin"] == "mixed" and "fixture_data" in _codes(b)  # p->q (fixture) and l->p (live)
    b = get(api, "/v1/kg/entities/syn:apip/neighbours", origin="live").json()
    assert b["origin"] == "live" and "fixture_data" not in _codes(b)
    assert [e["provenance"]["origin"] for e in b["data"]["edges"]] == ["live"]
    b = get(api, "/v1/kg/entities/syn:apip/neighbours", origin="test_fixture").json()
    assert {e["provenance"]["origin"] for e in b["data"]["edges"]} == {"test_fixture"}
    b = get(api, "/v1/kg/search", q="node", lang=["en"]).json()
    assert b["origin"] == "mixed" and "fixture_data" in _codes(b)
    b = get(api, "/v1/kg/search", q="node", lang=["en"], origin="live").json()
    assert {h["qid"] for h in b["data"]["results"]} == {"syn:apil", "syn:apim", "syn:apin"}
    assert get(api, "/v1/kg/search", q="node", origin="other").status_code == 422


def test_default_origin_is_live_in_production() -> None:
    from types import SimpleNamespace as NS

    from reva.api.routes.kg import _origin

    def req(env: str) -> Any:
        return NS(app=NS(state=NS(settings=NS(reva_env=env))))

    assert _origin(req("production"), None) == "live" and _origin(req("test"), None) is None
    assert _origin(req("production"), "any") is None and _origin(req("test"), "test_fixture") == "test_fixture"


def test_refs_carry_the_entity_kind(api) -> None:  # type: ignore[no-untyped-def]
    edges = get(api, "/v1/kg/entities/syn:apiq/neighbours").json()["data"]["edges"]
    assert {(e["subject"]["kind"], e["object"]["kind"]) for e in edges} == {("test_thing", "test_thing")}
    hops = get(api, "/v1/kg/path", **{"from": "syn:apip", "to": "syn:apis"}).json()["data"]["paths"][0]["hops"]
    assert {h["from"]["kind"] for h in hops} == {"test_thing"}


def test_search_truncation_is_flagged(api) -> None:  # type: ignore[no-untyped-def]
    """KG-4: ranking is per entity; more matching entities than the limit sets truncated."""
    b = get(api, "/v1/kg/search", q="nœud api", lang=["fr"], limit=2).json()
    assert len(b["data"]["results"]) == 2 and b["data"]["truncated"] is True and "truncated" in _codes(b)
    b = get(api, "/v1/kg/search", q="nœud api", lang=["fr"], limit=4).json()
    assert b["data"]["truncated"] is False


def test_iso3_lookup(api) -> None:  # type: ignore[no-untyped-def]
    b = get(api, "/v1/kg/search", iso3="ZZX").json()
    assert b["data"]["mode"] == "iso3" and [h["qid"] for h in b["data"]["results"]] == ["syn:apil"]
    assert b["data"]["results"][0]["match"] == "iso3_statement" and b["data"]["ambiguous"] is False
    b = get(api, "/v1/kg/search", iso3="ZZY").json()
    assert [h["qid"] for h in b["data"]["results"]] == ["syn:apin", "syn:apim"]  # preferred rank first
    assert b["data"]["ambiguous"] is True and "ambiguous" in _codes(b)
    assert get(api, "/v1/kg/search", iso3="ZZZ").json()["data"]["results"] == []
    assert get(api, "/v1/kg/search", iso3="zzx").status_code == 422
    assert get(api, "/v1/kg/search", iso3="ZZX", q="x").status_code == 422
    assert get(api, "/v1/kg/search").status_code == 422


def test_iso3_lookup_prefers_the_reviewed_registry(api, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import json

    import reva.kg.query as kq

    f = tmp_path / "country_qids.json"
    f.write_text(json.dumps({"countries": {"ZZY": {"qid": "Q90000077"}}}))
    monkeypatch.setattr(kq, "COUNTRY_QIDS", f)
    monkeypatch.setattr(kq.country_qid_registry, "__defaults__", (f,))
    b = get(api, "/v1/kg/search", iso3="ZZY").json()
    assert [h["qid"] for h in b["data"]["results"]] == ["Q90000077"]
    assert b["data"]["results"][0]["match"] == "iso3_registry" and b["data"]["ambiguous"] is False


def test_path_hub_limit_is_capped(api) -> None:  # type: ignore[no-untyped-def]
    """SEC-1: the caller-chosen hub limit is bounded (each candidate node counts up to hub_limit + 1 rows)."""
    q = {"from": "syn:apip", "to": "syn:apis"}
    assert get(api, "/v1/kg/path", **q, hub_limit=5_000).status_code == 200
    assert get(api, "/v1/kg/path", **q, hub_limit=5_001).status_code == 422
