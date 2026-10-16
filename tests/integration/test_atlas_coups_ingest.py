"""A-COUPS sources end to end on a throwaway database through the atlas platform (fixture mode: no network).

Powell & Thyne coups, Archigos 4.1 leaders (with the exact coup join and its coded claims) and the REIGN-format
leader list: raw slices archived as evidence acquisitions, atlas tables registered, KG records loaded without
rejection, the T1 ``coded_outcome`` claims loaded when CAUSCORE's schema exists; re-running is a no-op.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_coups, atlas_leaders
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered, get_source
from reva.atlas.tables import read_atlas_table

pytestmark = pytest.mark.db


async def _run(db: Any, name: str) -> dict[str, Any]:
    ensure_registered(db.owner, db.store, get_source(name).spec)
    out = await run_atlas_ingest(db.settings, db.worker, db.store, name, mode="fixture")
    assert out["status"] == "completed", out
    return out["summary"]


def _scalar(db: Any, sql: str, **kw: Any) -> Any:
    with db.owner.connect() as c:
        return c.execute(text(sql), kw).scalar_one()


async def test_coups_ingest(db: Any) -> None:
    s = await _run(db, atlas_coups.CONNECTOR_NAME)
    assert s["dataset_version"] == "V2026.08.29" and s["events"] == 69
    assert s["atlas"]["coup_events"]["created"] and s["atlas"]["coups_cy"]["created"]
    assert s["provider_note"]["date"] == "2026-08-29"
    kg = s["kg"]
    assert not kg["rejected"], kg
    assert kg["statements_inserted"] == s["kg_counts"]["statements"]
    # Chile 1973-09-11: successful, against Chile (QID through the crosswalk), day precision
    eid = "ptcoup:155:1973-09-11"
    assert (
        _scalar(
            db,
            "SELECT value->>'value' FROM kg.statement_current WHERE subject_qid = :e AND property_pid = 'coup:outcome'",
            e=eid,
        )
        == "successful"
    )
    assert _scalar(
        db,
        "SELECT object_qid FROM kg.statement_current WHERE subject_qid = :e AND property_pid = 'coup:against_state'",
        e=eid,
    ).startswith("Q")
    assert (
        _scalar(db, "SELECT kind FROM kg.entity WHERE qid = :e ORDER BY id DESC LIMIT 1", e="ptcoup:365:1991-08-21")
        == "event"
    )
    again = await _run(db, atlas_coups.CONNECTOR_NAME)
    assert not again["atlas"]["coups_cy"]["created"] and again["kg"]["statements_inserted"] == 0


async def test_coups_atlas_rows(db: Any) -> None:
    await _run(db, atlas_coups.CONNECTOR_NAME)
    art = _scalar(
        db,
        "SELECT artifact_sha256 FROM evidence.dataset WHERE dataset_key = 'atlas.powell_thyne.coups_cy' "
        "ORDER BY recorded_at DESC LIMIT 1",
    )
    f = read_atlas_table(db.store, art)
    chl = f.filter((f["iso3"] == "CHL") & (f["indicator"] == "COUPS_SUCCESSFUL"))
    assert [(r["period_start"].year, r["value"]) for r in chl.iter_rows(named=True)] == [(1973, "1")]
    # USSR 1991 (COW 365 before 1991-12-20) stays a historical state, with its QID
    ussr = f.filter(f["provider_geo"] == "cow:365")
    assert ussr["iso3"].null_count() == ussr.height and set(ussr["entity_qid"]) == {"Q15180"}


async def test_archigos_ingest_with_coup_links(db: Any) -> None:
    await _run(db, atlas_coups.CONNECTOR_NAME)  # coup event entities (the links point at them)
    s = await _run(db, atlas_leaders.ARCHIGOS_NAME)
    assert s["archigos"]["rows"] == 165
    assert s["archigos"]["encoding_fallbacks"], "the Honduras row is Windows-1252 in Archigos 4.1"
    assert s["atlas"]["leaders_cy"]["created"]
    assert not s["kg"]["rejected"], s["kg"]
    join = s["join"]
    assert join["coup_removals"] >= 20 and join["pt_events_with_several_matches"] == {}
    # Allende, 1973-09-11: Archigos codes his removal by the military; its own coup event names him, and a context
    # edge (same state, same day) points at the P&T event
    cid = _scalar(
        db,
        "SELECT subject_qid FROM kg.statement_current WHERE property_pid = 'coup:same_state_and_day_as' "
        "AND object_qid = 'ptcoup:155:1973-09-11'",
    )
    assert cid.startswith("archigos:coup:CHL-")
    leader = _scalar(
        db,
        "SELECT object_qid FROM kg.statement_current WHERE subject_qid = :c AND property_pid = 'coup:leader_removed'",
        c=cid,
    )
    assert leader.startswith("archigos:leader:")
    assert (
        _scalar(
            db,
            "SELECT qualifiers->>'exitcode' FROM kg.statement_current WHERE subject_qid = :l "
            "AND property_pid = 'archigos:leader_of' AND end_time = '1973-09-11'",
            l=leader,
        )
        == "Removed by Military, without Foreign Support"
    )
    causal = s["causal"]
    if causal["status"] == "loaded":
        assert causal["inserted"] == join["coup_removals"], causal
        row = (
            db.owner.connect()
            .execute(
                text(
                    "SELECT relation, effect_id FROM kg.causal_claim_current WHERE source_key = 'archigos.leaders' "
                    "AND cause_id = :c"
                ),
                {"c": cid},
            )
            .one()
        )
        assert row[0] == "coded_outcome" and row[1] == cid.replace(":coup:", ":exit:")
    else:
        assert causal["reason"]
    again = await _run(db, atlas_leaders.ARCHIGOS_NAME)
    assert again["kg"]["statements_inserted"] == 0
    if again["causal"]["status"] == "loaded":
        assert again["causal"]["inserted"] == 0


async def test_leaderlist_ingest(db: Any) -> None:
    s = await _run(db, atlas_leaders.LEADERLIST_NAME)
    rep = s["leaderlist"]
    assert rep["updated"] == "2026-04-14" and len(rep["provider_defects"]) == 3
    assert s["atlas"]["leaders_cy"]["created"]
    assert not s["kg"]["rejected"], s["kg"]
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM kg.statement_current WHERE property_pid = 'reign:leader_of' "
            "AND qualifiers ? 'provider_defect'",
        )
        == 3
    )
