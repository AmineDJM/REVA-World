"""IMF atlas ingest end to end on a fresh test database (agent A-IMF), offline (captured fixtures).

Raw bytes are archived first (evidence.acquisition, origin test_fixture), then each dataset is
registered as an atlas table (evidence.dataset, concept 'atlas.table'); a second run is idempotent.
"""

from __future__ import annotations

import asyncio
import json

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_imf as m
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


class _Res:
    """The module summary of one platform run (``run_atlas_ingest(..., mode="fixture")``)."""

    def __init__(self, out: dict) -> None:
        assert out["status"] == "completed" and out["mode"] == "fixture" and out["budget"] is None
        self.out = out
        self.tables = out["summary"]["tables"]
        self.failures = out["summary"]["failures"]
        self.requests = out["summary"]["requests"]


def _run(db) -> _Res:
    return _Res(asyncio.run(run_atlas_ingest(db.settings, db.worker, db.store, m.CONNECTOR_NAME, mode="fixture")))


@pytest.fixture(scope="module")
def first(db):
    ensure_registered(db.owner, db.store, m.atlas_source())
    return _run(db)


def test_every_imf_table_registered_from_archived_raw(db, first):
    assert first.failures == []
    assert set(first.tables) == set(m.DATASETS)
    assert all(t["created"] for t in first.tables.values())
    assert first.requests == 8 and first.out["archived_raw"] == 8
    assert {r["dataset_key"] for r in first.out["registered"]} == {f"atlas.imf.{d}" for d in m.DATASETS}
    with db.owner.connect() as c:
        rows = c.execute(
            text("""SELECT dataset_key, origin, row_count, cardinality(source_acquisition_ids) AS n
                    FROM evidence.dataset WHERE dataset_key LIKE 'atlas.imf.%' ORDER BY dataset_key""")
        ).all()
        acq = c.execute(
            text("""SELECT count(*) FROM evidence.acquisition a JOIN evidence.connector_version cv
                    ON cv.id = a.connector_version_id WHERE cv.name = :n AND a.origin = 'test_fixture'"""),
            {"n": m.CONNECTOR_NAME},
        ).scalar_one()
    assert {r.dataset_key for r in rows} == {f"atlas.imf.{d}" for d in m.DATASETS}
    assert all(r.origin == "test_fixture" for r in rows)
    imts = next(r for r in rows if r.dataset_key == "atlas.imf.imts")
    assert imts.n == 2  # codelist + one reporter chunk
    assert acq == 8


def test_registered_weo_table_reads_back_with_projection_flags(db, first):
    t = first.tables["weo"]
    df = read_atlas_table(db.store, t["artifact_sha256"])
    assert df.height == t["row_count"]
    fra = df.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "BCA_NGDPD"))
    flags = {r["period_start"].year: json.loads(r["flags"]) for r in fra.to_dicts()}
    assert flags[2026]["estimate_kind"] == "projection" and "estimate_kind" not in flags[2025]
    assert t["coverage"]["unmapped_provider_geo_count"] == 2  # G001, TWN kept
    assert "WEO October 2026" in t["coverage"]["coverage_note"]


def test_one_dataset_run_fetches_only_that_dataset(db, first):
    out = asyncio.run(run_atlas_ingest(db.settings, db.worker, db.store, m.CONNECTOR_NAME, dataset="er", mode="fixture"))
    assert out["archived_raw"] == 1 and set(out["summary"]["tables"]) == {"er"}
    assert out["summary"]["tables"]["er"]["created"] is False


def test_reingest_is_idempotent(db, first):
    second = _run(db)
    assert second.failures == []
    assert not any(t["created"] for t in second.tables.values())
    assert {d: t["artifact_sha256"] for d, t in second.tables.items()} == {
        d: t["artifact_sha256"] for d, t in first.tables.items()
    }
