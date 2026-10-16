"""A-RESOURCES atlas sources end to end on a throwaway database (fixture bytes, no network).

Through the single atlas entry point (``reva.atlas.ingest.run_atlas_ingest``, fixture mode): USGS ScienceBase
items, metadata and CSVs and the EIA manifest, footnotes page and bulk zip are archived as acquisitions of their
connectors, every table is registered with its raw sha256s, a re-run with identical bytes is a no-op, and a
single-dataset run touches only that table.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_eia, atlas_usgs
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


def _run(db: Any, source: str, **kw: Any) -> dict[str, Any]:
    return asyncio.run(run_atlas_ingest(db.settings, db.worker, db.store, source, mode="fixture", **kw))


def _latest(db: Any, key: str) -> dict[str, Any]:
    with db.owner.connect() as c:
        row = (
            c.execute(
                text("""SELECT artifact_sha256, coverage FROM evidence.dataset WHERE dataset_key = :k
                        ORDER BY recorded_at DESC LIMIT 1"""),
                {"k": key},
            )
            .mappings()
            .one()
        )
    cov = row["coverage"] if isinstance(row["coverage"], dict) else json.loads(row["coverage"])
    return {"artifact_sha256": row["artifact_sha256"], "coverage": cov}


def _acquisitions(db: Any, name: str) -> int:
    with db.owner.connect() as c:
        return int(
            c.execute(
                text("""SELECT count(DISTINCT a.raw_sha256) FROM evidence.acquisition a
                        JOIN evidence.connector_version v ON v.id = a.connector_version_id
                        WHERE v.name = :n AND a.origin = 'test_fixture'"""),
                {"n": name},
            ).scalar_one()
        )


def test_usgs_fixture_ingest_registers_every_release_idempotently(db: Any) -> None:
    ensure_registered(db.owner, db.store, atlas_usgs.atlas_source())
    out = _run(db, "atlas.usgs_minerals")
    assert out["status"] == "completed" and out["error"] is None
    assert {r["dataset_key"] for r in out["registered"]} == {f"atlas.usgs.{r.dataset}" for r in atlas_usgs.RELEASES}
    assert out["archived_raw"] == 9 and _acquisitions(db, "atlas.usgs_minerals") == 9
    mcs = _latest(db, "atlas.usgs.mcs2026")
    assert "World total" in json.dumps(mcs["coverage"]) and "CC0" in mcs["coverage"]["coverage_note"]
    f = read_atlas_table(db.store, mcs["artifact_sha256"])
    li = f.filter(
        (pl.col("iso3") == "CHL")
        & (
            pl.col("indicator")
            == "Lithium|World Mine Production and Reserves|Production|Mine production|unit=metric tons"
        )
        & (pl.col("period_start") == date(2025, 1, 1))
    )
    assert li["value"].to_list() == ["56000"]
    again = _run(db, "atlas.usgs_minerals")
    assert all(r["created"] is False for r in again["registered"])
    one = _run(db, "atlas.usgs_minerals", dataset="myb2019")
    assert [r["dataset_key"] for r in one["registered"]] == ["atlas.usgs.myb2019"] and one["archived_raw"] == 3


def test_eia_fixture_ingest_registers_five_tables_from_one_download(db: Any) -> None:
    ensure_registered(db.owner, db.store, atlas_eia.atlas_source())
    out = _run(db, "atlas.eia_intl")
    assert out["status"] == "completed" and out["error"] is None
    assert {r["dataset_key"] for r in out["registered"]} == {f"atlas.eia.{t.dataset}" for t in atlas_eia.TABLES}
    assert out["archived_raw"] == 3 and _acquisitions(db, "atlas.eia_intl") == 3
    pet = _latest(db, "atlas.eia.intl_petroleum")
    f = read_atlas_table(db.store, pet["artifact_sha256"])
    sau = f.filter(
        (pl.col("iso3") == "SAU")
        & (pl.col("indicator") == "INTL.57-1-TBPD.A")
        & (pl.col("period_start") == date(2024, 1, 1))
    )
    assert sau["value"].to_list() == ["9232.154456284154"] and sau["unit"].to_list() == ["thousand barrels per day"]
    assert "WORL" in json.dumps(pet["coverage"])
    again = _run(db, "atlas.eia_intl")
    assert all(r["created"] is False for r in again["registered"])
    one = _run(db, "atlas.eia_intl", dataset="intl_coal")
    assert [r["dataset_key"] for r in one["registered"]] == ["atlas.eia.intl_coal"]
