"""Cohort-component population projection, ``demography.cohort_component@1.0.0``.

A *conditional projection under stated assumptions, not a forecast*. Pure: no
database, network or clock; inputs are the dataset-input tables derived from
archived Eurostat bytes (schema ``reva-eurostat-demography-table/1``, see
``reva.adapters.connectors.eurostat_demography``).

Stable entry points (agent PEN and the plugin use only these):

* :meth:`CountryDemography.from_tables` — build a country's data from tables;
* :func:`project` — one projection under one assumption set -> :class:`Projection`;
* :func:`backtest` — out-of-sample comparison against an observed population.

Numerics
--------
``decimal.Decimal`` throughout, context precision 50, ``ROUND_HALF_EVEN``. Rates and
survival ratios are 50-significant-digit decimals. Every *person count* produced by
a multiplication (survivors, births, averaged migration) is quantized to
``1e-9`` persons; every accounting identity (deaths = population - survivors,
totals, next population) is then computed in a context that traps ``Inexact``, so the
balancing equation ``P(t+1) = P(t) - D + B + M`` holds *exactly* (it is asserted on
every step). Floats are never used. Published result values are rounded only at
the presentation step (see the plugin).

Method (annual steps, 1 January to 1 January, single ages 0..omega-1 and an open
group omega+, by recorded sex)
-------------------------------------------------------------------------------
Mortality (constant base-period rates, the only mortality option in 1.0.0).
  ``m_x = sum_t D_x(t) / sum_t E_x(t)`` with ``D_x(t)`` deaths at completed age x in
  calendar year t (demo_magec) and exposure ``E_x(t) = (P_x(t) + P_x(t+1)) / 2``
  (mean of the 1 January populations, demo_pjan). Conversion to probabilities:
  ``q_x = m_x / (1 + (1 - a_x) m_x)`` with ``a_x = 1/2`` for x >= 1 and the
  Coale-Demeny infant ``a_0`` (Preston, Heuveline & Guillot 2001, table 3.3: males
  ``0.045 + 2.684 m_0`` (0.330 if m_0 >= 0.107), females ``0.053 + 2.800 m_0`` (0.350)).
  Life table with radix ``l_0 = 1``: ``d_x = l_x q_x``, ``l_{x+1} = l_x - d_x``,
  ``L_x = l_{x+1} + a_x d_x``, open group ``L_w = l_w / m_w``.
  Survival ratios: births -> age 0: ``S_b = L_0 / l_0``; age x -> x+1:
  ``S_x = L_{x+1} / L_x`` (x <= w-2); ages w-1 and w+ -> w+: ``S_o = L_w / (L_{w-1} + L_w)``.
  (Lexis parallelogram survival from completed-age period rates, Preston et al. ch. 6.)
Fertility.
  ``constant_base_period_rates``: ``f_g = sum_t B_g(t) / sum_t EF_g(t)`` for mother's-age
  categories g of demo_fasec (completed age; single ages 15..49; ``Y10-14``/``Y_LT15``
  with exposure ages 10-14; ``Y_GE50`` with exposure ages 50-54), with
  ``EF_g(t) = (F_g(t) + F_g(t+1)) / 2``. Births with unknown mother's age (``UNK``) are
  never spread over ages: they enter as one separate rate per woman aged 15-49 (stated
  assumption). ``europop2023_bsl``: EUROPOP2023 baseline age-specific fertility-rate
  assumptions (proj_23naasfr) applied to the same exposures (assumption: same rate basis).
  Projected births ``B = sum_g f_g EF_g(t)``; sex at birth: male share
  ``pi_M = sum_t B_M(t) / sum_t (B_M(t) + B_F(t))`` from the base period (demo_fasec
  all-ages totals by newborn sex).
Net migration (by age and sex, *negative values allowed*), added at the end of the year
  (migrants are not exposed to mortality in their arrival year — stated simplification).
  Residual method: ``M_{x+1}(t) = P_{x+1}(t+1) - S_x P_x(t)``,
  ``M_w(t) = P_w(t+1) - S_o (P_{w-1}(t) + P_w(t))``, ``M_0(t) = P_0(t+1) - S_b B_s(t)``,
  with the *base-period* survival ratios and observed births by sex. The residual
  absorbs every inconsistency between the population and the vital statistics
  (census revisions, registration corrections, deaths/births with unknown age, the
  difference between single-year and base-period mortality), so it is "net migration
  plus statistical adjustment", not measured migration. Options: ``zero``;
  ``average_residual`` (mean of the residuals over a stated window of complete years);
  ``europop2023_bsl`` (EUROPOP2023 baseline net-migration assumption, proj_23nanmig).
  Reconstructing the base period with its own residuals reproduces the observed
  populations exactly (tested).

Data rules
----------
Open age: ``omega = min(100, open-age starts of every table used)``; a table's
``Y_OPEN`` starts after the last single age it reports. Single ages at or above omega are
*summed* into the open group (sum of disjoint leaves); an open group is never split. A
country whose data would need omega < 85 is refused. omega is lowered (never below 85)
while a single age has zero base-period exposure or the open group has no deaths.
Unknown-age population and deaths (``UNK``) are not distributed: they are excluded and
reported (the residual absorbs them). Status flags (``p``, ``e``, ``b``) of every input
year are reported with the projection.
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
)
from pathlib import Path
from typing import Any

from reva.kernel.errors import CapabilityGap, MissingInput, ValidationProblem

MODEL_KEY = "demography.cohort_component"
MODEL_VERSION = "1.0.0"
LABEL = "conditional projection under stated assumptions, not a forecast"
PRECISION = 50
QUANTUM = Decimal("1e-9")
NUMERIC_TYPE = (f"decimal.Decimal, precision {PRECISION}, ROUND_HALF_EVEN; person counts quantized to {QUANTUM} "
                "persons; accounting identities computed with Inexact trapped (exact)")
MAX_OPEN_AGE = 100
MIN_OPEN_AGE = 85
SEXES = ("F", "M")
ZERO = Decimal(0)
ONE = Decimal(1)
HALF = Decimal("0.5")

MIGRATION_OPTIONS = ("zero", "average_residual", "europop2023_bsl")
FERTILITY_OPTIONS = ("constant_base_period_rates", "europop2023_bsl")
MORTALITY_OPTIONS = ("constant_base_period_rates",)
ASSUMPTION_LABELS = {
    "migration:zero": "zero net migration at every age (closed population)",
    "migration:average_residual": ("net migration by age and sex = mean of the residual estimates over the stated "
                                   "window (residual method: includes statistical adjustment)"),
    "migration:europop2023_bsl": ("EUROPOP2023 baseline net-migration assumption by age and sex "
                                  "(Eurostat proj_23nanmig)"),
    "fertility:constant_base_period_rates": "age-specific fertility rates held at their base-period level",
    "fertility:europop2023_bsl": ("EUROPOP2023 baseline age-specific fertility-rate assumption "
                                  "(Eurostat proj_23naasfr)"),
    "mortality:constant_base_period_rates": "age- and sex-specific death rates held at their base-period level",
}

_CTX = Context(prec=PRECISION, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])
_EXACT = Context(prec=PRECISION, rounding=ROUND_HALF_EVEN,
                 traps=[InvalidOperation, DivisionByZero, Overflow, Inexact])


def qp(x: Decimal) -> Decimal:
    """Quantize a person count to the model quantum."""
    return _CTX.quantize(x, QUANTUM)


def mul(a: Decimal, b: Decimal) -> Decimal:
    return _CTX.multiply(a, b)


def div(a: Decimal, b: Decimal) -> Decimal:
    return _CTX.divide(a, b)


def xadd(*xs: Decimal) -> Decimal:
    """Exact sum (raises on any rounding)."""
    total = ZERO
    for x in xs:
        total = _EXACT.add(total, x)
    return total


def xsum(xs: Iterable[Decimal]) -> Decimal:
    return xadd(*list(xs))


def xsub(a: Decimal, b: Decimal) -> Decimal:
    return _EXACT.subtract(a, b)


def csum(xs: Iterable[Decimal]) -> Decimal:
    """Sum in the model context (rounded at 50 significant digits; for rates, not persons)."""
    total = ZERO
    for x in xs:
        total = _CTX.add(total, x)
    return total


def dstr(d: Decimal) -> str:
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# --------------------------------------------------------------------------- input data
@dataclass(frozen=True)
class Cell:
    value: Decimal | None
    kind: str  # single | open | open_upper | open_lower | group | unknown | total
    start: int | None
    end: int | None
    cell_type: str
    status: str


Table = dict[tuple[int, str], dict[str, Cell]]  # (year, sex) -> age code -> cell


def _rows(table: Any) -> list[Mapping[str, Any]]:
    if table is None:
        return []
    if hasattr(table, "to_pylist"):
        return list(table.to_pylist())
    if hasattr(table, "to_dicts"):
        return list(table.to_dicts())
    return [dict(r) for r in table]


def _table(rows: Iterable[Mapping[str, Any]], dataset: str | None = None) -> Table:
    out: Table = {}
    for r in rows:
        if dataset is not None and r.get("dataset") not in (None, dataset):
            continue
        v = r.get("value")
        out.setdefault((int(r["year"]), str(r.get("sex") or "T")), {})[str(r["age"])] = Cell(
            Decimal(str(v)) if v is not None else None, str(r["age_kind"]),
            r.get("age_start"), r.get("age_end"), str(r.get("cell_type") or "leaf"), str(r.get("status") or ""))
    return out


@dataclass
class CountryDemography:
    """One country's inputs (Eurostat tables) keyed by (year, sex) and age code."""

    iso3: str
    population: Table
    deaths: Table
    births: Table
    europop_migration: Table = field(default_factory=dict)
    europop_fertility: Table = field(default_factory=dict)
    europop_population: Table = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_tables(cls, iso3: str, *, population: Any, deaths: Any, births: Any, europop_migration: Any = None,
                    europop_fertility: Any = None, europop_population: Any = None,
                    provenance: Mapping[str, Any] | None = None) -> CountryDemography:
        """Tables are Arrow tables, Polars frames or row dicts in the dataset-input schema."""
        return cls(iso3, _table(_rows(population), "demo_pjan"), _table(_rows(deaths), "demo_magec"),
                   _table(_rows(births), "demo_fasec"), _table(_rows(europop_migration), "proj_23nanmig"),
                   _table(_rows(europop_fertility), "proj_23naasfr"),
                   _table(_rows(europop_population), "proj_23np"), dict(provenance or {}))

    def years(self, which: str) -> list[int]:
        return sorted({y for (y, _s) in getattr(self, which)})


