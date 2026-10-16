"""A-LAW atlas sources end to end through the ATLASCORE ingest entry point (fixture mode, no network)."""

from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_ccp, atlas_constitute, atlas_factbook, atlas_ssa_ssptw, atlas_wbl
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


async def _fixture_run(db, module, **kw):
    spec = module.atlas_source()
    if isinstance(spec, list):  # atlas_factbook declares two sources; the first is atlas.factbook
        spec = spec[0]
    ensure_registered(db.owner, db.store, spec)
    return await run_atlas_ingest(db.settings, db.worker, db.store, spec.name, mode="fixture", **kw)


async def test_wbl_tables_registered_with_provenance(db):
    out = await _fixture_run(db, atlas_wbl)
    assert out["status"] == "completed" and out["budget"] is None  # fixtures are never charged
    rep = out["summary"]
    assert rep["status"] == "ok" and rep["blocked"] == []
    keys = {t["dataset_key"]: t for t in rep["tables"]}
    assert set(keys) == {"atlas.wbl.panel_1971_2024", "atlas.wbl.wbl2026", "atlas.wbl.wbl1_2026"}
    assert all(t["created"] and t["unmapped_provider_geo"] == [] for t in rep["tables"])
    frame = read_atlas_table(db.store, keys["atlas.wbl.wbl2026"]["artifact_sha256"])
    m = frame.filter((pl.col("iso3") == "USA") & (pl.col("indicator") == "Pen_Law_PensAgeEq_FullM")).to_dicts()
    assert len(m) == 1 and m[0]["value"] is not None and json.loads(m[0]["flags"])["legal_basis"]
    raws = set(frame["raw_sha256"].unique())
    with db.owner.connect() as c:
        n = c.execute(
            text("""SELECT count(*) FROM evidence.acquisition a JOIN evidence.connector_version v
                    ON v.id = a.connector_version_id WHERE v.name = :n AND a.raw_sha256 = ANY(:s)
                    AND a.origin = 'test_fixture'"""),
            {"n": atlas_wbl.CONNECTOR_NAME, "s": list(raws)},
        ).scalar_one()
    assert n == len(raws) == 1
    again = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.wbl", mode="fixture", dataset="wbl2026")
    assert [t["created"] for t in again["summary"]["tables"]] == [False]  # idempotent, one dataset


async def test_constitute_ccp_factbook_ssa(db):
    out = await _fixture_run(db, atlas_constitute)
    (t,) = out["summary"]["tables"]
    assert t["dataset_key"] == "atlas.constitute.constitutions" and t["countries"] >= 185
    assert {u["provider_geo"] for u in t["unmapped_provider_geo"]} == {"Baden", "Bavaria", "Germany_Prussia"}

    out = await _fixture_run(db, atlas_ccp)
    rep = out["summary"]
    assert rep["status"] == "ok", rep["blocked"]
    keys = {t["dataset_key"]: t for t in rep["tables"]}
    assert set(keys) == {"atlas.ccp.chronology_v6", "atlas.ccp.characteristics_v5"}
    fr = read_atlas_table(db.store, keys["atlas.ccp.chronology_v6"]["artifact_sha256"]).filter(
        (pl.col("iso3") == "FRA") & (pl.col("indicator") == "evnttype") & (pl.col("period_start") == date(1958, 1, 1))
    )
    assert fr["value_text"].to_list() == ["new"]
    ch = read_atlas_table(db.store, keys["atlas.ccp.characteristics_v5"]["artifact_sha256"])
    usa = ch.filter((pl.col("iso3") == "USA") & (pl.col("indicator") == "votemin"))
    assert set(usa["value"].drop_nulls()) <= {"18"}

    # only 12 of the 261 profiles are recorded: the 249 others end as typed fixture_missing, nothing invented
    # (atlas.factbook is A-FACTBOOK's merged source: legal_2026_01 + figures_2026_01 + texts_2026_01)
    out = await _fixture_run(db, atlas_factbook)
    rep = out["summary"]
    assert rep["status"] == "partial" and {b["reason"] for b in rep["blocked"]} == {"fixture_missing"}
    assert len(rep["blocked"]) == 249
    t = next(x for x in rep["tables"] if x["dataset_key"] == "atlas.factbook.legal_2026_01")
    assert (
        t["countries"] >= 8
    )  # FRA USA SAU XKX TWN GRL ESH VAT PRK ... (partial territories WE/SV and World XX unmapped)
    assert {u["provider_geo"] for u in t["unmapped_provider_geo"]} <= {"WE", "SV", "XX"}  # partial / World

    # SSPTW: fixtures hold the Europe index, guide and 5 country pages; every other request is a typed
    # fixture_missing entry (the other regions' index/guide and 40 Europe pages), never an invented value
    out = await _fixture_run(db, atlas_ssa_ssptw)
    rep = out["summary"]
    assert rep["status"] == "partial" and {b["reason"] for b in rep["blocked"]} == {"fixture_missing"}
    (t,) = rep["tables"]
    assert t["dataset_key"] == "atlas.ssa.ssptw_2018_2019" and t["countries"] == 5 and t["unmapped_provider_geo"] == []
    frame = read_atlas_table(db.store, t["artifact_sha256"])
    che = frame.filter((pl.col("iso3") == "CHE") & pl.col("indicator").str.starts_with("old_age.pension_age."))
    assert sorted(che["value"].to_list()) == ["64", "65"]
