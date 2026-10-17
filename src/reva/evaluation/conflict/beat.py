"""V7 BEAT-CONFLICT: country-month conflict-RISK estimates vs persistence and VIEWS (pure: no database).

Pre-registration: ``registry/forecasts/beat_conflict/protocol.yaml`` (binding; committed before any evaluation of
its windows). Everything here is an aggregate country-month risk estimate built from UCDP counts: never a
prediction of a specific attack, actor or person. Associations used for prediction are not causal claims.

Data model (:class:`CMData`): month-indexed arrays (``i = 12 * year + month - 1``) per GW state of the UCDP
country-year universe. GED / Candidate aggregates write a cell only where an event is recorded; within the covered
months an absent cell of a universe state is read as **0 recorded** (the FIX6 rule
:data:`reva.evaluation.registry.absence.ABSENCE_RULE_TEXT`; the zero exists only in this computation). Months
outside the coverage or states outside the universe are NaN (missing), never 0.

Forecasters (origin month T = last month of data; step s = target month T + s):

* ``forecast.cm_hurdle`` (REVA candidate): three ridge models per step, refitted every December F (data through F,
  training pairs (t, t+s) with F-120 <= t and t+s <= F): logistic P(deaths >= 25), logistic P(deaths >= 1), ridge
  on log1p(deaths) given >= 1 (homoscedastic normal). Features (:data:`FEATURES`): GED dynamics (state-based,
  non-state, one-sided deaths; events), months since last >= 25 / >= 1, trend, spatial spread (distinct ADM1 and
  PRIO-GRID cells with events), neighbours via CShapes land borders, monthly CPI and exchange-rate shocks (IMF,
  2-month publication lag). Origin T uses the fit of the latest December <= T.
* ``forecast.cm_hurdle_ged`` (informational ablation): same without the IMF features.
* ``forecast.conflict_persistence`` (benchmark): point = deaths in T; P(>= 25) = (k+1)/(n+2) over past
  transitions from the same intensity bin (as V6); fair density = log1p(y_T) + past log1p changes over s months from
  the same bin (its own past errors), floored at 0.
* ``forecast.conflict_base_rate`` (informational): last-12-month mean; country frequency of >= 25 over 120 months.
* ``forecast.reference_views`` (benchmark, attributed): VIEWS ``main_mean`` / ``main_dich`` of a run.
* ``forecast.cm_stack_views``: linear pool w * VIEWS + (1 - w) * cm_hurdle for P(>= 25) and on the log1p scale for
  the point; w chosen on a 0.05 grid from past pairs only (scored outcomes before the stack's origin).

Scores: Brier and log loss (floor 5e-5) for P(>= 25); CRPS on the log1p(deaths) scale for densities vs densities;
MSLE for points vs points (never a point vs a density). DM: per (country, origin) the mean differential over the
group's steps, variance two-way cluster-robust (country, origin year), p from Student t with min(G) - 1 df.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy import stats as sps  # type: ignore[import-untyped]

from reva.evaluation.conflict.models import RIDGE, fit_logit, fit_ridge, logit_prob
from reva.evaluation.conflict.stats import dm_test, holm

THRESHOLD = 25.0
WINDOW = 120
MAX_STEP = 12
N_Q = 100
PROB_FLOOR = 5e-5
BINS = ((0.0, 0.0), (1.0, 24.0), (25.0, 99.0), (100.0, 999.0), (1000.0, math.inf))
CAND = "forecast.cm_hurdle"
CAND_GED = "forecast.cm_hurdle_ged"
PERS = "forecast.conflict_persistence"
BASE = "forecast.conflict_base_rate"
VIEWS = "forecast.reference_views"
STACK = "forecast.cm_stack_views"
GED_FEATURES = (
    "sb1",
    "sb3",
    "sb12",
    "max12",
    "trend",
    "since25",
    "since1",
    "ns3",
    "os3",
    "ev3",
    "adm3",
    "pg3",
    "nb3",
    "nb25",
)
ECON_FEATURES = ("cpi_yoy", "fx3")
FEATURES = GED_FEATURES + ECON_FEATURES
METRICS = ("brier", "logloss", "crps_log1p", "msle")
W_GRID = tuple(round(0.05 * k, 2) for k in range(21))


def midx(y: int, m: int) -> int:
    return y * 12 + (m - 1)


def mlabel(i: int) -> str:
    return f"{i // 12}-{i % 12 + 1:02d}"


# --------------------------------------------------------------------------- data


@dataclass
class CMData:
    """Month-indexed panel; arrays are (n_geos, n_months) from month ``i0``; NaN = missing (outside coverage or
    outside the universe)."""

    geos: list[str]
    i0: int
    sb: np.ndarray
    ns: np.ndarray
    os: np.ndarray
    ev: np.ndarray
    adm3: np.ndarray  # distinct ADM1 units with events in months T-2..T
    pg3: np.ndarray  # distinct PRIO-GRID cells with events in months T-2..T
    cpi: np.ndarray  # CPI index (NaN where not published)
    fx: np.ndarray  # national currency per USD, period average
    neighbours: Callable[[str, int], set[str]]
    covered: tuple[int, int]
    notes: list[str] = field(default_factory=list)

    @property
    def n_months(self) -> int:
        return int(self.sb.shape[1])

    def col(self, i: int) -> int:
        return i - self.i0

    def y(self, gi: int, i: int) -> float:
        c = i - self.i0
        if c < 0 or c >= self.n_months:
            return math.nan
        return float(self.sb[gi, c])


def build_cmdata(
    *,
    cells: Mapping[str, Mapping[tuple[str, int], float]],
    spread_sets: Mapping[str, Mapping[tuple[str, int], set[str]]],
    econ: Mapping[str, Mapping[tuple[str, int], float]],
    universe: Mapping[int, set[str]],
    covered: tuple[int, int],
    neighbours: Callable[[str, int], set[str]],
    i0: int,
) -> CMData:
    """``cells[name][(gw, month)]`` for name in sb/ns/os/ev (absent within coverage & universe = 0 recorded);
    ``spread_sets['adm1'|'pg'][(gw, month)]`` = set of unit ids with events; ``econ['cpi'|'fx'][(gw, month)]``.
    ``universe[year]`` = GW codes (years beyond the last one reuse the last)."""
    last_year = max(universe)
    geos = sorted(set().union(*universe.values()))
    gpos = {g: k for k, g in enumerate(geos)}
    n = covered[1] - i0 + 1
    mask = np.zeros((len(geos), n), dtype=bool)
    for c in range(n):
        i = i0 + c
        if i < covered[0]:
            continue
        for g in universe.get(min(i // 12, last_year), set()):
            mask[gpos[g], c] = True

    def arr(name: str) -> np.ndarray:
        a = np.zeros((len(geos), n))
        for (g, i), v in cells.get(name, {}).items():
            if g in gpos and i0 <= i <= covered[1]:
                a[gpos[g], i - i0] = v
        a[~mask] = np.nan
        return a

    def spread(name: str) -> np.ndarray:
        a = np.full((len(geos), n), np.nan)
        sets = spread_sets.get(name, {})
        by_g: dict[str, dict[int, set[str]]] = defaultdict(dict)
        for (g, i), s in sets.items():
            by_g[g][i] = s
        for g, k in gpos.items():
            d = by_g.get(g, {})
            for c in range(n):
                if not mask[k, c]:
                    continue
                i = i0 + c
                u: set[str] = set()
                for j in (i - 2, i - 1, i):
                    if covered[0] <= j:
                        u |= d.get(j, set())
                a[k, c] = len(u)
        return a

    def econ_arr(name: str) -> np.ndarray:
        a = np.full((len(geos), n), np.nan)
        for (g, i), v in econ.get(name, {}).items():
            if g in gpos and i0 <= i <= covered[1] and v is not None and v > 0:
                a[gpos[g], i - i0] = v
        return a

    return CMData(
        geos=geos,
        i0=i0,
        sb=arr("sb"),
        ns=arr("ns"),
        os=arr("os"),
        ev=arr("ev"),
        adm3=spread("adm1"),
        pg3=spread("pg"),
        cpi=econ_arr("cpi"),
        fx=econ_arr("fx"),
        neighbours=neighbours,
        covered=covered,
    )


# --------------------------------------------------------------------------- features


def _nanmean(a: np.ndarray) -> np.ndarray:
    with np.errstate(all="ignore"):
        cnt = np.sum(~np.isnan(a), axis=1)
        s = np.nansum(a, axis=1)
        out: np.ndarray = np.where(cnt > 0, s / np.maximum(cnt, 1), np.nan)
    return out


def features_at(d: CMData, t: int) -> np.ndarray:
    """(n_geos, len(FEATURES)) features using months <= t only; rows of states outside the universe at t are NaN."""
    c = d.col(t)
    g_n = len(d.geos)
    out = np.full((g_n, len(FEATURES)), np.nan)
    if c < 0 or c >= d.n_months:
        return out
    valid = ~np.isnan(d.sb[:, c])

    def w(a: np.ndarray, lo: int, hi: int) -> np.ndarray:  # months t-lo .. t-hi (inclusive, lo >= hi)
        a0, a1 = max(c - lo, 0), c - hi + 1
        if a1 <= 0:
            return np.full((g_n, 1), np.nan)
        return a[:, a0:a1]

    sb12 = w(d.sb, 11, 0)
    out[:, 0] = np.log1p(d.sb[:, c])
    out[:, 1] = np.log1p(_nanmean(w(d.sb, 2, 0)))
    out[:, 2] = np.log1p(_nanmean(sb12))
    with np.errstate(all="ignore"):
        out[:, 3] = np.log1p(np.nanmax(np.where(np.isnan(sb12), -np.inf, sb12), axis=1))
    prev = np.log1p(_nanmean(w(d.sb, 5, 3)))
    out[:, 4] = np.where(np.isnan(prev), 0.0, out[:, 1] - prev)
    hist = w(d.sb, WINDOW - 1, 0)  # oldest .. newest
    rev = hist[:, ::-1]
    for k, thr in ((5, THRESHOLD), (6, 1.0)):
        hit = np.nan_to_num(rev, nan=-1.0) >= thr
        first = np.where(hit.any(axis=1), hit.argmax(axis=1), WINDOW)
        out[:, k] = np.log1p(first)
    out[:, 7] = np.log1p(_nanmean(w(d.ns, 2, 0)))
    out[:, 8] = np.log1p(_nanmean(w(d.os, 2, 0)))
    out[:, 9] = np.log1p(np.nansum(w(d.ev, 2, 0), axis=1))
    out[:, 10] = np.log1p(d.adm3[:, c])
    out[:, 11] = np.log1p(d.pg3[:, c])
    s3 = np.nansum(w(d.sb, 2, 0), axis=1)
    m3 = np.nanmax(np.nan_to_num(w(d.sb, 2, 0), nan=0.0), axis=1)
    gpos = {g: k for k, g in enumerate(d.geos)}
    year = t // 12
    for k, g in enumerate(d.geos):
        nb = [gpos[x] for x in d.neighbours(g, year) if x in gpos]
        out[k, 12] = math.log1p(float(np.sum(s3[nb]))) if nb else 0.0
        out[k, 13] = float(np.sum(m3[nb] >= THRESHOLD)) if nb else 0.0
    # IMF shocks with a 2-month publication lag (values of month t-2 are the latest used at origin t)
    with np.errstate(all="ignore"):
        if c - 14 >= 0:
            out[:, 14] = np.clip(np.log(d.cpi[:, c - 2] / d.cpi[:, c - 14]), -2.0, 2.0)
        if c - 5 >= 0:
            out[:, 15] = np.clip(np.log(d.fx[:, c - 2] / d.fx[:, c - 5]), -1.0, 1.0)
    out[~valid, :] = np.nan
    return out


class FeatureCache:
    def __init__(self, d: CMData) -> None:
        self.d = d
        self._c: dict[int, np.ndarray] = {}

    def __call__(self, t: int) -> np.ndarray:
        if t not in self._c:
            self._c[t] = features_at(self.d, t)
        return self._c[t]


# --------------------------------------------------------------------------- hurdle model


@dataclass
class StepFit:
    feats: tuple[int, ...]
    mean: np.ndarray
    sd: np.ndarray
    impute: np.ndarray
    miss_cols: tuple[int, ...]
    b25: np.ndarray
    b1: np.ndarray
    bpos: np.ndarray
    sigma: float
    n: int

    def design(self, x: np.ndarray) -> np.ndarray:
        xs = x[:, self.feats]
        miss = np.isnan(xs)
        filled = np.where(miss, self.impute[None, :], xs)
        cols = [filled] + [miss[:, [j]].astype(float) for j in self.miss_cols]
        z: np.ndarray = (np.hstack(cols) - self.mean) / self.sd
        return z


def fit_step(fc: FeatureCache, fit_month: int, s: int, feats: Sequence[int]) -> StepFit | None:
    d = fc.d
    xs, ys = [], []
    for t in range(fit_month - WINDOW, fit_month - s + 1):
        if t < d.covered[0] or d.col(t) < 0:
            continue
        x = fc(t)
        yv = d.sb[:, d.col(t + s)]
        ok = ~np.isnan(x[:, 0]) & ~np.isnan(yv)
        if ok.any():
            xs.append(x[ok])
            ys.append(yv[ok])
    if not xs:
        return None
    x = np.vstack(xs)[:, list(feats)]
    y = np.concatenate(ys)
    miss = np.isnan(x)
    impute = np.array([np.nanmean(x[:, j]) if (~miss[:, j]).any() else 0.0 for j in range(x.shape[1])])
    miss_cols = tuple(j for j in range(x.shape[1]) if miss[:, j].mean() >= 0.01)
    proto = StepFit(tuple(feats), np.zeros(1), np.ones(1), impute, miss_cols, *([np.zeros(1)] * 3), 1.0, len(y))
    raw = np.hstack([np.where(miss, impute[None, :], x)] + [miss[:, [j]].astype(float) for j in miss_cols])
    mean, sd = raw.mean(axis=0), raw.std(axis=0)
    sd[sd < 1e-9] = 1.0
    z = (raw - mean) / sd
    proto.mean, proto.sd = mean, sd
    b25 = fit_logit(z, (y >= THRESHOLD).astype(float), RIDGE)
    b1 = fit_logit(z, (y >= 1.0).astype(float), RIDGE)
    pos = y >= 1.0
    if pos.sum() > z.shape[1] + 2:
        bpos, sigma = fit_ridge(z[pos], np.log1p(y[pos]), RIDGE)
    else:
        bpos, sigma = np.zeros(z.shape[1] + 1), 1.0
        bpos[0] = float(np.log1p(y[pos]).mean()) if pos.any() else 0.0
    proto.b25, proto.b1, proto.bpos, proto.sigma = b25, b1, bpos, sigma
    return proto


_GRID = (np.arange(N_Q) + 0.5) / N_Q
_ZQ = sps.norm.ppf(_GRID)


def hurdle_quantiles(p1: float, mu: float, sigma: float) -> np.ndarray:
    """N_Q quantiles on the log1p scale of: 0 w.p. 1 - p1, else max(log 2, N(mu, sigma))."""
    p0 = 1.0 - p1
    q = np.clip((_GRID - p0) / max(p1, 1e-12), 1e-9, 1 - 1e-9)
    v = np.maximum(math.log(2.0), mu + sigma * sps.norm.ppf(q))
    out: np.ndarray = np.where(_GRID <= p0, 0.0, v)
    return np.sort(out)


@dataclass
class Fc:
    point_l: float  # point on the log1p scale (for MSLE)
    prob: float  # P(>= 25)
    q: np.ndarray | None  # quantiles on the log1p scale (None: point-only)


def hurdle_forecasts(fit: StepFit, x: np.ndarray) -> list[Fc]:
    z = fit.design(x)
    p25 = logit_prob(fit.b25, z)
    p1 = logit_prob(fit.b1, z)
    mu = fit.bpos[0] + z @ fit.bpos[1:]
    out = []
    for k in range(len(x)):
        q = hurdle_quantiles(float(p1[k]), float(mu[k]), fit.sigma)
        out.append(Fc(float(q.mean()), float(p25[k]), q))
    return out


# --------------------------------------------------------------------------- benchmarks


def _bin_of(v: np.ndarray) -> np.ndarray:
    b = np.zeros(v.shape, dtype=int)
    for k, (lo, _hi) in enumerate(BINS):
        b[v >= lo] = k
    return b


def persistence_tables(d: CMData, origin: int, s: int) -> dict[int, tuple[int, int, np.ndarray]]:
    """bin -> (k >= 25, n, log1p changes) over transitions t -> t+s with origin-WINDOW <= t, t+s <= origin."""
    ks: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    errs: dict[int, list[np.ndarray]] = defaultdict(list)
    for t in range(origin - WINDOW, origin - s + 1):
        if t < d.covered[0] or d.col(t) < 0:
            continue
        a, b = d.sb[:, d.col(t)], d.sb[:, d.col(t + s)]
        ok = ~np.isnan(a) & ~np.isnan(b)
        a, b = a[ok], b[ok]
        bins = _bin_of(a)
        for k in np.unique(bins):
            m = bins == k
            ks[int(k)][0] += int(np.sum(b[m] >= THRESHOLD))
            ks[int(k)][1] += int(m.sum())
            errs[int(k)].append(np.log1p(b[m]) - np.log1p(a[m]))
    out = {}
    for k, (kk, nn) in ks.items():
        e = np.sort(np.concatenate(errs[k])) if errs[k] else np.zeros(1)
        if len(e) > N_Q:
            e = np.quantile(e, _GRID)
        out[k] = (kk, nn, e)
    return out


def persistence_fc(tab: Mapping[int, tuple[int, int, np.ndarray]], y0: float) -> Fc:
    k = int(_bin_of(np.array([y0]))[0])
    kk, nn, e = tab.get(k, (0, 0, np.zeros(1)))
    l0 = math.log1p(y0)
    return Fc(l0, (kk + 1.0) / (nn + 2.0), np.sort(np.maximum(0.0, l0 + e)))


def base_rate_fc(d: CMData, gi: int, origin: int) -> Fc | None:
    c = d.col(origin)
    h = d.sb[gi, max(c - WINDOW + 1, 0) : c + 1]
    h = h[~np.isnan(h)]
    l12 = d.sb[gi, max(c - 11, 0) : c + 1]
    l12 = l12[~np.isnan(l12)]
    if len(h) == 0 or len(l12) == 0:
        return None
    k = int(np.sum(h >= THRESHOLD))
    q = np.sort(np.log1p(h))
    if len(q) > N_Q:
        q = np.quantile(q, _GRID)
    return Fc(math.log1p(float(l12.mean())), (k + 1.0) / (len(h) + 2.0), q)


# --------------------------------------------------------------------------- scores


def crps_sample(q: np.ndarray, y: float) -> float:
    s = np.sort(q)
    n = len(s)
    i = np.arange(1, n + 1)
    return float(np.mean(np.abs(s - y)) - np.sum((2 * i - n - 1) * s) / (n * n))


def losses(fc: Fc, y: float) -> dict[str, float | None]:
    o = 1.0 if y >= THRESHOLD else 0.0
    pc = min(max(fc.prob, PROB_FLOOR), 1 - PROB_FLOOR)
    ly = math.log1p(y)
    return {
        "brier": (fc.prob - o) ** 2,
        "logloss": -math.log(pc if o else 1 - pc),
        "crps_log1p": crps_sample(fc.q, ly) if fc.q is not None else None,
        "msle": (fc.point_l - ly) ** 2,
    }


def dm_t(d: Sequence[float], countries: Sequence[str], times: Sequence[Any]) -> dict[str, Any]:
    """Two-way cluster-robust DM (stats.dm_test) with the p-value from Student t, df = min(G_country, G_time) - 1."""
    r = dm_test(d, countries, times)
    g = min(r.get("n_clusters", 0), r.get("n_time_clusters", 0))
    r["df"] = g - 1
    if r.get("z") is None or g < 2:
        r["p_one_sided_t"] = None
        return r
    r["p_one_sided_t"] = float(sps.t.cdf(r["z"], g - 1))
    return r


def paired_test(
    rows: Mapping[tuple[str, int, int], Mapping[str, Mapping[str, float | None]]],
    a: str,
    b: str,
    metric: str,
    steps: tuple[int, int],
    time_key: Callable[[int, int], Any],
) -> dict[str, Any]:
    """rows[(gw, origin, step)][model][metric]; per (gw, origin) the mean differential a - b over the steps in
    ``steps`` present for both; clusters gw and time_key(origin, step-group)."""
    acc: dict[tuple[str, int], list[float]] = defaultdict(list)
    la: list[float] = []
    lb: list[float] = []
    for (g, o, s), m in rows.items():
        if not (steps[0] <= s <= steps[1]) or a not in m or b not in m:
            continue
        va, vb = m[a].get(metric), m[b].get(metric)
        if va is None or vb is None:
            continue
        acc[(g, o)].append(va - vb)
        la.append(va)
        lb.append(vb)
    keys = sorted(acc)
    d = [math.fsum(acc[k]) / len(acc[k]) for k in keys]
    r = dm_t(d, [k[0] for k in keys], [time_key(k[1], steps[0]) for k in keys])
    r.update(
        {
            "a": a,
            "b": b,
            "metric": metric,
            "steps": f"{steps[0]}-{steps[1]}",
            "n_forecasts": len(la),
            "mean_a": math.fsum(la) / len(la) if la else None,
            "mean_b": math.fsum(lb) / len(lb) if lb else None,
            "ratio": (math.fsum(la) / math.fsum(lb)) if lb and math.fsum(lb) > 0 else None,
        }
    )
    return r


def family_holm(tests: Mapping[str, Mapping[str, Any]], declared: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Holm over every declared test id; a declared test without a p-value (not evaluable or pending) counts with
    p = 1. Win = lower mean loss and Holm-adjusted one-sided p < 0.05."""
    pv: dict[str, float | None] = {}
    for k in declared:
        t = tests.get(k)
        p = t.get("p_one_sided_t") if t else None
        pv[k] = 1.0 if p is None else float(p)
    adj = holm(pv)
    out = {}
    for k in declared:
        t = dict(tests.get(k) or {"status": "pending_or_not_evaluable"})
        t["p_holm"] = adj[k]
        md = t.get("mean_diff")
        t["win"] = bool(md is not None and md < 0 and adj[k] is not None and adj[k] < 0.05)
        out[k] = t
    return out


