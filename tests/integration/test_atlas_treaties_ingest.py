"""A-TREATIES sources end to end on a throwaway database through the atlas platform (fixture mode: no network).

UNTC pages and OHCHR JSON are archived as evidence acquisitions, the atlas tables are registered, the UNTC KG
statements load without rejection (signatures P1891, parties, withdrawals as end dates); re-running is a no-op.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_ohchr, atlas_treaties
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered

pytestmark = pytest.mark.db


async def _run(db: Any, module: Any) -> dict[str, Any]:
    spec = module.atlas_source()
    ensure_registered(db.owner, db.store, spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, spec.name, mode="fixture")
    assert out["status"] == "completed", out
    return out["summary"]


async def test_untc_ingest(db: Any) -> None:
    s = await _run(db, atlas_treaties)
    assert s["atlas"]["created"] and s["atlas"]["row_count"] > 1000
    assert s["unmapped_participants"] == ["European Union"]
    assert s["terms"]["phrase_found"]
    for k, v in s["treaties"].items():
        assert v["listed_consents"] == v["provider_parties"], k
    kg = s["kg"]
    assert not kg["rejected"], kg
    assert kg["statements_inserted"] == sum(s["kg_build"]["statements"].values())
    with db.owner.connect() as c:
        rows = c.execute(
            text(
                "SELECT property_pid, start_time, end_time FROM kg.statement_current "
                "WHERE source_key = 'untc.mtdsg' AND statement_id = 'party:XXVII-7-d:united_states_of_america'"
            )
        ).all()
        assert [(r[0], r[1][:10], r[2][:10]) for r in rows] == [("untc:party", "2021-01-20", "2026-01-27")]
        n = c.execute(
            text(
                "SELECT count(*) FROM kg.statement_current WHERE source_key = 'untc.mtdsg' AND property_pid = 'P1891' "
                "AND subject_qid = :s"
            ),
            {"s": atlas_treaties.load_treaty_qids().get("XVIII-10", "untc:treaty:XVIII-10")},
        ).scalar_one()
        assert n == s["treaties"]["XVIII-10"]["listed_signatures"] + 2  # + Burundi, Philippines (bracketed)
    again = await _run(db, atlas_treaties)
    assert not again["atlas"]["created"] and again["kg"]["statements_inserted"] == 0


async def test_ohchr_ingest(db: Any) -> None:
    s = await _run(db, atlas_ohchr)
    assert s["atlas"]["created"] and len(s["treaties"]) == 18
    assert {u["provider_geo"] for u in s["atlas"]["unmapped"] or []} <= {"ERU"}
    again = await _run(db, atlas_ohchr)
    assert not again["atlas"]["created"]
