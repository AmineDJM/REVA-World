"""WASDE vintages, the pre-declared truth rule, evaluation cases and REVA's candidate forecasts (BEAT-AGRI).

Everything a candidate uses at origin ``t`` (the instant a WASDE release becomes usable: release day + 1, 00:00 UTC)
was published by ``t``:

* WASDE values of earlier releases and of the release itself (``atlas.usda_wasde_esmis.headline``, each report as
  published);
* truths of earlier marketing years only once the truth release is available (``truth_available <= t``);
* NCEI Climate at a Glance ag-belt monthly temperature/precipitation (final vintage) for month ``m`` only from day
  :data:`WEATHER_DAY` of month ``m + 1`` (NCEI's September 2026 release is stamped 2026-10-06; 6 days margin).

Relative errors ``e = (truth - point) / point``; candidates correct the WASDE point multiplicatively.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import polars as pl

CROPS = ("corn", "soybeans", "wheat", "rice")
SERIES: tuple[str, ...] = tuple(f"us.{c}.{a}" for c in CROPS for a in ("yield", "production", "ending_stocks")) + tuple(
    f"world.{c}.{a}" for c in CROPS for a in ("production", "ending_stocks")
)
#: truth = the series' value for marketing year Y in the FIRST WASDE released on/after (Y + 1, month, 1)
TRUTH_MONTH: dict[str, int] = {
    s: (1 if s.startswith("us.") and not s.endswith("ending_stocks") else 11) for s in SERIES
}
#: report-month offsets k (0 = May of the marketing year's first calendar year) that are evaluation origins
MAX_K: dict[str, int] = {
    s: (4 if s.startswith("us.wheat.") and not s.endswith("ending_stocks") else 7)
    if s.startswith("us.") and not s.endswith("ending_stocks")
    else 11
    for s in SERIES
}
GROUPS: dict[str, tuple[str, ...]] = {
    "us_yield": tuple(f"us.{c}.yield" for c in CROPS),
    "us_production": tuple(f"us.{c}.production" for c in CROPS),
    "us_ending_stocks": tuple(f"us.{c}.ending_stocks" for c in CROPS),
    "world_production": tuple(f"world.{c}.production" for c in CROPS),
    "world_ending_stocks": tuple(f"world.{c}.ending_stocks" for c in CROPS),
    "us_cornsoy_yield": ("us.corn.yield", "us.soybeans.yield"),
    "us_cornsoy_production": ("us.corn.production", "us.soybeans.production"),
}
WINDOWS: dict[str, tuple[int, int]] = {"early": (0, 2), "mid": (3, 5), "late": (6, 11)}
#: (group, window) cells per candidate; wx only where summer weather can be published by the release
CELLS: dict[str, tuple[tuple[str, str], ...]] = {
    "bc": tuple((g, w) for g in list(GROUPS)[:5] for w in WINDOWS),
    "rev": tuple((g, w) for g in list(GROUPS)[:5] for w in WINDOWS),
    "wx": tuple((g, w) for g in ("us_cornsoy_yield", "us_cornsoy_production") for w in ("mid", "late")),
}
WX_REGION = {"corn": 261, "soybeans": 262}
WX_MONTHS = (6, 7, 8)
WEATHER_DAY = 12
WEATHER_FIRST_YEAR = 1990
SHRINK_N0 = 5.0  # intercept weight n / (n + 5)
SLOPE_M0 = 20.0  # pooled revision slope weight m / (m + 20)
SLOPE_CLIP = (-1.0, 2.0)
RIDGE_LAMBDA = 1.0
MIN_PAST = 3  # past marketing years with a known truth before a candidate issues a point
MIN_SLOPE_ROWS = 10
MIN_WX_ROWS = 6
MIN_DENSITY = 5  # past own errors before a density (candidate or the fair WASDE fan)
TREND_YEARS = 20
TREND_MIN = 10
Z80 = 1.2815515655446004
MODELS = ("wasde", "bc", "rev", "wx", "trend")


def t0(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


@dataclass(frozen=True)
class Release:
    release: date
    available_at: datetime
    report_month: int | None
    values: Mapping[tuple[str, int], tuple[float, str]]


@dataclass
class Vintages:
    releases: list[Release]

    @classmethod
    def from_frame(cls, frame: pl.DataFrame) -> Vintages:
        """``atlas.usda_wasde_esmis.headline`` rows -> releases (each report as published)."""
        acc: dict[date, dict[tuple[str, int], tuple[float, str]]] = defaultdict(dict)
        meta: dict[date, tuple[datetime, int | None]] = {}
        for ind, ps, v, fl in frame.select("indicator", "period_start", "value", "flags").iter_rows():
            if not str(ind).startswith("wasde_esmis:") or v is None:
                continue
            series, vv = str(ind)[len("wasde_esmis:") :].split("|v=")
            f = json.loads(fl)
            rel = date.fromisoformat(vv)
            acc[rel][(series, ps.year)] = (float(v), str(f["estimate_kind"]))
            avail = datetime.fromisoformat(str(f["available_at"]).replace("Z", "+00:00"))
            if avail < t0(rel):
                raise ValueError(f"available_at before the release date: {ind}")
            meta[rel] = (avail, f.get("report_month"))
        return cls([Release(d, meta[d][0], meta[d][1], acc[d]) for d in sorted(acc)])

    def truth_release(self, series: str, my: int) -> Release | None:
        cut = date(my + 1, TRUTH_MONTH[series], 1)
        for r in self.releases:
            if r.release >= cut:
                return r if (series, my) in r.values else None
        return None

    def truth(self, series: str, my: int) -> tuple[float, datetime, date] | None:
        r = self.truth_release(series, my)
        if r is None:
            return None
        return r.values[(series, my)][0], r.available_at, r.release

    def in_force(self, t: datetime) -> Release | None:
        got = [r for r in self.releases if r.available_at <= t]
        return got[-1] if got else None


@dataclass(frozen=True)
class Weather:
    """region -> {(year, month): (tavg, pcp)}; month m usable from day WEATHER_DAY of m + 1."""

    values: Mapping[int, Mapping[tuple[int, int], tuple[float, float]]]

    @classmethod
    def from_frame(cls, frame: pl.DataFrame) -> Weather:
        acc: dict[int, dict[tuple[int, int], dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
        for ind, ps, v in frame.select("indicator", "period_start", "value").iter_rows():
            _, reg, par = str(ind).split(":")
            if v is not None:
                acc[int(reg)][(ps.year, ps.month)][par] = float(v)
        out = {
            r: {k: (d["tavg"], d["pcp"]) for k, d in m.items() if "tavg" in d and "pcp" in d} for r, m in acc.items()
        }
        return cls(out)

    @staticmethod
    def usable(y: int, m: int, t: datetime) -> bool:
        return t >= datetime(y + (m == 12), 1 if m == 12 else m + 1, WEATHER_DAY, tzinfo=UTC)

    def features(self, region: int, y: int, months: Sequence[int], ref_years: Sequence[int]) -> list[float] | None:
        """Standardised (tavg, pcp) anomalies of ``months`` of year ``y`` against ``ref_years``."""
        reg = self.values.get(region, {})
        out: list[float] = []
        for m in months:
            if (y, m) not in reg:
                return None
            for j in (0, 1):
                ref = [reg[(yy, m)][j] for yy in ref_years if (yy, m) in reg]
                if len(ref) < 10:
                    return None
                mu, sd = float(np.mean(ref)), float(np.std(ref, ddof=1))
                out.append((reg[(y, m)][j] - mu) / sd if sd > 0 else 0.0)
        return out


@dataclass
class Case:
    series: str
    my: int
    k: int
    release: date
    t: datetime
    official: float
    prev: float | None
    truth: float
    truth_available: datetime
    truth_release: date
    points: dict[str, float | None] = field(default_factory=dict)
    sds: dict[str, float | None] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def err(self) -> float:
        return (self.truth - self.official) / self.official

    @property
    def delta(self) -> float:
        return 0.0 if self.prev is None or self.prev == 0 else (self.official - self.prev) / self.prev

    @property
    def crop(self) -> str:
        return self.series.split(".")[1]


def window_of(k: int) -> str:
    for w, (a, b) in WINDOWS.items():
        if a <= k <= b:
            return w
    raise ValueError(k)


def groups_of(series: str) -> list[str]:
    return [g for g, ss in GROUPS.items() if series in ss]


def build_cases(v: Vintages, *, first_my: int, last_my: int) -> tuple[list[Case], list[dict[str, Any]]]:
    """Every (series, marketing year, origin release) with k in the series' window, the first release of each
    calendar month printing the year as the series' projection; truth by the pre-declared rule."""
    cases: list[Case] = []
    dropped: list[dict[str, Any]] = []
    for s in SERIES:
        for y in range(first_my, last_my + 1):
            tr = v.truth(s, y)
            seen_months: set[tuple[int, int]] = set()
            prev: float | None = None
            for r in v.releases:
                val = r.values.get((s, y))
                if val is None:
                    continue
                if val[1] != "projection":
                    continue
                k = (r.release.year - y) * 12 + r.release.month - 5
                ym = (r.release.year, r.release.month)
                if 0 <= k <= MAX_K[s] and ym not in seen_months:
                    seen_months.add(ym)
                    if tr is None:
                        dropped.append({"series": s, "my": y, "release": r.release.isoformat(), "reason": "no truth"})
                    elif tr[1] <= r.available_at:
                        dropped.append(
                            {"series": s, "my": y, "release": r.release.isoformat(), "reason": "truth known"}
                        )
                    else:
                        cases.append(Case(s, y, k, r.release, r.available_at, val[0], prev, tr[0], tr[1], tr[2]))
                prev = val[0]
    cases.sort(key=lambda c: (c.t, c.series))
    return cases, dropped


