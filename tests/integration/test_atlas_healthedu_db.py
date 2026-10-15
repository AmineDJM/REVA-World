"""A-HEALTHEDU atlas sources end to end through ATLASCORE's ingest entry point on captured fixtures
(origin test_fixture): raw bytes are archived as evidence acquisitions of the atlas connector
versions, the atlas tables are registered from the *archived* bytes, a re-run is idempotent, and a
run with one drifted response archives the bytes but registers nothing."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_uis as uis
from reva.adapters.connectors import atlas_who as who
from reva.adapters.http.safe_client import request_fingerprint
from reva.atlas.ingest import AtlasRun, run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import read_atlas_table
from reva.evidence.runner import FixtureHttp
from reva.kernel.errors import ValidationProblem

pytestmark = pytest.mark.db


def test_sources_are_seeded(db) -> None:
    with db.owner.connect() as c:
        keys = set(c.execute(text("SELECT source_key FROM evidence.source")).scalars())
    assert {"who.gho", "unesco.uis"} <= keys


async def test_fixture_ingest_registers_atlas_tables_idempotently(db) -> None:
    for mod, key in ((who, "atlas.who.gho"), (uis, "atlas.uis.api")):
        spec = mod.atlas_source()
        ensure_registered(db.owner, db.store, spec)
        out = await run_atlas_ingest(db.settings, db.worker, db.store, spec.name, mode="fixture")
        assert out["status"] == "completed" and out["budget"] is None, out
        atlas = out["summary"]["atlas"]
        assert atlas["dataset_key"] == key and atlas["created"] is True
        assert atlas["unmapped_provider_geo"] == []
        frame = read_atlas_table(db.store, atlas["artifact_sha256"])
        assert frame.height == atlas["row_count"] > 0
        with db.owner.connect() as c:
            archived = set(
                c.execute(
                    text(
                        """SELECT a.raw_sha256 FROM evidence.acquisition a
                           JOIN evidence.connector_version v ON v.id = a.connector_version_id
                           WHERE a.origin = 'test_fixture' AND v.name = :n"""
                    ),
                    {"n": spec.name},
                ).scalars()
            )
        assert set(frame["raw_sha256"].unique().to_list()) <= archived
        assert out["archived_raw"] == out["summary"]["items"]
        again = await run_atlas_ingest(db.settings, db.worker, db.store, spec.name, mode="fixture")
        assert again["summary"]["atlas"]["created"] is False
        assert again["summary"]["atlas"]["artifact_sha256"] == atlas["artifact_sha256"]


async def test_a_drifted_response_blocks_the_whole_table(db) -> None:
    spec = who.atlas_source()
    ensure_registered(db.owner, db.store, spec)
    conn = who.WhoGhoConnector(who.FIXTURE_INDICATORS, who.FIXTURE_COUNTRIES)
    bad_url = who.data_url("RS_198", conn.countries)
    drifted = json.dumps({"value": [{"Renamed": 1}]}).encode()
    http = FixtureHttp(spec.name, overrides={request_fingerprint("GET", bad_url): drifted})
    run = AtlasRun(spec=spec, http=http, engine=db.worker, store=db.store, settings=db.settings, origin="test_fixture")
    with db.owner.connect() as c:
        before = c.execute(text("SELECT count(*) FROM evidence.dataset WHERE dataset_key='atlas.who.gho'")).scalar_one()
    with pytest.raises(ValidationProblem) as ei:
        await who.atlas_ingest(run)
    failed = ei.value.details["failed_items"]
    assert [f["item"] for f in failed] == ["gho.data:RS_198"]
    assert len(run.archived) == len(conn.items())  # every response archived, drifted one included
    assert run.registered == []
    with db.owner.connect() as c:
        after = c.execute(text("SELECT count(*) FROM evidence.dataset WHERE dataset_key='atlas.who.gho'")).scalar_one()
    assert after == before
