"""Politics atlas sources (A-POL) through the atlas ingest entry point, fixture mode, throwaway database."""

from __future__ import annotations

import json

import polars as pl
import pytest
from sqlalchemy import text

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


async def _run(db, name: str, dataset: str | None = None) -> dict:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    rep = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture", dataset=dataset)
    assert rep["status"] == "completed", rep["error"]
    return rep


def _rights(db, key: str) -> dict:
    with db.owner.connect() as c:
        r = c.execute(
            text("SELECT a.rights FROM evidence.dataset d JOIN meta.artifact a ON a.sha256 = d.artifact_sha256 "
                 "WHERE d.dataset_key = :k ORDER BY d.recorded_at DESC LIMIT 1"),
            {"k": key},
        ).scalar_one()
    return r if isinstance(r, dict) else json.loads(r)


async def test_vdem_ingest_registers_three_tables(db):
    rep = await _run(db, "atlas.vdem")
    s = rep["summary"]
    assert s["release"] == "16.0" and s["missing_variables"] == [] and rep["archived_raw"] == 3
    for ds in ("indices", "institutions", "liberties"):
        t = s["tables"][ds]
        assert t["dataset_key"] == f"atlas.vdem.{ds}" and t["created"] is True
        assert {u["provider_geo"] for u in t["unmapped_provider_geo"]} == {"PSE", "BVR", "TWN"}
    frame = read_atlas_table(db.store, s["tables"]["indices"]["artifact_sha256"])
    fr = frame.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "v2x_libdem")
                      & (pl.col("period_start").dt.year() == 2025))
    assert fr.height == 1 and 0 < float(fr["value"][0]) < 1
    assert _rights(db, "atlas.vdem.indices")["licence"] == "CC-BY-SA-4.0"
    again = await _run(db, "atlas.vdem", dataset="indices")
    assert list(again["summary"]["tables"]) == ["indices"] and again["summary"]["tables"]["indices"]["created"] is False


async def test_lied_and_bmr_ingest(db):
    lied = await _run(db, "atlas.lied")
    t = lied["summary"]["table"]
    assert t["dataset_key"] == "atlas.lied.lied"
    frame = read_atlas_table(db.store, t["artifact_sha256"])
    assert frame.filter(pl.col("provider_geo") == "Prussia|vdem=77")["iso3"].unique().to_list() == ["DEU"]
    bmr = await _run(db, "atlas.bmr")
    assert {"260", "364", "713"} == {u["provider_geo"] for u in bmr["summary"]["table"]["unmapped_provider_geo"]}
    assert _rights(db, "atlas.bmr.bmr")["licence"] == "CC0-1.0"


async def test_ipu_ingest(db):
    rep = await _run(db, "atlas.ipu")
    tables = rep["summary"]["tables"]
    assert tables["parline"]["unmapped_provider_geo"] == [] and tables["elections"]["unmapped_provider_geo"] == []
    rights = _rights(db, "atlas.ipu.parline")
    assert rights["licence"] == "CC-BY-NC-SA-4.0" and rights["non_commercial"] is True
    el = read_atlas_table(db.store, tables["elections"]["artifact_sha256"])
    assert el.filter((pl.col("iso3") == "NAM") & pl.col("indicator").str.starts_with("election_date")).height > 0
