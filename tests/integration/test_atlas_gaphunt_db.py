"""GAPHUNT gap-fill atlas sources end to end on a throwaway database (fixture mode of the atlas ingest entry point).

Raw bytes are archived first (evidence.acquisition of the registered connector version, origin test_fixture),
then the tables are registered (evidence.dataset, concept 'atlas.table'); a second run is idempotent. No network.
"""

from __future__ import annotations

import asyncio

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import (
    atlas_gap_cbs_bes,
    atlas_gap_eurostat_nuts,
    atlas_gap_insee,
    atlas_gap_jersey,
    atlas_gap_spc,
)
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db

EXPECTED = {
    atlas_gap_spc.NAME: (
        4,
        {"atlas.spc.pocket", "atlas.spc.keyfacts", "atlas.spc.emprates", "atlas.spc.digital"},
        {"NIU", "TKL", "WLF", "PCN", "COK", "TON"},  # pocket NU+TK+WF, keyfacts +PN+CK, emprates/digital +TO
    ),
    atlas_gap_eurostat_nuts.NAME: (7, {"atlas.eurostat.nuts2_gapfill"}, {"GLP", "MTQ", "GUF", "REU", "MYT", "ALA", "SJM"}),
    atlas_gap_insee.NAME: (2, {"atlas.insee.ipc_dom", "atlas.insee.smic_dom"}, {"GLP", "MTQ", "GUF", "REU", "MYT"}),
    atlas_gap_cbs_bes.NAME: (4, {"atlas.cbs.bes_population", "atlas.cbs.bes_gdp", "atlas.cbs.bes_labour"}, {"BES"}),
    atlas_gap_jersey.NAME: (1, {"atlas.statsjersey.earnings"}, {"JEY"}),
}


def _run(db, name: str) -> dict:
    return asyncio.run(run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture"))


@pytest.fixture(scope="module")
def registered(db):
    for name in EXPECTED:
        ensure_registered(db.owner, db.store, get_source(name).spec)
    return db


@pytest.mark.parametrize("name", list(EXPECTED))
def test_gap_source_archives_registers_and_is_idempotent(registered, name):
    db = registered
    n_raw, keys, entities = EXPECTED[name]
    out = _run(db, name)
    assert out["status"] == "completed", out
    assert out["summary"]["ok"] is True, out["summary"]
    assert out["archived_raw"] == n_raw
    got = {r["dataset_key"]: r for r in out["registered"]}
    assert set(got) == keys
    ids = [r["dataset_id"] for r in out["registered"]]
    with db.owner.connect() as c:
        rows = c.execute(
            text("SELECT dataset_key, origin, concept_id, artifact_sha256 FROM evidence.dataset WHERE id = ANY(:ids)"),
            {"ids": ids},
        ).all()
    assert {r.concept_id for r in rows} == {"atlas.table"} and {r.origin for r in rows} == {"test_fixture"}
    seen: set[str] = set()
    for r in rows:
        f = read_atlas_table(db.store, r.artifact_sha256)
        seen |= set(f.filter(pl.col("iso3").is_not_null())["iso3"].unique().to_list())
    assert seen == entities
    again = _run(db, name)
    assert {r["created"] for r in again["registered"]} == {False}
