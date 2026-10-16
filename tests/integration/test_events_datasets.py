"""Dataset events in the event view (EVENTS), from the finished agents' fixture-mode ingests (no network):
UCDP conflicts, COW wars, UN peace operations, UNTC treaties and CShapes status changes (derived independence),
plus evidence-based ``same_as`` links (UNTC mapping table, shared MTDSG number) and their retraction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from reva.adapters.connectors import atlas_cow, atlas_cshapes, atlas_peacekeeping, atlas_treaties, atlas_ucdp
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.events import same_as as sa
from reva.events import view as ev
from reva.kg.model import KgEntity, KgStatement, load_batch

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
async def datasets(db: Any) -> Any:
    for module in (atlas_ucdp, atlas_cow, atlas_cshapes, atlas_peacekeeping, atlas_treaties):
        spec = module.atlas_source()
        ensure_registered(db.owner, db.store, spec)
        out = await run_atlas_ingest(db.settings, db.worker, db.store, spec.name, mode="fixture")
        assert out["status"] == "completed", out
    return db


async def test_ucdp_conflict_card(datasets: Any) -> None:
    with datasets.api.connect() as c:
        card = ev.event_card(c, "ucdp:conflict:246", origin="test_fixture")
    assert card is not None
    assert [t["id"] for t in card["types"]["primary"]] == ["extrasystemic_armed_conflict"]
    assert card["types"]["basis"] == "dataset_kind"
    iv = card["interval"]
    assert iv["start"]["time"] == "1954-11-01" and iv["start"]["provenance"]["source_key"] == "ucdp.downloads"
    assert iv["episode_end"]["time"] == "1962-03-19" and iv["latest"] == "1962-03-19"
    sides = {(p["participant"]["id"], p["role"]["id"], p["side"]) for p in card["participants"]}
    assert ("Q142", "belligerent", "a") in sides and any(s[2] == "b" for s in sides)
    assert any(p["relation"] == "country" and p["place"]["id"] == "Q262" for p in card["places"])
    assert {x["property"] for x in card["coded"]} >= {"ucdp:conflict_type", "ucdp:incompatibility"}


async def test_cow_war_interval_is_derived_from_spells(datasets: Any) -> None:
    with datasets.api.connect() as c:
        card = ev.event_card(c, "cow:war:1", origin="test_fixture")
    assert card is not None and card["types"]["primary"][0]["id"] == "war"
    iv = card["interval"]
    assert iv["basis"] == "derived_from_participation_spells" and iv["earliest"] == "1823-04-07"
    assert any("derived by REVA" in n for n in iv["notes"])
    bel = [p for p in card["participants"] if p["role"]["id"] == "belligerent"]
    assert {p["side"] for p in bel} == {"1", "2"} and bel[0]["statements"][0]["coded"]["war_type"]


async def test_peace_operation_and_treaty_cards(datasets: Any) -> None:
    with datasets.api.connect() as c:
        unef = ev.event_card(c, "unpk:mission:UNEF_I", origin="test_fixture")
        rows = (
            c.execute(
                text(
                    "SELECT DISTINCT subject_qid FROM kg.statement_current WHERE property_pid = "
                    "'untc:party' ORDER BY 1 LIMIT 1"
                )
            )
            .scalars()
            .all()
        )
        treaty = ev.event_card(c, rows[0], origin="test_fixture") if rows else None
    assert unef is not None and unef["types"]["primary"][0]["id"] == "peacekeeping_mission"
    assert unef["interval"]["start"]["time"] == "1956-11-01"
    assert treaty is not None and treaty["types"]["primary"][0]["id"] == "treaty"
    roles = {p["role"]["id"] for p in treaty["participants"]}
    assert "party" in roles


async def test_cshapes_independence_is_derived_and_labelled(datasets: Any) -> None:
    with datasets.api.connect() as c:
        evs = ev.cshapes_independence_events(c, origin="test_fixture")
        dz = [e for e in evs if e["territory"] == "cshapes:gw:615"]
        assert [e["date"] for e in dz] == ["1962-07-05"]
        card = ev.event_card(c, dz[0]["id"], origin="test_fixture")
        listed = ev.list_events(c, types=["independence"], start="1962", end="1962", origin="test_fixture", limit=200)
        dza = ev.list_events(c, types=["independence"], country="Q262", origin="test_fixture")
    assert card is not None and card["derived"]["method"] == "reva.events.cshapes_status_change/1"
    assert card["types"]["primary"][0]["id"] == "independence"
    assert card["interval"]["point_in_time"]["lo"] == "1962-07-05"
    assert (
        card["participants"][0]["role"]["id"] == "former_ruler"
        and card["participants"][0]["participant"]["id"] == "Q142"
    )
    assert dz[0]["id"] in {i["id"] for i in listed["items"]}
    assert [i["id"] for i in dza["items"]] == [dz[0]["id"]]


async def test_listing_mixes_wikidata_and_dataset_events(datasets: Any) -> None:
    with datasets.api.connect() as c:
        res = ev.list_events(
            c, types=["armed_conflict"], start="1954", end="1962", country="Q262", origin="test_fixture"
        )
    ids = {i["id"] for i in res["items"]}
    assert "ucdp:conflict:246" in ids


async def test_same_as_from_evidence_only(datasets: Any) -> None:
    db = datasets
    with db.api.connect() as c:
        has_paris = c.execute(
            text("SELECT count(*) FROM kg.statement_current WHERE subject_qid = 'Q21707860'")
        ).scalar_one()
    if not has_paris:
        pytest.skip("UNTC fixture has no Paris Agreement rows")
    # an untc:treaty record as a load made before the mapping would have keyed it, with the same MTDSG number
    raw = db.store.put_bytes(b"events same_as test record", "raw", "application/json").sha256
    rep = load_batch(
        db.worker,
        db.store,
        entities=[KgEntity("untc:treaty:XXVII-7-d", "treaty", {"en": "Paris Agreement"})],
        statements=[
            KgStatement(
                "untc:treaty:XXVII-7-d",
                "untc:mtdsg_number",
                value={"type": "string", "value": "XXVII-7-d"},
                statement_id="m1",
            )
        ],
        source_key="eventstest.untc",
        raw_sha256s=[raw],
        retrieved_at=datetime.now(UTC),
        origin="test_fixture",
    )
    assert rep.rejected_total == 0, rep.rejected
    with db.worker.connect() as c:
        report = sa.derive_links(c, origin="test_fixture")
    rules = {x.rule for x in report.links}
    assert {"untc_treaty_qids", "untc_mtdsg_number"} <= rules
    assert all({x.left, x.right} == {"untc:treaty:XXVII-7-d", "Q21707860"} for x in report.links)
    assert {s["rule"] for s in report.skipped_rules} >= {"ucdp_conflict_id_on_wikidata", "cow_war_number_on_wikidata"}
    out = sa.load_links(db.worker, db.store, report, origin="test_fixture", rules_sha256=sa.rules_sha256())
    assert out.statements_inserted == len(report.links) and out.rejected_total == 0
    again = sa.load_links(db.worker, db.store, report, origin="test_fixture", rules_sha256=sa.rules_sha256())
    assert again.statements_inserted == 0
    with db.api.connect() as c:
        card = ev.event_card(c, "Q21707860", origin="test_fixture")
    assert card is not None and {x["event"]["id"] for x in card["same_as"]} == {"untc:treaty:XXVII-7-d"}
    ev_types = {x["evidence"]["type"] for x in card["same_as"]}
    assert ev_types == {"mapping_table", "shared_identifier"}
    assert all(x["provenance"]["verification"] == "derived" for x in card["same_as"])
    # the evidence disappears (no active rule): the links are retracted, never silently kept
    empty = sa.SameAsReport([], [], [])
    gone = sa.load_links(db.worker, db.store, empty, origin="test_fixture")
    assert gone.retractions_inserted == len(report.links)
    with db.api.connect() as c:
        card = ev.event_card(c, "Q21707860", origin="test_fixture")
    assert card is not None and card["same_as"] == []
