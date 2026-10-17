"""Forecast comparison statistics (pure): Diebold-Mariano tests on loss differentials and Holm's correction.

Panel losses are dependent within a country over time AND across countries within a year (global shocks), so the
DM variance is **two-way cluster-robust by country and by year/origin** (Cameron, Gelbach & Miller 2011:
V = V_country + V_year - V_country×year, each with its small-sample factor G/(G-1); if that is not positive the
larger one-way variance is used) when time clusters are given (FIX7, QA-PRED X13); one-way by country otherwise.
The country-only and the naive i.i.d. statistics are reported next to it.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any


def _norm_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def _cluster_var(d: Sequence[float], mean: float, keys: Sequence[Any]) -> float:
    sums: dict[Any, float] = defaultdict(float)
    for v, c in zip(d, keys, strict=True):
        sums[c] += v - mean
    g = len(sums)
    n = len(d)
    return (g / (g - 1)) * math.fsum(s * s for s in sums.values()) / (n * n) if g > 1 else 0.0


def dm_test(d: Sequence[float], clusters: Sequence[str], time_clusters: Sequence[Any] | None = None) -> dict[str, Any]:
    """DM test of mean(d) = 0 for loss differentials ``d = loss_A - loss_B`` (negative: A better).

    ``clusters``: country of each differential; ``time_clusters`` (optional): its year / origin. Returns the mean,
    the cluster-robust z statistic (two-way when ``time_clusters`` is given), the one-sided p-value for "A better"
    (mean < 0), the two-sided p-value, the country-only z, the i.i.d. z, n and the cluster counts. Fewer than 2
    clusters or a zero variance -> statistics None.
    """
    n = len(d)
    if n != len(clusters) or (time_clusters is not None and n != len(time_clusters)):
        raise ValueError("d and clusters differ in length")
    out: dict[str, Any] = {"n": n, "n_clusters": len(set(clusters))}
    if time_clusters is not None:
        out["n_time_clusters"] = len(set(time_clusters))
    if n < 2:
        return {**out, "mean_diff": None, "z": None, "p_one_sided": None, "p_two_sided": None, "z_iid": None}
    mean = math.fsum(d) / n
    var_c = _cluster_var(d, mean, clusters)
    var = var_c
    variance = "cluster_country"
    if time_clusters is not None:
        var_t = _cluster_var(d, mean, time_clusters)
        var_ct = _cluster_var(d, mean, list(zip(clusters, time_clusters, strict=True)))
        var = var_c + var_t - var_ct
        variance = "two_way_country_time"
        if var <= 0:
            var = max(var_c, var_t)
            variance = "max_one_way (two-way not positive)"
    var_iid = math.fsum((v - mean) ** 2 for v in d) / (n * (n - 1))
    z = mean / math.sqrt(var) if var > 0 else None
    z_iid = mean / math.sqrt(var_iid) if var_iid > 0 else None
    return {
        **out,
        "variance": variance,
        "mean_diff": mean,
        "z": z,
        "p_one_sided": _norm_cdf(z) if z is not None else None,
        "p_two_sided": 2.0 * (1.0 - _norm_cdf(abs(z))) if z is not None else None,
        "z_country_only": mean / math.sqrt(var_c) if var_c > 0 else None,
        "z_iid": z_iid,
    }


def holm(pvals: Mapping[str, float | None]) -> dict[str, float | None]:
    """Holm step-down adjusted p-values (family-wise error) over the non-None entries."""
    items = sorted(((k, p) for k, p in pvals.items() if p is not None), key=lambda kv: kv[1])
    m = len(items)
    adj: dict[str, float | None] = {k: None for k, p in pvals.items() if p is None}
    running = 0.0
    for i, (k, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        adj[k] = running
    return adj


__all__ = ["dm_test", "holm"]
