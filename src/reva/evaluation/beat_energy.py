"""V7 BEAT-ENERGY: REVA vs the EIA Short-Term Energy Outlook on windows not used in V6 (agent BEAT-ENERGY).

Pre-registered in ``registry/forecasts/beat_energy/protocol.yaml`` (committed before any live run). Three windows,
ONE Holm family over every declared test (unevaluated tests count with p = 1):

* **W1 timeliness, prices** (Brent ``BREPUUS``, WTI ``WTIPUUS``, Henry Hub ``NGHHUUS``; monthly means, target
  month = issue month + h, h in 0..2). Origins: every archived STEO issue's availability instant + 7/14/21/28 days,
  kept only while that issue is still the latest STEO (origin day strictly before the next issue's release date,
  read from the archived STEO archive page). REVA uses EIA daily spot prices with a conservative weekly-release
  availability (day ``d`` usable from ``d + DAILY_LAG_DAYS``). The STEO issue in force is the benchmark.
* **W2 bias correction / combination, quantities not used in V6** (8 STEO series, h in {1, 3, 6, 12}) at the
  quarterly archived issues' availability instants; corrections and weights from STEO's and REVA's past errors
  whose target month was already published in the information set at the origin.
* **W3 weekly data** (WPSR, U.S. crude production and commercial crude stocks): declared, not evaluable until
  ``atlas.eia_wpsr`` is ingested (free disk < 5 GB at registration) -> p = 1 in the family.

Point forecasts are compared with STEO's point on absolute error; densities with STEO's *fair* density (STEO point,
sigma from STEO's own past errors known at the origin, same window and family as REVA's) on CRPS. Never
point-vs-density. DM-HLN as in :mod:`reva.evaluation.energy_backtests`.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from reva.evaluation.energy_backtests import Origin, dm_hln, holm, label, overlap_steps
from reva.models.forecasting import energy_models as em

LABEL = "v7_beat_energy"
PROTOCOL_PATH = "registry/forecasts/beat_energy/protocol.yaml"
ALPHA = 0.05
# W1
SPOT_SERIES = {"BREPUUS": "RBRTE", "WTIPUUS": "RWTC", "NGHHUUS": "RNGWHHD"}
DAILY_LAG_DAYS = 9
OFFSETS = (7, 14, 21, 28)
INFO_OFFSETS = (0,)
PRICE_H = (0, 1, 2)
PRICE_WINDOW = 20
# W2
QTY_COMPONENT = {
    "COPR_OPEC": "ar_diff",
    "PAPR_NONOPEC": "ar_diff",
    "NGPRPUS": "ar_diff",
    "PATC_WORLD": "seasonal",
    "PATC_US": "seasonal",
    "NGWGPUS": "seasonal",
    "PASC_US": "seasonal",
    "COSXPUS": "seasonal",
}
QTY_H = (1, 3, 6, 12)
QTY_WINDOW = 12
MIN_PAST = 8
# W3 (declared; needs atlas.eia_wpsr)
W3_TESTS = (("COPRPUS", 0), ("COPRPUS", 1), ("COSXPUS", 0), ("COSXPUS", 1))

W1_CANDIDATES = (
    ("daily_rw", "steo", "ae"),
    ("combo_steo_daily", "steo", "ae"),
    ("daily_rw_ln", "steo_fair_ln", "crps"),
)
W2_CANDIDATES = (("steo_bc", "steo", "ae"), ("combo_steo_reva", "steo", "ae"), ("steo_bc_n", "steo_fair_n", "crps"))


def declared_tests() -> list[tuple[str, str, int, str, str, str]]:
    """Every test of the single Holm family: (window, code, h, challenger, benchmark, loss)."""
    out: list[tuple[str, str, int, str, str, str]] = []
    for code in SPOT_SERIES:
        for h in PRICE_H:
            out += [("W1", code, h, c, b, loss) for c, b, loss in W1_CANDIDATES]
    for code in QTY_COMPONENT:
        for h in QTY_H:
            out += [("W2", code, h, c, b, loss) for c, b, loss in W2_CANDIDATES]
    out += [("W3", code, h, "wpsr_nowcast", "steo", "ae") for code, h in W3_TESTS]
    return out


# --------------------------------------------------------------------------- W1 helpers (pure)


def _weekdays(y: int, m: int) -> list[date]:
    d = date(y, m, 1)
    out = []
    while d.month == m:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def known_daily(daily: Mapping[date, float], t: datetime) -> dict[date, float]:
    cut = t.date() - timedelta(days=DAILY_LAG_DAYS)
    return {d: v for d, v in daily.items() if d <= cut}


def daily_point(known: Mapping[date, float], target: int) -> float | None:
    """Month-mean forecast: known trading days of the target month + last known price for its remaining weekdays."""
    if not known:
        return None
    last_d = max(known)
    last_v = known[last_d]
    y, m = target // 12, target % 12 + 1
    inside = [v for d, v in known.items() if d.year == y and d.month == m]
    rest = [d for d in _weekdays(y, m) if d > last_d]
    n = len(inside) + len(rest)
    if n == 0:
        return None
    return (sum(inside) + last_v * len(rest)) / n


def month_mean_known(known: Mapping[date, float], target: int) -> float | None:
    """Monthly mean of a month entirely inside the known daily data (last calendar day of the month covered)."""
    y, m = target // 12, target % 12 + 1
    if not known:
        return None
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    if max(known) < nxt - timedelta(days=1):
        return None
    vals = [v for d, v in known.items() if d.year == y and d.month == m]
    return statistics.fmean(vals) if vals else None


def next_release_map(releases: Sequence[date]) -> dict[date, date]:
    rs = sorted(set(releases))
    return dict(itertools.pairwise(rs))


@dataclass
class W1Row:
    code: str
    issue: str
    issue_idx: int
    offset: int
    t: datetime
    h: int
    steo: float
    reva: float | None
    outcome: float | None


def w1_rows(
    origins: Sequence[Origin],
    release_of: Mapping[str, date],
    next_release: Mapping[date, date],
    daily: Mapping[str, Mapping[date, float]],
    outcomes: Mapping[str, Mapping[int, float]],
) -> tuple[list[W1Row], dict[str, int]]:
    rows: list[W1Row] = []
    skipped: dict[str, int] = defaultdict(int)
    for o in origins:
        rel = release_of.get(o.vintage)
        nxt = next_release.get(rel) if rel else None
        for k in (*INFO_OFFSETS, *OFFSETS):
            t = o.available_at + timedelta(days=k)
            if k > 0 and (nxt is None or t.date() >= nxt):
                skipped["origin not before the next STEO release (or next release unknown)"] += 1
                continue
            for code, sid in SPOT_SERIES.items():
                kn = known_daily(daily.get(sid, {}), t)
                for h in PRICE_H:
                    target = o.issue_idx + h
                    s = (o.steo.get(code) or {}).get(target)
                    if s is None or s <= 0:
                        skipped["no STEO projection for the target month"] += 1
                        continue
                    rows.append(
                        W1Row(
                            code,
                            o.vintage,
                            o.issue_idx,
                            k,
                            t,
                            h,
                            s,
                            daily_point(kn, target),
                            outcomes.get(code, {}).get(target),
                        )
                    )
    return rows, dict(skipped)


def _rms(xs: Sequence[float]) -> float:
    return math.sqrt(statistics.fmean(x * x for x in xs))


def w1_forecasts(rows: Sequence[W1Row], daily: Mapping[str, Mapping[date, float]]) -> list[dict[str, Any]]:
    """Point and density forecasts of every W1 row; training errors only from months fully known at the origin."""
    by_key: dict[tuple[str, int, int], list[W1Row]] = defaultdict(list)
    for r in rows:
        by_key[(r.code, r.h, r.offset)].append(r)
    out: list[dict[str, Any]] = []
    for (code, h, k), rs in by_key.items():
        rs = sorted(rs, key=lambda r: r.t)
        for i, r in enumerate(rs):
            kn = known_daily(daily.get(SPOT_SERIES[code], {}), r.t)
            past_s: list[float] = []
            past_r: list[float] = []
            for p in rs[:i][::-1]:
                if len(past_s) >= PRICE_WINDOW:
                    break
                y = month_mean_known(kn, p.issue_idx + p.h)
                if y is None or y <= 0 or p.reva is None or p.reva <= 0:
                    continue
                past_s.append(math.log(y) - math.log(p.steo))
                past_r.append(math.log(y) - math.log(p.reva))
            rec: dict[str, Any] = {
                "window": "W1",
                "code": code,
                "h": h,
                "offset": k,
                "issue": r.issue,
                "t": r.t.isoformat(),
                "target": label(r.issue_idx + h),
                "outcome": r.outcome,
                "n_past": len(past_s),
                "steo": r.steo,
                "daily_rw": r.reva,
            }
            if r.reva is not None and r.reva > 0 and len(past_s) >= MIN_PAST:
                ms, mr = statistics.fmean(e * e for e in past_s), statistics.fmean(e * e for e in past_r)
                w = (1 / ms) / (1 / ms + 1 / mr) if ms > 0 and mr > 0 else 0.5
                rec["w_steo"] = w
                rec["combo_steo_daily"] = math.exp(w * math.log(r.steo) + (1 - w) * math.log(r.reva))
                sr, ss = _rms(past_r), _rms(past_s)
                if sr > 0 and ss > 0:  # densities only with a positive spread (both or neither)
                    rec["daily_rw_ln"] = em.Pred("lognormal", r.reva, math.log(r.reva), sr)
                    rec["steo_fair_ln"] = em.Pred("lognormal", r.steo, math.log(r.steo), ss)
            out.append(rec)
    return out


# --------------------------------------------------------------------------- W2 helpers (pure)


def seasonal_point(x: Mapping[int, float], last: int, k: int) -> float | None:
    ch = [x[t0 + k] - x[t0] for j in range(1, 6) if (t0 := last - 12 * j) in x and t0 + k in x and t0 + k <= last]
    if len(ch) < 3 or last not in x:
        return None
    return x[last] + sum(ch) / len(ch)


def reva_component(code: str, x: Mapping[int, float], last: int, target: int) -> float | None:
    k = target - last
    if k < 1 or last not in x:
        return None
    if QTY_COMPONENT[code] == "seasonal":
        return seasonal_point(x, last, k)
    got = em._ar_diff(x, last, k)
    return got[0] if got else None


def w2_forecasts(origins: Sequence[Origin], outcomes: Mapping[str, Mapping[int, float]]) -> list[dict[str, Any]]:
    origins = sorted(origins, key=lambda o: o.available_at)
    out: list[dict[str, Any]] = []
    for code in QTY_COMPONENT:
        for h in QTY_H:
            hist: list[tuple[Origin, int, float, float | None]] = []  # (origin, target, steo, reva)
            for o in origins:
                target = o.issue_idx + h
                s = (o.steo.get(code) or {}).get(target)
                last = o.last.get(code)
                x = o.info.get(code) or {}
                r = reva_component(code, x, last, target) if last is not None else None
                es: list[float] = []
                er: list[float] = []
                for _po, ptarget, ps, pr in hist[::-1]:
                    if len(es) >= QTY_WINDOW:
                        break
                    if last is None or ptarget > last or ptarget not in x:
                        continue  # past target not yet published in this origin's information set
                    es.append(x[ptarget] - ps)
                    er.append(x[ptarget] - pr if pr is not None else math.nan)
                if s is not None:
                    hist.append((o, target, s, r))
                if s is None:
                    continue
                rec: dict[str, Any] = {
                    "window": "W2",
                    "code": code,
                    "h": h,
                    "issue": o.vintage,
                    "t": o.available_at.isoformat(),
                    "target": label(target),
                    "outcome": outcomes.get(code, {}).get(target),
                    "n_past": len(es),
                    "steo": s,
                    "reva_component": r,
                }
                if len(es) >= MIN_PAST:
                    bias = statistics.fmean(es)
                    rec["bias"] = bias
                    rec["steo_bc"] = s + bias
                    sd, rms = statistics.stdev(es), _rms(es)
                    if sd > 0 and rms > 0:  # densities only with a positive spread (both or neither)
                        rec["steo_bc_n"] = em.Pred("normal", s + bias, s + bias, sd)
                        rec["steo_fair_n"] = em.Pred("normal", s, s, rms)
                    pairs = [(a, b) for a, b in zip(es, er, strict=True) if math.isfinite(b)]
                    if r is not None and len(pairs) >= MIN_PAST:
                        ms = statistics.fmean(a * a for a, _ in pairs)
                        mr = statistics.fmean(b * b for _, b in pairs)
                        w = (1 / ms) / (1 / ms + 1 / mr) if ms > 0 and mr > 0 else 0.5
                        rec["w_steo"] = w
                        rec["combo_steo_reva"] = w * s + (1 - w) * r
                out.append(rec)
    return out


# --------------------------------------------------------------------------- scoring and tests (pure)


def _loss(f: Any, y: float, loss: str) -> float | None:
    if f is None:
        return None
    if loss == "crps":
        return em.crps(f, y) if isinstance(f, em.Pred) else None
    v = f.point if isinstance(f, em.Pred) else float(f)
    return abs(v - y) if loss == "ae" else (v - y) ** 2


def evaluate(
    recs: Sequence[dict[str, Any]], *, w3_status: str = "not evaluable: atlas.eia_wpsr not ingested"
) -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    pvals: dict[tuple[str, str, int, str], float] = {}
    by: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for r in recs:
        if r["outcome"] is not None:
            by[(r["window"], r["code"], r["h"])].append(r)
    for win, code, h, cand, bench, loss in declared_tests():
        key = (win, code, h, cand)
        if win == "W3":
            tests.append(
                {
                    "window": win,
                    "code": code,
                    "h": h,
                    "challenger": cand,
                    "benchmark": bench,
                    "loss": loss,
                    "status": w3_status,
                    "p_challenger_better": 1.0,
                }
            )
            pvals[key] = 1.0
            continue
        rs = [r for r in by.get((win, code, h), []) if (win == "W2" or r["offset"] in OFFSETS)]
        per: dict[str, list[float]] = defaultdict(list)  # issue -> differentials (W1: pooled over offsets)
        lc: list[float] = []
        lb: list[float] = []
        for r in rs:
            a, b = _loss(r.get(cand), r["outcome"], loss), _loss(r.get(bench), r["outcome"], loss)
            if a is None or b is None:
                continue
            per[r["issue"]].append(a - b)
            lc.append(a)
            lb.append(b)
        d = [statistics.fmean(v) for _, v in sorted(per.items())]
        k = 1 if win == "W1" else overlap_steps(h)
        t = dm_hln(d, k)
        row = {
            "window": win,
            "code": code,
            "h": h,
            "challenger": cand,
            "benchmark": bench,
            "loss": loss,
            "n_issues": len(d),
            "n_forecasts": len(lc),
            **t,
        }
        if lc:
            row["mean_loss_challenger"] = statistics.fmean(lc)
            row["mean_loss_benchmark"] = statistics.fmean(lb)
            row["ratio"] = (
                row["mean_loss_challenger"] / row["mean_loss_benchmark"] if row["mean_loss_benchmark"] else None
            )
        tests.append(row)
        pvals[key] = float(t.get("p_challenger_better", 1.0))
    sig = holm(pvals, ALPHA)
    for row in tests:
        row["holm_family_size"] = len(pvals)
        row["holm_win"] = sig[(row["window"], row["code"], row["h"], row["challenger"])]
        p2 = row.get("p_two_sided")
        row["steo_significantly_better_unadjusted"] = bool(
            p2 is not None and p2 < ALPHA and row.get("mean_diff", 0) > 0
        )
    return {"tests": tests, "family_size": len(pvals), "wins": [t for t in tests if t["holm_win"]]}


def informational(recs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-offset W1 ratios (offset 0 = the V6 origin date; not in the Holm family)."""
    out = []
    groups: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
    for r in recs:
        if r["window"] == "W1" and r["outcome"] is not None:
            groups[(r["code"], r["h"], r["offset"])].append(r)
    for (code, h, k), rs in sorted(groups.items()):
        ok = [r for r in rs if r["daily_rw"] is not None]
        if not ok:
            continue
        ae_r = statistics.fmean(abs(r["daily_rw"] - r["outcome"]) for r in ok)
        ae_s = statistics.fmean(abs(r["steo"] - r["outcome"]) for r in ok)
        out.append(
            {
                "code": code,
                "h": h,
                "offset": k,
                "n": len(ok),
                "mae_daily_rw": ae_r,
                "mae_steo": ae_s,
                "ratio": ae_r / ae_s if ae_s else None,
            }
        )
    return out


