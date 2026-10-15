"""Plugin #1: ``economic.crude_exposure@1.0.0`` on the V2 simulation platform.

This module holds everything model-specific that the V1 engine and compiler held
(typed intervention, per-country plans, coupling schedule, L1/L2 result rows,
the separate sensitivity-range row). The arithmetic lives, unchanged, in
:mod:`reva.models.economic.crude_exposure`. Outputs, output digests and L0 cache
keys are bit-identical to the V1 engine (``tests/integration/
test_simulation_v2_plugin_identity.py`` and ``tests/unit/test_simulation_v2_plugins.py``
compare against goldens captured from the V1 engine and the recorded live demo).

Pure: no database, HTTP or clock. The platform resolves inputs.

L1  deterministic fixed-quantity crude border-value exposure.
L2  labelled elasticity sensitivity over the declared grid. Each grid point is its
    own result variant. The per-country/output min/max over grid ∪ {ε=(0,0)} is
    published as a SEPARATE row (variant ``L2:sensitivity_range``) whose value is
    the range object (kind ``sensitivity_range``) and whose uncertainty is a
    ``sensitivity_range`` without a coverage level: assumption sensitivity, not a
    confidence interval. The fixed-quantity ``L1`` row keeps its own unquantified
    uncertainty (R3-03 / kernel rule R3-19); the range always contains it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from reva.kernel.errors import CapabilityGap, IncompatibleSemantics, MissingInput, ValidationProblem
from reva.kernel.ids import logical_hash
from reva.kernel.uncertainty import UncertaintyKind, UncertaintySpec, UncertaintyStatus
from reva.models.economic import crude_exposure as ce
from reva.simulation import plugins
from reva.simulation.coupling import CouplingGraph, PortSpec
from reva.simulation.plugins import InputRequirement, ResolvedInputs, ResultRow, ScalarInput
from reva.simulation.spec import ScenarioSpec

SUPPORTED_CHANGES = ("set_exogenous_border_price_multiplier",)
SENSITIVITY_METHOD = "reva.simulation.l2_elasticity_grid_minmax@1.1.0"
SENSITIVITY_VARIANT = "L2:sensitivity_range"
SENSITIVITY_STATEMENT = "assumption sensitivity, not a confidence interval"
FIXED_POINT_CONDITION = (
    "fixed_quantity_point:import_elasticity=0;export_elasticity=0 (the L1 value) is always "
    "included in the range, whether or not it is listed in the grid"
)
INPUT_ORDER = ("M", "X", "G")


# --------------------------------------------------------------------------- compiled plan (V1 shapes)
@dataclass(frozen=True)
class TypedChange:
    """A compiled intervention. Only exogenous border-price multipliers exist in V1."""

    kind: str
    import_multiplier: Decimal
    export_multiplier: Decimal
    unit: str
    duration: str
    held_fixed: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "import_multiplier": ce.dstr(self.import_multiplier),
            "export_multiplier": ce.dstr(self.export_multiplier),
            "unit": self.unit,
            "duration": self.duration,
            "held_fixed": list(self.held_fixed),
            "affected_market": "crude petroleum (HS 2709) border values, world partner totals",
        }


def compile_intervention(intervention: dict[str, Any], quantity_response: str) -> TypedChange:
    kind = intervention.get("kind")
    if kind not in SUPPORTED_CHANGES:
        raise CapabilityGap(
            f"intervention kind {kind!r} is unsupported; supported: {list(SUPPORTED_CHANGES)}",
            capability=f"intervention.{kind}",
        )
    if intervention.get("unit") != "ratio":
        raise IncompatibleSemantics("border-price multipliers must be ratios")
    p_m = Decimal(repr(float(intervention["import_multiplier"])))
    p_x = Decimal(repr(float(intervention["export_multiplier"])))
    for name, p in (("import_multiplier", p_m), ("export_multiplier", p_x)):
        if p <= 0:
            raise ValidationProblem(f"{name} must be > 0")
    held = ["import and export quantities"] if quantity_response == "fixed" else []
    held += ["GDP (baseline denominator)", "exchange rates", "all other prices and flows"]
    return TypedChange(
        kind=str(kind),
        import_multiplier=p_m,
        export_multiplier=p_x,
        unit="ratio",
        duration="one annual accounting period",
        held_fixed=tuple(held),
    )


@dataclass
class CountryPlan:
    iso3: str
    subject_id: UUID
    subject_resolution: str
    inputs: ce.CountryInputs
    status: str = "ready"  # ready | missing_input | incompatible
    problems: list[dict[str, Any]] = field(default_factory=list)
    values: tuple[Decimal, Decimal, Decimal] | None = None

    def input_refs(self) -> dict[str, Any]:
        return {
            n: {
                "estimate_id": i.estimate_id,
                "concept": ce.INPUT_CONCEPTS[n],
                "value": (ce.dstr(i.value) if isinstance(i.value, Decimal) else None),
                "reference_year": i.reference_year,
                "valuation": i.valuation,
            }
            for n, i in self.inputs.by_name().items()
        }


@dataclass
class CompiledScenario:
    spec: ScenarioSpec
    release: dict[str, Any]
    change: TypedChange
    countries: list[CountryPlan]
    schedule: list[str]
    # V2 step 1 (ALLC): set by the platform when the spec asked for countries ["ALL"].
    country_selection: dict[str, Any] | None = None

    # --- V2 step 1 (ALLC): missing_input_policy = skip_missing_countries -------------------
    # "fail" (default, V1): any country with a missing or incompatible input refuses the
    # whole run. "skip_missing_countries": the countries with complete inputs run; every
    # other country is listed in ``skipped`` with its typed problems (nothing is filled).
    # When no country has complete inputs the run is refused exactly as under "fail".
    def skipping(self) -> bool:
        return getattr(self.spec, "missing_input_policy", "fail") == "skip_missing_countries" and any(
            c.status == "ready" for c in self.countries
        )

    @property
    def skipped(self) -> list[dict[str, Any]]:
        if not self.skipping():
            return []
        # ``inputs``: per input the release estimate id (None = no estimate in the release) and value
        # (None = typed Missing or absent), so a reader can tell "absent" from "provider Missing".
        return [
            {
                "country": c.iso3,
                "subject_id": str(c.subject_id),
                "subject_resolution": c.subject_resolution,
                "status": c.status,
                "reasons": list(c.problems),
                "inputs": c.input_refs(),
            }
            for c in sorted(self.countries, key=lambda c: c.iso3)
            if c.status != "ready"
        ]

    @property
    def missing(self) -> list[dict[str, Any]]:
        if self.skipping():
            return []
        return [p for c in self.countries for p in c.problems if p.get("code") == "missing_input"]

    @property
    def incompatible(self) -> list[dict[str, Any]]:
        if self.skipping():
            return []
        return [p for c in self.countries for p in c.problems if p.get("code") != "missing_input"]

    # --- end V2 step 1 (ALLC) ---------------------------------------------------------------

    def baseline_inputs(self) -> dict[str, Any]:
        return {
            c.iso3: {
                "subject_id": str(c.subject_id),
                "subject_resolution": c.subject_resolution,
                "status": c.status,
                "inputs": c.input_refs(),
            }
            for c in self.countries
        }

    def baseline_digest(self) -> str:
        return logical_hash({"release_id": str(self.release["id"]), "inputs": self.baseline_inputs()})


def _port_from_input(inp: ce.MonetaryInput, name: str) -> PortSpec:
    nominal_usd = (
        (inp.currency or "").upper() == "USD"
        and inp.price_basis in ce.NOMINAL_PRICE_BASES
        and inp.price_reference_year is None
    )
    return PortSpec(
        name=name,
        concept=ce.INPUT_CONCEPTS[name],
        unit=str(inp.unit_ref),
        basis="nominal_current_usd" if nominal_usd else f"{inp.currency}:{inp.price_basis}:{inp.price_reference_year}",
        aggregation=str(inp.aggregation),
        temporal_support="calendar_year" if inp.reference_year is not None else "other",
    )


def build_coupling(countries: list[CountryPlan]) -> list[str]:
    """Baseline-provider -> model, one acyclic edge per input; returns the deterministic schedule."""
    g = CouplingGraph()
    model_inputs = [PortSpec.from_dict(p) for p in ce.DESCRIPTOR["input_ports"]]
    model_outputs = [PortSpec.from_dict(p) for p in ce.DESCRIPTOR["output_ports"]]
    for c in countries:
        if c.status != "ready":
            continue
        provider = f"baseline:{c.iso3}"
        model = f"{ce.MODEL_KEY}:{c.iso3}"
        g.add_node(provider, outputs=[_port_from_input(i, n) for n, i in c.inputs.by_name().items()])
        g.add_node(model, inputs=model_inputs, outputs=model_outputs)
        for n in c.inputs.by_name():
            g.connect(provider, n, model, n)
    return g.schedule()


def calendar_year_of(temporal: Any) -> int | None:
    """Reference year if the extent is exactly one Gregorian calendar year, else None."""
    if not isinstance(temporal, dict) or temporal.get("kind") != "interval" or temporal.get("precision") != "year":
        return None
    try:
        start, end = date.fromisoformat(temporal["start"]), date.fromisoformat(temporal["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if (start.month, start.day) == (1, 1) and end == date(start.year + 1, 1, 1):
        return start.year
    return None


def monetary_input(s: ScalarInput) -> ce.MonetaryInput:
    """A resolved scalar as the crude model's typed monetary input (V1 semantics)."""
    name, year = s.requirement.name, s.year
    if s.value_dict is None:
        return ce.MonetaryInput(name, None, year)
    return ce.MonetaryInput.from_value_dict(
        name,
        s.value_dict,
        reference_year=calendar_year_of(dict(s.temporal_extent or {})),
        aggregation=s.aggregation,
        source_flags=dict(s.source_flags or {}),
        estimate_id=s.estimate_id,
    )