# --------------------------------------------------------------------------- candidates


def _shrunk_mean(xs: Sequence[float]) -> float:
    n = len(xs)
    return (n / (n + SHRINK_N0)) * math.fsum(xs) / n if n else 0.0


def compute_candidates(cases: list[Case], weather: Weather | None) -> None:
    """Fill ``points`` and ``sds`` of every case, in time order, from information published by its origin."""
    by_sk: dict[tuple[str, int], list[Case]] = defaultdict(list)
    for c in cases:
        by_sk[(c.series, c.k)].append(c)
    by_gw: dict[tuple[str, str], list[Case]] = defaultdict(list)
    for c in cases:
        if c.k >= 1:
            for g in groups_of(c.series):
                by_gw[(g, window_of(c.k))].append(c)
    truths: dict[tuple[str, int], tuple[float, datetime]] = {}
    for c in cases:
        truths[(c.series, c.my)] = (c.truth, c.truth_available)
    for c in sorted(cases, key=lambda x: (x.t, x.series)):
        past = [p for p in by_sk[(c.series, c.k)] if p.my < c.my and p.truth_available <= c.t]
        c.points["wasde"] = c.official
        c.info["n_past"] = len(past)
        # bias correction
        c.points["bc"] = c.official * (1 + _shrunk_mean([p.err for p in past])) if len(past) >= MIN_PAST else None
        # revision smoothing: pooled slope over the group x window, intercept per (series, k)
        c.points["rev"] = _rev_point(c, by_gw, past)
        # weather (U.S. corn / soybeans yield and production)
        c.points["wx"] = _wx_point(c, past, weather) if weather is not None else None
        # pure REVA trend (descriptive, U.S. yields)
        c.points["trend"] = _trend_point(c, truths) if c.series.endswith(".yield") else None
        # densities: Gaussian, sigma = RMS of the model's own past relative errors at the same (series, k)
        for m in ("wasde", "bc", "rev", "wx"):
            if c.points.get(m) is None:
                c.sds[m] = None
                continue
            own = [
                (p.truth - float(p.points[m])) / float(p.points[m])  # type: ignore[arg-type]
                for p in past
                if p.points.get(m) is not None
            ]
            c.sds[m] = (
                abs(float(c.points[m])) * math.sqrt(math.fsum(x * x for x in own) / len(own))  # type: ignore[arg-type]
                if len(own) >= MIN_DENSITY
                else None
            )


