"""Live KGGEOPOL ingest (UCDP 26.1, Voeten UNGA votes v33.0, CShapes 2.0) into the per-session throwaway database.

Deselected by default (``pytest -m "live and db" tests/integration/test_atlas_geopol_live.py``). Budget-metered
through ``reva.atlas.ingest.run_atlas_ingest(mode="live")``: 11 requests, ~15 MB; also loads the KG statements
(~10k UCDP, ~950k UNGA votes, ~2k CShapes).
"""

from __future__ import annotations

import pytest

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source

pytestmark = [pytest.mark.live, pytest.mark.db]


@pytest.mark.parametrize("name", ["atlas.ucdp", "atlas.cshapes", "atlas.unga_votes"])
async def test_live_ingest(db, name: str) -> None:  # type: ignore[no-untyped-def]
    ensure_registered(db.owner, db.store, get_source(name).spec)
    rep = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="live")
    assert rep["status"] == "completed", rep["error"]
    assert rep["registered"] and all(r["row_count"] > 0 for r in rep["registered"])
    kg = rep["summary"].get("kg")
    assert kg is not None and not kg["rejected"], kg
    print(name, rep["registered"], rep["budget"], kg)
