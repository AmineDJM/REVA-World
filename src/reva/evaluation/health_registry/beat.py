"""V7 BEAT-HEALTH: post-processing / stacking of the CDC hub ensembles, frozen for the pre-registered evaluation.

Pre-registration: ``registry/forecasts/beat_health/protocol.yaml`` (records the sha256 of this file; the holdout
evaluation refuses to run if this file changed). Pure numpy, no database.

Information set of round R (reference date R): the hub ensemble quantiles of round R, REVA's ``health.blend`` computed
on the NHSN vintage of the round (as in the V6 as-of back-test), and the *as-of* truth of earlier rounds' targets,
i.e. the value of each past target week as published in round R's vintage (never a later revision). Weights and
recalibration factors are chosen per horizon, pooled over every location of the hub hierarchy (US + states), on the
past WIS only; before ``MIN_PAST`` scored past tasks exist the candidate equals the ensemble.

Candidates (frozen):

* ``beat.qa_blend`` - quantile averaging ``w * ensemble + (1 - w) * REVA blend``, ``w`` in ``QA_GRID`` minimising
  the summed past WIS of that horizon.
* ``beat.width_recal`` - the ensemble's quantiles stretched around its median, ``m + s (q - m)``, ``s`` in
  ``WIDTH_GRID`` minimising the summed past WIS (calibration correction from the ensemble's own past errors).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from reva.models.forecasting import health_quantile as HQ

LEVELS = np.array(HQ.HUB_LEVELS)
K = len(LEVELS)
MED = K // 2
MIN_PAST = 200
QA_GRID = tuple(round(x, 2) for x in np.linspace(0.0, 1.0, 21))
WIDTH_GRID = (0.8, 0.9, 1.0, 1.1, 1.2, 1.35, 1.5)
DM_LAG = 3
CANDIDATES = ("beat.qa_blend", "beat.width_recal")
TRUTHS = ("final", "first_release")


def wis_rows(q: np.ndarray, y: np.ndarray) -> np.ndarray:
    """WIS of each row of ``q`` [N, K] at ``y`` [N] (identical to :func:`HQ.wis`: sum of pinball / (k + 1/2))."""
    d = y[:, None] - q
    return np.asarray(np.maximum(LEVELS * d, (LEVELS - 1) * d).sum(-1) / (MED + 0.5))


def stretch(q: np.ndarray, s: float) -> np.ndarray:
    m = q[:, MED : MED + 1]
    return np.asarray(np.maximum(m + s * (q - m), 0.0))


@dataclass
class Panel:
    """One hub family's as-of tasks: row arrays plus the as-of history of each (round, location)."""

    refs: list[str]
    r: np.ndarray  # round index of each row
    h: np.ndarray
    geo: list[str]
    target: np.ndarray
    ens: np.ndarray  # [N, K]
    blend: np.ndarray  # [N, K]; ensemble where the blend abstained (flagged in blend_ok)
    blend_ok: np.ndarray
    truth: dict[str, np.ndarray]  # truth kind -> [N] (nan = unknown)
    asof: dict[tuple[str, str], dict[int, float]]

    @property
    def n(self) -> int:
        return len(self.r)


def first_release(vintages: Mapping[date, Mapping[str, Mapping[int, float]]]) -> dict[tuple[str, int], float]:
    """(location, epiweek index) -> value in the earliest vintage publishing it."""
    out: dict[tuple[str, int], float] = {}
    for v in sorted(vintages):
        for g, cells in vintages[v].items():
            for t, val in cells.items():
                out.setdefault((g, t), val)
    return out


