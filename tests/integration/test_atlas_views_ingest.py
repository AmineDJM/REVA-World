"""VIEWS forecasts end to end on the per-session throwaway database (atlas platform, fixture mode), then the conflict
issue path reading them (V6 PRED-CONFLICT).

The fixture (tests/fixtures/source/atlas.views) holds real VIEWS bytes restricted by the API's ``iso`` filter to SDN,
COL and NOR for two runs; the run options select exactly those requests.
"""

from __future__ import annotations

import json
from typing import Any

import polars as pl
import pytest

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table
from reva.evaluation.conflict.views import latest_covering, runs_from_frame

pytestmark = pytest.mark.db

OPTIONS = {"runs": "fatalities001_2022_12_t01,fatalities003_2026_08_t01", "iso": "SDN,COL,NOR"}


async def test_views_fixture_ingest(db: Any) -> None:
    ensure_registered(db.owner, db.store, get_source("atlas.views").spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.views", mode="fixture", options=OPTIONS)
    assert out["status"] == "completed", out
    s = out["summary"]
    t = s["tables"]["cm_sb"]
    assert t["rows"] == 2 * 2 * 3 * 36 and t["created"]
    with db.worker.connect() as conn:
        from sqlalchemy import text

        r = conn.execute(
            text("SELECT artifact_sha256, origin FROM evidence.dataset WHERE dataset_key = 'atlas.views.cm_sb'")
        ).one()
    assert r.origin == "test_fixture"
    f = read_atlas_table(db.store, r.artifact_sha256)
    assert all(json.loads(x)["estimate_kind"] == "projection" for x in f["flags"])
    runs = runs_from_frame(f)
    # the 2022-12 run (fatalities001, ln(fatalities+1)) covers 2023; the 2026-08 run covers 2027
    assert runs["fatalities001_2022_12_t01"].annual("gw:625", 2023) is not None
    vr = latest_covering(runs, 2027, "2026-10-17")
    assert vr is not None and vr.run == "fatalities003_2026_08_t01"
    sdn = f.filter((pl.col("provider_geo") == "gw:625") & (pl.col("indicator").str.ends_with("|main_mean")))
    assert sdn.height == 36
    # a second run with the same bytes registers nothing new
    again = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.views", mode="fixture", options=OPTIONS)
    assert again["status"] == "completed" and not again["summary"]["tables"]["cm_sb"]["created"]
