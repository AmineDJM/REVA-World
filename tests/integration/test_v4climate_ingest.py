"""V4CLIMATE end to end (offline, fresh test database): archived raw -> atlas tables -> KG entities/statements ->
attributed causal claims, for the four V4CLIMATE atlas sources on their captured fixtures (origin test_fixture).
"""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_attribution as at
from reva.adapters.connectors import atlas_cckp_series as cs
from reva.adapters.connectors import atlas_climate_indices as ci
from reva.adapters.connectors import atlas_ghcnm as gm
from reva.adapters.connectors.atlas_geo_common import make_run, register_connector
from reva.atlas.tables import read_atlas_table
from reva.evidence.runner import FixtureHttp

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def registered(db):  # type: ignore[no-untyped-def]
    for m in (cs, ci, gm, at):
        specs = m.atlas_source()
        for spec in specs if isinstance(specs, list) else [specs]:
            register_connector(db.owner, db.store, spec)
    return db


def _run(db: Any, mod: Any, **options: Any) -> Any:
    specs = mod.atlas_source()
    run = make_run(specs[0] if isinstance(specs, list) else specs, db.worker, db.store, FixtureHttp(mod.NAME))
    run.options = dict(options)
    return run


def _count(db: Any, sql: str, **p: Any) -> int:
    with db.worker.connect() as c:
        return int(c.execute(text(sql), p).scalar_one())


async def test_cckp_series_tables(registered) -> None:  # type: ignore[no-untyped-def]
    db = registered
    out = await cs.atlas_ingest(_run(db, cs, geos=["FRA", "KSV", "BVT", "MLI"]))
    assert {t["table"] for t in out["tables"]} == {f"cckp_ts.{d}" for d in cs.DATASETS}
    assert "RESTRICTED" in out["scope"]
    proj = next(t for t in out["tables"] if t["table"] == "cckp_ts.cmip6_annual")
    f = read_atlas_table(db.store, proj["artifact_sha256"])
    assert f.filter(pl.col("flags").str.contains('"estimate_kind":"projection"')).height > 0
    again = await cs.atlas_ingest(_run(db, cs, geos=["FRA", "KSV", "BVT", "MLI"]))
    assert not any(t["created"] for t in again["tables"])


async def test_climate_indices_tables_and_kg(registered) -> None:  # type: ignore[no-untyped-def]
    db = registered
    out = await ci.atlas_ingest(_run(db, ci))
    assert len(out["tables"]) == len(ci.DATASETS) and out["kg"]["loaded"]
    assert out["kg"]["report"]["statements_inserted"] == out["kg"]["statements"]
    n = _count(db, "SELECT count(*) FROM kg.entity WHERE kind = 'climate_index'")
    assert n == len(ci.SERIES)


async def test_ghcnm_stations_adm1_and_series(registered) -> None:  # type: ignore[no-untyped-def]
    db = registered
    out = await gm.atlas_ingest(_run(db, gm))
    assert out["kg"]["loaded"] and out["located_adm1"] > 0 and out["selected"] > 0
    assert out["tables"][0]["rows"] > 1000
    rep = out["kg"]["load_stations"]
    assert rep["statements_inserted"] == out["kg"]["station_statements"], rep["rejected_sample"]
    assert out["kg"]["load_adm1"]["statements_inserted"] == out["kg"]["adm1_statements"]
    n = _count(
        db,
        "SELECT count(*) FROM kg.statement WHERE source_key = :s AND property_pid = 'ghcnm:in_adm1' "
        "AND object_qid LIKE 'iso3166_2:SE-%'",
        s=gm.SOURCE_KEY,
    )
    assert n > 0
    assert _count(db, "SELECT count(*) FROM kg.entity WHERE kind = 'region_adm1'") == out["kg"]["adm1_entities"]
    # AUDIT3 L2: the Natural Earth zip is archived with Natural Earth's terms, not NOAA's
    with db.worker.connect() as c:
        ne_sha = c.execute(
            text("SELECT DISTINCT raw_sha256 FROM kg.statement WHERE source_key = :s"), {"s": gm.NE_SOURCE_KEY}
        ).scalar_one()
        rights = c.execute(text("SELECT rights FROM meta.artifact WHERE sha256 = :h"), {"h": ne_sha}).scalar_one()
    assert rights["source_key"] == gm.NE_SOURCE_KEY and "Natural Earth" in rights["licence"], rights
    assert "NOAA" not in str(rights)


async def test_attribution_kg_and_t2_claims(registered) -> None:  # type: ignore[no-untyped-def]
    db = registered
    out = await at.atlas_ingest(_run(db, at, min_rows=10))
    assert out["revision"] == "19 March 2026"
    assert out["kg"]["load"]["statements_inserted"] == out["kg"]["statements"]
    c = out["causal"]
    assert c["claims"] > 0 and c["load"]["inserted"] == c["claims"]
    tiers = _count(
        db, "SELECT count(*) FROM kg.causal_claim WHERE source_key = :s AND source_tier = 'T2'", s=at.SOURCE_KEY
    )
    assert tiers == c["claims"]
    again = await at.atlas_ingest(_run(db, at, min_rows=10))
    assert again["causal"]["load"]["inserted"] == 0
