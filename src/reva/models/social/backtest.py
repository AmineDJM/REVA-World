"""Back-test of ``social.retirement_age`` on the French 2010 reform (pre-registered, no tuning).

Protocol (fixed before looking at the test years; see docs/dev/v2_pen_backtest.md)
---------------------------------------------------------------------------------
* Information set: Eurostat LFS employment and activity rates of France for 2010 (the base
  year) - only rows with ``year <= 2010`` are handed to the prediction function - plus the
  FRA rule table (law as applied: Loi 2010-1330 accelerated by LFSS 2012, path
  ``law_2010_as_amended_2011``; reference ``pre_2010_counterfactual``) and the literature
  grid. Using the as-applied schedule is a statement about the policy input, not about
  the outcome; the as-enacted November 2010 schedule (4 months per cohort) is not in the
  archived sources and is not used.
* Prediction for ages 60-64, sex s, year t in 2012..2019:
  ``e(t) = e(2010) + 100 * s_g * rho(s) * I(60-64, t)``, with ``I`` the exact share of
  person-time below the minimum age (uniform weights over single ages 60-64: no post-2010
  population data), ``rho(s) = 1 - activity_rate(60-64, s, 2010)/100`` (French data) and
  ``s_g`` the literature share for grid point ``g``.
* Grid: every verified literature point **except** estimates whose estimation sample
  overlaps the reform being tested (any French estimate - Rabaté & Rochut estimate the
  2010 reform itself). Pre-registered central point: Staubli & Zweimüller (2013, AUT).
  Benchmark: persistence (``s = 0``, the 2010 rate carried forward).
* Errors are reported per year and sex: predicted - observed (percentage points), plus
  mean error and mean absolute error over 2012-2019. Observed values carry their Eurostat
  flags (France has a break in series in 2014: flag ``b``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from reva.kernel.errors import ValidationProblem
from reva.kernel.ids import logical_hash
from reva.models.social import assumptions as lit
from reva.models.social import rules as rl
from reva.models.social.retirement_age import CTX, dstr, fr

PROTOCOL_VERSION = "reva-pen-backtest-fra-2010@1.0.0"
COUNTRY = "FRA"
BASE_YEAR = 2010
TEST_YEARS = tuple(range(2012, 2020))
GROUP = ("Y60-64", 60, 64)
SCENARIO_PATH = "law_2010_as_amended_2011"
REFERENCE_PATH = "pre_2010_counterfactual"
CENTRAL = "staubli_zweimuller_2013_aut"
SEXES = ("F", "M")


def backtest_grid(grid: lit.AssumptionGrid) -> tuple[tuple[lit.GridPoint, ...], tuple[dict[str, Any], ...]]:
    """Literature points allowed in the back-test and the excluded ones with reasons."""
    keep, out = [], []
    for p in grid.literature():
        if p.overlaps(COUNTRY, BASE_YEAR, TEST_YEARS[-1]):
            out.append({"point_id": p.point_id, "reason": f"estimation sample overlaps the {COUNTRY} reform/period "
                        f"being back-tested ({BASE_YEAR}-{TEST_YEARS[-1]}): leakage"})
        else:
            keep.append(p)
    return tuple(keep), tuple(out)


def _rate(rows: Sequence[Mapping[str, Any]], year: int, sex: str) -> tuple[Decimal, str]:
    for r in rows:
        if r["year"] == year and r["dim1"] == sex and r["dim2"] == GROUP[0]:
            if r["value"] is None:
                raise ValidationProblem(f"rate missing for {sex} {GROUP[0]} {year}")
            return Decimal(r["value"]), str(r.get("status") or "")
    raise ValidationProblem(f"no row for {sex} {GROUP[0]} {year}")


def placebo(emp_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Ages 55-59 are below the minimum age under both paths, so the model predicts no reform effect
    there: the observed 2010 -> t change of that group shows how much of the 60-64 change could be a
    trend the model does not represent (diagnostic only, never used to adjust predictions)."""
    out: dict[str, Any] = {}
    for s in SEXES:
        base = next(Decimal(r["value"]) for r in emp_rows if r["year"] == BASE_YEAR and r["dim1"] == s
                    and r["dim2"] == "Y55-59")
        per = []
        for t in TEST_YEARS:
            r = next(r for r in emp_rows if r["year"] == t and r["dim1"] == s and r["dim2"] == "Y55-59")
            per.append({"year": t, "predicted": dstr(base), "observed": r["value"], "observed_flag": r["status"],
                        "error_pp": dstr(base - Decimal(r["value"]))})
        out[s] = per
    return out


