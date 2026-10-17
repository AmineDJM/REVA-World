"""Numerical core of the event-study / local-projection engine (no I/O, no database, numpy only).

Everything here is deterministic: no hidden randomness (permutation placebos take an explicit seed), no
parallel reductions whose order could change a result, and exact fixed-effect partialling (one-way by group
means, two-way by alternating projections iterated to ``FE_TOL``).

Estimators
----------

* :func:`ols_fe` — OLS of ``y`` on ``X`` after partialling out fixed effects (Frisch-Waugh-Lovell), with
  classical, HC1 or cluster-robust (CR1) covariance.
* :func:`local_projection` — Jordà (2005) local projections on a country-year grid: for each horizon
  ``h = 0..H`` one regression of the horizon-``h`` outcome on the treatment onset dummy ``D[i,t]`` with ``p``
  lags of the outcome change and of the treatment, country and year fixed effects, standard errors clustered
  by country. Placebo leads (``h = -L..-2``) use the same right-hand side without lagged-outcome controls.
* :func:`event_study` — two-way fixed-effects distributed lead/lag regression
  ``y[i,t] = sum_k b_k D[i,t-k] + a_i + g_t + e`` (``k = -L..K``, ``k = -1`` omitted, endpoints binned) with a
  cluster-robust joint Wald test of the leads (pre-trend test).
* :func:`permutation_placebo` — onset years re-drawn at random inside each treated country's sample years
  (seeded); the share of placebo coefficients at least as large in absolute value as the estimate is the
  permutation p-value per horizon.
* :func:`bin_means` — event-sample regression on mutually exclusive bin dummies without constant (the
  "unconditional path" design of Jordà, Schularick & Taylor 2013, Table 4).
* :func:`hp_filter` — Hodrick-Prescott trend (exact linear solve).

These are statistical associations under stated identifying assumptions; nothing here establishes causation.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray
from scipy import stats  # type: ignore[import-untyped]

FE_TOL = 1e-11
FE_MAX_ITER = 10_000
Vcov = Literal["classical", "hc1", "cluster"]
LhsMode = Literal["level", "diff", "logdiff", "cumsum"]

F64 = NDArray[np.float64]
I64 = NDArray[np.int64]


class EstimationError(ValueError):
    """The requested estimate cannot be computed from the data (too few observations, singular design...)."""


# --------------------------------------------------------------------------- fixed effects
def _codes(g: Sequence[Any] | NDArray[Any]) -> tuple[I64, int]:
    _, inv = np.unique(np.asarray(g), return_inverse=True)
    inv = inv.astype(np.int64)
    return inv, int(inv.max()) + 1 if inv.size else 0


def _demean_once(m: F64, codes: I64, n_groups: int) -> F64:
    counts = np.bincount(codes, minlength=n_groups).astype(np.float64)
    out = m.copy()
    for j in range(m.shape[1]):
        sums = np.bincount(codes, weights=m[:, j], minlength=n_groups)
        out[:, j] = m[:, j] - (sums / counts)[codes]
    return out


def partial_out(m: F64, fes: Sequence[I64]) -> tuple[F64, int]:
    """Residuals of every column of ``m`` on the fixed effects (alternating projections; exact for one FE).
    Returns the residual matrix and the number of iterations used."""
    if not fes:
        return m.copy(), 0
    groups = [(c, int(c.max()) + 1) for c in fes]
    if len(groups) == 1:
        return _demean_once(m, *groups[0]), 1
    cur = m.copy()
    for it in range(1, FE_MAX_ITER + 1):
        prev = cur
        for c, n in groups:
            cur = _demean_once(cur, c, n)
        if float(np.max(np.abs(cur - prev), initial=0.0)) < FE_TOL * max(1.0, float(np.max(np.abs(m), initial=1.0))):
            return cur, it
    raise EstimationError("fixed-effect partialling did not converge")


def fe_dof(fes: Sequence[I64]) -> int:
    """Degrees of freedom absorbed by the fixed effects (sum of levels minus redundancies; for two FE the number
    of connected components of the bipartite group graph is subtracted)."""
    if not fes:
        return 0
    levels = [int(c.max()) + 1 for c in fes]
    if len(fes) == 1:
        return levels[0]
    if len(fes) == 2:
        a, b = fes
        parent = list(range(levels[0] + levels[1]))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i, j in zip(a.tolist(), b.tolist(), strict=True):
            ra, rb = find(i), find(levels[0] + j)
            if ra != rb:
                parent[ra] = rb
        comps = len({find(x) for x in range(len(parent))})
        return levels[0] + levels[1] - comps
    return sum(levels) - (len(levels) - 1)  # conservative for > 2 FE


# --------------------------------------------------------------------------- OLS
@dataclass(frozen=True)
class OLSResult:
    names: tuple[str, ...]
    coef: F64
    vcov: F64
    nobs: int
    n_clusters: int | None
    df_resid: int
    vcov_kind: str
    fe_iterations: int

    def se(self) -> F64:
        return np.sqrt(np.clip(np.diag(self.vcov), 0.0, None))

    def get(self, name: str) -> tuple[float, float]:
        i = self.names.index(name)
        return float(self.coef[i]), float(self.se()[i])

    def t_crit(self, level: float = 0.95) -> float:
        df = (self.n_clusters - 1) if (self.vcov_kind == "cluster" and self.n_clusters) else self.df_resid
        return float(stats.t.ppf(0.5 + level / 2.0, max(df, 1)))

    def wald(self, names: Sequence[str]) -> dict[str, float]:
        """Joint test that the named coefficients are all zero: F = W/q with (q, G-1) dof under clustering."""
        idx = [self.names.index(n) for n in names]
        b = self.coef[idx]
        v = self.vcov[np.ix_(idx, idx)]
        q = len(idx)
        try:
            w = float(b @ np.linalg.solve(v, b))
        except np.linalg.LinAlgError:
            w = float(b @ np.linalg.pinv(v) @ b)
        df2 = (self.n_clusters - 1) if (self.vcov_kind == "cluster" and self.n_clusters) else self.df_resid
        f = w / q
        return {"F": f, "df1": float(q), "df2": float(df2), "p": float(stats.f.sf(f, q, max(df2, 1)))}


def ols_fe(
    y: F64,
    x: F64,
    names: Sequence[str],
    *,
    fes: Sequence[I64] = (),
    cluster: I64 | None = None,
    vcov: Vcov = "cluster",
) -> OLSResult:
    """OLS of ``y`` on ``x`` (no constant unless a column of ``x`` is one) after partialling out ``fes``.

    Cluster-robust covariance is CR1 (Stata convention): ``G/(G-1) * (N-1)/(N-K)``, where ``K`` counts the
    regressors plus the fixed-effect levels that are *not* nested within the clusters (a country fixed effect
    nested in country clusters is not counted, as in ``reghdfe``)."""
    n = y.shape[0]
    if x.ndim != 2 or x.shape[0] != n:
        raise EstimationError("design matrix shape mismatch")
    m, iters = partial_out(np.column_stack([y, x]), fes)
    yt, xt = m[:, 0], m[:, 1:]
    k = xt.shape[1]
    keep = np.ones(k, dtype=bool)
    xtx = xt.T @ xt
    if np.linalg.matrix_rank(xtx) < k:
        raise EstimationError("design matrix is rank deficient after fixed effects")
    xtx_inv = np.linalg.inv(xtx)
    beta = xtx_inv @ (xt.T @ yt)
    resid = yt - xt @ beta
    absorbed = fe_dof(fes)
    nested = 0
    if cluster is not None and fes:
        for c in fes:
            # a FE is nested in the clusters when every FE level maps to a single cluster
            pairs = np.unique(np.column_stack([c, cluster]), axis=0)
            if pairs.shape[0] == int(c.max()) + 1:
                nested += int(c.max()) + 1
        nested = min(nested, absorbed)
    df_resid = n - k - absorbed
    if df_resid <= 0:
        raise EstimationError(f"no residual degrees of freedom (nobs={n}, k={k}, absorbed={absorbed})")
    n_clusters: int | None = None
    if vcov == "classical":
        s2 = float(resid @ resid) / df_resid
        v = s2 * xtx_inv
    elif vcov == "hc1":
        meat = (xt * resid[:, None]).T @ (xt * resid[:, None])
        v = xtx_inv @ meat @ xtx_inv * (n / df_resid)
    else:
        if cluster is None:
            raise EstimationError("cluster vcov requested without cluster ids")
        codes, g = _codes(cluster)
        n_clusters = g
        if g < 2:
            raise EstimationError("cluster-robust covariance needs at least 2 clusters")
        scores = np.zeros((g, k))
        xu = xt * resid[:, None]
        for j in range(k):
            scores[:, j] = np.bincount(codes, weights=xu[:, j], minlength=g)
        meat = scores.T @ scores
        k_total = k + absorbed - nested
        adj = (g / (g - 1)) * ((n - 1) / max(n - k_total, 1))
        v = xtx_inv @ meat @ xtx_inv * adj
    _ = keep
    return OLSResult(tuple(names), beta, v, n, n_clusters, df_resid, vcov, iters)


# --------------------------------------------------------------------------- panel grid
@dataclass(frozen=True)
class Grid:
    """A balanced country x year grid (NaN = no observation). ``treat`` counts onsets in (country, year)."""

    countries: tuple[str, ...]
    years: tuple[int, ...]
    y: F64  # shape (C, T)
    treat: F64  # shape (C, T), onset counts (0 where no onset)
    at_risk: NDArray[np.bool_]  # (C, T) True where the treatment is coded (inside the event dataset's coverage)

    @classmethod
    def build(
        cls,
        outcome: dict[tuple[str, int], float],
        onsets: dict[tuple[str, int], int],
        *,
        countries: Sequence[str] | None = None,
        years: tuple[int, int] | None = None,
        treat_years: tuple[int, int] | None = None,
        treat_countries: Sequence[str] | None = None,
    ) -> Grid:
        cs = sorted(set(countries) if countries is not None else {c for c, _ in outcome})
        ys = [y for _, y in outcome] or [0]
        lo, hi = years if years is not None else (min(ys), max(ys))
        yrs = list(range(lo, hi + 1))
        ci = {c: i for i, c in enumerate(cs)}
        y = np.full((len(cs), len(yrs)), np.nan)
        d = np.zeros((len(cs), len(yrs)))
        for (c, t), v in outcome.items():
            if c in ci and lo <= t <= hi and v is not None and math.isfinite(v):
                y[ci[c], t - lo] = v
        for (c, t), k in onsets.items():
            if c in ci and lo <= t <= hi:
                d[ci[c], t - lo] += k
        risk = np.ones_like(d, dtype=bool)
        if treat_years is not None:
            ty = np.array(yrs)
            risk &= ((ty >= treat_years[0]) & (ty <= treat_years[1]))[None, :]
        if treat_countries is not None:
            tc = set(treat_countries)
            risk &= np.array([c in tc for c in cs])[:, None]
        return cls(tuple(cs), tuple(yrs), y, d, risk)


def _shift(a: F64, k: int) -> F64:
    """``out[:, t] = a[:, t + k]`` (NaN outside)."""
    out = np.full_like(a, np.nan)
    t = a.shape[1]
    if k >= 0:
        if k < t:
            out[:, : t - k] = a[:, k:]
    else:
        if -k < t:
            out[:, -k:] = a[:, : t + k]
    return out


def lhs_matrix(y: F64, h: int, mode: LhsMode) -> F64:
    """Horizon-``h`` dependent variable on the grid.

    * ``level``: ``y[t+h]``;
    * ``diff``: ``y[t+h] - y[t-1]`` (cumulative change of a level, e.g. unemployment rate in pp);
    * ``logdiff``: ``100 * (ln y[t+h] - ln y[t-1])`` (cumulative % change of a positive level);
    * ``cumsum``: ``sum_{k=0..h} y[t+k]`` (cumulative effect of a growth rate in %, i.e. ~ % level change);
      for negative ``h`` (placebo leads) ``-sum_{k=h+1..-1} y[t+k]`` (= the pre-event level change from
      ``t+h`` to ``t-1``, sign-aligned with ``diff``), and ``0`` at ``h = -1`` (excluded)."""
    if mode == "level":
        return _shift(y, h)
    if mode == "diff":
        return _shift(y, h) - _shift(y, -1)
    if mode == "logdiff":
        with np.errstate(divide="ignore", invalid="ignore"):
            ly = np.where(y > 0, np.log(np.where(y > 0, y, 1.0)), np.nan)
        return 100.0 * (_shift(ly, h) - _shift(ly, -1))
    if mode == "cumsum":
        if h >= 0:
            acc = np.zeros_like(y)
            for k in range(h + 1):
                acc = acc + _shift(y, k)
            return acc
        acc = np.zeros_like(y)
        for k in range(h + 1, 0):
            acc = acc - _shift(y, k)
        return acc
    raise EstimationError(f"unknown lhs mode {mode}")


def change_matrix(y: F64, mode: LhsMode) -> F64:
    """The one-period change used as lagged control: first difference for level-type modes (``level``, ``diff``),
    log-difference x100 for ``logdiff``, the growth rate itself for ``cumsum``."""
    if mode == "cumsum":
        return y.copy()
    if mode == "logdiff":
        with np.errstate(divide="ignore", invalid="ignore"):
            ly = np.where(y > 0, np.log(np.where(y > 0, y, 1.0)), np.nan)
        return 100.0 * (ly - _shift(ly, -1))
    return y - _shift(y, -1)


# --------------------------------------------------------------------------- local projections
@dataclass(frozen=True)
class HorizonEstimate:
    h: int
    point: float
    se: float
    ci_low: float
    ci_high: float
    nobs: int
    n_clusters: int
    n_events: int
    t_crit: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "h": self.h,
            "point": self.point,
            "se": self.se,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "nobs": self.nobs,
            "n_clusters": self.n_clusters,
            "n_events": self.n_events,
            "t_crit": self.t_crit,
        }


@dataclass(frozen=True)
class LPSpec:
    horizons: int = 5
    leads: int = 3  # placebo leads h = -leads..-2
    lags: int = 2
    lhs: LhsMode = "cumsum"
    country_fe: bool = True
    year_fe: bool = True
    vcov: Vcov = "cluster"
    ci_level: float = 0.95
    treat_lags: bool = True
    binary_treatment: bool = True
    #: Teulings & Zubanov (2014) correction: control for onsets inside the projection window (t+1..t+h).
    future_onsets: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "future_onsets": self.future_onsets,
            "horizons": self.horizons,
            "leads": self.leads,
            "lags": self.lags,
            "lhs": self.lhs,
            "country_fe": self.country_fe,
            "year_fe": self.year_fe,
            "vcov": self.vcov,
            "ci_level": self.ci_level,
            "treat_lags": self.treat_lags,
            "binary_treatment": self.binary_treatment,
        }


@dataclass
class LPResult:
    spec: LPSpec
    irf: list[HorizonEstimate]
    leads: list[HorizonEstimate]
    sample: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec.as_dict(),
            "irf": [e.as_dict() for e in self.irf],
            "placebo_leads": [e.as_dict() for e in self.leads],
            "sample": self.sample,
        }


def _regress_horizon(grid: Grid, lhs: F64, controls: list[tuple[str, F64]], spec: LPSpec, h: int) -> HorizonEstimate:
    d = grid.treat
    if spec.binary_treatment:
        d = (d > 0).astype(np.float64)
    c_idx, t_idx = np.meshgrid(np.arange(len(grid.countries)), np.arange(len(grid.years)), indexing="ij")
    cols = [d] + [m for _, m in controls]
    ok = np.isfinite(lhs) & grid.at_risk
    for m in cols:
        ok &= np.isfinite(m)
    yv = lhs[ok]
    xv = np.column_stack([m[ok] for m in cols])
    names = ["treat"] + [n for n, _ in controls]
    fes: list[I64] = []
    cl = c_idx[ok].astype(np.int64)
    if spec.country_fe:
        fes.append(_codes(cl)[0])
    if spec.year_fe:
        fes.append(_codes(t_idx[ok])[0])
    n_events = int((d[ok] > 0).sum())
    if n_events == 0:
        raise EstimationError(f"no treated observation in the estimation sample (h={h})")
    res = ols_fe(yv, xv, names, fes=fes, cluster=cl, vcov=spec.vcov)
    b, se = res.get("treat")
    tc = res.t_crit(spec.ci_level)
    return HorizonEstimate(h, b, se, b - tc * se, b + tc * se, res.nobs, res.n_clusters or 0, n_events, tc)


def local_projection(grid: Grid, spec: LPSpec) -> LPResult:
    """Jordà local projections of the outcome on treatment onsets, horizons 0..H and placebo leads."""
    chg = change_matrix(grid.y, spec.lhs)
    d = (grid.treat > 0).astype(np.float64) if spec.binary_treatment else grid.treat
    controls: list[tuple[str, F64]] = []
    for lag in range(1, spec.lags + 1):
        controls.append((f"dy_l{lag}", _shift(chg, -lag)))
    if spec.treat_lags:
        for lag in range(1, spec.lags + 1):
            controls.append((f"treat_l{lag}", _shift(d, -lag)))
    irf = []
    for h in range(spec.horizons + 1):
        ctl = list(controls)
        if spec.future_onsets:
            ctl += [(f"treat_f{j}", np.nan_to_num(_shift(d, j), nan=0.0)) for j in range(1, h + 1)]
        irf.append(_regress_horizon(grid, lhs_matrix(grid.y, h, spec.lhs), ctl, spec, h))
    lead_controls = [(n, m) for n, m in controls if n.startswith("treat_")]
    leads = []
    for h in range(-spec.leads, -1):
        try:
            leads.append(_regress_horizon(grid, lhs_matrix(grid.y, h, spec.lhs), lead_controls, spec, h))
        except EstimationError:
            continue
    sample = {
        "countries": len(grid.countries),
        "years": [grid.years[0], grid.years[-1]] if grid.years else None,
        "onsets_on_grid": int((grid.treat > 0).sum()),
        "onsets_at_risk": int(((grid.treat > 0) & grid.at_risk).sum()),
        "outcome_cells": int(np.isfinite(grid.y).sum()),
    }
    return LPResult(spec, irf, leads, sample)


# --------------------------------------------------------------------------- event study (TWFE leads/lags)
def event_study(
    grid: Grid,
    *,
    leads: int = 3,
    lags: int = 5,
    bin_endpoints: bool = True,
    vcov: Vcov = "cluster",
    ci_level: float = 0.95,
    extra: Sequence[tuple[str, F64]] = (),
) -> dict[str, Any]:
    """Distributed lead/lag regression ``y[i,t] = sum_k b_k D[i,t-k] + a_i + g_t + e`` with ``k`` in
    ``-leads..lags`` (``k = -1`` is the reference), binned endpoints (``D`` summed beyond the window), country
    and year fixed effects, cluster-robust SE by country; joint pre-trend test of ``b_{-leads..-2} = 0``."""
    d = (grid.treat > 0).astype(np.float64)
    terms: list[tuple[str, F64]] = []
    t = d.shape[1]
    for k in range(-leads, lags + 1):
        if k == -1:
            continue
        m = _shift(d, -k)
        m = np.nan_to_num(m, nan=0.0)
        if bin_endpoints and k == -leads:
            for kk in range(-t, -leads):
                m = m + np.nan_to_num(_shift(d, -kk), nan=0.0)
        if bin_endpoints and k == lags:
            for kk in range(lags + 1, t):
                m = m + np.nan_to_num(_shift(d, -kk), nan=0.0)
        terms.append((f"k{k}", m))
    terms.extend(extra)
    c_idx, t_idx = np.meshgrid(np.arange(len(grid.countries)), np.arange(len(grid.years)), indexing="ij")
    ok = np.isfinite(grid.y) & grid.at_risk
    for _, m in terms:
        ok &= np.isfinite(m)
    names = [n for n, _ in terms]
    xv = np.column_stack([m[ok] for _, m in terms])
    cl = c_idx[ok].astype(np.int64)
    res = ols_fe(grid.y[ok], xv, names, fes=[_codes(cl)[0], _codes(t_idx[ok])[0]], cluster=cl, vcov=vcov)
    tc = res.t_crit(ci_level)
    coefs = []
    for k in range(-leads, lags + 1):
        if k == -1:
            coefs.append({"k": -1, "point": 0.0, "se": 0.0, "reference": True})
            continue
        b, se = res.get(f"k{k}")
        coefs.append({"k": k, "point": b, "se": se, "ci_low": b - tc * se, "ci_high": b + tc * se})
    pre = [f"k{k}" for k in range(-leads, -1)]
    return {
        "coefficients": coefs,
        "pretrend_test": res.wald(pre) if pre else None,
        "nobs": res.nobs,
        "n_clusters": res.n_clusters,
        "binned_endpoints": bin_endpoints,
    }


# --------------------------------------------------------------------------- placebo
def permutation_placebo(grid: Grid, spec: LPSpec, observed: LPResult, *, n_perm: int, seed: int) -> dict[str, Any]:
    """Re-draw each treated country's onset years uniformly (without replacement) among its at-risk sample years
    with an observed outcome, ``n_perm`` times (``numpy.random.default_rng(seed)``); re-estimate the LP; report
    per-horizon two-sided permutation p-values ``(1 + #{|b_perm| >= |b|}) / (1 + n_valid)``."""
    rng = np.random.default_rng(seed)
    d = (grid.treat > 0).astype(np.float64)
    treated = np.nonzero(d.sum(axis=1) > 0)[0]
    draws: list[list[float]] = [[] for _ in range(spec.horizons + 1)]
    quick = LPSpec(**{**spec.as_dict(), "leads": 2})
    failures = 0
    for _ in range(n_perm):
        nd = np.zeros_like(d)
        for ci in treated:
            k = int(d[ci].sum())
            pool = np.nonzero(grid.at_risk[ci] & np.isfinite(grid.y[ci]))[0]
            if pool.size == 0:
                continue
            nd[ci, rng.choice(pool, size=min(k, pool.size), replace=False)] = 1.0
        g2 = Grid(grid.countries, grid.years, grid.y, nd, grid.at_risk)
        try:
            r = local_projection(g2, quick)
        except EstimationError:
            failures += 1
            continue
        for e in r.irf:
            draws[e.h].append(e.point)
    out = []
    for e in observed.irf:
        arr = np.asarray(draws[e.h])
        n = arr.size
        p = (1 + int((np.abs(arr) >= abs(e.point)).sum())) / (1 + n) if n else float("nan")
        out.append(
            {
                "h": e.h,
                "observed": e.point,
                "perm_p": p,
                "perm_mean": float(arr.mean()) if n else None,
                "perm_sd": float(arr.std(ddof=1)) if n > 1 else None,
                "n_valid": n,
            }
        )
    return {"n_perm": n_perm, "seed": seed, "failures": failures, "horizons": out}


# --------------------------------------------------------------------------- event-sample bins
def bin_means(values: F64, bins: Sequence[str], *, vcov: Vcov = "classical") -> dict[str, dict[str, float]]:
    """Regression of ``values`` on mutually exclusive bin dummies without constant (= bin means), classical or
    HC1 standard errors; NaN values are dropped."""
    v = np.asarray(values, dtype=np.float64)
    b = np.asarray(bins)
    ok = np.isfinite(v)
    labels = sorted(set(b[ok].tolist()))
    x = np.column_stack([(b[ok] == lab).astype(np.float64) for lab in labels])
    res = ols_fe(v[ok], x, labels, vcov=vcov)
    out = {}
    for lab in labels:
        p, se = res.get(lab)
        out[lab] = {"point": p, "se": se, "n": int(((b == lab) & ok).sum())}
    return out


# --------------------------------------------------------------------------- HP filter
def hp_filter(y: Sequence[float], lam: float) -> F64:
    """Hodrick-Prescott trend of ``y`` (exact solve of ``(I + lam D'D) tau = y``, D = second differences)."""
    a = np.asarray(y, dtype=np.float64)
    n = a.size
    if n < 3:
        raise EstimationError("HP filter needs at least 3 observations")
    dm = np.zeros((n - 2, n))
    for i in range(n - 2):
        dm[i, i : i + 3] = (1.0, -2.0, 1.0)
    return np.asarray(np.linalg.solve(np.eye(n) + lam * dm.T @ dm, a), dtype=np.float64)