def build_panel(
    rows: Sequence[Mapping[str, Any]],
    vintages: Mapping[date, Mapping[str, Mapping[int, float]]],
) -> Panel:
    """``rows``: dicts with ref, geo, h, target, history (as-of), truth (final or None), ens (K values), blend (K
    values or None)."""
    refs = sorted({str(x["ref"]) for x in rows})
    ri = {r: i for i, r in enumerate(refs)}
    fr = first_release(vintages)
    asof: dict[tuple[str, str], dict[int, float]] = {}
    for x in rows:
        asof.setdefault((str(x["ref"]), str(x["geo"])), dict(x["history"]))
    ens = np.array([x["ens"] for x in rows], dtype=float).reshape(-1, K)
    ok = np.array([x["blend"] is not None for x in rows], dtype=bool)
    blend = np.array([x["blend"] if x["blend"] is not None else x["ens"] for x in rows], dtype=float).reshape(-1, K)
    nan = float("nan")
    return Panel(
        refs=refs,
        r=np.array([ri[str(x["ref"])] for x in rows], dtype=int),
        h=np.array([int(x["h"]) for x in rows], dtype=int),
        geo=[str(x["geo"]) for x in rows],
        target=np.array([int(x["target"]) for x in rows], dtype=int),
        ens=ens,
        blend=blend,
        blend_ok=ok,
        truth={
            "final": np.array([nan if x["truth"] is None else float(x["truth"]) for x in rows]),
            "first_release": np.array([fr.get((str(x["geo"]), int(x["target"])), nan) for x in rows]),
        },
        asof=asof,
    )


def past_scored(p: Panel, i: int) -> tuple[np.ndarray, np.ndarray]:
    """Rows of rounds before round ``i`` whose target is published in round ``i``'s vintage, with that as-of value."""
    ref = p.refs[i]
    keep: list[int] = []
    ys: list[float] = []
    for j in np.flatnonzero(p.r < i):
        v = p.asof.get((ref, p.geo[j]), {}).get(int(p.target[j]))
        if v is not None:
            keep.append(int(j))
            ys.append(v)
    return np.array(keep, dtype=int), np.array(ys, dtype=float)


def _argmin_grid(grid: Sequence[float], losses: Sequence[float]) -> float:
    """Grid value with the smallest loss (first one on ties)."""
    best = min(range(len(grid)), key=lambda k: (losses[k], k))
    return float(grid[best])


def run_candidates(p: Panel) -> tuple[dict[str, np.ndarray], list[dict[str, Any]]]:
    """Quantiles of every candidate for every row (rolling, past-only) and the chosen parameters per round/horizon."""
    out = {c: np.full((p.n, K), np.nan) for c in CANDIDATES}
    chosen: list[dict[str, Any]] = []
    for i, ref in enumerate(p.refs):
        cur = np.flatnonzero(p.r == i)
        pj, py = past_scored(p, i)
        for h in sorted({int(x) for x in p.h[cur]}):
            c = cur[p.h[cur] == h]
            sel = p.h[pj] == h
            pp, yp = pj[sel], py[sel]
            if len(pp) >= MIN_PAST:
                e, b = p.ens[pp], p.blend[pp]
                w = _argmin_grid(QA_GRID, [float(wis_rows(g * e + (1 - g) * b, yp).sum()) for g in QA_GRID])
                s = _argmin_grid(WIDTH_GRID, [float(wis_rows(stretch(e, g), yp).sum()) for g in WIDTH_GRID])
            else:
                w, s = 1.0, 1.0
            out["beat.qa_blend"][c] = w * p.ens[c] + (1 - w) * p.blend[c]
            out["beat.width_recal"][c] = stretch(p.ens[c], s)
            chosen.append({"round": ref, "horizon": h, "n_past": len(pp), "qa_weight_ensemble": w, "width": s})
    return out, chosen


def holm(pvals: Mapping[str, float | None]) -> dict[str, float | None]:
    """Holm-adjusted p-values over the whole family (missing p-values count as 1 and stay in the family size)."""
    keys = sorted(pvals, key=lambda k: (1.0 if pvals[k] is None else float(pvals[k] or 0.0), k))
    m = len(keys)
    adj: dict[str, float | None] = {}
    run = 0.0
    for rank, k in enumerate(keys):
        pv = 1.0 if pvals[k] is None else float(pvals[k] or 0.0)
        run = max(run, min(1.0, (m - rank) * pv))
        adj[k] = run
    return adj


