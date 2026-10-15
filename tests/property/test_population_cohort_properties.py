"""Property tests (Hypothesis) of the cohort-component projection (demography.cohort_component).

* the balancing equation P(t+1) = P(t) - deaths + births + net migration holds exactly, by sex,
  in every projected year (Decimal, no tolerance);
* reconstructing the base period with its own residual migration reproduces the observed
  populations exactly;
* a negative cohort appears only with negative net migration (or inherited from one), is
  always flagged, and never appears under zero migration with non-negative inputs.
"""

from __future__ import annotations

import random
from decimal import Decimal
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from reva.models.population import cohort as C

OPEN = 90  # synthetic tables: single ages 0..89 and Y_OPEN (90+)
YEARS = range(2010, 2017)  # populations 2010..2016, events 2010..2015


def _row(ds: str, year: int, sex: str, age: str, kind: str, start: int | None, value: int) -> dict[str, Any]:
    return {"dataset": ds, "year": year, "sex": sex, "age": age, "age_kind": kind, "age_start": start,
            "age_end": None if start is None else start + 1, "cell_type": "leaf", "value": str(value), "status": ""}


def synthetic(seed: int, scale: int, shock: int) -> C.CountryDemography:
    rnd = random.Random(seed)
    pop, deaths, births = [], [], []
    for y in YEARS:
        for s in ("F", "M"):
            for a in range(OPEN + 1):
                base = scale * (1000 - 8 * a) if a < OPEN else scale * 300
                v = max(1, base + rnd.randint(-shock, shock) * scale)
                code, kind, start = (f"Y{a}", "single", a) if a < OPEN else ("Y_OPEN", "open", None)
                pop.append(_row("demo_pjan", y, s, code, kind, start, v))
                d = max(1, int(v * (0.0005 + a * a * 0.00002))) if a < OPEN else max(1, v // 4)
                deaths.append(_row("demo_magec", y, s, code, kind, start, d))
        tot = 0
        for a in range(15, 50):
            b = scale * rnd.randint(5, 80)
            tot += b
            births.append(_row("demo_fasec", y, "T", f"Y{a}", "single", a, b))
        m = tot // 2 + rnd.randint(0, 9)
        births.append(_row("demo_fasec", y, "M", "TOTAL", "total", None, m))
        births.append(_row("demo_fasec", y, "F", "TOTAL", "total", None, tot - m))
    return C.CountryDemography.from_tables("TST", population=pop, deaths=deaths, births=births)


@settings(max_examples=25, deadline=None)
@given(seed=st.integers(0, 10_000), scale=st.integers(1, 50), shock=st.integers(0, 900),
       migration=st.sampled_from(["zero", "average_residual"]), horizon=st.integers(1, 12))
def test_population_balancing_equation_exact_and_negative_cohorts_flagged(
        seed: int, scale: int, shock: int, migration: str, horizon: int) -> None:
    data = synthetic(seed, scale, shock)
    a = C.Assumptions(migration=migration, base_period=(2010, 2014))
    p = C.project(data, jump_off=2015, end_year=2015 + horizon, assumptions=a)
    negative = set()
    for t in range(2015, 2015 + horizon):
        f = p.flows[t]
        for s in C.SEXES:
            lhs = C.xsum(p.population[t + 1][s])
            rhs = C.xadd(C.xsub(C.xsum(p.population[t][s]), f.deaths[s]), f.births[s], f.net_migration[s])
            assert lhs == rhs  # exact: no tolerance
            negative |= {(t + 1, s, x) for x, v in enumerate(p.population[t + 1][s]) if v < 0}
    flagged = {(fl["year"], fl["sex"], fl["age"]) for fl in p.flags}
    assert flagged == negative
    assert all(fl["cause"] != "unexplained" for fl in p.flags)
    if migration == "zero":
        assert not p.flags  # non-negative inputs and no migration: never a negative cohort
    for fl in p.flags:
        if fl["cause"].startswith("negative net migration"):
            assert Decimal(fl["net_migration"]) < 0


@settings(max_examples=15, deadline=None)
@given(seed=st.integers(0, 10_000), scale=st.integers(1, 50), shock=st.integers(0, 900))
def test_population_residual_reconstruction_is_exact(seed: int, scale: int, shock: int) -> None:
    data = synthetic(seed, scale, shock)
    rates = C.base_rates(data, 2010, 2014)
    assert rates.omega == OPEN
    for y, by_sex in C.reconstruct(data, rates, 2010, 2014).items():
        for s, vec in by_sex.items():
            assert vec == C._vec(data, "population", y, s, OPEN)
