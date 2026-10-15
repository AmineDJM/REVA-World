"""A-FACTBOOK atlas sources end to end on a throwaway database (fixture mode of the atlas ingest entry point).

Raw bytes are archived first (evidence.acquisition of the registered atlas connector version, origin
test_fixture), then the tables are registered (evidence.dataset, concept 'atlas.table'); a second run
is idempotent. No network.
"""

from __future__ import annotations

import asyncio
import json

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_factbook as fb
from reva.adapters.connectors import atlas_unsd_ama as ama
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db

GECS = "fr,tw,kv,gl,wi,we,vt,xx,kn,sv"


def _run(db, name: str, options: dict) -> dict:
    return asyncio.run(run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture", options=options))


@pytest.fixture(scope="module")
def registered(db):
    for name in (fb.NAME_PROFILES, fb.NAME_DISPUTES, ama.NAME):
        ensure_registered(db.owner, db.store, get_source(name).spec)
    return db


def test_factbook_profiles_tables(registered):
    db = registered
    out = _run(db, fb.NAME_PROFILES, {"gecs": GECS})
    assert out["status"] == "completed" and out["archived_raw"] == 10
    keys = {r["dataset_key"]: r for r in out["registered"]}
    assert set(keys) == {
        "atlas.factbook.legal_2026_01",
        "atlas.factbook.figures_2026_01",
        "atlas.factbook.texts_2026_01",
    }
    assert all(r["created"] for r in out["registered"])
    ids = [r["dataset_id"] for r in out["registered"]]
    with db.owner.connect() as c:
        ds = c.execute(
            text("""SELECT dataset_key, origin, concept_id, artifact_sha256, cardinality(source_acquisition_ids) AS n
                    FROM evidence.dataset WHERE id = ANY(:ids)"""),
            {"ids": ids},
        ).all()
        acq = c.execute(
            text("""SELECT count(*) FROM evidence.acquisition a JOIN evidence.connector_version v
                    ON v.id = a.connector_version_id WHERE v.name = :n AND a.origin = 'test_fixture'"""),
            {"n": fb.NAME_PROFILES},
        ).scalar_one()
    assert {d.concept_id for d in ds} == {"atlas.table"} and {d.origin for d in ds} == {"test_fixture"}
    assert len(ds) == 3 and all(d.n == 10 for d in ds) and acq >= 10  # the law test may share this database
    sha = next(d.artifact_sha256 for d in ds if d.dataset_key.endswith("figures_2026_01"))
    f = read_atlas_table(db.store, sha)
    gl = f.filter((pl.col("iso3") == "GRL") & (pl.col("indicator") == "population.total")).to_dicts()
    assert gl and json.loads(gl[0]["flags"])["estimate"] is True
    assert f.filter(pl.col("provider_geo") == "TW")["iso3"].unique().to_list() == ["TWN"]
    again = _run(db, fb.NAME_PROFILES, {"gecs": GECS})
    assert [r["created"] for r in again["registered"]] == [False, False, False]  # idempotent


def test_factbook_disputes_and_ama_tables(registered):
    db = registered
    out = _run(db, fb.NAME_DISPUTES, {"gecs": ["fr", "wi", "tw"]})
    assert out["status"] == "completed"
    assert [r["dataset_key"] for r in out["registered"]] == ["atlas.factbook.disputes_2024_04"]
    out = _run(db, ama.NAME, {"files": "2,9,30"})
    assert out["status"] == "completed" and out["archived_raw"] == 4  # file list + 3 workbooks
    (reg,) = out["registered"]
    assert reg["dataset_key"] == "atlas.unsd.ama" and reg["created"] and reg["row_count"] > 1000
    assert out["summary"]["ama"]["unmapped_provider_geo"] == ["810", "835"]
