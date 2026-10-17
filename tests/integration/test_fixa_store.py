"""Second-audit fixes on the database (agent FIXA, docs/dev/v3_fixa.md; audit docs/dev/review_v3_causal_audit2.md):
derived retraction reasons and corrected-reason annotations (N1, migration 0018), P1480 hedges (N2), honest
unloaded labels (N3), the source licence in claims (N9), generic (class) statements apart in "why" (M1) and chains
that never route through an entity the KG has not loaded (M2).

Synthetic ids (``fx...:``) and per-test source keys keep this module independent of other suites sharing the session
database. Nothing here is a real-world fact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from reva.causal import Codebook, DatasetClaim, grade_claims, load_dataset_claims, load_kg_claims
from reva.causal import graph as cg
from reva.causal.model import _new_load
from reva.causal.retractions import annotate_retraction_reasons
from reva.kg.model import KgEntity, KgProperty, KgStatement, load_batch, register_properties

pytestmark = pytest.mark.db

PROPS = [
    KgProperty("P828", "item", "has_cause", "has cause"),
    KgProperty("P1542", "item", "has_effect", "has effect"),
    KgProperty("P279", "item", "subclass_of", "subclass of"),
    KgProperty("fx:type", "string", "fx_type", "fixa test type"),
]


@pytest.fixture(scope="module")
def fx(db):  # type: ignore[no-untyped-def]
    register_properties(db.worker, PROPS)
    return db


def _ns() -> str:
    return "fx" + uuid.uuid4().hex[:8]


def _raw(db, tag: str) -> str:  # type: ignore[no-untyped-def]
    return db.store.put_bytes(f"fixa {tag} {uuid.uuid4()}".encode(), "raw", "application/json").sha256


def _kg(db, src: str, sts, *, raw: str | None = None, entities=(), when=None, **kw):  # type: ignore[no-untyped-def]
    rep = load_batch(
        db.worker,
        db.store,
        entities=list(entities),
        statements=sts,
        source_key=src,
        raw_sha256s=[raw or _raw(db, src)],
        retrieved_at=when or datetime.now(UTC),
        origin="test_fixture",
        **kw,
    )
    assert rep.rejected_total == 0, rep.rejected[:5]
    return rep


def _q(db, sql: str, **p: Any) -> list[Any]:  # type: ignore[no-untyped-def]
    with db.api.connect() as c:
        return [tuple(r) for r in c.execute(text(sql), p).all()]


# ---------------------------------------------------------------------------------------------- N1
def _dataset(db, src: str, raw: str, ids: list[int], when: datetime, **kw: Any):  # type: ignore[no-untyped-def]
    cb = Codebook("cb", "1", None, {"outcome": "The coded outcome (verbatim)."})
    claims = [
        DatasetClaim(
            cause_id=f"{src.split('.')[0]}:ev{i}",
            effect_id=f"{src.split('.')[0]}:ev{i}:outcome",
            relation="coded_outcome",
            selector={"row": i},
            raw_sha256=raw,
            codebook_term="outcome",
            source_claim_id=str(i),
        )
        for i in ids
    ]
    return load_dataset_claims(
        db.worker,
        db.store,
        source_key=src,
        dataset="cases",
        dataset_revision="v1",
        codebook=cb,
        raw_sha256s=[raw],
        origin="test_fixture",
        retrieved_at=when,
        claims=claims,
        **kw,
    )


def test_dataset_retraction_reasons_are_derived(fx) -> None:  # type: ignore[no-untyped-def]
    src = _ns() + ".src"
    t0 = datetime.now(UTC) - timedelta(minutes=30)
    raw = _raw(fx, "ds")
    assert _dataset(fx, src, raw, [0, 1, 2, 3], t0).inserted == 4
    # same bytes, row 3 no longer mapped: REVA's reclassification, not a removal at the source
    r = _dataset(fx, src, raw, [0, 1, 2], t0 + timedelta(minutes=1), parameters={"connector": "fx@2"})
    assert r.retraction_reasons == {"reclassified_by_reva": 1}
    # new bytes without row 2, not checked by the caller: absent from the delivery
    raw2 = _raw(fx, "ds2")
    r = _dataset(fx, src, raw2, [0, 1], t0 + timedelta(minutes=2))
    assert r.retraction_reasons == {"absent_from_delivery": 1}
    # new bytes, the caller verified row 1 is gone from the new revision: removed at the source
    raw3 = _raw(fx, "ds3")
    r = _dataset(fx, src, raw3, [0], t0 + timedelta(minutes=3), source_row_present=lambda lt: False)
    assert r.retraction_reasons == {"removed_at_source": 1}
    rows = _q(
        fx,
        """SELECT r.claim_key, r.reason, r.detail->>'basis', r.detail->>'connector' FROM kg.causal_claim_retraction r
            WHERE starts_with(r.claim_key, :p) ORDER BY r.claim_key""",
        p=f"ds:{src}:",
    )
    assert rows == [
        (f"ds:{src}:cases:1", "removed_at_source", "row_absent_from_new_revision", None),
        (f"ds:{src}:cases:2", "absent_from_delivery", "raw_changed_row_not_checked", None),
        (f"ds:{src}:cases:3", "reclassified_by_reva", "raw_unchanged", "fx@2"),
    ]


def test_statement_retraction_reasons_are_derived(fx) -> None:  # type: ignore[no-untyped-def]
    src = _ns() + ".kg"
    raw = _raw(fx, "kg")
    t0 = datetime.now(UTC) - timedelta(minutes=30)

    def st(i: int) -> KgStatement:
        return KgStatement(
            f"{src.split('.')[0]}:e{i}", "fx:type", value={"type": "string", "value": "x"}, statement_id=f"s{i}"
        )

    _kg(fx, src, [st(i) for i in range(3)], raw=raw, when=t0)
    _kg(fx, src, [], raw=raw, when=t0 + timedelta(minutes=1), retract_keys=[f"{src}:s0"])
    _kg(fx, src, [], when=t0 + timedelta(minutes=2), retract_keys=[f"{src}:s1"])
    _kg(fx, src, [], when=t0 + timedelta(minutes=3), retract_keys=[f"{src}:s2"], retract_reason="removed_at_source")
    rows = _q(
        fx,
        """SELECT statement_key, reason, detail->>'basis' FROM kg.statement_retraction
            WHERE starts_with(statement_key, :p) ORDER BY statement_key""",
        p=f"{src}:",
    )
    assert rows == [
        (f"{src}:s0", "reclassified_by_reva", "raw_unchanged"),
        (f"{src}:s1", "absent_from_delivery", "raw_changed_row_not_checked"),
        (f"{src}:s2", "removed_at_source", "removal_verified_by_caller"),
    ]


def test_legacy_removed_at_source_rows_get_a_corrected_reason(fx) -> None:  # type: ignore[no-untyped-def]
    """What the version-2 loaders wrote (a hard-coded removed_at_source with the same raw bytes) is annotated, never
    edited; the database refuses an annotation that does not restate its row."""
    src = _ns() + ".legacy"
    t0 = datetime.now(UTC) - timedelta(minutes=30)
    raw = _raw(fx, "legacy")
    _dataset(fx, src, raw, [0, 1], t0)
    with fx.worker.begin() as conn:  # the version-2 loader's row, written as it wrote it
        load = _new_load(
            conn,
            kind="dataset",
            source_key=src,
            dataset="cases",
            origin="test_fixture",
            raws=[raw],
            retrieved_at=t0 + timedelta(minutes=1),
            parameters={"dataset_revision": "v1"},
        )
        conn.execute(
            text("""INSERT INTO kg.causal_claim_retraction(load_id, claim_row_id, claim_key, origin, reason)
                    SELECT :l, c.id, c.claim_key, 'test_fixture', 'removed_at_source' FROM kg.causal_claim_current c
                     WHERE c.claim_key = :k"""),
            {"l": load, "k": f"ds:{src}:cases:1"},
        )
    out = annotate_retraction_reasons(fx.worker, origin="test_fixture")
    assert out["causal_claim_retraction"]["inserted"] >= 1
    rid, eff, ev = _q(
        fx,
        """SELECT v.retraction_id, v.effective_reason, a.evidence FROM kg.retraction_reason_current v
             JOIN kg.retraction_annotation a ON a.id = v.annotation_id WHERE v.record_key = :k""",
        k=f"ds:{src}:cases:1",
    )[0]
    assert eff == "reclassified_by_reva" and ev["basis"] == "raw_unchanged" and ev["raw_sha256"] == raw
    assert _q(fx, "SELECT reason FROM kg.causal_claim_retraction WHERE id = :i", i=rid) == [("removed_at_source",)]
    again = annotate_retraction_reasons(fx.worker, origin="test_fixture")
    assert again["causal_claim_retraction"]["inserted"] == 0
    with pytest.raises(DBAPIError, match="restate"), fx.worker.begin() as conn:
        conn.execute(
            text("""INSERT INTO kg.retraction_annotation(target, retraction_id, origin, recorded_reason,
                        corrected_reason, evidence, note, annotator)
                    VALUES ('causal_claim_retraction', :i, 'test_fixture', 'no_longer_causal', 'absent_from_delivery',
                            '{}'::jsonb, 'wrong', 'test')"""),
            {"i": rid},
        )
    with pytest.raises(DBAPIError), fx.worker.begin() as conn:
        conn.execute(text("UPDATE kg.retraction_annotation SET note = 'x'"))


# ---------------------------------------------------------------------------------------------- N2 / N3 / M1 / M2
def test_hedged_claims_are_capped_worded_and_not_chained(fx) -> None:  # type: ignore[no-untyped-def]
    ns = _ns()
    src = f"wikidata.{ns}"
    a, b, c = f"{ns}:a", f"{ns}:b", f"{ns}:c"
    ref = [{"P854": ["https://example.org/report"]}]
    _kg(
        fx,
        src,
        [
            KgStatement(b, "P828", a, statement_id="H", references=ref, qualifiers={"P1480": ["Q18122778"]}),
            KgStatement(b, "P1542", c, statement_id="OK", references=ref, qualifiers={"P518": ["Q7569"]}),
        ],
        entities=[KgEntity(x, "event", {"en": x}) for x in (a, b, c)],
    )
    load_kg_claims(fx.worker, origin="test_fixture", source_keys=[src])
    grade_claims(fx.worker, origin="test_fixture")
    with fx.api.connect() as conn:
        h = cg.causes(conn, b, origin="test_fixture")["claims"][0]
        assert h["grade"]["grade"] == "D" and "grade.D.sourcing_hedged" in h["grade"]["rule_ids"]
        assert "Wikidata qualifie : Q18122778 (libellé non chargé)" in h["wording_fr"]
        assert h["attribution"]["sourcing_circumstances"][0]["label_loaded"] is False
        ok = cg.effects(conn, b, origin="test_fixture")["claims"][0]
        assert "applies to part: Q7569 (label not loaded)" in ok["wording"] and ok["grade"]["grade"] == "C"
        ch = cg.chain(conn, a, c, origin="test_fixture")
        assert not ch["found"] and ch["search"]["quarantined_skipped"] >= 1


def test_why_files_class_level_statements_apart(fx) -> None:  # type: ignore[no-untyped-def]
    ns = _ns()
    src = f"wikidata.{ns}"
    ev, war, cause = f"{ns}:ev", f"{ns}:war", f"{ns}:cause"
    _kg(
        fx,
        src,
        [
            KgStatement(war, "P279", f"{ns}:conflict", statement_id="cls"),
            KgStatement(ev, "P828", war, statement_id="G"),  # "event has cause war (a class)"
            KgStatement(ev, "P828", cause, statement_id="E"),
        ],
        entities=[KgEntity(x, "event", {"en": x}) for x in (ev, war, cause)],
    )
    load_kg_claims(fx.worker, origin="test_fixture", source_keys=[src])
    grade_claims(fx.worker, origin="test_fixture")
    with fx.api.connect() as conn:
        w = cg.why(conn, ev, origin="test_fixture")
    lanes = {x["lane"]: x["claims"] for x in w["lanes"]}
    assert [c["cause"]["id"] for c in lanes["generic"]] == [war]
    assert lanes["generic"][0]["generic"]["class_endpoints"] == ["cause"]
    assert all(c["cause"]["id"] != war for lane, cs in lanes.items() if lane != "generic" for c in cs)
    assert (w["causal_claims"], w["generic_claims"]) == (1, 1)
    assert "énoncé(s) générique(s)" in w["summary_fr"]


def test_chains_do_not_route_through_unloaded_entities(fx) -> None:  # type: ignore[no-untyped-def]
    ns = _ns()
    src = f"wikidata.{ns}"
    a, mid, c = f"{ns}:a", f"{ns}:mid", f"{ns}:c"
    # a -> mid (statement on a), mid -> c (statement on c): the KG knows nothing about "mid" itself
    _kg(fx, src, [KgStatement(a, "P1542", mid, statement_id="1"), KgStatement(c, "P828", mid, statement_id="2")])
    load_kg_claims(fx.worker, origin="test_fixture", source_keys=[src])
    grade_claims(fx.worker, origin="test_fixture")
    with fx.api.connect() as conn:
        assert cg.unloaded_nodes(conn, [a, mid, c]) == {mid}
        ch = cg.chain(conn, a, c, origin="test_fixture")
        assert not ch["found"] and ch["search"]["unloaded_nodes_sample"] == [mid]
        assert "not loaded in the knowledge graph" in ch["not_found_reason"]
        assert cg.chain(conn, a, mid, origin="test_fixture")["found"]  # an endpoint asked for is kept
    _kg(fx, src + "e", [], entities=[KgEntity(mid, "event", {"en": "middle"})])
    with fx.api.connect() as conn:
        ch = cg.chain(conn, a, c, origin="test_fixture")
    assert ch["found"] and ch["paths"][0]["hops"][0]["to"]["label"] == "middle"


# ---------------------------------------------------------------------------------------------- N9
def test_claims_carry_the_licence_of_their_source(fx) -> None:  # type: ignore[no-untyped-def]
    """idmc.gidd is registered (registry/sources/idmc_gidd.yaml, seeded) under CC BY-NC: restricted."""
    raw = _raw(fx, "idmc")
    ns = _ns()
    cb = Codebook("IDMC GIDD README", "1", None, {"d": "Internal displacements (verbatim, test)."})
    claim = DatasetClaim(
        cause_id=f"idmc:event:{ns}",
        effect_id=f"idmc:displacement:{ns}",
        relation="has_effect",
        selector={"row": 1},
        raw_sha256=raw,
        codebook_term="d",
        source_claim_id=ns,
    )
    r = load_dataset_claims(
        fx.worker,
        fx.store,
        source_key="idmc.gidd",
        dataset=f"fixa_{ns}",
        dataset_revision="t",
        codebook=cb,
        raw_sha256s=[raw],
        origin="test_fixture",
        retrieved_at=datetime.now(UTC),
        claims=[claim],
    )
    assert r.inserted == 1
    with fx.api.connect() as conn:
        c = cg.effects(conn, f"idmc:event:{ns}", origin="test_fixture")["claims"][0]
    rights = c["source"]["rights"]
    assert rights["basis"] == "source_registry" and rights["non_commercial"] and rights["restricted"]
    assert "NC" in rights["licence"]
    w = cg.licence_warning([c])
    assert w is not None and "idmc.gidd" in w["message"] and "non-commercial" in w["message"]
