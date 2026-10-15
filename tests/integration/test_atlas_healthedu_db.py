"""A-HEALTHEDU atlas connectors end to end on captured fixtures (origin test_fixture): raw bytes are
archived as evidence acquisitions, the atlas tables are registered from the *archived* bytes, a
re-run is idempotent, and a run with one drifted item registers nothing."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_uis as uis
from reva.adapters.connectors import atlas_who as who
from reva.adapters.connectors.atlas_healthedu_common import register_connector, run_and_register
from reva.adapters.http.safe_client import request_fingerprint
from reva.atlas.tables import read_atlas_table
from reva.evidence.runner import FixtureHttp

pytestmark = pytest.mark.db

WHO_IND = ["WHOSIS_000001", "MDG_0000000032", "MDG_0000000007", "M_Est_tob_curr_std", "GHED_CHEGDP_SHA2011", "RS_198"]
WHO_CTRY = ["DZA", "FRA", "HKG", "IND", "NGA"]
UIS_BATCHES = [["LR.AG15T99", "LR.AG15T99.F"], ["XGDP.FSGOV", "EXPGDP.TOT", "CR.MOD.1"]]
UIS_CTRY = ["DZA", "FRA", "IND", "NGA", "TUR"]


def test_sources_are_seeded(db) -> None:
    with db.owner.connect() as c:
        keys = set(c.execute(text("SELECT source_key FROM evidence.source")).scalars())
    assert {"who.gho", "unesco.uis"} <= keys


async def test_fixture_ingest_registers_atlas_tables_idempotently(db) -> None:
    for conn, source, dataset in (
        (who.WhoGhoConnector(WHO_IND, WHO_CTRY), "who", "gho"),
        (uis.UisConnector(UIS_BATCHES, UIS_CTRY), "uis", "api"),
    ):
        register_connector(db.owner, db.store, conn)
        out = await run_and_register(conn, FixtureHttp(conn.describe().name), db.store, db.worker)
        assert out["status"] == "registered", out
        atlas = out["atlas"]
        assert atlas["dataset_key"] == f"atlas.{source}.{dataset}" and atlas["created"] is True
        assert atlas["unmapped_provider_geo"] == []
        frame = read_atlas_table(db.store, atlas["artifact_sha256"])
        assert frame.height == atlas["row_count"] > 0
        # every cell points at an archived raw acquisition of this run
        with db.owner.connect() as c:
            archived = set(
                c.execute(text("SELECT raw_sha256 FROM evidence.acquisition WHERE origin='test_fixture'")).scalars()
            )
        assert set(frame["raw_sha256"].unique().to_list()) <= archived
        again = await run_and_register(conn, FixtureHttp(conn.describe().name), db.store, db.worker)
        assert again["status"] == "registered" and again["atlas"]["created"] is False
        assert again["atlas"]["artifact_sha256"] == atlas["artifact_sha256"]


async def test_a_drifted_item_blocks_the_whole_table(db) -> None:
    conn = who.WhoGhoConnector(WHO_IND, WHO_CTRY)  # same spec as above (a name@version is immutable)
    register_connector(db.owner, db.store, conn)
    bad_url = who.data_url("RS_198", conn.countries)
    drifted = json.dumps({"value": [{"Renamed": 1}]}).encode()
    http = FixtureHttp(conn.describe().name, overrides={request_fingerprint("GET", bad_url): drifted})
    with db.owner.connect() as c:
        before = c.execute(text("SELECT count(*) FROM evidence.dataset WHERE dataset_key='atlas.who.gho'")).scalar_one()
    out = await run_and_register(conn, http, db.store, db.worker)
    assert out["status"] == "blocked" and out["atlas"] is None
    assert [f["item"] for f in out["failed_items"]] == ["gho.data:RS_198"]
    assert out["failed_items"][0]["status"] == "quarantined"
    with db.owner.connect() as c:
        after = c.execute(text("SELECT count(*) FROM evidence.dataset WHERE dataset_key='atlas.who.gho'")).scalar_one()
    assert after == before
