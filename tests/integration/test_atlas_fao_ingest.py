"""FAOSTAT atlas source end to end on a throwaway database (fixture bytes, no network).

Through the single atlas entry point (``reva.atlas.ingest.run_atlas_ingest``, fixture mode): raw zips are
archived as acquisitions of connector ``atlas.fao``, every domain table is registered, a re-run with
identical bytes is a no-op, and a single-dataset run touches only that table.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_fao
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


def _run(db: Any, **kw: Any) -> dict[str, Any]:
    return asyncio.run(run_atlas_ingest(db.settings, db.worker, db.store, "atlas.fao", mode="fixture", **kw))


def test_fao_fixture_ingest_registers_every_domain_idempotently(db: Any) -> None:
    ensure_registered(db.owner, db.store, atlas_fao.atlas_source())
    out = _run(db)
    assert out["status"] == "completed" and out["error"] is None
    keys = {r["dataset_key"]: r for r in out["registered"]}
    assert set(keys) == {f"atlas.fao.{d.dataset}" for d in atlas_fao.DOMAINS}
    assert all(r["created"] for r in out["registered"])
    assert out["archived_raw"] == len(atlas_fao.DOMAINS) + 1
    with db.owner.connect() as c:
        n_acq = c.execute(
            text("""SELECT count(DISTINCT a.raw_sha256) FROM evidence.acquisition a
                    JOIN evidence.connector_version v ON v.id = a.connector_version_id
                    WHERE v.name = 'atlas.fao' AND a.origin = 'test_fixture'"""),
        ).scalar_one()
        row = (
            c.execute(
                text("""SELECT artifact_sha256, coverage FROM evidence.dataset WHERE dataset_key = 'atlas.fao.fbs'
                        ORDER BY recorded_at DESC LIMIT 1""")
            )
            .mappings()
            .one()
        )
    assert n_acq == len(atlas_fao.DOMAINS) + 1
    cov = row["coverage"] if isinstance(row["coverage"], dict) else json.loads(row["coverage"])
    assert cov["countries"] >= 4 and "FAOSTAT FBS" in cov["coverage_note"]
    fbs = read_atlas_table(db.store, row["artifact_sha256"])
    fra = fbs.filter(
        (pl.col("iso3") == "FRA") & (pl.col("indicator") == "2901|664") & (pl.col("period_start") == date(2020, 1, 1))
    )
    assert fra["value"].to_list() == ["3485.450000"]
    again = _run(db)
    assert all(r["created"] is False for r in again["registered"])
    one = _run(db, dataset="rl")
    assert [r["dataset_key"] for r in one["registered"]] == ["atlas.fao.rl"] and one["archived_raw"] == 2
