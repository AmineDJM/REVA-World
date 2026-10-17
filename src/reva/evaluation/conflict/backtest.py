"""As-of rolling-origin back-test of the conflict forecasters, with DM tests and the VIEWS comparison (pure).

Origins ``t`` (data through calendar year ``t``) from 2004 to ``last - h``; every model is fitted only on
country-years whose target year ``s + h`` is at or before ``t``; forecasts for ``t + h`` are scored against the
outcome published in the pinned table. Values are the **final vintage** (UCDP 26.1, V-Dem v16, current WDI), not
the data as published at each origin: a *final-vintage pseudo-real-time* back-test, never real-time skill.

Scores: binary -- Brier and log loss (probabilities clipped to [1e-6, 1-1e-6] for the log loss only); quantity --
CRPS of the predictive sample (a point forecast is a point mass: its CRPS is its absolute error), absolute error
of the point and squared error of ``ln(1 + point)`` vs ``ln(1 + y)`` (MSLE, VIEWS' customary metric).

Decision rule (declared before running): the candidate becomes a *registry forecaster* for a (family, horizon)
only if its mean primary loss (Brier for binary, CRPS for quantity) is lower than **every** baseline's and every
one-sided DM test candidate-vs-baseline (cluster-robust by country) has a Holm-adjusted p-value < 0.05, the Holm
family being all candidate-vs-baseline primary tests of the back-test.

VIEWS: the December run of year ``Y`` (last input data December ``Y``) gives the h = 1 and h = 2 annual sums for
``Y + 1`` and ``Y + 2`` (quantity family only); REVA's forecasts at origin ``Y`` use the final UCDP data of ``Y``
(published around June ``Y + 1``, *after* VIEWS released its run, and revised since): the comparison is tilted
in REVA's favour on information, stated with every result.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from reva.evaluation.conflict.families import FAMILIES, ConflictFamily
from reva.evaluation.conflict.models import (
    BASELINE_KEYS,
    CANDIDATE_KEYS,
    VIEWS_KEY,
    Example,
    features,
    forecast_family,
)
from reva.evaluation.conflict.panel import Panel
from reva.evaluation.conflict.stats import dm_test, holm
from reva.evaluation.conflict.views import ViewsRun, december_runs
from reva.evaluation.registry.scores import crps_empirical

FIRST_TRAIN_YEAR = 1993  # five years of fatality history (orgvio_cy starts in 1989)
FIRST_ORIGIN = 2004
ALPHA = 0.05
PRIMARY = {"binary": "brier", "quantity": "crps"}
SECONDARY = {"binary": ("logloss",), "quantity": ("msle", "abs_error")}
LABEL = (
    "final-vintage pseudo-real-time back-test (country-year conflict families): every fit uses only target years "
    "up to its origin, but values are the final vintage (UCDP 26.1, V-Dem v16, current WDI); never real-time skill"
)


def build_examples(p: Panel, h: int) -> dict[int, list[Example]]:
    out: dict[int, list[Example]] = defaultdict(list)
    for g in p.geos():
        for t in sorted(p.deaths[g]):
            x = features(p, g, t)
            if x is None:
                continue
            out[t].append(Example(g, t, p.deaths[g][t], x, p.y(g, t + h)))
    return dict(out)


def _score(fam: ConflictFamily, fc: Any, y: float) -> dict[str, float]:
    if fam.kind == "binary":
        o = fam.event(y)
        pr = float(fc.prob)
        pc = min(max(pr, 1e-6), 1 - 1e-6)
        return {"o": o, "p": pr, "brier": (pr - o) ** 2, "logloss": -math.log(pc if o else 1 - pc)}
    pt = float(fc.point)
    return {
        "y": y,
        "point": pt,
        "crps": crps_empirical(fc.sample, y) if fc.sample else abs(pt - y),
        "abs_error": abs(pt - y),
        "msle": (math.log1p(max(pt, 0.0)) - math.log1p(y)) ** 2,
    }


def _summary(fam: ConflictFamily, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n_scored": 0}
    keys = ("brier", "logloss") if fam.kind == "binary" else ("crps", "abs_error", "msle")
    out: dict[str, Any] = {"n_scored": len(rows)}
    for k in keys:
        out[k] = math.fsum(r[k] for r in rows) / len(rows)
    if fam.kind == "binary":
        out["base_rate"] = sum(r["o"] for r in rows) / len(rows)
        out["mean_prob"] = math.fsum(r["p"] for r in rows) / len(rows)
        bins: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
        for r in rows:
            bins[min(int(r["p"] * 10), 9)].append(r)
        out["calibration"] = [
            {
                "bin": f"{b / 10:.1f}-{(b + 1) / 10:.1f}",
                "n": len(v),
                "mean_prob": math.fsum(r["p"] for r in v) / len(v),
                "freq": sum(r["o"] for r in v) / len(v),
            }
            for b, v in sorted(bins.items())
        ]
    return out


def run_backtest(
    p: Panel,
    *,
    horizons: Sequence[int] = (1, 2),
    first_origin: int = FIRST_ORIGIN,
    families: Sequence[ConflictFamily] = FAMILIES,
    views_runs: Mapping[str, ViewsRun] | None = None,
) -> dict[str, Any]:
    last = p.last_year
    models = (*BASELINE_KEYS, *CANDIDATE_KEYS)
    scored: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    abstained: dict[tuple[str, int, str], int] = defaultdict(int)
    for h in horizons:
        ex = build_examples(p, h)
        for t0 in range(first_origin, last - h + 1):
            for fam in families:
                train = [
                    e
                    for t in range(FIRST_TRAIN_YEAR, t0 - h + 1)
                    for e in ex.get(t, ())
                    if e.target is not None and fam.applies(e.last)
                ]
                test = [e for e in ex.get(t0, ()) if e.target is not None and fam.applies(e.last)]
                if not test:
                    continue
                for mk in models:
                    fcs = forecast_family(mk, fam, p, h, train, test)
                    for e, fc in zip(test, fcs, strict=True):
                        if fc.abstain:
                            abstained[(fam.key, h, mk)] += 1
                            continue
                        assert e.target is not None
                        scored[(fam.key, h, mk)].append(
                            {"geo": e.geo, "origin": t0, "year": t0 + h, **_score(fam, fc, e.target)}
                        )
    views_scored: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    views_missing: dict[tuple[str, int], int] = defaultdict(int)
    views_used: dict[str, Any] = {}
    if views_runs:
        dec = december_runs(dict(views_runs))
        for fam in families:
            if fam.kind != "quantity":
                continue
            for h in horizons:
                for y0, vr in sorted(dec.items()):
                    target = y0 + h
                    if target > last or not vr.covers_year(target):
                        continue
                    for g in p.geos():
                        y = p.y(g, target)
                        if y is None or p.y(g, y0) is None:
                            continue
                        v = vr.annual(g, target)
                        if v is None:
                            views_missing[(fam.key, h)] += 1
                            continue
                        views_scored[(fam.key, h)].append(
                            {
                                "geo": g,
                                "origin": y0,
                                "year": target,
                                "run": vr.run,
                                "y": y,
                                "point": v,
                                "crps": abs(v - y),
                                "abs_error": abs(v - y),
                                "msle": (math.log1p(max(v, 0.0)) - math.log1p(y)) ** 2,
                            }
                        )
                    views_used[vr.run] = {"release_date": vr.release_date, "last_input": list(vr.last_input)}
    return evaluate(scored, abstained, views_scored, views_missing, views_used, families, horizons, last)


def _pair(
    a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]], metric: str
) -> tuple[list[float], list[str]]:
    ib = {(r["geo"], r["origin"]): r for r in b}
    d, cl = [], []
    for r in a:
        o = ib.get((r["geo"], r["origin"]))
        if o is not None:
            d.append(r[metric] - o[metric])
            cl.append(r["geo"])
    return d, cl


def evaluate(
    scored: Mapping[tuple[str, int, str], list[dict[str, Any]]],
    abstained: Mapping[tuple[str, int, str], int],
    views_scored: Mapping[tuple[str, int], list[dict[str, Any]]],
    views_missing: Mapping[tuple[str, int], int],
    views_used: Mapping[str, Any],
    families: Sequence[ConflictFamily],
    horizons: Sequence[int],
    last: int,
) -> dict[str, Any]:
    fam_out: list[dict[str, Any]] = []
    tests: dict[str, dict[str, Any]] = {}
    pvals: dict[str, float | None] = {}
    secondary: dict[str, dict[str, Any]] = {}
    for fam in families:
        prim = PRIMARY[fam.kind]
        for h in horizons:
            results = []
            for mk in (*BASELINE_KEYS, *CANDIDATE_KEYS):
                rows = scored.get((fam.key, h, mk), [])
                results.append({"model": mk, **_summary(fam, rows), "n_abstained": abstained.get((fam.key, h, mk), 0)})
            pers = scored.get((fam.key, h, "forecast.conflict_persistence"), [])
            for r in results:
                rows = scored.get((fam.key, h, r["model"]), [])
                d, _ = _pair(rows, pers, prim)
                if d:
                    common = {(x["geo"], x["origin"]) for x in rows}
                    base = [x for x in pers if (x["geo"], x["origin"]) in common]
                    bm = math.fsum(x[prim] for x in base) / len(base)
                    r[f"{prim}_vs_persistence"] = (bm + math.fsum(d) / len(d)) / bm if bm else None
            for cand in CANDIDATE_KEYS:
                for base in BASELINE_KEYS:
                    d, cl = _pair(scored.get((fam.key, h, cand), []), scored.get((fam.key, h, base), []), prim)
                    k = f"{fam.key}|h{h}|{cand}|vs|{base}|{prim}"
                    tests[k] = {"family": fam.key, "horizon": h, "candidate": cand, "baseline": base, "metric": prim,
                                **dm_test(d, cl)}
                    pvals[k] = tests[k]["p_one_sided"]
                    for m2 in SECONDARY[fam.kind]:
                        d2, cl2 = _pair(scored.get((fam.key, h, cand), []), scored.get((fam.key, h, base), []), m2)
                        secondary[f"{fam.key}|h{h}|{cand}|vs|{base}|{m2}"] = {"metric": m2, **dm_test(d2, cl2)}
            fam_out.append({"family": fam.key, "horizon": h, "kind": fam.kind, "primary_metric": prim,
                            "results": results})
    adj = holm(pvals)
    for k, t in tests.items():
        t["p_holm"] = adj.get(k)
    winners = []
    for fam in families:
        for h in horizons:
            for cand in CANDIDATE_KEYS:
                ts = [t for t in tests.values() if t["family"] == fam.key and t["horizon"] == h and t["candidate"] == cand]
                ok = bool(ts) and all(
                    t["mean_diff"] is not None and t["mean_diff"] < 0 and t["p_holm"] is not None and t["p_holm"] < ALPHA
                    for t in ts
                )
                if ok:
                    winners.append({"family": fam.key, "horizon": h, "model": cand})
    # VIEWS comparison (quantity family)
    vtests: dict[str, dict[str, Any]] = {}
    vp: dict[str, float | None] = {}
    vsum: list[dict[str, Any]] = []
    for (fk, h), vrows in sorted(views_scored.items()):
        fam = next(f for f in families if f.key == fk)
        vs = _summary(fam, vrows)
        vsum.append({"family": fk, "horizon": h, "model": VIEWS_KEY, **vs, "n_missing": views_missing.get((fk, h), 0),
                     "runs": sorted({r["run"] for r in vrows})})
        for mk in (*BASELINE_KEYS, *CANDIDATE_KEYS):
            rows = scored.get((fk, h, mk), [])
            for metric in ("crps", "msle"):
                iv = {(r["geo"], r["origin"]): r for r in vrows}
                pairs = [(r, iv[(r["geo"], r["origin"])]) for r in rows if (r["geo"], r["origin"]) in iv]
                d = [a[metric] - b[metric] for a, b in pairs]
                cl = [a["geo"] for a, _ in pairs]
                k = f"{fk}|h{h}|{mk}|vs|{VIEWS_KEY}|{metric}"
                vtests[k] = {
                    "family": fk,
                    "horizon": h,
                    "model": mk,
                    "metric": metric,
                    "model_mean": math.fsum(a[metric] for a, _ in pairs) / len(pairs) if pairs else None,
                    "views_mean": math.fsum(b[metric] for _, b in pairs) / len(pairs) if pairs else None,
                    **dm_test(d, cl),
                }
                vp[k] = vtests[k]["p_two_sided"]
    vadj = holm(vp)
    for k, t in vtests.items():
        t["p_holm_two_sided"] = vadj.get(k)
    return {
        "label": LABEL,
        "vintage_mode": "final_vintage_pseudo_real_time",
        "last_outcome_year": last,
        "first_origin": FIRST_ORIGIN,
        "first_train_year": FIRST_TRAIN_YEAR,
        "decision_rule": {
            "alpha": ALPHA,
            "primary_metric": PRIMARY,
            "rule": "candidate issued for a (family, horizon) only if its mean primary loss is below every "
            "baseline's and every one-sided cluster-robust DM test vs a baseline has Holm-adjusted p < alpha "
            "(Holm over all candidate-vs-baseline primary tests)",
        },
        "families": fam_out,
        "dm_tests": tests,
        "dm_secondary": secondary,
        "winners": winners,
        "views": {
            "summary": vsum,
            "tests": vtests,
            "runs": dict(views_used),
            "note": "VIEWS December runs: sum of 12 monthly point predictions per calendar year (REVA aggregation); "
            "REVA's origin-Y forecasts use final UCDP data for Y, published after VIEWS' run and revised since, so the "
            "comparison favours REVA on information; DM variance cluster-robust by country, cross-country "
            "dependence within a year not modelled",
        },
        "dependence_note": "cluster-robust by country; cross-country dependence within a year is not modelled",
    }


__all__ = ["FIRST_ORIGIN", "FIRST_TRAIN_YEAR", "LABEL", "build_examples", "evaluate", "run_backtest"]
