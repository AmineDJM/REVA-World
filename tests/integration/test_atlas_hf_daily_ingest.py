"""PRED-HF daily series end to end on the per-session throwaway database (atlas platform, fixture mode).

For each of the six sources (ECB, BIS bulk, US Treasury, EIA spot, Bank of England, New York Fed): raw files are
archived as evidence acquisitions, the daily atlas tables are registered with their rights, every row is one day
with as-of / publication / revision flags, and a second run with the same bytes registers nothing new.
"""

from __future__ import annotations

import json
from typing import Any

import polars as pl
import pytest

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db

SOURCES = {
    "atlas.ecb_daily": {"ecb.fx_ref", "ecb.estr", "ecb.key_rates"},
    "atlas.bis_daily": {"bis.cbpol_d", "bis.cbpol_d_series", "bis.eer_d"},
    "atlas.ust_yields": {"ust.par_yield", "ust.real_yield"},
    "atlas.eia_spot": {"eia.spot_d"},
    "atlas.boe_daily": {"boe.rates_d"},
    "atlas.nyfed_rates": {"nyfed.rates_d"},
}


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert out["status"] == "completed", out
    return out["summary"]


async def test_daily_sources_end_to_end(db: Any) -> None:
    for name, tables in SOURCES.items():
        s = await _run(db, name)
        got = {t["table"]: t for t in s["tables"]}
        assert set(got) == tables, (name, got)
        assert all(t["created"] and t["rows"] > 0 for t in got.values())
        assert all(s["report"]["policy_quotes_found"].values()), (name, s["report"]["policy_quotes_found"])
        for t in got.values():
            f = read_atlas_table(db.store, t["artifact_sha256"])
            data = f.filter(pl.col("value").is_not_null())
            if data.height:
                assert (data["period_end"] - data["period_start"]).dt.total_days().unique().to_list() == [1]
            fl = json.loads(f["flags"][0])
            assert fl["as_of"] and fl["licence"] and fl["publication"] and fl["revision_policy"]
        again = await _run(db, name)
        assert not any(t["created"] for t in again["tables"]), name

    s = await _run(db, "atlas.bis_daily")
    cb = read_atlas_table(db.store, next(t for t in s["tables"] if t["table"] == "bis.cbpol_d")["artifact_sha256"])
    assert set(cb.filter(pl.col("provider_geo") == "US")["iso3"].unique().to_list()) == {"USA"}
    assert (
        cb.filter(pl.col("provider_geo") == "XM")["iso3"].null_count()
        == cb.filter(pl.col("provider_geo") == "XM").height
    )
