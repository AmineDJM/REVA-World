"""V4INDUSTRY atlas sources end to end on a throwaway database, through ``reva.atlas.ingest`` (no network)."""

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

TOYOTA, APPLE, SIEMENS_PK, GM = (
    "5493006W3QUS5LMH6R84",
    "HWUPKR0MPOU8FGXBT394",
    "529900IR3Y1XBIR1GX29",
    "54930070NSV60J38I987",
)


def _reg(db: Any, name: str) -> None:
    ensure_registered(db.owner, db.store, get_source(name).spec)


def _current(db: Any, sql: str, **kw: Any) -> list[Any]:
    with db.owner.connect() as c:
        return list(c.execute(text(sql), kw).all())


async def test_gleif_wikidata_then_entities(db):
    _reg(db, "atlas.gleif_wikidata")
    _reg(db, "atlas.gleif")
    wd = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.gleif_wikidata", mode="fixture")
    assert wd["status"] == "completed", wd
    assert wd["summary"]["kg"]["status"] == "loaded" and wd["summary"]["build"]["skipped"]["value_not_a_valid_lei"] == 3
    # seeds: the explicit LEIs only (the fixture holds exactly that request); parents are fetched in a 2nd pass
    out = await run_atlas_ingest(
        db.settings,
        db.worker,
        db.store,
        "atlas.gleif",
        dataset="entities",
        mode="fixture",
        options={"leis": [TOYOTA, APPLE, SIEMENS_PK, GM], "only_option_leis": True},
    )
    assert out["status"] == "completed", out
    ent = out["summary"]["entities"]
    assert ent["kg"]["status"] == "loaded" and ent["kg"]["rejected_counts"] == {}, ent
    assert ent["parents_fetched"] == 2 and ent["missing_count"] == 0
    rows = _current(
        db,
        """SELECT property_pid, object_qid FROM kg.statement_current WHERE subject_qid = :s AND origin = 'test_fixture'
           AND property_pid IN ('gleif:direct_parent', 'gleif:ultimate_parent')""",
        s=f"lei:{SIEMENS_PK}",
    )
    assert dict(rows) == {
        "gleif:direct_parent": "lei:529900QBVWXMWANH7H45",
        "gleif:ultimate_parent": "lei:W38RGI023J3WT1HWRP32",
    }
    assert ent["relationship_records"] == 2 and ent["relationship_record_errors"] == {}
    (q,) = _current(
        db,
        """SELECT qualifiers FROM kg.statement_current WHERE subject_qid = :s AND origin = 'test_fixture'
           AND property_pid = 'gleif:direct_parent'""",
        s=f"lei:{SIEMENS_PK}",
    )
    assert q[0]["relationship_corroboration_level"] == "FULLY_CORROBORATED" and "registration_status" not in q[0]
    # Toyota: GLEIF record -> GEM id; Wikidata Q53268 -> same LEI (P1278): a 2-hop identifier chain, no name match
    bridge = _current(
        db,
        """SELECT a.object_qid, b.object_qid FROM kg.statement_current a JOIN kg.statement_current b
           ON a.subject_qid = b.subject_qid WHERE a.subject_qid = :s AND a.property_pid = 'gleif:wikidata_item'
           AND b.property_pid = 'gleif:gem_entity' AND a.origin = 'test_fixture' AND b.origin = 'test_fixture'""",
        s=f"lei:{TOYOTA}",
    )
    assert bridge == [("Q53268", "gem:E100001016208")]
    again = await run_atlas_ingest(
        db.settings,
        db.worker,
        db.store,
        "atlas.gleif",
        dataset="entities",
        mode="fixture",
        options={"leis": [TOYOTA, APPLE, SIEMENS_PK, GM], "only_option_leis": True},
    )
    assert again["summary"]["entities"]["kg"]["statements_inserted"] == 0