# --------------------------------------------------------------------------- runners


def fit_month_for(origin: int) -> int:
    """Latest December <= origin."""
    return origin if origin % 12 == 11 else (origin // 12) * 12 - 1


def run_origins(
    d: CMData,
    origins: Sequence[int],
    steps: Sequence[int],
    *,
    outcome: Callable[[int, int], float] | None = None,
    extra: Callable[[str, int, int], Fc | None] | None = None,
    fc_cache: FeatureCache | None = None,
    log: Callable[[str], None] | None = None,
    keep_records: bool = False,
) -> tuple[dict[tuple[str, int, int], dict[str, dict[str, float | None]]], list[dict[str, Any]]]:
    """Score (or just produce) every forecaster at each origin. ``outcome(gi, target)`` (NaN = not scored; default:
    ``d``'s own value). ``extra(gw, origin, step)`` adds a benchmark (VIEWS). Returns score rows and forecast
    records (forecast records always, scored or not)."""
    fc = fc_cache or FeatureCache(d)
    feats_all = tuple(range(len(FEATURES)))
    feats_ged = tuple(range(len(GED_FEATURES)))
    fits: dict[tuple[int, int, str], StepFit | None] = {}
    rows: dict[tuple[str, int, int], dict[str, dict[str, float | None]]] = {}
    recs: list[dict[str, Any]] = []
    out_fn = outcome or (lambda gi, i: d.y(gi, i))
    for o in origins:
        fm = fit_month_for(o)
        x = fc(o)
        ok = ~np.isnan(x[:, 0])
        idx = np.nonzero(ok)[0]
        for s in steps:
            for name, fs in (("all", feats_all), ("ged", feats_ged)):
                if (fm, s, name) not in fits:
                    fits[(fm, s, name)] = fit_step(fc, fm, s, fs)
                    if log:
                        log(f"fit {mlabel(fm)} s={s} {name}")
            f_all, f_ged = fits[(fm, s, "all")], fits[(fm, s, "ged")]
            if f_all is None or f_ged is None:
                continue
            h_all = hurdle_forecasts(f_all, x[idx])
            h_ged = hurdle_forecasts(f_ged, x[idx])
            ptab = persistence_tables(d, o, s)
            for j, gi in enumerate(idx):
                g = d.geos[gi]
                y0 = d.y(int(gi), o)
                cands: dict[str, Fc | None] = {
                    CAND: h_all[j],
                    CAND_GED: h_ged[j],
                    PERS: persistence_fc(ptab, y0),
                    BASE: base_rate_fc(d, int(gi), o),
                }
                if extra is not None:
                    cands[VIEWS] = extra(g, o, s)
                if keep_records:
                    recs.append(
                        {
                            "gw": g,
                            "origin": o,
                            "step": s,
                            **{k: v for k, v in cands.items() if v is not None},
                        }
                    )
                y = out_fn(int(gi), o + s)
                if y is None or math.isnan(y):
                    continue
                rows[(g, o, s)] = {k: losses(v, y) for k, v in cands.items() if v is not None}
                rows[(g, o, s)]["_y"] = {"y": y}
    return rows, recs


def stack_weight(
    recs: Sequence[Mapping[str, Any]], outcome: Callable[[str, int], float | None], metric: str
) -> dict[str, Any]:
    """w on W_GRID minimising the mean loss of the pool w*VIEWS + (1-w)*cm_hurdle over past records with a known
    outcome (log loss for the probability pool; MSLE for the log1p point pool)."""
    pairs = []
    for r in recs:
        v, c = r.get(VIEWS), r.get(CAND)
        if v is None or c is None:
            continue
        y = outcome(r["gw"], r["origin"] + r["step"])
        if y is None or math.isnan(y):
            continue
        pairs.append((v, c, y))
    if not pairs:
        return {"w": None, "n": 0}
    best = None
    curve = {}
    for w in W_GRID:
        tot = 0.0
        for v, c, y in pairs:
            if metric == "logloss":
                p = w * v.prob + (1 - w) * c.prob
                tot += losses(Fc(0.0, p, None), y)["logloss"] or 0.0
            else:
                tot += (w * v.point_l + (1 - w) * c.point_l - math.log1p(y)) ** 2
        curve[str(w)] = tot / len(pairs)
        if best is None or curve[str(w)] < curve[str(best)] - 1e-15:
            best = w
    return {"w": best, "n": len(pairs), "curve": curve, "metric": metric}


def stack_fc(v: Fc, c: Fc, w_prob: float, w_point: float) -> Fc:
    return Fc(w_point * v.point_l + (1 - w_point) * c.point_l, w_prob * v.prob + (1 - w_prob) * c.prob, None)


def summarise(
    rows: Mapping[tuple[str, int, int], Mapping[str, Mapping[str, float | None]]], groups: Sequence[tuple[int, int]]
) -> list[dict[str, Any]]:
    out = []
    for lo, hi in groups:
        acc: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        ev = []
        for (_g, _o, s), m in rows.items():
            if not lo <= s <= hi:
                continue
            ev.append(1.0 if m["_y"]["y"] >= THRESHOLD else 0.0)
            for mk, ls in m.items():
                if mk == "_y":
                    continue
                for met, v in ls.items():
                    if v is not None:
                        acc[mk][met].append(v)
        for mk in sorted(acc):
            out.append(
                {
                    "model": mk,
                    "steps": f"{lo}-{hi}",
                    "n": len(acc[mk]["brier"]),
                    "event_rate": (sum(ev) / len(ev)) if ev else None,
                    **{met: math.fsum(v) / len(v) for met, v in acc[mk].items() if v},
                }
            )
    return out


def calibration(
    recs_rows: Iterable[tuple[float, float]], bins: Sequence[float] = (0, 0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0001)
) -> list[dict[str, Any]]:
    """Reliability table of (prob, outcome 0/1) pairs."""
    acc: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for p, o in recs_rows:
        for k in range(len(bins) - 1):
            if bins[k] <= p < bins[k + 1]:
                acc[k].append((p, o))
                break
    return [
        {
            "bin": f"[{bins[k]:g}, {min(bins[k + 1], 1):g})",
            "n": len(v),
            "mean_prob": math.fsum(p for p, _ in v) / len(v),
            "freq": math.fsum(o for _, o in v) / len(v),
        }
        for k, v in sorted(acc.items())
    ]


__all__ = [
    "BASE",
    "CAND",
    "CAND_GED",
    "FEATURES",
    "PERS",
    "STACK",
    "VIEWS",
    "CMData",
    "build_cmdata",
    "calibration",
    "crps_sample",
    "family_holm",
    "features_at",
    "fit_month_for",
    "losses",
    "midx",
    "paired_test",
    "run_origins",
    "stack_fc",
    "stack_weight",
    "summarise",
]
