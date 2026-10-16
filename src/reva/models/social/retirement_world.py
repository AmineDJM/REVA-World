"""``social.retirement_age@1.1.0`` - the retirement-age module for countries outside the Eurostat LFS.

Version 1.1.0 of the same model (V3 agent SIMWORLD). Version 1.0.0 (EU countries: Eurostat LFS,
ESSPROS, Eurostat demography, ages 55-69) stays registered as the default and unchanged. 1.1.0
runs the same eligibility arithmetic and the same cited employment-response grid on inputs
available for most countries of the world:

* **Legal ages**: the country's rule table (``registry/rules/retirement_age/<ISO3>.yaml``): the
  tables transcribed from archived law text (FRA, DEU, ESP, NLD) or the tables transcribed by
  agent SIMWORLD from World Bank WBL 2026 (full-benefit age by sex as of 1 October 2025, path
  ``wbl2026_held_constant`` = that age held constant, a labelled counterfactual). Tables may
  give one track per sex (``model_lever.sex_tracks``); eligibility is then computed per sex.
* **Population**: ``demography.cohort_component`` numerics on UN WPP 2024 estimates (connector
  ``un.wpp``): jump-off 1 January ``reference_year + 1`` (at most 2023).
* **Employment and activity rates**: ILOSTAT employment-to-population ratio and labour force
  participation rate by sex and five-year band (connector ``ilostat.labour_age5``), of the
  latest year <= ``reference_year`` (and at most :data:`MAX_RATES_AGE` years older) in which
  both rates exist for every (sex, band) cell; held constant from that year (stated).

**Age range 55-64.** ILOSTAT publishes no 65-69 band (the five-year classification ends with
65+), so this version models single ages 55-64 only; a request whose eligibility changes at
any age outside 55-64 is refused with ``capability_gap`` (never cut, never filled with the 65+
rate). **Not computed** (refused with ``capability_gap`` when requested): pension recipients
*level* and pension *expenditure* (no open per-country beneficiary/expenditure data are wired
for this path) and any 55-69 aggregate.

Method per country (as 1.0.0, restricted to 55-64): ``A = N (I_scn - I_ref)`` with ``N`` the
mean of the projected 1 January populations; pension-receipt drop ``rho = 1 - LFPR/100`` of
the age's own band (a proxy of unknown direction; L2 bounding variants use the 55-59 and the
60-64 band at every age); employment change ``s rho A`` with ``s`` a cited literature value
(every point is a European estimate: outside its country it is a *transferred* assumption and
every value says so); levels ``e = e_obs + s rho (I - I_obs)`` from the rates year's regime.
A conditional projection under stated assumptions, not a forecast; experimental, review pending.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from reva.kernel.errors import CapabilityGap, MissingInput, ValidationProblem
from reva.kernel.ids import logical_hash
from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec, UncertaintyStatus
from reva.models.social import assumptions as lit
from reva.models.social import retirement_age as ra
from reva.models.social import rules as rl

MODEL_KEY = ra.MODEL_KEY
MODEL_VERSION = "1.1.0"
REVIEW_STATUS = ra.REVIEW_STATUS
LABEL = ra.LABEL
EXPERIMENTAL_LABEL = ra.EXPERIMENTAL_LABEL
SEXES = ("F", "M")
AGES = tuple(range(55, 65))
AGE_GROUPS: tuple[tuple[str, int, int], ...] = (("Y55-59", 55, 59), ("Y60-64", 60, 64))
MODEL_AGE_RANGE_MONTHS = (12 * AGES[0], 12 * (AGES[-1] + 1))
PENSION_DROP_BASES = ("own_group", "Y55-59", "Y60-64")
MAX_RATES_AGE = 10
MAX_JUMP_OFF = 2023  # last 1 January population estimate of WPP 2024 kept by the connector
ALL = "ALL"
CTX = ra.CTX
SENSITIVITY_VARIANT = "L2:sensitivity_range"
SENSITIVITY_METHOD = "reva.models.social.retirement_world.l2_literature_grid_minmax@1.1.0"
CONCEPTS = {
    "employment_rate": "labour.ilostat.employment_to_population_ratio.age5_sex",
    "activity_rate": "labour.ilostat.labour_force_participation_rate.age5_sex",
    "population": "demography.wpp.population.jan1_age_sex",
    "deaths": "demography.wpp.deaths.age_sex",
    "births": "demography.wpp.live_births.mother_age5_newborn_sex",
}
DATA_SOURCES = {
    "rule_table": (
        "registry/rules/retirement_age/<ISO3>.yaml (transcribed from archived law text: DEU ESP FRA NLD), else "
        "registry/rules/retirement_age_wbl2026/<ISO3>.yaml (transcribed from World Bank WBL 2026)"
    ),
    "population": "UN World Population Prospects 2024 estimates (connector un.wpp), cohort-component projection",
    "rates": "ILOSTAT EMP_DWAP_SEX_AGE_RT_A and EAP_DWAP_SEX_AGE_RT_A, five-year bands (connector ilostat.labour_age5)",
}
PENSION_DROP_LABEL = (
    "pension-receipt drop rho = share of the five-year age band outside the labour force in the rates year "
    "(ILOSTAT): a proxy of unknown direction (includes non-pensioner inactivity, omits unemployed claimants; "
    "where pension coverage is low, most inactive persons draw no contributory pension at all)"
)
OUTPUTS: dict[str, dict[str, str]] = {
    "employed_persons_55_64": {
        "unit": "unit.persons",
        "kind": "level",
        "label": "Employed persons aged 55-64 under the scenario rule path",
    },
    "employed_persons_55_64_reference": {
        "unit": "unit.persons",
        "kind": "level",
        "label": "Employed persons aged 55-64 under the reference rule path",
    },
    "employment_change_55_64": {
        "unit": "unit.persons",
        "kind": "change",
        "label": "Employed persons aged 55-64: scenario minus reference",
    },
    "pension_recipients_change_55_64": {
        "unit": "unit.persons",
        "kind": "change",
        "label": "Persons aged 55-64 kept out of (negative) or brought into (positive) pension receipt: scenario "
        "minus reference (rho proxy)",
    },
    "old_age_dependency_ratio": {
        "unit": "unit.ratio",
        "kind": "level",
        "label": "Persons 65+ per 100 persons aged 15-64 on 1 January (WPP-based projection)",
    },
}
for _sex in SEXES:
    for _g, _lo, _hi in AGE_GROUPS:
        OUTPUTS[f"employment_rate.{_sex}.{_g}"] = {
            "unit": "unit.percent",
            "kind": "level",
            "label": f"Employment rate (percent), recorded sex {_sex}, ages {_lo}-{_hi}, scenario rule path",
        }
DEFAULT_OUTPUTS = (
    "employed_persons_55_64",
    "employment_change_55_64",
    "pension_recipients_change_55_64",
    "old_age_dependency_ratio",
)
NOT_AVAILABLE = {
    "pension_recipients": "no open per-country old-age pension beneficiary counts are wired for this path",
    "pension_expenditure_change_approx_eur": "no open per-country pension expenditure data are wired for this path",
    "employed_persons_55_69": "ILOSTAT publishes no 65-69 age band (five-year bands end with 65+)",
    "employment_change_55_69": "ILOSTAT publishes no 65-69 age band (five-year bands end with 65+)",
}
NOT_CLAIMED = (
    *ra.NOT_CLAIMED[:-2],
    "eligibility ages outside single ages 55-64 (refused, never cut)",
    "pension recipient levels or expenditure (not computed on this path)",
    "the transferability of a European response estimate to this country",
    "a forecast of what will happen",
)


# --------------------------------------------------------------------------- spec handling
def group_of(age: int) -> str:
    for g, lo, hi in AGE_GROUPS:
        if lo <= age <= hi:
            return g
    raise ValueError(age)


def sex_tracks(table: rl.RuleTable) -> dict[str, str]:
    lever = dict(table.model_lever)
    if not lever:
        raise CapabilityGap(f"{table.country}: the rule table declares no model lever", capability=MODEL_KEY)
    tracks = dict(lever.get("sex_tracks") or {})
    if tracks:
        if set(tracks) != set(SEXES):
            raise ValidationProblem(f"{table.country}: model_lever.sex_tracks must name F and M")
        return {s: str(tracks[s]) for s in SEXES}
    t = str(lever.get("track", "general"))
    return {s: t for s in SEXES}


def compile_interventions(
    iso3: str, intervention: Mapping[str, Any], assumptions: Mapping[str, Any]
) -> dict[str, ra.Intervention]:
    """One :class:`ra.Intervention` per recorded sex (the same path twice when the table has one track)."""
    table = rl.load_any_rule_table(iso3)  # law-text table, else WBL 2026 table, else capability_gap (reason)
    age_kind = str(dict(table.model_lever).get("age_kind"))
    kind = intervention.get("kind")
    ref_id = intervention.get("reference_path")
    if not isinstance(ref_id, str):
        raise ValidationProblem("intervention.reference_path is required", available=sorted(table.paths))
    obs_id = str(assumptions.get("observed_path", ref_id))
    out: dict[str, ra.Intervention] = {}
    for s, track in sex_tracks(table).items():
        reference = rl.TablePath(table, ref_id, age_kind, track)
        if kind == "retirement_age_rule_path":
            scn_id = intervention.get("scenario_path")
            if not isinstance(scn_id, str) or scn_id == ref_id:
                raise ValidationProblem("intervention.scenario_path is required and must differ from reference_path")
            scenario: Any = rl.TablePath(table, scn_id, age_kind, track)
        elif kind == "retirement_age_shift":
            scenario = rl.ShiftedPath(
                reference,
                int(intervention["shift_months"]),
                int(intervention["first_birth_year"]),
                int(intervention.get("phase_in_months_per_cohort", 0)),
            )
        elif kind == "retirement_age_target":
            target, first = intervention.get("target_age_months"), intervention.get("first_birth_year")
            if not isinstance(target, int) or isinstance(target, bool) or not isinstance(first, int):
                raise ValidationProblem("intervention.target_age_months and first_birth_year (integers) are required")
            scenario = rl.TargetPath(reference, target, first, int(intervention.get("phase_in_months_per_cohort", 0)))
        else:
            raise CapabilityGap(
                f"intervention kind {kind!r} is unsupported",
                capability=f"intervention.{kind}",
                supported=list(ra.INTERVENTION_KINDS),
            )
        out[s] = ra.Intervention(str(kind), scenario, reference, rl.TablePath(table, obs_id, age_kind, track))
    return out


def check_age_range(iso3: str, ivs: Mapping[str, ra.Intervention], obs_year: int, years: Sequence[int]) -> None:
    """Refuse when eligibility differs between the compared paths at an age outside 55-64 (never cut)."""
    cells: list[dict[str, Any]] = []
    for s, iv in ivs.items():
        paths = (iv.scenario, iv.reference, iv.observed)
        lo = min(p.age_bounds_months()[0] for p in paths)
        hi = max(p.age_bounds_months()[1] for p in paths)
        outer = [*range(max(lo // 12, 0), AGES[0])] if lo < MODEL_AGE_RANGE_MONTHS[0] else []
        if hi > MODEL_AGE_RANGE_MONTHS[1]:
            outer += list(range(AGES[-1] + 1, hi // 12 + 1))
        for a in outer:
            i_obs = rl.ineligible_share_single_age(iv.observed, obs_year, a)
            for t in years:
                i_s = rl.ineligible_share_single_age(iv.scenario, t, a)
                i_r = rl.ineligible_share_single_age(iv.reference, t, a)
                if i_s != i_r or i_s != i_obs:
                    cells.append(
                        {
                            "sex": s,
                            "year": t,
                            "age": a,
                            "scenario_share_below": str(i_s),
                            "reference_share_below": str(i_r),
                        }
                    )
    if cells:
        raise CapabilityGap(
            f"{iso3}: the requested eligibility ages change eligibility outside the ages this version models "
            f"(55-64: ILOSTAT has no 65-69 band) in {len(cells)} (sex, year, age) cells, e.g. age {cells[0]['age']} "
            f"in {cells[0]['year']}; refused rather than cut",
            capability="social.retirement_age.age_range",
            country=iso3,
            model_ages=[AGES[0], AGES[-1]],
            cells=cells[:20],
        )


def countries_of(spec: Mapping[str, Any]) -> list[str]:
    c = list(spec.get("countries") or [])
    if not c:
        raise ValidationProblem("countries must list at least one country key (or exactly ['ALL'])")
    return c


def validate_spec(spec: Mapping[str, Any]) -> None:
    if spec.get("model_version") != MODEL_VERSION:
        raise ValidationProblem(f"spec is not for {MODEL_KEY}@{MODEL_VERSION}")
    b, years = ra.years_of(spec)
    if b + 1 > MAX_JUMP_OFF:
        raise CapabilityGap(
            f"the population projection starts on 1 January reference_year + 1 = {b + 1}; WPP 2024 estimates end "
            f"with 1 January {MAX_JUMP_OFF} (reference_year <= {MAX_JUMP_OFF - 1})",
            capability="social.retirement_age.reference_year",
            latest_reference_year=MAX_JUMP_OFF - 1,
        )
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
    pop = dict((spec.get("assumptions") or {}).get("population") or {})
    if (
        pop.get("migration", "average_residual") not in ("zero", "average_residual")
        or pop.get("fertility", "constant_base_period_rates") != "constant_base_period_rates"
    ):
        raise CapabilityGap(
            "population assumptions on the WPP path: migration zero or average_residual, fertility constant "
            "(EUROPOP2023 exists for EU/EFTA only)",
            capability="demography.assumption.europop2023",
        )
    if (
        pop.get("migration", "average_residual") == "average_residual"
        and pop.get("base_period_years", 5) == 1
        and not pop.get("allow_single_year_migration", False)
    ):
        raise ValidationProblem(
            "assumptions.population.base_period_years = 1 with average-residual migration is refused unless "
            "assumptions.population.allow_single_year_migration is true"
        )
    fidelity = str(spec.get("fidelity", "L1"))
    countries = countries_of(spec)
    if countries == [ALL] or spec.get("missing_input_policy") == "skip_missing_countries":
        return  # per-country refusals are then reported per country by compile (and the country skipped)
    for iso3 in countries:
        ivs = compile_interventions(iso3, spec.get("intervention") or {}, spec.get("assumptions") or {})
        ra.grid_for(iso3, spec.get("assumptions") or {}, fidelity if fidelity in ("L1", "L2") else "L1")
        gaps = [
            g for iv in ivs.values() for p in (iv.scenario, iv.reference) for g in rl.coverage_problems(p, years, AGES)
        ]
        if gaps:
            raise rl.RuleCoverageGap(
                f"{iso3}: the rule paths do not cover every (year, age) cell of the request ({len(gaps)} cells, e.g. {gaps[0]})",
                capability="social.retirement_age",
                country=iso3,
                cells=gaps[:20],
            )
        check_age_range(iso3, ivs, b, years)


# --------------------------------------------------------------------------- inputs
def ilo_rates(rows: Sequence[Mapping[str, Any]]) -> dict[int, dict[tuple[str, str], dict[str, Any]]]:
    """``{year: {(sex, band): row}}`` of the bands the model uses (non-null values only)."""
    out: dict[int, dict[tuple[str, str], dict[str, Any]]] = {}
    bands = {g for g, _lo, _hi in AGE_GROUPS}
    for r in rows:
        if r.get("sex") in SEXES and r.get("age") in bands and r.get("value") is not None:
            out.setdefault(int(r["year"]), {})[(str(r["sex"]), str(r["age"]))] = dict(r)
    return out


def rates_year(emp: Mapping[int, Any], act: Mapping[int, Any], b: int) -> int | None:
    need = {(s, g) for s in SEXES for g, _lo, _hi in AGE_GROUPS}
    ok = [y for y in emp if y <= b and y >= b - MAX_RATES_AGE and need <= set(emp[y]) and need <= set(act.get(y, {}))]
    return max(ok) if ok else None


@dataclass
class CountryData:
    iso3: str
    base_year: int
    rates_year: int
    employment_rate: dict[tuple[str, str], Decimal]
    activity_rate: dict[tuple[str, str], Decimal]
    rate_flags: dict[str, Any]
    population: dict[int, dict[str, tuple[Decimal, ...]]]
    dependency: dict[int, Decimal]
    population_summary: dict[str, Any]


@dataclass
class CountryPlan:
    iso3: str
    subject_id: Any
    interventions: dict[str, ra.Intervention] | None
    central: lit.GridPoint | None
    points: tuple[lit.GridPoint, ...]
    data: CountryData | None = None
    status: str = "ready"
    problems: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    shares: dict[str, dict[str, dict[tuple[int, int], Fraction]]] = field(default_factory=dict)  # sex -> path -> cell


@dataclass
class Compiled:
    spec: dict[str, Any]
    base_year: int
    years: tuple[int, ...]
    outputs: tuple[str, ...]
    countries: list[CountryPlan]
    policy: str
    release: Mapping[str, Any] = field(default_factory=dict)

    @property
    def missing(self) -> list[dict[str, Any]]:
        if self.policy == "skip_missing_countries" and any(c.status == "ready" for c in self.countries):
            return []
        return [p for c in self.countries for p in c.problems if p.get("code") == "missing_input"]

    @property
    def incompatible(self) -> list[dict[str, Any]]:
        if self.policy == "skip_missing_countries" and any(c.status == "ready" for c in self.countries):
            return []
        return [p for c in self.countries for p in c.problems if p.get("code") != "missing_input"]

    @property
    def skipped(self) -> list[str]:
        return [c.iso3 for c in self.countries if c.status != "ready"]


def population_inputs(
    iso3: str, base_year: int, end_year: int, tables: Mapping[str, Any], assumptions: Mapping[str, Any]
) -> tuple[dict[int, dict[str, tuple[Decimal, ...]]], dict[int, Decimal], dict[str, Any]]:
    from reva.models.population import cohort, wpp

    pa = dict(assumptions.get("population") or {})
    n_base = int(pa.get("base_period_years", 5))
    if not 1 <= n_base <= 10:
        raise ValidationProblem("assumptions.population.base_period_years must be in [1, 10]")
    jump = base_year + 1
    data = wpp.country_demography(
        iso3, population=tables["population"], deaths=tables["deaths"], births=tables["births"]
    )
    ass = cohort.Assumptions(
        migration=str(pa.get("migration", "average_residual")),
        fertility="constant_base_period_rates",
        base_period=(jump - n_base, jump - 1),
        allow_single_year_migration=bool(pa.get("allow_single_year_migration", False)),
    )
    proj = cohort.project(data, jump_off=jump, end_year=end_year + 1, assumptions=ass)
    values = cohort.output_values(proj)
    dep = {y: values[("old_age_dependency_ratio_65plus_per_15_64", y)] for y in sorted(proj.population)}
    summary = proj.summary()
    summary["model"] = f"{cohort.MODEL_KEY} numerics on UN WPP 2024 estimates (data_source un_wpp2024)"
    return dict(proj.population), dep, summary


def _rows(table: Any) -> list[dict[str, Any]]:
    return ra._rows(table)


def build_country_plan(iso3: str, subject_id: Any, spec: Mapping[str, Any], tables: Mapping[str, Any]) -> CountryPlan:
    """Pure: ``tables`` maps input names to Arrow tables (or None when the release pins nothing)."""
    b, years = ra.years_of(spec)
    fidelity = str(spec.get("fidelity", "L1"))
    assumptions = dict(spec.get("assumptions") or {})
    try:
        ivs = compile_interventions(iso3, spec.get("intervention") or {}, assumptions)
        central, points = ra.grid_for(iso3, assumptions, fidelity)
    except (CapabilityGap, ValidationProblem) as e:
        return CountryPlan(
            iso3, subject_id, None, None, (), status="unsupported", problems=[{**e.problem(), "country": iso3}]
        )
    plan = CountryPlan(iso3, subject_id, ivs, central, points)
    missing = [
        {"code": "missing_input", "country": iso3, "input": n, "concept": c, "kind": "dataset", "reason": "unavailable"}
        for n, c in CONCEPTS.items()
        if tables.get(n) is None
    ]
    if missing:
        plan.status, plan.problems = "missing_input", missing
        return plan
    emp, act = ilo_rates(_rows(tables["employment_rate"])), ilo_rates(_rows(tables["activity_rate"]))
    y = rates_year(emp, act, b)
    if y is None:
        latest = max([yy for yy in emp if yy <= b], default=None)
        plan.status = "missing_input"
        plan.problems = [
            {
                "code": "missing_input",
                "country": iso3,
                "input": "employment_rate/activity_rate",
                "concept": f"{CONCEPTS['employment_rate']} + {CONCEPTS['activity_rate']}",
                "kind": "dataset",
                "reason": (
                    f"no year in [{b - MAX_RATES_AGE}, {b}] with both ILOSTAT rates for every (sex, band) of "
                    f"{[g for g, _lo, _hi in AGE_GROUPS]}"
                ),
                "latest_employment_rate_year": latest,
            }
        ]
        return plan
    erate = {k: ra.D(r["value"]) for k, r in emp[y].items()}
    arate = {k: ra.D(r["value"]) for k, r in act[y].items()}
    for k, e in erate.items():
        if e > arate[k]:
            plan.problems.append(
                {
                    "code": "inconsistent_input",
                    "country": iso3,
                    "detail": f"employment rate {e} exceeds participation rate {arate[k]} for {k} in {y}",
                }
            )
    flags = {
        "rates_year": y,
        "held_constant_from": y,
        "years_before_reference_year": b - y,
        "sources": sorted({str(r["source"]) for d in (emp[y], act[y]) for r in d.values()}),
        "status_codes": {f"{k[0]}.{k[1]}": r["status"] for k, r in emp[y].items() if r.get("status")},
    }
    if y < b:
        plan.notes.append(
            {
                "code": "rates_older_than_reference_year",
                "rates_year": y,
                "reference_year": b,
                "message": f"{iso3}: the latest year with complete ILOSTAT rates is {y}; held constant from {y}",
            }
        )
    if flags["status_codes"]:
        plan.notes.append({"code": "ilostat_status_flags", "flags": flags["status_codes"]})
    try:
        pop, dep, summary = population_inputs(iso3, b, years[-1], tables, assumptions)
    except MissingInput as e:
        plan.problems += [
            {"code": "missing_input", "country": iso3, "input": "population_projection", "concept": "WPP inputs", **m}
            for m in (e.details.get("missing") or [{"detail": e.message}])
        ]
        pop, dep, summary = {}, {}, {}
    except CapabilityGap as e:
        plan.problems.append({**e.problem(), "country": iso3})
        pop, dep, summary = {}, {}, {}
    try:
        for iv in ivs.values():
            gaps = rl.coverage_problems(iv.observed, (y,), AGES)
            if gaps:
                raise rl.RuleCoverageGap(
                    f"{iso3}: the observed-regime path does not cover the rates year {y}",
                    capability="social.retirement_age",
                    country=iso3,
                    cells=gaps[:10],
                )
        check_age_range(iso3, ivs, y, years)
    except CapabilityGap as e:
        plan.problems.append({**e.problem(), "country": iso3})
    if plan.problems:
        plan.status = "missing_input" if any(p["code"] == "missing_input" for p in plan.problems) else "incompatible"
        return plan
    plan.data = CountryData(iso3, b, y, erate, arate, flags, pop, dep, summary)
    for s, iv in ivs.items():
        sh: dict[str, dict[tuple[int, int], Fraction]] = {"scenario": {}, "reference": {}, "observed": {}}
        for t in years:
            for a in AGES:
                sh["scenario"][(t, a)] = rl.ineligible_share_single_age(iv.scenario, t, a)
                sh["reference"][(t, a)] = rl.ineligible_share_single_age(iv.reference, t, a)
        for a in AGES:
            sh["observed"][(y, a)] = rl.ineligible_share_single_age(iv.observed, y, a)
        plan.shares[s] = sh
    return plan


# --------------------------------------------------------------------------- computation
def pension_drop(d: CountryData, sex: str, group: str, basis: str = "own_group") -> Decimal:
    if basis not in PENSION_DROP_BASES:
        raise ValidationProblem(f"unknown pension-drop basis {basis!r}", allowed=list(PENSION_DROP_BASES))
    g = group if basis == "own_group" else basis
    return Decimal(1) - CTX.divide(d.activity_rate[(sex, g)], Decimal(100))


def compute_point(
    plan: CountryPlan, point: lit.GridPoint, years: Sequence[int], rho_basis: str = "own_group"
) -> tuple[dict[str, dict[int, Decimal]], list[dict[str, Any]]]:
    d = plan.data
    assert d is not None
    out: dict[str, dict[int, Decimal]] = {k: {} for k in OUTPUTS}
    problems: list[dict[str, Any]] = []
    y0 = d.rates_year
    for t in years:
        emp_s = emp_r = d_emp = d_rec = Decimal(0)
        num: dict[tuple[str, ...], Decimal] = {}
        den: dict[tuple[str, str], Decimal] = {}
        for s in SEXES:
            sh = plan.shares[s]
            for a in AGES:
                n = ra.n_mid(d.population, t, s, a)
                g = group_of(a)
                rho = pension_drop(d, s, g, rho_basis)
                k = point.share_employed[s] * rho
                base = CTX.divide(d.employment_rate[(s, g)], Decimal(100))
                i_obs = ra.fr(sh["observed"][(y0, a)])
                i_s, i_r = ra.fr(sh["scenario"][(t, a)]), ra.fr(sh["reference"][(t, a)])
                e_s, e_r = base + k * (i_s - i_obs), base + k * (i_r - i_obs)
                emp_s += n * e_s
                emp_r += n * e_r
                d_emp += k * n * (i_s - i_r)
                d_rec -= rho * n * (i_s - i_r)
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
def qualification(plan: CountryPlan) -> str:
    assert plan.interventions is not None
    table = next(iter(plan.interventions.values())).reference.table
    q = str(dict(table.model_lever).get("qualification") or "").strip()
    tr = (
        f" Response estimate transferred from {plan.central.country} ({plan.central.point_id})."
        if plan.central is not None and plan.central.country and plan.central.country != plan.iso3
        else ""
    )
    return f"QUALIFIED ({plan.iso3}): {q}{tr} Also: {PENSION_DROP_LABEL}."


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
        "Unquantified: ILOSTAT rates carry no published uncertainty, the population is a conditional projection "
        "on WPP estimates and the response is a cited (transferred) assumption; conditional on the stated assumptions"
    ).model_dump(mode="json")


def range_uncertainty(lo: Decimal, hi: Decimal, unit: str, variants: Sequence[str]) -> dict[str, Any]:
    return UncertaintySpec(
        status=UncertaintyStatus.quantified,
        kind=UncertaintyKind.sensitivity_range,
        description=(
            f"Sensitivity range ({ra.SENSITIVITY_STATEMENT}): min/max over the cited employment-response grid, "
            "the no-response reference point and the two pension-drop bounding variants (rho of ages 55-59 / "
            "60-64 at every age). No coverage level."
        ),
        method_version=SENSITIVITY_METHOD,
        lower=ra.present(lo, unit),
        upper=ra.present(hi, unit),
        conditioning=tuple(variants),
    ).model_dump(mode="json")


def applicability(
    plan: CountryPlan, point: lit.GridPoint, variant: str, output: str, rho_basis: str = "own_group"
) -> dict[str, Any]:
    assert plan.interventions is not None and plan.data is not None
    lever = dict(next(iter(plan.interventions.values())).reference.table.model_lever)
    caveats = [str(lever.get("qualification") or ""), str(lever.get("rationale", "")), PENSION_DROP_LABEL]
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
        "data_sources": DATA_SOURCES,
        "caveats": [c for c in caveats if c],
        "not_claimed": list(NOT_CLAIMED),
    }


def evaluations(plan: CountryPlan, fidelity: str) -> list[tuple[str, lit.GridPoint, str]]:
    assert plan.central is not None
    if fidelity != "L2":
        return [("L1", plan.central, "own_group")]
    out = [(f"L2:{p.point_id}", p, "own_group") for p in plan.points]
    out += [(f"L2:{plan.central.point_id};pension_drop={b}", plan.central, b) for b in PENSION_DROP_BASES[1:]]
    return out


def rows_for_country(plan: CountryPlan, compiled: Compiled, fidelity: str, row_cls: Any) -> list[Any]:
    assert plan.central is not None
    evals = evaluations(plan, fidelity)
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


def explain(fidelity: str | None = None) -> dict[str, Any]:
    return {
        "model": f"{MODEL_KEY}@{MODEL_VERSION}",
        "label": LABEL,
        "model_label": EXPERIMENTAL_LABEL,
        "review_status": REVIEW_STATUS,
        "fidelity": fidelity,
        "method": (
            "legal eligibility age by sex (rule table; WBL 2026 age held constant unless a law-text table exists) -> "
            "share of person-time below it per single age 55-64 and year -> cited response (s) and own-country "
            "pension-receipt proxy (rho, ILOSTAT participation); population from the cohort-component projection on "
            "WPP 2024 estimates; ILOSTAT employment rates of the latest complete year held constant"
        ),
        "data_sources": DATA_SOURCES,
        "ages": [AGES[0], AGES[-1]],
        "not_computed": NOT_AVAILABLE,
        "pension_drop": PENSION_DROP_LABEL,
        "outputs": {k: v["label"] for k, v in OUTPUTS.items()},
        "not_claimed": list(NOT_CLAIMED),
    }


def implementation_files() -> tuple[Path, ...]:
    here = Path(__file__)
    from reva.models.population import cohort, wpp

    return (
        here,
        here.with_name("retirement_age.py"),
        here.with_name("rules.py"),
        here.with_name("assumptions.py"),
        Path(cohort.__file__),
        Path(wpp.__file__),
    )


def data_files() -> tuple[Path, ...]:
    files = [*ra.data_files(), *sorted(rl.WBL_RULE_DIR.glob("*.yaml"))]
    return tuple(f for f in files if f.exists())


def manifest_dict() -> dict[str, Any]:
    from reva.simulation.plugins import source_digest

    tables = {c: rl.load_rule_table(c).digest() for c in rl.available_countries()}
    tables.update({c: rl.load_wbl_rule_table(c).digest() for c in rl.wbl_countries() if c not in tables})
    return {
        "implementation": "reva.models.social.retirement_world",
        "implementation_digest": source_digest(*implementation_files(), *data_files()),
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
            "employment response: cited European literature values (transferred outside their country, labelled)",
            PENSION_DROP_LABEL,
            "WBL 2026 tables: the age in force on 1 October 2025 held constant for every claim year (counterfactual)",
            "eligibility modelled at single ages 55-64 only (ILOSTAT has no 65-69 band); other requests refused",
            "population from the cohort-component projection on WPP 2024 estimates",
        ],
        "max_end_year": ra.MAX_END_YEAR,
    }


def spec_schema() -> dict[str, Any]:
    import copy

    sch = copy.deepcopy(ra.spec_schema())
    sch["reference_year"] = {"type": "integer", "minimum": ra.MIN_REFERENCE_YEAR, "maximum": MAX_JUMP_OFF - 1}
    sch["outputs"] = {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"enum": sorted(OUTPUTS)}}
    sch["countries"] = {
        "type": "array",
        "items": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "minItems": 1,
        "uniqueItems": True,
    }
    for branch in sch["intervention"]["oneOf"]:
        if branch["properties"]["kind"].get("const") == "retirement_age_target":
            branch["properties"]["target_age_months"] = {"type": "integer", "minimum": 12 * 40, "maximum": 12 * 80}
    pop = sch["assumptions"]["properties"]["population"]["properties"]
    pop["migration"] = {"enum": ["zero", "average_residual"]}
    pop["fertility"] = {"enum": ["constant_base_period_rates"]}
    return sch


class RetirementAgeWorldModel:
    """Plugin ``social.retirement_age@1.1.0`` (experimental, review pending)."""

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
        for iso3 in countries_of(spec):
            for name, concept in CONCEPTS.items():
                yrs = (
                    tuple(range(b - MAX_RATES_AGE, b + 1))
                    if name in ("employment_rate", "activity_rate")
                    else tuple(range(b - 9, b + 2))
                )
                out.append(InputRequirement(name, concept, iso3, yrs, "dataset"))
        return out

    def compile(self, inputs: Any, spec: dict[str, Any]) -> Compiled:
        b, years = ra.years_of(spec)
        plans = []
        for iso3 in countries_of(spec):
            tables = {n: (None if (ds := inputs.dataset(n, iso3)).is_missing else ds.table) for n in CONCEPTS}
            plans.append(build_country_plan(iso3, inputs.subject(iso3).entity_id, spec, tables))
        outputs = tuple(spec.get("outputs") or DEFAULT_OUTPUTS)
        return Compiled(
            dict(spec), b, years, outputs, plans, str(spec.get("missing_input_policy", "fail")), dict(inputs.release)
        )

    def compute(self, compiled: Compiled, fidelity: str) -> list[Any]:
        from reva.simulation.plugins import ResultRow

        if fidelity not in self.fidelities:
            raise CapabilityGap(
                f"{MODEL_KEY}@{MODEL_VERSION} computes L1 and L2 only", capability=f"fidelity.{fidelity}"
            )
        rows: list[Any] = []
        for plan in compiled.countries:
            if plan.status == "ready":
                rows.extend(rows_for_country(plan, compiled, fidelity, ResultRow))
        from reva.models.population.cohort_world import result_size_guard

        result_size_guard(rows, f"{MODEL_KEY}@{MODEL_VERSION}")
        return sorted(rows, key=lambda r: r.sort_key())

    def manifest(self) -> dict[str, Any]:
        return manifest_dict()

    def spec_schema(self) -> dict[str, Any]:
        return spec_schema()

    def explanation(self, fidelity: str) -> dict[str, Any]:
        return explain(fidelity)

    def run_applicability(self, compiled: Compiled, fidelity: str) -> dict[str, Any]:
        countries = {}
        for c in compiled.countries:
            entry: dict[str, Any] = {"status": c.status, "problems": c.problems, "notes": c.notes}
            if c.interventions is not None:
                entry["intervention"] = {s: iv.as_json() for s, iv in c.interventions.items()}
            if c.central is not None:
                entry["central_grid_point"] = c.central.point_id
                entry["grid_points"] = [p.point_id for p in c.points]
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


MODEL = RetirementAgeWorldModel()


def _register() -> None:
    from reva.simulation import plugins

    plugins.register(MODEL)


_register()
