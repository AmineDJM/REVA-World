"""atlas.fed_nowcasts through the atlas entry point (fixture mode, throwaway test database): five vintage tables
registered (idempotent re-run) and read back through the vintage accessor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_fed_nowcasts as F
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.evaluation.vintages import VintageAccessor, load_vintage_cells

pytestmark = pytest.mark.db

KEYS = {f"atlas.fed_nowcasts.{d}" for d in F.DATASETS}


async def _run(db: Any) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(F.NAME).spec)
    return await run_atlas_ingest(db.settings, db.worker, db.store, F.NAME, mode="fixture")


async def test_fixture_ingest_registers_and_reads_back(db: Any) -> None:
    out = await _run(db)
    assert out["status"] == "completed", out
    assert {r["dataset_key"] for r in out["registered"]} == KEYS
    again = await _run(db)
    assert all(r["created"] is False for r in again["registered"])
    with db.worker.connect() as conn:
        cells = load_vintage_cells(conn, db.store, origin="test_fixture", dataset_keys=sorted(KEYS))
    assert cells.height > 0
    acc = VintageAccessor(cells, datetime(2014, 4, 26, tzinfo=UTC))
    fc = acc.forecasts(family="atl_gdpnow").rows
    assert fc.height > 0 and max(fc["available_at"].to_list()) <= datetime(2014, 4, 26, tzinfo=UTC)
    assert fc.filter(pl.col("period_start") > datetime(2014, 4, 26).date()).height == 0
