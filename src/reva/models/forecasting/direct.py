"""Direct multi-horizon AR / ARDL regressions (V6, agent PRED-MODELS). Pure numerics, no I/O.

A *direct* h-step model regresses the value ``h`` periods ahead on the information at the forecast origin
(Marcellino, Stock & Watson 2006, J. Econometrics 135:499-526):

``y[t+h] = c + sum_{i<p} a_i y[t-i] + sum_j sum_{i<q} b_{j,i} x_j[t-i] + e[t+h]``

fitted by OLS on the sample the caller gives (which must hold only data published at the origin: the
evaluation layer builds it from :class:`reva.evaluation.vintages.VintageAccessor`). ``p`` (and ``q``) are chosen
by BIC on a common estimation sample. The predictive is Gaussian with the residual variance (degrees-of-freedom
corrected) inflated by the leverage of the origin regressor row (parameter uncertainty). h-step direct residuals
overlap (MA(h-1)); they are used only for the variance, never for inference here.

Inputs are equally spaced consecutive observations (the caller passes the final consecutive run); a NaN
anywhere in the used window is an abstention, never imputed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np

from reva.models.forecasting.registry_baselines import Abstain, Predictive

KEY_AR = "forecast.direct_ar"
KEY_ARDL = "forecast.direct_ardl"
VERSION = "1.0.0"
MIN_DOF = 8


def _design(y: np.ndarray, xs: Sequence[np.ndarray], h: int, p: int, q: int, start: int) -> tuple[np.ndarray, ...]:
    """Rows t = start..T-1-h (targets y[t+h]) and the origin row t = T-1."""
    T = len(y)
    rows = np.arange(start, T - h)
    cols = [np.ones(len(rows))]
    orig = [1.0]
    for i in range(p):
        cols.append(y[rows - i])
        orig.append(float(y[T - 1 - i]))
    for x in xs:
        for i in range(q):
            cols.append(x[rows - i])
            orig.append(float(x[T - 1 - i]))
    X = np.column_stack(cols)
    return X, y[rows + h], np.asarray(orig)


def _fit(X: np.ndarray, z: np.ndarray) -> tuple[np.ndarray, float, int, int]:
    coef, *_ = np.linalg.lstsq(X, z, rcond=None)
    r = z - X @ coef
    return coef, float(r @ r), X.shape[0], X.shape[1]


def direct_forecast(
    y: Sequence[float],
    h: int,
    *,
    exog: Mapping[str, Sequence[float]] | None = None,
    pmax: int = 4,
    qmax: int = 2,
    window: int | None = None,
    min_obs: int = 20,
) -> Predictive | Abstain:
    """Direct h-step forecast of ``y`` (AR when ``exog`` is empty, ARDL otherwise).

    ``exog`` series must be aligned with ``y`` (same length, same last period)."""
    if h < 1:
        raise ValueError("h must be >= 1")
    ya = np.asarray(y, dtype=float)
    xs = [np.asarray(v, dtype=float) for _, v in sorted((exog or {}).items())]
    if any(len(x) != len(ya) for x in xs):
        raise ValueError("exogenous series must be aligned with y")
    if window is not None:
        ya = ya[-window:]
        xs = [x[-window:] for x in xs]
    if len(ya) < min_obs:
        return Abstain(f"history too short for a direct regression ({len(ya)} < {min_obs} points)")
    if not np.all(np.isfinite(ya)) or any(not np.all(np.isfinite(x)) for x in xs):
        return Abstain("missing or non-finite values in the estimation window (never imputed)")
    qgrid = range(1, qmax + 1) if xs else range(0, 1)
    start = max(pmax, qmax if xs else 0) - 1
    best: tuple[float, int, int] | None = None
    for p in range(1, pmax + 1):
        for q in qgrid:
            X, z, _ = _design(ya, xs, h, p, q, start)
            n, k = X.shape
            if n - k < MIN_DOF:
                continue
            _, rss, n, k = _fit(X, z)
            if rss <= 0:
                return Abstain("degenerate direct regression (zero residuals)")
            bic = n * math.log(rss / n) + k * math.log(n)
            if best is None or bic < best[0]:
                best = (bic, p, q)
    if best is None:
        return Abstain("too few degrees of freedom for any lag order")
    _, p, q = best
    X, z, x0 = _design(ya, xs, h, p, q, max(p, q) - 1)
    coef, rss, n, k = _fit(X, z)
    if n - k < MIN_DOF:
        return Abstain("too few degrees of freedom for the selected lag order")
    s2 = rss / (n - k)
    try:
        lev = float(x0 @ np.linalg.solve(X.T @ X, x0))
    except np.linalg.LinAlgError:
        return Abstain("singular design matrix")
    point = float(x0 @ coef)
    sigma = math.sqrt(s2 * (1.0 + lev))
    if not (math.isfinite(point) and math.isfinite(sigma) and sigma > 0):
        return Abstain("non-finite direct forecast")
    return Predictive(
        "normal",
        point,
        mu=point,
        sigma=sigma,
        detail=(("h", str(h)), ("p", str(p)), ("q", str(q if xs else 0)), ("n_fit", str(n))),
    )


__all__ = ["KEY_AR", "KEY_ARDL", "VERSION", "direct_forecast"]