def coverage80(recs: Sequence[dict[str, Any]], key: str) -> float | None:
    pits = [em.pit(r[key], r["outcome"]) for r in recs if isinstance(r.get(key), em.Pred) and r["outcome"] is not None]
    ok = [p for p in pits if p is not None]
    return sum(1 for p in ok if 0.1 <= p <= 0.9) / len(ok) if ok else None


def _plain(v: Any) -> Any:
    if isinstance(v, em.Pred):
        return {"dist": v.dist, "point": v.point, "mu": v.mu, "sigma": v.sigma}
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, np.floating):
        return float(v)
    return v


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def run(
    w1_origins: Sequence[Origin],
    w2_origins: Sequence[Origin],
    release_of: Mapping[str, date],
    daily: Mapping[str, Mapping[date, float]],
    outcomes: Mapping[str, Mapping[int, float]],
) -> dict[str, Any]:
    """``release_of``: every listed STEO issue (``YYYY-MM`` -> release date), monthly issues included."""
    rows, skipped = w1_rows(w1_origins, release_of, next_release_map(list(release_of.values())), daily, outcomes)
    recs = w1_forecasts(rows, daily) + w2_forecasts(w2_origins, outcomes)
    ev = evaluate(recs)
    cov = {}
    for win, keys in (("W1", ("daily_rw_ln", "steo_fair_ln")), ("W2", ("steo_bc_n", "steo_fair_n"))):
        for key in keys:
            for code in SPOT_SERIES if win == "W1" else QTY_COMPONENT:
                rr = [r for r in recs if r["window"] == win and r["code"] == code and r.get("offset", 7) != 0]
                cov[f"{win}|{code}|{key}"] = coverage80(rr, key)
    res = {
        "label": LABEL,
        "protocol": PROTOCOL_PATH,
        "w1_skipped": skipped,
        "family_size": ev["family_size"],
        "tests": ev["tests"],
        "wins": ev["wins"],
        "informational_w1_per_offset": informational(recs),
        "coverage_80": cov,
        "n_records": len(recs),
    }
    res["digest"] = digest(res)
    res["records"] = [{k: _plain(v) for k, v in r.items()} for r in recs]
    return res


# The database entry point (load_and_run) lives in reva.cli.beat_energy (invariant 11: no adapter imports here).
