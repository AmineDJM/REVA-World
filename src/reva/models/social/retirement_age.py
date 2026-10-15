"""``social.retirement_age@1.0.0`` - pension eligibility-age changes, employment and pension recipients.

Experimental (``review_status: maintainer_review_pending``). A *conditional projection
under stated assumptions*, not a forecast. Pure: no database, network or clock; the
platform resolves the dataset inputs pinned by the scenario's baseline release.

What it computes (annual periods ``reference_year+1 .. reference_year+periods``, at most
2040; ages 55-69 by recorded sex; Decimal, precision 50)
------------------------------------------------------------------------------------
* **Eligibility.** For each calendar year ``t``, completed age ``a`` and rule path ``P`` the
  exact share ``I_P(a, t)`` of person-time below the path's eligibility age
  (:mod:`reva.models.social.rules`: births uniform within month cohorts, exact age uniform
  within the completed year; rows transcribed from archived legal text).
* **Affected population.** ``A(a, s, t) = N(a, s, t) * (I_scn(a, t) - I_ref(a, t))`` with
  ``N`` the population at completed age ``a`` during year ``t`` (mean of the 1 January
  populations of ``t`` and ``t+1`` from the ``demography.cohort_component`` projection of
  agent POP).
* **Pension-receipt drop (own-country data).** ``rho(g, s) = 1 - activity_rate(g, s) / 100``
  of the base year (LFS ``lfsa_argan``): the share of the five-year age group outside the
  labour force, applied to the affected single ages. It is a *proxy of unknown direction*
  for the share of an affected cell that would otherwise draw a pension: it counts inactive
  persons who are not pensioners (homemakers, disability, long careers) and omits unemployed
  persons who would claim; and a group mean overstates at ages just above the old minimum
  age (still mostly active) but can understate at ages already mostly retired under the
  base-year regime (e.g. 62-64 for the French 2023 reform, base 2024). It is never taken from
  a foreign study. Pension recipients change ``-rho * A``. **Bounding variants (L2):** the
  central grid point is re-run with the 60-64 group's rho and with the 65-69 group's rho
  applied at every single age (variants ``L2:<central>;pension_drop=Y60-64`` / ``=Y65-69``);
  they enter the sensitivity range. They bracket the group-mean proxy, not the unknown truth.
* **Employment response (cited assumption grid).** Per grid point (literature registry of
  agent LIT, ``verified_from_text`` values only): ``s`` = share of the persons kept out of
  pension receipt who are employed instead. Employment change ``s * rho * A``.
* **Levels.** Employment rates of each five-year group are held at their base-year LFS
  values (``lfsa_ergan``) and shifted by the response to eligibility relative to the
  regime under which the base year was observed (``observed_path``):
  ``e_P(a, s, t) = e_obs(g(a), s) + s * rho(g, s) * (I_P(a, t) - I_obs(a, b))``. Employed persons
  = ``sum N * e``. Pension recipients (level) = base-year ESSPROS old-age beneficiaries
  scaled by the 65+ population (constant beneficiaries-per-65+ ratio, stated assumption)
  plus the modelled change relative to the observed regime.
* **Expenditure (approximate).** ``avg_pension * delta_recipients`` with ``avg_pension`` =
  ESSPROS old-age pension expenditure (OLD + AOLD + PART) / old-age beneficiaries (OLD_TOT)
  of the latest year where both exist, nominal euro of that year, no indexation: labelled
  approximate.
* **Old-age dependency ratio** (persons 65+ per 100 aged 15-64, 1 January) from the POP
  projection; the eligibility age does not change it.

Interventions: a rule path of the table (``retirement_age_rule_path``), a shift of a path
(``retirement_age_shift``) or a target statutory age *level* for cohorts from a birth year
(``retirement_age_target``: ``target_age_months``, e.g. 780 = 65 years, applied to the table's
lever age). The model covers single ages 55-69: a request whose scenario/reference/observed
eligibility differs at any age outside 55-69 in a modelled year (e.g. a shift beyond 70) is
refused with ``capability_gap`` (never silently cut).

Labels: every country's results are ``qualified`` (rule table ``model_lever.qualification``:
early routes such as long careers or disability are not modelled, so the affected population
is overstated where they apply; plus the rho proxy of unknown direction). The qualification is
repeated in each value's basis notes, so a central value is never published unqualified.

Not modelled / not claimed: GDP, unemployment, wages, welfare, taxes, substitution into
unemployment/disability benefits as outputs, behavioural responses at the full-rate age,
exceptions to the general eligibility age (long careers, disability, hardship), any
interaction with the pension amount. L2 is a sensitivity grid over the cited response
assumptions and the two rho bounding variants (no probabilities).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from reva.kernel.errors import CapabilityGap, MissingInput, ValidationProblem
from reva.kernel.ids import logical_hash
from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec, UncertaintyStatus
from reva.models.social import assumptions as lit
from reva.models.social import rules as rl

MODEL_KEY = "social.retirement_age"
MODEL_VERSION = "1.0.0"
REVIEW_STATUS = "maintainer_review_pending"
LABEL = "conditional projection under stated assumptions, not a forecast"
EXPERIMENTAL_LABEL = "experimental model, not yet reviewed"
MAX_END_YEAR = 2040
MIN_REFERENCE_YEAR = 2000
AGES = tuple(range(55, 70))
MODEL_AGE_RANGE_MONTHS = (12 * AGES[0], 12 * (AGES[-1] + 1))  # eligibility ages the model can represent
PENSION_DROP_BASES = ("own_group", "Y60-64", "Y65-69")  # own_group = central; the others are bounding variants
PENSION_DROP_LABEL = ("pension-receipt drop rho = base-year share of the five-year age group outside the labour "
                      "force (LFS): a proxy of unknown direction (overstates at ages still mostly active, can "
                      "understate at ages already mostly retired; includes non-pensioner inactivity, omits "
                      "unemployed claimants)")
AGE_GROUPS: tuple[tuple[str, int, int], ...] = (("Y55-59", 55, 59), ("Y60-64", 60, 64), ("Y65-69", 65, 69))
SEXES = ("F", "M")
PRECISION = 50
CTX = Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)
SENSITIVITY_VARIANT = "L2:sensitivity_range"
SENSITIVITY_STATEMENT = "assumption sensitivity over cited literature values, not a confidence interval"
SENSITIVITY_METHOD = "reva.models.social.retirement_age.l2_literature_grid_minmax@1.0.0"

CONCEPTS = {
    "employment_rate": "labour.employment_rate.lfs_age_sex",
    "activity_rate": "labour.activity_rate.lfs_age_sex",
    "pension_beneficiaries": "social.pension_beneficiaries.esspros_old_age",
    "pension_expenditure": "social.pension_expenditure.esspros",
    "population": "demography.population.jan1_age_sex",
    "deaths": "demography.deaths.age_sex",
    "births": "demography.live_births.mother_age_newborn_sex",
}

OUTPUTS: dict[str, dict[str, str]] = {
    "employed_persons_55_69": {"unit": "unit.persons", "kind": "level", "label":
                               "Employed persons aged 55-69 under the scenario rule path"},
    "employed_persons_55_69_reference": {"unit": "unit.persons", "kind": "level", "label":
                                         "Employed persons aged 55-69 under the reference rule path"},
    "employment_change_55_69": {"unit": "unit.persons", "kind": "change", "label":
                                "Employed persons aged 55-69: scenario minus reference"},
    "pension_recipients": {"unit": "unit.persons", "kind": "level", "label":
                           "Old-age pension recipients under the scenario rule path (base-year ESSPROS level "
                           "scaled with the 65+ population, plus the modelled change)"},
    "pension_recipients_change": {"unit": "unit.persons", "kind": "change", "label":
                                  "Old-age pension recipients: scenario minus reference"},
    "pension_expenditure_change_approx_eur": {"unit": "unit.eur", "kind": "change", "label":
                                              "Approximate pension expenditure change = average old-age pension "
                                              "x change in recipients (nominal euro of the average's year, "
                                              "not indexed)"},
    "old_age_dependency_ratio": {"unit": "unit.ratio", "kind": "level", "label":
                                 "Persons 65+ per 100 persons aged 15-64 on 1 January (POP projection)"},
}
for _sex in SEXES:
    for _g, _lo, _hi in AGE_GROUPS:
        OUTPUTS[f"employment_rate.{_sex}.{_g}"] = {
            "unit": "unit.percent", "kind": "level",
            "label": f"Employment rate (percent), recorded sex {_sex}, ages {_lo}-{_hi}, scenario rule path"}
DEFAULT_OUTPUTS = ("employed_persons_55_69", "employment_change_55_69", "pension_recipients",
                   "pension_recipients_change", "pension_expenditure_change_approx_eur", "old_age_dependency_ratio")

NOT_CLAIMED = ("GDP or output effects", "unemployment, disability or other benefit substitution as outputs",
               "wages, taxes, contributions or household welfare", "effects of full-rate / normal-age changes",
               "exceptions to the general eligibility age (long careers, disability, hardship)",
               "the direction of the pension-receipt proxy rho (neither an upper nor a lower bound)",
               "eligibility ages outside single ages 55-69 (refused, never cut)",
               "a forecast of what will happen")


def D(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def fr(x: Fraction) -> Decimal:
    return CTX.divide(Decimal(x.numerator), Decimal(x.denominator))


def dstr(d: Decimal) -> str:
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def group_of(age: int) -> str:
    for g, lo, hi in AGE_GROUPS:
        if lo <= age <= hi:
            return g
    raise ValueError(age)


# --------------------------------------------------------------------------- spec handling
INTERVENTION_KINDS = ("retirement_age_rule_path", "retirement_age_shift", "retirement_age_target")


@dataclass(frozen=True)
class Intervention:
    kind: str
    scenario: Any  # rl.AgePath
    reference: rl.TablePath
    observed: rl.AgePath

    def as_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "scenario_path": self.scenario.describe(),
                "reference_path": self.reference.describe(), "observed_regime_path": self.observed.describe()}


def _lever(table: rl.RuleTable) -> tuple[str, str]:
    lever = dict(table.model_lever)
    if not lever:
        raise CapabilityGap(f"{table.country}: the rule table declares no model lever", capability=MODEL_KEY,
                            country=table.country)
    return str(lever["age_kind"]), str(lever.get("track", "general"))


def compile_intervention(iso3: str, intervention: Mapping[str, Any], assumptions: Mapping[str, Any]) -> Intervention:
    table = rl.load_rule_table(iso3)
    age_kind, track = _lever(table)
    kind = intervention.get("kind")
    ref_id = intervention.get("reference_path")
    if not isinstance(ref_id, str):
        raise ValidationProblem("intervention.reference_path is required")
    reference = rl.TablePath(table, ref_id, age_kind, track)
    if kind == "retirement_age_rule_path":
        scn_id = intervention.get("scenario_path")
        if not isinstance(scn_id, str):
            raise ValidationProblem("intervention.scenario_path is required")
        if scn_id == ref_id:
            raise ValidationProblem("scenario_path and reference_path must differ")
        scenario: Any = rl.TablePath(table, scn_id, age_kind, track)
    elif kind == "retirement_age_shift":
        scenario = rl.ShiftedPath(reference, int(intervention["shift_months"]), int(intervention["first_birth_year"]),
                                  int(intervention.get("phase_in_months_per_cohort", 0)))
    elif kind == "retirement_age_target":
        target = intervention.get("target_age_months")
        if not isinstance(target, int) or isinstance(target, bool):
            raise ValidationProblem("intervention.target_age_months (integer months of age, e.g. 780 = 65 years) "
                                    "is required")
        first = intervention.get("first_birth_year")
        if not isinstance(first, int) or isinstance(first, bool):
            raise ValidationProblem("intervention.first_birth_year (first birth cohort the target applies to) is "
                                    "required")
        scenario = rl.TargetPath(reference, target, first, int(intervention.get("phase_in_months_per_cohort", 0)))
    else:
        raise CapabilityGap(f"intervention kind {kind!r} is unsupported", capability=f"intervention.{kind}",
                            supported=list(INTERVENTION_KINDS))
    obs_id = assumptions.get("observed_path", ref_id)
    observed = rl.TablePath(table, str(obs_id), age_kind, track)
    return Intervention(str(kind), scenario, reference, observed)


def years_of(spec: Mapping[str, Any]) -> tuple[int, tuple[int, ...]]:
    b = spec.get("reference_year")
    h = spec.get("horizon") or {}
    if not isinstance(b, int) or isinstance(b, bool):
        raise ValidationProblem("reference_year (the base year of the employment rates) must be an integer")
    if h.get("unit") != "calendar_year" or not isinstance(h.get("periods"), int):
        raise ValidationProblem("horizon must be {periods: n, unit: calendar_year}")
    n = int(h["periods"])
    if not MIN_REFERENCE_YEAR <= b < MAX_END_YEAR:
        raise ValidationProblem(f"reference_year must be in [{MIN_REFERENCE_YEAR}, {MAX_END_YEAR - 1}]")
    if n < 1 or b + n > MAX_END_YEAR:
        raise ValidationProblem(f"horizon must end by {MAX_END_YEAR}: reference_year + periods <= {MAX_END_YEAR}")
    return b, tuple(range(b + 1, b + n + 1))


def grid_for(iso3: str, assumptions: Mapping[str, Any], fidelity: str) -> tuple[lit.GridPoint, tuple[lit.GridPoint,
                                                                                                     ...]]:
    grid = lit.load_grid()
    resp = dict(assumptions.get("employment_response") or {})
    central_id = resp.get("central")
    if central_id is None:
        central = grid.default_central(iso3)
        if central is None:
            raise ValidationProblem(
                f"{iso3}: no single verified own-country estimate; assumptions.employment_response.central must "
                "name a literature grid point", available=[p.point_id for p in grid.literature()])
    else:
        central = grid.point(str(central_id))
    points = lit.select_points(grid, resp.get("grid"))
    if fidelity == "L2":
        pts = [central, *[p for p in points if p.point_id != central.point_id]]
        pts.append(lit.no_response_point(central))
        return central, tuple(pts)
    return central, (central,)


def validate_spec(spec: Mapping[str, Any]) -> None:
    countries = spec.get("countries") or []
    if not countries:
        raise ValidationProblem("countries must list at least one ISO3 code")
    b, years = years_of(spec)
    outputs = spec.get("outputs") or list(DEFAULT_OUTPUTS)
    unknown = [o for o in outputs if o not in OUTPUTS]
    if unknown:
        raise CapabilityGap(f"outputs {unknown} are not computed by {MODEL_KEY}", capability="outputs",
                            supported=sorted(OUTPUTS))
    fidelity = spec.get("fidelity", "L1")
    for iso3 in countries:
        iv = compile_intervention(iso3, spec.get("intervention") or {}, spec.get("assumptions") or {})
        grid_for(iso3, spec.get("assumptions") or {}, fidelity if fidelity in ("L1", "L2") else "L1")
        gaps = []
        for p in (iv.scenario, iv.reference):
            gaps += rl.coverage_problems(p, years, AGES)
        gaps += rl.coverage_problems(iv.observed, (b,), AGES)
        if gaps:
            raise rl.RuleCoverageGap(
                f"{iso3}: the rule paths do not cover every (year, age) cell of the request "
                f"({len(gaps)} cells, e.g. {gaps[0]})", capability="social.retirement_age", country=iso3,
                cells=gaps[:20])
        check_age_range(iso3, iv, b, years)
    pop = dict((spec.get("assumptions") or {}).get("population") or {})
    n_base = pop.get("base_period_years", 5)
    if (pop.get("migration", "average_residual") == "average_residual" and n_base == 1
            and not pop.get("allow_single_year_migration", False)):
        raise ValidationProblem("assumptions.population.base_period_years = 1 with average-residual migration would "
                                "hold one year's residual constant; refused unless "
                                "assumptions.population.allow_single_year_migration is true (or use another "
                                "migration assumption)")


def check_age_range(iso3: str, iv: Intervention, base_year: int, years: Sequence[int]) -> None:
    """Refuse (``capability_gap``) when eligibility differs between the compared paths at an age outside
    55-69 in a modelled year: those persons would be affected but not counted (never silently cut)."""
    paths = (iv.scenario, iv.reference, iv.observed)
    lo = min(p.age_bounds_months()[0] for p in paths)
    hi = max(p.age_bounds_months()[1] for p in paths)
    outer = [*range(max(lo // 12, 0), AGES[0])] if lo < MODEL_AGE_RANGE_MONTHS[0] else []
    if hi > MODEL_AGE_RANGE_MONTHS[1]:
        outer += list(range(AGES[-1] + 1, hi // 12 + 1))
    cells: list[dict[str, Any]] = []
    for a in outer:
        i_obs = rl.ineligible_share_single_age(iv.observed, base_year, a)
        for t in years:
            i_s = rl.ineligible_share_single_age(iv.scenario, t, a)
            i_r = rl.ineligible_share_single_age(iv.reference, t, a)
            if i_s != i_r or i_s != i_obs:
                cells.append({"year": t, "age": a, "scenario_share_below": str(i_s),
                              "reference_share_below": str(i_r), "observed_share_below": str(i_obs)})
    if cells:
        raise CapabilityGap(
            f"{iso3}: the requested eligibility ages leave the model's age range 55-69 ({len(cells)} affected "
            f"(year, age) cells outside it, e.g. age {cells[0]['age']} in {cells[0]['year']}); the model refuses "
            "rather than cut them", capability="social.retirement_age.age_range", country=iso3,
            model_ages=[AGES[0], AGES[-1]], cells=cells[:20])


# --------------------------------------------------------------------------- inputs
@dataclass
class CountryData:
    """Base-year data of one country (all values exact decimals)."""

    iso3: str
    base_year: int
    employment_rate: dict[tuple[str, str], Decimal]  # (sex, group) -> percent
    employment_flags: dict[tuple[str, str], str]
    activity_rate: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    beneficiaries: Decimal | None = None
    beneficiaries_year: int | None = None
    avg_pension_eur: Decimal | None = None
    avg_pension_year: int | None = None
    avg_pension_basis: dict[str, Any] = field(default_factory=dict)
    population: dict[int, dict[str, tuple[Decimal, ...]]] = field(default_factory=dict)  # Jan 1
    dependency: dict[int, Decimal] = field(default_factory=dict)
    population_summary: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)


def _rows(table: Any) -> list[dict[str, Any]]:
    if table is None:
        return []
    if hasattr(table, "to_pylist"):
        return list(table.to_pylist())
    return [dict(r) for r in table]


RateTable = dict[tuple[str, str], Decimal]


def employment_rates(rows: Sequence[Mapping[str, Any]], year: int, name: str = "employment_rate"
                     ) -> tuple[RateTable, dict[tuple[str, str], str], list[dict[str, Any]]]:
    rates: dict[tuple[str, str], Decimal] = {}
    flags: dict[tuple[str, str], str] = {}
    missing: list[dict[str, Any]] = []
    have = {(r["dim1"], r["dim2"]): r for r in rows if r.get("year") == year and r.get("dim1_name") == "sex"}
    for s in SEXES:
        for g, _lo, _hi in AGE_GROUPS:
            r = have.get((s, g))
            if r is None or r.get("value") is None:
                missing.append({"input": name, "sex": s, "age_group": g, "year": year,
                                "reason": (r or {}).get("missing_reason") or "unavailable"})
                continue
            rates[(s, g)] = D(r["value"])
            flags[(s, g)] = str(r.get("status") or "")
    return rates, flags, missing


def latest_beneficiaries(ben_rows: Sequence[Mapping[str, Any]], max_year: int) -> dict[str, Any]:
    """Latest year <= max_year with old-age beneficiaries (both sexes) present."""
    ben = {r["year"]: r for r in ben_rows if r.get("dim1") == "T" and r.get("value") is not None
           and r["year"] <= max_year}
    if not ben:
        return {}
    y = max(ben)
    return {"year": y, "beneficiaries": D(ben[y]["value"]) * D(ben[y]["scale_factor"]),
            "flag": ben[y].get("status", "")}


def latest_pension_basis(ben_rows: Sequence[Mapping[str, Any]], exp_rows: Sequence[Mapping[str, Any]],
                         max_year: int) -> dict[str, Any]:
    """Latest year <= max_year with old-age beneficiaries (T) and OLD, AOLD and PART expenditure all present.

    A component missing in the source is never treated as zero: such a year is skipped, and when no
    year qualifies the expenditure output is not computed."""
    ben = {r["year"]: r for r in ben_rows if r.get("dim1") == "T" and r.get("value") is not None}
    exp: dict[int, dict[str, Mapping[str, Any]]] = {}
    for r in exp_rows:
        if r.get("dim1") in ("OLD", "AOLD", "PART") and r.get("value") is not None:
            exp.setdefault(r["year"], {})[r["dim1"]] = r
    for y in sorted(ben, reverse=True):
        if y > max_year:
            continue
        e = exp.get(y, {})
        if set(e) == {"OLD", "AOLD", "PART"}:
            total = sum((D(e[k]["value"]) * D(e[k]["scale_factor"]) for k in ("OLD", "AOLD", "PART")), Decimal(0))
            persons = D(ben[y]["value"]) * D(ben[y]["scale_factor"])
            return {"year": y, "beneficiaries": persons, "expenditure_eur": total,
                    "avg_pension_eur": CTX.divide(total, persons),
                    "flags": {"beneficiaries": ben[y].get("status"), **{k: e[k].get("status") for k in e}}}
    return {}


def population_inputs(iso3: str, base_year: int, end_year: int, tables: Mapping[str, Any],
                      assumptions: Mapping[str, Any]) -> tuple[dict[int, dict[str, tuple[Decimal, ...]]],
                                                               dict[int, Decimal], dict[str, Any]]:
    """Jan-1 populations base_year+1 .. end_year+1 and dependency ratios via POP's ``project``."""
    from reva.models.population import cohort

    pa = dict(assumptions.get("population") or {})
    n_base = int(pa.get("base_period_years", 5))
    if not 1 <= n_base <= 10:
        raise ValidationProblem("assumptions.population.base_period_years must be in [1, 10]")
    jump = base_year + 1
    data = cohort.CountryDemography.from_tables(iso3, population=tables["population"], deaths=tables["deaths"],
                                                births=tables["births"])
    ass = cohort.Assumptions(migration=str(pa.get("migration", "average_residual")),
                             fertility=str(pa.get("fertility", "constant_base_period_rates")),
                             base_period=(jump - n_base, jump - 1),
                             migration_breaks=str(pa.get("migration_breaks", "exclude")),
                             allow_single_year_migration=bool(pa.get("allow_single_year_migration", False)))
    proj = cohort.project(data, jump_off=jump, end_year=end_year + 1, assumptions=ass)
    values = cohort.output_values(proj)
    dep = {y: values[("old_age_dependency_ratio_65plus_per_15_64", y)] for y in sorted(proj.population)}
    summary = proj.summary()
    summary["observed_population_65_plus_jan1"] = {str(y): dstr(v) for y, v in observed_65_plus(data).items()}
    return dict(proj.population), dep, summary


