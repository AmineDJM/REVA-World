"""V6CULTURE atlas sources end to end on the throwaway test database (fixture replay, no network)."""

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


async def test_musicbrainz_stats(db) -> None:
    _reg(db, "atlas.musicbrainz_stats")
    out = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.musicbrainz_stats", mode="fixture")
    assert out["status"] == "completed", out
    rep = out["summary"]
    c = rep["country_stats"]
    assert c["dataset_key"] == "atlas.musicbrainz.country_stats" and c["created"] is True
    frame = read_atlas_table(db.store, c["artifact_sha256"])
    bra = frame.filter((pl.col("iso3") == "BRA") & (pl.col("indicator") == "mb_artists"))
    assert bra["value"].to_list() == ["22156"]
    # pseudo / historical codes and the unknown-country row are kept, unmapped
    assert {"mb:null", "SU", "XW", "XE", "YU"} <= {u["provider_geo"] for u in c["unmapped_provider_geo"]}
    assert frame.filter(pl.col("provider_geo") == "SU")["iso3"].null_count() == 5
    lang = rep["language_stats"]
    lf = read_atlas_table(db.store, lang["artifact_sha256"])
    eng = lf.filter(pl.col("indicator") == "mb_releases_by_language|language=eng")
    assert eng["value"].to_list() == ["3701389"] and eng["provider_geo"].to_list() == ["world"]
    again = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.musicbrainz_stats", mode="fixture")
    assert again["summary"]["country_stats"]["created"] is False


async def test_openlibrary_subjects_atlas_and_kg(db) -> None:
    _reg(db, "atlas.openlibrary_subjects")
    out = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.openlibrary_subjects", mode="fixture")
    assert out["status"] == "completed", out
    rep = out["summary"]
    assert rep["subjects"] == 3 and rep["authors_fetched"] == 6
    atlas = rep["atlas"]
    assert atlas["dataset_key"] == "atlas.openlibrary.subjects"
    frame = read_atlas_table(db.store, atlas["artifact_sha256"])
    assert set(frame["provider_geo"].unique().to_list()) == {"world"}
    assert frame.filter(pl.col("indicator") == "ol_works|subject=place:brazil").height == 1
    kg = rep["kg"]
    assert kg["rejected"] == {} and kg["statements_inserted"] > 0, kg
    n = _count(
        db,
        "SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'openlibrary:OL28294A' "
        "AND property_pid = 'openlibrary:wikidata_id' AND origin = 'test_fixture'",
    )
    assert n == 1  # Virgil -> Q1398, as Open Library publishes it
    m = _count(
        db,
        "SELECT count(*) FROM kg.statement_current WHERE property_pid = 'openlibrary:has_works_in_subject' "
        "AND object_qid = 'openlibrary:subject.poetry' AND origin = 'test_fixture'",
    )
    assert m == 2
    again = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.openlibrary_subjects", mode="fixture")
    assert again["summary"]["atlas"]["created"] is False and again["summary"]["kg"]["statements_inserted"] == 0
