"""A-CULTURE atlas sources end to end on the throwaway test database (fixture replay, no network)."""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


def _reg(db: Any, name: str) -> None:
    ensure_registered(db.owner, db.store, get_source(name).spec)


def _count(db: Any, sql: str, **kw: Any) -> int:
    with db.owner.connect() as c:
        return int(c.execute(text(sql), kw).scalar_one())


async def test_ich_atlas_and_kg(db) -> None:
    _reg(db, "atlas.unesco_ich")
    out = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.unesco_ich", mode="fixture")
    assert out["status"] == "completed", out
    rep = out["summary"]
    atlas = rep["atlas"]
    assert atlas["dataset_key"] == "atlas.unesco_ich.ich001" and atlas["created"] is True
    assert atlas["unmapped_provider_geo"] == []
    frame = read_atlas_table(db.store, atlas["artifact_sha256"])
    jpn = frame.filter((pl.col("iso3") == "JPN") & (pl.col("indicator") == "ich_elements_total"))
    assert jpn["value"].to_list() == ["4"]
    kg = rep["kg"]
    assert kg["status"] == "loaded", kg
    assert kg["rejected_counts"] == {} and kg["statements_inserted"] == kg["statements_built"] > 0
    # dry stone walling: 13 States linked through P17 (France <-> Italy via one element)
    n = _count(
        db,
        "SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'ich:2106' AND property_pid = 'P17' "
        "AND origin = 'test_fixture' AND source_key = 'unesco.ich'",
    )
    assert n == 13
    again = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.unesco_ich", mode="fixture")
    assert again["summary"]["atlas"]["created"] is False
    assert again["summary"]["kg"]["statements_inserted"] == 0


async def test_whc_links_kg_only(db) -> None:
    _reg(db, "atlas.unesco_whc_links")
    out = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.unesco_whc_links", mode="fixture")
    assert out["status"] == "completed", out
    kg = out["summary"]["kg"]
    assert kg["status"] == "loaded" and kg["rejected_counts"] == {}, kg
    assert out["summary"]["transboundary"] == 10
    n = _count(
        db,
        "SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'whc:1567' AND property_pid = 'P17' "
        "AND origin = 'test_fixture'",
    )
    assert n == 2  # Funerary and memory sites of the First World War: Belgium and France
    assert _count(db, "SELECT count(*) FROM evidence.dataset WHERE dataset_key LIKE 'atlas.unesco_whc.%'") == 0


async def test_uis_culture(db) -> None:
    _reg(db, "atlas.uis_culture")
    out = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.uis_culture", mode="fixture")
    assert out["status"] == "completed", out
    atlas = out["summary"]["atlas"]
    assert atlas["dataset_key"] == "atlas.uis.culture" and atlas["unmapped_provider_geo"] == []
    frame = read_atlas_table(db.store, atlas["artifact_sha256"])
    assert set(frame["iso3"].unique().to_list()) <= {"FRA", "BRA", "SYC", "BDI", "BEL", "MEX"}
    syc = frame.filter((pl.col("iso3") == "SYC") & pl.col("flags").str.contains('"magnitude":"NA"'))
    assert syc.height > 0 and syc["value"].null_count() == syc.height


async def test_olympics_medals(db) -> None:
    _reg(db, "atlas.olympics_medals")
    out = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.olympics_medals", mode="fixture")
    assert out["status"] == "completed", out
    rep = out["summary"]
    assert rep["games"] == 4 and rep["games_without_qid"] == []
    atlas = rep["atlas"]
    assert atlas["dataset_key"] == "atlas.olympics.medal_tables"
    assert {"RU1", "ROC", "AIN"} <= {u["provider_geo"] for u in atlas["unmapped_provider_geo"]}
    kg = rep["kg"]
    assert kg["status"] == "loaded" and kg["rejected_counts"] == {}, kg
    n = _count(
        db,
        "SELECT count(*) FROM kg.statement_current WHERE property_pid = 'P17' AND origin = 'test_fixture' "
        "AND source_key = 'wikipedia.olympic_medal_tables'",
    )
    assert n == 4  # one host per Games
    again = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.olympics_medals", mode="fixture")
    assert again["summary"]["atlas"]["created"] is False and again["summary"]["kg"]["statements_inserted"] == 0