def observed_65_plus(data: Any) -> dict[int, Decimal]:
    """Observed 1 January population aged 65+ (both sexes) per year: sum of leaf age cells starting at 65 or
    above; a year with any such leaf missing is left out (never zero-filled)."""
    out: dict[int, Decimal] = {}
    for y in sorted({yy for (yy, _s) in data.population}):
        total, ok = Decimal(0), True
        for s in SEXES:
            cells = data.population.get((y, s)) or {}
            leaves = [c for c in cells.values() if c.cell_type == "leaf" and c.start is not None and c.start >= 65
                      and c.kind not in ("total", "unknown")]
            if not leaves or any(c.value is None for c in leaves):
                ok = False
                break
            total += sum((c.value for c in leaves), Decimal(0))
        if ok:
            out[y] = total
    return out


# --------------------------------------------------------------------------- compiled plan
@dataclass
class CountryPlan:
    iso3: str
    subject_id: Any
    intervention: Intervention | None
    central: lit.GridPoint | None
    points: tuple[lit.GridPoint, ...]
    data: CountryData | None
    status: str = "ready"
    problems: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    shares: dict[str, dict[tuple[int, int], Fraction]] = field(default_factory=dict)  # path -> (year, age) -> I


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
        probs = [p for c in self.countries for p in c.problems if p.get("code") == "missing_input"]
        if self.policy == "skip_missing_countries" and any(c.status == "ready" for c in self.countries):
            return []
        return probs

    @property
    def incompatible(self) -> list[dict[str, Any]]:
        return [p for c in self.countries for p in c.problems if p.get("code") != "missing_input"]

    @property
    def skipped(self) -> list[str]:
        return [c.iso3 for c in self.countries if c.status != "ready"]


