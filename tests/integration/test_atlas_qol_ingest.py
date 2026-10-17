"""A-QOL atlas connectors end to end on a throwaway database (fixture bytes, no network).

Raw responses are archived as evidence acquisitions, then the atlas tables are registered; a second
run with identical bytes is a no-op.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_owid, atlas_undp, atlas_whr, atlas_wid
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


async def _ingest(db: Any, module: Any, **kw: Any) -> dict[str, Any]:
    """One platform run in fixture mode (``reva atlas ingest <source> --fixture``)."""
    out = await run_atlas_ingest(db.settings, db.worker, db.store, module.CONNECTOR_NAME, mode="fixture", **kw)
    assert out["status"] == "completed" and out["budget"] is None
    return out["summary"]  # type: ignore[no-any-return]


async def _run(db: Any, module: Any, **kw: Any) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, module.atlas_source())
    return await _ingest(db, module, **kw)


async def test_undp_and_whr_ingest_idempotent(db: Any) -> None:
    rep = await _run(db, atlas_undp)
    keys = {t["dataset_key"]: t for t in rep["tables"]}
    assert set(keys) == {"atlas.undp.hdr25", "atlas.undp.gmpi2026"}
    hdr = keys["atlas.undp.hdr25"]
    assert hdr["created"] is True and hdr["origin"] == "test_fixture"
    assert {u["provider_geo"] for u in hdr["unmapped_provider_geo"]} == {"ZZK.WORLD", "ZZJ.SSA"}
    frame = read_atlas_table(db.store, hdr["artifact_sha256"])
    fra = frame.filter(
        (pl.col("iso3") == "FRA") & (pl.col("indicator") == "hdi") & (pl.col("period_start") == date(2023, 1, 1))
    )
    assert fra.height == 1
    with db.owner.connect() as c:
        n_acq = c.execute(
            text("""SELECT count(*) FROM evidence.acquisition a JOIN evidence.connector_version v ON v.id = a.connector_version_id
                    WHERE v.name = :n AND a.origin = 'test_fixture'"""),
            {"n": atlas_undp.CONNECTOR_NAME},
        ).scalar_one()
        ds = c.execute(text("SELECT count(*) FROM evidence.dataset WHERE dataset_key LIKE 'atlas.undp.%'")).scalar_one()
    assert n_acq == 3 and ds == 2
    again = await _ingest(db, atlas_undp)
    assert all(t["created"] is False for t in again["tables"])
    only = await _ingest(db, atlas_undp, dataset="gmpi2026")  # --dataset: the other table is not registered
    assert [t["dataset_key"] for t in only["tables"]] == ["atlas.undp.gmpi2026"]

    w = await _run(db, atlas_whr)
    (t,) = w["tables"]
    assert t["dataset_key"] == "atlas.whr.whr26_fig21" and t["row_count"] > 10_000
    with db.owner.connect() as c:
        rights = c.execute(
            text("SELECT rights FROM meta.artifact WHERE sha256 = :s"), {"s": t["artifact_sha256"]}
        ).scalar_one()
    assert rights["licence_status"] == "free_download_no_explicit_licence"


async def test_owid_and_wid_ingest(db: Any) -> None:
    rep = await _run(db, atlas_owid)
    keys = {t["dataset_key"]: t for t in rep["tables"]}
    assert set(keys) == {
        "atlas.owid.energy",
        "atlas.owid.co2",
        "atlas.owid.g_ghg_by_sector",
        "atlas.owid.g_temperature_anomaly",
        "atlas.owid.g_living_planet_index",  # V4ECO: WWF/ZSL Living Planet Index (published trends)
    }
    lpi = read_atlas_table(db.store, keys["atlas.owid.g_living_planet_index"]["artifact_sha256"])
    w1970 = lpi.filter((pl.col("provider_geo") == "OWID_WRL") & (pl.col("period_start").dt.year() == 1970))
    assert sorted(w1970["indicator"].to_list()) == ["ci_high", "ci_low", "lpi_final"] and set(w1970["value"]) == {"1"}
    co2 = read_atlas_table(db.store, keys["atlas.owid.co2"]["artifact_sha256"])
    assert co2.filter(pl.col("provider_geo") == "Kosovo")["iso3"].unique().to_list() == ["XKX"]
    assert all("original_source" in json.loads(f) for f in co2["flags"].unique().to_list())

    wid = await _run(db, atlas_wid)  # byte-range archive fixture (ArchiveRangeFixtureHttp)
    (t,) = wid["tables"]
    assert t["dataset_key"] == "atlas.wid.wid_key" and t["unmapped_provider_geo"] == []
    assert t["coverage"]["countries"] == 3
    with db.owner.connect() as c:
        n = c.execute(
            text("""SELECT count(*) FROM evidence.acquisition a JOIN evidence.connector_version v ON v.id = a.connector_version_id
                    WHERE v.name = :n"""),
            {"n": atlas_wid.CONNECTOR_NAME},
        ).scalar_one()
    # tail + WID_countries.csv + data/metadata of FR, KS, DZ (one raw per byte range) + WID_data_TW.csv
    # (TW maps to TWN since crosswalk 1.1.0; the excerpt has no TW metadata member and no selected series)
    assert n == 2 + 3 * 2 + 1
    sub = await _ingest(db, atlas_wid, options={"countries": "FR"})  # run option: a country subset
    (t2,) = sub["tables"]
    assert t2["coverage"]["countries"] == 1 and t2["created"] is True