class DataGap(MissingInput):
    """A required input cell is missing (typed; never zero-filled)."""


def _gap(iso3: str, what: str, **details: Any) -> DataGap:
    return DataGap(f"{iso3}: {what}", missing=[{"country": iso3, "detail": what, **details}])


# --------------------------------------------------------------------------- age vectors
def open_start(cells: Mapping[str, Cell] | None) -> int | None:
    """Start of the open age group a (year, sex) table implies; None when unusable.

    ``Y_OPEN``: one past the last single age with a value (all single ages below it must be
    present). ``Y_GEn``: n. A table without a present open group is unusable as a stock."""
    if not cells:
        return None
    singles = {c.start: c for c in cells.values() if c.kind == "single" and c.value is not None}
    opens = [c for c in cells.values() if c.kind in ("open", "open_upper") and c.cell_type == "leaf"]
    if not singles or not opens or any(o.value is None for o in opens) or len(opens) != 1:
        return None
    o = opens[0]
    last = max(a for a in singles if a is not None)
    if any(a not in singles for a in range(0, last + 1)):
        return None
    if o.kind == "open_upper":
        return int(o.start) if o.start is not None and o.start <= last + 1 else None
    return last + 1


def to_vector(cells: Mapping[str, Cell] | None, omega: int) -> list[Decimal] | None:
    """Single ages 0..omega-1 and the open group omega+ (sum of disjoint leaves); None if incomplete."""
    s = open_start(cells)
    if s is None or s < omega or cells is None:
        return None
    singles = {c.start: c.value for c in cells.values() if c.kind == "single" and c.value is not None}
    vec = [singles[a] for a in range(omega)]
    top = [singles[a] for a in range(omega, s)]
    top.append(next(c.value for c in cells.values()
                    if c.kind in ("open", "open_upper") and c.cell_type == "leaf" and c.value is not None))
    vec.append(xsum(top))  # type: ignore[arg-type]
    return vec


