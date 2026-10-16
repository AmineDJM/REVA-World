"""A-SANCTIONS sources end to end on the per-session throwaway database (fixture mode: no network).

Raw captures are archived as evidence acquisitions, the atlas tables registered, the KG statements (sanction
episodes, coded outcomes, regimes, programs) load without rejection, EUSANCT cases are linked to TIES cases only
on evidence, and re-running is a no-op.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_sanctions as S
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered

pytestmark = pytest.mark.db


async def _run(db: Any, name: str) -> dict[str, Any]:
    spec = next(s for s in S.atlas_source() if s.name == name)
    ensure_registered(db.owner, db.store, spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert out["status"] == "completed", out
    return out["summary"]


def _count(db: Any, sql: str, **kw: Any) -> int:
    with db.owner.connect() as c:
        return int(c.execute(text(sql), kw).scalar_one())


async def test_ties_then_eusanct(db: Any) -> None:
    s = await _run(db, "atlas.ties")
    assert s["cases"] == 1412 and s["atlas"]["dyad_year"]["created"] and s["atlas"]["country_year"]["created"]
    assert not s["kg"]["rejected"], s["kg"]
    assert s["kg"]["statements_inserted"] == s["kg_counts"]["statements"]
    assert s["causal_claims"] == {"coded_outcome": 1024}
    n = _count(
        db,
        "SELECT count(*) FROM kg.statement_current WHERE property_pid = 'ties:outcome_record' "
        "AND object_qid LIKE 'ties:outcome:%'",
    )
    assert n == 1024
    d = s["claims_delivery"]
    if d["status"] == "loaded":  # CAUSCORE's loader and schema present
        assert d["report"]["inserted"] == 1024 and not d["report"]["rejected_counts"], d
        assert (
            _count(
                db,
                "SELECT count(*) FROM kg.causal_claim_current WHERE source_key = 'ties.unc' "
                "AND relation = 'coded_outcome' AND source_tier = 'T1' AND codebook IS NOT NULL",
            )
            == 1024
        )
    assert (
        _count(
            db,
            "SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'ties:case:1945121601' "
            "AND property_pid = 'ties:sender'",
        )
        == 1
    )

    e = await _run(db, "atlas.eusanct")
    assert e["cases"] == 23 and not e["kg"]["rejected"], e["kg"]
    assert e["same_case_as"]["linked"] == 6 and e["same_case_as"]["case_id_shared_but_target_differs"] == 2
    assert _count(db, "SELECT count(*) FROM kg.statement_current WHERE property_pid = 'eusanct:same_case_as'") == 6
    assert set(e["causal_claims"]) <= {"coded_outcome", "contributing_factor"}
    if e["claims_delivery"]["status"] == "loaded":
        assert not e["claims_delivery"]["report"]["rejected_counts"], e["claims_delivery"]
        assert _count(db, "SELECT count(*) FROM kg.causal_claim_current WHERE relation IN ('reaction_to', "
                      "'precipitated') AND source_key IN ('ties.unc', 'eusanct.kondata')") == 0  # fmt: skip

    again = await _run(db, "atlas.ties")
    assert not again["atlas"]["dyad_year"]["created"] and again["kg"]["statements_inserted"] == 0
    if again["claims_delivery"]["status"] == "loaded":
        assert again["claims_delivery"]["report"]["inserted"] == 0


async def test_eu_map_and_ofac(db: Any) -> None:
    m = await _run(db, "atlas.eu_sanctionsmap")
    assert m["items"] == 8 and m["atlas"]["regimes"]["created"] and not m["kg"]["rejected"], m
    o = await _run(db, "atlas.ofac_programs")
    assert o["items"] == 36 and o["atlas"]["programs"]["created"] and not o["kg"]["rejected"], o
    assert (
        _count(
            db,
            "SELECT count(*) FROM kg.statement_current WHERE property_pid = 'ofac:program_country' "
            "AND subject_qid = 'ofac:program:cuba-sanctions'",
        )
        == 1
    )
    # regime level only: no acquisition of a designation list was ever made
    assert (
        _count(
            db,
            """SELECT count(*) FROM evidence.acquisition a
                 JOIN evidence.connector_version v ON v.id = a.connector_version_id
                WHERE v.name IN ('atlas.eu_sanctionsmap', 'atlas.ofac_programs')""",
        )
        == 2
    )