def eligibility_shares(iv: Intervention, base_year: int, years: Sequence[int]) -> dict[str, dict[tuple[int, int],
                                                                                             Fraction]]:
    out: dict[str, dict[tuple[int, int], Fraction]] = {"scenario": {}, "reference": {}, "observed": {}}
    for y in years:
        for a in AGES:
            out["scenario"][(y, a)] = rl.ineligible_share_single_age(iv.scenario, y, a)
            out["reference"][(y, a)] = rl.ineligible_share_single_age(iv.reference, y, a)
    for a in AGES:
        out["observed"][(base_year, a)] = rl.ineligible_share_single_age(iv.observed, base_year, a)
    return out


def build_country_plan(iso3: str, subject_id: Any, spec: Mapping[str, Any], tables: Mapping[str, Any]) -> CountryPlan:
    """Pure: ``tables`` maps input names to Arrow tables (or None when the release pins nothing)."""
    b, years = years_of(spec)
    fidelity = str(spec.get("fidelity", "L1"))
    assumptions = dict(spec.get("assumptions") or {})
    iv = compile_intervention(iso3, spec.get("intervention") or {}, assumptions)
    central, points = grid_for(iso3, assumptions, fidelity)
    plan = CountryPlan(iso3, subject_id, iv, central, points, None)
    missing: list[dict[str, Any]] = []
    for name in CONCEPTS:
        if tables.get(name) is None:
            missing.append({"code": "missing_input", "country": iso3, "input": name, "concept": CONCEPTS[name],
                            "kind": "dataset", "reason": "unavailable"})
    if missing:
        plan.status, plan.problems = "missing_input", missing
        return plan
    rates, flags, miss = employment_rates(_rows(tables["employment_rate"]), b)
    act, _aflags, amiss = employment_rates(_rows(tables["activity_rate"]), b, "activity_rate")
    for m in [*miss, *amiss]:
        plan.problems.append({"code": "missing_input", "country": iso3, "concept": CONCEPTS[m["input"]],
                              "reference_year": b, **m})
    for key, e in rates.items():
        if key in act and e > act[key]:
            plan.problems.append({"code": "inconsistent_input", "country": iso3, "detail":
                                  f"employment rate {e} exceeds activity rate {act[key]} for {key} in {b}"})
    bens = latest_beneficiaries(_rows(tables["pension_beneficiaries"]), b)
    if not bens:
        plan.problems.append({"code": "missing_input", "country": iso3, "input": "pension_beneficiaries",
                              "concept": CONCEPTS["pension_beneficiaries"], "reference_year": b,
                              "reason": "no year <= reference_year with old-age pension beneficiaries"})
    basis = latest_pension_basis(_rows(tables["pension_beneficiaries"]), _rows(tables["pension_expenditure"]), b)
    if not basis:
        plan.notes.append({"code": "output_not_computable", "output": "pension_expenditure_change_approx_eur",
                           "reason": "no year <= reference_year with old-age beneficiaries and OLD, AOLD and PART "
                                     "expenditure all present in ESSPROS (a missing component is never zero)"})
    try:
        pop, dep, summary = population_inputs(iso3, b, years[-1], tables, assumptions)
    except MissingInput as e:
        for m in e.details.get("missing", []) or [{"detail": e.message}]:
            plan.problems.append({"code": "missing_input", "country": iso3, "input": "population_projection",
                                  "concept": "demography inputs of demography.cohort_component", **m})
        pop, dep, summary = {}, {}, {}
    if plan.problems:
        plan.status = "missing_input" if any(p["code"] == "missing_input" for p in plan.problems) else "incompatible"
        return plan
    plan.data = CountryData(
        iso3=iso3, base_year=b, employment_rate=rates, employment_flags=flags, activity_rate=act,
        beneficiaries=bens["beneficiaries"], beneficiaries_year=bens["year"],
        avg_pension_eur=basis.get("avg_pension_eur"), avg_pension_year=basis.get("year"),
        avg_pension_basis={"year": basis["year"], "expenditure_eur": dstr(basis["expenditure_eur"]),
                           "beneficiaries": dstr(basis["beneficiaries"]), "flags": basis["flags"],
                           "definition": "ESSPROS spr_exp_pens (OLD + AOLD + PART, MIO_EUR) / spr_pns_ben (OLD_TOT, "
                                         "T, persons); nominal euro of that year"} if basis else {
            "status": "not_computable", "reason": plan.notes[-1]["reason"]},
        population=pop, dependency=dep, population_summary=summary)
    obs65 = summary.get("observed_population_65_plus_jan1") or {}
    by = bens["year"]
    if str(by) not in obs65 or str(by + 1) not in obs65:
        plan.notes.append({"code": "output_not_computable", "output": "pension_recipients",
                           "reason": f"no observed 65+ population on 1 January {by} and {by + 1} to scale the "
                                     "ESSPROS beneficiaries"})
    plan.shares = eligibility_shares(iv, b, years)
    return plan


