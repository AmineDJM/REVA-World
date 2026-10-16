"""Causal claims storage (migration 0015_causal): KG loader, dataset loader, grader, candidates, grants and
immutability.

Synthetic ids (``cz...:``) and per-test source keys keep this module independent of other suites sharing the
session database. Nothing here is a real-world fact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from reva.causal import (
    CandidateIn,
    Codebook,
    DatasetClaim,
    grade_claims,
    insert_candidates,
    load_dataset_claims,
    load_kg_claims,
)
from reva.kernel.errors import ValidationProblem
from reva.kg.model import KgProperty, KgStatement, load_batch, register_properties, statement_key

pytestmark = pytest.mark.db

PROPS = [
    KgProperty("P828", "item", "has_cause", "has cause", "a pour cause", "cause_of", "cause of"),
    KgProperty("P1542", "item", "has_effect", "has effect", "a pour effet", "effect_of", "effect of"),
    KgProperty("P1478", "item", "has_immediate_cause", "has immediate cause"),
    KgProperty("P1536", "item", "immediate_cause_of", "immediate cause of"),
    KgProperty("P1479", "item", "has_contributing_factor", "has contributing factor"),
    KgProperty("P1537", "item", "contributing_factor_of", "contributing factor of"),
    KgProperty("P580", "time", "start_time", "start time"),
    KgProperty("P582", "time", "end_time", "end time"),
    KgProperty("P585", "time", "point_in_time", "point in time"),
    KgProperty("P571", "time", "inception", "inception"),
]


@pytest.fixture(scope="module")
def cz(db):  # type: ignore[no-untyped-def]
    register_properties(db.worker, PROPS)
    return db


def _ns() -> str:
    return "cz" + uuid.uuid4().hex[:8]


def _raw(db, tag: str) -> str:  # type: ignore[no-untyped-def]
    return db.store.put_bytes(f"causal-store-test {tag} {uuid.uuid4()}".encode(), "raw", "application/json").sha256


def _date(subject: str, pid: str, iso: str, prec: int, sid: str) -> KgStatement:
    return KgStatement(subject, pid, value={"type": "time", "time": iso, "precision": prec}, statement_id=sid)


def _kg(db, src: str, sts, *, retract=(), when=None):  # type: ignore[no-untyped-def]
    rep = load_batch(
        db.worker,
        db.store,
        statements=sts,
        source_key=src,
        raw_sha256s=[_raw(db, src)],
        retrieved_at=when or datetime.now(UTC),
        origin="test_fixture",
        retract_keys=retract,
    )
    assert rep.rejected_total == 0, rep.rejected[:5]
    return rep


def _current(db, prefix: str) -> list[dict]:  # type: ignore[no-untyped-def]
    with db.api.connect() as c:
        return [
            dict(r)
            for r in c.execute(
                text("""SELECT c.*, g.grade, g.rule_ids, g.temporal_check, g.corroboration, g.contested_by
                          FROM kg.causal_claim_current c LEFT JOIN kg.causal_grade_current g ON g.claim_row_id = c.id
                         WHERE starts_with(c.claim_key, :p) ORDER BY c.claim_key"""),
                {"p": prefix},
            ).mappings()
        ]


def test_kg_loader_normalises_direction_tiers_and_is_idempotent(cz) -> None:  # type: ignore[no-untyped-def]
    ns = _ns()
    src = f"wikidata.{ns}"
    a, b, c = f"{ns}:a", f"{ns}:b", f"{ns}:c"
    ref = [{"P854": ["https://example.org/history/a-b"], "P813": [{"time": "+2026-01-01T00:00:00Z"}]}]
    _kg(
        cz,
        src,
        [
            KgStatement(b, "P828", a, statement_id="S1", references=ref),  # b has cause a  -> a => b, T3
            KgStatement(a, "P1542", c, statement_id="S2"),  # a has effect c  -> a => c, T4
            KgStatement(c, "P1478", b, statement_id="S3", references=[{"P356": ["10.1000/xyz"]}]),  # T2 (DOI)
        ],
    )
    r1 = load_kg_claims(cz.worker, origin="test_fixture", source_keys=[src])
    assert (r1.new, r1.inserted, r1.retracted, r1.rejected_total) == (3, 3, 0, 0)
    rows = {r["statement_id"]: r for r in _current(cz, f"kg:{src}:")}
    assert (rows["S1"]["cause_id"], rows["S1"]["effect_id"], rows["S1"]["relation"]) == (a, b, "has_cause")
    assert rows["S1"]["asserted_on"] == "effect" and rows["S1"]["source_tier"] == "T3"
    assert rows["S1"]["upstream_keys"] == ["url:https://example.org/history/a-b"]
    assert (rows["S2"]["cause_id"], rows["S2"]["effect_id"], rows["S2"]["asserted_on"]) == (a, c, "cause")
    assert rows["S2"]["source_tier"] == "T4" and rows["S2"]["upstream_keys"] == ["community:wikidata"]
    assert rows["S3"]["source_tier"] == "T2" and rows["S3"]["citation"] == {"doi": ["10.1000/xyz"]}
    assert (rows["S3"]["cause_id"], rows["S3"]["relation"]) == (b, "immediate_cause")
    assert rows["S1"]["review_status"] == "unreviewed"
    with cz.api.connect() as conn:  # the claim restates its KG statement
        st = conn.execute(
            text("SELECT id, raw_sha256 FROM kg.statement_current WHERE statement_key = :k"), {"k": f"{src}:S1"}
        ).one()
        assert (rows["S1"]["statement_row_id"], rows["S1"]["raw_sha256"]) == (st[0], st[1])
    # an unchanged reload records nothing, not even a load row
    with cz.api.connect() as conn:
        n_loads = conn.execute(text("SELECT count(*) FROM kg.causal_load")).scalar_one()
    r2 = load_kg_claims(cz.worker, origin="test_fixture", source_keys=[src])
    assert (r2.inserted, r2.unchanged, r2.load_ids) == (0, 3, [])
    with cz.api.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM kg.causal_load")).scalar_one() == n_loads


def test_kg_retraction_and_reinstatement(cz) -> None:  # type: ignore[no-untyped-def]
    ns = _ns()
    src = f"wikidata.{ns}"
    st = KgStatement(f"{ns}:e", "P828", f"{ns}:f", statement_id="R1")
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    _kg(cz, src, [st], when=t0)
    load_kg_claims(cz.worker, origin="test_fixture", source_keys=[src])
    assert len(_current(cz, f"kg:{src}:")) == 1
    _kg(cz, src, [], retract=[statement_key(st, src)], when=t0 + timedelta(minutes=1))
    r = load_kg_claims(cz.worker, origin="test_fixture", source_keys=[src])
    assert r.retracted == 1 and _current(cz, f"kg:{src}:") == []
    with cz.api.connect() as conn:
        reason = conn.execute(
            text("""SELECT r.reason FROM kg.causal_claim_retraction r JOIN kg.causal_claim c ON c.id = r.claim_row_id
                     WHERE starts_with(c.claim_key, :p)"""),
            {"p": f"kg:{src}:"},
        ).scalar_one()
        assert reason == "source_statement_retracted"
    _kg(cz, src, [st], when=t0 + timedelta(minutes=2))  # the source re-asserts it (KG reinstatement)
    r = load_kg_claims(cz.worker, origin="test_fixture", source_keys=[src])
    assert r.reinstated == 1
    cur = _current(cz, f"kg:{src}:")
    assert len(cur) == 1 and cur[0]["reinstates_row_id"] is not None


def test_dataset_claims_t1_codebook_retraction_and_rejections(cz) -> None:  # type: ignore[no-untyped-def]
    ns = _ns()
    src, raw = f"{ns}.data", _raw(cz, "ds")
    cb = Codebook("Synthetic crisis codebook", "1", None, {"trigger": "The act that triggered the crisis (verbatim)."})
    t0 = datetime.now(UTC) - timedelta(minutes=30)

    def claim(i: int, **kw):  # type: ignore[no-untyped-def]
        base = dict(
            cause_id=f"{ns}:trig{i}",
            effect_id=f"{ns}:crisis{i}",
            relation="coded_trigger",
            selector={"file": "c.csv", "row": i},
            codebook_term="trigger",
            source_claim_id=f"{i}",
        )
        return DatasetClaim(**{**base, **kw})

    kw = dict(
        source_key=src, dataset="crises", dataset_revision="v1", codebook=cb, raw_sha256s=[raw], origin="test_fixture"
    )
    r = load_dataset_claims(cz.worker, cz.store, retrieved_at=t0, claims=[claim(1), claim(2)], **kw)
    assert (r.inserted, r.unknown_entities, r.rejected_total) == (2, 4, 0)
    cur = _current(cz, f"ds:{src}:crises:")
    assert [c["source_tier"] for c in cur] == ["T1", "T1"]
    assert cur[0]["codebook"]["definition"] == "The act that triggered the crisis (verbatim)."
    assert cur[0]["claim_key"] == f"ds:{src}:crises:1" and cur[0]["raw_sha256"] == raw
    # idempotent
    r = load_dataset_claims(
        cz.worker, cz.store, retrieved_at=t0 + timedelta(minutes=1), claims=[claim(1), claim(2)], **kw
    )
    assert (r.inserted, r.unchanged) == (0, 2)
    # complete delivery without row 2 retracts it; a changed row 1 is a new version
    r = load_dataset_claims(
        cz.worker, cz.store, retrieved_at=t0 + timedelta(minutes=2), claims=[claim(1, mechanism="Quoted text.")], **kw
    )
    assert (r.new_revision, r.retracted) == (1, 1)
    cur = _current(cz, f"ds:{src}:crises:")
    assert [(c["claim_key"], c["mechanism"]) for c in cur] == [(f"ds:{src}:crises:1", "Quoted text.")]
    # an older delivery is stale and changes nothing
    r = load_dataset_claims(cz.worker, cz.store, retrieved_at=t0 - timedelta(minutes=5), claims=[claim(1)], **kw)
    assert (r.inserted, r.stale) == (0, 1)
    # per-claim rejections, never silent; a partly rejected complete delivery retracts nothing
    r = load_dataset_claims(
        cz.worker,
        cz.store,
        retrieved_at=t0 + timedelta(minutes=3),
        claims=[
            claim(3, codebook_term="nope"),
            claim(4, relation="caused_by_magic"),
            claim(5, tier="T2"),
            claim(6, cause_id="not an id"),
        ],
        **kw,
    )
    assert r.rejected_counts == {
        "codebook_term_not_defined": 1,
        "unknown_relation": 1,
        "t2_requires_citation": 1,
        "invalid_cause_id": 1,
        "complete_downgraded_to_partial": 1,
    }
    assert len(_current(cz, f"ds:{src}:crises:")) == 1
    with pytest.raises(ValidationProblem):  # Wikidata is never a dataset
        load_dataset_claims(cz.worker, cz.store, **{**kw, "source_key": "wikidata.x"}, retrieved_at=t0, claims=[])


def test_grader_rules_corroboration_contest_and_recompute(cz) -> None:  # type: ignore[no-untyped-def]
    ns = _ns()
    src = f"wikidata.{ns}"
    x, y, z, w = (f"{ns}:{n}" for n in "xyzw")
    same = [{"P854": ["http://www.example.org/book/"], "P813": [{"time": "+2025-01-01T00:00:00Z"}]}]
    same2 = [{"P854": ["https://example.org/book"], "P813": [{"time": "+2026-02-02T00:00:00Z"}]}]
    other = [{"P248": ["Q999999"]}]
    _kg(
        cz,
        src,
        [
            _date(x, "P580", "+1914-07-28T00:00:00Z", 11, "dx"),
            _date(y, "P585", "+1918-00-00T00:00:00Z", 9, "dy"),
            _date(z, "P571", "+1700-00-00T00:00:00Z", 9, "dz"),
            _date(w, "P580", "-0043-03-15T00:00:00Z", 11, "dw"),
            # x => y twice with the same upstream reference (URL variants) -> one group
            KgStatement(y, "P828", x, statement_id="C1", references=same),
            KgStatement(x, "P1542", y, statement_id="C2", references=same2),
            # x => z: the cause starts after the effect -> temporal inconsistency -> E
            KgStatement(x, "P1542", z, statement_id="C3", references=other),
            # w (44 BCE) => x: consistent, unreferenced -> D
            KgStatement(w, "P1542", x, statement_id="C4"),
            # y => z twice with independent references -> corroborated T3 -> B
            KgStatement(y, "P1542", z, statement_id="C5", references=other),
            KgStatement(z, "P828", y, statement_id="C6", references=same),
        ],
    )
    load_kg_claims(cz.worker, origin="test_fixture", source_keys=[src])
    g1 = grade_claims(cz.worker, origin="test_fixture")
    assert g1.written >= 6
    rows = {r["statement_id"]: r for r in _current(cz, f"kg:{src}:")}
    assert (rows["C1"]["grade"], rows["C1"]["corroboration"], rows["C1"]["temporal_check"]) == ("C", 1, "consistent")
    assert rows["C1"]["rule_ids"] == [
        "tier.T3",
        "temporal.consistent",
        "corroboration.single",
        "contest.none",
        "grade.C.t3_single",
    ]
    assert (rows["C3"]["grade"], rows["C3"]["temporal_check"]) == ("E", "inconsistent")
    assert (rows["C4"]["grade"], rows["C4"]["temporal_check"]) == ("D", "consistent")  # BCE cause precedes
    # y (1918, year precision) => z (inception 1700): inconsistent too, even though corroborated
    assert (rows["C5"]["grade"], rows["C5"]["corroboration"]) == ("E", 2)
    # recompute with unchanged inputs writes nothing
    g2 = grade_claims(cz.worker, origin="test_fixture")
    assert g2.written == 0 and g2.unchanged >= 6
    # a denying T1 dataset claim contests x => y: the uncorroborated assertions become E; new rows only for them
    raw = _raw(cz, "deny")
    load_dataset_claims(
        cz.worker,
        cz.store,
        source_key=f"{ns}.deny",
        dataset="d",
        dataset_revision="1",
        raw_sha256s=[raw],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
        codebook=Codebook("cb", "1", None, {"no_link": "verbatim"}),
        claims=[
            DatasetClaim(x, y, "has_cause", {"row": 1}, codebook_term="no_link", stance="denies", source_claim_id="1")
        ],
    )
    g3 = grade_claims(cz.worker, origin="test_fixture")
    assert g3.written == 3  # C1, C2 and the denial itself
    rows = {r["statement_id"]: r for r in _current(cz, f"kg:{src}:")}
    assert rows["C1"]["grade"] == "E" and rows["C1"]["contested_by"] == [f"ds:{ns}.deny:d:1"]
    assert (
        "contest.contested" in rows["C1"]["rule_ids"] and "grade.E.contested_uncorroborated" in rows["C1"]["rule_ids"]
    )


def test_candidates_are_quarantined(cz) -> None:  # type: ignore[no-untyped-def]
    ns = _ns()
    raw = _raw(cz, "cand")
    kw = dict(
        extractor_id="causcand.test",
        extractor_version="1",
        source_key="wikipedia.en",
        raw_sha256s=[raw],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
    )
    c = CandidateIn(
        f"{ns}:p", f"{ns}:q", "has_cause", "T5", "123", "P was caused by Q, the article says.", {"page": "P", "s": 1}
    )
    r = insert_candidates(cz.worker, cz.store, candidates=[c, CandidateIn(**{**c.__dict__, "tier": "T3"})], **kw)
    assert (r.inserted, r.rejected_counts) == (1, {"candidate_tier_must_be_t5_or_t6": 1})
    assert insert_candidates(cz.worker, cz.store, candidates=[c], **kw).inserted == 0
    with cz.api.connect() as conn:
        row = conn.execute(
            text("SELECT promotion_status, tier FROM kg.causal_candidate_current WHERE cause_id = :c"), {"c": f"{ns}:p"}
        ).one()
        assert tuple(row) == ("not_promoted", "T5")
        assert (
            conn.execute(
                text("SELECT count(*) FROM kg.causal_claim WHERE cause_id = :c"), {"c": f"{ns}:p"}
            ).scalar_one()
            == 0
        )


def test_grants_immutability_and_load_binding(cz) -> None:  # type: ignore[no-untyped-def]
    ns = _ns()
    src = f"wikidata.{ns}"
    _kg(cz, src, [KgStatement(f"{ns}:g", "P828", f"{ns}:h", statement_id="G1")])
    load_kg_claims(cz.worker, origin="test_fixture", source_keys=[src])
    grade_claims(cz.worker, origin="test_fixture")
    claim_id = _current(cz, f"kg:{src}:")[0]["id"]
    for eng in (cz.api, cz.sim):
        with pytest.raises(DBAPIError), eng.begin() as c:
            c.execute(
                text("""INSERT INTO kg.causal_load(kind, source_key, origin, loader_version)
                        VALUES ('grading', 'x', 'test_fixture', 'x')""")
            )
    for sql in (
        "UPDATE kg.causal_claim SET mechanism = 'x' WHERE id = :i",
        "DELETE FROM kg.causal_claim WHERE id = :i",
        "UPDATE kg.causal_grade SET grade = 'A' WHERE claim_row_id = :i",
    ):
        for eng in (cz.worker, cz.owner):
            with pytest.raises(DBAPIError), eng.begin() as c:
                c.execute(text(sql), {"i": claim_id})
    with pytest.raises(DBAPIError), cz.owner.begin() as c:
        c.execute(text("TRUNCATE kg.causal_grade"))
    # agents cannot record reviews
    with pytest.raises(DBAPIError), cz.worker.begin() as c:
        c.execute(
            text("""INSERT INTO kg.causal_review(target, target_key, origin, status)
                    VALUES ('claim', :k, 'test_fixture', 'accepted')"""),
            {"k": f"kg:{src}:G1"},
        )
    if cz.gate is not None:
        with pytest.raises(DBAPIError), cz.gate.connect() as c:
            c.execute(text("SELECT count(*) FROM kg.causal_claim"))
    # rows must be inserted in the transaction that created their load
    with cz.worker.begin() as c:
        load = c.execute(
            text("""INSERT INTO kg.causal_load(kind, source_key, origin, loader_version)
                    VALUES ('grading', 'reva.causal.grader', 'test_fixture', 't') RETURNING id""")
        ).scalar_one()
    with pytest.raises(DBAPIError, match="sealed"), cz.worker.begin() as c:
        c.execute(
            text("""INSERT INTO kg.causal_grade(load_id, claim_row_id, claim_key, origin, rules_version, rules_sha256,
                        grade, rule_ids, temporal_check, corroboration, inputs_sha256)
                    VALUES (:l, :c, :k, 'test_fixture', '1', repeat('a', 64), 'A', ARRAY['x'], 'consistent', 1,
                            repeat('b', 64))"""),
            {"l": load, "c": claim_id, "k": f"kg:{src}:G1"},
        )
    # a KG-derived claim must restate its statement row
    with pytest.raises(DBAPIError, match="restate"), cz.worker.begin() as c:
        load = c.execute(
            text("""INSERT INTO kg.causal_load(kind, source_key, origin, loader_version)
                    VALUES ('kg_statements', 'reva.causal.kg', 'test_fixture', 't') RETURNING id""")
        ).scalar_one()
        st = (
            c.execute(text("SELECT * FROM kg.statement_current WHERE statement_key = :k"), {"k": f"{src}:G1"})
            .mappings()
            .one()
        )
        c.execute(
            text("""INSERT INTO kg.causal_claim(load_id, claim_key, origin, content_sha256, cause_id, effect_id,
                        relation, family, source_key, source_tier, statement_key, statement_id, statement_row_id,
                        raw_sha256, retrieved_at)
                    VALUES (:l, :k, 'test_fixture', repeat('c', 64), :a, :b, 'has_cause', 'cause', :sk, 'T4', :sk2,
                            'G1', :sid, :raw, :ret)"""),
            {
                "l": load,
                "k": f"kg:{src}:G1",
                "a": f"{ns}:forged",
                "b": f"{ns}:g",
                "sk": src,
                "sk2": f"{src}:OTHER",
                "sid": st["id"],
                "raw": st["raw_sha256"],
                "ret": st["retrieved_at"],
            },
        )
