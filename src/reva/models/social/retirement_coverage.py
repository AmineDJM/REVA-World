"""``social.retirement_age@1.2.0`` - the world retirement-age module with pension coverage (V3 agent SIMFIX).

Version 1.1.0 (``retirement_world.py``) treats every person of an affected age as subject to the legal
pension age. Where the pension system covers a small share of the population (informal economies) its
employment and pension-receipt changes are overstated: e.g. India, target age 62 from the 1965 cohort,
2030: +10.6 M employed and -24.2 M persons in pension receipt. Version 1.2.0 runs the same eligibility
arithmetic, inputs and cited response grid as 1.1.0 and applies the legal-age change **only to the
pension-covered share** of each (sex, single age) cell:

* **Coverage input**: ILOSTAT SDG indicator 1.3.1, function ``SOC_CONTIG_PENSION`` - "Persons above
  retirement age receiving a pension" (%, connector ``ilostat.social_protection``, CC BY 4.0, ILO
  Social Security Inquiry), of the latest year <= ``reference_year`` (at most
  :data:`MAX_COVERAGE_AGE` years older) with a value for both sexes or for the total. By sex when
  ILOSTAT gives F and M for that year, else the total applied to both sexes (stated per country).
  The source's own value is used; no coverage variant or grid is invented (the L2 range does not move
  coverage). A year older than the reference year is stated in every row; newer years in the source
  that the reference year excludes are listed.
* **Method**: with ``c`` the coverage share of the sex, ``A = N (I_scn - I_ref)`` as in 1.1.0;
  employment change ``c s rho A``; pension-receipt change ``-c rho A``; employment-rate levels
  ``e = e_obs + c s rho (I - I_obs)``. The uncovered share ``1 - c`` keeps the observed rates (no
  legal pension age binds it).
* **Refusal**: a country without a usable coverage value is refused with ``missing_input`` (never
  assumed to be 100 %); under ``skip_missing_countries`` it is listed and skipped.

Stated assumptions (in every row): coverage measured among persons *above* pensionable age today is
applied unchanged to persons aged 55-64 in every projection year; uniform across single ages 55-64
(the source has no age breakdown); covered and uncovered persons share the band's employment rate,
participation-based rho and response share. SDG 1.3.1 counts contributory **and non-contributory**
pensions; where a social pension with its own age (e.g. a universal pension from 65) drives coverage,
``c`` overstates the share bound by the rule table's contributory age, so results there can still be
overstated. The direction of the coverage proxy is otherwise unknown (contributory coverage of today's
55-64 year-olds may differ from today's pensioners'). A conditional projection under stated
assumptions, not a forecast; experimental, review pending.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from reva.kernel.errors import CapabilityGap, ValidationProblem
from reva.kernel.ids import logical_hash
from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec, UncertaintyStatus
from reva.models.social import assumptions as lit
from reva.models.social import retirement_age as ra
from reva.models.social import retirement_world as rw
from reva.models.social import rules as rl

MODEL_KEY = rw.MODEL_KEY
MODEL_VERSION = "1.2.0"
REVIEW_STATUS = rw.REVIEW_STATUS
LABEL = rw.LABEL
EXPERIMENTAL_LABEL = rw.EXPERIMENTAL_LABEL
SEXES = rw.SEXES
AGES = rw.AGES
AGE_GROUPS = rw.AGE_GROUPS
PENSION_DROP_BASES = rw.PENSION_DROP_BASES
MAX_RATES_AGE = rw.MAX_RATES_AGE
MAX_COVERAGE_AGE = 10
OLD_COVERAGE_YEARS = 5
CTX = rw.CTX
SENSITIVITY_VARIANT = rw.SENSITIVITY_VARIANT
SENSITIVITY_METHOD = "reva.models.social.retirement_coverage.l2_literature_grid_minmax@1.2.0"
COVERAGE_CONCEPT = "social_protection.ilostat.old_age_pension_coverage.sex"
COVERAGE_SERIES = "ILOSTAT SDG_0131_SEX_SOC_RT_A, function SOC_CONTIG_PENSION (SDG indicator 1.3.1)"
CONCEPTS = {**rw.CONCEPTS, "pension_coverage": COVERAGE_CONCEPT}
DATA_SOURCES = {
    **rw.DATA_SOURCES,
    "pension_coverage": (
        "ILOSTAT SDG indicator 1.3.1, persons above retirement age receiving a pension (%), by sex, ILO Social "
        "Security Inquiry (connector ilostat.social_protection, CC BY 4.0)"
    ),
}
PENSION_DROP_LABEL = (
    "pension-receipt drop rho = share of the five-year age band outside the labour force in the rates year "
    "(ILOSTAT): a proxy of unknown direction (includes non-pensioner inactivity, omits unemployed claimants); "
    "applied to the pension-covered share only"
)
COVERAGE_LABEL = (
    "pension coverage c = ILOSTAT SDG 1.3.1 'persons above retirement age receiving a pension' (source's own "
    "value): the legal-age change is applied to share c of every (sex, single age 55-64) cell, the uncovered "
    "share keeps the observed rates; c is measured among persons above pensionable age today and applied "
    "unchanged to persons aged 55-64 in every projection year, uniformly across ages (no age breakdown in the "
    "source); c counts contributory and non-contributory pensions, so where a social pension with its own age "
    "drives coverage it overstates the share bound by the rule table's age (results then still overstated); "
    "no coverage variant is evaluated"
)
OUTPUTS: dict[str, dict[str, str]] = {
    "employed_persons_55_64": {
        "unit": "unit.persons",
        "kind": "level",
        "label": "Employed persons aged 55-64 under the scenario rule path (legal-age change applied to the "
        "pension-covered share)",
    },
    "employed_persons_55_64_reference": {
        "unit": "unit.persons",
        "kind": "level",
        "label": "Employed persons aged 55-64 under the reference rule path (pension-covered share)",
    },
    "employment_change_55_64": {
        "unit": "unit.persons",
        "kind": "change",
        "label": "Employed persons aged 55-64: scenario minus reference (pension-covered share only)",
    },
    "pension_recipients_change_55_64": {
        "unit": "unit.persons",
        "kind": "change",
        "label": "Persons aged 55-64 kept out of (negative) or brought into (positive) pension receipt: scenario "
        "minus reference (rho proxy, pension-covered share only)",
    },
    "old_age_dependency_ratio": dict(rw.OUTPUTS["old_age_dependency_ratio"]),
}
for _sex in SEXES:
    for _g, _lo, _hi in AGE_GROUPS:
        OUTPUTS[f"employment_rate.{_sex}.{_g}"] = {
            "unit": "unit.percent",
            "kind": "level",
            "label": f"Employment rate (percent), recorded sex {_sex}, ages {_lo}-{_hi}, scenario rule path "
            "(legal-age change applied to the pension-covered share)",
        }
assert set(OUTPUTS) == set(rw.OUTPUTS)
DEFAULT_OUTPUTS = rw.DEFAULT_OUTPUTS
NOT_AVAILABLE = rw.NOT_AVAILABLE
NOT_CLAIMED = (
    *rw.NOT_CLAIMED[:-1],
    "the share of today's 55-64 year-olds covered by the contributory scheme of the rule table (coverage is "
    "measured among persons above pensionable age and includes non-contributory pensions)",
    "coverage by single age or a coverage trend (the source's latest value is held constant)",
    "a forecast of what will happen",
)


# --------------------------------------------------------------------------- coverage input
@dataclass(frozen=True)
class Coverage:
    """The coverage value a country's run uses (source's own value; never assumed)."""

    year: int
    share: dict[str, Decimal]  # recorded sex -> proportion in [0, 1]
    percent: dict[str, str]  # recorded sex -> verbatim ILOSTAT value used
    breakdown: str  # "by_sex" | "total_applied_to_both_sexes"
    total_percent: str | None
    sources: tuple[str, ...]
    status: dict[str, str]
    notes: tuple[dict[str, Any], ...]
    reference_year: int
    latest_year_in_source: int
    newer_years_not_used: tuple[int, ...]

    @property
    def years_before_reference_year(self) -> int:
        return self.reference_year - self.year

    def as_json(self) -> dict[str, Any]:
        return {
            "series": COVERAGE_SERIES,
            "concept": COVERAGE_CONCEPT,
            "year": self.year,
            "reference_year": self.reference_year,
            "years_before_reference_year": self.years_before_reference_year,
            "latest_year_in_source": self.latest_year_in_source,
            "newer_years_not_used": list(self.newer_years_not_used),
            "breakdown": self.breakdown,
            "percent_by_sex_used": dict(self.percent),
            "total_percent": self.total_percent,
            "sources": list(self.sources),
            "status_codes": dict(self.status),
            "notes": [dict(n) for n in self.notes],
            "assumption": COVERAGE_LABEL,
        }

    def caveats(self) -> list[str]:
        out = [COVERAGE_LABEL]
        if self.breakdown != "by_sex":
            out.append(
                f"pension coverage: ILOSTAT gives no sex breakdown for {self.year}; the total "
                f"({self.total_percent} %) is applied to both sexes"
            )
        if self.years_before_reference_year > 0:
            old = " (old)" if self.years_before_reference_year >= OLD_COVERAGE_YEARS else ""
            out.append(
                f"pension coverage of {self.year}{old}, {self.years_before_reference_year} years before the reference "
                f"year {self.reference_year}, held constant"
            )
        if self.newer_years_not_used:
            out.append(
                f"pension coverage for {list(self.newer_years_not_used)} exists in the source but is later than the "
                f"reference year {self.reference_year} (not used)"
            )
        for n in self.notes:
            out.append(f"ILOSTAT coverage note {n['code']}: {n['label'] or 'unreviewed note code'}")
        for k, st in self.status.items():
            out.append(f"ILOSTAT coverage status {k}: {st}")
        return out


def coverage_cells(rows: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, dict[str, Any]]]:
    """``{year: {sex: row}}`` of the old-age pension rows with a value (F, M, T)."""
    out: dict[int, dict[str, dict[str, Any]]] = {}
    for r in rows:
        if r.get("sex") in ("F", "M", "T") and r.get("value") is not None:
            out.setdefault(int(r["year"]), {})[str(r["sex"])] = dict(r)
    return out


def select_coverage(iso3: str, rows: Sequence[Mapping[str, Any]], b: int) -> Coverage | dict[str, Any]:
    """The latest year <= ``b`` (at most :data:`MAX_COVERAGE_AGE` years older) with F and M, or T; else a
    typed problem (``missing_input`` or ``inconsistent_input``). Never a default value."""
    cells = coverage_cells(rows)
    usable = [y for y, c in cells.items() if b - MAX_COVERAGE_AGE <= y <= b and ({"F", "M"} <= set(c) or "T" in c)]
    if not usable:
        before = [y for y in cells if y <= b]
        return {
            "code": "missing_input",
            "country": iso3,
            "input": "pension_coverage",
            "concept": COVERAGE_CONCEPT,
            "kind": "dataset",
            "reason": (
                f"no ILOSTAT SDG 1.3.1 old-age pension coverage value in [{b - MAX_COVERAGE_AGE}, {b}]; "
                "coverage is never assumed (not 100 %)"
            ),
            "latest_coverage_year_before_reference": max(before, default=None),
            "years_in_source": sorted(cells),
        }
    y = max(usable)
    c = cells[y]
    if {"F", "M"} <= set(c):
        used = {s: c[s] for s in SEXES}
        breakdown = "by_sex"
    else:
        used = {s: c["T"] for s in SEXES}
        breakdown = "total_applied_to_both_sexes"
    share: dict[str, Decimal] = {}
    for s, r in used.items():
        v = ra.D(r["value"])
        if not Decimal(0) <= v <= Decimal(100):
            return {
                "code": "inconsistent_input",
                "country": iso3,
                "input": "pension_coverage",
                "detail": f"ILOSTAT coverage {v} % for {s} in {y} is outside [0, 100]",
            }
        share[s] = CTX.divide(v, Decimal(100))
    rows_used = [*used.values(), *([c["T"]] if "T" in c and breakdown == "by_sex" else [])]
    notes = sorted(
        {(str(r.get("note_classif")), r.get("note_classif_label")) for r in rows_used if r.get("note_classif")}
    )
    return Coverage(
        year=y,
        share=share,
        percent={s: str(r["value"]) for s, r in used.items()},
        breakdown=breakdown,
        total_percent=str(c["T"]["value"]) if "T" in c else None,
        sources=tuple(sorted({str(r.get("source")) for r in rows_used})),
        status={f"{r['sex']}": str(r.get("status_label") or r["status"]) for r in rows_used if r.get("status")},
        notes=tuple({"code": code, "label": label} for code, label in notes),
        reference_year=b,
        latest_year_in_source=max(cells),
        newer_years_not_used=tuple(sorted(yy for yy in cells if yy > b)),
    )


# --------------------------------------------------------------------------- plans
@dataclass
class CountryPlan(rw.CountryPlan):
    coverage: Coverage | None = None
    rows: list[Any] | None = None
    rows_fidelity: str | None = None


Compiled = rw.Compiled  # its ``countries`` hold :class:`CountryPlan` (this module's subclass)


def build_country_plan(iso3: str, subject_id: Any, spec: Mapping[str, Any], tables: Mapping[str, Any]) -> CountryPlan:
    """Pure: the 1.1.0 plan plus the coverage input (``tables["pension_coverage"]``, None when not pinned)."""
    base = rw.build_country_plan(iso3, subject_id, spec, tables)
    plan = CountryPlan(**{f.name: getattr(base, f.name) for f in fields(rw.CountryPlan)})
    if plan.status == "unsupported":
        return plan
    b, _years = ra.years_of(spec)
    table = tables.get("pension_coverage")
    if table is None:
        problem: dict[str, Any] | Coverage = {
            "code": "missing_input",
            "country": iso3,
            "input": "pension_coverage",
            "concept": COVERAGE_CONCEPT,
            "kind": "dataset",
            "reason": "unavailable: ILOSTAT publishes no SDG 1.3.1 old-age pension coverage for this country "
            "(or the release pins none); coverage is never assumed (not 100 %)",
        }
    else:
        problem = select_coverage(iso3, rw._rows(table), b)
    if isinstance(problem, Coverage):
        plan.coverage = problem
        if problem.years_before_reference_year > 0:
            plan.notes.append(
                {
                    "code": "coverage_older_than_reference_year",
                    "coverage_year": problem.year,
                    "reference_year": b,
                    "old": problem.years_before_reference_year >= OLD_COVERAGE_YEARS,
                    "message": f"{iso3}: the latest ILOSTAT pension coverage <= {b} is of {problem.year}; held constant",
                }
            )
        if problem.newer_years_not_used:
            plan.notes.append(
                {
                    "code": "coverage_newer_years_not_used",
                    "years": list(problem.newer_years_not_used),
                    "message": f"{iso3}: coverage years after the reference year {b} are not used",
                }
            )
        if problem.breakdown != "by_sex":
            plan.notes.append({"code": "coverage_total_applied_to_both_sexes", "year": problem.year})
        return plan
    plan.problems.append(problem)
    plan.status = "missing_input" if any(p["code"] == "missing_input" for p in plan.problems) else "incompatible"
    return plan


# --------------------------------------------------------------------------- computation
def compute_point(
    plan: CountryPlan, point: lit.GridPoint, years: Sequence[int], rho_basis: str = "own_group"
) -> tuple[dict[str, dict[int, Decimal]], list[dict[str, Any]]]:
    d, cov = plan.data, plan.coverage
    assert d is not None and cov is not None
    out: dict[str, dict[int, Decimal]] = {k: {} for k in OUTPUTS}
    problems: list[dict[str, Any]] = []
    y0 = d.rates_year
    for t in years:
        emp_s = emp_r = d_emp = d_rec = Decimal(0)
        num: dict[tuple[str, ...], Decimal] = {}
        den: dict[tuple[str, str], Decimal] = {}
        for s in SEXES:
            sh = plan.shares[s]
            c = cov.share[s]
            for a in AGES:
                n = ra.n_mid(d.population, t, s, a)
                g = rw.group_of(a)
                rho = rw.pension_drop(d, s, g, rho_basis)
                k = point.share_employed[s] * rho
                base = CTX.divide(d.employment_rate[(s, g)], Decimal(100))
                i_obs = ra.fr(sh["observed"][(y0, a)])
                i_s, i_r = ra.fr(sh["scenario"][(t, a)]), ra.fr(sh["reference"][(t, a)])
                e_s, e_r = base + c * k * (i_s - i_obs), base + c * k * (i_r - i_obs)
                emp_s += n * e_s
                emp_r += n * e_r
                d_emp += c * k * n * (i_s - i_r)
                d_rec -= c * rho * n * (i_s - i_r)
                num[(s, g)] = num.get((s, g), Decimal(0)) + n * e_s
                num[(s, g, "r")] = num.get((s, g, "r"), Decimal(0)) + n * e_r
                den[(s, g)] = den.get((s, g), Decimal(0)) + n
        out["employed_persons_55_64"][t] = emp_s
        out["employed_persons_55_64_reference"][t] = emp_r
        out["employment_change_55_64"][t] = d_emp
        out["pension_recipients_change_55_64"][t] = d_rec
        out["old_age_dependency_ratio"][t] = d.dependency[t]
        for key, v in num.items():
            rate = CTX.divide(v, den[(key[0], key[1])])
            if not Decimal(0) <= rate <= Decimal(1):
                problems.append(
                    {
                        "code": "employment_rate_out_of_bounds",
                        "country": d.iso3,
                        "year": t,
                        "sex": key[0],
                        "age_group": key[1],
                        "path": "reference" if len(key) == 3 else "scenario",
                        "rate": ra.dstr(rate),
                        "grid_point": point.point_id,
                        "pension_drop_basis": rho_basis,
                    }
                )
            if len(key) == 2:
                out[f"employment_rate.{key[0]}.{key[1]}"][t] = CTX.multiply(Decimal(100), rate)
    return out, problems


# --------------------------------------------------------------------------- result rows
def coverage_phrase(cov: Coverage) -> str:
    if cov.breakdown == "by_sex":
        v = f"F {cov.percent['F']} %, M {cov.percent['M']} %"
    else:
        v = f"total {cov.total_percent} % applied to both sexes"
    return f"ILOSTAT SDG 1.3.1 old-age pension coverage {cov.year}: {v}"


def qualification(plan: CountryPlan) -> str:
    assert plan.interventions is not None and plan.coverage is not None
    table = next(iter(plan.interventions.values())).reference.table
    q = str(dict(table.model_lever).get("qualification") or "").strip()
    tr = (
        f" Response estimate transferred from {plan.central.country} ({plan.central.point_id})."
        if plan.central is not None and plan.central.country and plan.central.country != plan.iso3
        else ""
    )
    return (
        f"QUALIFIED ({plan.iso3}): {q}{tr} Legal-age change applied to the pension-covered share only "
        f"({coverage_phrase(plan.coverage)}; contributory and non-contributory pensions). Also: {PENSION_DROP_LABEL}."
    )


def value_dict(output: str, v: Decimal, plan: CountryPlan) -> dict[str, Any]:
    unit = OUTPUTS[output]["unit"]
    return {
        "kind": "decimal",
        "value": ra.present(v, unit),
        "unit_ref": unit,
        "basis": {"scale_factor": "1", "notes": f"{OUTPUTS[output]['label']}. {qualification(plan)}"},
    }


def unknown_uncertainty() -> dict[str, Any]:
    return UncertaintySpec.unknown(
        "Unquantified: ILOSTAT rates and SDG 1.3.1 coverage carry no published uncertainty, the population is a "
        "conditional projection on WPP estimates and the response is a cited (transferred) assumption; "
        "conditional on the stated assumptions"
    ).model_dump(mode="json")


def range_uncertainty(lo: Decimal, hi: Decimal, unit: str, variants: Sequence[str]) -> dict[str, Any]:
    return UncertaintySpec(
        status=UncertaintyStatus.quantified,
        kind=UncertaintyKind.sensitivity_range,
        description=(
            f"Sensitivity range ({ra.SENSITIVITY_STATEMENT}): min/max over the cited employment-response grid, "
            "the no-response reference point and the two pension-drop bounding variants (rho of ages 55-59 / "
            "60-64 at every age). Pension coverage fixed at the source's value (no coverage variant). "
            "No coverage level."
        ),
        method_version=SENSITIVITY_METHOD,
        lower=ra.present(lo, unit),
        upper=ra.present(hi, unit),
        conditioning=tuple(variants),
    ).model_dump(mode="json")


def applicability(
    plan: CountryPlan, point: lit.GridPoint, variant: str, output: str, rho_basis: str = "own_group"
) -> dict[str, Any]:
    assert plan.interventions is not None and plan.data is not None and plan.coverage is not None
    lever = dict(next(iter(plan.interventions.values())).reference.table.model_lever)
    caveats = [str(lever.get("qualification") or ""), str(lever.get("rationale", "")), PENSION_DROP_LABEL]
    caveats += plan.coverage.caveats()
    for w in plan.data.population_summary.get("warnings") or []:
        caveats.append(f"population projection warning {w.get('code')}: {w.get('message', '')}")
    if point.country and point.country != plan.iso3:
        caveats.append(f"response estimate transferred from {point.country} ({point.point_id})")
    if point.sex_transfer:
        caveats.append(point.sex_transfer)
    if plan.data.rates_year < plan.data.base_year:
        caveats.append(f"ILOSTAT rates of {plan.data.rates_year} held constant (latest complete year)")
    return {
        "status": "qualified",
        "qualification": qualification(plan),
        "pension_drop": {
            "basis": rho_basis,
            "direction": "unknown",
            "role": "central" if rho_basis == "own_group" else "bounding variant",
        },
        "pension_coverage": plan.coverage.as_json(),
        "modality": "assumption_conditioned_scenario",
        "label": LABEL,
        "model": f"{MODEL_KEY}@{MODEL_VERSION}",
        "model_label": EXPERIMENTAL_LABEL,
        "review_status": REVIEW_STATUS,
        "country": plan.iso3,
        "variant": variant,
        "output": output,
        "output_label": OUTPUTS[output]["label"],
        "grid_point": point.as_dict(),
        "base_year": plan.data.base_year,
        "rates_year": plan.data.rates_year,
        "coverage_year": plan.coverage.year,
        "data_sources": DATA_SOURCES,
        "caveats": [c for c in caveats if c],
        "not_claimed": list(NOT_CLAIMED),
    }


def rows_for_country(plan: CountryPlan, compiled: Compiled, fidelity: str, row_cls: Any) -> list[Any]:
    assert plan.central is not None
    evals = rw.evaluations(plan, fidelity)
    results: dict[str, dict[str, dict[int, Decimal]]] = {}
    for variant, p, rho_basis in evals:
        vals, problems = compute_point(plan, p, compiled.years, rho_basis)
        if problems:
            raise ValidationProblem(
                f"{plan.iso3}: an employment rate leaves [0, 100] under grid point {p.point_id} (pension drop "
                f"{rho_basis}); the response assumption is incompatible with the base data",
                problems=problems[:10],
            )
        results[variant] = vals
    rows: list[Any] = []
    for output in compiled.outputs:
        for t in compiled.years:
            period = (
                (date(t, 1, 1), date(t + 1, 1, 1))
                if output != "old_age_dependency_ratio"
                else (date(t, 1, 1), date(t, 1, 2))
            )
            series = [(variant, p, rb, results[variant][output][t]) for variant, p, rb in evals]
            for variant, p, rb, v in series:
                rows.append(
                    row_cls(
                        output_key=output,
                        iso3=plan.iso3,
                        subject_id=plan.subject_id,
                        variant=variant,
                        value=value_dict(output, v, plan),
                        uncertainty=unknown_uncertainty(),
                        applicability=applicability(plan, p, variant, output, rb),
                        period=period,
                    )
                )
            if fidelity == "L2":
                vs = [v for *_x, v in series]
                lo, hi = min(vs), max(vs)
                unit = OUTPUTS[output]["unit"]
                names = [variant for variant, *_x in series]
                rows.append(
                    row_cls(
                        output_key=output,
                        iso3=plan.iso3,
                        subject_id=plan.subject_id,
                        variant=SENSITIVITY_VARIANT,
                        value={
                            "kind": "sensitivity_range",
                            "lower": ra.present(lo, unit),
                            "upper": ra.present(hi, unit),
                            "unit_ref": unit,
                            "basis": value_dict(output, lo, plan)["basis"],
                            "evaluated_points": len(vs),
                            "statement": ra.SENSITIVITY_STATEMENT,
                        },
                        uncertainty=range_uncertainty(lo, hi, unit, names),
                        applicability={
                            **applicability(plan, plan.central, SENSITIVITY_VARIANT, output),
                            "grid_points": [p.point_id for p in plan.points],
                            "range_over": names,
                        },
                        period=period,
                    )
                )
    return rows


# --------------------------------------------------------------------------- spec, manifest, plugin
def validate_spec(spec: Mapping[str, Any]) -> None:
    if spec.get("model_version") != MODEL_VERSION:
        raise ValidationProblem(f"spec is not for {MODEL_KEY}@{MODEL_VERSION}")
    outputs = list(spec.get("outputs") or DEFAULT_OUTPUTS)
    na = {o: NOT_AVAILABLE[o] for o in outputs if o in NOT_AVAILABLE}
    if na:
        raise CapabilityGap(
            f"outputs {sorted(na)} are not computed by {MODEL_KEY}@{MODEL_VERSION}",
            capability="social.retirement_age.outputs",
            reasons=na,
            supported=sorted(OUTPUTS),
        )
    unknown = [o for o in outputs if o not in OUTPUTS]
    if unknown:
        raise CapabilityGap(
            f"outputs {unknown} are not computed by {MODEL_KEY}@{MODEL_VERSION}",
            capability="outputs",
            supported=sorted(OUTPUTS),
        )
    # every other check (reference year, population assumptions, per-country rule tables, eligibility age
    # range) is exactly 1.1.0's: same rule tables, same population and rate inputs
    rw.validate_spec({**spec, "model_version": rw.MODEL_VERSION})


def explain(fidelity: str | None = None) -> dict[str, Any]:
    out = rw.explain(fidelity)
    out.update(
        model=f"{MODEL_KEY}@{MODEL_VERSION}",
        method=(
            "legal eligibility age by sex (rule table; WBL 2026 age held constant unless a law-text table exists) -> "
            "share of person-time below it per single age 55-64 and year -> applied to the pension-covered share c "
            "(ILOSTAT SDG 1.3.1, source's own value) -> cited response (s) and own-country pension-receipt proxy "
            "(rho, ILOSTAT participation); population from the cohort-component projection on WPP 2024 estimates; "
            "ILOSTAT employment rates of the latest complete year held constant"
        ),
        data_sources=DATA_SOURCES,
        pension_drop=PENSION_DROP_LABEL,
        pension_coverage=COVERAGE_LABEL,
        outputs={k: v["label"] for k, v in OUTPUTS.items()},
        not_claimed=list(NOT_CLAIMED),
    )
    return out


def implementation_files() -> tuple[Path, ...]:
    return (Path(__file__), *rw.implementation_files())


def manifest_dict() -> dict[str, Any]:
    from reva.simulation.plugins import source_digest

    tables = {c: rl.load_rule_table(c).digest() for c in rl.available_countries()}
    tables.update({c: rl.load_wbl_rule_table(c).digest() for c in rl.wbl_countries() if c not in tables})
    return {
        "implementation": "reva.models.social.retirement_coverage",
        "implementation_digest": source_digest(*implementation_files(), *rw.data_files()),
        "model_version": MODEL_VERSION,
        "review_status": REVIEW_STATUS,
        "label": EXPERIMENTAL_LABEL,
        "decimal_precision": ra.PRECISION,
        "rule_tables_count": len(tables),
        "rule_tables_digest": logical_hash(tables),
        "assumption_registry_digest": lit.load_grid().source_digest,
        "data_sources": DATA_SOURCES,
        "assumptions": [
            "ILOSTAT employment and participation rates of the latest complete year (<= reference year, at most "
            f"{MAX_RATES_AGE} years older) held constant by sex and five-year band",
            "ILOSTAT SDG 1.3.1 old-age pension coverage of the latest year <= reference year (at most "
            f"{MAX_COVERAGE_AGE} years older), by sex when available, else the total for both sexes; held constant",
            COVERAGE_LABEL,
            "countries without a coverage value are refused (missing_input), never assumed covered",
            "employment response: cited European literature values (transferred outside their country, labelled)",
            PENSION_DROP_LABEL,
            "WBL 2026 tables: the age in force on 1 October 2025 held constant for every claim year (counterfactual)",
            "eligibility modelled at single ages 55-64 only (ILOSTAT has no 65-69 band); other requests refused",
            "population from the cohort-component projection on WPP 2024 estimates",
        ],
        "max_end_year": ra.MAX_END_YEAR,
    }


class RetirementAgeCoverageModel:
    """Plugin ``social.retirement_age@1.2.0`` (experimental, review pending)."""

    model_key = MODEL_KEY
    model_version = MODEL_VERSION
    fidelities = frozenset({"L1", "L2"})
    missing_input_policies = frozenset({"fail", "skip_missing_countries"})
    supports_all_countries = True

    def validate(self, spec: dict[str, Any]) -> None:
        validate_spec(spec)

    def requirements(self, spec: dict[str, Any]) -> list[Any]:
        from reva.simulation.plugins import InputRequirement

        b, _years = ra.years_of(spec)
        out = []
        for iso3 in rw.countries_of(spec):
            for name, concept in CONCEPTS.items():
                if name in ("employment_rate", "activity_rate"):
                    yrs = tuple(range(b - MAX_RATES_AGE, b + 1))
                elif name == "pension_coverage":
                    yrs = tuple(range(b - MAX_COVERAGE_AGE, b + 1))
                else:
                    yrs = tuple(range(b - 9, b + 2))
                out.append(InputRequirement(name, concept, iso3, yrs, "dataset"))
        return out

    def compile(self, inputs: Any, spec: dict[str, Any]) -> Compiled:
        b, years = ra.years_of(spec)
        plans: list[CountryPlan] = []
        for iso3 in rw.countries_of(spec):
            tables = {n: (None if (ds := inputs.dataset(n, iso3)).is_missing else ds.table) for n in CONCEPTS}
            plans.append(build_country_plan(iso3, inputs.subject(iso3).entity_id, spec, tables))
        outputs = tuple(spec.get("outputs") or DEFAULT_OUTPUTS)
        compiled = Compiled(
            dict(spec),
            b,
            years,
            outputs,
            list[rw.CountryPlan](plans),
            str(spec.get("missing_input_policy", "fail")),
            dict(inputs.release),
        )
        # as 1.1.0: evaluate every ready country now, so a country whose response assumption drives an
        # employment rate out of [0, 100] is reported per country instead of failing the whole run
        from reva.simulation.plugins import ResultRow

        fidelity = str(spec.get("fidelity", "L1"))
        if fidelity in self.fidelities:
            for plan in plans:  # this module's CountryPlan
                if plan.status != "ready":
                    continue
                try:
                    plan.rows = rows_for_country(plan, compiled, fidelity, ResultRow)
                    plan.rows_fidelity = fidelity
                except ValidationProblem as e:
                    plan.status = "incompatible"
                    plan.problems.append({**e.problem(), "country": plan.iso3})
        return compiled

    def compute(self, compiled: Compiled, fidelity: str) -> list[Any]:
        from reva.simulation.plugins import ResultRow

        if fidelity not in self.fidelities:
            raise CapabilityGap(
                f"{MODEL_KEY}@{MODEL_VERSION} computes L1 and L2 only", capability=f"fidelity.{fidelity}"
            )
        rows: list[Any] = []
        for plan in compiled.countries:
            if plan.status == "ready":
                assert isinstance(plan, CountryPlan)
                if plan.rows is not None and plan.rows_fidelity == fidelity:
                    rows.extend(plan.rows)
                else:
                    rows.extend(rows_for_country(plan, compiled, fidelity, ResultRow))
        from reva.models.population.cohort_world import result_size_guard

        result_size_guard(rows, f"{MODEL_KEY}@{MODEL_VERSION}")
        return sorted(rows, key=lambda r: r.sort_key())

    def manifest(self) -> dict[str, Any]:
        return manifest_dict()

    def spec_schema(self) -> dict[str, Any]:
        return rw.spec_schema()

    def explanation(self, fidelity: str) -> dict[str, Any]:
        return explain(fidelity)

    def run_applicability(self, compiled: Compiled, fidelity: str) -> dict[str, Any]:
        countries = {}
        for c in compiled.countries:
            assert isinstance(c, CountryPlan)
            entry: dict[str, Any] = {"status": c.status, "problems": c.problems, "notes": c.notes}
            if c.interventions is not None:
                entry["intervention"] = {s: iv.as_json() for s, iv in c.interventions.items()}
            if c.central is not None:
                entry["central_grid_point"] = c.central.point_id
                entry["grid_points"] = [p.point_id for p in c.points]
            if c.coverage is not None:
                entry["pension_coverage"] = c.coverage.as_json()
            if c.data is not None:
                entry["base_year"] = c.data.base_year
                entry["rates"] = c.data.rate_flags
                entry["population_projection"] = c.data.population_summary
            countries[c.iso3] = entry
        out = {
            "explanation": explain(fidelity),
            "countries": countries,
            "years": list(compiled.years),
            "label": LABEL,
            "model_label": EXPERIMENTAL_LABEL,
        }
        if compiled.policy == "skip_missing_countries":
            out["missing_input_policy"] = "skip_missing_countries"
            out["skipped_countries"] = compiled.skipped
        return out

    def cost(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {"periods": int((spec.get("horizon") or {}).get("periods", 0)), "ages": len(AGES)}

    def implementation_files(self) -> tuple[Path, ...]:
        return implementation_files()


MODEL = RetirementAgeCoverageModel()


def _register() -> None:
    from reva.simulation import plugins

    plugins.register(MODEL)


_register()