def predict(train_emp: Sequence[Mapping[str, Any]], train_act: Sequence[Mapping[str, Any]], table: rl.RuleTable,
            points: Sequence[lit.GridPoint]) -> dict[str, dict[str, dict[int, Decimal]]]:
    """point_id -> sex -> year -> predicted employment rate (percent). Uses only ``year <= BASE_YEAR`` rows."""
    for rows in (train_emp, train_act):
        late = sorted({r["year"] for r in rows if r["year"] > BASE_YEAR})
        if late:
            raise ValidationProblem(f"training data contains years after {BASE_YEAR}: {late} (leakage)")
    scn = rl.TablePath(table, SCENARIO_PATH, "minimum_pension_age")
    ref = rl.TablePath(table, REFERENCE_PATH, "minimum_pension_age")
    ages = range(GROUP[1], GROUP[2] + 1)
    i_base = rl.ineligible_share(ref, BASE_YEAR, ages)
    if i_base != rl.ineligible_share(scn, BASE_YEAR, ages):
        raise ValidationProblem("scenario and reference differ in the base year")
    shares = {t: fr(rl.ineligible_share(scn, t, ages) - i_base) for t in TEST_YEARS}
    out: dict[str, dict[str, dict[int, Decimal]]] = {}
    for p in points:
        out[p.point_id] = {}
        for s in SEXES:
            e0, _ = _rate(train_emp, BASE_YEAR, s)
            a0, _ = _rate(train_act, BASE_YEAR, s)
            rho = Decimal(1) - CTX.divide(a0, Decimal(100))
            k = CTX.multiply(Decimal(100), CTX.multiply(p.share_employed[s], rho))
            out[p.point_id][s] = {t: e0 + CTX.multiply(k, shares[t]) for t in TEST_YEARS}
    return out


def run(emp_rows: Sequence[Mapping[str, Any]], act_rows: Sequence[Mapping[str, Any]],
        grid: lit.AssumptionGrid | None = None, table: rl.RuleTable | None = None) -> dict[str, Any]:
    grid = grid or lit.load_grid()
    table = table or rl.load_rule_table(COUNTRY)
    points, excluded = backtest_grid(grid)
    if CENTRAL not in {p.point_id for p in points}:
        raise ValidationProblem(f"pre-registered central point {CENTRAL} is not in the back-test grid")
    persistence = lit.GridPoint(point_id="benchmark_persistence", label="Persistence: 2010 rate carried forward",
                                share_employed={s: Decimal(0) for s in SEXES},
                                pension_drop={s: Decimal(0) for s in SEXES}, country=None, doi=None,
                                citation="benchmark (no reform effect)", kind="reference")
    train_emp = [r for r in emp_rows if r["year"] <= BASE_YEAR]
    train_act = [r for r in act_rows if r["year"] <= BASE_YEAR]
    preds = predict(train_emp, train_act, table, [*points, persistence])
    observed = {s: {t: _rate(emp_rows, t, s) for t in TEST_YEARS} for s in SEXES}
    results: dict[str, Any] = {}
    for pid, by_sex in preds.items():
        res: dict[str, Any] = {}
        for s in SEXES:
            per_year = []
            for t in TEST_YEARS:
                obs, flag = observed[s][t]
                err = by_sex[s][t] - obs
                per_year.append({"year": t, "predicted": dstr(CTX.quantize(by_sex[s][t], Decimal("0.01"))),
                                 "observed": dstr(obs), "observed_flag": flag,
                                 "error_pp": dstr(CTX.quantize(err, Decimal("0.01")))})
            errs = [by_sex[s][t] - observed[s][t][0] for t in TEST_YEARS]
            n = Decimal(len(errs))
            res[s] = {"per_year": per_year,
                      "mean_error_pp": dstr(CTX.quantize(CTX.divide(sum(errs, Decimal(0)), n), Decimal("0.01"))),
                      "mean_absolute_error_pp": dstr(CTX.quantize(CTX.divide(sum((abs(e) for e in errs), Decimal(0)),
                                                                             n), Decimal("0.01")))}
        results[pid] = res
    base = {s: {"employment_rate_2010": dstr(_rate(train_emp, BASE_YEAR, s)[0]),
                "activity_rate_2010": dstr(_rate(train_act, BASE_YEAR, s)[0])} for s in SEXES}
    ages = range(GROUP[1], GROUP[2] + 1)
    scn = rl.TablePath(table, SCENARIO_PATH, "minimum_pension_age")
    report = {
        "protocol": PROTOCOL_VERSION,
        "country": COUNTRY, "age_group": GROUP[0], "base_year": BASE_YEAR, "test_years": list(TEST_YEARS),
        "scenario_path": SCENARIO_PATH, "reference_path": REFERENCE_PATH,
        "rule_table_digest": table.digest(), "assumption_registry_digest": grid.source_digest,
        "central_point": CENTRAL, "benchmark": "benchmark_persistence",
        "grid_points": [p.as_dict() for p in points], "excluded_points": list(excluded),
        "ineligible_share_60_64": {str(t): dstr(CTX.quantize(fr(rl.ineligible_share(scn, t, ages)), Decimal("0.0001")))
                                   for t in TEST_YEARS},
        "base_data": base,
        "results": results,
        "placebo_55_59": placebo(emp_rows),
        "weights": "uniform over single ages 60-64 (no post-2010 population used)",
        "label": "out-of-sample back-test of a conditional projection; no parameter was fitted on 2011-2019",
    }
    report["digest"] = logical_hash({k: v for k, v in report.items() if k != "digest"})
    return report
