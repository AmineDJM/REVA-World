"""Live A-TREATIES ingest (UNTC 53 status pages + terms, OHCHR dashboard API) into the per-session throwaway database.

Deselected by default (``pytest -m "live and db" tests/integration/test_atlas_treaties_live.py``). Budget-metered
through ``reva.atlas.ingest.run_atlas_ingest(mode="live")``: UNTC 54 requests (~26 MB), OHCHR 20 (~4.5 MB).
"""

from __future__ import annotations

import json

import pytest

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source

pytestmark = [pytest.mark.live, pytest.mark.db]


@pytest.mark.parametrize("name", ["atlas.untc", "atlas.ohchr"])
async def test_live_ingest(db, name: str) -> None:  # type: ignore[no-untyped-def]
    ensure_registered(db.owner, db.store, get_source(name).spec)
    rep = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="live")
    assert rep["status"] == "completed", rep["error"]
    assert rep["registered"] and all(r["row_count"] > 0 for r in rep["registered"])
    kg = rep["summary"].get("kg")
    if kg is not None:
        assert not kg.get("rejected"), kg
    print(name, json.dumps(rep, default=str)[:20000])