# --------------------------------------------------------------------------- computation
def pension_drop(d: CountryData, sex: str, group: str, basis: str = "own_group") -> Decimal:
    """Own-country proxy of unknown direction: share outside the labour force in the base year of the
    age's own five-year group (``own_group``, central) or of a named group applied at every age (bounding
    variants ``Y60-64`` / ``Y65-69``)."""
    if basis not in PENSION_DROP_BASES:
        raise ValidationProblem(f"unknown pension-drop basis {basis!r}", allowed=list(PENSION_DROP_BASES))
    g = group if basis == "own_group" else basis
    return Decimal(1) - CTX.divide(d.activity_rate[(sex, g)], Decimal(100))


def n_mid(pop: Mapping[int, Mapping[str, Sequence[Decimal]]], t: int, s: str, a: int) -> Decimal:
    return CTX.divide(CTX.add(pop[t][s][a], pop[t + 1][s][a]), Decimal(2))


@dataclass(frozen=True)
class YearValues:
    year: int
    values: dict[str, Decimal]


def compute_point(plan: CountryPlan, point: lit.GridPoint, years: Sequence[int], rho_basis: str = "own_group"
                  ) -> tuple[dict[str, dict[int, Decimal]], list[dict[str, Any]]]:
    """All outputs for one grid point (and pension-drop basis). Returns (output -> year -> value, problems)."""
    d = plan.data
    assert d is not None
    sh = plan.shares
    b = d.base_year
    out: dict[str, dict[int, Decimal]] = {k: {} for k in OUTPUTS}
    problems: list[dict[str, Any]] = []
    obs65 = {int(k): D(v) for k, v in (d.population_summary.get("observed_population_65_plus_jan1") or {}).items()}
    p65_base = None
    by = d.beneficiaries_year
    if by is not None and by in obs65 and by + 1 in obs65:
        p65_base = CTX.divide(obs65[by] + obs65[by + 1], Decimal(2))  # mid-year, as N(t) below
    for t in years:
        emp_s = emp_r = d_rec_scn_obs = Decimal(0)
        d_emp = d_rec = Decimal(0)
        grp_num: dict[tuple[str, ...], Decimal] = {}
        grp_den: dict[tuple[str, str], Decimal] = {}
        for s in SEXES:
            for a in AGES:
                n = n_mid(d.population, t, s, a)
                g = group_of(a)
                rho = pension_drop(d, s, g, rho_basis)
                k = point.share_employed[s] * rho
                base = CTX.divide(d.employment_rate[(s, g)], Decimal(100))
                i_obs = fr(sh["observed"][(b, a)])
                i_s, i_r = fr(sh["scenario"][(t, a)]), fr(sh["reference"][(t, a)])
                e_s = base + k * (i_s - i_obs)
                e_r = base + k * (i_r - i_obs)
                emp_s += n * e_s
                emp_r += n * e_r
                d_emp += k * n * (i_s - i_r)
                d_rec -= rho * n * (i_s - i_r)
                d_rec_scn_obs -= rho * n * (i_s - i_obs)
                grp_num[(s, g)] = grp_num.get((s, g), Decimal(0)) + n * e_s
                grp_num[(s, g, "r")] = grp_num.get((s, g, "r"), Decimal(0)) + n * e_r
                grp_den[(s, g)] = grp_den.get((s, g), Decimal(0)) + n
        out["employed_persons_55_69"][t] = emp_s
        out["employed_persons_55_69_reference"][t] = emp_r
        out["employment_change_55_69"][t] = d_emp
        out["pension_recipients_change"][t] = d_rec
        if d.avg_pension_eur is not None:
            out["pension_expenditure_change_approx_eur"][t] = CTX.multiply(d.avg_pension_eur, d_rec)
        if p65_base is not None and d.beneficiaries is not None:
            p65_t = sum((n_mid(d.population, t, s, a) for s in SEXES for a in range(65, len(d.population[t][s]))),
                        Decimal(0))
            out["pension_recipients"][t] = CTX.multiply(d.beneficiaries, CTX.divide(p65_t, p65_base)) + d_rec_scn_obs
        out["old_age_dependency_ratio"][t] = d.dependency[t]
        # Bounds are checked on the published group rates. (A single age inside a group can show a
        # rate outside [0, 1]: the base rate is a group average while the eligibility change is
        # concentrated on some ages; only group aggregates are published.)
        for key, num in grp_num.items():
            rate = CTX.divide(num, grp_den[(key[0], key[1])])
            if not Decimal(0) <= rate <= Decimal(1):
                problems.append({"code": "employment_rate_out_of_bounds", "country": d.iso3, "year": t,
                                 "sex": key[0], "age_group": key[1], "path": "reference" if len(key) == 3 else
                                 "scenario", "rate": dstr(rate), "grid_point": point.point_id,
                                 "pension_drop_basis": rho_basis})
            if len(key) == 2:
                out[f"employment_rate.{key[0]}.{key[1]}"][t] = CTX.multiply(Decimal(100), rate)
    if p65_base is None:
        problems.append({"code": "pension_level_not_computable", "country": d.iso3,
                         "detail": f"no observed 65+ population on 1 January {d.beneficiaries_year} and "
                                   f"{(d.beneficiaries_year or 0) + 1}"})
    return out, problems


