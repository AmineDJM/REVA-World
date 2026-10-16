"""A-CITIES end to end on a throwaway database with recorded fixture bytes (origin test_fixture), through the
single atlas entry point ``reva.atlas.ingest.run_atlas_ingest``: fetch -> archive raw -> atlas tables ->
(GeoNames) KG statements on items that Wikidata's own P1566 links to a geonameid."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_cities as C
from reva.adapters.connectors import atlas_geo_common as common
from reva.adapters.connectors import atlas_wup as W
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


async def _run(db, spec, dataset: str | None = None, options: dict | None = None) -> dict:  # type: ignore[no-untyped-def]
    with db.owner.connect() as c:
        n = c.execute(
            text("SELECT count(*) FROM evidence.connector_version WHERE name = :n AND version = :v"),
            {"n": spec.name, "v": spec.version},
        ).scalar_one()
    if not n:
        ensure_registered(db.owner, db.store, spec)
    out = await run_atlas_ingest(
        db.settings, db.worker, db.store, spec.name, dataset=dataset, mode="fixture", options=options
    )
    assert out["status"] == "completed" and out["budget"] is None, out
    return out["summary"]


def _wikidata_p1566(db, pairs: list[tuple[str, str]]) -> None:  # type: ignore[no-untyped-def]
    """Load Wikidata-shaped P1566 statements (source wikidata.wdqs, test_fixture origin)."""
    from reva.kg.model import KgStatement, load_batch

    common.ensure_kg_property(db.worker, "P1566")
    raw = json.dumps({"fixture": "wdqs P1566 rows", "pairs": pairs}).encode()
    sha = db.store.put_bytes(raw, "raw", "application/sparql-results+json").sha256
    sts = [
        KgStatement(
            subject_qid=q,
            property_pid="P1566",
            value={"type": "string", "value": gn},
            source_key="wikidata.wdqs",
            source_revision="wdqs@2026-10-15T23:40:00Z",
            statement_id=f"{q}$fixture-{gn}",
            raw_sha256=sha,
        )
        for q, gn in pairs
    ]
    rep = load_batch(
        db.worker,
        db.store,
        statements=sts,
        source_key="wikidata.wdqs",
        raw_sha256s=[sha],
        retrieved_at=datetime(2026, 10, 15, 23, 40, tzinfo=UTC),
        origin="test_fixture",
    )
    assert not rep.rejected_counts, rep.rejected_counts


async def test_geonames_tables_kg_blocked_then_linked(db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    spec = C.atlas_source()
    rep = await _run(db, spec)
    assert set(rep["tables"]) == {C.DS_ADMIN1, C.DS_CITIES, C.DS_COUNTRY}  # names only on request
    assert rep["places"] == 20 and rep["kg"]["loaded"] is False and "P1566" in rep["kg"]["blocked"]
    cities = read_atlas_table(db.store, rep["tables"][C.DS_CITIES]["artifact_sha256"])
    assert cities.filter(pl.col("provider_geo") == "XK")["iso3"].unique().to_list() == ["XKX"]
    assert rep["tables"][C.DS_CITIES]["unmapped_provider_geo"] == []
    agg = read_atlas_table(db.store, rep["tables"][C.DS_COUNTRY]["artifact_sha256"])
    pt = agg.filter((pl.col("iso3") == "PRT") & (pl.col("indicator") == "capital_pplc"))
    assert pt["value_text"].to_list() == ["Lisbon"]

    # the archived acquisition is found again by URL (the Wikidata step reads the GeoNames bytes from it)
    got = C.latest_raw(db.worker, db.store, C.URL_CITIES, origin="test_fixture")
    assert got is not None and got[1] == rep["cities_raw_sha256"] and len(C.parse_cities(got[0])) == 20

    # Wikidata's own P1566 links: Lisbon, Porto, Lisbon district; 786714 (Pristina) held by two items
    _wikidata_p1566(
        db,
        [
            ("Q597", "2267057"),
            ("Q36433", "2735943"),
            ("Q244521", "2267056"),
            ("Q25270", "786714"),
            ("Q999999", "786714"),
        ],
    )
    monkeypatch.setattr(common, "load_country_qids", lambda path=None: {"PRT": "Q45", "XKX": "Q1246"})
    rep2 = await _run(db, spec, dataset=C.DS_CITIES)
    kg = rep2["kg"]
    assert kg["loaded"] is True and kg["places_mapped"] == 2 and kg["ambiguous_geonameids"] == 1
    assert kg["ambiguous_sample"] == {"786714": ["Q25270", "Q999999"]}
    with db.owner.connect() as c:
        rows = c.execute(
            text("""SELECT subject_qid, property_pid, object_qid, qualifiers FROM kg.statement_current
                                 WHERE source_key = 'geonames.cities' AND origin = 'test_fixture'""")
        ).all()
    got_sts = {(r.subject_qid, r.property_pid, r.object_qid) for r in rows}
    assert ("Q597", "P1376", "Q45") in got_sts and ("Q597", "P17", "Q45") in got_sts
    assert ("Q597", "P131", "Q244521") in got_sts and ("Q36433", "P17", "Q45") in got_sts
    assert not any(s[0] in ("Q25270", "Q999999") for s in got_sts)  # ambiguous: never used
    assert ("Q36433", "P1376", "Q45") not in got_sts  # Porto is not PPLC
    rep3 = await _run(db, spec, dataset=C.DS_CITIES)  # reload: nothing new
    assert sum(r["statements_inserted"] for r in rep3["kg"]["reports"]) == 0
    assert rep3["tables"][C.DS_CITIES]["created"] is False


async def test_wup_tables(db) -> None:  # type: ignore[no-untyped-def]
    rep = await _run(db, W.atlas_source())
    assert set(rep["tables"]) == set(W.DATASETS)
    u = rep["tables"][W.DS_URBAN]
    assert u["dataset_key"] == "atlas.un_wup.wup2025_urban_rural" and u["created"] is True
    assert {"900"} <= {x["provider_geo"] for x in u["unmapped_provider_geo"]}
    frame = read_atlas_table(db.store, u["artifact_sha256"])
    assert {"PRT", "XKX", "TWN", "MYS"} <= set(frame["iso3"].drop_nulls().unique().to_list())
    proj = frame.filter(pl.col("indicator").str.ends_with("|proj=wup2025"))
    assert proj["period_start"].dt.year().min() == 2026
    with db.owner.connect() as c:
        n = c.execute(
            text("""SELECT count(*) FROM evidence.acquisition a JOIN evidence.connector_version v
                    ON v.id = a.connector_version_id WHERE v.name = :n AND a.origin = 'test_fixture'"""),
            {"n": W.CONNECTOR_NAME},
        ).scalar_one()
    assert n == 4  # the cities file feeds two tables but is fetched once
    again = await _run(db, W.atlas_source(), dataset=W.DS_CAPITALS)
    assert set(again["tables"]) == {W.DS_CAPITALS} and again["tables"][W.DS_CAPITALS]["created"] is False
