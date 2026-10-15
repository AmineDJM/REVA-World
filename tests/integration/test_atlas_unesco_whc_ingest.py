"""UNESCO World Heritage List -> atlas table through the atlas ingest entry point (throwaway database)."""

from __future__ import annotations

import pytest

from reva.adapters.connectors import atlas_unesco_whc as whc
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


def _ensure(db) -> None:
    from reva.atlas.sources import ensure_registered

    ensure_registered(db.owner, db.store, whc.atlas_source())


async def test_fixture_ingest_registers_atlas_table(db):
    from reva.atlas.ingest import run_atlas_ingest

    _ensure(db)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, whc.CONNECTOR_NAME, mode="fixture")
    assert out["status"] == "completed" and out["archived_raw"] == 2
    summary = out["summary"]
    assert summary["dataset_key"] == "atlas.unesco_whc.whc001" and summary["created"] is True
    assert summary["properties"] == 1273
    assert [u["provider_geo"] for u in summary["unmapped_provider_geo"]] == [
        "states_names:Jerusalem (Site proposed by Jordan)"
    ]
    frame = read_atlas_table(db.store, summary["artifact_sha256"])
    bra = frame.filter((frame["iso3"] == "BRA") & (frame["indicator"] == "whc_properties_total"))
    assert bra["value"].to_list() == ["26"]
    again = await run_atlas_ingest(db.settings, db.worker, db.store, whc.CONNECTOR_NAME, mode="fixture")
    assert again["summary"]["created"] is False
    assert again["summary"]["artifact_sha256"] == summary["artifact_sha256"]


@pytest.mark.live
async def test_live_ingest_into_throwaway_database(db):
    """One real, budgeted ingest (2 requests) into the per-session throwaway database."""
    from reva.atlas.ingest import run_atlas_ingest
    from reva.operations.budgets import load_budget

    if whc.SOURCE_KEY not in load_budget().per_source_requests:
        pytest.skip("unesco.whc has no daily budget yet (registry/atlas/budget_requests/kgcult.yaml not merged)")
    _ensure(db)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, whc.CONNECTOR_NAME, mode="live")
    assert out["status"] == "completed", out
    assert out["summary"]["properties"] >= 1000
    assert out["budget"]["source_key"] == whc.SOURCE_KEY
