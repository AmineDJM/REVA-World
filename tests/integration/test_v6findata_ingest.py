"""V6FINDATA finance sources end to end on the per-session throwaway database (atlas platform, fixture mode):
raw files archived, tables registered with rights, licence / as-of / precision on every row, idempotent re-run."""

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
    "atlas.bis_fin": {"bis.lbs_rep", "bis.lbs_cp", "bis.dsec_gov"},
    "atlas.bis_fin_bulk": {"bis.cbta", "bis.gli"},
    "atlas.imf_fin": {"imf.cofer", "imf.gdd", "imf.hpd"},
    "atlas.ecb_fin": {"ecb.wfs_w", "ecb.lty_m", "ecb.yc_d"},
    "atlas.frb_h41": {"frb.h41_w"},
    "atlas.boe_balance": {"boe.weekly_w", "boe.bs_q"},
    "atlas.ff_factors": {"kfrench.ff3", "kfrench.ff3_d", "kfrench.ff5", "kfrench.mom"},
}


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert out["status"] == "completed", out
    summary: dict[str, Any] = out["summary"]
    return summary


async def test_findata_sources_end_to_end(db: Any) -> None:
    for name, tables in SOURCES.items():
        s = await _run(db, name)
        got = {t["table"]: t for t in s["tables"]}
        assert set(got) == tables, (name, got)
        assert all(t["created"] and t["rows"] > 0 for t in got.values())
        for t in got.values():
            f = read_atlas_table(db.store, t["artifact_sha256"])
            fl = json.loads(f["flags"][0])
            assert fl["as_of"] and fl["licence"] and fl["precision"] and fl["revision_policy"], (name, fl)
        again = await _run(db, name)
        assert not any(t["created"] for t in again["tables"]), name

    s = await _run(db, "atlas.bis_fin")
    lbs = read_atlas_table(db.store, next(t for t in s["tables"] if t["table"] == "bis.lbs_cp")["artifact_sha256"])
    assert set(lbs.filter(pl.col("provider_geo") == "FR")["iso3"].unique().to_list()) == {"FRA"}
    assert (
        lbs.filter(pl.col("provider_geo") == "5J")["iso3"].null_count()
        == lbs.filter(pl.col("provider_geo") == "5J").height
    )
    s = await _run(db, "atlas.ff_factors")
    f = read_atlas_table(db.store, s["tables"][0]["artifact_sha256"])
    assert f["iso3"].null_count() == f.height  # the US stock-market universe is not a country statistic
