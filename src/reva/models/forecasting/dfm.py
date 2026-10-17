"""Dynamic-factor nowcast of quarterly GDP growth from monthly indicators (V6, agent PRED-MODELS). Pure numerics.

Two-step estimator in the spirit of Giannone, Reichlin & Small (2008, J. Monetary Econ. 55:665-676) and
Doz, Giannone & Reichlin (2011), reduced to one factor:

1. Each monthly indicator (already transformed to a stationary monthly change by the caller) is standardised
   with its own mean / s.d. over the months it is observed in the sample.
2. Principal component on the balanced block (months where every indicator is observed) gives a first factor;
   loadings ``lambda_i`` and idiosyncratic variances ``r_i`` by OLS of each indicator on it; the factor follows
   an AR(1) ``f[m] = phi f[m-1] + eta``.
3. A Kalman filter + RTS smoother over all months (missing cells, e.g. the ragged edge of the latest releases,
   are skipped in the update, never imputed) gives the factor for every month up to the end of the target
   quarter (months after the last observation are model forecasts of the factor).
4. Bridge equation on quarters: ``g[q] = a + b F[q] + c g[q-1] + e`` with ``F[q]`` the mean of the smoothed
   monthly factor in the quarter, OLS on the quarters with a published growth; the target quarter's predictive
   is Gaussian with the residual variance inflated by the leverage of the target row.

The caller passes only data published at the origin (as-of selection is done in the evaluation layer).
"""

from __future__ import annotations

import math

import numpy as np

from reva.models.forecasting.registry_baselines import Abstain, Predictive

KEY = "forecast.dfm_nowcast"
VERSION = "1.0.0"
MIN_BALANCED = 36
MIN_QUARTERS = 16


def _pca_factor(Z: np.ndarray) -> np.ndarray:
    C = np.cov(Z, rowvar=False)
    w, V = np.linalg.eigh(np.atleast_2d(C))
    v = V[:, -1]
    if v.sum() < 0:
        v = -v
    return np.asarray(Z @ v / math.sqrt(max(float(w[-1]), 1e-12)))


def smoothed_factor(X: np.ndarray) -> np.ndarray | Abstain:
    """Monthly factor (length M) from ``X`` (M x k, NaN = not published)."""
    X = np.asarray(X, dtype=float)
    M, k = X.shape
    mu = np.nanmean(X, axis=0)
    sd = np.nanstd(X, axis=0)
    if np.any(~np.isfinite(sd)) or np.any(sd <= 0):
        return Abstain("an indicator has no variance in the sample")
    Z = (X - mu) / sd
    bal = np.all(np.isfinite(Z), axis=1)
    if bal.sum() < MIN_BALANCED:
        return Abstain(f"fewer than {MIN_BALANCED} months with every indicator observed")
    f0 = np.full(M, np.nan)
    f0[bal] = _pca_factor(Z[bal])
    lam = np.empty(k)
    r = np.empty(k)
    for i in range(k):
        ok = bal & np.isfinite(Z[:, i])
        lam[i] = float(f0[ok] @ Z[ok, i] / (f0[ok] @ f0[ok]))
        e = Z[ok, i] - lam[i] * f0[ok]
        r[i] = max(float(e @ e) / ok.sum(), 1e-3)
    fb = f0[bal]
    idx = np.flatnonzero(bal)
    consec = np.flatnonzero(np.diff(idx) == 1)
    if len(consec) < 12:
        return Abstain("too few consecutive balanced months for the factor dynamics")
    a, b = fb[consec], fb[consec + 1]
    phi = float(np.clip(a @ b / (a @ a), -0.98, 0.98))
    q = max(float(np.var(b - phi * a)), 1e-3)
    # Kalman filter (scalar state)
    fp = np.empty(M)
    pp = np.empty(M)
    ff = np.empty(M)
    pf = np.empty(M)
    f_prev, p_prev = 0.0, q / (1 - phi * phi)
    for m in range(M):
        fpred = phi * f_prev
        ppred = phi * phi * p_prev + q
        fp[m], pp[m] = fpred, ppred
        obs = np.isfinite(Z[m])
        if obs.any():
            L = lam[obs]
            Rinv = 1.0 / r[obs]
            prec = 1.0 / ppred + float(L * Rinv @ L)
            pnew = 1.0 / prec
            fnew = pnew * (fpred / ppred + float((L * Rinv) @ Z[m, obs]))
        else:
            fnew, pnew = fpred, ppred
        ff[m], pf[m] = fnew, pnew
        f_prev, p_prev = fnew, pnew
    fs = ff.copy()
    for m in range(M - 2, -1, -1):
        J = pf[m] * phi / pp[m + 1]
        fs[m] = ff[m] + J * (fs[m + 1] - fp[m + 1])
    return fs


def dfm_nowcast(
    X: np.ndarray,
    month_quarter: np.ndarray,
    g: np.ndarray,
    target_q: int,
) -> Predictive | Abstain:
    """Nowcast of ``g[target_q]``.

    * ``X``: (M, k) monthly indicator changes, NaN where not published at the origin; its last row must be the
      last month of the target quarter (rows after the ragged edge are all NaN);
    * ``month_quarter``: (M,) quarter index (0..Q-1) of each month row;
    * ``g``: (Q,) quarterly growth published at the origin (NaN when not published, at least the target);
    * ``target_q``: index of the target quarter in ``g``.
    """
    fs = smoothed_factor(X)
    if isinstance(fs, Abstain):
        return fs
    Q = len(g)
    F = np.full(Q, np.nan)
    for qi in range(Q):
        sel = month_quarter == qi
        if sel.sum() == 3:
            F[qi] = float(fs[sel].mean())
    rows = [qi for qi in range(1, Q) if qi != target_q and np.isfinite(g[qi]) and np.isfinite(g[qi - 1]) and np.isfinite(F[qi])]
    if len(rows) < MIN_QUARTERS:
        return Abstain(f"fewer than {MIN_QUARTERS} quarters for the bridge equation")
    Xb = np.column_stack([np.ones(len(rows)), F[rows], g[[r - 1 for r in rows]]])
    yb = g[rows]
    coef, *_ = np.linalg.lstsq(Xb, yb, rcond=None)
    res = yb - Xb @ coef
    n, kk = Xb.shape
    if n - kk < 8:
        return Abstain("too few degrees of freedom for the bridge equation")
    s2 = float(res @ res) / (n - kk)
    prev = g[target_q - 1]
    if not np.isfinite(prev) or not np.isfinite(F[target_q]):
        return Abstain("previous quarter not published or target quarter months missing")
    x0 = np.array([1.0, F[target_q], prev])
    lev = float(x0 @ np.linalg.solve(Xb.T @ Xb, x0))
    point = float(x0 @ coef)
    sigma = math.sqrt(s2 * (1 + lev))
    if not (math.isfinite(point) and sigma > 0):
        return Abstain("non-finite nowcast")
    return Predictive(
        "normal", point, mu=point, sigma=sigma, detail=(("n_bridge", str(n)), ("b_factor", f"{coef[1]:.4g}"))
    )


__all__ = ["KEY", "VERSION", "dfm_nowcast", "smoothed_factor"]
