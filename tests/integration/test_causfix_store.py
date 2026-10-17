"""grading.yaml version 2 on the database (agent CAUSFIX, docs/dev/v3_causfix.md): a row the database refuses is
rejected per statement (the load goes on), WDQS rows are deferred (references unknown), non-source references
give T4, attributed claims are worded with their claimant, chains skip classes, cycles, self-loops and coded
outcomes.

Synthetic ids (``cf...:``) and per-test source keys keep this module independent of other suites sharing the
session database. Nothing here is a real-world fact.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from reva.causal import grade_claims, load_kg_claims
from reva.causal import graph as cg
from reva.kg.model import KgProperty, KgStatement, load_batch, register_properties

pytestmark = pytest.mark.db

PROPS = [
    KgProperty("P828", "item", "has_cause", "has cause"),
    KgProperty("P1542", "item", "has_effect", "has effect"),
    KgProperty("P31", "item", "instance_of", "instance of"),
    KgProperty("P279", "item", "subclass_of", "subclass of"),
    KgProperty("P580", "time", "start_time", "start time"),
    KgProperty("P571", "time", "inception", "inception"),
    KgProperty("P576", "time", "dissolved", "dissolved, abolished or demolished date"),
]


@pytest.fixture(scope="module")
def cf(db):  # type: ignore[no-untyped-def]
    register_properties(db.worker, PROPS)
    return db


def _ns() -> str:
    return "cf" + uuid.uuid4().hex[:8]


def _kg(db, src: str, sts: list[KgStatement]) -> None:  # type: ignore[no-untyped-def]
    raw = db.store.put_bytes(f"causfix {src} {uuid.uuid4()}".encode(), "raw", "application/json").sha256
    rep = load_batch(
        db.worker,
        db.store,
        statements=sts,
        source_key=src,
        raw_sha256s=[raw],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
    )
    assert rep.rejected_total == 0, rep.rejected[:5]


def _current(db, prefix: str) -> dict[str, dict[str, Any]]:  # type: ignore[no-untyped-def]
    with db.api.connect() as c:
        return {
            r["statement_id"]: dict(r)
            for r in c.execute(
                text("""SELECT c.*, g.grade, g.rule_ids, g.temporal_check, g.temporal
                          FROM kg.causal_claim_current c LEFT JOIN kg.causal_grade_current g ON g.claim_row_id = c.id
                         WHERE starts_with(c.claim_key, :p)"""),
                {"p": prefix},
            ).mappings()
        }


def _n_loads(db) -> int:  # type: ignore[no-untyped-def]
    with db.api.connect() as c:
        return int(c.execute(text("SELECT count(*) FROM kg.causal_load")).scalar_one())


def _t(subject: str, pid: str, iso: str, prec: int) -> KgStatement:
    return KgStatement(subject, pid, value={"type": "time", "time": iso, "precision": prec}, statement_id=f"{pid}{iso}")


def test_a_refused_row_is_rejected_and_never_aborts_the_load(cf) -> None:  # type: ignore[no-untyped-def]
    """Reproduces the reva_kg_causwd failure (a stale CHECK refusing T2 statement rows aborted the whole load)."""
    ns = _ns()
    src = f"wikidata.{ns}"
    a, b, c = f"{ns}:a", f"{ns}:b", f"{ns}:c"
    _kg(
        cf,
        src,
        [
            KgStatement(a, "P1542", b, statement_id="OK1"),
            KgStatement(b, "P1542", c, statement_id="BAD", references=[{"P356": ["10.1000/causfix"]}]),
            KgStatement(a, "P1542", c, statement_id="OK2", references=[{"P854": ["https://example.org/x"]}]),
        ],
    )
    con = f"causfix_refuse_{ns}"
    with cf.owner.begin() as conn:  # the stale shape: T2 refused on statement rows
        conn.execute(text(f"ALTER TABLE kg.causal_claim ADD CONSTRAINT {con} CHECK (statement_id <> 'BAD') NOT VALID"))
    try:
        r = load_kg_claims(cf.worker, origin="test_fixture", source_keys=[src])
        assert (r.inserted, r.new) == (2, 2)
        assert r.rejected_counts == {f"db_refused:{con}": 1}
        assert r.rejected_sample[0]["key"] == f"kg:{src}:BAD"
        assert set(_current(cf, f"kg:{src}:")) == {"OK1", "OK2"}
        # a reload changes nothing and records no load row; the refused row is reported again
        n = _n_loads(cf)
        r2 = load_kg_claims(cf.worker, origin="test_fixture", source_keys=[src])
        assert (r2.inserted, r2.unchanged, r2.load_ids, r2.rejected_total) == (0, 2, [], 1)
        assert _n_loads(cf) == n
    finally:
        with cf.owner.begin() as conn:
            conn.execute(text(f"ALTER TABLE kg.causal_claim DROP CONSTRAINT {con}"))
    r3 = load_kg_claims(cf.worker, origin="test_fixture", source_keys=[src])
    assert (r3.inserted, r3.rejected_total) == (1, 0)
    assert _current(cf, f"kg:{src}:")["BAD"]["source_tier"] == "T2"


def test_wdqs_rows_are_deferred_and_pointer_references_give_t4(cf) -> None:  # type: ignore[no-untyped-def]
    ns = _ns()
    a, b = f"{ns}:a", f"{ns}:b"
    _kg(cf, "wikidata.wdqs", [KgStatement(a, "P1542", b, statement_id=f"{ns}-W")])
    r = load_kg_claims(cf.worker, origin="test_fixture", source_keys=["wikidata.wdqs"])
    assert r.deferred_counts.get("references_unknown", 0) >= 1
    assert f"{ns}-W" not in _current(cf, "kg:wikidata.wdqs:")
    src = f"wikidata.{ns}"
    imp = [{"P143": ["Q58251"], "P4656": ["https://az.wikipedia.org/?oldid=7446971"]}]
    _kg(cf, src, [KgStatement(a, "P1542", b, statement_id="IMP", references=imp)])
    load_kg_claims(cf.worker, origin="test_fixture", source_keys=[src])
    grade_claims(cf.worker, origin="test_fixture")
    row = _current(cf, f"kg:{src}:")["IMP"]
    assert row["source_tier"] == "T4" and row["upstream_keys"] == ["community:wikidata"] and row["grade"] == "D"
    assert "tier.T4.non_source_references_only" in row["rule_ids"]


def test_attributed_claims_are_worded_with_their_claimant_and_not_composed(cf) -> None:  # type: ignore[no-untyped-def]
    ns = _ns()
    src = f"wikidata.{ns}"
    a, b, c = f"{ns}:a", f"{ns}:b", f"{ns}:c"
    ref = [{"P854": ["https://example.org/speech"]}]
    _kg(
        cf,
        src,
        [
            KgStatement(b, "P828", a, statement_id="ATT", references=ref, qualifiers={"P3680": [f"{ns}:party"]}),
            KgStatement(b, "P1542", c, statement_id="NAT", qualifiers={"P5102": [f"{ns}:allegation"]}),
        ],
    )
    load_kg_claims(cf.worker, origin="test_fixture", source_keys=[src])
    grade_claims(cf.worker, origin="test_fixture")
    rows = _current(cf, f"kg:{src}:")
    assert rows["ATT"]["grade"] == "D" and "grade.D.attributed" in rows["ATT"]["rule_ids"]
    assert rows["NAT"]["grade"] == "D" and "nature.qualified" in rows["NAT"]["rule_ids"]
    with cf.api.connect() as conn:
        claims = cg.causes(conn, b, origin="test_fixture")["claims"]
        att = next(x for x in claims if x["source"]["statement_id"] == "ATT")
        # the party has no KG label: named "<id> (label not loaded)", never a bare id (FIXA, audit2 N3)
        assert att["wording"].startswith("Wikidata (")
        assert f"records that {ns}:party (label not loaded) asserts that" in att["wording"]
        assert "According to" not in att["wording"] and att["attribution"]["supported_by"][0]["id"] == f"{ns}:party"
        nat = next(x for x in cg.effects(conn, b, origin="test_fixture")["claims"] if x["claim_key"].endswith("NAT"))
        assert f"nature of this statement as: {ns}:allegation" in nat["wording"]
        ch = cg.chain(conn, a, c, origin="test_fixture")
        assert not ch["found"] and ch["search"]["quarantined_skipped"] >= 1


def test_chains_skip_classes_cycles_self_loops_and_outcomes(cf) -> None:  # type: ignore[no-untyped-def]
    ns = _ns()
    src = f"wikidata.{ns}"
    e1, war, e2, kind, e3, e4 = (f"{ns}:{x}" for x in ("e1", "war", "e2", "kind", "e3", "e4"))
    x, y = f"{ns}:x", f"{ns}:y"
    _kg(
        cf,
        src,
        [
            KgStatement(war, "P279", f"{ns}:conflict", statement_id="cls1"),  # "war" is a class (P279)
            KgStatement(f"{ns}:inst", "P31", kind, statement_id="cls2"),  # "kind": only a P31 target -> class
            KgStatement(e1, "P1542", war, statement_id="h1"),
            KgStatement(war, "P1542", e2, statement_id="h2"),
            KgStatement(e1, "P1542", kind, statement_id="h3"),
            KgStatement(kind, "P1542", e3, statement_id="h4"),
            KgStatement(e4, "P1542", e4, statement_id="loop"),  # self-loop
            KgStatement(x, "P1542", y, statement_id="c1"),  # cycle x <-> y
            KgStatement(y, "P1542", x, statement_id="c2"),
        ],
    )
    load_kg_claims(cf.worker, origin="test_fixture", source_keys=[src])
    grade_claims(cf.worker, origin="test_fixture")
    rows = _current(cf, f"kg:{src}:")
    assert "consistency.self_loop" in rows["loop"]["rule_ids"]
    assert "consistency.cycle" in rows["c1"]["rule_ids"] and "consistency.cycle" in rows["c2"]["rule_ids"]
    with cf.api.connect() as conn:
        assert cg.class_nodes(conn, [war, kind, e1, f"{ns}:unknown"], "test_fixture") == {war, kind}
        ch = cg.chain(conn, e1, e2, origin="test_fixture")
        # e1's two successors are classes ("war" by P279, "kind" as a P31-only target): neither is traversed
        assert not ch["found"] and ch["search"]["class_nodes_sample"] == sorted([war, kind])
        assert "class node" in ch["not_found_reason"]
        assert not cg.chain(conn, e1, e3, origin="test_fixture")["found"]
        # a class asked for as an endpoint is kept (the reader chose it)
        assert cg.chain(conn, e1, war, origin="test_fixture")["found"]
        cyc = cg.chain(conn, x, y, origin="test_fixture")
        assert not cyc["found"] and cyc["search"]["quarantined_skipped"] >= 1
        cons = cg.consistency(conn, origin="test_fixture", limit=200)
        assert (e4, e4) in {(c["cause"]["id"], c["effect"]["id"]) for c in cons["self_loops"]["sample"]}
