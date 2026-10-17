"""Reproduction of published results (``registry/estimation/replications.yaml``) before any new estimate.

Each target declares its published numbers (verbatim quotes), REVA's specification, data, documented
differences and a tolerance fixed before the first run. :func:`run_replications` computes every target on a
pinned atlas snapshot and returns per-target comparisons with a PASS / FAIL verdict. New estimates are allowed
only when at least :data:`MIN_PASSED` targets pass (:func:`gate`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from scipy import stats  # type: ignore[import-untyped]

from reva.estimation.core import EstimationError, bin_means, hp_filter
from reva.estimation.data import PanelData, SnapshotPanels, canonical_sha256
from reva.kernel.errors import ValidationProblem
from reva.ontology.registry import REGISTRY_ROOT

REPLICATIONS_PATH = REGISTRY_ROOT / "estimation" / "replications.yaml"
MIN_PASSED = 3


def load_targets(path: Path = REPLICATIONS_PATH) -> dict[str, dict[str, Any]]:
    doc = yaml.safe_load(path.read_text())
    return {t["id"]: t for t in doc["targets"]}


def registry_sha256(path: Path = REPLICATIONS_PATH) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- JST (2013) Table 4
def jst_excluded(peak: int) -> bool:
    for a, b in ((1914, 1918), (1939, 1945)):
        if peak + 5 >= a and peak - 2 <= b:
            return True
    return peak + 5 > 2008


def jst_table4(target: Mapping[str, Any], y: PanelData) -> dict[str, Any]:
    pub = target["published"]["coefficients"]
    horizons = list(target["published"]["horizons"])
    rows: list[tuple[str, str, int]] = []
    excluded = 0
    for iso3, d in sorted(target["peaks"].items()):
        for kind in ("N", "F"):
            for p in d.get(kind, []):
                if jst_excluded(int(p)):
                    excluded += 1
                    continue
                rows.append((iso3, kind, int(p)))
    per_h: dict[int, dict[str, dict[str, float]]] = {}
    missing = 0
    for h in horizons:
        vals, bins = [], []
        for iso3, kind, p in rows:
            a, b = y.values.get((iso3, p)), y.values.get((iso3, p + h))
            if a is None or b is None or a <= 0 or b <= 0:
                vals.append(math.nan)
                missing += 1
            else:
                vals.append(100.0 * (math.log(b) - math.log(a)))
            bins.append(kind)
        per_h[h] = bin_means(np.asarray(vals), bins, vcov="classical")
    comparisons = []
    ok_all = True
    for kind in ("N", "F"):
        for i, h in enumerate(horizons):
            r = per_h[h][kind]
            pp, ps = float(pub[kind]["point"][i]), float(pub[kind]["se"][i])
            diff = r["point"] - pp
            within = abs(diff) <= target["tolerance"]["max_se_multiple"] * ps + 1e-9
            ok_all &= within
            comparisons.append(
                {
                    "bin": kind,
                    "h": h,
                    "reva": round(r["point"], 3),
                    "reva_se": round(r["se"], 3),
                    "reva_n": r["n"],
                    "published": pp,
                    "published_se": ps,
                    "diff": round(diff, 3),
                    "diff_in_published_se": round(diff / ps, 2),
                    "within_tolerance": within,
                }
            )
    counts = {k: sum(1 for _, kk, _ in rows if kk == k) for k in ("N", "F")}
    counts_ok = counts["N"] == pub["N"]["n"] and counts["F"] == pub["F"]["n"]
    return {
        "target": target["id"],
        "verdict": "PASS" if ok_all and counts_ok else "FAIL",
        "counts": counts,
        "counts_match": counts_ok,
        "peaks_excluded": excluded,
        "missing_outcome_cells": missing,
        "comparisons": comparisons,
        "within": sum(1 for c in comparisons if c["within_tolerance"]),
        "of": len(comparisons),
        "outcome": y.summary(),
    }


# --------------------------------------------------------------------------- Laeven-Valencia output losses
def _index_from_growth(g: Mapping[int, float]) -> dict[int, float]:
    """Chain annual % growth rates into a level index over the longest contiguous run(s); each run is its own
    index (a gap restarts at 100 - log trends per window only use one contiguous run)."""
    out: dict[int, float] = {}
    level = None
    prev = None
    for yr in sorted(g):
        if prev is None or yr != prev + 1 or level is None:
            level = 100.0
            out[yr - 1] = level if (yr - 1) not in out else out[yr - 1]
        level = level * (1.0 + g[yr] / 100.0)
        out[yr] = level
        prev = yr
    return out


def _contiguous_back(series: Mapping[int, float], end: int, start: int) -> list[int]:
    yrs = []
    t = end
    while t >= start and t in series and series[t] > 0:
        yrs.append(t)
        t -= 1
    return sorted(yrs)


def lv_loss(series: Mapping[int, float], t0: int, *, extrapolation: str = "last") -> float | None:
    """Output loss of a crisis starting in ``t0`` on a positive real GDP level ``series`` (year -> level)."""
    pre = _contiguous_back(series, t0 - 1, t0 - 20)
    if len(pre) < 4 or any((t0 + k) not in series or series[t0 + k] <= 0 for k in range(4)):
        return None
    # the index restarts are run-local: require T..T+3 in the same contiguous run as the window
    if any((t0 + k - 1) not in series for k in range(4)):
        return None
    tau = hp_filter([math.log(series[t]) for t in pre], 100.0)
    if extrapolation == "last":
        g = float(tau[-1] - tau[-2])
    elif extrapolation == "mean":
        g = float(np.mean(np.diff(tau)))
    else:
        raise ValidationProblem("unknown extrapolation", extrapolation=extrapolation)
    loss = 0.0
    for k in range(4):
        trend = math.exp(float(tau[-1]) + (k + 1) * g)
        loss += 100.0 * (trend - series[t0 + k]) / trend
    return max(loss, 0.0)


def lv_output_loss(target: Mapping[str, Any], panels: SnapshotPanels) -> dict[str, Any]:
    pub = panels.outcome(target["published"]["series"])
    weo = panels.outcome(target["data"]["primary"])
    wdi = panels.outcome(target["data"]["fallback"])
    weo_by: dict[str, dict[int, float]] = {}
    for (c, t), v in weo.values.items():
        weo_by.setdefault(c, {})[t] = v
    wdi_by: dict[str, dict[int, float]] = {}
    for (c, t), v in wdi.values.items():
        wdi_by.setdefault(c, {})[t] = v
    weo_idx = {c: _index_from_growth(g) for c, g in weo_by.items()}
    rows = []
    for (c, t0), published in sorted(pub.values.items()):
        wi = weo_idx.get(c, {})
        use_weo = all((t0 + k) in wi for k in range(-4, 4))
        primary = lv_loss(wi, t0) if use_weo else None
        src = "weo" if primary is not None else None
        if primary is None:
            primary = lv_loss(wdi_by.get(c, {}), t0)
            src = "wdi" if primary is not None else None
        rows.append(
            {
                "iso3": c,
                "start": t0,
                "published": published,
                "reva": None if primary is None else round(primary, 3),
                "data": src,
                "reva_wdi_only": _r(lv_loss(wdi_by.get(c, {}), t0)),
                "reva_mean_trend_growth": _r(
                    lv_loss(wi, t0, extrapolation="mean")
                    if src == "weo"
                    else lv_loss(wdi_by.get(c, {}), t0, extrapolation="mean")
                ),
            }
        )
    tol = target["tolerance"]

    def score(key: str) -> dict[str, Any]:
        pairs = [(r["published"], r[key]) for r in rows if r[key] is not None]
        if len(pairs) < 3:
            return {"n": len(pairs)}
        a = np.array(pairs)
        rho = float(stats.spearmanr(a[:, 0], a[:, 1]).statistic)
        mad = float(np.median(np.abs(a[:, 0] - a[:, 1])))
        within10 = float(np.mean(np.abs(a[:, 0] - a[:, 1]) <= 10.0))
        zero_agree = float(np.mean((a[:, 0] == 0) == (a[:, 1] == 0)))
        return {
            "n": len(pairs),
            "spearman": round(rho, 4),
            "median_abs_diff": round(mad, 3),
            "share_within_10pp": round(within10, 3),
            "zero_agreement": round(zero_agree, 3),
            "mean_published": round(float(a[:, 0].mean()), 3),
            "mean_reva": round(float(a[:, 1].mean()), 3),
        }

    prim = score("reva")
    passed = (
        prim.get("n", 0) >= tol["min_n"]
        and prim.get("spearman", -1) >= tol["min_spearman"]
        and prim.get("median_abs_diff", 1e9) <= tol["max_median_abs_diff"]
    )
    return {
        "target": target["id"],
        "verdict": "PASS" if passed else "FAIL",
        "primary": prim,
        "sensitivity": {"wdi_only": score("reva_wdi_only"), "mean_trend_growth": score("reva_mean_trend_growth")},
        "published_crises": len(rows),
        "not_computable": sum(1 for r in rows if r["reva"] is None),
        "data_used": {
            "weo": sum(1 for r in rows if r["data"] == "weo"),
            "wdi": sum(1 for r in rows if r["data"] == "wdi"),
        },
        "rows": rows,
        "inputs": {"published": pub.summary(), "weo": weo.summary(), "wdi": wdi.summary()},
    }


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 3)


# --------------------------------------------------------------------------- Cerra-Saxena (2007) Table 2
def recession_episodes(growth: Mapping[tuple[str, int], float], years: tuple[int, int]) -> list[dict[str, Any]]:
    by: dict[str, dict[int, float]] = {}
    for (c, t), v in growth.items():
        if years[0] <= t <= years[1]:
            by.setdefault(c, {})[t] = v
    eps = []
    for c, g in sorted(by.items()):
        run: list[int] = []
        for t in sorted(g):
            if g[t] < 0 and (not run or run[-1] == t - 1):
                run.append(t)
                continue
            if run:
                eps.append({"iso3": c, "start": run[0], "years": len(run), "loss": sum(g[x] for x in run)})
                run = []
            if g[t] < 0:
                run = [t]
        if run:
            eps.append({"iso3": c, "start": run[0], "years": len(run), "loss": sum(g[x] for x in run)})
    return eps


def cs_table2(
    target: Mapping[str, Any], y: PanelData, banking_starts: set[tuple[str, int]] | None = None
) -> dict[str, Any]:
    eps = recession_episodes(y.values, (1960, 2001))
    pub = target["published"]
    tol = target["tolerance"]
    loss = float(np.mean([e["loss"] for e in eps])) if eps else math.nan
    dur = float(np.mean([e["years"] for e in eps])) if eps else math.nan
    n = len(eps)
    ok = (
        abs(loss - pub["mean_cumulative_loss"]) <= tol["loss_abs"]
        and abs(dur - pub["mean_duration_years"]) <= tol["duration_abs"]
        and abs(n - pub["n_episodes"]) <= tol["n_rel"] * pub["n_episodes"]
    )
    out: dict[str, Any] = {
        "target": target["id"],
        "verdict": "PASS" if ok else "FAIL",
        "reva": {"mean_cumulative_loss": round(loss, 3), "mean_duration_years": round(dur, 3), "n_episodes": n},
        "published": {k: pub[k] for k in ("mean_cumulative_loss", "mean_duration_years", "n_episodes")},
        "countries_with_episodes": len({e["iso3"] for e in eps}),
        "outcome": y.summary(),
    }
    if banking_starts is not None:
        # documented-difference variant (not part of the verdict): recessions whose run contains a
        # Laeven-Valencia banking crisis start year or starts the year after one (the paper used
        # Caprio-Klingebiel dates; its association rule is not stated)
        bk = [
            e
            for e in eps
            if any((e["iso3"], t) in banking_starts for t in range(e["start"] - 1, e["start"] + e["years"]))
        ]
        out["variant_banking_crisis_lv_dates"] = {
            "published_row": "Banking crisis –11.7 2.19 104 (Caprio-Klingebiel dates)",
            "mean_cumulative_loss": round(float(np.mean([e["loss"] for e in bk])), 3) if bk else None,
            "mean_duration_years": round(float(np.mean([e["years"] for e in bk])), 3) if bk else None,
            "n_episodes": len(bk),
            "note": "not a reproduction: different crisis dates and an association rule chosen by REVA",
        }
    return out


# --------------------------------------------------------------------------- driver
def run_replications(
    panels: SnapshotPanels, *, banking_starts: set[tuple[str, int]] | None = None, only: list[str] | None = None
) -> dict[str, Any]:
    targets = load_targets()
    results = []
    for tid, t in targets.items():
        if only and tid not in only:
            continue
        try:
            if t["design"] == "event_sample_bins":
                results.append(jst_table4(t, panels.outcome(t["data"]["outcome"])))
            elif t["design"] == "per_crisis_computation":
                results.append(lv_output_loss(t, panels))
            elif t["design"] == "recession_episodes":
                results.append(cs_table2(t, panels.outcome(t["data"]["outcome"]), banking_starts))
            else:
                raise ValidationProblem("unknown replication design", design=t["design"])
        except EstimationError as e:
            results.append({"target": tid, "verdict": "ERROR", "error": str(e)})
    passed = [r["target"] for r in results if r["verdict"] == "PASS"]
    return {
        "snapshot": panels.describe(),
        "registry_sha256": registry_sha256(),
        "results": results,
        "passed": passed,
        "failed": [r["target"] for r in results if r["verdict"] != "PASS"],
        "gate_open": len(passed) >= MIN_PASSED,
        "min_passed": MIN_PASSED,
        "digest": canonical_sha256([{k: v for k, v in r.items() if k != "outcome"} for r in results]),
    }


def gate(report: Mapping[str, Any]) -> None:
    """Refuse new estimates unless at least MIN_PASSED published results were reproduced."""
    if not report.get("gate_open"):
        raise ValidationProblem(
            "new estimates are refused: fewer than 3 published results reproduced within tolerance",
            passed=list(report.get("passed", [])),
        )
