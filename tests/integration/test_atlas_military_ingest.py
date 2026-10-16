"""A-MILITARY: UN peacekeeping (atlas.peacekeeping) end to end on a throwaway database, fixture mode (no network).

The HDX licence records and the Hub CSV slices are archived as evidence acquisitions, the five atlas tables are
registered, the KG statements load without rejection, and re-running is a no-op.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_peacekeeping as pk
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered

pytestmark = pytest.mark.db


async def _run(db: Any, **kw: Any) -> dict[str, Any]:
    spec = pk.atlas_source()
    ensure_registered(db.owner, db.store, spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, spec.name, mode="fixture", **kw)
    assert out["status"] == "completed", out
    return out


async def test_peacekeeping_fixture_ingest(db: Any) -> None:
    out = await _run(db)
    s = out["summary"]
    assert out["archived_raw"] == 2 * len(pk.FILES)
    assert {k for k, v in s["atlas"].items() if v["created"]} == set(pk.DATASETS)
    assert all(v["licence_id"] == "cc-by-igo" for v in s["licences"].values())
    unmapped = {u["provider_geo"] for u in s["atlas"]["contrib_pre2010"]["unmapped_provider_geo"]}
    assert {"RUS", "CZE"} <= unmapped  # 1990 rows: USSR / Czechoslovakia, kept with QIDs
    kg = s["kg"]
    assert not kg["rejected"], kg
    assert kg["statements_inserted"] == s["kg_counts"]["statements"]
    with db.owner.connect() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'Q15180' "
                "AND property_pid = 'unpk:contributed_personnel_to' AND object_qid = 'unpk:mission:UNTSO'"
            )
        ).scalar_one()
        assert n == 1
        lab = c.execute(
            text("SELECT label_en FROM kg.property_current WHERE pid = 'unpk:mission_country'")
        ).scalar_one_or_none()
        assert lab and "as reported" in lab
    again = await _run(db)
    assert not any(v["created"] for v in again["summary"]["atlas"].values())
    assert again["summary"]["kg"]["statements_inserted"] == 0


async def test_single_dataset_run_skips_kg(db: Any) -> None:
    out = await _run(db, dataset="fatalities")
    s = out["summary"]
    assert set(s["atlas"]) == {"fatalities"} and "kg" not in s
    assert out["archived_raw"] == 2  # one HDX record + one CSV