# --------------------------------------------------------------------------- result rows (V1 shapes)
def _value(output_key: str, d: Decimal) -> dict[str, Any]:
    unit = ce.OUTPUT_UNITS[output_key]
    if unit == ce.USD:
        basis = {
            "currency": "USD",
            "price_basis": "nominal",
            "scale_factor": "1",
            "notes": "change in border value over one annual accounting period",
        }
    else:
        basis = {"scale_factor": "1", "notes": "percent of same-year baseline nominal GDP (current USD)"}
    return {"kind": "decimal", "value": ce.dstr(d), "unit_ref": unit, "basis": basis}


def _unknown_uncertainty() -> dict[str, Any]:
    return UncertaintySpec.unknown(
        "Unquantified: inputs carry no published uncertainty and no error model is introduced; the value is "
        "conditional on the stated scenario assumptions"
    ).model_dump(mode="json")


def _grid_conditioning(grid: list[dict[str, Any]]) -> tuple[str, ...]:
    return (
        *(
            f"import_elasticity={g['import_elasticity']};export_elasticity={g['export_elasticity']};basis={g['basis']}"
            for g in grid
        ),
        FIXED_POINT_CONDITION,
    )


def _range_uncertainty(lo: Decimal, hi: Decimal, grid: list[dict[str, Any]]) -> dict[str, Any]:
    """Uncertainty of the separate range row: assumption sensitivity only (kernel rule R3-19)."""
    spec = UncertaintySpec(
        status=UncertaintyStatus.quantified,
        kind=UncertaintyKind.sensitivity_range,
        description=(
            f"Elasticity sensitivity range ({SENSITIVITY_STATEMENT}): min/max of this output over the "
            "labelled scenario-assumption grid together with the fixed-quantity point ε=(0,0). Not a "
            "confidence, credible or prediction interval; no coverage level. It says nothing about the "
            "measurement uncertainty of the inputs, which is unquantified."
        ),
        method_version=SENSITIVITY_METHOD,
        lower=ce.dstr(lo),
        upper=ce.dstr(hi),
        conditioning=_grid_conditioning(grid),
    )
    return spec.model_dump(mode="json")


