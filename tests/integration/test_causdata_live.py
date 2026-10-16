"""Live CAUSDATA ingest (ICB v16, COW MID 5.0/5.01, UCDP termination v4-2024 + peace agreements 22.2) into the
per-session throwaway database.

Deselected by default (``pytest -m "live and db" tests/integration/test_causdata_live.py``). Budget-metered through
``reva.atlas.ingest.run_atlas_ingest(mode="live")``: ICB 5 requests / ~1.8 MB, MID 3 / ~4.3 MB, UCDP 5 / ~2 MB.
ICB needs the ``icb.data`` budget (registry/atlas/budget_requests/causdata.yaml) merged into the pinned policy: until
then a live run is refused before any request (fail closed) and the ICB case is skipped.
"""

from __future__ import annotations

from typing import Any

import pytest

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.operations.budgets import load_budget

pytestmark = [pytest.mark.live, pytest.mark.db]


@pytest.mark.parametrize("name", ["atlas.mid", "atlas.ucdp_term", "atlas.icb"])
async def test_live_ingest(db: Any, name: str, tmp_path: Any) -> None:
    spec = get_source(name).spec
    if spec.budget_key not in load_budget().per_source_requests:
        pytest.skip(f"{spec.budget_key} has no pinned budget yet (ATLASCORE merge pending)")
    ensure_registered(db.owner, db.store, spec)
    rep = await run_atlas_ingest(
        db.settings, db.worker, db.store, name, mode="live", options={"claims_staging": str(tmp_path)}
    )
    assert rep["status"] == "completed", rep["error"]
    assert rep["registered"] and all(r["row_count"] > 0 for r in rep["registered"])
    s = rep["summary"]
    assert not s["kg"]["rejected"], s["kg"]
    assert all(v["matches"] for v in s["codebooks"].values()), s["codebooks"]
    assert s["claims"] and all(v["claims"] > 0 for v in s["claims"].values()), s["claims"]
    print(name, rep["registered"], rep["budget"], s["kg"], s["claims"])