# --------------------------------------------------------------------------- result rows
QUANTA = {"unit.persons": Decimal("0.001"), "unit.eur": Decimal("0.01"), "unit.ratio": Decimal("0.000001"),
          "unit.percent": Decimal("0.000001")}


def present(d: Decimal, unit: str) -> str:
    return dstr(CTX.quantize(d, QUANTA[unit]))


def qualification(plan: CountryPlan) -> str:
    """The country's qualification (rule table ``model_lever.qualification``) plus the rho caveat."""
    assert plan.intervention is not None
    q = str(dict(plan.intervention.reference.table.model_lever).get("qualification") or "").strip()
    return " ".join(x for x in (f"QUALIFIED ({plan.iso3}): {q}" if q else f"QUALIFIED ({plan.iso3}).",
                                f"Also: {PENSION_DROP_LABEL}.") if x)


def value_dict(output: str, d: Decimal, plan: CountryPlan) -> dict[str, Any]:
    unit = OUTPUTS[output]["unit"]
    label = OUTPUTS[output]["label"] + (" (approximate)" if unit == "unit.eur" else "")
    basis: dict[str, Any] = {"scale_factor": "1", "notes": f"{label}. {qualification(plan)}"}
    if unit == "unit.eur":
        assert plan.data is not None
        basis.update(currency="EUR", price_basis="nominal", price_reference_year=plan.data.avg_pension_year)
    return {"kind": "decimal", "value": present(d, unit), "unit_ref": unit, "basis": basis}


