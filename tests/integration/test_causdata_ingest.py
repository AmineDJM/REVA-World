"""CAUSDATA sources end to end on a throwaway database through the atlas platform (fixture mode: no network).

Raw slices and codebook PDFs are archived as evidence acquisitions, the computed atlas tables are registered, KG
events load without rejection, claims are delivered through CAUSCORE's ``load_dataset_claims`` when the causal schema
exists (else staged as JSON for a later load), and re-running is a no-op.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_icb, atlas_mid, atlas_ucdp_term
from reva.adapters.connectors import causdata_common as cd
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered

pytestmark = pytest.mark.db


async def _run(db: Any, module: Any, staging: Path) -> dict[str, Any]:
    spec = module.atlas_source()
    ensure_registered(db.owner, db.store, spec)
    out = await run_atlas_ingest(
        db.settings, db.worker, db.store, spec.name, mode="fixture", options={"claims_staging": str(staging)}
    )
    assert out["status"] == "completed", out
    return out["summary"]


def _claims_ok(summary: dict[str, Any], datasets: tuple[str, ...]) -> None:
    for ds in datasets:
        rep = summary["claims"][ds]
        assert rep["claims"] > 0, (ds, rep)
        if rep["status"] == "loaded":
            r = rep["report"]
            assert not r.get("rejected_counts"), r
        else:
            assert rep["status"] == "staged", rep
            doc = json.loads(Path(rep["file"]).read_text())
            assert doc["origin"] == "test_fixture" and len(doc["claims"]) == rep["claims"]


def _count(db: Any, sql: str, **kw: Any) -> int:
    with db.owner.connect() as c:
        return int(c.execute(text(sql), kw).scalar_one())


async def test_icb_ingest(db: Any, tmp_path: Path) -> None:
    s = await _run(db, atlas_icb, tmp_path)
    assert s["atlas"]["actor_cy"]["created"] and s["atlas"]["actor_cy"]["row_count"] > 20
    assert all(v["matches"] for v in s["codebooks"].values())
    kg = s["kg"]
    assert not kg["rejected"], kg
    assert kg["statements_inserted"] == s["kg_counts"]["statements"]
    _claims_ok(s, ("icb1", "icb2"))
    n = _count(
        db,
        "SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'icb:crisis:196' AND property_pid = 'P580'",
    )
    assert n == 1
    again = await _run(db, atlas_icb, tmp_path)
    assert not again["atlas"]["actor_cy"]["created"] and again["kg"]["statements_inserted"] == 0


async def test_mid_ingest(db: Any, tmp_path: Path) -> None:
    s = await _run(db, atlas_mid, tmp_path)
    assert s["atlas"]["mid5_cy"]["created"]
    assert not s["kg"]["rejected"], s["kg"]
    _claims_ok(s, ("mid5",))
    n = _count(
        db,
        "SELECT count(*) FROM kg.statement_current WHERE property_pid = 'causdata:coded_issue' "
        "AND subject_qid LIKE 'mid:dispute:%'",
    )
    assert n > 5


async def test_ucdp_term_ingest(db: Any, tmp_path: Path) -> None:
    s = await _run(db, atlas_ucdp_term, tmp_path)
    assert s["atlas"]["term_cy"]["created"] and s["atlas"]["pa_cy"]["created"]
    assert not s["kg"]["rejected"], s["kg"]
    _claims_ok(s, ("term_conflict",))
    assert s["claims"]["peace_agreements"]["claims"] == 0  # context only (audit M6); the empty delivery retracts
    if cd.causal_available(db.worker):  # CAUSCORE present: claims are rows, dataset-coded, T1
        n = _count(
            db,
            "SELECT count(*) FROM kg.causal_claim_current WHERE effect_id = 'ucdp:episode:20001:termination' "
            "AND relation = 'coded_outcome'",
        )
        assert n == 1
