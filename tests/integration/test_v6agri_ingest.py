"""V6AGRI sources through the atlas entry point (fixture mode, throwaway test database).

Fixture bytes -> archived acquisitions -> registered atlas tables (idempotent re-run); IFPRI measures become
``export_restriction`` KG events with their country, dates and HS headings; WASDE cells are read back through the
vintage accessor.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_fao_tm as tm
from reva.adapters.connectors import atlas_ifpri_er as ie
from reva.adapters.connectors import atlas_usda_psd as psd
from reva.adapters.connectors import atlas_usda_wasde as wa
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table
from reva.evaluation.cutoff import CutoffMode, CutoffRepository

pytestmark = pytest.mark.db

EXPECTED = {
    psd.CONNECTOR_NAME: {"atlas.usda_psd.grains", "atlas.usda_psd.oilseeds"},
    wa.NAME: {"atlas.usda_wasde.wasde"},
    tm.CONNECTOR_NAME: {"atlas.fao_tm.tm_staples_asia"},
    ie.CONNECTOR_NAME: {"atlas.ifpri_er.measures"},
}
OPTIONS: dict[str, dict[str, Any]] = {wa.NAME: {"first": "2026-05", "last": "2026-10"}}


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    return await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture", options=OPTIONS.get(name))


@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_fixture_ingest_registers_every_table(db: Any, name: str) -> None:
    out = await _run(db, name)
    assert out["status"] == "completed", out
    assert {r["dataset_key"] for r in out["registered"]} == EXPECTED[name]
    again = await _run(db, name)
    assert all(r["created"] is False for r in again["registered"])


async def test_wasde_months_and_vintage_accessor(db: Any) -> None:
    out = await _run(db, wa.NAME)
    summ = out["summary"]
    # 2026-05 exists only as the corrected '-V2' file; months without a captured file are recorded as missing
    assert [f["file"] for f in summ["files"]] == [
        "oce-wasde-report-data-2026-05-V2.csv",
        "oce-wasde-report-data-2026-10.csv",
    ]
    assert summ["missing_months"] == ["2026-06", "2026-07", "2026-08", "2026-09"]
    with db.worker.connect() as conn:
        acc = CutoffRepository(
            conn, datetime(2026, 7, 1, tzinfo=UTC), CutoffMode.available_then, origins=("test_fixture",)
        ).vintages(db.store)
        assert acc.vintage_in_force("usda_wasde")["vintage"] == "2026-05"


async def test_ifpri_events_in_kg(db: Any) -> None:
    out = await _run(db, ie.CONNECTOR_NAME)
    kg = out["summary"]["kg"]
    assert kg["status"] == "loaded", kg
    with db.owner.connect() as c:
        n = c.execute(
            text("SELECT count(DISTINCT qid) FROM kg.entity WHERE origin = 'test_fixture' AND qid LIKE 'ifpri_er:%'")
        ).scalar_one()
        assert n == 82
        rows = c.execute(
            text(
                "SELECT property_pid, object_qid FROM kg.statement WHERE origin = 'test_fixture' "
                "AND subject_qid = 'ifpri_er:covid19:61'"
            )
        ).all()
    props = {(r[0], r[1]) for r in rows}
    assert ("ifpri_er:covers_hs_heading", "hs2017:1006") in props
    assert any(p == "ifpri_er:imposed_by" and o for p, o in props)


async def test_tm_table_content(db: Any) -> None:
    out = await _run(db, tm.CONNECTOR_NAME)
    t = out["registered"][0]
    with db.owner.connect() as c:
        sha = c.execute(
            text(
                "SELECT artifact_sha256 FROM evidence.dataset WHERE dataset_key = :k ORDER BY recorded_at DESC LIMIT 1"
            ),
            {"k": t["dataset_key"]},
        ).scalar_one()
    frame = read_atlas_table(db.store, sha)
    afg = frame.filter(pl.col("iso3") == "AFG")
    assert afg.height > 0
    assert {json.loads(f)["regional_file"] for f in afg["flags"]} == {"Asia"}
