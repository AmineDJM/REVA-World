"""Versioned, machine-readable capability catalogue.

REVA must answer an unsupported request with an explicit capability gap, never a
plausible-looking output (architecture §1, §16.3, §19, invariant 34). This module
is the single declaration of what the running build can and cannot do:

* ``implemented`` — executable code exists and its acceptance tests run in CI.
* ``reserved``    — a typed extension interface exists (or is planned) but the
  capability is not available; requests receive ``capability_not_available``.
* ``blocked``     — unavailable because an external precondition (configuration,
  infrastructure, licensed data) is absent, even though an interface exists.

Later-phase capabilities (P8–P12) are *never* ``implemented`` here. A few pure
verification helpers that those phases' acceptance gates need (an analytic
field-aggregation check, a double-entry ledger verifier, a BMI lifecycle checker)
are listed separately with ids under ``verification.*`` and are explicitly
labelled verification-only: they do not provide the domain capability.

The catalogue reads one configuration value (whether ``REVA_SANDBOX_ENDPOINT`` is
set) and never exposes the endpoint or any token.

``require(capability_id)`` raises :class:`reva.kernel.errors.CapabilityGap`
with structured details for anything not implemented; ``as_dict()`` is the JSON
shape for API exposure (e.g. ``GET /v1/models``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from reva.kernel.errors import CapabilityGap
from reva.kernel.ids import logical_hash

CATALOGUE_VERSION = "1.0.0"
CATALOGUE_SCHEMA = "reva.capability_catalogue/1"

Status = Literal["implemented", "reserved", "blocked"]
PHASES: tuple[str, ...] = (*(f"P{i}" for i in range(13)), "unscheduled")

SANDBOX_ENDPOINT_ENV = "REVA_SANDBOX_ENDPOINT"


@dataclass(frozen=True)
class Capability:
    capability_id: str
    title: str
    status: Status
    phase: str
    owner_module: str
    """Module that owns (or, per architecture §22/§25, will own) the capability."""
    required_evidence: tuple[str, ...]
    """Acceptance gates / evidence that must pass before status may become implemented
    (for implemented entries: the gates that currently hold it there)."""
    reason: str | None = None
    """Why the capability is not available. Required unless implemented."""
    interface: str | None = None
    """Dotted path of the typed extension interface present in this build, if any."""
    requires_config: tuple[str, ...] = ()
    verification_only: bool = False
    notes: str | None = None
    available_instead: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.status not in ("implemented", "reserved", "blocked"):
            raise ValueError(f"invalid status {self.status!r} for {self.capability_id}")
        if self.phase not in PHASES:
            raise ValueError(f"invalid phase {self.phase!r} for {self.capability_id}")
        if self.status != "implemented" and not (self.reason and self.reason.strip()):
            raise ValueError(f"{self.capability_id}: a {self.status} capability needs a reason")
        if not self.required_evidence:
            raise ValueError(f"{self.capability_id}: required_evidence must name at least one gate")

    @property
    def available(self) -> bool:
        return self.status == "implemented"

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "title": self.title,
            "status": self.status,
            "available": self.available,
            "phase": self.phase,
            "owner_module": self.owner_module,
            "interface": self.interface,
            "required_evidence": list(self.required_evidence),
            "reason": self.reason,
            "requires_config": list(self.requires_config),
            "verification_only": self.verification_only,
            "notes": self.notes,
            "available_instead": list(self.available_instead),
        }


@dataclass(frozen=True)
class CapabilityNotAvailable:
    """Typed result returned (instead of an output) for a non-implemented capability."""

    capability_id: str
    status: Literal["reserved", "blocked", "unknown"]
    phase: str | None
    reason: str
    required_evidence: tuple[str, ...]
    catalogue_version: str = CATALOGUE_VERSION
    kind: Literal["capability_not_available"] = "capability_not_available"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "capability_id": self.capability_id,
            "status": self.status,
            "phase": self.phase,
            "reason": self.reason,
            "required_evidence": list(self.required_evidence),
            "catalogue_version": self.catalogue_version,
        }

    def to_error(self) -> CapabilityGap:
        return CapabilityGap(
            f"capability {self.capability_id!r} is not available ({self.status}): {self.reason}",
            **{k: v for k, v in self.as_dict().items() if k != "capability_id"},
            capability=self.capability_id,
        )


_ROADMAP = "architecture §25 phase table"

_BASE: tuple[Capability, ...] = (
    # ------------------------------------------------------------------ P4/P5: V1 executable envelope
    Capability(
        "fidelity.L0",
        "Exact full-specification cache lookup",
        "implemented",
        "P4",
        "reva.simulation.engine",
        ("identical spec digest + pinned manifests return the cached result; otherwise an explicit cache miss",),
        notes="No surrogate: a near-identical specification is a cache miss.",
    ),
    Capability(
        "fidelity.L1",
        "Deterministic fixed-quantity crude-value exposure",
        "implemented",
        "P4",
        "reva.models.economic.crude_exposure",
        ("hand-computed cases match", "p=1 gives zero", "missing input blocks"),
        notes="Assumption-conditioned accounting model; not GDP loss, inflation, unemployment or welfare.",
    ),
    Capability(
        "fidelity.L2",
        "Labelled elasticity sensitivity of crude-value exposure",
        "implemented",
        "P4",
        "reva.models.economic.crude_exposure",
        ("elasticity 0 equals L1", "registered elasticity envelope enforced"),
        notes="Sensitivity grid over registered elasticities; not an uncertainty distribution.",
    ),
    Capability(
        "fidelity.L3",
        "Paired Monte Carlo uncertainty propagation",
        "reserved",
        "P4",
        "reva.simulation.engine",
        (
            "every parameter distribution has justified provenance (registered evidence)",
            "named counter-based random streams verified scheduling-independent",
            "Monte Carlo error reported with the result",
        ),
        reason=(
            "no model parameter has a distribution with justified provenance; sampling invented "
            "distributions would fabricate uncertainty"
        ),
        available_instead=("fidelity.L2",),
    ),
    Capability(
        "fidelity.L4",
        "Competing models, global sensitivity and deep research",
        "reserved",
        "P11",
        "reva.simulation.engine",
        (
            "more than one approved model for the estimand with locked-holdout tournament evidence",
            "named sensitivity method valid for the input dependence structure",
            "secure external research execution (autonomy.external_sandbox_runner)",
        ),
        reason="research-grade multi-model execution and global sensitivity are not implemented",
        available_instead=("fidelity.L1", "fidelity.L2"),
    ),
    Capability(
        "coupling.acyclic",
        "Acyclic one-way model coupling with port compatibility and SCC detection",
        "implemented",
        "P4",
        "reva.simulation.coupling",
        ("port stock/flow/rate compatibility enforced", "strongly connected components detected and rejected"),
    ),
    Capability(
        "coupling.feedback",
        "Feedback-block coupling solver",
        "reserved",
        "P9",
        "reva.simulation.feedback",
        (
            "explicit convergence contract (step size, criterion, damping, iteration cap, conservative exchange, "
            "error bounds)",
            "solver converges on reference problems",
            "non-convergence fails or returns qualified partial results, never the last iterate as equilibrium",
        ),
        reason="V1 executes acyclic coupling only; no feedback solver has passed reference-problem convergence",
        interface="reva.extensions.feedback",
        available_instead=("coupling.acyclic",),
    ),
    Capability(
        "horizon.multi_period",
        "Dynamic multi-period simulation",
        "reserved",
        "P9",
        "reva.simulation.engine",
        ("stock/flow ledger and dynamic model with validated transition dynamics",),
        reason="the V1 model is a one-annual-period accounting identity; dynamics are not modelled",
    ),
    Capability(
        "outputs.macroeconomic",
        "GDP loss, unemployment, consumer inflation or household welfare outputs",
        "reserved",
        "P9",
        "reva.models.economic",
        ("structural macro/IO model with identification review", "independent causal policy model review"),
        reason="exposure arithmetic is not a macroeconomic or welfare model",
    ),
    Capability(
        "forecasting.baselines",
        "last_value_v1, log_drift_v1 and local_level_log_v1 forecasts",
        "implemented",
        "P5",
        "reva.models.forecasting",
        ("fit inside fold", "insufficient observations produce an ineligible report", "naive baseline shown"),
    ),
    # ------------------------------------------------------------------ verification-only helpers
    Capability(
        "verification.analytic_field_aggregation",
        "Conservative spherical area-weighted aggregation of an analytic field (verification only)",
        "implemented",
        "P8",
        "reva.models.physical.verification",
        (
            "constant field returns its exact value",
            "separable analytic field matches closed-form spherical integral to 1e-9 relative",
            "partition of regions conserves the global integral",
            "antimeridian-crossing region handled",
        ),
        interface="reva.models.physical.verification",
        verification_only=True,
        notes=(
            "Numerical verification utility for the P8 acceptance gate. It does not read datasets and provides "
            "no weather, climate or hazard capability."
        ),
    ),
    Capability(
        "verification.double_entry_balance",
        "Double-entry and stock-flow consistency verifier (verification only)",
        "implemented",
        "P9",
        "reva.extensions.io_sfc",
        ("every transaction balances per unit", "instrument claims net to zero", "opening + flows = closing"),
        interface="reva.extensions.io_sfc",
        verification_only=True,
        notes="Pure checker of supplied ledgers; there is no SFC or IO model in this build.",
    ),
    Capability(
        "verification.external_model_lifecycle",
        "BMI-style external-model lifecycle conformance checker",
        "implemented",
        "P8",
        "reva.models.physical.adapter",
        ("initialize/update/get_value/finalize lifecycle exercised", "declared units and monotone model time"),
        interface="reva.models.physical.adapter",
        verification_only=True,
        notes="Checks an in-process object against the port contract; no external model is integrated.",
    ),
    # ------------------------------------------------------------------ P8 physical fields
    Capability(
        "physical.field_adapter",
        "CF/COG/Zarr physical-field adapter and STAC asset catalogue",
        "reserved",
        "P8",
        "reva.models.physical.fields",
        (
            "CF metadata (standard name, units, calendar, CRS, axes, cell methods) validated on ingest",
            "antimeridian and CRS tests",
            "same concept/support contract works without kernel change",
        ),
        reason="no field format reader, asset catalogue or field storage is implemented",
        interface="reva.models.physical.fields",
    ),
    Capability(
        "physical.field_to_region",
        "Conservative field-to-region aggregation of real datasets",
        "reserved",
        "P8",
        "reva.world.regridding",
        (
            "known analytic field integrates correctly (see verification.analytic_field_aggregation)",
            "real region geometries with versioned boundaries",
            "regridding error recorded as a versioned model component",
        ),
        reason="only the verification helper on rectangular regions exists; no polygon support or real fields",
        interface="reva.models.physical.adapter",
        available_instead=("verification.analytic_field_aggregation",),
    ),
    Capability(
        "physical.hazard_exposure",
        "Bounded historical heat/drought exposure dataset",
        "reserved",
        "P8",
        "reva.adapters.physical",
        ("withheld temporal/spatial validation", "no weather forecast claimed from climate averages"),
        reason="no hazard dataset has been acquired, validated or licensed",
    ),
    Capability(
        "physical.external_model",
        "Coupled external physical model via BMI/FMI port",
        "reserved",
        "P8",
        "reva.models.physical.adapter",
        ("external-model port conformance", "calendar/timestep/support translation as versioned components"),
        reason="no external model is integrated; only the conformance protocol and checker exist",
        interface="reva.models.physical.adapter",
        available_instead=("verification.external_model_lifecycle",),
    ),
    # ------------------------------------------------------------------ P9 economics
    Capability(
        "economic.input_output",
        "Multi-region sector input-output model with capacity constraints",
        "reserved",
        "P9",
        "reva.models.economic.io",
        (
            "accounting balances",
            "nonnegative feasible production",
            "no double counting between totals and bilateral data",
            "IO multipliers not applied to arbitrarily large supply shocks",
        ),
        reason="no licensed multi-region IO table is ingested and no IO model is implemented",
        interface="reva.extensions.io_sfc",
    ),
    Capability(
        "economic.sfc_ledger",
        "Stock-flow-consistent financial ledger model",
        "reserved",
        "P9",
        "reva.models.economic.ledger",
        ("double-entry balance on every period", "stock-flow reconciliation", "revaluation separated from flows"),
        reason="no sector balance-sheet data or SFC behavioural model exists; only a ledger verifier",
        interface="reva.extensions.io_sfc",
        available_instead=("verification.double_entry_balance",),
    ),
    Capability(
        "causal.policy_model",
        "Independently reviewed causal policy model",
        "reserved",
        "P9",
        "reva.models.economic",
        ("SCM/identification profile", "independent identification review"),
        reason="no causal identification profile has been reviewed; interventions remain typed assumptions",
    ),
    # ------------------------------------------------------------------ P10 population
    Capability(
        "population.households",
        "Household-aware synthetic population",
        "reserved",
        "P10",
        "reva.population.households",
        (
            "licensed/approved joint household data",
            "structural constraints exact",
            "uncertainty over unidentified joints",
            "held-out joints scored",
        ),
        reason="no approved joint household microdata; V1 uses public age/sex aggregate cells only",
        interface="reva.extensions.population",
    ),
    Capability(
        "population.privacy_accounting",
        "Differential-privacy accounting for population releases",
        "reserved",
        "P10",
        "reva.population.privacy",
        ("explicit epsilon/delta and neighbouring relation", "privacy attack assessment"),
        reason="no sensitive microdata is processed and no DP mechanism is implemented",
        interface="reva.extensions.population",
    ),
    Capability(
        "compression.network",
        "Network-aware population compression and rare-event benchmarks",
        "reserved",
        "P10",
        "reva.compression.network",
        ("intervention/subgroup/tail compression certificates pass",),
        reason="household/contact networks are not represented",
    ),
    # ------------------------------------------------------------------ P11 autonomy
    Capability(
        "autonomy.external_sandbox_runner",
        "Disposable VM/microVM runner for generated code",
        "blocked",
        "P11",
        "reva.autonomy.external_runner",
        (
            "deliberate privilege/egress/resource attacks blocked",
            "no secrets, read-only inputs, egress blocked by default, bounded CPU/RAM/disk/time",
            "independent gate evidence",
        ),
        reason=f"no secure external runner is configured ({SANDBOX_ENDPOINT_ENV} unset); proposals remain unexecuted",
        interface="reva.extensions.runner",
        requires_config=(SANDBOX_ENDPOINT_ENV,),
    ),
    Capability(
        "autonomy.parser_repair",
        "Autonomous parser-repair branches",
        "reserved",
        "P11",
        "reva.autonomy.code_changes",
        ("isolated branch + external runner evidence", "named review for T2"),
        reason="generated parser code cannot run without autonomy.external_sandbox_runner",
    ),
    Capability(
        "models.shadow_promotion",
        "Shadow models, locked-holdout tournament and signed promotion",
        "reserved",
        "P11",
        "reva.operations.releases",
        ("no holdout access by proposer", "rollback exercised", "prospective improvement or rollback"),
        reason="signed promotion and locked holdouts are not implemented",
    ),
    # ------------------------------------------------------------------ P12 decisions
    Capability(
        "decision.search",
        "Robust multi-objective policy search",
        "reserved",
        "P12",
        "reva.decision.optimise",
        (
            "utility and constraints explicit in a DecisionSpec",
            "frontier robust across scenarios and models",
            "external intervention tests and distributional reporting",
        ),
        reason="no validated decision-relevant model set or robust optimiser; prediction does not choose values",
        interface="reva.extensions.decision",
    ),
    Capability(
        "decision.multi_actor",
        "Multi-actor reaction models",
        "reserved",
        "P12",
        "reva.decision.reactions",
        ("bounded two-actor test solved", "multiple equilibria retained", "no unsupported equilibrium claim"),
        reason="no reaction model or equilibrium solver has been validated",
        interface="reva.extensions.decision",
    ),
    Capability(
        "domains.ecological_social",
        "Ecological, health, governance and language domain plugins",
        "reserved",
        "P12",
        "reva.models.ecological",
        ("concepts, sources, model/measurement contract, tests and a benefiting task per domain",),
        reason="no planetary domain beyond the crude-exposure vertical slice has entered production",
    ),
    # ------------------------------------------------------------------ later
    Capability(
        "query.ask_reva",
        "Conversational 'Ask REVA' bounded query compiler",
        "reserved",
        "unscheduled",
        "reva.api",
        (
            "prose compiled only into a validated QuerySpec",
            "deterministic tools only; model applicability cannot be bypassed",
            "no prose-to-executable-code path",
        ),
        reason="later capability; V1 has no LLM budget and exposes typed QuerySpec/ScenarioSpec only",
    ),
)


def _sandbox_configured(settings: Any | None) -> bool:
    if settings is not None:
        value = getattr(settings, "reva_sandbox_endpoint", None)
    else:
        value = os.environ.get(SANDBOX_ENDPOINT_ENV)
    return bool(value and str(value).strip())


def catalogue(settings: Any | None = None) -> tuple[Capability, ...]:
    """Return the catalogue, resolving configuration-dependent statuses.

    With a sandbox endpoint configured the runner moves from ``blocked`` to
    ``reserved`` — never ``implemented``: no runner client has passed the P11
    attack gates in this build.
    """
    out: list[Capability] = []
    for cap in _BASE:
        if cap.capability_id == "autonomy.external_sandbox_runner" and _sandbox_configured(settings):
            cap = replace(
                cap,
                status="reserved",
                reason="an external runner endpoint is configured, but no runner client has passed the P11 "
                "privilege/egress/resource attack gates; generated code remains unexecuted",
            )
        out.append(cap)
    return tuple(out)


def get(capability_id: str, settings: Any | None = None) -> Capability | None:
    for cap in catalogue(settings):
        if cap.capability_id == capability_id:
            return cap
    return None


def not_available(capability_id: str, settings: Any | None = None) -> CapabilityNotAvailable | None:
    """Typed unavailable result, or ``None`` when the capability is implemented."""
    cap = get(capability_id, settings)
    if cap is None:
        return CapabilityNotAvailable(
            capability_id=capability_id,
            status="unknown",
            phase=None,
            reason="capability is not declared in the catalogue",
            required_evidence=(),
        )
    if cap.available:
        return None
    assert cap.reason is not None and cap.status != "implemented"
    return CapabilityNotAvailable(
        capability_id=cap.capability_id,
        status=cap.status,
        phase=cap.phase,
        reason=cap.reason,
        required_evidence=cap.required_evidence,
    )


def require(capability_id: str, settings: Any | None = None, **context: Any) -> Capability:
    """Return the implemented capability or raise a structured :class:`CapabilityGap`.

    ``context`` (JSON-safe values) is attached to the error details, e.g. the
    request digest that hit the gap.
    """
    gap = not_available(capability_id, settings)
    if gap is None:
        cap = get(capability_id, settings)
        assert cap is not None
        return cap
    err = gap.to_error()
    if context:
        err.details["context"] = context
    cap = get(capability_id, settings)
    if cap is not None:
        err.details["owner_module"] = cap.owner_module
        err.details["interface"] = cap.interface
        err.details["available_instead"] = list(cap.available_instead)
    raise err


def as_dict(settings: Any | None = None) -> dict[str, Any]:
    """JSON-serialisable catalogue for API exposure."""
    caps = [c.as_dict() for c in catalogue(settings)]
    body = {"schema": CATALOGUE_SCHEMA, "catalogue_version": CATALOGUE_VERSION, "capabilities": caps}
    counts = {s: sum(1 for c in caps if c["status"] == s) for s in ("implemented", "reserved", "blocked")}
    return {**body, "summary": counts, "digest": logical_hash(body)}
