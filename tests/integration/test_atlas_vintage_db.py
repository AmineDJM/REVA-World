"""Vintages layer end to end on a fresh test database (V5, PRED-VINTAGE), offline (captured fixtures).

Every vintage source is ingested in fixture mode (raw bytes archived, atlas tables registered), then read back
through ``CutoffRepository.vintages`` (the vintage-aware accessor) and ``vintage_replay`` (replay manifest).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import polars as pl
import pytest

from reva.adapters.connectors import (
    atlas_vintage_ecb_spf,
    atlas_vintage_oecd,
    atlas_vintage_philfed,
    atlas_vintage_steo,
    atlas_vintage_weo,
)
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.evaluation.cutoff import CutoffMode, CutoffRepository
from reva.evaluation.vintages import VINTAGE_DATASET_KEYS, vintage_replay

pytestmark = pytest.mark.db

SPECS = [
    *atlas_vintage_weo.atlas_source(),
    atlas_vintage_philfed.atlas_source(),
    atlas_vintage_ecb_spf.atlas_source(),
    atlas_vintage_oecd.atlas_source(),
    atlas_vintage_steo.atlas_source(),
]
ORIGINS = ("test_fixture",)


@pytest.fixture(scope="module")
def ingested(db):  # type: ignore[no-untyped-def]
    out = {}
    for spec in SPECS:
        ensure_registered(db.owner, db.store, spec)
        res = asyncio.run(run_atlas_ingest(db.settings, db.worker, db.store, spec.name, mode="fixture"))
        assert res["status"] == "completed", res
        out[spec.name] = res
    return out


def test_every_vintage_table_is_registered_from_archived_raw(db, ingested) -> None:  # type: ignore[no-untyped-def]
    keys = {r["dataset_key"] for res in ingested.values() for r in res["registered"]}
    assert keys == set(VINTAGE_DATASET_KEYS)
    assert all(not res["summary"].get("failures") for res in ingested.values())


def test_available_then_accessor_reads_the_vintage_in_force(db, ingested) -> None:  # type: ignore[no-untyped-def]
    with db.worker.connect() as conn:
        acc = CutoffRepository(
            conn, datetime(2008, 6, 1, tzinfo=UTC), CutoffMode.available_then, origins=ORIGINS
        ).vintages(db.store)
        assert acc.vintage_in_force("imf_weo")["vintage"] == "2008-04"
        assert acc.vintage_in_force("philfed_spf")["vintage"] == "2008Q2"
        assert acc.vintage_in_force("philfed_rtdsm")["vintage"] == "2008Q2"  # counts from 1 June 00:00 UTC (= cutoff)
        assert acc.vintage_in_force("ecb_spf")["vintage"] == "2008-Q1"
        assert acc.vintage_in_force("oecd_mei_rt")["vintage"] == "200805"  # May edition: from 1 June
        fc = acc.forecasts(family="imf_weo", variable="real_gdp_growth", geos=["FRA"]).rows
        assert set(fc["vintage"]) == {"2008-04"} and fc["period_start"].min() == date(2008, 1, 1)
        obs = acc.latest(family="imf_weo", variable="real_gdp_growth", geos=["FRA"])
        assert obs.rows["period_start"].max() == date(2007, 1, 1) and obs.exclusions["available_after_cutoff"] > 0
        # nothing about later vintages leaks in
        assert (
            acc.latest(include_projections=True)
            .rows.filter(pl.col("available_at") > datetime(2008, 6, 1, tzinfo=UTC))
            .height
            == 0
        )


def test_as_known_mode_admits_nothing_before_reva_recorded_it(db, ingested) -> None:  # type: ignore[no-untyped-def]
    with db.worker.connect() as conn:
        acc = CutoffRepository(conn, datetime(2008, 6, 1, tzinfo=UTC), CutoffMode.as_known, origins=ORIGINS).vintages(
            db.store
        )
        scope = acc.latest(include_projections=True)
        assert scope.rows.height == 0 and scope.exclusions.get("recorded_after_cutoff", 0) == 0
        # as_known reads the table versions REVA had recorded at the cutoff: none in 2008
        assert acc.vintage_in_force("imf_weo") is None


def test_vintage_replay_manifest(db, ingested) -> None:  # type: ignore[no-untyped-def]
    with db.worker.connect() as conn:
        m = vintage_replay(
            conn,
            db.store,
            datetime(2009, 12, 1, tzinfo=UTC),
            family="imf_weo",
            variable="real_gdp_growth",
            geos=["USA"],
            origins=ORIGINS,
        )
    assert m["mode"] == "available_then_reconstruction" and m["status"] == "ok"
    assert m["vintage_in_force"]["vintage"] == "2009-10"
    assert all(i["availability_proof"]["source_available_at"] <= "2009-12-01" for i in m["inputs"])
    assert {i["vintage"] for i in m["inputs"]} == {"2009-10"}


def test_steo_archive_vintages_as_of(db, ingested) -> None:  # type: ignore[no-untyped-def]
    """EIA STEO archive issues (V6 PRED-ENERGY): the issue in force and its projections, read as of a date."""
    with db.worker.connect() as conn:
        acc = CutoffRepository(
            conn, datetime(2014, 1, 1, tzinfo=UTC), CutoffMode.available_then, origins=ORIGINS
        ).vintages(db.store)
        assert acc.vintage_in_force("eia_steo")["vintage"] == "2013-07"
        fc = acc.forecasts(family="eia_steo", variable="eia_steo:BREPUUS").rows
        assert fc.height and fc["period_start"].min() == date(2013, 7, 1)
        hist = acc.latest(family="eia_steo", variable="eia_steo:BREPUUS").rows
        assert hist["period_start"].max() == date(2013, 6, 1)
