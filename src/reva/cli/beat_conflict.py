"""V7 BEAT-CONFLICT runner: ``python -m reva.cli.beat_conflict evaluate --out <full.json> --summary <summary.json>``.

Reads the pinned atlas tables (latest version at the database clock; bytes verified against their sha256),
refuses to run unless ``registry/forecasts/beat_conflict/protocol.yaml`` is committed in git and its
``code_sha256`` matches :mod:`reva.evaluation.conflict.beat`, then evaluates exactly the pre-registered windows.
Read-only on the database (``evaluate``). Kept out of :mod:`reva.evaluation` (invariant 11: object-store factory).
Country-month RISK ESTIMATES only.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
import yaml

ROOT = Path(__file__).resolve().parents[3]
PROTOCOL = ROOT / "registry/forecasts/beat_conflict/protocol.yaml"
CODE = ROOT / "src/reva/evaluation/conflict/beat.py"
RUNNER = Path(__file__).resolve()
CM_FINAL = "atlas.ucdp_ged.country_month"
CM_CAND = "atlas.ucdp_ged.cand_country_month"
EV_FINAL = "atlas.ucdp_ged.events"
EV_CAND = "atlas.ucdp_ged.cand_events"
CPI = ("atlas.imf.cpi_m", "CPI|coicop_1999=_T|type_of_transformation=IX")
FX = ("atlas.imf.er_m", "XDC_USD|type_of_transformation=PA_RT")
CM_IND = {
    "sb": "deaths_best|tov=sb",
    "ns": "deaths_best|tov=ns",
    "os": "deaths_best|tov=os",
    "ev": "events|tov=all",
}


def code_sha() -> str:
    return hashlib.sha256(CODE.read_bytes()).hexdigest()


def runner_sha() -> str:
    return hashlib.sha256(RUNNER.read_bytes()).hexdigest()


def check_protocol() -> dict[str, Any]:
    proto = yaml.safe_load(PROTOCOL.read_text())
    rel = str(PROTOCOL.relative_to(ROOT))
    head = subprocess.run(["git", "-C", str(ROOT), "show", f"HEAD:{rel}"], capture_output=True, text=True)  # noqa: S607
    if head.returncode != 0 or head.stdout != PROTOCOL.read_text():
        raise SystemExit("protocol.yaml is not committed unchanged in HEAD: refusing to evaluate")
    if proto["code_sha256"] != code_sha() or proto["runner_sha256"] != runner_sha():
        raise SystemExit("beat.py or the runner differs from the registered sha256: refusing to evaluate")
    return dict(proto)


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(Decimal(str(v)))
    except Exception:
        return None
    return x if math.isfinite(x) else None


def load(engine: Any, store: Any) -> dict[str, Any]:
    from sqlalchemy import text

    from reva.evaluation.conflict import beat as B
    from reva.evaluation.conflict.service import load_inputs, read_filtered
    from reva.evaluation.registry.data import latest_version

    inp = load_inputs(engine, store, origin="live")
    with engine.connect() as conn:
        cutoff: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        pins = {
            k: latest_version(conn, k, origin="live", cutoff=cutoff)
            for k in (CM_FINAL, CM_CAND, EV_FINAL, EV_CAND, CPI[0], FX[0])
        }
    used = {k: {"artifact_sha256": p["artifact_sha256"], "dataset_id": p["dataset_id"]} for k, p in pins.items() if p}
    used["atlas.views.cm_sb"] = {"artifact_sha256": inp["views_pin"]["artifact_sha256"]} if inp["views_pin"] else {}
    panel = inp["panel"]
    universe = {y: {g for g, d in panel.deaths.items() if y in d} for y in range(panel.first_year, panel.last_year + 1)}
    iso2gw: dict[int, dict[str, str]] = defaultdict(dict)
    for g, ys in panel.iso3.items():
        for y, iso in ys.items():
            if iso:
                iso2gw[y][iso] = g

    def cells(key: str) -> tuple[dict[str, dict[tuple[str, int], float]], tuple[int, int]]:
        f = read_filtered(store, pins[key]["artifact_sha256"], list(CM_IND.values()))  # type: ignore[index]
        out: dict[str, dict[tuple[str, int], float]] = {k: {} for k in CM_IND}
        rev = {v: k for k, v in CM_IND.items()}
        lo, hi = 10**9, -1
        for r in f.select("indicator", "provider_geo", "period_start", "value").iter_rows(named=True):
            v = _num(r["value"])
            ps = r["period_start"]
            i = B.midx(ps.year, ps.month)
            if r["indicator"] == CM_IND["sb"]:
                lo, hi = min(lo, i), max(hi, i)
            if v is not None:
                out[rev[r["indicator"]]][(str(r["provider_geo"]), i)] = v
        return out, (lo, hi)

    def spread(key: str) -> dict[str, dict[tuple[str, int], set[str]]]:
        f = read_filtered(store, pins[key]["artifact_sha256"], None)  # type: ignore[index]
        f = f.select(
            pl.col("provider_geo"),
            pl.col("period_start"),
            pl.col("flags").str.json_path_match("$.priogrid_gid").alias("pg"),
            pl.col("flags").str.json_path_match("$.adm1").alias("adm1"),
            pl.col("flags").str.json_path_match("$.adm1_status").alias("st"),
        )
        out: dict[str, dict[tuple[str, int], set[str]]] = {"adm1": defaultdict(set), "pg": defaultdict(set)}
        for g, ps, pg, a1, st in f.iter_rows():
            i = B.midx(ps.year, ps.month)
            if pg:
                out["pg"][(str(g), i)].add(pg)
            if a1 and st == "placed":
                out["adm1"][(str(g), i)].add(a1)
        return out

    def econ(spec: tuple[str, str]) -> dict[tuple[str, int], float]:
        if not pins.get(spec[0]):
            return {}
        f = read_filtered(store, pins[spec[0]]["artifact_sha256"], [spec[1]])  # type: ignore[index]
        out = {}
        ly = max(iso2gw)
        for iso, ps, v in f.select("iso3", "period_start", "value").iter_rows():
            x = _num(v)
            if iso is None or x is None:
                continue
            g = iso2gw[min(ps.year, ly)].get(iso) if ps.year >= min(iso2gw) else None
            if g:
                out[(g, B.midx(ps.year, ps.month))] = x
        return out

    final, fcov = cells(CM_FINAL)
    cand, ccov = cells(CM_CAND) if pins.get(CM_CAND) else ({k: {} for k in CM_IND}, None)
    sp_final = spread(EV_FINAL)
    sp_cand = spread(EV_CAND) if pins.get(EV_CAND) else {"adm1": {}, "pg": {}}
    return {
        "cutoff": cutoff.isoformat(),
        "tables": used,
        "universe": universe,
        "neighbours": panel.neighbours,
        "final": final,
        "final_cov": fcov,
        "cand": cand,
        "cand_cov": ccov,
        "sp_final": sp_final,
        "sp_cand": sp_cand,
        "econ": {"cpi": econ(CPI), "fx": econ(FX)},
        "views_runs": inp["views_runs"],
        "iso3": {g: dict(v) for g, v in panel.iso3.items()},
    }


def _cut(dct: dict[tuple[str, int], Any], hi: int) -> dict[tuple[str, int], Any]:
    return {k: v for k, v in dct.items() if k[1] <= hi}


def evaluate(L: dict[str, Any], proto: dict[str, Any], log: Any) -> dict[str, Any]:
    from reva.evaluation.conflict import beat as B

    out: dict[str, Any] = {"cutoff": L["cutoff"], "tables": L["tables"], "protocol_code_sha256": proto["code_sha256"]}
    i0 = B.midx(1989, 1)
    # ---------------- W1: final-vintage pseudo-real-time, GED 26.1, data cut at 2022-12
    w1 = proto["windows"]["W1"]
    hi = B.midx(*map(int, str(w1["data_cut"]).split("-")))
    d1 = B.build_cmdata(
        cells={k: _cut(v, hi) for k, v in L["final"].items()},
        spread_sets={k: _cut(v, hi) for k, v in L["sp_final"].items()},
        econ={k: _cut(v, hi) for k, v in L["econ"].items()},
        universe=L["universe"],
        covered=(L["final_cov"][0], hi),
        neighbours=L["neighbours"],
        i0=i0,
    )
    o_lo = B.midx(*map(int, str(w1["origins"]["first"]).split("-")))
    o_hi = B.midx(*map(int, str(w1["origins"]["last"]).split("-")))
    steps = list(range(1, int(w1["max_step"]) + 1))
    t0 = time.time()
    rows1, _ = B.run_origins(d1, list(range(o_lo, o_hi + 1)), steps, log=log)
    log(f"W1 scored {len(rows1)} in {time.time() - t0:.0f}s")
    groups: list[tuple[int, int]] = [(int(g.split("-")[0]), int(g.split("-")[1])) for g in w1["step_groups"]]
    tests: dict[str, dict[str, Any]] = {}
    for lo, hi_ in groups:
        for met in w1["metrics"]:
            tid = f"W1|{B.CAND}|vs|{B.PERS}|{met}|{lo}-{hi_}"
            tests[tid] = B.paired_test(rows1, B.CAND, B.PERS, met, (lo, hi_), lambda o, _g: o // 12)
    info1: dict[str, Any] = {}
    for lo, hi_ in groups:
        for a, b in ((B.CAND_GED, B.PERS), (B.CAND, B.BASE), (B.CAND, B.CAND_GED)):
            for met in w1["metrics"]:
                info1[f"W1info|{a}|vs|{b}|{met}|{lo}-{hi_}"] = B.paired_test(
                    rows1, a, b, met, (lo, hi_), lambda o, _g: o // 12
                )
    cal = {
        mk: B.calibration(
            (_p(rows1, k, mk), 1.0 if float(m["_y"]["y"] or 0.0) >= B.THRESHOLD else 0.0)
            for k, m in rows1.items()
            if mk in m
        )
        for mk in (B.CAND, B.PERS)
    }
    out["W1"] = {
        "label": "FINAL-VINTAGE pseudo-real-time backtest (GED 26.1 as published in 2026, data cut "
        f"{w1['data_cut']}); not real-time skill",
        "n_scored": len(rows1),
        "summary": B.summarise(rows1, groups),
        "info_tests": info1,
        "calibration": cal,
    }
    del rows1
    # ---------------- V (informational, V6-seen) + W3 preparation: real-time-ish mixed vintage
    ccov = L["cand_cov"]
    hi2 = ccov[1] if ccov else L["final_cov"][1]
    cells2 = {k: {**L["final"][k], **L["cand"][k]} for k in L["final"]}
    sp2 = {k: {**L["sp_final"][k], **L["sp_cand"][k]} for k in L["sp_final"]}
    d2 = B.build_cmdata(
        cells=cells2,
        spread_sets=sp2,
        econ=L["econ"],
        universe=L["universe"],
        covered=(L["final_cov"][0], hi2),
        neighbours=L["neighbours"],
        i0=i0,
    )
    runs = L["views_runs"]

    def views_extra(g: str, o: int, s: int) -> Any:
        vr = by_origin.get(o)
        if vr is None:
            return None
        key = (g, (o + s) // 12, (o + s) % 12 + 1)
        if key not in vr.monthly or key not in vr.prob:
            return None
        return B.Fc(math.log1p(max(vr.monthly[key], 0.0)), vr.prob[key], None)

    by_origin = {B.midx(*vr.last_input): vr for vr in runs.values()}
    v_orig = sorted(o for o in by_origin if o < hi2)
    fc2 = B.FeatureCache(d2)
    rowsv, recsv = B.run_origins(
        d2, v_orig, list(range(1, 13)), extra=views_extra, fc_cache=fc2, log=log, keep_records=True
    )
    vt: dict[str, Any] = {}
    for lo, hi_ in ((1, 3), (3, 8), (4, 6), (7, 12)):
        for a, b in ((B.CAND, B.VIEWS), (B.PERS, B.VIEWS), (B.CAND, B.PERS)):
            for met in ("brier", "logloss", "msle"):
                vt[f"V|{a}|vs|{b}|{met}|{lo}-{hi_}"] = B.paired_test(rowsv, a, b, met, (lo, hi_), lambda o, _g: o)
    w3 = proto["windows"]["W3"]
    s_lo, s_hi = map(int, str(w3["steps"]).split("-"))

    def outc(g: str, i: int) -> float | None:
        gi = d2.geos.index(g)
        v = d2.y(gi, i)
        return None if math.isnan(v) else v

    past = [r for r in recsv if s_lo <= r["step"] <= s_hi]
    w_prob = B.stack_weight(past, outc, "logloss")
    w_pt = B.stack_weight(past, outc, "msle")
    out["V"] = {
        "label": "INFORMATIONAL, V6-SEEN window (VIEWS runs loaded in V6, outcomes GED 26.1 final <= 2025-12 and "
        "UCDP Candidate 2026 preliminary); REVA inputs: GED final to the run's last input month (information "
        "VIEWS did not have) + Candidate 2026; not in the Holm family",
        "runs": sorted(vr.run for vr in runs.values() if B.midx(*vr.last_input) in v_orig),
        "summary": B.summarise(rowsv, ((1, 3), (3, 8), (4, 6), (7, 12))),
        "tests": vt,
        "stack_weights": {"prob": w_prob, "point": w_pt},
    }
    # ---------------- family (W1 evaluated now; W3 pending: p = 1 until the Candidate months are published)
    declared = list(proto["family"])
    out["family"] = B.family_holm(tests, declared)
    out["winners"] = sorted(k for k, t in out["family"].items() if t["win"])
    adm = proto["admission"]
    out["admitted"] = {
        fam: all(out["family"].get(t, {}).get("win") for t in req) for fam, req in adm["requires"].items()
    }
    out["w3_origin"] = w3["origin"]
    out["_d2"] = d2
    out["_fc2"] = fc2
    out["_by_origin"] = by_origin
    out["_weights"] = (w_prob.get("w"), w_pt.get("w"))
    return out


def _p(rows: Any, k: Any, mk: str) -> float:
    # recover the probability from the Brier score and the outcome (brier = (p - o)^2, p in [0, 1])
    m = rows[k]
    o = 1.0 if m["_y"]["y"] >= 25.0 else 0.0
    b = m[mk]["brier"]
    return o - math.sqrt(b) if o == 1.0 else math.sqrt(b)


def _clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, float):
        return None if not math.isfinite(o) else round(o, 6)
    return o


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from reva.adapters.objects.factory import get_store
    from reva.adapters.postgres.engine import make_engine
    from reva.settings import get_settings

    ap = argparse.ArgumentParser(description="V7 BEAT-CONFLICT (country-month risk estimates)")
    ap.add_argument("action", choices=("evaluate",))
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary", required=True)
    a = ap.parse_args(argv)
    proto = check_protocol()

    def log(m: str) -> None:
        print(m, file=sys.stderr, flush=True)

    eng = make_engine("worker", get_settings())
    L = load(eng, get_store())
    log("loaded")
    res = evaluate(L, proto, log)
    full = _clean(res)
    Path(a.out).write_text(json.dumps(full, indent=1, default=str))
    summ = {k: full[k] for k in ("cutoff", "tables", "protocol_code_sha256", "family", "winners", "admitted")}
    summ["W1_summary"] = full["W1"]["summary"]
    summ["W1_label"] = full["W1"]["label"]
    summ["V_summary"] = full["V"]["summary"]
    summ["V_label"] = full["V"]["label"]
    summ["V_stack_weights"] = {
        k: {kk: vv for kk, vv in v.items() if kk != "curve"} for k, v in full["V"]["stack_weights"].items()
    }
    Path(a.summary).write_text(json.dumps(summ, indent=1, default=str))
    print(json.dumps({"winners": full["winners"], "admitted": full["admitted"]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
