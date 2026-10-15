"""KGGEOPOL sources end to end on a throwaway database through the atlas platform (fixture mode: no network).

Raw slices are archived as evidence acquisitions, atlas tables (and the CShapes polygon dataset) are
registered, and the KG statements load without rejection; re-running is a no-op.
"""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_cshapes, atlas_ucdp, atlas_unga_votes
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered

pytestmark = pytest.mark.db


async def _run(db: Any, module: Any) -> dict[str, Any]:
    spec = module.atlas_source()
    ensure_registered(db.owner, db.store, spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, spec.name, mode="fixture")
    assert out["status"] == "completed", out
    return out["summary"]


async def test_ucdp_ingest(db: Any) -> None:
    s = await _run(db, atlas_ucdp)
    assert s["atlas"]["orgvio_cy"]["created"] and s["atlas"]["acd_cy"]["created"]
    kg = s["kg"]
    assert not kg["rejected"], kg
    assert kg["statements_inserted"] == s["kg_counts"]["statements"]
    with db.owner.connect() as c:
        n = c.execute(text("SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'ucdp:conflict:246' "
                           "AND property_pid = 'ucdp:battle_related_deaths'")).scalar_one()
        assert n == 0  # 1954-62 war: UCDP has battle-related deaths only from 1989
        n = c.execute(text("SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'ucdp:conflict:246' "
                           "AND property_pid = 'ucdp:intensity_level'")).scalar_one()
        assert n >= 8
    again = await _run(db, atlas_ucdp)
    assert not again["atlas"]["orgvio_cy"]["created"] and again["kg"]["statements_inserted"] == 0


async def test_unga_ingest(db: Any) -> None:
    s = await _run(db, atlas_unga_votes)
    assert {k for k, v in s["atlas"].items() if v["created"]} == {"votes", "rollcalls", "vote_shares"}
    assert not s["kg"]["rejected"], s["kg"]
    with db.owner.connect() as c:
        n = c.execute(text("SELECT count(*) FROM kg.statement_current WHERE property_pid LIKE 'unga:voted_%' "
                           "AND object_qid LIKE 'unga:rcid:%'")).scalar_one()
    assert n > 50


async def test_cshapes_ingest(db: Any) -> None:
    s = await _run(db, atlas_cshapes)
    assert s["polygons"]["dataset_key"] == "geo.cshapes.polygons" and s["polygons"]["row_count"] > 20
    assert not s["kg"]["rejected"], s["kg"]
    with db.owner.connect() as c:
        rows = c.execute(text("""SELECT start_time, end_time FROM kg.statement_current
                                 WHERE subject_qid = 'cshapes:gw:211' AND property_pid = 'cshapes:shares_border_with'
                                   AND object_qid = 'cshapes:gw:220'""")).all()
    assert rows and rows[-1][1] == "2019-12-31"
    assert isinstance(pl.DataFrame(), pl.DataFrame)
