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
    # FIX6 (AUDIT4 M1): GED statistics the new build no longer derives are retracted, append-only, with the
    # loader's derived reason (the statement's raw GED zip is in the delivery -> reclassified_by_reva)
    with db.worker.connect() as c:
        keys = set(
            c.execute(
                text("SELECT statement_key FROM kg.statement_current WHERE source_key = :s AND statement_key LIKE :p"),
                {"s": g.SOURCE_KEY, "p": "ucdp.ged:ged:%:adm1:%"},
            ).scalars()
        )
        ged_sha, ret_at = c.execute(
            text("SELECT raw_sha256, retrieved_at FROM kg.statement_current WHERE statement_key = ANY(:k) LIMIT 1"),
            {"k": sorted(keys)},
        ).one()
    drop = sorted(keys)[:2]
    with db.worker.connect() as c:
        everything = set(
            c.execute(
                text("SELECT statement_key FROM kg.statement_current WHERE source_key = :s AND statement_key LIKE :p"),
                {"s": g.SOURCE_KEY, "p": "ucdp.ged:ged:%"},
            ).scalars()
        )
    rep = g.retract_absent_ged_statements(
        db.worker, db.store, everything - set(drop), raw_sha256s=[ged_sha], retrieved_at=ret_at, origin="test_fixture"
    )
    assert rep["absent"] == 2 and rep["retractions_inserted"] == 2
    with db.worker.connect() as c:
        rows = c.execute(
            text("SELECT reason, detail->>'basis' FROM kg.statement_retraction WHERE statement_key = ANY(:k)"),
            {"k": drop},
        ).all()
    assert sorted(rows) == [("reclassified_by_reva", "raw_unchanged")] * 2