def unknown_age(cells: Mapping[str, Cell] | None) -> Decimal | None:
    if not cells:
        return None
    c = cells.get("UNK")
    return c.value if c is not None else None


# --------------------------------------------------------------------------- life table
def coale_demeny_a0(m0: Decimal, sex: str) -> Decimal:
    """Infant a_0 from m_0 (Preston, Heuveline & Guillot 2001, table 3.3, Coale-Demeny West)."""
    if sex == "M":
        return Decimal("0.330") if m0 >= Decimal("0.107") else _CTX.add(Decimal("0.045"), mul(Decimal("2.684"), m0))
    return Decimal("0.350") if m0 >= Decimal("0.107") else _CTX.add(Decimal("0.053"), mul(Decimal("2.800"), m0))


@dataclass(frozen=True)
class LifeTable:
    sex: str
    m: tuple[Decimal, ...]
    a: tuple[Decimal, ...]
    q: tuple[Decimal, ...]
    lx: tuple[Decimal, ...]
    Lx: tuple[Decimal, ...]
    s_birth: Decimal
    s: tuple[Decimal, ...]  # S_x for x = 0..omega-2
    s_open: Decimal
    capped_q: tuple[int, ...] = ()

    @property
    def omega(self) -> int:
        return len(self.m) - 1

    @property
    def e0(self) -> Decimal:
        return div(csum(self.Lx), self.lx[0])


def life_table(m: Sequence[Decimal], sex: str) -> LifeTable:
    omega = len(m) - 1
    a0 = coale_demeny_a0(m[0], sex)
    a = [a0] + [HALF] * (omega - 1) + [ZERO]
    lx = [ONE]
    Lx: list[Decimal] = []
    q: list[Decimal] = []
    capped: list[int] = []
    for x in range(omega):
        qx = div(m[x], _CTX.add(ONE, mul(_CTX.subtract(ONE, a[x]), m[x])))
        if qx > ONE:
            qx = ONE
            capped.append(x)
        q.append(qx)
        d = mul(lx[x], qx)
        nxt = _CTX.subtract(lx[x], d)
        Lx.append(_CTX.add(nxt, mul(a[x], d)))
        lx.append(nxt)
        if nxt <= 0 and x < omega - 1:
            raise ValidationProblem(f"life table exhausted at age {x + 1} before the open group {omega}",
                                    sex=sex, age=x + 1)
    if m[omega] <= 0:
        raise ValidationProblem("open age group has no deaths: open-group survival is undefined", sex=sex,
                                omega=omega)
    q.append(ONE)
    Lx.append(div(lx[omega], m[omega]))
    s_birth = div(Lx[0], lx[0])
    s = tuple(div(Lx[x + 1], Lx[x]) for x in range(omega - 1))
    s_open = div(Lx[omega], _CTX.add(Lx[omega - 1], Lx[omega]))
    return LifeTable(sex, tuple(m), tuple(a), tuple(q), tuple(lx), tuple(Lx), s_birth, s, s_open, tuple(capped))


def survive(p: Sequence[Decimal], lt: LifeTable) -> list[Decimal]:
    """Survivors of each 1 January cohort to the next 1 January, quantized (index = start age)."""
    omega = lt.omega
    out = [qp(mul(lt.s[x], p[x])) for x in range(omega - 1)]
    out.append(qp(mul(lt.s_open, p[omega - 1])))
    out.append(qp(mul(lt.s_open, p[omega])))
    return out


def age_forward(surv: Sequence[Decimal]) -> list[Decimal]:
    """Survivors placed at their age on the next 1 January (ages 1..omega; index 0 unused)."""
    omega = len(surv) - 1
    nxt = [ZERO] + [surv[x - 1] for x in range(1, omega)]
    nxt.append(xadd(surv[omega - 1], surv[omega]))
    return nxt


# --------------------------------------------------------------------------- base-period rates
@dataclass(frozen=True)
class FertilityGroup:
    code: str
    start: int
    end: int  # exclusive exposure range