def _cov(q: np.ndarray, y: np.ndarray, central: float) -> float:
    lv = [round(x, 6) for x in LEVELS]
    lo, hi = q[:, lv.index(round(0.5 - central / 2, 6))], q[:, lv.index(round(0.5 + central / 2, 6))]
    return float(np.mean((lo <= y) & (y <= hi)))


def compare(
    p: Panel, forecasts: Mapping[str, np.ndarray], truth: str, rounds: set[str] | None = None
) -> dict[str, Any]:
    """Every forecast vs the hub ensemble on rows with a known truth (optionally restricted to ``rounds``)."""
    y = p.truth[truth]
    mask = np.isfinite(y)
    if rounds is not None:
        mask &= np.array([p.refs[i] in rounds for i in p.r])
    idx = np.flatnonzero(mask)
    base = wis_rows(p.ens[idx], y[idx])
    res: dict[str, Any] = {
        "truth": truth,
        "n_tasks": len(idx),
        "n_rounds": len({int(p.r[j]) for j in idx}),
        "ensemble": {
            "mean_wis": float(base.mean()) if len(idx) else None,
            "coverage50": _cov(p.ens[idx], y[idx], 0.5) if len(idx) else None,
            "coverage90": _cov(p.ens[idx], y[idx], 0.9) if len(idx) else None,
        },
        "models": {},
    }
    for name, q in forecasts.items():
        if not len(idx):
            res["models"][name] = {"status": "no_tasks"}
            continue
        w = wis_rows(q[idx], y[idx])
        po: dict[int, list[float]] = defaultdict(list)
        for k, j in enumerate(idx):
            po[int(p.r[j])].append(float(w[k] - base[k]))
        dm = HQ.dm_test([float(np.mean(v)) for _, v in sorted(po.items())], DM_LAG)
        rel = float(w.sum() / base.sum()) if base.sum() > 0 else None
        res["models"][name] = {
            "mean_wis": float(w.mean()),
            "relative_wis_vs_ensemble": rel,
            "dm_vs_ensemble": {k: (v if not isinstance(v, float) or math.isfinite(v) else None) for k, v in dm.items()},
            "coverage50": _cov(q[idx], y[idx], 0.5),
            "coverage90": _cov(q[idx], y[idx], 0.9),
            "by_horizon_relative_wis": {
                str(h): float(w[p.h[idx] == h].sum() / base[p.h[idx] == h].sum())
                for h in sorted({int(x) for x in p.h[idx]})
                if base[p.h[idx] == h].sum() > 0
            },
        }
    return res


def verdicts(results: Mapping[str, Mapping[str, Any]], alpha: float) -> dict[str, Any]:
    """One Holm family over ``{test key: compare()['models'][candidate]}``; a test is a win when relative WIS < 1,
    DM mean difference < 0 and the Holm-adjusted p < alpha."""
    pv: dict[str, float | None] = {}
    for k, r in results.items():
        dm = r.get("dm_vs_ensemble") or {}
        pv[k] = dm.get("p_value") if dm.get("status") == "ok" else None
    adj = holm(pv)
    out = {}
    for k, r in results.items():
        dm = r.get("dm_vs_ensemble") or {}
        rel = r.get("relative_wis_vs_ensemble")
        win = bool(
            rel is not None and rel < 1 and dm.get("status") == "ok" and dm["mean_diff"] < 0 and (adj[k] or 1.0) < alpha
        )
        out[k] = {"relative_wis": rel, "p_value": pv[k], "holm_p": adj[k], "win": win}
    return {"family_size": len(results), "alpha": alpha, "tests": out}


__all__ = [
    "CANDIDATES",
    "Panel",
    "build_panel",
    "compare",
    "first_release",
    "holm",
    "past_scored",
    "run_candidates",
    "stretch",
    "verdicts",
    "wis_rows",
]
