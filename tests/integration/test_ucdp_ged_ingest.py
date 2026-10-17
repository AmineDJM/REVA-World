"""V6CONFLICT end to end (offline, fresh test database): archived raw -> atlas tables ucdp_ged.* -> KG conflict-level
entities and statements (source key ucdp.ged), on the captured fixture slices (origin test_fixture)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_ucdp_ged as g
from reva.adapters.connectors.atlas_geo_common import make_run, register_connector
from reva.evidence.runner import FixtureHttp

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def registered(db):  # type: ignore[no-untyped-def]
    register_connector(db.owner, db.store, g.atlas_source())
    return db


def _count(db: Any, sql: str, **p: Any) -> int:
    with db.worker.connect() as c:
        return int(c.execute(text(sql), p).scalar_one())


async def test_ucdp_ged_fixture_ingest(registered) -> None:  # type: ignore[no-untyped-def]
    db = registered
    out = await g.atlas_ingest(make_run(g.atlas_source(), db.worker, db.store, FixtureHttp(g.NAME)))
    assert {t["table"] for t in out["tables"]} == {f"ucdp_ged.{d}" for d in g.DATASETS}
    assert out["ged_events"] == 168 and out["candidate"]["superseded_by_later_release"] > 0
    kg = out["kg"]
    assert kg["loaded"], kg
    load = kg["load"]
    assert load["statements_inserted"] == kg["statements"], load["rejected_sample"]
    assert not load["rejected_sample"]
    n = _count(db, "SELECT count(*) FROM kg.entity WHERE kind IN ('non_state_conflict', 'one_sided_violence')")
    assert n == kg["report"]["nonstate_conflicts"] + kg["report"]["onesided_instances"]
    n = _count(
        db,
        "SELECT count(*) FROM kg.statement WHERE source_key = :s AND property_pid = 'ucdpged:events_in_adm1' "
        "AND object_qid LIKE 'iso3166_2:PS-%'",
        s=g.SOURCE_KEY,
    )
    assert n > 0
    again = await g.atlas_ingest(make_run(g.atlas_source(), db.worker, db.store, FixtureHttp(g.NAME)))
    assert not any(t["created"] for t in again["tables"])
    assert again["kg"]["load"]["statements_inserted"] == 0
