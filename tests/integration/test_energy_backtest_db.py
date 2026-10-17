"""STEO archive vintages + current STEO -> energy backtest end to end on a fresh test database (V6, PRED-ENERGY).

Offline: both connectors run in fixture mode (raw bytes archived, atlas tables registered), then
``reva.evaluation.energy_backtests.load_and_run`` reads them back (vintage accessor at each issue's availability
instant; outcomes from the current STEO observed table).
"""

from __future__ import annotations

import asyncio

import pytest

from reva.adapters.connectors import atlas_eia_steo, atlas_vintage_steo
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.evaluation import energy_backtests as eb

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def ingested(db):  # type: ignore[no-untyped-def]
    for spec in (atlas_eia_steo.atlas_source(), atlas_vintage_steo.atlas_source()):
        ensure_registered(db.owner, db.store, spec)
        res = asyncio.run(run_atlas_ingest(db.settings, db.worker, db.store, spec.name, mode="fixture"))
        assert res["status"] == "completed", res
    return True


def test_backtest_reads_vintages_and_scores_against_current_history(db, ingested) -> None:  # type: ignore[no-untyped-def]
    with db.worker.connect() as conn:
        res = eb.load_and_run(conn, db.store, origin="test_fixture")
    assert [o["vintage"] for o in res["origins"]] == ["2013-07", "2026-10"]
    assert res["origins"][0]["available_at"].startswith("2013-07-10")
    # the fixture bulk file holds BREPUUS/WTIPUUS monthly history: July 2013 issue forecasts are scored
    brent = [r for r in res["records"] if r["code"] == "BREPUUS" and r["origin"] == "2013-07"]
    assert {r["model"] for r in brent} >= {"rw", "steo"}
    assert all(r["target"] >= "2013-08" for r in brent)
    assert res["outcome_basis"].startswith("EIA STEO current published history")
    assert res["digest"] == eb.result_digest({k: v for k, v in res.items() if k != "digest"})