def fertility_groups(cells: Mapping[str, Cell]) -> list[FertilityGroup]:
    """Leaf mother's-age categories with their exposure ranges (``UNK`` handled separately)."""
    out: list[FertilityGroup] = []
    for code, c in cells.items():
        if c.cell_type != "leaf" or c.kind in ("total", "unknown"):
            continue
        if c.kind == "single" and c.start is not None:
            out.append(FertilityGroup(code, int(c.start), int(c.start) + 1))
        elif c.kind == "group" and c.start is not None and c.end is not None:
            out.append(FertilityGroup(code, int(c.start), int(c.end)))
        elif c.kind == "open_lower" and c.end is not None:  # Y_LT15: exposure ages 10..14 (stated)
            out.append(FertilityGroup(code, 10, int(c.end)))
        elif c.kind == "open_upper" and c.start is not None:  # Y_GE50: exposure ages 50..54 (stated)
            out.append(FertilityGroup(code, int(c.start), int(c.start) + 5))
        else:
            raise ValidationProblem(f"unexpected fertility age category {code!r}")
    out.sort(key=lambda g: (g.start, g.end))
    for a, b in itertools.pairwise(out):
        if b.start < a.end:
            raise ValidationProblem(f"overlapping fertility age categories {a.code} / {b.code}")
    return out


UNK_GROUP = FertilityGroup("UNK", 15, 50)


def group_sum(vec: Sequence[Decimal], g: FertilityGroup) -> Decimal:
    return xsum(vec[g.start:g.end])


@dataclass(frozen=True)
class BaseRates:
    iso3: str
    omega: int
    years: tuple[int, ...]
    life_tables: dict[str, LifeTable]
    fertility: tuple[tuple[FertilityGroup, Decimal], ...]
    unknown_age_birth_rate: Decimal
    male_share_at_birth: Decimal
    residual_migration: dict[int, dict[str, list[Decimal]]]  # year t (flow t -> t+1) -> sex -> age 0..omega
    notes: tuple[str, ...]
    input_status_flags: dict[str, dict[int, list[str]]]

    @property
    def tfr(self) -> Decimal:
        return _CTX.add(csum(mul(r, Decimal(g.end - g.start)) for g, r in self.fertility),
                        mul(self.unknown_age_birth_rate, Decimal(35)))


def _vec(data: CountryDemography, which: str, year: int, sex: str, omega: int) -> list[Decimal]:
    v = to_vector(getattr(data, which).get((year, sex)), omega)
    if v is None:
        raise _gap(data.iso3, f"{which} {year} {sex} incomplete for single ages 0..{omega - 1} and open group",
                   input=which, year=year, sex=sex)
    return v


def determine_omega(data: CountryDemography, pop_years: Iterable[int], death_years: Iterable[int],
                    extra: Iterable[Mapping[str, Cell] | None] = ()) -> int:
    starts: list[int] = []
    for which, years in (("population", pop_years), ("deaths", death_years)):
        for y in years:
            for s in SEXES:
                st = open_start(getattr(data, which).get((y, s)))
                if st is None:
                    raise _gap(data.iso3, f"{which} {y} {s} has no usable single-age table with an open group",
                               input=which, year=y, sex=s)
                starts.append(st)
    for cells in extra:
        st = open_start(cells)
        if st is None:
            raise _gap(data.iso3, "comparison table has no usable single-age table with an open group")
        starts.append(st)
    omega = min([MAX_OPEN_AGE, *starts])
    if omega < MIN_OPEN_AGE:
        raise CapabilityGap(f"{data.iso3}: open age group starts at {omega} (< {MIN_OPEN_AGE}); a single-age "
                            "projection would need the open group to be split, which this model never does",
                            capability="demography.cohort_component.open_age", country=data.iso3, omega=omega)
    return omega


