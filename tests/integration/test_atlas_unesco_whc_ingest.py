"""UNESCO World Heritage List -> atlas table, end to end on a throwaway database (fixture bytes)."""

from __future__ import annotations

import pytest

from reva.adapters.connectors import atlas_unesco_whc as whc
from reva.atlas.tables import read_atlas_table
from reva.evidence.runner import FixtureHttp

pytestmark = pytest.mark.db


def _register_connector(db) -> None:
    from reva.adapters.postgres import repositories as repo
    from reva.cli.seed import register_connector_version

    with db.owner.begin() as conn:
        act = repo.simple_activity("registry.seed", "review", "system:kgcult", {"connector": whc.CONNECTOR_NAME})
        repo.record_activity(conn, act)
        register_connector_version(conn, db.store, whc.UnescoWhcConnector().describe(), act.id)


async def test_ingest_registers_atlas_table(db):
    _register_connector(db)
    summary = await whc.ingest(db.worker, db.store, FixtureHttp(whc.CONNECTOR_NAME))
    assert summary["dataset_key"] == "atlas.unesco_whc.whc001"
    assert summary["created"] is True
    assert [u["provider_geo"] for u in summary["unmapped_provider_geo"]] == [
        "states_names:Jerusalem (Site proposed by Jordan)"
    ]
    frame = read_atlas_table(db.store, summary["artifact_sha256"])
    bra = frame.filter((frame["iso3"] == "BRA") & (frame["indicator"] == "whc_properties_total"))
    assert bra["value"].to_list() == ["26"]
    # idempotent: the same bytes do not create a new version
    again = await whc.ingest(db.worker, db.store, FixtureHttp(whc.CONNECTOR_NAME))
    assert again["created"] is False and again["artifact_sha256"] == summary["artifact_sha256"]


@pytest.mark.live
async def test_live_ingest_into_throwaway_database(db):
    """One real, budgeted ingest (2 requests) into the per-session throwaway database."""
    from reva.adapters.http.safe_client import SafeHttpClient
    from reva.operations.budgets import BudgetExhausted, load_budget, metered, reserve_ingest, settle_metered

    if whc.SOURCE_KEY not in load_budget().per_source_requests:
        pytest.skip("unesco.whc has no daily budget yet (registry/atlas/budget_requests/kgcult.yaml not merged)")
    _register_connector(db)
    try:
        reservation = reserve_ingest(db.worker, job_id="kgcult-live-whc", attempt=1, source_key=whc.SOURCE_KEY,
                                     planned_requests=2)
    except BudgetExhausted as e:  # pragma: no cover - depends on today's usage
        pytest.skip(f"budget refused: {e}")
    client = SafeHttpClient(allowed_hosts=(whc.HOST,), timeout_seconds=60, max_bytes=whc.MAX_EXPORT_BYTES,
                            proxy_mode=db.settings.reva_http_proxy_mode)
    http = metered(client, db.worker, reservation)
    try:
        summary = await whc.ingest(db.worker, db.store, http)
    finally:
        settle_metered(db.worker, http)
        await client.aclose()
    assert summary["origin"] == "live" and summary["row_count"] > 1000
    assert summary["coverage"]["countries"] >= 160
