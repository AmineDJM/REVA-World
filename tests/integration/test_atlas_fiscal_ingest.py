"""A-FISCAL atlas sources through the ATLASCORE entry point (fixture mode, throwaway test database).

Fixture bytes -> archived acquisitions of the atlas connector versions -> registered atlas tables; idempotent
re-run; single-dataset run; a live run without a budget for the source is refused before any request.
"""

from __future__ import annotations

import json
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_bis as bis
from reva.adapters.connectors import atlas_oecd_fiscal as of
from reva.adapters.connectors import atlas_pisa as pz
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table
from reva.operations import budgets as B

pytestmark = pytest.mark.db


def _register(db: Any, name: str) -> None:
    ensure_registered(db.owner, db.store, get_source(name).spec)


def _table(out: dict[str, Any], key: str) -> dict[str, Any]:
    return next(t for t in out["summary"]["tables"] if t["table"] == key)


@pytest.mark.parametrize("mod", [of, pz, bis])
async def test_fixture_ingest_registers_every_table(db: Any, mod: Any) -> None:
    _register(db, mod.CONNECTOR_NAME)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, mod.CONNECTOR_NAME, mode="fixture")
    assert out["status"] == "completed" and out["summary"]["ok"], out["summary"]["errors"]
    src = "bis" if mod is bis else "oecd"
    assert {t["table"] for t in out["summary"]["tables"]} == {f"{src}.{f.dataset}" for f in mod.FLOWS}
    assert all(t["created"] for t in out["summary"]["tables"])
    again = await run_atlas_ingest(db.settings, db.worker, db.store, mod.CONNECTOR_NAME, mode="fixture")
    assert again["summary"]["ok"] and all(t["created"] is False for t in again["summary"]["tables"])


async def test_revenue_table_content_and_single_dataset_run(db: Any) -> None:
    _register(db, of.CONNECTOR_NAME)
    one = await run_atlas_ingest(
        db.settings, db.worker, db.store, of.CONNECTOR_NAME, mode="fixture", dataset="rs_global"
    )
    assert [t["table"] for t in one["summary"]["tables"]] == ["oecd.rs_global"]
    assert all(f["item"].startswith("data:DSD_REV_COMP_GLOBAL") for f in one["summary"]["fetched"])
    t = _table(one, "oecd.rs_global")
    assert t["unmapped_provider_geo"] == ["A9"]
    frame = read_atlas_table(db.store, t["artifact_sha256"])
    fra = frame.filter(
        (pl.col("iso3") == "FRA")
        & (pl.col("indicator") == "TAX_REV|sector=S13|standard_revenue=_T|unit_measure=PT_B1GQ")
    )
    assert fra.height >= 5 and fra["value"].null_count() == 0
    with db.owner.connect() as c:
        n = c.exec_driver_sql(
            "SELECT count(*) FROM evidence.acquisition WHERE origin = 'test_fixture' AND raw_sha256 = %(s)s",
            {"s": frame["raw_sha256"][0]},
        ).scalar_one()
    assert n >= 1
    assert json.loads(frame["selector"][0])["file"].startswith("DSD_REV_COMP_GLOBAL@DF_RSGLOBAL.")


async def test_live_run_without_budget_is_refused_before_any_request(db: Any, monkeypatch: Any) -> None:
    pinned = B.load_budget()
    no_bis = {k: v for k, v in pinned.per_source_requests.items() if k != "bis.stats"}
    budget = B.IngestBudget(
        pinned.policy_version, pinned.policy_sha256, pinned.total_requests, pinned.total_bytes, no_bis
    )
    monkeypatch.setattr(B, "load_budget", lambda policy=None: budget)

    def factory(spec: Any, settings: Any) -> Any:  # pragma: no cover - must never be called
        raise AssertionError("client built without budget")

    _register(db, bis.CONNECTOR_NAME)
    with pytest.raises(B.BudgetExhausted):
        await run_atlas_ingest(db.settings, db.worker, db.store, bis.CONNECTOR_NAME, http_factory=factory)
