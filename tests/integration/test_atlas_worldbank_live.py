"""Live World Bank atlas check (``-m live``; network): the WGI bulk ZIP + its French metadata,
budget-metered through ``reva.atlas.ingest``, into the session's throwaway database.

The full live ingest (all 8 databases + PIP, ~680 MB) is an operator run
(``uv run python -m reva.atlas.ingest atlas.worldbank``); its results are in docs/dev/v3_a_wb.md.
"""

from __future__ import annotations

import asyncio

import pytest

from reva.adapters.connectors import atlas_worldbank as wb
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.operations.budgets import BudgetExhausted

pytestmark = [pytest.mark.db, pytest.mark.live]


def test_live_wgi_bulk_ingest(db) -> None:  # type: ignore[no-untyped-def]
    ensure_registered(db.owner, db.store, get_source(wb.CONNECTOR_NAME).spec)
    try:
        out = asyncio.run(
            run_atlas_ingest(db.settings, db.worker, db.store, wb.CONNECTOR_NAME, dataset="wgi", mode="live")
        )
    except BudgetExhausted as e:
        if e.problem().get("details", {}).get("reason") == "unknown_source":
            pytest.skip("worldbank.atlas is not yet in registry/policies/budgets.yaml (budget request a_wb.yaml)")
        raise
    assert out["status"] == "completed", out
    s = out["summary"]
    assert s["blocked"] == [], s["blocked"]
    t = s["tables"]["wgi"]
    assert t["countries"] >= 200 and t["indicators"] >= 30
    assert {u["provider_geo"] for u in t["unmapped_provider_geo"]} <= {"TWN", "ANT"}
    assert out["budget"]["consumed_requests"] == 2
