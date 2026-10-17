"""As-of views of the official nowcasts, the truths and REVA's real-time inputs (V7, BEAT-NOWCAST). Pure numerics.

Everything is built from vintage cells (:data:`reva.evaluation.vintages.CELL_SCHEMA`, ``available_at`` +
``availability_basis`` on every cell) and selected **by availability instant only**: a value is usable at cutoff
``C`` iff its ``available_at <= C``. Every selection used by a case is also passed through a
:class:`~reva.evaluation.leakage.LeakageGuard` (``available_then``) by :func:`guard_refs` (defence in depth: a
selection bug raises ``LeakageBlocked`` instead of leaking).

Time units: ``mi`` = month index ``year * 12 + month - 1``; ``qi`` = quarter index ``year * 4 + quarter - 1``
(months of quarter ``qi`` are ``3 qi .. 3 qi + 2``).
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from reva.evaluation.leakage import GuardMode, InputRef, LeakageGuard, RefKind


def mi_of(d: date) -> int:
    return d.year * 12 + d.month - 1


def qi_of(d: date) -> int:
    return d.year * 4 + (d.month - 1) // 3


def qi_start(qi: int) -> date:
    return date(qi // 4, 3 * (qi % 4) + 1, 1)


def mi_start(mi: int) -> date:
    return date(mi // 12, mi % 12 + 1, 1)


def qlabel(qi: int) -> str:
    return f"{qi // 4}Q{qi % 4 + 1}"


def mlabel(mi: int) -> str:
    return f"{mi // 12}-{mi % 12 + 1:02d}"


def midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


@dataclass(frozen=True)
class Stamp:
    """One selected input: its value, availability instant and rule, and a reference to the cell(s)."""

    value: float
    available_at: datetime
    basis: str
    ref: str


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# ------------------------------------------------------------------------------------------------ official books
@dataclass
class OfficialBook:
    """Every published update of one official nowcast: target period start -> updates sorted by publication."""

    key: str
    updates: dict[date, list[tuple[date, Stamp]]] = field(default_factory=dict)

    @classmethod
    def from_cells(cls, key: str, cells: pl.DataFrame, family: str, indicator_prefix: str) -> OfficialBook:
        sub = cells.filter(
            (pl.col("family") == family)
            & pl.col("indicator").str.starts_with(indicator_prefix + "|v=")
            & (pl.col("estimate_kind") == "projection")
            & pl.col("available_at").is_not_null()
        )
        book = cls(key)
        acc: dict[date, list[tuple[date, Stamp]]] = defaultdict(list)
        for r in sub.select("indicator", "period_start", "vintage", "value", "available_at",
                            "availability_basis").iter_rows(named=True):  # fmt: skip
            v = _f(r["value"])
            if v is None:
                continue
            pub = date.fromisoformat(r["vintage"])
            acc[r["period_start"]].append(
                (pub, Stamp(v, r["available_at"], r["availability_basis"], f"{r['indicator']}@{r['period_start']}"))
            )
        book.updates = {k: sorted(v, key=lambda x: (x[1].available_at, x[0])) for k, v in acc.items()}
        return book

    def latest(self, target: date, cutoff: datetime) -> tuple[date, Stamp] | None:
        ups = self.updates.get(target)
        if not ups:
            return None
        i = bisect.bisect_right([u[1].available_at for u in ups], cutoff)
        return ups[i - 1] if i else None


# ------------------------------------------------------------------------------------------------ truths
@dataclass(frozen=True)
class Truth:
    value: float
    known_at: datetime
    basis: str
    release_day: date  # official nowcasts are compared only when published strictly before this day
    ref: str
    cross_check: float | None = None  # same value as recorded by another publisher (descriptive)


def gdp_truths(cells: pl.DataFrame, which: str = "first") -> dict[date, Truth]:
    """Philadelphia Fed first (or third) release of real GDP growth; the release day of the first release is
    BEA's publication date as recorded by the Atlanta Fed when available (else the conservative bound)."""
    rel = cells.filter((pl.col("family") == "philfed_release_values") & (pl.col("vintage") == which))
    adv = {
        r["period_start"]: r
        for r in cells.filter(
            (pl.col("family") == "atl_gdpnow") & (pl.col("indicator") == "GDPNOW_BEA_ADVANCE|v=advance")
        ).iter_rows(named=True)
    }
    out: dict[date, Truth] = {}
    for r in rel.iter_rows(named=True):
        v = _f(r["value"])
        if v is None or r["available_at"] is None:
            continue
        a = adv.get(r["period_start"]) if which == "first" else None
        if a is not None:
            known, basis = a["available_at"], a["availability_basis"]
            day = date.fromisoformat(a["vintage_date_text"])
        else:
            known, basis = r["available_at"], r["availability_basis"]
            day = (known - timedelta(days=1)).date()
        out[r["period_start"]] = Truth(v, known, basis, day, f"{r['indicator']}@{r['period_start']}",
                                       _f(a["value"]) if a is not None else None)  # fmt: skip
    return out


