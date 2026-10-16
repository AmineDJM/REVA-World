"""A-ILO labour atlas through the ATLASCORE entry point (fixture mode, throwaway DB).

Fixture bytes -> archived acquisitions of the atlas connector versions -> registered atlas tables;
a live run without a budget for the source is refused before any request.
"""

from __future__ import annotations

import json
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_ilo as ilo
from reva.adapters.connectors import atlas_wages_gapfill as gap
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table
from reva.operations import budgets as B

pytestmark = pytest.mark.db


def _register(db: Any, name: str) -> None:
    ensure_registered(db.owner, db.store, get_source(name).spec)


def _table(out: dict[str, Any], key: str) -> dict[str, Any]:
    return next(t for t in out["summary"]["tables"] if t["table"] == key)


async def test_ilostat_fixture_ingest_registers_every_table(db: Any) -> None:
    _register(db, ilo.CONNECTOR_NAME)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, ilo.CONNECTOR_NAME, mode="fixture")
    assert out["status"] == "completed" and out["summary"]["ok"], out["summary"]["errors"]
    assert {t["table"] for t in out["summary"]["tables"]} == {f"ilostat.{i.dataset}" for i in ilo.INDICATORS}
    mw = _table(out, "ilostat.ear_inee_cur_nb_a")
    assert mw["created"] is True and mw["unmapped_provider_geo"] == []  # TWN: crosswalk entity since 1.1.0
    frame = read_atlas_table(db.store, mw["artifact_sha256"])
    fra = frame.filter(
        (pl.col("iso3") == "FRA")
        & (pl.col("indicator") == "EAR_INEE_CUR_NB|classif1=CUR_TYPE_LCU")
        & (pl.col("period_start").dt.year() == 2026)
    )
    assert fra["value"].to_list() == ["1867"] and fra["unit"].to_list() == ["EUR per month"]
    with db.owner.connect() as c:
        n = c.exec_driver_sql(
            "SELECT count(*) FROM evidence.acquisition WHERE origin = 'test_fixture' AND raw_sha256 = %(s)s",
            {"s": frame["raw_sha256"][0]},
        ).scalar_one()
    assert n >= 1
    # idempotent: same bytes, same tables, nothing new
    again = await run_atlas_ingest(db.settings, db.worker, db.store, ilo.CONNECTOR_NAME, mode="fixture")
    assert again["summary"]["ok"] and all(t["created"] is False for t in again["summary"]["tables"])
    # --dataset: only that table (plus the shared dictionaries) is fetched and registered
    one = await run_atlas_ingest(
        db.settings, db.worker, db.store, ilo.CONNECTOR_NAME, mode="fixture", dataset="ilr_tumt_noc_rt_a"
    )
    assert [t["table"] for t in one["summary"]["tables"]] == ["ilostat.ilr_tumt_noc_rt_a"]
    assert not any(f["item"].startswith("data:UNE") for f in one["summary"]["fetched"])


async def test_gapfill_fixture_ingest(db: Any) -> None:
    for name in (gap.EUROSTAT_NAME, gap.OECD_NAME):
        _register(db, name)
    r1 = await run_atlas_ingest(db.settings, db.worker, db.store, gap.EUROSTAT_NAME, mode="fixture")
    r2 = await run_atlas_ingest(db.settings, db.worker, db.store, gap.OECD_NAME, mode="fixture")
    assert r1["summary"]["ok"] and r2["summary"]["ok"], (r1["summary"]["errors"], r2["summary"]["errors"])
    frame = read_atlas_table(db.store, _table(r1, "eurostat.earn_mw_cur")["artifact_sha256"])
    aut = frame.filter(pl.col("iso3") == "AUT")
    assert aut.height > 0 and aut["value"].null_count() == aut.height
    assert json.loads(aut["flags"][0])["missing"] == "not_applicable"
    assert {t["table"] for t in r2["summary"]["tables"]} == {"oecd.earnings_mw_curp", "oecd.earnings_av_an_wage"}


async def test_live_run_without_budget_is_refused_before_any_request(db: Any, monkeypatch: Any) -> None:
    pinned = B.load_budget()
    no_ilo = {k: v for k, v in pinned.per_source_requests.items() if k != "ilostat.rplumber"}
    budget = B.IngestBudget(
        pinned.policy_version, pinned.policy_sha256, pinned.total_requests, pinned.total_bytes, no_ilo
    )
    monkeypatch.setattr(B, "load_budget", lambda policy=None: budget)

    def factory(spec: Any, settings: Any) -> Any:  # pragma: no cover - must never be called
        raise AssertionError("client built without budget")

    _register(db, ilo.CONNECTOR_NAME)
    with pytest.raises(B.BudgetExhausted):
        await run_atlas_ingest(db.settings, db.worker, db.store, ilo.CONNECTOR_NAME, http_factory=factory)