def unknown_uncertainty() -> dict[str, Any]:
    return UncertaintySpec.unknown(
        "Unquantified: LFS/ESSPROS inputs carry no published uncertainty, the population is a conditional projection "
        "and the response is a cited assumption; the value is conditional on the stated assumptions").model_dump(
        mode="json")


def range_uncertainty(lo: Decimal, hi: Decimal, unit: str, variants: Sequence[str]) -> dict[str, Any]:
    return UncertaintySpec(
        status=UncertaintyStatus.quantified,
        kind=UncertaintyKind.sensitivity_range,
        description=(f"Sensitivity range ({SENSITIVITY_STATEMENT}): min/max of this output over the cited "
                     "employment-response grid, the no-response reference point and the two pension-drop "
                     "bounding variants (rho of ages 60-64 / 65-69 at every age). No coverage level."),
        method_version=SENSITIVITY_METHOD,
        lower=present(lo, unit),
        upper=present(hi, unit),
        conditioning=tuple(variants),
    ).model_dump(mode="json")


def applicability(plan: CountryPlan, compiled: Compiled, point: lit.GridPoint, variant: str,
                  output: str, rho_basis: str = "own_group") -> dict[str, Any]:
    assert plan.intervention is not None and plan.data is not None
    lever = dict(plan.intervention.reference.table.model_lever)
    caveats = [str(lever.get("qualification") or ""), str(lever.get("rationale", "")), PENSION_DROP_LABEL]
    for w in (plan.data.population_summary.get("warnings") or []):
        caveats.append(f"population projection warning {w.get('code')}: {w.get('message', '')}")
    if point.country and point.country != plan.iso3:
        caveats.append(f"response estimate transferred from {point.country} ({point.point_id})")
    if point.sex_transfer:
        caveats.append(point.sex_transfer)
    return {
        "status": "qualified",
        "qualification": qualification(plan),
        "pension_drop": {"basis": rho_basis, "direction": "unknown",
                         "role": "central" if rho_basis == "own_group" else "bounding variant"},
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
        "caveats": [c for c in caveats if c],
        "not_claimed": list(NOT_CLAIMED),
    }


