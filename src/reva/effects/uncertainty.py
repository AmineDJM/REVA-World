"""Uncertainty of an effect estimate for Monte-Carlo propagation (PRED-ENGINE).

``normal_approx`` never invents uncertainty: without a reported SE, interval, draws or (for meta-analytic
summaries) a reported dispersion across estimates, ``basis`` is ``None`` and the estimate is a scenario point.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from statistics import NormalDist
from typing import Any

import numpy as np

BASES = ("se", "ci", "range", "dispersion_across_estimates", None)


def _unc(est: Mapping[str, Any]) -> dict[str, Any]:
    if "uncertainty" in est and isinstance(est["uncertainty"], Mapping):
        return dict(est["uncertainty"])
    return {k: est.get(k) for k in ("se", "ci_low", "ci_high", "ci_level", "interval_kind", "draws")} | {
        "status": est.get("uncertainty_status")
    }


def normal_approx(est: Mapping[str, Any]) -> dict[str, Any]:
    """``{"mean", "sd", "basis", "note"}`` for an estimate (JSON of :mod:`reva.effects.query` or a table row)."""
    point = est.get("point")
    mean = None if point is None else float(point)
    u = _unc(est)
    if mean is None:
        return {"mean": None, "sd": None, "basis": None, "note": "no point estimate (sign only)"}
    if u.get("se") is not None:
        return {"mean": mean, "sd": float(u["se"]), "basis": "se", "note": "sd = reported standard error"}
    lo, hi, lvl, kind = u.get("ci_low"), u.get("ci_high"), u.get("ci_level"), u.get("interval_kind")
    if lo is not None and hi is not None and kind in ("confidence", "credible") and lvl is not None:
        z = NormalDist().inv_cdf(0.5 + float(lvl) / 2)
        return {
            "mean": mean,
            "sd": (float(hi) - float(lo)) / (2 * z),
            "basis": "ci",
            "note": f"sd = interval half-width / z({float(lvl):g}); assumes a symmetric normal interval",
        }
    if lo is not None and hi is not None:
        return {
            "mean": mean,
            "sd": (float(hi) - float(lo)) / 4,
            "basis": "range",
            "note": f"heuristic_range: sd = ({kind}) width / 4; not a sampling standard error",
        }
    meta = est.get("meta") or {}
    if isinstance(meta, Mapping) and meta.get("sd_across_estimates") is not None:
        return {
            "mean": mean,
            "sd": float(meta["sd_across_estimates"]),
            "basis": "dispersion_across_estimates",
            "note": "sd = standard deviation of the published estimates summarised (heterogeneity, not sampling error)",
        }
    return {"mean": mean, "sd": None, "basis": None, "note": "no uncertainty reported: use as a scenario point"}


def sample(est: Mapping[str, Any], n: int, rng: np.random.Generator, *, store: Any = None) -> np.ndarray:
    """``n`` draws: from the archived draws (``draws.raw_sha256`` read from ``store``, one number per line or
    a ``.npy`` array) when present and a store is given, else from :func:`normal_approx`. Raises ``ValueError``
    when the estimate has no uncertainty basis."""
    u = _unc(est)
    d = u.get("draws")
    if d and store is not None:
        body = store.get("raw", d["raw_sha256"])
        try:
            arr = np.load(io.BytesIO(body), allow_pickle=False)
        except ValueError:
            arr = np.loadtxt(io.BytesIO(body), dtype=float)
        arr = np.asarray(arr, dtype=float).ravel()
        if arr.size == 0:
            raise ValueError("archived draws are empty")
        return rng.choice(arr, size=n, replace=True)
    na = normal_approx(est)
    if na["basis"] is None or na["sd"] is None:
        raise ValueError(f"estimate has no uncertainty basis: {na['note']}")
    return rng.normal(na["mean"], na["sd"], size=n)
