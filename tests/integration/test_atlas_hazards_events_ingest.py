"""A-HAZARDS end to end in the per-session throwaway database (fixture mode, no network): NCEI pages archived ->
atlas table ``ncei_hazards.country_year`` -> KG events -> T1 coded_trigger claims (when CAUSCORE's ``reva.causal`` and
migration 0015 are present) -> Wikidata P1886 ``same_as`` for volcanoes. Idempotent re-runs insert nothing.
"""

from __future__ import annotations

import json

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_noaa_hazards as hz
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db

OPTIONS = {"min_year": 2018, "max_year": 2018}


@pytest.fixture(scope="module")
def first_run(db):  # type: ignore[no-untyped-def]
    import asyncio

    for spec in hz.atlas_source():
        ensure_registered(db.owner, db.store, spec)
    return asyncio.run(
        run_atlas_ingest(db.settings, db.worker, db.store, hz.NAME, mode="fixture", options=dict(OPTIONS))
    )


def test_atlas_table(db, first_run) -> None:  # type: ignore[no-untyped-def]
    out = first_run
    assert out["status"] == "completed", out.get("error")
    s = out["summary"]
    assert s["records"] == {"earthquakes": 71, "tsunamis": 25, "runups": 927, "volcanoes": 15}
    assert s["archived_raw"] == 16
    atlas = s["atlas"]
    assert atlas["dataset_key"] == "atlas.ncei_hazards.country_year" and atlas["created"] is True
    assert [u["provider_geo"] for u in atlas["unmapped_provider_geo"]] == ["USA TERRITORY"]
    frame = read_atlas_table(db.store, atlas["artifact_sha256"])
    assert frame.height == atlas["row_count"] == 193
    idn = frame.filter((pl.col("iso3") == "IDN") & (pl.col("indicator") == "ts_runup_deaths"))
    assert idn["value"].to_list() == ["4132"] and idn["period_start"].dt.year().to_list() == [2018]
    flags = json.loads(idn["flags"][0])
    assert flags["ncei_country_names"] == ["INDONESIA"] and flags["records_with_value"] >= 1


def test_kg_events(db, first_run) -> None:  # type: ignore[no-untyped-def]
    kg = first_run["summary"]["kg"]
    assert kg["status"] == "loaded" and not kg["rejected"], kg
    assert kg["entities_inserted"] == 71 + 25 + 15 + 12
    assert kg["statements_inserted"] > 2000
    with db.worker.connect() as c:
        kinds = dict(
            c.execute(
                text("""SELECT kind, count(*) FROM kg.entity_current WHERE qid LIKE 'ncei_hazards:%'
                        AND origin = 'test_fixture' GROUP BY kind""")
            ).all()
        )
        palu = c.execute(
            text("""SELECT value->>'time', (value->>'precision')::int FROM kg.statement_current
                    WHERE subject_qid = :q AND property_pid = 'P585' AND origin = 'test_fixture'"""),
            {"q": hz.ts_qid(5689)},
        ).one()
        country = c.execute(
            text("""SELECT object_qid FROM kg.statement_current
                    WHERE subject_qid = :q AND property_pid = 'ncei:country' AND origin = 'test_fixture'"""),
            {"q": hz.eq_qid(10369)},
        ).scalar_one()
        deaths = c.execute(
            text("""SELECT value->>'amount' FROM kg.statement_current
                    WHERE subject_qid = :q AND property_pid = 'ncei:deaths' AND origin = 'test_fixture'
                      AND qualifiers->>'scope' = 'from the tsunami'"""),
            {"q": hz.ts_qid(5689)},
        ).scalar_one()
    assert kinds == {"earthquake": 71, "tsunami": 25, "volcanic_eruption": 15, "volcano": 12}
    assert palu == ("2018-09-28", 11)
    assert country == "Q252"  # Indonesia (registry/entities/country_qids.json)
    assert deaths == "4340"


def test_coded_trigger_claims(db, first_run) -> None:  # type: ignore[no-untyped-def]
    causal = first_run["summary"]["causal"]
    if causal.get("status") == "unavailable":
        pytest.skip(f"CAUSCORE not available in this checkout: {causal}")
    assert causal["inserted"] == 19 and not causal.get("rejected_counts"), causal
    with db.worker.connect() as c:
        rows = c.execute(
            text("""SELECT cause_id, effect_id, relation, source_tier FROM kg.causal_claim_current
                    WHERE source_key = :s AND origin = 'test_fixture'"""),
            {"s": hz.SOURCE_KEY},
        ).all()
    pairs = {(r[0], r[1]) for r in rows}
    assert {r[2] for r in rows} == {"coded_trigger"} and {r[3] for r in rows} == {"T1"}
    assert (hz.eq_qid(10369), hz.ts_qid(5689)) in pairs  # Sulawesi (Palu) earthquake -> tsunami
    assert (hz.eruption_qid(7264), hz.ts_qid(5699)) in pairs  # Krakatau eruption -> tsunami
    assert (hz.eruption_qid(7541), hz.ts_qid(5796)) not in pairs  # NCEI cause code 1 (earthquake): context only


def test_rerun_is_idempotent(db, first_run) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    again = asyncio.run(
        run_atlas_ingest(db.settings, db.worker, db.store, hz.NAME, mode="fixture", options=dict(OPTIONS))
    )
    assert again["status"] == "completed", again.get("error")
    s = again["summary"]
    assert (
        s["atlas"]["created"] is False
        and s["atlas"]["artifact_sha256"] == first_run["summary"]["atlas"]["artifact_sha256"]
    )
    assert s["kg"]["statements_inserted"] == 0 and s["kg"]["entities_inserted"] == 0
    assert s["kg"]["retractions_inserted"] == 0  # a windowed run never retracts
    if s["causal"].get("status") != "unavailable":
        assert s["causal"]["inserted"] == 0


def test_wikidata_volcano_same_as(db, first_run) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    out = asyncio.run(run_atlas_ingest(db.settings, db.worker, db.store, hz.WD_NAME, mode="fixture"))
    assert out["status"] == "completed", out.get("error")
    b = out["summary"]["build"]
    assert b["same_as"] == 8 and b["gvp_number_on_several_items"] == 4
    with db.worker.connect() as c:
        mayon = c.execute(
            text("""SELECT s.object_qid FROM kg.statement_current s
                    JOIN kg.statement_current g ON g.subject_qid = s.subject_qid
                     AND g.property_pid = 'ncei:gvp_volcano_number' AND g.value->>'value' = '273030'
                   WHERE s.property_pid = 'ncei:same_as' AND s.source_key = 'wikidata.wdqs'
                     AND s.origin = 'test_fixture'""")
        ).scalar_one()
    assert mayon == "Q1484"