def evaluations(plan: CountryPlan, fidelity: str) -> list[tuple[str, lit.GridPoint, str]]:
    """(variant, grid point, pension-drop basis): L1 = central; L2 = every grid point + the central point
    with the two rho bounding variants."""
    assert plan.central is not None
    if fidelity != "L2":
        return [("L1", plan.central, "own_group")]
    out = [(f"L2:{p.point_id}", p, "own_group") for p in plan.points]
    out += [(f"L2:{plan.central.point_id};pension_drop={b}", plan.central, b) for b in PENSION_DROP_BASES[1:]]
    return out


def rows_for_country(plan: CountryPlan, compiled: Compiled, fidelity: str, row_cls: Any) -> list[Any]:
    rows: list[Any] = []
    assert plan.central is not None
    central: lit.GridPoint = plan.central
    evals = evaluations(plan, fidelity)
    results: dict[str, dict[str, dict[int, Decimal]]] = {}
    for variant, p, rho_basis in evals:
        vals, problems = compute_point(plan, p, compiled.years, rho_basis)
        bad = [x for x in problems if x["code"] == "employment_rate_out_of_bounds"]
        if bad:
            raise ValidationProblem(f"{plan.iso3}: an employment rate leaves [0, 100] under grid point "
                                    f"{p.point_id} (pension drop {rho_basis}); the response assumption is "
                                    "incompatible with the base data", problems=bad[:10])
        results[variant] = vals
    for output in compiled.outputs:
        for t in compiled.years:
            period = (date(t, 1, 1), date(t + 1, 1, 1)) if output != "old_age_dependency_ratio" else \
                (date(t, 1, 1), date(t, 1, 2))
            series = [(variant, p, rho_basis, results[variant][output].get(t)) for variant, p, rho_basis in evals]
            if any(v is None for *_x, v in series):
                continue
            for variant, p, rho_basis, v in series:
                assert v is not None
                rows.append(row_cls(output_key=output, iso3=plan.iso3, subject_id=plan.subject_id, variant=variant,
                                    value=value_dict(output, v, plan), uncertainty=unknown_uncertainty(),
                                    applicability=applicability(plan, compiled, p, variant, output, rho_basis),
                                    period=period))
            if fidelity == "L2":
                vs = [v for *_x, v in series if v is not None]
                lo, hi = min(vs), max(vs)
                unit = OUTPUTS[output]["unit"]
                base = value_dict(output, lo, plan)
                names = [variant for variant, *_x in series]
                rows.append(row_cls(
                    output_key=output, iso3=plan.iso3, subject_id=plan.subject_id, variant=SENSITIVITY_VARIANT,
                    value={"kind": "sensitivity_range", "lower": present(lo, unit), "upper": present(hi, unit),
                           "unit_ref": unit, "basis": base["basis"], "evaluated_points": len(vs),
                           "statement": SENSITIVITY_STATEMENT},
                    uncertainty=range_uncertainty(lo, hi, unit, names),
                    applicability={**applicability(plan, compiled, central, SENSITIVITY_VARIANT, output),
                                   "grid_points": [p.point_id for p in plan.points], "range_over": names},
                    period=period))
    return rows


def explain(fidelity: str | None = None) -> dict[str, Any]:
    return {
        "model": f"{MODEL_KEY}@{MODEL_VERSION}",
        "label": LABEL,
        "model_label": EXPERIMENTAL_LABEL,
        "review_status": REVIEW_STATUS,
        "fidelity": fidelity,
        "method": ("legal eligibility age by cohort/claim year (rule tables) -> share of person-time below it per "
                   "age and year -> cited response: pension receipt drop rho and employment share s; population from "
                   "the cohort-component projection; base-year LFS employment rates held constant"),
        "fidelities": {"L1": "central cited grid point (own-country estimate or the one named in the spec)",
                       "L2": "every verified literature grid point plus a no-response reference and the central "
                             "point with the two pension-drop bounding variants (rho of 60-64 / 65-69 at every "
                             "age); separate sensitivity_range rows (min/max), not a confidence interval"},
        "pension_drop": PENSION_DROP_LABEL,
        "labels": "every country is 'qualified' (see each value's basis notes and applicability.qualification)",
        "outputs": {k: v["label"] for k, v in OUTPUTS.items()},
        "not_claimed": list(NOT_CLAIMED),
        "numeric": f"decimal.Decimal precision {PRECISION}, ROUND_HALF_EVEN; eligibility shares exact fractions; "
                   "values rounded only for presentation",
    }


def implementation_files() -> tuple[Path, ...]:
    here = Path(__file__)
    return (here, here.with_name("rules.py"), here.with_name("assumptions.py"))


def data_files() -> tuple[Path, ...]:
    files = [*sorted(rl.RULE_DIR.glob("*.yaml")), lit.ASSUMPTIONS_FILE]
    return tuple(f for f in files if f.exists())


def manifest_dict() -> dict[str, Any]:
    from reva.simulation.plugins import source_digest

    return {
        "implementation": "reva.models.social.retirement_age",
        "implementation_digest": source_digest(*implementation_files(), *data_files()),
        "review_status": REVIEW_STATUS,
        "label": EXPERIMENTAL_LABEL,
        "decimal_precision": PRECISION,
        "rule_tables": {c: rl.load_rule_table(c).digest() for c in rl.available_countries()},
        "assumption_registry_digest": lit.load_grid().source_digest,
        "assumptions": [
            "base-year LFS employment rates by sex and five-year group held constant",
            "employment and pension-receipt responses are cited literature values (stated assumptions)",
            PENSION_DROP_LABEL,
            "claim-year eligibility ages are attained once (NLD: AOW art. 7a(1) verbatim; ESP: model assumption)",
            "eligibility ages are only modelled at single ages 55-69; requests affecting other ages are refused",
            "population from the cohort-component conditional projection (agent POP)",
            "pension recipients level scales with the 65+ population (constant ratio)",
            "average pension constant in nominal euro of its year (no indexation)",
        ],
        "max_end_year": MAX_END_YEAR,
    }


def digest_of(obj: Any) -> str:
    return logical_hash(obj)


