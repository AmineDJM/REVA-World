"""Live ingest of the A-LAW atlas sources into the throwaway test database (deselected by default).

Goes through the ATLASCORE entry point ``reva.atlas.ingest.run_atlas_ingest(mode="live")``: budget
reservation on each source's key, the platform's metered SafeHttpClient (this test builds no client),
settlement with actual usage. WBL (3 XLSX), Constitute (1 JSON), CCP (chronology + 118 MB
characteristics ZIP), the Factbook legal fields (255 JSON at a pinned commit) and the SSA SSPTW probe.
The ``db`` fixture's database and object root are temporary.

``REVA_LAW_LIVE_OUT=<path.json>`` writes the per-country coverage used by docs/dev/v3_a_law.md.
"""

from __future__ import annotations

import json
import os
from typing import Any

import polars as pl
import pytest

from reva.adapters.connectors import atlas_ccp, atlas_constitute, atlas_factbook, atlas_ssa_ssptw, atlas_wbl
from reva.atlas.ingest import run_atlas_ingest
from reva.atlas.sources import ensure_registered
from reva.atlas.tables import known_country_keys, read_atlas_table

pytestmark = [pytest.mark.live, pytest.mark.db]


async def _live(db: Any, module: Any) -> dict[str, Any]:
    from reva.operations.budgets import BudgetExhausted

    spec = module.atlas_source()
    if isinstance(spec, list):  # atlas_factbook declares two sources; the first is atlas.factbook
        spec = spec[0]
    ensure_registered(db.owner, db.store, spec)
    try:
        out = await run_atlas_ingest(db.settings, db.worker, db.store, spec.name, mode="live")
    except BudgetExhausted as e:  # pinned budget does not (yet) hold this run: refused before any request
        return {"status": "budget_refused", "tables": [], "blocked": [], "problem": e.problem()}
    rep = dict(out["summary"] or {})
    rep["platform_status"], rep["budget"] = out["status"], out["budget"]
    return rep


def _latest(frame: pl.DataFrame, indicator: str, *, numeric: bool = True) -> dict[str, tuple[str, str]]:
    col = "value" if numeric else "value_text"
    sub = frame.filter((pl.col("indicator") == indicator) & pl.col("iso3").is_not_null() & pl.col(col).is_not_null())
    sub = sub.sort("period_start").group_by("iso3").agg(pl.col(col).last(), pl.col("period_start").last())
    return {r["iso3"]: (r[col], str(r["period_start"])) for r in sub.to_dicts()}


async def test_live_ingest_law(db) -> None:
    out: dict[str, Any] = {"runs": {}}
    runs = out["runs"]
    for key, module in (
        ("wbl", atlas_wbl),
        ("constitute", atlas_constitute),
        ("ccp", atlas_ccp),
        ("factbook", atlas_factbook),
        ("ssa", atlas_ssa_ssptw),
    ):
        runs[key] = await _live(db, module)

    assert runs["wbl"]["status"] == "ok", runs["wbl"]
    assert runs["constitute"]["status"] == "ok", runs["constitute"]
    assert runs["ccp"]["status"] == "ok", runs["ccp"]
    assert runs["factbook"]["tables"], runs["factbook"]
    assert runs["ssa"]["status"] in ("ok", "partial", "budget_refused"), runs["ssa"]

    tables = {t["dataset_key"]: t for r in runs.values() for t in r["tables"]}
    ssa = tables.get("atlas.ssa.ssptw_2018_2019")
    frames = {k: read_atlas_table(db.store, t["artifact_sha256"]) for k, t in tables.items()}
    wbl26, wbl1, panel = frames["atlas.wbl.wbl2026"], frames["atlas.wbl.wbl1_2026"], frames["atlas.wbl.panel_1971_2024"]
    con, chron, fb = (
        frames["atlas.constitute.constitutions"],
        frames["atlas.ccp.chronology_v6"],
        frames["atlas.factbook.legal_2026_01"],
    )
    chars = frames["atlas.ccp.characteristics_v5"]
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
        "const_votemin": (chars, "votemin", True),
        "const_votemin_text": (chars, "votemin", False),
        "const_cappun": (chars, "cappun", False),
        "const_samesexm": (chars, "samesexm", False),
        "ccp_systyear": (chars, "systyear", True),
        "suffrage_text": (fb, "suffrage", False),
        "legal_families": (fb, "legal_system.families_mentioned", False),
    }
    if ssa is not None:
        sf = read_atlas_table(db.store, ssa["artifact_sha256"])
        for name, ind in (
            ("ssa_men", "old_age.pension_age.men"),
            ("ssa_women", "old_age.pension_age.women"),
            ("ssa_men_months", "old_age.pension_age_months.men"),
            ("ssa_women_months", "old_age.pension_age_months.women"),
            ("ssa_qc", "old_age.qualifying_conditions.first"),
        ):
            cols[name] = (sf, ind, not name.endswith("qc"))
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
