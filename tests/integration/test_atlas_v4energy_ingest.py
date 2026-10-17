"""V4ENERGY sources end to end on a throwaway database (fixture bytes, no network).

Through the single atlas entry point (``reva.atlas.ingest.run_atlas_ingest``, fixture mode): the Pink Sheet page and
workbooks, the EIA STEO manifest and bulk zip, the GPPD README / version / CSV are archived as acquisitions of their
connectors; atlas tables are registered with their raw sha256s; GPPD plants are loaded into the KG; re-runs with
identical bytes write nothing new.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_eia_steo, atlas_gppd, atlas_pinksheet
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


def _run(db: Any, source: str, **kw: Any) -> dict[str, Any]:
    return asyncio.run(run_atlas_ingest(db.settings, db.worker, db.store, source, mode="fixture", **kw))


def _latest(db: Any, key: str) -> str:
    with db.owner.connect() as c:
        return str(
            c.execute(
                text(
                    "SELECT artifact_sha256 FROM evidence.dataset WHERE dataset_key = :k ORDER BY recorded_at DESC LIMIT 1"
                ),
                {"k": key},
            ).scalar_one()
        )


def test_pinksheet_fixture_ingest(db: Any) -> None:
    ensure_registered(db.owner, db.store, atlas_pinksheet.atlas_source())
    out = _run(db, "atlas.wb_pinksheet")
    assert out["status"] == "completed" and out["error"] is None, out
    assert {r["dataset_key"] for r in out["registered"]} == {
        f"atlas.wb_cmo.{s.dataset}" for s in atlas_pinksheet.SHEETS
    }
    assert out["archived_raw"] == 3
    f = read_atlas_table(db.store, _latest(db, "atlas.wb_cmo.monthly_prices"))
    hh = f.filter((pl.col("indicator") == "Natural gas, US") & (pl.col("period_start") == date(1960, 1, 1)))
    assert hh.height == 1
    again = _run(db, "atlas.wb_pinksheet")
    assert all(r["created"] is False for r in again["registered"])


def test_steo_fixture_ingest(db: Any) -> None:
    ensure_registered(db.owner, db.store, atlas_eia_steo.atlas_source())
    out = _run(db, "atlas.eia_steo")
    assert out["status"] == "completed" and out["error"] is None, out
    keys = {r["dataset_key"] for r in out["registered"]}
    assert keys == {"atlas.eia_steo.steo_observed", "atlas.eia_steo.steo_forecast"}
    fc = read_atlas_table(db.store, _latest(db, "atlas.eia_steo.steo_forecast"))
    assert all(json.loads(x)["estimate_kind"] in ("projection", "unknown_boundary") for x in fc["flags"])


def test_gppd_fixture_ingest_loads_kg_idempotently(db: Any) -> None:
    ensure_registered(db.owner, db.store, atlas_gppd.atlas_source())
    out = _run(db, "atlas.wri_gppd")
    assert out["status"] == "completed" and out["error"] is None, out
    kg = out["result"]["kg"] if "result" in out else None
    with db.owner.connect() as c:
        n = c.execute(text("SELECT count(*) FROM kg.statement_current WHERE source_key = 'wri.gppd'")).scalar_one()
        kinds = dict(
            c.execute(text("SELECT kind, count(*) FROM kg.entity_current WHERE qid LIKE 'gppd:%' GROUP BY kind")).all()
        )
    assert n > 50, kg
    assert set(kinds) == {"facility", "energy_source"}
    _run(db, "atlas.wri_gppd")
    with db.owner.connect() as c:
        n2 = c.execute(text("SELECT count(*) FROM kg.statement WHERE source_key = 'wri.gppd'")).scalar_one()
    assert n2 == n


def test_usgs_facilities_fixture_ingest_loads_kg(db: Any) -> None:
    from reva.adapters.connectors import atlas_usgs_facilities

    ensure_registered(db.owner, db.store, atlas_usgs_facilities.atlas_source())
    out = _run(db, "atlas.usgs_myb_facilities")
    assert out["status"] == "completed" and out["error"] is None, out
    with db.owner.connect() as c:
        n = c.execute(
            text("SELECT count(*) FROM kg.statement_current WHERE property_pid LIKE 'usgs_myb:%'")
        ).scalar_one()
        kinds = dict(
            c.execute(
                text("SELECT kind, count(*) FROM kg.entity_current WHERE qid LIKE 'usgs_myb:%' GROUP BY kind")
            ).all()
        )
    assert n > 100 and set(kinds) == {"facility", "commodity"}