# --------------------------------------------------------------------------- plugin
def spec_schema() -> dict[str, Any]:
    point_ids = {"type": "string", "pattern": "^[a-z0-9_]{3,80}$"}
    return {
        "reference_year": {"type": "integer", "minimum": MIN_REFERENCE_YEAR, "maximum": MAX_END_YEAR - 1},
        "horizon": {"type": "object", "additionalProperties": False,
                    "properties": {"periods": {"type": "integer", "minimum": 1, "maximum": MAX_END_YEAR -
                                               MIN_REFERENCE_YEAR},
                                   "unit": {"const": "calendar_year"}},
                    "required": ["periods", "unit"]},
        "fidelity": {"enum": ["L0", "L1", "L2"]},
        "intervention": {"oneOf": [
            {"type": "object", "additionalProperties": False,
             "properties": {"kind": {"const": "retirement_age_rule_path"},
                            "scenario_path": {"type": "string", "pattern": "^[a-z0-9_]{1,80}$"},
                            "reference_path": {"type": "string", "pattern": "^[a-z0-9_]{1,80}$"}},
             "required": ["kind", "scenario_path", "reference_path"]},
            {"type": "object", "additionalProperties": False,
             "properties": {"kind": {"const": "retirement_age_shift"},
                            "reference_path": {"type": "string", "pattern": "^[a-z0-9_]{1,80}$"},
                            "shift_months": {"type": "integer", "minimum": -60, "maximum": 60},
                            "first_birth_year": {"type": "integer", "minimum": 1900, "maximum": 2100},
                            "phase_in_months_per_cohort": {"type": "integer", "minimum": 0, "maximum": 24}},
             "required": ["kind", "reference_path", "shift_months", "first_birth_year"]},
            {"type": "object", "additionalProperties": False,
             "description": ("Target statutory age LEVEL (months of age, e.g. 780 = 65 years) of the rule table's "
                             "lever age for cohorts born in first_birth_year or later; earlier cohorts keep the "
                             "reference path. Use this, not a shift, when the request states an age level."),
             "properties": {"kind": {"const": "retirement_age_target"},
                            "reference_path": {"type": "string", "pattern": "^[a-z0-9_]{1,80}$"},
                            "target_age_months": {"type": "integer", "minimum": MODEL_AGE_RANGE_MONTHS[0],
                                                  "maximum": MODEL_AGE_RANGE_MONTHS[1]},
                            "first_birth_year": {"type": "integer", "minimum": 1900, "maximum": 2100},
                            "phase_in_months_per_cohort": {"type": "integer", "minimum": 0, "maximum": 24}},
             "required": ["kind", "reference_path", "target_age_months", "first_birth_year"]}]},
        "assumptions": {"type": "object", "additionalProperties": False, "properties": {
            "observed_path": {"type": "string", "pattern": "^[a-z0-9_]{1,80}$"},
            "employment_response": {"type": "object", "additionalProperties": False, "properties": {
                "central": point_ids, "grid": {"type": "array", "items": point_ids, "maxItems": 50,
                                               "uniqueItems": True}}},
            "population": {"type": "object", "additionalProperties": False, "properties": {
                "migration": {"enum": ["zero", "average_residual", "europop2023_bsl"]},
                "fertility": {"enum": ["constant_base_period_rates", "europop2023_bsl"]},
                "base_period_years": {"type": "integer", "minimum": 1, "maximum": 10},
                "migration_breaks": {"enum": ["exclude", "include"]},
                "allow_single_year_migration": {"type": "boolean"}}}}},
        "outputs": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"enum": sorted(OUTPUTS)}},
    }


class RetirementAgeModel:
    """Plugin ``social.retirement_age@1.0.0`` (experimental, review pending)."""

    model_key = MODEL_KEY
    model_version = MODEL_VERSION
    fidelities = frozenset({"L1", "L2"})

    def validate(self, spec: dict[str, Any]) -> None:
        validate_spec(spec)

    def requirements(self, spec: dict[str, Any]) -> list[Any]:
        from reva.simulation.plugins import InputRequirement

        b, _years = years_of(spec)
        out = []
        for iso3 in spec.get("countries") or []:
            for name, concept in CONCEPTS.items():
                yrs = (b,) if name in ("employment_rate", "activity_rate") else \
                    tuple(range(b - 9, b + 2)) if name in ("population", "deaths", "births") else tuple(
                        range(b - 9, b + 1))
                out.append(InputRequirement(name, concept, iso3, yrs, "dataset"))
        return out

    def compile(self, inputs: Any, spec: dict[str, Any]) -> Compiled:
        b, years = years_of(spec)
        plans = []
        for iso3 in spec.get("countries") or []:
            tables = {}
            for name in CONCEPTS:
                ds = inputs.dataset(name, iso3)
                tables[name] = None if ds.is_missing else ds.table
            plans.append(build_country_plan(iso3, inputs.subject(iso3).entity_id, spec, tables))
        outputs = tuple(spec.get("outputs") or DEFAULT_OUTPUTS)
        return Compiled(dict(spec), b, years, outputs, plans, str(spec.get("missing_input_policy", "fail")),
                        dict(inputs.release))

    def compute(self, compiled: Compiled, fidelity: str) -> list[Any]:
        from reva.simulation.plugins import ResultRow

        if fidelity not in self.fidelities:
            raise CapabilityGap(f"{MODEL_KEY} computes L1 and L2 only", capability=f"fidelity.{fidelity}")
        rows: list[Any] = []
        for plan in compiled.countries:
            if plan.status == "ready":
                rows.extend(rows_for_country(plan, compiled, fidelity, ResultRow))
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
            if c.intervention is not None:
                entry["intervention"] = c.intervention.as_json()
            if c.central is not None:
                entry["central_grid_point"] = c.central.point_id
                entry["grid_points"] = [p.point_id for p in c.points]
            if c.data is not None:
                entry["base_year"] = c.data.base_year
                entry["employment_rate_flags"] = {f"{k[0]}.{k[1]}": v for k, v in c.data.employment_flags.items()}
                entry["average_pension"] = c.data.avg_pension_basis
                entry["population_projection"] = c.data.population_summary
            countries[c.iso3] = entry
        out = {"explanation": explain(fidelity), "countries": countries, "years": list(compiled.years),
               "label": LABEL, "model_label": EXPERIMENTAL_LABEL}
        if compiled.policy == "skip_missing_countries":
            out["missing_input_policy"] = "skip_missing_countries"
            out["skipped_countries"] = compiled.skipped
        return out

    def cost(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {"periods": int((spec.get("horizon") or {}).get("periods", 0)), "ages": len(AGES)}

    def implementation_files(self) -> tuple[Path, ...]:
        return implementation_files()


MODEL = RetirementAgeModel()


def _register() -> None:
    from reva.simulation import plugins

    plugins.register(MODEL)


_register()
