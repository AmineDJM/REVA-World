"""Live ingest of the A-LAW atlas connectors into the throwaway test database (deselected by default).

Runs WBL (3 XLSX), Constitute (1 JSON), CCP (chronology ZIP; characteristics refused before any
request by the 50 MB response bound), the Factbook legal fields (255 JSON files at a pinned commit)
and the SSA SSPTW access probe through the budget path of the ingest job (``reserve_ingest`` ->
``metered`` -> ``settle_metered``, pinned ``registry/policies/budgets.yaml``). The ``db`` fixture's
database and object root are temporary.

``REVA_LAW_LIVE_OUT=<path.json>`` writes the per-country coverage used by docs/dev/v3_a_law.md.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_ccp, atlas_constitute, atlas_factbook, atlas_ssa_ssptw, atlas_wbl
from reva.adapters.connectors.atlas_law_common import register_connector
from reva.atlas.tables import known_country_keys, read_atlas_table
from reva.evidence.runner import make_live_client
from reva.operations.budgets import metered, reserve_ingest, settle_metered

pytestmark = [pytest.mark.live, pytest.mark.db]


async def _live(db: Any, module: Any, conn: Any, planned: int, **kw: Any) -> dict[str, Any]:
    spec = conn.describe()
    register_connector(db.owner, db.store, spec)
    reservation = reserve_ingest(
        db.worker, job_id=uuid.uuid4(), attempt=1, source_key=spec.source_key, planned_requests=planned
    )
    http = metered(make_live_client(spec, db.settings), db.worker, reservation)
    try:
        rep = await module.run(db.worker, db.store, http, origin="live", **kw)
    finally:
        budget = settle_metered(db.worker, http)
        await http.aclose()
    out = rep.as_dict()
    out["budget"] = budget
    return out


def _latest(frame: pl.DataFrame, indicator: str, *, numeric: bool = True) -> dict[str, tuple[str, str]]:
    col = "value" if numeric else "value_text"
    sub = frame.filter((pl.col("indicator") == indicator) & pl.col("iso3").is_not_null() & pl.col(col).is_not_null())
    sub = sub.sort("period_start").group_by("iso3").agg(pl.col(col).last(), pl.col("period_start").last())
    return {r["iso3"]: (r[col], str(r["period_start"])) for r in sub.to_dicts()}


async def test_live_ingest_law(db) -> None:
    out: dict[str, Any] = {"runs": {}}
    runs = out["runs"]
    runs["wbl"] = await _live(db, atlas_wbl, atlas_wbl.WblConnector(), 3)
    runs["constitute"] = await _live(db, atlas_constitute, atlas_constitute.ConstituteConnector(), 1)
    runs["ccp"] = await _live(db, atlas_ccp, atlas_ccp.CcpConnector(), 1)
    runs["factbook"] = await _live(db, atlas_factbook, atlas_factbook.FactbookConnector(), 255)
    runs["ssa"] = await _live(db, atlas_ssa_ssptw, atlas_ssa_ssptw.SsaSsptwConnector(), 1)

    assert runs["wbl"]["status"] == "ok", runs["wbl"]
    assert runs["constitute"]["status"] == "ok", runs["constitute"]
    assert [b["reason"] for b in runs["ccp"]["blocked"]] == ["response_cap"] and runs["ccp"]["tables"]
    assert runs["factbook"]["tables"], runs["factbook"]
    assert runs["ssa"]["status"] == "blocked"  # typed, nothing extracted

    tables = {t["dataset_key"]: t for r in runs.values() for t in r["tables"]}
    frames = {k: read_atlas_table(db.store, t["artifact_sha256"]) for k, t in tables.items()}
    wbl26, wbl1, panel = frames["atlas.wbl.wbl2026"], frames["atlas.wbl.wbl1_2026"], frames["atlas.wbl.panel_1971_2024"]
    con, chron, fb = frames["atlas.constitute.constitutions"], frames["atlas.ccp.chronology_v6"], frames["atlas.factbook.legal_2026_01"]
    per: dict[str, dict[str, Any]] = {k: {} for k in sorted(known_country_keys())}
    cols = {
        "constitution_year_enacted": (con, "in_force.year_enacted", True),
        "constitution_latest_revision": (con, "in_force.year_updated", True),
        "wbl1_index": (wbl1, "WBL INDEX", True),
        "wbl1_index_panel": (panel, "WBL INDEX", True),
        "wbl2026_lf_index": (wbl26, "LF_econ_index", True),
        "retire_full_men": (wbl26, "Pen_Law_PensAgeEq_FullM", True),
        "retire_full_women": (wbl26, "Pen_Law_PensAgeEq_FullW", True),
        "retire_full_men_text": (wbl26, "Pen_Law_PensAgeEq_FullM", False),
        "marriage_18_both": (wbl26, "Saf_Law_ChildMarr_Age", False),
        "voting_age": (fb, "suffrage.minimum_age", True),
        "suffrage_text": (fb, "suffrage", False),
        "legal_families": (fb, "legal_system.families_mentioned", False),
    }
    for name, (frame, ind, numeric) in cols.items():
        for iso, v in _latest(frame, ind, numeric=numeric).items():
            if iso in per:
                per[iso][name] = v
    # CCP chronology: last 'new' event and number of amendment years (computed here for the report only)
    ev = chron.filter(pl.col("iso3").is_not_null() & pl.col("indicator").str.starts_with("evnttype"))
    ev = ev.with_columns(pl.col("period_start").dt.year().alias("y"))
    for r in (
        ev.group_by("iso3")
        .agg(
            pl.col("y").filter(pl.col("value_text") == "new").max().alias("last_new"),
            (pl.col("value_text") == "amendment").sum().alias("amend_years"),
            pl.col("y").max().alias("last_year"),
        )
        .to_dicts()
    ):
        if r["iso3"] in per:
            per[r["iso3"]]["ccp_last_new"] = r["last_new"]
            per[r["iso3"]]["ccp_amendment_years"] = r["amend_years"]
            per[r["iso3"]]["ccp_last_year"] = r["last_year"]
    out["per_country"] = per
    out["tables"] = tables
    counts = {name: sum(1 for p in per.values() if name in p) for name in [*cols, "ccp_last_new"]}
    out["counts"] = counts
    assert counts["retire_full_women"] >= 170 and counts["constitution_year_enacted"] >= 185
    assert counts["wbl1_index"] >= 185 and counts["voting_age"] >= 180
    fra = per["FRA"]
    assert fra["constitution_year_enacted"][0] == "1958" and fra["voting_age"][0] == "18"
    path = os.environ.get("REVA_LAW_LIVE_OUT")
    if path:
        with open(path, "w") as f:
            json.dump(out, f, indent=1, default=str)