def _status_flags(table: Table, years: Iterable[int]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for y in years:
        seen = sorted({c.status for (yy, _s), cells in table.items() if yy == y for c in cells.values() if c.status})
        if seen:
            out[y] = seen
    return out


def base_rates(data: CountryDemography, first_year: int, last_year: int, *,
               extra_open_tables: Iterable[Mapping[str, Cell] | None] = (),
               migration_window: tuple[int, int] | None = None) -> BaseRates:
    """Base-period rates from events of calendar years first..last (populations first..last+1)."""
    if last_year < first_year:
        raise ValidationProblem("base period must contain at least one year")
    years = tuple(range(first_year, last_year + 1))
    mig_years = tuple(range(migration_window[0], migration_window[1] + 1)) if migration_window else years
    pop_years = sorted(set(years) | {y + 1 for y in years} | set(mig_years) | {y + 1 for y in mig_years})
    omega = determine_omega(data, pop_years, years, extra_open_tables)
    notes: list[str] = []
    # lower omega while exposure or open-group deaths are zero (small populations)
    while True:
        exp: dict[str, list[Decimal]] = {}
        dth: dict[str, list[Decimal]] = {}
        for s in SEXES:
            e = [ZERO] * (omega + 1)
            d = [ZERO] * (omega + 1)
            for t in years:
                p0, p1 = _vec(data, "population", t, s, omega), _vec(data, "population", t + 1, s, omega)
                dt = _vec(data, "deaths", t, s, omega)
                for x in range(omega + 1):
                    e[x] = xadd(e[x], mul(HALF, xadd(p0[x], p1[x])))
                    d[x] = xadd(d[x], dt[x])
            exp[s], dth[s] = e, d
        # zero exposure, an open group without deaths, or m_x >= 2 below the open group (q_x would
        # reach 1 and exhaust the life table: a small-number artefact) -> lower omega
        bad = [x for s in SEXES for x in range(omega + 1) if exp[s][x] <= 0] + \
              [omega for s in SEXES if dth[s][omega] <= 0] + \
              [x for s in SEXES for x in range(1, omega) if exp[s][x] > 0 and dth[s][x] >= 2 * exp[s][x]]
        if not bad:
            break
        new = min(bad) if min(bad) < omega else omega - 1
        if new < MIN_OPEN_AGE:
            raise CapabilityGap(f"{data.iso3}: zero base-period exposure/deaths at age {min(bad)} below "
                                f"{MIN_OPEN_AGE}", capability="demography.cohort_component.open_age",
                                country=data.iso3)
        notes.append(f"open age lowered from {omega} to {new}: zero base-period exposure, no open-group deaths "
                     "or a death rate >= 2 (small numbers)")
        omega = new
    lts = {s: life_table([div(dth[s][x], exp[s][x]) for x in range(omega + 1)], s) for s in SEXES}
    for s, lt in lts.items():
        if lt.capped_q:
            notes.append(f"{s}: q_x capped at 1 for ages {list(lt.capped_q)} (m_x >= 2)")
    # unknown-age population/deaths: never distributed; reported
    for t in years:
        for s in SEXES:
            for which in ("population", "deaths"):
                u = unknown_age(getattr(data, which).get((t, s)))
                if u is not None and u != 0:
                    notes.append(f"{which} {t} {s}: {dstr(u)} persons of unknown age excluded (absorbed by the "
                                 "migration residual)")
    # fertility
    num: dict[str, Decimal] = {}
    den: dict[str, Decimal] = {}
    groups: dict[str, FertilityGroup] = {}
    unk_b = ZERO
    unk_e = ZERO
    b_m = ZERO
    b_f = ZERO
    for t in years:
        cells = data.births.get((t, "T"))
        if not cells:
            raise _gap(data.iso3, f"births {t} by mother's age missing", input="births", year=t)
        f0 = _vec(data, "population", t, "F", omega)
        f1 = _vec(data, "population", t + 1, "F", omega)
        for g in fertility_groups(cells):
            v = cells[g.code].value
            if v is None:
                raise _gap(data.iso3, f"births {t} mother's age {g.code} missing", input="births", year=t)
            groups[g.code] = g
            num[g.code] = xadd(num.get(g.code, ZERO), v)
            den[g.code] = xadd(den.get(g.code, ZERO), mul(HALF, xadd(group_sum(f0, g), group_sum(f1, g))))
        u = unknown_age(cells)
        if u:
            unk_b = xadd(unk_b, u)
        unk_e = xadd(unk_e, mul(HALF, xadd(group_sum(f0, UNK_GROUP), group_sum(f1, UNK_GROUP))))
        bm = data.births.get((t, "M"), {}).get("TOTAL")
        bf = data.births.get((t, "F"), {}).get("TOTAL")
        if bm is None or bf is None or bm.value is None or bf.value is None:
            raise _gap(data.iso3, f"births {t} by newborn sex missing", input="births", year=t)
        b_m, b_f = xadd(b_m, bm.value), xadd(b_f, bf.value)
    for code in groups:
        if den[code] <= 0:
            raise _gap(data.iso3, f"zero female exposure for mother's age {code}")
    fert = tuple(sorted(((groups[c], div(num[c], den[c])) for c in groups), key=lambda gr: gr[0].start))
    if unk_b:
        notes.append(f"{dstr(unk_b)} births with unknown mother's age: separate rate per woman aged 15-49 "
                     "(never spread over ages)")
    male_share = div(b_m, xadd(b_m, b_f))
    # residual migration with base-period survival
    residual: dict[int, dict[str, list[Decimal]]] = {}
    for t in mig_years:
        residual[t] = {}
        for s in SEXES:
            p0 = _vec(data, "population", t, s, omega)
            p1 = _vec(data, "population", t + 1, s, omega)
            bt = data.births.get((t, s), {}).get("TOTAL")
            if bt is None or bt.value is None:
                raise _gap(data.iso3, f"births {t} {s} (all mothers) missing", input="births", year=t)
            nxt = age_forward(survive(p0, lts[s]))
            m = [xsub(p1[0], qp(mul(lts[s].s_birth, bt.value)))]
            m += [xsub(p1[x], nxt[x]) for x in range(1, omega + 1)]
            residual[t][s] = m
    flags = {
        "demo_pjan": _status_flags(data.population, pop_years),
        "demo_magec": _status_flags(data.deaths, years),
        "demo_fasec": _status_flags(data.births, years),
    }
    return BaseRates(data.iso3, omega, years, lts, fert, div(unk_b, unk_e) if unk_e > 0 else ZERO, male_share,
                     residual, tuple(notes), flags)


# --------------------------------------------------------------------------- assumptions
@dataclass(frozen=True)
class Assumptions:
    migration: str = "average_residual"
    fertility: str = "constant_base_period_rates"
    mortality: str = "constant_base_period_rates"
    base_period: tuple[int, int] = (0, 0)
    migration_window: tuple[int, int] | None = None  # default: the base period

    def __post_init__(self) -> None:
        if self.migration not in MIGRATION_OPTIONS:
            raise ValidationProblem(f"unknown migration assumption {self.migration!r}", allowed=MIGRATION_OPTIONS)
        if self.fertility not in FERTILITY_OPTIONS:
            raise ValidationProblem(f"unknown fertility assumption {self.fertility!r}", allowed=FERTILITY_OPTIONS)
        if self.mortality not in MORTALITY_OPTIONS:
            raise ValidationProblem(f"unknown mortality assumption {self.mortality!r}", allowed=MORTALITY_OPTIONS)

    @property
    def variant(self) -> str:
        return f"mig={self.migration};fert={self.fertility}"

    def as_json(self) -> dict[str, Any]:
        mw = self.migration_window or self.base_period
        return {
            "migration": self.migration, "fertility": self.fertility, "mortality": self.mortality,
            "base_period": {"first_year": self.base_period[0], "last_year": self.base_period[1]},
            "migration_window": {"first_year": mw[0], "last_year": mw[1]} if self.migration == "average_residual"
            else None,
            "labels": [ASSUMPTION_LABELS[f"migration:{self.migration}"],
                       ASSUMPTION_LABELS[f"fertility:{self.fertility}"],
                       ASSUMPTION_LABELS[f"mortality:{self.mortality}"]],
        }


def _europop_migration(data: CountryDemography, year: int, omega: int) -> dict[str, list[Decimal]]:
    out = {}
    for s in SEXES:
        v = to_vector(data.europop_migration.get((year, s)), omega)
        if v is None:
            raise CapabilityGap(f"{data.iso3}: no EUROPOP2023 net-migration assumption for {year}",
                                capability="demography.assumption.europop2023", country=data.iso3, year=year)
        out[s] = [qp(x) for x in v]
    return out


def _europop_fertility(data: CountryDemography, year: int) -> tuple[tuple[FertilityGroup, Decimal], ...]:
    cells = data.europop_fertility.get((year, "T"))
    if not cells:
        raise CapabilityGap(f"{data.iso3}: no EUROPOP2023 fertility assumption for {year}",
                            capability="demography.assumption.europop2023", country=data.iso3, year=year)
    out = []
    for g in fertility_groups(cells):
        v = cells[g.code].value
        if v is None:
            raise CapabilityGap(f"{data.iso3}: EUROPOP2023 fertility {year} {g.code} missing",
                                capability="demography.assumption.europop2023", country=data.iso3, year=year)
        out.append((g, v))
    return tuple(out)


def check_assumption_inputs(data: CountryDemography, a: Assumptions, jump_off: int, end_year: int,
                            omega: int) -> None:
    """Raise ``CapabilityGap`` when an assumption's own data (EUROPOP2023) is unavailable for a year."""
    for t in range(jump_off, end_year):
        if a.migration == "europop2023_bsl":
            _europop_migration(data, t, omega)
        if a.fertility == "europop2023_bsl":
            _europop_fertility(data, t)


def average_migration(rates: BaseRates, window: tuple[int, int]) -> dict[str, list[Decimal]]:
    years = [y for y in range(window[0], window[1] + 1)]
    missing = [y for y in years if y not in rates.residual_migration]
    if missing:
        raise ValidationProblem(f"migration window years {missing} have no residual estimate")
    n = Decimal(len(years))
    return {s: [qp(div(xsum(rates.residual_migration[y][s][x] for y in years), n))
                for x in range(rates.omega + 1)] for s in SEXES}


# --------------------------------------------------------------------------- projection
@dataclass(frozen=True)
class StepFlows:
    year: int  # flows during calendar year `year` (1 Jan year -> 1 Jan year+1)
    births: dict[str, Decimal]
    deaths: dict[str, Decimal]
    net_migration: dict[str, Decimal]
    migration_by_age: dict[str, tuple[Decimal, ...]]


@dataclass(frozen=True)
class Projection:
    """Result of :func:`project`. ``population[year][sex]`` is a tuple of ages 0..omega
    (index omega = open group omega+), on 1 January of ``year``; sexes ``"F"``, ``"M"``."""

    iso3: str
    jump_off: int
    end_year: int
    omega: int
    assumptions: Assumptions
    population: dict[int, dict[str, tuple[Decimal, ...]]]
    flows: dict[int, StepFlows]
    rates: BaseRates
    flags: tuple[dict[str, Any], ...]
    label: str = LABEL

    def total(self, year: int, sex: str | None = None, lo: int = 0, hi: int | None = None) -> Decimal:
        """Persons aged lo..hi-1 (hi None = to the open group) of ``sex`` (None = both)."""
        hi_ = self.omega + 1 if hi is None else hi
        if hi is not None and hi > self.omega:
            raise ValidationProblem(f"age bound {hi} exceeds the open age group {self.omega}+")
        sexes = SEXES if sex is None else (sex,)
        return xsum(self.population[year][s][x] for s in sexes for x in range(lo, hi_))

    def summary(self) -> dict[str, Any]:
        return {
            "model": f"{MODEL_KEY}@{MODEL_VERSION}", "label": self.label, "country": self.iso3,
            "jump_off": self.jump_off, "end_year": self.end_year, "open_age": self.omega,
            "assumptions": self.assumptions.as_json(), "numeric_type": NUMERIC_TYPE,
            "implied_tfr_base_period": dstr(_CTX.quantize(self.rates.tfr, Decimal("0.0001"))),
            "life_expectancy_at_birth_base_period": {s: dstr(_CTX.quantize(self.rates.life_tables[s].e0,
                                                                          Decimal("0.01"))) for s in SEXES},
            "male_share_at_birth": dstr(_CTX.quantize(self.rates.male_share_at_birth, Decimal("0.0001"))),
            "data_notes": list(self.rates.notes), "input_status_flags": self.rates.input_status_flags,
            "flags": list(self.flags),
        }

    def single_age_rows(self) -> list[dict[str, Any]]:
        """Single-age table (exact decimal strings) for a dataset output."""
        rows = []
        for y in sorted(self.population):
            for s in SEXES:
                for x, v in enumerate(self.population[y][s]):
                    rows.append({"iso3": self.iso3, "year": y, "sex": s, "age": x, "open_group": x == self.omega,
                                 "persons": dstr(v), "variant": self.assumptions.variant})
        return rows

    def single_age_table(self) -> Any:
        import pyarrow as pa

        rows = self.single_age_rows()
        schema = pa.schema([("iso3", pa.string()), ("year", pa.int32()), ("sex", pa.string()), ("age", pa.int32()),
                            ("open_group", pa.bool_()), ("persons", pa.string()), ("variant", pa.string())])
        return pa.table({k: [r[k] for r in rows] for k in schema.names}, schema=schema)


def project(data: CountryDemography, *, jump_off: int, end_year: int, assumptions: Assumptions,
            rates: BaseRates | None = None) -> Projection:
    """Project 1 January ``jump_off`` -> 1 January ``end_year`` under ``assumptions``.

    No information after the jump-off enters: the base period (and migration window) must
    end before ``jump_off``, so every event used happened before the jump-off date.
    """
    first, last = assumptions.base_period
    if not first or last >= jump_off:
        raise ValidationProblem("base period must end before the jump-off year (no information after the jump-off)",
                                base_period=[first, last], jump_off=jump_off)
    mw = assumptions.migration_window or assumptions.base_period
    if mw[1] >= jump_off:
        raise ValidationProblem("migration window must end before the jump-off year", migration_window=list(mw))
    if end_year <= jump_off:
        raise ValidationProblem("end_year must be after the jump-off year")
    if rates is None:
        rates = base_rates(data, first, last, migration_window=mw if assumptions.migration == "average_residual"
                           else None)
    omega = rates.omega
    pop: dict[int, dict[str, tuple[Decimal, ...]]] = {
        jump_off: {s: tuple(_vec(data, "population", jump_off, s, omega)) for s in SEXES}}
    fixed_mig = None
    if assumptions.migration == "average_residual":
        fixed_mig = average_migration(rates, mw)
    elif assumptions.migration == "zero":
        fixed_mig = {s: [ZERO] * (omega + 1) for s in SEXES}
    flows: dict[int, StepFlows] = {}
    flags: list[dict[str, Any]] = []
    for t in range(jump_off, end_year):
        p = pop[t]
        mig = fixed_mig if fixed_mig is not None else _europop_migration(data, t, omega)
        fert = rates.fertility if assumptions.fertility == "constant_base_period_rates" else \
            _europop_fertility(data, t)
        unk_rate = rates.unknown_age_birth_rate if assumptions.fertility == "constant_base_period_rates" else ZERO
        nxt: dict[str, list[Decimal]] = {}
        deaths: dict[str, Decimal] = {}
        for s in SEXES:
            surv = survive(p[s], rates.life_tables[s])
            deaths[s] = xsum(xsub(p[s][x], surv[x]) for x in range(omega + 1))
            fwd = age_forward(surv)
            nxt[s] = [ZERO] + [xadd(fwd[x], mig[s][x]) for x in range(1, omega + 1)]
        f0, f1 = p["F"], nxt["F"]
        b = csum(mul(r, mul(HALF, xadd(group_sum(f0, g), group_sum(f1, g)))) for g, r in fert)
        b = _CTX.add(b, mul(unk_rate, mul(HALF, xadd(group_sum(f0, UNK_GROUP), group_sum(f1, UNK_GROUP)))))
        births_total = qp(b)
        births = {"M": qp(mul(births_total, rates.male_share_at_birth))}
        births["F"] = xsub(births_total, births["M"])
        for s in SEXES:
            infants = qp(mul(rates.life_tables[s].s_birth, births[s]))
            deaths[s] = xadd(deaths[s], xsub(births[s], infants))
            nxt[s][0] = xadd(infants, mig[s][0])
        netmig = {s: xsum(mig[s]) for s in SEXES}
        for s in SEXES:  # exact balancing equation, every step
            lhs = xsum(nxt[s])
            rhs = xadd(xsub(xsum(p[s]), deaths[s]), births[s], netmig[s])
            if lhs != rhs:
                raise AssertionError(f"conservation violated {data.iso3} {t} {s}: {lhs} != {rhs}")
            for x, v in enumerate(nxt[s]):
                if v < 0:  # kept (clipping would break the balancing equation) and flagged
                    origin = [x - 1] if 0 < x < omega else ([omega - 1, omega] if x == omega else [])
                    inherited = any(p[s][o] < 0 for o in origin) or (x == 0 and births[s] < 0)
                    cause = ("negative net migration larger than the surviving cohort" if mig[s][x] < 0 else
                             "inherited from a negative cohort" if inherited else "unexplained")
                    flags.append({"code": "negative_cohort", "year": t + 1, "sex": s, "age": x,
                                  "persons": dstr(v), "net_migration": dstr(mig[s][x]), "cause": cause})
        pop[t + 1] = {s: tuple(nxt[s]) for s in SEXES}
        flows[t] = StepFlows(t, births, deaths, netmig, {s: tuple(mig[s]) for s in SEXES})
    return Projection(data.iso3, jump_off, end_year, omega, assumptions, pop, flows, rates, tuple(flags))


def reconstruct(data: CountryDemography, rates: BaseRates, first: int,
                last: int) -> dict[int, dict[str, list[Decimal]]]:
    """Re-run years first..last with observed births and the residual migration of each year;
    returns the reconstructed 1 January populations (must equal the observed ones exactly)."""
    omega = rates.omega
    out: dict[int, dict[str, list[Decimal]]] = {}
    p = {s: _vec(data, "population", first, s, omega) for s in SEXES}
    for t in range(first, last + 1):
        nxt = {}
        for s in SEXES:
            fwd = age_forward(survive(p[s], rates.life_tables[s]))
            bt = data.births[(t, s)]["TOTAL"].value
            mig = rates.residual_migration[t][s]
            row = [xadd(qp(mul(rates.life_tables[s].s_birth, bt)), mig[0])]  # type: ignore[arg-type]
            row += [xadd(fwd[x], mig[x]) for x in range(1, omega + 1)]
            nxt[s] = row
        out[t + 1] = nxt
        p = nxt
    return out


# --------------------------------------------------------------------------- outputs
BROAD_GROUPS: dict[str, tuple[int, int | None]] = {
    "all_ages": (0, None), "age_0_14": (0, 15), "age_15_64": (15, 65), "age_65_plus": (65, None),
    "age_80_plus": (80, None), "age_20_66": (20, 67), "age_67_plus": (67, None),
}
SEX_KEYS = {"total": None, "female": "F", "male": "M"}
DEPENDENCY_RATIOS = {
    "old_age_dependency_ratio_65plus_per_15_64": ("age_65_plus", "age_15_64"),
    "old_age_dependency_ratio_67plus_per_20_66": ("age_67_plus", "age_20_66"),
}
FLOW_KEYS = ("births", "deaths", "net_migration")


def output_values(proj: Projection) -> dict[tuple[str, int], Decimal]:
    """Unrounded outputs keyed by (output_key, year): stocks on 1 January, flows during the year,
    dependency ratios in percent (persons 65+ per 100 persons 15-64, etc.)."""
    out: dict[tuple[str, int], Decimal] = {}
    for y in sorted(proj.population):
        for sk, s in SEX_KEYS.items():
            for gk, (lo, hi) in BROAD_GROUPS.items():
                out[(f"population.{sk}.{gk}", y)] = proj.total(y, s, lo, hi)
        for rk, (num, den) in DEPENDENCY_RATIOS.items():
            n = out[(f"population.total.{num}", y)]
            d = out[(f"population.total.{den}", y)]
            out[(rk, y)] = div(mul(Decimal(100), n), d)
    for y, f in sorted(proj.flows.items()):
        for k in FLOW_KEYS:
            vals = getattr(f, k)
            out[(f"{k}.total", y)] = xadd(vals["F"], vals["M"])
            out[(f"{k}.female", y)] = vals["F"]
            out[(f"{k}.male", y)] = vals["M"]
    return out


def output_keys() -> list[str]:
    keys = [f"population.{sk}.{gk}" for sk in SEX_KEYS for gk in BROAD_GROUPS]
    keys += list(DEPENDENCY_RATIOS)
    keys += [f"{k}.{s}" for k in FLOW_KEYS for s in ("total", "female", "male")]
    return keys


# --------------------------------------------------------------------------- back-test
COMPARISON_GROUPS: dict[str, tuple[int, int | None]] = {
    "age_0_14": (0, 15), "age_15_64": (15, 65), "age_65_plus": (65, None), "all_ages": (0, None),
}


def _ape(pred: Decimal, obs: Decimal) -> Decimal | None:
    if obs == 0:
        return None
    return div(mul(Decimal(100), _CTX.abs(_CTX.subtract(pred, obs))), obs)


def backtest(data: CountryDemography, *, jump_off: int, target_year: int, base_period: tuple[int, int],
             migrations: Sequence[str] = ("average_residual", "zero")) -> dict[str, Any]:
    """Project from observed ``jump_off`` with rates from ``base_period`` (events before the
    jump-off only) and compare with observed demo_pjan of ``target_year``. Nothing from after
    the jump-off is used to fit anything (no tuning). Metrics (percent):

    * ``group_ape``: absolute percentage error of each broad age group total (both sexes);
    * ``group_mape_single_age``: mean over the single-age x sex cells of the group of the
      absolute percentage error (cells with observed 0 excluded, count reported);
    * ``total_ape``: APE of the total population; ``naive_total_ape``: APE of the
      no-change benchmark (target population = jump-off population).
    """
    target_cells = [data.population.get((target_year, s)) for s in SEXES]
    rates = base_rates(data, base_period[0], base_period[1], extra_open_tables=target_cells)
    omega = rates.omega
    observed = {s: _vec(data, "population", target_year, s, omega) for s in SEXES}
    start = {s: _vec(data, "population", jump_off, s, omega) for s in SEXES}
    result: dict[str, Any] = {
        "country": data.iso3, "jump_off": jump_off, "target_year": target_year,
        "base_period": list(base_period), "open_age": omega, "variants": {},
        "data_notes": list(rates.notes), "input_status_flags": rates.input_status_flags,
        "target_status_flags": _status_flags(data.population, [target_year]),
        # reported, never used: breaks/estimates in the test period explain part of the error
        "test_period_status_flags": _status_flags(data.population, range(jump_off + 1, target_year + 1)),
    }
    obs_total = xsum(observed[s][x] for s in SEXES for x in range(omega + 1))
    naive = xsum(start[s][x] for s in SEXES for x in range(omega + 1))
    for mig in migrations:
        a = Assumptions(migration=mig, base_period=base_period)
        proj = project(data, jump_off=jump_off, end_year=target_year, assumptions=a, rates=rates)
        pred = proj.population[target_year]
        groups: dict[str, Any] = {}
        for gk, (lo, hi) in COMPARISON_GROUPS.items():
            hi_ = omega + 1 if hi is None else hi
            cells = [(pred[s][x], observed[s][x]) for s in SEXES for x in range(lo, hi_)]
            apes = [e for e in (_ape(p, o) for p, o in cells) if e is not None]
            g_pred = xsum(p for p, _ in cells)
            g_obs = xsum(o for _, o in cells)
            groups[gk] = {
                "predicted": dstr(_CTX.quantize(g_pred, Decimal(1))),
                "observed": dstr(g_obs),
                "ape": dstr(_CTX.quantize(_ape(g_pred, g_obs) or ZERO, Decimal("0.001"))),
                "mape_single_age_sex": dstr(_CTX.quantize(div(csum(apes), Decimal(len(apes))),
                                                          Decimal("0.001"))) if apes else None,
                "cells": len(apes), "cells_with_zero_observed": len(cells) - len(apes),
            }
        result["variants"][a.variant] = {
            "assumptions": a.as_json(), "groups": groups,
            "total_ape": groups["all_ages"]["ape"],
            "negative_cohorts": len(proj.flags),
        }
    result["naive_no_change_total_ape"] = dstr(_CTX.quantize(_ape(naive, obs_total) or ZERO, Decimal("0.001")))
    return result


def implementation_digest() -> str:
    h = hashlib.sha256()
    p = Path(__file__)
    h.update(p.name.encode())
    h.update(p.read_bytes())
    return f"sha256:{h.hexdigest()}"
