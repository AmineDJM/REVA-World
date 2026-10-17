"""BEAT-MACRO evaluation: development stage, frozen hypotheses, holdout opened once (ledger), descriptives.

Pre-registration: ``registry/forecasts/beat_macro/protocol.yaml`` (design, committed before any window was opened);
``hypotheses.yaml`` (written by the development stage from the protocol's mechanical selection rule, committed
before the holdout is opened); ``holdout_ledger.yaml`` (append-only, one opening per hypotheses digest).

Scores: AE of points against the official point; CRPS of densities against the fair density benchmark
(``combine.fan_key``); DM-HLN on the per-origin mean loss differential (origin = unit of inference); Holm over the
whole family. Inference unit and lags: monthly origins -> HAC horizon 12 (rectangular kernel, lag 11, covering
the origins that share a target year); quarter-start origins -> 4.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from reva.evaluation.beat_macro import cases as C
from reva.evaluation.beat_macro import combine as K
from reva.evaluation.beat_macro.inputs import LaggedMonthly
from reva.evaluation.pred_models import gate as G
from reva.evaluation.pred_models.realtime import Case

SCHEMA = "reva-beat-macro/1"
REGISTRY_DIR = Path(__file__).resolve().parents[4] / "registry" / "forecasts" / "beat_macro"
SPLIT = date(2019, 1, 1)
LAST_SCORED_ORIGIN = date(2025, 12, 1)
V6_MONTHS = (6, 12)  # PRED-MODELS evaluated WEO origins on 1 June / 1 December only
ALPHA = 0.05
COVERAGE_BAND = (0.70, 0.90)
SOURCE_FILES = ("inputs.py", "cases.py", "combine.py", "service.py")
MODEL_VERSION = "1.0.0"


@dataclass(frozen=True)
class Inputs:
    cells: pl.DataFrame
    cpi: LaggedMonthly
    hicp: LaggedMonthly
    oil: LaggedMonthly
    pins: Mapping[str, Any]


TASKS = ("weo_cpi", "g7rt", "ecb_hicp")
RULES = {
    "weo_cpi": K.Rules(C.WEO, 3, 15),
    "g7rt": K.Rules(C.WEO, 3, 15),
    "ecb_hicp": K.Rules(C.ECB, 8, 8),
}


def official(task: str) -> str:
    return RULES[task].official


def dm_horizon(task: str, variable: str) -> int:
    if task == "ecb_hicp" or (task == "g7rt" and variable == "real_gdp_growth"):
        return 4
    return 12


def origins(task: str, stage: str, *, last: date = LAST_SCORED_ORIGIN) -> list[datetime]:
    """Development: every origin before the split. Holdout: development origins (their errors feed the
    second pass, never scored) + the origins after the split that V6 never evaluated."""
    if task == "ecb_hicp":
        allo = [t for t in C.monthly_origins(date(2000, 1, 1), last) if t.month in (1, 4, 7, 10)]
    else:
        allo = C.monthly_origins(date(2008, 5, 1), last)
    dev = [t for t in allo if t.date() < SPLIT]
    if stage == "development":
        return dev
    return dev + [t for t in allo if t.date() >= SPLIT and t.month not in V6_MONTHS]


def build(task: str, inp: Inputs, ors: Sequence[datetime]) -> list[Case]:
    if task == "weo_cpi":
        cs = C.weo_cpi_cases(inp.cells, inp.cpi, inp.oil, ors)
    elif task == "g7rt":
        cs = C.g7_cases(inp.cells, inp.oil, ors)
    elif task == "ecb_hicp":
        cs = C.ecb_cases(inp.cells, inp.hicp, inp.oil, ors)
    else:
        raise ValueError(task)
    K.add_second_pass(cs, RULES[task])
    return cs


def candidates(task: str, variable: str) -> list[str]:
    ms = ["bridge", "stack"] + (["bridge_oil", "stack_oil"] if "inflation" in variable else [])
    return [K.key(m) for m in ms]


def uses_official(model: str) -> bool:
    return "stack" in model


def _cells(scored: Sequence[G.Scored]) -> dict[str, list[G.Scored]]:
    out: dict[str, list[G.Scored]] = defaultdict(list)
    for s in scored:
        out[s.case.variable].append(s)
    return out


def comparisons(task: str, scored: Sequence[G.Scored]) -> list[dict[str, Any]]:
    """Every candidate x {AE vs official point, CRPS vs fair fan} per variable (the full grid)."""
    off = official(task)
    tests = []
    for var, rows in sorted(_cells(scored).items()):
        for cand in candidates(task, var):
            for bench, loss in ((off, "ae"), (K.fan_key(off), "crps")):
                t = G.paired_test(rows, cand, bench, loss, dm_horizon(task, var))
                if t["n_pairs"]:
                    tests.append({"task": task, "variable": var, "horizon": "cy", **t,
                                  "uses_official_forecast": uses_official(cand)})  # fmt: skip
    return tests


def summaries(task: str, scored: Sequence[G.Scored]) -> list[dict[str, Any]]:
    off = official(task)
    out = []
    for var, rows in sorted(_cells(scored).items()):
        for m in (off, K.fan_key(off), K.own_fan_key(off), C.PERSIST, *candidates(task, var)):
            sm = G.summary(rows, m)
            if sm["n"]:
                out.append({"task": task, "variable": var, "model": m, **sm})
    return out


def by_month(task: str, scored: Sequence[G.Scored], model: str, bench: str, loss: str) -> list[dict[str, Any]]:
    """Descriptive: mean loss ratio per calendar month of the origin (common cases)."""
    acc: dict[tuple[str, int], list[tuple[float, float]]] = defaultdict(list)
    for s in scored:
        a, b = s.scores.get(model), s.scores.get(bench)
        if a and b and a.get(loss) is not None and b.get(loss) is not None:
            acc[(s.case.variable, s.case.origin.month)].append((float(a[loss]), float(b[loss])))
    out = []
    for (v, m), xs in sorted(acc.items()):
        lb = sum(x[1] for x in xs)
        out.append({"variable": v, "month": m, "n": len(xs),
                    "relative": (sum(x[0] for x in xs) / lb) if lb > 0 else None})  # fmt: skip
    return out


def per_series_median_ratio(scored: Sequence[G.Scored], model: str, bench: str, loss: str) -> dict[str, Any]:
    """Descriptive (QA-PRED X6): median over series of the per-series mean-loss ratio."""
    acc: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for s in scored:
        a, b = s.scores.get(model), s.scores.get(bench)
        if a and b and a.get(loss) is not None and b.get(loss) is not None:
            acc[(s.case.variable, s.case.geo)].append((float(a[loss]), float(b[loss])))
    res: dict[str, Any] = {}
    byv: dict[str, list[float]] = defaultdict(list)
    for (v, _g), xs in acc.items():
        lb = sum(x[1] for x in xs)
        if lb > 0 and len(xs) >= 5:
            byv[v].append(sum(x[0] for x in xs) / lb)
    for v, rs in byv.items():
        res[v] = {"n_series": len(rs), "median_ratio": statistics.median(rs),
                  "share_series_better": sum(r < 1 for r in rs) / len(rs)}  # fmt: skip
    return res


def cpi_revision_check(weo_scored: Sequence[G.Scored], g7_scored: Sequence[G.Scored]) -> dict[str, Any]:
    """Robustness of the non-revision assumption: on the G7 CPI cells, the bridge built from today's IMF CPI
    index (lag rule) vs the bridge built from the OECD MEI real-time editions in force at the origin."""
    fin = {(s.case.geo, s.case.origin): s for s in weo_scored if s.case.geo in C.G7}
    pairs = []
    for s in g7_scored:
        if s.case.variable != "cpi_inflation":
            continue
        f = fin.get((s.case.geo, s.case.origin))
        a, b = (f.scores.get(K.key("bridge")) if f else None), s.scores.get(K.key("bridge"))
        if a and b:
            pairs.append((float(a["ae"]), float(b["ae"])))
    if not pairs:
        return {"n": 0}
    return {
        "n": len(pairs),
        "mae_final_vintage_imf_cpi": sum(p[0] for p in pairs) / len(pairs),
        "mae_real_time_mei": sum(p[1] for p in pairs) / len(pairs),
        "statement": "same target, same origins; a large gap would mean the final-vintage CPI flatters the panel",
    }


# --------------------------------------------------------------------------- stages
def code_sha256() -> str:
    h = hashlib.sha256()
    base = Path(__file__).resolve().parent
    for f in SOURCE_FILES:
        h.update(f.encode() + b"\0" + (base / f).read_bytes() + b"\0")
    return h.hexdigest()


def canonical_sha256(doc: Any) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def stage_scored(task: str, inp: Inputs, stage: str) -> tuple[list[Case], list[G.Scored]]:
    cs = build(task, inp, origins(task, stage))
    if stage == "development":
        keep = [c for c in cs if c.origin.date() < SPLIT]
    else:
        keep = [c for c in cs if c.origin.date() >= SPLIT]
    return cs, G.score_cases(keep)


def development(inp: Inputs) -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    sums: list[dict[str, Any]] = []
    scored_by: dict[str, list[G.Scored]] = {}
    n_cases: dict[str, Any] = {}
    for task in TASKS:
        _cs, sc = stage_scored(task, inp, "development")
        scored_by[task] = sc
        tests += comparisons(task, sc)
        sums += summaries(task, sc)
        n_cases[task] = {"scored": len(sc), "origins": len({s.case.origin for s in sc})}
    G.holm(tests, ALPHA)
    return {
        "schema": SCHEMA,
        "stage": "development",
        "split": SPLIT.isoformat(),
        "code_sha256": code_sha256(),
        "pins": dict(inp.pins),
        "n_cases": n_cases,
        "tests": tests,
        "summaries": sums,
        "cpi_revision_check": cpi_revision_check(scored_by["weo_cpi"], scored_by["g7rt"]),
    }


def select_hypotheses(dev: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The protocol's mechanical rule: per (task, variable, loss), among the development tests that are
    Holm-rejected with relative < 1, the best combination (uses the official forecast) and the best pure REVA
    model (does not) - at most two hypotheses per cell."""
    best: dict[tuple[str, str, str, bool], dict[str, Any]] = {}
    for t in dev["tests"]:
        if not (t.get("holm_rejected") and t.get("relative") is not None and t["relative"] < 1):
            continue
        k = (t["task"], t["variable"], t["loss"], bool(t["uses_official_forecast"]))
        if k not in best or t["relative"] < best[k]["relative"]:
            best[k] = t
    return [
        {"task": t["task"], "variable": t["variable"], "horizon": "cy", "candidate": t["candidate"],
         "benchmark": t["benchmark"], "loss": t["loss"], "uses_official_forecast": t["uses_official_forecast"],
         "dev_relative": round(t["relative"], 6), "dev_p_better": float(f"{t['p_better']:.6g}")}  # fmt: skip
        for _k, t in sorted(best.items())
    ]


