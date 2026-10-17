"""V6POLITICS sources end to end on a throwaway database (fixture mode, no network): ParlGov cabinets/elections/
parties, NELDA election rounds, OxCGRT policy indices and changes, WhoGov governments, and the derived BIS policy-rate
and OECD CIT change tables. Atlas tables registered, KG loaded without rejection, events typed by the taxonomy,
re-runs are no-ops."""

from __future__ import annotations

from typing import Any

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
    summary: dict[str, Any] = out["summary"]
    return summary


def _scalar(db: Any, sql: str, **kw: Any) -> Any:
    with db.owner.connect() as c:
        return c.execute(text(sql), kw).scalar_one()


async def test_parlgov_cabinets_ingest(db: Any) -> None:
    s = await _run(db, "atlas.parlgov_cabinets")
    assert s["atlas"]["cabinets"]["created"] and s["atlas"]["cabinets_cy"]["created"]
    kg = s["kg"]
    assert not kg["rejected"], kg
    assert kg["statements_inserted"] == s["report"]["kg_statements"]
    # a German cabinet: start day, country, coalition parties
    cab = _scalar(
        db,
        """SELECT subject_qid FROM kg.statement_current WHERE property_pid = 'P580'
                         AND value->>'time' LIKE '2005-11-22%' AND subject_qid LIKE 'parlgov:cabinet:%'""",
    )
    n = _scalar(db, "SELECT count(*) FROM kg.statement_current WHERE subject_qid = :c AND property_pid = "
                "'parlgov:cabinet_party'", c=cab)  # fmt: skip
    assert n == 2
    from reva.events.view import event_card

    with db.owner.connect() as c:
        card = event_card(c, cab)
    assert card is not None and "cabinet_formation" in str(card)
    again = await _run(db, "atlas.parlgov_cabinets")
    assert again["kg"]["statements_inserted"] == 0 and not again["atlas"]["cabinets"]["created"]


async def test_nelda_ingest(db: Any) -> None:
    s = await _run(db, "atlas.nelda")
    assert s["report"]["rounds"] == 6 and s["report"]["month_precision"] == 1
    assert not s["kg"]["rejected"], s["kg"]
    prec = _scalar(db, "SELECT value->>'precision' FROM kg.statement_current WHERE subject_qid = "
                   "'nelda:election:625-1974-0400-L1' AND property_pid = 'P585'")  # fmt: skip
    assert prec == "10"
    art = _scalar(db, "SELECT artifact_sha256 FROM evidence.dataset WHERE dataset_key = 'atlas.nelda.elections_cy' "
                  "ORDER BY recorded_at DESC LIMIT 1")  # fmt: skip
    f = read_atlas_table(db.store, art)
    fra = f.filter(f["provider_geo"] == "gw:220")
    assert fra["value"].to_list() == ["2"] and fra["iso3"].to_list() == ["FRA"]


async def test_oxcgrt_ingest(db: Any) -> None:
    s = await _run(db, "atlas.oxcgrt")
    assert set(s["atlas"]) == {"indices_daily", "policy_changes", "fiscal_announcements"}
    assert s["report"]["countries"] == 2 and s["atlas"]["policy_changes"]["row_count"] == s["report"]["change_rows"]
    assert all(not v["unmapped_provider_geo"] for v in s["atlas"].values())


async def test_whogov_ingest(db: Any) -> None:
    s = await _run(db, "atlas.whogov")
    assert s["atlas"]["cabinet_cy"]["created"] and s["atlas"]["government_starts"]["created"]
    assert not s["kg"]["rejected"], s["kg"]
    assert _scalar(db, "SELECT count(DISTINCT subject_qid) FROM kg.statement_current WHERE subject_qid LIKE "
                   "'whogov:government:DEU:%' AND property_pid = 'P580'") > 0  # fmt: skip


async def test_derived_change_tables(db: Any) -> None:
    skipped = await _run(db, "atlas.bis_cbpol_changes")
    assert "skipped" in skipped  # input not registered yet: nothing derived, nothing invented
    await _run(db, "atlas.bis_daily")
    s = await _run(db, "atlas.bis_cbpol_changes")
    assert s["report"]["changes"] == s["atlas"]["row_count"] > 0
    await _run(db, "atlas.oecd.fiscal")
    c = await _run(db, "atlas.oecd_cit_changes")
    assert "atlas" in c and c["input"]["dataset_key"] == "atlas.oecd.cts_cit"
