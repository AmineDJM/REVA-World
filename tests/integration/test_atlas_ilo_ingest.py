"""A-ILO labour atlas: fixture bytes -> archived acquisitions -> registered atlas tables (throwaway DB)."""

from __future__ import annotations

import json
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_ilo as ilo
from reva.adapters.connectors import atlas_wages_gapfill as gap
from reva.adapters.connectors.atlas_labour_common import register_connector, run_atlas_ingest
from reva.atlas.tables import read_atlas_table
from reva.evidence.runner import FixtureHttp
from reva.operations.budgets import BudgetExhausted, IngestBudget

pytestmark = pytest.mark.db

AREAS = ("FRA", "DZA", "KOS", "AUT", "TWN", "X01")


async def test_ilostat_fixture_ingest_registers_every_table(db: Any) -> None:
    conn = ilo.IlostatConnector(ref_areas=AREAS)
    register_connector(db.owner, db.store, conn.describe())
    report = await run_atlas_ingest(db.worker, db.store, conn, FixtureHttp(conn.describe().name))
    assert report.ok, report.errors
    assert {t["table"] for t in report.tables} == {f"ilostat.{i.dataset}" for i in ilo.INDICATORS}
    mw = next(t for t in report.tables if t["table"] == "ilostat.ear_inee_cur_nb_a")
    assert mw["created"] is True and mw["unmapped_provider_geo"] == ["TWN"]
    frame = read_atlas_table(db.store, mw["artifact_sha256"])
    fra = frame.filter(
        (pl.col("iso3") == "FRA")
        & (pl.col("indicator") == "EAR_INEE_CUR_NB|classif1=CUR_TYPE_LCU")
        & (pl.col("period_start").dt.year() == 2026)
    )
    assert fra["value"].to_list() == ["1867"] and fra["unit"].to_list() == ["EUR per month"]
    # every cited raw artifact is an archived test_fixture acquisition
    with db.owner.connect() as c:
        n = c.exec_driver_sql(
            "SELECT count(*) FROM evidence.acquisition WHERE origin = 'test_fixture' AND raw_sha256 = %(s)s",
            {"s": frame["raw_sha256"][0]},
        ).scalar_one()
    assert n >= 1
    # idempotent: same bytes, same tables, nothing new
    again = await run_atlas_ingest(db.worker, db.store, conn, FixtureHttp(conn.describe().name))
    assert again.ok and all(t["created"] is False for t in again.tables)


async def test_gapfill_fixture_ingest(db: Any) -> None:
    es = gap.EurostatMinimumWageConnector(geos=("FR", "AT", "EL", "UK", "DE"))
    oe = gap.OecdEarningsConnector(areas=("FRA", "USA", "OECD"))
    for c in (es, oe):
        register_connector(db.owner, db.store, c.describe())
    r1 = await run_atlas_ingest(db.worker, db.store, es, FixtureHttp(es.describe().name))
    r2 = await run_atlas_ingest(db.worker, db.store, oe, FixtureHttp(oe.describe().name))
    assert r1.ok and r2.ok, (r1.errors, r2.errors)
    t = r1.tables[0]
    frame = read_atlas_table(db.store, t["artifact_sha256"])
    aut = frame.filter(pl.col("iso3") == "AUT")
    assert aut["value"].null_count() == aut.height
    assert json.loads(aut["flags"][0])["missing"] == "not_applicable"
    assert {x["table"] for x in r2.tables} == {"oecd.earnings_mw_curp", "oecd.earnings_av_an_wage"}


async def test_live_run_without_budget_is_refused_before_any_request(db: Any) -> None:
    class Boom:
        origin = "live"

        async def get(self, url: str, **kw: Any) -> Any:  # pragma: no cover - must never be called
            raise AssertionError("request made without budget")

    conn = ilo.IlostatConnector(ref_areas=("FRA",))
    budget = IngestBudget("test", "0" * 64, 100, 10_000_000, {"worldbank.wdi": 10})  # no ilostat.rplumber entry
    with pytest.raises(BudgetExhausted):
        await run_atlas_ingest(db.worker, db.store, conn, Boom(), budget=budget)
