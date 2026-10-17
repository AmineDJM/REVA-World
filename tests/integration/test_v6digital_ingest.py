"""V6DIGITAL fixture ingests: Access Now STOP and CFR Cyber Operations Tracker -> atlas tables + KG events."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_cfr_cyber as cfr
from reva.adapters.connectors import atlas_keepiton as kio
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db

EXPECTED = {
    kio.CONNECTOR_NAME: {"atlas.keepiton.shutdowns_cy", "atlas.keepiton.shutdowns"},
    cfr.CONNECTOR_NAME: {"atlas.cfr_cyber.sponsor_year"},
}


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    return await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")


@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_fixture_ingest_registers_tables_and_events(db: Any, name: str) -> None:
    out = await _run(db, name)
    assert out["status"] == "completed", out
    assert {r["dataset_key"] for r in out["registered"]} == EXPECTED[name]
    assert out["summary"]["kg"]["status"] == "loaded", out["summary"]["kg"]
    again = await _run(db, name)
    assert all(r["created"] is False for r in again["registered"])


async def test_events_and_rights(db: Any) -> None:
    await _run(db, kio.CONNECTOR_NAME)
    await _run(db, cfr.CONNECTOR_NAME)
    with db.owner.connect() as c:
        kinds = dict(
            c.execute(
                text(
                    "SELECT kind, count(*) FROM kg.entity WHERE origin = 'test_fixture' "
                    "AND (qid LIKE 'keepiton:shutdown:%' OR qid LIKE 'cfr_cyber:operation:%') GROUP BY kind"
                )
            ).all()
        )
        assert kinds == {"internet_shutdown": 15, "cyber_operation": 40}
        n = c.execute(
            text(
                "SELECT count(*) FROM kg.statement WHERE origin = 'test_fixture' "
                "AND property_pid = 'cfr_cyber:suspected_state_sponsor'"
            )
        ).scalar_one()
        assert n == 58
        sha = c.execute(
            text(
                "SELECT artifact_sha256 FROM evidence.dataset WHERE dataset_key = 'atlas.cfr_cyber.sponsor_year' "
                "ORDER BY recorded_at DESC LIMIT 1"
            )
        ).scalar_one()
    frame = read_atlas_table(db.store, sha)
    rus = frame.filter((frame["iso3"] == "RUS") & (frame["indicator"] == "SUSPECTED_SPONSORED_OPERATIONS"))
    assert rus.height > 0
    assert "attributed by CFR" in json.loads(rus["flags"][0])["sponsor"]