async def test_gleif_counts_forms_groups(db):
    _reg(db, "atlas.gleif")
    out = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.gleif", dataset="lei_counts", mode="fixture")
    assert out["status"] == "completed", out
    c = out["summary"]["lei_counts"]
    assert {u["provider_geo"] for u in c["atlas"]["unmapped_provider_geo"]} == {"XK"}
    frame = read_atlas_table(db.store, c["atlas"]["artifact_sha256"])
    bv = frame.filter(pl.col("iso3") == "BVT")
    assert bv["indicator"].to_list() == ["lei_records|filter=all"] and bv["value"].to_list() == ["0"]
    fra = frame.filter((pl.col("iso3") == "FRA") & (pl.col("indicator") == "lei_records|filter=issued"))
    assert 50_000 < int(fra["value"][0]) < 192_860  # ISSUED is a subset of all FR records (192,860)
    forms = await run_atlas_ingest(
        db.settings,
        db.worker,
        db.store,
        "atlas.gleif",
        dataset="legal_form_counts",
        mode="fixture",
        options={"legal_form_countries": ["JP"]},
    )
    f = forms["summary"]["legal_form_counts"]
    t417 = read_atlas_table(db.store, f["atlas"]["artifact_sha256"]).filter(
        pl.col("indicator") == "lei_records|legal_form=T417"
    )
    assert t417["iso3"].to_list() == ["JPN"] and json.loads(t417["flags"][0])["elf_names"][0]["localName"] == "株式会社"
    grp = await run_atlas_ingest(
        db.settings, db.worker, db.store, "atlas.gleif", dataset="groups", mode="fixture", options={"top_groups": 3}
    )
    gs = grp["summary"]["groups"]
    assert gs["groups"] == 3 and gs["kg"]["status"] == "loaded" and gs["top10"][0][1] == "IGJSJL3JD5P30I6NJZ34"


async def test_tiva(db):
    _reg(db, "atlas.oecd_tiva")
    out = await run_atlas_ingest(
        db.settings,
        db.worker,
        db.store,
        "atlas.oecd_tiva",
        mode="fixture",
        options={"areas": ["FRA", "DEU", "CHN"], "start_year": 2021, "end_year": 2022},
    )
    assert out["status"] == "completed", out
    s = out["summary"]
    assert s["edition_years"] == [1995, 2022]
    main = read_atlas_table(db.store, s["tiva_principal"]["atlas"]["artifact_sha256"])
    assert set(main["iso3"].unique()) == {"FRA", "DEU", "CHN"}
    fd = read_atlas_table(db.store, s["tiva_fdva"]["atlas"]["artifact_sha256"])
    sec = fd.filter(pl.col("indicator") == "FDVA|fd_area=FRA|va_activity=C")
    assert set(sec["iso3"]) == {"FRA", "DEU", "CHN"} and sec["period_start"].dt.year().unique().to_list() == [2022]


async def test_gleif_wikidata_replay_retracts_ambiguous_links(db, monkeypatch):
    """AUDIT3 M8: links loaded before the identity rules are retracted on a replay of the same archived bytes,
    with the derived reason ``reclassified_by_reva`` (the source still states them; REVA no longer links them)."""
    from reva.adapters.connectors import atlas_gleif as g

    _reg(db, "atlas.gleif_wikidata")
    real = g.apply_identity_rules
    monkeypatch.setattr(g, "apply_identity_rules", lambda c: (list(c), {"linked": len(c)}))  # pre-fix behaviour
    first = await run_atlas_ingest(db.settings, db.worker, db.store, "atlas.gleif_wikidata", mode="fixture")
    assert first["status"] == "completed", first
    sha = first["summary"]["raw_sha256"]
    monkeypatch.setattr(g, "apply_identity_rules", real)
    again = await run_atlas_ingest(
        db.settings, db.worker, db.store, "atlas.gleif_wikidata", mode="fixture",
        options={"replay_raw_sha256": sha},
    )  # fmt: skip
    assert again["status"] == "completed", again
    s = again["summary"]
    assert s["replayed"] and s["kg"]["statements_inserted"] == 0 and s["kg"]["retractions_inserted"] == 2
    rows = _current(
        db,
        """SELECT r.reason, r.detail->>'basis' FROM kg.statement_retraction r JOIN kg.statement st
           ON st.id = r.statement_row_id WHERE st.subject_qid = 'lei:W38RGI023J3WT1HWRP32' AND r.origin = 'test_fixture'""",
    )
    assert sorted(rows) == [("reclassified_by_reva", "raw_unchanged")] * 2
    left = _current(
        db,
        """SELECT count(*) FROM kg.statement_current WHERE property_pid = 'gleif:wikidata_item'
           AND subject_qid = 'lei:W38RGI023J3WT1HWRP32' AND origin = 'test_fixture'""",
    )
    assert left == [(0,)]
