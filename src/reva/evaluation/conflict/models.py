"""Conflict-risk forecasters (V6 PRED-CONFLICT): baselines, one KG candidate, the attributed VIEWS reference.

Pure numerics (numpy), no I/O, deterministic (no random draws). Every model sees only examples whose target year
is at or before the forecast origin (the caller builds the training set "as of" the origin).

Baselines (the registry judges them; they are not validated models):

* ``forecast.conflict_persistence`` -- no-change point (last published country-year total); predictive = the last
  value moved by the pooled historical log-changes ``ln(1+y[s+h]) - ln(1+y[s])`` of country-years in the same
  intensity bin (0, 1-24, 25-99, 100-999, 1000-9999, >= 10000); probabilities by the rule of succession over
  that sample.
* ``forecast.conflict_base_rate`` -- the country's own history up to the origin: binary = (k+1)/(n+2) of the
  family's event (for onset / escalation: among the country's past years meeting the family's condition, h years
  later); quantity = the empirical distribution of the country's past annual totals.
* ``forecast.conflict_history_logit`` -- pooled ridge logistic regression on conflict history only (fatalities in
  the last year and the last five, UCDP/PRIO armed-conflict years since 1946 as a decayed recency and a 10-year
  share, conflict / war level now); quantities by a hurdle model (logit for > 0, ridge regression of ln deaths
  for the positive part, log-normal spread).

Candidate (issued only where it beats every baseline significantly on the as-of back-test):

* ``forecast.conflict_kg_logit`` -- the history features plus REVA KG/atlas covariates: neighbours' fatalities and
  conflict level (CShapes land borders), V-Dem electoral democracy and its one-year change, V-Dem physical
  violence index, WDI GDP per capita (log), GDP growth and a < -3 % growth shock, population (log), infant
  mortality. These are **associations used for prediction**, never causal claims.

Reference: ``forecast.reference_views`` -- VIEWS country-month forecasts attributed to VIEWS, aggregated by REVA
to a calendar year (sum of the 12 monthly point predictions of one run; quantity questions only).

Missing covariates are imputed with the training mean and flagged by a missing-indicator column (model internals,
recorded in each forecast's ``detail``); missing *data* are never written as zero anywhere else.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from reva.evaluation.conflict.families import ConflictFamily
from reva.evaluation.conflict.panel import Panel

#: model_key -> (version, kind, description)
CONFLICT_MODELS: dict[str, tuple[str, str, str]] = {
    "forecast.conflict_persistence": (
        "1.0.0",
        "baseline",
        "no-change point; pooled same-intensity-bin historical log-changes as predictive",
    ),
    "forecast.conflict_base_rate": ("1.0.0", "baseline", "country's own historical frequency / distribution"),
    "forecast.conflict_history_logit": (
        "1.0.0",
        "baseline",
        "pooled ridge logit (hurdle log-normal for counts) on conflict history only",
    ),
    "forecast.conflict_kg_logit": (
        "1.0.0",
        "candidate",
        "history + KG covariates (neighbours, V-Dem, WDI); pooled ridge logit / hurdle",
    ),
    "forecast.reference_views": (
        "1.0.0",
        "external_reference",
        "VIEWS country-month point forecasts summed by REVA over a calendar year; attributed to VIEWS; point only",
    ),
}
BASELINE_KEYS = ("forecast.conflict_persistence", "forecast.conflict_base_rate", "forecast.conflict_history_logit")
CANDIDATE_KEYS = ("forecast.conflict_kg_logit",)
VIEWS_KEY = "forecast.reference_views"
HIST_FEATURES = ("d0", "d1", "dh", "inc0", "war0", "decay", "frac10")
KG_FEATURES = HIST_FEATURES + (
    "nb_ln",
    "nb_inc",
    "poly",
    "dpoly",
    "clphy",
    "lgdppc",
    "growth",
    "shock",
    "lpop",
    "imr",
)
BINS = ((0.0, 0.0), (1.0, 24.0), (25.0, 99.0), (100.0, 999.0), (1000.0, 9999.0), (10000.0, math.inf))
N_QUANTILES = 100
RIDGE = 1.0
MIN_BIN = 30
PROB_CLIP = 1e-6


@dataclass(frozen=True)
class Example:
    geo: str
    t: int  # origin year (last observed)
    last: float
    x: dict[str, float | None]
    target: float | None = None  # value h years later (training / scoring)


@dataclass(frozen=True)
class Forecast:
    prob: float | None = None
    sample: tuple[float, ...] = ()
    point: float | None = None
    abstain: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- features


def features(p: Panel, g: str, t: int) -> dict[str, float | None] | None:
    """Features known at the end of year ``t`` (``None`` when the country-year has no published outcome)."""
    y0 = p.y(g, t)
    if y0 is None:
        return None
    y1 = p.y(g, t - 1)
    hist = [v for u in range(t - 4, t + 1) if (v := p.y(g, u)) is not None]
    acd = [u for u in p.acd_years.get(g, ()) if u <= t]
    ysl = t - max(acd) if acd else None
    x: dict[str, float | None] = {
        "d0": math.log1p(y0),
        "d1": math.log1p(y1) if y1 is not None else None,
        "dh": math.log1p(sum(hist) / len(hist)),
        "inc0": 1.0 if y0 >= 25 else 0.0,
        "war0": 1.0 if y0 >= 1000 else 0.0,
        "decay": 2.0 ** (-ysl / 4.0) if ysl is not None else 0.0,
        "frac10": sum(1 for u in acd if u > t - 10) / 10.0,
    }
    nb = [v for n in p.neighbours(g, t) if (v := p.y(n, t)) is not None]
    x["nb_ln"] = math.log1p(sum(nb)) if nb else 0.0
    x["nb_inc"] = 1.0 if any(v >= 25 for v in nb) else 0.0
    poly, poly1 = p.covariate("polyarchy", g, t), p.covariate("polyarchy", g, t - 1)
    x["poly"] = poly
    x["dpoly"] = poly - poly1 if poly is not None and poly1 is not None else None
    x["clphy"] = p.covariate("clphy", g, t)
    gdp = _first(p, "gdppc", g, (t, t - 1))
    x["lgdppc"] = math.log(gdp) if gdp is not None and gdp > 0 else None
    gr = _first(p, "growth", g, (t, t - 1))
    x["growth"] = gr
    x["shock"] = None if gr is None else (1.0 if gr < -3.0 else 0.0)
    pop = _first(p, "pop", g, (t, t - 1))
    x["lpop"] = math.log(pop) if pop is not None and pop > 0 else None
    x["imr"] = _first(p, "imr", g, (t - 1, t - 2))
    return x


def _first(p: Panel, name: str, g: str, years: Sequence[int]) -> float | None:
    for yy in years:
        v = p.covariate(name, g, yy)
        if v is not None:
            return v
    return None


# --------------------------------------------------------------------------- design matrices


@dataclass(frozen=True)
class Design:
    names: tuple[str, ...]
    mean: np.ndarray
    sd: np.ndarray
    impute: dict[str, float]
    miss_cols: tuple[str, ...]

    def matrix(self, xs: Sequence[dict[str, float | None]]) -> np.ndarray:
        base = [f for f in self.names if not f.startswith("miss:")]
        out = np.empty((len(xs), len(self.names)))
        for i, x in enumerate(xs):
            row = [x.get(f) if x.get(f) is not None else self.impute[f] for f in base]
            row += [1.0 if x.get(m) is None else 0.0 for m in self.miss_cols]
            out[i] = row
        return (out - self.mean) / self.sd


def make_design(xs: Sequence[dict[str, float | None]], feats: Sequence[str]) -> Design:
    impute: dict[str, float] = {}
    miss: list[str] = []
    for f in feats:
        vals = [x[f] for x in xs if x.get(f) is not None]
        impute[f] = float(np.mean(vals)) if vals else 0.0
        if len(vals) < len(xs) and len(xs) - len(vals) >= 0.01 * len(xs):
            miss.append(f)
    names = tuple(feats) + tuple(f"miss:{m}" for m in miss)
    tmp = Design(names, np.zeros(len(names)), np.ones(len(names)), impute, tuple(miss))
    raw = tmp.matrix(xs)
    mean = raw.mean(axis=0)
    sd = raw.std(axis=0)
    sd[sd < 1e-9] = 1.0
    return Design(names, mean, sd, impute, tuple(miss))


def fit_logit(x: np.ndarray, y: np.ndarray, lam: float = RIDGE, iters: int = 60) -> np.ndarray:
    """Ridge logistic regression by Newton-Raphson (intercept unpenalised). Returns [b0, b...]."""
    n, k = x.shape
    xa = np.column_stack([np.ones(n), x])
    pen = np.eye(k + 1) * lam
    pen[0, 0] = 0.0
    b = np.zeros(k + 1)
    m = min(max(y.mean(), 1e-4), 1 - 1e-4)
    b[0] = math.log(m / (1 - m))
    for _ in range(iters):
        eta = np.clip(xa @ b, -35, 35)
        mu = 1.0 / (1.0 + np.exp(-eta))
        w = mu * (1 - mu)
        grad = xa.T @ (y - mu) - pen @ b
        hess = (xa * w[:, None]).T @ xa + pen + 1e-9 * np.eye(k + 1)
        step = np.linalg.solve(hess, grad)
        b = b + step
        if np.max(np.abs(step)) < 1e-8:
            break
    return b


def logit_prob(b: np.ndarray, x: np.ndarray) -> np.ndarray:
    eta = np.clip(b[0] + x @ b[1:], -35, 35)
    return 1.0 / (1.0 + np.exp(-eta))


def fit_ridge(x: np.ndarray, y: np.ndarray, lam: float = RIDGE) -> tuple[np.ndarray, float]:
    n, k = x.shape
    xa = np.column_stack([np.ones(n), x])
    pen = np.eye(k + 1) * lam
    pen[0, 0] = 0.0
    b = np.linalg.solve(xa.T @ xa + pen, xa.T @ y)
    resid = y - xa @ b
    dof = max(n - k - 1, 1)
    return b, float(math.sqrt(max(float(resid @ resid) / dof, 1e-6)))


def norm_ppf(u: float) -> float:
    from reva.models.forecasting.registry_baselines import _norm_ppf

    return _norm_ppf(u)


def quantile_grid(n: int = N_QUANTILES) -> list[float]:
    return [(i + 0.5) / n for i in range(n)]


def compress(sample: Sequence[float], n: int = N_QUANTILES) -> tuple[float, ...]:
    s = np.sort(np.asarray(sample, dtype=float))
    if len(s) <= n:
        return tuple(float(v) for v in s)
    return tuple(float(v) for v in np.quantile(s, quantile_grid(n)))


def prob_above(sample: Sequence[float], threshold: float) -> float:
    s = sorted(sample)
    k = len(s) - bisect.bisect_right(s, threshold)
    return (k + 1.0) / (len(s) + 2.0)


def hurdle_sample(p_pos: float, mu: float, sigma: float, n: int = N_QUANTILES) -> tuple[float, ...]:
    """Quantiles of: 0 with probability 1 - p_pos, else max(1, exp(N(mu, sigma)))."""
    p0 = 1.0 - p_pos
    out = []
    for u in quantile_grid(n):
        if u <= p0:
            out.append(0.0)
        else:
            q = min(max((u - p0) / max(p_pos, 1e-12), 1e-9), 1 - 1e-9)
            out.append(max(1.0, math.exp(mu + sigma * norm_ppf(q))))
    return tuple(sorted(out))


# --------------------------------------------------------------------------- forecasters


def _bin(v: float) -> int:
    for i, (lo, hi) in enumerate(BINS):
        if lo <= v <= hi:
            return i
    return len(BINS) - 1 if v > BINS[-1][0] else 0


def persistence(fam: ConflictFamily, train: Sequence[Example], test: Sequence[Example]) -> list[Forecast]:
    by_bin: dict[int, list[float]] = {}
    for e in train:
        assert e.target is not None
        by_bin.setdefault(_bin(e.last), []).append(math.log1p(e.target) - math.log1p(e.last))
    allr = [r for rs in by_bin.values() for r in rs]
    out = []
    for e in test:
        rs = by_bin.get(_bin(e.last), [])
        pooled = len(rs) < MIN_BIN
        if pooled:
            rs = allr
        if len(rs) < MIN_BIN:
            out.append(Forecast(abstain=f"fewer than {MIN_BIN} historical changes"))
            continue
        base = math.log1p(e.last)
        full = sorted(max(0.0, math.expm1(base + r)) for r in rs)
        det = {"n_changes": len(rs), "bin": _bin(e.last), "pooled_all_bins": pooled}
        prob = prob_above(full, fam.threshold) if fam.threshold is not None else None
        out.append(Forecast(prob=prob, sample=compress(full), point=e.last, detail=det))
    return out


def base_rate(fam: ConflictFamily, p: Panel, h: int, test: Sequence[Example]) -> list[Forecast]:
    out = []
    for e in test:
        years = sorted(u for u in p.deaths.get(e.geo, {}) if u <= e.t)
        vals = [p.deaths[e.geo][u] for u in years]
        if fam.kind == "quantity":
            if len(vals) < 5:
                out.append(Forecast(abstain="fewer than 5 published country-years"))
                continue
            s = tuple(sorted(vals))
            out.append(Forecast(sample=compress(s), point=float(np.median(s)), detail={"n_years": len(s)}))
            continue
        if fam.condition is None:
            ev = [fam.event(v) for v in vals]
        else:
            ev = [
                fam.event(p.deaths[e.geo][u + h])
                for u in years
                if u + h <= e.t and u + h in p.deaths[e.geo] and fam.applies(p.deaths[e.geo][u])
            ]
        if not ev:
            out.append(Forecast(abstain="no past country-year meets the family's condition"))
            continue
        out.append(Forecast(prob=(sum(ev) + 1.0) / (len(ev) + 2.0), detail={"n": len(ev), "k": sum(ev)}))
    return out


def logit_model(
    fam: ConflictFamily, feats: Sequence[str], train: Sequence[Example], test: Sequence[Example]
) -> list[Forecast]:
    if len(train) < 50:
        return [Forecast(abstain="fewer than 50 training country-years") for _ in test]
    design = make_design([e.x for e in train], feats)
    xtr = design.matrix([e.x for e in train])
    xte = design.matrix([e.x for e in test]) if test else np.empty((0, len(design.names)))
    tgt = np.array([e.target for e in train], dtype=float)
    det_base = {"n_train": len(train), "features": list(design.names)}
    if fam.kind == "binary":
        assert fam.threshold is not None
        yb = (tgt > fam.threshold).astype(float)
        b = fit_logit(xtr, yb)
        pr = logit_prob(b, xte)
        return [
            Forecast(prob=float(pv), detail={**det_base, "n_events": int(yb.sum()), "missing": _missing(e.x, feats)})
            for pv, e in zip(pr, test, strict=True)
        ]
    pos = (tgt >= 1.0).astype(float)
    b0 = fit_logit(xtr, pos)
    if pos.sum() < 20:
        return [Forecast(abstain="fewer than 20 positive training country-years") for _ in test]
    bp, sigma = fit_ridge(xtr[pos == 1], np.log(tgt[pos == 1]))
    pp = logit_prob(b0, xte)
    mu = bp[0] + xte @ bp[1:] if len(test) else np.empty(0)
    out = []
    for i, e in enumerate(test):
        s = hurdle_sample(float(pp[i]), float(mu[i]), sigma)
        out.append(
            Forecast(
                sample=s,
                point=float(np.median(s)),
                detail={**det_base, "p_positive": float(pp[i]), "sigma_log": sigma, "missing": _missing(e.x, feats)},
            )
        )
    return out


def _missing(x: dict[str, float | None], feats: Sequence[str]) -> list[str]:
    return [f for f in feats if x.get(f) is None]


def forecast_family(
    model_key: str, fam: ConflictFamily, p: Panel, h: int, train: Sequence[Example], test: Sequence[Example]
) -> list[Forecast]:
    """Forecasts of one model for one family and horizon; ``train`` holds examples with targets (already filtered by
    the family's condition and the origin), ``test`` the examples at the origin."""
    if model_key == "forecast.conflict_persistence":
        return persistence(fam, train, test)
    if model_key == "forecast.conflict_base_rate":
        return base_rate(fam, p, h, test)
    if model_key == "forecast.conflict_history_logit":
        return logit_model(fam, HIST_FEATURES, train, test)
    if model_key == "forecast.conflict_kg_logit":
        return logit_model(fam, KG_FEATURES, train, test)
    raise KeyError(model_key)


__all__ = [
    "BASELINE_KEYS",
    "CANDIDATE_KEYS",
    "CONFLICT_MODELS",
    "HIST_FEATURES",
    "KG_FEATURES",
    "VIEWS_KEY",
    "Example",
    "Forecast",
    "compress",
    "features",
    "fit_logit",
    "fit_ridge",
    "forecast_family",
    "hurdle_sample",
    "prob_above",
]
