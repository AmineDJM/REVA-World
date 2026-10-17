"""BEAT-NOWCAST evaluation: development stage, frozen hypotheses, holdout opened once (ledger), descriptives.

Pre-registration: ``registry/forecasts/beat_nowcast/protocol.yaml`` (design + code digest, committed before any
evaluation window was opened); ``hypotheses.yaml`` (written by the development stage from the protocol's
mechanical selection rule, committed before the holdout is opened); ``holdout_ledger.yaml`` (append-only).

Scores: AE of points against the official point; CRPS of densities against the fair density benchmark
(``<official>_errfan``); DM-HLN (one-sided, candidate better) on the per-target-period mean loss differential
(target period = unit of inference), horizon argument 2; Holm over the whole holdout family.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from reva.evaluation.beat_nowcast import cases as C
from reva.evaluation.beat_nowcast import data as D
from reva.evaluation.beat_nowcast import models as M
from reva.evaluation.pred_models import gate as G
from reva.evaluation.pred_models.realtime import Case

SCHEMA = "reva-beat-nowcast/1"
REPO = Path(__file__).resolve().parents[4]
REGISTRY_DIR = REPO / "registry" / "forecasts" / "beat_nowcast"
MODEL_VERSION = "1.0.0"
ALPHA = 0.05
COVERAGE_BAND = (0.70, 0.90)
DM_HORIZON = 2
SOURCE_FILES = (
    "src/reva/evaluation/beat_nowcast/__init__.py",
    "src/reva/evaluation/beat_nowcast/data.py",
    "src/reva/evaluation/beat_nowcast/models.py",
    "src/reva/evaluation/beat_nowcast/cases.py",
    "src/reva/evaluation/beat_nowcast/service.py",
    "src/reva/models/forecasting/dfm.py",
    "src/reva/evaluation/pred_models/gate.py",
    "src/reva/adapters/connectors/atlas_fed_nowcasts.py",
    "src/reva/cli/beat_nowcast.py",
)

# target windows (inclusive): quarter / month indices
GDP_BURN_IN = 2011 * 4 + 2  # 2011Q3 (first GDPNow quarter): errors feed the second pass, never scored
CPI_BURN_IN = 2013 * 12 + 6  # 2013-07
DEV = {"gdp": (2014 * 4, 2021 * 4 + 3), "cpi": (2014 * 12, 2021 * 12 + 11)}
HOLDOUT = {"gdp": (2022 * 4, 2025 * 4 + 3), "cpi": (2022 * 12, 2025 * 12 + 11)}


def kind(cell: str) -> str:
    return "gdp" if cell.startswith("gdp") else "cpi"


def candidates(cell: str) -> list[str]:
    if kind(cell) == "gdp":
        return [M.key(m) for m in ("bridge", "dfm", "stack_reva", "stack_all", "biascorr")]
    return [M.key(m) for m in ("cpi_bridge", "stack_reva", "biascorr")]


def uses_official(model: str) -> bool:
    return any(s in model for s in ("stack", "biascorr"))


def target_index(c: Case) -> int:
    d = c.origin.date()
    return D.qi_of(d) if kind(c.task) == "gdp" else D.mi_of(d)


# ------------------------------------------------------------------------------------------------ inputs
def inputs_from_cells(
    cells: pl.DataFrame, rtdsm_q: pl.DataFrame, spot: pl.DataFrame, pins: Mapping[str, Any]
) -> C.Inputs:
    """``cells``: vintage cells of the five fed_nowcasts tables; ``rtdsm_q``: vintage cells of
    ``vintage_us.rtdsm`` (CPI levels); ``spot``: the atlas table ``eia.spot_d`` (long schema)."""
    cpi_actual = D.clev_actuals(cells, "CPI")
    return C.Inputs(
        gdpnow=D.OfficialBook.from_cells(C.OFFICIAL["gdpnow"], cells, "atl_gdpnow", "GDPNOW"),
        nyfed=D.OfficialBook.from_cells(C.OFFICIAL["nyfed"], cells, "nyfed_nowcast", "NYFED_NOWCAST"),
        clev_cpi=D.OfficialBook.from_cells(C.OFFICIAL["clev"], cells, "clev_inflation_nowcast", "CLEV_MONTH_CPI"),
        clev_core=D.OfficialBook.from_cells(C.OFFICIAL["clev"], cells, "clev_inflation_nowcast", "CLEV_MONTH_CORE_CPI"),
        gdp_first=D.gdp_truths(cells, "first"),
        gdp_third=D.gdp_truths(cells, "third"),
        cpi_actual=cpi_actual,
        core_actual=D.clev_actuals(cells, "CORE_CPI"),
        rt={k: D.VintageSeries.from_cells(k, cells, "philfed_rtdsm_monthly", k) for k in M.GDP_INDICATORS},
        cpi_hist=D.CpiHistory(D.VintageSeries.from_cells("RTDSM_CPI", rtdsm_q, "philfed_rtdsm", "CPI"), cpi_actual),
        gas=D.DailySeries.from_frame(
            "eia.spot_d:EER_EPMRU_PF4_Y35NY_DPG",
            spot,
            C.GAS_SERIES,
            C.GAS_LAG_DAYS,
            "eia_weekly_spot_release_conservative_lag",
        ),
        pins=dict(pins),
    )


def build(inp: C.Inputs, stage: str) -> tuple[dict[str, list[Case]], C.BuildLog]:
    """Cases from the burn-in to the end of ``stage`` (development: dev window only; holdout: through the holdout
    window). Nothing after the development window is built in the development stage."""
    if stage not in ("development", "holdout"):
        raise ValueError(stage)
    last = DEV if stage == "development" else HOLDOUT
    log = C.BuildLog()
    out = {}
    for cell in C.CELLS:
        if kind(cell) == "gdp":
            out[cell] = C.gdp_cases(inp, cell, GDP_BURN_IN, last["gdp"][1], log)
        else:
            out[cell] = C.cpi_cases(inp, cell, CPI_BURN_IN, last["cpi"][1], log)
    return out, log


def scored_in(
    cases: Sequence[Case], window: Mapping[str, tuple[int, int]], *, drop_pandemic: bool = False
) -> list[G.Scored]:
    rows = []
    for c in cases:
        lo, hi = window[kind(c.task)]
        ti = target_index(c)
        if not lo <= ti <= hi:
            continue
        if drop_pandemic and kind(c.task) == "gdp" and ti in M.PANDEMIC_QIS:
            continue
        rows.append(c)
    return G.score_cases(rows)


def comparisons(cell: str, rows: Sequence[G.Scored]) -> list[dict[str, Any]]:
    off = C.RULES[cell].official
    tests = []
    for cand in candidates(cell):
        for bench, loss in ((off, "ae"), (M.fan_key(off), "crps")):
            t = G.paired_test(rows, cand, bench, loss, DM_HORIZON)
            if t["n_pairs"]:
                tests.append({"cell": cell, **t, "uses_official_forecast": uses_official(cand)})
    return tests


def summaries(cell: str, rows: Sequence[G.Scored]) -> list[dict[str, Any]]:
    off = C.RULES[cell].official
    out = []
    for m in (off, M.fan_key(off), *candidates(cell)):
        sm = G.summary(rows, m)
        if sm["n"]:
            out.append(
                {"cell": cell, "model": m, **sm, "n_targets": len({r.case.origin for r in rows if m in r.scores})}
            )
    return out


def third_release(cell: str, rows: Sequence[G.Scored]) -> dict[str, Any]:
    """Descriptive (declared secondary truth): MAE against BEA's third estimate, common cases."""
    if kind(cell) != "gdp":
        return {}
    off = C.RULES[cell].official
    acc: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        y3 = r.case.info.get("third")
        if y3 is None or off not in r.case.preds:
            continue
        for m in (off, *candidates(cell)):
            p = r.case.preds.get(m)
            if hasattr(p, "point"):
                acc[m].append(abs(p.point - y3))  # type: ignore[union-attr]
    base = statistics.fmean(acc[off]) if acc.get(off) else None
    return {m: {"n": len(v), "mae_vs_third": statistics.fmean(v),
                "relative_to_official": (statistics.fmean(v) / base) if base else None}
            for m, v in acc.items() if v}  # fmt: skip


