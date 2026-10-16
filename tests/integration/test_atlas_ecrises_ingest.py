"""A-ECRISES crisis datasets end to end on the per-session throwaway database (atlas platform, fixture mode).

For each of the four sources: the raw workbook is archived as an evidence acquisition, the atlas table is
registered, the crisis events are loaded into the KG (kind ``event``), the events / coded-relations datasets are
registered, and the ESRB "accelerator" claims go through CAUSCORE's ``load_dataset_claims`` (T1) and are graded.
A second run with the same bytes changes nothing.
"""

from __future__ import annotations

from typing import Any

import polars as pl
import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_ecrises as m
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


async def _run(db: Any, key: str) -> dict[str, Any]:
    name = m.SOURCES[key].name
    ensure_registered(db.owner, db.store, get_source(name).spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert out["status"] == "completed", out
    return out["summary"]


async def test_laeven_valencia_and_esrb_end_to_end(db: Any) -> None:
    s = await _run(db, m.LV)
    assert s["atlas"]["dataset_key"] == "atlas.laeven_valencia.sbcd" and s["atlas"]["created"] is True
    assert {u["provider_geo"] for u in s["unmapped_provider_geo"]} == {"Yugoslavia, SFR", m.LV_WORLD_GEO}
    assert not s["kg"]["rejected_counts"], s["kg"]
    assert s["kg"]["entities_inserted"] == s["events"] == 546
    assert s["datasets"]["events"]["rows"] == 546 and s["datasets"]["coded_relations"]["rows"] == 77
    assert s["causal"]["status"] == "no_coded_causal_relations"
    f = read_atlas_table(db.store, s["atlas"]["artifact_sha256"])
    assert (
        f.filter((pl.col("iso3") == "ISL") & (pl.col("indicator") == "crisis_start|type=systemic_banking")).height == 1
    )
    with db.owner.connect() as c:
        rows = c.execute(
            text(
                """SELECT property_pid, object_qid, value FROM kg.statement_current
                   WHERE subject_qid = 'ecrises:lv:systemic_banking:ISL:2008' ORDER BY property_pid"""
            )
        ).all()
        kind = c.execute(text("SELECT kind FROM kg.entity WHERE qid = 'ecrises:lv:systemic_banking:ISL:2008'")).scalar()
        ctx = c.execute(
            text("SELECT count(*) FROM kg.statement_current WHERE property_pid = 'ecrises:same_crisis_window'")
        ).scalar_one()
    props = {r[0]: r for r in rows}
    assert kind == "event" and props["P31"][1] == "Q806729" and props["P580"][2]["time"].startswith("2008-")
    assert ctx == 77

    e = await _run(db, m.ESRB)
    assert e["causal"]["status"] == "loaded", e["causal"]
    rep = e["causal"]["report"]
    assert rep["inserted"] == e["relations"]["causal:contributing_factor"] > 50 and not rep["rejected_counts"]
    assert rep["unknown_entities"] == 0  # events and condition entities were loaded into the KG first
    with db.owner.connect() as c:
        claims = c.execute(
            text(
                """SELECT cause_id, relation, source_tier, mechanism FROM kg.causal_claim_current
                   WHERE effect_id = 'ecrises:esrb:systemic:AUT:2007-12' ORDER BY cause_id"""
            )
        ).all()
        grades = c.execute(
            text(
                """SELECT g.grade, g.temporal_check FROM kg.causal_grade_current g
                   JOIN kg.causal_claim_current k ON k.id = g.claim_row_id
                   WHERE k.effect_id = 'ecrises:esrb:systemic:AUT:2007-12'"""
            )
        ).all()
    assert len(claims) == 4 and {r[1] for r in claims} == {"contributing_factor"} and {r[2] for r in claims} == {"T1"}
    assert all(r[3].startswith("Excessive credit growth") for r in claims)
    assert grades and {g[1] for g in grades} == {"undetermined"}  # the condition is undated in the source

    again = await _run(db, m.ESRB)
    assert again["atlas"]["created"] is False
    assert again["kg"]["statements_inserted"] == 0 and again["kg"]["entities_inserted"] == 0
    assert again["causal"]["report"]["inserted"] == 0 and again["causal"]["report"]["unchanged"] == rep["inserted"]


async def test_reinhart_rogoff_and_jst_end_to_end(db: Any) -> None:
    r = await _run(db, m.RR)
    assert r["atlas"]["dataset_key"] == "atlas.reinhart_rogoff.global_crises" and not r["unmapped_provider_geo"]
    assert not r["kg"]["rejected_counts"] and r["events"] == r["kg"]["entities_inserted"]
    assert r["causal"]["status"] == "no_coded_causal_relations" and "coded_relations" not in r["datasets"]
    j = await _run(db, m.JST)
    assert j["atlas"]["dataset_key"] == "atlas.jst.macrohistory" and j["licence_page_sha256"]
    with db.owner.connect() as c:
        n_ds = c.execute(
            text(
                "SELECT count(*) FROM evidence.dataset WHERE dataset_key LIKE 'ecrises.%' OR dataset_key LIKE 'atlas.jst.%'"
            )
        ).scalar_one()
        ev = c.execute(
            text(
                """SELECT value FROM kg.statement_current
                   WHERE subject_qid = 'ecrises:rr:banking:USA:2007' AND property_pid = 'P582'"""
            )
        ).scalar()
    assert n_ds >= 3  # atlas.jst.macrohistory + ecrises.reinhart_rogoff.events + ecrises.jst.events
    assert ev is not None and int(ev["time"][:4]) >= 2008
