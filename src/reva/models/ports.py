"""Model port contracts and the WorldModel protocol (architecture §12.1).

A :class:`PortSpec` fixes everything required to know whether a value leaving
one model may enter another: concept version, unit and basis, support,
dimensions, temporal resolution and aggregation, stock/flow/rate
interpretation, uncertainty/dependence group, permitted interpolation, state
ownership and conservation role.

:func:`compatible` never converts implicitly. A link whose ports differ in any
semantic attribute is incompatible unless an *approved*, versioned
:class:`TransformOperator` of the right kind bridges exactly that difference
(for example an annual flow can feed a monthly port only through an approved
``temporal_disaggregation`` operator). Incompatibility is returned with reasons;
callers that execute couplings must refuse to run on ``ok=False``.

Pure module: no database, HTTP or adapter imports (import-linter contract).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

# --------------------------------------------------------------------------- vocabularies

TEMPORAL_RESOLUTION_MONTHS: dict[str, float] = {
    "annual": 12.0,
    "quarterly": 3.0,
    "monthly": 1.0,
    "daily": 12.0 / 365.0,
}
"""Calendar resolutions with a natural ordering. ``static`` and ``instant`` are
not on this scale and only connect to themselves."""

TemporalResolution = Literal["annual", "quarterly", "monthly", "daily", "instant", "static"]
Interpretation = Literal["stock", "flow", "rate", "ratio", "state", "parameter", "categorical"]
ConservationRole = Literal["conserved_quantity", "balance_term", "none"]
StateAccess = Literal["none", "read", "write"]
Direction = Literal["in", "out"]

OperatorKind = Literal[
    "temporal_disaggregation",  # coarse -> fine (e.g. annual flow -> monthly)
    "temporal_aggregation",  # fine -> coarse (e.g. monthly flow -> annual total)
    "unit_conversion",
    "basis_conversion",  # e.g. nominal -> real with a pinned deflator
    "support_crosswalk",  # spatial / population support change
    "concept_mapping",  # approved ontology mapping between concept versions
    "stock_flow_conversion",  # e.g. stock differencing to a flow
]


@dataclass(frozen=True)
class PortSpec:
    """Semantics of one model port. Immutable; hashed into the model spec."""

    name: str
    direction: Direction
    concept: str
    concept_version: str
    unit: str
    basis: str  # e.g. "nominal_current", "persons", "not_applicable"
    spatial_support: str  # e.g. "provider_reporter_territory:country"
    population_universe: str  # e.g. "economy", "de_facto_residents_wdi", "not_applicable"
    dimensions: tuple[str, ...] = ("entity", "time")
    temporal_resolution: TemporalResolution = "annual"
    temporal_aggregation: str = "period_flow_total"  # kernel Support.aggregation vocabulary
    interpretation: Interpretation = "flow"
    uncertainty_group: str | None = None
    dependence_group: str | None = None
    permitted_interpolation: tuple[str, ...] = ()
    state_owner: str | None = None  # model key that owns the state variable carried by this port
    state_access: StateAccess = "none"
    conservation_role: ConservationRole = "none"

    def __post_init__(self) -> None:
        if self.direction not in ("in", "out"):
            raise ValueError(f"port {self.name}: direction must be 'in' or 'out'")
        if self.temporal_resolution not in (*TEMPORAL_RESOLUTION_MONTHS, "instant", "static"):
            raise ValueError(f"port {self.name}: unknown temporal resolution {self.temporal_resolution!r}")
        if self.state_access == "write" and not self.state_owner:
            raise ValueError(f"port {self.name}: a state-writing port must name its state owner")
        if len(set(self.dimensions)) != len(self.dimensions):
            raise ValueError(f"port {self.name}: duplicate dimensions")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "direction": self.direction,
            "concept": self.concept,
            "concept_version": self.concept_version,
            "unit": self.unit,
            "basis": self.basis,
            "spatial_support": self.spatial_support,
            "population_universe": self.population_universe,
            "dimensions": list(self.dimensions),
            "temporal_resolution": self.temporal_resolution,
            "temporal_aggregation": self.temporal_aggregation,
            "interpretation": self.interpretation,
            "uncertainty_group": self.uncertainty_group,
            "dependence_group": self.dependence_group,
            "permitted_interpolation": list(self.permitted_interpolation),
            "state_owner": self.state_owner,
            "state_access": self.state_access,
            "conservation_role": self.conservation_role,
        }


@dataclass(frozen=True)
class TransformOperator:
    """A versioned operator that may bridge one declared semantic difference.

    Only operators with ``approved=True`` are ever used by :func:`compatible`.
    ``from_value``/``to_value`` name the attribute values it maps between
    (e.g. ``"annual"`` -> ``"monthly"`` for a temporal disaggregation), and
    ``interpretations`` restricts it to stock/flow/rate semantics it is valid for.
    ``method`` names the interpolation/disaggregation method, which must also be
    listed in the source port's ``permitted_interpolation`` when it creates
    finer-resolution values.
    """

    key: str
    version: str
    kind: OperatorKind
    from_value: str
    to_value: str
    approved: bool
    method: str = ""
    interpretations: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        return f"{self.key}@{self.version}"


@dataclass(frozen=True)
class Compatibility:
    ok: bool
    reasons: tuple[str, ...]  # blocking problems (empty when ok)
    operators: tuple[str, ...] = ()  # approved operators required on the link
    warnings: tuple[str, ...] = ()  # non-blocking notes (e.g. dependence groups)

    def __bool__(self) -> bool:
        return self.ok


def _find_operator(
    operators: Sequence[TransformOperator],
    kind: OperatorKind,
    from_value: str,
    to_value: str,
    interpretation: str,
) -> TransformOperator | None:
    for op in operators:
        if (
            op.approved
            and op.kind == kind
            and op.from_value == from_value
            and op.to_value == to_value
            and (not op.interpretations or interpretation in op.interpretations)
        ):
            return op
    return None


def compatible(
    out_port: PortSpec, in_port: PortSpec, operators: Sequence[TransformOperator] = ()
) -> Compatibility:
    """May ``out_port`` feed ``in_port`` (possibly through approved operators)?

    Returns a :class:`Compatibility` carrying every blocking reason (not only the
    first), the operator references the link requires, and warnings.
    """
    reasons: list[str] = []
    used: list[str] = []
    warnings: list[str] = []

    if out_port.direction != "out":
        reasons.append(f"direction: {out_port.name} is not an output port")
    if in_port.direction != "in":
        reasons.append(f"direction: {in_port.name} is not an input port")

    def bridge(attr: str, kind: OperatorKind, src: str, dst: str) -> None:
        if src == dst:
            return
        op = _find_operator(operators, kind, src, dst, out_port.interpretation)
        if op is None:
            reasons.append(f"{attr}: {src!r} -> {dst!r} requires an approved {kind} operator")
        else:
            used.append(op.ref)

    # Concept identity (version included): only an approved mapping operator.
    bridge(
        "concept",
        "concept_mapping",
        f"{out_port.concept}@{out_port.concept_version}",
        f"{in_port.concept}@{in_port.concept_version}",
    )
    bridge("unit", "unit_conversion", out_port.unit, in_port.unit)
    bridge("basis", "basis_conversion", out_port.basis, in_port.basis)
    bridge("spatial_support", "support_crosswalk", out_port.spatial_support, in_port.spatial_support)
    bridge(
        "population_universe", "support_crosswalk", out_port.population_universe, in_port.population_universe
    )

    if tuple(out_port.dimensions) != tuple(in_port.dimensions):
        reasons.append(f"dimensions: {list(out_port.dimensions)} -> {list(in_port.dimensions)} differ")

    # Stock / flow / rate interpretation never changes implicitly.
    if out_port.interpretation != in_port.interpretation:
        bridge(
            "interpretation", "stock_flow_conversion", out_port.interpretation, in_port.interpretation
        )

    # Temporal resolution: coarse -> fine needs disaggregation (+ a permitted
    # interpolation method); fine -> coarse needs an aggregation operator.
    src_r, dst_r = out_port.temporal_resolution, in_port.temporal_resolution
    if src_r != dst_r:
        if src_r not in TEMPORAL_RESOLUTION_MONTHS or dst_r not in TEMPORAL_RESOLUTION_MONTHS:
            reasons.append(f"temporal_resolution: {src_r!r} -> {dst_r!r} has no defined conversion")
        else:
            coarse_to_fine = TEMPORAL_RESOLUTION_MONTHS[src_r] > TEMPORAL_RESOLUTION_MONTHS[dst_r]
            kind: OperatorKind = "temporal_disaggregation" if coarse_to_fine else "temporal_aggregation"
            op = _find_operator(operators, kind, src_r, dst_r, out_port.interpretation)
            if op is None:
                what = "disaggregation" if coarse_to_fine else "aggregation"
                reasons.append(
                    f"temporal_resolution: {out_port.interpretation} at {src_r!r} cannot feed a {dst_r!r} "
                    f"port without an approved {what} operator"
                )
            elif coarse_to_fine and op.method not in out_port.permitted_interpolation:
                reasons.append(
                    f"temporal_resolution: operator {op.ref} uses method {op.method!r}, which is not in "
                    f"{out_port.name}.permitted_interpolation {list(out_port.permitted_interpolation)}"
                )
            else:
                used.append(op.ref)
    if out_port.temporal_aggregation != in_port.temporal_aggregation and src_r == dst_r:
        reasons.append(
            f"temporal_aggregation: {out_port.temporal_aggregation!r} -> {in_port.temporal_aggregation!r} differ"
        )

    # State ownership: only the owner may write a state variable.
    if in_port.state_access == "write":
        if out_port.state_owner != in_port.state_owner:
            reasons.append(
                f"state_ownership: {in_port.name} writes state owned by {in_port.state_owner!r}; "
                f"producer owner {out_port.state_owner!r} may not overwrite it"
            )
    elif out_port.state_owner and in_port.state_owner and out_port.state_owner != in_port.state_owner:
        warnings.append(
            f"state_ownership: read-only link between states owned by {out_port.state_owner!r} "
            f"and {in_port.state_owner!r}"
        )

    # Conservation: conserved quantities connect only to conserved ports.
    if out_port.conservation_role != in_port.conservation_role and "conserved_quantity" in (
        out_port.conservation_role,
        in_port.conservation_role,
    ):
        reasons.append(
            f"conservation_role: {out_port.conservation_role!r} -> {in_port.conservation_role!r}; "
            "a conserved quantity cannot enter or leave a non-conserving port"
        )

    # Dependence: not blocking, but joint uncertainty must not be silently assumed independent.
    if (
        out_port.dependence_group
        and in_port.dependence_group
        and out_port.dependence_group != in_port.dependence_group
    ):
        warnings.append(
            f"dependence_group: {out_port.dependence_group!r} vs {in_port.dependence_group!r}; joint "
            "uncertainty propagation must declare the dependence explicitly"
        )
    if out_port.uncertainty_group is None and in_port.uncertainty_group is not None:
        warnings.append("uncertainty_group: producer declares no uncertainty group; consumer expects one")

    return Compatibility(
        ok=not reasons,
        reasons=tuple(reasons),
        operators=tuple(dict.fromkeys(used)) if not reasons else (),
        warnings=tuple(warnings),
    )


def port_from_registry(entry: Mapping[str, Any], direction: Direction) -> PortSpec | None:
    """Build a PortSpec from a ``registry/models/methods.yaml`` port entry.

    Registry entries for some models are partial (e.g. forecasting ports only
    name a series kind). Returns ``None`` when the entry does not carry the
    minimum semantic fields (concept, unit, basis, aggregation): such a port is
    *not* treated as compatible with anything.
    """
    needed = ("concept", "unit", "basis", "aggregation")
    if not all(k in entry for k in needed):
        return None
    agg = str(entry["aggregation"])
    interpretation: Interpretation = (
        "flow" if agg == "period_flow_total" else "rate" if agg == "period_rate" else "stock"
    )
    return PortSpec(
        name=str(entry["name"]),
        direction=direction,
        concept=str(entry["concept"]),
        concept_version=str(entry.get("concept_version", "1.0.0")),
        unit=str(entry["unit"]),
        basis=str(entry["basis"]),
        spatial_support=str(entry.get("spatial_support", "provider_reporter_territory:country")),
        population_universe=str(entry.get("population_universe", "economy")),
        temporal_resolution=entry.get("temporal_resolution", "annual"),
        temporal_aggregation=agg,
        interpretation=interpretation,
    )


# --------------------------------------------------------------------------- WorldModel protocol


class CapabilityLevel(StrEnum):
    """Execution fidelity levels (see :mod:`reva.models.applicability`)."""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


@dataclass(frozen=True)
class ApplicabilityReport:
    """Result of checking a request against a model's declared envelope.

    ``applicable=False`` is a typed refusal: the model must not be run (no silent
    extrapolation). ``checked`` lists the checks performed so an empty
    ``reasons`` tuple is auditable.
    """

    applicable: bool
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    checked: tuple[str, ...] = ()
    model_ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "applicable": self.applicable,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "checked": list(self.checked),
            "model_ref": self.model_ref,
        }


@dataclass(frozen=True)
class ModelSpec:
    """Immutable description of one model version (architecture §12.1)."""

    model_key: str
    version: str
    kind: str
    family: str
    estimand: str
    input_ports: tuple[PortSpec, ...]
    output_ports: tuple[PortSpec, ...]
    static: bool  # static models implement step() as one evaluation
    admissible_interventions: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    known_failures: tuple[str, ...] = ()
    licence: str = "internal"
    owners: tuple[str, ...] = ("reva-maintainers",)
    approval_status: Literal["approved", "candidate", "retired"] = "candidate"
    capability_levels: tuple[CapabilityLevel, ...] = (CapabilityLevel.L1,)
    resource_bounds: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.model_key}@{self.version}"


@dataclass(frozen=True)
class InputBundle:
    """Immutable, release-pinned model inputs keyed by input port name."""

    values: Mapping[str, Any]
    release_id: str | None = None
    knowledge_cutoff: str | None = None


@dataclass(frozen=True)
class PortBundle:
    """Values exchanged on coupling ports during one step."""

    values: Mapping[str, Any]


@dataclass(frozen=True)
class ModelClock:
    step_index: int
    period_label: str
    resolution: TemporalResolution = "annual"


@dataclass(frozen=True)
class TargetSet:
    targets: tuple[str, ...]


@dataclass(frozen=True)
class PredictionBundle:
    predictions: Mapping[str, Any]
    uncertainty: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Checkpoint:
    model_ref: str
    state_digest: str
    payload: Mapping[str, Any]


@runtime_checkable
class RandomStreams(Protocol):
    """Named, seeded random streams (no global RNG)."""

    def stream(self, name: str) -> Any: ...


@runtime_checkable
class ModelState(Protocol):
    """Opaque model state owned by exactly one model."""

    @property
    def owner(self) -> str: ...


@dataclass(frozen=True)
class StepResult:
    state: ModelState
    outputs: PortBundle
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class WorldModel(Protocol):
    """Model interface (architecture §12.1).

    Models receive immutable inputs and capability-limited artifact readers and
    writers, never HTTP clients or database sessions.
    """

    def specification(self) -> ModelSpec: ...
    def validate_inputs(self, inputs: InputBundle) -> ApplicabilityReport: ...
    def initialise(self, inputs: InputBundle, rng: RandomStreams) -> ModelState: ...
    def step(self, state: ModelState, ports: PortBundle, clock: ModelClock) -> StepResult: ...
    def observe(self, state: ModelState, targets: TargetSet) -> PredictionBundle: ...
    def checkpoint(self, state: ModelState) -> Checkpoint: ...


__all__ = [
    "ApplicabilityReport",
    "CapabilityLevel",
    "Checkpoint",
    "Compatibility",
    "InputBundle",
    "ModelClock",
    "ModelSpec",
    "ModelState",
    "PortBundle",
    "PortSpec",
    "PredictionBundle",
    "RandomStreams",
    "StepResult",
    "TargetSet",
    "TransformOperator",
    "WorldModel",
    "compatible",
    "port_from_registry",
]
