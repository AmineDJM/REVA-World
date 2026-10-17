"""Small Bayesian VAR with a Minnesota (Litterman) prior (V6, agent PRED-MODELS). Pure numerics, no I/O.

Model: ``Y[t] = c + sum_{l=1..p} A_l Y[t-l] + u[t]``, ``u ~ N(0, Sigma)``, n variables (here: real GDP growth,
inflation, unemployment rate of one economy).

Prior (Litterman 1986; Doan, Litterman & Sims 1984), equation by equation with the residual scale ``sigma_i``
of a univariate AR(p) fitted on the same sample:

* mean: own first lag ``delta_i`` (0 for growth rates: white-noise prior; 1 for persistent levels such as the
  unemployment rate: random-walk prior), every other coefficient 0;
* standard deviation of the coefficient on variable j at lag l in equation i:
  ``lambda1 / lag**lambda3`` if j == i, ``lambda1 * lambda2 * sigma_i / (l**lambda3 * sigma_j)`` otherwise;
  the intercept is diffuse (``100 * sigma_i``).

With a fixed diagonal residual scale in the prior the posterior of each equation is Gaussian (Theil mixed
estimation): ``V = (V0^-1 + X'X / sigma_i^2)^-1``, ``b = V (V0^-1 b0 + X'y_i / sigma_i^2)``. Predictive draws:
coefficients from the per-equation posteriors, shocks from ``N(0, Sigma_hat)`` (residual covariance at the
posterior mean), iterated ``H`` steps; draws whose companion matrix is explosive (modulus > 1) are redrawn (at
most ``max_redraw`` times, then the model abstains). Hyper-parameters are fixed in advance (``DEFAULTS``), not
tuned on the evaluation sample.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from reva.models.forecasting.registry_baselines import Abstain, Predictive

KEY = "forecast.bvar_minnesota"
VERSION = "1.0.0"
DEFAULTS = {"lambda1": 0.2, "lambda2": 0.5, "lambda3": 1.0, "p": 4}
N_DRAWS = 500


@dataclass(frozen=True)
class BvarFit:
    coef: np.ndarray  # (k, n) posterior means; row 0 = intercept, then lag 1 block, lag 2 block, ...
    post_chol: tuple[np.ndarray, ...]  # per equation Cholesky factor of the posterior covariance
    sigma: np.ndarray  # (n, n) residual covariance at the posterior mean
    p: int
    n_obs: int


def _ar_scale(y: np.ndarray, p: int) -> float:
    T = len(y)
    X = np.column_stack([np.ones(T - p)] + [y[p - lag : T - lag] for lag in range(1, p + 1)])
    coef, *_ = np.linalg.lstsq(X, y[p:], rcond=None)
    r = y[p:] - X @ coef
    return math.sqrt(float(r @ r) / max(1, len(r) - p - 1))


def fit_bvar(
    Y: np.ndarray,
    *,
    own_lag_mean: Sequence[float],
    p: int = 4,
    lambda1: float = 0.2,
    lambda2: float = 0.5,
    lambda3: float = 1.0,
) -> BvarFit | Abstain:
    Y = np.asarray(Y, dtype=float)
    if Y.ndim != 2:
        raise ValueError("Y must be (T, n)")
    T, n = Y.shape
    if len(own_lag_mean) != n:
        raise ValueError("own_lag_mean needs one value per variable")
    if not np.all(np.isfinite(Y)):
        return Abstain("missing values in the BVAR sample (never imputed)")
    k = 1 + n * p
    if T - p < max(20, n * p // 2):
        return Abstain(f"history too short for a BVAR({p}) with {n} variables ({T} periods)")
    s = np.array([_ar_scale(Y[:, j], p) for j in range(n)])
    if np.any(s <= 0) or not np.all(np.isfinite(s)):
        return Abstain("degenerate univariate scale")
    X = np.column_stack([np.ones(T - p)] + [Y[p - l : T - l, :] for lag in range(1, p + 1)])
    XtX = X.T @ X
    coef = np.zeros((k, n))
    chols = []
    for i in range(n):
        b0 = np.zeros(k)
        b0[1 + i] = float(own_lag_mean[i])
        v0 = np.empty(k)
        v0[0] = (100.0 * s[i]) ** 2
        for lag in range(1, p + 1):
            for j in range(n):
                sd = lambda1 / lag**lambda3 if j == i else lambda1 * lambda2 * s[i] / (lag**lambda3 * s[j])
                v0[1 + (lag - 1) * n + j] = sd * sd
        prec = np.diag(1.0 / v0) + XtX / s[i] ** 2
        try:
            V = np.linalg.inv(prec)
            V = 0.5 * (V + V.T)
            b = V @ (b0 / v0 + X.T @ Y[p:, i] / s[i] ** 2)
            chols.append(np.linalg.cholesky(V))
        except np.linalg.LinAlgError:
            return Abstain("singular BVAR posterior")
        coef[:, i] = b
    U = Y[p:] - X @ coef
    sigma = U.T @ U / (T - p)
    return BvarFit(coef, tuple(chols), sigma, p, T - p)


def _companion_ok(B: np.ndarray, n: int, p: int) -> bool:
    A = B[1:].T  # (n, n p)
    comp = np.zeros((n * p, n * p))
    comp[:n, :] = A
    if p > 1:
        comp[n:, :-n] = np.eye(n * (p - 1))
    return float(np.max(np.abs(np.linalg.eigvals(comp)))) < 1.0


def simulate(
    fit: BvarFit, Y: np.ndarray, horizons: int, *, n_draws: int = N_DRAWS, seed: int = 0, max_redraw: int = 20
) -> np.ndarray | Abstain:
    """Predictive draws ``(n_draws, horizons, n)`` of ``Y[T], ..., Y[T+horizons-1]``."""
    Y = np.asarray(Y, dtype=float)
    n = Y.shape[1]
    p = fit.p
    rng = np.random.default_rng(seed)
    try:
        Ls = np.linalg.cholesky(fit.sigma + 1e-12 * np.eye(n))
    except np.linalg.LinAlgError:
        return Abstain("residual covariance not positive definite")
    out = np.empty((n_draws, horizons, n))
    k = fit.coef.shape[0]
    for d in range(n_draws):
        for _ in range(max_redraw + 1):
            B = fit.coef + np.column_stack([fit.post_chol[i] @ rng.standard_normal(k) for i in range(n)])
            if _companion_ok(B, n, p):
                break
        else:
            return Abstain("explosive posterior draws (companion modulus >= 1) after redraws")
        hist = [Y[-lag] for lag in range(1, p + 1)]  # most recent first
        for s in range(horizons):
            x = np.concatenate([[1.0], *hist[:p]])
            ynew = x @ B + Ls @ rng.standard_normal(n)
            out[d, s] = ynew
            hist.insert(0, ynew)
    return out


def bvar_forecast(
    Y: np.ndarray,
    *,
    own_lag_mean: Sequence[float],
    horizons: int,
    seed: int = 0,
    n_draws: int = N_DRAWS,
    **hyper: float,
) -> list[list[Predictive]] | Abstain:
    """Per step s = 1..horizons and per variable, an empirical predictive (sorted draws; point = median)."""
    p = int(hyper.pop("p", DEFAULTS["p"]))
    hp = {k: float(hyper.get(k, DEFAULTS[k])) for k in ("lambda1", "lambda2", "lambda3")}
    fit = fit_bvar(Y, own_lag_mean=own_lag_mean, p=p, **hp)
    if isinstance(fit, Abstain):
        return fit
    draws = simulate(fit, Y, horizons, n_draws=n_draws, seed=seed)
    if isinstance(draws, Abstain):
        return draws
    out: list[list[Predictive]] = []
    for s in range(horizons):
        row = []
        for j in range(Y.shape[1]):
            smp = np.sort(draws[:, s, j])
            row.append(
                Predictive(
                    "empirical",
                    float(np.median(smp)),
                    sample=tuple(float(v) for v in smp),
                    detail=(("step", str(s + 1)), ("p", str(p)), ("n_fit", str(fit.n_obs))),
                )
            )
        out.append(row)
    return out


__all__ = ["DEFAULTS", "KEY", "VERSION", "BvarFit", "bvar_forecast", "fit_bvar", "simulate"]
