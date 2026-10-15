"""A-HISTECON connectors end to end on a throwaway database (fixture bytes, no network).

Raw responses are archived as evidence acquisitions, atlas tables are registered from them, and the COW
state-system membership / war participation statements are loaded into the KG; re-running with the same
bytes is a no-op.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_clio, atlas_cow, atlas_gapminder, atlas_maddison
from reva.adapters.connectors.atlas_history_common import ingest, register_connector
from reva.atlas.tables import read_atlas_table
from reva.evidence.runner import FixtureHttp

pytestmark = pytest.mark.db


async def _run(db: Any, connector: Any, name: str) -> dict[str, Any]:
    register_connector(db.owner, db.store, connector.describe())
    return await ingest(db.worker, db.store, connector, FixtureHttp(name))


async def test_maddison_clio_gapminder_ingest_idempotent(db: Any) -> None:
    rep = await _run(db, atlas_maddison.MaddisonConnector(min_rows=100), atlas_maddison.NAME)
    (t,) = rep["tables"]
    assert t["dataset_key"] == "atlas.maddison.mpd2023" and t["created"] is True
    frame = read_atlas_table(db.store, t["artifact_sha256"])
    fra = frame.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "pop") & (pl.col("period_start") == date(1820, 1, 1)))
    assert fra["value"].to_list() == ["31250"]
    again = await ingest(db.worker, db.store, atlas_maddison.MaddisonConnector(min_rows=100), FixtureHttp(atlas_maddison.NAME))
    assert again["tables"][0]["created"] is False

    rep = await _run(db, atlas_clio.ClioConnector(min_datasets=1), atlas_clio.NAME)
    assert rep["tables"][0]["dataset_key"] == "atlas.clio.infra" and rep["tables"][0]["row_count"] > 10_000
    rep = await _run(db, atlas_gapminder.GapminderConnector(), atlas_gapminder.NAME)
    assert rep["tables"][0]["coverage"]["period_max"] == "2025-01-01"
    with db.owner.connect() as c:
        n = c.execute(text("SELECT count(*) FROM evidence.dataset WHERE dataset_key IN "
                           "('atlas.maddison.mpd2023','atlas.clio.infra','atlas.gapminder.fasttrack')")).scalar_one()
    assert n == 3


async def test_cow_atlas_and_kg_load(db: Any) -> None:
    conn = atlas_cow.CowConnector()
    rep = await _run(db, conn, atlas_cow.NAME)
    keys = {t["dataset_key"] for t in rep["tables"]}
    assert keys == {"atlas.cow.nmc7", "atlas.cow.system2024", "atlas.cow.wars4"}
    raws = rep["raws"]
    kg = atlas_cow.load_kg(db.worker, db.store, conn.kg, raw_sha256s=[atlas_cow.sha256_hex(r.body) for r in raws],
                           retrieved_at=max(r.retrieved_at for r in raws), origin="test_fixture")
    assert kg["rejected_total"] == 0, kg["rejected_sample"]
    assert kg["statements_inserted"] == len(conn.kg["statements"])
    with db.owner.connect() as c:
        rows = c.execute(text("""SELECT start_time, end_time FROM kg.statement_current
                                 WHERE subject_qid = 'cow:220' AND property_pid = 'P463'
                                   AND object_qid = 'cow:system:interstate' ORDER BY start_time""")).all()
    assert [tuple(r) for r in rows] == [("1816-01-01", "1942-11-11"), ("1944-08-25", None)]
    kg2 = atlas_cow.load_kg(db.worker, db.store, conn.kg, raw_sha256s=[atlas_cow.sha256_hex(r.body) for r in raws],
                            retrieved_at=max(r.retrieved_at for r in raws), origin="test_fixture")
    assert kg2["statements_inserted"] == 0 and kg2["statements_unchanged"] == len(conn.kg["statements"])