def _rev_point(c: Case, by_gw: Mapping[tuple[str, str], Sequence[Case]], past: Sequence[Case]) -> float | None:
    if len(past) < MIN_PAST:
        return None
    g = next(x for x in groups_of(c.series) if x in list(GROUPS)[:5])
    pool = [p for p in by_gw.get((g, window_of(c.k)), ()) if p.my < c.my and p.truth_available <= c.t]
    by: dict[tuple[str, int], list[Case]] = defaultdict(list)
    for p in pool:
        by[(p.series, p.k)].append(p)
    num = den = 0.0
    m = 0
    for ps in by.values():
        if len(ps) < 2:
            continue
        me = math.fsum(p.err for p in ps) / len(ps)
        md = math.fsum(p.delta for p in ps) / len(ps)
        for p in ps:
            num += (p.err - me) * (p.delta - md)
            den += (p.delta - md) ** 2
            m += 1
    b = 0.0
    if m >= MIN_SLOPE_ROWS and den > 0:
        b = min(max(num / den * (m / (m + SLOPE_M0)), SLOPE_CLIP[0]), SLOPE_CLIP[1])
    a = _shrunk_mean([p.err - b * p.delta for p in past])
    c.info["rev_slope"] = b
    return c.official * (1 + a + b * c.delta)


