"""A-TRADE atlas connectors end to end on a throwaway database (fixture bytes, no network).

BACI is read by byte ranges of a rebuilt excerpt zip (ArchiveRangeFixtureHttp), UNCTADstat bulk 7z
files and OECD-WTO BaTIS SDMX-CSV are replayed by FixtureHttp. Raw bytes are archived as evidence
acquisitions of the atlas connector versions, tables registered, and a second run is a no-op.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_baci, atlas_trade_batis, atlas_unctad
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import read_atlas_table
from reva.operations import budgets as B

pytestmark = pytest.mark.db


async def _run(db: Any, module: Any, **kw: Any) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, module.atlas_source())
    out = await run_atlas_ingest(db.settings, db.worker, db.store, module.CONNECTOR_NAME, mode="fixture", **kw)
    assert out["status"] == "completed" and out["budget"] is None
    assert out["summary"]["ok"], out["summary"]["errors"]
    return out["summary"]  # type: ignore[no-any-return]


def _tables(rep: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["table"]: t for t in rep["tables"]}


def _one(frame: pl.DataFrame, geo: str, indicator: str, year: int) -> dict[str, Any]:
    rows = frame.filter(
        (pl.col("provider_geo") == geo)
        & (pl.col("indicator") == indicator)
        & (pl.col("period_start") == date(year, 1, 1))
    ).to_dicts()
    assert len(rows) == 1, (geo, indicator, year, rows)
    return rows[0]


async def test_baci_byte_range_ingest(db: Any) -> None:
    rep = await _run(db, atlas_baci)
    tabs = _tables(rep)
    assert set(tabs) == {f"baci.{d}" for d in atlas_baci.DATASETS if d != "hs92_pair_section_1995_2015"}
    assert rep["years"] == [2023, 2024]
    assert all(t["unmapped_provider_geo"] == ["490"] for t in tabs.values())
    country = read_atlas_table(db.store, tabs["baci.hs92_country"]["artifact_sha256"])
    x = _one(country, "251", "X|section=TOTAL", 2024)
    m = _one(country, "251", "M|section=TOTAL", 2024)
    bal = _one(country, "251", "BAL|section=TOTAL", 2024)
    assert x["iso3"] == "FRA" and Decimal(bal["value"]) == Decimal(x["value"]) - Decimal(m["value"])
    assert json.loads(bal["flags"])["derived"] == "exports_minus_imports"
    assert bal["raw_sha256"] == x["raw_sha256"]  # same year member
    with db.owner.connect() as c:
        n = c.execute(
            text("SELECT count(*) FROM evidence.acquisition WHERE origin = 'test_fixture' AND raw_sha256 = :s"),
            {"s": x["raw_sha256"]},
        ).scalar_one()
    assert n == 1
    pair = read_atlas_table(db.store, tabs["baci.hs92_pair_total"]["artifact_sha256"])
    row = _one(pair, "842", "X|partner=156|section=TOTAL", 2023)
    assert row["iso3"] == "USA" and json.loads(row["flags"])["partner_iso3"] == "CHN"
    again = await _run(db, atlas_baci)
    assert all(t["created"] is False for t in again["tables"])
    only = await _run(db, atlas_baci, dataset="hs92_pair_hs2")
    assert [t["table"] for t in only["tables"]] == ["baci.hs92_pair_hs2"]


async def test_unctad_bulk_ingest(db: Any) -> None:
    rep = await _run(db, atlas_unctad)
    tabs = _tables(rep)
    assert set(tabs) == {f"unctad.{s.dataset}" for s in atlas_unctad.SETS}
    svc = read_atlas_table(db.store, tabs["unctad.services_cat"]["artifact_sha256"])
    fra = svc.filter(pl.col("iso3") == "FRA")
    assert fra.height > 0
    assert "158" in tabs["unctad.services_cat"]["unmapped_provider_geo"]
    assert "0000" in tabs["unctad.services_cat"]["unmapped_provider_geo"]
    again = await _run(db, atlas_unctad)
    assert all(t["created"] is False for t in again["tables"])


async def test_batis_ingest_and_budget_refusal(db: Any, monkeypatch: Any) -> None:
    rep = await _run(db, atlas_trade_batis)
    tab = _tables(rep)["oecd.batis_services"]
    frame = read_atlas_table(db.store, tab["artifact_sha256"])
    assert set(frame.filter(pl.col("iso3").is_not_null())["iso3"].unique().to_list()) >= {"FRA", "USA", "XKX"}
    assert rep["edition"] == "December2025"
    # a live run for a source without a budget is refused before any request
    pinned = B.load_budget()
    budget = B.IngestBudget(
        pinned.policy_version,
        pinned.policy_sha256,
        pinned.total_requests,
        pinned.total_bytes,
        {k: v for k, v in pinned.per_source_requests.items() if k != atlas_baci.SOURCE_KEY},
    )
    monkeypatch.setattr(B, "load_budget", lambda policy=None: budget)

    def factory(spec: Any, settings: Any) -> Any:  # pragma: no cover - must never be called
        raise AssertionError("client built without budget")

    with pytest.raises(B.BudgetExhausted):
        await run_atlas_ingest(db.settings, db.worker, db.store, atlas_baci.CONNECTOR_NAME, http_factory=factory)
