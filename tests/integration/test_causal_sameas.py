"""Event identity across sources (agent SAMEAS, docs/dev/v3_sameas.md): evidence-based ``events:same_as`` links
(provider crosswalk rule), corroboration of the same link coded by two independent datasets under their own record
ids (grade A), lineage of derived records (counted once), and chains crossing an identity hop shown with its
evidence. Synthetic ids (``sxt:``) and source keys; nothing here is a real-world fact."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from reva.causal import Codebook, DatasetClaim, grade_claims, load_dataset_claims
from reva.events import same_as as sa
from reva.kg.model import KgEntity, KgProperty, KgStatement, load_batch, register_properties
from reva.settings import Settings

pytestmark = pytest.mark.db
ORIGIN = "test_fixture"

RULES = {
    "rules": [
        {
            "id": "sxt_agreement_id",
            "kind": "prefix_vs_property",
            "id_prefix": "sxt:pa:",
            "property": "sxt:agreement_id",
            "status": "active",
            "one_value_per_holder": True,
            "independent_sources": True,
        },
        {
            "id": "sxt_derived_case",
            "kind": "statement_link",
            "property": "sxt:same_case_as",
            "subject_prefix": "sxt:dcase:",
            "object_prefix": "sxt:case:",
            "status": "active",
            "independent_sources": False,
        },
    ]
}


def _date(q: str, pid: str, year: int, sid: str) -> KgStatement:
    return KgStatement(
        q, pid, value={"type": "time", "time": f"+{year}-00-00T00:00:00Z", "precision": 9}, statement_id=sid
    )


def _claims(db: Any, source_key: str, claims: list[DatasetClaim], tag: bytes) -> None:
    raw = db.store.put_bytes(b"sxt dataset " + tag, "raw", "text/csv").sha256
    load_dataset_claims(
        db.worker,
        db.store,
        source_key=source_key,
        dataset="d",
        dataset_revision="1",
        raw_sha256s=[raw],
        retrieved_at=datetime.now(UTC),
        origin=ORIGIN,
        codebook=Codebook("Synthetic codebook", "1", None, {"t": "Verbatim definition."}),
        claims=claims,
    )


@pytest.fixture(scope="module")
def setup(db) -> Iterator[tuple[TestClient, str, Any]]:  # type: ignore[no-untyped-def]
    from reva.api.app import create_app
    from reva.api.auth import new_api_token

    register_properties(
        db.worker,
        [
            KgProperty("P580", "time", "start_time", "start time"),
            KgProperty("P585", "time", "point_in_time", "point in time"),
            KgProperty("sxt:agreement_id", "string", "sxt_agreement_id", "synthetic agreement id"),
            KgProperty("sxt:same_case_as", "item", "sxt_same_case_as", "synthetic same case as"),
        ],
    )
    ids = ["pre", "conflict", "pa:1", "agt:9", "end", "case:1", "dcase:1", "outcome", "agt:7", "agt:8", "pa:2", "pa:3"]
    ents = [KgEntity(f"sxt:{x}", "event", {"en": f"Synthetic {x}"}) for x in ids]
    raw = db.store.put_bytes(b"sxt kg v1", "raw", "application/json").sha256
    sts = [
        _date("sxt:conflict", "P580", 1990, "d1"),
        _date("sxt:pre", "P580", 1985, "d0"),
        _date("sxt:pa:1", "P585", 1995, "d2"),
        _date("sxt:agt:9", "P585", 1995, "d3"),
        _date("sxt:end", "P585", 1999, "d4"),
        _date("sxt:case:1", "P580", 1980, "d5"),
        _date("sxt:outcome", "P585", 1982, "d6"),
        # the provider crosswalk: agreement 9 of source B is agreement 1 of source A
        KgStatement("sxt:agt:9", "sxt:agreement_id", value={"type": "string", "value": "1"}, statement_id="x1"),
        # a record citing two records of the other dataset: ambiguous, never linked
        KgStatement("sxt:agt:7", "sxt:agreement_id", value={"type": "string", "value": "2"}, statement_id="x2"),
        KgStatement("sxt:agt:7", "sxt:agreement_id", value={"type": "string", "value": "3"}, statement_id="x3"),
        # a derived record (provider says it took the case over)
        KgStatement("sxt:dcase:1", "sxt:same_case_as", "sxt:case:1", statement_id="x4"),
    ]
    rep = load_batch(
        db.worker,
        db.store,
        entities=ents,
        statements=sts,
        source_key="sxt.kg",
        raw_sha256s=[raw],
        retrieved_at=datetime.now(UTC),
        origin=ORIGIN,
    )
    assert rep.rejected_total == 0, rep.rejected[:5]
    with db.worker.connect() as conn:
        report = sa.derive_links(conn, RULES, origin=ORIGIN)
    sa.load_links(db.worker, db.store, report, origin=ORIGIN, retract_missing=False)
    c = "t"
    _claims(
        db,
        "sxta.data",
        [
            DatasetClaim("sxt:conflict", "sxt:pa:1", "reaction_to", {"r": 1}, codebook_term=c, source_claim_id="a1"),
            DatasetClaim("sxt:pre", "sxt:pa:1", "has_effect", {"r": 2}, codebook_term=c, source_claim_id="a2"),
        ],
        b"a",
    )
    _claims(
        db,
        "sxtb.data",
        [
            DatasetClaim("sxt:conflict", "sxt:agt:9", "reaction_to", {"r": 1}, codebook_term=c, source_claim_id="b1"),
            DatasetClaim("sxt:agt:9", "sxt:end", "has_effect", {"r": 2}, codebook_term=c, source_claim_id="b2"),
        ],
        b"b",
    )
    _claims(
        db,
        "sxtc.data",
        [DatasetClaim("sxt:case:1", "sxt:outcome", "coded_outcome", {"r": 1}, codebook_term=c, source_claim_id="c1")],
        b"c",
    )
    _claims(
        db,
        "sxtd.data",
        [DatasetClaim("sxt:dcase:1", "sxt:outcome", "coded_outcome", {"r": 1}, codebook_term=c, source_claim_id="d1")],
        b"d",
    )
    grade_claims(db.worker, origin=ORIGIN)
    tok, sha = new_api_token()
    with db.owner.begin() as conn:
        conn.execute(
            text("INSERT INTO ops.api_token(token_sha256, scopes, label) VALUES (:h, :s, 'sxt-read')"),
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
            yield client, tok, report
    finally:
        app.state.engine.dispose()


def _grades(db: Any) -> dict[str, tuple[str, int, list[str]]]:
    with db.owner.connect() as conn:
        rows = conn.execute(
            text("""SELECT c.source_key || ':' || c.cause_id || '->' || c.effect_id, g.grade, g.corroboration, g.rule_ids
                      FROM kg.causal_claim_current c JOIN kg.causal_grade_current g ON g.claim_row_id = c.id
                     WHERE c.origin = :o AND c.cause_id LIKE 'sxt:%'"""),
            {"o": ORIGIN},
        ).all()
    return {k: (g, n, list(r)) for k, g, n, r in rows}


def test_links_from_evidence_only(setup, db) -> None:  # type: ignore[no-untyped-def]
    _, _, report = setup
    got = {(x.rule, x.left, x.right) for x in report.links}
    assert ("sxt_agreement_id", "sxt:agt:9", "sxt:pa:1") in got
    assert ("sxt_derived_case", "sxt:dcase:1", "sxt:case:1") in got
    assert not any("sxt:agt:7" in (x.left, x.right) for x in report.links)  # cites two records: ambiguous
    amb = [a for a in report.ambiguous if a.get("reason") == "holder_cites_several_records"]
    assert {a["value"] for a in amb} == {"2", "3"}
    ev = next(x for x in report.links if x.rule == "sxt_derived_case").evidence
    assert ev["type"] == "provider_identity_statement" and ev["independent_sources"] is False


def test_independent_sources_corroborate_derived_count_once(setup, db) -> None:  # type: ignore[no-untyped-def]
    g = _grades(db)
    a, b = g["sxta.data:sxt:conflict->sxt:pa:1"], g["sxtb.data:sxt:conflict->sxt:agt:9"]
    assert a[0] == b[0] == "A" and a[1] == b[1] == 2
    assert "corroboration.via_same_as" in a[2] and "grade.A.expert_consistent_corroborated" in a[2]
    # the derived record's claim and its origin's claim share a lineage upstream: one group, B
    c, d = g["sxtc.data:sxt:case:1->sxt:outcome"], g["sxtd.data:sxt:dcase:1->sxt:outcome"]
    assert c[0] == d[0] == "B" and c[1] == d[1] == 1 and "corroboration.via_same_as" in c[2]


def test_chain_crosses_identity_hop_with_evidence(setup) -> None:  # type: ignore[no-untyped-def]
    client, tok, _ = setup
    r = client.get(
        "/v1/causal/chain",
        params={"from": "sxt:pre", "to": "sxt:end", "origin": "test_fixture"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["found"], d
    p = d["paths"][0]
    kinds = [h["kind"] for h in p["hops"]]
    assert p["length"] == 2 and p["identity_hops"] == 1 and p["cross_source"] is True
    assert kinds == ["claim", "identity", "claim"]
    idh = p["hops"][1]
    assert {idh["from"]["id"], idh["to"]["id"]} == {"sxt:pa:1", "sxt:agt:9"}
    assert idh["same_as"][0]["rule"] == "sxt_agreement_id" and idh["same_as"][0]["evidence"]["value"] == "1"
    assert idh["hop_grade"] is None and idh["claims"] == []
    assert "identity link" in p["wording"] and p["wording"].startswith("Composition by REVA")
    # two records of one event are not a chain
    r = client.get(
        "/v1/causal/chain",
        params={"from": "sxt:pa:1", "to": "sxt:agt:9", "origin": "test_fixture"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    d = r.json()["data"]
    assert not d["found"] and "same event" in d["not_found_reason"] and d["same_as"]