def by_bucket(cell: str, rows: Sequence[G.Scored]) -> list[dict[str, Any]]:
    off = C.RULES[cell].official
    acc: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        b = r.scores.get(off)
        for m in candidates(cell):
            a = r.scores.get(m)
            if a and b:
                acc[(m, r.case.horizon)].append((a["ae"], b["ae"]))
    return [{"model": m, "bucket": k, "n": len(v), "relative_ae": math.fsum(x[0] for x in v) / math.fsum(x[1] for x in v)}
            for (m, k), v in sorted(acc.items()) if math.fsum(x[1] for x in v) > 0]  # fmt: skip


def cross_checks(inp: C.Inputs) -> dict[str, Any]:
    """Truth cross-check: Philadelphia Fed first release vs the BEA advance estimate recorded by the Atlanta Fed."""
    diffs = [(k, t.value - t.cross_check) for k, t in inp.gdp_first.items() if t.cross_check is not None]
    big = [(D.qlabel(D.qi_of(k)), round(d, 4)) for k, d in diffs if abs(d) > 0.05]
    return {"gdp_first_vs_atlanta_recorded_advance": {"n": len(diffs), "max_abs_diff": max((abs(d) for _, d in diffs), default=None),
                                                       "quarters_diff_gt_0.05": big}}  # fmt: skip


