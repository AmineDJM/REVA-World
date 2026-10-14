"""Scenario compilation: pinned baseline inputs, typed intervention changes, coupling.

Baseline inputs come only from the scenario's pinned *published* belief release
(``selected_report`` estimates whose support is exactly the calendar year equal to
the reference year). Missing estimates or typed ``Missing`` values become
per-country missing inputs; they are never omitted silently or replaced by zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

from reva.adapters.postgres.repositories import resolve_alias
from reva.belief.snapshots import estimates, release_by_id
from reva.kernel.errors import (
    CapabilityGap,
    IncompatibleSemantics,
    LeakageBlocked,
    MissingInput,
    ValidationProblem,
)
from reva.kernel.ids import external_id, logical_hash
from reva.kernel.values import decimal_str
from reva.models.economic import crude_exposure as ce
from reva.simulation.coupling import CouplingGraph, PortSpec
from reva.simulation.spec import ScenarioSpec

SUPPORTED_CHANGES = ("set_exogenous_border_price_multiplier",)


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
            "import_multiplier": decimal_str(self.import_multiplier),
            "export_multiplier": decimal_str(self.export_multiplier),
            "unit": self.unit,
            "duration": self.duration,
            "held_fixed": list(self.held_fixed),
            "affected_market": "crude petroleum (HS 2709) border values, world partner totals",
        }


def compile_intervention(intervention: dict[str, Any], quantity_response: str) -> TypedChange:
    kind = intervention.get("kind")
    if kind not in SUPPORTED_CHANGES:
        raise CapabilityGap(f"intervention kind {kind!r} is unsupported; supported: {list(SUPPORTED_CHANGES)}",
                            capability=f"intervention.{kind}")
    if intervention.get("unit") != "ratio":
        raise IncompatibleSemantics("border-price multipliers must be ratios")
    p_m = Decimal(repr(float(intervention["import_multiplier"])))
    p_x = Decimal(repr(float(intervention["export_multiplier"])))
    for name, p in (("import_multiplier", p_m), ("export_multiplier", p_x)):
        if p <= 0:
            raise ValidationProblem(f"{name} must be > 0")
    held = ["import and export quantities"] if quantity_response == "fixed" else []
    held += ["GDP (baseline denominator)", "exchange rates", "all other prices and flows"]
    return TypedChange(kind=str(kind), import_multiplier=p_m, export_multiplier=p_x, unit="ratio",
                       duration="one annual accounting period", held_fixed=tuple(held))


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
        return {n: {"estimate_id": i.estimate_id, "concept": ce.INPUT_CONCEPTS[n],
                    "value": (decimal_str(i.value) if isinstance(i.value, Decimal) else None),
                    "reference_year": i.reference_year, "valuation": i.valuation}
                for n, i in self.inputs.by_name().items()}


@dataclass
class CompiledScenario:
    spec: ScenarioSpec
    release: dict[str, Any]
    change: TypedChange
    countries: list[CountryPlan]
    schedule: list[str]

    @property
    def missing(self) -> list[dict[str, Any]]:
        return [p for c in self.countries for p in c.problems if p.get("code") == "missing_input"]

    @property
    def incompatible(self) -> list[dict[str, Any]]:
        return [p for c in self.countries for p in c.problems if p.get("code") != "missing_input"]

    def baseline_inputs(self) -> dict[str, Any]:
        return {c.iso3: {"subject_id": str(c.subject_id), "subject_resolution": c.subject_resolution,
                         "status": c.status, "inputs": c.input_refs()} for c in self.countries}

    def baseline_digest(self) -> str:
        return logical_hash({"release_id": str(self.release["id"]), "inputs": self.baseline_inputs()})


def _year_of_calendar_support(temporal: dict[str, Any]) -> int | None:
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


def resolve_subject(conn: Connection, iso3: str) -> tuple[UUID, str]:
    sid = resolve_alias(conn, "iso3166_alpha3", iso3)
    if sid is not None:
        return sid, "reviewed_alias:iso3166_alpha3"
    return external_id("reva.entity", iso3), "deterministic_entity_id"


def _resolve_input(conn: Connection, release_id: UUID, subject_id: UUID, name: str,
                   reference_year: int) -> ce.MonetaryInput:
    rows = [
        r for r in estimates(conn, release_id, subject_id=subject_id, concept_id=ce.INPUT_CONCEPTS[name])
        if r["modality"] == "selected_report" and _year_of_calendar_support(r["temporal_extent"]) == reference_year
    ]
    if not rows:
        return ce.MonetaryInput(name, None, reference_year)
    if len({str(r["id"]) for r in rows}) > 1:
        raise IncompatibleSemantics(
            f"{len(rows)} distinct selected estimates for {ce.INPUT_CONCEPTS[name]} {reference_year}; "
            "the baseline is ambiguous", input=name)
    r = rows[0]
    return ce.MonetaryInput.from_value_dict(
        name, r["value"], reference_year=_year_of_calendar_support(r["temporal_extent"]),
        aggregation=r["aggregation"], source_flags=r.get("source_flags") or {}, estimate_id=str(r["id"]),
    )


def _port_from_input(inp: ce.MonetaryInput, name: str) -> PortSpec:
    nominal_usd = (
        (inp.currency or "").upper() == "USD" and inp.price_basis in ce.NOMINAL_PRICE_BASES
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


def load_release(conn: Connection, release_id: UUID, knowledge_cutoff: str) -> dict[str, Any]:
    rel = release_by_id(conn, release_id)
    if rel is None or rel.get("sequence") is None:
        raise ValidationProblem(f"baseline release {release_id} is not a published release")
    cutoff = datetime.fromisoformat(knowledge_cutoff.replace("Z", "+00:00"))
    if rel["knowledge_cutoff"] > cutoff:
        raise LeakageBlocked(
            f"baseline release cutoff {rel['knowledge_cutoff'].isoformat()} is after the scenario knowledge "
            f"cutoff {cutoff.isoformat()}"
        )
    return rel


def compile_scenario(conn: Connection, spec: ScenarioSpec) -> CompiledScenario:
    """Resolve baseline inputs and compile the intervention. Raises on unsupported requests;
    missing inputs are reported per country on the result (never raised away or zeroed)."""
    ce.check_horizon(spec.horizon.periods, spec.horizon.unit)
    change = compile_intervention(spec.intervention.model_dump(mode="json"), spec.assumptions.quantity_response)
    release = load_release(conn, UUID(spec.baseline_release_id), spec.knowledge_cutoff)
    plans: list[CountryPlan] = []
    for iso3 in spec.countries:
        sid, how = resolve_subject(conn, iso3)
        inputs = ce.CountryInputs(
            iso3, *(_resolve_input(conn, release["id"], sid, n, spec.reference_year) for n in ("M", "X", "G"))
        )
        plan = CountryPlan(iso3, sid, how, inputs)
        try:
            plan.values = ce.validate_country_inputs(
                inputs, reference_year=spec.reference_year,
                horizon_periods=spec.horizon.periods, horizon_unit=spec.horizon.unit,
            )
        except MissingInput as e:
            plan.status = "missing_input"
            plan.problems = [{"code": "missing_input", **m} for m in e.details.get("missing", [])]
        except (IncompatibleSemantics, ValidationProblem) as e:
            plan.status = "incompatible"
            plan.problems = [{"code": e.code, "country": iso3, "message": e.message, **e.details}]
        plans.append(plan)
    schedule = build_coupling(plans)
    return CompiledScenario(spec=spec, release=release, change=change, countries=plans, schedule=schedule)
