"""A-LAW atlas connectors end to end on a throwaway database (recorded/derived fixtures, no network)."""

from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_ccp, atlas_constitute, atlas_factbook, atlas_ssa_ssptw, atlas_wbl
from reva.adapters.connectors.atlas_law_common import register_connector
from reva.atlas.tables import read_atlas_table
from reva.evidence.runner import FixtureHttp

pytestmark = pytest.mark.db


async def test_wbl_tables_registered_with_provenance(db):
    register_connector(db.owner, db.store, atlas_wbl.WblConnector().describe())
    rep = await atlas_wbl.run(db.worker, db.store, FixtureHttp(atlas_wbl.CONNECTOR_NAME), origin="test_fixture")
    assert rep.blocked == [] and rep.ok
    keys = {t["dataset_key"]: t for t in rep.tables}
    assert set(keys) == {"atlas.wbl.panel_1971_2024", "atlas.wbl.wbl2026", "atlas.wbl.wbl1_2026"}
    assert all(t["created"] and t["unmapped_provider_geo"] == [] for t in rep.tables)
    frame = read_atlas_table(db.store, keys["atlas.wbl.wbl2026"]["artifact_sha256"])
    m = frame.filter((pl.col("iso3") == "USA") & (pl.col("indicator") == "Pen_Law_PensAgeEq_FullM")).to_dicts()
    assert len(m) == 1 and m[0]["value"] is not None and json.loads(m[0]["flags"])["legal_basis"]
    # every cell's raw artifact is an archived acquisition of the connector
    raws = set(frame["raw_sha256"].unique())
    with db.owner.connect() as c:
        n = c.execute(
            text("""SELECT count(*) FROM evidence.acquisition a JOIN evidence.connector_version v
                    ON v.id = a.connector_version_id WHERE v.name = :n AND a.raw_sha256 = ANY(:s)
                    AND a.origin = 'test_fixture'"""),
            {"n": atlas_wbl.CONNECTOR_NAME, "s": list(raws)},
        ).scalar_one()
        ds = c.execute(
            text("SELECT dataset_key, concept_id, row_count FROM evidence.dataset WHERE dataset_key LIKE 'atlas.wbl.%'")
        ).all()
    assert n == len(raws) == 1
    assert {d[0] for d in ds} == set(keys) and {d[1] for d in ds} == {"atlas.table"}
    again = await atlas_wbl.run(db.worker, db.store, FixtureHttp(atlas_wbl.CONNECTOR_NAME), origin="test_fixture")
    assert [t["created"] for t in again.tables] == [False, False, False]  # idempotent


async def test_constitute_ccp_factbook_tables(db):
    for spec in (
        atlas_constitute.ConstituteConnector().describe(),
        atlas_ccp.CcpConnector().describe(),
        atlas_factbook.FactbookConnector().describe(),
    ):
        register_connector(db.owner, db.store, spec)
    rep = await atlas_constitute.run(
        db.worker, db.store, FixtureHttp(atlas_constitute.CONNECTOR_NAME), origin="test_fixture"
    )
    (t,) = rep.tables
    assert t["dataset_key"] == "atlas.constitute.constitutions" and t["countries"] >= 185
    assert {u["provider_geo"] for u in t["unmapped_provider_geo"]} == {"Baden", "Bavaria", "Germany_Prussia", "Taiwan"}

    rep = await atlas_ccp.run(db.worker, db.store, FixtureHttp(atlas_ccp.CONNECTOR_NAME), origin="test_fixture")
    assert [b.reason for b in rep.blocked] == ["response_cap"]  # characteristics: 118 MB > 50 MB bound
    (t,) = rep.tables
    assert t["dataset_key"] == "atlas.ccp.chronology_v6" and rep.as_dict()["status"] == "partial"
    fr = read_atlas_table(db.store, t["artifact_sha256"]).filter(
        (pl.col("iso3") == "FRA") & (pl.col("indicator") == "evnttype") & (pl.col("period_start") == date(1958, 1, 1))
    )
    assert fr["value_text"].to_list() == ["new"]

    only = ["fr", "us", "sa", "tw", "kv"]
    rep = await atlas_factbook.run(
        db.worker, db.store, FixtureHttp(atlas_factbook.CONNECTOR_NAME), origin="test_fixture", gecs=only
    )
    (t,) = rep.tables
    assert (
        rep.blocked == [] and t["countries"] == 4 and [u["provider_geo"] for u in t["unmapped_provider_geo"]] == ["TW"]
    )


async def test_missing_fixture_is_blocked_not_empty_success(db):
    register_connector(db.owner, db.store, atlas_factbook.FactbookConnector().describe())
    rep = await atlas_factbook.run(
        db.worker, db.store, FixtureHttp(atlas_factbook.CONNECTOR_NAME), origin="test_fixture", gecs=["fr", "gm"]
    )
    assert [b.reason for b in rep.blocked] == ["fixture_missing"] and rep.as_dict()["status"] == "partial"
    rep = await atlas_ssa_ssptw.run(
        db.worker, db.store, FixtureHttp(atlas_ssa_ssptw.CONNECTOR_NAME), origin="test_fixture"
    )
    assert rep.as_dict()["status"] == "blocked" and rep.blocked[0].http_status == 403
