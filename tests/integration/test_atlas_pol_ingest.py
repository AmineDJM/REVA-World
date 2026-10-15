"""Politics atlas connectors (A-POL) end to end on the per-session throwaway database (fixtures, offline)."""

from __future__ import annotations

import json

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_ipu, atlas_regimes, atlas_vdem
from reva.adapters.connectors.atlas_sci_common import register_connector
from reva.atlas.tables import read_atlas_table
from reva.evidence.runner import FixtureHttp

pytestmark = pytest.mark.db


async def test_vdem_ingest_registers_three_tables(db):
    register_connector(db.owner, db.store, atlas_vdem.connector_spec())
    raws = await atlas_vdem.fetch_all(FixtureHttp(atlas_vdem.CONNECTOR_NAME))
    rep = atlas_vdem.ingest(db.worker, db.store, raws, origin="test_fixture")
    assert rep["release"] == "16.0" and rep["missing_variables"] == []
    for ds in atlas_vdem.DATASETS:
        t = rep[ds]
        assert t["dataset_key"] == f"atlas.vdem.{ds}" and t["created"] is True
        assert {u["provider_geo"] for u in t["unmapped_provider_geo"]} == {"PSE", "BVR", "TWN"}
    frame = read_atlas_table(db.store, rep["indices"]["artifact_sha256"])
    fr = frame.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "v2x_libdem")
                      & (pl.col("period_start").dt.year() == 2025))
    assert fr.height == 1 and 0 < float(fr["value"][0]) < 1
    with db.owner.connect() as c:
        rights = c.execute(
            text("SELECT rights FROM evidence.dataset WHERE dataset_key = 'atlas.vdem.indices' ORDER BY recorded_at DESC LIMIT 1")
        ).scalar_one()
        n_acq = c.execute(
            text("SELECT count(*) FROM evidence.acquisition WHERE origin = 'test_fixture' AND raw_sha256 = :s"),
            {"s": frame["raw_sha256"][0]},
        ).scalar_one()
    assert (rights if isinstance(rights, dict) else json.loads(rights))["licence"] == "CC-BY-SA-4.0"
    assert n_acq >= 1
    again = atlas_vdem.ingest(db.worker, db.store, raws, origin="test_fixture", datasets=("indices",))
    assert again["indices"]["created"] is False


async def test_regimes_ingest(db):
    for spec in (atlas_regimes.lied_connector_spec(), atlas_regimes.bmr_connector_spec()):
        register_connector(db.owner, db.store, spec)
    raws = await atlas_regimes.fetch_all(FixtureHttp(atlas_regimes.CONNECTOR_NAME))
    rep = atlas_regimes.ingest(db.worker, db.store, raws, origin="test_fixture", min_rows=(1, 1))
    assert rep["lied"]["dataset_key"] == "atlas.lied.lied" and rep["bmr"]["dataset_key"] == "atlas.bmr.bmr"
    assert {"260", "364", "713"} == {u["provider_geo"] for u in rep["bmr"]["unmapped_provider_geo"]}
    lied = read_atlas_table(db.store, rep["lied"]["artifact_sha256"])
    assert lied.filter(pl.col("provider_geo") == "Prussia|vdem=77")["iso3"].unique().to_list() == ["DEU"]


async def test_regimes_partial_file_writes_nothing(db):
    from reva.kernel.errors import SourceResponseError

    raws = await atlas_regimes.fetch_all(FixtureHttp(atlas_regimes.CONNECTOR_NAME))
    with pytest.raises(SourceResponseError, match="partial"):
        atlas_regimes.ingest(db.worker, db.store, raws, origin="test_fixture")


async def test_ipu_ingest(db):
    register_connector(db.owner, db.store, atlas_ipu.connector_spec())
    raws = await atlas_ipu.fetch_all(FixtureHttp(atlas_ipu.CONNECTOR_NAME))
    rep = atlas_ipu.ingest(db.worker, db.store, raws, origin="test_fixture")
    assert rep["parline"]["unmapped_provider_geo"] == [] and rep["elections"]["unmapped_provider_geo"] == []
    with db.owner.connect() as c:
        rights = c.execute(
            text("SELECT rights FROM evidence.dataset WHERE dataset_key = 'atlas.ipu.parline' ORDER BY recorded_at DESC LIMIT 1")
        ).scalar_one()
    rights = rights if isinstance(rights, dict) else json.loads(rights)
    assert rights["licence"] == "CC-BY-NC-SA-4.0" and rights["non_commercial"] is True
    el = read_atlas_table(db.store, rep["elections"]["artifact_sha256"])
    assert el.filter((pl.col("iso3") == "NAM") & pl.col("indicator").str.starts_with("election_date")).height > 0
