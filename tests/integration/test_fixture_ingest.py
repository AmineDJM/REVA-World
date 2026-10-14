"""Smoke: captured-fixture replay through the real ingest path is test_fixture origin."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from reva.adapters.connectors.registry import builtin_connectors
from reva.evidence.runner import FixtureHttp, run_connector

pytestmark = pytest.mark.db


async def test_worldbank_fixture_replay_is_fixture_origin(db):
    c = builtin_connectors()["worldbank.wdi.indicators"]
    rep = await run_connector(c, FixtureHttp(c.describe().name), db.store, db.worker, persist_cursor=False)
    assert all(o.status in ("published", "unchanged") for o in rep.outcomes), rep.as_dict()
    with db.owner.connect() as conn:
        origins = conn.execute(
            text("SELECT DISTINCT a.origin FROM evidence.acquisition a JOIN evidence.source s ON s.id=a.source_id "
                 "WHERE s.source_key='worldbank.wdi'")
        ).scalars().all()
    assert origins == ["test_fixture"]
    with db.owner.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM evidence.observation o JOIN evidence.source s "
                              "ON s.id=o.source_id WHERE s.source_key='worldbank.wdi' "
                              "AND o.logical_key LIKE :p"), {"p": "wb:%:20%"}).scalar_one()
    assert n >= 200