def _range_value(output_key: str, lo: Decimal, hi: Decimal, points: int) -> dict[str, Any]:
    """The range object itself (value kind ``sensitivity_range``)."""
    base = _value(output_key, lo)
    return {
        "kind": "sensitivity_range",
        "lower": ce.dstr(lo),
        "upper": ce.dstr(hi),
        "unit_ref": base["unit_ref"],
        "basis": base["basis"],
        "evaluated_points": points,
        "includes_fixed_quantity_point": True,
        "statement": SENSITIVITY_STATEMENT,
    }


def _applicability(spec: ScenarioSpec, country: Any, variant: str, assumptions: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "applicable",
        "modality": "assumption_conditioned_scenario",
        "model": f"{ce.MODEL_KEY}@{ce.MODEL_VERSION}",
        "country": country.iso3,
        "reference_year": spec.reference_year,
        "horizon": spec.horizon.model_dump(mode="json"),
        "variant": variant,
        "variant_assumptions": assumptions,
        "inputs": country.input_refs(),
        "source_flags": ce.explain(country.inputs)["source_flags"],
        "not_claimed": ce.NOT_CLAIMED,
    }


def compute(compiled: CompiledScenario, fidelity: str) -> list[ResultRow]:
    """Deterministic L1 or L2 evaluation over ready countries (sorted, scheduling-independent)."""
    spec = compiled.spec
    ch = compiled.change
    grid = [g.model_dump(mode="json") for g in spec.assumptions.elasticity_grid]
    rows: list[ResultRow] = []
    wanted = list(spec.outputs)
    for c in sorted(compiled.countries, key=lambda c: c.iso3):
        if c.status != "ready" or c.values is None:
            continue
        m, x, g = c.values
        l1 = ce.exposure_l1(m, x, g, ch.import_multiplier, ch.export_multiplier)
        if fidelity == "L1":
            for k in wanted:
                rows.append(
                    ResultRow(
                        k,
                        c.iso3,
                        c.subject_id,
                        "L1",
                        _value(k, getattr(l1, k)),
                        _unknown_uncertainty(),
                        _applicability(spec, c, "L1", {"quantity_response": "fixed"}),
                    )
                )
            continue
        variants = [
            ce.exposure_l2(
                m,
                x,
                g,
                ch.import_multiplier,
                ch.export_multiplier,
                Decimal(repr(p["import_elasticity"])),
                Decimal(repr(p["export_elasticity"])),
            )
            for p in grid
        ]
        # ε=(0,0) reproduces L1 bit-for-bit; it is always part of the range so the range contains L1.
        fixed_point = ce.exposure_l2(m, x, g, ch.import_multiplier, ch.export_multiplier, Decimal(0), Decimal(0))
        evaluated = {r.variant for r in variants} | {fixed_point.variant}
        for k in wanted:
            lo, hi = ce.sensitivity_range([fixed_point, *variants], k)
            # The fixed-quantity row keeps its own (unquantified) uncertainty: R3-03.
            rows.append(
                ResultRow(
                    k,
                    c.iso3,
                    c.subject_id,
                    "L1",
                    _value(k, getattr(l1, k)),
                    _unknown_uncertainty(),
                    _applicability(spec, c, "L1", {"quantity_response": "fixed"}),
                )
            )
            rows.append(
                ResultRow(
                    k,
                    c.iso3,
                    c.subject_id,
                    SENSITIVITY_VARIANT,
                    _range_value(k, lo, hi, len(evaluated)),
                    _range_uncertainty(lo, hi, grid),
                    _applicability(
                        spec,
                        c,
                        SENSITIVITY_VARIANT,
                        {
                            "quantity_response": "elasticity_sensitivity",
                            "range_over": "elasticity grid ∪ {(0,0)} (fixed-quantity L1 point)",
                            "elasticity_grid": grid,
                            "statement": SENSITIVITY_STATEMENT,
                        },
                    ),
                )
            )
            for p, r in zip(grid, variants, strict=True):
                if r.variant in {row.variant for row in rows if row.output_key == k and row.iso3 == c.iso3}:
                    continue  # duplicate grid points are evaluated once
                rows.append(
                    ResultRow(
                        k,
                        c.iso3,
                        c.subject_id,
                        r.variant,
                        _value(k, getattr(r, k)),
                        _unknown_uncertainty(),
                        _applicability(spec, c, r.variant, {"quantity_response": "elasticity", **p}),
                    )
                )
    return rows


