"""V4ECO ecology sources end to end on the throwaway test database (fixture replay, no network)."""

from __future__ import annotations

import json
from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert out["status"] == "completed", out
    return dict(out["summary"])


def _scalar(db: Any, sql: str, **kw: Any) -> Any:
    with db.owner.connect() as c:
        return c.execute(text(sql), kw).scalar_one()


async def test_gbif_occ_tables_and_kg(db: Any) -> None:
    rep = await _run(db, "atlas.gbif_occ")
    atlas = rep["atlas"]
    assert set(atlas) == {"country_group", "country_year", "country_iucn", "taxon_country"}
    assert all(a["created"] for a in atlas.values())
    assert set(atlas["country_group"]["unmapped_provider_geo"]) <= {"XZ", "ZZ", "XK", "AQ", "UM", "CP"}
    key = _scalar(
        db,
        "SELECT artifact_sha256 FROM evidence.dataset WHERE dataset_key = 'atlas.gbif_occ.country_group' "
        "ORDER BY id DESC LIMIT 1",
    )
    frame = read_atlas_table(db.store, key)
    fra = frame.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "distinct_species.mammalia"))
    assert fra.height == 1 and int(fra["value"][0]) > 100
    assert json.loads(fra["flags"][0])["approximate"] is True
    kg = rep["kg"]
    assert kg["status"] == "loaded" and kg["rejected_counts"] == {}, kg
    # lion: recorded in Kenya (>= 10 records), IUCN VU, measured by the taxon table
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'gbif:5219404' "
            "AND property_pid = 'gbif_occ:recorded_in' AND object_qid IN ('Q114', 'iso3:KEN') AND origin = 'test_fixture'",
        )
        == 1
    )
    assert (
        _scalar(
            db,
            "SELECT value->>'value' FROM kg.statement_current WHERE subject_qid = 'gbif:5219404' "
            "AND property_pid = 'gbif:iucn_red_list_category' AND origin = 'test_fixture'",
        )
        == "VU"
    )
    again = await _run(db, "atlas.gbif_occ")
    assert not any(a["created"] for a in again["atlas"].values())
    assert again["kg"]["statements_inserted"] == 0


async def test_globi_interactions_kg(db: Any) -> None:
    rep = await _run(db, "atlas.globi")
    assert rep["queries"] == 4
    kg = rep["kg"]
    assert kg["status"] == "loaded" and kg["rejected_counts"] == {}, kg
    assert rep["kg_build"]["truncated"] == [{"seed": 1341976, "direction": "source", "rows": 2000}]
    # every edge is between ids GloBI returned; the lion ids GloBI used resolve to the GBIF lion node
    n_within = _scalar(
        db,
        "SELECT count(*) FROM kg.statement_current WHERE property_pid = 'globi:resolved_within' "
        "AND object_qid = 'gbif:5219404' AND origin = 'test_fixture'",
    )
    assert n_within >= 1
    eats = _scalar(
        db,
        "SELECT count(*) FROM kg.statement_current s JOIN kg.statement_current w ON w.subject_qid = s.subject_qid "
        "AND w.property_pid = 'globi:resolved_within' AND w.object_qid = 'gbif:5219404' "
        "WHERE s.property_pid IN ('globi:eats', 'globi:preys_on') AND s.origin = 'test_fixture'",
    )
    assert eats > 50
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM kg.statement_current WHERE source_key = 'globi.interactions' "
            "AND property_pid LIKE 'P%' AND origin = 'test_fixture'",
        )
        == 0
    )


async def test_ecoregions_kg(db: Any) -> None:
    rep = await _run(db, "atlas.ecoregions")
    assert rep["ecoregions"] == 847 and rep["biomes"] == 14 and rep["dbf_encoding"] == "cp1252"
    kg = rep["kg"]
    assert kg["status"] == "loaded" and kg["rejected_counts"] == {}, kg
    # Adelie Land tundra (ECO_ID 117) -> Tundra (biome 11), Antarctica realm
    assert (
        _scalar(
            db,
            "SELECT object_qid FROM kg.statement_current WHERE subject_qid = 'resolve_eco:117' "
            "AND property_pid = 'resolve:in_biome' AND origin = 'test_fixture'",
        )
        == "resolve_biome:11"
    )
    assert (
        _scalar(
            db,
            "SELECT object_qid FROM kg.statement_current WHERE subject_qid = 'resolve_eco:117' "
            "AND property_pid = 'resolve:in_realm' AND origin = 'test_fixture'",
        )
        == "resolve_realm:Antarctica"
    )
    # Rock and Ice (ECO_ID 0): biome / realm / NNH are "N/A" -> no statement, never a guess
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'resolve_eco:0' AND origin = 'test_fixture'",
        )
        == 0
    )
    assert (
        _scalar(db, "SELECT count(*) FROM evidence.dataset WHERE dataset_key LIKE 'atlas.ecoregions.%'") == 0
    )  # KG only


async def test_fishstat_production(db: Any) -> None:
    rep = await _run(db, "atlas.fishstat")
    a = rep["atlas"]
    assert a["dataset_key"] == "atlas.fishstat.production" and a["created"] is True
    key = _scalar(
        db,
        "SELECT artifact_sha256 FROM evidence.dataset WHERE dataset_key = 'atlas.fishstat.production' "
        "ORDER BY id DESC LIMIT 1",
    )
    frame = read_atlas_table(db.store, key)
    nor = frame.filter(
        (pl.col("iso3") == "NOR")
        & (pl.col("indicator") == "capture.yb_aquatic_animals_fish_crustaceans_and_molluscs_etc.tonnes")
        & (pl.col("period_start").dt.year() == 2023)
    )
    assert nor.height == 1 and 1_000_000 < float(nor["value"][0]) < 5_000_000
    assert nor["unit"][0] == "tonnes live weight"
    # former / non-ISO FAO areas keep their FAO code and stay unmapped
    assert all(g.startswith("un:") or len(g) == 3 for g in a["unmapped_provider_geo"])