# ------------------------------------------------------------------------------------------------ stages
def code_sha256() -> str:
    h = hashlib.sha256()
    for f in SOURCE_FILES:
        h.update(f.encode() + b"\0" + (REPO / f).read_bytes() + b"\0")
    return h.hexdigest()


def canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _strip(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items()} for r in rows]


def development(inp: C.Inputs) -> dict[str, Any]:
    allc, log = build(inp, "development")
    doc: dict[str, Any] = {"schema": SCHEMA, "stage": "development", "code_sha256": code_sha256(),
                           "model_version": MODEL_VERSION, "pins": inp.pins, "windows": {"development": DEV},
                           "build": {"skipped": dict(log.skipped), "guard_checks": log.guard_checks,
                                     "cache_hits": log.cache_hits, "cache_misses": log.cache_misses},
                           "cells": {}}  # fmt: skip
    sel_tests: list[dict[str, Any]] = []
    for cell, cs in allc.items():
        rows = scored_in(cs, DEV)
        rows_sel = scored_in(cs, DEV, drop_pandemic=True)
        tests_all = comparisons(cell, rows)
        tests_sel = comparisons(cell, rows_sel)
        sel_tests += tests_sel
        doc["cells"][cell] = {
            "n_cases": len(rows),
            "n_targets": len({r.case.origin for r in rows}),
            "summaries": _strip(summaries(cell, rows)),
            "tests_all_targets": _strip(tests_all),
            "tests_selection_sample": _strip(tests_sel),
            "by_bucket": _strip(by_bucket(cell, rows_sel)),
            "third_release": third_release(cell, rows),
        }
    G.holm(sel_tests)
    doc["development_family"] = {"m": len([t for t in sel_tests if t.get("tested")]), "tests": _strip(sel_tests)}
    doc["cross_checks"] = cross_checks(inp)
    return doc


