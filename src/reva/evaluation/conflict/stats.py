"""Forecast comparison statistics (pure): Diebold-Mariano tests on loss differentials and Holm's correction.

Panel losses are dependent within a country over time, so the DM variance is **cluster-robust by country**
(sum of within-country deviations, small-sample factor G/(G-1)); the naive i.i.d. statistic is reported next to
it. Cross-country dependence within a year (global shocks) is not modelled -- stated with every result.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any


def _norm_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def dm_test(d: Sequence[float], clusters: Sequence[str]) -> dict[str, Any]:
    """DM test of mean(d) = 0 for loss differentials ``d = loss_A - loss_B`` (negative: A better).

    Returns the mean, the cluster-robust z statistic, the one-sided p-value for "A better" (mean < 0), the two-sided
    p-value, the i.i.d. z, n and the number of clusters. Fewer than 2 clusters or a zero variance -> statistics None.
    """
    n = len(d)
    if n != len(clusters):
        raise ValueError("d and clusters differ in length")
    out: dict[str, Any] = {"n": n, "n_clusters": len(set(clusters))}
    if n < 2:
        return {**out, "mean_diff": None, "z": None, "p_one_sided": None, "p_two_sided": None, "z_iid": None}
    mean = math.fsum(d) / n
    sums: dict[str, float] = defaultdict(float)
    for v, c in zip(d, clusters, strict=True):
        sums[c] += v - mean
    g = len(sums)
    var_cl = (g / (g - 1)) * math.fsum(s * s for s in sums.values()) / (n * n) if g > 1 else 0.0
    var_iid = math.fsum((v - mean) ** 2 for v in d) / (n * (n - 1))
    z = mean / math.sqrt(var_cl) if var_cl > 0 else None
    z_iid = mean / math.sqrt(var_iid) if var_iid > 0 else None
    return {
        **out,
        "mean_diff": mean,
        "z": z,
        "p_one_sided": _norm_cdf(z) if z is not None else None,
        "p_two_sided": 2.0 * (1.0 - _norm_cdf(abs(z))) if z is not None else None,
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