def clev_actuals(cells: pl.DataFrame, series: str) -> dict[date, Truth]:
    """Cleveland Fed record of the published month-over-month value (``CPI`` / ``CORE_CPI``) on its release day."""
    ind = f"CLEV_MONTH_{series}_ACTUAL|v=actual"
    out: dict[date, Truth] = {}
    for r in cells.filter((pl.col("family") == "clev_inflation_nowcast") & (pl.col("indicator") == ind)).iter_rows(
        named=True
    ):
        v = _f(r["value"])
        if v is None or r["available_at"] is None:
            continue
        day = (r["available_at"] - timedelta(days=1)).date()
        out[r["period_start"]] = Truth(v, r["available_at"], r["availability_basis"], day, f"{ind}@{r['period_start']}")
    return out


def known_values(truths: Mapping[date, Truth], cutoff: datetime) -> dict[date, Truth]:
    return {k: t for k, t in truths.items() if t.known_at <= cutoff}


# ------------------------------------------------------------------------------------------------ vintages
@dataclass
class VintageSeries:
    """Monthly (or quarterly) vintages of one provider series: sorted by availability instant."""

    name: str
    instants: list[datetime] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    values: list[dict[int, float]] = field(default_factory=list)  # period index (mi) -> value

    @classmethod
    def from_cells(cls, name: str, cells: pl.DataFrame, family: str, provider_variable: str) -> VintageSeries:
        sub = cells.filter(
            (pl.col("family") == family)
            & (pl.col("provider_variable") == provider_variable)
            & pl.col("available_at").is_not_null()
        )
        by: dict[str, tuple[datetime, str, dict[int, float]]] = {}
        for r in sub.select("vintage", "period_start", "value", "available_at", "availability_basis").iter_rows():
            vint, ps, val, av, basis = r
            v = _f(val)
            if v is None:
                continue
            ent = by.setdefault(vint, (av, basis, {}))
            if ent[0] != av:
                raise ValueError(f"{name} vintage {vint}: two availability instants")
            ent[2][mi_of(ps)] = v
        s = cls(name)
        for vint, (av, basis, vals) in sorted(by.items(), key=lambda kv: (kv[1][0], kv[0])):
            s.instants.append(av)
            s.labels.append(vint)
            s.bases.append(basis)
            s.values.append(vals)
        return s

    def as_of(self, cutoff: datetime) -> tuple[int, dict[int, float]] | None:
        """(position, values) of the latest vintage available at ``cutoff``."""
        i = bisect.bisect_right(self.instants, cutoff)
        return (i - 1, self.values[i - 1]) if i else None

    def stamp(self, pos: int) -> InputRef:
        return InputRef(
            RefKind.observation_revision,
            f"{self.name}|v={self.labels[pos]}",
            source_available_at=self.instants[pos],
            availability_basis={"rule": self.bases[pos], "vintage": self.labels[pos]},
        )


