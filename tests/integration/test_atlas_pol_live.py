"""Live A-POL ingest (V-Dem, LIED, BMR, IPU) into the per-session throwaway database.

Deselected by default (``pytest -m "live and db" tests/integration/test_atlas_pol_live.py``). Budget-metered
through ``reva.atlas.ingest.run_atlas_ingest(mode="live")``: 11 requests, ~85 MB.
"""

from __future__ import annotations

import pytest

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source

pytestmark = [pytest.mark.live, pytest.mark.db]


@pytest.mark.parametrize("name", ["atlas.lied", "atlas.bmr", "atlas.ipu", "atlas.vdem"])
async def test_live_ingest(db, name: str) -> None:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    rep = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="live")
    assert rep["status"] == "completed", rep["error"]
    assert rep["registered"] and all(r["row_count"] > 0 for r in rep["registered"])
    print(name, rep["registered"], rep["budget"])
