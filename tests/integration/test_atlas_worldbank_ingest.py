"""World Bank atlas source end to end in a throwaway database (fixture replay, no network).

``run_atlas_ingest(..., "atlas.worldbank", mode="fixture")`` archives every recorded slice as an
``evidence.acquisition`` (test_fixture origin) before parsing, registers one atlas table per
database (WDI split in two) and is idempotent on a re-run.
"""

from __future__ import annotations

import asyncio

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_worldbank as wb
from reva.adapters.connectors.atlas_worldbank_catalog import catalog_from_archive
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db

EXPECTED = {"wdi_econ", "wdi_social", "wgi", "gender", "hnp", "edstats", "jobs", "doingbusiness", "ids", "pip"}


def _run(db, dataset=None):  # type: ignore[no-untyped-def]
    return asyncio.run(
        run_atlas_ingest(db.settings, db.worker, db.store, wb.CONNECTOR_NAME, dataset=dataset, mode="fixture")
    )


def test_fixture_ingest_registers_every_table_and_is_idempotent(db) -> None:  # type: ignore[no-untyped-def]
    ensure_registered(db.owner, db.store, get_source(wb.CONNECTOR_NAME).spec)
    out = _run(db)
    assert out["status"] == "completed", out
    s = out["summary"]
    assert s["blocked"] == []
    assert set(s["tables"]) == EXPECTED
    assert out["archived_raw"] == len(wb.fetch_items())  # 8 ZIPs + 8 French lists + 3 PIP files
    assert out["budget"] is None  # fixture replays are never charged
    with db.worker.connect() as c:
        n = c.execute(
            text("SELECT count(*) FROM evidence.acquisition WHERE origin = 'test_fixture' AND raw_sha256 = ANY(:s)"),
            {"s": [a["raw_sha256"] for a in s["acquisitions"].values()]},
        ).scalar_one()
        rows = c.execute(
            text("SELECT dataset_key, artifact_sha256 FROM evidence.dataset WHERE dataset_key LIKE 'atlas.wb.%'")
        ).all()
    assert n == len(s["acquisitions"])
    assert {r[0] for r in rows} == {f"atlas.wb.{d}" for d in EXPECTED}
    wgi = read_atlas_table(db.store, s["tables"]["wgi"]["artifact_sha256"])
    assert wgi.filter(pl.col("provider_geo") == "TWN")["iso3"].is_null().all()
    assert s["tables"]["wgi"]["unmapped_provider_geo"] == [
        {"provider_geo": "ANT", "rows": wgi.filter(pl.col("provider_geo") == "ANT").height},
        {"provider_geo": "TWN", "rows": wgi.filter(pl.col("provider_geo") == "TWN").height},
    ]
    wdi = read_atlas_table(db.store, s["tables"]["wdi_social"]["artifact_sha256"])
    assert not wdi["provider_geo"].is_in(["WLD", "HIC"]).any()
    # The catalog is rebuilt from the archived raw bytes (not from a local copy).
    doc, stats = catalog_from_archive(db.worker, db.store, origin="test_fixture")
    assert stats["concepts"] == len(doc["concepts"]) > 0
    assert set(stats["raw_sha256s"].values()) <= {a["raw_sha256"] for a in s["acquisitions"].values()}
    # Re-run: same bytes -> same acquisitions and same table versions.
    again = _run(db)
    assert all(t["created"] is False for t in again["summary"]["tables"].values())


def test_single_dataset_run_only_registers_that_table(db) -> None:  # type: ignore[no-untyped-def]
    ensure_registered(db.owner, db.store, get_source(wb.CONNECTOR_NAME).spec)
    out = _run(db, dataset="wgi")
    assert set(out["summary"]["tables"]) == {"wgi"}
    assert set(out["summary"]["acquisitions"]) == {"bulk:wgi", "fr:wgi"}