@dataclass
class DailySeries:
    """A daily series that is not revised (market prices), usable ``lag_days`` after the observation day."""

    name: str
    lag_days: int
    basis: str
    days: list[date] = field(default_factory=list)
    vals: list[float] = field(default_factory=list)

    @classmethod
    def from_frame(cls, name: str, df: pl.DataFrame, indicator: str, lag_days: int, basis: str) -> DailySeries:
        sub = df.filter(pl.col("indicator") == indicator).select("period_start", "value").sort("period_start")
        s = cls(name, lag_days, basis)
        for d, v in sub.iter_rows():
            x = _f(v)
            if x is not None and x > 0:
                s.days.append(d)
                s.vals.append(x)
        return s

    def last_usable_day(self, cutoff: datetime) -> date:
        return (cutoff - timedelta(days=self.lag_days)).date() - timedelta(days=1)

    def month_mean(self, mi: int, cutoff: datetime | None = None) -> tuple[float, int] | None:
        a, b = mi_start(mi), mi_start(mi + 1)
        if cutoff is not None:
            b = min(b, self.last_usable_day(cutoff) + timedelta(days=1))
        i, j = bisect.bisect_left(self.days, a), bisect.bisect_left(self.days, b)
        if j <= i:
            return None
        return math.fsum(self.vals[i:j]) / (j - i), j - i

    def last_mean(self, cutoff: datetime, n: int = 5) -> tuple[float, date] | None:
        j = bisect.bisect_right(self.days, self.last_usable_day(cutoff))
        if j == 0:
            return None
        xs = self.vals[max(0, j - n) : j]
        return math.fsum(xs) / len(xs), self.days[j - 1]

    def ref(self, cutoff: datetime) -> InputRef | None:
        j = bisect.bisect_right(self.days, self.last_usable_day(cutoff))
        if j == 0:
            return None
        d = self.days[j - 1]
        return InputRef(
            RefKind.observation_revision,
            f"{self.name}@{d.isoformat()}",
            source_available_at=midnight(d) + timedelta(days=1 + self.lag_days),
            availability_basis={"rule": self.basis, "lag_days": self.lag_days},
        )


# ------------------------------------------------------------------------------------------------ guard
def truth_ref(t: Truth) -> InputRef:
    return InputRef(
        RefKind.observation_revision, t.ref, source_available_at=t.known_at, availability_basis={"rule": t.basis}
    )


def stamp_ref(s: Stamp) -> InputRef:
    return InputRef(
        RefKind.observation_revision, s.ref, source_available_at=s.available_at, availability_basis={"rule": s.basis}
    )


def model_ref(name: str, trained_until: datetime) -> InputRef:
    return InputRef(RefKind.trained_model, name, training_cutoff=trained_until)


def guard_refs(cutoff: datetime, refs: Iterable[InputRef | None]) -> int:
    """Checks every reference against ``cutoff`` (``available_then``); raises ``LeakageBlocked`` on a violation."""
    g = LeakageGuard(cutoff, GuardMode.available_then)
    n = 0
    for r in refs:
        if r is not None:
            g.check(r)
            n += 1
    return n


# ------------------------------------------------------------------------------------------------ CPI history
@dataclass
class CpiHistory:
    """Headline CPI m/m history as known at a cutoff: the RTDSM quarterly CPI vintage in force (m/m from its
    levels) overridden by the first-release values recorded by the Cleveland Fed (known on their release day)."""

    rtdsm: VintageSeries
    actual: Mapping[date, Truth]

    def as_of(self, cutoff: datetime) -> tuple[dict[int, float], list[InputRef]]:
        out: dict[int, float] = {}
        refs: list[InputRef] = []
        got = self.rtdsm.as_of(cutoff)
        if got is not None:
            pos, lv = got
            for m, v in lv.items():
                p = lv.get(m - 1)
                if p:
                    out[m] = 100.0 * (v / p - 1.0)
            refs.append(self.rtdsm.stamp(pos))
        last = None
        for k, t in self.actual.items():
            if t.known_at <= cutoff:
                out[mi_of(k)] = t.value
                if last is None or t.known_at > last.known_at:
                    last = t
        if last is not None:
            refs.append(truth_ref(last))
        return out, refs


def mean_last(hist: Mapping[int, float], before: int, n: int, min_n: int) -> float | None:
    xs = [hist[m] for m in range(before - n, before) if m in hist]
    if len(xs) < min_n:
        return None
    return math.fsum(xs) / len(xs)


def sorted_targets(xs: Sequence[date]) -> list[date]:
    return sorted(set(xs))


__all__ = [
    "CpiHistory",
    "DailySeries",
    "OfficialBook",
    "Stamp",
    "Truth",
    "VintageSeries",
    "clev_actuals",
    "gdp_truths",
    "guard_refs",
    "known_values",
    "mean_last",
    "mi_of",
    "mi_start",
    "midnight",
    "mlabel",
    "model_ref",
    "qi_of",
    "qi_start",
    "qlabel",
    "stamp_ref",
    "truth_ref",
]
