"""V7 BEAT-AGRI sources through the atlas entry point (fixture mode, throwaway test database)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from reva.adapters.connectors import atlas_ncei_cag as cag
from reva.adapters.connectors import atlas_usda_wasde_esmis as W
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db

OPTIONS: dict[str, dict[str, Any]] = {
    W.NAME: {"pages": "13,19,26", "first": "2005-01-01", "last": "2016-12-31"},
    cag.NAME: {"last_year": 2026},
}


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    return await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture", options=OPTIONS[name])


async def test_wasde_esmis_fixture_ingest(db: Any) -> None:
    out = await _run(db, W.NAME)
    assert out["status"] == "completed", out
    assert {r["dataset_key"] for r in out["registered"]} == {"atlas.usda_wasde_esmis.headline"}
    summ = out["summary"]
    parsed = {r["release"]: r for r in summ["releases"] if r["status"] == "parsed"}
    assert set(parsed) == {"2005-07-12", "2010-08-12", "2015-07-10"}
    assert parsed["2005-07-12"]["format"] == "txt" and parsed["2015-07-10"]["format"] == "xml"
    assert summ["status_counts"]["not_captured"] > 0  # listed releases whose files are not in the fixture bank
    reg = out["registered"][0]
    with db.worker.connect() as conn:
        from sqlalchemy import text

        sha = conn.execute(
            text("SELECT artifact_sha256 FROM evidence.dataset WHERE dataset_key = :k AND origin = 'test_fixture'"),
            {"k": reg["dataset_key"]},
        ).scalar_one()
    f = read_atlas_table(db.store, sha)
    assert f.height == 180
    row = f.filter(f["indicator"] == "wasde_esmis:us.corn.production|v=2015-07-10").filter(
        f["period_start"].dt.year() == 2015
    )
    assert row["value"].to_list() == ["13530"]
    flags = json.loads(row["flags"][0])
    assert flags["estimate_kind"] == "projection" and flags["available_at"] == "2015-07-11T00:00:00Z"
    again = await _run(db, W.NAME)
    assert all(r["created"] is False for r in again["registered"])


async def test_cag_fixture_ingest(db: Any) -> None:
    out = await _run(db, cag.NAME)
    assert out["status"] == "completed", out
    assert {r["dataset_key"] for r in out["registered"]} == {"atlas.ncei_cag.agbelt_monthly"}
    assert len(out["summary"]["files"]) == 6
