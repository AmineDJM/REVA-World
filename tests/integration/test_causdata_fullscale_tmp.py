"""TEMPORARY (CAUSDATA full-scale load, deleted after the run): full real bytes from the scratch fixture root."""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

import reva.atlas.ingest as ingest
from reva.adapters.connectors import causdata_common as cd
from reva.atlas.sources import ensure_registered, get_source
from reva.evidence.runner import FixtureHttp

pytestmark = [pytest.mark.db]
FULL = Path(os.environ["CAUSDATA_FULL"])
OUT = Path(os.environ["CAUSDATA_OUT"])


async def test_full(db: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(ingest, "fixture_http", lambda name: FixtureHttp(name, root=FULL))
    stats: dict[str, Any] = {}
    staging = OUT / "staging"
    for name in ("atlas.icb", "atlas.mid", "atlas.ucdp_term", "atlas.ucdp"):
        if name == "atlas.ucdp":
            # KGGEOPOL's conflicts (ucdp:conflict:<id>, the cause of peace-agreement claims) from its committed fixture
            monkeypatch.setattr(ingest, "fixture_http", lambda n: FixtureHttp(n))
        spec = get_source(name).spec
        ensure_registered(db.owner, db.store, spec)
        t0 = time.time()
        rep = await ingest.run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture",
                                            options={"claims_staging": str(staging)})
        assert rep["status"] == "completed", rep
        s = rep["summary"]
        stats[name] = {"seconds": round(time.time() - t0, 1), "registered": rep["registered"],
                       "kg": s.get("kg"), "claims": s.get("claims"), "row_counts": s.get("row_counts"),
                       "codebooks": s.get("codebooks"), "anomalies": s.get("anomalies")}
        assert not s["kg"]["rejected"], s["kg"]
    if cd.causal_available(db.worker):
        gr = cd._causal_api("grade_claims")(db.worker)
        stats["grading"] = gr.as_dict() if hasattr(gr, "as_dict") else str(gr)
        with db.owner.connect() as c:
            rows = c.execute(text("""
                SELECT c.source_key, c.dataset, c.relation, g.grade, g.temporal_check, count(*)
                FROM kg.causal_claim_current c LEFT JOIN kg.causal_grade_current g ON g.claim_row_id = c.id
                WHERE c.source_tier = 'T1' GROUP BY 1,2,3,4,5 ORDER BY 1,2,3,4,5""")).all()
            stats["claims_by_grade"] = [list(map(str, r)) for r in rows]
            stats["unknown_entities"] = c.execute(text("""
                SELECT count(*) FROM kg.causal_claim_current c WHERE c.source_tier='T1' AND (
                  NOT EXISTS (SELECT 1 FROM kg.entity e WHERE e.qid = c.cause_id) OR
                  NOT EXISTS (SELECT 1 FROM kg.entity e WHERE e.qid = c.effect_id))""")).scalar_one()
            stats["examples"] = [list(map(str, r)) for r in c.execute(text("""
                SELECT c.cause_id, c.relation, c.effect_id, g.grade, g.temporal_check, g.rule_ids
                FROM kg.causal_claim_current c JOIN kg.causal_grade_current g ON g.claim_row_id = c.id
                WHERE c.effect_id IN ('icb:crisis:196', 'icb:crisis:196:actor:450:response', 'ucdp:episode:33201:termination',
                                      'mid:dispute:3551:outcome', 'ucdp:pa:1565')""")).all()]
            stats["inconsistent"] = [list(map(str, r)) for r in c.execute(text("""
                SELECT c.source_key, c.dataset, c.cause_id, c.effect_id, g.temporal
                FROM kg.causal_claim_current c JOIN kg.causal_grade_current g ON g.claim_row_id = c.id
                WHERE g.temporal_check = 'inconsistent' LIMIT 40""")).all()]
            stats["sizes"] = {r[0]: r[1] for r in c.execute(text(
                "SELECT relname, pg_total_relation_size(oid) FROM pg_class WHERE relname IN "
                "('statement','entity','causal_claim','causal_grade') AND relkind='r'")).all()}
            stats["kg_statements_by_source"] = {r[0]: r[1] for r in c.execute(text(
                "SELECT source_key, count(*) FROM kg.statement_current GROUP BY 1")).all()}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "fullscale_stats.json").write_text(json.dumps(stats, indent=1, default=str))
