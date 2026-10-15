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
