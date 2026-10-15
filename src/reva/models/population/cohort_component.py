"""Plugin ``demography.cohort_component@1.0.0`` on the V2 simulation platform.

Pure (no database, network or clock): the platform resolves the dataset inputs pinned
by the scenario's baseline release; this module validates the model part of the spec,
declares its inputs, compiles per-country plans and computes Decimal result rows. The
numerics live in :mod:`reva.models.population.cohort`.

Every result is a *conditional projection under stated assumptions, not a forecast*.

Spec (model branch of ScenarioSpec 2.0.0)::

    reference_year  jump-off: the projection starts from observed population on 1 January
    horizon         {"periods": n, "unit": "calendar_year"}; end = reference_year + n (n <= 50)
    intervention    {"kind": "baseline_projection"}  (no policy intervention in 1.0.0)
    assumptions     {"mortality": "constant_base_period_rates",
                     "fertility": "constant_base_period_rates" | "europop2023_bsl",
                     "migration": "zero" | "average_residual" | "europop2023_bsl",
                     "base_period": {"first_year": y0, "last_year": y1},      # y1 < reference_year
                     "migration_window": {"first_year": .., "last_year": ..},  # optional, < reference_year
                     "migration_breaks": "exclude" | "include",   # average_residual: series-break years (default
                                                                  #   exclude, typed warning either way)
                     "allow_single_year_migration": false,        # averaging < 2 residual years refused unless true
                     "sensitivity": {"migration": [...], "fertility": [...]}}  # L2 only
    outputs         subset of OUTPUT_KEYS, plus "population_by_single_age" (opt-in, many rows)

L1  one projection under the stated assumptions (variant ``L1``).
L2  the L1 projection plus every (migration, fertility) combination of the sensitivity
    grid (variants ``L2:mig=..;fert=..``) and, per output and year, a separate
    ``L2:sensitivity_range`` row: min/max over the grid and the L1 point. Assumption
    sensitivity, not a confidence interval; no probability level.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from reva.kernel.errors import CapabilityGap, MissingInput, RevaError, ValidationProblem
from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec, UncertaintyStatus
from reva.models.population import cohort as C
from reva.simulation import plugins
from reva.simulation.plugins import InputRequirement, ResolvedInputs, ResultRow

COUNTRIES = (
    "AUT",
    "BEL",
    "BGR",
    "CHE",
    "CYP",
    "CZE",
    "DEU",
    "DNK",
    "ESP",
    "EST",
    "FIN",
    "FRA",
    "GRC",
    "HRV",
    "HUN",
    "IRL",
    "ISL",
    "ITA",
    "LIE",
    "LTU",
    "LUX",
    "LVA",
    "MLT",
    "NLD",
    "NOR",
    "POL",
    "PRT",
    "ROU",
    "SVK",
    "SVN",
    "SWE",
)
DATASET_CONCEPTS = {
    "population": "demography.population.jan1_age_sex",
    "deaths": "demography.deaths.age_sex",
    "births": "demography.live_births.mother_age_newborn_sex",
    "europop_migration": "demography.assumption.europop2023.net_migration_age_sex",
    "europop_fertility": "demography.assumption.europop2023.fertility_rate_age",
}
EUROPOP_YEARS = (2022, 2060)
MAX_PERIODS = 50
SINGLE_AGE_TOKEN = "population_by_single_age"
OUTPUT_KEYS = tuple(C.output_keys())
RANGE_VARIANT = "L2:sensitivity_range"
SENSITIVITY_METHOD = "reva.models.population.cohort_component.l2_assumption_grid_minmax@1.0.0"
SENSITIVITY_STATEMENT = "assumption sensitivity, not a confidence interval"
REVIEW_STATUS = "maintainer_review_pending"
NOT_CLAIMED = (
    "forecast",
    "probability of any outcome",
    "mortality improvement",
    "economic effects",
    "migration policy effects",
    "sub-national detail",
)


def _unit(output_key: str) -> tuple[str, dict[str, Any]]:
    if output_key.startswith("old_age_dependency_ratio"):
        return "unit.percent", {
            "scale_factor": "1",
            "notes": output_key.replace("_", " ")
            + " (persons in the older group per 100 persons in the working-age group)",
        }
    if output_key.split(".")[0] in C.FLOW_KEYS:
        return "unit.persons", {"scale_factor": "1", "notes": "flow during the calendar year (1 Jan to 1 Jan)"}
    return "unit.persons", {"scale_factor": "1", "notes": "stock on 1 January"}


def _round(output_key: str, v: Decimal) -> Decimal:
    q = Decimal("0.0001") if output_key.startswith("old_age_dependency_ratio") else Decimal(1)
    return C._CTX.quantize(v, q)


def _period(output_key: str, year: int) -> tuple[date, date]:
    if output_key.split(".")[0] in C.FLOW_KEYS:
        return (date(year, 1, 1), date(year + 1, 1, 1))
    return (date(year, 1, 1), date(year, 1, 2))  # the 1 January instant as a one-day half-open period


# --------------------------------------------------------------------------- spec handling
@dataclass(frozen=True)
class ModelSpec:
    countries: tuple[str, ...]
    jump_off: int
    end_year: int
    central: C.Assumptions
    grid: tuple[C.Assumptions, ...]
    outputs: tuple[str, ...]
    single_age: bool
    policy: str
    fidelity: str


def _years(obj: Any, what: str) -> tuple[int, int]:
    if (
        not isinstance(obj, dict)
        or not isinstance(obj.get("first_year"), int)
        or not isinstance(obj.get("last_year"), int)
    ):
        raise ValidationProblem(f"assumptions.{what} must be {{first_year, last_year}} integers")
    a, b = int(obj["first_year"]), int(obj["last_year"])
    if b < a:
        raise ValidationProblem(f"assumptions.{what}: last_year before first_year")
    return a, b


def parse_spec(spec: Any) -> ModelSpec:
    s = spec.as_json() if hasattr(spec, "as_json") else dict(spec)
    if s.get("model_key") != C.MODEL_KEY or s.get("model_version") != C.MODEL_VERSION:
        raise ValidationProblem(f"spec is not for {C.MODEL_KEY}@{C.MODEL_VERSION}")
    if (s.get("intervention") or {}).get("kind") != "baseline_projection":
        raise CapabilityGap(
            f"only 'baseline_projection' (no intervention) is supported by {C.MODEL_KEY}@{C.MODEL_VERSION}",
            capability="demography.intervention",
        )
    hz = s.get("horizon") or {}
    if (
        hz.get("unit") != "calendar_year"
        or not isinstance(hz.get("periods"), int)
        or not (1 <= hz["periods"] <= MAX_PERIODS)
    ):
        raise ValidationProblem(f"horizon must be 1..{MAX_PERIODS} calendar years")
    jump_off = int(s["reference_year"])
    end_year = jump_off + int(hz["periods"])
    a = s.get("assumptions") or {}
    base = _years(a.get("base_period"), "base_period")
    if base[1] >= jump_off:
        raise ValidationProblem(
            "assumptions.base_period must end before reference_year (the jump-off): no "
            "information after the jump-off may enter"
        )
    mw = _years(a["migration_window"], "migration_window") if a.get("migration_window") is not None else None
    if mw is not None and mw[1] >= jump_off:
        raise ValidationProblem("assumptions.migration_window must end before reference_year")
    breaks = a.get("migration_breaks", "exclude")
    if breaks not in C.MIGRATION_BREAK_OPTIONS:
        raise ValidationProblem(f"assumptions.migration_breaks must be one of {list(C.MIGRATION_BREAK_OPTIONS)}")
    single = a.get("allow_single_year_migration", False)
    if not isinstance(single, bool):
        raise ValidationProblem("assumptions.allow_single_year_migration must be a boolean")
    window = mw or base
    grid_migrations = (a.get("sensitivity") or {}).get("migration") or []
    uses_avg = "average_residual" in {str(a.get("migration")), *[str(m) for m in grid_migrations]}
    if uses_avg and window[1] - window[0] + 1 < C.MIN_MIGRATION_YEARS and not single:
        raise ValidationProblem(
            f"the residual-migration window {window[0]}-{window[1]} has one year: averaging over "
            f"fewer than {C.MIN_MIGRATION_YEARS} years is refused unless "
            "assumptions.allow_single_year_migration is true",
            migration_window=list(window),
        )
    if a.get("mortality", "constant_base_period_rates") not in C.MORTALITY_OPTIONS:
        raise CapabilityGap(
            f"mortality assumption {a.get('mortality')!r} is not available",
            capability="demography.mortality_assumption",
            supported=list(C.MORTALITY_OPTIONS),
        )

    def assumption(mig: str, fert: str) -> C.Assumptions:
        try:
            return C.Assumptions(
                migration=mig,
                fertility=fert,
                base_period=base,
                migration_window=mw,
                migration_breaks=str(breaks),
                allow_single_year_migration=single,
            )
        except ValidationProblem as e:
            raise CapabilityGap(e.message, capability="demography.assumption", **e.details) from None

    central = assumption(str(a.get("migration")), str(a.get("fertility")))
    fidelity = str(s.get("fidelity"))
    sens = a.get("sensitivity")
    grid: list[C.Assumptions] = []
    if fidelity == "L2":
        if not isinstance(sens, dict) or not sens.get("migration") or not sens.get("fertility"):
            raise ValidationProblem(
                "L2 requires assumptions.sensitivity with non-empty migration and fertility option lists"
            )
        for mig, fert in itertools.product(sens["migration"], sens["fertility"]):
            g = assumption(str(mig), str(fert))
            if g.variant != central.variant and g.variant not in {x.variant for x in grid}:
                grid.append(g)
    elif fidelity == "L1" and sens:
        raise ValidationProblem("L1 takes no sensitivity grid (use L2)")
    uses_europop = any("europop2023" in (x.migration + x.fertility) for x in (central, *grid))
    if uses_europop and (jump_off < EUROPOP_YEARS[0] or end_year - 1 > EUROPOP_YEARS[1]):
        raise CapabilityGap(
            f"EUROPOP2023 assumptions cover the flows of {EUROPOP_YEARS[0]}-{EUROPOP_YEARS[1]} only (as fetched)",
            capability="demography.assumption.europop2023",
            jump_off=jump_off,
            end_year=end_year,
        )
    outputs = [o for o in s.get("outputs") or []]
    single = SINGLE_AGE_TOKEN in outputs
    unknown = sorted(set(outputs) - set(OUTPUT_KEYS) - {SINGLE_AGE_TOKEN})
    if unknown:
        raise CapabilityGap(
            f"outputs {unknown} are not produced by {C.MODEL_KEY}",
            capability="demography.outputs",
            supported=[*OUTPUT_KEYS, SINGLE_AGE_TOKEN],
        )
    countries = tuple(s.get("countries") or ())
    bad = sorted(set(countries) - set(COUNTRIES))
    if bad:
        raise CapabilityGap(
            f"countries {bad} have no reviewed Eurostat demography connector coverage",
            capability="demography.country_coverage",
            supported=list(COUNTRIES),
        )
    return ModelSpec(
        countries,
        jump_off,
        end_year,
        central,
        tuple(grid),
        tuple(o for o in OUTPUT_KEYS if o in outputs),
        single,
        str(s.get("missing_input_policy", "fail")),
        fidelity,
    )


# --------------------------------------------------------------------------- compiled plan
@dataclass
class CountryPlan:
    iso3: str
    subject_id: UUID
    status: str = "ready"  # ready | missing_input | unsupported
    problems: list[dict[str, Any]] = field(default_factory=list)
    data: C.CountryDemography | None = None
    rates: C.BaseRates | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    unavailable_variants: list[dict[str, Any]] = field(default_factory=list)
    migration_window: C.MigrationWindow | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)  # typed run warnings (stable codes)


@dataclass
class CompiledProjection:
    spec: ModelSpec
    release: dict[str, Any]
    countries: list[CountryPlan]

    @property
    def missing(self) -> list[dict[str, Any]]:
        if self.spec.policy == "skip_missing_countries" and any(c.status == "ready" for c in self.countries):
            return []
        return [p for c in self.countries for p in c.problems if p.get("code") == "missing_input"]

    @property
    def incompatible(self) -> list[dict[str, Any]]:
        if self.spec.policy == "skip_missing_countries" and any(c.status == "ready" for c in self.countries):
            return []
        return [p for c in self.countries for p in c.problems if p.get("code") != "missing_input"]

    @property
    def skipped(self) -> list[str]:
        return [c.iso3 for c in self.countries if c.status != "ready"]


def _needed(ms: ModelSpec) -> list[str]:
    names = ["population", "deaths", "births"]
    variants = (ms.central, *ms.grid)
    if any(v.migration == "europop2023_bsl" for v in variants):
        names.append("europop_migration")
    if any(v.fertility == "europop2023_bsl" for v in variants):
        names.append("europop_fertility")
    return names


def _input_years(ms: ModelSpec) -> tuple[int, ...]:
    a, b = ms.central.base_period
    mw = ms.central.migration_window or (a, b)
    return tuple(range(min(a, mw[0]), ms.jump_off + 1))


def _row_value(output_key: str, v: Decimal) -> dict[str, Any]:
    unit, basis = _unit(output_key)
    return {"kind": "decimal", "value": C.dstr(_round(output_key, v)), "unit_ref": unit, "basis": basis}


def _uncertainty() -> dict[str, Any]:
    return UncertaintySpec.unknown(
        "Unquantified: a conditional projection under the stated assumptions, not a forecast; no probability is "
        "attached. Inputs carry no published uncertainty. The out-of-sample back-test error "
        "(docs/dev/v2_pop_backtest.md) describes past performance of the method only."
    ).model_dump(mode="json")


def _range_uncertainty(lo: Decimal, hi: Decimal, variants: list[str]) -> dict[str, Any]:
    return UncertaintySpec(
        status=UncertaintyStatus.quantified,
        kind=UncertaintyKind.sensitivity_range,
        description=(
            f"Assumption sensitivity range ({SENSITIVITY_STATEMENT}): min/max of this output over the "
            "labelled migration x fertility assumption grid and the L1 assumption set. Not a confidence, "
            "credible or prediction interval; no coverage level; mortality is held at base-period rates "
            "in every variant."
        ),
        method_version=SENSITIVITY_METHOD,
        lower=C.dstr(lo),
        upper=C.dstr(hi),
        conditioning=tuple(variants),
    ).model_dump(mode="json")


def _applicability(plan: CountryPlan, variant: str, a: C.Assumptions, ms: ModelSpec) -> dict[str, Any]:
    out = {
        "status": "applicable",
        "modality": "assumption_conditioned_scenario",
        "label": C.LABEL,
        "model": f"{C.MODEL_KEY}@{C.MODEL_VERSION}",
        "country": plan.iso3,
        "variant": variant,
        "variant_assumptions": {"migration": a.migration, "fertility": a.fertility, "mortality": a.mortality},
        "jump_off": ms.jump_off,
        "open_age": plan.rates.omega if plan.rates else None,
    }
    if plan.warnings:
        out["warnings"] = [{k: v for k, v in w.items() if k != "message"} for w in plan.warnings]
    if a.migration == "average_residual" and plan.migration_window is not None:
        out["migration_years_used"] = list(plan.migration_window.years_used)
    return out


class CohortComponentModel:
    """``demography.cohort_component@1.0.0`` as a :class:`reva.simulation.plugins.SimulationModel`."""

    model_key = C.MODEL_KEY
    model_version = C.MODEL_VERSION
    fidelities = frozenset({"L1", "L2"})
    missing_input_policies = frozenset({"fail", "skip_missing_countries"})
    scheduling = "deterministic: countries sorted; years ascending; one pass per year"

    # -- protocol ----------------------------------------------------------------------
    def validate(self, spec: dict[str, Any]) -> None:
        parse_spec(spec)

    def requirements(self, spec: dict[str, Any]) -> list[InputRequirement]:
        ms = parse_spec(spec)
        years = _input_years(ms)
        reqs = []
        for iso3 in ms.countries:
            for name in _needed(ms):
                yrs = years if not name.startswith("europop") else tuple(range(ms.jump_off, ms.end_year))
                reqs.append(InputRequirement(name, DATASET_CONCEPTS[name], iso3, yrs, "dataset"))
        return reqs

    def compile(self, inputs: ResolvedInputs, spec: dict[str, Any]) -> CompiledProjection:
        ms = parse_spec(spec)
        plans: list[CountryPlan] = []
        for iso3 in ms.countries:
            plan = CountryPlan(iso3, inputs.subject(iso3).entity_id)
            tables: dict[str, Any] = {}
            central_needs = {"population", "deaths", "births"}
            if ms.central.migration == "europop2023_bsl":
                central_needs.add("europop_migration")
            if ms.central.fertility == "europop2023_bsl":
                central_needs.add("europop_fertility")
            for name in _needed(ms):
                d = inputs.dataset(name, iso3)
                plan.provenance[name] = d.provenance()
                if d.is_missing and name not in central_needs:
                    continue  # only grid variants need it: they are reported unavailable, not zero-filled
                if d.is_missing:
                    plan.status = "missing_input"
                    plan.problems.append(
                        {
                            "code": "missing_input",
                            "country": iso3,
                            "input": name,
                            "concept": DATASET_CONCEPTS[name],
                            "kind": "dataset",
                            "reason": d.missing_reason,
                        }
                    )
                else:
                    tables[name] = d.table
            if plan.status == "ready":
                try:
                    plan.data = C.CountryDemography.from_tables(
                        iso3,
                        population=tables["population"],
                        deaths=tables["deaths"],
                        births=tables["births"],
                        europop_migration=tables.get("europop_migration"),
                        europop_fertility=tables.get("europop_fertility"),
                        provenance=plan.provenance,
                    )
                    a = ms.central
                    plan.rates = C.base_rates(
                        plan.data,
                        a.base_period[0],
                        a.base_period[1],
                        migration_window=a.migration_window or a.base_period,
                    )
                    C._vec(plan.data, "population", ms.jump_off, "F", plan.rates.omega)
                    C._vec(plan.data, "population", ms.jump_off, "M", plan.rates.omega)
                    plan.warnings = C.base_period_warnings(plan.rates)
                    window_problem: MissingInput | None = None
                    if any(v.migration == "average_residual" for v in (ms.central, *ms.grid)):
                        # series-break years excluded by default; a window left too short is missing_input
                        try:
                            plan.migration_window = C.resolve_migration_window(
                                plan.rates,
                                a.migration_window or a.base_period,
                                breaks=a.migration_breaks,
                                allow_single_year=a.allow_single_year_migration,
                            )
                            plan.warnings += list(plan.migration_window.warnings)
                        except MissingInput as e:
                            if ms.central.migration == "average_residual":
                                raise
                            window_problem = e  # only grid variants need it: they are reported unavailable
                    for a in (ms.central, *ms.grid):
                        if window_problem is not None and a.migration == "average_residual":
                            plan.unavailable_variants.append({"variant": a.variant, **window_problem.problem()})
                            continue
                        try:
                            C.check_assumption_inputs(plan.data, a, ms.jump_off, ms.end_year, plan.rates.omega)
                        except CapabilityGap as e:
                            if a is ms.central:
                                raise
                            plan.unavailable_variants.append({"variant": a.variant, **e.problem()})
                except MissingInput as e:
                    plan.status = "missing_input"
                    plan.problems = [
                        {"code": "missing_input", "country": iso3, **m} for m in e.details.get("missing", [])
                    ] or [{**e.problem(), "country": iso3}]
                except RevaError as e:
                    plan.status = "unsupported"
                    plan.problems = [{**e.problem(), "country": iso3}]
            plans.append(plan)
        return CompiledProjection(ms, dict(inputs.release), plans)

    def compute(self, compiled: CompiledProjection, fidelity: str) -> list[ResultRow]:
        ms = compiled.spec
        rows: list[ResultRow] = []
        for plan in sorted(compiled.countries, key=lambda c: c.iso3):
            if plan.status != "ready" or plan.data is None or plan.rates is None:
                continue
            central = C.project(
                plan.data, jump_off=ms.jump_off, end_year=ms.end_year, assumptions=ms.central, rates=plan.rates
            )
            cvals = C.output_values(central)
            rows += self._rows(plan, "L1", ms.central, central, cvals, ms)
            if fidelity != "L2":
                continue
            evaluated = [(ms.central.variant, cvals)]
            unavailable = {u["variant"] for u in plan.unavailable_variants}
            for a in ms.grid:
                if a.variant in unavailable:  # e.g. no EUROPOP2023 assumption for Liechtenstein
                    continue
                proj = C.project(plan.data, jump_off=ms.jump_off, end_year=ms.end_year, assumptions=a, rates=plan.rates)
                vals = C.output_values(proj)
                evaluated.append((a.variant, vals))
                rows += self._rows(plan, f"L2:{a.variant}", a, proj, vals, ms)
            names = [v for v, _ in evaluated]
            for key, year in sorted(cvals):
                if key not in ms.outputs:
                    continue
                pts = [_round(key, vals[(key, year)]) for _, vals in evaluated]
                lo, hi = min(pts), max(pts)
                unit, basis = _unit(key)
                rows.append(
                    ResultRow(
                        key,
                        plan.iso3,
                        plan.subject_id,
                        RANGE_VARIANT,
                        {
                            "kind": "sensitivity_range",
                            "lower": C.dstr(lo),
                            "upper": C.dstr(hi),
                            "unit_ref": unit,
                            "basis": basis,
                            "evaluated_points": len(pts),
                            "includes_l1_point": True,
                            "statement": SENSITIVITY_STATEMENT,
                        },
                        _range_uncertainty(lo, hi, names),
                        {
                            **_applicability(plan, RANGE_VARIANT, ms.central, ms),
                            "range_over": names,
                            "unavailable_variants": [u["variant"] for u in plan.unavailable_variants],
                        },
                        _period(key, year),
                    )
                )
        return rows

    @staticmethod
    def _rows(
        plan: CountryPlan,
        variant: str,
        a: C.Assumptions,
        proj: C.Projection,
        vals: dict[tuple[str, int], Decimal],
        ms: ModelSpec,
    ) -> list[ResultRow]:
        out = []
        app = _applicability(plan, variant, a, ms)
        unc = _uncertainty()
        for (key, year), v in sorted(vals.items()):
            if key in ms.outputs:
                out.append(
                    ResultRow(
                        key, plan.iso3, plan.subject_id, variant, _row_value(key, v), unc, app, _period(key, year)
                    )
                )
        if ms.single_age:
            for year in sorted(proj.population):
                for s, sk in (("F", "female"), ("M", "male")):
                    for x, v in enumerate(proj.population[year][s]):
                        key = f"population.{sk}.age_{x:03d}" + ("_plus" if x == proj.omega else "")
                        out.append(
                            ResultRow(
                                key,
                                plan.iso3,
                                plan.subject_id,
                                variant,
                                {
                                    "kind": "decimal",
                                    "value": C.dstr(v),
                                    "unit_ref": "unit.persons",
                                    "basis": {
                                        "scale_factor": "1",
                                        "notes": f"single age on 1 January, exact to {C.QUANTUM} "
                                        "persons (model quantum)",
                                    },
                                },
                                unc,
                                app,
                                (date(year, 1, 1), date(year, 1, 2)),
                            )
                        )
        return out

    def manifest(self) -> dict[str, Any]:
        return {
            "implementation": "reva.models.population.cohort",
            "implementation_digest": plugins.source_digest(*self.implementation_files()),
            "numeric_type": C.NUMERIC_TYPE,
            "decimal_precision": C.PRECISION,
            "review_status": REVIEW_STATUS,
            "label": C.LABEL,
            "assumptions": C.ASSUMPTION_LABELS,
            "not_claimed": list(NOT_CLAIMED),
            "backtest_doc": "docs/dev/v2_pop_backtest.md",
        }

    # -- optional hooks ------------------------------------------------------------------
    def spec_schema(self) -> dict[str, Any]:
        opts = {"type": "array", "minItems": 1, "uniqueItems": True}
        years = {
            "type": "object",
            "additionalProperties": False,
            "required": ["first_year", "last_year"],
            "properties": {
                "first_year": {"type": "integer", "minimum": 1990, "maximum": 2100},
                "last_year": {"type": "integer", "minimum": 1990, "maximum": 2100},
            },
        }
        return {
            "countries": {
                "type": "array",
                "items": {"enum": list(COUNTRIES)},
                "minItems": 1,
                "maxItems": len(COUNTRIES),
                "uniqueItems": True,
            },
            "reference_year": {"type": "integer", "minimum": 2001, "maximum": 2100},
            "horizon": {
                "type": "object",
                "additionalProperties": False,
                "required": ["periods", "unit"],
                "properties": {
                    "periods": {"type": "integer", "minimum": 1, "maximum": MAX_PERIODS},
                    "unit": {"const": "calendar_year"},
                },
            },
            "intervention": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind"],
                "properties": {"kind": {"const": "baseline_projection"}},
            },
            "assumptions": {
                "type": "object",
                "additionalProperties": False,
                "required": ["mortality", "fertility", "migration", "base_period"],
                "properties": {
                    "mortality": {"enum": list(C.MORTALITY_OPTIONS)},
                    "fertility": {"enum": list(C.FERTILITY_OPTIONS)},
                    "migration": {"enum": list(C.MIGRATION_OPTIONS)},
                    "base_period": years,
                    "migration_window": years,
                    "migration_breaks": {"enum": list(C.MIGRATION_BREAK_OPTIONS)},
                    "allow_single_year_migration": {"type": "boolean"},
                    "sensitivity": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["migration", "fertility"],
                        "properties": {
                            "migration": {**opts, "items": {"enum": list(C.MIGRATION_OPTIONS)}},
                            "fertility": {**opts, "items": {"enum": list(C.FERTILITY_OPTIONS)}},
                        },
                    },
                },
            },
            "outputs": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"enum": [*OUTPUT_KEYS, SINGLE_AGE_TOKEN]},
            },
        }

    def explanation(self, fidelity: str) -> dict[str, Any]:
        return {
            "model": f"{C.MODEL_KEY}@{C.MODEL_VERSION}",
            "label": C.LABEL,
            "method": (
                "annual cohort-component projection by single age and recorded sex; constant base-period "
                "mortality (m_x -> q_x with a_x = 1/2, Coale-Demeny a_0); births from age-specific "
                "fertility rates x female exposure; sex ratio at birth from data; net migration by the "
                "stated assumption (residual method estimates include statistical adjustment)"
            ),
            "fidelity": fidelity,
            "fidelity_meaning": {
                "L1": "one projection under the stated assumptions",
                "L2": "L1 plus a migration x fertility assumption grid and a separate min/max "
                f"range row ({SENSITIVITY_STATEMENT})",
            }.get(fidelity),
            "numeric_type": C.NUMERIC_TYPE,
            "not_claimed": list(NOT_CLAIMED),
        }

    def run_applicability(self, compiled: CompiledProjection, fidelity: str) -> dict[str, Any]:
        ms = compiled.spec
        countries = {}
        for c in compiled.countries:
            info: dict[str, Any] = {"status": c.status, "problems": c.problems, "inputs": c.provenance}
            if c.rates is not None:
                r = c.rates
                info.update(
                    {
                        "open_age": r.omega,
                        "implied_tfr_base_period": C.dstr(C._CTX.quantize(r.tfr, Decimal("0.0001"))),
                        "life_expectancy_at_birth_base_period": {
                            s: C.dstr(C._CTX.quantize(r.life_tables[s].e0, Decimal("0.01"))) for s in C.SEXES
                        },
                        "male_share_at_birth": C.dstr(C._CTX.quantize(r.male_share_at_birth, Decimal("0.0001"))),
                        "data_notes": list(r.notes),
                        "base_period_years_used": r.years_used(),
                        "input_status_flags": r.input_status_flags,
                        "unavailable_variants": c.unavailable_variants,
                        "migration_window": c.migration_window.as_json() if c.migration_window else None,
                        "warnings": c.warnings,
                    }
                )
            countries[c.iso3] = info
        return {
            "explanation": self.explanation(fidelity),
            "label": C.LABEL,
            "jump_off": ms.jump_off,
            "end_year": ms.end_year,
            "assumptions": ms.central.as_json(),
            "sensitivity_grid": [g.as_json() for g in ms.grid],
            "skipped_countries": compiled.skipped if ms.policy == "skip_missing_countries" else [],
            "countries": countries,
        }

    def seed_subjects(self, spec: dict[str, Any]) -> list[str]:
        return sorted(spec.get("countries") or [])

    def cost(self, spec: dict[str, Any]) -> dict[str, Any]:
        sens = (spec.get("assumptions") or {}).get("sensitivity") or {}
        return {"grid_points": len(sens.get("migration") or []) * len(sens.get("fertility") or [])}

    def implementation_files(self) -> tuple[Path, ...]:
        return (Path(C.__file__), Path(__file__))


MODEL = plugins.register(CohortComponentModel())
