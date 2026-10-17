"""KGLANG end to end on a throwaway database: the Glottolog, Wikidata P1394, WALS and Grambank atlas sources in
fixture mode through the atlas platform (``reva.atlas.ingest.run_atlas_ingest``; no network): KG loads with
provenance, the per-country count table, idempotence, retraction of a fact a new release drops, and the "neurons":
Brazil -> Portuguese -> Indo-European, Brazil -> Portugal through Portuguese, Portuguese -> its Wikidata item.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_glottolog as gl
from reva.adapters.connectors import atlas_grambank as ag
from reva.adapters.connectors import atlas_wals as aw
from reva.adapters.connectors.atlas_geo_common import load_country_qids
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.kg import query as kq

pytestmark = pytest.mark.db
SOURCES = ("atlas.glottolog", "atlas.glottolog_wikidata", "atlas.wals", "atlas.grambank")


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert out["status"] == "completed", out
    return out


def _country(iso3: str) -> str:
    return load_country_qids().get(iso3) or f"iso3:{iso3}"


async def test_language_layer_fixture_ingest_paths_and_idempotence(db: Any) -> None:
    out = {n: await _run(db, n) for n in SOURCES}
    g = out["atlas.glottolog"]["summary"]
    assert g["release"]["version"] == "v5.3" and g["release"]["licence"] == "CC BY 4.0"
    for n in SOURCES:
        kg = out[n]["summary"]["kg"]
        assert not kg["rejected_counts"], (n, kg["rejected_sample"])
        assert kg["statements_inserted"] > 0, n
    assert g["kg"]["entities_inserted"] == g["build"]["entities"]
    assert out["atlas.glottolog"]["registered"][0]["dataset_key"] == "atlas.glottolog.languages"
    brazil, portugal = _country("BRA"), _country("PRT")
    with db.owner.connect() as c:
        row = c.execute(
            text("""SELECT s.object_qid, s.qualifiers, s.raw_sha256, s.source_key, s.source_revision, s.refs, l.raw_sha256s
                    FROM kg.statement_current s JOIN kg.load l ON l.id = s.load_id
                    WHERE s.subject_qid = 'glottolog:port1283' AND s.property_pid = 'glottolog:spoken_in'
                      AND s.qualifiers->>'iso3' = 'BRA'""")
        ).one()
        assert row.object_qid == brazil and row.source_key == gl.SOURCE_KEY
        assert row.raw_sha256 == g["raw"]["zip_sha256"] and row.raw_sha256 in row.raw_sha256s
        assert row.source_revision == "v5.3 doi:10.5281/zenodo.18840967"
        assert row.refs == [
            {
                "stated_in": "Glottolog (CLDF)",
                "selector": {"table": "languages.csv", "row_id": "port1283", "column": "Countries"},
            }
        ]
        # the record JSON and the zip are verified raw artifacts (evidence acquisitions of the connector)
        n_acq = c.execute(
            text("SELECT count(*) FROM meta.artifact WHERE sha256 = ANY(:s)"),
            {"s": [g["raw"]["record_sha256"], g["raw"]["zip_sha256"]]},
        ).scalar_one()
        assert n_acq == 2
        # Wikidata cross-reference: community-edited source key, Wikidata statement id kept
        xref = c.execute(
            text("""SELECT object_qid, source_key, qualifiers FROM kg.statement_current
                    WHERE subject_qid = 'glottolog:port1283' AND property_pid = 'glottolog:wikidata_item'""")
        ).all()
        assert xref and {r.source_key for r in xref} == {"wikidata.wdqs"}
        assert "Q5146" in {r.object_qid for r in xref}
        assert all(r.qualifiers["wikidata_property"] == "P1394" for r in xref)
        # WALS / Grambank literals on the Glottolog languoid
        wo = c.execute(
            text("""SELECT value->>'value' FROM kg.statement_current
                    WHERE subject_qid = 'glottolog:nucl1643' AND property_pid = 'wals:f81a'""")
        ).scalar_one()
        assert wo == "SOV"
        assert (
            c.execute(text("SELECT label_en FROM kg.property_current WHERE pid = 'grambank:gb020'"))
            .scalar_one()
            .endswith("(Grambank GB020)")
        )
        # the neurons: Brazil <- spoken in - Portuguese - family -> Indo-European
        res = kq.paths(c, brazil, "glottolog:indo1319", max_hops=2)
        assert res["paths"], res
        assert [h["property"]["pid"] for h in res["paths"][0]["hops"]] == ["glottolog:spoken_in", "glottolog:family"]
        # the language layer's own hops: other tests sharing the session DB may link Brazil and Portugal or
        # Portuguese directly (test_links_db's Wikidata fixture: P37 official language), which is shorter (SWEEP2)
        lang_props = ["glottolog:spoken_in", "glottolog:family", "glottolog:in_group", "glottolog:wikidata_item"]
        res = kq.paths(c, brazil, portugal, max_hops=2, allowed_properties=lang_props)
        assert any(
            [h["property"]["pid"] for h in p["hops"]] == ["glottolog:spoken_in", "glottolog:spoken_in"]
            for p in res["paths"]
        ), res
        # Brazil -> Portuguese -> Romance <- French -> France (in_group hops)
        res = kq.paths(c, brazil, "glottolog:roma1334", max_hops=2)
        assert [h["property"]["pid"] for h in res["paths"][0]["hops"]] == ["glottolog:spoken_in", "glottolog:in_group"]
        res = kq.paths(c, brazil, "Q5146", max_hops=2, allowed_properties=lang_props)
        assert res["paths"] and res["paths"][0]["hops"][1]["property"]["pid"] == "glottolog:wikidata_item"
        # typology is literal: no path through a shared word order
        assert not c.execute(
            text(
                "SELECT count(*) FROM kg.statement_current WHERE property_pid LIKE 'wals:%' AND object_qid IS NOT NULL"
            )
        ).scalar_one()

    again = {n: await _run(db, n) for n in SOURCES}
    for n in SOURCES:
        k = again[n]["summary"]["kg"]
        assert k["statements_inserted"] == 0 and k["statements_new_revision"] == 0 and k["retractions_inserted"] == 0, n


async def test_dropped_fact_is_retracted(db: Any) -> None:
    from reva.kg.model import statement_key

    s = (await _run(db, "atlas.glottolog"))["summary"]
    zip_sha = s["raw"]["zip_sha256"]
    data = gl.parse_glottolog(db.store.get("raw", zip_sha))
    parts = gl.glottolog_kg(
        data,
        raw_sha256=zip_sha,
        source_revision=f"{s['release']['version']} doi:{s['release']['doi']}",
        country_qids=load_country_qids(),
    )
    gone = next(x for x in parts.statements if x.statement_id == "spoken_in:port1283:BR")
    kept = [x for x in parts.statements if x is not gone]
    rep = gl.load_kg(
        db.worker,
        db.store,
        entities=parts.entities,
        statements=kept,
        source_key=gl.SOURCE_KEY,
        raw_sha256s=[zip_sha],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
        parameters={"test": "retraction"},
    )
    assert rep["retractions_inserted"] == 1 and rep["statements_inserted"] == 0, rep
    with db.owner.connect() as c:
        assert not c.execute(
            text("SELECT count(*) FROM kg.statement_current WHERE statement_key = :k"),
            {"k": statement_key(gone, gl.SOURCE_KEY)},
        ).scalar_one()


def test_modules_are_discovered() -> None:
    assert {get_source(n).module for n in SOURCES} == {
        "reva.adapters.connectors.atlas_glottolog",
        aw.__name__,
        ag.__name__,
    }