def _wx_point(c: Case, past: Sequence[Case], weather: Weather) -> float | None:
    if c.crop not in WX_REGION:
        return None
    months = [m for m in WX_MONTHS if Weather.usable(c.my, m, c.t)]
    if not months or len(past) < MIN_WX_ROWS:
        return None
    reg = WX_REGION[c.crop]
    ref = list(range(WEATHER_FIRST_YEAR, c.my))
    x0 = weather.features(reg, c.my, months, ref)
    rows = [(weather.features(reg, p.my, months, ref), p.err) for p in past]
    rows_ok = [(x, e) for x, e in rows if x is not None]
    if x0 is None or len(rows_ok) < MIN_WX_ROWS:
        return None
    X = np.array([x for x, _ in rows_ok], dtype=float)
    e = np.array([e for _, e in rows_ok], dtype=float)
    xm, em = X.mean(axis=0), float(e.mean())
    Xc = X - xm
    n = len(e)
    beta = np.linalg.solve(Xc.T @ Xc + RIDGE_LAMBDA * n * np.eye(X.shape[1]), Xc.T @ (e - em))
    adj = (n / (n + SHRINK_N0)) * em + float((np.array(x0) - xm) @ beta)
    c.info["wx_months"] = months
    return c.official * (1 + adj)


def _trend_point(c: Case, truths: Mapping[tuple[str, int], tuple[float, datetime]]) -> float | None:
    pts = [
        (y, v)
        for y in range(c.my - TREND_YEARS, c.my)
        if (tv := truths.get((c.series, y))) is not None and tv[1] <= c.t
        for v in (tv[0],)
    ]
    if len(pts) < TREND_MIN:
        return None
    ys = np.array([p[0] for p in pts], dtype=float)
    vs = np.array([p[1] for p in pts], dtype=float)
    b1, b0 = np.polyfit(ys, vs, 1)
    return float(b0 + b1 * c.my)


# --------------------------------------------------------------------------- scores


def crps_gauss(mu: float, sd: float, y: float) -> float:
    z = (y - mu) / sd
    pdf = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    cdf = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return sd * (z * (2 * cdf - 1) + 2 * pdf - 1 / math.sqrt(math.pi))


def score_case(c: Case) -> dict[str, dict[str, float | None]]:
    """Per model: AE and CRPS in percent of the truth; 80 % central coverage; PIT."""
    out: dict[str, dict[str, float | None]] = {}
    scale = abs(c.truth) if c.truth != 0 else None
    for m in MODELS:
        p = c.points.get(m)
        if p is None or scale is None:
            continue
        sd = c.sds.get(m)
        rec: dict[str, float | None] = {"ae": 100 * abs(p - c.truth) / scale, "crps": None, "cov80": None, "pit": None}
        if sd is not None and sd > 0:
            rec["crps"] = 100 * crps_gauss(p, sd, c.truth) / scale
            rec["cov80"] = 1.0 if abs(c.truth - p) <= Z80 * sd else 0.0
            rec["pit"] = 0.5 * (1 + math.erf((c.truth - p) / (sd * math.sqrt(2))))
        out[m] = rec
    return out


def scored(cases: Iterable[Case]) -> list[tuple[Case, dict[str, dict[str, float | None]]]]:
    return [(c, score_case(c)) for c in cases]


__all__ = [
    "CELLS",
    "GROUPS",
    "MODELS",
    "SERIES",
    "WINDOWS",
    "Case",
    "Release",
    "Vintages",
    "Weather",
    "build_cases",
    "compute_candidates",
    "crps_gauss",
    "score_case",
    "scored",
    "window_of",
]
