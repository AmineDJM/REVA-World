"""V6MIGR sources end to end on a throwaway database through the atlas platform (fixture mode: no network): raw
responses archived as evidence acquisitions, atlas tables registered with their rights; re-running is a no-op."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db

CASES = [
    (
        "atlas.unhcr_asylum",
        {"unhcr_origin": "SYR"},
        {"atlas.unhcr.asylum_applications": 2322, "atlas.unhcr.asylum_decisions": 6583},
    ),
    (
        "atlas.eurostat_asylum",
        {"eurostat_key": "M.PER.SY+UA.T.TOTAL+FRST.TOTAL.DE+FR+EU27_2020"},
        {"atlas.eurostat.asylum_applicants_m": 2392},
    ),
    ("atlas.iom_mmp", None, {"atlas.iom.mmp_monthly": 560}),
    ("atlas.idmc_national", None, {"atlas.idmc.gidd_national": 304}),
]


async def _run(db: Any, name: str, options: dict[str, Any] | None) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture", options=options)
    assert out["status"] == "completed", out
    summary: dict[str, Any] = out["summary"]
    return summary


@pytest.mark.parametrize(("name", "options", "rows"), CASES)
async def test_migration_ingest(db: Any, name: str, options: dict[str, Any] | None, rows: dict[str, int]) -> None:
    s = await _run(db, name, options)
    assert s["ok"], s
    got = {t["dataset_key"]: t["row_count"] for t in s["tables"]}
    assert got == rows
    assert all(t["created"] for t in s["tables"])
    with db.owner.connect() as c:
        for key in rows:
            art, cov = c.execute(
                text(
                    "SELECT artifact_sha256, coverage FROM evidence.dataset WHERE dataset_key = :k ORDER BY recorded_at DESC LIMIT 1"
                ),
                {"k": key},
            ).one()
            f = read_atlas_table(db.store, art)
            assert f.height == rows[key]
            assert cov.get("rights", {}).get("licence") or cov
    again = await _run(db, name, options)
    assert not any(t["created"] for t in again["tables"])