def select_hypotheses(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Protocol rule: per (cell, loss), among tested development comparisons on the selection sample with relative
    loss < 1, the best combination (uses the official nowcast) and the best pure-REVA candidate."""
    best: dict[tuple[str, str, bool], dict[str, Any]] = {}
    for t in doc["development_family"]["tests"]:
        if not t.get("tested") or t.get("relative") is None or t["relative"] >= 1:
            continue
        k = (t["cell"], t["loss"], bool(t["uses_official_forecast"]))
        if k not in best or t["relative"] < best[k]["relative"]:
            best[k] = t
    return [{"cell": c, "loss": loss, "candidate": t["candidate"], "benchmark": t["benchmark"],
             "uses_official_forecast": uo, "dev_relative": t["relative"], "dev_p_better": t["p_better"],
             "dev_n_targets": t["n_origins"]}
            for (c, loss, uo), t in sorted(best.items())]  # fmt: skip


def hyp_key(h: Mapping[str, Any]) -> str:
    return f"{h['cell']}|{h['candidate']}|{h['benchmark']}|{h['loss']}"


def holdout(inp: C.Inputs, hyp_doc: Mapping[str, Any]) -> dict[str, Any]:
    if hyp_doc.get("code_sha256") != code_sha256():
        raise RuntimeError("code changed since the hypotheses were frozen")
    allc, log = build(inp, "holdout")
    results = []
    descriptive: dict[str, Any] = {}
    for cell, cs in allc.items():
        rows = scored_in(cs, HOLDOUT)
        descriptive[cell] = {
            "n_cases": len(rows),
            "n_targets": len({r.case.origin for r in rows}),
            "summaries": _strip(summaries(cell, rows)),
            "all_comparisons_descriptive": _strip(comparisons(cell, rows)),
            "by_bucket": _strip(by_bucket(cell, rows)),
            "third_release": third_release(cell, rows),
        }
        for h in hyp_doc["hypotheses"]:
            if h["cell"] != cell:
                continue
            t = G.paired_test(rows, h["candidate"], h["benchmark"], h["loss"], DM_HORIZON)
            cov = G.summary(rows, h["candidate"])["coverage80"] if h["loss"] == "crps" else None
            results.append({**h, **{k: v for k, v in t.items() if k not in ("candidate", "benchmark", "loss")},
                            "cov80_candidate": cov})  # fmt: skip
    G.holm(results)
    for r in results:
        ok = bool(r.get("holm_rejected")) and r.get("relative") is not None and r["relative"] < 1
        if r["loss"] == "crps":
            ok = (
                ok and r["cov80_candidate"] is not None and COVERAGE_BAND[0] <= r["cov80_candidate"] <= COVERAGE_BAND[1]
            )
        r["confirmed"] = ok
    return {"schema": SCHEMA, "stage": "holdout", "code_sha256": code_sha256(), "model_version": MODEL_VERSION,
            "hypotheses_sha256": canonical_sha256(hyp_doc), "pins": inp.pins, "windows": {"holdout": HOLDOUT},
            "family_size": len(results), "results": _strip(results), "descriptive": descriptive,
            "build": {"skipped": dict(log.skipped), "guard_checks": log.guard_checks},
            "cross_checks": cross_checks(inp)}  # fmt: skip


def write_json(path: Path, doc: Mapping[str, Any]) -> str:
    text = json.dumps(doc, indent=1, sort_keys=True, default=str) + "\n"
    path.write_text(text)
    return hashlib.sha256(text.encode()).hexdigest()


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def check_unopened(hyp_doc: Mapping[str, Any], ledger: Mapping[str, Any]) -> str:
    sha = canonical_sha256(hyp_doc)
    for o in ledger.get("openings", []) or []:
        if o.get("hypotheses_sha256") == sha:
            raise RuntimeError(f"holdout already opened for hypotheses {sha} at {o.get('opened_at')}")
    return sha


def append_ledger(path: Path, entry: Mapping[str, Any]) -> None:
    doc = load_yaml(path) or {"schema": "reva-beat-nowcast-holdout-ledger/1",
                              "statement": "Append-only: one entry per opening of the BEAT-NOWCAST holdout."}  # fmt: skip
    doc.setdefault("openings", []).append(dict(entry))
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def stamp_now() -> str:
    return datetime.now(UTC).isoformat()


def count_by(xs: Sequence[Any]) -> dict[str, int]:
    return dict(Counter(str(x) for x in xs))


def target_date(label: str) -> date:
    return (
        D.qi_start(int(label[:4]) * 4 + int(label[-1]) - 1) if "Q" in label else date(int(label[:4]), int(label[5:]), 1)
    )


__all__ = [
    "DEV",
    "HOLDOUT",
    "REGISTRY_DIR",
    "append_ledger",
    "build",
    "canonical_sha256",
    "check_unopened",
    "code_sha256",
    "development",
    "holdout",
    "hyp_key",
    "inputs_from_cells",
    "load_yaml",
    "select_hypotheses",
    "write_json",
]
