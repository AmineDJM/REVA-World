"""GET /v1/causal/* (read scope): envelopes, attribution wording, grades with reasons, chains as compositions,
consistency (cycles, self-loops), quarantined candidates, bounds and problem details.

Real Wikidata fixture (``reva.causal.fixture``) plus a synthetic graph (``czapi:`` ids, source
``wikidata.czapi``) for cycles, self-loops and long chains; nothing synthetic is a real-world fact.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from reva.causal import (
    CandidateIn,
    Codebook,
    DatasetClaim,
    grade_claims,
    insert_candidates,
    load_dataset_claims,
    load_kg_claims,
)
from reva.causal.fixture import load_into_kg
from reva.kg.model import KgEntity, KgStatement, load_batch
from reva.settings import Settings

pytestmark = pytest.mark.db
SRC = "wikidata.czapi"


@pytest.fixture(scope="module")
def api(db) -> Iterator[tuple[TestClient, str]]:  # type: ignore[no-untyped-def]
    from reva.api.app import create_app
    from reva.api.auth import new_api_token

    load_into_kg(db.worker, db.store)  # registers the allow-listed properties too
    raw = db.store.put_bytes(b"causal api synthetic graph v1", "raw", "application/json").sha256
    n = lambda x: f"czapi:{x}"  # noqa: E731
    sts = [
        # a cycle a -> b -> c -> a (asserted by three statements), a self-loop d -> d, a chain e -> f -> g -> h
        KgStatement(n("a"), "P1542", n("b"), statement_id="ab"),
        KgStatement(n("b"), "P1542", n("c"), statement_id="bc"),
        KgStatement(n("a"), "P828", n("c"), statement_id="ca"),
        KgStatement(n("d"), "P828", n("d"), statement_id="dd"),
        KgStatement(n("e"), "P1542", n("f"), statement_id="ef"),
        KgStatement(n("f"), "P1542", n("g"), statement_id="fg"),
        KgStatement(n("g"), "P1542", n("h"), statement_id="gh", references=[{"P248": ["Q1"]}]),
    ]
    ents = [KgEntity(n(x), "event", {"en": f"Synthetic {x}", "fr": f"Synthétique {x}"}) for x in "abcdefgh"]
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
    load_kg_claims(db.worker, origin="test_fixture", source_keys=[SRC, "wikidata.causal_fixture"])
    raw2 = db.store.put_bytes(b"causal api synthetic dataset v1", "raw", "text/csv").sha256
    load_dataset_claims(
        db.worker,
        db.store,
        source_key="czapi.data",
        dataset="crises",
        dataset_revision="1",
        raw_sha256s=[raw2],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
        codebook=Codebook("Synthetic codebook", "1", None, {"trigger": "Verbatim trigger definition."}),
        claims=[
            DatasetClaim(
                n("e"),
                n("h"),
                "coded_trigger",
                {"row": 7},
                codebook_term="trigger",
                mechanism="Quoted mechanism text.",
                source_claim_id="7",
            )
        ],
    )
    insert_candidates(
        db.worker,
        db.store,
        extractor_id="causcand.test",
        extractor_version="1",
        source_key="wikipedia.en",
        raw_sha256s=[raw2],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
        candidates=[
            CandidateIn(n("h"), n("e"), "has_cause", "T5", "42", "H was caused by E, per the article.", {"s": 1})
        ],
    )
    grade_claims(db.worker, origin="test_fixture")
    tok, sha = new_api_token()
    with db.owner.begin() as c:
        c.execute(
            text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'causal-read')"),
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


def _codes(body: dict) -> set[str]:
    return {w["code"] for w in body["warnings"]}


def test_event_summary(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/causal/events/Q361")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["event"]["label"] == "World War I" and d["event"]["label_fr"] == "Première Guerre mondiale"
    assert d["dates"]["start"]["from"] == "1914-07-28" and d["dates"]["start"]["basis"] == "P580"
    assert sum(d["effects"]["cause"].values()) >= 10 and d["effects"]["cause"].get("E", 0) >= 1
    assert r.json()["origin"] == "test_fixture" and "claims_not_causation" in _codes(r.json())


def test_causes_effects_wording_and_grades(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/causal/events/Q8698/causes")
    assert r.status_code == 200, r.text
    body = r.json()
    claims = body["data"]["claims"]
    crash = [c for c in claims if c["cause"]["id"] == "Q201684"]
    assert len(crash) == 2 and all(c["grade"]["grade"] == "D" for c in crash)
    assert all(c["wording"].startswith("According to Wikidata") for c in claims)
    assert {"claims_not_causation", "community_edited", "fixture_data"} <= _codes(body)
    reasons = crash[0]["grade"]["reasons"]
    assert reasons[-1]["rule_id"] == "grade.D.t4" and reasons[-1]["fr"].startswith("Déclaration Wikidata sans")
    # filters: min_grade C hides D claims; relation filter
    assert get(api, "/v1/causal/events/Q8698/causes", min_grade="C").json()["data"]["claims"] == []
    eff = get(api, "/v1/causal/events/czapi:e/effects", relation="coded_trigger").json()["data"]["claims"]
    assert [c["source"]["tier"] for c in eff] == ["T1"]
    assert eff[0]["mechanism"] == "Quoted mechanism text."
    assert eff[0]["source"]["codebook"]["definition"] == "Verbatim trigger definition."
    assert eff[0]["grade"]["grade"] == "B"  # T1, time undetermined (no dates) -> not A


def test_why_lanes_mechanisms(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/causal/events/czapi:h/why")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert [lane["lane"] for lane in d["lanes"]] == ["general", "coded"]
    assert d["mechanisms"] == [
        {"claim_key": "ds:czapi.data:crises:7", "mechanism": "Quoted mechanism text.", "source_key": "czapi.data"}
    ]
    assert d["summary_fr"].startswith("2 affirmation(s) causale(s)")


def test_chain_is_a_bounded_composition(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/causal/chain", **{"from": "czapi:e", "to": "czapi:h", "max_hops": 6})
    assert r.status_code == 200, r.text
    body = r.json()
    d = body["data"]
    assert d["found"] and "chain_is_composition" in _codes(body)
    # the dataset claim e -> h is one hop: the minimal chain
    assert [p["length"] for p in d["paths"]] == [1] and d["paths"][0]["chain_grade"] == "B"
    assert d["note"].startswith("composition by REVA of separately asserted claims; chain grade = weakest hop")
    # restricted to Wikidata-graded hops of at least D the 3-hop chain is found, weakest hop D
    r = get(api, "/v1/causal/chain", **{"from": "czapi:f", "to": "czapi:h"})
    p = r.json()["data"]["paths"][0]
    assert p["length"] == 2 and [h["hop_grade"] for h in p["hops"]] == ["D", "C"] and p["chain_grade"] == "D"
    assert p["weakest_hop"] == 0
    # min_grade C: the D hop is not traversed and the search says so
    r = get(api, "/v1/causal/chain", **{"from": "czapi:f", "to": "czapi:h", "min_grade": "C"})
    d = r.json()["data"]
    assert not d["found"] and d["search"]["below_min_grade_skipped"] >= 1
    assert "grade filter may hide" in d["not_found_reason"]
    # direction matters: an effect is not a cause of its cause
    assert not get(api, "/v1/causal/chain", **{"from": "czapi:h", "to": "czapi:e"}).json()["data"]["found"]
    # bounds
    assert get(api, "/v1/causal/chain", **{"from": "czapi:e", "to": "czapi:h", "max_hops": 7}).status_code == 422
    assert get(api, "/v1/causal/chain", **{"from": "czapi:e", "to": "czapi:e"}).status_code == 422
    assert get(api, "/v1/causal/chain", **{"from": "czapi:e", "to": "czapi:zzz"}).status_code == 404


def test_consistency_reports_cycles_and_self_loops(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/causal/consistency", limit=200)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    loops = {(c["cause"]["id"], c["effect"]["id"]) for c in d["self_loops"]["sample"]}
    assert ("czapi:d", "czapi:d") in loops
    cyc = [c for c in d["cycles"]["sample"] if {m["id"] for m in c["members"]} == {"czapi:a", "czapi:b", "czapi:c"}]
    assert len(cyc) == 1
    ex = [m["id"] for m in cyc[0]["example_cycle"]]
    assert ex[0] == ex[-1] and len(ex) == 4
    assert d["temporal_violations"]["count"] >= 4  # the real WWI -> empires claims


def test_candidates_are_separate_and_labelled(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/causal/candidates", event="czapi:h")
    assert r.status_code == 200, r.text
    body = r.json()
    c = body["data"]["candidates"]
    assert len(c) == 1 and c[0]["tier"] == "T5" and c[0]["promotion_status"] == "not_promoted"
    assert c[0]["source_sentence"] == "H was caused by E, per the article."
    assert "non sourcé" in c[0]["label"] and "unsourced_candidates" in _codes(body)
    # never among claims
    causes = get(api, "/v1/causal/events/czapi:h/causes").json()["data"]["claims"]
    assert all(x["claim_key"].startswith(("kg:", "ds:")) for x in causes)
    assert get(api, "/v1/causal/events/czapi:h").json()["data"]["candidates"] == 1


def test_problems_and_auth(api) -> None:  # type: ignore[no-untyped-def]
    r = get(api, "/v1/causal/events/Q999999999/why")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")
    assert get(api, "/v1/causal/events/not-an-id").status_code == 422
    assert get(api, "/v1/causal/events/Q361/causes", min_grade="F").status_code == 422
    assert get(api, "/v1/causal/events/Q361/causes", limit=501).status_code == 422
    assert get(api, "/v1/causal/consistency", origin="other").status_code == 422
    client, _ = api
    assert client.get("/v1/causal/events/Q361").status_code == 401