def hyp_key(h: Mapping[str, Any]) -> str:
    return "|".join(str(h[k]) for k in ("task", "variable", "horizon", "candidate", "benchmark", "loss"))


def check_unopened(hyp_doc: Mapping[str, Any], ledger: Mapping[str, Any]) -> str:
    sha = canonical_sha256(hyp_doc)
    for o in ledger.get("openings", []) or []:
        if o.get("hypotheses_sha256") == sha:
            raise RuntimeError(f"holdout already opened for hypotheses {sha[:12]} at {o.get('opened_at')}")
    if hyp_doc.get("code_sha256") != code_sha256():
        raise RuntimeError("code changed since the hypotheses were frozen: a new model version needs a new holdout")
    return sha


def holdout(inp: Inputs, hyp_doc: Mapping[str, Any]) -> dict[str, Any]:
    hyps = list(hyp_doc["hypotheses"])
    results: list[dict[str, Any]] = []
    scored_by: dict[str, list[G.Scored]] = {}
    for task in TASKS:
        _cs, scored_by[task] = stage_scored(task, inp, "holdout")
    for h in hyps:
        rows = [s for s in scored_by[h["task"]] if s.case.variable == h["variable"]]
        t = G.paired_test(rows, h["candidate"], h["benchmark"], h["loss"], dm_horizon(h["task"], h["variable"]))
        cov = G.summary(rows, h["candidate"])["coverage80"] if h["loss"] == "crps" else None
        results.append({**h, **{k: t.get(k) for k in ("n_pairs", "n_origins", "loss_candidate", "loss_benchmark",
                                                      "relative", "p_better", "dm_stat", "tested")},
                        "cov80_candidate": cov})  # fmt: skip
    G.holm(results, ALPHA)
    for r in results:
        ok = bool(r["holm_rejected"]) and r["relative"] is not None and r["relative"] < 1
        if r["loss"] == "crps":
            ok = ok and r["cov80_candidate"] is not None and COVERAGE_BAND[0] <= r["cov80_candidate"] <= COVERAGE_BAND[1]
        r["confirmed"] = ok
    desc: dict[str, Any] = {"summaries": [], "all_comparisons": [], "by_month": [], "per_series": []}
    for task in TASKS:
        sc = scored_by[task]
        off = official(task)
        desc["summaries"] += summaries(task, sc)
        allc = comparisons(task, sc)
        desc["all_comparisons"] += allc
        for var in sorted({s.case.variable for s in sc}):
            for cand in candidates(task, var):
                for bench, loss in ((off, "ae"), (K.fan_key(off), "crps")):
                    desc["by_month"].append({"task": task, "candidate": cand, "benchmark": bench, "loss": loss,
                                             "rows": [r for r in by_month(task, sc, cand, bench, loss)
                                                      if r["variable"] == var]})  # fmt: skip
                desc["per_series"].append({"task": task, "variable": var, "candidate": cand,
                                           "ae_vs_official": per_series_median_ratio(sc, cand, off, "ae").get(var)})  # fmt: skip
    desc["cpi_revision_check"] = cpi_revision_check(scored_by["weo_cpi"], scored_by["g7rt"])
    return {
        "schema": SCHEMA,
        "stage": "holdout",
        "split": SPLIT.isoformat(),
        "hypotheses_sha256": canonical_sha256(hyp_doc),
        "code_sha256": code_sha256(),
        "pins": dict(inp.pins),
        "n_cases": {t: {"scored": len(s), "origins": len({x.case.origin for x in s})} for t, s in scored_by.items()},
        "family_size": len(results),
        "results": results,
        "descriptive": desc,
        "statement": "Holdout = origins on/after 2019-01-01 on the 1st of every month except June and December "
        "(never evaluated by V6) for the WEO tasks, quarter starts for the ECB SPF task (never evaluated before). "
        "Targets 2019-2025 overlap V6's holdout targets (outcomes were seen in V6 reports); the information dates "
        "and the REVA nowcasts are new.",
    }


def rounded(obj: Any, digits: int = 6) -> Any:
    if isinstance(obj, float):
        return float(f"{obj:.{digits}g}")
    if isinstance(obj, dict):
        return {k: rounded(v, digits) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [rounded(v, digits) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def write_json(path: Path, doc: Mapping[str, Any]) -> str:
    data = json.dumps(rounded(dict(doc)), indent=1, sort_keys=True, default=str).encode() + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def append_ledger(path: Path, entry: Mapping[str, Any]) -> None:
    doc = load_yaml(path) or {
        "schema": "reva-beat-macro-holdout-ledger/1",
        "statement": "Append-only: one entry per opening of the BEAT-MACRO holdout. Never edit past entries.",
        "openings": [],
    }
    doc.setdefault("openings", []).append(dict(entry))
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def now_utc() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "Inputs",
    "REGISTRY_DIR",
    "SPLIT",
    "TASKS",
    "append_ledger",
    "build",
    "canonical_sha256",
    "check_unopened",
    "code_sha256",
    "development",
    "holdout",
    "hyp_key",
    "origins",
    "select_hypotheses",
    "write_json",
]
