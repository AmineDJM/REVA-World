"""V6TRADEHF atlas sources through the atlas entry point (fixture mode, throwaway test database).

Fixture bytes -> archived acquisitions -> registered monthly atlas tables (idempotent re-run)."""

from __future__ import annotations

import json
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_eurostat_comext as cx
from reva.adapters.connectors import atlas_imf_imts_m as im
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db

EXPECTED = {
    im.CONNECTOR_NAME: {"imf.imts_world_m", "imf.imts_m"},
    cx.CONNECTOR_NAME: {"eurostat.comext_hs2_m", "eurostat.comext_partner_m"},
}


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    return await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")


@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_fixture_ingest_registers_every_table(db: Any, name: str) -> None:
    out = await _run(db, name)
    assert out["status"] == "completed", out
    summ = out["summary"]
    assert summ["ok"], summ.get("errors")
    assert {t["table"] for t in summ["tables"]} == EXPECTED[name]
    again = await _run(db, name)
    assert all(t["created"] is False for t in again["summary"]["tables"])


async def test_imts_monthly_iso3_and_partners(db: Any) -> None:
    out = await _run(db, im.CONNECTOR_NAME)
    t = next(t for t in out["summary"]["tables"] if t["table"] == "imf.imts_m")
    frame = read_atlas_table(db.store, t["artifact_sha256"])
    kos = frame.filter(pl.col("provider_geo") == "KOS")
    assert kos.height > 0 and set(kos["iso3"]) == {"XKX"}
    fl = [json.loads(f) for f in frame.filter(pl.col("indicator") == "XG_FOB_USD|partner=USA")["flags"]]
    assert fl and all(f["partner_iso3"] == "USA" for f in fl)


async def test_comext_reporters_mapped(db: Any) -> None:
    out = await _run(db, cx.CONNECTOR_NAME)
    t = next(t for t in out["summary"]["tables"] if t["table"] == "eurostat.comext_hs2_m")
    frame = read_atlas_table(db.store, t["artifact_sha256"])
    assert set(frame["iso3"]) == {"FRA", "GRC"}
