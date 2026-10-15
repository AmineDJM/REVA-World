"""UN atlas layer (agent A-UN) end to end on a throwaway database with recorded fixture bytes,
through the platform entry point ``reva.atlas.ingest.run_atlas_ingest`` (fixture mode):
fetch -> archive raw (evidence.acquisition) -> parse -> register_atlas_table, idempotent."""

from __future__ import annotations

import json

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_un as U
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


async def _run(db, connector: U.UnConnector, dataset: str | None = None) -> dict:
    with db.owner.connect() as c:
        exists = c.execute(
            text("SELECT count(*) FROM evidence.connector_version WHERE name = :n"), {"n": connector.name}
        ).scalar_one()
    if not exists:
        ensure_registered(db.owner, db.store, connector.spec())
    out = await run_atlas_ingest(db.settings, db.worker, db.store, connector.name, dataset=dataset, mode="fixture")
    assert out["status"] == "completed" and out["budget"] is None  # fixtures are never charged
    return out["summary"]


async def test_wpp_tables_registered_and_idempotent(db):
    rep = await _run(db, U.WPP_CONNECTOR)
    assert rep["errors"] == []
    assert set(rep["tables"]) == {"wpp2024_demo", "wpp2024_variants", "wpp2024_age5", "wpp2024_age5_proj"}
    demo = rep["tables"]["wpp2024_demo"]
    assert demo["dataset_key"] == "atlas.un_wpp.wpp2024_demo" and demo["created"] is True
    unmapped = {u["provider_geo"] for u in demo["unmapped_provider_geo"]}
    assert {"158", "900", "1834"} <= unmapped and "412" not in unmapped
    frame = read_atlas_table(db.store, demo["artifact_sha256"])
    xkx = frame.filter((pl.col("iso3") == "XKX") & (pl.col("indicator") == "TPopulation1July"))
    assert xkx.height == 74  # 1950-2023 estimates
    with db.owner.connect() as c:
        n = c.execute(
            text("""SELECT count(*) FROM evidence.acquisition a JOIN evidence.connector_version v
                    ON v.id = a.connector_version_id WHERE v.name = :n AND a.origin = 'test_fixture'"""),
            {"n": U.WPP_CONNECTOR.name},
        ).scalar_one()
        assert n == 3
        manifest = c.execute(
            text("SELECT row_count, concept_id FROM evidence.dataset WHERE dataset_key = 'atlas.un_wpp.wpp2024_demo'")
        ).one()
        assert manifest.row_count == frame.height and manifest.concept_id == "atlas.table"
    again = await _run(db, U.WPP_CONNECTOR, dataset="wpp2024_demo")
    assert set(again["tables"]) == {"wpp2024_demo"}  # --dataset: one file fetched, one table
    assert again["tables"]["wpp2024_demo"]["created"] is False
    assert again["tables"]["wpp2024_demo"]["artifact_sha256"] == demo["artifact_sha256"]


async def test_ims_sdg_unhcr_syb_tables(db, monkeypatch):
    ims = await _run(db, U.IMS_CONNECTOR)
    assert ims["errors"] == [] and ims["tables"]["ims2024"]["countries"] == 2
    stock = read_atlas_table(db.store, ims["tables"]["ims2024"]["artifact_sha256"])
    assert set(stock["iso3"].drop_nulls()) == {"FRA", "CHI"}

    sdg = await _run(db, U.SDG_CONNECTOR)
    # only two series were recorded: every other series is a typed fixture miss, and the table
    # names what it left out instead of pretending to be complete
    assert {e["item"] for e in sdg["errors"]} == {
        U.sdg_data_item(s).key for s in U.SDG_SERIES if s.code not in ("SH_STA_MORT", "IT_USE_ii99")
    }
    assert sdg["table_reports"]["sdg_headline"]["series"].keys() == {"SH_STA_MORT", "IT_USE_ii99"}
    frame = read_atlas_table(db.store, sdg["tables"]["sdg_headline"]["artifact_sha256"])
    assert set(frame["indicator"].str.split("|").list.first()) == {"SH_STA_MORT", "IT_USE_ii99"}

    monkeypatch.setattr(U, "latest_complete_year", lambda: 2025)  # fixtures were recorded in 2026
    unhcr = await _run(db, U.UNHCR_CONNECTOR)
    assert unhcr["errors"] == []
    assert set(unhcr["tables"]) == {"pop_bilateral", "pop_by_asylum", "pop_by_origin"}
    bil = read_atlas_table(db.store, unhcr["tables"]["pop_bilateral"]["artifact_sha256"])
    assert not (bil["value"] == "0").any()
    row = bil.filter((pl.col("iso3") == "POL") & (pl.col("indicator") == "refugees|origin=UKR")).to_dicts()
    assert row and json.loads(row[0]["flags"])["counterpart_iso3"] == "UKR"

    syb = await _run(db, U.SYB_CONNECTOR)
    assert syb["errors"] == [] and syb["tables"]["syb68_t02"]["row_count"] > 0