# --------------------------------------------------------------------------- plugin
class CrudeExposureModel:
    """``economic.crude_exposure@1.0.0`` as a :class:`reva.simulation.plugins.SimulationModel`."""

    model_key = ce.MODEL_KEY
    model_version = ce.MODEL_VERSION
    fidelities = frozenset({"L1", "L2"})
    missing_input_policies = frozenset({"fail", "skip_missing_countries"})  # V2 step 1 (ALLC)
    supports_all_countries = True  # countries ["ALL"] is resolved by the platform (V2 step 1, ALLC)

    @staticmethod
    def _spec(spec: dict[str, Any] | ScenarioSpec) -> ScenarioSpec:
        return spec if isinstance(spec, ScenarioSpec) else ScenarioSpec.model_validate(spec)

    # -- protocol ----------------------------------------------------------------------
    def validate(self, spec: dict[str, Any]) -> None:
        """Model checks after the schema: horizon, then the intervention (V1 order)."""
        s = self._spec(spec)
        ce.check_horizon(s.horizon.periods, s.horizon.unit)
        compile_intervention(s.intervention.model_dump(mode="json"), s.assumptions.quantity_response)

    def requirements(self, spec: dict[str, Any]) -> list[InputRequirement]:
        s = self._spec(spec)
        return [
            InputRequirement(n, ce.INPUT_CONCEPTS[n], iso3, (s.reference_year,), "scalar")
            for iso3 in s.countries
            for n in INPUT_ORDER
        ]

    def compile(self, inputs: ResolvedInputs, spec: dict[str, Any]) -> CompiledScenario:
        """Typed per-country plans; missing inputs are reported per country, never zeroed."""
        s = self._spec(spec)
        change = compile_intervention(s.intervention.model_dump(mode="json"), s.assumptions.quantity_response)
        plans: list[CountryPlan] = []
        for iso3 in s.countries:
            subj = inputs.subject(iso3)
            ci = ce.CountryInputs(
                iso3, *(monetary_input(inputs.scalar(n, iso3, s.reference_year)) for n in INPUT_ORDER)
            )
            plan = CountryPlan(iso3, subj.entity_id, subj.resolution, ci)
            try:
                plan.values = ce.validate_country_inputs(
                    ci,
                    reference_year=s.reference_year,
                    horizon_periods=s.horizon.periods,
                    horizon_unit=s.horizon.unit,
                )
            except MissingInput as e:
                plan.status = "missing_input"
                plan.problems = [{"code": "missing_input", **m} for m in e.details.get("missing", [])]
            except (IncompatibleSemantics, ValidationProblem) as e:
                plan.status = "incompatible"
                plan.problems = [{"code": e.code, "country": iso3, "message": e.message, **e.details}]
            plans.append(plan)
        return CompiledScenario(
            spec=s, release=dict(inputs.release), change=change, countries=plans, schedule=build_coupling(plans)
        )

    def compute(self, compiled: CompiledScenario, fidelity: str) -> list[ResultRow]:
        return compute(compiled, fidelity)

    def manifest(self) -> dict[str, Any]:
        return {
            "implementation": "reva.models.economic.crude_exposure",
            "implementation_digest": implementation_digest(),
            "decimal_precision": ce.PRECISION,
        }

    # -- optional hooks ------------------------------------------------------------------
    def spec_schema(self) -> dict[str, Any]:
        from reva.simulation.scenario_schema import crude_branch_schema

        return crude_branch_schema()

    def explanation(self, fidelity: str) -> dict[str, Any]:
        return ce.explain(None, fidelity=fidelity)

    def run_applicability(self, compiled: CompiledScenario, fidelity: str) -> dict[str, Any]:
        out = {
            "explanation": ce.explain(None, fidelity=fidelity),
            "intervention": compiled.change.as_json(),
            "countries": {c.iso3: {"status": c.status, "problems": c.problems} for c in compiled.countries},
            "schedule": compiled.schedule,
            "baseline_inputs": compiled.baseline_inputs(),
            "baseline_digest": compiled.baseline_digest(),
        }
        # V2 step 1 (ALLC): only present for the new options, so V1 runs keep their exact shape.
        if compiled.country_selection is not None:
            out["country_selection"] = compiled.country_selection
        if getattr(compiled.spec, "missing_input_policy", "fail") == "skip_missing_countries":
            out["missing_input_policy"] = "skip_missing_countries"
            out["skipped_countries"] = compiled.skipped
            out["computed_countries"] = sorted(c.iso3 for c in compiled.countries if c.status == "ready")
        return out

    def cost(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {"grid_points": len(spec.get("assumptions", {}).get("elasticity_grid", []))}

    def implementation_files(self) -> tuple[Path, ...]:
        return _impl_files()


def _impl_files() -> tuple[Path, ...]:
    here = Path(__file__)
    return (
        Path(ce.__file__),
        here.with_name("engine.py"),
        here.with_name("compile.py"),
        here.with_name("coupling.py"),
        here.with_name("plugins.py"),
        here,
    )


_DIGEST: list[str] = []


def implementation_digest() -> str:
    """Hash of the model + platform sources: any change invalidates the L0 cache and run reuse."""
    if not _DIGEST:
        _DIGEST.append(plugins.source_digest(*_impl_files()))
    return _DIGEST[0]


MODEL = plugins.register(CrudeExposureModel())
