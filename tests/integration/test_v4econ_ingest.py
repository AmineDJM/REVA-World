"""V4ECON atlas sources through the atlas entry point (fixture mode, throwaway test database).

Fixture bytes -> archived acquisitions -> registered atlas tables (idempotent re-run); NUTS and OECD region
entities with part_of / located_in / measured_by statements in the KG.
"""

from __future__ import annotations

import json
from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_eurostat_macro as es
from reva.adapters.connectors import atlas_fao_ffpi as ffpi
from reva.adapters.connectors import atlas_imf_hf as ih
from reva.adapters.connectors import atlas_oecd_macro as om
from reva.adapters.connectors import atlas_wb_icp as icp
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db

EXPECTED = {
    ih.CONNECTOR_NAME: {f"imf.{f.dataset}" for f in ih.FLOWS},
    es.CONNECTOR_NAME: {f"eurostat.{q.dataset}" for q in es.QUERIES},
    om.CONNECTOR_NAME: {f"oecd.{f.dataset}" for f in om.FLOWS},
    icp.CONNECTOR_NAME: {"wb.icp2021"},
    ffpi.CONNECTOR_NAME: {"fao_ffpi.ffpi_m"},
}


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    return await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")


@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_fixture_ingest_registers_every_table(db: Any, name: str) -> None:
    out = await _run(db, name)
    assert out["status"] == "completed", out
    summ = out["summary"]
    assert summ["ok"], summ.get("errors")
    assert {t["table"] for t in summ["tables"]} == EXPECTED[name]
    again = await _run(db, name)
    assert all(t["created"] is False for t in again["summary"]["tables"])


async def test_qnea_table_content(db: Any) -> None:
    out = await _run(db, ih.CONNECTOR_NAME)
    t = next(t for t in out["summary"]["tables"] if t["table"] == "imf.qnea_q")
    frame = read_atlas_table(db.store, t["artifact_sha256"])
    fra = frame.filter(pl.col("iso3") == "FRA")
    assert fra.height > 0
    flags = [json.loads(f) for f in fra["flags"]]
    assert {f["frequency"] for f in flags} == {"Q"}
    assert all(f["seasonal_adjustment_label"] for f in flags)


async def test_region_kg_statements(db: Any) -> None:
    await _run(db, es.CONNECTOR_NAME)
    await _run(db, om.CONNECTOR_NAME)
    with db.owner.connect() as c:
        rows = c.execute(
            text("""SELECT subject_qid, property_pid, object_qid FROM kg.statement_current
                    WHERE subject_qid IN ('nuts:FR101', 'nuts:FR10', 'oecdreg:FRL04') AND object_qid IS NOT NULL""")
        ).all()
        kinds = dict(
            c.execute(
                text("SELECT qid, kind FROM kg.entity_current WHERE qid IN ('nuts:FR101', 'oecdreg:FRL04')")
            ).all()
        )
    edges = {(r.subject_qid, r.property_pid, r.object_qid) for r in rows}
    assert ("nuts:FR101", "nuts:part_of", "nuts:FR10") in edges
    assert ("nuts:FR10", "nuts:in_country", "Q142") in edges
    assert ("nuts:FR101", "econ:measured_by", "atlas:eurostat.nuts_gdp") in edges
    assert ("oecdreg:FRL04", "oecdreg:part_of", "oecdreg:FRL") in edges
    assert ("oecdreg:FRL04", "oecdreg:in_country", "Q142") in edges
    assert kinds == {"nuts:FR101": "nuts_region", "oecdreg:FRL04": "oecd_region"}
